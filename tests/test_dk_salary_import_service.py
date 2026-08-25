"""Tests for the Slice C salary import service.

Covers ``docs/SALARY_PERSISTENCE_DESIGN.md`` §9 Phase C: service-layer
composition of validate -> parse -> ``FighterRepository.upsert_for_slate``.
UI wiring (Phase D) is not exercised here — these tests run purely against
an in-memory SQLite database with no Streamlit fixtures.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.db.repositories import (
    FighterRepository,
    FighterUpsertResult,
    ManualMatchOverrideRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.dk_salary_import_service import (
    IMPORTED,
    PARSE_FAILED,
    VALIDATION_FAILED,
    SalaryImportResult,
    import_dk_salary_dataframe,
)
from src.ingestion.dk_salary_importer import REQUIRED_COLUMNS


# ---------------------------------------------------------------------------
# Fixtures + helpers
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
    return SlateRepository(conn).create(event_name="UFC 999").id


def _row(
    name: str,
    salary: str | int,
    *,
    position: str = "F",
    dk_id: str = "1",
    game_info: str = "Jon Doe@Jane Roe 05/22/2026",
    team: str = "ABC",
) -> dict:
    return {
        "Position": position,
        "Name": name,
        "ID": dk_id,
        "Salary": salary,
        "Game Info": game_info,
        "TeamAbbrev": team,
    }


def _valid_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(REQUIRED_COLUMNS))


class _FlakyConn:
    """Forwards every method the repository uses to a real
    ``sqlite3.Connection`` but raises on a chosen SQL/params combo.

    Mirrors the helper in ``tests/test_fighter_repository.py`` so we can
    exercise the upsert rollback path through the service entry point
    without monkey-patching ``Connection.execute``.
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_service_imports_valid_dataframe(conn, slate_id):
    df = _valid_df([_row("Jon Doe", "9000"), _row("Jane Roe", "8500")])

    result = import_dk_salary_dataframe(conn, slate_id=slate_id, df=df)

    assert isinstance(result, SalaryImportResult)
    assert result.status == IMPORTED
    assert result.error_message is None
    assert result.parsed_row_count == 2
    assert result.upsert == FighterUpsertResult(
        inserted=2, updated=0, unchanged=0, deactivated=0
    )

    fighters = FighterRepository(conn).list_for_slate(slate_id)
    assert {f.name for f in fighters} == {"Jon Doe", "Jane Roe"}
    assert all(f.status == "active" for f in fighters)
    assert {f.name: f.salary for f in fighters} == {
        "Jon Doe": 9000,
        "Jane Roe": 8500,
    }


def test_service_result_carries_parsed_row_count_and_upsert(conn, slate_id):
    df = _valid_df([_row("Jon Doe", "9000"), _row("Jane Roe", "8500")])

    result = import_dk_salary_dataframe(conn, slate_id=slate_id, df=df)

    # parsed_row_count comes from the parser (typed records actually built),
    # which equals validation.row_count on the happy path but is sourced
    # independently so the field stays meaningful if parser behavior diverges.
    assert result.parsed_row_count == 2
    assert result.validation.is_valid is True
    assert result.validation.row_count == 2
    assert isinstance(result.upsert, FighterUpsertResult)
    assert (
        result.upsert.inserted
        + result.upsert.updated
        + result.upsert.unchanged
        == result.parsed_row_count
    )


# ---------------------------------------------------------------------------
# Validation failure — no DB writes
# ---------------------------------------------------------------------------


def test_service_validation_failure_missing_column_does_not_persist(conn, slate_id):
    # Salary column missing → structural validator rejects.
    df = pd.DataFrame(
        [
            {
                "Position": "F",
                "Name": "Jon Doe",
                "ID": "1",
                "Game Info": "x",
                "TeamAbbrev": "JDO",
            }
        ]
    )

    result = import_dk_salary_dataframe(conn, slate_id=slate_id, df=df)

    assert result.status == VALIDATION_FAILED
    assert result.parsed_row_count == 0
    assert result.upsert is None
    assert result.error_message is not None
    assert "Salary" in result.error_message
    assert result.validation.is_valid is False
    assert "Salary" in result.validation.missing_columns

    assert FighterRepository(conn).list_for_slate(slate_id) == []


