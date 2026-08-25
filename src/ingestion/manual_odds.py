"""Manual odds entry helpers.

Includes the small ``ManualOddsEntry`` shape (v0 skeleton) plus the save
helper that persists session-only manual entries from the Odds page into
the ``odds_rows`` table. The save path is deliberately narrow:

- ``source`` is forced to ``"manual"`` so saved rows are clearly attributable
  to the manual-entry form rather than a CSV import.
- The row key uses the ``manual:<normalized_fighter>:<captured_at>`` scheme
  from :mod:`src.ingestion.odds_row_key`, which keeps re-clicks idempotent
  and avoids hashing an almost-empty bookmaker.
- Bookmaker is always ``None`` — the manual form does not capture one.
- Saved rows do NOT yet feed projections, the optimizer, or match results
  (Phase B of ``docs/ODDS_PERSISTENCE_DESIGN.md``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src.db.repositories import OddsRowRecord, OddsRowRepository
from src.ingestion.odds_matching_service import (
    EmptyDkRosterError,
    RecomputeSummary,
    recompute_and_replace_match_results,
)
from src.ingestion.odds_row_key import compute_manual_odds_row_key


@dataclass
class ManualOddsEntry:
    fighter_name: str
    american_odds: int
    source: str = "manual"


@dataclass(frozen=True)
class ManualOddsSaveResult:
    saved: list[OddsRowRecord]
    already_existed: list[OddsRowRecord]
    failures: list[tuple[str, str]]

    @property
    def saved_count(self) -> int:
        return len(self.saved)

    @property
    def existing_count(self) -> int:
        return len(self.already_existed)

    @property
    def failure_count(self) -> int:
        return len(self.failures)


def save_manual_odds_entries(
    repo: OddsRowRepository,
    *,
    slate_id: int,
    entries: list[dict[str, Any]],
) -> ManualOddsSaveResult:
    """Persist each session manual entry as a row in ``odds_rows``.

    ``entries`` is the shape produced by ``app/pages/03_odds.py`` —
    each dict has at least ``fighter``, ``moneyline``, and ``timestamp``,
    and optionally ``opponent``. Per-entry validation failures are
    captured in the result rather than raised, so a single bad row
    does not abort the batch.
    """
    saved: list[OddsRowRecord] = []
    existed: list[OddsRowRecord] = []
    failures: list[tuple[str, str]] = []

    for entry in entries:
        fighter = (entry.get("fighter") or "").strip()
        captured_at = (entry.get("timestamp") or "").strip()
        opponent = entry.get("opponent")
        if isinstance(opponent, str):
            opponent = opponent.strip() or None
        elif opponent == "":
            opponent = None

        try:
            key = compute_manual_odds_row_key(
                fighter_name=fighter,
                captured_at=captured_at,
            )
            pre_existing = repo.get_by_key(
                slate_id=slate_id, odds_row_key=key
            )
            record = repo.create_or_get(
                slate_id=slate_id,
                fighter_name_raw=fighter,
                american_odds=int(entry["moneyline"]),
                source="manual",
                captured_at=captured_at,
                bookmaker=None,
                opponent_name_raw=opponent,
                odds_row_key=key,
            )
            if pre_existing is not None:
                existed.append(record)
            else:
                saved.append(record)
        except Exception as exc:  # noqa: BLE001
            label = fighter or "<unnamed>"
            failures.append((label, str(exc)))

    return ManualOddsSaveResult(
        saved=saved,
        already_existed=existed,
        failures=failures,
    )


@dataclass(frozen=True)
class ManualOddsRecomputeResult:
    """Outcome of one :func:`save_manual_odds_and_recompute` call.

    Mirrors ``DraftKingsPasteSaveResult``: ``saved`` / ``already_existed`` /
    ``failures`` are the per-row save outcome, and ``recompute`` is the
    ``RecomputeSummary`` from the chained match-result recompute (or ``None``
    with ``recompute_error`` set when the slate has no active DK fighters yet —
    the row is still saved).
    """

    saved: list[OddsRowRecord] = field(default_factory=list)
    already_existed: list[OddsRowRecord] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    recompute: RecomputeSummary | None = None
    recompute_error: str | None = None

    @property
    def saved_count(self) -> int:
        return len(self.saved)

    @property
    def existing_count(self) -> int:
        return len(self.already_existed)

    @property
    def failure_count(self) -> int:
        return len(self.failures)


def save_manual_odds_and_recompute(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    fighter_name: str,
    american_odds: int,
    captured_at: str,
    opponent_name: str | None = None,
) -> ManualOddsRecomputeResult:
    """Persist one hand-entered moneyline into ``odds_rows`` and recompute.

    Composes :func:`save_manual_odds_entries` (the same ``source="manual"`` /
    ``manual:<fighter>:<captured_at>`` key path the Odds page uses) with the
    existing ``recompute_and_replace_match_results`` — so a single moneyline
    typed on Build flows into the persisted match results (and active overrides
    are reapplied) in one user action. Using the fighter's exact DK name lets
    the matcher auto-match the new row, which is what makes the fighter count
    as covered.
    """
    repo = OddsRowRepository(conn)
    save = save_manual_odds_entries(
        repo,
        slate_id=int(slate_id),
        entries=[
            {
                "fighter": fighter_name,
                "moneyline": american_odds,
                "timestamp": captured_at,
                "opponent": opponent_name,
            }
        ],
    )

    recompute: RecomputeSummary | None = None
    recompute_error: str | None = None
    try:
        recompute = recompute_and_replace_match_results(conn, int(slate_id))
    except EmptyDkRosterError as exc:
        recompute_error = str(exc)

    return ManualOddsRecomputeResult(
        saved=save.saved,
        already_existed=save.already_existed,
        failures=save.failures,
        recompute=recompute,
        recompute_error=recompute_error,
    )
