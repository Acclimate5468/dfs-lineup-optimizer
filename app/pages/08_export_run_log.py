"""Export / Run Log v1 (Slice C.3) — Streamlit page.

Realizes ``docs/EXPORT_RUN_LOG_V1_DESIGN.md`` §2 / §6 / §7 / §8 C.3:
a single-button research-only export surface around
:func:`src.exports.export_service.build_run_log`. On every render the
page reads the slate selector, the Manual Review Gate readout, and the
``n_lineups`` input; on an explicit ``Build Internal Export`` click it
calls the C.3 service and hands the C.2 formatter bytes to three
``st.download_button`` widgets (CSV / JSON / Markdown).

Hard contracts (design §1.1 / §2 / §5 / §7 / §11; ``docs/DEVELOPMENT_NOTES.md`` §11):

- **Read-only end to end.** No INSERT / UPDATE / DELETE on any table —
  page load, slate switch, and Build click all compose only the
  read-only ``evaluate_manual_review`` and ``build_run_log`` paths.
- **No export build on page load.** The C.3 service only runs when
  the user clicks ``Build Internal Export`` (design §2).
- **Internal research export only.** The page does NOT produce a
  DK-upload-compatible CSV, does NOT enter DraftKings contests, does
  NOT log in to DraftKings, does NOT persist exports to disk, and
  does NOT write to the DB (design §1.1 / §5 Option A). All bytes are
  delivered exclusively through ``st.download_button``.
- **Manual Review Gate enforced twice.** The Build button is disabled
  when the gate is not green, and the service still synthesizes a
  diagnostics-only ``gate_blocked`` bundle for race-safety
  (design §7 rule 4).
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.prototype_mode import lock_to_build_page  # noqa: E402

from src.db.connection import get_connection  # noqa: E402
from src.db.migrations import bootstrap_database  # noqa: E402
from src.db.repositories import SlateRecord, SlateRepository  # noqa: E402
from src.exports.export_service import (  # noqa: E402
    STATUS_GATE_BLOCKED,
    ExportValidationError,
    build_run_log,
)
from src.exports.internal_export import (  # noqa: E402
    InternalExportBundle,
    format_lineups_csv,
    format_lineups_json,
    format_lineups_wide_csv,
    format_markdown_summary,
)
from src.optimizer.lineup_solver import (  # noqa: E402
    STATUS_INFEASIBLE_CONSTRAINTS,
    STATUS_INFEASIBLE_POOL_TOO_SMALL,
    STATUS_OK,
    STATUS_OK_PARTIAL,
)
from src.slate.manual_review_service import (  # noqa: E402
    ReviewReadiness,
    evaluate_manual_review,
)

st.set_page_config(
    page_title="Export & Run Log — DK Lineup Lab", layout="wide"
)
lock_to_build_page("Export & Run Log")
st.title("Export & Run Log (v1)")

st.warning(
    "Internal research export only.\n\n"
    "- This page produces internal research artifacts, NOT a "
    "DraftKings upload file.\n"
    "- It does NOT enter DraftKings contests.\n"
    "- It does NOT log in to or automate DraftKings.\n"
    "- The app writes no files to disk — exports are delivered only as "
    "download_button payloads. Save them yourself under the gitignored "
    "`exports/` directory if you want to keep a copy.\n"
    "- It does NOT persist exports or run history to the database.\n"
    "- It requires the Manual Review Gate to be green for the selected "
    "slate.\n"
    "- effective_status and Fighter Status are not consulted in v1."
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


def _safe_run_id_for_filename(run_id: str) -> str:
    """Make ``run_id`` filesystem-portable (design §6 — replace ``:``)."""
    return run_id.replace(":", "-")


def _render_status_banner(bundle: InternalExportBundle) -> None:
    status = bundle.optimizer_status
    reason = bundle.optimizer_reason or "—"
    st.markdown(f"**Solver status:** `{status}`")
    if status == STATUS_OK:
        st.success(
            f"Generated {bundle.n_lineups_generated} lineup(s) for the "
            f"internal research export."
        )
    elif status == STATUS_OK_PARTIAL:
        st.warning(
            f"Partial solve: only {bundle.n_lineups_generated} feasible "
            f"lineup(s). {reason}"
        )
    elif status == STATUS_GATE_BLOCKED:
        st.error(
            f"Cannot build a non-diagnostic export — Manual Review Gate "
            f"is not green. {reason}"
        )
    elif status == STATUS_INFEASIBLE_POOL_TOO_SMALL:
        st.error(
            f"Optimizer pool is too small to build a lineup. {reason}"
        )
    elif status == STATUS_INFEASIBLE_CONSTRAINTS:
        st.error(
            f"No feasible lineup under the current constraints. {reason}"
        )
    else:
        st.error(f"Unexpected solver status `{status}`: {reason}")


def _render_validation_panel(bundle: InternalExportBundle) -> None:
    st.subheader("Validation")
    if not bundle.lineups:
        st.info(
            "No lineups in this bundle — diagnostics-only export "
            "(design §7 rule 5)."
        )
        return
    for lu in bundle.lineups:
        ok_count = len(lu.fighters) == 6
        ok_salary = lu.total_salary <= 50_000
        fg_ids = [
            f.fight_group_id
            for f in lu.fighters
            if f.fight_group_id is not None
        ]
        ok_pairs = len(fg_ids) == len(set(fg_ids))
        st.write(
            f"Lineup {lu.lineup_index}: "
            f"6 fighters {'OK' if ok_count else 'FAIL'} · "
            f"salary ≤ $50,000 {'OK' if ok_salary else 'FAIL'} · "
            f"no same-fight pair {'OK' if ok_pairs else 'FAIL'} · "
            f"total ${lu.total_salary:,} · "
            f"projection {lu.total_projection:.2f} DK pts"
        )


def _render_preview(bundle: InternalExportBundle) -> None:
    if not bundle.lineups:
        return
    st.subheader("Preview")
    total_lineups = len(bundle.lineups)
    for lu in bundle.lineups:
        st.markdown(f"**Lineup {lu.lineup_index} of {total_lineups}**")
        rows = [
            {
                "Fighter": f.fighter_name,
                "Salary": int(f.dk_salary),
                "Projection": round(float(f.default_projection), 2),
                "Fight group": (
                    "" if f.fight_group_id is None else int(f.fight_group_id)
                ),
            }
            for f in lu.fighters
        ]
        st.dataframe(
            rows,
            hide_index=True,
            width="stretch",
            key=f"export_preview_df_{lu.lineup_index}",
        )
        st.caption(
            f"Total salary: ${lu.total_salary:,} · "
            f"Total projection: {lu.total_projection:.2f} DK pts"
        )


def _render_diagnostics(bundle: InternalExportBundle) -> None:
    diag = bundle.diagnostics
    if diag is None:
        return
    st.subheader("Diagnostics")
    st.write(f"Pool size: {int(diag.pool_size)}")
    if diag.excluded:
        st.write("Excluded fighters:")
        for entry in diag.excluded:
            st.write(f"- {entry.name} — {entry.reason}")
    else:
        st.caption("No excluded fighters recorded for this pool.")


def _render_downloads(bundle: InternalExportBundle) -> None:
    st.subheader("Downloads")
    st.caption(
        "Save downloads under the project's gitignored `exports/` "
        "directory if you want to keep them. Do NOT commit. Do NOT "
        "upload to DraftKings."
    )
    safe_id = _safe_run_id_for_filename(bundle.metadata.run_id)
    csv_bytes = format_lineups_csv(bundle)
    wide_csv_bytes = format_lineups_wide_csv(bundle)
    json_bytes = format_lineups_json(bundle)
    md_bytes = format_markdown_summary(bundle)
    st.download_button(
        "Download internal CSV summary (tidy, one row per fighter)",
        data=csv_bytes,
        file_name=f"optimizer_run_{safe_id}.csv",
        mime="text/csv",
        key="export_download_csv",
    )
    st.download_button(
        "Download per-lineup CSV (wide, one row per lineup)",
        data=wide_csv_bytes,
        file_name=f"optimizer_run_{safe_id}_wide.csv",
        mime="text/csv",
        key="export_download_wide_csv",
    )
    st.download_button(
        "Download internal JSON summary",
        data=json_bytes,
        file_name=f"optimizer_run_{safe_id}.json",
        mime="application/json",
        key="export_download_json",
    )
    st.download_button(
        "Download Markdown run log",
        data=md_bytes,
        file_name=f"optimizer_run_{safe_id}.md",
        mime="text/markdown",
        key="export_download_md",
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
    key="export_slate_id",
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
        "failing. Resolve them on the Manual Review page before building "
        "an export."
    )

n_lineups = st.number_input(
    "Number of lineups to export (1–5)",
    min_value=1,
    max_value=5,
    value=1,
    step=1,
    key="export_n_lineups",
)

st.caption(
    "The export is not built on page load; the bundle is only produced "
    "after you click Build Internal Export."
)

build_clicked = st.button(
    "Build Internal Export",
    key="export_build_btn",
    disabled=not gate_ready,
)

if build_clicked:
    try:
        bundle = build_run_log(
            conn,
            slate_id=int(slate_choice),
            n_lineups=int(n_lineups),
        )
    except ExportValidationError as exc:
        st.error(f"Cannot build export — {exc.reason}")
    else:
        _render_status_banner(bundle)
        _render_validation_panel(bundle)
        _render_preview(bundle)
        _render_diagnostics(bundle)
        _render_downloads(bundle)
