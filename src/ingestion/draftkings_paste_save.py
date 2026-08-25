"""Save DraftKings copied-board paste rows into ``odds_rows`` (Phase 3A).

Realizes ``docs/ODDS_ACQUISITION_V0_DESIGN.md`` §2 / Phase 3 (table line 115) for
the **paste** acquisition path (Phase 4): the normalized moneyline rows the user
reviewed in the Build Step 2 DraftKings-paste preview are persisted through the
*existing* ``odds_rows`` → recompute pipeline. It introduces no parallel store
and no new match logic (design §2 / §10) — it is the paste path's reuse of the
same save+recompute flow the snapshot importer already uses.

Shape and boundaries (design §2, §1.7; ``docs/DEVELOPMENT_NOTES.md`` §3 / §11):

  - **Moneylines only.** One ``odds_rows`` row per parsed fighter line, exactly
    as previewed. No props / totals / news are persisted.
  - **Path-labelled source.** Every row is tagged ``source="draftkings_paste"``
    so paste-acquired rows are distinguishable from ``manual`` / ``csv`` /
    ``snapshot:…`` rows in the odds-status breakdown; ``bookmaker`` stays
    ``"DraftKings"`` (the copied board *is* the DraftKings line — §1.7 #3).
  - **Opponent preserved.** ``opponent`` is carried into
    ``opponent_name_raw`` (the paste always pairs fighters via ``vs``).
  - **No ``source_url`` column.** ``odds_rows`` has no URL field and v0 adds no
    schema, so a supplied DraftKings source URL is carried for traceability /
    surfacing only and is **not** persisted. It is returned on the result so the
    caller can echo the limitation.
  - **Append-only / idempotent.** Rows go in via
    ``OddsRowRepository.create_or_get`` keyed on ``(slate_id, odds_row_key)``;
    re-saving the same previewed batch (same ``captured_at``) is a no-op.
  - **Manual overrides survive.** This module never writes
    ``manual_match_overrides``; the chained recompute reapplies active overrides
    in its own transaction (mirrors the snapshot save path — design §6 / §8).
  - **No single-snapshot guard.** Unlike ``save_snapshot_odds_to_slate`` this
    path is *not* the single-snapshot-per-slate envelope; paste rows live in the
    ``draftkings_paste`` source namespace and never collide with that guard.

Transaction note: like the CSV / manual / snapshot save paths,
``OddsRowRepository.create`` commits per row and the recompute owns its own
transaction, so insert + recompute are not one atomic unit. Idempotency makes a
retry after a partial failure safe.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src.db.repositories import OddsRowRecord, OddsRowRepository
from src.ingestion.odds_matching_service import (
    EmptyDkRosterError,
    RecomputeSummary,
    recompute_and_replace_match_results,
)
from src.ingestion.odds_row_key import compute_odds_row_key
from src.ingestion.providers.draftkings_paste import BOOK_DRAFTKINGS

# §2 source label for the paste acquisition path. Distinct from ``manual`` /
# ``csv`` / ``snapshot:…`` so the Build odds-status breakdown can attribute rows.
SOURCE_DRAFTKINGS_PASTE = "draftkings_paste"

_BATCH_ID_HASH_LEN = 12


@dataclass(frozen=True)
class DraftKingsPasteSaveResult:
    """Outcome of one :func:`save_draftkings_paste_rows` call.

    ``saved`` are newly inserted rows; ``already_existed`` are rows whose
    ``(slate_id, odds_row_key)`` was already present (idempotent re-save).
    ``failures`` is ``(label, reason)`` for per-row save failures (a single bad
    row never aborts the batch). ``recompute`` is the ``RecomputeSummary`` from
    the chained match-result recompute, or ``None`` with ``recompute_error`` set
    when the slate had no active DK fighters yet. ``source_url`` is the supplied
    DraftKings provenance URL echoed back (never persisted — no column).
    """

    source_label: str
    import_batch_id: str
    saved: list[OddsRowRecord] = field(default_factory=list)
    already_existed: list[OddsRowRecord] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    recompute: RecomputeSummary | None = None
    recompute_error: str | None = None
    source_url: str | None = None

    @property
    def saved_count(self) -> int:
        return len(self.saved)

    @property
    def existing_count(self) -> int:
        return len(self.already_existed)

    @property
    def failure_count(self) -> int:
        return len(self.failures)


def _compute_batch_id(source_label: str, captured_at: str) -> str:
    """Deterministic batch id grouping one previewed paste-save.

    ``captured_at`` is fixed when the preview is produced, so the identical
    previewed batch yields the identical id (idempotent re-save); a fresh parse
    (new ``captured_at``) yields a new id.
    """
    digest = hashlib.sha1(
        f"{source_label}|{captured_at or ''}".encode("utf-8")
    ).hexdigest()
    return f"dkpaste-{digest[:_BATCH_ID_HASH_LEN]}"


def save_draftkings_paste_rows(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    rows: list[dict[str, Any]],
    captured_at: str,
    source_url: str | None = None,
) -> DraftKingsPasteSaveResult:
    """Persist previewed DraftKings paste moneylines into ``odds_rows``.

    ``rows`` is the preview payload shape produced by
    ``app/pages/00_build.py`` — each dict has ``fighter_name``,
    ``american_moneyline``, and optionally ``opponent`` / ``book``. Each row is
    inserted via ``OddsRowRepository.create_or_get`` with
    ``source="draftkings_paste"`` and ``bookmaker="DraftKings"`` (per-row commit,
    mirroring the CSV / manual / snapshot save paths), then
    ``recompute_and_replace_match_results`` runs so the new lines flow into the
    persisted match results (and active overrides are reapplied).

    Per-row validation errors land in ``failures`` and the batch continues; a
    slate with no active DK fighters yet surfaces as ``recompute_error`` (the
    rows are still saved). ``source_url`` is echoed back for traceability and is
    **not** persisted — ``odds_rows`` has no URL column and v0 adds no schema.
    """
    slate_id = int(slate_id)
    source_label = SOURCE_DRAFTKINGS_PASTE
    import_batch_id = _compute_batch_id(source_label, captured_at)

    repo = OddsRowRepository(conn)

    saved: list[OddsRowRecord] = []
    existed: list[OddsRowRecord] = []
    failures: list[tuple[str, str]] = []

    for row in rows:
        fighter = (row.get("fighter_name") or "").strip()
        opponent = row.get("opponent")
        if isinstance(opponent, str):
            opponent = opponent.strip() or None
        elif opponent == "":
            opponent = None
        bookmaker = (row.get("book") or BOOK_DRAFTKINGS) or None
        label = fighter or "<unnamed>"

        try:
            ml = int(row["american_moneyline"])
            key = compute_odds_row_key(
                fighter_name=fighter,
                bookmaker=bookmaker,
                source=source_label,
                captured_at=captured_at,
            )
            pre_existing = repo.get_by_key(slate_id=slate_id, odds_row_key=key)
            record = repo.create_or_get(
                slate_id=slate_id,
                fighter_name_raw=fighter,
                american_odds=ml,
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
            failures.append((label, str(exc)))

    # --- recompute match results (own transaction; reapplies overrides) ---
    recompute: RecomputeSummary | None = None
    recompute_error: str | None = None
    try:
        recompute = recompute_and_replace_match_results(conn, slate_id)
    except EmptyDkRosterError as exc:
        recompute_error = str(exc)

    return DraftKingsPasteSaveResult(
        source_label=source_label,
        import_batch_id=import_batch_id,
        saved=saved,
        already_existed=existed,
        failures=failures,
        recompute=recompute,
        recompute_error=recompute_error,
        source_url=(source_url or None),
    )
