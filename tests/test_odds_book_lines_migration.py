"""Migration test for the odds_book_lines provenance table (consensus Slice 5).

``docs/DEVELOPMENT_NOTES.md`` §8 requires a new table to ship a *paired migration* in
``src/db/migrations.py`` (not only the ``schema.py`` CREATE statement), so an
existing on-disk DB created before this slice is promoted in place. This test
isolates that helper: it builds a DB whose ``slates`` table predates
``odds_book_lines``, runs ``apply_pending_migrations`` alone, and asserts the
table now exists and the migration is re-runnable.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.migrations import _ensure_odds_book_lines_table, apply_pending_migrations

PRE_MIGRATION_SLATES_SQL = """
CREATE TABLE slates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


@pytest.fixture
def pre_migration_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(PRE_MIGRATION_SLATES_SQL)
    try:
        yield conn
    finally:
        conn.close()


def test_migration_creates_odds_book_lines_on_existing_db(pre_migration_conn):
    assert not _table_exists(pre_migration_conn, "odds_book_lines")
    apply_pending_migrations(pre_migration_conn)
    assert _table_exists(pre_migration_conn, "odds_book_lines")


def test_migration_is_idempotent(pre_migration_conn):
    apply_pending_migrations(pre_migration_conn)
    # Second pass must be a no-op, not raise (CREATE TABLE IF NOT EXISTS).
    apply_pending_migrations(pre_migration_conn)
    assert _table_exists(pre_migration_conn, "odds_book_lines")


def test_ensure_helper_runs_before_slates_exists():
    # The helper has no partial-fixture guard: SQLite defers the slates FK
    # check, so creating the table before slates exists must not raise.
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_odds_book_lines_table(conn)
        assert _table_exists(conn, "odds_book_lines")
    finally:
        conn.close()


def test_migrated_table_enforces_unique_and_check(pre_migration_conn):
    apply_pending_migrations(pre_migration_conn)
    slate_id = pre_migration_conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC M') RETURNING id"
    ).fetchone()[0]
    pre_migration_conn.execute(
        "INSERT INTO odds_book_lines (slate_id, fighter_name_raw, "
        "fighter_name_normalized, book, american_odds, source, captured_at) "
        "VALUES (?, 'Foo', 'foo', 'DraftKings', -150, 'bestfightodds', "
        "'2026-05-20T00:00:00Z')",
        (slate_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        pre_migration_conn.execute(
            "INSERT INTO odds_book_lines (slate_id, fighter_name_raw, "
            "fighter_name_normalized, book, american_odds, source, captured_at) "
            "VALUES (?, 'Foo', 'foo', 'DraftKings', -120, 'paste', "
            "'2026-05-20T00:00:00Z')",
            (slate_id,),
        )
