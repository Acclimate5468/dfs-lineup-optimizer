"""UFC DFS Lineup Optimizer — app entrypoint (Build-page redirect).

``streamlit run app/streamlit_app.py`` (the default entrypoint at
localhost:8501) opens directly into the prototype-style two-step Build page
(``app/pages/00_build.py``). After ``set_page_config`` this script does nothing
but ``st.switch_page`` to Build, which stops executing the rest of this script
and renders the Build page instead.

This is a navigation-only entrypoint: it reads/writes no database and changes
no business logic (services, schema, optimizer, odds, salary import, Manual
Review, exports are all untouched). The old Lineup Command Center dashboard that
once lived in this file was removed once the Build page became the primary
product surface — it was unreachable after the redirect below.

Run: streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

st.set_page_config(
    page_title="UFC DFS Lineup Optimizer",
    page_icon=":fire:",
    layout="wide",
)

# Open the app directly into the prototype-style two-step Build page.
# ``st.switch_page`` stops executing the rest of this script and renders the
# Build page instead. Navigation-only — no service / schema / optimizer / odds /
# salary / Manual-Review behavior is touched here.
st.switch_page("pages/00_build.py")
