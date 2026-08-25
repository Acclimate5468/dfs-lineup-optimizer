"""Read-only reasoning-context assembler (Build B6).

Realizes ``docs/TWO_STEP_BUILDER_PRODUCTION_DESIGN.md`` §8.1 (the B6
deliverable) and §9.2 item 3: the read-only bridge between the optimizer
/ export bundle and the pure :func:`build_lineup_reasoning` generator.

It composes an :class:`~src.exports.internal_export.InternalExportBundle`
(from :func:`~src.exports.export_service.build_run_log`) with two existing
read services — :func:`~src.projections.projection_input_service.aggregate_projection_inputs`
and :func:`~src.projections.slate_projection_service.project_slate` — plus
the Manual Review gate readout, and packs the already-computed facts into a
:class:`~src.exports.lineup_reasoning.ReasoningContext`.

Contract (design §8.1 / §7.6 / §7.7):

- **Read-only end to end.** No INSERT / UPDATE / DELETE, no projection
  recompute, no override mutation, no ``effective_status`` consumption
  (``ODDS_PERSISTENCE_DESIGN.md`` §15.11 risk #7). Every input read is one
  the Build page already performs on an explicit Build click.
- **No new math.** Per-fighter implied win probability, salary, projection,
  scheduled rounds and fight group are carried through verbatim from the
  services above; the value-gap / five-round bonuses are re-derived only via
  the pure formula helpers in :mod:`src.projections.value_bonus`
  (``docs/DEVELOPMENT_NOTES.md`` §4 — the same functions the projection uses), and only when
  their inputs are present. Nothing is inferred, predicted, or back-filled.
- **No fabrication.** A fact the services did not supply is left ``None`` so
  the pure generator omits any claim about it (design §8.2 — closed list;
  §8.3 — hard guardrails). A bundle fighter with no matching projection row
  (should not happen for a solved lineup) simply carries no optional facts.

The per-fighter join uses the persisted display name. The bundle, the
projection-input aggregator and ``project_slate`` all derive that name from
the same ``FighterRepository`` rows, so an exact-string join is consistent;
the first occurrence of a name wins if a slate ever carried a duplicate.
"""

from __future__ import annotations

import sqlite3

from src.config.constants import LINEUP_SIZE, SALARY_CAP
from src.exports.internal_export import InternalExportBundle
from src.exports.lineup_reasoning import (
    ExcludedFighterNote,
    FighterReasoningInput,
    LineupReasoningInput,
    ReasoningContext,
    WarningNote,
)
from src.projections.projection_input_service import (
    aggregate_projection_inputs,
)
from src.projections.projection_service import ProjectionInputs
from src.projections.slate_projection_service import project_slate
from src.projections.value_bonus import five_round_bonus, value_gap_bonus
from src.slate.manual_review import (
    CATEGORY_BLOCKING,
    CATEGORY_WARNING,
    STATUS_FAIL,
)
from src.slate.manual_review_service import evaluate_manual_review

# Gate categories whose *failing* checks are surfaced as active review flags
# (design §8.2 — "active Warning / Blocking flags from the gate").
_ACTIVE_FLAG_CATEGORIES = frozenset({CATEGORY_BLOCKING, CATEGORY_WARNING})


def _fighter_inputs_by_name(
    conn: sqlite3.Connection, slate_id: int
) -> dict[str, ProjectionInputs]:
    """Map persisted fighter name → resolved Projection v1 inputs.

    Reuses :func:`aggregate_projection_inputs` (Phase B) so implied win
    probability and scheduled rounds come from the existing read model,
    never recomputed here.
    """
    by_name: dict[str, ProjectionInputs] = {}
    for bundle in aggregate_projection_inputs(conn, slate_id):
        by_name.setdefault(bundle.fighter_name, bundle.inputs)
    return by_name


def _projection_status_by_name(
    conn: sqlite3.Connection, slate_id: int
) -> dict[str, str]:
    """Map persisted fighter name → Projection v1 status string."""
    by_name: dict[str, str] = {}
    for row in project_slate(conn, slate_id):
        by_name.setdefault(row.fighter_name, row.projection_status)
    return by_name


