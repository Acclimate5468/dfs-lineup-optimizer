"""Tests for Optimizer v1 lineup solver (Slice B.3).

Covers ``solve_lineups`` in ``src/optimizer/lineup_solver.py`` per
``docs/OPTIMIZER_V1_DESIGN.md`` §5.2 / §5.4. The solver is a pure
function: these tests build :class:`OptimizerPool` values directly and
never touch the DB.
"""

from __future__ import annotations

import copy

import pytest

from src.optimizer.constraints import UFCClassicConstraints
from src.optimizer.lineup_solver import (
    Lineup,
    MAX_N_LINEUPS,
    MIN_N_LINEUPS,
    SolveResult,
    STATUS_INFEASIBLE_CONSTRAINTS,
    STATUS_INFEASIBLE_POOL_TOO_SMALL,
    STATUS_OK,
    STATUS_OK_PARTIAL,
    solve_lineups,
)
from src.optimizer.pool_builder import OptimizerPool, OptimizerPoolEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    fid: int,
    *,
    salary: int,
    projection: float,
    fight_group_id: int | None = None,
) -> OptimizerPoolEntry:
    return OptimizerPoolEntry(
        fighter_id=fid,
        slate_id=1,
        dk_name=f"F{fid}",
        dk_salary=salary,
        default_projection=projection,
        fight_group_id=fight_group_id,
    )


def _pool(entries, *, pairs=()) -> OptimizerPool:
    return OptimizerPool(
        slate_id=1,
        entries=tuple(entries),
        same_fight_pairs=frozenset(frozenset(p) for p in pairs),
        excluded=(),
    )


def _assert_valid_lineup(
    lu: Lineup,
    *,
    by_id: dict[int, OptimizerPoolEntry],
    salary_cap: int = 50_000,
    lineup_size: int = 6,
    pairs: tuple[frozenset[int], ...] = (),
) -> None:
    assert len(lu.fighter_ids) == lineup_size
    assert len(set(lu.fighter_ids)) == lineup_size, "duplicate fighter in lineup"
    assert lu.total_salary <= salary_cap
    chosen = set(lu.fighter_ids)
    for pair in pairs:
        assert not pair.issubset(chosen), f"same-fight conflict: {pair}"
    expected_salary = sum(by_id[fid].dk_salary for fid in lu.fighter_ids)
    expected_proj = sum(by_id[fid].default_projection for fid in lu.fighter_ids)
    assert lu.total_salary == expected_salary
    assert lu.total_projection == pytest.approx(expected_proj, abs=1e-9)


# ---------------------------------------------------------------------------
# n_lineups bounds (programmer-error guard, design §5.2)
# ---------------------------------------------------------------------------


def test_n_lineups_zero_raises_value_error():
    with pytest.raises(ValueError):
        solve_lineups(_pool([]), n_lineups=0)


def test_n_lineups_six_raises_value_error():
    with pytest.raises(ValueError):
        solve_lineups(_pool([]), n_lineups=MAX_N_LINEUPS + 1)


def test_n_lineups_negative_raises_value_error():
    with pytest.raises(ValueError):
        solve_lineups(_pool([]), n_lineups=-1)


def test_n_lineups_bounds_constants_match_design():
    # Pinned by the v1 contract (design §5.2): N ∈ [1, 5].
    assert MIN_N_LINEUPS == 1
    assert MAX_N_LINEUPS == 5


# ---------------------------------------------------------------------------
# Pool-size precondition (design §5.2 step 2)
# ---------------------------------------------------------------------------


def test_pool_smaller_than_six_returns_infeasible_pool_too_small():
    pool = _pool([_entry(i, salary=8000, projection=40.0) for i in range(1, 6)])
    res = solve_lineups(pool, n_lineups=1)
    assert isinstance(res, SolveResult)
    assert res.status == STATUS_INFEASIBLE_POOL_TOO_SMALL
    assert res.lineups == ()
    # Diagnostic should name the actual pool size and the required size.
    assert "5" in res.reason
    assert "6" in res.reason


