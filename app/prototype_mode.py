"""Prototype-mode page lock for the legacy detail pages.

The product's user-facing surface is the two-step **Build** page
(``app/pages/00_build.py``). The legacy ``NN_*.py`` detail pages (Slate Setup,
Fight Groups, Odds, …) are kept in the repo for reference / reuse but are *not*
part of the prototype experience: their multipage sidebar nav lets a user wander
off the Build surface, and landing on one directly exposes the old workflow UI.

:func:`lock_to_build_page` is called by every legacy page immediately after its
``st.set_page_config(...)``. When the prototype lock is enabled it:

1. hides the sidebar / multipage navigation (so the page exposes no nav), and
2. replaces the page body with a minimal "advanced page disabled in prototype
   mode" notice plus an explicit **Back to Build** button, then
3. ``st.stop()``s so none of the legacy workflow UI renders.

The two **required setup pages** the Build gate links to — Fight Groups and
Odds — opt out of the body replacement via ``allow_in_prototype=True``. For
those the lock still hides the sidebar / nav and adds an explicit **Back to
Build** control, but it does *not* stop the page: it renders its real review UI
so the user can resolve the exact blocking fix the Build gate sent them to.
Every other legacy page stays fully locked. This is the only navigation path
off the Build surface in the prototype, so it is deliberately narrow.

This is **navigation / presentation only**: it imports no service, reads/writes
no database, and changes no business logic (optimizer, odds, salary import,
Manual Review, schema are all untouched). ``st.switch_page`` only fires on an
explicit button click — never on load — so simply loading a locked page is inert
apart from the notice.

The lock is gated on the ``DK_LAB_PROTOTYPE_LOCK`` environment variable
(default **on**), mirroring the existing ``DK_LAB_DB_PATH`` env convention. A dev
or a test can set ``DK_LAB_PROTOTYPE_LOCK=0`` to render the legacy pages as-is;
the project's test suite does this session-wide (``tests/conftest.py``) so the
legacy-page AppTests keep exercising the real UI.
"""

from __future__ import annotations

import os

import streamlit as st

# The Build page, addressed relative to the app entrypoint's directory (the way
# ``streamlit_app.py`` already addresses it). Only used inside the click handler.
_BUILD_PAGE = "pages/00_build.py"

_ENV_FLAG = "DK_LAB_PROTOTYPE_LOCK"
_DISABLED_VALUES = {"0", "false", "no", "off", ""}

# Hide the whole sidebar + multipage nav + the collapsed/expand controls, so a
# locked legacy page exposes no navigation at all (mirrors the Build page's own
# sidebar-hiding rules in ``app/pages/00_build.py``). Scoped to this render only.
_HIDE_NAV_CSS = """
<style>
[data-testid="stSidebar"],
[data-testid="stSidebarNav"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="stSidebarCollapseButton"],
[data-testid="stExpandSidebarButton"],
[data-testid="collapsedControl"] {
  display:none !important;
}
</style>
"""


def prototype_lock_enabled() -> bool:
    """True when the prototype lock is active (the default).

    Disabled only when ``DK_LAB_PROTOTYPE_LOCK`` is explicitly set to a falsey
    value (``0`` / ``false`` / ``no`` / ``off`` / empty). Any other value — and
    the unset default — means locked.
    """
    return os.getenv(_ENV_FLAG, "1").strip().lower() not in _DISABLED_VALUES


def lock_to_build_page(
    page_name: str = "This page", *, allow_in_prototype: bool = False
) -> None:
    """Lock a legacy detail page to the Build surface.

    Call once, immediately after ``st.set_page_config(...)`` on every legacy
    page. When the lock is enabled this renders the disabled notice + the
    Back-to-Build button and never returns (it ends in ``st.stop()``); the only
    way off the page is the explicit button, which ``st.switch_page``-s to Build
    on click. When the lock is disabled it returns immediately and the page
    renders normally.

    ``allow_in_prototype`` narrowly exempts the *required setup* pages the Build
    gate links to (Fight Groups, Odds). For those the lock still hides the
    sidebar / multipage nav and adds an explicit **← Back to Build** control, but
    it returns instead of stopping, so the page renders its real review UI and
    the user can fix the blocking issue the Build gate sent them to. The
    ``st.switch_page`` fires only on an explicit click — never on load.
    """
    if not prototype_lock_enabled():
        return

    st.markdown(_HIDE_NAV_CSS, unsafe_allow_html=True)

    if allow_in_prototype:
        # Required setup page: keep the sidebar/nav hidden, offer an explicit way
        # back to Build, then let the real page body render (no st.stop()).
        if st.button(
            "← Back to Build",
            key="prototype_back_to_build",
            type="secondary",
        ):
            st.switch_page(_BUILD_PAGE)
        return

    st.warning("Advanced page disabled in prototype mode")
    st.caption(
        f"{page_name} is part of the advanced workflow and is turned off in the "
        "prototype. UFC DFS Lineup Optimizer is built around the single Build "
        "surface — upload salaries, check odds, and build lineups there."
    )
    if st.button(
        "← Back to Build",
        key="prototype_back_to_build",
        type="primary",
    ):
        st.switch_page(_BUILD_PAGE)
    st.stop()
