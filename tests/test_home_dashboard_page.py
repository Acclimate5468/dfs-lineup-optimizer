"""AppTest coverage for the app entrypoint → Build redirect (B7 cutover).

``app/streamlit_app.py`` is the default entrypoint (localhost:8501). As of the
B7 cutover it immediately ``st.switch_page``-redirects to the prototype-style
two-step Build page (``app/pages/00_build.py``), so opening the app lands on
the Build experience, not the old Lineup Command Center.

These tests load ``app/streamlit_app.py`` via ``AppTest`` and assert that the
landing surface is the Build page (its markers present, the Command Center
surface absent), that the redirect carries the seeded active slate, and that
the entrypoint load stays read-only and runs neither the optimizer nor the
export builder (``docs/DEVELOPMENT_NOTES.md`` §11).

The Build page's own rendering / write-path behavior is covered exhaustively
in ``tests/test_build_page.py``; this module only pins the cutover. (It
replaces the former Lineup Command Center rendering tests, which pinned a page
that is no longer reached on load.)

Seed helpers mirror ``tests/test_build_page.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import get_connection
from src.db.repositories import (
    FightGroupRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching_service import (
    recompute_and_replace_match_results,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = REPO_ROOT / "app" / "streamlit_app.py"

_BUILD_BTN = "Build research lineups"
# Surfaces unique to the old Lineup Command Center home dashboard — none of
# these may appear on the landing page after the redirect.
_COMMAND_CENTER_MARKERS = (
    "Build pipeline",
    "Slate signals",
    "Next recommended action",
    "Workflow checklist",
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "home_dashboard_page.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _open_page() -> AppTest:
    """Load the entrypoint; the B7 redirect lands AppTest on the Build page."""
    at = AppTest.from_file(str(PAGE_PATH), default_timeout=60)
    at.run()
    return at


def _insert_fighter(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    name: str,
    salary: int = 8000,
) -> int:
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, 'active')",
        (int(slate_id), name, int(salary)),
    )
    conn.commit()
    return int(cur.lastrowid)


def _save_odds_row(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    fighter_name_raw: str,
    american_odds: int,
    captured_at: str,
) -> None:
    OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source="manual",
        captured_at=captured_at,
    )


FIGHTS: tuple[tuple[str, str], ...] = (
    ("Aldo", "Vera"),
    ("Holloway", "Topuria"),
    ("Pereira", "Hill"),
    ("Adesanya", "Strickland"),
    ("Volkanovski", "Makhachev"),
    ("Oliveira", "Gaethje"),
)


def _seed_groups_no_odds(name: str = "UFC NoOdds") -> int:
    """Salary + confirmed fight groups, but no odds at all (a representative
    seeded slate for the redirect to resolve as active)."""
    conn = get_connection()
    try:
        apply_schema(conn)
        sid = SlateRepository(conn).create(
            event_name=name,
            salary_csv_status="validated",
            salary_row_count=2 * len(FIGHTS),
        ).id
        for fav, dog in FIGHTS:
            _insert_fighter(conn, slate_id=sid, name=fav, salary=8500)
            _insert_fighter(conn, slate_id=sid, name=dog, salary=7500)
        for fav, dog in FIGHTS:
            fg = FightGroupRepository(conn).create(
                slate_id=sid,
                fighter_1_name=fav,
                fighter_2_name=dog,
                scheduled_rounds=3,
            )
            FightGroupRepository(conn).update_status(fg.id, "confirmed")
        return sid
    finally:
        conn.close()


def _seed_structurally_clean(name: str = "UFC Clean", *, reviewed: bool) -> int:
    """Full coverage + confirmed groups; ``reviewed`` True → ready gate. Used
    to prove the entrypoint load stays read-only even on a build-ready slate."""
    conn = get_connection()
    try:
        apply_schema(conn)
        sid = SlateRepository(conn).create(
            event_name=name,
            event_date="2026-05-31",
            salary_csv_status="validated",
            salary_row_count=2 * len(FIGHTS),
        ).id
        for fav, dog in FIGHTS:
            _insert_fighter(conn, slate_id=sid, name=fav, salary=8500)
            _insert_fighter(conn, slate_id=sid, name=dog, salary=7500)
        for fav, dog in FIGHTS:
            fg = FightGroupRepository(conn).create(
                slate_id=sid,
                fighter_1_name=fav,
                fighter_2_name=dog,
                scheduled_rounds=3,
            )
            FightGroupRepository(conn).update_status(fg.id, "confirmed")
        for i, (fav, dog) in enumerate(FIGHTS):
            _save_odds_row(
                conn,
                slate_id=sid,
                fighter_name_raw=fav,
                american_odds=-160,
                captured_at=f"2026-05-20T00:00:{2 * i:02d}Z",
            )
            _save_odds_row(
                conn,
                slate_id=sid,
                fighter_name_raw=dog,
                american_odds=+140,
                captured_at=f"2026-05-20T00:00:{2 * i + 1:02d}Z",
            )
        recompute_and_replace_match_results(conn, sid)
        if reviewed:
            SlateRepository(conn).set_manual_review_reviewed(sid)
        return sid
    finally:
        conn.close()


_SNAPSHOT_TABLES = (
    "slates",
    "fighters",
    "fight_groups",
    "odds_rows",
    "odds_match_results",
    "manual_match_overrides",
)


def _db_snapshot() -> dict[str, list[tuple]]:
    conn = get_connection()
    try:
        apply_schema(conn)
        snap: dict[str, list[tuple]] = {}
        for table in _SNAPSHOT_TABLES:
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            snap[table] = [tuple(r) for r in rows]
        return snap
    finally:
        conn.close()


def _text_blob(at: AppTest) -> str:
    parts: list[str] = []
    parts.extend(m.value for m in at.markdown)
    parts.extend(c.value for c in at.caption)
    parts.extend(i.value for i in at.info)
    parts.extend(w.value for w in at.warning)
    parts.extend(s.value for s in at.subheader)
    parts.extend(t.value for t in at.title)
    return " ".join(parts)


def _button_labels(at: AppTest) -> list[str]:
    return [b.label for b in at.button]


# ---------------------------------------------------------------------------
# Redirect lands on the Build page (not the Command Center)
# ---------------------------------------------------------------------------


def test_entrypoint_lands_on_build_empty_db(isolated_db):
    """Opening localhost:8501 (empty DB) redirects to the Build page, not the
    old Command Center."""
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    # Build page markers.
    assert "Lineup Lab" in blob
    assert "Load both inputs to build." in blob  # not-started build status
    assert _BUILD_BTN in _button_labels(at), _button_labels(at)
    # Old Command Center surface is gone from the landing page.
    for marker in _COMMAND_CENTER_MARKERS:
        assert marker not in blob, f"Command Center surface {marker!r} on landing"


def test_entrypoint_lands_on_build_with_slate(isolated_db):
    """With a seeded slate, the landing page is still the two-step Build
    canvas (Step 1 salary + Step 2 odds cards), not the Command Center."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    assert "Lineup Lab" in blob
    assert "DraftKings salary" in blob  # Step 1 card
    assert "Odds checker" in blob  # Step 2 card
    assert _BUILD_BTN in _button_labels(at), _button_labels(at)
    for marker in _COMMAND_CENTER_MARKERS:
        assert marker not in blob, f"Command Center surface {marker!r} on landing"


