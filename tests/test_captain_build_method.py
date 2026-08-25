"""Unit tests for the Captain pluggable build-method interface (DESIGN §7).

Covers the Heuristic engine (it reuses the Classic projection math, never
re-implements it), the registry/selector (lookup + clean unknown-name error),
and the additive registration seam that lets a future method plug in without
touching this interface or the optimizer. The slate-level integration test that
pins the method+optimizer chain to a known answer lives separately — it needs
the captain-mode salaries as inputs.
"""

from __future__ import annotations

import pytest

from src.captain import build_method as bm
from src.captain.build_method import (
    FINISH_AWARE_METHOD_NAME,
    FINISH_BONUS_K_DEFAULT,
    HEURISTIC_METHOD_NAME,
    DuplicateProjectionMethodError,
    FighterProjectionInput,
    FinishAwareMethod,
    HeuristicMethod,
    ProjectionMethod,
    UnknownProjectionMethodError,
    available_methods,
    get_method,
    is_experimental,
    method_label,
    register_method,
)
from src.captain.finish_signal import (
    FinishOddsBout,
    MethodOfVictoryOdds,
    compute_finish_signals,
)
from src.captain.optimizer import (
    CaptainCandidate,
    CaptainOptimizerStatus,
    StackMode,
    optimize_captain_lineups,
    rank_captains_by_cptproj,
)
from src.projections.default_projection import default_projection

# (win_prob, base_salary, captain_salary, scheduled_rounds) fixtures spanning
# every value_gap_bonus tier (+8 / +5 / +3 / 0) and both round lengths, so the
# Heuristic is checked against default_projection across the formula's branches.
_FIXTURES = [
    # cheap + live -> +8 tier, 3R
    ("CheapLive", 0.50, 7600, 11400, 3),
    # mid salary -> +5 tier, 3R
    ("MidValue", 0.48, 8000, 12000, 3),
    # +3 tier needs p >= 0.55, 3R
    ("HighProbValue", 0.55, 8500, 12750, 3),
    # no value bonus (expensive favorite), 5R -> only the five-round bonus
    ("ExpensiveFav", 0.80, 9500, 14250, 5),
    # underdog, no bonuses, 3R
    ("Underdog", 0.21, 9000, 13500, 3),
    # even-money heavyweight, 5R, expensive (no value bonus)
    ("EvenHeavy", 0.50, 9800, 14700, 5),
]


def _input(fixture) -> FighterProjectionInput:
    name, win_prob, base, cpt, rounds = fixture
    return FighterProjectionInput(
        name=name,
        base_salary=base,
        captain_salary=cpt,
        win_prob=win_prob,
        scheduled_rounds=rounds,
    )


def test_heuristic_reproduces_default_projection_exactly():
    """Each candidate's projection equals the Classic default_projection (§4/§7)."""
    inputs = [_input(f) for f in _FIXTURES]
    candidates = HeuristicMethod().project(inputs)

    assert len(candidates) == len(inputs)
    for fixture, candidate in zip(_FIXTURES, candidates):
        name, win_prob, base, cpt, rounds = fixture
        expected = default_projection(win_prob, base, rounds)
        assert candidate.projection == expected
        # Identity + both salaries pass through untouched; the 1.5x multiplier
        # is the optimizer's job, so projection is the BASE points.
        assert candidate.name == name
        assert candidate.base_salary == base
        assert candidate.captain_salary == cpt


def test_heuristic_output_is_candidates_in_input_order():
    inputs = [_input(f) for f in _FIXTURES]
    candidates = HeuristicMethod().project(inputs)

    assert all(isinstance(c, CaptainCandidate) for c in candidates)
    assert [c.name for c in candidates] == [f[0] for f in _FIXTURES]


def test_heuristic_value_gap_bonus_keys_on_base_not_captain_salary():
    """The cheap-but-live bonus must use base_salary, not the 1.5x captain row."""
    # base 7600 @ p=0.50 -> +8 tier; the captain salary (11400) is well above
    # every tier threshold, so reading it instead would zero the bonus.
    candidate = HeuristicMethod().project(
        [
            FighterProjectionInput(
                name="Live",
                base_salary=7600,
                captain_salary=11400,
                win_prob=0.50,
                scheduled_rounds=3,
            )
        ]
    )[0]
    assert candidate.projection == default_projection(0.50, 7600, 3)
    assert candidate.projection == pytest.approx(0.50 * 70 + 8.0)


