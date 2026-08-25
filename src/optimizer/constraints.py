"""Optimizer constraint definitions for UFC DK Classic. v0 skeleton."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.config.constants import LINEUP_SIZE, SALARY_CAP


@dataclass
class UFCClassicConstraints:
    lineup_size: int = LINEUP_SIZE
    salary_cap: int = SALARY_CAP
    forbid_same_fight: bool = True
    locked_fighter_ids: set[int] = field(default_factory=set)
    excluded_fighter_ids: set[int] = field(default_factory=set)