def test_empty_pool_returns_infeasible_pool_too_small_with_useful_reason():
    res = solve_lineups(_pool([]), n_lineups=1)
    assert res.status == STATUS_INFEASIBLE_POOL_TOO_SMALL
    assert res.lineups == ()
    assert "0" in res.reason


# ---------------------------------------------------------------------------
# Optimal single lineup
# ---------------------------------------------------------------------------


def test_optimal_single_lineup_picks_best_six_by_projection():
    # 7 fighters, all affordable; lowest-projection fighter (fid 1) should
    # be excluded from the optimal six.
    entries = [
        _entry(1, salary=7000, projection=10.0),
        _entry(2, salary=7000, projection=20.0),
        _entry(3, salary=7000, projection=30.0),
        _entry(4, salary=7000, projection=40.0),
        _entry(5, salary=7000, projection=50.0),
        _entry(6, salary=7000, projection=60.0),
        _entry(7, salary=7000, projection=70.0),
    ]
    res = solve_lineups(_pool(entries), n_lineups=1)
    assert res.status == STATUS_OK
    assert res.reason == ""
    assert len(res.lineups) == 1
    lu = res.lineups[0]
    assert set(lu.fighter_ids) == {2, 3, 4, 5, 6, 7}
    assert lu.total_salary == 6 * 7000
    assert lu.total_projection == pytest.approx(20 + 30 + 40 + 50 + 60 + 70)
    # Lineup fighter_ids are sorted ascending so a lineup is uniquely
    # represented across runs.
    assert list(lu.fighter_ids) == sorted(lu.fighter_ids)


def test_six_eligible_fighters_returns_single_feasible_lineup():
    entries = [_entry(i, salary=5000, projection=50.0) for i in range(1, 7)]
    res = solve_lineups(_pool(entries), n_lineups=1)
    assert res.status == STATUS_OK
    assert len(res.lineups) == 1
    assert set(res.lineups[0].fighter_ids) == {1, 2, 3, 4, 5, 6}


# ---------------------------------------------------------------------------
# Salary cap tradeoff
# ---------------------------------------------------------------------------


def test_salary_cap_forces_swap_to_a_cheaper_higher_value_fighter():
    # If the salary cap were unbounded, the top six by projection would be
    # fids 1..5 + fid 7 (projection 60+58+56+54+52+10 = 290). But cap
    # forces dropping the most expensive top-projector (fid 1) for the
    # cheap-but-low fid 6 → final lineup is {2,3,4,5,6,7}.
    entries = [
        _entry(1, salary=10_000, projection=60.0),
        _entry(2, salary=10_000, projection=58.0),
        _entry(3, salary=10_000, projection=56.0),
        _entry(4, salary=10_000, projection=54.0),
        _entry(5, salary=10_000, projection=52.0),
        _entry(6, salary=1, projection=50.0),
        _entry(7, salary=1, projection=10.0),
    ]
    pool = _pool(entries)
    res = solve_lineups(pool, n_lineups=1)
    assert res.status == STATUS_OK
    lu = res.lineups[0]
    assert lu.total_salary <= 50_000
    # 1+5*10000 over cap, so fid 1 cannot ship with five other 10k fighters.
    by_id = {e.fighter_id: e for e in entries}
    _assert_valid_lineup(lu, by_id=by_id)


def test_custom_salary_cap_is_honored():
    entries = [_entry(i, salary=5000, projection=50.0) for i in range(1, 8)]
    tight = UFCClassicConstraints(salary_cap=30_000)
    # 30_000 / 5_000 = 6 fighters at this salary fits exactly.
    res = solve_lineups(_pool(entries), constraints=tight, n_lineups=1)
    assert res.status == STATUS_OK
    assert res.lineups[0].total_salary <= 30_000

    too_tight = UFCClassicConstraints(salary_cap=29_999)
    res2 = solve_lineups(_pool(entries), constraints=too_tight, n_lineups=1)
    assert res2.status == STATUS_INFEASIBLE_CONSTRAINTS


# ---------------------------------------------------------------------------
# Same-fight pair constraint
# ---------------------------------------------------------------------------


