"""Manual Review Gate v1 — Phase D Streamlit page.

Realizes ``docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`` §6 / §7 / §15 Phase
D: the only UI surface that writes ``slates.manual_review_status`` in
v1. Page load is read-only; the lone write action is the explicit
``Mark Slate Manually Reviewed`` button, gated on every Blocking check
passing and persisted through
``SlateRepository.set_manual_review_reviewed`` per ``docs/DEVELOPMENT_NOTES.md`` §11.

Hard contracts (design §6 / §7 / §14; ``docs/DEVELOPMENT_NOTES.md`` §11):

- Page load performs no writes.
- The page does not execute SQL directly — every write goes through
  ``SlateRepository.set_manual_review_reviewed``.
- The button is the only write affordance. No Undo / Unmark, no
  per-warning controls, no link that mutates any other table.
- ``effective_status`` and Fighter Status are not consulted here; the
  service layer already enforces that contract (design §13 / §14).
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
from src.db.repositories import SlateRecord, SlateRepository  # noqa: E402
from src.slate import manual_review as mr  # noqa: E402
from src.slate.manual_review_service import (  # noqa: E402
    ReviewReadiness,
    evaluate_manual_review,
)

st.set_page_config(page_title="Manual Review — DK Lineup Lab", layout="wide")
lock_to_build_page("Manual Review")
st.title("Manual Review (v1)")

st.warning(
    "Manual review is local. It does NOT call any external service and "
    "does NOT auto-detect fighter availability.\n\n"
    "Marking a slate reviewed does NOT invalidate when underlying data "
    "changes — re-review after any salary re-import, odds save, "
    "recompute, override, or fight-group edit.\n\n"
    "Manual review is the gate that will block the future optimizer and "
    "the future export / run log from running on an un-reviewed slate. "
    "Neither is implemented in v0.\n\n"
    "effective_status is not consulted; Fighter Status is not yet "
    "integrated. See docs/MANUAL_REVIEW_GATE_V1_DESIGN.md §14 / §13."
)


def _slate_label(slate: SlateRecord) -> str:
    return (
        f"#{slate.id} — {slate.event_name}"
        + (f" ({slate.event_date})" if slate.event_date else "")
    )


def _status_badge(status: str) -> str:
    if status == mr.STATUS_PASS:
        return "PASS"
    if status == mr.STATUS_FAIL:
        return "FAIL"
    return "INFO"


# Friendly, fixed labels for the §5 check codes so the table reads in plain
# English instead of raw snake_case identifiers. Display-only — the codes
# remain the source of truth in the service / messages.
_CHECK_LABELS: dict[str, str] = {
    mr.CHECK_SALARY_IMPORTED: "Salary import",
    mr.CHECK_FIGHT_GROUP_COVERAGE: "Fight-group coverage",
    mr.CHECK_FIGHT_GROUP_REVIEW: "Fight-group review",
    mr.CHECK_SCHEDULED_ROUNDS_REVIEWED: "Scheduled rounds",
    mr.CHECK_ODDS_UNMATCHED_ACTIVE: "Odds coverage (blocking)",
    mr.CHECK_ODDS_COVERAGE_PARTIAL: "Odds coverage (partial)",
    mr.CHECK_ODDS_MATCH_REVIEW: "Odds match review",
    mr.CHECK_ODDS_COVERAGE_STAT: "Odds coverage (stat)",
    mr.CHECK_PROJECTION_NON_PROJECTABLE: "Projection coverage",
    mr.CHECK_PROJECTION_MISSING_INPUTS: "Projection inputs",
    mr.CHECK_MISMATCH_ALERTS_WARN: "Mismatch alerts",
    mr.CHECK_MISMATCH_ALERTS_INFO: "Mismatch alerts (info)",
    mr.CHECK_LATE_NEWS_RISK_LOCKED: "Late-news risk",
    mr.CHECK_FIGHTER_STATUS_REVIEW: "Fighter status",
    mr.CHECK_LATE_NEWS_ACKNOWLEDGED: "Late-news acknowledgement",
    mr.CHECK_MANUAL_REVIEW_USER_ACK: "Reviewer acknowledgement",
}


def _humanize_check(code: str) -> str:
    """Plain-English label for a §5 check code (display-only)."""
    return _CHECK_LABELS.get(code, code.replace("_", " ").capitalize())


def _section_column_config(*, include_badge: bool) -> dict:
    """Column sizing so long Message text gets the room it needs and the
    Check / Status columns stay compact (readability — no data change)."""
    cfg = {
        "Check": st.column_config.TextColumn("Check", width="medium"),
        "Message": st.column_config.TextColumn(
            "What it means / how to clear it", width="large"
        ),
    }
    if include_badge:
        cfg["Status"] = st.column_config.TextColumn("Status", width="small")
    return cfg


def _section_dataframe(
    rows: list[mr.ReviewCheckResult], *, include_badge: bool
) -> pd.DataFrame:
    if include_badge:
        return pd.DataFrame(
            [
                {
                    "Check": _humanize_check(r.code),
                    "Status": _status_badge(r.status),
                    "Message": r.message,
                }
                for r in rows
            ],
            columns=["Check", "Status", "Message"],
        )
    return pd.DataFrame(
        [
            {"Check": _humanize_check(r.code), "Message": r.message}
            for r in rows
        ],
        columns=["Check", "Message"],
    )


def _slate_header_line(readiness: ReviewReadiness) -> str:
    status = readiness.manual_review_status or "not_reviewed"
    if status == "reviewed":
        when = readiness.manual_review_completed_at
        return f"Status: reviewed (at {when} UTC)" if when else "Status: reviewed"
    return f"Status: {status}"


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
    key="manual_review_slate_id",
)

readiness = evaluate_manual_review(conn, int(slate_choice))

# §6 enablement: every Blocking check other than ``manual_review_user_ack``
# must pass. ``manual_review_user_ack`` itself is the row this button
# flips on click, so it is excluded from the gate predicate (otherwise
# the button would never become enabled). Per design §6 / §16 step 7,
# the button remains visible and enabled after a successful click so
# the user can refresh ``manual_review_completed_at`` via idempotent
# re-click.
ready_to_mark = not any(
    r.category == mr.CATEGORY_BLOCKING
    and r.status == mr.STATUS_FAIL
    and r.code != mr.CHECK_MANUAL_REVIEW_USER_ACK
    for r in readiness.checks
)

st.markdown(f"**{_slate_header_line(readiness)}**")
st.caption(
    f"Blocking: {readiness.summary.blocking_count} · "
    f"Warning: {readiness.summary.warning_count} · "
    f"Informational: {readiness.summary.info_count} · "
    f"Ready: {'yes' if readiness.summary.ready else 'no'}"
)

# Plain-English readiness banner: mirror the Mark Reviewed button's
# enablement so the page leads with "done vs needs work" + the next step.
_blocking_open = [
    r
    for r in readiness.checks
    if r.category == mr.CATEGORY_BLOCKING
    and r.status == mr.STATUS_FAIL
    and r.code != mr.CHECK_MANUAL_REVIEW_USER_ACK
]
if ready_to_mark:
    st.success(
        "✅ All blocking checks pass — this slate is ready to mark "
        "manually reviewed. Use the button at the bottom of the page."
    )
else:
    st.error(
        f"❌ Needs review — {len(_blocking_open)} blocking check(s) must be "
        "resolved before this slate can be marked reviewed. Each one is "
        "listed under **Blocking** below with the page that fixes it."
    )

blocking_rows = [
    r for r in readiness.checks if r.category == mr.CATEGORY_BLOCKING
]
warning_rows = [
    r for r in readiness.checks if r.category == mr.CATEGORY_WARNING
]
informational_rows = [
    r for r in readiness.checks if r.category == mr.CATEGORY_INFORMATIONAL
]

st.subheader("Blocking")
if blocking_rows:
    st.dataframe(
        _section_dataframe(blocking_rows, include_badge=True),
        hide_index=True,
        width="stretch",
        column_config=_section_column_config(include_badge=True),
    )
else:
    st.caption("No blocking checks.")

st.subheader("Warning")
if warning_rows:
    st.caption(
        "Warnings do not block marking the slate reviewed, but resolve or "
        "consciously accept each one before building."
    )
    st.dataframe(
        _section_dataframe(warning_rows, include_badge=True),
        hide_index=True,
        width="stretch",
        column_config=_section_column_config(include_badge=True),
    )
else:
    st.caption("No warnings.")

# §7 step 7 — session-only late-news / weigh-in acknowledgement. v1
# does not persist this (design §9.2 / §18.6); the toggle resets on
# every page render and never feeds the readiness aggregator.
st.checkbox(
    "I have completed the off-app late-news / weigh-in checklist for "
    "this slate (session-only acknowledgement).",
    value=False,
    key="manual_review_late_news_ack",
)

st.subheader("Informational")
if informational_rows:
    st.dataframe(
        _section_dataframe(informational_rows, include_badge=False),
        hide_index=True,
        width="stretch",
        column_config=_section_column_config(include_badge=False),
    )
else:
    st.caption("No informational notes.")

st.divider()
st.subheader("Mark Slate Manually Reviewed")

if not ready_to_mark:
    st.caption("Resolve the Blocking list before marking this slate reviewed.")

if st.button(
    "Mark Slate Manually Reviewed",
    key="manual_review_mark_btn",
    disabled=not ready_to_mark,
):
    try:
        SlateRepository(conn).set_manual_review_reviewed(int(slate_choice))
    except ValueError as exc:
        st.error(f"Could not mark slate reviewed: {exc}")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not mark slate reviewed: {exc}")
    else:
        st.success(
            f"Marked slate #{int(slate_choice)} manually reviewed."
        )
        st.rerun()
