"""Schema + migration coverage for the DK ``fighters.game_info`` column.

B2 of ``docs/DK_GAME_INFO_PAIRING_DESIGN.md`` (§2.1, §2.2; test plan tests
1–2). Mirrors the migration coverage in
``tests/test_fighter_status_repository.py`` and the schema-shape style of
``tests/test_odds_persistence_schema.py``.

The repository read/write behaviour for ``game_info`` (insert, update,
backfill, unchanged accounting, ``list_for_slate`` round-trip) is covered
alongside the other ``FighterRepository`` cases in
``tests/test_fighter_repository.py``.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.migrations import (
    _ensure_fighter_game_info_column,
    apply_pending_migrations,
)
from src.db.schema import apply_schema


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    """Return {column_name: (type, notnull, dflt_value, pk)} for a table."""
    return {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    apply_schema(c)
    try:
        yield c
    finally:
        c.close()


# A ``fighters`` table at the Fighter Status v1 shape (manual_status present)
# but predating the Game Info feature — the realistic upgrade path a user on
# disk will have. The FK to ``slates`` is dropped so the fixture stands alone.
PRE_GAME_INFO_FIGHTERS_SQL = """
CREATE TABLE fighters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slate_id INTEGER NOT NULL,
    dk_player_id TEXT,
    name TEXT NOT NULL,
    salary INTEGER NOT NULL,
    position TEXT NOT NULL DEFAULT 'F',
    team_abbrev TEXT,
    opponent_abbrev TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    manual_status TEXT,
    manual_status_set_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(slate_id, name)
)
"""


@pytest.fixture
def pre_game_info_conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(PRE_GAME_INFO_FIGHTERS_SQL)
    c.executemany(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, ?)",
        [
            (1, "Pre Jon", 9000, "active"),
            (1, "Pre Jane", 8500, "inactive"),
        ],
    )
    c.commit()
    try:
        yield c
    finally:
        c.close()


# --- fresh schema -------------------------------------------------------


def test_apply_schema_creates_nullable_game_info_column(conn):
    cols = _columns(conn, "fighters")
    assert "game_info" in cols
    # (type, notnull, dflt, pk) — notnull index 1; nullable means 0.
    assert cols["game_info"][1] == 0


def test_apply_schema_idempotent_on_game_info(conn):
    apply_schema(conn)
    assert "game_info" in _columns(conn, "fighters")


# --- migration on a pre-Game-Info DB (test 1) ---------------------------


def test_migration_adds_game_info_column(pre_game_info_conn):
    cols_before = _columns(pre_game_info_conn, "fighters")
    assert "game_info" not in cols_before

    apply_pending_migrations(pre_game_info_conn)

    cols_after = _columns(pre_game_info_conn, "fighters")
    assert "game_info" in cols_after
    assert cols_after["game_info"][1] == 0  # nullable


def test_migration_preserves_rows_and_defaults_game_info_null(
    pre_game_info_conn,
):
    before = pre_game_info_conn.execute(
        "SELECT id, name, salary, status FROM fighters ORDER BY id"
    ).fetchall()

    apply_pending_migrations(pre_game_info_conn)

    after = pre_game_info_conn.execute(
        "SELECT id, name, salary, status FROM fighters ORDER BY id"
    ).fetchall()
    assert before == after
    game_infos = pre_game_info_conn.execute(
        "SELECT game_info FROM fighters"
    ).fetchall()
    assert game_infos  # sanity
    assert all(gi is None for (gi,) in game_infos)


def test_migration_is_idempotent(pre_game_info_conn):
    apply_pending_migrations(pre_game_info_conn)
    # Second pass must not raise ("duplicate column name") and leaves the
    # column in place.
    apply_pending_migrations(pre_game_info_conn)
    assert "game_info" in _columns(pre_game_info_conn, "fighters")


def test_migration_is_noop_on_fresh_schema(conn):
    """Running the migration set on a DB already at the current shape must
    not raise and must not disturb a seeded row."""
    conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC NOOP')"
    )
    conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status, game_info) "
        "VALUES (1, 'Already Migrated', 8000, 'active', 'A@B 05/22/2026')"
    )
    conn.commit()
    before = conn.execute(
        "SELECT id, name, salary, status, game_info FROM fighters"
    ).fetchall()

    apply_pending_migrations(conn)

    after = conn.execute(
        "SELECT id, name, salary, status, game_info FROM fighters"
    ).fetchall()
    assert before == after


# --- partial-fixture safety (test 2) ------------------------------------


def test_ensure_game_info_column_noop_when_fighters_absent():
    """``_ensure_fighter_game_info_column`` must be a no-op when ``fighters``
    does not yet exist — a test seed may run migrations before
    ``apply_schema`` creates the table (§2.2)."""
    c = sqlite3.connect(":memory:")
    try:
        # No tables at all.
        assert (
            c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='fighters'"
            ).fetchone()
            is None
        )
        # Must not raise.
        _ensure_fighter_game_info_column(c)
        # Still no fighters table — the helper did not create one.
        assert (
            c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='fighters'"
            ).fetchone()
            is None
        )
    finally:
        c.close()