def test_redirect_carries_active_slate(isolated_db):
    """The Build page reached via the redirect resolves + stores the active
    slate in the shared session key."""
    sid = _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["active_slate_id"] == sid


# ---------------------------------------------------------------------------
# Read-only invariants on the entrypoint load (docs/DEVELOPMENT_NOTES.md §11)
# ---------------------------------------------------------------------------


def test_entrypoint_load_is_read_only(isolated_db):
    _seed_structurally_clean(reviewed=True)
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _db_snapshot() == before, (
        "entrypoint redirect + Build load must be read-only (§11)"
    )


def test_entrypoint_runs_no_optimizer_or_export_on_load(isolated_db, monkeypatch):
    """The entrypoint redirect (and the Build page it lands on) must never run
    the solver or the export builder on load (design §11.3; docs/DEVELOPMENT_NOTES.md §11)."""

    def _boom(*args, **kwargs):  # pragma: no cover - only on regression
        raise AssertionError("entrypoint/build load must not run this (§11)")

    monkeypatch.setattr(
        "src.optimizer.optimizer_service.run_optimizer", _boom, raising=False
    )
    monkeypatch.setattr(
        "src.exports.export_service.build_run_log", _boom, raising=False
    )

    _seed_structurally_clean(reviewed=True)
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    # No lineup / preview tables render on a read-only landing load.
    assert len(at.dataframe) == 0
