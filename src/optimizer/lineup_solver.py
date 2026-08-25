"""Optimizer v1 lineup solver (Slice B.3).

Phase B.3 of ``docs/OPTIMIZER_V1_DESIGN.md`` §5.2 / §5.4: a pure-Python
PuLP integer program that turns an :class:`OptimizerPool` (Slice B.2)
into up to five DK UFC Classic lineups.

Pure function, by design:

- No DB access, no I/O, no service calls.
- The pool is consumed read-only; ``solve_lineups`` does not mutate it.
- All status strings and value-object shapes are part of the v1 contract
  (design §5.4); tests pin them.

The :class:`UFCClassicConstraints` ``lineup_size`` (6) and ``salary_cap``
(50_000) defaults match the DK UFC Classic contest format
(``src/config/constants.py``). The solver does **not** consume
``forbid_same_fight`` (always enforced per design §2 / §5.2),
``locked_fighter_ids``, or ``excluded_fighter_ids`` (out of scope per
design §2 / §11). Those fields are reserved for future slices.
"""

from __future__ import annotations

from dataclasses import dataclass

import pulp

from src.optimizer.constraints import UFCClassicConstraints
from src.optimizer.pool_builder import OptimizerPool

STATUS_OK = "ok"
STATUS_OK_PARTIAL = "ok_partial"
STATUS_INFEASIBLE_POOL_TOO_SMALL = "infeasible_pool_too_small"
STATUS_INFEASIBLE_CONSTRAINTS = "infeasible_constraints"

MIN_N_LINEUPS = 1
MAX_N_LINEUPS = 5


@dataclass(frozen=True)
class Lineup:
    """A single DK UFC Classic lineup (design §5.4).

    ``fighter_ids`` is always sorted ascending so a lineup is uniquely
    represented and easy to diff against another.
    """

    fighter_ids: tuple[int, ...]
    total_salary: int
    total_projection: float


@dataclass(frozen=True)
class SolveResult:
    """Result of one ``solve_lineups`` call (design §5.4).

    ``status`` is one of ``ok``, ``ok_partial``,
    ``infeasible_pool_too_small``, ``infeasible_constraints``. The
    ``gate_blocked`` status from design §5.4 is produced by the
    optimizer service layer (Slice B.4), not here.

    ``reason`` is human-readable diagnostic text and is empty when
    ``status == "ok"``.
    """

    slate_id: int
    status: str
    lineups: tuple[Lineup, ...]
    reason: str


