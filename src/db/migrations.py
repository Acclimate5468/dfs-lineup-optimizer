"""Migrations entry point.

v0 still relies on ``apply_schema`` for fresh databases — every CREATE
TABLE statement is ``IF NOT EXISTS`` so calling it on an existing DB is
a no-op for already-defined tables. For schema additions that need to
land on an *existing* DB (column adds, index adds) we run
``apply_pending_migrations`` after ``apply_schema``; each migration step
is idempotent so re-running has no side effect.
"""

from __future__ import annotations

import sqlite3

from src.db.connection import get_connection
from src.db.schema import apply_schema


def apply_pending_migrations(conn: sqlite3.Connection) -> None:
    """Apply post-CREATE schema migrations idempotently.

    Each helper here MUST be a no-op on a DB that already has the target
    shape so re-running the migration set on a fresh DB (where
    ``apply_schema`` already created the columns) does nothing.
    """
    _ensure_fighter_manual_status_columns(conn)
    _ensure_fighter_game_info_column(conn)
    _ensure_slate_manual_review_columns(conn)
    _ensure_odds_book_lines_table(conn)


def _ensure_fighter_manual_status_columns(conn: sqlite3.Connection) -> None:
    """Add the Fighter Status v1 override columns to ``fighters`` if absent.

    Phase B of ``docs/FIGHTER_STATUS_V1_DESIGN.md`` (§13.2, Option B).
    Both columns are nullable: ``NULL`` means "no user override", in which
    case the effective Fighter Status falls back to the importer-owned
    ``fighters.status`` column via
    ``src/slate/fighter_status.py::resolve_effective_fighter_status``.
    """
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(fighters)").fetchall()
    }
    if not existing:
        # Partial-fixture safety: a test seed for a different table may
        # call ``apply_pending_migrations`` before ``apply_schema``
        # creates ``fighters``. Production callers always run
        # ``apply_schema`` first via ``run_migrations``.
        return
    altered = False
    if "manual_status" not in existing:
        conn.execute("ALTER TABLE fighters ADD COLUMN manual_status TEXT")
        altered = True
    if "manual_status_set_at" not in existing:
        conn.execute(
            "ALTER TABLE fighters ADD COLUMN manual_status_set_at TEXT"
        )
        altered = True
    if altered:
        conn.commit()


def _ensure_fighter_game_info_column(conn: sqlite3.Connection) -> None:
    """Add the DK ``game_info`` column to ``fighters`` if absent.

    B2 of ``docs/DK_GAME_INFO_PAIRING_DESIGN.md`` (§2.2). The column is
    nullable: ``NULL`` means "not captured" — a row imported before this
    feature shipped, or a blank ``Game Info`` cell in the CSV. A non-null
    value is the verbatim, persist-time-stripped DK string that lets the
    Fight Groups page reconstruct pairings by exact-string grouping (§3).

    The column is not retroactively backfilled (§2.5): existing rows keep
    ``NULL`` until the next salary re-import flips them to ``updated`` with
    a populated value.

    Defensive against partial fixtures: if ``fighters`` does not exist yet
    (a test seed may run ``apply_pending_migrations`` before
    ``apply_schema``), the helper is a no-op. Production callers always run
    ``apply_schema`` first via ``run_migrations`` / ``bootstrap_database``.
    """
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(fighters)").fetchall()
    }
    if not existing:
        return
    if "game_info" not in existing:
        conn.execute("ALTER TABLE fighters ADD COLUMN game_info TEXT")
        conn.commit()


def _ensure_slate_manual_review_columns(conn: sqlite3.Connection) -> None:
    """Add the Manual Review Gate v1 columns to ``slates`` if absent.

    Phase B of ``docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`` (§9.2 Option B,
    §10 Phase B). ``manual_review_status`` carries the closed v1 value
    set {'not_reviewed', 'reviewed'} and defaults to 'not_reviewed' so
    pre-Phase-B rows present a safe default to the future gate.
    ``manual_review_completed_at`` is nullable; it is set when the user
    clicks Mark Slate Manually Reviewed (§6) and remains NULL until
    then. ``late_news_acknowledged_at`` is intentionally deferred to a
    follow-up slice (§9.2 / §18.6 opt-in).

    Defensive against partial fixtures: if ``slates`` does not exist
    yet (a pre-Phase-B test fixture for a different table, for example
    Fighter Status' ``fighters``-only seed), the helper is a no-op.
    The full ``apply_schema`` pass in ``run_migrations`` creates the
    table first, so production callers always see ``slates`` present.
    """
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(slates)").fetchall()
    }
    if not existing:
        return
    altered = False
    if "manual_review_status" not in existing:
        conn.execute(
            "ALTER TABLE slates ADD COLUMN manual_review_status "
            "TEXT NOT NULL DEFAULT 'not_reviewed'"
        )
        altered = True
    if "manual_review_completed_at" not in existing:
        conn.execute(
            "ALTER TABLE slates ADD COLUMN manual_review_completed_at TEXT"
        )
        altered = True
    if altered:
        conn.commit()


def _ensure_odds_book_lines_table(conn: sqlite3.Connection) -> None:
    """Create the multi-book consensus provenance table if absent.

    Slice 5 of ``docs/ODDS_CONSENSUS_DESIGN.md`` (§5.4 / §6). One row per
    (slate, fighter, book): the raw per-book lines the consensus blend was
    computed from. No change to ``odds_rows`` / ``odds_match_results`` columns;
    the synthesized ``source="consensus"`` row is an ordinary ``odds_rows``
    insert (see ``OddsRowRepository.replace_for_slate_source``).

    Idempotent via ``CREATE TABLE IF NOT EXISTS``. ``apply_schema`` already
    carries the same statement, so on a fresh DB (where ``bootstrap_database``
    runs ``apply_schema`` first) this helper is a no-op; it exists to promote an
    *existing* on-disk DB created before this slice, per ``docs/DEVELOPMENT_NOTES.md`` §8 ("new
    tables require a paired migration in ``src/db/migrations.py``"). Unlike the
    column-add helpers above it needs no partial-fixture guard: the statement is
    self-creating and SQLite defers the ``slates`` foreign-key check until a row
    is inserted, so it is safe to run before ``slates`` exists.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS odds_book_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
            fighter_name_raw TEXT NOT NULL,
            fighter_name_normalized TEXT NOT NULL,
            opponent_name_raw TEXT,
            book TEXT NOT NULL,
            american_odds INTEGER NOT NULL,
            source TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT (datetime('now')),
            import_batch_id TEXT,
            UNIQUE(slate_id, fighter_name_normalized, book),
            CHECK (american_odds <> 0)
        )
        """
    )
    conn.commit()


def bootstrap_database(conn: sqlite3.Connection) -> None:
    """Apply base schema and any pending migrations to ``conn``.

    Single entry point for Streamlit pages and other app code paths so a
    DB that was created at an older shape (``apply_schema`` only) is
    safely promoted to the current column set before reads/writes. Both
    halves are idempotent, so it is safe to call once per page load.
    """
    apply_schema(conn)
    apply_pending_migrations(conn)


def run_migrations() -> None:
    with get_connection() as conn:
        bootstrap_database(conn)


if __name__ == "__main__":
    run_migrations()
    print("Schema applied.")
