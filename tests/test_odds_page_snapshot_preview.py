"""AppTest coverage for the Odds page Zone 1e odds/news snapshot preview.

Loads ``app/pages/03_odds.py`` via ``streamlit.testing.v1.AppTest`` against an
isolated temp SQLite DB and exercises the **read-only** snapshot preview added
in S4 (``docs/ODDS_NEWS_SNAPSHOT_DESIGN.md`` §5). The preview reuses the pure
``validate_snapshot_text`` parser/validator (S3); this slice only renders it.

Hard contracts pinned here (``docs/DEVELOPMENT_NOTES.md`` §11):
  - Page load + the section render with no write to any table.
  - A valid snapshot surfaces the summary metrics, the app-derived implied
    probability column, news-flag text, and the "nothing is saved" copy.
  - An invalid snapshot (entry-level error) and a malformed document both
    surface errors WITHOUT raising to the page.
  - Optional slate matching is preview-only: it reports matched / unmatched /
    duplicate counts and persists nothing.

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
SLATE_SELECT_KEY = "odds_news_snapshot_slate_id"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Redirect the page's ``get_connection()`` at a per-test SQLite file."""
    db_path = tmp_path / "snapshot_preview.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _now_z() -> str:
    """Current UTC time as the snapshot's second-precision ``Z`` timestamp.

    Built at call time so the fixture is always fresh relative to the
    validator's wall-clock ``now`` — keeps staleness warnings out of the
    'valid snapshot' assertions regardless of when the suite runs.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_snapshot_bytes() -> bytes:
    """A two-sided, fresh, synthetic snapshot with zero hard errors."""
    doc = {
        "schema_version": 1,
        "snapshot_kind": "odds_news",
        "event": {"name": "UFC 999: Alpha vs Beta", "date": date.today().isoformat()},
        "collected_at": _now_z(),
        "collected_by": {"method": "manual", "agent": "test"},
        "sources_checked": [{"name": "Example Book", "category": "Betting"}],
        "entries": [
            {
                "fighter_name": "Alpha Fighter",
                "opponent_name": "Beta Fighter",
                "moneyline": -180,
                "book": "Example Book",
                "line_open": -150,
                "line_current": -180,
                "news_flags": ["short_notice"],
                "news_note": "Stepped in on 10 days notice.",
                "status": "ok",
                "confidence": 0.9,
            },
            {
                "fighter_name": "Beta Fighter",
                "opponent_name": "Alpha Fighter",
                "moneyline": 160,
                "book": "Example Book",
                "status": "needs_review",
            },
        ],
    }
    return json.dumps(doc).encode("utf-8")


def _entry_error_snapshot_bytes() -> bytes:
    """Valid envelope, one entry with a hard error (moneyline == 0)."""
    doc = {
        "schema_version": 1,
        "snapshot_kind": "odds_news",
        "event": {"name": "UFC 999", "date": date.today().isoformat()},
        "collected_at": _now_z(),
        "collected_by": {"method": "manual"},
        "sources_checked": [{"name": "Example Book"}],
        "entries": [
            {
                "fighter_name": "Bad Line",
                "opponent_name": "Someone",
                "moneyline": 0,
            }
        ],
    }
    return json.dumps(doc).encode("utf-8")


def _open_page() -> AppTest:
    at = AppTest.from_file(str(ODDS_PAGE), default_timeout=30)
    at.run()
    return at


def _uploader(at: AppTest):
    matched = [u for u in at.file_uploader if u.key == UPLOADER_KEY]
    assert len(matched) == 1, (
        f"Expected exactly one snapshot uploader with key {UPLOADER_KEY!r}; "
        f"saw uploader keys: {[u.key for u in at.file_uploader]}"
    )
    return matched[0]


def _upload(at: AppTest, content: bytes) -> AppTest:
    _uploader(at).upload("snapshot.json", content, "application/json")
    return at.run()


def _join(elements) -> str:
    return " ".join(e.value for e in elements)


def _seed_slate_with_fighters(names: list[str], *, event: str = "UFC 999") -> int:
    """Seed one slate with the given active DK fighters; return slate id."""
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


def _count_odds_rows(slate_id: int) -> int:
    conn = get_connection()
    try:
        return len(OddsRowRepository(conn).list_for_slate(slate_id))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Page load + no-write guarantees
# ---------------------------------------------------------------------------


def test_section_renders_readonly_copy_on_load(isolated_db):
    """The section renders on an empty DB with no upload, no exception, and
    shows the read-only / future-S5 framing copy."""
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert [e.value for e in at.error] == []

    blob = _join(at.subheader) + " " + _join(at.caption)
    assert "Odds/news snapshot preview — read-only" in blob, blob
    assert "Preview only — nothing is saved." in blob, blob
    # S5a replaced the "future save" framing with the present-tense save action.
    assert "Save snapshot odds to slate" in blob, blob

    # Exactly two uploaders now: the Zone 1a odds CSV + the Zone 1e snapshot.
    assert len(at.file_uploader) == 2, [u.key for u in at.file_uploader]


def test_upload_does_not_write_any_odds_rows(isolated_db):
    """Uploading + previewing a valid snapshot persists nothing to odds_rows."""
    slate_id = _seed_slate_with_fighters(["Alpha Fighter", "Beta Fighter"])
    at = _open_page()
    _upload(at, _valid_snapshot_bytes())
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _count_odds_rows(slate_id) == 0


# ---------------------------------------------------------------------------
# Valid snapshot preview
# ---------------------------------------------------------------------------


def test_valid_snapshot_renders_summary_and_entries(isolated_db):
    """A valid snapshot shows the success banner, summary metrics, and the
    app-derived implied probability + news flags in the entries table."""
    at = _open_page()
    _upload(at, _valid_snapshot_bytes())
    assert not at.exception, [str(e.value) for e in at.exception]

    successes = _join(at.success)
    assert "Snapshot is valid" in successes, successes
    assert "nothing is saved" in successes, successes

    metric_labels = [m.label for m in at.metric]
    for label in ("Entries", "Valid entries", "Errors", "Warnings", "News flags"):
        assert label in metric_labels, metric_labels
    metric_by_label = {m.label: m.value for m in at.metric}
    assert metric_by_label["Valid entries"] == "2", metric_by_label
    assert metric_by_label["Errors"] == "0", metric_by_label
    assert metric_by_label["News flags"] == "1", metric_by_label

    # Entries table carries the app-derived implied column + news flag text.
    frames = [d.value for d in at.dataframe]
    entry_frames = [
        f for f in frames if "implied (app-derived)" in getattr(f, "columns", [])
    ]
    assert len(entry_frames) == 1, [list(f.columns) for f in frames]
    df = entry_frames[0]
    # -180 → 64.3% app-derived implied probability.
    assert "64.3%" in df["implied (app-derived)"].tolist(), df.to_dict()
    assert "short_notice" in df["news_flags"].tolist(), df.to_dict()


# ---------------------------------------------------------------------------
# Error handling (no crash)
# ---------------------------------------------------------------------------


def test_entry_error_snapshot_shows_errors_without_crashing(isolated_db):
    """An entry-level hard error (moneyline 0) surfaces an error banner and
    the specific error line, with no page exception and no success banner."""
    at = _open_page()
    _upload(at, _entry_error_snapshot_bytes())
    assert not at.exception, [str(e.value) for e in at.exception]

    errors = _join(at.error)
    assert "error(s)" in errors, errors
    assert "moneyline" in errors, errors
    assert "Snapshot is valid" not in _join(at.success)


def test_malformed_document_shows_parse_error_without_crashing(isolated_db):
    """A non-JSON / format-gate failure surfaces a parse error, not an
    exception."""
    at = _open_page()
    _upload(at, b"this is not json {{{")
    assert not at.exception, [str(e.value) for e in at.exception]

    errors = _join(at.error)
    assert "could not be parsed" in errors, errors


# ---------------------------------------------------------------------------
# Optional preview-only slate matching
# ---------------------------------------------------------------------------


def test_slate_matching_preview_matches_active_fighters(isolated_db):
    """Selecting a slate name-matches the snapshot's fighters against the
    slate's active fighters and reports matched / unmatched counts —
    persisting nothing."""
    slate_id = _seed_slate_with_fighters(["Alpha Fighter", "Beta Fighter"])
    at = _open_page()
    _upload(at, _valid_snapshot_bytes())
    assert not at.exception, [str(e.value) for e in at.exception]

    select = at.selectbox(key=SLATE_SELECT_KEY)
    select.set_value(slate_id)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    captions = _join(at.caption)
    assert f"Matched 2 / 2 snapshot entries to active fighters on slate #{slate_id}" in captions, captions
    assert "0 unmatched" in captions, captions

    # Still a pure preview — no odds rows were written.
    assert _count_odds_rows(slate_id) == 0
