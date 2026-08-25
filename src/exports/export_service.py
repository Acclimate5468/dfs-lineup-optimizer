"""Export / Run Log v1 orchestration service (Slice C.3).

Phase C.3 of ``docs/EXPORT_RUN_LOG_V1_DESIGN.md`` §2 / §7 / §8: the
single read-only entry point that turns a ``slate_id`` + ``n_lineups``
request into an :class:`InternalExportBundle` ready for the C.2
formatters (``format_lineups_csv`` / ``format_lineups_json`` /
``format_markdown_summary``).

Behavior, in order, per design §2 / §7 / §12:

1. Re-read the Manual Review Gate via
   :func:`evaluate_manual_review`. If ``summary.ready`` is False, the
   service builds and returns a diagnostics-only bundle with
   ``optimizer_status == "gate_blocked"`` and no lineup rows — it does
   not raise. The Streamlit page (``app/pages/08_export_run_log.py``)
   gates the click on the gate readout as well, so a not-green gate
   normally never reaches the service; this branch is defense in
   depth for a race between page render and click.
2. Validate ``n_lineups`` is an integer in ``[1, 5]``. Anything else
   raises :class:`ExportValidationError` *before* the optimizer is
   invoked. (Mirrors the bound the solver enforces; surfacing it as
   the same exception type the page already handles keeps the click
   handler tidy.)
3. Build the :class:`OptimizerPool` via :func:`build_optimizer_pool`
   and call :func:`run_optimizer` once. The optimizer service
   re-reads the gate internally — if it has flipped to not-ready
   between step 1 and now, the service catches
   :class:`ManualReviewGateError` and returns the same diagnostics-
   only ``gate_blocked`` bundle as step 1.
4. Apply design §7 rules 1–3 (exactly 6 fighter ids, salary cap, no
   same-fight pair conflict) to every lineup returned by the solver.
   On any failure, raise :class:`ExportValidationError` with the
   offending lineup attached — no partial emit.
5. Build the :class:`ExportRunMetadata` (run id, generated-at, slate
   fields, Manual Review snapshot) and call
   :func:`build_internal_export_bundle` to produce the
   :class:`InternalExportBundle`.

Hard contracts (design §5 / §7 / §11; ``docs/DEVELOPMENT_NOTES.md`` §7 / §11):

- **Read-only.** The service issues no ``INSERT`` / ``UPDATE`` /
  ``DELETE`` on any table. The Manual Review status column is not
  written here. No projection recompute, no override mutation.
- **No file writes.** Per design §5 Option A the service returns
  bytes-ready data structures only; persistence is the user's choice
  via the Streamlit ``st.download_button`` handed the C.2 formatter
  output.
- **No DK upload schema.** The returned bundle is fed to the C.2
  internal-research formatters; neither this service nor the C.2
  module ever produces a DK-upload-compatible CSV.
- **No raw upload-row passthrough.** Only persisted ``Fighter`` /
  ``Salary`` / projection / fight-group fields appear in the bundle;
  raw odds CSV rows and raw salary CSV rows are not embedded
  (design §4.1).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.config.constants import LINEUP_SIZE, SALARY_CAP
from src.db.repositories import FighterRepository, SlateRepository
from src.exports.internal_export import (
    ExcludedFighterEntry,
    ExportDiagnostics,
    ExportRunMetadata,
    InternalExportBundle,
    ManualReviewSnapshot,
    build_internal_export_bundle,
)
from src.optimizer.lineup_solver import (
    Lineup,
    SolveResult,
    MAX_N_LINEUPS,
    MIN_N_LINEUPS,
)
from src.optimizer.optimizer_service import (
    ManualReviewGateError,
    run_optimizer,
)
from src.optimizer.pool_builder import OptimizerPool, build_optimizer_pool
from src.projections.slate_projection_service import project_slate
from src.slate.manual_review_service import (
    ReviewReadiness,
    evaluate_manual_review,
)

STATUS_GATE_BLOCKED = "gate_blocked"


class ExportValidationError(ValueError):
    """Raised when the orchestration service refuses to emit an export
    because a §7 rule 1–3 check failed (or because ``n_lineups`` is
    out of range).

    ``reason`` is a short, human-readable string suitable for display.
    ``lineup`` carries the offending :class:`Lineup` (or ``None`` when
    the failure is not lineup-scoped, e.g. an out-of-range
    ``n_lineups``).
    """

    def __init__(
        self,
        reason: str,
        *,
        lineup: Lineup | None = None,
    ) -> None:
        self.reason = reason
        self.lineup = lineup
        super().__init__(reason)


def _now_utc_iso() -> str:
    """ISO-8601 UTC timestamp, second precision (design §4)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_run_id(slate_id: int, generated_at_utc: str) -> str:
    """Compose the §4 run id — ISO-8601 UTC + slate id."""
    return f"{generated_at_utc}-slate{int(slate_id)}"


