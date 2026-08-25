"""Optimizer v1 service orchestration (Slice B.4).

Phase B.4 of ``docs/OPTIMIZER_V1_DESIGN.md`` §5.3. Thin orchestration
layer the Streamlit page (Slice B.5) will call into. One public
function, :func:`run_optimizer`, that composes:

1. ``evaluate_manual_review`` (the Manual Review Gate readout).
   Re-read on every call per design §4 — no cross-call caching.
   If ``summary.ready`` is False, raise :class:`ManualReviewGateError`
   immediately, *before* the pool is built or the solver is invoked.
2. ``build_optimizer_pool`` (Slice B.2 pool builder).
3. ``solve_lineups`` (Slice B.3 solver). ``n_lineups`` bounds and the
   pool-size precondition both live in the solver; the service does
   not duplicate them.

Read-only end to end. The service issues no INSERT / UPDATE / DELETE
on any table, never recomputes projections, never mutates the gate's
``manual_review_status`` column, and never consults
``odds_match_results.effective_status`` directly (design §9 /
``ODDS_PERSISTENCE_DESIGN.md`` §15.11 risk #7). It trusts upstream
signals via the Manual Review Gate and Projection v1.

Out of scope per design §5.3 / §10 / §11: per-run audit row insert,
DK upload CSV export, last-run cache, history. Each of those needs
its own design pass and is explicitly deferred.
"""

from __future__ import annotations

import sqlite3

from src.optimizer.constraints import UFCClassicConstraints
from src.optimizer.lineup_solver import SolveResult, solve_lineups
from src.optimizer.pool_builder import build_optimizer_pool
from src.slate.manual_review_service import (
    ReviewReadiness,
    evaluate_manual_review,
)


class ManualReviewGateError(RuntimeError):
    """Raised by :func:`run_optimizer` when the Manual Review Gate is
    not green for the requested slate (design §4).

    Carries the :class:`ReviewReadiness` snapshot from the gate so
    callers (Slice B.5 Streamlit page) can render the failing
    Blocking checks without re-running ``evaluate_manual_review``.
    """

    def __init__(
        self, readiness: ReviewReadiness, message: str | None = None
    ) -> None:
        self.readiness = readiness
        self.slate_id = readiness.slate_id
        msg = message or (
            f"Manual Review Gate is not green for slate #{readiness.slate_id}; "
            f"{readiness.summary.blocking_count} Blocking check(s) — resolve "
            "them on the Manual Review page before solving."
        )
        super().__init__(msg)


def run_optimizer(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    n_lineups: int = 1,
    constraints: UFCClassicConstraints | None = None,
) -> SolveResult:
    """Build and solve up to ``n_lineups`` DK UFC Classic lineups.

    Behavior per ``docs/OPTIMIZER_V1_DESIGN.md`` §5.3:

    1. Call :func:`evaluate_manual_review` and inspect
       ``summary.ready``. If False, raise :class:`ManualReviewGateError`
       carrying the readiness snapshot. The pool builder and solver
       are not invoked.
    2. Build the pool via :func:`build_optimizer_pool`.
    3. Solve via :func:`solve_lineups`, passing the supplied
       ``constraints`` (or :class:`UFCClassicConstraints` defaults)
       and ``n_lineups``. ``n_lineups`` bounds validation
       (``[1, 5]``) and the pool-size precondition
       (``infeasible_pool_too_small``) both live in the solver and
       are deliberately not duplicated here.

    Read-only: no writes, no recompute, no override mutation.
    ``effective_status`` is not consulted (design §9).
    """
    readiness = evaluate_manual_review(conn, slate_id)
    if not readiness.summary.ready:
        raise ManualReviewGateError(readiness)

    pool = build_optimizer_pool(conn, slate_id)
    return solve_lineups(
        pool,
        constraints=(
            constraints if constraints is not None else UFCClassicConstraints()
        ),
        n_lineups=n_lineups,
    )
