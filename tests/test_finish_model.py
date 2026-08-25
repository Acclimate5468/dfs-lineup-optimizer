"""Invariant tests for the Tier-0 finish-aware model (Phase A).

Covers the design §6 invariants plus the behavioral invariants required for the
session. These pin the model's *structure and monotonicity*; they do NOT claim
v2 beats v0 (that is the Phase A' calibration's job).
"""

import math

import pytest

from src.config import scoring
from src.projections import finish_model as fm
from src.projections.finish_model import compute_finish_projection


def _prob(result, label):
    return next(b.probability for b in result.outcome_branches if b.label == label)


def _pts(result, label):
    return next(b.expected_points for b in result.outcome_branches if b.label == label)


# --- Branch probability invariants (design §6.1) ------------------------------

@pytest.mark.parametrize("p_win", [0.0, 0.2, 0.5, 0.63, 0.8, 1.0])
@pytest.mark.parametrize("rounds", [3, 5])
def test_branch_probabilities_sum_to_one_and_in_unit_interval(p_win, rounds):
    r = compute_finish_projection(p_win, rounds, p_fight_finishes=0.57, finish_share=0.5)
    probs = [b.probability for b in r.outcome_branches]
    assert all(0.0 <= p <= 1.0 for p in probs)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)


@pytest.mark.parametrize("p_win", [0.1, 0.4, 0.5, 0.75, 0.9])
def test_finish_win_plus_decision_win_equals_p_win_exactly(p_win):
    r = compute_finish_projection(p_win, 3, p_fight_finishes=0.57, finish_share=0.5)
    assert math.isclose(
        _prob(r, fm.FINISH_WIN) + _prob(r, fm.DECISION_WIN), p_win, abs_tol=1e-12
    )
    # And the loss side mirrors it.
    assert math.isclose(
        _prob(r, fm.FINISH_LOSS) + _prob(r, fm.DECISION_LOSS), 1.0 - p_win, abs_tol=1e-12
    )


def test_finish_share_defaults_to_p_win():
    r = compute_finish_projection(0.6, 3)
    assert r.finish_share == 0.6
    assert r.p_fight_finishes == fm.LEAGUE_FINISH_RATE


# --- Mean / bounds invariants -------------------------------------------------

def test_mean_is_bounded_by_branch_values():
    r = compute_finish_projection(0.6, 3, p_fight_finishes=0.57, finish_share=0.6)
    vals = [b.expected_points for b in r.outcome_branches]
    assert min(vals) - 1e-9 <= r.projected_dk_points <= max(vals) + 1e-9
    assert r.worst_branch_pts == min(vals)
    assert r.best_branch_pts == max(vals)


def test_mean_equals_probability_weighted_branches():
    r = compute_finish_projection(0.55, 5, p_fight_finishes=0.6, finish_share=0.55)
    manual = sum(b.probability * b.expected_points for b in r.outcome_branches)
    assert math.isclose(r.projected_dk_points, manual, abs_tol=1e-9)


@pytest.mark.parametrize("p_win", [0.0, 0.25, 0.5, 0.75, 1.0])
@pytest.mark.parametrize("rounds", [3, 5])
def test_no_negative_or_impossible_outputs(p_win, rounds):
    r = compute_finish_projection(p_win, rounds)
    assert r.projected_dk_points >= 0.0
    for b in r.outcome_branches:
        assert b.probability >= 0.0
        assert b.expected_points >= 0.0


# --- Monotonicity invariants --------------------------------------------------

def test_higher_win_probability_does_not_decrease_projection():
    # finish_share held FIXED to isolate the win-probability effect.
    means = [
        compute_finish_projection(p, 3, p_fight_finishes=0.57, finish_share=0.5).projected_dk_points
        for p in (0.1, 0.3, 0.5, 0.7, 0.9)
    ]
    for lo, hi in zip(means, means[1:]):
        assert hi >= lo - 1e-9


