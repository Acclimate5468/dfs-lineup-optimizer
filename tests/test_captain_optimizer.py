"""Unit tests for the DK UFC Captain optimizer (CAPTAIN_MODE_DESIGN §6).

Synthetic fixtures with hand-set projections only — the optimizer is
projection-source-agnostic, so no odds/CSV/DB is involved (docs/DEVELOPMENT_NOTES.md §8).
"""

from __future__ import annotations

import pytest

from src.captain.optimizer import (
    CAPTAIN_MULTIPLIER,
    SALARY_CAP,
    CaptainCandidate,
    CaptainOptimizerError,
    CaptainOptimizerStatus,
    CaptainRanking,
    StackMode,
    optimize_captain_lineups,
    rank_captains_by_cptproj,
)


def _candidate(
    name: str,
    base_salary: int,
    projection: float,
    *,
    captain_salary: int | None = None,
) -> CaptainCandidate:
    return CaptainCandidate(
        name=name,
        base_salary=base_salary,
        captain_salary=captain_salary
        if captain_salary is not None
        else round(1.5 * base_salary),
        projection=projection,
    )


def _pool_of(n: int, *, base_salary: int = 7000, projection: float = 50.0):
    """A pool of ``n`` interchangeable, comfortably-affordable fighters."""
    return [
        _candidate(f"F{i:02d}", base_salary, projection + i)
        for i in range(n)
    ]


def test_lineup_is_six_distinct_fighters_one_captain():
    result = optimize_captain_lineups(_pool_of(8))

    assert result.status is CaptainOptimizerStatus.OK
    top = result.lineups[0]
    assert len(top.flex_names) == 5
    assert len(top.fighter_names) == 6
    # Six distinct names, captain not duplicated among the flex.
    assert len(set(top.fighter_names)) == 6
    assert top.captain_name not in top.flex_names


def test_captain_multiplier_applied_to_points_and_captain_salary():
    # Two fighters are pricey; the rest are cheap so the cap never binds and we
    # can verify the exact math on a known lineup.
    cap = _candidate("Captain", base_salary=10000, projection=40.0)  # cpt 15000
    others = [_candidate(f"X{i}", 5000, 10.0) for i in range(5)]
    result = optimize_captain_lineups([cap, *others])

    assert result.status is CaptainOptimizerStatus.OK
    # Only one feasible 6-set exists here (6 candidates), captain = highest EV.
    top = result.lineups[0]
    assert top.captain_name == "Captain"
    # salary = captain_salary(15000) + 5 * 5000
    assert top.salary == 15000 + 5 * 5000
    # points = 1.5 * 40 + 5 * 10
    assert top.points == pytest.approx(CAPTAIN_MULTIPLIER * 40.0 + 5 * 10.0)
    assert top.points == pytest.approx(110.0)


def test_both_fighters_of_a_bout_may_appear_together():
    # No same-fight exclusion (§6): the two highest-projection fighters are a
    # bout pair; the optimum must roster BOTH. (The optimizer has no concept of
    # a bout — this proves it imposes no pairing constraint.)
    a = _candidate("Bout A", 8000, 90.0)
    b = _candidate("Bout B", 8000, 88.0)
    fillers = [_candidate(f"Fill{i}", 6000, 10.0 + i) for i in range(4)]
    result = optimize_captain_lineups([a, b, *fillers])

    assert result.status is CaptainOptimizerStatus.OK
    top = result.lineups[0]
    assert "Bout A" in top.fighter_names
    assert "Bout B" in top.fighter_names


def test_known_optimum_lineup_points_and_salary():
    # Hand-built so the optimum is unambiguous. All affordable; the captaincy
    # should fall on the highest-projection fighter to maximize the 1.5x bonus.
    pool = [
        _candidate("Ace", 9000, 100.0),   # cpt 13500
        _candidate("Two", 8000, 80.0),
        _candidate("Tre", 7000, 70.0),
        _candidate("Fyr", 7000, 60.0),
        _candidate("Fiv", 6000, 50.0),
        _candidate("Six", 6000, 40.0),
    ]
    result = optimize_captain_lineups(pool)

    assert result.status is CaptainOptimizerStatus.OK
    top = result.lineups[0]
    assert top.captain_name == "Ace"
    assert top.flex_names == ("Fiv", "Fyr", "Six", "Tre", "Two")
    # salary = 13500 + 8000 + 7000 + 7000 + 6000 + 6000
    assert top.salary == 13500 + 8000 + 7000 + 7000 + 6000 + 6000
    assert top.salary <= SALARY_CAP
    # points = 1.5*100 + 80 + 70 + 60 + 50 + 40
    assert top.points == pytest.approx(150.0 + 80 + 70 + 60 + 50 + 40)


