"""Slate Setup — DK UFC Classic salary CSV validate + import (v0)."""

from __future__ import annotations

import io
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
from src.db.repositories import FighterRepository, SlateRepository  # noqa: E402
from src.ingestion.dk_salary_import_service import (  # noqa: E402
    IMPORTED,
    PARSE_FAILED,
    VALIDATION_FAILED,
    import_dk_salary_dataframe,
)
from src.ingestion.dk_salary_importer import (  # noqa: E402
    REQUIRED_COLUMNS,
    SalaryCsvValidationResult,
    validate_dk_salary_dataframe,
)
from src.slate.fight_grouping import (  # noqa: E402
    ACTIVE_FIGHTER_STATUS,
    group_fighters_by_game_info,
)

st.set_page_config(page_title="Slate Setup — DK Lineup Lab", layout="wide")
lock_to_build_page("Slate Setup")
st.title("Slate Setup")
st.write(
    "Upload the official DK UFC Classic salary CSV to validate its structure, "
    "then explicitly import salaries into a saved slate."
)

st.warning(
    "Importer is NOT complete — validation has not yet been tested against a "
    "real official DK UFC Classic salary CSV. Treat results as provisional. "
    "Salary rows are persisted only when you click the Import button below; "
    "real-file confirmation against an official DK UFC Classic salary CSV is "
    "still required before importer completion is claimed."
)


def _open_conn():
    conn = get_connection()
    bootstrap_database(conn)
    return conn


st.subheader("Event details")
event_name = st.text_input("Event name", key="event_name", placeholder="e.g. UFC 999")
event_date_val = st.date_input("Event date (optional)", value=None, key="event_date")

uploaded = st.file_uploader(
    "DK UFC Classic salary CSV",
    type=["csv"],
    accept_multiple_files=False,
    help="Official DraftKings UFC Classic salary export.",
    key="salary_csv_upload",
)

salary_df: pd.DataFrame | None = None
validation_result: SalaryCsvValidationResult | None = None
load_error: str | None = None

if uploaded is not None:
    try:
        salary_df = pd.read_csv(io.BytesIO(uploaded.getvalue()))
        salary_df.columns = [c.strip() for c in salary_df.columns]
    except pd.errors.EmptyDataError:
        salary_df = None
        load_error = (
            "CSV is empty. Expected an official DK UFC Classic salary "
            "export with columns: " + ", ".join(REQUIRED_COLUMNS)
        )
    except Exception as exc:  # noqa: BLE001
        salary_df = None
        load_error = f"Could not parse CSV: {exc}"

    if salary_df is not None:
        validation_result = validate_dk_salary_dataframe(salary_df)

    if load_error is not None:
        st.error(load_error)
    elif validation_result is not None and validation_result.is_valid:
        st.success(
            f"Valid DK UFC Classic salary CSV — "
            f"{validation_result.row_count} rows detected."
        )
    elif validation_result is not None:
        st.error(validation_result.error_message or "CSV failed validation.")

    if validation_result is not None:
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Detected columns")
            if validation_result.detected_columns:
                st.write(validation_result.detected_columns)
            else:
                st.caption("None detected.")
        with col2:
            st.subheader("Missing required columns")
            if validation_result.missing_columns:
                st.write(validation_result.missing_columns)
            else:
                st.caption("None.")
        st.metric("Row count", validation_result.row_count)

st.divider()
st.subheader("Create slate")

can_create = (
    bool(event_name and event_name.strip())
    and validation_result is not None
    and validation_result.is_valid
)
if validation_result is None and load_error is None:
    st.caption("Upload and validate a salary CSV to enable slate creation.")
elif load_error is not None or (
    validation_result is not None and not validation_result.is_valid
):
    st.caption("Salary CSV must pass validation before a slate can be created.")
elif not event_name or not event_name.strip():
    st.caption("Enter an event name to enable slate creation.")

if st.button("Create Slate", key="create_slate_btn", disabled=not can_create):
    try:
        conn = _open_conn()
        try:
            record = SlateRepository(conn).create(
                event_name=event_name.strip(),
                event_date=event_date_val.isoformat() if event_date_val else None,
                salary_csv_status="validated",
                salary_row_count=validation_result.row_count,
            )
        finally:
            conn.close()
        st.success(f"Saved slate #{record.id}: {record.event_name}")
        st.session_state["last_created_slate_id"] = record.id
    except Exception as exc:  # noqa: BLE001
        st.error(f"Failed to save slate: {exc}")

st.divider()
st.subheader("Saved slates")
try:
    conn = _open_conn()
    try:
        slates = SlateRepository(conn).list_all()
    finally:
        conn.close()
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not load saved slates: {exc}")
    slates = []