def test_empty_input_yields_empty_candidates():
    assert HeuristicMethod().project([]) == []


def test_input_validation_rejects_bad_rounds_and_prob():
    with pytest.raises(ValueError, match="scheduled_rounds"):
        FighterProjectionInput("X", 8000, 12000, 0.5, 4)
    with pytest.raises(ValueError, match="win_prob"):
        FighterProjectionInput("X", 8000, 12000, 1.5, 3)
    with pytest.raises(ValueError, match="name"):
        FighterProjectionInput("  ", 8000, 12000, 0.5, 3)


def test_registry_returns_heuristic_by_name():
    method = get_method(HEURISTIC_METHOD_NAME)
    assert method.name == HEURISTIC_METHOD_NAME
    assert isinstance(method, HeuristicMethod)
    # Satisfies the structural contract used by future engines.
    assert isinstance(method, ProjectionMethod)
    assert HEURISTIC_METHOD_NAME in available_methods()


def test_registry_lookup_is_case_insensitive():
    assert get_method("Heuristic") is get_method("heuristic")
    assert get_method("  HEURISTIC  ").name == HEURISTIC_METHOD_NAME


def test_unknown_method_name_errors_cleanly():
    with pytest.raises(UnknownProjectionMethodError) as excinfo:
        get_method("monte_carlo")
    # The error names the offending input and lists what IS available.
    msg = str(excinfo.value)
    assert "monte_carlo" in msg
    assert HEURISTIC_METHOD_NAME in msg


# --- Integration: method + optimizer chain pinned to a known answer ---------
#
# A synthetic-but-realistic 14-fighter Captain slate (7 bouts). De-vigged win
# probabilities and the 5-round set are the test inputs; the salaries are held
# in this fixture ONLY (no real CSV is committed, docs/DEVELOPMENT_NOTES.md §7). captain_salary
# is round(1.5 * base), matching the DK CPT row. The bout pairings (each ~sums
# to 1.0) are: Topuria/Gaethje, Ruffy/Chandler, O'Malley/Zahabi, Hokit/Lewis,
# Nickal/Daukaus, Gane/Pereira, Lopes/Garcia.
#
# (name, base_salary, win_prob, scheduled_rounds)
_SLATE = [
    ("Ilia Topuria", 9600, 0.791, 5),
    ("Mauricio Ruffy", 10000, 0.804, 3),
    ("Sean O'Malley", 9200, 0.782, 3),
    ("Josh Hokit", 9000, 0.772, 3),
    ("Bo Nickal", 8800, 0.722, 3),
    ("Ciryl Gane", 7600, 0.50, 5),
    ("Alex Pereira", 7400, 0.50, 5),
    ("Diego Lopes", 8400, 0.563, 3),
    ("Steve Garcia", 6600, 0.437, 3),
    ("Justin Gaethje", 5400, 0.209, 5),
    ("Kyle Daukaus", 6200, 0.278, 3),
    ("Derrick Lewis", 6000, 0.228, 3),
    ("Aiemann Zahabi", 5800, 0.218, 3),
    ("Michael Chandler", 5000, 0.196, 3),
]


def _slate_inputs() -> list[FighterProjectionInput]:
    return [
        FighterProjectionInput(
            name=name,
            base_salary=base,
            captain_salary=round(1.5 * base),
            win_prob=win_prob,
            scheduled_rounds=rounds,
        )
        for (name, base, win_prob, rounds) in _SLATE
    ]


