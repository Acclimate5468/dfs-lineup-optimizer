"""AppTest coverage for the Odds page Zone 3 review-by-exception banner.

Loads ``app/pages/03_odds.py`` via ``streamlit.testing.v1.AppTest`` against
an isolated temp SQLite DB, seeds persisted ``odds_match_results`` in a few
shapes, and pins the rendered Zone 3 summary banner + the split needs-review /
clean tables (``docs/DEVELOPMENT_NOTES.md`` §11 — derived state must have a backing test).

The banner is display-only: it reads persisted match results and renders a
"complete vs needs review" summary. No write actions are added here, so the
reject / recompute / save behaviour pinned by ``test_odds_page_reject_action``
is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import get_connection
from src.db.repositories import (
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching_service import OddsMatchResultRecord

REPO_ROOT = Path(__file__).resolve().parent.parent
ODDS_PAGE = REPO_ROOT / "app" / "pages" / "03_odds.py"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Redirect the page's ``get_connection()`` at a per-test SQLite file."""
    db_path = tmp_path / "review_banner.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


# Match-result specs keyed by intent. ``effective_status == "unmatched"``
# rows carry no fighter binding (fighter_id is None).
_AUTO = {
    "match_status": "auto_match",
    "effective_status": "auto_match",
    "match_stage": "exact_conservative",
    "match_score": 100,
}
_REVIEW = {
    "match_status": "review_required",
    "effective_status": "review_required",
    "match_stage": "fuzzy",
    "match_score": 92,
}
_UNMATCHED = {
    "match_status": "unmatched",
    "effective_status": "unmatched",
    "match_stage": "none",
    "match_score": 0,
}


def _seed_slate(specs, *, odds_rows_without_results: int = 0) -> int:
    """Seed one slate with one odds row + match result per ``specs`` entry,
    plus ``odds_rows_without_results`` bare odds rows with no match result.
    Returns the slate id."""
    conn = get_connection()
    try:
        apply_schema(conn)
        slate = SlateRepository(conn).create(event_name="UFC Banner Event")

        results = []
        for i, spec in enumerate(specs):
            fighter_id = None
            if spec["effective_status"] != "unmatched":
                cur = conn.execute(
                    "INSERT INTO fighters (slate_id, name, salary, status) "
                    "VALUES (?, ?, ?, 'active')",
                    (slate.id, f"Fighter {i}", 8000),
                )
                conn.commit()
                fighter_id = int(cur.lastrowid)
            row = OddsRowRepository(conn).create(
                slate_id=slate.id,
                fighter_name_raw=f"Fighter {i}",
                american_odds=-150,
                source="manual",
                captured_at=f"2026-05-20T00:0{i}:00Z",
            )
            results.append(
                OddsMatchResultRecord(
                    slate_id=slate.id,
                    odds_row_id=row.id,
                    odds_row_key=row.odds_row_key,
                    fighter_id=fighter_id,
                    match_status=spec["match_status"],
                    effective_status=spec["effective_status"],
                    match_stage=spec["match_stage"],
                    match_score=spec["match_score"],
                    preferred_candidate=(
                        None if fighter_id is None else f"Fighter {i}"
                    ),
                    opponent_check="not_applicable",
                    candidates=(
                        () if fighter_id is None else (f"Fighter {i}",)
                    ),
                    notes=("seeded for banner AppTest",),
                )
            )

        for j in range(odds_rows_without_results):
            OddsRowRepository(conn).create(
                slate_id=slate.id,
                fighter_name_raw=f"Extra {j}",
                american_odds=120,
                source="manual",
                captured_at=f"2026-05-21T00:0{j}:00Z",
            )

        if results:
            OddsMatchResultRepository(conn).replace_for_slate(
                slate.id, results
            )
        return slate.id
    finally:
        conn.close()


def _open_odds_page() -> AppTest:
    at = AppTest.from_file(str(ODDS_PAGE), default_timeout=30)
    at.run()
    return at


def _join(elements) -> str:
    return " ".join(e.value for e in elements)


def test_banner_warns_and_splits_when_rows_need_review(isolated_db):
    """A mix of auto / review_required / unmatched rows surfaces a warning
    banner with the breakdown, a counts caption, and a 'Needs review' table
    header."""
    _seed_slate([_AUTO, _REVIEW, _UNMATCHED])
    at = _open_odds_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    captions = _join(at.caption)
    assert "Match results: 3" in captions, captions
    assert "Matched: 1" in captions, captions
    assert "Needs review: 1" in captions, captions
    assert "Unmatched: 1" in captions, captions

    warnings = _join(at.warning)
    assert "need review" in warnings, warnings
    assert "review_required: 1" in warnings, warnings
    assert "unmatched: 1" in warnings, warnings

    markdown = _join(at.markdown)
    assert "Needs review — 2 of 3 match result(s)" in markdown, markdown


def test_banner_reports_complete_when_all_auto_matched(isolated_db):
    """All-clean slate shows the success banner and the clean-rows success
    note, and counts zero needing review."""
    _seed_slate([_AUTO, _AUTO])
    at = _open_odds_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    successes = _join(at.success)
    assert "Odds review complete" in successes, successes
    assert "cleanly auto-matched" in successes, successes

    captions = _join(at.caption)
    assert "Matched: 2" in captions, captions
    assert "Needs review: 0" in captions, captions


def test_banner_prompts_recompute_when_odds_saved_but_no_results(isolated_db):
    """Odds rows present but no match results yet → an info nudge to
    Recompute, and a zero match-result count."""
    _seed_slate([], odds_rows_without_results=2)
    at = _open_odds_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    infos = _join(at.info)
    assert "no match results yet" in infos, infos

    captions = _join(at.caption)
    assert "Match results: 0" in captions, captions
