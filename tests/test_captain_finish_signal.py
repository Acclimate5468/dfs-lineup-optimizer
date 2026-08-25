"""Tests for the Captain method-of-victory finish signal (C9, design §14.1).

Pins the §14.5 reference-slate finish signals (to ~0.1%), the §14.1 fallback
ladder (tier 0 MOV → tier 1 go-the-distance → tier 2 round-total, with the tier
recorded), and the typed-error contract on malformed / missing odds.
"""

from __future__ import annotations

import pytest

from src.captain.finish_signal import (
    BoutFinishSignal,
    FighterFinishSignal,
    FinishOddsBout,
    FinishSignalError,
    FinishSignalTier,
    MethodOfVictoryOdds,
    compute_finish_signals,
)

# ---------------------------------------------------------------------------
# §14.5 reference slate — method-of-victory odds (American) per bout.
# win_prob_* is unused by tier 0; a placeholder 0.5 keeps the inputs valid.
# ---------------------------------------------------------------------------

_REFERENCE_BOUTS = (
    FinishOddsBout(
        fighter_a="Topuria",
        fighter_b="Gaethje",
        win_prob_a=0.5,
        win_prob_b=0.5,
        mov_a=MethodOfVictoryOdds(ko_tko=-225, submission=500, decision=1200),
        mov_b=MethodOfVictoryOdds(ko_tko=500, submission=3500, decision=1600),
    ),
    FinishOddsBout(
        fighter_a="Pereira",
        fighter_b="Gane",
        win_prob_a=0.5,
        win_prob_b=0.5,
        mov_a=MethodOfVictoryOdds(ko_tko=150, submission=2200, decision=600),
        mov_b=MethodOfVictoryOdds(ko_tko=330, submission=1100, decision=250),
    ),
    FinishOddsBout(
        fighter_a="O'Malley",
        fighter_b="Zahabi",
        win_prob_a=0.5,
        win_prob_b=0.5,
        mov_a=MethodOfVictoryOdds(ko_tko=180, submission=1600, decision=-105),
        mov_b=MethodOfVictoryOdds(ko_tko=1400, submission=2200, decision=550),
    ),
    FinishOddsBout(
        fighter_a="Hokit",
        fighter_b="Lewis",
        win_prob_a=0.5,
        win_prob_b=0.5,
        mov_a=MethodOfVictoryOdds(ko_tko=-135, submission=330, decision=700),
        mov_b=MethodOfVictoryOdds(ko_tko=400, submission=4500, decision=2000),
    ),
    FinishOddsBout(
        fighter_a="Ruffy",
        fighter_b="Chandler",
        win_prob_a=0.5,
        win_prob_b=0.5,
        mov_a=MethodOfVictoryOdds(ko_tko=-200, submission=1000, decision=450),
        mov_b=MethodOfVictoryOdds(ko_tko=800, submission=1800, decision=1100),
    ),
    FinishOddsBout(
        fighter_a="Nickal",
        fighter_b="Daukaus",
        win_prob_a=0.5,
        win_prob_b=0.5,
        mov_a=MethodOfVictoryOdds(ko_tko=200, submission=350, decision=225),
        mov_b=MethodOfVictoryOdds(ko_tko=1100, submission=500, decision=900),
    ),
    FinishOddsBout(
        fighter_a="Lopes",
        fighter_b="Garcia",
        win_prob_a=0.5,
        win_prob_b=0.5,
        mov_a=MethodOfVictoryOdds(ko_tko=250, submission=400, decision=400),
        mov_b=MethodOfVictoryOdds(ko_tko=250, submission=3000, decision=400),
    ),
)

# Expected finish signals from design §14.5 (assert to ~0.1%).
_EXPECTED_FINISH_SIGNALS = {
    "Topuria": 0.722,
    "Hokit": 0.672,
    "Ruffy": 0.639,
    "Nickal": 0.458,
    "Lopes": 0.404,
    "Pereira": 0.373,
    "O'Malley": 0.349,
    "Gane": 0.266,
}


