"""Tests for the Phase D.5.1 assign override service.

Covers ``record_assign_match_override`` in
``src/ingestion/odds_matching_service.py`` — the composed write that
derives ``accept_match`` vs ``force_pair`` (§16.10), inserts the override
via ``ManualMatchOverrideRepository._add_override_unlocked`` (validation
§16.11 + cross-type supersession §16.4), and runs
``_apply_overrides_unlocked`` (binding write §16.5) inside one
transaction.

Design: ``docs/ODDS_PERSISTENCE_DESIGN.md`` §16.

Sequencing note (§16.14): D.5.1 writes the binding and ``fighter_id`` but
projections still read ``match_status == 'auto_match'`` until D.5.2, so
these tests assert persistence/binding only — NOT that Build un-excludes
the fighter. That is the D.5.2 integration test's job.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    ManualMatchOverrideRecord,
    ManualMatchOverrideRepository,
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching_service import (
    ApplyOverridesSummary,
    AssignMatchOverrideResult,
    OddsMatchResultRecord,
    record_assign_match_override,
    record_reject_match_override,
    recompute_and_replace_match_results,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


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
    captured_at: str = "2026-05-20T00:00:00Z",
    source: str = "manual",
):
    return OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source=source,
        captured_at=captured_at,
    )


def _result_for_key(conn, *, slate_id, odds_row_key):
    return next(
        r
        for r in OddsMatchResultRepository(conn).list_for_slate(slate_id)
        if r.odds_row_key == odds_row_key
    )


def _seed_unmatched(conn, *, slate_id):
    """An active fighter whose name-mismatched odds row the matcher leaves
    ``unmatched`` (the §16.1 force_pair case)."""
    bruno_id = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Totally Unrelated Person"
    )
    recompute_and_replace_match_results(conn, slate_id)
    return bruno_id, row


def _seed_review_row(
    conn,
    *,
    slate_id,
    matched_fighter_id,
    preferred_candidate,
    candidates,
):
    """Persist one ``review_required`` result row directly so the
    accept/force derivation can be exercised without depending on the
    matcher's fuzzy-scoring internals.

    A fuzzy 88–94 single candidate has ``matched_fighter_id`` set and
    ``preferred_candidate`` None; an opponent-disambiguated ambiguous row
    has ``matched_fighter_id`` None and ``preferred_candidate`` named."""
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Some Odds Name"
    )
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            OddsMatchResultRecord(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=matched_fighter_id,
                match_status="review_required",
                effective_status="review_required",
                match_stage="fuzzy",
                match_score=90,
                preferred_candidate=preferred_candidate,
                opponent_check="not_applicable",
                candidates=tuple(candidates),
                notes=(),
            )
        ],
    )
    return row


# ---------------------------------------------------------------------------
# Type derivation (§16.10)
# ---------------------------------------------------------------------------


def test_unmatched_row_derives_force_pair(conn, slate_id):
    bruno_id, row = _seed_unmatched(conn, slate_id=slate_id)

    result = record_assign_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row.odds_row_key,
        fighter_id=bruno_id,
        reason="name diverged beyond auto bar",
    )

    assert isinstance(result, AssignMatchOverrideResult)
    assert result.override_type == "force_pair"
    assert result.override.override_type == "force_pair"
    assert result.override.fighter_id == bruno_id
    assert result.override.reason == "name diverged beyond auto bar"
    assert isinstance(result.apply, ApplyOverridesSummary)

    rec = _result_for_key(conn, slate_id=slate_id, odds_row_key=row.odds_row_key)
    assert rec.match_status == "unmatched"          # raw matcher verdict kept
    assert rec.effective_status == "force_pair"
    assert rec.fighter_id == bruno_id


def test_review_fuzzy_matched_fighter_derives_accept_match(conn, slate_id):
    """Fuzzy 88–94 single candidate (matcher proposed a fighter via
    ``fighter_id``): confirming that fighter is an accept_match (§16.2)."""
    matched_fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")
    row = _seed_review_row(
        conn,
        slate_id=slate_id,
        matched_fighter_id=matched_fid,
        preferred_candidate=None,
        candidates=("Jose Aldo",),
    )

    result = record_assign_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row.odds_row_key,
        fighter_id=matched_fid,
    )

    assert result.override_type == "accept_match"
    rec = _result_for_key(conn, slate_id=slate_id, odds_row_key=row.odds_row_key)
    assert rec.match_status == "review_required"
    assert rec.effective_status == "review_accepted"
    assert rec.fighter_id == matched_fid


def test_review_preferred_candidate_name_derives_accept_match(conn, slate_id):
    """Opponent-disambiguated ambiguous row (matcher named its pick in
    ``preferred_candidate``, ``fighter_id`` NULL): confirming that named
    fighter is an accept_match."""
    preferred_fid = _insert_fighter(
        conn, slate_id=slate_id, name="Daniel Smith"
    )
    _insert_fighter(conn, slate_id=slate_id, name="Dan Smith")
    row = _seed_review_row(
        conn,
        slate_id=slate_id,
        matched_fighter_id=None,
        preferred_candidate="Daniel Smith",
        candidates=("Dan Smith", "Daniel Smith"),
    )

    result = record_assign_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row.odds_row_key,
        fighter_id=preferred_fid,
    )

    assert result.override_type == "accept_match"
    rec = _result_for_key(conn, slate_id=slate_id, odds_row_key=row.odds_row_key)
    assert rec.effective_status == "review_accepted"
    assert rec.fighter_id == preferred_fid


def test_review_other_fighter_derives_force_pair(conn, slate_id):
    """A review_required row resolved to a fighter the matcher did NOT
    propose is a force_pair."""
    matched_fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    other_fid = _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")
    row = _seed_review_row(
        conn,
        slate_id=slate_id,
        matched_fighter_id=matched_fid,
        preferred_candidate=None,
        candidates=("Jose Aldo",),
    )

    result = record_assign_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row.odds_row_key,
        fighter_id=other_fid,
    )

    assert result.override_type == "force_pair"
    rec = _result_for_key(conn, slate_id=slate_id, odds_row_key=row.odds_row_key)
    assert rec.effective_status == "force_pair"
    assert rec.fighter_id == other_fid


# ---------------------------------------------------------------------------
# Validation failures do not persist
# ---------------------------------------------------------------------------


def test_inactive_fighter_rejected_and_nothing_persists(conn, slate_id):
    _, row = _seed_unmatched(conn, slate_id=slate_id)
    inactive_fid = _insert_fighter(
        conn, slate_id=slate_id, name="Inactive Guy", status="excluded"
    )
    repo = ManualMatchOverrideRepository(conn)

    with pytest.raises(ValueError, match="not active"):
        record_assign_match_override(
            conn,
            slate_id=slate_id,
            odds_row_key=row.odds_row_key,
            fighter_id=inactive_fid,
        )

    assert repo.list_active_for_slate(slate_id) == []
    rec = _result_for_key(conn, slate_id=slate_id, odds_row_key=row.odds_row_key)
    assert rec.effective_status == "unmatched"
    assert rec.fighter_id is None


def test_wrong_slate_fighter_rejected(conn, slate_id, other_slate_id):
    _, row = _seed_unmatched(conn, slate_id=slate_id)
    other_fid = _insert_fighter(
        conn, slate_id=other_slate_id, name="Conor McGregor"
    )
    with pytest.raises(ValueError, match="slate"):
        record_assign_match_override(
            conn,
            slate_id=slate_id,
            odds_row_key=row.odds_row_key,
            fighter_id=other_fid,
        )
    assert ManualMatchOverrideRepository(conn).list_active_for_slate(
        slate_id
    ) == []


def test_already_bound_fighter_rejected(conn, slate_id):
    bruno_id, row = _seed_unmatched(conn, slate_id=slate_id)
    # First binding sticks.
    record_assign_match_override(
        conn, slate_id=slate_id, odds_row_key=row.odds_row_key, fighter_id=bruno_id
    )
    # A second odds row the user tries to bind to the SAME fighter.
    row_b = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Another Unrelated Person",
        captured_at="2026-05-20T05:00:00Z",
    )
    recompute_and_replace_match_results(conn, slate_id)

    with pytest.raises(ValueError, match="already bound"):
        record_assign_match_override(
            conn,
            slate_id=slate_id,
            odds_row_key=row_b.odds_row_key,
            fighter_id=bruno_id,
        )


# ---------------------------------------------------------------------------
# Idempotence + supersession
# ---------------------------------------------------------------------------


def test_idempotent_reassign_same_key_same_fighter(conn, slate_id):
    bruno_id, row = _seed_unmatched(conn, slate_id=slate_id)

    first = record_assign_match_override(
        conn, slate_id=slate_id, odds_row_key=row.odds_row_key, fighter_id=bruno_id
    )
    assert first.apply.rows_updated == 1

    second = record_assign_match_override(
        conn, slate_id=slate_id, odds_row_key=row.odds_row_key, fighter_id=bruno_id
    )
    # The binding is already in place — the apply writes zero changed rows.
    assert second.apply.rows_updated == 0

    persisted = conn.execute(
        "SELECT id, superseded_at FROM manual_match_overrides "
        "WHERE slate_id = ? ORDER BY id ASC",
        (slate_id,),
    ).fetchall()
    assert len(persisted) == 2
    assert persisted[0][0] == first.override.id and persisted[0][1] is not None
    assert persisted[1][0] == second.override.id and persisted[1][1] is None


def test_assign_supersedes_active_reject_recovery_path(conn, slate_id):
    """§16.4: assigning a previously-rejected row supersedes the reject and
    binds the fighter."""
    bruno_id, row = _seed_unmatched(conn, slate_id=slate_id)
    rejected = record_reject_match_override(
        conn, slate_id=slate_id, odds_row_key=row.odds_row_key
    )
    assert (
        _result_for_key(
            conn, slate_id=slate_id, odds_row_key=row.odds_row_key
        ).effective_status
        == "review_rejected"
    )

    assigned = record_assign_match_override(
        conn, slate_id=slate_id, odds_row_key=row.odds_row_key, fighter_id=bruno_id
    )

    [active] = ManualMatchOverrideRepository(conn).list_active_for_slate(
        slate_id
    )
    assert active.id == assigned.override.id
    assert active.override_type == "force_pair"
    superseded = conn.execute(
        "SELECT superseded_at FROM manual_match_overrides WHERE id = ?",
        (rejected.override.id,),
    ).fetchone()
    assert superseded[0] is not None
    rec = _result_for_key(conn, slate_id=slate_id, odds_row_key=row.odds_row_key)
    assert rec.effective_status == "force_pair"
    assert rec.fighter_id == bruno_id


def test_later_reject_supersedes_active_assign(conn, slate_id):
    bruno_id, row = _seed_unmatched(conn, slate_id=slate_id)
    assigned = record_assign_match_override(
        conn, slate_id=slate_id, odds_row_key=row.odds_row_key, fighter_id=bruno_id
    )
    rejected = record_reject_match_override(
        conn, slate_id=slate_id, odds_row_key=row.odds_row_key
    )

    [active] = ManualMatchOverrideRepository(conn).list_active_for_slate(
        slate_id
    )
    assert active.id == rejected.override.id
    assert active.override_type == "reject_match"
    superseded = conn.execute(
        "SELECT superseded_at FROM manual_match_overrides WHERE id = ?",
        (assigned.override.id,),
    ).fetchone()
    assert superseded[0] is not None
    assert (
        _result_for_key(
            conn, slate_id=slate_id, odds_row_key=row.odds_row_key
        ).effective_status
        == "review_rejected"
    )


# ---------------------------------------------------------------------------
# Apply failure rolls back the insert
# ---------------------------------------------------------------------------


def test_apply_failure_rolls_back_assign_insert(conn, slate_id, monkeypatch):
    bruno_id, row = _seed_unmatched(conn, slate_id=slate_id)
    repo = ManualMatchOverrideRepository(conn)
    assert repo.list_active_for_slate(slate_id) == []

    import src.ingestion.odds_matching_service as svc

    def _boom(*_args, **_kwargs):
        raise RuntimeError("forced apply failure for rollback test")

    monkeypatch.setattr(svc, "_apply_overrides_unlocked", _boom)

    with pytest.raises(RuntimeError, match="forced apply failure"):
        record_assign_match_override(
            conn,
            slate_id=slate_id,
            odds_row_key=row.odds_row_key,
            fighter_id=bruno_id,
        )

    assert repo.list_active_for_slate(slate_id) == []
    rec = _result_for_key(conn, slate_id=slate_id, odds_row_key=row.odds_row_key)
    assert rec.effective_status == "unmatched"
    assert rec.fighter_id is None


# ---------------------------------------------------------------------------
# Slate isolation
# ---------------------------------------------------------------------------


def test_assign_does_not_touch_other_slate(conn, slate_id, other_slate_id):
    bruno_id, row = _seed_unmatched(conn, slate_id=slate_id)
    other_bruno_id, other_row = _seed_unmatched(conn, slate_id=other_slate_id)

    record_assign_match_override(
        conn, slate_id=slate_id, odds_row_key=row.odds_row_key, fighter_id=bruno_id
    )

    # Slate B's result row is untouched (its own apply never ran).
    other_rec = _result_for_key(
        conn, slate_id=other_slate_id, odds_row_key=other_row.odds_row_key
    )
    assert other_rec.effective_status == "unmatched"
    assert other_rec.fighter_id is None
    assert (
        ManualMatchOverrideRepository(conn).list_active_for_slate(other_slate_id)
        == []
    )
