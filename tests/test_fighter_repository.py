"""Tests for FighterRepository.

Read side (``list_for_slate``) — original coverage. Write side
(``upsert_for_slate``) — Phase B of
``docs/SALARY_PERSISTENCE_DESIGN.md`` §9: parsed DK salary rows → slate-
scoped fighter rows, idempotent, absent fighters marked inactive.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    FighterRecord,
    FighterRepository,
    FighterUpsertResult,
    ManualMatchOverrideRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.dk_salary_importer import ParsedSalaryRow


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
    return SlateRepository(conn).create(event_name="UFC 999").id


@pytest.fixture
def other_slate_id(conn):
    return SlateRepository(conn).create(event_name="UFC 998").id


def _insert_fighter(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    name: str,
    salary: int = 8000,
    status: str = "active",
) -> int:
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, ?)",
        (int(slate_id), name, int(salary), status),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_list_for_slate_empty(conn, slate_id):
    assert FighterRepository(conn).list_for_slate(slate_id) == []


def test_list_for_slate_returns_record_fields(conn, slate_id):
    fid = _insert_fighter(
        conn,
        slate_id=slate_id,
        name="Jon Jones",
        salary=9500,
        status="active",
    )
    rows = FighterRepository(conn).list_for_slate(slate_id)
    assert len(rows) == 1
    rec = rows[0]
    assert isinstance(rec, FighterRecord)
    assert rec.id == fid
    assert rec.slate_id == slate_id
    assert rec.name == "Jon Jones"
    assert rec.salary == 9500
    assert rec.status == "active"


def test_list_for_slate_orders_by_name_case_insensitive(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="charlie")
    _insert_fighter(conn, slate_id=slate_id, name="Alpha")
    _insert_fighter(conn, slate_id=slate_id, name="bravo")
    rows = FighterRepository(conn).list_for_slate(slate_id)
    assert [r.name for r in rows] == ["Alpha", "bravo", "charlie"]


def test_list_for_slate_scopes_by_slate(conn, slate_id, other_slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="A Fighter")
    _insert_fighter(conn, slate_id=slate_id, name="B Fighter")
    _insert_fighter(conn, slate_id=other_slate_id, name="C Fighter")

    s1 = FighterRepository(conn).list_for_slate(slate_id)
    s2 = FighterRepository(conn).list_for_slate(other_slate_id)
    assert {r.name for r in s1} == {"A Fighter", "B Fighter"}
    assert {r.name for r in s2} == {"C Fighter"}


def test_list_for_slate_includes_non_active_status(conn, slate_id):
    """Callers (the future matcher) filter on status themselves; the
    repository must not silently hide rows."""
    _insert_fighter(conn, slate_id=slate_id, name="Active F", status="active")
    _insert_fighter(
        conn, slate_id=slate_id, name="Excluded F", status="excluded"
    )
    rows = FighterRepository(conn).list_for_slate(slate_id)
    by_name = {r.name: r.status for r in rows}
    assert by_name == {"Active F": "active", "Excluded F": "excluded"}


def test_list_for_slate_is_deterministic_across_calls(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Bravo")
    _insert_fighter(conn, slate_id=slate_id, name="Alpha")
    first = FighterRepository(conn).list_for_slate(slate_id)
    second = FighterRepository(conn).list_for_slate(slate_id)
    assert first == second


def test_cascade_delete_from_slate_removes_fighters(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="A")
    _insert_fighter(conn, slate_id=slate_id, name="B")
    assert len(FighterRepository(conn).list_for_slate(slate_id)) == 2
    conn.execute("DELETE FROM slates WHERE id = ?", (slate_id,))
    conn.commit()
    assert FighterRepository(conn).list_for_slate(slate_id) == []


# ---------------------------------------------------------------------------
# upsert_for_slate — Slice B write path
# ---------------------------------------------------------------------------


def _parsed(
    name: str,
    salary: int,
    *,
    source_row_number: int = 1,
    roster_position: str | None = "F",
    game_info: str | None = "Jon Doe@Jane Roe 05/22/2026",
) -> ParsedSalaryRow:
    return ParsedSalaryRow(
        fighter_name=name,
        salary=salary,
        roster_position=roster_position,
        game_info=game_info,
        source_row_number=source_row_number,
    )


def test_upsert_inserts_new_rows(conn, slate_id):
    repo = FighterRepository(conn)
    result = repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9000, source_row_number=1),
            _parsed("Jane Roe", 8500, source_row_number=2),
        ],
    )
    assert isinstance(result, FighterUpsertResult)
    assert result == FighterUpsertResult(
        inserted=2, updated=0, unchanged=0, deactivated=0
    )

    rows = repo.list_for_slate(slate_id)
    assert [r.name for r in rows] == ["Jane Roe", "Jon Doe"]
    assert all(r.status == "active" for r in rows)
    by_name = {r.name: r.salary for r in rows}
    assert by_name == {"Jon Doe": 9000, "Jane Roe": 8500}


def test_upsert_persists_roster_position_defaulting_to_F(conn, slate_id):
    FighterRepository(conn).upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("With Pos", 9000, roster_position="F"),
            _parsed("No Pos", 8800, roster_position=None, source_row_number=2),
        ],
    )
    positions = dict(
        conn.execute(
            "SELECT name, position FROM fighters WHERE slate_id = ?",
            (slate_id,),
        ).fetchall()
    )
    assert positions == {"With Pos": "F", "No Pos": "F"}


def test_upsert_is_idempotent_on_repeat(conn, slate_id):
    repo = FighterRepository(conn)
    parsed = [
        _parsed("Jon Doe", 9000, source_row_number=1),
        _parsed("Jane Roe", 8500, source_row_number=2),
    ]
    repo.upsert_for_slate(slate_id=slate_id, parsed_rows=parsed)
    first = repo.list_for_slate(slate_id)

    result = repo.upsert_for_slate(slate_id=slate_id, parsed_rows=parsed)
    assert result == FighterUpsertResult(
        inserted=0, updated=0, unchanged=2, deactivated=0
    )

    second = repo.list_for_slate(slate_id)
    # Same ids, same fields — no row was re-inserted.
    assert [(r.id, r.name, r.salary, r.status) for r in first] == [
        (r.id, r.name, r.salary, r.status) for r in second
    ]


def test_upsert_updates_changed_salary_in_place(conn, slate_id):
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jon Doe", 9000)]
    )
    original_id = repo.list_for_slate(slate_id)[0].id

    result = repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jon Doe", 9300)]
    )
    assert result == FighterUpsertResult(
        inserted=0, updated=1, unchanged=0, deactivated=0
    )

    rows = repo.list_for_slate(slate_id)
    assert len(rows) == 1
    assert rows[0].id == original_id  # preserved → overrides not orphaned
    assert rows[0].salary == 9300
    assert rows[0].status == "active"


def test_upsert_updates_changed_position_in_place(conn, slate_id):
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[_parsed("Jon Doe", 9000, roster_position="F")],
    )

    result = repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[_parsed("Jon Doe", 9000, roster_position="CPT")],
    )
    assert result == FighterUpsertResult(
        inserted=0, updated=1, unchanged=0, deactivated=0
    )
    pos = conn.execute(
        "SELECT position FROM fighters WHERE slate_id = ? AND name = 'Jon Doe'",
        (slate_id,),
    ).fetchone()[0]
    assert pos == "CPT"


def test_upsert_same_name_different_slate_no_conflict(conn, slate_id, other_slate_id):
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Shared Name", 9000)]
    )
    result = repo.upsert_for_slate(
        slate_id=other_slate_id, parsed_rows=[_parsed("Shared Name", 7500)]
    )
    assert result.inserted == 1

    a = repo.list_for_slate(slate_id)
    b = repo.list_for_slate(other_slate_id)
    assert [(r.name, r.salary) for r in a] == [("Shared Name", 9000)]
    assert [(r.name, r.salary) for r in b] == [("Shared Name", 7500)]
    # Independent identities.
    assert a[0].id != b[0].id


def test_upsert_rejects_duplicate_name_in_input(conn, slate_id):
    repo = FighterRepository(conn)
    with pytest.raises(ValueError, match="duplicate fighter name"):
        repo.upsert_for_slate(
            slate_id=slate_id,
            parsed_rows=[
                _parsed("Jon Doe", 9000, source_row_number=1),
                _parsed("Jon Doe", 9500, source_row_number=2),
            ],
        )
    # No partial write.
    assert repo.list_for_slate(slate_id) == []


def test_upsert_rejects_empty_input(conn, slate_id):
    with pytest.raises(ValueError, match="must not be empty"):
        FighterRepository(conn).upsert_for_slate(
            slate_id=slate_id, parsed_rows=[]
        )


def test_upsert_rejects_unknown_slate(conn):
    with pytest.raises(ValueError, match="does not exist"):
        FighterRepository(conn).upsert_for_slate(
            slate_id=999_999, parsed_rows=[_parsed("Jon Doe", 9000)]
        )


def test_upsert_rejects_non_parsed_row_type(conn, slate_id):
    with pytest.raises(ValueError, match="ParsedSalaryRow"):
        FighterRepository(conn).upsert_for_slate(
            slate_id=slate_id,
            parsed_rows=[{"fighter_name": "Jon Doe", "salary": 9000}],  # type: ignore[list-item]
        )


def test_upsert_marks_absent_fighters_inactive_not_deleted(conn, slate_id):
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9000, source_row_number=1),
            _parsed("Jane Roe", 8500, source_row_number=2),
        ],
    )
    jon_id = next(r.id for r in repo.list_for_slate(slate_id) if r.name == "Jon Doe")

    # Re-import without Jon Doe.
    result = repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jane Roe", 8500)]
    )
    assert result == FighterUpsertResult(
        inserted=0, updated=0, unchanged=1, deactivated=1
    )

    rows = repo.list_for_slate(slate_id)
    by_name = {r.name: r for r in rows}
    # Jon Doe is still present (row not deleted) but marked inactive.
    assert by_name["Jon Doe"].id == jon_id
    assert by_name["Jon Doe"].status == "inactive"
    assert by_name["Jane Roe"].status == "active"


def test_upsert_reactivates_when_absent_fighter_reappears(conn, slate_id):
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jon Doe", 9000)]
    )
    # Re-import without Jon Doe → he goes inactive.
    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jane Roe", 8500)]
    )
    assert (
        next(r for r in repo.list_for_slate(slate_id) if r.name == "Jon Doe").status
        == "inactive"
    )
    # Re-import with Jon Doe back at a new salary → reactivated + updated.
    result = repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9100, source_row_number=1),
            _parsed("Jane Roe", 8500, source_row_number=2),
        ],
    )
    # Jon Doe: status was inactive → counted as updated (status flip + salary change).
    # Jane Roe: identical → unchanged.
    assert result == FighterUpsertResult(
        inserted=0, updated=1, unchanged=1, deactivated=0
    )
    jon = next(r for r in repo.list_for_slate(slate_id) if r.name == "Jon Doe")
    assert jon.status == "active"
    assert jon.salary == 9100


def test_upsert_does_not_disturb_other_slate_fighters(conn, slate_id, other_slate_id):
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=other_slate_id, parsed_rows=[_parsed("Other Slate F", 9000)]
    )
    before = repo.list_for_slate(other_slate_id)

    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Slate-A Fighter", 8000)]
    )
    after = repo.list_for_slate(other_slate_id)
    assert before == after


def test_upsert_preserves_fighter_id_across_updates(conn, slate_id):
    """Override-orphaning protection (design §8): updates must keep id stable
    so any ``manual_match_overrides`` referencing the fighter remain valid."""
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jon Doe", 9000)]
    )
    fid_before = repo.list_for_slate(slate_id)[0].id
    # Three more rounds of churn: salary, status, salary.
    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jon Doe", 9300)]
    )
    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jane Roe", 8500)]
    )  # Jon → inactive
    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jon Doe", 9200)]
    )  # Jon back
    fid_after = next(
        r.id for r in repo.list_for_slate(slate_id) if r.name == "Jon Doe"
    )
    assert fid_after == fid_before


def test_upsert_does_not_change_odds_or_overrides(conn, slate_id):
    """Slice B must not touch ``odds_match_results`` or
    ``manual_match_overrides`` (design §8, §11)."""
    odds_repo = OddsRowRepository(conn)
    overrides_repo = ManualMatchOverrideRepository(conn)

    # Seed an odds row and an active override.
    odds = odds_repo.create(
        slate_id=slate_id,
        fighter_name_raw="Jon Doe",
        american_odds=-150,
        source="manual",
        captured_at="2026-05-22T12:00:00Z",
    )
    overrides_repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=odds.odds_row_key,
        reason="seeded for test",
    )
    odds_before = odds_repo.list_for_slate(slate_id)
    overrides_before = overrides_repo.list_active_for_slate(slate_id)
    match_results_before = conn.execute(
        "SELECT COUNT(*) FROM odds_match_results WHERE slate_id = ?",
        (slate_id,),
    ).fetchone()[0]

    FighterRepository(conn).upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9000, source_row_number=1),
            _parsed("Jane Roe", 8500, source_row_number=2),
        ],
    )

    assert odds_repo.list_for_slate(slate_id) == odds_before
    assert overrides_repo.list_active_for_slate(slate_id) == overrides_before
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM odds_match_results WHERE slate_id = ?",
            (slate_id,),
        ).fetchone()[0]
        == match_results_before
    )


def test_upsert_updates_directly_seeded_rows(conn, slate_id):
    """A fighter row written outside the importer (raw SQL, fixture seed)
    must still be picked up by ``upsert_for_slate`` as an existing row —
    the importer matches on ``(slate_id, name)``, not on prior knowledge
    of who wrote the row."""
    repo = FighterRepository(conn)
    _insert_fighter(conn, slate_id=slate_id, name="Existing F", salary=8000)

    result = repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[_parsed("Existing F", 8800)],
    )
    assert result == FighterUpsertResult(
        inserted=0, updated=1, unchanged=0, deactivated=0
    )
    rows = repo.list_for_slate(slate_id)
    assert len(rows) == 1
    assert rows[0].salary == 8800


# ---------------------------------------------------------------------------
# game_info persistence — B2 of DK_GAME_INFO_PAIRING_DESIGN.md (§2.3, §2.4)
# ---------------------------------------------------------------------------


def test_upsert_persists_game_info_on_insert(conn, slate_id):
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9000, game_info="Doe@Roe 05/22/2026 7:00PM ET")
        ],
    )
    stored = conn.execute(
        "SELECT game_info FROM fighters WHERE slate_id = ? AND name = 'Jon Doe'",
        (slate_id,),
    ).fetchone()[0]
    assert stored == "Doe@Roe 05/22/2026 7:00PM ET"
    assert repo.list_for_slate(slate_id)[0].game_info == (
        "Doe@Roe 05/22/2026 7:00PM ET"
    )


def test_upsert_persists_null_game_info_when_blank(conn, slate_id):
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[_parsed("No GI", 9000, game_info=None)],
    )
    stored = conn.execute(
        "SELECT game_info FROM fighters WHERE slate_id = ? AND name = 'No GI'",
        (slate_id,),
    ).fetchone()[0]
    assert stored is None
    assert repo.list_for_slate(slate_id)[0].game_info is None


def test_list_for_slate_surfaces_game_info(conn, slate_id):
    """``FighterRecord`` now carries ``game_info`` (design §2.3, test 6)."""
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed(
                "Has GI", 9000, source_row_number=1, game_info="A@B 05/22/2026"
            ),
            _parsed("No GI", 8500, source_row_number=2, game_info=None),
        ],
    )
    by_name = {r.name: r.game_info for r in repo.list_for_slate(slate_id)}
    assert by_name == {"Has GI": "A@B 05/22/2026", "No GI": None}


def test_upsert_unchanged_includes_game_info(conn, slate_id):
    """Re-importing an identical file (same ``game_info``) is a no-op:
    every row is ``unchanged`` and ``updated == 0`` (design §2.4, test 5)."""
    repo = FighterRepository(conn)
    parsed = [
        _parsed("Jon Doe", 9000, source_row_number=1, game_info="X@Y 05/22/2026"),
        _parsed("Jane Roe", 8500, source_row_number=2, game_info="X@Y 05/22/2026"),
    ]
    repo.upsert_for_slate(slate_id=slate_id, parsed_rows=parsed)
    result = repo.upsert_for_slate(slate_id=slate_id, parsed_rows=parsed)
    assert result == FighterUpsertResult(
        inserted=0, updated=0, unchanged=2, deactivated=0
    )


def test_upsert_changed_game_info_alone_counts_updated(conn, slate_id):
    """When only ``game_info`` differs (salary/position/status identical),
    the row is ``updated``, not ``unchanged`` (design §2.4)."""
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[_parsed("Jon Doe", 9000, game_info="OLD@TIME 05/22/2026")],
    )
    fid = repo.list_for_slate(slate_id)[0].id

    result = repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[_parsed("Jon Doe", 9000, game_info="NEW@TIME 05/23/2026")],
    )
    assert result == FighterUpsertResult(
        inserted=0, updated=1, unchanged=0, deactivated=0
    )
    rec = repo.list_for_slate(slate_id)[0]
    assert rec.id == fid  # id preserved → overrides not orphaned
    assert rec.salary == 9000
    assert rec.game_info == "NEW@TIME 05/23/2026"


def test_upsert_backfills_null_game_info_counts_updated(conn, slate_id):
    """A pre-feature row (``game_info IS NULL``) flips to ``updated`` with the
    captured value on the next re-import — the intended one-click backfill
    (design §2.4 / §2.5, test 4). Isolated so only ``game_info`` changes."""
    repo = FighterRepository(conn)
    _insert_fighter(conn, slate_id=slate_id, name="Existing F", salary=8000)
    assert (
        conn.execute(
            "SELECT game_info FROM fighters "
            "WHERE slate_id = ? AND name = 'Existing F'",
            (slate_id,),
        ).fetchone()[0]
        is None
    )

    # Same salary, same default position, still active — only game_info added.
    result = repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[_parsed("Existing F", 8000, game_info="A@B 05/22/2026")],
    )
    assert result == FighterUpsertResult(
        inserted=0, updated=1, unchanged=0, deactivated=0
    )
    assert repo.list_for_slate(slate_id)[0].game_info == "A@B 05/22/2026"


class _FlakyConn:
    """Thin wrapper that forwards every method the repository uses to a
    real ``sqlite3.Connection`` but raises on a chosen SQL/params combo.

    Lets us exercise the ``with self.conn:`` rollback path without
    monkey-patching the (read-only) ``Connection.execute`` attribute.
    """

    def __init__(self, real: sqlite3.Connection, should_fail) -> None:
        self._real = real
        self._should_fail = should_fail

    def execute(self, sql, params=()):
        if self._should_fail(sql, params):
            raise sqlite3.OperationalError("simulated mid-transaction failure")
        return self._real.execute(sql, params)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


def test_upsert_rolls_back_on_mid_transaction_failure(conn, slate_id):
    """Force the second INSERT to raise mid-pass. The surrounding
    ``with self.conn:`` block must roll back so neither the first INSERT
    nor any deactivation persists."""
    real_repo = FighterRepository(conn)
    real_repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[_parsed("Pre-existing", 8000)],
    )
    before = real_repo.list_for_slate(slate_id)

    def should_fail(sql, params):
        return (
            "INSERT INTO fighters" in sql
            and params
            and "Second New" in params
        )

    flaky_repo = FighterRepository(_FlakyConn(conn, should_fail))  # type: ignore[arg-type]
    with pytest.raises(sqlite3.OperationalError):
        flaky_repo.upsert_for_slate(
            slate_id=slate_id,
            parsed_rows=[
                _parsed("First New", 9000, source_row_number=1),
                _parsed("Second New", 8500, source_row_number=2),
                _parsed("Pre-existing", 8200, source_row_number=3),
            ],
        )

    # Nothing committed: "First New" / "Second New" absent; "Pre-existing"
    # still at 8000 (not bumped to 8200); no deactivation flipped.
    after = real_repo.list_for_slate(slate_id)
    assert [(r.name, r.salary, r.status) for r in after] == [
        (r.name, r.salary, r.status) for r in before
    ]
