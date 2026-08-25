"""Tests for the Phase C.2 pure odds-matching service.

Covers ``compute_match_results``: it must wire persisted ``odds_rows`` and
``FighterRecord`` lists into ``match_odds_to_dk`` exactly the same way the
preview already does, then return in-memory ``OddsMatchResultRecord`` dataclasses
whose fields line up with the eventual ``odds_match_results`` columns.

The service is pure / in-memory — these tests never read or write
``odds_match_results``.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    FightGroupRepository,
    FighterRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching import (
    OPPONENT_FAILED,
    OPPONENT_NOT_APPLICABLE,
    OPPONENT_PASSED,
    STAGE_EXACT_AGGRESSIVE,
    STAGE_EXACT_CONSERVATIVE,
    STAGE_FUZZY,
    STATUS_AUTO,
    STATUS_REVIEW,
    STATUS_UNMATCHED,
)
from src.ingestion.odds_matching_service import (
    EmptyDkRosterError,
    OddsMatchResultRecord,
    compute_match_results,
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


def _insert_fighter(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    name: str,
    salary: int = 8000,
    status: str = "active",
) -> int:
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, ?)",
        (int(slate_id), name, int(salary), status),
    )
    conn.commit()
    return int(cur.lastrowid)


def _save_odds_row(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    fighter_name_raw: str,
    american_odds: int = -150,
    opponent_name_raw: str | None = None,
    captured_at: str = "2026-05-20T00:00:00Z",
    source: str = "manual",
    bookmaker: str | None = None,
):
    return OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source=source,
        captured_at=captured_at,
        opponent_name_raw=opponent_name_raw,
        bookmaker=bookmaker,
    )


# ---------------------------------------------------------------------------
# Auto match (happy path)
# ---------------------------------------------------------------------------


def test_compute_auto_match_resolves_fighter_id(conn, slate_id):
    aldo_id = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")

    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    fighters = FighterRepository(conn).list_for_slate(slate_id)

    [result] = compute_match_results(
        slate_id=slate_id, odds_rows=[row], fighters=fighters
    )

    assert isinstance(result, OddsMatchResultRecord)
    assert result.slate_id == slate_id
    assert result.odds_row_id == row.id
    assert result.odds_row_key == row.odds_row_key
    assert result.fighter_id == aldo_id
    assert result.match_status == STATUS_AUTO
    assert result.effective_status == STATUS_AUTO
    assert result.match_stage == STAGE_EXACT_CONSERVATIVE
    assert result.match_score == 100
    assert result.preferred_candidate is None
    assert result.opponent_check == OPPONENT_NOT_APPLICABLE
    assert result.candidates == ()
    assert result.notes == ()


def test_compute_preserves_input_order(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Conor McGregor")

    a = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Conor McGregor",
        captured_at="2026-05-20T00:00:00Z",
    )
    b = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        captured_at="2026-05-20T00:01:00Z",
    )
    fighters = FighterRepository(conn).list_for_slate(slate_id)

    results = compute_match_results(
        slate_id=slate_id, odds_rows=[a, b], fighters=fighters
    )
    assert [r.odds_row_id for r in results] == [a.id, b.id]
    assert [r.match_status for r in results] == [STATUS_AUTO, STATUS_AUTO]


# ---------------------------------------------------------------------------
# Review / unmatched
# ---------------------------------------------------------------------------


def test_compute_review_required_for_ambiguous_aggressive(conn, slate_id):
    """Two DK fighters collide on the aggressive key — matcher returns
    ``review_required`` with both listed; service must propagate candidates
    and refuse to assign a fighter_id (would invent an arbitrary pick)."""
    _insert_fighter(conn, slate_id=slate_id, name="Dan Smith")
    _insert_fighter(conn, slate_id=slate_id, name="Daniel Smith")

    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Daniel Smith Jr."
    )
    fighters = FighterRepository(conn).list_for_slate(slate_id)

    [result] = compute_match_results(
        slate_id=slate_id, odds_rows=[row], fighters=fighters
    )

    assert result.match_status == STATUS_REVIEW
    assert result.effective_status == STATUS_REVIEW
    assert result.fighter_id is None
    assert set(result.candidates) == {"Dan Smith", "Daniel Smith"}
    assert result.match_stage == STAGE_EXACT_AGGRESSIVE
    assert "ambiguous_aggressive" in result.notes


def test_compute_review_required_in_fuzzy_band_still_maps_fighter_id(conn, slate_id):
    """Fuzzy 88–94 → review_required, but the matcher still picks one DK
    fighter. The service should map that name back to its fighter_id even
    though the row needs human review (the reviewer needs an FK to act on)."""
    mck_id = _insert_fighter(
        conn, slate_id=slate_id, name="Terrence McKinney"
    )
    _insert_fighter(conn, slate_id=slate_id, name="Drew Dober")

    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Terrance Mckinney"
    )
    fighters = FighterRepository(conn).list_for_slate(slate_id)

    [result] = compute_match_results(
        slate_id=slate_id, odds_rows=[row], fighters=fighters
    )

    assert result.match_status == STATUS_REVIEW
    assert result.effective_status == STATUS_REVIEW
    assert result.fighter_id == mck_id
    assert result.match_stage == STAGE_FUZZY
    assert 88 <= result.match_score < 95


def test_compute_unmatched_leaves_fighter_id_none(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Conor McGregor")

    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Totally Unrelated Person"
    )
    fighters = FighterRepository(conn).list_for_slate(slate_id)

    [result] = compute_match_results(
        slate_id=slate_id, odds_rows=[row], fighters=fighters
    )

    assert result.match_status == STATUS_UNMATCHED
    assert result.effective_status == STATUS_UNMATCHED
    assert result.fighter_id is None
    assert result.match_score < 88


# ---------------------------------------------------------------------------
# Effective status mirrors match status in Phase C
# ---------------------------------------------------------------------------


def test_effective_status_equals_match_status_across_all_statuses(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Dan Smith")
    _insert_fighter(conn, slate_id=slate_id, name="Daniel Smith")

    rows = [
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw="Jose Aldo",
            captured_at="2026-05-20T00:00:00Z",
        ),
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw="Daniel Smith Jr.",
            captured_at="2026-05-20T00:01:00Z",
        ),
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw="Totally Unrelated Person",
            captured_at="2026-05-20T00:02:00Z",
        ),
    ]
    fighters = FighterRepository(conn).list_for_slate(slate_id)

    results = compute_match_results(
        slate_id=slate_id, odds_rows=rows, fighters=fighters
    )

    statuses = [r.match_status for r in results]
    assert set(statuses) == {STATUS_AUTO, STATUS_REVIEW, STATUS_UNMATCHED}
    for r in results:
        assert r.effective_status == r.match_status


# ---------------------------------------------------------------------------
# Opponent context (fight groups)
# ---------------------------------------------------------------------------


def test_opponent_context_demotes_auto_match_to_review(conn, slate_id):
    aldo_id = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")

    # Confirmed pairing: Jose Aldo vs Marlon Vera.
    fg = FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
    )
    FightGroupRepository(conn).update_status(fg.id, "confirmed")

    # Odds row claims Jose Aldo is fighting Conor McGregor → mismatch.
    row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        opponent_name_raw="Conor McGregor",
    )
    fighters = FighterRepository(conn).list_for_slate(slate_id)
    fight_groups = FightGroupRepository(conn).list_for_slate(slate_id)

    [result] = compute_match_results(
        slate_id=slate_id,
        odds_rows=[row],
        fighters=fighters,
        fight_groups=fight_groups,
    )
    assert result.match_status == STATUS_REVIEW
    assert result.fighter_id == aldo_id
    assert result.opponent_check == OPPONENT_FAILED
    assert "opponent_mismatch" in result.notes


def test_opponent_context_passes_through_when_aligned(conn, slate_id):
    aldo_id = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")

    fg = FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
    )
    FightGroupRepository(conn).update_status(fg.id, "confirmed")

    row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        opponent_name_raw="Marlon Vera",
    )
    fighters = FighterRepository(conn).list_for_slate(slate_id)
    fight_groups = FightGroupRepository(conn).list_for_slate(slate_id)

    [result] = compute_match_results(
        slate_id=slate_id,
        odds_rows=[row],
        fighters=fighters,
        fight_groups=fight_groups,
    )
    assert result.match_status == STATUS_AUTO
    assert result.fighter_id == aldo_id
    assert result.opponent_check == OPPONENT_PASSED


# ---------------------------------------------------------------------------
# Active-only roster filtering
# ---------------------------------------------------------------------------


def test_excluded_fighter_is_not_a_match_candidate(conn, slate_id):
    """Design §14.3 mode A: excluded fighters never appear as candidates.
    An odds row for an excluded fighter must land as ``unmatched`` even
    though the name exists in the ``fighters`` table."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", status="excluded")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", status="active")

    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    fighters = FighterRepository(conn).list_for_slate(slate_id)

    [result] = compute_match_results(
        slate_id=slate_id, odds_rows=[row], fighters=fighters
    )
    assert result.match_status == STATUS_UNMATCHED
    assert result.fighter_id is None


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------


