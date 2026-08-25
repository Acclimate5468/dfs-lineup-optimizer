"""Internal export formatter for Optimizer v1 runs.

Realizes ``docs/EXPORT_RUN_LOG_V1_DESIGN.md`` §3 (export formats), §4
(run-log fields), and §5 Option A (in-memory build, no DB write, no
file write). Pure, side-effect-free formatting:

- No DB access.
- No file writes.
- No persistence.
- Not a DraftKings upload file.

This module is the C.2 internal-formatter slice. Per the user's slice
prompt it intentionally collapses the design's three-module split
(``run_log_builder`` / ``run_log_formatters`` / ``export_service``,
design §8) into a single pure module; the orchestration service
(gate re-read + solver invocation + §7 validation) is deferred to a
later slice.

Public surface:

- :class:`ExportRunMetadata` — caller-supplied run metadata.
- :class:`InternalExportBundle` — combined run + lineup payload ready
  for serialization.
- :func:`build_internal_export_bundle` — pure builder.
- :func:`format_lineups_csv` / :func:`format_lineups_json` /
  :func:`format_markdown_summary` — three pure formatters returning
  ``bytes`` suitable for ``st.download_button``.

Per design §4.1 / ``docs/DEVELOPMENT_NOTES.md`` §7 the formatters must never embed
raw uploaded salary or odds CSV rows. The dataclasses below only
carry the persisted ``Fighter`` / ``Salary`` / projection / fight
group fields the app already surfaces in the UI, plus the gate
snapshot the caller chooses to pass in.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass

from src.optimizer.lineup_solver import SolveResult


INTERNAL_EXPORT_WARNING: str = (
    "Internal research export only. Not a DraftKings upload file. "
    "Do not commit. Do not upload to DraftKings or any external service."
)

CSV_HEADERS: tuple[str, ...] = (
    "run_id",
    "slate_id",
    "lineup_index",
    "roster_slot",
    "fighter_name",
    "dk_salary",
    "default_projection",
    "fight_group_id",
)

# Number of fighter columns in the wide CSV (UFC DK Classic roster size).
WIDE_CSV_FIGHTER_COLUMNS: int = 6

WIDE_CSV_HEADERS: tuple[str, ...] = (
    "run_id",
    "slate_id",
    "lineup_index",
    *(
        f"fighter_{slot}"
        for slot in range(1, WIDE_CSV_FIGHTER_COLUMNS + 1)
    ),
    "total_salary",
    "total_projection",
    "num_fighters",
)


@dataclass(frozen=True)
class ManualReviewSnapshot:
    """Snapshot of the Manual Review gate at solve time (design §4)."""

    ready: bool
    status: str
    completed_at_utc: str | None
    blocking_count: int
    warning_count: int
    informational_count: int


@dataclass(frozen=True)
class ExcludedFighterEntry:
    """One pool-exclusion diagnostic row (design §3.2 / §4).

    Only the persisted fighter name and the short reason string are
    carried — no raw odds payload, no raw salary payload, no
    ``fighter_id`` (design §4.1).
    """

    name: str
    reason: str


@dataclass(frozen=True)
class ExportDiagnostics:
    """Optimizer pool diagnostics carried into the run log (design §4)."""

    pool_size: int
    excluded: tuple[ExcludedFighterEntry, ...] = ()


@dataclass(frozen=True)
class ExportRunMetadata:
    """Caller-supplied context for a single export run (design §4).

    The optimizer-derived fields (status, reason, ``n_lineups_generated``,
    lineups) are filled in by :func:`build_internal_export_bundle` from
    the :class:`SolveResult`; this dataclass carries only the values
    the caller already knows when invoking the formatter.

    ``slate_*`` and ``manual_review`` are optional so the formatter
    can be exercised with a synthetic ``SolveResult`` (tests, smoke
    runs) without forcing the caller to fabricate slate or gate
    state.
    """

    run_id: str
    generated_at_utc: str
    n_lineups_requested: int
    slate_id: int | None = None
    slate_name: str | None = None
    slate_event_date: str | None = None
    manual_review: ManualReviewSnapshot | None = None


@dataclass(frozen=True)
class BundleFighter:
    """One fighter row inside an exported lineup (design §3.1 / §3.2)."""

    fighter_name: str
    dk_salary: int
    default_projection: float
    fight_group_id: int | None


@dataclass(frozen=True)
class BundleLineup:
    """One lineup as it will appear in the export (design §3.2)."""

    lineup_index: int
    total_salary: int
    total_projection: float
    fighters: tuple[BundleFighter, ...]


@dataclass(frozen=True)
class InternalExportBundle:
    """Serializable view of a single optimizer run.

    Combines the caller-supplied :class:`ExportRunMetadata` with the
    optimizer-derived status, reason, and lineup rows, plus the
    research-only :data:`INTERNAL_EXPORT_WARNING`. The three
    ``format_*`` functions each turn an instance of this dataclass
    into ``bytes`` ready for ``st.download_button``.
    """

    metadata: ExportRunMetadata
    optimizer_status: str
    optimizer_reason: str
    n_lineups_generated: int
    lineups: tuple[BundleLineup, ...]
    diagnostics: ExportDiagnostics | None = None
    warning: str = INTERNAL_EXPORT_WARNING


def build_internal_export_bundle(
    solve_result: SolveResult,
    *,
    metadata: ExportRunMetadata,
    fighter_name_by_id: Mapping[int, str],
    fighter_salary_by_id: Mapping[int, int],
    fighter_projection_by_id: Mapping[int, float] | None = None,
    fighter_fight_group_by_id: Mapping[int, int | None] | None = None,
    diagnostics: ExportDiagnostics | None = None,
) -> InternalExportBundle:
    """Build a :class:`InternalExportBundle` from a solver output.

    Pure: no DB access, no I/O. The input ``solve_result`` is not
    mutated and the supplied mappings are read-only.

    For each :class:`Lineup` in ``solve_result.lineups``, fighters are
    rendered in the lineup's ``fighter_ids`` order (the solver
    already sorts them ascending, so the export inherits a
    deterministic per-lineup ordering). Fighter name / salary /
    projection / fight-group fields are looked up by id in the
    caller-supplied mappings; a missing fighter id is rendered as
    ``#<id>`` for the name and ``0`` for the salary so the export is
    never silently truncated.

    The optimizer's per-lineup ``total_salary`` and ``total_projection``
    are propagated verbatim from the :class:`Lineup` so the exported
    totals match the solver's contract (design §4 lineups[].total_*).

    Empty ``solve_result.lineups`` (``gate_blocked`` /
    ``infeasible_*`` per the optimizer-service contract) yields a
    bundle with ``lineups=()`` and ``n_lineups_generated=0`` — the
    diagnostics-only export shape (design §7 rule 5).
    """
    proj_map: Mapping[int, float] = (
        fighter_projection_by_id
        if fighter_projection_by_id is not None
        else {}
    )
    fg_map: Mapping[int, int | None] = (
        fighter_fight_group_by_id
        if fighter_fight_group_by_id is not None
        else {}
    )

    bundle_lineups: list[BundleLineup] = []
    for idx, lineup in enumerate(solve_result.lineups, start=1):
        fighters: list[BundleFighter] = []
        for fid in lineup.fighter_ids:
            fid_int = int(fid)
            fighters.append(
                BundleFighter(
                    fighter_name=str(
                        fighter_name_by_id.get(fid_int, f"#{fid_int}")
                    ),
                    dk_salary=int(fighter_salary_by_id.get(fid_int, 0)),
                    default_projection=float(proj_map.get(fid_int, 0.0)),
                    fight_group_id=fg_map.get(fid_int),
                )
            )
        bundle_lineups.append(
            BundleLineup(
                lineup_index=idx,
                total_salary=int(lineup.total_salary),
                total_projection=float(lineup.total_projection),
                fighters=tuple(fighters),
            )
        )

    return InternalExportBundle(
        metadata=metadata,
        optimizer_status=str(solve_result.status),
        optimizer_reason=str(solve_result.reason),
        n_lineups_generated=len(solve_result.lineups),
        lineups=tuple(bundle_lineups),
        diagnostics=diagnostics,
        warning=INTERNAL_EXPORT_WARNING,
    )


def format_lineups_csv(bundle: InternalExportBundle) -> bytes:
    """Render the bundle as the design §3.1 internal-research CSV.

    Layout:

    - Leading ``#`` comment line carrying
      :data:`INTERNAL_EXPORT_WARNING` so the file is self-labelling
      outside the app (design §11 risk #5). Most CSV readers either
      show it as a one-cell row or skip it when configured with a
      comment prefix.
    - Header row in the §3.1 column order:
      ``run_id, slate_id, lineup_index, roster_slot, fighter_name,
      dk_salary, default_projection, fight_group_id``.
    - One data row per fighter per lineup. ``default_projection`` is
      rendered to four decimal places; ``fight_group_id`` is empty
      when ``None``.

    The header row uses snake_case names that the DraftKings UFC
    Classic upload schema does not, so the file cannot be confused
    for a contest entry (design §3.1 / §11 risk #1).

    Empty ``bundle.lineups`` (diagnostics-only run) yields warning +
    header only (design §7 rule 5). Special characters (commas,
    quotes, newlines) in fighter names are quoted by the ``csv``
    module per RFC 4180.
    """
    buf = io.StringIO()
    buf.write(f"# {INTERNAL_EXPORT_WARNING}\r\n")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)

    md = bundle.metadata
    run_id = md.run_id
    slate_id = md.slate_id if md.slate_id is not None else ""

    for lineup in bundle.lineups:
        for slot, fighter in enumerate(lineup.fighters, start=1):
            writer.writerow(
                [
                    run_id,
                    slate_id,
                    lineup.lineup_index,
                    slot,
                    fighter.fighter_name,
                    int(fighter.dk_salary),
                    f"{float(fighter.default_projection):.4f}",
                    (
                        fighter.fight_group_id
                        if fighter.fight_group_id is not None
                        else ""
                    ),
                ]
            )
    return buf.getvalue().encode("utf-8")


def format_lineups_wide_csv(bundle: InternalExportBundle) -> bytes:
    """Render the bundle as the design §3.1.1 wide (per-lineup) CSV.

    This is the **optional** fourth export format. It is an additional,
    independently downloadable file — it never replaces the §3.1 tidy
    CSV produced by :func:`format_lineups_csv`, whose shape is unchanged.

    Layout (design §3.1.1):

    - Leading ``#`` comment line carrying
      :data:`INTERNAL_EXPORT_WARNING`, identical to the tidy CSV, so
      the file is self-labelling outside the app (design §11 risk #5).
    - Header row in the §3.1.1 column order:
      ``run_id, slate_id, lineup_index, fighter_1 ... fighter_6,
      total_salary, total_projection, num_fighters``.
    - One data row per lineup (not per fighter). ``fighter_1..6`` hold
      the lineup's fighter names in the solver's per-lineup order (the
      same ascending order the tidy CSV uses for ``roster_slot``);
      unused slots are empty. ``total_projection`` is rendered to two
      decimal places. ``num_fighters`` is the count of fighters
      actually present in the row, so a truncated/partial lineup is
      self-evident and ``wide row == full roster`` is never assumed
      (design §3.1.1 / §11 risk #8).

    The ``fighter_N`` / ``total_*`` / ``num_fighters`` header names are
    snake_case and deliberately do not match the DraftKings upload
    vocabulary (no "F" position labels, no ``Entry ID`` / ``Contest ID``
    / ``Contest Name`` / ``Lineup Name`` columns, no DK numeric
    fighter-id column), so the file cannot be confused for a contest
    entry even though it is column-oriented (design §3.1.1 / §11
    risk #1 / #9).

    Empty ``bundle.lineups`` (diagnostics-only run) yields warning +
    header only (design §3.1.1 / §7 rule 5). Special characters
    (commas, quotes, newlines) in fighter names are quoted by the
    ``csv`` module per RFC 4180.
    """
    buf = io.StringIO()
    buf.write(f"# {INTERNAL_EXPORT_WARNING}\r\n")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(WIDE_CSV_HEADERS)

    md = bundle.metadata
    run_id = md.run_id
    slate_id = md.slate_id if md.slate_id is not None else ""

    for lineup in bundle.lineups:
        names = [f.fighter_name for f in lineup.fighters]
        fighter_cells = [
            names[i] if i < len(names) else ""
            for i in range(WIDE_CSV_FIGHTER_COLUMNS)
        ]
        writer.writerow(
            [
                run_id,
                slate_id,
                lineup.lineup_index,
                *fighter_cells,
                int(lineup.total_salary),
                f"{float(lineup.total_projection):.2f}",
                len(lineup.fighters),
            ]
        )
    return buf.getvalue().encode("utf-8")


def _manual_review_dict(snap: ManualReviewSnapshot | None) -> dict | None:
    if snap is None:
        return None
    return {
        "ready": bool(snap.ready),
        "status": snap.status,
        "completed_at_utc": snap.completed_at_utc,
        "blocking_count": int(snap.blocking_count),
        "warning_count": int(snap.warning_count),
        "informational_count": int(snap.informational_count),
    }


def _diagnostics_dict(diag: ExportDiagnostics | None) -> dict:
    if diag is None:
        return {"pool_size": 0, "excluded": []}
    return {
        "pool_size": int(diag.pool_size),
        "excluded": [
            {"name": e.name, "reason": e.reason} for e in diag.excluded
        ],
    }


def format_lineups_json(bundle: InternalExportBundle) -> bytes:
    """Render the bundle as the design §3.2 internal-research JSON.

    The output is byte-stable for any given input:

    - Top-level keys are emitted in the design §3.2 order
      (``run_id``, ``generated_at_utc``, ``slate``, ``optimizer``,
      ``manual_review``, ``lineups``, ``diagnostics``, ``warning``).
    - Nested dicts use the same hand-specified order.
    - Lineups follow the bundle's order (preserved from the solver).
    - ``json.dumps`` is called with ``sort_keys=False`` so the
      hand-specified key order survives serialization, and
      ``ensure_ascii=False`` so unicode fighter names round-trip
      without escape noise.
    - Two-space indentation makes the file diffable across runs.
    """
    md = bundle.metadata
    payload = {
        "run_id": md.run_id,
        "generated_at_utc": md.generated_at_utc,
        "slate": {
            "id": md.slate_id,
            "name": md.slate_name,
            "event_date": md.slate_event_date,
        },
        "optimizer": {
            "status": bundle.optimizer_status,
            "n_lineups_requested": int(md.n_lineups_requested),
            "n_lineups_generated": int(bundle.n_lineups_generated),
            "reason": bundle.optimizer_reason,
        },
        "manual_review": _manual_review_dict(md.manual_review),
        "lineups": [
            {
                "lineup_index": int(lu.lineup_index),
                "total_salary": int(lu.total_salary),
                "total_projection": float(lu.total_projection),
                "fighters": [
                    {
                        "fighter_name": f.fighter_name,
                        "dk_salary": int(f.dk_salary),
                        "default_projection": float(f.default_projection),
                        "fight_group_id": f.fight_group_id,
                    }
                    for f in lu.fighters
                ],
            }
            for lu in bundle.lineups
        ],
        "diagnostics": _diagnostics_dict(bundle.diagnostics),
        "warning": bundle.warning,
    }
    return json.dumps(
        payload,
        indent=2,
        sort_keys=False,
        ensure_ascii=False,
    ).encode("utf-8")


def _md_escape_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace(
        "\r", " "
    ).replace("\n", " ")


def format_markdown_summary(bundle: InternalExportBundle) -> bytes:
    """Render the bundle as the design §3.3 internal-research Markdown.

    Sections (in order):

    1. ``# Optimizer run {run_id}`` title.
    2. Bullet list with generated timestamp, slate, optimizer status
       + reason, lineups requested vs. generated, and the Manual
       Review snapshot (omitted gracefully when ``metadata.manual_review``
       is ``None``).
    3. One ``## Lineup N`` section per lineup with a fighter table
       and a totals line.
    4. ``## Diagnostics`` section when ``bundle.diagnostics`` is
       supplied.
    5. ``> Internal research export only. ...`` blockquote so the
       file is self-labelling outside the app.

    Empty ``bundle.lineups`` (diagnostics-only run) skips the per-
    lineup tables but keeps the header bullets, diagnostics (if any)
    and the trailing blockquote (design §7 rule 5).

    Pipe characters in fighter names are backslash-escaped so they
    don't break Markdown table rendering.
    """
    md = bundle.metadata
    lines: list[str] = []
    lines.append(f"# Optimizer run {md.run_id}")
    lines.append("")
    lines.append(f"- Generated: {md.generated_at_utc}")

    if (
        md.slate_id is not None
        or md.slate_name
        or md.slate_event_date
    ):
        sid = f"#{md.slate_id}" if md.slate_id is not None else "#?"
        name = md.slate_name or "(unnamed slate)"
        date = md.slate_event_date or "?"
        lines.append(f"- Slate: {sid} — {name} ({date})")
    else:
        lines.append("- Slate: (not supplied)")

    reason = bundle.optimizer_reason or "—"
    lines.append(
        f"- Optimizer status: `{bundle.optimizer_status}` — {reason}"
    )
    lines.append(
        f"- Lineups requested: {md.n_lineups_requested}; "
        f"generated: {bundle.n_lineups_generated}"
    )

    if md.manual_review is not None:
        mr = md.manual_review
        completed = mr.completed_at_utc or "—"
        lines.append(
            f"- Manual Review: {mr.status} (gate ready: "
            f"{'yes' if mr.ready else 'no'}; completed at: {completed}; "
            f"blocking: {mr.blocking_count}, warning: {mr.warning_count}, "
            f"informational: {mr.informational_count})"
        )
    else:
        lines.append("- Manual Review: (snapshot not supplied)")

    for lu in bundle.lineups:
        lines.append("")
        lines.append(f"## Lineup {lu.lineup_index}")
        lines.append("| Fighter | Salary | Projection | Fight group |")
        lines.append("| --- | ---:| ---:| ---:|")
        for f in lu.fighters:
            fg = f.fight_group_id if f.fight_group_id is not None else ""
            lines.append(
                f"| {_md_escape_cell(f.fighter_name)} | "
                f"{int(f.dk_salary)} | "
                f"{float(f.default_projection):.2f} | "
                f"{fg} |"
            )
        lines.append(
            f"Total salary: ${int(lu.total_salary):,} · "
            f"Total projection: {float(lu.total_projection):.2f}"
        )

    diag = bundle.diagnostics
    if diag is not None:
        lines.append("")
        lines.append("## Diagnostics")
        lines.append(f"Pool size: {int(diag.pool_size)}")
        if diag.excluded:
            lines.append("Excluded:")
            for e in diag.excluded:
                lines.append(f"- {e.name} — {e.reason}")
        else:
            lines.append("Excluded: (none)")

    lines.append("")
    lines.append(f"> {bundle.warning}")
    lines.append("")
    return "\n".join(lines).encode("utf-8")
