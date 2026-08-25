"""Optimizer v1 pool builder (Slice B.2).

Phase B.2 of ``docs/OPTIMIZER_V1_DESIGN.md`` §5.1 / §5.4: a read-only
assembly of the optimizer's input view. Given a slate id, walks
:func:`project_slate`, :class:`FighterRepository`, and
:class:`FightGroupRepository`, and returns an :class:`OptimizerPool`
containing only fighters whose Projection v1 status is ``"ok"``
together with the same-fight pair set those fighters belong to.

Read end to end:

- No INSERT / UPDATE / DELETE on any table.
- No projection recompute, no override mutation, no fight-group write.
- ``effective_status`` is intentionally NOT consulted (design §9
  deferral, mirroring ``ODDS_PERSISTENCE_DESIGN.md`` §15.11 risk #7).

Pool builder, not solver: returning a pool with fewer than six entries
is allowed here. The lineup solver (design §5.2) is the layer that
turns that into a ``status="infeasible_pool_too_small"`` result.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.db.repositories import FightGroupRepository, FighterRepository
from src.projections.slate_projection_service import project_slate
from src.utils.text_cleaning import normalize_name

PROJECTION_STATUS_OK = "ok"


@dataclass(frozen=True)
class OptimizerPoolEntry:
    """One selectable fighter row for the solver (design §5.4).

    ``fight_group_id`` is ``None`` when the fighter's persisted name
    cannot be tied back to any ``fight_groups`` row on the slate via
    ``normalize_name``. The solver still treats the fighter as
    selectable; the same-fight pair set simply contributes no
    exclusion for an unanchored row.
    """

    fighter_id: int
    slate_id: int
    dk_name: str
    dk_salary: int
    default_projection: float
    fight_group_id: int | None


@dataclass(frozen=True)
class ExcludedFighter:
    """A fighter that was considered for the pool but filtered out.

    Surfaced as a diagnostic surface for the optimizer page / future
    audit log. ``reason`` is a short, human-readable string; it is
    not part of a stable taxonomy and must not be parsed by callers.
    """

    fighter_id: int | None
    name: str
    reason: str


@dataclass(frozen=True)
class OptimizerPool:
    """Assembled optimizer pool for one slate (design §5.4).

    ``same_fight_pairs`` contains ``frozenset({fighter_id_a,
    fighter_id_b})`` entries — fighter ids, never names — and includes
    only pairs where *both* sides are eligible entries. Pairs whose
    opposing side is ineligible are dropped so the still-eligible
    side remains individually selectable.
    """

    slate_id: int
    entries: tuple[OptimizerPoolEntry, ...]
    same_fight_pairs: frozenset[frozenset[int]]
    excluded: tuple[ExcludedFighter, ...]


def build_optimizer_pool(
    conn: sqlite3.Connection, slate_id: int
) -> OptimizerPool:
    """Assemble the Optimizer v1 pool for ``slate_id``.

    Behavior per ``docs/OPTIMIZER_V1_DESIGN.md`` §5.1:

    1. ``project_slate(conn, slate_id)`` is the per-fighter projection
       source. Only rows whose ``projection_status == "ok"`` and whose
       ``projected_dk_points`` is numeric are kept.
    2. ``missing_inputs`` / ``non_projectable`` / any other non-ok
       projection row is dropped and recorded in ``excluded`` with a
       short reason string.
    3. Salary is read from ``FighterRepository.list_for_slate``;
       rows with missing or non-positive salary are dropped (defense
       in depth — the salary importer should never produce these).
    4. ``FightGroupRepository.list_for_slate`` is the source for the
       same-fight pair set. Fight-group fighter names are tied back
       to fighter ids using :func:`normalize_name` so the pair set is
       always ``frozenset[int]`` entries, never strings.
    5. A fight pair is emitted only when *both* sides are in the
       eligible pool. If one side is ineligible (non-ok projection,
       missing salary, name didn't resolve to a fighter), the pair
       is dropped and the still-eligible side stays selectable.

    Entry order follows :func:`project_slate`'s deterministic
    emission order so consumers can rely on a stable per-fighter
    sequence without re-sorting.

    Read-only: this function issues no writes.
    """
    sid = int(slate_id)

    projections = project_slate(conn, sid)
    fighter_rows = FighterRepository(conn).list_for_slate(sid)
    fight_groups = FightGroupRepository(conn).list_for_slate(sid)

    salary_by_id: dict[int, int] = {
        int(f.id): int(f.salary) for f in fighter_rows
    }

    name_to_fighter_id: dict[str, int] = {}
    for f in fighter_rows:
        key = normalize_name(f.name)
        if not key:
            continue
        # First fighter wins on a normalized-name collision; the
        # SQL UNIQUE(slate_id, name) constraint makes raw-name
        # collisions impossible, and a normalized-name collision
        # between two distinct DK rows is a data error we don't
        # try to repair here.
        name_to_fighter_id.setdefault(key, int(f.id))

    resolved_groups: list[tuple[int | None, int | None, int]] = []
    fight_group_id_by_fighter: dict[int, int] = {}
    for group in sorted(fight_groups, key=lambda g: int(g.id)):
        a = name_to_fighter_id.get(normalize_name(group.fighter_1_name))
        b = name_to_fighter_id.get(normalize_name(group.fighter_2_name))
        gid = int(group.id)
        resolved_groups.append((a, b, gid))
        if a is not None:
            fight_group_id_by_fighter.setdefault(a, gid)
        if b is not None:
            fight_group_id_by_fighter.setdefault(b, gid)

    entries: list[OptimizerPoolEntry] = []
    excluded: list[ExcludedFighter] = []
    eligible_ids: set[int] = set()

    for proj in projections:
        fid = proj.fighter_id

        if proj.projection_status != PROJECTION_STATUS_OK:
            excluded.append(
                ExcludedFighter(
                    fighter_id=fid,
                    name=proj.fighter_name,
                    reason=_format_projection_reason(proj),
                )
            )
            continue

        if proj.projected_dk_points is None:
            excluded.append(
                ExcludedFighter(
                    fighter_id=fid,
                    name=proj.fighter_name,
                    reason="projected_dk_points is None",
                )
            )
            continue

        if fid is None:
            excluded.append(
                ExcludedFighter(
                    fighter_id=None,
                    name=proj.fighter_name,
                    reason="missing fighter_id",
                )
            )
            continue

        salary = salary_by_id.get(int(fid))
        if salary is None:
            excluded.append(
                ExcludedFighter(
                    fighter_id=fid,
                    name=proj.fighter_name,
                    reason="missing dk_salary",
                )
            )
            continue
        if salary <= 0:
            excluded.append(
                ExcludedFighter(
                    fighter_id=fid,
                    name=proj.fighter_name,
                    reason=f"non-positive dk_salary={salary}",
                )
            )
            continue

        entries.append(
            OptimizerPoolEntry(
                fighter_id=int(fid),
                slate_id=sid,
                dk_name=proj.fighter_name,
                dk_salary=int(salary),
                default_projection=float(proj.projected_dk_points),
                fight_group_id=fight_group_id_by_fighter.get(int(fid)),
            )
        )
        eligible_ids.add(int(fid))

    pairs: set[frozenset[int]] = set()
    for a, b, _gid in resolved_groups:
        if a is None or b is None:
            continue
        if a in eligible_ids and b in eligible_ids:
            pairs.add(frozenset({a, b}))

    return OptimizerPool(
        slate_id=sid,
        entries=tuple(entries),
        same_fight_pairs=frozenset(pairs),
        excluded=tuple(excluded),
    )


def _format_projection_reason(proj) -> str:
    tags = ",".join(proj.missing_inputs) if proj.missing_inputs else ""
    if tags:
        return f"projection_status={proj.projection_status}:{tags}"
    return f"projection_status={proj.projection_status}"