def test_heuristic_then_optimizer_pins_known_top_lineup():
    """HeuristicMethod -> optimize_captain_lineups on the real slate (§6/§7).

    Pins the full method+optimizer chain to the known answer: the top lineup is
    CPT Alex Pereira at $49,500 / ~294.3 pts, with CPT Ciryl Gane ($49,600) the
    runner-up (both even-money 5-round heavyweights project to 50.0 base ->
    75.0 as captain; the salary tiebreak orders Pereira ahead of Gane).
    """
    candidates = get_method(HEURISTIC_METHOD_NAME).project(_slate_inputs())
    result = optimize_captain_lineups(candidates, top_n=5)

    assert result.status is CaptainOptimizerStatus.OK

    top = result.lineups[0]
    assert top.captain_name == "Alex Pereira"
    assert top.salary == 49_500
    assert top.points == pytest.approx(294.33, abs=0.05)
    assert top.flex_names == (
        "Ciryl Gane",
        "Ilia Topuria",
        "Justin Gaethje",
        "Sean O'Malley",
        "Steve Garcia",
    )

    runner_up = result.lineups[1]
    assert runner_up.captain_name == "Ciryl Gane"
    assert runner_up.salary == 49_600
    assert runner_up.points == pytest.approx(294.33, abs=0.05)


def test_chain_allows_both_fighters_of_a_bout():
    """The chain rosters both sides of a bout (§6 no same-fight exclusion).

    The top lineup pairs opponents Ilia Topuria and Justin Gaethje, and the
    top-two lineups roster both Alex Pereira and Ciryl Gane (opponents) — the
    Captain optimizer applies no same-fight constraint, unlike Classic.
    """
    candidates = get_method(HEURISTIC_METHOD_NAME).project(_slate_inputs())
    result = optimize_captain_lineups(candidates, top_n=2)

    top_names = set(result.lineups[0].fighter_names)
    assert {"Ilia Topuria", "Justin Gaethje"} <= top_names
    assert {"Alex Pereira", "Ciryl Gane"} <= top_names


def test_future_method_registers_without_touching_interface_or_optimizer():
    """The §7 seam: a new engine plugs in via register_method, heuristic intact."""

    class _StubMethod:
        # A second engine satisfies the contract structurally, not by subclassing.
        name = "stub_for_test"

        def project(self, fighter_inputs):
            return [
                CaptainCandidate(
                    name=f.name,
                    base_salary=f.base_salary,
                    captain_salary=f.captain_salary,
                    projection=0.0,
                )
                for f in fighter_inputs
            ]

    stub = _StubMethod()
    assert isinstance(stub, ProjectionMethod)
    try:
        register_method(stub)
        assert get_method("stub_for_test") is stub
        # The permanent Heuristic is still reachable alongside it (§7).
        assert isinstance(get_method(HEURISTIC_METHOD_NAME), HeuristicMethod)
        # Re-registering a live name is rejected, never silently overwritten.
        with pytest.raises(DuplicateProjectionMethodError):
            register_method(_StubMethod())
    finally:
        bm._METHODS.pop("stub_for_test", None)

    # Cleanup restored the registry; heuristic remains the registered default.
    assert "stub_for_test" not in available_methods()
    assert HEURISTIC_METHOD_NAME in available_methods()


# --- MOV finish-aware method (DESIGN §14.2, slice C10) ----------------------
#
# The Finish-aware slot ("finish_aware") now holds the MOV method:
#   adjProj(f) = default_projection(win_prob, base_salary, rounds)
#                + K * finish_signal(f)
# where finish_signal is the C9 P(win inside distance) and K defaults to 20.
# The C7 v2 method (which wrapped finish_model.compute_finish_projection) is
# RETIRED (design §14.6); see test_v2_method_is_retired.


def test_finish_aware_is_registered_alongside_heuristic():
    """get_method returns the MOV method; available_methods lists BOTH; the
    Heuristic stays intact and reachable (§7 / §14.2)."""
    method = get_method(FINISH_AWARE_METHOD_NAME)
    assert method.name == FINISH_AWARE_METHOD_NAME
    assert isinstance(method, FinishAwareMethod)
    assert isinstance(method, ProjectionMethod)

    names = available_methods()
    assert FINISH_AWARE_METHOD_NAME in names
    assert HEURISTIC_METHOD_NAME in names
    # The permanent Heuristic still resolves to its own engine.
    assert isinstance(get_method(HEURISTIC_METHOD_NAME), HeuristicMethod)


def test_experimental_flag_and_labels():
    """Finish-aware is flagged experimental; the Heuristic default is not (§7)."""
    assert is_experimental(FINISH_AWARE_METHOD_NAME) is True
    assert is_experimental(HEURISTIC_METHOD_NAME) is False
    # Labels are non-empty and distinct, so a selector can show them.
    fa_label = method_label(FINISH_AWARE_METHOD_NAME)
    heur_label = method_label(HEURISTIC_METHOD_NAME)
    assert fa_label and heur_label and fa_label != heur_label