def _signals_by_name() -> dict[str, FighterFinishSignal]:
    out: dict[str, FighterFinishSignal] = {}
    for bout in _REFERENCE_BOUTS:
        result = compute_finish_signals(bout)
        out[result.fighter_a.name] = result.fighter_a
        out[result.fighter_b.name] = result.fighter_b
    return out


@pytest.mark.parametrize("name, expected", sorted(_EXPECTED_FINISH_SIGNALS.items()))
def test_reference_slate_finish_signals(name: str, expected: float) -> None:
    """Tier-0 MOV signals match the §14.5 reference slate to ~0.1%."""
    signal = _signals_by_name()[name]
    assert signal.finish_signal == pytest.approx(expected, abs=0.001)
    assert signal.tier is FinishSignalTier.METHOD_OF_VICTORY


def test_finish_signal_is_devig_share_of_six_outcomes() -> None:
    """finish_signal = (impliedKO + impliedSub) / S over all six outcomes."""
    # Topuria/Gaethje. Numerators: Topuria KO+Sub = .692308 + .166667 = .858974;
    # Gaethje KO+Sub = .166667 + .027778 = .194444. S (all six) = 1.189165.
    result = compute_finish_signals(_REFERENCE_BOUTS[0])
    # Absolute signals (Gaethje is the B side, not in the §14.5 single-fighter list).
    assert result.fighter_a.finish_signal == pytest.approx(0.722334, abs=1e-4)
    assert result.fighter_b.finish_signal == pytest.approx(0.163514, abs=1e-4)
    # Structural: the two finish shares are in the ratio of their numerators,
    # independent of the shared de-vig denominator S.
    ratio = result.fighter_a.finish_signal / result.fighter_b.finish_signal
    assert ratio == pytest.approx(0.858974 / 0.194444, abs=1e-3)


def test_tier_recorded_per_fighter() -> None:
    """Both fighters carry the same recorded tier; the bout exposes it too."""
    result = compute_finish_signals(_REFERENCE_BOUTS[0])
    assert isinstance(result, BoutFinishSignal)
    assert result.fighter_a.tier is FinishSignalTier.METHOD_OF_VICTORY
    assert result.fighter_b.tier is FinishSignalTier.METHOD_OF_VICTORY
    assert result.tier is FinishSignalTier.METHOD_OF_VICTORY


# ---------------------------------------------------------------------------
# Fallback ladder (§14.1)
# ---------------------------------------------------------------------------


def test_tier1_distance_fallback_when_no_mov_tree() -> None:
    """No MOV tree → tier 1 = P(not distance) × win_prob, tier recorded."""
    bout = FinishOddsBout(
        fighter_a="A",
        fighter_b="B",
        win_prob_a=0.6,
        win_prob_b=0.4,
        distance_no=-150,  # implies a finish
        distance_yes=120,  # implies going the distance
    )
    result = compute_finish_signals(bout)
    # de-vig No/Yes: implied(-150)=.6, implied(+120)=.454545 -> P(finish)=.568966
    p_finish = 0.568966
    assert result.tier is FinishSignalTier.DISTANCE
    assert result.fighter_a.finish_signal == pytest.approx(p_finish * 0.6, abs=1e-4)
    assert result.fighter_b.finish_signal == pytest.approx(p_finish * 0.4, abs=1e-4)


def test_tier2_round_total_fallback_when_no_mov_or_distance() -> None:
    """No MOV and no distance market → tier 2 = P(Under) × win_prob."""
    bout = FinishOddsBout(
        fighter_a="A",
        fighter_b="B",
        win_prob_a=0.55,
        win_prob_b=0.45,
        round_total_under=-130,  # implies a finish (fewer rounds)
        round_total_over=110,
    )
    result = compute_finish_signals(bout)
    # de-vig Under/Over: implied(-130)=.565217, implied(+110)=.476190 -> .542736
    p_under = 0.542736
    assert result.tier is FinishSignalTier.ROUND_TOTAL
    assert result.fighter_a.finish_signal == pytest.approx(p_under * 0.55, abs=1e-4)
    assert result.fighter_b.finish_signal == pytest.approx(p_under * 0.45, abs=1e-4)