def test_service_validation_failure_header_only_does_not_persist(conn, slate_id):
    df = pd.DataFrame(columns=list(REQUIRED_COLUMNS))

    result = import_dk_salary_dataframe(conn, slate_id=slate_id, df=df)

    assert result.status == VALIDATION_FAILED
    assert result.parsed_row_count == 0
    assert result.upsert is None
    assert result.validation.row_count == 0
    assert FighterRepository(conn).list_for_slate(slate_id) == []


# ---------------------------------------------------------------------------
# Parse failure — no DB writes
# ---------------------------------------------------------------------------


def test_service_parse_failure_non_integer_salary_does_not_persist(conn, slate_id):
    df = _valid_df([_row("Jon Doe", "nine thousand")])

    result = import_dk_salary_dataframe(conn, slate_id=slate_id, df=df)

    assert result.status == PARSE_FAILED
    assert result.parsed_row_count == 0
    assert result.upsert is None
    assert result.error_message is not None
    assert "not an integer" in result.error_message
    # Structural validation passed before parsing kicked in.
    assert result.validation.is_valid is True

    assert FighterRepository(conn).list_for_slate(slate_id) == []


def test_service_parse_failure_duplicate_name_does_not_persist(conn, slate_id):
    df = _valid_df([_row("Jon Doe", "9000"), _row("Jon Doe", "8500", dk_id="2")])

    result = import_dk_salary_dataframe(conn, slate_id=slate_id, df=df)

    assert result.status == PARSE_FAILED
    assert result.parsed_row_count == 0
    assert result.upsert is None
    assert result.error_message is not None
    assert "duplicate" in result.error_message
    assert FighterRepository(conn).list_for_slate(slate_id) == []


def test_service_parse_failure_blank_name_does_not_persist(conn, slate_id):
    df = _valid_df([_row("   ", "9000")])

    result = import_dk_salary_dataframe(conn, slate_id=slate_id, df=df)

    assert result.status == PARSE_FAILED
    assert result.parsed_row_count == 0
    assert result.upsert is None
    assert result.error_message is not None
    assert "fighter name" in result.error_message
    assert FighterRepository(conn).list_for_slate(slate_id) == []


# ---------------------------------------------------------------------------
# Idempotence + update + deactivate semantics flow through the service path
# ---------------------------------------------------------------------------


def test_service_idempotent_repeat_returns_unchanged_counts(conn, slate_id):
    df = _valid_df([_row("Jon Doe", "9000"), _row("Jane Roe", "8500")])

    first = import_dk_salary_dataframe(conn, slate_id=slate_id, df=df)
    assert first.upsert == FighterUpsertResult(
        inserted=2, updated=0, unchanged=0, deactivated=0
    )
    snapshot = [
        (f.id, f.name, f.salary, f.status)
        for f in FighterRepository(conn).list_for_slate(slate_id)
    ]

    second = import_dk_salary_dataframe(conn, slate_id=slate_id, df=df)
    assert second.status == IMPORTED
    assert second.parsed_row_count == 2
    assert second.upsert == FighterUpsertResult(
        inserted=0, updated=0, unchanged=2, deactivated=0
    )
    # No row id churn between identical imports.
    assert snapshot == [
        (f.id, f.name, f.salary, f.status)
        for f in FighterRepository(conn).list_for_slate(slate_id)
    ]


def test_service_changed_salary_updates_through_service_path(conn, slate_id):
    import_dk_salary_dataframe(
        conn, slate_id=slate_id, df=_valid_df([_row("Jon Doe", "9000")])
    )
    fid_before = FighterRepository(conn).list_for_slate(slate_id)[0].id

    result = import_dk_salary_dataframe(
        conn, slate_id=slate_id, df=_valid_df([_row("Jon Doe", "9500")])
    )

    assert result.status == IMPORTED
    assert result.upsert == FighterUpsertResult(
        inserted=0, updated=1, unchanged=0, deactivated=0
    )
    rows = FighterRepository(conn).list_for_slate(slate_id)
    assert len(rows) == 1
    # id preserved so any future override remains attached.
    assert rows[0].id == fid_before
    assert rows[0].salary == 9500
    assert rows[0].status == "active"


