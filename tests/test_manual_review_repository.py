"""Phase B tests for Manual Review Gate v1 persistence.

Covers ``docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`` §9.2 (Option B schema
additions), §10 Phase B (schema/migration/repository), §15 (test plan
— schema / migration / repository sections), §6 (idempotence timestamp
policy), and §15 cross-cutting (no projection / odds / fighter status
side effects).

Phase B is persistence only:

- ``slates.manual_review_status`` / ``slates.manual_review_completed_at``
  columns exist after ``apply_schema``.
- ``apply_pending_migrations`` adds the same columns to a DB created
  from a pre-Phase-B schema without touching existing rows.
- ``SlateRepository.set_manual_review_reviewed`` writes the columns in
  one transaction, validates the slate exists, and is idempotent on the
  value column while refreshing the timestamp (§6).
- Salary re-import (``FighterRepository.upsert_for_slate``) must not
  silently flip ``manual_review_status`` (§15 cross-cutting + §18.3
  open question contract).

Phase B does NOT wire ``manual_review_status`` into projections,
alerts, the optimizer, exports, or Fighter Status. Per §9.2 / §18.6,
``late_news_acknowledged_at`` is deferred to a follow-up slice.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.migrations import apply_pending_migrations
from src.db.repositories import (
    FighterRepository,
    ManualMatchOverrideRepository,
    OddsRowRepository,
    SlateRecord,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.dk_salary_importer import ParsedSalaryRow


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


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    """Return {column_name: (type, notnull, dflt_value, pk)} for a table."""
    return {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _read_review(conn: sqlite3.Connection, sid: int):
    return conn.execute(
        "SELECT manual_review_status, manual_review_completed_at "
        "FROM slates WHERE id = ?",
        (int(sid),),
    ).fetchone()


# ---------------------------------------------------------------------------
# schema (apply_schema on a fresh DB)
# ---------------------------------------------------------------------------


def test_apply_schema_creates_manual_review_columns(conn):
    cols = _columns(conn, "slates")
    assert "manual_review_status" in cols
    assert "manual_review_completed_at" in cols


def test_manual_review_status_is_not_null_with_default(conn):
    # (type, notnull, dflt_value, pk) — notnull index 1, default index 2.
    cols = _columns(conn, "slates")
    typ, notnull, default, _ = cols["manual_review_status"]
    assert typ.upper() == "TEXT"
    assert notnull == 1
    # SQLite reports DEFAULT for a string literal with surrounding quotes.
    assert default is not None
    assert "not_reviewed" in default


def test_manual_review_completed_at_is_nullable_with_no_default(conn):
    cols = _columns(conn, "slates")
    typ, notnull, default, _ = cols["manual_review_completed_at"]
    assert typ.upper() == "TEXT"
    assert notnull == 0
    assert default is None


def test_apply_schema_idempotent_on_manual_review_columns(conn):
    apply_schema(conn)
    cols = _columns(conn, "slates")
    assert "manual_review_status" in cols
    assert "manual_review_completed_at" in cols


def test_apply_schema_does_not_introduce_late_news_acknowledged_at(conn):
    """§9.2 / §18.6: Phase B deliberately defers the optional
    ``late_news_acknowledged_at`` column to a follow-up slice. Pin the
    deferral so a future contributor cannot silently widen the slice."""
    cols = _columns(conn, "slates")
    assert "late_news_acknowledged_at" not in cols


# ---------------------------------------------------------------------------
# migration (apply_pending_migrations on a pre-Phase-B DB)
# ---------------------------------------------------------------------------


PRE_PHASE_B_SLATES_SQL = """
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


@pytest.fixture
def pre_phase_b_conn():
    """A DB whose ``slates`` table lacks the Manual Review Phase B columns.

    Mirrors what a user upgrading from a pre-Phase-B build will have on
    disk. ``apply_pending_migrations`` must promote this to the
    post-Phase-B shape without losing the seeded rows.
    """
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(PRE_PHASE_B_SLATES_SQL)
    c.executemany(
        "INSERT INTO slates (event_name, event_date, salary_csv_status, "
        "salary_row_count) VALUES (?, ?, ?, ?)",
        [
            ("Pre Slate A", "2026-05-20", "validated", 26),
            ("Pre Slate B", None, "unvalidated", 0),
        ],
    )
    c.commit()
    try:
        yield c
    finally:
        c.close()


