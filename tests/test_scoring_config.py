"""Locking test for the DK UFC Classic scoring constants (Phase 0).

Pins every constant in ``src.config.scoring`` so an accidental edit fails loudly.
Mirrors ``tests/test_projection_formula.py``. This test proves the module matches
the *agreed* (researched) table — it does NOT validate the values against DK, and
it cannot catch DK-side rule drift (re-verify periodically per the module
docstring).
"""

from src.config import scoring


def test_per_action_scoring_constants_locked():
    assert scoring.STRIKE == 0.2
    assert scoring.SIG_STRIKE == 0.2  # bonus on top of STRIKE => +0.4 total
    assert scoring.TAKEDOWN == 5.0
    assert scoring.REVERSAL_SWEEP == 5.0
    assert scoring.KNOCKDOWN == 10.0
    assert scoring.CONTROL_TIME_PER_SEC == 0.03  # per SECOND (= +1.8/min)


def test_significant_strike_total_is_point_four():
    # A significant strike scores the base strike AND the significant bonus.
    assert scoring.STRIKE + scoring.SIG_STRIKE == 0.4


def test_win_bonuses_locked():
    assert scoring.WIN_FIRST_ROUND == 90.0
    assert scoring.WIN_SECOND_ROUND == 70.0
    assert scoring.WIN_THIRD_ROUND == 45.0
    assert scoring.WIN_FOURTH_ROUND == 40.0
    assert scoring.WIN_FIFTH_ROUND == 40.0
    assert scoring.WIN_DECISION == 30.0
    assert scoring.QUICK_WIN_BONUS_R1 == 25.0


def test_round_win_bonuses_are_non_increasing():
    # Non-increasing but NOT strictly decreasing (R4 and R5 tied at 40).
    bonuses = [
        scoring.WIN_FIRST_ROUND,
        scoring.WIN_SECOND_ROUND,
        scoring.WIN_THIRD_ROUND,
        scoring.WIN_FOURTH_ROUND,
        scoring.WIN_FIFTH_ROUND,
    ]
    assert bonuses == sorted(bonuses, reverse=True)
    assert scoring.WIN_FOURTH_ROUND == scoring.WIN_FIFTH_ROUND  # the tie is real
    # Decision is the floor of the win-resolution bonuses.
    assert scoring.WIN_DECISION < scoring.WIN_FIFTH_ROUND


def test_phantom_advance_constant_is_removed():
    # "Advancing Position" was removed in the Jan 2021 overhaul. It must not exist
    # (deleted, not rezeroed — a present-but-zero constant invites a bad "fix").
    assert not hasattr(scoring, "ADVANCE")


def test_no_method_or_submission_bonus_constants():
    # The win bonus is round-based only; there is no KO-vs-sub method bonus and no
    # submission-attempt line item. Guard against someone inventing them.
    for forbidden in ("KO_BONUS", "TKO_BONUS", "SUBMISSION_BONUS", "SUB_ATTEMPT"):
        assert not hasattr(scoring, forbidden)


def test_secondary_sourced_bonuses_are_documented_and_unverified():
    # Fork A: the five round/decision bonuses are secondary-sourced. The module
    # must advertise that and must NOT claim primary verification yet.
    assert scoring.SECONDARY_SOURCED_BONUSES == (
        "WIN_SECOND_ROUND",
        "WIN_THIRD_ROUND",
        "WIN_FOURTH_ROUND",
        "WIN_FIFTH_ROUND",
        "WIN_DECISION",
    )
    # Every named secondary-sourced bonus actually exists on the module.
    for name in scoring.SECONDARY_SOURCED_BONUSES:
        assert hasattr(scoring, name)
    # Not yet confirmed against the in-app DK legend.
    assert scoring.DK_SCORING_VERIFIED_ON is None
    assert scoring.DK_SCORING_RESEARCHED_ON == "2026-06-06"
