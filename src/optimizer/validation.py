"""Lineup validation helpers.

v0 implements the core same-fight pair check. Full lineup validation lives
in a later milestone.
"""

from __future__ import annotations

from collections.abc import Iterable


def has_same_fight_conflict(
    fighter_ids: Iterable[int],
    fights: Iterable[tuple[int, int]],
) -> bool:
    """Return True if any pair of (fighter_a_id, fighter_b_id) in `fights`
    is fully contained in `fighter_ids` — i.e. both opponents are in the lineup.
    """
    selected = set(fighter_ids)
    for a, b in fights:
        if a in selected and b in selected:
            return True
    return False


def same_fight_conflicts(
    fighter_ids: Iterable[int],
    fights: Iterable[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Return all (a, b) pairs from `fights` where both fighters are in the lineup."""
    selected = set(fighter_ids)
    return [(a, b) for a, b in fights if a in selected and b in selected]
