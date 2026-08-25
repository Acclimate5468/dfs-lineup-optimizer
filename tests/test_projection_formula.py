import math

import pytest

from src.projections.default_projection import default_projection
from src.projections.value_bonus import five_round_bonus, value_gap_bonus


def test_value_gap_bonus_tiers():
    assert value_gap_bonus(7500, 0.50) == 8.0
    assert value_gap_bonus(7600, 0.45) == 8.0
    # cheapest tier wins even if higher tier also qualifies
    assert value_gap_bonus(7600, 0.60) == 8.0
    # 7601 misses the 7600 tier, hits 8000/0.48
    assert value_gap_bonus(7800, 0.49) == 5.0
    assert value_gap_bonus(8500, 0.55) == 3.0
    # below threshold -> 0
    assert value_gap_bonus(8500, 0.40) == 0.0
    assert value_gap_bonus(9000, 0.70) == 0.0


def test_five_round_bonus():
    assert five_round_bonus(5) == 7.0
    assert five_round_bonus(3) == 0.0
    assert five_round_bonus(1) == 0.0


def test_default_projection_three_round_no_bonuses():
    # p=0.5, salary=9000, 3 rounds -> 0.5*70 + 0 + 0 = 35
    assert math.isclose(default_projection(0.5, 9000, 3), 35.0, abs_tol=1e-9)


def test_default_projection_five_round_with_value_bonus():
    # p=0.50, salary=7500 -> 0.5*70 + 8 (value) + 7 (5rd) = 50
    assert math.isclose(default_projection(0.50, 7500, 5), 50.0, abs_tol=1e-9)


def test_default_projection_rejects_out_of_range_probability():
    with pytest.raises(ValueError):
        default_projection(1.5, 8000, 3)
    with pytest.raises(ValueError):
        default_projection(-0.01, 8000, 3)
