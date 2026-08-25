"""Validation tests — same-fight conflict checks live in optimizer/validation.py."""

from src.optimizer.validation import has_same_fight_conflict


def test_validation_smoke():
    fights = [(10, 20)]
    assert has_same_fight_conflict([10], fights) is False
    assert has_same_fight_conflict([10, 20], fights) is True
