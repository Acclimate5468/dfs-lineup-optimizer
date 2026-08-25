"""Projections (v1 preview) — read-only Phase D page.

Implements ``docs/PROJECTION_V1_DESIGN.md`` §8 Phase D: a Streamlit page
that renders ``project_slate(...)`` output for a selected slate.

Hard contract (design §5, §6, §8; ``docs/DEVELOPMENT_NOTES.md`` §11):

- Read-only. No buttons, forms, callbacks, or write actions.
- No projection persistence. The page calls ``project_slate`` per render
  and discards the result on the next interaction.
- ``effective_status`` is not consulted (design §2 and
  ``ODDS_PERSISTENCE_DESIGN.md`` §15.11 risk #7).
- Downstream consumers (alerts, optimizer, exports) do not yet read these
  projections; this page is a preview only.
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
from src.projections.slate_projection_service import (  # noqa: E402
    PROJECTION_MODE_V0,
    PROJECTION_MODE_V2,
    FighterSlateProjection,
    project_slate,
)

st.set_page_config(page_title="Projections — DK Lineup Lab", layout="wide")
lock_to_build_page("Projections")
st.title("Projections (v1 preview)")

st.warning(
    "Read-only preview. No projections are persisted. "
    "`effective_status` is not consulted yet, so reject/accept overrides "
    "do not affect these numbers. The salary importer still requires a "
    "real DK UFC Classic salary CSV smoke validation before it is "
    "considered complete. Alerts, the optimizer, and exports do NOT yet "
    "consume these projections."
)


# Read-only projection-mode toggle (v2 design §15 Phase D). v0 is the default
# production engine; v2 ("v2_finish") is EXPERIMENTAL — not validated, not
# promoted, and not consumed by the optimizer / alerts / exports. The control is
# session-only (Streamlit widget state) and writes nothing to the DB.
PROJECTION_MODE_LABELS = {
    PROJECTION_MODE_V0: "v0 formula (default)",
    PROJECTION_MODE_V2: "v2 finish-aware (Experimental)",
}


def _slate_label(slate: SlateRecord) -> str:
    return (
        f"#{slate.id} — {slate.event_name}"
        + (f" ({slate.event_date})" if slate.event_date else "")
    )


def _format_points(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def _format_prob(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.3f}"


def _format_branch_count(branches: tuple) -> str:
    return str(len(branches)) if branches else "—"


def _projections_dataframe(
    projections: list[FighterSlateProjection],
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Fighter": p.fighter_name,
                "Status": p.projection_status,
                "Projected DK pts": _format_points(p.projected_dk_points),
                "Missing inputs": ", ".join(p.missing_inputs),
                "Notes": " · ".join(p.notes),
            }
            for p in projections
        ],
        columns=["Fighter", "Status", "Projected DK pts", "Missing inputs", "Notes"],
    )


def _v2_projections_dataframe(
    projections: list[FighterSlateProjection],
) -> pd.DataFrame:
    """Experimental v2 (finish-aware) view: the v0 columns plus the additive
    v2-only fields (mode, best/worst branch conditional means, the Tier-0 finish
    signal, and the outcome-branch count). v2-only cells are ``—`` for rows that
    are ``missing_inputs`` / ``non_projectable`` — no projection value is
    invented (v2 design §9). Read-only; nothing here is persisted or promoted."""
    return pd.DataFrame(
        [
            {
                "Fighter": p.fighter_name,
                "Status": p.projection_status,
                "Mode": p.projection_mode,
                "Projected DK pts (experimental)": _format_points(
                    p.projected_dk_points
                ),
                "Best branch pts": _format_points(p.best_branch_pts),
                "Worst branch pts": _format_points(p.worst_branch_pts),
                "P(fight finishes)": _format_prob(p.p_fight_finishes),
                "Finish share": _format_prob(p.finish_share),
                "Branches": _format_branch_count(p.outcome_branches),
                "Missing inputs": ", ".join(p.missing_inputs),
                "Notes": " · ".join(p.notes),
            }
            for p in projections
        ],
        columns=[
            "Fighter",
            "Status",
            "Mode",
            "Projected DK pts (experimental)",
            "Best branch pts",
            "Worst branch pts",
            "P(fight finishes)",
            "Finish share",
            "Branches",
            "Missing inputs",
            "Notes",
        ],
    )


def _status_summary(projections: list[FighterSlateProjection]) -> str:
    counts: dict[str, int] = {}
    for p in projections:
        counts[p.projection_status] = counts.get(p.projection_status, 0) + 1
    parts = [
        f"{counts.get(status, 0)} {status}"
        for status in ("ok", "missing_inputs", "non_projectable")
    ]
    return " · ".join(parts)


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
    key="projections_slate_id",
)

projection_mode = st.radio(
    "Projection mode",
    options=[PROJECTION_MODE_V0, PROJECTION_MODE_V2],
    format_func=lambda mode: PROJECTION_MODE_LABELS[mode],
    index=0,
    horizontal=True,
    key="projections_mode",
)

if projection_mode == PROJECTION_MODE_V2:
    st.warning(
        "⚠️ EXPERIMENTAL — v2 finish-aware projections are NOT validated and "
        "NOT promoted. v0 remains the default production engine; the optimizer, "
        "alerts, and exports continue to use v0 regardless of this preview. "
        "Choosing a mode is session-only and persists nothing."
    )

projections = project_slate(
    conn, int(slate_choice), projection_mode=projection_mode
)

if not projections:
    st.info(
        "No active fighters on this slate yet. Import salaries on Build, "
        "Step 1 to populate fighters."
    )
    st.stop()

st.caption(
    f"{len(projections)} fighter(s) — {_status_summary(projections)}"
)

projection_table = (
    _v2_projections_dataframe(projections)
    if projection_mode == PROJECTION_MODE_V2
    else _projections_dataframe(projections)
)

st.dataframe(
    projection_table,
    hide_index=True,
    width="stretch",
)