def test_higher_finish_probability_increases_finish_win_upside():
    # Holding p_win and finish_share, more finish likelihood => more mass on the
    # high-ceiling finish-win branch and a larger finish-win contribution.
    low = compute_finish_projection(0.5, 3, p_fight_finishes=0.40, finish_share=0.5)
    high = compute_finish_projection(0.5, 3, p_fight_finishes=0.70, finish_share=0.5)
    assert _prob(high, fm.FINISH_WIN) > _prob(low, fm.FINISH_WIN)
    contrib_low = _prob(low, fm.FINISH_WIN) * _pts(low, fm.FINISH_WIN)
    contrib_high = _prob(high, fm.FINISH_WIN) * _pts(high, fm.FINISH_WIN)
    assert contrib_high > contrib_low
    # The per-branch ceiling itself is invariant to the finish probability.
    assert math.isclose(_pts(low, fm.FINISH_WIN), _pts(high, fm.FINISH_WIN), abs_tol=1e-9)


def test_five_round_outprojects_three_round_via_accumulation_not_bonus():
    three = compute_finish_projection(0.5, 3, p_fight_finishes=0.57, finish_share=0.5)
    five = compute_finish_projection(0.5, 5, p_fight_finishes=0.57, finish_share=0.5)
    # 5-round fighter outprojects the same 3-round fighter at equal inputs...
    assert five.projected_dk_points > three.projected_dk_points
    # ...and it is NOT because the finish bonus is higher (it is lower — more late
    # finishes at 40 vs early at 90). The gain is accumulation (design §16 Q6).
    assert fm.expected_finish_bonus(5) <= fm.expected_finish_bonus(3)
    assert fm.full_length(5) > fm.full_length(3)


# --- No-double-count invariant (design §6.2) ----------------------------------

def test_finish_accumulation_is_shorter_than_decision_accumulation():
    for rounds in (3, 5):
        fin_len = fm.expected_finish_length(rounds)
        dist_len = fm.full_length(rounds)
        assert fin_len < dist_len
        # Winner action in a finish < winner action in a decision (round ratio).
        assert fm.accumulated_points(fm.WINNER_ACC_RATE_PER_ROUND, fin_len) < \
            fm.accumulated_points(fm.WINNER_ACC_RATE_PER_ROUND, dist_len)


def test_finish_win_has_no_double_counted_accumulation():
    for rounds in (3, 5):
        pts = fm.branch_expected_points(rounds)
        fin_len = fm.expected_finish_length(rounds)
        # E[finish win] uses the SHARED finish length, not scheduled_rounds.
        expected = fm.expected_finish_bonus(rounds) + \
            fm.accumulated_points(fm.WINNER_ACC_RATE_PER_ROUND, fin_len)
        assert math.isclose(pts[fm.FINISH_WIN], expected, abs_tol=1e-9)
        # Upper bound: bonus can't exceed R1 win + quick-win, accumulation is
        # consistent with the finish length (no full-fight total bolted on).
        max_bonus = scoring.WIN_FIRST_ROUND + scoring.QUICK_WIN_BONUS_R1
        assert pts[fm.FINISH_WIN] <= max_bonus + \
            fm.accumulated_points(fm.WINNER_ACC_RATE_PER_ROUND, fin_len) + 1e-9


def test_finish_action_length_pins_shared_latent_length():
    # The single shared latent length that couples bonus + accumulation (§6.2).
    # Pinned directly so a mutation to the helper fails loudly (the per-round /
    # branch reconciliation below cannot catch a change applied to BOTH sides).
    assert [fm.finish_action_length(r) for r in range(1, 6)] == [0.5, 1.5, 2.5, 3.5, 4.5]


def test_later_finish_round_lowers_bonus_but_raises_accumulation():
    bonuses = [fm.win_bonus_for_finish_round(r) for r in range(1, 6)]
    # Recover accumulation through the SOURCE helper (not a hand-copied formula),
    # so a mutation to the production length is visible here too.
    accs = [fm.WINNER_ACC_RATE_PER_ROUND * fm.finish_action_length(r) for r in range(1, 6)]
    # Bonus is non-increasing across rounds...
    assert bonuses == sorted(bonuses, reverse=True)
    # ...while accumulation strictly increases (the monotone trade-off).
    for lo, hi in zip(accs, accs[1:]):
        assert hi > lo