def solve_lineups(
    pool: OptimizerPool,
    *,
    constraints: UFCClassicConstraints | None = None,
    n_lineups: int = 1,
) -> SolveResult:
    """Solve up to ``n_lineups`` DK UFC Classic lineups for ``pool``.

    Behavior per ``docs/OPTIMIZER_V1_DESIGN.md`` §5.2:

    1. ``n_lineups`` must be in ``[1, 5]``; anything else raises
       :class:`ValueError` (programmer error — the UI never produces a
       value outside this range).
    2. If the pool has fewer than ``constraints.lineup_size`` eligible
       entries, return ``status="infeasible_pool_too_small"`` immediately
       with the actual pool size in ``reason``.
    3. Build a PuLP binary program: maximize total projection subject to
       exactly ``lineup_size`` fighters, total salary ≤ ``salary_cap``,
       and at most one fighter per same-fight pair.
    4. For lineups 2..N, add a diversity cut forbidding the exact
       previously solved lineup (``sum(x_i for i in last) <= size - 1``).
       Stop early with ``status="ok_partial"`` if a later solve becomes
       infeasible.
    5. Return ``status="ok"`` when all N solves succeed; if the *first*
       solve is infeasible, return ``status="infeasible_constraints"``.

    Determinism:

    - Pool entries are re-sorted by ``fighter_id`` before the variables
      are created, so PuLP's variable ordering is independent of caller
      order.
    - The objective adds a tiny ``-epsilon * fighter_id`` term per
      variable so ties between equally-projecting lineups resolve toward
      the lower summed fighter id. ``epsilon`` is normalized to
      ``1e-4 / max_fid`` so the per-lineup tiebreak impact is bounded at
      ~6e-4 and cannot override real projection gaps (real DK
      projections are well above that).
    - CBC is invoked single-threaded for reproducibility.

    Post-solve sanity (design §5.4): each returned lineup is asserted to
    have exactly ``lineup_size`` fighters, a total salary at or below
    ``salary_cap``, and no same-fight conflict. Totals on the returned
    :class:`Lineup` are recomputed from the selected fighters, not read
    from the solver.

    Pure: no writes, no DB connection, no global mutation. The input
    ``pool`` is not touched.
    """
    if not isinstance(n_lineups, int) or isinstance(n_lineups, bool):
        raise ValueError(
            f"n_lineups must be an int in [{MIN_N_LINEUPS}, {MAX_N_LINEUPS}], "
            f"got {n_lineups!r}"
        )
    if n_lineups < MIN_N_LINEUPS or n_lineups > MAX_N_LINEUPS:
        raise ValueError(
            f"n_lineups must be in [{MIN_N_LINEUPS}, {MAX_N_LINEUPS}], "
            f"got {n_lineups}"
        )

    cons = constraints if constraints is not None else UFCClassicConstraints()
    lineup_size = int(cons.lineup_size)
    salary_cap = int(cons.salary_cap)

    pool_size = len(pool.entries)
    if pool_size < lineup_size:
        return SolveResult(
            slate_id=pool.slate_id,
            status=STATUS_INFEASIBLE_POOL_TOO_SMALL,
            lineups=(),
            reason=(
                f"pool has {pool_size} eligible fighter(s); "
                f"need at least {lineup_size}"
            ),
        )

    entries = sorted(pool.entries, key=lambda e: e.fighter_id)
    n = len(entries)
    fids = [int(e.fighter_id) for e in entries]
    salaries = [int(e.dk_salary) for e in entries]
    projections = [float(e.default_projection) for e in entries]

    id_to_idx = {fid: i for i, fid in enumerate(fids)}
    pair_indices: list[tuple[int, int]] = []
    for pair in pool.same_fight_pairs:
        members = [fid for fid in pair if fid in id_to_idx]
        if len(members) == 2:
            i, j = id_to_idx[members[0]], id_to_idx[members[1]]
            pair_indices.append((i, j) if i < j else (j, i))
    pair_indices.sort()

    max_fid = max(fids) if fids else 1
    epsilon = 1e-4 / max(1, max_fid)

    lineups: list[Lineup] = []
    seen_index_sets: list[frozenset[int]] = []
    final_status = STATUS_OK
    final_reason = ""

    for k in range(n_lineups):
        prob = pulp.LpProblem(f"ufc_dk_classic_lineup_{k}", pulp.LpMaximize)
        x = [pulp.LpVariable(f"x_{fid}", cat=pulp.LpBinary) for fid in fids]
        prob += pulp.lpSum(
            (projections[i] - epsilon * fids[i]) * x[i] for i in range(n)
        )
        prob += pulp.lpSum(x[i] for i in range(n)) == lineup_size
        prob += (
            pulp.lpSum(salaries[i] * x[i] for i in range(n)) <= salary_cap
        )
        for i, j in pair_indices:
            prob += x[i] + x[j] <= 1
        for prev in seen_index_sets:
            prob += pulp.lpSum(x[i] for i in prev) <= lineup_size - 1

        prob.solve(pulp.PULP_CBC_CMD(msg=False, threads=1))

        if pulp.LpStatus[prob.status] != "Optimal":
            if k == 0:
                return SolveResult(
                    slate_id=pool.slate_id,
                    status=STATUS_INFEASIBLE_CONSTRAINTS,
                    lineups=(),
                    reason=(
                        f"no feasible {lineup_size}-fighter lineup under "
                        f"salary cap {salary_cap} with "
                        f"{len(pair_indices)} same-fight pair(s) "
                        f"and pool size {n}"
                    ),
                )
            final_status = STATUS_OK_PARTIAL
            final_reason = (
                f"only {k} of {n_lineups} requested lineups were feasible; "
                f"diversity cuts exhausted the search space"
            )
            break

        chosen_idx = [
            i for i in range(n) if (pulp.value(x[i]) or 0.0) > 0.5
        ]
        chosen_ids = sorted(fids[i] for i in chosen_idx)
        total_salary = sum(salaries[i] for i in chosen_idx)
        total_projection = sum(projections[i] for i in chosen_idx)

        # Post-solve sanity (design §5.4). Each holds by construction
        # given Optimal status above; these are belt-and-braces so a
        # silent solver regression cannot ship a bad lineup.
        assert len(chosen_ids) == lineup_size, (
            f"solver returned {len(chosen_ids)} fighters, expected "
            f"{lineup_size}"
        )
        assert total_salary <= salary_cap, (
            f"selected lineup salary {total_salary} exceeds cap "
            f"{salary_cap}"
        )
        chosen_set = set(chosen_idx)
        for i, j in pair_indices:
            assert not (i in chosen_set and j in chosen_set), (
                f"same-fight conflict on pair ({fids[i]}, {fids[j]})"
            )

        lineups.append(
            Lineup(
                fighter_ids=tuple(chosen_ids),
                total_salary=int(total_salary),
                total_projection=float(total_projection),
            )
        )
        seen_index_sets.append(frozenset(chosen_idx))

    return SolveResult(
        slate_id=pool.slate_id,
        status=final_status,
        lineups=tuple(lineups),
        reason=final_reason,
    )
