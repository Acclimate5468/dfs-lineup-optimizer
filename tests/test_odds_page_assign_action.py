"""AppTest coverage for the Odds page 3f Assign / Accept UI action (D.5.3).

Loads ``app/pages/03_odds.py`` via ``streamlit.testing.v1.AppTest`` against an
isolated temp SQLite DB, seeds persisted match results, and exercises the new
bordered "Assign / Accept a Match" container that wires the D.5.1
``record_assign_match_override`` service (``docs/ODDS_PERSISTENCE_DESIGN.md``
§16.10 / §16.15).

Covered:
    - the assign UI appears only when assignable (review_required / unmatched)
      rows exist; an auto_match-only slate shows the info message instead;
    - assigning an ``unmatched`` row to an active fighter writes a
      ``force_pair`` override and flips ``effective_status`` + ``fighter_id``;
    - assigning a ``review_required`` row to the matcher's proposed fighter
      writes ``accept_match`` → ``review_accepted``;
    - no write happens on page load;
    - an already-bound fighter surfaces the service error with no DB write;
    - the 3c Reject UI still renders alongside the new container.
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
    """Point the app's ``get_connection()`` at a per-test SQLite file."""
    db_path = tmp_path / "assign_action.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _create_fighter(conn, slate_id: int, name: str, salary: int = 8000) -> int:
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, 'active')",
        (slate_id, name, salary),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_match_result(conn, slate_id, odds_row, **kwargs):
    """Persist one ``odds_match_results`` row for ``odds_row``.

    Defaults describe an ``unmatched`` row (no matcher binding); callers
    override ``match_status`` / ``effective_status`` / ``fighter_id`` /
    ``preferred_candidate`` for the review_required and auto_match cases.
    """
    defaults = dict(
        fighter_id=None,
        match_status="unmatched",
        effective_status="unmatched",
        match_stage="none",
        match_score=0,
        preferred_candidate=None,
        opponent_check="not_applicable",
        candidates=(),
        notes=("seeded for AppTest",),
    )
    defaults.update(kwargs)
    OddsMatchResultRepository(conn).replace_for_slate(
        slate_id,
        [
            OddsMatchResultRecord(
                slate_id=slate_id,
                odds_row_id=odds_row.id,
                odds_row_key=odds_row.odds_row_key,
                **defaults,
            )
        ],
    )


@pytest.fixture
def unmatched_slate(isolated_db):
    """Slate with one active fighter and one ``unmatched`` odds row.

    Models the real smoke case: odds say ``Bruno Gustavo da Silva`` but the DK
    salary lists ``Bruno Silva`` — the matcher leaves the odds row unmatched.
    """
    conn = get_connection()
    try:
        apply_schema(conn)
        slate = SlateRepository(conn).create(event_name="UFC Assign Event")
        fighter_id = _create_fighter(conn, slate.id, "Bruno Silva", salary=7600)
        row = OddsRowRepository(conn).create(
            slate_id=slate.id,
            fighter_name_raw="Bruno Gustavo da Silva",
            opponent_name_raw="Joe Pyfer",
            american_odds=-150,
            source="manual",
            captured_at="2026-05-20T00:00:00Z",
        )
        _seed_match_result(conn, slate.id, row)
    finally:
        conn.close()
    return {
        "slate_id": slate.id,
        "fighter_id": fighter_id,
        "odds_row_id": row.id,
        "odds_row_key": row.odds_row_key,
    }


def _open_odds_page() -> AppTest:
    at = AppTest.from_file(str(ODDS_PAGE), default_timeout=30)
    at.run()
    return at


def _assign_button_key(state) -> str:
    return f"assign_match_btn_{state['slate_id']}_{state['odds_row_id']}"


def _find_buttons_by_key(at: AppTest, key: str) -> list:
    return [b for b in at.button if b.key == key]


# ---------------------------------------------------------------------------
# UI presence / absence
# ---------------------------------------------------------------------------


def test_assign_ui_appears_when_assignable_rows_exist(unmatched_slate):
    at = _open_odds_page()
    assert not at.exception, (
        "Page raised on first render: "
        f"{[str(e.value) for e in at.exception]}"
    )
    matched = _find_buttons_by_key(at, _assign_button_key(unmatched_slate))
    assert len(matched) == 1, (
        f"Expected Assign button {_assign_button_key(unmatched_slate)!r}; "
        f"saw button keys: {[b.key for b in at.button]}"
    )


