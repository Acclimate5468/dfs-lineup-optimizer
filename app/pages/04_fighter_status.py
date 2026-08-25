"""Fighter Status v1 — Phase D Streamlit workflow.

Realizes ``docs/FIGHTER_STATUS_V1_DESIGN.md`` §14 / §15 Phase D: the
only UI surface that writes Fighter Status in v1. The page is local /
manual / single-user — every write is an explicit button click, page
load is read-only, and the write path goes through
``FighterRepository.set_manual_status`` /
``FighterRepository.clear_manual_status`` (no direct SQL from the page,
per ``docs/DEVELOPMENT_NOTES.md`` §11).

Hard contract (design §2 / §6 / §7 / §8 / §11; ``docs/DEVELOPMENT_NOTES.md`` §11):

- Writes only ``fighters.manual_status`` / ``fighters.manual_status_set_at``.
- Does NOT read or write ``odds_match_results.effective_status``;
  Fighter Status is strictly disjoint from the odds-match override layer.
- Does NOT feed projections, alerts, the Manual Review gate, the
  optimizer, exports, or any external system. Those promotions are
  Phase F slices, each gated on its own design pass.
- Phase E manual real-feed smoke is still required before the
  workflow is considered validated.
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
from src.slate import fighter_status as fs  # noqa: E402
from src.slate.fighter_status_service import (  # noqa: E402
    FighterStatusRow,
    category_counts,
    list_fighter_status_rows,
)

st.set_page_config(page_title="Fighter Status — DK Lineup Lab", layout="wide")
lock_to_build_page("Fighter Status")
st.title("Fighter Status (v1)")

st.warning(
    "Fighter Status is manual and local-first. This page writes only "
    "manual fighter-status overrides on the selected slate. It does NOT "
    "change odds_match_results.effective_status. It does NOT yet feed "
    "projections, alerts, the Manual Review gate, the optimizer, "
    "exports, or any external system. Phase E manual real-feed smoke "
    "is still required before workflow validation is claimed."
)


def _slate_label(slate: SlateRecord) -> str:
    return (
        f"#{slate.id} — {slate.event_name}"
        + (f" ({slate.event_date})" if slate.event_date else "")
    )


def _manual_cell(row: FighterStatusRow) -> str:
    return row.manual_status if row.manual_status is not None else "—"


def _manual_set_at_cell(row: FighterStatusRow) -> str:
    return (
        row.manual_status_set_at
        if row.manual_status_set_at is not None
        else "—"
    )


def _importer_cell(row: FighterStatusRow) -> str:
    return row.importer_status if row.importer_status else "—"


def _status_rows_dataframe(rows: list[FighterStatusRow]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Fighter": r.name,
                "Salary": r.salary,
                "Importer/Base Status": _importer_cell(r),
                "Manual Override": _manual_cell(r),
                "Manual Override Set At": _manual_set_at_cell(r),
                "Resolved Status": r.effective_status,
                "Category": r.category,
            }
            for r in rows
        ],
        columns=[
            "Fighter",
            "Salary",
            "Importer/Base Status",
            "Manual Override",
            "Manual Override Set At",
            "Resolved Status",
            "Category",
        ],
    )


def _summary_caption(rows: list[FighterStatusRow]) -> str:
    counts = category_counts(rows)
    return (
        f"{counts[fs.CATEGORY_ACTIVE]} active · "
        f"{counts[fs.CATEGORY_WARNING]} warning · "
        f"{counts[fs.CATEGORY_BLOCKING]} blocking"
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
    key="fighter_status_slate_id",
)

rows = list_fighter_status_rows(conn, int(slate_choice))

if not rows:
    st.info(
        "No fighters on this slate yet. Import salaries on Build, Step 1 "
        "first."
    )
    st.stop()

st.caption(f"{len(rows)} fighter(s) — {_summary_caption(rows)}")

st.dataframe(
    _status_rows_dataframe(rows),
    hide_index=True,
    width="stretch",
)

st.divider()
st.subheader("Set manual override")
st.caption(
    "Pick a fighter and a status, then click Set manual status. Nothing "
    "is written until you click the button."
)

fighter_options = {r.fighter_id: r.name for r in rows}
allowed_status_values = sorted(fs.ALLOWED_STATUSES)

set_fighter_id = st.selectbox(
    "Fighter (set)",
    options=list(fighter_options.keys()),
    format_func=lambda fid: fighter_options[fid],
    index=0,
    key="fighter_status_set_fighter_id",
)
set_status_value = st.selectbox(
    "Manual status",
    options=allowed_status_values,
    index=0,
    key="fighter_status_set_status_value",
)

if st.button("Set manual status", key="fighter_status_set_btn"):
    try:
        FighterRepository(conn).set_manual_status(
            slate_id=int(slate_choice),
            fighter_id=int(set_fighter_id),
            status=str(set_status_value),
        )
    except ValueError as exc:
        st.error(f"Could not set manual status: {exc}")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not set manual status: {exc}")
    else:
        st.success(
            f"Set manual status for {fighter_options[int(set_fighter_id)]} "
            f"to {set_status_value!r}."
        )
        st.rerun()

st.divider()
st.subheader("Clear manual override")
st.caption(
    "Pick a fighter and click Clear manual status to remove their "
    "manual override. The resolved status will fall back to the "
    "importer/base status."
)

clear_fighter_id = st.selectbox(
    "Fighter (clear)",
    options=list(fighter_options.keys()),
    format_func=lambda fid: fighter_options[fid],
    index=0,
    key="fighter_status_clear_fighter_id",
)

if st.button("Clear manual status", key="fighter_status_clear_btn"):
    try:
        FighterRepository(conn).clear_manual_status(
            slate_id=int(slate_choice),
            fighter_id=int(clear_fighter_id),
        )
    except ValueError as exc:
        st.error(f"Could not clear manual status: {exc}")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not clear manual status: {exc}")
    else:
        st.success(
            f"Cleared manual status for "
            f"{fighter_options[int(clear_fighter_id)]}."
        )
        st.rerun()