def test_ladder_prefers_mov_then_distance_then_round_total() -> None:
    """Highest available tier wins; partial MOV falls through to a fallback."""
    # MOV present alongside fallbacks -> tier 0 chosen.
    full = FinishOddsBout(
        fighter_a="A",
        fighter_b="B",
        win_prob_a=0.6,
        win_prob_b=0.4,
        mov_a=MethodOfVictoryOdds(ko_tko=-200, submission=1000, decision=450),
        mov_b=MethodOfVictoryOdds(ko_tko=800, submission=1800, decision=1100),
        distance_no=-150,
        distance_yes=120,
        round_total_under=-130,
        round_total_over=110,
    )
    assert compute_finish_signals(full).tier is FinishSignalTier.METHOD_OF_VICTORY

    # Only one fighter's MOV tree present -> bout "lacks the MOV tree" -> tier 1.
    partial_mov = FinishOddsBout(
        fighter_a="A",
        fighter_b="B",
        win_prob_a=0.6,
        win_prob_b=0.4,
        mov_a=MethodOfVictoryOdds(ko_tko=-200, submission=1000, decision=450),
        mov_b=None,
        distance_no=-150,
        distance_yes=120,
        round_total_under=-130,
        round_total_over=110,
    )
    assert compute_finish_signals(partial_mov).tier is FinishSignalTier.DISTANCE

    # Distance present alongside round-total -> tier 1 before tier 2.
    distance_and_total = FinishOddsBout(
        fighter_a="A",
        fighter_b="B",
        win_prob_a=0.6,
        win_prob_b=0.4,
        distance_no=-150,
        distance_yes=120,
        round_total_under=-130,
        round_total_over=110,
    )
    assert compute_finish_signals(distance_and_total).tier is FinishSignalTier.DISTANCE


# ---------------------------------------------------------------------------
# Malformed / missing input -> typed error, never a fabricated signal (§14.1)
# ---------------------------------------------------------------------------


def test_no_market_available_raises() -> None:
    """A bout with neither MOV tree nor a fallback market raises."""
    bout = FinishOddsBout(fighter_a="A", fighter_b="B", win_prob_a=0.6, win_prob_b=0.4)
    with pytest.raises(FinishSignalError):
        compute_finish_signals(bout)


def test_malformed_mov_odds_raises() -> None:
    """A zero price in a selected MOV tree raises the typed error at compute."""
    bout = FinishOddsBout(
        fighter_a="A",
        fighter_b="B",
        win_prob_a=0.6,
        win_prob_b=0.4,
        mov_a=MethodOfVictoryOdds(ko_tko=0, submission=1000, decision=450),
        mov_b=MethodOfVictoryOdds(ko_tko=800, submission=1800, decision=1100),
    )
    with pytest.raises(FinishSignalError):
        compute_finish_signals(bout)


def test_malformed_fallback_odds_raises() -> None:
    """A present-but-zero fallback price selects the tier then raises."""
    bout = FinishOddsBout(
        fighter_a="A",
        fighter_b="B",
        win_prob_a=0.6,
        win_prob_b=0.4,
        distance_no=0,
        distance_yes=120,
    )
    with pytest.raises(FinishSignalError):
        compute_finish_signals(bout)


def test_invalid_win_prob_raises_at_construction() -> None:
    """A win probability outside [0, 1] is a typed error at construction."""
    with pytest.raises(FinishSignalError):
        FinishOddsBout(fighter_a="A", fighter_b="B", win_prob_a=1.5, win_prob_b=0.4)


def test_empty_fighter_name_raises_at_construction() -> None:
    """An empty fighter name is a typed error at construction."""
    with pytest.raises(FinishSignalError):
        FinishOddsBout(fighter_a="", fighter_b="B", win_prob_a=0.6, win_prob_b=0.4)