def _snapshot_from_readiness(readiness: ReviewReadiness) -> ManualReviewSnapshot:
    summary = readiness.summary
    return ManualReviewSnapshot(
        ready=bool(summary.ready),
        status=readiness.manual_review_status or "not_reviewed",
        completed_at_utc=readiness.manual_review_completed_at,
        blocking_count=int(summary.blocking_count),
        warning_count=int(summary.warning_count),
        informational_count=int(summary.info_count),
    )


def _slate_fields(
    conn: sqlite3.Connection, slate_id: int
) -> tuple[str | None, str | None]:
    """Return ``(name, event_date)`` for ``slate_id`` or ``(None, None)``."""
    for slate in SlateRepository(conn).list_all():
        if int(slate.id) == int(slate_id):
            return slate.event_name, slate.event_date
    return None, None


def _diagnostics_from_pool(pool: OptimizerPool) -> ExportDiagnostics:
    return ExportDiagnostics(
        pool_size=len(pool.entries),
        excluded=tuple(
            ExcludedFighterEntry(name=e.name, reason=e.reason)
            for e in pool.excluded
        ),
    )


def _validate_lineups(
    result: SolveResult,
    *,
    pool: OptimizerPool | None,
) -> None:
    """Apply design §7 rules 1–3 to every lineup in ``result``.

    Empty ``result.lineups`` (gate_blocked / infeasible_*) is the
    design §7 rule 5 diagnostics-only shape — no rules to apply.
    """
    if not result.lineups:
        return

    pair_lookup: list[frozenset[int]] = []
    if pool is not None:
        pair_lookup = [frozenset(p) for p in pool.same_fight_pairs]

    for lineup in result.lineups:
        ids = tuple(int(fid) for fid in lineup.fighter_ids)
        if len(ids) != LINEUP_SIZE:
            raise ExportValidationError(
                f"lineup must contain exactly {LINEUP_SIZE} fighter ids; "
                f"got {len(ids)}",
                lineup=lineup,
            )
        if int(lineup.total_salary) > SALARY_CAP:
            raise ExportValidationError(
                f"lineup total salary {int(lineup.total_salary)} exceeds "
                f"cap {SALARY_CAP}",
                lineup=lineup,
            )
        id_set = set(ids)
        for pair in pair_lookup:
            if pair.issubset(id_set):
                a, b = sorted(pair)
                raise ExportValidationError(
                    f"lineup contains same-fight pair ({a}, {b})",
                    lineup=lineup,
                )


def _gate_blocked_bundle(
    *,
    slate_id: int,
    n_lineups: int,
    metadata: ExportRunMetadata,
    readiness: ReviewReadiness,
) -> InternalExportBundle:
    """Build a diagnostics-only bundle for a not-ready gate."""
    blocking = readiness.summary.blocking_count
    reason = (
        f"Manual Review Gate is not green for slate #{int(slate_id)}; "
        f"{blocking} Blocking check(s) still failing."
    )
    fake_result = SolveResult(
        slate_id=int(slate_id),
        status=STATUS_GATE_BLOCKED,
        lineups=(),
        reason=reason,
    )
    return build_internal_export_bundle(
        fake_result,
        metadata=metadata,
        fighter_name_by_id={},
        fighter_salary_by_id={},
        diagnostics=ExportDiagnostics(pool_size=0, excluded=()),
    )


