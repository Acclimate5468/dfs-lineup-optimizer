"""Optimizer v1 (Slice B.5) — Streamlit page.

Realizes ``docs/OPTIMIZER_V1_DESIGN.md`` §6 / §8 Phase B.5: a single
button-press surface around
:func:`src.optimizer.optimizer_service.run_optimizer`. On every render
the page reads the slate selector, the Manual Review Gate readout, and
the existing salary / projection state; on an explicit
``Generate Lineups`` click it invokes the optimizer service and renders
the resulting in-memory lineups.

Hard contracts (design §4 / §5.3 / §6 / §9 / §10 / §11; ``docs/DEVELOPMENT_NOTES.md`` §11):

- Read-only end to end. No INSERT / UPDATE / DELETE on any table —
  page load, slate switch, and Generate click all compose only the
  read-only ``evaluate_manual_review`` and ``run_optimizer`` paths.
- No optimizer run on page load. The solver only runs when the user
  clicks ``Generate Lineups`` (design §6).
- Manual Review Gate is enforced in two layers: the click is gated on
  ``readiness.summary.ready`` (button is disabled when not green) and
  the click handler still wraps ``run_optimizer`` in a
  ``ManualReviewGateError`` ``try``/``except`` as defense in depth
  (design §4 — "the UI is the primary UX, the service is the safety
  net").
- Optimizer v1 builds internal research lineups only. The page does
  NOT enter DraftKings contests, does NOT export a DK upload CSV,
  and does NOT persist generated lineups or per-run history (design
  §10 risk #5 / §11 — all explicitly out of scope for v1).
- ``effective_status`` and Fighter Status v1 are NOT consulted here
  (design §9 / ``ODDS_PERSISTENCE_DESIGN.md`` §15.11 risk #7).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.prototype_mode import lock_to_build_page  # noqa: E402
from src.db.connection import get_connection  # noqa: E402
from src.db.migrations import bootstrap_database  # noqa: E402
from src.db.repositories import (  # noqa: E402
    FighterRepository,
    SlateRecord,
    SlateRepository,
)
from src.optimizer.lineup_solver import (  # noqa: E402
    SolveResult,
    STATUS_INFEASIBLE_CONSTRAINTS,
    STATUS_INFEASIBLE_POOL_TOO_SMALL,
    STATUS_OK,
    STATUS_OK_PARTIAL,
)
from src.optimizer.optimizer_service import (  # noqa: E402
    ManualReviewGateError,
    run_optimizer,
)
from src.slate.manual_review_service import (  # noqa: E402
    ReviewReadiness,
    evaluate_manual_review,
)

st.set_page_config(page_title="Optimizer — DK Lineup Lab", layout="wide")
lock_to_build_page("Optimizer")
st.title("Optimizer (v1)")

st.warning(
    "Optimizer v1 — research lineups only.\n\n"
    "- Builds internal research lineups; it does NOT enter DraftKings "
    "contests.\n"
    "- It does NOT export a DK upload CSV (future work).\n"
    "- It does NOT persist generated lineups or per-run history "
    "(future work).\n"
    "- It requires the Manual Review Gate to be green for the selected "
    "slate.\n"
    "- effective_status and Fighter Status are not consulted in v1. "
    "See docs/OPTIMIZER_V1_DESIGN.md §9."
)


def _slate_label(slate: SlateRecord) -> str:
    return (
        f"#{slate.id} — {slate.event_name}"
        + (f" ({slate.event_date})" if slate.event_date else "")
    )


def _slate_header_line(readiness: ReviewReadiness) -> str:
    status = readiness.manual_review_status or "not_reviewed"
    if status == "reviewed":
        when = readiness.manual_review_completed_at
        return (
            f"Manual Review: reviewed (at {when} UTC)"
            if when
            else "Manual Review: reviewed"
        )
    return f"Manual Review: {status}"


def _render_solve_result(
    result: SolveResult,
    *,
    fighter_name_by_id: dict[int, str],
    fighter_salary_by_id: dict[int, int],
) -> None:
    st.markdown(f"**Solver status:** `{result.status}`")

    if result.status == STATUS_OK:
        st.success(
            f"Generated {len(result.lineups)} lineup(s) — "
            f"6 fighters each."
        )
    elif result.status == STATUS_OK_PARTIAL:
        st.warning(
            f"Partial solve: only {len(result.lineups)} feasible "
            f"lineup(s). {result.reason}"
        )
    elif result.status == STATUS_INFEASIBLE_POOL_TOO_SMALL:
        st.error(
            f"Cannot generate lineups — optimizer pool is too small. "
            f"{result.reason}"
        )
        return
    elif result.status == STATUS_INFEASIBLE_CONSTRAINTS:
        st.error(
            f"Cannot generate lineups — no feasible lineup under the "
            f"current constraints. {result.reason}"
        )
        return
    else:
        st.error(
            f"Unexpected solver status `{result.status}`: {result.reason}"
        )
        return

    total_lineups = len(result.lineups)
    for idx, lineup in enumerate(result.lineups, start=1):
        st.subheader(f"Lineup {idx} of {total_lineups}")
        rows = []
        for fid in lineup.fighter_ids:
            fid_int = int(fid)
            rows.append(
                {
                    "Fighter": fighter_name_by_id.get(
                        fid_int, f"#{fid_int}"
                    ),
                    "Salary": fighter_salary_by_id.get(fid_int, 0),
                }
            )
        st.dataframe(
            pd.DataFrame(rows, columns=["Fighter", "Salary"]),
            hide_index=True,
            width="stretch",
            key=f"optimizer_lineup_df_{idx}",
        )
        st.caption(
            f"Total salary: ${lineup.total_salary:,} · "
            f"Total projection: {lineup.total_projection:.2f} DK pts"
        )


conn = get_connection()
bootstrap_database(conn)

slates = SlateRepository(conn).list_all()

if not slates:
    st.info(
        "No slates yet. Create a slate and import a DK UFC Classic salary "
        "CSV on Build, Step 1 first."
    )
    st.stop()

slate_options = {s.id: _slate_label(s) for s in slates}
slate_choice = st.selectbox(
    "Slate",
    options=list(slate_options.keys()),
    format_func=lambda sid: slate_options[sid],
    index=0,
    key="optimizer_slate_id",
)

readiness = evaluate_manual_review(conn, int(slate_choice))
gate_ready = bool(readiness.summary.ready)

st.markdown(f"**{_slate_header_line(readiness)}**")
st.caption(
    f"Blocking: {readiness.summary.blocking_count} · "
    f"Warning: {readiness.summary.warning_count} · "
    f"Informational: {readiness.summary.info_count} · "
    f"Ready: {'yes' if gate_ready else 'no'}"
)

if not gate_ready:
    st.warning(
        f"Manual Review Gate is not green for slate #{int(slate_choice)} — "
        f"{readiness.summary.blocking_count} Blocking check(s) still "
        "failing. Resolve them on the Manual Review page before "
        "generating lineups."
    )

n_lineups = st.number_input(
    "Number of lineups to generate (1–5)",
    min_value=1,
    max_value=5,
    value=1,
    step=1,
    key="optimizer_n_lineups",
)

st.caption(
    "Each lineup is a full 6-fighter DK Classic roster, not 6 separate "
    "entries. Choosing 5 here produces 5 distinct 6-fighter lineups."
)

st.caption(
    "The optimizer does not run on page load; results appear only after "
    "you click Generate Lineups."
)

generate_clicked = st.button(
    "Generate Lineups",
    key="optimizer_generate_btn",
    disabled=not gate_ready,
)

if generate_clicked:
    try:
        result = run_optimizer(
            conn,
            slate_id=int(slate_choice),
            n_lineups=int(n_lineups),
        )
    except ManualReviewGateError as exc:
        st.error(
            f"Cannot generate lineups — Manual Review Gate is not green "
            f"for slate #{exc.slate_id} "
            f"({exc.readiness.summary.blocking_count} Blocking check(s) "
            "failing). Resolve them on the Manual Review page and try "
            "again."
        )
    else:
        fighters = FighterRepository(conn).list_for_slate(
            int(slate_choice)
        )
        fighter_name_by_id = {int(f.id): f.name for f in fighters}
        fighter_salary_by_id = {int(f.id): int(f.salary) for f in fighters}
        _render_solve_result(
            result,
            fighter_name_by_id=fighter_name_by_id,
            fighter_salary_by_id=fighter_salary_by_id,
        )