def test_cap_excludes_unaffordable_lineups():
    # Six fighters, but captaining the most expensive blows the cap. The solver
    # must still find the feasible captain choice rather than report infeasible.
    pool = [
        _candidate("Pricey", 13000, 60.0),   # cpt 19500
        _candidate("A", 9000, 55.0),
        _candidate("B", 9000, 54.0),
        _candidate("C", 9000, 53.0),
        _candidate("D", 9000, 52.0),
        _candidate("E", 9000, 51.0),
    ]
    # captaining "Pricey": 19500 + 5*9000 = 64500 > 50000 (infeasible)
    # captaining "A":      13000 + 13500 + 4*9000 = 62500 > 50000 ... still over
    # All single-set captaincies here exceed the cap -> NO_FEASIBLE_LINEUP.
    result = optimize_captain_lineups(pool)
    assert result.status is CaptainOptimizerStatus.NO_FEASIBLE_LINEUP

    # Now make it affordable and confirm every returned lineup respects the cap.
    cheap = _pool_of(7, base_salary=7000, projection=50.0)
    ok = optimize_captain_lineups(cheap)
    assert ok.status is CaptainOptimizerStatus.OK
    assert all(line.salary <= SALARY_CAP for line in ok.lineups)


def test_fewer_than_six_fighters_is_handled():
    result = optimize_captain_lineups(_pool_of(5))
    assert result.status is CaptainOptimizerStatus.NOT_ENOUGH_FIGHTERS
    assert result.lineups == ()
    assert "at least 6" in result.message


def test_nothing_fits_under_cap_is_handled():
    # Six fighters, all so expensive no captaincy fits under $50k.
    pool = [_candidate(f"Big{i}", 12000, 50.0) for i in range(6)]
    # cheapest lineup = 18000 (cpt) + 5*12000 = 78000 > 50000
    result = optimize_captain_lineups(pool)
    assert result.status is CaptainOptimizerStatus.NO_FEASIBLE_LINEUP
    assert result.lineups == ()


def test_top_n_limits_results():
    result = optimize_captain_lineups(_pool_of(10), top_n=3)
    assert result.status is CaptainOptimizerStatus.OK
    assert len(result.lineups) == 3


def test_results_ordered_by_points_desc():
    result = optimize_captain_lineups(_pool_of(10), top_n=5)
    points = [line.points for line in result.lineups]
    assert points == sorted(points, reverse=True)


def test_ordering_is_deterministic_regardless_of_input_order():
    pool = _pool_of(9)
    forward = optimize_captain_lineups(pool, top_n=5)
    backward = optimize_captain_lineups(list(reversed(pool)), top_n=5)
    assert forward.lineups == backward.lineups


def test_tie_break_prefers_lower_salary_then_names():
    # Two captaincy options reach identical points; the cheaper one must rank
    # first (salary tie-break). Equal-projection pool, but one fighter is
    # cheaper, so captaining the cheaper fighter yields the same points (all
    # projections equal) at a lower salary.
    pool = [
        _candidate("Cheap", 5000, 50.0),   # cpt 7500
        _candidate("Costly", 9000, 50.0),  # cpt 13500
        _candidate("P1", 6000, 50.0),
        _candidate("P2", 6000, 50.0),
        _candidate("P3", 6000, 50.0),
        _candidate("P4", 6000, 50.0),
    ]
    # Only 6 candidates -> one set; captaincy choice varies salary, not points
    # (all projections equal => points identical for every captain choice).
    result = optimize_captain_lineups(pool, top_n=6)
    assert result.status is CaptainOptimizerStatus.OK
    # All six lineups share the same points; first must be the lowest salary.
    assert len({line.points for line in result.lineups}) == 1
    salaries = [line.salary for line in result.lineups]
    assert salaries == sorted(salaries)
    # Captaining the cheapest fighter is the lowest-salary lineup.
    assert result.lineups[0].captain_name == "Cheap"