def test_same_fight_pair_prevents_both_fighters_from_appearing_together():
    # Fids 1 and 2 are the top two projectors. With them paired, the
    # solver must pick exactly one of them, never both.
    entries = [
        _entry(1, salary=8000, projection=100.0),
        _entry(2, salary=8000, projection=99.0),
        _entry(3, salary=8000, projection=50.0),
        _entry(4, salary=8000, projection=50.0),
        _entry(5, salary=8000, projection=50.0),
        _entry(6, salary=8000, projection=50.0),
        _entry(7, salary=8000, projection=50.0),
    ]
    pool = _pool(entries, pairs=[{1, 2}])
    res = solve_lineups(pool, n_lineups=1)
    assert res.status == STATUS_OK
    ids = set(res.lineups[0].fighter_ids)
    assert not {1, 2}.issubset(ids), (
        f"both halves of paired fight appeared in lineup: {sorted(ids)}"
    )
    # Higher-projection side (fid 1) should win the inclusion.
    assert 1 in ids and 2 not in ids


def test_impossible_pair_constraints_return_infeasible_constraints():
    # Six fighters in three same-fight pairs: at most 3 can be selected
    # together, so a 6-fighter lineup is infeasible.
    entries = [_entry(i, salary=8000, projection=50.0) for i in range(1, 7)]
    pool = _pool(entries, pairs=[{1, 2}, {3, 4}, {5, 6}])
    res = solve_lineups(pool, n_lineups=1)
    assert res.status == STATUS_INFEASIBLE_CONSTRAINTS
    assert res.lineups == ()
    assert res.reason  # non-empty diagnostic
    assert "same-fight" in res.reason


# ---------------------------------------------------------------------------
# Multi-lineup with diversity cut
# ---------------------------------------------------------------------------


def test_n_lineups_three_returns_three_distinct_valid_lineups():
    # Plenty of fighters, all affordable; multiple distinct optimal-ish
    # 6-subsets exist.
    entries = [_entry(i, salary=5000, projection=50.0 + i) for i in range(1, 9)]
    pool = _pool(entries)
    res = solve_lineups(pool, n_lineups=3)
    assert res.status == STATUS_OK
    assert len(res.lineups) == 3
    by_id = {e.fighter_id: e for e in entries}
    sets = set()
    prev_projection = None
    for lu in res.lineups:
        _assert_valid_lineup(lu, by_id=by_id)
        sets.add(frozenset(lu.fighter_ids))
        if prev_projection is not None:
            # Each subsequent lineup is no better than the previous
            # (diversity cuts only restrict feasibility, so later
            # lineups can only equal or be worse).
            assert lu.total_projection <= prev_projection + 1e-6
        prev_projection = lu.total_projection
    assert len(sets) == 3, "diversity cut should force distinct lineups"


def test_request_more_lineups_than_feasible_returns_partial_prefix():
    # Exactly six fighters → only one possible 6-subset exists; asking
    # for three must return a one-lineup prefix and ``ok_partial``.
    entries = [_entry(i, salary=5000, projection=50.0) for i in range(1, 7)]
    res = solve_lineups(_pool(entries), n_lineups=3)
    assert res.status == STATUS_OK_PARTIAL
    assert len(res.lineups) == 1
    assert res.reason
    assert "1" in res.reason and "3" in res.reason


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic_tie_break_prefers_lower_fighter_id():
    # Fids 1 and 2 are identical except for id; fids 3..7 are the
    # unambiguous top five. The tiebreak should choose fid 1 over fid 2
    # to fill the sixth slot.
    entries = [
        _entry(1, salary=5000, projection=50.0),
        _entry(2, salary=5000, projection=50.0),
        _entry(3, salary=5000, projection=60.0),
        _entry(4, salary=5000, projection=60.0),
        _entry(5, salary=5000, projection=60.0),
        _entry(6, salary=5000, projection=60.0),
        _entry(7, salary=5000, projection=60.0),
    ]
    res = solve_lineups(_pool(entries), n_lineups=1)
    assert res.status == STATUS_OK
    ids = set(res.lineups[0].fighter_ids)
    assert {3, 4, 5, 6, 7}.issubset(ids)
    assert 1 in ids
    assert 2 not in ids