def test_service_absent_fighter_marked_inactive_through_service_path(conn, slate_id):
    import_dk_salary_dataframe(
        conn,
        slate_id=slate_id,
        df=_valid_df([_row("Jon Doe", "9000"), _row("Jane Roe", "8500")]),
    )

    result = import_dk_salary_dataframe(
        conn,
        slate_id=slate_id,
        df=_valid_df([_row("Jane Roe", "8500")]),
    )

    assert result.status == IMPORTED
    assert result.upsert == FighterUpsertResult(
        inserted=0, updated=0, unchanged=1, deactivated=1
    )
    by_name = {
        r.name: r.status for r in FighterRepository(conn).list_for_slate(slate_id)
    }
    assert by_name == {"Jon Doe": "inactive", "Jane Roe": "active"}


# ---------------------------------------------------------------------------
# Unknown slate + repository-level errors
# ---------------------------------------------------------------------------


def test_service_unknown_slate_raises_without_changing_state(conn, slate_id):
    df = _valid_df([_row("Jon Doe", "9000")])

    with pytest.raises(ValueError, match="does not exist"):
        import_dk_salary_dataframe(conn, slate_id=999_999, df=df)

    # The legitimate slate must remain untouched.
    assert FighterRepository(conn).list_for_slate(slate_id) == []


# ---------------------------------------------------------------------------
# Transaction / rollback
# ---------------------------------------------------------------------------


def test_service_rolls_back_on_upsert_failure(conn, slate_id):
    """Force the second INSERT inside the upsert to raise. The repository's
    ``with self.conn:`` block — invoked from the service path — must roll
    back so neither the first INSERT nor any deactivation persists.
    """
    import_dk_salary_dataframe(
        conn, slate_id=slate_id, df=_valid_df([_row("Pre-existing", "8000")])
    )
    before = FighterRepository(conn).list_for_slate(slate_id)

    def should_fail(sql, params):
        return (
            "INSERT INTO fighters" in sql
            and params
            and "Second New" in params
        )

    flaky = _FlakyConn(conn, should_fail)
    df = _valid_df(
        [
            _row("First New", "9000"),
            _row("Second New", "8500", dk_id="2"),
            _row("Pre-existing", "8200", dk_id="3"),
        ]
    )

    with pytest.raises(sqlite3.OperationalError):
        import_dk_salary_dataframe(flaky, slate_id=slate_id, df=df)  # type: ignore[arg-type]

    after = FighterRepository(conn).list_for_slate(slate_id)
    # Nothing committed: First/Second New absent, Pre-existing still at 8000,
    # no deactivation flipped.
    assert [(r.name, r.salary, r.status) for r in after] == [
        (r.name, r.salary, r.status) for r in before
    ]


# ---------------------------------------------------------------------------
# Service must not touch odds / overrides
# ---------------------------------------------------------------------------


def test_service_does_not_touch_odds_or_overrides(conn, slate_id):
    """Design §8: a salary import never silently rewrites
    ``odds_match_results`` or ``manual_match_overrides``. The service
    must compose only the fighter write path."""
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
        reason="seeded for service test",
    )

    odds_before = odds_repo.list_for_slate(slate_id)
    overrides_before = overrides_repo.list_active_for_slate(slate_id)
    match_count_before = conn.execute(
        "SELECT COUNT(*) FROM odds_match_results WHERE slate_id = ?",
        (slate_id,),
    ).fetchone()[0]

    import_dk_salary_dataframe(
        conn,
        slate_id=slate_id,
        df=_valid_df([_row("Jon Doe", "9000"), _row("Jane Roe", "8500")]),
    )

    assert odds_repo.list_for_slate(slate_id) == odds_before
    assert overrides_repo.list_active_for_slate(slate_id) == overrides_before
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM odds_match_results WHERE slate_id = ?",
            (slate_id,),
        ).fetchone()[0]
        == match_count_before
    )


# ---------------------------------------------------------------------------
# Service is UI-free
# ---------------------------------------------------------------------------


def test_service_module_does_not_import_streamlit():
    """Slice C scope: service-layer only. The module must not import
    Streamlit (UI wiring is Slice D)."""
    import src.ingestion.dk_salary_import_service as svc

    source = Path(svc.__file__).read_text()
    assert "import streamlit" not in source
    assert "from streamlit" not in source
