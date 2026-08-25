"""Fighter Status v1 — Phase C read aggregator.

Composes the slate-scoped ``fighters`` rows (importer-owned base
``status`` plus the user-owned ``manual_status`` /
``manual_status_set_at`` columns added in Phase B) with the Phase A
resolver and category mapping from ``src/slate/fighter_status.py``.

Per ``docs/FIGHTER_STATUS_V1_DESIGN.md`` §15 Phase C this layer is
read-only:

- It never writes to the database.
- It never reads or references ``odds_match_results.effective_status``
  (Fighter Status is strictly disjoint from the odds-match override
  layer; see design §6, §7, §8).
- It does not feed projections, alerts, the Manual Review gate, the
  optimizer, exports, or run logs. Those promotions are gated Phase F
  work (design §15, ``docs/DEVELOPMENT_NOTES.md`` §10).

The aggregator exists so a future UI / workflow phase can display one
row per fighter with the resolved status and downstream category
without each call site re-implementing the join.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.slate import fighter_status as fs


@dataclass(frozen=True)
class FighterStatusRow:
    """One fighter's resolved Fighter Status v1 snapshot.

    ``importer_status`` and ``manual_status`` are surfaced separately so
    callers can distinguish "user has not asserted anything" (manual is
    ``None``) from "user has asserted the same value the importer
    already has". ``effective_status`` and ``category`` are derived via
    the Phase A resolver / category mapping and are guaranteed to be
    consistent with each other.
    """

    fighter_id: int
    slate_id: int
    name: str
    salary: int
    importer_status: str | None
    manual_status: str | None
    manual_status_set_at: str | None
    effective_status: str
    category: str


def list_fighter_status_rows(
    conn: sqlite3.Connection, slate_id: int
) -> list[FighterStatusRow]:
    """Return one ``FighterStatusRow`` per fighter on ``slate_id``.

    Ordering mirrors ``FighterRepository.list_for_slate``: case-insensitive
    by name, then by row id. An unknown or empty slate yields ``[]`` —
    consistent with the existing repository read style.
    """
    sid = int(slate_id)
    rows = conn.execute(
        "SELECT id, slate_id, name, salary, status, "
        "       manual_status, manual_status_set_at "
        "FROM fighters WHERE slate_id = ? "
        "ORDER BY name COLLATE NOCASE ASC, id ASC",
        (sid,),
    ).fetchall()

    result: list[FighterStatusRow] = []
    for r in rows:
        importer = r[4]
        manual = r[5]
        effective = fs.resolve_effective_fighter_status(importer, manual)
        category = fs.category_for(effective)
        result.append(
            FighterStatusRow(
                fighter_id=int(r[0]),
                slate_id=int(r[1]),
                name=r[2],
                salary=int(r[3]),
                importer_status=importer,
                manual_status=manual,
                manual_status_set_at=r[6],
                effective_status=effective,
                category=category,
            )
        )
    return result


def category_counts(rows: list[FighterStatusRow]) -> dict[str, int]:
    """Return ``{active, warning, blocking}`` counts for ``rows``.

    Counts every Phase A category, including zeroes, so the returned
    dict shape is stable regardless of the input slate. Intended as a
    convenience for the future Fighter Status UI summary line
    (``docs/FIGHTER_STATUS_V1_DESIGN.md`` §14) — callers that need a
    raw iteration can ignore it.
    """
    counts: dict[str, int] = {
        fs.CATEGORY_ACTIVE: 0,
        fs.CATEGORY_WARNING: 0,
        fs.CATEGORY_BLOCKING: 0,
    }
    for row in rows:
        counts[row.category] += 1
    return counts