def build_run_log(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    n_lineups: int,
) -> InternalExportBundle:
    """Orchestrate a single Export / Run Log v1 build (design §2 / §7).

    See module docstring for the step-by-step contract.

    Returns the :class:`InternalExportBundle` the C.2 formatters
    consume. Read-only end to end — no DB writes, no file writes, no
    persistence (design §5 Option A).

    Raises :class:`ExportValidationError` when ``n_lineups`` is out of
    range or any §7 rule 1–3 check fails on a returned lineup.
    """
    sid = int(slate_id)

    if not isinstance(n_lineups, int) or isinstance(n_lineups, bool):
        raise ExportValidationError(
            f"n_lineups must be an int in "
            f"[{MIN_N_LINEUPS}, {MAX_N_LINEUPS}], got {n_lineups!r}"
        )
    if n_lineups < MIN_N_LINEUPS or n_lineups > MAX_N_LINEUPS:
        raise ExportValidationError(
            f"n_lineups must be in [{MIN_N_LINEUPS}, {MAX_N_LINEUPS}], "
            f"got {n_lineups}"
        )

    readiness = evaluate_manual_review(conn, sid)
    slate_name, slate_event_date = _slate_fields(conn, sid)
    generated_at_utc = _now_utc_iso()
    run_id = _build_run_id(sid, generated_at_utc)

    metadata = ExportRunMetadata(
        run_id=run_id,
        generated_at_utc=generated_at_utc,
        n_lineups_requested=int(n_lineups),
        slate_id=sid,
        slate_name=slate_name,
        slate_event_date=slate_event_date,
        manual_review=_snapshot_from_readiness(readiness),
    )

    if not readiness.summary.ready:
        return _gate_blocked_bundle(
            slate_id=sid,
            n_lineups=int(n_lineups),
            metadata=metadata,
            readiness=readiness,
        )

    pool = build_optimizer_pool(conn, sid)
    diagnostics = _diagnostics_from_pool(pool)

    try:
        result = run_optimizer(
            conn, slate_id=sid, n_lineups=int(n_lineups)
        )
    except ManualReviewGateError:
        # Race: gate flipped to not-ready between our read and the
        # optimizer's re-read. Fall back to the diagnostics-only
        # gate_blocked shape (design §7 rule 4).
        readiness_now = evaluate_manual_review(conn, sid)
        metadata = ExportRunMetadata(
            run_id=run_id,
            generated_at_utc=generated_at_utc,
            n_lineups_requested=int(n_lineups),
            slate_id=sid,
            slate_name=slate_name,
            slate_event_date=slate_event_date,
            manual_review=_snapshot_from_readiness(readiness_now),
        )
        return _gate_blocked_bundle(
            slate_id=sid,
            n_lineups=int(n_lineups),
            metadata=metadata,
            readiness=readiness_now,
        )

    _validate_lineups(result, pool=pool)

    fighters = FighterRepository(conn).list_for_slate(sid)
    fighter_name_by_id: dict[int, str] = {
        int(f.id): f.name for f in fighters
    }
    fighter_salary_by_id: dict[int, int] = {
        int(f.id): int(f.salary) for f in fighters
    }

    projections = project_slate(conn, sid)
    fighter_projection_by_id: dict[int, float] = {}
    for p in projections:
        if p.fighter_id is None or p.projected_dk_points is None:
            continue
        fighter_projection_by_id[int(p.fighter_id)] = float(
            p.projected_dk_points
        )

    fighter_fight_group_by_id: dict[int, int | None] = {
        int(e.fighter_id): e.fight_group_id for e in pool.entries
    }

    return build_internal_export_bundle(
        result,
        metadata=metadata,
        fighter_name_by_id=fighter_name_by_id,
        fighter_salary_by_id=fighter_salary_by_id,
        fighter_projection_by_id=fighter_projection_by_id,
        fighter_fight_group_by_id=fighter_fight_group_by_id,
        diagnostics=diagnostics,
    )
