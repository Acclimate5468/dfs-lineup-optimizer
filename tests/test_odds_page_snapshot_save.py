"""AppTest coverage for the Odds page snapshot Save action (slice S5a).

Loads ``app/pages/03_odds.py`` via ``streamlit.testing.v1.AppTest`` against an
isolated temp SQLite DB and exercises the explicit **Save snapshot odds to
selected slate** action added in S5a
(``docs/ODDS_NEWS_SNAPSHOT_PERSISTENCE_DESIGN.md`` §7, §14). The pure save
service is unit-tested in ``tests/test_snapshot_odds_save.py``; this file pins
the UI write-action contracts (``docs/DEVELOPMENT_NOTES.md`` §11):

  - No write on page load / on upload — only on the explicit button click.
  - The Save button is unavailable until a slate exists (and a valid snapshot
    with at least one moneyline is uploaded).
  - Clicking Save persists ``source="snapshot:…"`` rows and reports counts +
    recompute status.
  - Re-clicking Save is idempotent (no duplicate rows).

All fixtures are synthetic — no real feed file is read or committed
(``docs/DEVELOPMENT_NOTES.md`` §7 / §8).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import get_connection
from src.db.repositories import OddsRowRepository, SlateRepository
from src.db.schema import apply_schema

REPO_ROOT = Path(__file__).resolve().parent.parent
ODDS_PAGE = REPO_ROOT / "app" / "pages" / "03_odds.py"

UPLOADER_KEY = "odds_news_snapshot_uploader"
SAVE_SLATE_KEY = "odds_news_snapshot_save_slate_id"
SAVE_BUTTON_KEY = "odds_news_snapshot_save_btn"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "snapshot_save.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_snapshot_bytes() -> bytes:
    """Two-sided, fresh, synthetic snapshot, event 'UFC 999: Alpha vs Beta'."""
    doc = {
        "schema_version": 1,
        "snapshot_kind": "odds_news",
        "event": {"name": "UFC 999: Alpha vs Beta", "date": date.today().isoformat()},
        "collected_at": _now_z(),
        "collected_by": {"method": "manual"},
        "sources_checked": [{"name": "Example Book"}],
        "entries": [
            {
                "fighter_name": "Alpha Fighter",
                "opponent_name": "Beta Fighter",
                "moneyline": -180,
                "book": "Example Book",
            },
            {
                "fighter_name": "Beta Fighter",
                "opponent_name": "Alpha Fighter",
                "moneyline": 160,
                "book": "Example Book",
            },
        ],
    }
    return json.dumps(doc).encode("utf-8")


def _open_page() -> AppTest:
    at = AppTest.from_file(str(ODDS_PAGE), default_timeout=30)
    at.run()
    return at


def _uploader(at: AppTest):
    return next(u for u in at.file_uploader if u.key == UPLOADER_KEY)


def _upload(at: AppTest, content: bytes) -> AppTest:
    _uploader(at).upload("snapshot.json", content, "application/json")
    return at.run()


def _join(elements) -> str:
    return " ".join(e.value for e in elements)


def _buttons_by_key(at: AppTest, key: str) -> list:
    return [b for b in at.button if b.key == key]


def _seed_slate_with_fighters(names: list[str], *, event: str = "UFC 999") -> int:
    conn = get_connection()
    try:
        apply_schema(conn)
        slate = SlateRepository(conn).create(event_name=event)
        for i, name in enumerate(names):
            conn.execute(
                "INSERT INTO fighters (slate_id, name, salary, status) "
                "VALUES (?, ?, ?, 'active')",
                (slate.id, name, 8000 + i),
            )
        conn.commit()
        return slate.id
    finally:
        conn.close()


def _odds_rows(slate_id: int) -> list:
    conn = get_connection()
    try:
        return OddsRowRepository(conn).list_for_slate(slate_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# No write on load / upload
# ---------------------------------------------------------------------------


def test_upload_alone_writes_nothing(isolated_db):
    """Uploading + previewing must not write — only the explicit click does."""
    slate_id = _seed_slate_with_fighters(["Alpha Fighter", "Beta Fighter"])
    at = _open_page()
    _upload(at, _valid_snapshot_bytes())
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _odds_rows(slate_id) == []


def test_save_block_renders_required_copy(isolated_db):
    _seed_slate_with_fighters(["Alpha Fighter", "Beta Fighter"])
    at = _open_page()
    _upload(at, _valid_snapshot_bytes())
    captions = _join(at.caption) + " " + _join(at.markdown)
    assert "Save snapshot odds to selected slate" in captions, captions
    assert (
        "Only moneylines are saved. News, props, and line movement remain "
        "preview-only in S5a." in captions
    ), captions


# ---------------------------------------------------------------------------
# Button availability
# ---------------------------------------------------------------------------


def test_save_button_unavailable_without_a_slate(isolated_db):
    """No saved slate → no Save button, with an explanatory caption."""
    at = _open_page()
    _upload(at, _valid_snapshot_bytes())
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _buttons_by_key(at, SAVE_BUTTON_KEY) == []
    assert "No slates saved yet" in _join(at.caption)


def test_save_button_available_with_slate_and_valid_snapshot(isolated_db):
    _seed_slate_with_fighters(["Alpha Fighter", "Beta Fighter"])
    at = _open_page()
    _upload(at, _valid_snapshot_bytes())
    assert len(_buttons_by_key(at, SAVE_BUTTON_KEY)) == 1, [
        b.key for b in at.button
    ]


# ---------------------------------------------------------------------------
# Explicit save writes rows + recomputes
# ---------------------------------------------------------------------------


def test_save_click_persists_snapshot_rows_and_recomputes(isolated_db):
    slate_id = _seed_slate_with_fighters(["Alpha Fighter", "Beta Fighter"])
    at = _open_page()
    _upload(at, _valid_snapshot_bytes())
    assert _odds_rows(slate_id) == []  # nothing yet

    at.selectbox(key=SAVE_SLATE_KEY).set_value(slate_id).run()
    [button] = _buttons_by_key(at, SAVE_BUTTON_KEY)
    at = button.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = _odds_rows(slate_id)
    assert len(rows) == 2
    assert all(r.source.startswith("snapshot:") for r in rows)
    assert {r.fighter_name_raw for r in rows} == {"Alpha Fighter", "Beta Fighter"}

    successes = _join(at.success)
    assert "Saved 2 snapshot moneyline(s)" in successes, successes
    assert "Recomputed match results" in successes, successes


def test_resave_click_is_idempotent(isolated_db):
    slate_id = _seed_slate_with_fighters(["Alpha Fighter", "Beta Fighter"])
    at = _open_page()
    _upload(at, _valid_snapshot_bytes())
    at.selectbox(key=SAVE_SLATE_KEY).set_value(slate_id).run()

    at = _buttons_by_key(at, SAVE_BUTTON_KEY)[0].click().run()
    assert len(_odds_rows(slate_id)) == 2

    # Second explicit click: no new rows, idempotent info surfaced.
    at = _buttons_by_key(at, SAVE_BUTTON_KEY)[0].click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(_odds_rows(slate_id)) == 2
    assert "already existed" in _join(at.info), _join(at.info)
