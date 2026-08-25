"""Tests for the Export / Run Log v1 C.2 internal formatter.

Covers ``src/exports/internal_export.py`` per
``docs/EXPORT_RUN_LOG_V1_DESIGN.md`` §3 (export formats), §4 (run-log
fields), §5 Option A (in-memory only — no DB write, no file write),
and the §11 risk list (no DK upload schema, no raw upload data
passthrough).

These tests are intentionally pure: they construct
:class:`SolveResult` and :class:`Lineup` values directly and never
touch the DB or the filesystem.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import pytest

from src.exports.internal_export import (
    CSV_HEADERS,
    INTERNAL_EXPORT_WARNING,
    WIDE_CSV_FIGHTER_COLUMNS,
    WIDE_CSV_HEADERS,
    BundleFighter,
    BundleLineup,
    ExcludedFighterEntry,
    ExportDiagnostics,
    ExportRunMetadata,
    InternalExportBundle,
    ManualReviewSnapshot,
    build_internal_export_bundle,
    format_lineups_csv,
    format_lineups_json,
    format_lineups_wide_csv,
    format_markdown_summary,
)
from src.optimizer.lineup_solver import (
    Lineup,
    SolveResult,
    STATUS_INFEASIBLE_POOL_TOO_SMALL,
    STATUS_OK,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lineup(
    fighter_ids: tuple[int, ...],
    *,
    total_salary: int,
    total_projection: float,
) -> Lineup:
    return Lineup(
        fighter_ids=fighter_ids,
        total_salary=total_salary,
        total_projection=total_projection,
    )


def _solve_result(
    *,
    slate_id: int = 7,
    status: str = STATUS_OK,
    reason: str = "",
    lineups: tuple[Lineup, ...] = (),
) -> SolveResult:
    return SolveResult(
        slate_id=slate_id,
        status=status,
        lineups=lineups,
        reason=reason,
    )


def _metadata(
    *,
    run_id: str = "2026-05-28T14-32-11Z-slate7",
    generated_at_utc: str = "2026-05-28T14:32:11Z",
    n_lineups_requested: int = 1,
    slate_id: int | None = 7,
    slate_name: str | None = "UFC 999: Jones vs. Aspinall",
    slate_event_date: str | None = "2026-05-28",
    manual_review: ManualReviewSnapshot | None = None,
) -> ExportRunMetadata:
    return ExportRunMetadata(
        run_id=run_id,
        generated_at_utc=generated_at_utc,
        n_lineups_requested=n_lineups_requested,
        slate_id=slate_id,
        slate_name=slate_name,
        slate_event_date=slate_event_date,
        manual_review=manual_review,
    )


def _six_fighter_lineup() -> Lineup:
    return _lineup(
        fighter_ids=(1, 2, 3, 4, 5, 6),
        total_salary=49000,
        total_projection=87.6543,
    )


def _name_salary_proj_fg_maps():
    name_by_id = {
        1: "Alice Adams",
        2: "Bob Brown",
        3: "Carlos Castillo",
        4: "Dmitri Drago",
        5: "Eva Eriksson",
        6: "Fei Fong",
    }
    salary_by_id = {
        1: 9000,
        2: 8500,
        3: 8000,
        4: 8000,
        5: 7800,
        6: 7700,
    }
    projection_by_id = {
        1: 65.1234,
        2: 60.5,
        3: 55.0,
        4: 50.25,
        5: 45.9,
        6: 42.1,
    }
    fight_group_by_id: dict[int, int | None] = {
        1: 101,
        2: 101,
        3: 102,
        4: 102,
        5: 103,
        6: None,
    }
    return name_by_id, salary_by_id, projection_by_id, fight_group_by_id


def _populated_bundle(
    *,
    manual_review: ManualReviewSnapshot | None = None,
    diagnostics: ExportDiagnostics | None = None,
    lineups: tuple[Lineup, ...] | None = None,
) -> InternalExportBundle:
    name_by_id, salary_by_id, proj_by_id, fg_by_id = (
        _name_salary_proj_fg_maps()
    )
    if lineups is None:
        lineups = (_six_fighter_lineup(),)
    result = _solve_result(lineups=lineups)
    md = _metadata(manual_review=manual_review)
    return build_internal_export_bundle(
        result,
        metadata=md,
        fighter_name_by_id=name_by_id,
        fighter_salary_by_id=salary_by_id,
        fighter_projection_by_id=proj_by_id,
        fighter_fight_group_by_id=fg_by_id,
        diagnostics=diagnostics,
    )


def _parse_csv(blob: bytes) -> tuple[str, list[str], list[list[str]]]:
    """Return ``(comment_line, header_row, data_rows)`` from a CSV blob.

    The internal-export CSV writes its warning as a leading ``#``
    comment line, the official §3.1 header row, and zero or more
    data rows. This helper splits them apart so individual tests can
    pin each piece independently.
    """
    text = blob.decode("utf-8")
    first_newline = text.index("\n")
    comment = text[:first_newline].rstrip("\r")
    remainder = text[first_newline + 1 :]
    reader = csv.reader(io.StringIO(remainder))
    rows = list(reader)
    assert rows, "CSV produced no header row"
    return comment, rows[0], rows[1:]


# ---------------------------------------------------------------------------
# Builder behavior
# ---------------------------------------------------------------------------


def test_build_bundle_propagates_solve_result_status_and_reason():
    result = _solve_result(
        status=STATUS_OK,
        reason="",
        lineups=(_six_fighter_lineup(),),
    )
    name_by_id, salary_by_id, *_ = _name_salary_proj_fg_maps()
    bundle = build_internal_export_bundle(
        result,
        metadata=_metadata(),
        fighter_name_by_id=name_by_id,
        fighter_salary_by_id=salary_by_id,
    )
    assert bundle.optimizer_status == STATUS_OK
    assert bundle.optimizer_reason == ""
    assert bundle.n_lineups_generated == 1
    assert bundle.warning == INTERNAL_EXPORT_WARNING


def test_build_bundle_totals_match_solve_result_input():
    lineup = _lineup(
        fighter_ids=(1, 2, 3, 4, 5, 6),
        total_salary=49321,
        total_projection=88.1234,
    )
    result = _solve_result(lineups=(lineup,))
    name_by_id, salary_by_id, proj_by_id, fg_by_id = (
        _name_salary_proj_fg_maps()
    )
    bundle = build_internal_export_bundle(
        result,
        metadata=_metadata(),
        fighter_name_by_id=name_by_id,
        fighter_salary_by_id=salary_by_id,
        fighter_projection_by_id=proj_by_id,
        fighter_fight_group_by_id=fg_by_id,
    )
    assert len(bundle.lineups) == 1
    only = bundle.lineups[0]
    assert only.total_salary == 49321
    assert only.total_projection == pytest.approx(88.1234)


def test_build_bundle_falls_back_for_missing_fighter_id():
    lineup = _lineup(
        fighter_ids=(1, 2, 3, 4, 5, 99),
        total_salary=40000,
        total_projection=70.0,
    )
    result = _solve_result(lineups=(lineup,))
    name_by_id, salary_by_id, proj_by_id, fg_by_id = (
        _name_salary_proj_fg_maps()
    )
    bundle = build_internal_export_bundle(
        result,
        metadata=_metadata(),
        fighter_name_by_id=name_by_id,
        fighter_salary_by_id=salary_by_id,
        fighter_projection_by_id=proj_by_id,
        fighter_fight_group_by_id=fg_by_id,
    )
    assert bundle.lineups[0].fighters[-1].fighter_name == "#99"
    assert bundle.lineups[0].fighters[-1].dk_salary == 0
    assert bundle.lineups[0].fighters[-1].fight_group_id is None


# ---------------------------------------------------------------------------
# CSV format
# ---------------------------------------------------------------------------


def test_csv_starts_with_warning_comment_and_header_row():
    bundle = _populated_bundle()
    blob = format_lineups_csv(bundle)
    comment, header, rows = _parse_csv(blob)

    assert comment.startswith("#")
    assert INTERNAL_EXPORT_WARNING in comment
    assert "Internal research export only" in comment
    assert "Not a DraftKings upload file" in comment

    assert tuple(header) == CSV_HEADERS
    assert len(rows) == 6


def test_csv_data_rows_match_lineup_fighters():
    bundle = _populated_bundle()
    blob = format_lineups_csv(bundle)
    _, header, rows = _parse_csv(blob)

    name_by_id, salary_by_id, proj_by_id, fg_by_id = (
        _name_salary_proj_fg_maps()
    )

    assert tuple(header) == CSV_HEADERS
    for slot, row in enumerate(rows, start=1):
        as_dict = dict(zip(header, row))
        fid = slot  # fighter_ids 1..6, sorted ascending
        assert as_dict["run_id"] == bundle.metadata.run_id
        assert as_dict["slate_id"] == str(bundle.metadata.slate_id)
        assert as_dict["lineup_index"] == "1"
        assert as_dict["roster_slot"] == str(slot)
        assert as_dict["fighter_name"] == name_by_id[fid]
        assert as_dict["dk_salary"] == str(salary_by_id[fid])
        assert as_dict["default_projection"] == f"{proj_by_id[fid]:.4f}"
        expected_fg = fg_by_id[fid]
        assert as_dict["fight_group_id"] == (
            "" if expected_fg is None else str(expected_fg)
        )

    # Per-fighter salary rows reconstruct the lineup total.
    reconstructed_salary = sum(int(r[header.index("dk_salary")]) for r in rows)
    assert reconstructed_salary == bundle.lineups[0].total_salary


def test_csv_is_not_dk_upload_compatible():
    bundle = _populated_bundle()
    blob = format_lineups_csv(bundle)
    comment, header, rows = _parse_csv(blob)

    # The official DK UFC Classic upload schema is column-based with
    # one row per lineup and uses headers like ``Entry ID`` /
    # ``Contest Name`` / ``F``. None of those may appear in the
    # internal-research CSV's header or data rows (design §3.1 /
    # §11 risk #1). The leading research-only warning intentionally
    # uses the word "DraftKings" to flag the file as NOT a contest
    # entry, so it is excluded from the negative check.
    data_text = "\n".join([",".join(header)] + [",".join(r) for r in rows])
    forbidden_substrings = (
        "Entry ID",
        "Contest Name",
        "Contest ID",
        "DraftKings",
        "Lineup Name",
    )
    for needle in forbidden_substrings:
        assert needle not in data_text, (
            f"unexpected DK upload marker in CSV data: {needle!r}"
        )

    # Comment line must still carry the research-only warning.
    assert INTERNAL_EXPORT_WARNING in comment

    # The header row must be the snake_case §3.1 schema, not a
    # whitespace-delimited DK position list.
    assert tuple(header) == CSV_HEADERS
    for col in header:
        assert col == col.lower(), f"header column not snake_case: {col!r}"
        assert " " not in col, f"DK-style spaced header: {col!r}"


def test_csv_handles_special_characters_in_fighter_names():
    name_by_id = {
        1: 'Khabib "The Eagle", Nurmagomedov',
        2: "Conor\nMcGregor",
        3: "Ronda, Rousey",
        4: "Plain Name",
        5: "Pipe|Name",
        6: "Quoted'Name",
    }
    salary_by_id = {fid: 8000 for fid in name_by_id}
    result = _solve_result(
        lineups=(
            _lineup(
                fighter_ids=tuple(sorted(name_by_id)),
                total_salary=48000,
                total_projection=70.0,
            ),
        )
    )
    bundle = build_internal_export_bundle(
        result,
        metadata=_metadata(),
        fighter_name_by_id=name_by_id,
        fighter_salary_by_id=salary_by_id,
    )
    blob = format_lineups_csv(bundle)

    # Round-trip via csv.reader and confirm each name comes back
    # bit-for-bit identical.
    _, header, rows = _parse_csv(blob)
    name_col = header.index("fighter_name")
    decoded_names = [r[name_col] for r in rows]
    assert decoded_names == [name_by_id[fid] for fid in sorted(name_by_id)]


def test_csv_empty_solve_result_emits_warning_and_header_only():
    result = _solve_result(
        status=STATUS_INFEASIBLE_POOL_TOO_SMALL,
        reason="pool has 3 eligible fighter(s); need at least 6",
        lineups=(),
    )
    bundle = build_internal_export_bundle(
        result,
        metadata=_metadata(),
        fighter_name_by_id={},
        fighter_salary_by_id={},
    )
    blob = format_lineups_csv(bundle)
    comment, header, rows = _parse_csv(blob)
    assert INTERNAL_EXPORT_WARNING in comment
    assert tuple(header) == CSV_HEADERS
    assert rows == []


# ---------------------------------------------------------------------------
# Wide CSV format (design §3.1.1 — optional, one row per lineup)
# ---------------------------------------------------------------------------


def _wide_bundle(n_lineups: int = 3) -> InternalExportBundle:
    """Bundle with ``n_lineups`` full six-fighter lineups (ids 1..6).

    Each lineup reuses the same fighter set; the wide CSV is row-per-
    lineup, so what matters here is the per-lineup row shape and count,
    not fighter-set distinctness.
    """
    lineups = tuple(
        _lineup(
            fighter_ids=(1, 2, 3, 4, 5, 6),
            total_salary=49000 + idx,
            total_projection=87.6543 + idx,
        )
        for idx in range(n_lineups)
    )
    return _populated_bundle(lineups=lineups)


def test_wide_csv_starts_with_warning_comment_and_header_row():
    bundle = _populated_bundle()
    blob = format_lineups_wide_csv(bundle)
    comment, header, rows = _parse_csv(blob)

    assert comment.startswith("#")
    assert INTERNAL_EXPORT_WARNING in comment
    assert "Internal research export only" in comment
    assert "Not a DraftKings upload file" in comment

    assert tuple(header) == WIDE_CSV_HEADERS
    # One six-fighter lineup -> exactly one data row.
    assert len(rows) == 1


def test_wide_csv_one_row_per_lineup():
    bundle = _wide_bundle(n_lineups=3)
    blob = format_lineups_wide_csv(bundle)
    _, header, rows = _parse_csv(blob)

    name_by_id, _, _, _ = _name_salary_proj_fg_maps()
    expected_names = [name_by_id[fid] for fid in (1, 2, 3, 4, 5, 6)]

    assert tuple(header) == WIDE_CSV_HEADERS
    assert len(rows) == 3
    for i, row in enumerate(rows):
        as_dict = dict(zip(header, row))
        lu = bundle.lineups[i]
        assert as_dict["run_id"] == bundle.metadata.run_id
        assert as_dict["slate_id"] == str(bundle.metadata.slate_id)
        assert as_dict["lineup_index"] == str(lu.lineup_index)
        for slot in range(1, WIDE_CSV_FIGHTER_COLUMNS + 1):
            assert as_dict[f"fighter_{slot}"] == expected_names[slot - 1]
        assert as_dict["total_salary"] == str(lu.total_salary)
        assert as_dict["total_projection"] == f"{lu.total_projection:.2f}"
        assert as_dict["num_fighters"] == "6"


def test_wide_csv_partial_lineup_sets_num_fighters_and_pads():
    # A short lineup (4 fighters) must report num_fighters=4 and leave
    # the trailing fighter columns empty (design §3.1.1 / §11 risk #8).
    short = _lineup(
        fighter_ids=(1, 2, 3, 4),
        total_salary=30000,
        total_projection=40.0,
    )
    bundle = _populated_bundle(lineups=(short,))
    blob = format_lineups_wide_csv(bundle)
    _, header, rows = _parse_csv(blob)

    assert len(rows) == 1
    as_dict = dict(zip(header, rows[0]))
    assert as_dict["num_fighters"] == "4"
    assert as_dict["fighter_1"] != ""
    assert as_dict["fighter_4"] != ""
    assert as_dict["fighter_5"] == ""
    assert as_dict["fighter_6"] == ""


def test_wide_csv_is_not_dk_upload_compatible():
    bundle = _wide_bundle(n_lineups=2)
    blob = format_lineups_wide_csv(bundle)
    comment, header, rows = _parse_csv(blob)

    # The wide CSV is column-oriented and row-per-lineup, structurally
    # closer to the DK upload shape than the tidy CSV. It must still
    # avoid every DK upload marker (design §3.1.1 / §11 risk #1 / #9).
    data_text = "\n".join([",".join(header)] + [",".join(r) for r in rows])
    forbidden_substrings = (
        "Entry ID",
        "Contest Name",
        "Contest ID",
        "DraftKings",
        "Lineup Name",
    )
    for needle in forbidden_substrings:
        assert needle not in data_text, (
            f"unexpected DK upload marker in wide CSV data: {needle!r}"
        )

    assert INTERNAL_EXPORT_WARNING in comment

    assert tuple(header) == WIDE_CSV_HEADERS
    for col in header:
        assert col == col.lower(), f"header column not snake_case: {col!r}"
        assert " " not in col, f"DK-style spaced header: {col!r}"


def test_wide_csv_handles_special_characters_in_fighter_names():
    name_by_id = {
        1: 'Khabib "The Eagle", Nurmagomedov',
        2: "Conor\nMcGregor",
        3: "Ronda, Rousey",
        4: "Plain Name",
        5: "Pipe|Name",
        6: "Quoted'Name",
    }
    salary_by_id = {fid: 8000 for fid in name_by_id}
    result = _solve_result(
        lineups=(
            _lineup(
                fighter_ids=tuple(sorted(name_by_id)),
                total_salary=48000,
                total_projection=70.0,
            ),
        )
    )
    bundle = build_internal_export_bundle(
        result,
        metadata=_metadata(),
        fighter_name_by_id=name_by_id,
        fighter_salary_by_id=salary_by_id,
    )
    blob = format_lineups_wide_csv(bundle)

    _, header, rows = _parse_csv(blob)
    assert len(rows) == 1
    as_dict = dict(zip(header, rows[0]))
    for slot in range(1, WIDE_CSV_FIGHTER_COLUMNS + 1):
        assert as_dict[f"fighter_{slot}"] == name_by_id[slot]


def test_wide_csv_empty_solve_result_emits_warning_and_header_only():
    result = _solve_result(
        status=STATUS_INFEASIBLE_POOL_TOO_SMALL,
        reason="pool has 3 eligible fighter(s); need at least 6",
        lineups=(),
    )
    bundle = build_internal_export_bundle(
        result,
        metadata=_metadata(),
        fighter_name_by_id={},
        fighter_salary_by_id={},
    )
    blob = format_lineups_wide_csv(bundle)
    comment, header, rows = _parse_csv(blob)
    assert INTERNAL_EXPORT_WARNING in comment
    assert tuple(header) == WIDE_CSV_HEADERS
    assert rows == []


def test_tidy_csv_headers_unchanged_by_wide_addition():
    # Guard: adding the wide CSV must not alter the §3.1 tidy schema.
    assert CSV_HEADERS == (
        "run_id",
        "slate_id",
        "lineup_index",
        "roster_slot",
        "fighter_name",
        "dk_salary",
        "default_projection",
        "fight_group_id",
    )
    assert WIDE_CSV_HEADERS != CSV_HEADERS


# ---------------------------------------------------------------------------
# JSON format
# ---------------------------------------------------------------------------


def test_json_top_level_keys_and_order():
    bundle = _populated_bundle()
    blob = format_lineups_json(bundle)
    payload = json.loads(blob)

    expected_order = [
        "run_id",
        "generated_at_utc",
        "slate",
        "optimizer",
        "manual_review",
        "lineups",
        "diagnostics",
        "warning",
    ]
    assert list(payload.keys()) == expected_order


def test_json_includes_metadata_lineups_diagnostics_and_warning():
    diagnostics = ExportDiagnostics(
        pool_size=14,
        excluded=(
            ExcludedFighterEntry(
                name="Skip Fighter",
                reason="projection_status=missing_inputs:moneyline",
            ),
        ),
    )
    bundle = _populated_bundle(diagnostics=diagnostics)
    payload = json.loads(format_lineups_json(bundle))

    assert payload["run_id"] == bundle.metadata.run_id
    assert payload["generated_at_utc"] == bundle.metadata.generated_at_utc
    assert payload["slate"] == {
        "id": bundle.metadata.slate_id,
        "name": bundle.metadata.slate_name,
        "event_date": bundle.metadata.slate_event_date,
    }
    assert payload["optimizer"] == {
        "status": STATUS_OK,
        "n_lineups_requested": 1,
        "n_lineups_generated": 1,
        "reason": "",
    }
    assert payload["warning"] == INTERNAL_EXPORT_WARNING

    assert isinstance(payload["lineups"], list)
    assert len(payload["lineups"]) == 1
    only = payload["lineups"][0]
    assert only["lineup_index"] == 1
    assert only["total_salary"] == bundle.lineups[0].total_salary
    assert only["total_projection"] == pytest.approx(
        bundle.lineups[0].total_projection
    )
    assert len(only["fighters"]) == 6
    fighter_keys = set(only["fighters"][0].keys())
    assert fighter_keys == {
        "fighter_name",
        "dk_salary",
        "default_projection",
        "fight_group_id",
    }

    assert payload["diagnostics"]["pool_size"] == 14
    assert payload["diagnostics"]["excluded"] == [
        {
            "name": "Skip Fighter",
            "reason": "projection_status=missing_inputs:moneyline",
        },
    ]


def test_json_is_deterministic_across_repeated_calls():
    bundle = _populated_bundle(
        diagnostics=ExportDiagnostics(
            pool_size=10,
            excluded=(
                ExcludedFighterEntry(name="X", reason="missing dk_salary"),
                ExcludedFighterEntry(name="Y", reason="non-positive dk_salary=0"),
            ),
        )
    )
    a = format_lineups_json(bundle)
    b = format_lineups_json(bundle)
    assert a == b


def test_json_omits_dk_upload_schema_markers():
    bundle = _populated_bundle()
    text = format_lineups_json(bundle).decode("utf-8")
    for needle in (
        "Entry ID",
        "Contest Name",
        "Contest ID",
        "Lineup Name",
    ):
        assert needle not in text


def test_json_diagnostics_omitted_field_renders_empty_shape():
    bundle = _populated_bundle()
    payload = json.loads(format_lineups_json(bundle))
    assert payload["diagnostics"] == {"pool_size": 0, "excluded": []}


# ---------------------------------------------------------------------------
# Markdown format
# ---------------------------------------------------------------------------


def test_markdown_contains_slate_status_totals_and_warning():
    bundle = _populated_bundle()
    text = format_markdown_summary(bundle).decode("utf-8")

    assert text.startswith(f"# Optimizer run {bundle.metadata.run_id}")
    assert f"#{bundle.metadata.slate_id} — {bundle.metadata.slate_name}" in text
    assert f"({bundle.metadata.slate_event_date})" in text
    assert f"`{bundle.optimizer_status}`" in text
    assert "Lineups requested: 1; generated: 1" in text
    assert "## Lineup 1" in text
    assert (
        f"Total salary: ${bundle.lineups[0].total_salary:,}" in text
    )
    assert (
        f"Total projection: {bundle.lineups[0].total_projection:.2f}"
        in text
    )
    assert f"> {INTERNAL_EXPORT_WARNING}" in text


def test_markdown_includes_diagnostics_section_when_supplied():
    diagnostics = ExportDiagnostics(
        pool_size=11,
        excluded=(
            ExcludedFighterEntry(
                name="Sidelined Sam",
                reason="projection_status=non_projectable",
            ),
        ),
    )
    bundle = _populated_bundle(diagnostics=diagnostics)
    text = format_markdown_summary(bundle).decode("utf-8")
    assert "## Diagnostics" in text
    assert "Pool size: 11" in text
    assert (
        "- Sidelined Sam — projection_status=non_projectable" in text
    )


def test_markdown_omits_diagnostics_section_when_not_supplied():
    bundle = _populated_bundle()
    text = format_markdown_summary(bundle).decode("utf-8")
    assert "## Diagnostics" not in text


def test_markdown_escapes_pipes_in_fighter_names():
    name_by_id = {
        1: "A | B",
        2: "C",
        3: "D",
        4: "E",
        5: "F",
        6: "G",
    }
    salary_by_id = {fid: 8000 for fid in name_by_id}
    result = _solve_result(
        lineups=(
            _lineup(
                fighter_ids=tuple(sorted(name_by_id)),
                total_salary=48000,
                total_projection=70.0,
            ),
        )
    )
    bundle = build_internal_export_bundle(
        result,
        metadata=_metadata(),
        fighter_name_by_id=name_by_id,
        fighter_salary_by_id=salary_by_id,
    )
    text = format_markdown_summary(bundle).decode("utf-8")
    assert "A \\| B" in text
    assert "| A | B |" not in text


# ---------------------------------------------------------------------------
# Manual Review snapshot
# ---------------------------------------------------------------------------


def test_manual_review_snapshot_renders_in_json_and_markdown():
    snapshot = ManualReviewSnapshot(
        ready=True,
        status="reviewed",
        completed_at_utc="2026-05-28T13:45:00Z",
        blocking_count=0,
        warning_count=2,
        informational_count=3,
    )
    bundle = _populated_bundle(manual_review=snapshot)

    payload = json.loads(format_lineups_json(bundle))
    assert payload["manual_review"] == {
        "ready": True,
        "status": "reviewed",
        "completed_at_utc": "2026-05-28T13:45:00Z",
        "blocking_count": 0,
        "warning_count": 2,
        "informational_count": 3,
    }

    md = format_markdown_summary(bundle).decode("utf-8")
    assert "Manual Review: reviewed (gate ready: yes;" in md
    assert "completed at: 2026-05-28T13:45:00Z" in md
    assert "blocking: 0, warning: 2, informational: 3" in md


def test_manual_review_absent_renders_explicit_placeholder():
    bundle = _populated_bundle(manual_review=None)
    payload = json.loads(format_lineups_json(bundle))
    assert payload["manual_review"] is None

    md = format_markdown_summary(bundle).decode("utf-8")
    assert "Manual Review: (snapshot not supplied)" in md


# ---------------------------------------------------------------------------
# Empty / diagnostics-only exports
# ---------------------------------------------------------------------------


def test_empty_solve_result_exports_gracefully_in_all_formats():
    result = _solve_result(
        status=STATUS_INFEASIBLE_POOL_TOO_SMALL,
        reason="pool has 3 eligible fighter(s); need at least 6",
        lineups=(),
    )
    bundle = build_internal_export_bundle(
        result,
        metadata=_metadata(),
        fighter_name_by_id={},
        fighter_salary_by_id={},
        diagnostics=ExportDiagnostics(
            pool_size=3,
            excluded=(
                ExcludedFighterEntry(
                    name="No Show", reason="missing dk_salary"
                ),
            ),
        ),
    )

    csv_bytes = format_lineups_csv(bundle)
    json_bytes = format_lineups_json(bundle)
    md_bytes = format_markdown_summary(bundle)

    _, header, rows = _parse_csv(csv_bytes)
    assert tuple(header) == CSV_HEADERS
    assert rows == []

    payload = json.loads(json_bytes)
    assert payload["lineups"] == []
    assert payload["optimizer"]["status"] == STATUS_INFEASIBLE_POOL_TOO_SMALL
    assert (
        payload["optimizer"]["reason"]
        == "pool has 3 eligible fighter(s); need at least 6"
    )
    assert payload["optimizer"]["n_lineups_generated"] == 0
    assert payload["diagnostics"]["pool_size"] == 3
    assert payload["diagnostics"]["excluded"] == [
        {"name": "No Show", "reason": "missing dk_salary"},
    ]

    md_text = md_bytes.decode("utf-8")
    assert "## Lineup" not in md_text
    assert f"`{STATUS_INFEASIBLE_POOL_TOO_SMALL}`" in md_text
    assert "## Diagnostics" in md_text
    assert f"> {INTERNAL_EXPORT_WARNING}" in md_text


# ---------------------------------------------------------------------------
# Pure-function guarantees (no I/O, no DB)
# ---------------------------------------------------------------------------


def test_formatters_do_not_require_db_connection():
    # All three formatters operate on the in-memory bundle alone; no
    # function in the public surface takes a ``conn`` argument.
    from inspect import signature

    for fn in (
        build_internal_export_bundle,
        format_lineups_csv,
        format_lineups_json,
        format_markdown_summary,
    ):
        params = signature(fn).parameters
        assert "conn" not in params, (
            f"{fn.__name__} unexpectedly accepts a DB connection"
        )

    bundle = _populated_bundle()
    # And running the formatters does not raise — i.e. they succeed
    # with zero DB state available.
    assert format_lineups_csv(bundle)
    assert format_lineups_json(bundle)
    assert format_markdown_summary(bundle)


def test_formatters_do_not_write_files(tmp_path: Path, monkeypatch):
    bundle = _populated_bundle(
        diagnostics=ExportDiagnostics(pool_size=10, excluded=()),
        manual_review=ManualReviewSnapshot(
            ready=True,
            status="reviewed",
            completed_at_utc="2026-05-28T13:45:00Z",
            blocking_count=0,
            warning_count=0,
            informational_count=0,
        ),
    )

    real_open = open

    def _no_writes(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(
                f"unexpected file write: {file!r} mode={mode!r}"
            )
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _no_writes)
    monkeypatch.chdir(tmp_path)

    csv_bytes = format_lineups_csv(bundle)
    json_bytes = format_lineups_json(bundle)
    md_bytes = format_markdown_summary(bundle)

    # The cwd must remain empty — no incidental files dropped.
    assert list(tmp_path.iterdir()) == []
    assert csv_bytes and json_bytes and md_bytes


def test_module_does_not_import_db_connection_helpers():
    # Defense in depth: importing the formatter must not pull in any
    # DB plumbing. If a future refactor adds a DB import the negative
    # assertion below will trip.
    mod = sys.modules["src.exports.internal_export"]
    assert not hasattr(mod, "sqlite3")
    assert not hasattr(mod, "get_connection")