def test_auto_match_only_slate_shows_no_assignable_rows(isolated_db):
    """An auto_match-only slate exposes no Assign button and shows the
    concise info message instead."""
    conn = get_connection()
    try:
        apply_schema(conn)
        slate = SlateRepository(conn).create(event_name="UFC Clean Slate")
        fighter_id = _create_fighter(conn, slate.id, "Jon Jones")
        row = OddsRowRepository(conn).create(
            slate_id=slate.id,
            fighter_name_raw="Jon Jones",
            american_odds=-300,
            source="manual",
            captured_at="2026-05-20T00:00:00Z",
        )
        _seed_match_result(
            conn,
            slate.id,
            row,
            fighter_id=fighter_id,
            match_status="auto_match",
            effective_status="auto_match",
            match_stage="exact_conservative",
            match_score=100,
        )
    finally:
        conn.close()

    at = _open_odds_page()
    assert not at.exception, (
        "Page raised on render: "
        f"{[str(e.value) for e in at.exception]}"
    )
    assign_buttons = [
        b for b in at.button if b.key and b.key.startswith("assign_match_btn_")
    ]
    assert assign_buttons == [], (
        "Assign button rendered on an auto_match-only slate; "
        f"saw keys: {[b.key for b in assign_buttons]}"
    )
    info_msgs = [i.value for i in at.info]
    assert any("No assignable odds rows" in m for m in info_msgs), (
        f"Expected the 'no assignable rows' info copy; saw: {info_msgs}"
    )


def test_reject_ui_still_renders_alongside_assign(isolated_db):
    """Regression: a review_required row exposes BOTH the 3c Reject button
    and the new 3f Assign button — the inserted container did not displace
    the reject control."""
    conn = get_connection()
    try:
        apply_schema(conn)
        slate = SlateRepository(conn).create(event_name="UFC Review Slate")
        fighter_id = _create_fighter(conn, slate.id, "Jose Aldo")
        row = OddsRowRepository(conn).create(
            slate_id=slate.id,
            fighter_name_raw="Jose Aldo",
            american_odds=-150,
            source="manual",
            captured_at="2026-05-20T00:00:00Z",
        )
        _seed_match_result(
            conn,
            slate.id,
            row,
            fighter_id=fighter_id,
            match_status="review_required",
            effective_status="review_required",
            match_stage="fuzzy",
            match_score=92,
            preferred_candidate="Jose Aldo",
        )
    finally:
        conn.close()

    at = _open_odds_page()
    assert not at.exception, (
        f"Page raised: {[str(e.value) for e in at.exception]}"
    )
    reject_buttons = [
        b for b in at.button if b.key and b.key.startswith("reject_match_btn_")
    ]
    assign_buttons = [
        b for b in at.button if b.key and b.key.startswith("assign_match_btn_")
    ]
    assert len(reject_buttons) == 1, "Reject button missing for review row"
    assert len(assign_buttons) == 1, "Assign button missing for review row"


# ---------------------------------------------------------------------------
# No page-load write
# ---------------------------------------------------------------------------


def test_no_assign_write_on_page_load(unmatched_slate):
    """Opening the page with an assignable row writes nothing — the override
    is created only on the button click."""
    at = _open_odds_page()
    assert not at.exception

    conn = get_connection()
    try:
        active = ManualMatchOverrideRepository(conn).list_active_for_slate(
            unmatched_slate["slate_id"]
        )
        assert active == [], (
            f"Page load wrote an override: {active}"
        )
        [match] = OddsMatchResultRepository(conn).list_for_slate(
            unmatched_slate["slate_id"]
        )
        assert match.effective_status == "unmatched"
        assert match.fighter_id is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_assign_unmatched_row_writes_force_pair(unmatched_slate):
    """Binding an ``unmatched`` row to an active fighter the matcher never
    proposed records a ``force_pair`` override and flips the result row's
    ``effective_status`` to ``force_pair`` with ``fighter_id`` populated."""
    at = _open_odds_page()
    # The unmatched row forces an explicit fighter pick (sentinel default).
    at.selectbox(
        key=f"assign_match_fighter_choice_{unmatched_slate['slate_id']}"
    ).set_value(unmatched_slate["fighter_id"]).run()

    [button] = _find_buttons_by_key(at, _assign_button_key(unmatched_slate))
    at = button.click().run()

    assert not at.exception, (
        f"Page raised after Assign click: "
        f"{[str(e.value) for e in at.exception]}"
    )
    error_msgs = [e.value for e in at.error]
    assert error_msgs == [], f"st.error after Assign click: {error_msgs}"

    success_msgs = [s.value for s in at.success]
    assert any(
        "force_pair" in m and "Bruno Silva" in m for m in success_msgs
    ), f"Expected force_pair success banner; saw: {success_msgs}"

    conn = get_connection()
    try:
        active = ManualMatchOverrideRepository(conn).list_active_for_slate(
            unmatched_slate["slate_id"]
        )
        assert len(active) == 1
        ov = active[0]
        assert ov.override_type == "force_pair"
        assert ov.odds_row_key == unmatched_slate["odds_row_key"]
        assert ov.fighter_id == unmatched_slate["fighter_id"]
        assert ov.superseded_at is None

        [match] = OddsMatchResultRepository(conn).list_for_slate(
            unmatched_slate["slate_id"]
        )
        assert match.match_status == "unmatched"
        assert match.effective_status == "force_pair"
        assert match.fighter_id == unmatched_slate["fighter_id"]
    finally:
        conn.close()