def _fighter_reasoning_input(
    fighter,
    *,
    inputs: ProjectionInputs | None,
    projection_status: str | None,
) -> FighterReasoningInput:
    """Build one :class:`FighterReasoningInput` from a bundle fighter.

    Optional facts default to ``None``; the value-gap and five-round
    bonuses are re-derived via the pure formula helpers (``docs/DEVELOPMENT_NOTES.md`` §4)
    only when their required inputs are present, so nothing is invented for
    a fighter whose odds / scheduled-rounds inputs are missing.
    """
    implied = inputs.implied_win_probability if inputs is not None else None
    scheduled = inputs.scheduled_rounds if inputs is not None else None

    value_bonus_pts: float | None = None
    if implied is not None:
        value_bonus_pts = value_gap_bonus(fighter.dk_salary, implied)

    five_round_pts: float | None = None
    if scheduled is not None:
        five_round_pts = five_round_bonus(scheduled)

    return FighterReasoningInput(
        name=fighter.fighter_name,
        salary=int(fighter.dk_salary),
        projection=float(fighter.default_projection),
        implied_win_probability=implied,
        value_gap_bonus=value_bonus_pts,
        five_round_bonus=five_round_pts,
        scheduled_rounds=scheduled,
        fight_group_id=fighter.fight_group_id,
        projection_status=projection_status,
    )


def _excluded_notes(
    bundle: InternalExportBundle,
) -> tuple[ExcludedFighterNote, ...]:
    """Carry the export diagnostics' excluded-fighter rows through verbatim."""
    diag = bundle.diagnostics
    if diag is None:
        return ()
    return tuple(
        ExcludedFighterNote(name=e.name, reason=e.reason)
        for e in diag.excluded
    )


def _warning_notes(
    conn: sqlite3.Connection, slate_id: int
) -> tuple[WarningNote, ...]:
    """Surface the gate's active Warning / Blocking flags (design §8.2).

    A flag is "active" when its check is in a Blocking or Warning category
    and its status is ``fail``. The gate's own deterministic ordering
    (``sort_results``) is preserved; the gate owns the wording, not the
    generator.
    """
    readiness = evaluate_manual_review(conn, slate_id)
    return tuple(
        WarningNote(code=check.code, message=check.message)
        for check in readiness.checks
        if check.category in _ACTIVE_FLAG_CATEGORIES
        and check.status == STATUS_FAIL
    )


def assemble_reasoning_context(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    bundle: InternalExportBundle,
) -> ReasoningContext:
    """Assemble a :class:`ReasoningContext` for ``bundle`` (design §8.1, B6).

    ``bundle`` is the :class:`InternalExportBundle` returned by
    :func:`build_run_log` for the same ``slate_id``. The returned context
    feeds :func:`build_lineup_reasoning` directly; this function performs
    only reads and contains no display logic.

    An empty ``bundle.lineups`` (gate-blocked / infeasible run) yields a
    context whose ``lineups`` is empty but whose exclusions and active gate
    flags are still populated, so the generator can emit its safe
    diagnostics-only explanation without losing context.
    """
    sid = int(slate_id)
    inputs_by_name = _fighter_inputs_by_name(conn, sid)
    status_by_name = _projection_status_by_name(conn, sid)

    lineups: list[LineupReasoningInput] = []
    for lineup in bundle.lineups:
        fighters = tuple(
            _fighter_reasoning_input(
                f,
                inputs=inputs_by_name.get(f.fighter_name),
                projection_status=status_by_name.get(f.fighter_name),
            )
            for f in lineup.fighters
        )
        lineups.append(
            LineupReasoningInput(
                lineup_index=int(lineup.lineup_index),
                fighters=fighters,
                total_salary=int(lineup.total_salary),
                total_projection=float(lineup.total_projection),
            )
        )

    return ReasoningContext(
        lineups=tuple(lineups),
        salary_cap=SALARY_CAP,
        roster_size=LINEUP_SIZE,
        excluded=_excluded_notes(bundle),
        warnings=_warning_notes(conn, sid),
    )
