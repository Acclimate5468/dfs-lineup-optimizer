"""American odds <-> implied probability, plus no-vig two-sided conversion."""

from __future__ import annotations


def american_to_implied_probability(american_odds: int | float) -> float:
    """Convert American moneyline odds to raw implied probability in [0, 1].

    Examples:
        +150 -> 100 / (150 + 100) = 0.40
        -200 -> 200 / (200 + 100) = 0.6667
    """
    if american_odds is None:
        raise ValueError("american_odds is required")
    if american_odds == 0:
        raise ValueError("american_odds cannot be 0")

    odds = float(american_odds)
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return (-odds) / ((-odds) + 100.0)


def no_vig_two_way(prob_a: float, prob_b: float) -> tuple[float, float]:
    """Remove the vig from two implied probabilities that should sum to ~1.

    Returns the fair (no-vig) probabilities, normalized to sum to 1.
    """
    if prob_a < 0 or prob_b < 0:
        raise ValueError("probabilities must be non-negative")
    total = prob_a + prob_b
    if total <= 0:
        raise ValueError("probabilities must sum to a positive number")
    return prob_a / total, prob_b / total


def american_pair_to_no_vig(
    american_a: int | float,
    american_b: int | float,
) -> tuple[float, float]:
    """Convenience: two-sided American odds -> no-vig (p_a, p_b)."""
    p_a = american_to_implied_probability(american_a)
    p_b = american_to_implied_probability(american_b)
    return no_vig_two_way(p_a, p_b)
