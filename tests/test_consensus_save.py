"""Vertical tests for save_consensus_to_slate (consensus Slice 5).

Drives the full persistence path: parser rows → odds_book_lines provenance +
synthesized source="consensus" odds_rows → chained recompute. Asserts the design
guarantees: exact consensus prob stored (§6), low-confidence fights skipped (§9),
idempotent last-save-wins replace (§7), and that the recompute is chained.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from src.db.repositories import (
    OddsBookLineRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.consensus_assembly import assemble_fights, merge_sources
from src.ingestion.consensus_save import (
    SOURCE_CONSENSUS,
    save_consensus_to_slate,
)
from src.projections.odds_consensus import compute_slate_consensus

CAPTURED_AT = "2026-05-20T00:00:00Z"


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
    return SlateRepository(conn).create(event_name="UFC Consensus").id


def _seed_fighter(conn, slate_id, name, salary=8000):
    conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, 'active')",
        (slate_id, name, salary),
    )
    conn.commit()


def _row(fighter_name, opponent, **books):
    return SimpleNamespace(
        fighter_name=fighter_name,
        opponent=opponent,
        book_lines=[
            SimpleNamespace(book=b, american_moneyline=ml)
            for b, ml in books.items()
        ],
    )


def _two_book_fight():
    return [
        _row("Alice Ace", "Bob Bee", DraftKings=-150, FanDuel=-160),
        _row("Bob Bee", "Alice Ace", DraftKings=+130, FanDuel=+140),
    ]


# --- provenance + consensus rows ----------------------------------------


def test_writes_provenance_and_consensus_rows(conn, slate_id):
    _seed_fighter(conn, slate_id, "Alice Ace")
    _seed_fighter(conn, slate_id, "Bob Bee")

    result = save_consensus_to_slate(
        conn,
        slate_id=slate_id,
        captured_at=CAPTURED_AT,
        bestfightodds_rows=_two_book_fight(),
    )

    # Provenance: 2 fighters × 2 books = 4 rows, source token lowercase.
    book_rows = OddsBookLineRepository(conn).list_for_slate(slate_id)
    assert len(book_rows) == 4
    assert {r.source for r in book_rows} == {"bestfightodds"}
    assert result.book_line_count == 4

    # Consensus odds_rows: one per fighter, source/bookmaker = "consensus".
    consensus = OddsRowRepository(conn).list_for_slate_source(
        slate_id, SOURCE_CONSENSUS
    )
    assert {r.fighter_name_raw for r in consensus} == {"Alice Ace", "Bob Bee"}
    assert all(r.bookmaker == "consensus" for r in consensus)
    assert result.consensus_count == 2
    assert result.low_confidence == []
    assert result.unpaired_fighters == []


def test_consensus_row_carries_exact_probability_and_fair_line(conn, slate_id):
    _seed_fighter(conn, slate_id, "Alice Ace")
    _seed_fighter(conn, slate_id, "Bob Bee")
    rows = _two_book_fight()

    save_consensus_to_slate(
        conn, slate_id=slate_id, captured_at=CAPTURED_AT, bestfightodds_rows=rows
    )

    # Independently recompute the expected blend from the same input.
    expected = compute_slate_consensus(
        assemble_fights(merge_sources(bestfightodds_rows=rows)).fights
    )[0]

    by_name = {
        r.fighter_name_raw: r
        for r in OddsRowRepository(conn).list_for_slate_source(
            slate_id, SOURCE_CONSENSUS
        )
    }
    alice = by_name["Alice Ace"]
    assert alice.american_odds == expected.fair_american_a
    # Exact prob stored verbatim — NOT the round-tripped implied of the line.
    assert alice.implied_probability == pytest.approx(expected.prob_a, abs=1e-12)


def test_recompute_is_chained_when_roster_present(conn, slate_id):
    _seed_fighter(conn, slate_id, "Alice Ace")
    _seed_fighter(conn, slate_id, "Bob Bee")
    result = save_consensus_to_slate(
        conn,
        slate_id=slate_id,
        captured_at=CAPTURED_AT,
        bestfightodds_rows=_two_book_fight(),
    )
    assert result.recompute is not None
    assert result.recompute_error is None
    # The recompute wrote match-result rows for the consensus odds_rows.
    n = conn.execute(
        "SELECT COUNT(*) FROM odds_match_results WHERE slate_id = ?", (slate_id,)
    ).fetchone()[0]
    assert n == 2


def test_rows_saved_even_when_roster_empty(conn, slate_id):
    # No fighters seeded → recompute surfaces an error, rows still persist.
    result = save_consensus_to_slate(
        conn,
        slate_id=slate_id,
        captured_at=CAPTURED_AT,
        bestfightodds_rows=_two_book_fight(),
    )
    assert result.recompute is None
    assert result.recompute_error is not None
    assert result.consensus_count == 2
    assert result.book_line_count == 4


# --- low confidence (§9) ------------------------------------------------


def test_low_confidence_fight_skips_consensus_row_keeps_provenance(conn, slate_id):
    # One book per fighter → book_count 1 < MIN_BOOKS (2): low confidence.
    one_book = [
        _row("Solo One", "Solo Two", DraftKings=-150),
        _row("Solo Two", "Solo One", DraftKings=+130),
    ]
    result = save_consensus_to_slate(
        conn, slate_id=slate_id, captured_at=CAPTURED_AT, bestfightodds_rows=one_book
    )
    # No consensus odds_rows for the low-confidence fight...
    assert result.consensus_count == 0
    assert len(result.low_confidence) == 1
    # ...but its provenance is still recorded.
    assert result.book_line_count == 2


# --- idempotence / last-save-wins (§7) ----------------------------------


def test_identical_resave_is_idempotent(conn, slate_id):
    _seed_fighter(conn, slate_id, "Alice Ace")
    _seed_fighter(conn, slate_id, "Bob Bee")
    rows = _two_book_fight()
    save_consensus_to_slate(
        conn, slate_id=slate_id, captured_at=CAPTURED_AT, bestfightodds_rows=rows
    )
    save_consensus_to_slate(
        conn, slate_id=slate_id, captured_at=CAPTURED_AT, bestfightodds_rows=rows
    )
    assert len(OddsBookLineRepository(conn).list_for_slate(slate_id)) == 4
    assert (
        len(OddsRowRepository(conn).list_for_slate_source(slate_id, SOURCE_CONSENSUS))
        == 2
    )


def test_reblend_replaces_changed_consensus(conn, slate_id):
    _seed_fighter(conn, slate_id, "Alice Ace")
    _seed_fighter(conn, slate_id, "Bob Bee")
    repo = OddsRowRepository(conn)

    save_consensus_to_slate(
        conn,
        slate_id=slate_id,
        captured_at=CAPTURED_AT,
        bestfightodds_rows=_two_book_fight(),
    )
    first = {
        r.fighter_name_raw: r.american_odds
        for r in repo.list_for_slate_source(slate_id, SOURCE_CONSENSUS)
    }

    # Re-blend with a heavier book that shifts the median.
    heavier = [
        _row("Alice Ace", "Bob Bee", DraftKings=-150, FanDuel=-160, BetMGM=-400),
        _row("Bob Bee", "Alice Ace", DraftKings=+130, FanDuel=+140, BetMGM=+320),
    ]
    save_consensus_to_slate(
        conn, slate_id=slate_id, captured_at=CAPTURED_AT, bestfightodds_rows=heavier
    )
    after = repo.list_for_slate_source(slate_id, SOURCE_CONSENSUS)

    assert len(after) == 2  # replaced, not duplicated
    after_by_name = {r.fighter_name_raw: r.american_odds for r in after}
    # Alice got more favored (extra -400 book pulls the median down).
    assert after_by_name["Alice Ace"] < first["Alice Ace"]


def test_resave_with_empty_input_clears_consensus(conn, slate_id):
    _seed_fighter(conn, slate_id, "Alice Ace")
    _seed_fighter(conn, slate_id, "Bob Bee")
    save_consensus_to_slate(
        conn,
        slate_id=slate_id,
        captured_at=CAPTURED_AT,
        bestfightodds_rows=_two_book_fight(),
    )
    # A subsequent save with nothing clears the prior consensus (last save wins).
    save_consensus_to_slate(conn, slate_id=slate_id, captured_at=CAPTURED_AT)
    assert (
        OddsRowRepository(conn).list_for_slate_source(slate_id, SOURCE_CONSENSUS)
        == []
    )
    assert OddsBookLineRepository(conn).list_for_slate(slate_id) == []


def test_paste_source_token_persisted(conn, slate_id):
    paste = [
        _row("Alice Ace", "Bob Bee", DraftKings=-150, FanDuel=-160),
        _row("Bob Bee", "Alice Ace", DraftKings=+130, FanDuel=+140),
    ]
    save_consensus_to_slate(
        conn, slate_id=slate_id, captured_at=CAPTURED_AT, paste_rows=paste
    )
    book_rows = OddsBookLineRepository(conn).list_for_slate(slate_id)
    assert {r.source for r in book_rows} == {"paste"}


def test_both_sources_paste_wins_through_persistence(conn, slate_id):
    # Both sources price Alice@DraftKings: the merge must collapse them to one
    # provenance row (else the odds_book_lines UNIQUE trips) and the survivor's
    # source token must be 'paste'. Distinct books from each side both persist.
    bfo = [
        _row("Alice Ace", "Bob Bee", DraftKings=-150, FanDuel=-160),
        _row("Bob Bee", "Alice Ace", DraftKings=+130, FanDuel=+140),
    ]
    paste = [
        _row("Alice Ace", "Bob Bee", DraftKings=-155, Polymarket=-170),
        _row("Bob Bee", "Alice Ace", DraftKings=+135, Polymarket=+150),
    ]
    # Must not raise (no IntegrityError on the provenance INSERT).
    save_consensus_to_slate(
        conn,
        slate_id=slate_id,
        captured_at=CAPTURED_AT,
        bestfightodds_rows=bfo,
        paste_rows=paste,
    )
    rows = OddsBookLineRepository(conn).list_for_slate(slate_id)
    alice = [r for r in rows if r.fighter_name_normalized == "alice ace"]
    by_book = {r.book: r for r in alice}
    # Exactly one DraftKings row for Alice, paste-wins.
    assert by_book["DraftKings"].source == "paste"
    assert by_book["DraftKings"].american_odds == -155
    # Union of distinct books across both sources.
    assert set(by_book) == {"DraftKings", "FanDuel", "Polymarket"}
    assert by_book["FanDuel"].source == "bestfightodds"
    assert by_book["Polymarket"].source == "paste"
