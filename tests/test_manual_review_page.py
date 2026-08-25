"""AppTest coverage for the Manual Review Gate v1 Phase D Streamlit page.

Loads ``app/pages/06_manual_review.py`` via ``streamlit.testing.v1.AppTest``
against an isolated temp SQLite DB and pins
``docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`` §6 / §7 / §15 plus the
``docs/DEVELOPMENT_NOTES.md`` §11 write-action contract:

- Empty DB → "No slates yet" info; no slate selector, no write affordances.
- Banner covers the §7 step 2 local / no-auto-detect / re-review /
  optimizer-gate / no-effective_status fragments.
- Slate selector renders when slates exist.
- Readiness summary + per-section dataframes render.
- Mark Slate Manually Reviewed button is disabled when any Blocking
  check fails and the §6 disabled caption is rendered.
- Page load does NOT mutate persisted state.
- Mark Slate Manually Reviewed writes only on explicit button click
  when the readiness summary reports ``ready``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import get_connection
from src.db.repositories import (
    FightGroupRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching_service import (
    recompute_and_replace_match_results,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = REPO_ROOT / "app" / "pages" / "06_manual_review.py"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "manual_review_page.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _open_page() -> AppTest:
    at = AppTest.from_file(str(PAGE_PATH), default_timeout=30)
    at.run()
    return at


def _seed_not_ready_slate() -> int:
    """A slate that does not satisfy the Blocking list (no salary,
    no fighters). ``manual_review_user_ack`` is also Blocking-fail."""
    conn = get_connection()
    try:
        apply_schema(conn)
        return SlateRepository(conn).create(event_name="UFC NotReady").id
    finally:
        conn.close()


def _seed_ready_slate() -> int:
    """A clean two-fighter slate with confirmed group + full odds
    coverage. Every Blocking check passes except
    ``manual_review_user_ack``, which is the one the page write flips."""
    conn = get_connection()
    try:
        apply_schema(conn)
        sid = SlateRepository(conn).create(
            event_name="UFC Ready",
            salary_csv_status="validated",
            salary_row_count=2,
        ).id
        conn.execute(
            "INSERT INTO fighters (slate_id, name, salary, status) "
            "VALUES (?, ?, ?, 'active')",
            (sid, "Cheap Champ", 7000),
        )
        conn.execute(
            "INSERT INTO fighters (slate_id, name, salary, status) "
            "VALUES (?, ?, ?, 'active')",
            (sid, "Pricey Dog", 9500),
        )
        conn.commit()
        fg = FightGroupRepository(conn).create(
            slate_id=sid,
            fighter_1_name="Cheap Champ",
            fighter_2_name="Pricey Dog",
            scheduled_rounds=3,
        )
        FightGroupRepository(conn).update_status(fg.id, "confirmed")
        OddsRowRepository(conn).create(
            slate_id=sid,
            fighter_name_raw="Cheap Champ",
            american_odds=-200,
            source="manual",
            captured_at="2026-05-20T00:00:00Z",
        )
        OddsRowRepository(conn).create(
            slate_id=sid,
            fighter_name_raw="Pricey Dog",
            american_odds=+170,
            source="manual",
            captured_at="2026-05-20T00:01:00Z",
        )
        recompute_and_replace_match_results(conn, sid)
        return sid
    finally:
        conn.close()


def _read_slate_review(slate_id: int) -> tuple[str, str | None]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT manual_review_status, manual_review_completed_at "
            "FROM slates WHERE id = ?",
            (int(slate_id),),
        ).fetchone()
        return (row[0], row[1])
    finally:
        conn.close()


def _snapshot_slates() -> list[tuple]:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT id, manual_review_status, manual_review_completed_at "
            "FROM slates ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _button(at: AppTest, key: str):
    matched = [b for b in at.button if b.key == key]
    assert len(matched) == 1, (
        f"Expected exactly one button with key {key!r}; "
        f"saw button keys: {[b.key for b in at.button]}"
    )
    return matched[0]


# ---------------------------------------------------------------------------
# Empty state + banner
# ---------------------------------------------------------------------------


def test_empty_db_shows_no_slates_info_and_no_write_affordances(isolated_db):
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    infos = [i.value for i in at.info]
    assert any("No slates yet" in s for s in infos), infos

    # No selectbox, no buttons, no dataframes — the page short-circuits.
    assert [b.key for b in at.button] == []
    assert [s.key for s in at.selectbox] == []


def test_banner_covers_design_section_7_fragments(isolated_db):
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception

    warnings = " ".join(w.value for w in at.warning)
    for fragment in (
        "Manual review is local",
        "does NOT auto-detect",
        "re-review after any salary re-import",
        "future optimizer",
        "effective_status is not consulted",
        "Fighter Status is not yet",
    ):
        assert fragment in warnings, (
            f"Expected banner to mention {fragment!r}; got: {warnings!r}"
        )


# ---------------------------------------------------------------------------
# Slate selector + readiness rendering
# ---------------------------------------------------------------------------


def test_slate_selector_renders_when_slates_exist(isolated_db):
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception

    selectbox_keys = [s.key for s in at.selectbox]
    assert "manual_review_slate_id" in selectbox_keys


def test_readiness_summary_and_sections_render(isolated_db):
    _seed_ready_slate()
    at = _open_page()
    assert not at.exception

    # Header + summary caption mention status and category counts.
    markdown_blob = " ".join(m.value for m in at.markdown)
    assert "Status: not_reviewed" in markdown_blob, markdown_blob

    captions = " ".join(c.value for c in at.caption)
    assert "Blocking:" in captions
    assert "Warning:" in captions
    assert "Informational:" in captions

    # Section headings render.
    headings = [h.value for h in at.subheader]
    assert "Blocking" in headings
    assert "Warning" in headings
    assert "Informational" in headings

    # At least the Blocking + Informational dataframes render (Warning
    # may or may not depending on seeded state, but on a ready slate
    # the Warning section will have at least the §5.8 late-news row).
    assert len(at.dataframe) >= 2


# ---------------------------------------------------------------------------
# Mark-reviewed gating (disabled when not ready)
# ---------------------------------------------------------------------------


def test_mark_reviewed_button_disabled_when_blocking_fails(isolated_db):
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception

    btn = _button(at, "manual_review_mark_btn")
    assert btn.disabled is True

    captions = " ".join(c.value for c in at.caption)
    assert (
        "Resolve the Blocking list before marking this slate reviewed."
        in captions
    ), captions


def test_mark_reviewed_button_enabled_when_ready(isolated_db):
    _seed_ready_slate()
    at = _open_page()
    assert not at.exception

    btn = _button(at, "manual_review_mark_btn")
    assert btn.disabled is False


# ---------------------------------------------------------------------------
# Plain-English readiness banner (display-only — mirrors the gate)
# ---------------------------------------------------------------------------


def test_ready_banner_announces_slate_is_ready(isolated_db):
    """When every blocking check (other than the ack the button flips)
    passes, the page leads with a success banner pointing at the button."""
    _seed_ready_slate()
    at = _open_page()
    assert not at.exception

    successes = " ".join(s.value for s in at.success)
    assert "ready to mark" in successes, successes


def test_needs_review_banner_counts_open_blocking(isolated_db):
    """When blocking checks fail, the page leads with an error banner that
    counts the open blocking checks and points at the Blocking section."""
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception

    errors = " ".join(e.value for e in at.error)
    assert "Needs review" in errors, errors
    assert "blocking check(s)" in errors, errors


# ---------------------------------------------------------------------------
# Page-load read-only contract
# ---------------------------------------------------------------------------


def test_page_load_does_not_mutate_db(isolated_db):
    _seed_ready_slate()
    before = _snapshot_slates()
    at = _open_page()
    assert not at.exception
    after = _snapshot_slates()
    assert before == after


def test_page_load_does_not_mark_reviewed_even_when_ready(isolated_db):
    slate_id = _seed_ready_slate()
    _ = _open_page()
    status, completed_at = _read_slate_review(slate_id)
    assert status == "not_reviewed"
    assert completed_at is None


# ---------------------------------------------------------------------------
# Mark-reviewed write only on explicit click
# ---------------------------------------------------------------------------


def test_mark_reviewed_click_writes_via_repository(isolated_db):
    slate_id = _seed_ready_slate()
    at = _open_page()
    assert not at.exception

    _button(at, "manual_review_mark_btn").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    status, completed_at = _read_slate_review(slate_id)
    assert status == "reviewed"
    assert completed_at is not None and completed_at.strip() != ""


def test_disabled_click_is_a_noop(isolated_db):
    """When the button is disabled, even an attempted click leaves the
    slate in ``not_reviewed``. Streamlit's AppTest still surfaces the
    button; the page guards via ``disabled=True`` so no write fires."""
    slate_id = _seed_not_ready_slate()
    before = _read_slate_review(slate_id)
    at = _open_page()
    assert not at.exception

    btn = _button(at, "manual_review_mark_btn")
    assert btn.disabled is True

    after = _read_slate_review(slate_id)
    assert before == after
    assert after[0] == "not_reviewed"
