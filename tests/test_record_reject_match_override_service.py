"""Tests for the Phase D.4.3.b reject override service.

Covers ``record_reject_match_override`` in
``src/ingestion/odds_matching_service.py`` — the composed write that
runs ``ManualMatchOverrideRepository._add_override_unlocked`` and
``_apply_overrides_unlocked`` inside a single transaction.

Design: ``docs/ODDS_PERSISTENCE_DESIGN.md`` §15.6 step 6 + §15.9.

Hard limits for D.4.3.b:

- ``match_status`` is never modified by this path.
- ``ManualMatchOverrideRepository.add_override`` public behavior is
  unchanged — this service composes the unlocked worker directly.
- A failed apply rolls back the override insert.
- Slate-scoped: another slate's overrides / persisted results are not
  read or written.
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
    OddsMatchResultRecord,
    RejectMatchOverrideResult,
    record_reject_match_override,
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
    fighter_name_raw: str = "Jose Aldo",
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


def _seed_review_required_result(
    conn: sqlite3.Connection, *, slate_id: int, fighter_name: str = "Jose Aldo"
):
    """Set up: one active fighter, one odds row, one ``review_required``
    persisted match result with ``effective_status = review_required``."""
    fighter_id = _insert_fighter(conn, slate_id=slate_id, name=fighter_name)
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw=fighter_name
    )
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            OddsMatchResultRecord(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fighter_id,
                match_status="review_required",
                effective_status="review_required",
                match_stage="fuzzy",
                match_score=90,
                preferred_candidate=fighter_name,
                opponent_check="not_applicable",
                candidates=(fighter_name,),
                notes=(),
            )
        ],
    )
    return fighter_id, row


def _select_statuses(
    conn: sqlite3.Connection, *, slate_id: int, odds_row_id: int
) -> tuple[str, str]:
    row = conn.execute(
        "SELECT match_status, effective_status FROM odds_match_results "
        "WHERE slate_id = ? AND odds_row_id = ?",
        (int(slate_id), int(odds_row_id)),
    ).fetchone()
    return row[0], row[1]


# ---------------------------------------------------------------------------
# Happy path — insert + flip
# ---------------------------------------------------------------------------


def test_inserts_override_and_flips_effective_status_when_result_exists(
    conn, slate_id
):
    fighter_id, row = _seed_review_required_result(conn, slate_id=slate_id)

    result = record_reject_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row.odds_row_key,
        fighter_id=fighter_id,
        reason="ambiguous fuzzy candidate",
    )

    assert isinstance(result, RejectMatchOverrideResult)
    assert isinstance(result.override, ManualMatchOverrideRecord)
    assert isinstance(result.apply, ApplyOverridesSummary)

    assert result.override.id > 0
    assert result.override.slate_id == slate_id
    assert result.override.odds_row_key == row.odds_row_key
    assert result.override.fighter_id == fighter_id
    assert result.override.override_type == "reject_match"
    assert result.override.reason == "ambiguous fuzzy candidate"
    assert result.override.payload_json is None
    assert result.override.superseded_at is None

    assert result.apply.slate_id == slate_id
    assert result.apply.rows_updated == 1
    assert result.apply.stale_override_ids == []

    match_status, effective_status = _select_statuses(
        conn, slate_id=slate_id, odds_row_id=row.id
    )
    assert match_status == "review_required"
    assert effective_status == "review_rejected"

    # The override is visible to the public read API and active.
    [active] = ManualMatchOverrideRepository(conn).list_active_for_slate(
        slate_id
    )
    assert active.id == result.override.id


def test_match_status_is_never_modified_by_reject_service(conn, slate_id):
    fighter_id, row = _seed_review_required_result(conn, slate_id=slate_id)

    record_reject_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row.odds_row_key,
        fighter_id=fighter_id,
    )

    # Direct column read — belt-and-braces against record-projection bugs.
    [(persisted_match_status,)] = conn.execute(
        "SELECT match_status FROM odds_match_results WHERE slate_id = ?",
        (slate_id,),
    ).fetchall()
    assert persisted_match_status == "review_required"


# ---------------------------------------------------------------------------
# Stale: no result row
# ---------------------------------------------------------------------------


def test_no_result_row_override_persists_and_apply_returns_stale_id(
    conn, slate_id
):
    """Reject targets an odds_row that has no persisted result — the
    override row is still durable, and the apply summary lists it as
    stale (design §15.4)."""
    row = _save_odds_row(conn, slate_id=slate_id)

    result = record_reject_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row.odds_row_key,
    )

    assert result.override.id > 0
    assert result.override.odds_row_key == row.odds_row_key
    assert result.override.fighter_id is None
    assert result.apply.rows_updated == 0
    assert result.apply.stale_override_ids == [result.override.id]

    [(persisted_id,)] = conn.execute(
        "SELECT id FROM manual_match_overrides WHERE slate_id = ?",
        (slate_id,),
    ).fetchall()
    assert persisted_id == result.override.id

    # The active read API also sees the new row.
    [active] = ManualMatchOverrideRepository(conn).list_active_for_slate(
        slate_id
    )
    assert active.id == result.override.id


# ---------------------------------------------------------------------------
# Validation failure does not persist
# ---------------------------------------------------------------------------


def test_validation_failure_does_not_persist_override(
    conn, slate_id, other_slate_id
):
    """Pre-DB validation in ``_add_override_unlocked`` fires before any
    INSERT — a bad input cannot leave the slate with a partially-written
    override row."""
    _, row = _seed_review_required_result(conn, slate_id=slate_id)

    repo = ManualMatchOverrideRepository(conn)
    assert repo.list_active_for_slate(slate_id) == []

    # 1) odds_row_key never inserted into any ``odds_rows`` row.
    with pytest.raises(ValueError, match="not found"):
        record_reject_match_override(
            conn,
            slate_id=slate_id,
            odds_row_key="not-a-real-key",
        )
    assert repo.list_active_for_slate(slate_id) == []

    # 2) fighter from a different slate.
    other_fid = _insert_fighter(
        conn, slate_id=other_slate_id, name="Marlon Vera"
    )
    with pytest.raises(ValueError, match="slate"):
        record_reject_match_override(
            conn,
            slate_id=slate_id,
            odds_row_key=row.odds_row_key,
            fighter_id=other_fid,
        )
    assert repo.list_active_for_slate(slate_id) == []

    # 3) missing odds_row_key.
    with pytest.raises(ValueError, match="odds_row_key"):
        record_reject_match_override(
            conn,
            slate_id=slate_id,
            odds_row_key="",
        )
    assert repo.list_active_for_slate(slate_id) == []

    # The persisted result row was never touched on any path.
    match_status, effective_status = _select_statuses(
        conn, slate_id=slate_id, odds_row_id=row.id
    )
    assert match_status == "review_required"
    assert effective_status == "review_required"


def test_validation_failure_does_not_supersede_prior_active_reject(
    conn, slate_id, other_slate_id
):
    """A prior active reject on the same ``(slate_id, odds_row_key)`` must
    survive a failed second call — supersession must not run until
    validation succeeds."""
    fighter_id, row = _seed_review_required_result(conn, slate_id=slate_id)
    other_fid = _insert_fighter(
        conn, slate_id=other_slate_id, name="Marlon Vera"
    )

    first = record_reject_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row.odds_row_key,
        fighter_id=fighter_id,
        reason="initial reject",
    )

    # Second call with same scope but invalid fighter_id.
    with pytest.raises(ValueError, match="slate"):
        record_reject_match_override(
            conn,
            slate_id=slate_id,
            odds_row_key=row.odds_row_key,
            fighter_id=other_fid,
            reason="should not land",
        )

    persisted = conn.execute(
        "SELECT id, reason, superseded_at FROM manual_match_overrides "
        "WHERE slate_id = ? ORDER BY id ASC",
        (slate_id,),
    ).fetchall()
    assert len(persisted) == 1
    assert persisted[0][0] == first.override.id
    assert persisted[0][1] == "initial reject"
    assert persisted[0][2] is None


# ---------------------------------------------------------------------------
# Apply failure rolls back override insert
# ---------------------------------------------------------------------------


def test_apply_failure_rolls_back_override_insert(
    conn, slate_id, monkeypatch
):
    """If ``_apply_overrides_unlocked`` raises after
    ``_add_override_unlocked`` has run inside the service's transaction,
    the INSERT must be rolled back — the override never becomes durable
    without the corresponding effective_status write."""
    fighter_id, row = _seed_review_required_result(conn, slate_id=slate_id)
    repo = ManualMatchOverrideRepository(conn)
    assert repo.list_active_for_slate(slate_id) == []

    import src.ingestion.odds_matching_service as svc

    def _boom(*_args, **_kwargs):
        raise RuntimeError("forced apply failure for rollback test")

    monkeypatch.setattr(svc, "_apply_overrides_unlocked", _boom)

    with pytest.raises(RuntimeError, match="forced apply failure"):
        record_reject_match_override(
            conn,
            slate_id=slate_id,
            odds_row_key=row.odds_row_key,
            fighter_id=fighter_id,
        )

    # No override row landed.
    assert repo.list_active_for_slate(slate_id) == []
    [(count,)] = conn.execute(
        "SELECT COUNT(*) FROM manual_match_overrides WHERE slate_id = ?",
        (slate_id,),
    ).fetchall()
    assert count == 0

    # The persisted result row was untouched.
    match_status, effective_status = _select_statuses(
        conn, slate_id=slate_id, odds_row_id=row.id
    )
    assert match_status == "review_required"
    assert effective_status == "review_required"


def test_apply_failure_rolls_back_supersession(
    conn, slate_id, monkeypatch
):
    """A failed apply must also roll back the UPDATE that supersedes the
    prior active reject — the previous row must remain active."""
    fighter_id, row = _seed_review_required_result(conn, slate_id=slate_id)

    first = record_reject_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row.odds_row_key,
        fighter_id=fighter_id,
        reason="first",
    )

    import src.ingestion.odds_matching_service as svc

    def _boom(*_args, **_kwargs):
        raise RuntimeError("forced apply failure for rollback test")

    monkeypatch.setattr(svc, "_apply_overrides_unlocked", _boom)

    with pytest.raises(RuntimeError, match="forced apply failure"):
        record_reject_match_override(
            conn,
            slate_id=slate_id,
            odds_row_key=row.odds_row_key,
            fighter_id=fighter_id,
            reason="second",
        )

    persisted = conn.execute(
        "SELECT id, reason, superseded_at FROM manual_match_overrides "
        "WHERE slate_id = ? ORDER BY id ASC",
        (slate_id,),
    ).fetchall()
    assert len(persisted) == 1
    assert persisted[0][0] == first.override.id
    assert persisted[0][1] == "first"
    assert persisted[0][2] is None


# ---------------------------------------------------------------------------
# Slate isolation
# ---------------------------------------------------------------------------


def test_record_reject_does_not_touch_other_slate(
    conn, slate_id, other_slate_id
):
    """Rejecting on slate A must leave slate B's overrides and persisted
    match results intact."""
    fid_a, row_a = _seed_review_required_result(conn, slate_id=slate_id)
    fid_b, row_b = _seed_review_required_result(
        conn, slate_id=other_slate_id
    )

    # Pre-existing active reject on slate B — must remain active and
    # un-superseded after the slate A reject lands.
    existing_b = ManualMatchOverrideRepository(conn).add_override(
        slate_id=other_slate_id,
        override_type="reject_match",
        odds_row_key=row_b.odds_row_key,
        fighter_id=fid_b,
        reason="slate B pre-existing",
    )

    result = record_reject_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row_a.odds_row_key,
        fighter_id=fid_a,
    )
    assert result.apply.rows_updated == 1

    # Slate A flipped.
    _, eff_a = _select_statuses(
        conn, slate_id=slate_id, odds_row_id=row_a.id
    )
    assert eff_a == "review_rejected"

    # Slate B's result row was not modified by the slate-A apply: the
    # pre-existing reject on B has not been applied yet (no apply pass
    # has run for B), so effective_status remains review_required.
    _, eff_b = _select_statuses(
        conn, slate_id=other_slate_id, odds_row_id=row_b.id
    )
    assert eff_b == "review_required"

    # Slate B's override is still active.
    [active_b] = ManualMatchOverrideRepository(conn).list_active_for_slate(
        other_slate_id
    )
    assert active_b.id == existing_b.id
    assert active_b.superseded_at is None


# ---------------------------------------------------------------------------
# Supersession through the service
# ---------------------------------------------------------------------------


def test_supersedes_prior_active_reject_leaves_only_latest_active(
    conn, slate_id
):
    """Two consecutive ``record_reject_match_override`` calls on the same
    ``(slate_id, odds_row_key)`` leave exactly one active row — the
    second — with the latest reason. Apply remains idempotent: the
    second call's ``rows_updated`` is 0 because ``effective_status`` is
    already ``review_rejected``."""
    fighter_id, row = _seed_review_required_result(conn, slate_id=slate_id)

    first = record_reject_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row.odds_row_key,
        fighter_id=fighter_id,
        reason="first",
    )
    assert first.apply.rows_updated == 1

    second = record_reject_match_override(
        conn,
        slate_id=slate_id,
        odds_row_key=row.odds_row_key,
        fighter_id=fighter_id,
        reason="second",
    )
    assert second.apply.rows_updated == 0

    persisted = conn.execute(
        "SELECT id, reason, superseded_at FROM manual_match_overrides "
        "WHERE slate_id = ? ORDER BY id ASC",
        (slate_id,),
    ).fetchall()
    assert len(persisted) == 2
    assert persisted[0][0] == first.override.id
    assert persisted[0][1] == "first"
    assert persisted[0][2] is not None
    assert persisted[1][0] == second.override.id
    assert persisted[1][1] == "second"
    assert persisted[1][2] is None

    [active] = ManualMatchOverrideRepository(conn).list_active_for_slate(
        slate_id
    )
    assert active.id == second.override.id
    assert active.reason == "second"
    assert active.superseded_at is None

    # The persisted result stays at review_rejected.
    _, effective_status = _select_statuses(
        conn, slate_id=slate_id, odds_row_id=row.id
    )
    assert effective_status == "review_rejected"
