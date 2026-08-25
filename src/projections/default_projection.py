"""Default UFC projection formula (v0).

default_projection =
    implied_win_probability * 70
    + value_gap_bonus(salary, implied_win_probability)
    + five_round_bonus(scheduled_rounds)
"""

from __future__ import annotations

from src.projections.value_bonus import five_round_bonus, value_gap_bonus

WIN_PROB_WEIGHT = 70.0


def default_projection(
    implied_win_probability: float,
    salary: int | float,
    scheduled_rounds: int,
) -> float:
    p = float(implied_win_probability)
    if not 0.0 <= p <= 1.0:
        raise ValueError("implied_win_probability must be in [0, 1]")
    base = p * WIN_PROB_WEIGHT
    return base + value_gap_bonus(salary, p) + five_round_bonus(scheduled_rounds)
