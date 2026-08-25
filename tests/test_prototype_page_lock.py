"""AppTest coverage for the prototype page lock (Build-only surface).

The product surface is the two-step Build page. The legacy ``NN_*.py`` detail
pages are kept in the repo but locked out of the prototype experience by
``app.prototype_mode.lock_to_build_page`` (called right after each page's
``st.set_page_config``), gated on ``DK_LAB_PROTOTYPE_LOCK`` (default on).

These tests pin the lock with the flag **enabled** (the suite-wide
``conftest._disable_prototype_lock`` turns it off, so each test here re-enables
it explicitly). They assert that every legacy page, when locked:

- raises no exception,
- shows the minimal "Advanced page disabled in prototype mode" notice,
- hides the sidebar / multipage navigation,
- renders only the Back-to-Build button (none of its own legacy workflow UI).

They also pin that the Build page and the entrypoint redirect are **not**
locked, and unit-check ``prototype_lock_enabled`` against the env flag.

This is a navigation/presentation lock only — no service, schema, optimizer,
odds, salary-import, or Manual-Review behavior is exercised or changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.prototype_mode import prototype_lock_enabled

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGES_DIR = REPO_ROOT / "app" / "pages"
ENTRYPOINT = REPO_ROOT / "app" / "streamlit_app.py"

_BACK_BTN_LABEL = "← Back to Build"
_DISABLED_PHRASE = "Advanced page disabled in prototype mode"
_BUILD_BTN_LABEL = "Build research lineups"

# Legacy detail pages that stay fully locked (everything in app/pages/ except
# the Build page and the two required setup pages the Build gate links to).
# Fight Groups + Odds are intentionally absent — see ``_REACHABLE_SETUP_PAGES``.
_LEGACY_PAGES = (
    "01_slate_setup.py",
    "04_fighter_status.py",
    "05_alerts.py",
    "06_manual_review.py",
    "07_optimizer.py",
    "08_export_run_log.py",
    "09_projections.py",
    "10_source_registry.py",
)

# The two required setup pages reachable from the Build gate even when the lock
# is on (``allow_in_prototype=True``): the page renders its real review UI (its
# own title), not the disabled notice, with the sidebar/nav still hidden and an
# explicit Back-to-Build control. Paired with a ``st.title`` marker unique to
# each page's real body.
_REACHABLE_SETUP_PAGES = (
    ("02_fight_groups.py", "Fight Groups"),
    ("03_odds.py", "Odds"),
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point the DB seam at a temp file (mirrors the other page tests). With
    the lock on the legacy pages stop before any DB access, but the Build page
    / entrypoint tests below do read it."""
    db_path = tmp_path / "prototype_lock.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


@pytest.fixture
def lock_on(monkeypatch):
    """Re-enable the prototype lock for a test (the suite default is off)."""
    monkeypatch.setenv("DK_LAB_PROTOTYPE_LOCK", "1")


def _markdown_blob(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown)


# ---------------------------------------------------------------------------
# Every legacy page is locked when the flag is on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page_file", _LEGACY_PAGES)
def test_legacy_page_is_locked_when_enabled(page_file, isolated_db, lock_on):
    at = AppTest.from_file(str(PAGES_DIR / page_file), default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # The minimal disabled notice is shown.
    warnings = " ".join(w.value for w in at.warning)
    assert _DISABLED_PHRASE in warnings, warnings

    # Sidebar / multipage nav is hidden (the same selectors the Build page uses).
    assert "stSidebarNav" in _markdown_blob(at), "nav-hiding CSS missing"

    # Only the Back-to-Build button renders — none of the page's own legacy UI.
    assert [b.label for b in at.button] == [_BACK_BTN_LABEL], [
        b.label for b in at.button
    ]


def test_locked_optimizer_suppresses_generate_button(isolated_db, lock_on):
    """A concrete legacy-UI marker (the Optimizer's Generate button) is gone
    when the page is locked."""
    at = AppTest.from_file(str(PAGES_DIR / "07_optimizer.py"), default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    labels = [b.label for b in at.button]
    assert "Generate Lineups" not in labels, labels
    assert labels == [_BACK_BTN_LABEL], labels


# ---------------------------------------------------------------------------
# The two required setup pages stay reachable when the lock is on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page_file,title_marker", _REACHABLE_SETUP_PAGES)
def test_required_setup_page_reachable_when_locked(
    page_file, title_marker, isolated_db, lock_on
):
    """Fight Groups + Odds are linked from the Build gate, so they render their
    real UI even with the lock on: no disabled notice, the page's own title is
    present, the sidebar/nav is still hidden, and an explicit Back-to-Build
    control is offered."""
    at = AppTest.from_file(str(PAGES_DIR / page_file), default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # Not the disabled notice — the real page body rendered.
    warnings = " ".join(w.value for w in at.warning)
    assert _DISABLED_PHRASE not in warnings, warnings

    # The real page's own title is present.
    titles = " ".join(t.value for t in at.title)
    assert title_marker in titles, titles

    # Sidebar / multipage nav is still hidden (same selectors as the lock).
    assert "stSidebarNav" in _markdown_blob(at), "nav-hiding CSS missing"

    # An explicit Back-to-Build control is offered.
    assert _BACK_BTN_LABEL in [b.label for b in at.button], [
        b.label for b in at.button
    ]


# ---------------------------------------------------------------------------
# Lock OFF (suite default) renders the real legacy page
# ---------------------------------------------------------------------------


def test_legacy_page_renders_normally_when_lock_disabled(isolated_db):
    """With the lock off (the suite default), a legacy page renders its real UI
    — proving the lock is a clean opt-out, not a hard edit to the page body."""
    at = AppTest.from_file(str(PAGES_DIR / "07_optimizer.py"), default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    titles = " ".join(t.value for t in at.title)
    assert "Optimizer (v1)" in titles, titles
    warnings = " ".join(w.value for w in at.warning)
    assert _DISABLED_PHRASE not in warnings, warnings


# ---------------------------------------------------------------------------
# The Build page and the entrypoint redirect are never locked
# ---------------------------------------------------------------------------


def test_build_page_not_locked_even_when_flag_on(isolated_db, lock_on):
    at = AppTest.from_file(str(PAGES_DIR / "00_build.py"), default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    warnings = " ".join(w.value for w in at.warning)
    assert _DISABLED_PHRASE not in warnings, warnings
    assert _BUILD_BTN_LABEL in [b.label for b in at.button], [
        b.label for b in at.button
    ]


def test_entrypoint_redirects_to_build_even_when_flag_on(isolated_db, lock_on):
    """The root entrypoint redirects to the (unlocked) Build page regardless of
    the prototype-lock flag."""
    at = AppTest.from_file(str(ENTRYPOINT), default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    warnings = " ".join(w.value for w in at.warning)
    assert _DISABLED_PHRASE not in warnings, warnings
    assert _BUILD_BTN_LABEL in [b.label for b in at.button], [
        b.label for b in at.button
    ]


# ---------------------------------------------------------------------------
# prototype_lock_enabled() flag parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("on", True),
        ("true", True),
        ("yes", True),
        ("anything", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", False),
        ("  OFF  ", False),
    ],
)
def test_prototype_lock_enabled_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("DK_LAB_PROTOTYPE_LOCK", value)
    assert prototype_lock_enabled() is expected


def test_prototype_lock_enabled_default_is_on(monkeypatch):
    monkeypatch.delenv("DK_LAB_PROTOTYPE_LOCK", raising=False)
    assert prototype_lock_enabled() is True
