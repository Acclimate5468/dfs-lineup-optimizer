"""Projection v1 pure service.

Phase A: wraps the v0 ``default_projection`` math from docs/DEVELOPMENT_NOTES.md §4 in the
value-object output shape described in ``docs/PROJECTION_V1_DESIGN.md`` §4.

Pure-Python, no DB, no Streamlit, no ``effective_status``. Inputs are plain
optional values; missing required inputs are reported via
``projection_status`` and ``missing_inputs`` rather than silently defaulted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.projections.default_projection import default_projection

STATUS_OK = "ok"
STATUS_MISSING_INPUTS = "missing_inputs"
STATUS_NON_PROJECTABLE = "non_projectable"

VALID_SCHEDULED_ROUNDS = (3, 5)


@dataclass(frozen=True)
class FighterProjectionInput:
    """Back-compat input for the original v0 helper. Retained for callers
    that pass fully-resolved values straight to ``default_projection``."""

    name: str
    salary: int
    implied_win_probability: float
    scheduled_rounds: int = 3


def project_fighter(inp: FighterProjectionInput) -> float:
    return default_projection(
        implied_win_probability=inp.implied_win_probability,
        salary=inp.salary,
        scheduled_rounds=inp.scheduled_rounds,
    )


@dataclass(frozen=True)
class ProjectionInputs:
    """Resolved per-fighter inputs for Projection v1.

    Data inputs are optional — ``None`` marks "absent" and is reported via
    ``missing_inputs`` rather than defaulted. Structural flags express
    whether the fighter has the fight-group context required to be
    projectable at all (per design §5).
    """

    fighter_id: int | None = None
    slate_id: int | None = None
    salary: int | None = None
    implied_win_probability: float | None = None
    scheduled_rounds: int | None = None
    has_fight_group: bool = True
    has_opponent: bool = True


@dataclass(frozen=True)
class ProjectionResult:
    """Per-fighter Projection v1 output (design §4)."""

    fighter_id: int | None
    slate_id: int | None
    projected_dk_points: float | None
    projection_status: str
    missing_inputs: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""


def compute_projection_v1(inputs: ProjectionInputs) -> ProjectionResult:
    """Apply the v0 formula to resolved inputs and return a v1 result.

    See ``docs/PROJECTION_V1_DESIGN.md`` §4–§5. Structural problems
    (missing fight group / opponent) dominate data problems and yield
    ``non_projectable``; otherwise any absent required data input yields
    ``missing_inputs``; only fully-resolved inputs yield ``ok``.
    """

    structural_missing: list[str] = []
    if not inputs.has_fight_group:
        structural_missing.append("fight_group")
    if not inputs.has_opponent:
        structural_missing.append("opponent")

    if structural_missing:
        return ProjectionResult(
            fighter_id=inputs.fighter_id,
            slate_id=inputs.slate_id,
            projected_dk_points=None,
            projection_status=STATUS_NON_PROJECTABLE,
            missing_inputs=tuple(structural_missing),
        )

    data_missing: list[str] = []
    if inputs.salary is None:
        data_missing.append("salary")
    if inputs.implied_win_probability is None:
        data_missing.append("win_probability")
    if inputs.scheduled_rounds is None or int(inputs.scheduled_rounds) not in VALID_SCHEDULED_ROUNDS:
        data_missing.append("scheduled_rounds")

    if data_missing:
        return ProjectionResult(
            fighter_id=inputs.fighter_id,
            slate_id=inputs.slate_id,
            projected_dk_points=None,
            projection_status=STATUS_MISSING_INPUTS,
            missing_inputs=tuple(data_missing),
        )

    points = default_projection(
        implied_win_probability=inputs.implied_win_probability,
        salary=inputs.salary,
        scheduled_rounds=inputs.scheduled_rounds,
    )
    return ProjectionResult(
        fighter_id=inputs.fighter_id,
        slate_id=inputs.slate_id,
        projected_dk_points=points,
        projection_status=STATUS_OK,
        missing_inputs=(),
    )
