"""Slate-level read-only projection service (selectable engine).

Phase C of ``docs/PROJECTION_V1_DESIGN.md`` (the v0/v1 scalar engine) **and**
Phase C of ``docs/PROJECTION_V2_METHOD_AWARE_DESIGN.md`` §13 / §15 (the
finish-aware v2 engine). Given a ``slate_id`` it returns one output row per
active fighter, computed on read.

Two selectable engines, **one mode per run** (v2 design §10 homogeneous-pool
invariant):

- ``projection_mode="v0_formula"`` (**DEFAULT**) — composes Phase B
  (``aggregate_projection_inputs``) with the v0 formula engine
  (``compute_projection_v1``). v0 remains the default engine (``docs/DEVELOPMENT_NOTES.md`` §4,
  v2 design §10), so every existing caller (optimizer, alerts, exports, manual
  review, the projections page) keeps getting it byte-for-byte unchanged
  because the parameter defaults to v0. **Phase C adds no optimizer behavior
  change.**
- ``projection_mode="v2_finish"`` — **EXPERIMENTAL** finish-aware engine
  (``compute_finish_projection``, v2 design §6), Tier 0. It is **not** validated
  to beat v0 and is **not** promoted: the §12 / Phase A′ calibration gate
  decides that and promotion stays a separate, explicit user decision. It is
  emitted only when a caller opts in (Phase D wires the UI toggle); nothing
  selects it by default.

Read-only end to end in **both** modes, per ``PROJECTION_V1_DESIGN.md`` §8/§9
and v2 design §13:

- No INSERT / UPDATE / DELETE on any table; no projection persistence
  (computed-on-read).
- No alerts / optimizer / exports / UI side effects. The optimizer is an
  unchanged scalar consumer of the DEFAULT v0 path (v2 design §13).
- Win-probability eligibility is decided in Phase B by ``effective_status``
  (Phase D.5.2; ``ODDS_PERSISTENCE_DESIGN.md`` §16.9). Phase C composes Phase
  B's bundles unchanged in both modes; it adds no status logic of its own
  beyond mapping the selected engine's result.

Ordering follows Phase B's deterministic emission order in both modes so
consumers can rely on a stable per-fighter sequence without re-sorting.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.projections.finish_model import (
    PROJECTION_MODE as PROJECTION_MODE_V2,
    OutcomeBranch,
    compute_finish_projection,
)
from src.projections.projection_input_service import (
    ProjectionInputBundle,
    aggregate_projection_inputs,
)
from src.projections.projection_service import (
    STATUS_NON_PROJECTABLE,
    compute_projection_v1,
)

# Mode tags (v2 design §8). v0 is the default; exactly one mode per run (§10).
PROJECTION_MODE_V0 = "v0_formula"
SUPPORTED_PROJECTION_MODES = (PROJECTION_MODE_V0, PROJECTION_MODE_V2)


@dataclass(frozen=True)
class FighterSlateProjection:
    """Per-fighter projection row for a slate-level run.

    ``notes`` preserves Phase B aggregation diagnostics first, followed by any
    engine notes (the v0 ``ProjectionResult.notes`` string, or the v2 clamp
    ``warnings``), so a UI can render *why* a projection is in its current state
    without re-querying.

    The finish-aware (v2) fields are **additive** and default to v0/empty so
    v0-mode rows and every existing consumer are unchanged (v2 design §8):

    - ``projection_mode`` — ``"v0_formula"`` or ``"v2_finish"`` so no consumer
      silently mixes engines.
    - ``outcome_branches`` — the four finish/decision × win/loss branches
      (populated in v2 mode only; empty in v0 mode).
    - ``best_branch_pts`` / ``worst_branch_pts`` — branch *conditional means*,
      NOT calibrated percentiles (v2 design §6.3); ``None`` in v0 mode.
    - ``p_fight_finishes`` / ``finish_share`` — the resolved Tier-0 finish
      signal echoed back for transparency; ``None`` in v0 mode.
    """

    fighter_id: int | None
    slate_id: int | None
    fighter_name: str
    projected_dk_points: float | None
    projection_status: str
    missing_inputs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    projection_mode: str = PROJECTION_MODE_V0
    outcome_branches: tuple[OutcomeBranch, ...] = ()
    best_branch_pts: float | None = None
    worst_branch_pts: float | None = None
    p_fight_finishes: float | None = None
    finish_share: float | None = None


def project_slate(
    conn: sqlite3.Connection,
    slate_id: int,
    projection_mode: str = PROJECTION_MODE_V0,
) -> list[FighterSlateProjection]:
    """Return one projection row per active fighter on ``slate_id``.

    ``projection_mode`` selects the engine and **defaults to the v0 formula**,
    so callers that do not pass it (optimizer, alerts, exports, manual review,
    the projections page) get the v0 engine exactly as before — Phase C adds no
    behavior change for them. ``"v2_finish"`` selects the experimental
    finish-aware engine (v2 design §13). Exactly one mode is emitted per run
    (§10 homogeneous-pool invariant); an unsupported mode raises ``ValueError``.

    Aggregates per-fighter inputs via ``aggregate_projection_inputs`` (Phase B),
    runs each bundle through the selected engine, and returns the composed
    results in Phase B's deterministic order. Inactive fighters are omitted by
    Phase B. Unknown ``slate_id`` → empty list, mirroring Phase B.
    """
    if projection_mode not in SUPPORTED_PROJECTION_MODES:
        raise ValueError(
            f"unsupported projection_mode: {projection_mode!r} "
            f"(expected one of {SUPPORTED_PROJECTION_MODES})"
        )

    bundles = aggregate_projection_inputs(conn, slate_id)
    if projection_mode == PROJECTION_MODE_V2:
        return [_finish_projection_row(bundle) for bundle in bundles]
    return [_v0_projection_row(bundle) for bundle in bundles]


def _v0_projection_row(
    bundle: ProjectionInputBundle,
) -> FighterSlateProjection:
    """Compose Phase B's bundle with the v0 formula engine (unchanged path)."""
    result = compute_projection_v1(bundle.inputs)
    combined_notes: tuple[str, ...] = bundle.notes
    if result.notes:
        combined_notes = combined_notes + (result.notes,)
    return FighterSlateProjection(
        fighter_id=result.fighter_id,
        slate_id=result.slate_id,
        fighter_name=bundle.fighter_name,
        projected_dk_points=result.projected_dk_points,
        projection_status=result.projection_status,
        missing_inputs=result.missing_inputs,
        notes=combined_notes,
        projection_mode=PROJECTION_MODE_V0,
    )


