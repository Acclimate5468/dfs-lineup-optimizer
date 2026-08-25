"""Tests for Optimizer v1 service orchestration (Slice B.4).

Covers ``run_optimizer`` in ``src/optimizer/optimizer_service.py`` per
``docs/OPTIMIZER_V1_DESIGN.md`` §5.3 and the Manual Review Gate
contract from ``docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`` §1 / §5 / §7.

The service composes :func:`evaluate_manual_review`,
:func:`build_optimizer_pool`, and :func:`solve_lineups`; these tests
seed the DB end to end and assert the composition behavior — gate
short-circuit, pool/solver pass-through, read-only invariant,
``effective_status`` deferral, and ``n_lineups`` bounds propagation.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    FightGroupRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching_service import (
    recompute_and_replace_match_results,
)
from src.optimizer import optimizer_service
from src.optimizer.constraints import UFCClassicConstraints
from src.optimizer.lineup_solver import (
    SolveResult,
    STATUS_INFEASIBLE_CONSTRAINTS,
    STATUS_INFEASIBLE_POOL_TOO_SMALL,
    STATUS_OK,
)
from src.optimizer.optimizer_service import (
    ManualReviewGateError,
    run_optimizer,
)
from src.slate.manual_review_service import ReviewReadiness


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
    return SlateRepository(conn).create(
        event_name="UFC 999",
        salary_csv_status="validated",
        salary_row_count=8,
    ).id


def _insert_fighter(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    name: str,
    salary: int = 8000,
) -> int:
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, 'active')",
        (int(slate_id), name, int(salary)),
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
):
    return OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source="manual",
        captured_at=captured_at,
    )


def _seed_ready_slate_minus_ack(conn, slate_id) -> list[int]:
    """Six confirmed fights (twelve active fighters), all auto-matched
    with odds. Every Blocking check passes except
    ``manual_review_user_ack`` — the gate is not green until the
    caller flips ``set_manual_review_reviewed``.

    Twelve fighters across six fights is the smallest setup that lets
    the solver actually fill a 6-fighter lineup under the at-most-one-
    per-fight constraint. Uniform $8,000 salaries fit any combination
    under the 50k cap with room to spare.

    Returns the fighter ids in insertion order.
    """
    fights = (
        ("F1_A", "F1_B"),
        ("F2_A", "F2_B"),
        ("F3_A", "F3_B"),
        ("F4_A", "F4_B"),
        ("F5_A", "F5_B"),
        ("F6_A", "F6_B"),
    )
    fids: list[int] = []
    for a, b in fights:
        fids.append(_insert_fighter(conn, slate_id=slate_id, name=a, salary=8000))
        fids.append(_insert_fighter(conn, slate_id=slate_id, name=b, salary=8000))

    for a, b in fights:
        fg = FightGroupRepository(conn).create(
            slate_id=slate_id,
            fighter_1_name=a,
            fighter_2_name=b,
            scheduled_rounds=3,
        )
        FightGroupRepository(conn).update_status(fg.id, "confirmed")

    odds_pattern = (-150, +130)
    for i, (a, b) in enumerate(fights):
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw=a,
            american_odds=odds_pattern[0],
            captured_at=f"2026-05-20T00:00:{2 * i:02d}Z",
        )
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw=b,
            american_odds=odds_pattern[1],
            captured_at=f"2026-05-20T00:00:{2 * i + 1:02d}Z",
        )
    recompute_and_replace_match_results(conn, slate_id)
    return fids


def _seed_ready_slate(conn, slate_id) -> list[int]:
    """As :func:`_seed_ready_slate_minus_ack` but also marks the slate
    manually reviewed so every Blocking check passes — the gate is
    green and :func:`run_optimizer` can proceed to the solver."""
    fids = _seed_ready_slate_minus_ack(conn, slate_id)
    SlateRepository(conn).set_manual_review_reviewed(slate_id)
    return fids


def _seed_undersized_pool_but_ready_gate(conn, slate_id) -> list[int]:
    """Eight confirmed-group fighters across four fights; only five
    have auto-matched odds. The pool ends up with five ``"ok"``
    projection rows (the three opponents without odds fall to
    ``missing_inputs``), which is below the lineup size of six.

    The gate stays ready: 5-of-8 covered is 37.5% uncovered, below the
    §5.4.a 50% Blocking threshold; ``missing_inputs`` is a Warning,
    never Blocking; ``non_projectable`` stays zero because every
    fighter is paired into a confirmed group.
    """
    fights = (
        ("U1_A", "U1_B"),
        ("U2_A", "U2_B"),
        ("U3_A", "U3_B"),
        ("U4_A", "U4_B"),
    )
    fids: list[int] = []
    for a, b in fights:
        fids.append(_insert_fighter(conn, slate_id=slate_id, name=a, salary=8000))
        fids.append(_insert_fighter(conn, slate_id=slate_id, name=b, salary=8000))

    for a, b in fights:
        fg = FightGroupRepository(conn).create(
            slate_id=slate_id,
            fighter_1_name=a,
            fighter_2_name=b,
            scheduled_rounds=3,
        )
        FightGroupRepository(conn).update_status(fg.id, "confirmed")

    # Five auto-matched odds rows: both sides of the first two fights
    # plus the A-side of the third. Three opponents are left without
    # odds → missing_inputs in projection → excluded from the pool.
    covered = (
        ("U1_A", -150, 0),
        ("U1_B", +130, 1),
        ("U2_A", -120, 2),
        ("U2_B", +110, 3),
        ("U3_A", -200, 4),
    )
    for name, odds, i in covered:
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw=name,
            american_odds=odds,
            captured_at=f"2026-05-20T00:00:{i:02d}Z",
        )
    recompute_and_replace_match_results(conn, slate_id)
    SlateRepository(conn).set_manual_review_reviewed(slate_id)
    return fids


def _db_snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Snapshot every table the service might touch transitively. Used
    to assert :func:`run_optimizer` is read-only."""
    tables = (
        "slates",
        "fighters",
        "fight_groups",
        "odds_rows",
        "odds_match_results",
        "manual_match_overrides",
    )
    snap: dict[str, list[tuple]] = {}
    for table in tables:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        ).fetchall()
        snap[table] = [tuple(r) for r in rows]
    return snap


