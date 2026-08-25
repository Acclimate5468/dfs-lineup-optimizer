"""Phase B tests for Fighter Status v1 persistence.

Covers ``docs/FIGHTER_STATUS_V1_DESIGN.md`` §13.2 (Option B schema
additions), §15 Phase B (schema/migration/repository), §16 (test plan),
§19.4 (idempotence timestamp policy), and §19.5 (re-import safety).

Phase B is persistence only:

- ``fighters.manual_status`` / ``fighters.manual_status_set_at`` columns
  exist after ``apply_schema``.
- ``apply_pending_migrations`` adds the same columns to a DB created
  from a pre-migration schema, without touching existing rows.
- ``FighterRepository.set_manual_status`` /
  ``clear_manual_status`` write the columns, validate the value via
  the Phase A taxonomy, and keep
  ``odds_match_results.effective_status`` / ``manual_match_overrides``
  rows untouched (§8).
- Salary re-import (``FighterRepository.upsert_for_slate``) does not
  silently clobber a user override (§13.2, §19.5).

Phase B does NOT wire ``manual_status`` into projections, alerts, the
manual review gate, the optimizer, or exports — those are gated Phase F
slices.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.migrations import apply_pending_migrations
from src.db.repositories import (
    FighterManualStatusRecord,
    FighterRepository,
    FighterUpsertResult,
    ManualMatchOverrideRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.dk_salary_importer import ParsedSalaryRow
from src.slate import fighter_status as fs


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


def _seed_fighter(conn: sqlite3.Connection, *, slate_id: int, name: str) -> int:
    FighterRepository(conn).upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed(name, 9000)]
    )
    return next(
        r.id for r in FighterRepository(conn).list_for_slate(slate_id)
        if r.name == name
    )


def _read_manual(conn: sqlite3.Connection, fighter_id: int):
    return conn.execute(
        "SELECT manual_status, manual_status_set_at "
        "FROM fighters WHERE id = ?",
        (int(fighter_id),),
    ).fetchone()


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    """Return {column_name: (type, notnull, dflt_value, pk)} for a table."""
    return {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


# ---------------------------------------------------------------------------
# schema (after apply_schema on a fresh DB)
# ---------------------------------------------------------------------------


def test_apply_schema_creates_manual_status_columns(conn):
    cols = _columns(conn, "fighters")
    assert "manual_status" in cols
    assert "manual_status_set_at" in cols


def test_apply_schema_manual_status_columns_are_nullable(conn):
    # (type, notnull, dflt, pk) — notnull index 1, default index 2.
    cols = _columns(conn, "fighters")
    assert cols["manual_status"][1] == 0
    assert cols["manual_status_set_at"][1] == 0
    # No DEFAULT — NULL is "no user override" per §13.2.
    assert cols["manual_status"][2] is None
    assert cols["manual_status_set_at"][2] is None


def test_apply_schema_manual_status_columns_text_type(conn):
    cols = _columns(conn, "fighters")
    assert cols["manual_status"][0].upper() == "TEXT"
    assert cols["manual_status_set_at"][0].upper() == "TEXT"


def test_apply_schema_idempotent_on_columns(conn):
    apply_schema(conn)
    cols = _columns(conn, "fighters")
    assert "manual_status" in cols
    assert "manual_status_set_at" in cols


# ---------------------------------------------------------------------------
# migration (apply_pending_migrations on a pre-Phase-B DB)
# ---------------------------------------------------------------------------


PRE_PHASE_B_FIGHTERS_SQL = """
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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(slate_id, name)
)
"""


@pytest.fixture
def pre_phase_b_conn():
    """A DB whose ``fighters`` table lacks the Phase B override columns.

    Mirrors what a user upgrading from a pre-Phase-B build will have on
    disk. ``apply_pending_migrations`` must turn this into the
    post-Phase-B shape without losing the seeded rows.
    """
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(PRE_PHASE_B_FIGHTERS_SQL)
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


def test_migration_adds_manual_status_columns(pre_phase_b_conn):
    cols_before = _columns(pre_phase_b_conn, "fighters")
    assert "manual_status" not in cols_before
    assert "manual_status_set_at" not in cols_before

    apply_pending_migrations(pre_phase_b_conn)

    cols_after = _columns(pre_phase_b_conn, "fighters")
    assert "manual_status" in cols_after
    assert "manual_status_set_at" in cols_after
    assert cols_after["manual_status"][1] == 0
    assert cols_after["manual_status_set_at"][1] == 0


def test_migration_preserves_existing_rows(pre_phase_b_conn):
    before_rows = pre_phase_b_conn.execute(
        "SELECT id, name, salary, status FROM fighters ORDER BY id"
    ).fetchall()

    apply_pending_migrations(pre_phase_b_conn)

    after_rows = pre_phase_b_conn.execute(
        "SELECT id, name, salary, status FROM fighters ORDER BY id"
    ).fetchall()
    assert before_rows == after_rows


def test_migration_sets_manual_status_to_null_for_existing_rows(pre_phase_b_conn):
    apply_pending_migrations(pre_phase_b_conn)
    rows = pre_phase_b_conn.execute(
        "SELECT manual_status, manual_status_set_at FROM fighters"
    ).fetchall()
    assert rows  # sanity
    assert all(ms is None and ts is None for ms, ts in rows)


def test_migration_is_idempotent(pre_phase_b_conn):
    apply_pending_migrations(pre_phase_b_conn)
    # Second call must not raise (no "duplicate column" error).
    apply_pending_migrations(pre_phase_b_conn)
    cols = _columns(pre_phase_b_conn, "fighters")
    assert "manual_status" in cols and "manual_status_set_at" in cols


def test_migration_is_noop_on_fresh_schema(conn):
    """Running the migration on a DB already at the post-Phase-B shape
    must not raise and must not change any row state."""
    fid = _seed_fighter(conn, slate_id=SlateRepository(conn).create(
        event_name="UFC NOOP").id, name="Already Migrated")
    before = conn.execute(
        "SELECT id, name, salary, status, manual_status, manual_status_set_at "
        "FROM fighters WHERE id = ?",
        (fid,),
    ).fetchone()
    apply_pending_migrations(conn)
    after = conn.execute(
        "SELECT id, name, salary, status, manual_status, manual_status_set_at "
        "FROM fighters WHERE id = ?",
        (fid,),
    ).fetchone()
    assert before == after


# ---------------------------------------------------------------------------
# set_manual_status — happy path & validation
# ---------------------------------------------------------------------------


def test_set_manual_status_persists_value(conn, slate_id):
    fid = _seed_fighter(conn, slate_id=slate_id, name="Jon Doe")

    result = FighterRepository(conn).set_manual_status(
        slate_id=slate_id, fighter_id=fid, status=fs.OUT
    )
    assert isinstance(result, FighterManualStatusRecord)
    assert result.fighter_id == fid
    assert result.slate_id == slate_id
    assert result.manual_status == "out"
    assert isinstance(result.manual_status_set_at, str) and result.manual_status_set_at

    persisted = _read_manual(conn, fid)
    assert persisted[0] == "out"
    assert persisted[1] is not None and persisted[1] != ""


def test_set_manual_status_does_not_change_importer_base_status(conn, slate_id):
    fid = _seed_fighter(conn, slate_id=slate_id, name="Jon Doe")
    importer_status_before = conn.execute(
        "SELECT status FROM fighters WHERE id = ?", (fid,)
    ).fetchone()[0]
    FighterRepository(conn).set_manual_status(
        slate_id=slate_id, fighter_id=fid, status=fs.QUESTIONABLE
    )
    importer_status_after = conn.execute(
        "SELECT status FROM fighters WHERE id = ?", (fid,)
    ).fetchone()[0]
    assert importer_status_before == importer_status_after == "active"


@pytest.mark.parametrize(
    "status",
    sorted(fs.ALLOWED_STATUSES),
)
def test_set_manual_status_accepts_every_v1_value(conn, slate_id, status):
    fid = _seed_fighter(conn, slate_id=slate_id, name=f"Acc {status}")
    FighterRepository(conn).set_manual_status(
        slate_id=slate_id, fighter_id=fid, status=status
    )
    assert _read_manual(conn, fid)[0] == status


def test_set_manual_status_rejects_unknown_value(conn, slate_id):
    fid = _seed_fighter(conn, slate_id=slate_id, name="Jon Doe")
    with pytest.raises(ValueError):
        FighterRepository(conn).set_manual_status(
            slate_id=slate_id, fighter_id=fid, status="late_replacement"
        )
    # No partial write.
    assert _read_manual(conn, fid) == (None, None)


def test_set_manual_status_rejects_empty_string(conn, slate_id):
    fid = _seed_fighter(conn, slate_id=slate_id, name="Jon Doe")
    with pytest.raises(ValueError):
        FighterRepository(conn).set_manual_status(
            slate_id=slate_id, fighter_id=fid, status=""
        )
    assert _read_manual(conn, fid) == (None, None)


def test_set_manual_status_rejects_missing_fighter(conn, slate_id):
    with pytest.raises(ValueError, match="does not exist"):
        FighterRepository(conn).set_manual_status(
            slate_id=slate_id, fighter_id=999_999, status=fs.OUT
        )


def test_set_manual_status_rejects_cross_slate_fighter(conn, slate_id, other_slate_id):
    fid = _seed_fighter(conn, slate_id=other_slate_id, name="Other Slate F")
    with pytest.raises(ValueError, match="belongs to slate"):
        FighterRepository(conn).set_manual_status(
            slate_id=slate_id, fighter_id=fid, status=fs.OUT
        )
    # And the row on the other slate remains untouched.
    assert _read_manual(conn, fid) == (None, None)


# ---------------------------------------------------------------------------
# clear_manual_status
# ---------------------------------------------------------------------------


def test_clear_manual_status_resets_columns_to_null(conn, slate_id):
    repo = FighterRepository(conn)
    fid = _seed_fighter(conn, slate_id=slate_id, name="Jon Doe")
    repo.set_manual_status(slate_id=slate_id, fighter_id=fid, status=fs.OUT)
    assert _read_manual(conn, fid)[0] == "out"

    result = repo.clear_manual_status(slate_id=slate_id, fighter_id=fid)
    assert result.manual_status is None
    assert result.manual_status_set_at is None
    assert _read_manual(conn, fid) == (None, None)


def test_clear_manual_status_is_idempotent_on_unset_row(conn, slate_id):
    fid = _seed_fighter(conn, slate_id=slate_id, name="Jon Doe")
    repo = FighterRepository(conn)
    # Cleared from never-set: still (None, None), no error.
    repo.clear_manual_status(slate_id=slate_id, fighter_id=fid)
    assert _read_manual(conn, fid) == (None, None)
    # And again.
    repo.clear_manual_status(slate_id=slate_id, fighter_id=fid)
    assert _read_manual(conn, fid) == (None, None)


def test_clear_manual_status_rejects_missing_fighter(conn, slate_id):
    with pytest.raises(ValueError, match="does not exist"):
        FighterRepository(conn).clear_manual_status(
            slate_id=slate_id, fighter_id=999_999
        )


def test_clear_manual_status_rejects_cross_slate_fighter(conn, slate_id, other_slate_id):
    fid = _seed_fighter(conn, slate_id=other_slate_id, name="Other Slate F")
    FighterRepository(conn).set_manual_status(
        slate_id=other_slate_id, fighter_id=fid, status=fs.OUT
    )
    with pytest.raises(ValueError, match="belongs to slate"):
        FighterRepository(conn).clear_manual_status(
            slate_id=slate_id, fighter_id=fid
        )
    # Other-slate override survived the rejection.
    assert _read_manual(conn, fid)[0] == "out"


def test_clear_after_set_returns_resolver_to_importer_base(conn, slate_id):
    """§13.2: clearing should let the effective status revert to the
    importer-owned base when read through the Phase A resolver."""
    repo = FighterRepository(conn)
    fid = _seed_fighter(conn, slate_id=slate_id, name="Jon Doe")
    repo.set_manual_status(slate_id=slate_id, fighter_id=fid, status=fs.OUT)
    repo.clear_manual_status(slate_id=slate_id, fighter_id=fid)

    importer, manual = conn.execute(
        "SELECT status, manual_status FROM fighters WHERE id = ?", (fid,)
    ).fetchone()
    assert fs.resolve_effective_fighter_status(importer, manual) == "active"


# ---------------------------------------------------------------------------
# idempotence (§19.4)
# ---------------------------------------------------------------------------


def test_set_same_value_twice_does_not_change_status_column(conn, slate_id):
    repo = FighterRepository(conn)
    fid = _seed_fighter(conn, slate_id=slate_id, name="Jon Doe")
    repo.set_manual_status(slate_id=slate_id, fighter_id=fid, status=fs.OUT)
    first = _read_manual(conn, fid)
    repo.set_manual_status(slate_id=slate_id, fighter_id=fid, status=fs.OUT)
    second = _read_manual(conn, fid)
    # Status column unchanged (no-op value), timestamp policy is "may
    # refresh" — pin only the value-column half here.
    assert first[0] == second[0] == "out"


def test_set_same_value_does_not_duplicate_rows(conn, slate_id):
    fid = _seed_fighter(conn, slate_id=slate_id, name="Jon Doe")
    repo = FighterRepository(conn)
    repo.set_manual_status(slate_id=slate_id, fighter_id=fid, status=fs.OUT)
    repo.set_manual_status(slate_id=slate_id, fighter_id=fid, status=fs.OUT)
    repo.set_manual_status(slate_id=slate_id, fighter_id=fid, status=fs.OUT)
    n = conn.execute(
        "SELECT COUNT(*) FROM fighters WHERE id = ?", (fid,)
    ).fetchone()[0]
    assert n == 1


def test_set_then_change_status_overwrites_previous_value(conn, slate_id):
    repo = FighterRepository(conn)
    fid = _seed_fighter(conn, slate_id=slate_id, name="Jon Doe")
    repo.set_manual_status(slate_id=slate_id, fighter_id=fid, status=fs.QUESTIONABLE)
    repo.set_manual_status(slate_id=slate_id, fighter_id=fid, status=fs.OUT)
    assert _read_manual(conn, fid)[0] == "out"


# ---------------------------------------------------------------------------
# re-import safety (§13.2, §19.5)
# ---------------------------------------------------------------------------


def test_salary_reimport_does_not_clobber_manual_status(conn, slate_id):
    """The importer must never silently overwrite a user override."""
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9000, source_row_number=1),
            _parsed("Jane Roe", 8500, source_row_number=2),
        ],
    )
    jon_id = next(
        r.id for r in repo.list_for_slate(slate_id) if r.name == "Jon Doe"
    )
    repo.set_manual_status(slate_id=slate_id, fighter_id=jon_id, status=fs.OUT)
    manual_before = _read_manual(conn, jon_id)

    # Re-import with the same set of rows — no salary change.
    result = repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9000, source_row_number=1),
            _parsed("Jane Roe", 8500, source_row_number=2),
        ],
    )
    assert result == FighterUpsertResult(
        inserted=0, updated=0, unchanged=2, deactivated=0
    )

    assert _read_manual(conn, jon_id) == manual_before


def test_salary_reimport_with_changed_salary_preserves_manual_status(conn, slate_id):
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jon Doe", 9000)]
    )
    jon_id = repo.list_for_slate(slate_id)[0].id
    repo.set_manual_status(slate_id=slate_id, fighter_id=jon_id, status=fs.OUT)
    manual_before = _read_manual(conn, jon_id)

    # Salary change → UPDATE path; manual_status must survive.
    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jon Doe", 9300)]
    )
    assert _read_manual(conn, jon_id) == manual_before
    assert (
        conn.execute(
            "SELECT salary FROM fighters WHERE id = ?", (jon_id,)
        ).fetchone()[0]
        == 9300
    )


def test_salary_reimport_deactivation_preserves_manual_status(conn, slate_id):
    """When a fighter falls out of the salary CSV the importer flips
    ``status`` to ``inactive``; ``manual_status`` must not be cleared."""
    repo = FighterRepository(conn)
    repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9000, source_row_number=1),
            _parsed("Jane Roe", 8500, source_row_number=2),
        ],
    )
    jon_id = next(
        r.id for r in repo.list_for_slate(slate_id) if r.name == "Jon Doe"
    )
    repo.set_manual_status(slate_id=slate_id, fighter_id=jon_id, status=fs.OUT)
    manual_before = _read_manual(conn, jon_id)

    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jane Roe", 8500)]
    )

    assert _read_manual(conn, jon_id) == manual_before
    # Importer base flipped to inactive — that part is the existing contract.
    assert (
        conn.execute(
            "SELECT status FROM fighters WHERE id = ?", (jon_id,)
        ).fetchone()[0]
        == "inactive"
    )


def test_salary_reimport_reactivation_preserves_manual_status(conn, slate_id):
    """A fighter re-appearing in the CSV after being marked inactive must
    keep their existing manual override (§19.5: importer base resurfaces
    when the user later clears the override, not on re-import)."""
    repo = FighterRepository(conn)
    repo.upsert_for_slate(slate_id=slate_id, parsed_rows=[_parsed("Jon Doe", 9000)])
    jon_id = repo.list_for_slate(slate_id)[0].id
    repo.set_manual_status(slate_id=slate_id, fighter_id=jon_id, status=fs.OUT)

    repo.upsert_for_slate(
        slate_id=slate_id, parsed_rows=[_parsed("Jane Roe", 8500)]
    )  # Jon → inactive (manual_status preserved)
    manual_after_deactivation = _read_manual(conn, jon_id)

    repo.upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=[
            _parsed("Jon Doe", 9100, source_row_number=1),
            _parsed("Jane Roe", 8500, source_row_number=2),
        ],
    )
    assert _read_manual(conn, jon_id) == manual_after_deactivation
    assert (
        conn.execute(
            "SELECT status FROM fighters WHERE id = ?", (jon_id,)
        ).fetchone()[0]
        == "active"
    )


# ---------------------------------------------------------------------------
# disjoint from odds-match override layer (§8)
# ---------------------------------------------------------------------------


def test_set_manual_status_does_not_change_odds_or_overrides(conn, slate_id):
    """A Fighter Status write must never mutate
    ``odds_match_results`` or ``manual_match_overrides`` rows."""
    repo = FighterRepository(conn)
    fid = _seed_fighter(conn, slate_id=slate_id, name="Jon Doe")

    # Seed an odds row and an active reject_match override on a different
    # odds row so the supersede pass doesn't fire.
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
    odds_before = odds_repo.list_for_slate(slate_id)
    overrides_before = overrides_repo.list_active_for_slate(slate_id)
    match_results_before = conn.execute(
        "SELECT COUNT(*) FROM odds_match_results WHERE slate_id = ?",
        (slate_id,),
    ).fetchone()[0]

    repo.set_manual_status(slate_id=slate_id, fighter_id=fid, status=fs.OUT)
    repo.clear_manual_status(slate_id=slate_id, fighter_id=fid)
    repo.set_manual_status(slate_id=slate_id, fighter_id=fid, status=fs.QUESTIONABLE)

    assert odds_repo.list_for_slate(slate_id) == odds_before
    assert overrides_repo.list_active_for_slate(slate_id) == overrides_before
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM odds_match_results WHERE slate_id = ?",
            (slate_id,),
        ).fetchone()[0]
        == match_results_before
    )


def test_set_manual_status_does_not_change_other_fighters(conn, slate_id):
    repo = FighterRepository(conn)
    a = _seed_fighter(conn, slate_id=slate_id, name="A Fighter")
    b = _seed_fighter(conn, slate_id=slate_id, name="B Fighter")
    repo.set_manual_status(slate_id=slate_id, fighter_id=a, status=fs.OUT)
    assert _read_manual(conn, b) == (None, None)


def test_set_manual_status_on_one_slate_does_not_affect_another(conn, slate_id, other_slate_id):
    repo = FighterRepository(conn)
    a = _seed_fighter(conn, slate_id=slate_id, name="Shared Name")
    b = _seed_fighter(conn, slate_id=other_slate_id, name="Shared Name")
    repo.set_manual_status(slate_id=slate_id, fighter_id=a, status=fs.OUT)
    assert _read_manual(conn, b) == (None, None)
