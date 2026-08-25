"""Tests for the DraftKings paste save helper (Odds Acquisition v0 Phase 3A).

Covers the previewed DraftKings-paste rows → ``odds_rows`` + recompute write
path exposed by Build Step 2 (``src/ingestion/draftkings_paste_save.py``).
Pins ``docs/ODDS_ACQUISITION_V0_DESIGN.md`` §2 / Phase 3 reuse of the existing
storage/match/recompute pipeline: path-labelled ``source="draftkings_paste"``
rows, ``bookmaker="DraftKings"``, opponent preserved, idempotent re-save, a
chained recompute (with ``recompute_error`` on an empty roster), per-row
failure isolation, slate scoping, and the non-persisted ``source_url`` echo.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.draftkings_paste_save import (
    SOURCE_DRAFTKINGS_PASTE,
    DraftKingsPasteSaveResult,
    save_draftkings_paste_rows,
)
from src.ingestion.odds_row_key import compute_odds_row_key

_CAPTURED_AT = "2026-06-06T16:00:00+00:00"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    apply_schema(c)
    try:
        yield c
    finally:
        c.close()


def _slate_with_fighters(conn, names: list[str], *, event: str = "UFC 999") -> int:
    sid = SlateRepository(conn).create(event_name=event).id
    for i, name in enumerate(names):
        conn.execute(
            "INSERT INTO fighters (slate_id, name, salary, status) "
            "VALUES (?, ?, ?, 'active')",
            (sid, name, 8000 + i),
        )
    conn.commit()
    return sid


def _rows() -> list[dict]:
    """One fight → two paired preview rows (the Build payload shape)."""
    return [
        {
            "fighter_name": "Matt Schnell",
            "opponent": "Alessandro Costa",
            "american_moneyline": 525,
            "source": "DraftKings",
            "book": "DraftKings",
        },
        {
            "fighter_name": "Alessandro Costa",
            "opponent": "Matt Schnell",
            "american_moneyline": -750,
            "source": "DraftKings",
            "book": "DraftKings",
        },
    ]


# --- happy path ---------------------------------------------------------


def test_save_inserts_paste_rows_with_path_source_and_dk_book(conn):
    sid = _slate_with_fighters(conn, ["Matt Schnell", "Alessandro Costa"])
    result = save_draftkings_paste_rows(
        conn, slate_id=sid, rows=_rows(), captured_at=_CAPTURED_AT
    )
    assert isinstance(result, DraftKingsPasteSaveResult)
    assert result.saved_count == 2
    assert result.existing_count == 0
    assert result.failure_count == 0

    stored = {
        r.fighter_name_raw: r for r in OddsRowRepository(conn).list_for_slate(sid)
    }
    assert set(stored) == {"Matt Schnell", "Alessandro Costa"}
    # Source identifies the acquisition path; the book stays DraftKings.
    assert {r.source for r in stored.values()} == {SOURCE_DRAFTKINGS_PASTE}
    assert {r.bookmaker for r in stored.values()} == {"DraftKings"}
    # Opponent is preserved through the save.
    assert stored["Matt Schnell"].opponent_name_raw == "Alessandro Costa"
    assert stored["Alessandro Costa"].opponent_name_raw == "Matt Schnell"
    # implied_probability is repo-derived from the moneyline.
    assert stored["Alessandro Costa"].american_odds == -750


def test_save_chains_recompute_and_persists_match_results(conn):
    sid = _slate_with_fighters(conn, ["Matt Schnell", "Alessandro Costa"])
    assert OddsMatchResultRepository(conn).list_for_slate(sid) == []
    result = save_draftkings_paste_rows(
        conn, slate_id=sid, rows=_rows(), captured_at=_CAPTURED_AT
    )
    assert result.recompute is not None
    assert result.recompute_error is None
    assert result.recompute.total == 2
    # The recompute persisted one match result per saved odds row.
    assert len(OddsMatchResultRepository(conn).list_for_slate(sid)) == 2


def test_save_uses_stable_row_key(conn):
    sid = _slate_with_fighters(conn, ["Matt Schnell", "Alessandro Costa"])
    save_draftkings_paste_rows(
        conn, slate_id=sid, rows=_rows(), captured_at=_CAPTURED_AT
    )
    rec = next(
        r
        for r in OddsRowRepository(conn).list_for_slate(sid)
        if r.fighter_name_raw == "Matt Schnell"
    )
    assert rec.odds_row_key == compute_odds_row_key(
        fighter_name="Matt Schnell",
        bookmaker="DraftKings",
        source=SOURCE_DRAFTKINGS_PASTE,
        captured_at=_CAPTURED_AT,
    )


def test_batch_id_is_deterministic_for_same_capture(conn):
    sid = _slate_with_fighters(conn, ["Matt Schnell", "Alessandro Costa"])
    first = save_draftkings_paste_rows(
        conn, slate_id=sid, rows=_rows(), captured_at=_CAPTURED_AT
    )
    second = save_draftkings_paste_rows(
        conn, slate_id=sid, rows=_rows(), captured_at=_CAPTURED_AT
    )
    assert first.import_batch_id == second.import_batch_id
    assert first.import_batch_id.startswith("dkpaste-")
    stored = OddsRowRepository(conn).list_for_slate(sid)
    assert {r.import_batch_id for r in stored} == {first.import_batch_id}


# --- source_url echo (never persisted: no odds_rows column) -------------


def test_source_url_is_echoed_but_not_persisted(conn):
    sid = _slate_with_fighters(conn, ["Matt Schnell", "Alessandro Costa"])
    url = "https://sportsbook.draftkings.com/leagues/mma/ufc"
    result = save_draftkings_paste_rows(
        conn, slate_id=sid, rows=_rows(), captured_at=_CAPTURED_AT, source_url=url
    )
    assert result.source_url == url
    # odds_rows carries no URL column — nothing to assert beyond the echo.
    assert "source_url" not in [
        d[1] for d in conn.execute("PRAGMA table_info(odds_rows)").fetchall()
    ]


# --- idempotency --------------------------------------------------------


def test_save_is_idempotent_for_same_captured_at(conn):
    sid = _slate_with_fighters(conn, ["Matt Schnell", "Alessandro Costa"])
    first = save_draftkings_paste_rows(
        conn, slate_id=sid, rows=_rows(), captured_at=_CAPTURED_AT
    )
    second = save_draftkings_paste_rows(
        conn, slate_id=sid, rows=_rows(), captured_at=_CAPTURED_AT
    )
    assert first.saved_count == 2 and first.existing_count == 0
    assert second.saved_count == 0 and second.existing_count == 2
    assert len(OddsRowRepository(conn).list_for_slate(sid)) == 2


# --- empty roster -> recompute refuses but rows still saved -------------


def test_empty_roster_sets_recompute_error_but_saves_rows(conn):
    sid = SlateRepository(conn).create(event_name="UFC NoFighters").id
    result = save_draftkings_paste_rows(
        conn, slate_id=sid, rows=_rows(), captured_at=_CAPTURED_AT
    )
    assert result.saved_count == 2  # rows persisted regardless
    assert result.recompute is None
    assert result.recompute_error  # EmptyDkRosterError surfaced, not raised
    assert len(OddsRowRepository(conn).list_for_slate(sid)) == 2


# --- per-row failure isolation ------------------------------------------


def test_invalid_moneyline_row_does_not_abort_batch(conn):
    sid = _slate_with_fighters(conn, ["Matt Schnell", "Alessandro Costa"])
    rows = _rows()
    rows[0]["american_moneyline"] = 0  # rejected by the repo CHECK / validation
    result = save_draftkings_paste_rows(
        conn, slate_id=sid, rows=rows, captured_at=_CAPTURED_AT
    )
    assert result.saved_count == 1
    assert result.failure_count == 1
    bad_label, _msg = result.failures[0]
    assert bad_label == "Matt Schnell"
    stored = OddsRowRepository(conn).list_for_slate(sid)
    assert {r.fighter_name_raw for r in stored} == {"Alessandro Costa"}


# --- slate scoping ------------------------------------------------------


def test_rows_are_scoped_by_slate(conn):
    sid = _slate_with_fighters(conn, ["Matt Schnell", "Alessandro Costa"])
    other = _slate_with_fighters(
        conn, ["Other A", "Other B"], event="UFC Other"
    )
    save_draftkings_paste_rows(
        conn, slate_id=sid, rows=_rows(), captured_at=_CAPTURED_AT
    )
    assert len(OddsRowRepository(conn).list_for_slate(sid)) == 2
    assert OddsRowRepository(conn).list_for_slate(other) == []
    assert OddsMatchResultRepository(conn).list_for_slate(other) == []
