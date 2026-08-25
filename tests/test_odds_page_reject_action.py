"""AppTest coverage for the Odds page Reject Match UI action.

Promotes the inline Streamlit AppTest smoke into a committed pytest test.
Loads ``app/pages/03_odds.py`` via ``streamlit.testing.v1.AppTest`` against
an isolated temp SQLite DB, seeds one ``review_required`` match result,
clicks the Reject button, and asserts the resulting write to
``manual_match_overrides`` plus the (intentional) lack of change to
``odds_match_results.match_status`` / ``effective_status``.

The Odds page is the first UI surface that writes via
``ManualMatchOverrideRepository.add_override`` — this test protects that
wiring against silent regressions. App behavior is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import get_connection
from src.db.repositories import (
    ManualMatchOverrideRepository,
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
    """Point the app's ``get_connection()`` at a per-test SQLite file.

    The page calls ``get_connection()`` with no arguments, so it resolves
    ``DB_PATH`` from ``src.db.connection``'s module namespace. Patching
    that attribute redirects every connection opened during the AppTest
    run to ``tmp_path`` — no risk of touching the dev DB. The env var is
    set as belt-and-braces in case any path re-reads settings.
    """
    db_path = tmp_path / "reject_action.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


@pytest.fixture
def seeded(isolated_db):
    """Seed one slate, one active fighter, one odds row, one
    ``review_required`` persisted match result. Returns the identifiers
    the tests assert against.
    """
    conn = get_connection()
    try:
        apply_schema(conn)

        slate = SlateRepository(conn).create(event_name="UFC AppTest Event")

        cur = conn.execute(
            "INSERT INTO fighters (slate_id, name, salary, status) "
            "VALUES (?, ?, ?, ?)",
            (slate.id, "Jose Aldo", 8000, "active"),
        )
        conn.commit()
        fighter_id = int(cur.lastrowid)

        row = OddsRowRepository(conn).create(
            slate_id=slate.id,
            fighter_name_raw="Jose Aldo",
            american_odds=-150,
            source="manual",
            captured_at="2026-05-20T00:00:00Z",
        )

        OddsMatchResultRepository(conn).replace_for_slate(
            slate.id,
            [
                OddsMatchResultRecord(
                    slate_id=slate.id,
                    odds_row_id=row.id,
                    odds_row_key=row.odds_row_key,
                    fighter_id=fighter_id,
                    match_status="review_required",
                    effective_status="review_required",
                    match_stage="fuzzy",
                    match_score=92,
                    preferred_candidate="Jose Aldo",
                    opponent_check="not_applicable",
                    candidates=("Jose Aldo",),
                    notes=("seeded for AppTest",),
                )
            ],
        )
    finally:
        conn.close()

    return {
        "slate_id": slate.id,
        "fighter_id": fighter_id,
        "odds_row_id": row.id,
        "odds_row_key": row.odds_row_key,
    }


def _open_odds_page() -> AppTest:
    """Open the Odds page and run the first render."""
    at = AppTest.from_file(str(ODDS_PAGE), default_timeout=30)
    at.run()
    return at


def _reject_button_key(seeded_state) -> str:
    return (
        "reject_match_btn_"
        f"{seeded_state['slate_id']}_{seeded_state['odds_row_id']}"
    )


def _find_buttons_by_key(at: AppTest, key: str) -> list:
    return [b for b in at.button if b.key == key]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_reject_button_renders_for_review_required_match(seeded):
    """First render with one seeded ``review_required`` row should expose
    a Reject button keyed off ``(slate_id, odds_row_id)``."""
    at = _open_odds_page()

    assert not at.exception, (
        "Page raised on first render: "
        f"{[str(e.value) for e in at.exception]}"
    )

    matched = _find_buttons_by_key(at, _reject_button_key(seeded))
    assert len(matched) == 1, (
        f"Expected Reject button {_reject_button_key(seeded)!r}; "
        f"saw button keys: {[b.key for b in at.button]}"
    )


def test_reject_click_writes_active_override_and_leaves_match_result_unchanged(
    seeded,
):
    """End-to-end: open the page, click Reject, then assert
        (a) no exception / no st.error,
        (b) success banner rendered,
        (c) exactly one active ``reject_match`` override exists, with the
            seeded ``odds_row_key`` and ``fighter_id``,
        (d) the persisted ``odds_match_results`` row still reads
            ``review_required`` on ``match_status``, and
            ``effective_status`` is now ``review_rejected`` — D.4.3.c
            wires the apply pass into the Reject action.
    """
    at = _open_odds_page()
    [button] = _find_buttons_by_key(at, _reject_button_key(seeded))
    at = button.click().run()

    assert not at.exception, (
        "Page raised after Reject click: "
        f"{[str(e.value) for e in at.exception]}"
    )
    error_msgs = [e.value for e in at.error]
    assert error_msgs == [], (
        f"st.error called after Reject click: {error_msgs}"
    )

    success_msgs = [s.value for s in at.success]
    assert any("Active reject override" in m for m in success_msgs), (
        "Expected the reject success banner; "
        f"saw success messages: {success_msgs}"
    )

    conn = get_connection()
    try:
        active = ManualMatchOverrideRepository(conn).list_active_for_slate(
            seeded["slate_id"]
        )
        assert len(active) == 1, (
            f"Expected exactly one active override; got: {active}"
        )
        rec = active[0]
        assert rec.override_type == "reject_match"
        assert rec.odds_row_key == seeded["odds_row_key"]
        assert rec.fighter_id == seeded["fighter_id"]
        assert rec.superseded_at is None

        [match] = OddsMatchResultRepository(conn).list_for_slate(
            seeded["slate_id"]
        )
        assert match.match_status == "review_required"
        assert match.effective_status == "review_rejected"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Negative path — no review_required rows means no Reject button to click
# ---------------------------------------------------------------------------


def test_no_reject_button_when_no_review_required_rows(isolated_db):
    """With a saved slate but no ``review_required`` match results,
    the page must not render a Reject button — there is nothing
    rejectable, so the write path is unreachable from the UI."""
    conn = get_connection()
    try:
        apply_schema(conn)
        SlateRepository(conn).create(event_name="UFC Empty Slate")
    finally:
        conn.close()

    at = _open_odds_page()
    assert not at.exception, (
        "Page raised on render with no review_required rows: "
        f"{[str(e.value) for e in at.exception]}"
    )

    reject_buttons = [
        b for b in at.button if b.key and b.key.startswith("reject_match_btn_")
    ]
    assert reject_buttons == [], (
        "Reject button rendered with no rejectable rows; "
        f"saw button keys: {[b.key for b in reject_buttons]}"
    )

    # And no override should have been written to the DB by mere page load.
    conn = get_connection()
    try:
        [slate] = SlateRepository(conn).list_all()
        assert (
            ManualMatchOverrideRepository(conn).list_active_for_slate(slate.id)
            == []
        )
    finally:
        conn.close()
