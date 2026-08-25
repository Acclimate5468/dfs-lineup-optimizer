"""Tests for the Phase D.4.2 effective_status apply pass.

Covers ``apply_effective_status_overrides_for_slate`` and its
``_apply_overrides_unlocked`` worker in
``src/ingestion/odds_matching_service.py``.

The apply pass composes:

- ``ManualMatchOverrideRepository.list_active_for_slate`` (already filters
  ``superseded_at IS NULL``)
- ``OddsMatchResultRepository.list_for_slate`` (Phase C.3 read-side)
- ``resolve_effective_status`` (Phase D.4.1 pure resolver)

Design: ``docs/ODDS_PERSISTENCE_DESIGN.md`` §15. Hard limits for D.4.2:
update ``effective_status`` only — never ``match_status``, never
``computed_at``, no DELETE/INSERT of ``odds_match_results`` rows.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    ManualMatchOverrideRepository,
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching_service import (
    ApplyOverridesSummary,
    OddsMatchResultRecord,
    apply_effective_status_overrides_for_slate,
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


def _result(
    *,
    slate_id: int,
    odds_row_id: int,
    odds_row_key: str,
    fighter_id: int | None = None,
    match_status: str = "review_required",
    effective_status: str | None = None,
    match_stage: str = "fuzzy",
    match_score: int = 90,
    preferred_candidate: str | None = None,
    opponent_check: str = "not_applicable",
    candidates: tuple = (),
    notes: tuple = (),
) -> OddsMatchResultRecord:
    return OddsMatchResultRecord(
        slate_id=slate_id,
        odds_row_id=odds_row_id,
        odds_row_key=odds_row_key,
        fighter_id=fighter_id,
        match_status=match_status,
        effective_status=effective_status or match_status,
        match_stage=match_stage,
        match_score=match_score,
        preferred_candidate=preferred_candidate,
        opponent_check=opponent_check,
        candidates=candidates,
        notes=notes,
    )


def _raw_insert_override(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    override_type: str,
    odds_row_key: str | None = None,
    fighter_id: int | None = None,
    payload_json: str | None = None,
    reason: str | None = None,
    superseded_at: str | None = None,
) -> int:
    """Raw-SQL helper for override types ``add_override`` doesn't write yet
    and for forcing a non-null ``superseded_at`` without a paired insert."""
    cur = conn.execute(
        "INSERT INTO manual_match_overrides "
        "(slate_id, odds_row_key, fighter_id, override_type, "
        " payload_json, reason, superseded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            int(slate_id),
            odds_row_key,
            fighter_id,
            override_type,
            payload_json,
            reason,
            superseded_at,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _select_effective_status(
    conn: sqlite3.Connection, *, slate_id: int, odds_row_id: int
) -> str:
    row = conn.execute(
        "SELECT effective_status FROM odds_match_results "
        "WHERE slate_id = ? AND odds_row_id = ?",
        (int(slate_id), int(odds_row_id)),
    ).fetchone()
    return row[0]


def _select_match_status(
    conn: sqlite3.Connection, *, slate_id: int, odds_row_id: int
) -> str:
    row = conn.execute(
        "SELECT match_status FROM odds_match_results "
        "WHERE slate_id = ? AND odds_row_id = ?",
        (int(slate_id), int(odds_row_id)),
    ).fetchone()
    return row[0]


def _select_fighter_id(
    conn: sqlite3.Connection, *, slate_id: int, odds_row_id: int
):
    row = conn.execute(
        "SELECT fighter_id FROM odds_match_results "
        "WHERE slate_id = ? AND odds_row_id = ?",
        (int(slate_id), int(odds_row_id)),
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Empty cases
# ---------------------------------------------------------------------------


def test_apply_with_no_overrides_and_no_results(conn, slate_id):
    summary = apply_effective_status_overrides_for_slate(conn, slate_id)
    assert summary == ApplyOverridesSummary(
        slate_id=slate_id, rows_updated=0, stale_override_ids=[]
    )


def test_apply_with_no_overrides_leaves_fresh_results_alone(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fid,
                match_status="auto_match",
                match_stage="exact_conservative",
                match_score=100,
            )
        ],
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 0
    assert summary.stale_override_ids == []
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "auto_match"
    )


# ---------------------------------------------------------------------------
# Reset behavior — supersession + apply re-mirrors match_status
# ---------------------------------------------------------------------------


def test_apply_resets_stale_review_rejected_to_match_status(conn, slate_id):
    """A row whose ``effective_status`` was previously set by an override
    that has since been superseded must reset on the next apply call
    (design §15.6 self-healing)."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    # Seed the result row with effective_status diverging from match_status,
    # as if a prior apply pass had set it to review_rejected.
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fid,
                match_status="review_required",
                effective_status="review_rejected",
            )
        ],
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 1
    assert summary.stale_override_ids == []
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "review_required"
    )
    assert (
        _select_match_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "review_required"
    )


