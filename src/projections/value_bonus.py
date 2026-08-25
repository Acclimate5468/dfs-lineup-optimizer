"""Salary / probability value-gap bonus and five-round bonus."""

from __future__ import annotations


def value_gap_bonus(salary: int | float, implied_win_probability: float) -> float:
    """Reward cheap-but-live fighters.

    +8 if salary <= 7600 and p_win >= 0.45
    +5 if salary <= 8000 and p_win >= 0.48
    +3 if salary <= 8500 and p_win >= 0.55
    0 otherwise

    Tiers are evaluated cheapest-first; the first matching tier wins.
    """
    p = float(implied_win_probability)
    s = float(salary)
    if s <= 7600 and p >= 0.45:
        return 8.0
    if s <= 8000 and p >= 0.48:
        return 5.0
    if s <= 8500 and p >= 0.55:
        return 3.0
    return 0.0


def five_round_bonus(scheduled_rounds: int) -> float:
    """+7 for scheduled five-round fights (main events / title fights), else 0."""
    return 7.0 if int(scheduled_rounds) == 5 else 0.0
