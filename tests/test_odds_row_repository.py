"""Tests for the Phase B OddsRowRepository (write path for ``odds_rows``).

Covers only the repository surface for raw odds rows. Match results and
manual overrides are out of scope for this phase.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import OddsRowRecord, OddsRowRepository, SlateRepository
from src.db.schema import apply_schema
from src.ingestion.odds_row_key import (
    compute_manual_odds_row_key,
    compute_odds_row_key,
)


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


@pytest.fixture
def other_slate_id(conn):
    return SlateRepository(conn).create(event_name="UFC 998").id


def _base_row(slate_id: int, **overrides) -> dict:
    payload = {
        "slate_id": slate_id,
        "fighter_name_raw": "Jon Jones",
        "american_odds": -200,
        "source": "csv:oddsapi",
        "captured_at": "2026-05-20T00:00:00Z",
        "bookmaker": "DraftKings",
    }
    payload.update(overrides)
    return payload


# --- create -------------------------------------------------------------


def test_create_inserts_row_with_derived_fields(conn, slate_id):
    repo = OddsRowRepository(conn)
    rec = repo.create(**_base_row(slate_id))
    assert isinstance(rec, OddsRowRecord)
    assert rec.id > 0
    assert rec.slate_id == slate_id
    assert rec.fighter_name_raw == "Jon Jones"
    assert rec.fighter_name_normalized == "jon jones"
    assert rec.american_odds == -200
    # -200 American → 200/(200+100) = 0.6667
    assert rec.implied_probability == pytest.approx(2.0 / 3.0, abs=1e-9)
    assert rec.source == "csv:oddsapi"
    assert rec.bookmaker == "DraftKings"
    assert rec.captured_at == "2026-05-20T00:00:00Z"
    assert rec.imported_at  # populated by DB default
    assert rec.odds_row_key == compute_odds_row_key(
        fighter_name="Jon Jones",
        bookmaker="DraftKings",
        source="csv:oddsapi",
        captured_at="2026-05-20T00:00:00Z",
    )


def test_create_trims_and_normalizes_text_fields(conn, slate_id):
    repo = OddsRowRepository(conn)
    rec = repo.create(
        **_base_row(
            slate_id,
            fighter_name_raw="  Conor   McGregor  ",
            opponent_name_raw="  Nate Diaz ",
            bookmaker="",  # blank → stored as NULL
            import_batch_id="   ",  # blank → stored as NULL
        )
    )
    assert rec.fighter_name_raw == "Conor   McGregor"
    assert rec.fighter_name_normalized == "conor mcgregor"
    assert rec.opponent_name_raw == "Nate Diaz"
    assert rec.bookmaker is None
    assert rec.import_batch_id is None


def test_create_explicit_odds_row_key_is_honored(conn, slate_id):
    repo = OddsRowRepository(conn)
    manual_key = compute_manual_odds_row_key(
        fighter_name="Khabib Nurmagomedov",
        captured_at="2026-05-20T12:00:00Z",
    )
    rec = repo.create(
        **_base_row(
            slate_id,
            fighter_name_raw="Khabib Nurmagomedov",
            source="manual",
            bookmaker=None,
            captured_at="2026-05-20T12:00:00Z",
            odds_row_key=manual_key,
        )
    )
    assert rec.odds_row_key == manual_key
    assert rec.odds_row_key.startswith("manual:")


# --- validation ---------------------------------------------------------


def test_create_rejects_empty_fighter_name(conn, slate_id):
    repo = OddsRowRepository(conn)
    with pytest.raises(ValueError):
        repo.create(**_base_row(slate_id, fighter_name_raw="   "))


def test_create_rejects_zero_american_odds(conn, slate_id):
    repo = OddsRowRepository(conn)
    with pytest.raises(ValueError):
        repo.create(**_base_row(slate_id, american_odds=0))


def test_create_rejects_empty_source(conn, slate_id):
    repo = OddsRowRepository(conn)
    with pytest.raises(ValueError):
        repo.create(**_base_row(slate_id, source=""))


def test_create_rejects_unparseable_captured_at(conn, slate_id):
    repo = OddsRowRepository(conn)
    with pytest.raises(ValueError):
        repo.create(**_base_row(slate_id, captured_at="not-a-timestamp"))


def test_create_rejects_empty_captured_at(conn, slate_id):
    repo = OddsRowRepository(conn)
    with pytest.raises(ValueError):
        repo.create(**_base_row(slate_id, captured_at="   "))


# --- DB-level guards (defense in depth) ---------------------------------


def test_db_rejects_zero_american_odds_at_check_constraint(conn, slate_id):
    # Bypass the repo and confirm the CHECK constraint still fires.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO odds_rows (slate_id, odds_row_key, fighter_name_raw, "
            "fighter_name_normalized, american_odds, source, captured_at) "
            "VALUES (?, 'k0', 'Foo', 'foo', 0, 'csv:test', "
            "'2026-05-20T00:00:00Z')",
            (slate_id,),
        )


# --- read paths ---------------------------------------------------------


def test_get_by_id_round_trip(conn, slate_id):
    repo = OddsRowRepository(conn)
    created = repo.create(**_base_row(slate_id))
    fetched = repo.get_by_id(created.id)
    assert fetched == created


def test_get_by_id_missing_returns_none(conn):
    repo = OddsRowRepository(conn)
    assert repo.get_by_id(999) is None


def test_get_by_key_round_trip(conn, slate_id):
    repo = OddsRowRepository(conn)
    created = repo.create(**_base_row(slate_id))
    fetched = repo.get_by_key(
        slate_id=slate_id, odds_row_key=created.odds_row_key
    )
    assert fetched == created


def test_get_by_key_missing_returns_none(conn, slate_id):
    repo = OddsRowRepository(conn)
    assert (
        repo.get_by_key(slate_id=slate_id, odds_row_key="does-not-exist")
        is None
    )


def test_list_for_slate_orders_by_insert_then_id(conn, slate_id):
    repo = OddsRowRepository(conn)
    a = repo.create(**_base_row(slate_id, fighter_name_raw="A Fighter"))
    b = repo.create(**_base_row(slate_id, fighter_name_raw="B Fighter"))
    c = repo.create(**_base_row(slate_id, fighter_name_raw="C Fighter"))
    rows = repo.list_for_slate(slate_id)
    assert [r.id for r in rows] == [a.id, b.id, c.id]


def test_list_for_slate_empty(conn, slate_id):
    repo = OddsRowRepository(conn)
    assert repo.list_for_slate(slate_id) == []


# --- scoping & duplicates ----------------------------------------------


def test_rows_are_scoped_by_slate(conn, slate_id, other_slate_id):
    repo = OddsRowRepository(conn)
    repo.create(**_base_row(slate_id, fighter_name_raw="A Fighter"))
    repo.create(**_base_row(slate_id, fighter_name_raw="B Fighter"))
    repo.create(**_base_row(other_slate_id, fighter_name_raw="C Fighter"))

    s1 = repo.list_for_slate(slate_id)
    s2 = repo.list_for_slate(other_slate_id)
    assert {r.fighter_name_raw for r in s1} == {"A Fighter", "B Fighter"}
    assert {r.fighter_name_raw for r in s2} == {"C Fighter"}


def test_create_duplicate_key_raises_integrity_error(conn, slate_id):
    repo = OddsRowRepository(conn)
    repo.create(**_base_row(slate_id))
    # Same inputs → same computed key → UNIQUE(slate_id, odds_row_key) trips.
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(**_base_row(slate_id))


def test_create_or_get_is_idempotent_for_same_key(conn, slate_id):
    repo = OddsRowRepository(conn)
    first = repo.create_or_get(**_base_row(slate_id))
    second = repo.create_or_get(**_base_row(slate_id))
    assert first == second
    # And only one physical row exists.
    assert len(repo.list_for_slate(slate_id)) == 1


def test_create_or_get_preserves_immutability(conn, slate_id):
    """Re-running create_or_get with a different American line but the same
    (slate, key) must NOT update the stored row — raw rows are immutable
    (design §5.1). The original implied_probability stays intact."""
    repo = OddsRowRepository(conn)
    key = compute_odds_row_key(
        fighter_name="Jon Jones",
        bookmaker="DraftKings",
        source="csv:oddsapi",
        captured_at="2026-05-20T00:00:00Z",
    )
    first = repo.create_or_get(**_base_row(slate_id, odds_row_key=key))
    # Force the same key but pretend the moneyline drifted — should be a no-op.
    second = repo.create_or_get(
        **_base_row(slate_id, odds_row_key=key, american_odds=+150)
    )
    assert first == second
    assert second.american_odds == -200  # original value, not overwritten


def test_same_key_allowed_across_different_slates(conn, slate_id, other_slate_id):
    repo = OddsRowRepository(conn)
    a = repo.create(**_base_row(slate_id))
    b = repo.create(**_base_row(other_slate_id))
    assert a.odds_row_key == b.odds_row_key
    assert a.slate_id != b.slate_id


# --- cascade ------------------------------------------------------------


def test_cascade_delete_from_slate_removes_odds_rows(conn, slate_id):
    repo = OddsRowRepository(conn)
    repo.create(**_base_row(slate_id, fighter_name_raw="A Fighter"))
    repo.create(**_base_row(slate_id, fighter_name_raw="B Fighter"))
    assert len(repo.list_for_slate(slate_id)) == 2
    conn.execute("DELETE FROM slates WHERE id = ?", (slate_id,))
    conn.commit()
    assert repo.list_for_slate(slate_id) == []


# --- implied_probability override (ODDS_CONSENSUS_DESIGN §6) -------------


def test_create_derives_implied_probability_by_default(conn, slate_id):
    repo = OddsRowRepository(conn)
    row = repo.create(**_base_row(slate_id, american_odds=-200))
    # -200 → 200/300 ≈ 0.6667 (raw implied of the line).
    assert row.implied_probability == pytest.approx(2 / 3, abs=1e-9)


def test_create_stores_exact_supplied_implied_probability(conn, slate_id):
    repo = OddsRowRepository(conn)
    # The consensus path passes the exact prob; it must be stored verbatim, not
    # re-derived from the (rounded) fair American line.
    row = repo.create(
        **_base_row(slate_id, american_odds=-117, source="consensus",
                    bookmaker="consensus"),
        implied_probability=0.46,
    )
    assert row.implied_probability == pytest.approx(0.46, abs=1e-12)


def test_create_rejects_supplied_implied_probability_out_of_range(conn, slate_id):
    repo = OddsRowRepository(conn)
    for bad in (0.0, 1.0, 1.5, -0.1):
        with pytest.raises(ValueError):
            repo.create(**_base_row(slate_id), implied_probability=bad)


# --- list_for_slate_source / delete_for_slate_source --------------------


def test_list_for_slate_source_filters_by_source(conn, slate_id):
    repo = OddsRowRepository(conn)
    repo.create(**_base_row(slate_id, fighter_name_raw="A", source="consensus",
                            bookmaker="consensus"))
    repo.create(**_base_row(slate_id, fighter_name_raw="B", source="csv:oddsapi"))
    consensus = repo.list_for_slate_source(slate_id, "consensus")
    assert [r.fighter_name_raw for r in consensus] == ["A"]


def test_delete_for_slate_source_removes_only_that_source(conn, slate_id):
    repo = OddsRowRepository(conn)
    repo.create(**_base_row(slate_id, fighter_name_raw="A", source="consensus",
                            bookmaker="consensus"))
    repo.create(**_base_row(slate_id, fighter_name_raw="B", source="csv:oddsapi"))
    deleted = repo.delete_for_slate_source(slate_id, "consensus")
    assert deleted == 1
    remaining = repo.list_for_slate(slate_id)
    assert [r.source for r in remaining] == ["csv:oddsapi"]


def test_delete_for_slate_source_requires_source(conn, slate_id):
    repo = OddsRowRepository(conn)
    with pytest.raises(ValueError):
        repo.delete_for_slate_source(slate_id, "  ")


# --- replace_for_slate_source (atomic delete + re-insert) ---------------


def _consensus_payload(fighter, american, prob, opponent):
    return {
        "fighter_name_raw": fighter,
        "american_odds": american,
        "captured_at": "2026-05-20T00:00:00Z",
        "bookmaker": "consensus",
        "opponent_name_raw": opponent,
        "implied_probability": prob,
    }


def test_replace_for_slate_source_inserts(conn, slate_id):
    repo = OddsRowRepository(conn)
    saved = repo.replace_for_slate_source(
        slate_id,
        "consensus",
        [
            _consensus_payload("Alice", -150, 0.6, "Bob"),
            _consensus_payload("Bob", +130, 0.4, "Alice"),
        ],
    )
    assert {r.fighter_name_raw for r in saved} == {"Alice", "Bob"}
    assert all(r.source == "consensus" for r in saved)
    assert all(r.bookmaker == "consensus" for r in saved)


def test_replace_for_slate_source_replaces_prior(conn, slate_id):
    repo = OddsRowRepository(conn)
    repo.replace_for_slate_source(
        slate_id, "consensus", [_consensus_payload("Alice", -150, 0.6, "Bob")]
    )
    # Re-blend with a different value: last save wins, no duplicate.
    saved = repo.replace_for_slate_source(
        slate_id, "consensus", [_consensus_payload("Alice", -200, 0.667, "Bob")]
    )
    assert len(saved) == 1
    assert saved[0].american_odds == -200
    assert saved[0].implied_probability == pytest.approx(0.667, abs=1e-9)
    assert len(repo.list_for_slate_source(slate_id, "consensus")) == 1


def test_replace_for_slate_source_leaves_other_sources(conn, slate_id):
    repo = OddsRowRepository(conn)
    repo.create(**_base_row(slate_id, fighter_name_raw="Paste Guy",
                            source="draftkings_paste", bookmaker="DraftKings"))
    repo.replace_for_slate_source(
        slate_id, "consensus", [_consensus_payload("Alice", -150, 0.6, "Bob")]
    )
    sources = {r.source for r in repo.list_for_slate(slate_id)}
    assert sources == {"draftkings_paste", "consensus"}


def test_replace_for_slate_source_empty_clears(conn, slate_id):
    repo = OddsRowRepository(conn)
    repo.replace_for_slate_source(
        slate_id, "consensus", [_consensus_payload("Alice", -150, 0.6, "Bob")]
    )
    repo.replace_for_slate_source(slate_id, "consensus", [])
    assert repo.list_for_slate_source(slate_id, "consensus") == []


def test_replace_for_slate_source_validation_is_atomic(conn, slate_id):
    # A malformed row in the batch must abort the whole replace BEFORE the
    # DELETE runs, so the prior consensus survives (params are built ahead of
    # the transaction). Mirrors the book-line repo's atomicity guarantee.
    repo = OddsRowRepository(conn)
    repo.replace_for_slate_source(
        slate_id, "consensus", [_consensus_payload("Alice", -150, 0.6, "Bob")]
    )
    bad = _consensus_payload("Bob", 0, 0.4, "Alice")  # american_odds=0 → invalid
    with pytest.raises(ValueError):
        repo.replace_for_slate_source(
            slate_id,
            "consensus",
            [_consensus_payload("Alice", -200, 0.667, "Bob"), bad],
        )
    surviving = repo.list_for_slate_source(slate_id, "consensus")
    assert len(surviving) == 1
    assert surviving[0].american_odds == -150  # the prior good value, untouched


def test_replace_for_slate_source_rejects_out_of_range_implied(conn, slate_id):
    # The shared validation must run on the batch entry point too, not just create().
    repo = OddsRowRepository(conn)
    with pytest.raises(ValueError):
        repo.replace_for_slate_source(
            slate_id, "consensus", [_consensus_payload("Alice", -150, 1.5, "Bob")]
        )