def test_v2_method_is_retired():
    """The C7 v2 wiring is gone (design §14.6): build_method no longer imports
    finish_model.compute_finish_projection, and the Finish-aware slot is the MOV
    method (proven by its K==0 ≡ Heuristic anchor below), not the v2 wrapper."""
    assert not hasattr(bm, "compute_finish_projection")
    # The registered finish_aware engine carries the MOV finish-bonus knob.
    method = get_method(FINISH_AWARE_METHOD_NAME)
    assert isinstance(method, FinishAwareMethod)
    assert method.finish_bonus_k == FINISH_BONUS_K_DEFAULT


# §14.5 reference slate. win_prob / base_salary / scheduled_rounds produce the
# §14.5 base projections; the finish signals are computed from the C9 MOV odds
# (the same odds as tests/test_captain_finish_signal.py) so this exercises the
# real C9 -> C10 pipeline. Pereira/Gane use the design win probs (0.4957/0.5043)
# so their base projections (49.70 / 50.30) reproduce the §14.5 adjProj exactly;
# the synthetic _SLATE above rounds them to even money (base 50.0 each) for the
# Heuristic anchor and is intentionally left unchanged.
_REFERENCE_MOV_BOUTS = (
    ("Topuria", "Gaethje", (-225, 500, 1200), (500, 3500, 1600)),
    ("Pereira", "Gane", (150, 2200, 600), (330, 1100, 250)),
    ("O'Malley", "Zahabi", (180, 1600, -105), (1400, 2200, 550)),
    ("Hokit", "Lewis", (-135, 330, 700), (400, 4500, 2000)),
    ("Ruffy", "Chandler", (-200, 1000, 450), (800, 1800, 1100)),
    ("Nickal", "Daukaus", (200, 350, 225), (1100, 500, 900)),
    ("Lopes", "Garcia", (250, 400, 400), (250, 3000, 400)),
)

# name -> (display name, base_salary, win_prob, scheduled_rounds, mov-fighter-key)
_REFERENCE_SLATE = [
    ("Ilia Topuria", 9600, 0.7914, 5, "Topuria"),
    ("Mauricio Ruffy", 10000, 0.8041, 3, "Ruffy"),
    ("Sean O'Malley", 9200, 0.7819, 3, "O'Malley"),
    ("Josh Hokit", 9000, 0.7714, 3, "Hokit"),
    ("Bo Nickal", 8800, 0.722, 3, "Nickal"),
    ("Ciryl Gane", 7600, 0.5043, 5, "Gane"),
    ("Alex Pereira", 7400, 0.4957, 5, "Pereira"),
    ("Diego Lopes", 8400, 0.563, 3, "Lopes"),
    ("Steve Garcia", 6600, 0.437, 3, "Garcia"),
    ("Justin Gaethje", 5400, 0.209, 5, "Gaethje"),
    ("Kyle Daukaus", 6200, 0.278, 3, "Daukaus"),
    ("Derrick Lewis", 6000, 0.228, 3, "Lewis"),
    ("Aiemann Zahabi", 5800, 0.218, 3, "Zahabi"),
    ("Michael Chandler", 5000, 0.196, 3, "Chandler"),
]

# §14.5 adjProj (K=20), keyed by mov-fighter-key (Lopes' adjProj is undocumented).
_EXPECTED_ADJPROJ = {
    "Topuria": 76.84,
    "Ruffy": 69.07,
    "Hokit": 67.44,
    "O'Malley": 61.71,
    "Nickal": 59.70,
    "Pereira": 57.16,
    "Gane": 55.62,
}


def _reference_signals() -> dict[str, float]:
    """Finish signals for the reference slate, computed from the C9 MOV odds."""
    signals: dict[str, float] = {}
    for a, b, mov_a, mov_b in _REFERENCE_MOV_BOUTS:
        result = compute_finish_signals(
            FinishOddsBout(
                fighter_a=a,
                fighter_b=b,
                win_prob_a=0.5,
                win_prob_b=0.5,
                mov_a=MethodOfVictoryOdds(*mov_a),
                mov_b=MethodOfVictoryOdds(*mov_b),
            )
        )
        signals[result.fighter_a.name] = result.fighter_a.finish_signal
        signals[result.fighter_b.name] = result.fighter_b.finish_signal
    return signals