# ---------------------------------------------------------------------------
# Active reject_match
# ---------------------------------------------------------------------------


def test_active_reject_updates_effective_status_to_review_rejected(
    conn, slate_id
):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fid,
                match_status="review_required",
            )
        ],
    )
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
        reason="ambiguous fuzzy candidate",
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 1
    assert summary.stale_override_ids == []
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "review_rejected"
    )


def test_active_reject_does_not_modify_match_status(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fid,
                match_status="review_required",
            )
        ],
    )
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    apply_effective_status_overrides_for_slate(conn, slate_id)

    assert (
        _select_match_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "review_required"
    )


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------


def test_superseded_reject_is_ignored(conn, slate_id):
    """A reject row whose ``superseded_at`` is non-null is excluded by
    ``list_active_for_slate`` — the apply pass must not consider it."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fid,
                match_status="review_required",
            )
        ],
    )
    # Seed a reject override and immediately mark it superseded via raw SQL.
    rec = ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )
    conn.execute(
        "UPDATE manual_match_overrides SET superseded_at = "
        "'2026-05-21T00:00:00' WHERE id = ?",
        (rec.id,),
    )
    conn.commit()

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 0
    assert summary.stale_override_ids == []
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "review_required"
    )


# ---------------------------------------------------------------------------
# Stale overrides
# ---------------------------------------------------------------------------


def test_stale_reject_returns_stale_id_and_no_update(conn, slate_id):
    """An active reject whose ``odds_row_key`` has no corresponding
    ``odds_match_results`` row appears in ``stale_override_ids``."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row_with_result = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Jose Aldo"
    )
    row_no_result = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Marlon Vera",
        captured_at="2026-05-20T01:00:00Z",
    )
    # Only persist a result for the first odds row.
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row_with_result.id,
                odds_row_key=row_with_result.odds_row_key,
                fighter_id=fid,
                match_status="auto_match",
                match_stage="exact_conservative",
                match_score=100,
            )
        ],
    )
    # Reject targets the odds_row that has no result row.
    stale_rec = ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row_no_result.odds_row_key,
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 0
    assert summary.stale_override_ids == [stale_rec.id]
    # The row that does have a result is unaffected.
    assert (
        _select_effective_status(
            conn, slate_id=slate_id, odds_row_id=row_with_result.id
        )
        == "auto_match"
    )


def test_overrides_with_no_results_returns_stale_only(conn, slate_id):
    """A slate with active rejects but zero ``odds_match_results`` rows
    (Phase C never ran) yields a stale list and zero writes."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    rec = ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 0
    assert summary.stale_override_ids == [rec.id]


# ---------------------------------------------------------------------------
# Unsupported override types
# ---------------------------------------------------------------------------


def test_unsupported_override_type_ignored_and_not_stale(conn, slate_id):
    """``mark_excluded`` falls through to resolver rule 7 (mirror
    ``match_status``); it is also NOT a ``reject_match`` so the stale
    list excludes it even when it would otherwise look orphaned."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fid,
                match_status="review_required",
            )
        ],
    )
    # ``add_override`` only writes reject_match; seed mark_excluded
    # directly. odds_row_key is NULL for fighter-level overrides.
    _raw_insert_override(
        conn,
        slate_id=slate_id,
        override_type="mark_excluded",
        odds_row_key=None,
        fighter_id=fid,
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 0
    assert summary.stale_override_ids == []
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "review_required"
    )


