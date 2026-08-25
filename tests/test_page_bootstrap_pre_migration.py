"""AppTest coverage for the page DB bootstrap path.

Pages that talk to SQLite (``app/pages/01_slate_setup.py`` through
``app/pages/09_projections.py``) call
``src/db/migrations.py::bootstrap_database`` after opening a connection
so a DB created at an older shape (``slates`` / ``fighters`` lacking
the Manual Review Gate / Fighter Status v1 override columns) is
promoted to the current column set before any read or write.

The Fighter Status (Phase D) and Manual Review (Phase D) pages are the
ones whose reads explicitly depend on those migration-only columns —
``fighters.manual_status`` / ``manual_status_set_at`` and
``slates.manual_review_status`` / ``manual_review_completed_at``
respectively. This test loads each page against a seeded pre-migration
DB file and asserts page load completes without raising.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIGHTER_STATUS_PAGE = REPO_ROOT / "app" / "pages" / "04_fighter_status.py"
MANUAL_REVIEW_PAGE = REPO_ROOT / "app" / "pages" / "06_manual_review.py"


PRE_MIGRATION_SLATES_SQL = """
CREATE TABLE slates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL DEFAULT 'UFC',
    contest_type TEXT NOT NULL DEFAULT 'CLASSIC',
    event_name TEXT NOT NULL,
    event_date TEXT,
    dk_draft_group_id TEXT,
    salary_csv_status TEXT NOT NULL DEFAULT 'unvalidated',
    salary_row_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

PRE_MIGRATION_FIGHTERS_SQL = """
CREATE TABLE fighters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
    dk_player_id TEXT,
    name TEXT NOT NULL,
    salary INTEGER NOT NULL,
    position TEXT NOT NULL DEFAULT 'F',
    team_abbrev TEXT,
    opponent_abbrev TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(slate_id, name)
)
"""


@pytest.fixture
def pre_migration_db(tmp_path, monkeypatch):
    """A persisted SQLite DB whose ``slates`` and ``fighters`` tables
    predate the migration-only override columns. ``DB_PATH`` is
    monkeypatched so ``get_connection()`` opens this exact file."""
    db_path = tmp_path / "pre_migration.sqlite3"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(PRE_MIGRATION_SLATES_SQL)
        conn.execute(PRE_MIGRATION_FIGHTERS_SQL)
        conn.execute(
            "INSERT INTO slates (event_name, event_date, salary_csv_status, "
            "salary_row_count) VALUES (?, ?, ?, ?)",
            ("UFC Pre-Migration", "2026-05-22", "validated", 2),
        )
        conn.execute(
            "INSERT INTO fighters (slate_id, name, salary, status) "
            "VALUES (?, ?, ?, ?)",
            (1, "Older Champ", 9000, "active"),
        )
        conn.execute(
            "INSERT INTO fighters (slate_id, name, salary, status) "
            "VALUES (?, ?, ?, ?)",
            (1, "Older Dog", 6500, "active"),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
    finally:
        conn.close()


def test_fighter_status_page_loads_against_pre_migration_db(pre_migration_db):
    """The Fighter Status page reads ``fighters.manual_status`` /
    ``manual_status_set_at``; on a pre-migration DB it would crash
    without the page-level bootstrap call."""
    cols_before = _columns(pre_migration_db, "fighters")
    assert "manual_status" not in cols_before
    assert "manual_status_set_at" not in cols_before

    at = AppTest.from_file(str(FIGHTER_STATUS_PAGE), default_timeout=30)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    cols_after = _columns(pre_migration_db, "fighters")
    assert "manual_status" in cols_after
    assert "manual_status_set_at" in cols_after


def test_manual_review_page_loads_against_pre_migration_db(pre_migration_db):
    """The Manual Review page reads ``slates.manual_review_status`` /
    ``manual_review_completed_at``; on a pre-migration DB it would
    crash without the page-level bootstrap call."""
    cols_before = _columns(pre_migration_db, "slates")
    assert "manual_review_status" not in cols_before
    assert "manual_review_completed_at" not in cols_before

    at = AppTest.from_file(str(MANUAL_REVIEW_PAGE), default_timeout=30)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    cols_after = _columns(pre_migration_db, "slates")
    assert "manual_review_status" in cols_after
    assert "manual_review_completed_at" in cols_after