def test_duplicate_names_raise():
    pool = _pool_of(6)
    pool.append(pool[0])  # same name twice
    with pytest.raises(CaptainOptimizerError, match="Duplicate candidate name"):
        optimize_captain_lineups(pool)


def test_top_n_below_one_raises():
    with pytest.raises(CaptainOptimizerError, match="top_n must be >= 1"):
        optimize_captain_lineups(_pool_of(7), top_n=0)


# ---------------------------------------------------------------------------
# Stack mode (CAPTAIN_MODE_DESIGN §14.3) — GPP same-fight exclusion vs. cash.
# ---------------------------------------------------------------------------
#
# A 7-fighter pool whose two strongest fighters are a bout pair, with five cheap
# fillers so a GPP-legal alternative (drop one side of the pair) always exists.
def _same_fight_pool():
    a = _candidate("Bout A", 8000, 90.0)
    b = _candidate("Bout B", 8000, 88.0)
    fillers = [_candidate(f"Fill{i}", 6000, 10.0 + i) for i in range(5)]
    return [a, b, *fillers]


_PAIR = [("Bout A", "Bout B")]


def test_gpp_drops_same_fight_pair_cash_keeps_it():
    """GPP rejects a lineup with both sides of a bout; cash allows it (§14.3).

    Pins one bout (Bout A / Bout B, the two highest projections). In cash the
    optimum rosters BOTH; in GPP the top lineup rosters at most one of them.
    """
    pool = _same_fight_pool()

    cash = optimize_captain_lineups(
        pool, top_n=1, stack_mode=StackMode.CASH, same_fight_pairs=_PAIR
    )
    assert cash.status is CaptainOptimizerStatus.OK
    assert {"Bout A", "Bout B"} <= set(cash.lineups[0].fighter_names)

    gpp = optimize_captain_lineups(
        pool, top_n=1, stack_mode=StackMode.GPP, same_fight_pairs=_PAIR
    )
    assert gpp.status is CaptainOptimizerStatus.OK
    # No GPP lineup may roster both sides of the pinned bout.
    assert not ({"Bout A", "Bout B"} <= set(gpp.lineups[0].fighter_names))
    assert all(
        not ({"Bout A", "Bout B"} <= set(line.fighter_names))
        for line in optimize_captain_lineups(
            pool, top_n=100, stack_mode=StackMode.GPP, same_fight_pairs=_PAIR
        ).lineups
    )


def test_default_is_cash_no_exclusion_even_with_pairs():
    """The parameter defaults to cash: with no stack_mode, the same-fight pair is
    kept, and passing pairs under explicit cash is identical (§14.3)."""
    pool = _same_fight_pool()

    default = optimize_captain_lineups(pool, top_n=5)
    cash_pairs = optimize_captain_lineups(
        pool, top_n=5, stack_mode=StackMode.CASH, same_fight_pairs=_PAIR
    )
    assert default.lineups == cash_pairs.lineups
    assert {"Bout A", "Bout B"} <= set(default.lineups[0].fighter_names)


def test_gpp_with_no_pairs_equals_default():
    """GPP with no bout pairings has nothing to exclude, so it matches cash."""
    pool = _same_fight_pool()
    default = optimize_captain_lineups(pool, top_n=5)
    gpp_nopairs = optimize_captain_lineups(pool, top_n=5, stack_mode=StackMode.GPP)
    assert gpp_nopairs.lineups == default.lineups


def test_stack_mode_accepts_string_alias():
    """A UI may pass the mode as a string ('gpp' / 'cash'), case-insensitively."""
    pool = _same_fight_pool()
    enum_gpp = optimize_captain_lineups(
        pool, top_n=5, stack_mode=StackMode.GPP, same_fight_pairs=_PAIR
    )
    str_gpp = optimize_captain_lineups(
        pool, top_n=5, stack_mode="GPP", same_fight_pairs=_PAIR
    )
    assert str_gpp.lineups == enum_gpp.lineups