# ---------------------------------------------------------------------------
# Row-scoping fighter_id checks
# ---------------------------------------------------------------------------


def test_mismatched_fighter_id_reject_does_not_apply(conn, slate_id):
    """Override targets ``(odds_row_key, fighter_id=B)`` but the result
    row resolved to ``fighter_id=A`` — the resolver's row-scoping
    rejects the override; effective_status stays put. Not stale: the
    odds_row_key has a result row."""
    fid_a = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    fid_b = _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fid_a,
                match_status="review_required",
            )
        ],
    )
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid_b,  # different fighter than the result row's
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 0
    assert summary.stale_override_ids == []
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "review_required"
    )


def test_result_fighter_id_none_still_applies_when_override_has_fighter_id(
    conn, slate_id
):
    """§15.11.6: a salary re-import may null ``fighter_id`` on the result
    row; an active override carrying a fighter_id still applies because
    the resolver only enforces fighter equality when BOTH sides are
    non-null."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Unknown Person"
    )
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=None,  # post-salary-reimport / unmatched
                match_status="unmatched",
                match_stage="fuzzy",
                match_score=40,
            )
        ],
    )
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 1
    assert summary.stale_override_ids == []
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "review_rejected"
    )


# ---------------------------------------------------------------------------
# Slate isolation
# ---------------------------------------------------------------------------


def test_apply_does_not_touch_other_slate(conn, slate_id, other_slate_id):
    fid_a = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    fid_b = _insert_fighter(conn, slate_id=other_slate_id, name="Jose Aldo")
    row_a = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    row_b = _save_odds_row(
        conn, slate_id=other_slate_id, fighter_name_raw="Jose Aldo"
    )

    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row_a.id,
                odds_row_key=row_a.odds_row_key,
                fighter_id=fid_a,
                match_status="review_required",
            )
        ],
    )
    repo.replace_for_slate(
        other_slate_id,
        [
            _result(
                slate_id=other_slate_id,
                odds_row_id=row_b.id,
                odds_row_key=row_b.odds_row_key,
                fighter_id=fid_b,
                match_status="review_required",
            )
        ],
    )
    # Reject on slate A only.
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row_a.odds_row_key,
        fighter_id=fid_a,
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)
    assert summary.rows_updated == 1

    # Slate A flipped.
    assert (
        _select_effective_status(
            conn, slate_id=slate_id, odds_row_id=row_a.id
        )
        == "review_rejected"
    )
    # Slate B untouched.
    assert (
        _select_effective_status(
            conn, slate_id=other_slate_id, odds_row_id=row_b.id
        )
        == "review_required"
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_apply_is_idempotent(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fid,
                match_status="review_required",
            )
        ],
    )
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    first = apply_effective_status_overrides_for_slate(conn, slate_id)
    second = apply_effective_status_overrides_for_slate(conn, slate_id)
    third = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert first.rows_updated == 1
    assert second.rows_updated == 0
    assert third.rows_updated == 0
    assert second == third
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "review_rejected"
    )


# ---------------------------------------------------------------------------
# Multi-row targeting
# ---------------------------------------------------------------------------


def test_multiple_rows_only_targeted_row_changes(conn, slate_id):
    fid_a = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    fid_b = _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")
    fid_c = _insert_fighter(conn, slate_id=slate_id, name="Conor McGregor")

    row_a = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        captured_at="2026-05-20T00:00:00Z",
    )
    row_b = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Marlon Vera",
        captured_at="2026-05-20T00:01:00Z",
    )
    row_c = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Conor McGregor",
        captured_at="2026-05-20T00:02:00Z",
    )

    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row_a.id,
                odds_row_key=row_a.odds_row_key,
                fighter_id=fid_a,
                match_status="auto_match",
                match_stage="exact_conservative",
                match_score=100,
            ),
            _result(
                slate_id=slate_id,
                odds_row_id=row_b.id,
                odds_row_key=row_b.odds_row_key,
                fighter_id=fid_b,
                match_status="review_required",
            ),
            _result(
                slate_id=slate_id,
                odds_row_id=row_c.id,
                odds_row_key=row_c.odds_row_key,
                fighter_id=fid_c,
                match_status="unmatched",
                match_stage="fuzzy",
                match_score=30,
            ),
        ],
    )
    # Reject only the middle row.
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row_b.odds_row_key,
        fighter_id=fid_b,
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 1
    assert summary.stale_override_ids == []
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row_a.id)
        == "auto_match"
    )
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row_b.id)
        == "review_rejected"
    )
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row_c.id)
        == "unmatched"
    )


# ---------------------------------------------------------------------------
# Summary shape
# ---------------------------------------------------------------------------


def test_summary_shape_and_types(conn, slate_id):
    summary = apply_effective_status_overrides_for_slate(conn, slate_id)
    assert isinstance(summary, ApplyOverridesSummary)
    assert summary.slate_id == slate_id
    assert isinstance(summary.rows_updated, int)
    assert isinstance(summary.stale_override_ids, list)


# ---------------------------------------------------------------------------
# Phase D.5.1 — accept_match / force_pair bindings write fighter_id (§16.5)
# ---------------------------------------------------------------------------


def test_active_force_pair_binds_unmatched_row(conn, slate_id):
    """A force_pair on an ``unmatched`` (fighter_id NULL) row writes both
    ``effective_status='force_pair'`` and the bound ``fighter_id``."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Bruno Gustavo da Silva"
    )
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=None,
                match_status="unmatched",
                match_stage="fuzzy",
                match_score=40,
            )
        ],
    )
    _raw_insert_override(
        conn,
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 1
    assert summary.stale_override_ids == []
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "force_pair"
    )
    assert _select_fighter_id(conn, slate_id=slate_id, odds_row_id=row.id) == fid
    # match_status stays the matcher's raw verdict (§16.8).
    assert (
        _select_match_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "unmatched"
    )