# ---------------------------------------------------------------------------
# Gate gating (design §4 / §5.3)
# ---------------------------------------------------------------------------


def test_unreviewed_slate_raises_manual_review_gate_error(conn, slate_id):
    """Gate not green → :class:`ManualReviewGateError` carrying the
    readiness snapshot. The slate is otherwise fully seeded; only the
    Mark-Reviewed click is missing."""
    _seed_ready_slate_minus_ack(conn, slate_id)

    with pytest.raises(ManualReviewGateError) as exc:
        run_optimizer(conn, slate_id=slate_id, n_lineups=1)

    err = exc.value
    assert err.slate_id == slate_id
    assert isinstance(err.readiness, ReviewReadiness)
    assert err.readiness.summary.ready is False
    assert err.readiness.summary.blocking_count >= 1


def test_unknown_slate_raises_manual_review_gate_error(conn):
    """An unknown slate id surfaces as a gate failure (the Phase C
    aggregator emits a single ``salary_imported`` Blocking row for
    unknown slates), not as some other exception type."""
    with pytest.raises(ManualReviewGateError) as exc:
        run_optimizer(conn, slate_id=999_999, n_lineups=1)
    assert exc.value.slate_id == 999_999
    assert exc.value.readiness.summary.ready is False


def test_gate_failure_short_circuits_before_pool_or_solver(
    conn, slate_id, monkeypatch
):
    """Per design §5.3 step 2, the pool builder and the solver must
    not be called when the gate fails."""
    _seed_ready_slate_minus_ack(conn, slate_id)

    pool_calls: list = []
    solver_calls: list = []

    def _boom_pool(*a, **kw):
        pool_calls.append((a, kw))
        raise AssertionError("build_optimizer_pool must not run when gate fails")

    def _boom_solve(*a, **kw):
        solver_calls.append((a, kw))
        raise AssertionError("solve_lineups must not run when gate fails")

    monkeypatch.setattr(optimizer_service, "build_optimizer_pool", _boom_pool)
    monkeypatch.setattr(optimizer_service, "solve_lineups", _boom_solve)

    with pytest.raises(ManualReviewGateError):
        run_optimizer(conn, slate_id=slate_id, n_lineups=1)

    assert pool_calls == []
    assert solver_calls == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_reviewed_ready_slate_returns_valid_solve_result(conn, slate_id):
    fids = _seed_ready_slate(conn, slate_id)

    res = run_optimizer(conn, slate_id=slate_id, n_lineups=1)
    assert isinstance(res, SolveResult)
    assert res.slate_id == slate_id
    assert res.status == STATUS_OK
    assert res.reason == ""
    assert len(res.lineups) == 1

    lu = res.lineups[0]
    assert len(lu.fighter_ids) == 6
    assert len(set(lu.fighter_ids)) == 6  # no duplicates
    assert lu.total_salary <= 50_000
    assert set(lu.fighter_ids).issubset(set(fids))
    # Same-fight: one pick per confirmed fight. With six fights and a
    # six-fighter lineup, the only feasible shape is exactly one
    # fighter per fight group.
    picked_groups = {
        row[0]
        for row in conn.execute(
            "SELECT fg.id FROM fight_groups fg "
            "JOIN fighters f ON f.name IN (fg.fighter_1_name, fg.fighter_2_name) "
            "WHERE fg.slate_id = ? AND f.id IN ({}) ".format(
                ",".join("?" * len(lu.fighter_ids))
            ),
            (slate_id, *lu.fighter_ids),
        ).fetchall()
    }
    assert len(picked_groups) == 6


