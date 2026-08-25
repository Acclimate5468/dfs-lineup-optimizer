"""Alerts (Mismatch Alerts v1 preview) — read-only Phase C page.

Implements ``docs/MISMATCH_ALERTS_V1_DESIGN.md`` §10 / Phase C: a
Streamlit page that renders ``evaluate_alerts(...)`` output for a
selected slate.

Hard contract (design §2, §8, §10, §11; ``docs/DEVELOPMENT_NOTES.md`` §11):

- Read-only. No buttons, forms, callbacks, or write actions.
- No alerts are persisted (design §5 / §14 non-goal).
- ``effective_status`` is not consulted (design §2 / §8 and
  ``ODDS_PERSISTENCE_DESIGN.md`` §15.11 risk #7); reject / accept
  overrides do not affect these alerts.
- Alerts do not feed the optimizer, exports, or any external system
  (design §10 / §14).
- Mismatch Alerts v1 still requires a Phase D real-feed smoke before
  it is considered complete (design §12, ``docs/DEVELOPMENT_NOTES.md`` §8).
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

from src.alerts.alert_rules import Alert, SEVERITY_INFO, SEVERITY_WARN  # noqa: E402
from src.alerts.alert_service import evaluate_alerts  # noqa: E402
from src.db.connection import get_connection  # noqa: E402
from src.db.migrations import bootstrap_database  # noqa: E402
from src.db.repositories import SlateRecord, SlateRepository  # noqa: E402

st.set_page_config(page_title="Alerts — DK Lineup Lab", layout="wide")
lock_to_build_page("Alerts")
st.title("Alerts (v1 preview)")

st.warning(
    "Read-only. No alerts are persisted. "
    "`effective_status` is not consulted; reject/accept overrides do "
    "not affect these alerts. These alerts do NOT feed the optimizer, "
    "exports, or any external system. Mismatch Alerts v1 still requires "
    "a Phase D real-feed smoke before completion is claimed."
)


def _slate_label(slate: SlateRecord) -> str:
    return (
        f"#{slate.id} — {slate.event_name}"
        + (f" ({slate.event_date})" if slate.event_date else "")
    )


def _alerts_dataframe(alerts: list[Alert]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Severity": a.severity,
                "Scope": a.scope,
                "Fighter": a.fighter_name if a.fighter_name is not None else "—",
                "Code": a.code,
                "Message": a.message,
            }
            for a in alerts
        ],
        columns=["Severity", "Scope", "Fighter", "Code", "Message"],
    )


def _severity_summary(alerts: list[Alert]) -> str:
    warn = sum(1 for a in alerts if a.severity == SEVERITY_WARN)
    info = sum(1 for a in alerts if a.severity == SEVERITY_INFO)
    return f"{warn} warn · {info} info"


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
    key="alerts_slate_id",
)

alerts = evaluate_alerts(conn, int(slate_choice))

if not alerts:
    st.info("No alerts for this slate.")
    st.stop()

st.caption(f"{len(alerts)} alert(s) — {_severity_summary(alerts)}")

st.dataframe(
    _alerts_dataframe(alerts),
    hide_index=True,
    width="stretch",
)
