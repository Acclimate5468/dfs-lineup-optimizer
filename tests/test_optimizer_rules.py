from src.optimizer.validation import has_same_fight_conflict, same_fight_conflicts


def test_no_conflict_when_only_one_side_present():
    fights = [(1, 2), (3, 4), (5, 6)]
    assert has_same_fight_conflict([1, 3, 5], fights) is False
    assert same_fight_conflicts([1, 3, 5], fights) == []


def test_conflict_detected_when_both_sides_present():
    fights = [(1, 2), (3, 4)]
    assert has_same_fight_conflict([1, 2, 5], fights) is True
    assert same_fight_conflicts([1, 2, 5], fights) == [(1, 2)]


def test_multiple_conflicts():
    fights = [(1, 2), (3, 4), (5, 6)]
    conflicts = same_fight_conflicts([1, 2, 3, 4], fights)
    assert set(conflicts) == {(1, 2), (3, 4)}


def test_empty_inputs():
    assert has_same_fight_conflict([], [(1, 2)]) is False
    assert has_same_fight_conflict([1, 2], []) is False