def test_empty_odds_rows_returns_empty_list(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    fighters = FighterRepository(conn).list_for_slate(slate_id)
    assert (
        compute_match_results(
            slate_id=slate_id, odds_rows=[], fighters=fighters
        )
        == []
    )


def test_empty_active_roster_raises_blocked_error(conn, slate_id):
    """No active ``fighters`` rows → service refuses rather than emit
    throwaway ``unmatched`` rows (design §14.3 mode B / §13.12)."""
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    fighters = FighterRepository(conn).list_for_slate(slate_id)
    assert fighters == []
    with pytest.raises(EmptyDkRosterError) as excinfo:
        compute_match_results(
            slate_id=slate_id, odds_rows=[row], fighters=fighters
        )
    assert excinfo.value.slate_id == slate_id


def test_all_excluded_roster_raises_blocked_error(conn, slate_id):
    """A salary CSV that only contains excluded fighters is functionally
    empty for matching — refuse rather than match against nothing."""
    _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", status="excluded"
    )
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    fighters = FighterRepository(conn).list_for_slate(slate_id)
    with pytest.raises(EmptyDkRosterError):
        compute_match_results(
            slate_id=slate_id, odds_rows=[row], fighters=fighters
        )


# ---------------------------------------------------------------------------
# Candidates / notes preservation
# ---------------------------------------------------------------------------