def _reference_inputs(*, with_signal: bool = True) -> list[FighterProjectionInput]:
    signals = _reference_signals()
    inputs: list[FighterProjectionInput] = []
    for name, base, win_prob, rounds, key in _REFERENCE_SLATE:
        inputs.append(
            FighterProjectionInput(
                name=name,
                base_salary=base,
                captain_salary=round(1.5 * base),
                win_prob=win_prob,
                scheduled_rounds=rounds,
                finish_signal=signals[key] if with_signal else None,
            )
        )
    return inputs


def test_finish_aware_adjproj_reproduces_reference_slate():
    """adjProj = baseProj + K·finish_signal reproduces the §14.5 numbers (~0.05)
    and equals default_projection(...) + K·signal exactly (design §14.2)."""
    signals = _reference_signals()
    inputs = _reference_inputs()
    candidates = FinishAwareMethod(finish_bonus_k=20.0).project(inputs)
    by_name = {c.name: c.projection for c in candidates}
    key_by_display = {disp: key for disp, _b, _w, _r, key in _REFERENCE_SLATE}

    for name, base, win_prob, rounds, key in _REFERENCE_SLATE:
        expected_formula = default_projection(win_prob, base, rounds) + 20.0 * signals[key]
        assert by_name[name] == pytest.approx(expected_formula)
        if key in _EXPECTED_ADJPROJ:
            assert by_name[name] == pytest.approx(_EXPECTED_ADJPROJ[key], abs=0.05)

    # Spot-check the two fighters the synthetic slate could not pin to §14.5.
    assert by_name["Alex Pereira"] == pytest.approx(57.16, abs=0.05)
    assert by_name["Ciryl Gane"] == pytest.approx(55.62, abs=0.05)
    assert key_by_display["Ilia Topuria"] == "Topuria"


def test_k_zero_reproduces_heuristic_exactly():
    """K == 0 is the exact-Heuristic anchor (design §14.2): every projection
    equals the Heuristic's, even with finish signals present."""
    inputs = _reference_inputs(with_signal=True)
    fa = FinishAwareMethod(finish_bonus_k=0.0).project(inputs)
    heur = HeuristicMethod().project(inputs)
    assert [c.name for c in fa] == [c.name for c in heur]
    for fa_c, heur_c in zip(fa, heur):
        assert fa_c.projection == heur_c.projection


def test_none_signal_reproduces_heuristic_exactly():
    """A None finish signal contributes no bonus even at K=20 (graceful — no MOV
    data → no bonus), so adjProj == baseProj == Heuristic (design §14.2)."""
    inputs = _reference_inputs(with_signal=False)
    assert all(i.finish_signal is None for i in inputs)
    fa = FinishAwareMethod(finish_bonus_k=20.0).project(inputs)
    heur = HeuristicMethod().project(inputs)
    for fa_c, heur_c in zip(fa, heur):
        assert fa_c.projection == heur_c.projection


def test_finish_aware_output_is_candidates_in_input_order():
    inputs = _reference_inputs()
    candidates = FinishAwareMethod().project(inputs)
    assert all(isinstance(c, CaptainCandidate) for c in candidates)
    assert [c.name for c in candidates] == [s[0] for s in _REFERENCE_SLATE]


def test_finish_aware_empty_input_yields_empty_candidates():
    assert FinishAwareMethod().project([]) == []


def test_finish_aware_then_optimizer_pins_cash_optimum():
    """Reference signals + K=20 -> optimize_captain_lineups: the cash optimum is
    CPT Ilia Topuria, $50,000, ~347.79 pts (§14.5), and it beats the Heuristic
    top on the same slate (the MOV signal lifts the finisher-heavy build)."""
    candidates = get_method(FINISH_AWARE_METHOD_NAME).project(_reference_inputs())
    result = optimize_captain_lineups(candidates, top_n=5)
    assert result.status is CaptainOptimizerStatus.OK

    top = result.lineups[0]
    assert top.captain_name == "Ilia Topuria"
    assert top.salary == 50_000
    # §14.5 cash optimum 347.79; the reference-slate value is 347.84.
    assert top.points == pytest.approx(347.79, abs=0.1)

    # Higher than the Heuristic top on the same slate (no finish bonus).
    heur_top = optimize_captain_lineups(
        HeuristicMethod().project(_reference_inputs(with_signal=False)), top_n=1
    ).lineups[0]
    assert top.points > heur_top.points