# ---------------------------------------------------------------------------
# n_lineups bounds propagation (design §5.2 step 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_n", [0, -1, 6, 100])
def test_invalid_n_lineups_propagates_value_error(conn, slate_id, bad_n):
    """The solver owns ``n_lineups`` validation; the service does not
    swallow or rewrap the resulting :class:`ValueError`."""
    _seed_ready_slate(conn, slate_id)
    with pytest.raises(ValueError):
        run_optimizer(conn, slate_id=slate_id, n_lineups=bad_n)


# ---------------------------------------------------------------------------
# Pool-too-small after gate passes (design §5.3 / §5.2 step 2)
# ---------------------------------------------------------------------------


def test_pool_below_lineup_size_after_gate_passes_returns_infeasible_pool_too_small(
    conn, slate_id
):
    """Gate green + pool < 6 must surface the solver's
    ``infeasible_pool_too_small`` result, not a service-level
    exception. The service does not duplicate the precondition."""
    _seed_undersized_pool_but_ready_gate(conn, slate_id)

    res = run_optimizer(conn, slate_id=slate_id, n_lineups=1)
    assert isinstance(res, SolveResult)
    assert res.status == STATUS_INFEASIBLE_POOL_TOO_SMALL
    assert res.lineups == ()
    # Diagnostic should name the actual pool size (5) and the required size (6).
    assert "5" in res.reason
    assert "6" in res.reason


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


def test_run_optimizer_does_not_mutate_persisted_state(conn, slate_id):
    """Service composes only read-only callees: snapshot every relevant
    table before and after two invocations."""
    _seed_ready_slate(conn, slate_id)
    before = _db_snapshot(conn)

    run_optimizer(conn, slate_id=slate_id, n_lineups=1)
    run_optimizer(conn, slate_id=slate_id, n_lineups=2)

    after = _db_snapshot(conn)
    assert before == after, "run_optimizer must not mutate persisted state"