def test_assign_review_required_proposed_writes_accept_match(isolated_db):
    """Confirming the matcher's own proposal on a ``review_required`` row
    records an ``accept_match`` override → ``review_accepted`` effective
    status. The fighter selectbox defaults to the proposed fighter, so a
    single click accepts it."""
    conn = get_connection()
    try:
        apply_schema(conn)
        slate = SlateRepository(conn).create(event_name="UFC Accept Event")
        fighter_id = _create_fighter(conn, slate.id, "Santiago Luna")
        row = OddsRowRepository(conn).create(
            slate_id=slate.id,
            fighter_name_raw="Luan Santiago",
            american_odds=120,
            source="manual",
            captured_at="2026-05-20T00:00:00Z",
        )
        _seed_match_result(
            conn,
            slate.id,
            row,
            fighter_id=fighter_id,
            match_status="review_required",
            effective_status="review_required",
            match_stage="fuzzy",
            match_score=90,
            preferred_candidate="Santiago Luna",
        )
    finally:
        conn.close()

    state = {"slate_id": slate.id, "odds_row_id": row.id}
    at = _open_odds_page()
    # Default selectbox already points at the proposed fighter — click directly.
    [button] = _find_buttons_by_key(at, _assign_button_key(state))
    at = button.click().run()

    assert not at.exception, (
        f"Page raised after Assign click: "
        f"{[str(e.value) for e in at.exception]}"
    )
    assert [e.value for e in at.error] == []
    success_msgs = [s.value for s in at.success]
    assert any("accept_match" in m for m in success_msgs), (
        f"Expected accept_match success banner; saw: {success_msgs}"
    )

    conn = get_connection()
    try:
        active = ManualMatchOverrideRepository(conn).list_active_for_slate(
            slate.id
        )
        assert len(active) == 1
        assert active[0].override_type == "accept_match"
        assert active[0].fighter_id == fighter_id

        [match] = OddsMatchResultRepository(conn).list_for_slate(slate.id)
        assert match.match_status == "review_required"
        assert match.effective_status == "review_accepted"
        assert match.fighter_id == fighter_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Validation / error surface
# ---------------------------------------------------------------------------


def test_already_bound_fighter_surfaces_error_no_write(isolated_db):
    """Assigning a fighter who is already auto-matched to another odds row
    surfaces the service ``ValueError`` and writes no override."""
    conn = get_connection()
    try:
        apply_schema(conn)
        slate = SlateRepository(conn).create(event_name="UFC Bound Event")
        fighter_id = _create_fighter(conn, slate.id, "Jon Jones")
        bound_row = OddsRowRepository(conn).create(
            slate_id=slate.id,
            fighter_name_raw="Jon Jones",
            american_odds=-300,
            source="manual",
            captured_at="2026-05-20T00:00:00Z",
        )
        loose_row = OddsRowRepository(conn).create(
            slate_id=slate.id,
            fighter_name_raw="Jonny Jones",
            american_odds=250,
            source="manual",
            captured_at="2026-05-20T00:00:01Z",
        )
        OddsMatchResultRepository(conn).replace_for_slate(
            slate.id,
            [
                OddsMatchResultRecord(
                    slate_id=slate.id,
                    odds_row_id=bound_row.id,
                    odds_row_key=bound_row.odds_row_key,
                    fighter_id=fighter_id,
                    match_status="auto_match",
                    effective_status="auto_match",
                    match_stage="exact_conservative",
                    match_score=100,
                    preferred_candidate=None,
                    opponent_check="not_applicable",
                    candidates=(),
                    notes=(),
                ),
                OddsMatchResultRecord(
                    slate_id=slate.id,
                    odds_row_id=loose_row.id,
                    odds_row_key=loose_row.odds_row_key,
                    fighter_id=None,
                    match_status="unmatched",
                    effective_status="unmatched",
                    match_stage="none",
                    match_score=0,
                    preferred_candidate=None,
                    opponent_check="not_applicable",
                    candidates=(),
                    notes=(),
                ),
            ],
        )
    finally:
        conn.close()

    state = {"slate_id": slate.id, "odds_row_id": loose_row.id}
    at = _open_odds_page()
    at.selectbox(
        key=f"assign_match_fighter_choice_{slate.id}"
    ).set_value(fighter_id).run()
    [button] = _find_buttons_by_key(at, _assign_button_key(state))
    at = button.click().run()

    assert not at.exception
    error_msgs = [e.value for e in at.error]
    assert any("already bound" in m for m in error_msgs), (
        f"Expected an 'already bound' error; saw: {error_msgs}"
    )

    conn = get_connection()
    try:
        active = ManualMatchOverrideRepository(conn).list_active_for_slate(
            slate.id
        )
        assert active == [], f"A rejected assign still wrote an override: {active}"
        loose = next(
            r
            for r in OddsMatchResultRepository(conn).list_for_slate(slate.id)
            if r.odds_row_id == loose_row.id
        )
        assert loose.effective_status == "unmatched"
        assert loose.fighter_id is None
    finally:
        conn.close()