def test_finish_aware_input_validates_finish_signal():
    """A finish signal outside [0, 1] is a typed error at construction (§14.2)."""
    with pytest.raises(ValueError, match="finish_signal"):
        FighterProjectionInput("X", 8000, 12000, 0.5, 3, finish_signal=1.5)


# --- Stack toggle on the reference slate (DESIGN §14.3 / §14.5, slice C11a) --
#
# The §14.5 reference bouts, by display name, so the optimizer can apply the GPP
# same-fight exclusion to the finish-aware candidates.
_REFERENCE_BOUTS = [
    ("Ilia Topuria", "Justin Gaethje"),
    ("Mauricio Ruffy", "Michael Chandler"),
    ("Sean O'Malley", "Aiemann Zahabi"),
    ("Josh Hokit", "Derrick Lewis"),
    ("Bo Nickal", "Kyle Daukaus"),
    ("Ciryl Gane", "Alex Pereira"),
    ("Diego Lopes", "Steve Garcia"),
]


def test_finish_aware_cash_optimum_unchanged_with_pairs():
    """Cash + the bout pairings reproduces the §14.5 cash optimum exactly — the
    pairs are ignored in cash (CPT Ilia Topuria, $50,000, ~347.79)."""
    candidates = get_method(FINISH_AWARE_METHOD_NAME).project(_reference_inputs())
    result = optimize_captain_lineups(
        candidates,
        top_n=1,
        stack_mode=StackMode.CASH,
        same_fight_pairs=_REFERENCE_BOUTS,
    )
    top = result.lineups[0]
    assert top.captain_name == "Ilia Topuria"
    assert top.salary == 50_000
    assert top.points == pytest.approx(347.79, abs=0.1)


def test_finish_aware_gpp_pure_ev_pins_garcia():
    """GPP, pure EV (no captain-leverage rule — that is C11b): the free-captain
    optimum is CPT Steve Garcia, $49,700, ~331.37 (design §14.5)."""
    candidates = get_method(FINISH_AWARE_METHOD_NAME).project(_reference_inputs())
    result = optimize_captain_lineups(
        candidates,
        top_n=5,
        stack_mode=StackMode.GPP,
        same_fight_pairs=_REFERENCE_BOUTS,
    )
    top = result.lineups[0]
    assert top.captain_name == "Steve Garcia"
    assert top.salary == 49_700
    assert top.points == pytest.approx(331.37, abs=0.1)
    # No GPP lineup rosters both sides of any reference bout.
    pairs = [frozenset(p) for p in _REFERENCE_BOUTS]
    for line in result.lineups:
        names = set(line.fighter_names)
        assert all(not (pair <= names) for pair in pairs)


def test_finish_aware_gpp_best_lineup_per_captain_ranking():
    """The GPP best-lineup-per-captain ranking (design §14.5, abs ~0.1):
    Garcia 331.37 > Pereira 326.90 > Gaethje 326.53 > Gane 324.59 >
    Nickal 324.58 > Topuria 323.96."""
    candidates = get_method(FINISH_AWARE_METHOD_NAME).project(_reference_inputs())
    # A top_n far above the feasible count returns every lineup, sorted desc, so
    # each captain's first appearance is its best lineup.
    result = optimize_captain_lineups(
        candidates,
        top_n=100_000,
        stack_mode=StackMode.GPP,
        same_fight_pairs=_REFERENCE_BOUTS,
    )
    best: dict[str, float] = {}
    for line in result.lineups:
        if line.captain_name not in best:
            best[line.captain_name] = line.points
    ranked = sorted(best.items(), key=lambda kv: -kv[1])[:6]

    expected = [
        ("Steve Garcia", 331.37),
        ("Alex Pereira", 326.90),
        ("Justin Gaethje", 326.53),
        ("Ciryl Gane", 324.59),
        ("Bo Nickal", 324.58),
        ("Ilia Topuria", 323.96),
    ]
    assert [name for name, _ in ranked] == [name for name, _ in expected]
    for (name, pts), (_exp_name, exp_pts) in zip(ranked, expected):
        assert pts == pytest.approx(exp_pts, abs=0.1)