def test_solve_lineups_is_deterministic_across_repeated_calls():
    entries = [
        _entry(i, salary=5000 + (i * 11) % 17, projection=50.0 + (i % 5))
        for i in range(1, 12)
    ]
    pool = _pool(entries, pairs=[{1, 2}, {3, 4}])
    r1 = solve_lineups(pool, n_lineups=3)
    r2 = solve_lineups(pool, n_lineups=3)
    assert r1.status == r2.status
    assert [lu.fighter_ids for lu in r1.lineups] == [
        lu.fighter_ids for lu in r2.lineups
    ]
    assert [lu.total_salary for lu in r1.lineups] == [
        lu.total_salary for lu in r2.lineups
    ]


def test_caller_side_entry_reordering_does_not_change_result():
    base = [_entry(i, salary=5000, projection=50.0 + i) for i in range(1, 9)]
    pool_a = _pool(base)
    pool_b = _pool(list(reversed(base)))
    res_a = solve_lineups(pool_a, n_lineups=2)
    res_b = solve_lineups(pool_b, n_lineups=2)
    assert res_a.status == res_b.status
    assert [lu.fighter_ids for lu in res_a.lineups] == [
        lu.fighter_ids for lu in res_b.lineups
    ]


# ---------------------------------------------------------------------------
# Totals on the returned Lineup match the selected fighters
# ---------------------------------------------------------------------------


def test_result_totals_match_selected_fighters():
    entries = [
        _entry(1, salary=7000, projection=40.0),
        _entry(2, salary=7100, projection=41.0),
        _entry(3, salary=7200, projection=42.0),
        _entry(4, salary=7300, projection=43.0),
        _entry(5, salary=7400, projection=44.0),
        _entry(6, salary=7500, projection=45.0),
        _entry(7, salary=7600, projection=46.0),
    ]
    by_id = {e.fighter_id: e for e in entries}
    res = solve_lineups(_pool(entries), n_lineups=1)
    lu = res.lineups[0]
    _assert_valid_lineup(lu, by_id=by_id)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_diagnostic_for_infeasible_pool_too_small_names_actual_size():
    pool = _pool([_entry(1, salary=8000, projection=50.0)])
    res = solve_lineups(pool, n_lineups=1)
    assert res.status == STATUS_INFEASIBLE_POOL_TOO_SMALL
    assert "1 eligible" in res.reason
    assert "6" in res.reason


def test_diagnostic_for_infeasible_constraints_is_human_readable():
    entries = [_entry(i, salary=8000, projection=50.0) for i in range(1, 7)]
    pool = _pool(entries, pairs=[{1, 2}, {3, 4}, {5, 6}])
    res = solve_lineups(pool, n_lineups=1)
    assert res.status == STATUS_INFEASIBLE_CONSTRAINTS
    assert res.reason
    # Should reference the binding inputs to help the user debug.
    assert "salary cap" in res.reason
    assert "same-fight" in res.reason
    assert "pool size" in res.reason


def test_diagnostic_for_partial_names_actual_and_requested_counts():
    entries = [_entry(i, salary=5000, projection=50.0) for i in range(1, 7)]
    res = solve_lineups(_pool(entries), n_lineups=4)
    assert res.status == STATUS_OK_PARTIAL
    assert res.reason
    assert "1" in res.reason
    assert "4" in res.reason


# ---------------------------------------------------------------------------
# Purity: input pool is not mutated
# ---------------------------------------------------------------------------


def test_solve_lineups_does_not_mutate_the_pool():
    entries = [_entry(i, salary=7000, projection=40.0 + i) for i in range(1, 8)]
    pool = _pool(entries, pairs=[{1, 2}])
    before_entries = pool.entries
    before_pairs = pool.same_fight_pairs
    before_snapshot = copy.deepcopy(pool)

    solve_lineups(pool, n_lineups=2)

    # Same object identity + structural equality.
    assert pool.entries is before_entries
    assert pool.same_fight_pairs is before_pairs
    assert pool == before_snapshot