if not slates:
    st.caption("No slates saved yet.")
else:
    st.dataframe(
        [
            {
                "id": s.id,
                "event_name": s.event_name,
                "event_date": s.event_date or "",
                "salary_csv_status": s.salary_csv_status,
                "salary_row_count": s.salary_row_count,
                "created_at": s.created_at,
            }
            for s in slates
        ],
        hide_index=True,
    )

st.divider()
st.subheader("Import salaries into slate")
st.caption(
    "Persists the validated salary CSV rows as slate-scoped fighter rows. "
    "Nothing is written until you click Import. The import is idempotent: "
    "re-importing the same CSV against the same slate updates existing rows "
    "in place and reports unchanged rows rather than duplicating. Odds "
    "matching is NOT recomputed by this action — recompute it manually on "
    "the Odds page if needed."
)

selected_slate_id: int | None = None
if not slates:
    st.caption("Create a slate above to enable salary import.")
else:
    default_idx = 0
    last_id = st.session_state.get("last_created_slate_id")
    if last_id is not None:
        for i, s in enumerate(slates):
            if s.id == last_id:
                default_idx = i
                break
    selected_slate_id = st.selectbox(
        "Target slate",
        options=[s.id for s in slates],
        format_func=lambda sid: next(
            (f"#{s.id} — {s.event_name}" for s in slates if s.id == sid),
            str(sid),
        ),
        index=default_idx,
        key="import_target_slate_id",
    )

ready_to_import = (
    selected_slate_id is not None
    and salary_df is not None
    and validation_result is not None
    and validation_result.is_valid
)
if selected_slate_id is not None:
    if salary_df is None or validation_result is None:
        st.caption("Upload a salary CSV to enable salary import.")
    elif not validation_result.is_valid:
        st.caption(
            "Salary CSV must pass validation before salaries can be imported."
        )

if st.button(
    "Import salaries into slate",
    key="import_salaries_btn",
    disabled=not ready_to_import,
):
    target_id = int(selected_slate_id)
    try:
        conn = _open_conn()
        try:
            result = import_dk_salary_dataframe(
                conn,
                slate_id=target_id,
                df=salary_df,
            )
            # Read the persisted roster in the same connection so the
            # post-import Game Info feedback (copy + counts only — no write)
            # reflects exactly what was just persisted (design §5).
            roster = (
                FighterRepository(conn).list_for_slate(target_id)
                if result.status == IMPORTED
                else []
            )
        finally:
            conn.close()
    except ValueError as exc:
        st.error(f"Salary import failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Salary import failed: {exc}")
    else:
        if result.status == IMPORTED and result.upsert is not None:
            ups = result.upsert
            st.success(
                f"Imported salaries into slate #{target_id}: "
                f"parsed {result.parsed_row_count}, "
                f"inserted {ups.inserted}, "
                f"updated {ups.updated}, "
                f"unchanged {ups.unchanged}, "
                f"deactivated {ups.deactivated}."
            )

            # Game Info persistence + suggested-pairing readout (design §5).
            # Counts only — this action creates no fight groups; the
            # preview -> Apply flow on the Fight Groups page does that.
            active = [
                f for f in roster if f.status == ACTIVE_FIGHTER_STATUS
            ]
            grouping = group_fighters_by_game_info(roster)
            captured = len(active) - grouping.uncovered_count
            if not active:
                st.info(
                    "No active fighters persisted — no Game Info to report."
                )
            elif captured == 0:
                st.warning(
                    f"Game Info captured: 0 of {len(active)} active fighters. "
                    "This CSV carried no usable Game Info, so no DK pairings "
                    "can be suggested — pair fighters manually on the Fight "
                    "Groups page."
                )
            else:
                st.info(
                    f"Game Info captured: {captured} of {len(active)} active "
                    f"fighters. Suggested DK pairings available: "
                    f"{grouping.suggested_count} — review and apply on the "
                    "Fight Groups page. Remember to set the 5-round main "
                    "event / title bout manually after applying."
                )
        elif result.status == VALIDATION_FAILED:
            st.error(
                "Salary CSV structural validation failed during import; no "
                "fighters were persisted. Details: "
                + (result.error_message or "(no detail)")
            )
        elif result.status == PARSE_FAILED:
            st.error(
                "Salary CSV row-level parsing failed after structural "
                "validation; no fighters were persisted. Details: "
                + (result.error_message or "(no detail)")
            )
        else:
            st.error(f"Unexpected salary import status: {result.status!r}")
