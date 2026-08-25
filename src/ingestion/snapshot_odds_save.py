"""Save odds/news snapshot moneylines into ``odds_rows`` (slice S5a).

Implements ``docs/ODDS_NEWS_SNAPSHOT_PERSISTENCE_DESIGN.md`` §2, §4, §5, §14
(S5a): the **append-only, schema-free** snapshot save. A validated snapshot
(the S3 :class:`SnapshotValidationReport`) maps each valid moneyline entry to
one ``odds_rows`` row, tagged with a ``source="snapshot:<event-slug>"`` label
and a deterministic ``import_batch_id``, then triggers a match-result
recompute.

Deliberately narrow (design §4, §5, §8–§11; ``docs/DEVELOPMENT_NOTES.md`` §3):

  - **Moneylines only.** ``news_flags`` / ``news_note``, props (``itd_odds`` /
    ``decision_odds`` / ``goes_distance``), ``line_movement`` and full
    provenance have no ``odds_rows`` column and are **not** persisted here —
    they stay preview-only (design §9, §10, §11). Entries without a moneyline
    (e.g. ``news_only``) are skipped.
  - **Append-only / idempotent.** Rows go in via
    ``OddsRowRepository.create_or_get`` keyed on ``(slate_id, odds_row_key)``;
    re-saving the identical snapshot is a no-op. No update / delete / replace
    of existing odds rows (design §1.1, §4).
  - **Manual / CSV rows are never touched.** Only ``source="snapshot:…"`` rows
    are written; ``"manual"`` / ``"csv"`` rows live in a different ``source``
    and key namespace (design §5).
  - **Manual overrides survive.** This module never writes
    ``manual_match_overrides``; the chained recompute reapplies active
    overrides in its own transaction (design §6, §8; Phase D.4.3.b).
  - **Single snapshot per slate (v0 guard).** If the slate already holds
    snapshot-sourced rows from a *different* snapshot (different source label
    or batch id), the save is **blocked** and nothing is written; fresher
    replacement is deferred to S5b (design §4, §13.3).
  - **App-derived implied probability.** The moneyline is canonical; the row's
    ``implied_probability`` is derived by ``OddsRowRepository.create`` from the
    American odds — never the snapshot's advisory field (design §2).

Transaction note (S5a deviation from design §8): the design preferred a single
transaction spanning insert + recompute, but ``OddsRowRepository.create``
commits per row and exposes no no-commit variant. S5a intentionally matches the
existing CSV / manual save behavior (per-row commit, then a separate recompute
that owns its own transaction) to avoid a repository refactor. Insert + recompute
are therefore **not** one atomic unit; idempotency makes a retry after a partial
failure safe. Atomic insert+recompute can be revisited in a future
repository/service cleanup slice.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field

from src.collection.odds_news_snapshot import (
    SnapshotEntry,
    SnapshotEnvelope,
    SnapshotValidationReport,
)
from src.db.repositories import OddsRowRecord, OddsRowRepository
from src.ingestion.odds_matching_service import (
    EmptyDkRosterError,
    RecomputeSummary,
    recompute_and_replace_match_results,
)
from src.ingestion.odds_row_key import compute_odds_row_key
from src.utils.text_cleaning import normalize_name

_SLUG_NONWORD = re.compile(r"[^a-z0-9]+")
_BATCH_ID_HASH_LEN = 12


@dataclass(frozen=True)
class SnapshotOddsSaveResult:
    """Outcome of one :func:`save_snapshot_odds_to_slate` call.

    ``saved`` are newly inserted rows; ``already_existed`` are rows whose
    ``(slate_id, odds_row_key)`` was already present (idempotent re-save).
    ``skipped`` is ``(label, reason)`` for entries that carried no moneyline
    or hit a per-row save failure. ``blocked`` is True only when the
    single-snapshot-per-slate guard fired — in that case nothing was written
    and ``blocked_reason`` explains why. ``recompute`` is the
    ``RecomputeSummary`` from the chained match-result recompute, or ``None``
    with ``recompute_error`` set when the slate had no active DK fighters yet.
    """

    source_label: str
    import_batch_id: str
    saved: list[OddsRowRecord] = field(default_factory=list)
    already_existed: list[OddsRowRecord] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked: bool = False
    blocked_reason: str | None = None
    recompute: RecomputeSummary | None = None
    recompute_error: str | None = None

    @property
    def saved_count(self) -> int:
        return len(self.saved)

    @property
    def existing_count(self) -> int:
        return len(self.already_existed)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


def _slugify_event(name: str | None) -> str:
    """Slugify an event name for the ``snapshot:<slug>`` source label."""
    if not name:
        return "unknown"
    slug = _SLUG_NONWORD.sub("-", name.strip().lower()).strip("-")
    return slug or "unknown"


def source_label_for(report: SnapshotValidationReport) -> str:
    """The ``source`` value snapshot rows are tagged with (design §3)."""
    env = report.envelope
    return f"snapshot:{_slugify_event(env.event_name if env else None)}"


def is_snapshot_source(source: str | None) -> bool:
    """True for a ``source`` written by this path (``snapshot`` / ``snapshot:…``)."""
    return bool(source) and (source == "snapshot" or source.startswith("snapshot:"))


def _effective_captured_at(entry: SnapshotEntry, envelope: SnapshotEnvelope) -> str:
    """Per-entry capture time → ``captured_at`` (entry value, else envelope)."""
    return (entry.collected_at or envelope.collected_at or "").strip()


def compute_import_batch_id(
    report: SnapshotValidationReport,
    moneyline_entries: list[SnapshotEntry],
) -> str:
    """Deterministic batch id for one snapshot's saved moneylines.

    Hashes the snapshot identity (event name + ``collected_at``) plus each
    saved line's ``(normalized fighter, moneyline, book, captured_at)``. The
    identical snapshot yields the identical id (idempotent re-save); a fresher
    snapshot — new envelope ``collected_at`` or an edited line — yields a
    different id, which the single-snapshot-per-slate guard rejects (design
    §4, §13.2–§13.3).
    """
    env = report.envelope
    parts = [
        (env.event_name or "") if env else "",
        (env.collected_at or "") if env else "",
    ]
    rows = sorted(
        (
            normalize_name(e.fighter_name),
            int(e.moneyline),
            (e.book or ""),
            _effective_captured_at(e, env),
        )
        for e in moneyline_entries
    )
    parts.extend(f"{fighter}|{ml}|{book}|{cap}" for fighter, ml, book, cap in rows)
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return f"snap-{digest[:_BATCH_ID_HASH_LEN]}"


def save_snapshot_odds_to_slate(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    report: SnapshotValidationReport,
) -> SnapshotOddsSaveResult:
    """Save a validated snapshot's moneylines into ``odds_rows`` (design §14 S5a).

    Inserts one ``source="snapshot:<event-slug>"`` row per valid moneyline
    entry via ``OddsRowRepository.create_or_get`` (per-row commit, mirroring
    the CSV / manual save paths — see the module transaction note), then runs
    ``recompute_and_replace_match_results`` so the new lines flow into the
    persisted match results (and active overrides are reapplied).

    Preconditions (the UI gate enforces these; this raises defensively if a
    caller bypasses it):

    - ``report`` must have **no** hard errors (``report.errors`` empty).
    - ``report`` must contain at least one entry with a moneyline.

    The single-snapshot-per-slate guard (design §4): if the slate already
    holds snapshot rows from a *different* source label or batch id, the save
    is blocked (``blocked=True``) and **nothing is written**. Manual / CSV rows
    and manual overrides are never read into the guard and never modified.
    """
    slate_id = int(slate_id)

    if report.errors:
        raise ValueError(
            "snapshot has hard validation errors; resolve them before saving "
            f"({len(report.errors)} error(s))"
        )
    env = report.envelope
    if env is None or not (env.collected_at or "").strip():
        raise ValueError("snapshot envelope is missing a 'collected_at' timestamp")

    moneyline_entries = [e for e in report.entries_ok if e.moneyline is not None]
    skipped: list[tuple[str, str]] = [
        (e.fighter_name or f"entry #{e.entry_index}", "no moneyline (not saved)")
        for e in report.entries_ok
        if e.moneyline is None
    ]
    if not moneyline_entries:
        raise ValueError("snapshot has no valid moneyline entries to save")

    source_label = source_label_for(report)
    import_batch_id = compute_import_batch_id(report, moneyline_entries)
    warnings = list(report.warnings)

    repo = OddsRowRepository(conn)

    # --- single-snapshot-per-slate guard (design §4) ----------------------
    current_identity = (source_label, import_batch_id)
    existing_identities = {
        (r.source, r.import_batch_id)
        for r in repo.list_for_slate(slate_id)
        if is_snapshot_source(r.source)
    }
    other_identities = existing_identities - {current_identity}
    if other_identities:
        labels = ", ".join(
            f"{src} (batch {batch or '—'})"
            for src, batch in sorted(
                other_identities, key=lambda t: (t[0], t[1] or "")
            )
        )
        return SnapshotOddsSaveResult(
            source_label=source_label,
            import_batch_id=import_batch_id,
            skipped=skipped,
            warnings=warnings,
            blocked=True,
            blocked_reason=(
                f"Slate #{slate_id} already has snapshot odds from a different "
                f"snapshot ({labels}). S5a saves one snapshot per slate; "
                "replacing it with a fresher snapshot is deferred to S5b. "
                "No rows were written."
            ),
        )

    # --- insert moneylines (per-row commit via create_or_get) -------------
    saved: list[OddsRowRecord] = []
    existed: list[OddsRowRecord] = []
    for entry in moneyline_entries:
        label = entry.fighter_name or f"entry #{entry.entry_index}"
        captured_at = _effective_captured_at(entry, env)
        bookmaker = entry.book or None
        opponent = entry.opponent_name or None
        try:
            key = compute_odds_row_key(
                fighter_name=entry.fighter_name,
                bookmaker=bookmaker,
                source=source_label,
                captured_at=captured_at,
            )
            pre_existing = repo.get_by_key(slate_id=slate_id, odds_row_key=key)
            record = repo.create_or_get(
                slate_id=slate_id,
                fighter_name_raw=entry.fighter_name,
                american_odds=int(entry.moneyline),
                source=source_label,
                captured_at=captured_at,
                bookmaker=bookmaker,
                opponent_name_raw=opponent,
                import_batch_id=import_batch_id,
                odds_row_key=key,
            )
            if pre_existing is not None:
                existed.append(record)
            else:
                saved.append(record)
        except Exception as exc:  # noqa: BLE001
            skipped.append((label, str(exc)))

    # --- recompute match results (own transaction; reapplies overrides) ---
    recompute: RecomputeSummary | None = None
    recompute_error: str | None = None
    try:
        recompute = recompute_and_replace_match_results(conn, slate_id)
    except EmptyDkRosterError as exc:
        recompute_error = str(exc)

    return SnapshotOddsSaveResult(
        source_label=source_label,
        import_batch_id=import_batch_id,
        saved=saved,
        already_existed=existed,
        skipped=skipped,
        warnings=warnings,
        recompute=recompute,
        recompute_error=recompute_error,
    )
