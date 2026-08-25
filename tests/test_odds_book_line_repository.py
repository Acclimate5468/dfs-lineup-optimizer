"""Tests for OddsBookLineRepository (consensus provenance, Slice 5).

Covers the clear-and-rewrite write path for ``odds_book_lines`` (design §5.4):
replace-by-slate, list, validation, source allow-list, and cascade. The
synthesized ``source="consensus"`` ``odds_rows`` writer is tested separately in
``test_consensus_save.py``; this file is the repository surface only.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import OddsBookLineRepository, SlateRepository
from src.db.schema import apply_schema


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


def _line(**overrides) -> dict:
    payload = {
        "fighter_name_raw": "Jon Jones",
        "opponent_name_raw": "Stipe Miocic",
        "book": "DraftKings",
        "american_odds": -250,
        "source": "bestfightodds",
        "captured_at": "2026-05-20T00:00:00Z",
        "import_batch_id": "consensus-abc123",
    }
    payload.update(overrides)
    return payload


def test_replace_for_slate_inserts_rows(conn, slate_id):
    repo = OddsBookLineRepository(conn)
    saved = repo.replace_for_slate(
        slate_id,
        [
            _line(book="DraftKings", american_odds=-250),
            _line(book="FanDuel", american_odds=-240),
        ],
    )
    assert len(saved) == 2
    assert {r.book for r in saved} == {"DraftKings", "FanDuel"}
    assert all(r.fighter_name_normalized == "jon jones" for r in saved)
    assert all(r.slate_id == slate_id for r in saved)


def test_replace_for_slate_normalizes_fighter_name(conn, slate_id):
    repo = OddsBookLineRepository(conn)
    saved = repo.replace_for_slate(
        slate_id, [_line(fighter_name_raw="Albert  Odzimkowski ")]
    )
    assert saved[0].fighter_name_raw == "Albert  Odzimkowski"
    assert saved[0].fighter_name_normalized  # non-empty


def test_replace_for_slate_clears_prior_rows(conn, slate_id):
    repo = OddsBookLineRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [_line(book="DraftKings"), _line(book="FanDuel")],
    )
    # A second save with a different set replaces, never accumulates.
    saved = repo.replace_for_slate(slate_id, [_line(book="BetMGM")])
    assert len(saved) == 1
    assert saved[0].book == "BetMGM"
    assert len(repo.list_for_slate(slate_id)) == 1


def test_replace_for_slate_empty_clears(conn, slate_id):
    repo = OddsBookLineRepository(conn)
    repo.replace_for_slate(slate_id, [_line()])
    repo.replace_for_slate(slate_id, [])
    assert repo.list_for_slate(slate_id) == []


def test_replace_for_slate_is_scoped_per_slate(conn):
    repo = OddsBookLineRepository(conn)
    a = SlateRepository(conn).create(event_name="UFC A").id
    b = SlateRepository(conn).create(event_name="UFC B").id
    repo.replace_for_slate(a, [_line(book="DraftKings")])
    repo.replace_for_slate(b, [_line(book="FanDuel")])
    # Re-saving slate A must not touch slate B.
    repo.replace_for_slate(a, [_line(book="BetMGM")])
    assert {r.book for r in repo.list_for_slate(a)} == {"BetMGM"}
    assert {r.book for r in repo.list_for_slate(b)} == {"FanDuel"}


def test_replace_for_slate_rejects_unknown_source(conn, slate_id):
    repo = OddsBookLineRepository(conn)
    with pytest.raises(ValueError):
        repo.replace_for_slate(slate_id, [_line(source="consensus")])


def test_replace_for_slate_rejects_zero_odds(conn, slate_id):
    repo = OddsBookLineRepository(conn)
    with pytest.raises(ValueError):
        repo.replace_for_slate(slate_id, [_line(american_odds=0)])


def test_replace_for_slate_rejects_empty_fighter(conn, slate_id):
    repo = OddsBookLineRepository(conn)
    with pytest.raises(ValueError):
        repo.replace_for_slate(slate_id, [_line(fighter_name_raw="   ")])


def test_replace_for_slate_validation_is_atomic(conn, slate_id):
    # A bad row in the batch must abort the whole replace, leaving prior intact.
    repo = OddsBookLineRepository(conn)
    repo.replace_for_slate(slate_id, [_line(book="DraftKings")])
    with pytest.raises(ValueError):
        repo.replace_for_slate(
            slate_id,
            [_line(book="FanDuel"), _line(book="BetMGM", american_odds=0)],
        )
    # Prior single row survives; the failed batch wrote nothing.
    rows = repo.list_for_slate(slate_id)
    assert len(rows) == 1
    assert rows[0].book == "DraftKings"


def test_within_batch_duplicate_book_raises(conn, slate_id):
    # The save service de-dups (fighter, book) before calling the repo; if a dup
    # slips through, the UNIQUE constraint rejects the whole batch.
    repo = OddsBookLineRepository(conn)
    with pytest.raises(sqlite3.IntegrityError):
        repo.replace_for_slate(
            slate_id,
            [_line(book="DraftKings", american_odds=-250),
             _line(book="DraftKings", american_odds=-240)],
        )


def test_cascade_on_slate_delete(conn, slate_id):
    repo = OddsBookLineRepository(conn)
    repo.replace_for_slate(slate_id, [_line()])
    conn.execute("DELETE FROM slates WHERE id = ?", (slate_id,))
    assert repo.list_for_slate(slate_id) == []