def test_migration_adds_manual_review_columns(pre_phase_b_conn):
    cols_before = _columns(pre_phase_b_conn, "slates")
    assert "manual_review_status" not in cols_before
    assert "manual_review_completed_at" not in cols_before

    apply_pending_migrations(pre_phase_b_conn)

    cols_after = _columns(pre_phase_b_conn, "slates")
    assert "manual_review_status" in cols_after
    assert "manual_review_completed_at" in cols_after
    assert cols_after["manual_review_status"][1] == 1  # NOT NULL
    assert cols_after["manual_review_completed_at"][1] == 0  # nullable


def test_migration_preserves_existing_slate_rows(pre_phase_b_conn):
    before = pre_phase_b_conn.execute(
        "SELECT id, event_name, event_date, salary_csv_status, "
        "salary_row_count FROM slates ORDER BY id"
    ).fetchall()

    apply_pending_migrations(pre_phase_b_conn)

    after = pre_phase_b_conn.execute(
        "SELECT id, event_name, event_date, salary_csv_status, "
        "salary_row_count FROM slates ORDER BY id"
    ).fetchall()
    assert before == after


def test_migration_defaults_existing_rows_to_not_reviewed(pre_phase_b_conn):
    apply_pending_migrations(pre_phase_b_conn)
    rows = pre_phase_b_conn.execute(
        "SELECT manual_review_status, manual_review_completed_at "
        "FROM slates ORDER BY id"
    ).fetchall()
    assert rows  # sanity — fixture seeded two slates
    for status, completed_at in rows:
        assert status == "not_reviewed"
        assert completed_at is None


def test_migration_is_idempotent_on_pre_phase_b_db(pre_phase_b_conn):
    apply_pending_migrations(pre_phase_b_conn)
    # Second call must not raise (no "duplicate column" error).
    apply_pending_migrations(pre_phase_b_conn)
    cols = _columns(pre_phase_b_conn, "slates")
    assert "manual_review_status" in cols
    assert "manual_review_completed_at" in cols


def test_migration_is_noop_on_fresh_schema(conn):
    """Running the migration on a DB already at the post-Phase-B shape
    must not raise and must not change any persisted row."""
    sid = SlateRepository(conn).create(event_name="UFC NOOP").id
    before = conn.execute(
        "SELECT id, event_name, event_date, salary_csv_status, "
        "salary_row_count, manual_review_status, "
        "manual_review_completed_at FROM slates WHERE id = ?",
        (sid,),
    ).fetchone()
    apply_pending_migrations(conn)
    after = conn.execute(
        "SELECT id, event_name, event_date, salary_csv_status, "
        "salary_row_count, manual_review_status, "
        "manual_review_completed_at FROM slates WHERE id = ?",
        (sid,),
    ).fetchone()
    assert before == after


# ---------------------------------------------------------------------------
# SlateRecord shape — new fields surfaced through create / list_all
# ---------------------------------------------------------------------------


def test_create_slate_defaults_to_not_reviewed_and_no_timestamp(conn):
    rec = SlateRepository(conn).create(event_name="UFC 999")
    assert isinstance(rec, SlateRecord)
    assert rec.manual_review_status == "not_reviewed"
    assert rec.manual_review_completed_at is None


def test_list_all_returns_manual_review_fields(conn):
    repo = SlateRepository(conn)
    a = repo.create(event_name="A")
    b = repo.create(event_name="B")
    rows = repo.list_all()
    by_id = {r.id: r for r in rows}
    for r in (a, b):
        assert by_id[r.id].manual_review_status == "not_reviewed"
        assert by_id[r.id].manual_review_completed_at is None


# ---------------------------------------------------------------------------
# set_manual_review_reviewed — happy path
# ---------------------------------------------------------------------------