def test_active_accept_match_binds_review_required_row(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="J Aldo")
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=None,
                match_status="review_required",
            )
        ],
    )
    _raw_insert_override(
        conn,
        slate_id=slate_id,
        override_type="accept_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 1
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "review_accepted"
    )
    assert _select_fighter_id(conn, slate_id=slate_id, odds_row_id=row.id) == fid
    assert (
        _select_match_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "review_required"
    )


def test_force_pair_apply_is_idempotent(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="B G Silva")
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=None,
                match_status="unmatched",
                match_stage="fuzzy",
                match_score=40,
            )
        ],
    )
    _raw_insert_override(
        conn,
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    first = apply_effective_status_overrides_for_slate(conn, slate_id)
    second = apply_effective_status_overrides_for_slate(conn, slate_id)
    assert first.rows_updated == 1
    assert second.rows_updated == 0


def test_force_pair_with_inactive_fighter_is_stale_and_not_written(
    conn, slate_id
):
    """§16.12: a binding to a since-deactivated fighter is stale — the
    binding is NOT written and the override id is reported stale."""
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, ?)",
        (slate_id, "Bruno Silva", 8000, "excluded"),
    )
    conn.commit()
    inactive_fid = int(cur.lastrowid)
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Bruno G da Silva"
    )
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=None,
                match_status="unmatched",
                match_stage="fuzzy",
                match_score=40,
            )
        ],
    )
    stale_id = _raw_insert_override(
        conn,
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=inactive_fid,
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 0
    assert summary.stale_override_ids == [stale_id]
    # Binding withheld — row stays at the matcher's values.
    assert (
        _select_effective_status(conn, slate_id=slate_id, odds_row_id=row.id)
        == "unmatched"
    )
    assert _select_fighter_id(conn, slate_id=slate_id, odds_row_id=row.id) is None


def test_force_pair_orphaned_key_is_stale(conn, slate_id):
    """A force_pair whose odds_row_key has no result row is stale, like an
    orphaned reject."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="B G Silva")
    # No result row persisted for this slate.
    stale_id = _raw_insert_override(
        conn,
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    summary = apply_effective_status_overrides_for_slate(conn, slate_id)

    assert summary.rows_updated == 0
    assert summary.stale_override_ids == [stale_id]