def test_per_round_finish_points_reconcile_with_finish_win_branch():
    # expected_points_for_finish_in_round() is a documented contract: weighted by
    # the finish-round distribution it must equal the finish-win branch value.
    # Pins the (previously untested) public helper and per-round/aggregate
    # consistency — a divergence between the two computations surfaces here.
    for rounds in (3, 5):
        weights = fm.FINISH_ROUND_WEIGHTS[rounds]
        recon = sum(
            w * fm.expected_points_for_finish_in_round(r + 1)
            for r, w in enumerate(weights)
        )
        assert math.isclose(
            recon, fm.branch_expected_points(rounds)[fm.FINISH_WIN], abs_tol=1e-9
        )


def test_finish_loss_is_small_and_decision_loss_is_substantial():
    # The loss split is the whole point of the model: an early finish loss scores
    # almost nothing; a competitive decision loss banks full-fight volume.
    pts = fm.branch_expected_points(3)
    assert pts[fm.FINISH_LOSS] < pts[fm.DECISION_LOSS]
    assert pts[fm.DECISION_LOSS] >= 40.0  # design §2: "40-80 pts" range floor


# --- Degenerate clamp (design §6.1) -------------------------------------------

def test_degenerate_finish_signal_clamps_with_warning_not_silently():
    # finish signal implies more finish-wins than the fighter has win prob.
    r = compute_finish_projection(0.2, 3, p_fight_finishes=0.9, finish_share=0.9)
    assert r.warnings  # not silent
    assert math.isclose(_prob(r, fm.DECISION_WIN), 0.0, abs_tol=1e-12)
    probs = [b.probability for b in r.outcome_branches]
    assert all(0.0 <= p <= 1.0 for p in probs)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)


def test_degenerate_finish_loss_signal_clamps_with_warning():
    r = compute_finish_projection(0.9, 3, p_fight_finishes=0.9, finish_share=0.1)
    assert r.warnings
    assert math.isclose(_prob(r, fm.DECISION_LOSS), 0.0, abs_tol=1e-12)
    assert math.isclose(
        sum(b.probability for b in r.outcome_branches), 1.0, abs_tol=1e-9
    )


# --- Missing / invalid inputs degrade safely (design §9) ----------------------

def test_missing_win_probability_degrades_safely():
    r = compute_finish_projection(None, 3)
    assert r.projection_status == fm.STATUS_MISSING_INPUTS
    assert "win_probability" in r.missing_inputs
    assert r.projected_dk_points is None
    assert r.outcome_branches == ()
    assert r.best_branch_pts is None and r.worst_branch_pts is None


@pytest.mark.parametrize("rounds", [None, 4, 0, 2])
def test_invalid_scheduled_rounds_degrades_safely(rounds):
    r = compute_finish_projection(0.5, rounds)
    assert r.projection_status == fm.STATUS_MISSING_INPUTS
    assert "scheduled_rounds" in r.missing_inputs


def test_out_of_range_numeric_inputs_raise():
    with pytest.raises(ValueError):
        compute_finish_projection(1.5, 3)
    with pytest.raises(ValueError):
        compute_finish_projection(-0.1, 3)
    with pytest.raises(ValueError):
        compute_finish_projection(0.5, 3, p_fight_finishes=1.2)
    with pytest.raises(ValueError):
        compute_finish_projection(0.5, 3, finish_share=-0.1)


# --- Output shape -------------------------------------------------------------

def test_projection_mode_is_tagged():
    r = compute_finish_projection(0.5, 3)
    assert r.projection_mode == "v2_finish"
    assert len(r.outcome_branches) == 4


def test_best_is_finish_win_and_worst_is_finish_loss_in_typical_case():
    r = compute_finish_projection(0.6, 3, p_fight_finishes=0.57, finish_share=0.6)
    assert r.best_branch_pts == _pts(r, fm.FINISH_WIN)
    assert r.worst_branch_pts == _pts(r, fm.FINISH_LOSS)
