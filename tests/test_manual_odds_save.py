"""Tests for the manual-odds save helper.

Covers the session-only manual entries → ``odds_rows`` save path exposed
by ``app/pages/03_odds.py``. Match results, projections, and optimizer
linkage are intentionally out of scope.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.manual_odds import (
    ManualOddsRecomputeResult,
    ManualOddsSaveResult,
    save_manual_odds_and_recompute,
    save_manual_odds_entries,
)
from src.ingestion.odds_row_key import compute_manual_odds_row_key


def _insert_fighter(conn, *, slate_id, name, salary=8000, status="active"):
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, ?)",
        (int(slate_id), name, int(salary), status),
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def slate_id(conn):
    return SlateRepository(conn).create(event_name="UFC 999").id


def _entry(**overrides):
    base = {
        "fighter": "Jon Jones",
        "opponent": "Stipe Miocic",
        "moneyline": -200,
        "implied_probability": 2 / 3,
        "implied_probability_pct": "66.7%",
        "source": "manual",
        "timestamp": "2026-05-20T12:00:00Z",
    }
    base.update(overrides)
    return base


def test_save_inserts_row_with_manual_source_and_manual_key(conn, slate_id):
    repo = OddsRowRepository(conn)
    result = save_manual_odds_entries(
        repo, slate_id=slate_id, entries=[_entry()]
    )
    assert isinstance(result, ManualOddsSaveResult)
    assert result.saved_count == 1
    assert result.existing_count == 0
    assert result.failure_count == 0

    rec = result.saved[0]
    assert rec.slate_id == slate_id
    assert rec.fighter_name_raw == "Jon Jones"
    assert rec.opponent_name_raw == "Stipe Miocic"
    assert rec.american_odds == -200
    assert rec.bookmaker is None
    assert rec.source == "manual"
    expected_key = compute_manual_odds_row_key(
        fighter_name="Jon Jones", captured_at="2026-05-20T12:00:00Z"
    )
    assert rec.odds_row_key == expected_key
    assert rec.odds_row_key.startswith("manual:")


def test_save_forces_source_to_manual_even_if_entry_says_otherwise(
    conn, slate_id
):
    repo = OddsRowRepository(conn)
    result = save_manual_odds_entries(
        repo,
        slate_id=slate_id,
        entries=[_entry(source="csv:google-sheets")],
    )
    assert result.saved_count == 1
    assert result.saved[0].source == "manual"


def test_save_is_idempotent_for_repeat_clicks(conn, slate_id):
    repo = OddsRowRepository(conn)
    entries = [_entry()]
    first = save_manual_odds_entries(repo, slate_id=slate_id, entries=entries)
    second = save_manual_odds_entries(repo, slate_id=slate_id, entries=entries)
    assert first.saved_count == 1 and first.existing_count == 0
    assert second.saved_count == 0 and second.existing_count == 1
    assert len(repo.list_for_slate(slate_id)) == 1


def test_save_blank_opponent_becomes_none(conn, slate_id):
    repo = OddsRowRepository(conn)
    result = save_manual_odds_entries(
        repo,
        slate_id=slate_id,
        entries=[_entry(opponent="   ")],
    )
    assert result.saved[0].opponent_name_raw is None


def test_save_collects_validation_failures_without_aborting(conn, slate_id):
    repo = OddsRowRepository(conn)
    entries = [
        _entry(fighter="Good Fighter"),
        # moneyline 0 fails repo-level validation
        _entry(fighter="Bad Fighter", moneyline=0),
        _entry(
            fighter="Another Good", timestamp="2026-05-20T13:00:00Z"
        ),
    ]
    result = save_manual_odds_entries(repo, slate_id=slate_id, entries=entries)
    assert result.saved_count == 2
    assert result.failure_count == 1
    bad_fighter, _msg = result.failures[0]
    assert bad_fighter == "Bad Fighter"
    # The two good rows landed in the DB; the bad one did not.
    stored = repo.list_for_slate(slate_id)
    assert {r.fighter_name_raw for r in stored} == {"Good Fighter", "Another Good"}


def test_save_returns_empty_result_for_no_entries(conn, slate_id):
    repo = OddsRowRepository(conn)
    result = save_manual_odds_entries(repo, slate_id=slate_id, entries=[])
    assert result.saved_count == 0
    assert result.existing_count == 0
    assert result.failure_count == 0


def test_save_distinguishes_entries_by_timestamp(conn, slate_id):
    repo = OddsRowRepository(conn)
    entries = [
        _entry(timestamp="2026-05-20T12:00:00Z"),
        _entry(timestamp="2026-05-20T18:00:00Z"),
    ]
    result = save_manual_odds_entries(repo, slate_id=slate_id, entries=entries)
    assert result.saved_count == 2
    assert len(repo.list_for_slate(slate_id)) == 2


# ---------------------------------------------------------------------------
# save_manual_odds_and_recompute — the Build inline single-fighter path
# ---------------------------------------------------------------------------


def test_save_and_recompute_matches_fighter(conn, slate_id):
    """A hand-entered moneyline for an active DK fighter (exact name) is saved
    and the chained recompute auto-matches it — the fighter becomes covered."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jon Jones", salary=9000)

    result = save_manual_odds_and_recompute(
        conn,
        slate_id=slate_id,
        fighter_name="Jon Jones",
        american_odds=-200,
        captured_at="2026-05-20T12:00:00Z",
    )
    assert isinstance(result, ManualOddsRecomputeResult)
    assert result.saved_count == 1
    assert result.failure_count == 0
    assert result.recompute is not None
    assert result.recompute_error is None

    # The row is persisted with the manual source/key.
    rows = OddsRowRepository(conn).list_for_slate(slate_id)
    assert len(rows) == 1
    assert rows[0].source == "manual"
    assert rows[0].american_odds == -200

    # The recompute bound it to the fighter as an eligible (covered) match.
    results = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    mine = [r for r in results if r.fighter_id == fid]
    assert len(mine) == 1
    assert mine[0].match_status == "auto_match"
    assert mine[0].effective_status == "auto_match"


def test_save_and_recompute_is_idempotent_on_same_timestamp(conn, slate_id):
    """Re-saving the identical (fighter, captured_at) adds no second row."""
    _insert_fighter(conn, slate_id=slate_id, name="Jon Jones", salary=9000)
    kw = dict(
        slate_id=slate_id,
        fighter_name="Jon Jones",
        american_odds=-200,
        captured_at="2026-05-20T12:00:00Z",
    )
    first = save_manual_odds_and_recompute(conn, **kw)
    assert first.saved_count == 1
    second = save_manual_odds_and_recompute(conn, **kw)
    assert second.saved_count == 0
    assert second.existing_count == 1
    assert len(OddsRowRepository(conn).list_for_slate(slate_id)) == 1


def test_save_and_recompute_reports_recompute_error_when_no_fighters(conn, slate_id):
    """With no active DK fighters the row still saves; recompute is skipped and
    the error is reported rather than raised."""
    result = save_manual_odds_and_recompute(
        conn,
        slate_id=slate_id,
        fighter_name="Ghost Fighter",
        american_odds=150,
        captured_at="2026-05-20T12:00:00Z",
    )
    assert result.saved_count == 1
    assert result.recompute is None
    assert result.recompute_error is not None
    assert len(OddsRowRepository(conn).list_for_slate(slate_id)) == 1
