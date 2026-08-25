"""UFC DraftKings Classic optimizer.

v0 SKELETON ONLY. The full PuLP-based ILP implementation will land in a later
milestone. Do not claim this is complete.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.optimizer.constraints import UFCClassicConstraints


@dataclass
class FighterPool:
    """Minimal fighter view consumed by the optimizer."""
    id: int
    name: str
    salary: int
    projection: float


def optimize(
    pool: list[FighterPool],  # noqa: ARG001 - skeleton
    fights: list[tuple[int, int]],  # noqa: ARG001 - skeleton
    constraints: UFCClassicConstraints | None = None,  # noqa: ARG001 - skeleton
) -> list[list[int]]:
    """Return optimal lineup(s) as lists of fighter ids.

    Not implemented in v0.
    """
    raise NotImplementedError("UFC Classic optimizer is a v0 skeleton")
