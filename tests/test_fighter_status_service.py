"""Phase C tests for ``src/slate/fighter_status_service.py``.

Covers ``docs/FIGHTER_STATUS_V1_DESIGN.md`` §15 Phase C: the read
aggregator returns one row per fighter on a slate with the importer
base, the manual override, the resolved effective status, and the
downstream category. Read-only — no DB writes, no
``odds_match_results.effective_status`` references, no projection /
alerts side effects.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import FighterRepository, SlateRepository
from src.db.schema import apply_schema
from src.ingestion.dk_salary_importer import ParsedSalaryRow
from src.slate import fighter_status as fs
from src.slate.fighter_status_service import (
    FighterStatusRow,
    category_counts,
    list_fighter_status_rows,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def slate_id(conn):
    return SlateRepository(conn).create(event_name="UFC 777").id


@pytest.fixture
def other_slate_id(conn):
    return SlateRepository(conn).create(event_name="UFC 778").id


def _parsed(name: str, salary: int, *, source_row_number: int = 1) -> ParsedSalaryRow:
    return ParsedSalaryRow(
        fighter_name=name,
        salary=salary,
        roster_position="F",
        game_info="Jon Doe@Jane Roe 05/22/2026",
        source_row_number=source_row_number,
    )


def _seed(conn, slate_id, names_salaries):
    parsed = [
        _parsed(name, salary, source_row_number=i + 1)
        for i, (name, salary) in enumerate(names_salaries)
    ]
    FighterRepository(conn).upsert_for_slate(slate_id=slate_id, parsed_rows=parsed)
    return {
        r.name: r.id
        for r in FighterRepository(conn).list_for_slate(slate_id)
    }


def _snapshot(conn):
    """Whole-table snapshot used to assert read-only behavior."""
    return {
        "fighters": conn.execute(
            "SELECT id, slate_id, name, salary, status, "
            "       manual_status, manual_status_set_at "
            "FROM fighters ORDER BY id"
        ).fetchall(),
        "odds_match_results": conn.execute(
            "SELECT COUNT(*) FROM odds_match_results"
        ).fetchone()[0],
        "manual_match_overrides": conn.execute(
            "SELECT COUNT(*) FROM manual_match_overrides"
        ).fetchone()[0],
    }


# ---------------------------------------------------------------------------
# unknown / empty slate
# ---------------------------------------------------------------------------


def test_unknown_slate_returns_empty_list(conn):
    assert list_fighter_status_rows(conn, 999_999) == []


def test_empty_slate_returns_empty_list(conn, slate_id):
    assert list_fighter_status_rows(conn, slate_id) == []


def test_category_counts_on_empty_rows():
    assert category_counts([]) == {
        fs.CATEGORY_ACTIVE: 0,
        fs.CATEGORY_WARNING: 0,
        fs.CATEGORY_BLOCKING: 0,
    }


# ---------------------------------------------------------------------------
# active fighter with no override
# ---------------------------------------------------------------------------


def test_active_fighter_no_override_resolves_to_importer_active(conn, slate_id):
    _seed(conn, slate_id, [("Jon Doe", 9000)])
    [row] = list_fighter_status_rows(conn, slate_id)
    assert isinstance(row, FighterStatusRow)
    assert row.name == "Jon Doe"
    assert row.salary == 9000
    assert row.importer_status == "active"
    assert row.manual_status is None
    assert row.manual_status_set_at is None
    assert row.effective_status == "active"
    assert row.category == fs.CATEGORY_ACTIVE


# ---------------------------------------------------------------------------
# override surfacing & resolution
# ---------------------------------------------------------------------------


def test_warning_manual_override_surfaced_and_resolved(conn, slate_id):
    ids = _seed(conn, slate_id, [("Jon Doe", 9000)])
    FighterRepository(conn).set_manual_status(
        slate_id=slate_id, fighter_id=ids["Jon Doe"], status=fs.QUESTIONABLE
    )
    [row] = list_fighter_status_rows(conn, slate_id)
    assert row.importer_status == "active"
    assert row.manual_status == "questionable"
    assert row.manual_status_set_at is not None and row.manual_status_set_at != ""
    assert row.effective_status == "questionable"
    assert row.category == fs.CATEGORY_WARNING


def test_blocking_manual_override_surfaced_and_resolved(conn, slate_id):
    ids = _seed(conn, slate_id, [("Jon Doe", 9000)])
    FighterRepository(conn).set_manual_status(
        slate_id=slate_id, fighter_id=ids["Jon Doe"], status=fs.OUT
    )
    [row] = list_fighter_status_rows(conn, slate_id)
    assert row.importer_status == "active"
    assert row.manual_status == "out"
    assert row.effective_status == "out"
    assert row.category == fs.CATEGORY_BLOCKING


def test_manual_override_wins_over_importer_base(conn, slate_id):
    """Importer base = inactive (deactivated), user override = active
    must resolve to active per the Phase A resolver."""
    ids = _seed(conn, slate_id, [("Jon Doe", 9000), ("Jane Roe", 8500)])
    # Simulate deactivation: re-import without Jon Doe.
    FighterRepository(conn).upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jane Roe", 8500)]
    )
    # Jon is now importer=inactive. User asserts active.
    FighterRepository(conn).set_manual_status(
        slate_id=slate_id, fighter_id=ids["Jon Doe"], status=fs.ACTIVE
    )
    rows_by_name = {r.name: r for r in list_fighter_status_rows(conn, slate_id)}
    jon = rows_by_name["Jon Doe"]
    assert jon.importer_status == "inactive"
    assert jon.manual_status == "active"
    assert jon.effective_status == "active"
    assert jon.category == fs.CATEGORY_ACTIVE


def test_clear_override_falls_back_to_importer_base(conn, slate_id):
    ids = _seed(conn, slate_id, [("Jon Doe", 9000)])
    repo = FighterRepository(conn)
    repo.set_manual_status(slate_id=slate_id, fighter_id=ids["Jon Doe"], status=fs.OUT)
    repo.clear_manual_status(slate_id=slate_id, fighter_id=ids["Jon Doe"])
    [row] = list_fighter_status_rows(conn, slate_id)
    assert row.manual_status is None
    assert row.manual_status_set_at is None
    assert row.effective_status == "active"
    assert row.category == fs.CATEGORY_ACTIVE


def test_missing_base_status_resolves_to_needs_review(conn, slate_id):
    """The Phase A resolver falls back to ``needs_review`` when the
    importer base is empty (§13.2, design §15 Phase A). Reach this
    path by writing an empty string to ``fighters.status`` directly —
    the importer would never do this, but the resolver contract is
    still part of the v1 surface and the aggregator must honor it."""
    _seed(conn, slate_id, [("Edge Case", 7000)])
    conn.execute(
        "UPDATE fighters SET status = '' WHERE slate_id = ? AND name = ?",
        (slate_id, "Edge Case"),
    )
    conn.commit()
    [row] = list_fighter_status_rows(conn, slate_id)
    assert row.importer_status == ""
    assert row.manual_status is None
    assert row.effective_status == fs.NEEDS_REVIEW
    assert row.category == fs.CATEGORY_WARNING


# ---------------------------------------------------------------------------
# multi-row composition, ordering, category counts
# ---------------------------------------------------------------------------


def test_one_row_per_fighter_with_deterministic_ordering(conn, slate_id):
    _seed(conn, slate_id, [
        ("charlie Fighter", 7000),
        ("Alpha Fighter", 9000),
        ("bravo Fighter", 8000),
    ])
    rows = list_fighter_status_rows(conn, slate_id)
    assert [r.name for r in rows] == [
        "Alpha Fighter", "bravo Fighter", "charlie Fighter"
    ]


def test_category_counts_aggregate_correctly(conn, slate_id):
    ids = _seed(conn, slate_id, [
        ("A", 9000), ("B", 8500), ("C", 8000), ("D", 7500), ("E", 7000),
    ])
    repo = FighterRepository(conn)
    repo.set_manual_status(slate_id=slate_id, fighter_id=ids["B"], status=fs.QUESTIONABLE)
    repo.set_manual_status(slate_id=slate_id, fighter_id=ids["C"], status=fs.MISSED_WEIGHT)
    repo.set_manual_status(slate_id=slate_id, fighter_id=ids["D"], status=fs.OUT)
    repo.set_manual_status(slate_id=slate_id, fighter_id=ids["E"], status=fs.WITHDRAWN)

    rows = list_fighter_status_rows(conn, slate_id)
    counts = category_counts(rows)
    assert counts == {
        fs.CATEGORY_ACTIVE: 1,   # A
        fs.CATEGORY_WARNING: 2,  # B, C
        fs.CATEGORY_BLOCKING: 2, # D, E
    }


def test_slate_isolation(conn, slate_id, other_slate_id):
    _seed(conn, slate_id, [("Slate1 Fighter", 9000)])
    _seed(conn, other_slate_id, [("Slate2 Fighter", 8500)])
    rows = list_fighter_status_rows(conn, slate_id)
    assert [r.name for r in rows] == ["Slate1 Fighter"]
    other = list_fighter_status_rows(conn, other_slate_id)
    assert [r.name for r in other] == ["Slate2 Fighter"]


# ---------------------------------------------------------------------------
# read-only contract
# ---------------------------------------------------------------------------


def test_list_does_not_mutate_db(conn, slate_id):
    ids = _seed(conn, slate_id, [("Jon Doe", 9000), ("Jane Roe", 8500)])
    FighterRepository(conn).set_manual_status(
        slate_id=slate_id, fighter_id=ids["Jon Doe"], status=fs.OUT
    )
    before = _snapshot(conn)
    rows = list_fighter_status_rows(conn, slate_id)
    _ = category_counts(rows)
    # And a second read — still no mutation.
    list_fighter_status_rows(conn, slate_id)
    after = _snapshot(conn)
    assert before == after


def test_no_odds_match_results_reads(conn, slate_id):
    """The Phase C aggregator must stay disjoint from the odds-match
    override layer (design §6, §7, §8). Snapshot ``odds_match_results``
    rowcounts before and after the read and assert the aggregator never
    touched the table — also covers the broader "no DB writes" contract
    against the override layer."""
    ids = _seed(conn, slate_id, [("Jon Doe", 9000)])
    FighterRepository(conn).set_manual_status(
        slate_id=slate_id, fighter_id=ids["Jon Doe"], status=fs.OUT
    )
    before = conn.execute(
        "SELECT COUNT(*) FROM odds_match_results"
    ).fetchone()[0]
    list_fighter_status_rows(conn, slate_id)
    after = conn.execute(
        "SELECT COUNT(*) FROM odds_match_results"
    ).fetchone()[0]
    assert before == after == 0


def test_service_module_does_not_query_odds_match_results():
    """The Phase C aggregator runs no SQL against the odds-match
    override tables (design §8). Pin this by source inspection — any
    future regression that adds such a query will trip the assertion."""
    import inspect
    import src.slate.fighter_status_service as svc

    source = inspect.getsource(svc)
    # No SQL against the disjoint override tables.
    assert "FROM odds_match_results" not in source
    assert "FROM manual_match_overrides" not in source
