"""Tests for the v0 SlateRepository."""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import SlateRepository
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


def test_create_slate_minimal(conn):
    repo = SlateRepository(conn)
    rec = repo.create(event_name="UFC 999")
    assert rec.id > 0
    assert rec.event_name == "UFC 999"
    assert rec.event_date is None
    assert rec.salary_csv_status == "unvalidated"
    assert rec.salary_row_count == 0
    assert rec.created_at  # populated by DB default


def test_create_slate_full_fields(conn):
    repo = SlateRepository(conn)
    rec = repo.create(
        event_name="UFC 1000",
        event_date="2026-06-15",
        salary_csv_status="validated",
        salary_row_count=24,
    )
    assert rec.event_date == "2026-06-15"
    assert rec.salary_csv_status == "validated"
    assert rec.salary_row_count == 24


def test_create_slate_requires_event_name(conn):
    repo = SlateRepository(conn)
    with pytest.raises(ValueError):
        repo.create(event_name="")
    with pytest.raises(ValueError):
        repo.create(event_name="   ")


def test_list_all_orders_newest_first(conn):
    repo = SlateRepository(conn)
    a = repo.create(event_name="A")
    b = repo.create(event_name="B")
    c = repo.create(event_name="C")
    rows = repo.list_all()
    assert [r.id for r in rows] == [c.id, b.id, a.id]


def test_list_all_empty(conn):
    assert SlateRepository(conn).list_all() == []


def test_schema_has_expected_slate_columns(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(slates)").fetchall()}
    for required in {
        "event_name",
        "event_date",
        "salary_csv_status",
        "salary_row_count",
        "created_at",
    }:
        assert required in cols


# ---------------------------------------------------------------------------
# delete / delete_all — local cleanup with ON DELETE CASCADE
# ---------------------------------------------------------------------------


def _seed_slate_with_dependents(conn: sqlite3.Connection, name: str) -> int:
    """Create a slate plus one row in every cascade level beneath it
    (fighter → odds / projection / lineup_fighter, optimizer_run → lineup,
    plus a fight group and a manual override). Returns the slate id."""
    sid = SlateRepository(conn).create(event_name=name).id
    fid = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, 'Jon Doe', 8000, 'active')",
        (sid,),
    ).lastrowid
    conn.execute(
        "INSERT INTO odds (fighter_id, american_odds) VALUES (?, -150)", (fid,)
    )
    conn.execute(
        "INSERT INTO projections (fighter_id, projection) VALUES (?, 42.0)",
        (fid,),
    )
    conn.execute(
        "INSERT INTO fight_groups (slate_id, fighter_1_name, fighter_2_name) "
        "VALUES (?, 'Jon Doe', 'Jane Roe')",
        (sid,),
    )
    conn.execute(
        "INSERT INTO manual_match_overrides (slate_id, override_type) "
        "VALUES (?, 'reject_match')",
        (sid,),
    )
    run_id = conn.execute(
        "INSERT INTO optimizer_runs (slate_id) VALUES (?)", (sid,)
    ).lastrowid
    lineup_id = conn.execute(
        "INSERT INTO lineups (run_id, total_salary, projected_points) "
        "VALUES (?, 48000, 240.0)",
        (run_id,),
    ).lastrowid
    conn.execute(
        "INSERT INTO lineup_fighters (lineup_id, fighter_id) VALUES (?, ?)",
        (lineup_id, fid),
    )
    conn.commit()
    return sid


_CASCADE_TABLES = (
    "fighters",
    "odds",
    "projections",
    "fight_groups",
    "manual_match_overrides",
    "optimizer_runs",
    "lineups",
    "lineup_fighters",
)


def _total_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_delete_removes_slate_and_cascades_dependents(conn):
    sid = _seed_slate_with_dependents(conn, "UFC Doomed")
    # Every cascade level has a row before the delete.
    for table in _CASCADE_TABLES:
        assert _total_rows(conn, table) == 1, table

    SlateRepository(conn).delete(sid)

    assert SlateRepository(conn).list_all() == []
    # The whole dependent graph cascaded away (recursive cascade).
    for table in _CASCADE_TABLES:
        assert _total_rows(conn, table) == 0, table


def test_delete_leaves_other_slates_untouched(conn):
    keep = _seed_slate_with_dependents(conn, "UFC Keep")
    drop = _seed_slate_with_dependents(conn, "UFC Drop")

    SlateRepository(conn).delete(drop)

    ids = {s.id for s in SlateRepository(conn).list_all()}
    assert ids == {keep}
    # The survivor keeps exactly its own dependent rows.
    for table in _CASCADE_TABLES:
        assert _total_rows(conn, table) == 1, table
    assert (
        conn.execute(
            "SELECT slate_id FROM fighters", ()
        ).fetchone()[0]
        == keep
    )


def test_delete_missing_slate_raises_and_persists_nothing(conn):
    keep = _seed_slate_with_dependents(conn, "UFC Keep")
    with pytest.raises(ValueError):
        SlateRepository(conn).delete(999_999)
    # The unrelated slate + its dependents are untouched.
    assert {s.id for s in SlateRepository(conn).list_all()} == {keep}
    for table in _CASCADE_TABLES:
        assert _total_rows(conn, table) == 1, table


def test_delete_all_clears_every_slate_and_dependent(conn):
    _seed_slate_with_dependents(conn, "UFC A")
    _seed_slate_with_dependents(conn, "UFC B")

    removed = SlateRepository(conn).delete_all()

    assert removed == 2
    assert SlateRepository(conn).list_all() == []
    for table in _CASCADE_TABLES:
        assert _total_rows(conn, table) == 0, table


def test_delete_all_on_empty_db_returns_zero(conn):
    assert SlateRepository(conn).delete_all() == 0
    assert SlateRepository(conn).list_all() == []