def _finish_projection_row(
    bundle: ProjectionInputBundle,
) -> FighterSlateProjection:
    """Compose Phase B's bundle with the experimental finish-aware (v2) engine.

    The structural ``non_projectable`` precedence (missing fight group /
    opponent) carries over from v1 unchanged (v2 design §9). Otherwise the
    resolved win probability + scheduled rounds flow into
    ``compute_finish_projection`` at Tier 0, where the league finish constant is
    always present — so there is no missing-finish-signal state (v2 design §9).

    Salary is **not** required by v2: its mean is salary-independent (v2 design
    §10), delegating value/leverage to the optimizer's salary-cap constraint
    rather than baking a salary heuristic into the points number. (In practice
    ``salary`` is always present on an aggregated bundle, so this never diverges
    from the v0 projectable set in real data.)
    """
    inputs = bundle.inputs

    structural_missing: list[str] = []
    if not inputs.has_fight_group:
        structural_missing.append("fight_group")
    if not inputs.has_opponent:
        structural_missing.append("opponent")
    if structural_missing:
        return FighterSlateProjection(
            fighter_id=inputs.fighter_id,
            slate_id=inputs.slate_id,
            fighter_name=bundle.fighter_name,
            projected_dk_points=None,
            projection_status=STATUS_NON_PROJECTABLE,
            missing_inputs=tuple(structural_missing),
            notes=bundle.notes,
            projection_mode=PROJECTION_MODE_V2,
        )

    finish = compute_finish_projection(
        p_win=inputs.implied_win_probability,
        scheduled_rounds=inputs.scheduled_rounds,
    )
    combined_notes: tuple[str, ...] = bundle.notes + tuple(finish.warnings)
    return FighterSlateProjection(
        fighter_id=inputs.fighter_id,
        slate_id=inputs.slate_id,
        fighter_name=bundle.fighter_name,
        projected_dk_points=finish.projected_dk_points,
        projection_status=finish.projection_status,
        missing_inputs=finish.missing_inputs,
        notes=combined_notes,
        projection_mode=PROJECTION_MODE_V2,
        outcome_branches=finish.outcome_branches,
        best_branch_pts=finish.best_branch_pts,
        worst_branch_pts=finish.worst_branch_pts,
        p_fight_finishes=finish.p_fight_finishes,
        finish_share=finish.finish_share,
    )
