"""Tests for the CSV odds save helper.

Covers the validated odds CSV → ``odds_rows`` write path exposed by the
Odds page. Match results, projections, optimizer linkage, and Odds API
integration are intentionally out of scope.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from src.db.repositories import OddsRowRepository, SlateRepository
from src.db.schema import apply_schema
from src.ingestion.odds_csv_save import (
    CsvOddsSaveResult,
    save_csv_odds_rows,
)
from src.ingestion.odds_row_key import compute_odds_row_key


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


def _df_min(**overrides) -> pd.DataFrame:
    """Two-row DataFrame in the v0 required schema (no optional cols)."""
    base = pd.DataFrame(
        [
            {
                "fighter": "Jon Jones",
                "moneyline": -200,
                "source": "oddsapi",
                "timestamp": "2026-05-20T12:00:00Z",
            },
            {
                "fighter": "Stipe Miocic",
                "moneyline": +170,
                "source": "oddsapi",
                "timestamp": "2026-05-20T12:00:00Z",
            },
        ]
    )
    for col, value in overrides.items():
        base[col] = value
    return base


def _df_full(**overrides) -> pd.DataFrame:
    """Two-row DataFrame including opponent + bookmaker columns."""
    base = pd.DataFrame(
        [
            {
                "fighter": "Jon Jones",
                "opponent": "Stipe Miocic",
                "moneyline": -200,
                "bookmaker": "DraftKings",
                "source": "oddsapi",
                "timestamp": "2026-05-20T12:00:00Z",
            },
            {
                "fighter": "Stipe Miocic",
                "opponent": "Jon Jones",
                "moneyline": +170,
                "bookmaker": "DraftKings",
                "source": "oddsapi",
                "timestamp": "2026-05-20T12:00:00Z",
            },
        ]
    )
    for col, value in overrides.items():
        base[col] = value
    return base


# --- happy path ---------------------------------------------------------


def test_save_inserts_minimum_schema_rows(conn, slate_id):
    repo = OddsRowRepository(conn)
    result = save_csv_odds_rows(repo, slate_id=slate_id, df=_df_min())
    assert isinstance(result, CsvOddsSaveResult)
    assert result.saved_count == 2
    assert result.existing_count == 0
    assert result.failure_count == 0

    stored = repo.list_for_slate(slate_id)
    assert {r.fighter_name_raw for r in stored} == {"Jon Jones", "Stipe Miocic"}
    # CSV-origin sources are prefixed to stay distinguishable from manual.
    assert {r.source for r in stored} == {"csv:oddsapi"}
    # No opponent / bookmaker in the minimum schema.
    assert all(r.opponent_name_raw is None for r in stored)
    assert all(r.bookmaker is None for r in stored)
    # implied_probability is computed by the repo from american_odds.
    jj = next(r for r in stored if r.fighter_name_raw == "Jon Jones")
    assert jj.american_odds == -200
    assert jj.implied_probability == pytest.approx(2.0 / 3.0, abs=1e-9)


def test_save_preserves_optional_columns(conn, slate_id):
    repo = OddsRowRepository(conn)
    result = save_csv_odds_rows(repo, slate_id=slate_id, df=_df_full())
    assert result.saved_count == 2

    stored = {r.fighter_name_raw: r for r in repo.list_for_slate(slate_id)}
    assert stored["Jon Jones"].opponent_name_raw == "Stipe Miocic"
    assert stored["Jon Jones"].bookmaker == "DraftKings"
    assert stored["Stipe Miocic"].opponent_name_raw == "Jon Jones"
    assert stored["Stipe Miocic"].bookmaker == "DraftKings"


def test_save_uses_stable_csv_row_key(conn, slate_id):
    repo = OddsRowRepository(conn)
    save_csv_odds_rows(repo, slate_id=slate_id, df=_df_full())
    rec = next(
        r for r in repo.list_for_slate(slate_id) if r.fighter_name_raw == "Jon Jones"
    )
    assert rec.odds_row_key == compute_odds_row_key(
        fighter_name="Jon Jones",
        bookmaker="DraftKings",
        source="csv:oddsapi",
        captured_at="2026-05-20T12:00:00Z",
    )


def test_save_blank_source_falls_back_to_csv_label(conn, slate_id):
    repo = OddsRowRepository(conn)
    df = _df_min()
    df.loc[0, "source"] = ""  # row 0 has blank source
    result = save_csv_odds_rows(repo, slate_id=slate_id, df=df)
    assert result.saved_count == 2
    sources = {r.source for r in repo.list_for_slate(slate_id)}
    assert sources == {"csv", "csv:oddsapi"}


def test_save_stamps_import_batch_id_when_provided(conn, slate_id):
    repo = OddsRowRepository(conn)
    save_csv_odds_rows(
        repo, slate_id=slate_id, df=_df_min(), import_batch_id="batch-abc"
    )
    stored = repo.list_for_slate(slate_id)
    assert {r.import_batch_id for r in stored} == {"batch-abc"}


def test_save_without_batch_id_leaves_column_null(conn, slate_id):
    repo = OddsRowRepository(conn)
    save_csv_odds_rows(repo, slate_id=slate_id, df=_df_min())
    stored = repo.list_for_slate(slate_id)
    assert all(r.import_batch_id is None for r in stored)


def test_save_handles_blank_optional_cells(conn, slate_id):
    """Optional columns may be present-but-empty for some rows."""
    repo = OddsRowRepository(conn)
    df = _df_full()
    df.loc[0, "bookmaker"] = ""
    df.loc[0, "opponent"] = "   "
    result = save_csv_odds_rows(repo, slate_id=slate_id, df=df)
    assert result.saved_count == 2
    jj = next(
        r for r in repo.list_for_slate(slate_id) if r.fighter_name_raw == "Jon Jones"
    )
    assert jj.bookmaker is None
    assert jj.opponent_name_raw is None


# --- idempotency --------------------------------------------------------


def test_save_is_idempotent_for_repeat_uploads(conn, slate_id):
    repo = OddsRowRepository(conn)
    first = save_csv_odds_rows(repo, slate_id=slate_id, df=_df_full())
    second = save_csv_odds_rows(repo, slate_id=slate_id, df=_df_full())
    assert first.saved_count == 2 and first.existing_count == 0
    assert second.saved_count == 0 and second.existing_count == 2
    # Only one physical row per (slate, key).
    assert len(repo.list_for_slate(slate_id)) == 2


def test_partial_overlap_reuses_existing_and_inserts_new(conn, slate_id):
    repo = OddsRowRepository(conn)
    save_csv_odds_rows(repo, slate_id=slate_id, df=_df_full())
    # Same rows + a new third entry at a later timestamp.
    df2 = pd.concat(
        [
            _df_full(),
            pd.DataFrame(
                [
                    {
                        "fighter": "Daniel Cormier",
                        "opponent": "Derrick Lewis",
                        "moneyline": -300,
                        "bookmaker": "DraftKings",
                        "source": "oddsapi",
                        "timestamp": "2026-05-20T18:00:00Z",
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    result = save_csv_odds_rows(repo, slate_id=slate_id, df=df2)
    assert result.saved_count == 1
    assert result.existing_count == 2
    assert len(repo.list_for_slate(slate_id)) == 3


# --- slate scoping ------------------------------------------------------


def test_rows_are_scoped_by_slate(conn, slate_id, other_slate_id):
    repo = OddsRowRepository(conn)
    save_csv_odds_rows(repo, slate_id=slate_id, df=_df_full())
    save_csv_odds_rows(repo, slate_id=other_slate_id, df=_df_full())

    s1 = repo.list_for_slate(slate_id)
    s2 = repo.list_for_slate(other_slate_id)
    assert len(s1) == 2 and len(s2) == 2
    assert {r.slate_id for r in s1} == {slate_id}
    assert {r.slate_id for r in s2} == {other_slate_id}


# --- per-row failures ---------------------------------------------------


def test_invalid_moneyline_row_does_not_abort_batch(conn, slate_id):
    repo = OddsRowRepository(conn)
    df = _df_min()
    df.loc[0, "moneyline"] = 0  # rejected by repo CHECK + helper validation
    result = save_csv_odds_rows(repo, slate_id=slate_id, df=df)
    assert result.saved_count == 1
    assert result.failure_count == 1
    bad_label, _msg = result.failures[0]
    assert bad_label == "Jon Jones"
    # Only the good row landed.
    stored = repo.list_for_slate(slate_id)
    assert {r.fighter_name_raw for r in stored} == {"Stipe Miocic"}


def test_invalid_timestamp_row_does_not_abort_batch(conn, slate_id):
    repo = OddsRowRepository(conn)
    df = _df_min()
    df.loc[1, "timestamp"] = "not-an-iso-timestamp"
    result = save_csv_odds_rows(repo, slate_id=slate_id, df=df)
    assert result.saved_count == 1
    assert result.failure_count == 1
    bad_label, msg = result.failures[0]
    assert bad_label == "Stipe Miocic"
    assert "iso-8601" in msg.lower()
    assert {r.fighter_name_raw for r in repo.list_for_slate(slate_id)} == {
        "Jon Jones"
    }


def test_blank_fighter_name_row_fails_with_indexed_label(conn, slate_id):
    repo = OddsRowRepository(conn)
    df = _df_min()
    df.loc[0, "fighter"] = "   "
    result = save_csv_odds_rows(repo, slate_id=slate_id, df=df)
    assert result.saved_count == 1
    assert result.failure_count == 1
    bad_label, _msg = result.failures[0]
    # Falls back to a row-index label when the fighter is missing.
    assert bad_label.startswith("row #")


def test_empty_dataframe_returns_empty_result(conn, slate_id):
    repo = OddsRowRepository(conn)
    df = pd.DataFrame(columns=["fighter", "moneyline", "source", "timestamp"])
    result = save_csv_odds_rows(repo, slate_id=slate_id, df=df)
    assert result.saved_count == 0
    assert result.existing_count == 0
    assert result.failure_count == 0