# --- Captain-leverage CPTproj rule (DESIGN §14.4 / §14.5, slice C11b) --------
#
# CPTproj = 1.5 × adjProj ranks captains by their points AS Captain. The §14.5
# adjProj order is Topuria 76.84 > Ruffy 69.07 > Hokit 67.44 > O'Malley 61.71 >
# Nickal 59.70 > Pereira 57.16 > Gane 55.62 (the documented favorites), so the
# CPTproj ranking puts Topuria top and orders these by adjProj. Pinning the top
# (Topuria) in GPP yields the §14.5 leverage lineup (323.96) — trading EV for
# ceiling vs the GPP free-EV optimum (Garcia 331.37, C11a).

# §14.5 adjProj order for the documented favorites (the CPTproj order, since
# CPTproj is a positive multiple of adjProj).
_CPTPROJ_ORDER = [
    "Ilia Topuria",
    "Mauricio Ruffy",
    "Josh Hokit",
    "Sean O'Malley",
    "Bo Nickal",
    "Alex Pereira",
    "Ciryl Gane",
]


def test_cptproj_ranking_puts_topuria_top_and_orders_by_adjproj():
    """CPTproj = 1.5 × adjProj ranks Topuria top (1.5 × 76.84 = 115.26) and orders
    the documented favorites by adjProj: Ruffy, Hokit, O'Malley, Nickal, Pereira,
    Gane (design §14.4 / §14.5)."""
    candidates = get_method(FINISH_AWARE_METHOD_NAME).project(_reference_inputs())
    ranked = rank_captains_by_cptproj(
        candidates, stack_mode=StackMode.GPP, same_fight_pairs=_REFERENCE_BOUTS
    )
    names = [r.captain_name for r in ranked]

    # Topuria is the leverage top; its CPTproj is 1.5 × its adjProj (76.84).
    assert names[0] == "Ilia Topuria"
    assert ranked[0].cptproj == pytest.approx(1.5 * 76.84, abs=0.1)

    # The documented favorites appear in adjProj (= CPTproj) order within the
    # full ranking (the cheap underdogs interleave lower and are not pinned here).
    assert [n for n in names if n in _CPTPROJ_ORDER] == _CPTPROJ_ORDER

    # CPTproj is exactly 1.5 × the candidate's (adjusted) projection — pure
    # selection logic, no projection recomputed.
    by_proj = {c.name: c.projection for c in candidates}
    for r in ranked:
        assert r.cptproj == pytest.approx(1.5 * by_proj[r.captain_name])


def test_gpp_pin_top_cptproj_captain_yields_leverage_lineup():
    """Pinning the top-CPTproj captain (Topuria) in GPP yields the §14.5 leverage
    lineup: CPT Ilia Topuria, $50,000, ~323.96 — distinct from and lower than the
    C11a GPP free-EV optimum (Garcia 331.37), trading EV for ceiling (§14.4)."""
    candidates = get_method(FINISH_AWARE_METHOD_NAME).project(_reference_inputs())
    ranked = rank_captains_by_cptproj(
        candidates, stack_mode=StackMode.GPP, same_fight_pairs=_REFERENCE_BOUTS
    )
    top_captain = ranked[0].captain_name
    assert top_captain == "Ilia Topuria"

    pinned = optimize_captain_lineups(
        candidates,
        top_n=5,
        stack_mode=StackMode.GPP,
        same_fight_pairs=_REFERENCE_BOUTS,
        captain=top_captain,
    )
    top = pinned.lineups[0]
    assert top.captain_name == "Ilia Topuria"
    assert top.salary == 50_000
    assert top.points == pytest.approx(323.96, abs=0.1)
    # The ranking's reported best-lineup total matches the pinned build.
    assert ranked[0].best_total == pytest.approx(323.96, abs=0.1)

    # Distinct from — and lower than — the GPP free-EV optimum (CPT Garcia 331.37).
    free = optimize_captain_lineups(
        candidates,
        top_n=1,
        stack_mode=StackMode.GPP,
        same_fight_pairs=_REFERENCE_BOUTS,
    )
    assert free.lineups[0].captain_name == "Steve Garcia"
    assert free.lineups[0].points == pytest.approx(331.37, abs=0.1)
    assert top.captain_name != free.lineups[0].captain_name
    assert top.points < free.lineups[0].points