def test_set_manual_review_reviewed_persists_status_and_timestamp(conn, slate_id):
    rec = SlateRepository(conn).set_manual_review_reviewed(slate_id)
    assert isinstance(rec, SlateRecord)
    assert rec.id == slate_id
    assert rec.manual_review_status == "reviewed"
    assert isinstance(rec.manual_review_completed_at, str)
    assert rec.manual_review_completed_at  # non-empty timestamp

    persisted = _read_review(conn, slate_id)
    assert persisted[0] == "reviewed"
    assert persisted[1] is not None and persisted[1] != ""


def test_set_manual_review_reviewed_does_not_change_other_slate_fields(conn, slate_id):
    before = conn.execute(
        "SELECT event_name, event_date, salary_csv_status, salary_row_count, "
        "created_at FROM slates WHERE id = ?",
        (slate_id,),
    ).fetchone()
    SlateRepository(conn).set_manual_review_reviewed(slate_id)
    after = conn.execute(
        "SELECT event_name, event_date, salary_csv_status, salary_row_count, "
        "created_at FROM slates WHERE id = ?",
        (slate_id,),
    ).fetchone()
    assert before == after


def test_set_manual_review_reviewed_rejects_missing_slate(conn):
    with pytest.raises(ValueError, match="does not exist"):
        SlateRepository(conn).set_manual_review_reviewed(999_999)


def test_set_manual_review_reviewed_does_not_persist_on_missing_slate(conn):
    before_rows = conn.execute(
        "SELECT id, manual_review_status, manual_review_completed_at "
        "FROM slates"
    ).fetchall()
    with pytest.raises(ValueError):
        SlateRepository(conn).set_manual_review_reviewed(424_242)
    after_rows = conn.execute(
        "SELECT id, manual_review_status, manual_review_completed_at "
        "FROM slates"
    ).fetchall()
    assert before_rows == after_rows


# ---------------------------------------------------------------------------
# idempotence (§6)
# ---------------------------------------------------------------------------


def test_set_manual_review_reviewed_is_idempotent_on_value_column(conn, slate_id):
    repo = SlateRepository(conn)
    repo.set_manual_review_reviewed(slate_id)
    first = _read_review(conn, slate_id)
    repo.set_manual_review_reviewed(slate_id)
    second = _read_review(conn, slate_id)
    # Status value column unchanged — design §6 "no-op on the value column".
    assert first[0] == second[0] == "reviewed"


def test_set_manual_review_reviewed_does_not_duplicate_slate_rows(conn, slate_id):
    repo = SlateRepository(conn)
    repo.set_manual_review_reviewed(slate_id)
    repo.set_manual_review_reviewed(slate_id)
    repo.set_manual_review_reviewed(slate_id)
    n = conn.execute(
        "SELECT COUNT(*) FROM slates WHERE id = ?", (slate_id,)
    ).fetchone()[0]
    assert n == 1


def test_set_manual_review_reviewed_refreshes_timestamp_on_recall(conn, slate_id):
    """§6: re-clicking refreshes ``manual_review_completed_at`` so a
    "last reviewed at" surface stays useful (mirrors
    ``FIGHTER_STATUS_V1_DESIGN`` §19.4 timestamp-refresh policy)."""
    repo = SlateRepository(conn)
    repo.set_manual_review_reviewed(slate_id)
    first_ts = _read_review(conn, slate_id)[1]
    assert first_ts is not None

    # Force the SQLite ``datetime('now')`` second to roll over so the
    # refresh is observable. Backdate the first timestamp by one second.
    conn.execute(
        "UPDATE slates "
        "SET manual_review_completed_at = "
        "datetime(manual_review_completed_at, '-1 seconds') "
        "WHERE id = ?",
        (slate_id,),
    )
    conn.commit()
    backdated = _read_review(conn, slate_id)[1]
    assert backdated != first_ts

    repo.set_manual_review_reviewed(slate_id)
    refreshed = _read_review(conn, slate_id)[1]
    # The refresh writes a fresh ``datetime('now')`` value strictly newer
    # than the backdated one.
    assert refreshed > backdated
    # Status remained at 'reviewed' throughout (idempotent value).
    assert _read_review(conn, slate_id)[0] == "reviewed"