def test_candidates_and_notes_passed_through_verbatim(conn, slate_id):
    """Ambiguous case with opponent context surfaces preferred_candidate +
    ``opponent_supported_disambiguation`` note. The service must propagate
    that order verbatim — no re-sorting, no dropped notes."""
    _insert_fighter(conn, slate_id=slate_id, name="Dan Smith")
    _insert_fighter(conn, slate_id=slate_id, name="Daniel Smith")
    _insert_fighter(conn, slate_id=slate_id, name="Drew Dober")

    fg = FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Daniel Smith",
        fighter_2_name="Drew Dober",
    )
    FightGroupRepository(conn).update_status(fg.id, "confirmed")

    row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Daniel Smith Jr.",
        opponent_name_raw="Drew Dober",
    )
    fighters = FighterRepository(conn).list_for_slate(slate_id)
    fight_groups = FightGroupRepository(conn).list_for_slate(slate_id)

    [result] = compute_match_results(
        slate_id=slate_id,
        odds_rows=[row],
        fighters=fighters,
        fight_groups=fight_groups,
    )
    assert result.match_status == STATUS_REVIEW
    assert result.fighter_id is None  # ambiguous → no invented FK
    assert set(result.candidates) == {"Dan Smith", "Daniel Smith"}
    assert result.preferred_candidate == "Daniel Smith"
    assert "ambiguous_aggressive" in result.notes
    assert "opponent_supported_disambiguation" in result.notes
    assert result.notes.index("ambiguous_aggressive") < result.notes.index(
        "opponent_supported_disambiguation"
    )


def test_empty_fighter_name_propagates_empty_fighter_note(conn, slate_id):
    """An odds row whose fighter name is blank should never reach
    ``odds_rows`` (the repo rejects it), but the matcher's behavior for
    empty input is part of the contract. Drive the matcher with an
    instance that bypasses the repo to verify the service does not drop
    the ``empty_fighter`` note."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")

    # Hand-construct an OddsRowRecord with an empty fighter name to test
    # propagation only; the repository would reject this on insert.
    from src.db.repositories import OddsRowRecord  # local import: test-only

    fake = OddsRowRecord(
        id=999,
        slate_id=slate_id,
        odds_row_key="fake-key",
        fighter_name_raw="",
        fighter_name_normalized="",
        opponent_name_raw=None,
        american_odds=-150,
        implied_probability=0.6,
        bookmaker=None,
        source="manual",
        captured_at="2026-05-20T00:00:00Z",
        imported_at="2026-05-20T00:00:00Z",
        import_batch_id=None,
    )
    fighters = FighterRepository(conn).list_for_slate(slate_id)
    [result] = compute_match_results(
        slate_id=slate_id, odds_rows=[fake], fighters=fighters
    )
    assert result.match_status == STATUS_UNMATCHED
    assert result.fighter_id is None
    assert "empty_fighter" in result.notes