def test_unknown_stack_mode_raises():
    with pytest.raises(CaptainOptimizerError, match="Unknown stack_mode"):
        optimize_captain_lineups(_pool_of(7), stack_mode="ceiling")


def test_malformed_same_fight_pair_raises():
    with pytest.raises(CaptainOptimizerError, match="exactly two distinct"):
        optimize_captain_lineups(
            _pool_of(7),
            stack_mode=StackMode.GPP,
            same_fight_pairs=[("Solo",)],
        )
    with pytest.raises(CaptainOptimizerError, match="exactly two distinct"):
        optimize_captain_lineups(
            _pool_of(7),
            stack_mode=StackMode.GPP,
            same_fight_pairs=[("Same", "Same")],
        )


def test_gpp_all_lineups_pair_yields_no_feasible():
    """If every feasible 6-set rosters a pinned pair, GPP reports NO_FEASIBLE with
    a message naming the same-fight exclusion (exactly 6 fighters, both paired)."""
    a = _candidate("Bout A", 7000, 90.0)
    b = _candidate("Bout B", 7000, 88.0)
    rest = [_candidate(f"R{i}", 6000, 10.0 + i) for i in range(4)]
    pool = [a, b, *rest]  # only one 6-set exists, and it holds both A and B
    result = optimize_captain_lineups(
        pool, stack_mode=StackMode.GPP, same_fight_pairs=_PAIR
    )
    assert result.status is CaptainOptimizerStatus.NO_FEASIBLE_LINEUP
    assert "GPP same-fight exclusion" in result.message


def test_captain_salary_independent_of_base_salary():
    # Optimizer must not assume captain_salary == 1.5 * base_salary; it uses the
    # supplied captain_salary verbatim (source-agnostic, §7).
    odd = _candidate("Odd", base_salary=8000, projection=60.0, captain_salary=11000)
    others = [_candidate(f"O{i}", 6000, 10.0) for i in range(5)]
    result = optimize_captain_lineups([odd, *others])
    top = result.lineups[0]
    assert top.captain_name == "Odd"
    assert top.salary == 11000 + 5 * 6000  # uses 11000, not round(1.5*8000)


# ---------------------------------------------------------------------------
# Captain pin + CPTproj ranking (CAPTAIN_MODE_DESIGN §14.4) — slice C11b.
# ---------------------------------------------------------------------------


def test_captain_pin_restricts_captain_to_named_fighter():
    """Pinning a captain forces every returned lineup to wear that fighter as the
    Captain; the rest of the slate still fills the five Fighter slots (§14.4)."""
    pool = _pool_of(8)  # F00..F07; free-EV captains the highest projection (F07)
    free = optimize_captain_lineups(pool, top_n=5)
    assert free.lineups[0].captain_name == "F07"

    pinned = optimize_captain_lineups(pool, top_n=5, captain="F02")
    assert pinned.status is CaptainOptimizerStatus.OK
    assert all(line.captain_name == "F02" for line in pinned.lineups)
    # The pinned captain's best lineup is worse than the free-EV optimum (it must
    # captain a lower-projection fighter), proving the pin actually bound.
    assert pinned.lineups[0].points < free.lineups[0].points


def test_captain_pin_none_is_free_ev_unchanged():
    """captain=None (the default) is byte-for-byte the free-EV behavior (C11a)."""
    pool = _pool_of(9)
    default = optimize_captain_lineups(pool, top_n=5)
    explicit_none = optimize_captain_lineups(pool, top_n=5, captain=None)
    assert default.lineups == explicit_none.lineups


def test_captain_pin_composes_with_gpp_exclusion():
    """The pin and the GPP same-fight exclusion both apply: a lineup captained by
    the pinned fighter must still not roster both sides of a bout (§14.3 + §14.4)."""
    pool = _same_fight_pool()
    pinned = optimize_captain_lineups(
        pool,
        top_n=100,
        stack_mode=StackMode.GPP,
        same_fight_pairs=_PAIR,
        captain="Bout A",
    )
    assert pinned.status is CaptainOptimizerStatus.OK
    for line in pinned.lineups:
        assert line.captain_name == "Bout A"
        assert not ({"Bout A", "Bout B"} <= set(line.fighter_names))