def test_cptproj_top_not_reproduced_by_win_probability_floor():
    """A win% floor does NOT reproduce the leverage pick (design §14.4): at
    θ=0.55 the EV-max eligible captain is Bo Nickal (cheaper, frees salary), while
    the CPTproj top is Ilia Topuria — the two disagree, so the CPTproj ranking is
    required (a floor is not a substitute)."""
    candidates = get_method(FINISH_AWARE_METHOD_NAME).project(_reference_inputs())
    cptproj_top = rank_captains_by_cptproj(
        candidates, stack_mode=StackMode.GPP, same_fight_pairs=_REFERENCE_BOUTS
    )[0].captain_name
    assert cptproj_top == "Ilia Topuria"

    # The win% floor heuristic: among captains over θ=0.55, the one whose best GPP
    # lineup has the highest EV. win_prob lives on the projection INPUT, not the
    # candidate, so source it from the reference slate.
    theta = 0.55
    win_by_name = {name: wp for name, _b, wp, _r, _k in _REFERENCE_SLATE}
    eligible = [c.name for c in candidates if win_by_name[c.name] >= theta]
    best_total: dict[str, float] = {}
    for name in eligible:
        res = optimize_captain_lineups(
            candidates,
            top_n=1,
            stack_mode=StackMode.GPP,
            same_fight_pairs=_REFERENCE_BOUTS,
            captain=name,
        )
        if res.lineups:
            best_total[name] = res.lineups[0].points
    win_floor_pick = max(best_total, key=best_total.get)

    assert win_floor_pick == "Bo Nickal"
    assert win_floor_pick != cptproj_top


def test_cash_leverage_pick_equals_free_ev_captain():
    """In cash the leverage pick already == the free-EV captain (no conflict —
    design §14.4): the top-CPTproj captain (Topuria) is also the cash free-EV
    optimum (CPT Topuria, $50,000, ~347.79)."""
    candidates = get_method(FINISH_AWARE_METHOD_NAME).project(_reference_inputs())
    cptproj_top = rank_captains_by_cptproj(
        candidates, stack_mode=StackMode.CASH, same_fight_pairs=_REFERENCE_BOUTS
    )[0].captain_name
    assert cptproj_top == "Ilia Topuria"

    free = optimize_captain_lineups(
        candidates,
        top_n=1,
        stack_mode=StackMode.CASH,
        same_fight_pairs=_REFERENCE_BOUTS,
    )
    assert free.lineups[0].captain_name == "Ilia Topuria"
    assert free.lineups[0].points == pytest.approx(347.79, abs=0.1)
    # Pinning the leverage top in cash reproduces the same free-EV optimum.
    pinned = optimize_captain_lineups(
        candidates,
        top_n=1,
        stack_mode=StackMode.CASH,
        same_fight_pairs=_REFERENCE_BOUTS,
        captain=cptproj_top,
    )
    assert pinned.lineups[0].points == pytest.approx(free.lineups[0].points)


def test_free_ev_regression_unchanged_by_pin_parameter():
    """Regression (§14.4): the free-EV optimum (captain=None) is unchanged — GPP
    Garcia 331.37, cash Topuria 347.79 — proving the additive pin defaults inert."""
    candidates = get_method(FINISH_AWARE_METHOD_NAME).project(_reference_inputs())
    gpp = optimize_captain_lineups(
        candidates,
        top_n=1,
        stack_mode=StackMode.GPP,
        same_fight_pairs=_REFERENCE_BOUTS,
    )
    assert gpp.lineups[0].captain_name == "Steve Garcia"
    assert gpp.lineups[0].points == pytest.approx(331.37, abs=0.1)

    cash = optimize_captain_lineups(
        candidates,
        top_n=1,
        stack_mode=StackMode.CASH,
        same_fight_pairs=_REFERENCE_BOUTS,
    )
    assert cash.lineups[0].captain_name == "Ilia Topuria"
    assert cash.lineups[0].points == pytest.approx(347.79, abs=0.1)