# ---------------------------------------------------------------------------
# no side effects (§15 cross-cutting)
# ---------------------------------------------------------------------------


def test_set_manual_review_reviewed_does_not_change_other_slates(conn, slate_id, other_slate_id):
    before_other = _read_review(conn, other_slate_id)
    SlateRepository(conn).set_manual_review_reviewed(slate_id)
    after_other = _read_review(conn, other_slate_id)
    assert before_other == after_other == ("not_reviewed", None)


def test_set_manual_review_reviewed_does_not_touch_other_tables(conn, slate_id):
    """§15 cross-cutting + §14: a Manual Review write must not mutate
    odds_match_results / manual_match_overrides / fighters rows."""
    # Seed a fighter, an odds row, and an active reject_match override
    # so we have something to leak into if the write were over-broad.
    FighterRepository(conn).upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[_parsed("Jon Doe", 9000)],
    )
    odds_repo = OddsRowRepository(conn)
    overrides_repo = ManualMatchOverrideRepository(conn)
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

    fighters_before = conn.execute(
        "SELECT id, name, salary, status, manual_status, manual_status_set_at "
        "FROM fighters WHERE slate_id = ? ORDER BY id",
        (slate_id,),
    ).fetchall()
    odds_before = odds_repo.list_for_slate(slate_id)
    overrides_before = overrides_repo.list_active_for_slate(slate_id)
    match_results_before = conn.execute(
        "SELECT COUNT(*) FROM odds_match_results WHERE slate_id = ?",
        (slate_id,),
    ).fetchone()[0]

    SlateRepository(conn).set_manual_review_reviewed(slate_id)

    fighters_after = conn.execute(
        "SELECT id, name, salary, status, manual_status, manual_status_set_at "
        "FROM fighters WHERE slate_id = ? ORDER BY id",
        (slate_id,),
    ).fetchall()
    assert fighters_after == fighters_before
    assert odds_repo.list_for_slate(slate_id) == odds_before
    assert overrides_repo.list_active_for_slate(slate_id) == overrides_before
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM odds_match_results WHERE slate_id = ?",
            (slate_id,),
        ).fetchone()[0]
        == match_results_before
    )


# ---------------------------------------------------------------------------
# re-import safety (§15 / §18.3 / §18.9)
# ---------------------------------------------------------------------------


def test_salary_reimport_does_not_change_manual_review_status(conn, slate_id):
    """§15 / §18.3 / §18.9 contract: the importer must never silently
    flip ``manual_review_status``. v1 does NOT auto-invalidate on
    upstream writes — re-review is a user-driven action."""
    slates = SlateRepository(conn)
    fighters = FighterRepository(conn)
    fighters.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9000, source_row_number=1),
            _parsed("Jane Roe", 8500, source_row_number=2),
        ],
    )
    slates.set_manual_review_reviewed(slate_id)
    before = _read_review(conn, slate_id)
    assert before[0] == "reviewed"
    assert before[1] is not None

    # No-change re-import.
    fighters.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9000, source_row_number=1),
            _parsed("Jane Roe", 8500, source_row_number=2),
        ],
    )
    assert _read_review(conn, slate_id) == before

    # Salary-change re-import (UPDATE path).
    fighters.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9300, source_row_number=1),
            _parsed("Jane Roe", 8500, source_row_number=2),
        ],
    )
    assert _read_review(conn, slate_id) == before

    # Deactivating re-import (Jon drops out).
    fighters.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jane Roe", 8500)]
    )
    assert _read_review(conn, slate_id) == before


# ---------------------------------------------------------------------------
# pre-write read state (§5.9 / §15 — default 'not_reviewed' surfaces correctly)
# ---------------------------------------------------------------------------


def test_fresh_slate_reads_as_not_reviewed(conn):
    rec = SlateRepository(conn).create(event_name="UFC NEW")
    persisted = _read_review(conn, rec.id)
    assert persisted == ("not_reviewed", None)
    # And surfaces through the record dataclass identically.
    assert rec.manual_review_status == "not_reviewed"
    assert rec.manual_review_completed_at is None