def test_gate_failure_path_is_also_read_only(conn, slate_id):
    """Even on the gate-failure short-circuit the service must not
    write."""
    _seed_ready_slate_minus_ack(conn, slate_id)
    before = _db_snapshot(conn)

    with pytest.raises(ManualReviewGateError):
        run_optimizer(conn, slate_id=slate_id, n_lineups=1)

    after = _db_snapshot(conn)
    assert before == after


# ---------------------------------------------------------------------------
# effective_status deferral (design §9 / ODDS_PERSISTENCE_DESIGN §15.11 #7)
# ---------------------------------------------------------------------------


def test_effective_status_governs_optimizer_pool(conn, slate_id):
    """D.5.2 (``ODDS_PERSISTENCE_DESIGN.md`` §16.9) + Slice 1: projection
    sourcing AND the Manual Review odds gate both read the projection-eligible
    ``effective_status`` set (``auto_match`` / ``review_accepted`` /
    ``force_pair``). The optimizer reads ``project_slate`` (pool membership) and
    runs behind ``evaluate_manual_review`` (the gate), so flipping
    ``effective_status`` now moves fighters in and out of the pool *and* the
    gate together. No optimizer code changes; this pins the intended downstream
    ripple.

    Once the gate also consumes ``effective_status`` (Slice 1), rejecting every
    odds row drops coverage to 0% → the Blocking §5.4.a check fails → the gate
    refuses the run before the solver is reached, rather than the solver
    returning an empty-pool ``infeasible_pool_too_small``.
    """
    fids = _seed_ready_slate(conn, slate_id)

    base = run_optimizer(conn, slate_id=slate_id, n_lineups=1)
    assert base.status == STATUS_OK

    # review_rejected is NOT projection-eligible → coverage drops to 0% → the
    # gate blocks (§5.4.a) and run_optimizer refuses before the solver.
    conn.execute(
        "UPDATE odds_match_results SET effective_status = 'review_rejected'"
    )
    conn.commit()
    with pytest.raises(ManualReviewGateError):
        run_optimizer(conn, slate_id=slate_id, n_lineups=1)

    # force_pair IS eligible → coverage restored, gate passes, solver succeeds.
    conn.execute(
        "UPDATE odds_match_results SET effective_status = 'force_pair'"
    )
    conn.commit()
    forced = run_optimizer(conn, slate_id=slate_id, n_lineups=1)
    assert forced.status == STATUS_OK
    assert set(forced.lineups[0].fighter_ids).issubset(set(fids))

    # review_accepted IS eligible → same restoration.
    conn.execute(
        "UPDATE odds_match_results SET effective_status = 'review_accepted'"
    )
    conn.commit()
    accepted = run_optimizer(conn, slate_id=slate_id, n_lineups=1)
    assert accepted.status == STATUS_OK
    assert set(accepted.lineups[0].fighter_ids).issubset(set(fids))


# ---------------------------------------------------------------------------
# Custom constraints pass-through (design §5.2)
# ---------------------------------------------------------------------------


def test_custom_constraints_are_passed_through_to_solver(conn, slate_id):
    """A pathological ``salary_cap`` must produce
    ``infeasible_constraints``, proving the service forwards the
    caller's :class:`UFCClassicConstraints` to ``solve_lineups``
    instead of substituting defaults."""
    _seed_ready_slate(conn, slate_id)
    tight = UFCClassicConstraints(salary_cap=10_000)  # six fighters cannot fit
    res = run_optimizer(
        conn, slate_id=slate_id, n_lineups=1, constraints=tight
    )
    assert res.status == STATUS_INFEASIBLE_CONSTRAINTS


def test_default_constraints_use_ufc_classic_defaults(conn, slate_id):
    """Omitting ``constraints`` must construct a default
    :class:`UFCClassicConstraints` so the standard 50k cap / 6-slot
    rules apply."""
    _seed_ready_slate(conn, slate_id)
    res = run_optimizer(conn, slate_id=slate_id, n_lineups=1)
    assert res.status == STATUS_OK
    assert res.lineups[0].total_salary <= 50_000
    assert len(res.lineups[0].fighter_ids) == 6
