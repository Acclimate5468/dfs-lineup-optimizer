import math

import pytest

from src.projections.implied_probability import (
    american_pair_to_no_vig,
    american_to_implied_probability,
    no_vig_two_way,
)


def test_positive_odds():
    # +150 -> 100 / 250 = 0.40
    assert math.isclose(american_to_implied_probability(150), 0.40, abs_tol=1e-9)


def test_negative_odds():
    # -200 -> 200 / 300 = 0.6667
    assert math.isclose(american_to_implied_probability(-200), 2 / 3, abs_tol=1e-9)


def test_even_money_positive_and_negative():
    assert math.isclose(american_to_implied_probability(100), 0.5, abs_tol=1e-9)
    assert math.isclose(american_to_implied_probability(-100), 0.5, abs_tol=1e-9)


def test_zero_raises():
    with pytest.raises(ValueError):
        american_to_implied_probability(0)


def test_no_vig_normalizes_to_one():
    p_a, p_b = no_vig_two_way(0.55, 0.50)
    assert math.isclose(p_a + p_b, 1.0, abs_tol=1e-9)
    assert p_a > p_b  # favorite stays the favorite


def test_no_vig_pair_from_american():
    p_a, p_b = american_pair_to_no_vig(-200, +170)
    assert math.isclose(p_a + p_b, 1.0, abs_tol=1e-9)
    assert p_a > p_b