def test_unknown_pinned_captain_raises():
    """A pin naming a fighter outside the pool is a programming error (§14.4)."""
    with pytest.raises(CaptainOptimizerError, match="not in the candidate pool"):
        optimize_captain_lineups(_pool_of(7), captain="Ghost")


def test_pinned_captain_with_no_feasible_lineup_reports_status():
    """If no lineup fits with the pinned captain, NO_FEASIBLE_LINEUP names it (not
    raised) — distinct from an unknown pin, which is a programming error."""
    # The pinned captain is so expensive that no 6-fighter lineup fits the cap.
    huge = _candidate("Huge", base_salary=40000, projection=10.0)  # cpt 60000
    rest = [_candidate(f"R{i}", 6000, 10.0 + i) for i in range(6)]
    result = optimize_captain_lineups([huge, *rest], captain="Huge")
    assert result.status is CaptainOptimizerStatus.NO_FEASIBLE_LINEUP
    assert "Huge as Captain" in result.message


def test_rank_captains_by_cptproj_orders_by_projection():
    """CPTproj = 1.5 × projection ranks captains by descending projection, and each
    entry carries that captain's best feasible lineup total (§14.4)."""
    pool = _pool_of(8)  # projections 50..57, so F07 is the CPTproj top
    rankings = rank_captains_by_cptproj(pool)
    assert all(isinstance(r, CaptainRanking) for r in rankings)
    # One entry per candidate, ordered by descending CPTproj (= projection).
    assert [r.captain_name for r in rankings] == [f"F{i:02d}" for i in range(7, -1, -1)]
    assert rankings[0].captain_name == "F07"
    assert rankings[0].cptproj == pytest.approx(CAPTAIN_MULTIPLIER * 57.0)
    # CPTproj is exactly 1.5 × the candidate's projection.
    by_name = {c.name: c.projection for c in pool}
    for r in rankings:
        assert r.cptproj == pytest.approx(CAPTAIN_MULTIPLIER * by_name[r.captain_name])
    # best_total mirrors the captain-pinned optimize for that fighter.
    for r in rankings:
        pinned = optimize_captain_lineups(pool, top_n=1, captain=r.captain_name)
        assert r.best_total == pytest.approx(pinned.lineups[0].points)
        assert r.best_lineup == pinned.lineups[0]


def test_rank_captains_reports_none_total_for_infeasible_captain():
    """A captain with no feasible lineup still appears in the ranking, with
    best_lineup / best_total None (CPTproj is independent of feasibility)."""
    huge = _candidate("Huge", base_salary=40000, projection=99.0)  # cpt 60000
    rest = [_candidate(f"R{i}", 6000, 10.0 + i) for i in range(6)]
    rankings = rank_captains_by_cptproj([huge, *rest])
    huge_entry = next(r for r in rankings if r.captain_name == "Huge")
    # Highest projection -> top CPTproj, yet no feasible lineup as Captain.
    assert rankings[0].captain_name == "Huge"
    assert huge_entry.best_lineup is None
    assert huge_entry.best_total is None


def test_rank_captains_honors_stack_mode():
    """The per-captain best lineup respects the build's stack mode: GPP totals are
    computed under the same-fight exclusion (§14.3 + §14.4)."""
    pool = _same_fight_pool()
    gpp = rank_captains_by_cptproj(
        pool, stack_mode=StackMode.GPP, same_fight_pairs=_PAIR
    )
    # No captain's GPP best lineup rosters both sides of the pinned bout.
    for r in gpp:
        if r.best_lineup is not None:
            assert not ({"Bout A", "Bout B"} <= set(r.best_lineup.fighter_names))


def test_rank_captains_empty_pool_is_empty():
    assert rank_captains_by_cptproj([]) == ()


def test_rank_captains_propagates_input_validation():
    """Duplicate names / malformed pairs raise the same error the optimizer does."""
    dup = [_candidate("Dup", 7000, 50.0), _candidate("Dup", 7000, 40.0)]
    with pytest.raises(CaptainOptimizerError, match="Duplicate candidate name"):
        rank_captains_by_cptproj(dup + _pool_of(6))
