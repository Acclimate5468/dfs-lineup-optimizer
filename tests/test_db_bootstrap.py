"""Bootstrap helper for app DB connections.

Covers ``src/db/migrations.py::bootstrap_database`` — the single entry
point Streamlit pages use after acquiring a connection. The contract
under test:

- Running bootstrap on a fresh empty connection creates the v0 schema
  AND applies all pending migrations.
- Running bootstrap on a DB built from an older shape (``slates`` /
  ``fighters`` created without the Manual Review Gate / Fighter Status
  override columns) promotes it to the current column set without
  losing existing rows.
- Bootstrap is idempotent — running it twice is a no-op the second
  time and does not raise (no "duplicate column" errors).
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.migrations import bootstrap_database


# Pre-Phase-B / pre-Fighter-Status-v1 table shapes — these mirror what a
# user upgrading from an older build will have on disk. Columns added
# later by migration helpers are intentionally absent.
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


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


@pytest.fixture
def fresh_conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def pre_migration_conn():
    """A DB whose ``slates`` and ``fighters`` tables predate the Manual
    Review Gate / Fighter Status v1 override columns. Mirrors what a
    user upgrading from an older build will have on disk."""
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(PRE_MIGRATION_SLATES_SQL)
    c.execute(PRE_MIGRATION_FIGHTERS_SQL)
    c.executemany(
        "INSERT INTO slates (event_name, event_date, salary_csv_status, "
        "salary_row_count) VALUES (?, ?, ?, ?)",
        [
            ("Pre Slate A", "2026-05-20", "validated", 2),
            ("Pre Slate B", None, "unvalidated", 0),
        ],
    )
    c.executemany(
        "INSERT INTO fighters (slate_id, name, salary, status) VALUES (?, ?, ?, ?)",
        [
            (1, "Older Champ", 9000, "active"),
            (1, "Older Dog", 6500, "active"),
        ],
    )
    c.commit()
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Fresh DB path
# ---------------------------------------------------------------------------


def test_bootstrap_creates_schema_with_manual_review_columns(fresh_conn):
    bootstrap_database(fresh_conn)

    slate_cols = _columns(fresh_conn, "slates")
    assert "manual_review_status" in slate_cols
    assert "manual_review_completed_at" in slate_cols


def test_bootstrap_creates_schema_with_fighter_manual_status_columns(fresh_conn):
    bootstrap_database(fresh_conn)

    fighter_cols = _columns(fresh_conn, "fighters")
    assert "manual_status" in fighter_cols
    assert "manual_status_set_at" in fighter_cols


# ---------------------------------------------------------------------------
# Pre-migration DB upgrade path
# ---------------------------------------------------------------------------


def test_bootstrap_promotes_pre_migration_slate_columns(pre_migration_conn):
    cols_before = _columns(pre_migration_conn, "slates")
    assert "manual_review_status" not in cols_before
    assert "manual_review_completed_at" not in cols_before

    bootstrap_database(pre_migration_conn)

    cols_after = _columns(pre_migration_conn, "slates")
    assert "manual_review_status" in cols_after
    assert "manual_review_completed_at" in cols_after


def test_bootstrap_promotes_pre_migration_fighter_columns(pre_migration_conn):
    cols_before = _columns(pre_migration_conn, "fighters")
    assert "manual_status" not in cols_before
    assert "manual_status_set_at" not in cols_before

    bootstrap_database(pre_migration_conn)

    cols_after = _columns(pre_migration_conn, "fighters")
    assert "manual_status" in cols_after
    assert "manual_status_set_at" in cols_after


def test_bootstrap_preserves_existing_rows(pre_migration_conn):
    slates_before = pre_migration_conn.execute(
        "SELECT id, event_name, event_date, salary_csv_status, salary_row_count "
        "FROM slates ORDER BY id"
    ).fetchall()
    fighters_before = pre_migration_conn.execute(
        "SELECT id, slate_id, name, salary, status FROM fighters ORDER BY id"
    ).fetchall()

    bootstrap_database(pre_migration_conn)

    slates_after = pre_migration_conn.execute(
        "SELECT id, event_name, event_date, salary_csv_status, salary_row_count "
        "FROM slates ORDER BY id"
    ).fetchall()
    fighters_after = pre_migration_conn.execute(
        "SELECT id, slate_id, name, salary, status FROM fighters ORDER BY id"
    ).fetchall()
    assert slates_before == slates_after
    assert fighters_before == fighters_after


def test_bootstrap_defaults_existing_slate_rows_to_not_reviewed(pre_migration_conn):
    bootstrap_database(pre_migration_conn)

    rows = pre_migration_conn.execute(
        "SELECT manual_review_status, manual_review_completed_at FROM slates "
        "ORDER BY id"
    ).fetchall()
    assert rows
    for status, completed_at in rows:
        assert status == "not_reviewed"
        assert completed_at is None


def test_bootstrap_defaults_existing_fighter_rows_to_null_override(
    pre_migration_conn,
):
    bootstrap_database(pre_migration_conn)

    rows = pre_migration_conn.execute(
        "SELECT manual_status, manual_status_set_at FROM fighters ORDER BY id"
    ).fetchall()
    assert rows
    for manual_status, manual_status_set_at in rows:
        assert manual_status is None
        assert manual_status_set_at is None


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_bootstrap_is_idempotent_on_fresh_db(fresh_conn):
    bootstrap_database(fresh_conn)
    bootstrap_database(fresh_conn)  # must not raise

    slate_cols = _columns(fresh_conn, "slates")
    fighter_cols = _columns(fresh_conn, "fighters")
    assert "manual_review_status" in slate_cols
    assert "manual_review_completed_at" in slate_cols
    assert "manual_status" in fighter_cols
    assert "manual_status_set_at" in fighter_cols


def test_bootstrap_is_idempotent_on_pre_migration_db(pre_migration_conn):
    bootstrap_database(pre_migration_conn)
    bootstrap_database(pre_migration_conn)  # must not raise on second pass

    slate_cols = _columns(pre_migration_conn, "slates")
    fighter_cols = _columns(pre_migration_conn, "fighters")
    assert "manual_review_status" in slate_cols
    assert "manual_review_completed_at" in slate_cols
    assert "manual_status" in fighter_cols
    assert "manual_status_set_at" in fighter_cols
