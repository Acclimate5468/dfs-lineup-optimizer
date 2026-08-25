"""Schema tests for Phase A of odds persistence.

Covers only the schema definitions (tables, columns, indexes, constraints)
added per docs/ODDS_PERSISTENCE_DESIGN.md §5. Repository / write-path code
is intentionally not exercised here — Phases B+ will add that.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.schema import apply_schema


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    apply_schema(c)
    try:
        yield c
    finally:
        c.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    """Return {column_name: (type, notnull, dflt_value, pk)} for a table."""
    return {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
    }


# --- table existence ----------------------------------------------------


def test_apply_schema_creates_odds_persistence_tables(conn):
    for table in (
        "odds_rows",
        "odds_match_results",
        "manual_match_overrides",
        "odds_book_lines",
    ):
        assert _table_exists(conn, table), f"{table} table missing"


def test_apply_schema_is_idempotent(conn):
    # Re-applying must not raise (CREATE TABLE IF NOT EXISTS / CREATE INDEX
    # IF NOT EXISTS). Phases B+ will rely on this for boot-time migrations.
    apply_schema(conn)
    for table in (
        "odds_rows",
        "odds_match_results",
        "manual_match_overrides",
        "odds_book_lines",
    ):
        assert _table_exists(conn, table)


# --- odds_rows ----------------------------------------------------------


def test_odds_rows_has_expected_columns(conn):
    cols = _columns(conn, "odds_rows")
    for required in {
        "id",
        "slate_id",
        "odds_row_key",
        "fighter_name_raw",
        "fighter_name_normalized",
        "opponent_name_raw",
        "american_odds",
        "implied_probability",
        "bookmaker",
        "source",
        "captured_at",
        "imported_at",
        "import_batch_id",
    }:
        assert required in cols, f"odds_rows missing column {required}"


def test_odds_rows_required_columns_are_not_null(conn):
    cols = _columns(conn, "odds_rows")
    # (type, notnull, dflt, pk) — notnull is index 1.
    for required in (
        "slate_id",
        "odds_row_key",
        "fighter_name_raw",
        "fighter_name_normalized",
        "american_odds",
        "source",
        "captured_at",
        "imported_at",
    ):
        assert cols[required][1] == 1, f"{required} should be NOT NULL"


def test_odds_rows_optional_columns_are_nullable(conn):
    cols = _columns(conn, "odds_rows")
    for nullable in ("opponent_name_raw", "implied_probability", "bookmaker", "import_batch_id"):
        assert cols[nullable][1] == 0, f"{nullable} should be nullable"


def test_odds_rows_unique_slate_and_key(conn):
    # Insert a slate to satisfy the FK.
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC X') RETURNING id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO odds_rows (slate_id, odds_row_key, fighter_name_raw, "
        "fighter_name_normalized, american_odds, source, captured_at) "
        "VALUES (?, 'k1', 'Foo', 'foo', -150, 'csv:test', '2026-05-20T00:00:00Z')",
        (slate_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO odds_rows (slate_id, odds_row_key, fighter_name_raw, "
            "fighter_name_normalized, american_odds, source, captured_at) "
            "VALUES (?, 'k1', 'Foo', 'foo', -150, 'csv:test', '2026-05-20T00:00:01Z')",
            (slate_id,),
        )


def test_odds_rows_rejects_zero_american_odds(conn):
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC Y') RETURNING id"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO odds_rows (slate_id, odds_row_key, fighter_name_raw, "
            "fighter_name_normalized, american_odds, source, captured_at) "
            "VALUES (?, 'k0', 'Foo', 'foo', 0, 'csv:test', '2026-05-20T00:00:00Z')",
            (slate_id,),
        )


def test_odds_rows_cascades_on_slate_delete(conn):
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC Z') RETURNING id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO odds_rows (slate_id, odds_row_key, fighter_name_raw, "
        "fighter_name_normalized, american_odds, source, captured_at) "
        "VALUES (?, 'k1', 'Foo', 'foo', -150, 'csv:test', '2026-05-20T00:00:00Z')",
        (slate_id,),
    )
    conn.execute("DELETE FROM slates WHERE id = ?", (slate_id,))
    remaining = conn.execute(
        "SELECT COUNT(*) FROM odds_rows WHERE slate_id = ?", (slate_id,)
    ).fetchone()[0]
    assert remaining == 0


def test_odds_rows_has_normalized_name_index(conn):
    assert (
        "idx_odds_rows_slate_normalized_name" in _index_names(conn, "odds_rows")
    )


# --- odds_match_results -------------------------------------------------


def test_odds_match_results_has_expected_columns(conn):
    cols = _columns(conn, "odds_match_results")
    for required in {
        "id",
        "slate_id",
        "odds_row_id",
        "odds_row_key",
        "fighter_id",
        "match_status",
        "match_stage",
        "match_score",
        "opponent_check",
        "preferred_candidate",
        "candidates_json",
        "notes_json",
        "effective_status",
        "computed_at",
    }:
        assert required in cols, f"odds_match_results missing column {required}"


def test_odds_match_results_fighter_id_is_nullable(conn):
    # Design §5.2: nullable for `unmatched` / ambiguous verdicts.
    cols = _columns(conn, "odds_match_results")
    assert cols["fighter_id"][1] == 0


def test_odds_match_results_required_columns_are_not_null(conn):
    cols = _columns(conn, "odds_match_results")
    for required in (
        "slate_id",
        "odds_row_id",
        "odds_row_key",
        "match_status",
        "match_stage",
        "match_score",
        "opponent_check",
        "effective_status",
        "computed_at",
    ):
        assert cols[required][1] == 1, f"{required} should be NOT NULL"


def test_odds_match_results_unique_slate_and_odds_row(conn):
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC A') RETURNING id"
    ).fetchone()[0]
    odds_row_id = conn.execute(
        "INSERT INTO odds_rows (slate_id, odds_row_key, fighter_name_raw, "
        "fighter_name_normalized, american_odds, source, captured_at) "
        "VALUES (?, 'k1', 'Foo', 'foo', -150, 'csv:test', '2026-05-20T00:00:00Z') "
        "RETURNING id",
        (slate_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO odds_match_results (slate_id, odds_row_id, odds_row_key, "
        "match_status, match_stage, match_score, opponent_check, "
        "effective_status) "
        "VALUES (?, ?, 'k1', 'auto_match', 'exact_conservative', 100, "
        "'passed', 'auto_match')",
        (slate_id, odds_row_id),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO odds_match_results (slate_id, odds_row_id, odds_row_key, "
            "match_status, match_stage, match_score, opponent_check, "
            "effective_status) "
            "VALUES (?, ?, 'k1', 'auto_match', 'exact_conservative', 100, "
            "'passed', 'auto_match')",
            (slate_id, odds_row_id),
        )


def test_odds_match_results_has_slate_status_indexes(conn):
    idx = _index_names(conn, "odds_match_results")
    assert "idx_odds_match_results_slate_fighter" in idx
    assert "idx_odds_match_results_slate_effective_status" in idx


def test_odds_match_results_cascades_on_odds_row_delete(conn):
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC B') RETURNING id"
    ).fetchone()[0]
    odds_row_id = conn.execute(
        "INSERT INTO odds_rows (slate_id, odds_row_key, fighter_name_raw, "
        "fighter_name_normalized, american_odds, source, captured_at) "
        "VALUES (?, 'k1', 'Foo', 'foo', -150, 'csv:test', '2026-05-20T00:00:00Z') "
        "RETURNING id",
        (slate_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO odds_match_results (slate_id, odds_row_id, odds_row_key, "
        "match_status, match_stage, match_score, opponent_check, "
        "effective_status) "
        "VALUES (?, ?, 'k1', 'unmatched', 'none', 0, 'unknown', 'unmatched')",
        (slate_id, odds_row_id),
    )
    conn.execute("DELETE FROM odds_rows WHERE id = ?", (odds_row_id,))
    remaining = conn.execute(
        "SELECT COUNT(*) FROM odds_match_results WHERE odds_row_id = ?",
        (odds_row_id,),
    ).fetchone()[0]
    assert remaining == 0


def test_odds_match_results_fighter_id_nulls_on_fighter_delete(conn):
    # Design §6.2: ON DELETE SET NULL so a salary re-import that drops the
    # fighter leaves the result row in place for the recompute pass.
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC C') RETURNING id"
    ).fetchone()[0]
    fighter_id = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary) "
        "VALUES (?, 'Foo Bar', 8000) RETURNING id",
        (slate_id,),
    ).fetchone()[0]
    odds_row_id = conn.execute(
        "INSERT INTO odds_rows (slate_id, odds_row_key, fighter_name_raw, "
        "fighter_name_normalized, american_odds, source, captured_at) "
        "VALUES (?, 'k1', 'Foo Bar', 'foo bar', -150, 'csv:test', "
        "'2026-05-20T00:00:00Z') RETURNING id",
        (slate_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO odds_match_results (slate_id, odds_row_id, odds_row_key, "
        "fighter_id, match_status, match_stage, match_score, opponent_check, "
        "effective_status) "
        "VALUES (?, ?, 'k1', ?, 'auto_match', 'exact_conservative', 100, "
        "'passed', 'auto_match')",
        (slate_id, odds_row_id, fighter_id),
    )
    conn.execute("DELETE FROM fighters WHERE id = ?", (fighter_id,))
    row = conn.execute(
        "SELECT fighter_id FROM odds_match_results WHERE odds_row_id = ?",
        (odds_row_id,),
    ).fetchone()
    assert row is not None and row[0] is None


# --- manual_match_overrides --------------------------------------------


def test_manual_match_overrides_has_expected_columns(conn):
    cols = _columns(conn, "manual_match_overrides")
    for required in {
        "id",
        "slate_id",
        "odds_row_key",
        "fighter_id",
        "override_type",
        "payload_json",
        "reason",
        "created_at",
        "superseded_at",
    }:
        assert (
            required in cols
        ), f"manual_match_overrides missing column {required}"


def test_manual_match_overrides_nullable_targets(conn):
    # Design §5.3: both odds_row_key and fighter_id are nullable so the
    # row can represent fighter-level or row-level overrides.
    cols = _columns(conn, "manual_match_overrides")
    assert cols["odds_row_key"][1] == 0
    assert cols["fighter_id"][1] == 0
    assert cols["payload_json"][1] == 0
    assert cols["reason"][1] == 0
    assert cols["superseded_at"][1] == 0


def test_manual_match_overrides_required_columns_are_not_null(conn):
    cols = _columns(conn, "manual_match_overrides")
    for required in ("slate_id", "override_type", "created_at"):
        assert cols[required][1] == 1, f"{required} should be NOT NULL"


def test_manual_match_overrides_has_active_partial_indexes(conn):
    # Design §5.3: active rows (`superseded_at IS NULL`) are filtered via
    # partial indexes. Verify by inspecting sqlite_master for the WHERE clause.
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'manual_match_overrides'"
    ).fetchall()
    by_name = {name: sql or "" for name, sql in rows}
    assert "idx_manual_match_overrides_active_fighter" in by_name
    assert "idx_manual_match_overrides_active_odds_row_key" in by_name
    for name in (
        "idx_manual_match_overrides_active_fighter",
        "idx_manual_match_overrides_active_odds_row_key",
    ):
        assert "superseded_at IS NULL" in by_name[name], (
            f"{name} should be a partial index on superseded_at IS NULL"
        )


def test_manual_match_overrides_allows_soft_replace(conn):
    # Two override rows of the same type for the same (slate, fighter) must
    # coexist as long as the older one is superseded. No hard UNIQUE on
    # (slate, fighter, type).
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC D') RETURNING id"
    ).fetchone()[0]
    fighter_id = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary) "
        "VALUES (?, 'Foo Bar', 8000) RETURNING id",
        (slate_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO manual_match_overrides "
        "(slate_id, fighter_id, override_type, superseded_at) "
        "VALUES (?, ?, 'mark_excluded', '2026-05-20T00:00:00Z')",
        (slate_id, fighter_id),
    )
    conn.execute(
        "INSERT INTO manual_match_overrides "
        "(slate_id, fighter_id, override_type) "
        "VALUES (?, ?, 'mark_excluded')",
        (slate_id, fighter_id),
    )
    count = conn.execute(
        "SELECT COUNT(*) FROM manual_match_overrides "
        "WHERE slate_id = ? AND fighter_id = ?",
        (slate_id, fighter_id),
    ).fetchone()[0]
    assert count == 2


def test_manual_match_overrides_cascades_on_slate_delete(conn):
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC E') RETURNING id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO manual_match_overrides "
        "(slate_id, override_type) VALUES (?, 'mark_excluded')",
        (slate_id,),
    )
    conn.execute("DELETE FROM slates WHERE id = ?", (slate_id,))
    remaining = conn.execute(
        "SELECT COUNT(*) FROM manual_match_overrides WHERE slate_id = ?",
        (slate_id,),
    ).fetchone()[0]
    assert remaining == 0


# --- odds_book_lines (ODDS_CONSENSUS_DESIGN §5.4 / §6) ------------------


def _insert_book_line(conn, slate_id, **overrides) -> None:
    payload = {
        "fighter_name_raw": "Foo",
        "fighter_name_normalized": "foo",
        "book": "DraftKings",
        "american_odds": -150,
        "source": "bestfightodds",
        "captured_at": "2026-05-20T00:00:00Z",
    }
    payload.update(overrides)
    cols = ", ".join(("slate_id", *payload.keys()))
    placeholders = ", ".join(["?"] * (len(payload) + 1))
    conn.execute(
        f"INSERT INTO odds_book_lines ({cols}) VALUES ({placeholders})",
        (slate_id, *payload.values()),
    )


def test_odds_book_lines_has_expected_columns(conn):
    cols = _columns(conn, "odds_book_lines")
    for required in {
        "id",
        "slate_id",
        "fighter_name_raw",
        "fighter_name_normalized",
        "opponent_name_raw",
        "book",
        "american_odds",
        "source",
        "captured_at",
        "imported_at",
        "import_batch_id",
    }:
        assert required in cols, f"odds_book_lines missing column {required}"


def test_odds_book_lines_required_columns_are_not_null(conn):
    cols = _columns(conn, "odds_book_lines")
    # (type, notnull, dflt, pk) — notnull is index 1.
    for required in (
        "slate_id",
        "fighter_name_raw",
        "fighter_name_normalized",
        "book",
        "american_odds",
        "source",
        "captured_at",
        "imported_at",
    ):
        assert cols[required][1] == 1, f"{required} should be NOT NULL"


def test_odds_book_lines_optional_columns_are_nullable(conn):
    cols = _columns(conn, "odds_book_lines")
    for nullable in ("opponent_name_raw", "import_batch_id"):
        assert cols[nullable][1] == 0, f"{nullable} should be nullable"


def test_odds_book_lines_unique_slate_fighter_book(conn):
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC BL1') RETURNING id"
    ).fetchone()[0]
    _insert_book_line(conn, slate_id)
    with pytest.raises(sqlite3.IntegrityError):
        # Same (slate, fighter_normalized, book) — even with a different line.
        _insert_book_line(conn, slate_id, american_odds=-140)


def test_odds_book_lines_unique_allows_other_book_same_fighter(conn):
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC BL2') RETURNING id"
    ).fetchone()[0]
    _insert_book_line(conn, slate_id, book="DraftKings")
    _insert_book_line(conn, slate_id, book="FanDuel")
    count = conn.execute(
        "SELECT COUNT(*) FROM odds_book_lines WHERE slate_id = ?", (slate_id,)
    ).fetchone()[0]
    assert count == 2


def test_odds_book_lines_rejects_zero_american_odds(conn):
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC BL3') RETURNING id"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        _insert_book_line(conn, slate_id, american_odds=0)


def test_odds_book_lines_cascades_on_slate_delete(conn):
    slate_id = conn.execute(
        "INSERT INTO slates (event_name) VALUES ('UFC BL4') RETURNING id"
    ).fetchone()[0]
    _insert_book_line(conn, slate_id)
    conn.execute("DELETE FROM slates WHERE id = ?", (slate_id,))
    remaining = conn.execute(
        "SELECT COUNT(*) FROM odds_book_lines WHERE slate_id = ?", (slate_id,)
    ).fetchone()[0]
    assert remaining == 0
