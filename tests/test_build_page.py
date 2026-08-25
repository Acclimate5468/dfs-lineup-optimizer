"""AppTest coverage for the two-step builder (B2 shell + B3/B4/B5 wiring).

Loads ``app/pages/00_build.py`` via ``streamlit.testing.v1.AppTest``
against an isolated temp SQLite DB and pins
``docs/TWO_STEP_BUILDER_PRODUCTION_DESIGN.md`` §11.1 (B2 / B3) / §11.3 plus
``docs/DEVELOPMENT_NOTES.md`` §11:

- Empty DB → a "No slates yet" call-to-action, no slate selector, the
  not-started gate panel, the Step 1 upload controls (Create enabled once a
  valid CSV + event name exist; Import disabled with no slate), and a
  disabled Build button.
- With slates → the active-slate selector, the two Step cards, the Step 1
  upload/import controls, and the folded Build gate panel render; the
  rendered verdict equals ``builder_gate_view(readiness)`` for the blocked /
  warning / ready scenarios.
- **Step 1 (B3, design §5):** upload/validate is read-only; Create slate and
  Import salaries persist only on an explicit button click and route through
  the existing repository/service layer; the Game Info readout is
  suggest-only; the imported-fighter count refreshes the Step 1 card.
- **Step 2 stays status + direction:** the odds card reflects the persisted
  odds-match status and the page points to the 03 Odds page; no odds write
  path and no network call live in the builder (design §6.4).
- **Build gate (B5, design §7):** the explicit **Mark slate reviewed**
  control appears only when ``builder_gate_view.ready_to_mark`` is True and
  writes ``set_manual_review_reviewed`` only on an explicit click; the
  **Build lineups** button is disabled while blocked / warning / unreviewed
  and enabled only when ``ready_to_build`` (== ``summary.ready``); a Build
  click re-evaluates the gate fresh and refuses a stale / not-ready slate
  before the solver, and the optimizer is never run on page load or while the
  gate is not green; a ready-slate Build renders read-only lineup tables.
- Page load (and slate switch) is read-only apart from the explicit Step 1
  Create / Import, Step 2 DraftKings paste Save, and Build-section
  Mark-reviewed writes; the only session write is ``active_slate_id``.

Seed helpers mirror ``tests/test_home_dashboard_page.py`` and the salary CSV
helpers mirror ``tests/test_slate_setup_salary_import_page.py``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import get_connection
from src.db.repositories import (
    FightGroupRepository,
    FighterRepository,
    ManualMatchOverrideRepository,
    OddsBookLineRepository,
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
import src.ingestion.providers.bestfightodds_fetch as bfo_fetch_mod
import src.ingestion.consensus_save as consensus_save_mod
from src.ingestion.dk_salary_importer import REQUIRED_COLUMNS
from src.ingestion.providers.bestfightodds import (
    AcquiredMoneylineRow,
    AllBooksFighterRow,
    BestFightOddsParseError,
    BookLine,
)
from src.ingestion.providers.bestfightodds_fetch import (
    BestFightOddsFetchError,
    BestFightOddsFetchResult,
)
from src.ingestion.odds_matching_service import (
    OddsMatchResultRecord,
    recompute_and_replace_match_results,
)
from src.slate import home_dashboard as hd
from src.slate.manual_review_service import evaluate_manual_review

_SALARY_UPLOAD_KEY = "builder_salary_upload"
_BFO_URL_KEY = "builder_bfo_url"
_BFO_FETCH_BTN = "builder_bfo_fetch_btn"
_BFO_URL = "https://www.bestfightodds.com/events/test-event-1"

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = REPO_ROOT / "app" / "pages" / "00_build.py"

_BUILD_BTN_LABEL = "Build research lineups"
_MARK_REVIEWED_BTN_KEY = "builder_mark_reviewed_btn"
_SLATE_SELECTOR_KEY = "builder_active_slate_selector"

# Contest-format router selector (Captain Mode design §2 / §3, slice C1).
_CONTEST_FORMAT_KEY = "builder_contest_format"

# Step 1 DK Game Info Apply button (slice 1b).
_APPLY_GI_BTN_KEY = "builder_apply_game_info_btn"

# Gate setup-page jump button (Build gate actionability). Odds are resolved
# inline on Build, so there is no 03 Odds jump.
_FIX_FIGHT_GROUPS_BTN_KEY = "builder_goto_fight_groups_btn"
_FIX_FIGHT_GROUPS_LABEL = "Fix fight groups"
_REVIEW_ODDS_BTN_KEY = "builder_goto_odds_btn"

# Standing (always-rendered) Fight Groups link in the Step 1 card — distinct from
# the gate's blocking jump above.
_FIGHT_GROUPS_NAV_BTN_KEY = "builder_fight_groups_nav_btn"
_FIGHT_GROUPS_NAV_LABEL = "Review fight card (02 Fight Groups)"

# DraftKings copied-board paste → preview (Phase 4). Preview-only widget keys
# and the shared parser fixture used by the offline paste tests below.
_DK_PASTE_TEXT_KEY = "builder_dk_paste_text"
_DK_PASTE_URL_KEY = "builder_dk_paste_url"
_DK_PASTE_BTN_KEY = "builder_dk_paste_btn"
_DK_PASTE_SAVE_BTN_KEY = "builder_dk_paste_save_btn"
_DK_PASTE_PREVIEW_SESSION_KEY = "builder_dk_paste_preview"
_DK_PASTE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "draftkings_paste_sample.txt"

# Multi-book consensus → preview + save (ODDS_CONSENSUS_DESIGN §5.5 / §8).
_CONSENSUS_URL_KEY = "builder_consensus_url"
_CONSENSUS_PASTE_KEY = "builder_consensus_paste"
_CONSENSUS_PREVIEW_BTN_KEY = "builder_consensus_preview_btn"
_CONSENSUS_SAVE_BTN_KEY = "builder_consensus_save_btn"
_CONSENSUS_PREVIEW_DF_KEY = "builder_consensus_preview_df"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "build_page.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _open_page() -> AppTest:
    at = AppTest.from_file(str(PAGE_PATH), default_timeout=60)
    at.run()
    return at


def _insert_fighter(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    name: str,
    salary: int = 8000,
) -> int:
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, 'active')",
        (int(slate_id), name, int(salary)),
    )
    conn.commit()
    return int(cur.lastrowid)


def _save_odds_row(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    fighter_name_raw: str,
    american_odds: int,
    captured_at: str,
) -> None:
    OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source="manual",
        captured_at=captured_at,
    )


def _seed_empty_slate(name: str = "UFC NoSalary") -> int:
    """Slate row only — no salary import → ``salary_imported`` Blocking
    check fails → blocked gate."""
    conn = get_connection()
    try:
        apply_schema(conn)
        return SlateRepository(conn).create(event_name=name).id
    finally:
        conn.close()


def _seed_validated_slate(name: str = "UFC Validated", *, rows: int = 2) -> int:
    """A slate whose CSV is validated (as the Create step records) but with
    no fighters yet — the state into which the builder Import button writes."""
    conn = get_connection()
    try:
        apply_schema(conn)
        return SlateRepository(conn).create(
            event_name=name,
            salary_csv_status="validated",
            salary_row_count=rows,
        ).id
    finally:
        conn.close()


# --- Salary CSV + widget helpers (mirror the Slate Setup import test) -------


def _valid_csv_bytes(rows: list[tuple[str, int]]) -> bytes:
    header = ",".join(REQUIRED_COLUMNS)
    lines = [header]
    for i, (name, salary) in enumerate(rows, start=1):
        lines.append(
            f"F,{name},{i},{salary},Jon Doe@Jane Roe 05/22/2026,ABC"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _uploader(at: AppTest, key: str):
    return next(u for u in at.file_uploader if u.key == key)


def _upload(at: AppTest, content: bytes) -> AppTest:
    _uploader(at, _SALARY_UPLOAD_KEY).upload("dk_salary.csv", content, "text/csv")
    return at.run()


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dk_paste_sample() -> str:
    """The representative copied DraftKings UFC board (12 fights → 24 rows),
    shared with the pure-parser test fixture."""
    return _DK_PASTE_FIXTURE.read_text(encoding="utf-8")


def _set_dk_paste(at: AppTest, text: str) -> AppTest:
    at.text_area(key=_DK_PASTE_TEXT_KEY).set_value(text)
    return at.run()


def _set_dk_paste_url(at: AppTest, url: str) -> AppTest:
    at.text_input(key=_DK_PASTE_URL_KEY).set_value(url)
    return at.run()


def _parse_dk_paste(at: AppTest, text: str, *, url: str | None = None) -> AppTest:
    """Set the optional source URL + paste text, then click Parse → preview."""
    if url is not None:
        at.text_input(key=_DK_PASTE_URL_KEY).set_value(url)
    at.text_area(key=_DK_PASTE_TEXT_KEY).set_value(text)
    at = at.run()
    return _button_by_key(at, _DK_PASTE_BTN_KEY).click().run()


def _select_slate(at: AppTest, slate_id: int) -> AppTest:
    at.selectbox(key=_SLATE_SELECTOR_KEY).set_value(int(slate_id))
    return at.run()


def _seed_slate_with_active_fighters(
    names: list[str], *, event: str = "UFC Snap"
) -> int:
    """A validated slate carrying the named active fighters — the state the
    Step 2 DraftKings paste save writes into (so recompute can match)."""
    conn = get_connection()
    try:
        apply_schema(conn)
        slate = SlateRepository(conn).create(
            event_name=event,
            salary_csv_status="validated",
            salary_row_count=len(names),
        )
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
        apply_schema(conn)
        return OddsRowRepository(conn).list_for_slate(slate_id)
    finally:
        conn.close()


def _match_results(slate_id: int) -> list:
    conn = get_connection()
    try:
        apply_schema(conn)
        return OddsMatchResultRepository(conn).list_for_slate(slate_id)
    finally:
        conn.close()


def _book_lines(slate_id: int) -> list:
    conn = get_connection()
    try:
        apply_schema(conn)
        return OddsBookLineRepository(conn).list_for_slate(slate_id)
    finally:
        conn.close()


def _active_overrides(slate_id: int) -> list:
    conn = get_connection()
    try:
        apply_schema(conn)
        return ManualMatchOverrideRepository(conn).list_active_for_slate(
            slate_id
        )
    finally:
        conn.close()


def _set_event_name(at: AppTest, name: str) -> AppTest:
    at.text_input(key="builder_event_name").set_value(name)
    return at.run()


def _button_by_key(at: AppTest, key: str):
    matched = [b for b in at.button if b.key == key]
    assert len(matched) == 1, (
        f"Expected exactly one button with key {key!r}; "
        f"saw button keys: {[b.key for b in at.button]}"
    )
    return matched[0]


def _list_slates():
    conn = get_connection()
    try:
        apply_schema(conn)
        return SlateRepository(conn).list_all()
    finally:
        conn.close()


def _list_fighters(slate_id: int):
    conn = get_connection()
    try:
        apply_schema(conn)
        return FighterRepository(conn).list_for_slate(slate_id)
    finally:
        conn.close()


def _manual_review_status(slate_id: int) -> str | None:
    for s in _list_slates():
        if s.id == slate_id:
            return s.manual_review_status
    return None


def _number_input(at: AppTest, key: str):
    matched = [n for n in at.number_input if n.key == key]
    assert len(matched) == 1, (
        f"Expected exactly one number_input with key {key!r}; "
        f"saw number_input keys: {[n.key for n in at.number_input]}"
    )
    return matched[0]


FIGHTS: tuple[tuple[str, str], ...] = (
    ("Aldo", "Vera"),
    ("Holloway", "Topuria"),
    ("Pereira", "Hill"),
    ("Adesanya", "Strickland"),
    ("Volkanovski", "Makhachev"),
    ("Oliveira", "Gaethje"),
)


def _seed_groups_no_odds(name: str = "UFC NoOdds") -> int:
    """Salary + confirmed fight groups, but no odds at all →
    ``odds_unmatched_active`` Blocking fail → blocked gate."""
    conn = get_connection()
    try:
        apply_schema(conn)
        sid = SlateRepository(conn).create(
            event_name=name,
            salary_csv_status="validated",
            salary_row_count=2 * len(FIGHTS),
        ).id
        for fav, dog in FIGHTS:
            _insert_fighter(conn, slate_id=sid, name=fav, salary=8500)
            _insert_fighter(conn, slate_id=sid, name=dog, salary=7500)
        for fav, dog in FIGHTS:
            fg = FightGroupRepository(conn).create(
                slate_id=sid,
                fighter_1_name=fav,
                fighter_2_name=dog,
                scheduled_rounds=3,
            )
            FightGroupRepository(conn).update_status(fg.id, "confirmed")
        return sid
    finally:
        conn.close()


def _seed_salary_no_groups(name: str = "UFC NoGroups") -> int:
    """Salary imported (validated + active fighters) but no fight groups →
    ``fight_group_coverage`` Blocking fail → blocked gate keyed on fight groups
    (the 'Fix fight groups' jump condition)."""
    conn = get_connection()
    try:
        apply_schema(conn)
        sid = SlateRepository(conn).create(
            event_name=name,
            salary_csv_status="validated",
            salary_row_count=2 * len(FIGHTS),
        ).id
        for fav, dog in FIGHTS:
            _insert_fighter(conn, slate_id=sid, name=fav, salary=8500)
            _insert_fighter(conn, slate_id=sid, name=dog, salary=7500)
        return sid
    finally:
        conn.close()


def _seed_structurally_clean(name: str = "UFC Clean", *, reviewed: bool) -> int:
    """Full coverage + confirmed groups. ``reviewed`` False → structurally
    clean but unacked (warning gate); True → ready gate."""
    conn = get_connection()
    try:
        apply_schema(conn)
        sid = SlateRepository(conn).create(
            event_name=name,
            event_date="2026-05-31",
            salary_csv_status="validated",
            salary_row_count=2 * len(FIGHTS),
        ).id
        for fav, dog in FIGHTS:
            _insert_fighter(conn, slate_id=sid, name=fav, salary=8500)
            _insert_fighter(conn, slate_id=sid, name=dog, salary=7500)
        for fav, dog in FIGHTS:
            fg = FightGroupRepository(conn).create(
                slate_id=sid,
                fighter_1_name=fav,
                fighter_2_name=dog,
                scheduled_rounds=3,
            )
            FightGroupRepository(conn).update_status(fg.id, "confirmed")
        for i, (fav, dog) in enumerate(FIGHTS):
            _save_odds_row(
                conn,
                slate_id=sid,
                fighter_name_raw=fav,
                american_odds=-160,
                captured_at=f"2026-05-20T00:00:{2 * i:02d}Z",
            )
            _save_odds_row(
                conn,
                slate_id=sid,
                fighter_name_raw=dog,
                american_odds=+140,
                captured_at=f"2026-05-20T00:00:{2 * i + 1:02d}Z",
            )
        recompute_and_replace_match_results(conn, sid)
        if reviewed:
            SlateRepository(conn).set_manual_review_reviewed(sid)
        return sid
    finally:
        conn.close()


_SNAPSHOT_TABLES = (
    "slates",
    "fighters",
    "fight_groups",
    "odds_rows",
    "odds_match_results",
    "manual_match_overrides",
    "odds_book_lines",
)


def _db_snapshot() -> dict[str, list[tuple]]:
    conn = get_connection()
    try:
        apply_schema(conn)
        snap: dict[str, list[tuple]] = {}
        for table in _SNAPSHOT_TABLES:
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            snap[table] = [tuple(r) for r in rows]
        return snap
    finally:
        conn.close()


def _text_blob(at: AppTest) -> str:
    parts: list[str] = []
    parts.extend(m.value for m in at.markdown)
    parts.extend(c.value for c in at.caption)
    parts.extend(i.value for i in at.info)
    parts.extend(w.value for w in at.warning)
    parts.extend(s.value for s in at.subheader)
    parts.extend(t.value for t in at.title)
    return " ".join(parts)


def _build_button(at: AppTest):
    for b in at.button:
        if b.label == _BUILD_BTN_LABEL:
            return b
    raise AssertionError(
        f"Build button {_BUILD_BTN_LABEL!r} not found among "
        f"{[b.label for b in at.button]}"
    )


def _verdict_title(slate_id: int) -> str:
    """The verdict title ``builder_gate_view`` produces for ``slate_id`` —
    the single source the page renders (no re-derivation in the test)."""
    conn = get_connection()
    try:
        apply_schema(conn)
        readiness = evaluate_manual_review(conn, slate_id)
        return hd.builder_gate_view(readiness, has_slates=True).title
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Contest-format router (Captain Mode design §2 / §3, slice C1)
# ---------------------------------------------------------------------------


def _contest_radio(at: AppTest):
    """The single contest-format selector (keyed, so it is unambiguous)."""
    return at.radio(key=_CONTEST_FORMAT_KEY)


def test_contest_selector_exists_and_defaults_to_classic(isolated_db):
    """The additive Classic | Captain selector renders at the top of the
    workflow and defaults to Classic (Captain Mode design §2)."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    radio = _contest_radio(at)
    assert radio.label == "Contest format"
    assert list(radio.options) == ["Classic", "Captain"]
    assert radio.value == "Classic"
    # The default is also reflected in session state (the selector's own key).
    assert at.session_state[_CONTEST_FORMAT_KEY] == "Classic"


def test_classic_format_renders_full_builder_unchanged(isolated_db):
    """With Classic selected (the default) the existing two-step builder still
    renders in full — the router is purely additive (design §3). The Captain
    stub does not appear in Classic mode."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "Step 1" in blob
    assert "DraftKings salary" in blob
    assert "Step 2" in blob
    assert "Odds checker" in blob
    assert "Build research lineups" in blob
    # The Classic importer non-claim still renders.
    assert "Importer is NOT complete" in " ".join(w.value for w in at.warning)
    # The gated Build button is present in the Classic flow.
    assert any(b.label == _BUILD_BTN_LABEL for b in at.button)
    # The Captain builder is absent in Classic mode.
    assert "Captain Mode (Showdown)" not in blob


def test_captain_format_shows_builder_and_short_circuits_classic(isolated_db):
    """Selecting Captain renders the read-only Captain builder (slice C5) and
    stops before any Classic code runs (design §2 / §3 / §4). The Classic
    two-step builder body is absent in Captain mode. (Detailed Captain coverage
    lives in ``tests/test_captain_build_page.py``.)"""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    _contest_radio(at).set_value("Captain")
    at = at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    # The Captain read-only builder is reachable (its heading + uploader).
    assert "Captain Mode (Showdown)" in blob, blob
    assert any(u.key == "captain_salary_upload" for u in at.file_uploader)
    # The Classic two-step builder body did NOT render (st.stop fired first):
    # its Step cards, the importer warning, and the Build button are all gone.
    assert "DraftKings salary" not in blob
    assert "Odds checker" not in blob
    assert "Importer is NOT complete" not in " ".join(
        w.value for w in at.warning
    )
    assert all(b.label != _BUILD_BTN_LABEL for b in at.button)


# ---------------------------------------------------------------------------
# Empty DB
# ---------------------------------------------------------------------------


def test_empty_db_shows_cta_and_no_selector(isolated_db):
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    infos = " ".join(i.value for i in at.info)
    assert "No slates yet" in infos, infos
    # No slate selector in the empty-DB branch.
    assert [s.key for s in at.selectbox] == []
    # Empty / not-started state shows the prototype build-bar prompt.
    assert "Load both inputs to build." in _text_blob(at)


def test_empty_db_gate_not_started_and_build_disabled(isolated_db):
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    # The not-started verdict renders.
    assert "Not started" in _text_blob(at)
    # Build button present but disabled.
    assert _build_button(at).disabled is True


# ---------------------------------------------------------------------------
# Slates exist → selector + cards + gate
# ---------------------------------------------------------------------------


def test_slates_render_active_slate_selector(isolated_db):
    _seed_empty_slate()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    # The selector uses its own widget key, distinct from the shared
    # ``active_slate_id`` session value (so Create/Import can update the active
    # slate after the selector is instantiated — see the hotfix below).
    assert _SLATE_SELECTOR_KEY in [s.key for s in at.selectbox]
    # ``active_slate_id`` must not be a widget key (that caused the crash).
    assert "active_slate_id" not in [s.key for s in at.selectbox]


def test_active_slate_id_stored_in_session_state(isolated_db):
    sid = _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["active_slate_id"] == sid


def test_two_step_cards_render(isolated_db):
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    assert "Step 1" in blob
    assert "DraftKings salary" in blob
    assert "Step 2" in blob
    assert "Odds checker" in blob
    # Step-1 stat labels from the prototype.
    assert "Fighters" in blob
    assert "Fights" in blob
    assert "Cap" in blob


def test_build_card_has_titled_header(isolated_db):
    """The Build panel reads as the third stacked action: it carries its own
    titled header ("Build research lineups") alongside the Step 1 / Step 2
    input-card headers, reinforcing the 3-action flow. Presentation only — no
    "Step 3" renumbering and no gate/enablement change."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    assert "Build research lineups" in blob, blob
    # The other two action cards still carry their titles (3 titled actions).
    assert "DraftKings salary" in blob
    assert "Odds checker" in blob


def test_gate_panel_and_chips_render(isolated_db):
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    assert "Manual Review gate" in blob
    # Scoped component markers prove the gate panel + chips rendered.
    assert "tsb-gate-panel" in blob
    assert "tsb-gc" in blob


def test_importer_not_validated_warning_present(isolated_db):
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    warnings = " ".join(w.value for w in at.warning)
    assert "Importer is NOT complete" in warnings, warnings


def test_detail_pages_section_removed(isolated_db):
    """The latest prototype canvas drops the bottom Detail-pages directory —
    the builder is two stacked input cards + the Build panel, with no
    detail-page list. (Replaces the former ``test_detail_page_references_render``
    which pinned that now-removed section.)"""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    assert "Detail pages" not in blob, blob
    # The Step 2 odds note still points to 03 Odds for CSV / manual entry.
    assert "03 Odds" in blob, blob


# ---------------------------------------------------------------------------
# Gate-state display — verdict equals builder_gate_view (no re-derivation)
# ---------------------------------------------------------------------------


def test_gate_blocked_display(isolated_db):
    sid = _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    title = _verdict_title(sid)
    assert title == "Blocked"
    assert title in _text_blob(at)
    # Build stays disabled while blocked.
    assert _build_button(at).disabled is True


def test_gate_warning_display(isolated_db):
    sid = _seed_structurally_clean(reviewed=False)
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    title = _verdict_title(sid)
    assert title == "Needs review"
    assert title in _text_blob(at)
    assert _build_button(at).disabled is True


def test_gate_ready_display_build_enabled(isolated_db):
    sid = _seed_structurally_clean(reviewed=True)
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    title = _verdict_title(sid)
    assert title == "Ready"
    assert title in _text_blob(at)
    # B5 wires Build — a reviewed/ready slate enables it (design §7.1).
    assert _build_button(at).disabled is False


# ---------------------------------------------------------------------------
# Build gate — Mark-reviewed affordance gating (B5, design §7.2 / §7.4)
# ---------------------------------------------------------------------------


def test_mark_reviewed_hidden_when_blocked(isolated_db):
    """A blocked slate (structural Blocking fail) shows no Mark-reviewed
    affordance — only the structural fixes unblock it (design §7.2)."""
    _seed_groups_no_odds()  # odds_unmatched_active fails → blocked
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    keys = [b.key for b in at.button]
    assert _MARK_REVIEWED_BTN_KEY not in keys, keys
    # The Step 1 write buttons are still present; Build stays disabled.
    assert "builder_create_slate_btn" in keys, keys
    assert "builder_import_salaries_btn" in keys, keys
    assert _build_button(at).disabled is True


def test_mark_reviewed_shown_when_ready_to_mark(isolated_db):
    """A structurally-clean-but-unreviewed (warning) slate shows the explicit
    Mark-reviewed control, while Build stays disabled until it is clicked
    (design §7.2)."""
    sid = _seed_structurally_clean(reviewed=False)
    # Sanity: the gate verdict is the warning state for this fixture.
    assert _verdict_title(sid) == "Needs review"
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    keys = [b.key for b in at.button]
    assert _MARK_REVIEWED_BTN_KEY in keys, keys
    assert _build_button(at).disabled is True


_SCHEDULED_ROUNDS_ACK_KEY = "builder_scheduled_rounds_ack"


def test_scheduled_rounds_ack_checkbox_dismisses_the_warning(isolated_db):
    """A confirmed card with a 5-round main event warns on Scheduled rounds;
    ticking the rounds-reviewed checkbox flips that chip to ok (Slice #3)."""
    sid = _seed_structurally_clean(reviewed=False)
    # Promote one confirmed bout to a 5-round main event so §5.3 warns.
    conn = get_connection()
    try:
        apply_schema(conn)
        groups = FightGroupRepository(conn).list_for_slate(sid)
        FightGroupRepository(conn).update_scheduled_rounds(groups[0].id, 5)
    finally:
        conn.close()

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    assert "⚠️ Scheduled rounds" in blob, blob
    assert _SCHEDULED_ROUNDS_ACK_KEY in [c.key for c in at.checkbox], [
        c.key for c in at.checkbox
    ]

    # Tick the ack → the chip flips to ok on the next run.
    at.checkbox(key=_SCHEDULED_ROUNDS_ACK_KEY).set_value(True)
    at = at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob2 = _text_blob(at)
    assert "✅ Scheduled rounds" in blob2, blob2
    assert "⚠️ Scheduled rounds" not in blob2


# ---------------------------------------------------------------------------
# Build gate — direct setup-page jumps (gate actionability)
# ---------------------------------------------------------------------------


def test_fix_fight_groups_jump_when_coverage_blocks(isolated_db):
    """A slate blocked by fight-group coverage (salary imported, fighters
    unpaired) renders an explicit 'Fix fight groups' jump to 02 Fight Groups."""
    sid = _seed_salary_no_groups()
    assert _verdict_title(sid) == "Blocked"
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    btn = _button_by_key(at, _FIX_FIGHT_GROUPS_BTN_KEY)
    assert btn.label == _FIX_FIGHT_GROUPS_LABEL, btn.label


def test_no_review_odds_jump_when_odds_block(isolated_db):
    """A slate blocked by odds coverage (confirmed groups, no odds) renders NO
    jump to the 03 Odds page — odds are resolved inline on Build (Step 2 paste +
    name-match fixer). The fight-groups jump is also absent since coverage passes
    there."""
    sid = _seed_groups_no_odds()
    assert _verdict_title(sid) == "Blocked"
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    keys = [b.key for b in at.button]
    assert _REVIEW_ODDS_BTN_KEY not in keys, keys
    assert _FIX_FIGHT_GROUPS_BTN_KEY not in keys, keys


def test_no_setup_jumps_when_ready(isolated_db):
    """A reviewed/ready slate (gate green) shows neither setup-page jump — there
    is nothing left to fix, so the 'next required fix' block is suppressed."""
    _seed_structurally_clean(reviewed=True)
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    keys = [b.key for b in at.button]
    assert _FIX_FIGHT_GROUPS_BTN_KEY not in keys, keys
    assert _REVIEW_ODDS_BTN_KEY not in keys, keys


def test_setup_jumps_do_not_write_on_load(isolated_db):
    """Rendering the gate jump is read-only: loading a fight-group-blocked slate
    (which shows the Fix fight groups jump) changes no persisted row."""
    _seed_salary_no_groups()
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _db_snapshot() == before


def test_fight_groups_nav_link_renders_on_empty_db(isolated_db):
    """The standing 'Review fight card' link renders even on an empty DB, so a
    first-time user can reach Fight Card Review from Build immediately."""
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    nav = _button_by_key(at, _FIGHT_GROUPS_NAV_BTN_KEY)
    assert nav.label == _FIGHT_GROUPS_NAV_LABEL, nav.label


def test_fight_groups_nav_link_coexists_with_blocking_jump(isolated_db):
    """On a fight-group-blocked slate the standing link and the gate's blocking
    'Fix fight groups' jump both render, with distinct keys."""
    _seed_salary_no_groups()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    keys = {b.key for b in at.button}
    assert _FIGHT_GROUPS_NAV_BTN_KEY in keys, keys
    assert _FIX_FIGHT_GROUPS_BTN_KEY in keys, keys


def test_fight_groups_nav_link_present_when_green_and_jump_absent(isolated_db):
    """On a green/reviewed slate the gate's Fix-jump is suppressed, but the
    standing link remains — the exact case where the collapsed sidebar was
    previously the only route to Fight Groups. Read-only on load."""
    _seed_structurally_clean(reviewed=True)
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    keys = {b.key for b in at.button}
    assert _FIGHT_GROUPS_NAV_BTN_KEY in keys, keys
    assert _FIX_FIGHT_GROUPS_BTN_KEY not in keys, keys
    assert _db_snapshot() == before, "rendering the standing link must not write"


# ---------------------------------------------------------------------------
# Step 1 — salary upload / Create / Import (B3, design §5)
# ---------------------------------------------------------------------------


def test_step1_upload_controls_render(isolated_db):
    """The Step 1 upload UI (event name, CSV uploader, Create + Import
    buttons) renders, even on an empty DB, so a first-time user can
    bootstrap a slate from the builder."""
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "builder_event_name" in [t.key for t in at.text_input]
    assert len(at.file_uploader) == 1
    keys = [b.key for b in at.button]
    assert "builder_create_slate_btn" in keys, keys
    assert "builder_import_salaries_btn" in keys, keys
    # With nothing uploaded, both writes are disabled.
    assert _button_by_key(at, "builder_create_slate_btn").disabled is True
    assert _button_by_key(at, "builder_import_salaries_btn").disabled is True
    assert "Importer is NOT complete" in " ".join(w.value for w in at.warning)


def test_validated_upload_alone_writes_nothing(isolated_db):
    """Uploading + validating a CSV must not write anything; only the
    explicit Create / Import clicks persist (design §5.6)."""
    sid = _seed_validated_slate()
    before = _db_snapshot()
    at = _open_page()
    at = _upload(at, _valid_csv_bytes([("Jon Doe", 9000), ("Jane Roe", 8500)]))
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any(
        "Valid DK UFC Classic salary CSV" in s.value for s in at.success
    ), [s.value for s in at.success]
    # No fighters written, and the DB is otherwise untouched by the upload.
    assert _list_fighters(sid) == []
    assert _db_snapshot() == before, "upload+validate must not write (§5.6)"


def test_create_slate_writes_only_on_explicit_click(isolated_db):
    """On an empty DB, Create slate persists a validated slate — but only
    when the button is clicked (design §5.2 / §5.6)."""
    at = _open_page()
    at = _upload(at, _valid_csv_bytes([("Jon Doe", 9000), ("Jane Roe", 8500)]))
    at = _set_event_name(at, "UFC Builder")

    # Upload + event name alone create nothing.
    assert _list_slates() == []

    create_btn = _button_by_key(at, "builder_create_slate_btn")
    assert create_btn.disabled is False
    at = create_btn.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    slates = _list_slates()
    assert len(slates) == 1
    assert slates[0].event_name == "UFC Builder"
    assert slates[0].salary_csv_status == "validated"
    assert slates[0].salary_row_count == 2
    # The new slate becomes active, and Import is now reachable.
    assert at.session_state["active_slate_id"] == slates[0].id
    assert _button_by_key(at, "builder_import_salaries_btn").disabled is False


def test_create_second_slate_does_not_crash_and_becomes_active(isolated_db):
    """Regression: creating a slate while the active-slate selector is already
    instantiated must not raise the StreamlitAPIException
    ``st.session_state.active_slate_id cannot be modified after the widget ...``
    The new slate becomes active and the selector reflects it.

    (The empty-DB Create test never exercised this: with no slates there is no
    selector that run, so the post-instantiation write never fired.)"""
    first = _seed_validated_slate(name="UFC Existing")
    at = _open_page()
    # Sanity: a selector is present this run (its own widget key, not the
    # shared active_slate_id), so a naive active_slate_id write would crash.
    assert _SLATE_SELECTOR_KEY in [s.key for s in at.selectbox]
    assert at.session_state["active_slate_id"] == first

    at = _upload(at, _valid_csv_bytes([("Jon Doe", 9000), ("Jane Roe", 8500)]))
    at = _set_event_name(at, "UFC Second")

    create_btn = _button_by_key(at, "builder_create_slate_btn")
    assert create_btn.disabled is False
    at = create_btn.click().run()
    # The crux: no StreamlitAPIException on create with a live selector.
    assert not at.exception, [str(e.value) for e in at.exception]

    slates = _list_slates()
    assert len(slates) == 2
    new_slate = next(s for s in slates if s.event_name == "UFC Second")
    # The newly created slate is now the active slate, and the selector widget
    # reflects it after the rerun.
    assert at.session_state["active_slate_id"] == new_slate.id
    assert at.session_state[_SLATE_SELECTOR_KEY] == new_slate.id
    # Import targets the new active slate.
    import_btn = _button_by_key(at, "builder_import_salaries_btn")
    assert import_btn.disabled is False
    assert f"#{new_slate.id}" in import_btn.label


def test_import_writes_fighters_only_on_explicit_click(isolated_db):
    """Import salaries persists the uploaded CSV's fighters into the active
    slate — but only on an explicit click (design §5.6); the
    parsed/inserted counts surface in the success banner."""
    sid = _seed_validated_slate()
    at = _open_page()
    at = _upload(at, _valid_csv_bytes([("Jon Doe", 9000), ("Jane Roe", 8500)]))

    # Upload alone does not write fighters.
    assert _list_fighters(sid) == []

    import_btn = _button_by_key(at, "builder_import_salaries_btn")
    assert import_btn.disabled is False
    at = import_btn.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    fighters = _list_fighters(sid)
    assert {f.name for f in fighters} == {"Jon Doe", "Jane Roe"}
    assert all(f.status == "active" for f in fighters)
    success = next(
        (s.value for s in at.success if "Imported salaries into slate" in s.value),
        None,
    )
    assert success is not None, [s.value for s in at.success]
    assert "parsed 2" in success and "inserted 2" in success


def test_import_updates_step1_status_card_and_game_info(isolated_db):
    """After an explicit import the Step 1 card reflects the persisted
    active-fighter count, and the suggest-only Game Info readout surfaces
    (design §5.3 / §5.5)."""
    _seed_validated_slate()
    at = _open_page()
    at = _upload(at, _valid_csv_bytes([("Jon Doe", 9000), ("Jane Roe", 8500)]))
    at = _button_by_key(at, "builder_import_salaries_btn").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    # The salary card now shows the two imported active fighters.
    assert (
        '<div class="tsb-v">2</div><div class="tsb-l">Fighters</div>' in blob
    ), "Step 1 card should refresh to 2 fighters after import"
    # Suggest-only Game Info readout (counts only — no fight groups created).
    assert any(
        "Game Info captured: 2 of 2 active fighters" in m.value
        for m in at.info
    ), [m.value for m in at.info]
    # Import alone creates no fight groups (the Apply button is a separate,
    # un-clicked write here); the gate stays blocked, so Build is disabled.
    assert _build_button(at).disabled is True


def test_import_failure_writes_nothing_and_surfaces_error(isolated_db):
    """A row-level parse failure (non-integer salary) surfaces an error and
    persists no fighters (design §5; service ``parse_failed`` branch)."""
    sid = _seed_validated_slate()
    header = ",".join(REQUIRED_COLUMNS)
    bad_csv = (
        f"{header}\n"
        "F,Jon Doe,1,nine thousand,Jon Doe@Jane Roe 05/22/2026,JDO\n"
    ).encode("utf-8")

    at = _open_page()
    at = _upload(at, bad_csv)
    at = _button_by_key(at, "builder_import_salaries_btn").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert any("row-level parsing failed" in e.value for e in at.error), [
        e.value for e in at.error
    ]
    assert _list_fighters(sid) == []


# ---------------------------------------------------------------------------
# Step 2 — odds status (B4, design §6)
# ---------------------------------------------------------------------------


def test_step2_status_renders_no_odds(isolated_db):
    """With salary + groups but no odds, Step 2 status shows zeroed counts
    and the direction to the 03 Odds page (design §6.4)."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    assert "Odds checker" in blob
    assert "Odds loaded:** 0 odds rows" in blob
    assert "0 matched" in blob
    assert "03 Odds" in blob
    # The Step 1 / Build workflow buttons render; the local-slate cleanup
    # controls also render (a slate exists), but neither writes on load.
    keys = {b.key for b in at.button}
    assert {
        "builder_create_slate_btn",
        "builder_import_salaries_btn",
        "build_btn",
    } <= keys, keys


def test_step2_status_renders_matched_odds(isolated_db):
    """A fully-matched slate (12 odds rows recomputed to auto_match) reports
    those counts read-only in the Step 2 status block (design §6.4)."""
    _seed_structurally_clean(reviewed=False)
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    assert "Odds loaded:** 12 odds rows" in blob
    assert "12 matched" in blob


def test_step2_status_counts_inline_assigned_row_as_matched(isolated_db):
    """The Step 2 'N matched' count reflects projection-eligible effective_status,
    not just the raw matcher's auto_match: an inline Assign (which produces a
    force_pair binding) moves the row from 'need review' to 'matched'."""
    seed = _seed_unmatched_odds_slate()
    sid = seed["slate_id"]
    oid = seed["odds_row_id"]

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "0 matched · 1 need review" in _text_blob(at)

    # Assign the unmatched row to the DK fighter via the inline fixer.
    at.selectbox(key=f"builder_odds_fix_fighter_{sid}_{oid}").set_value(
        seed["fighter_id"]
    )
    at = at.run()
    at = _button_by_key(at, f"builder_odds_fix_assign_{sid}_{oid}").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # The status line now counts the assigned row as matched (force_pair is
    # projection-eligible), and nothing remains in 'need review'.
    assert "1 matched · 0 need review" in _text_blob(at)


# ---------------------------------------------------------------------------
# Step 2 — DraftKings copied-board paste → preview (Phase 4; offline, no save)
#
# The user pastes the text copied from the public DraftKings UFC odds board and
# clicks Parse; the page runs the pure ``parse_draftkings_paste`` parser and
# previews the normalized moneylines. Preview-only: no parse on page load, no
# DB write on success or failure, no recompute, the Manual Review gate is
# untouched, and there is no network I/O (design §3 Phase 4 / §1.9 / §1.11).
# ---------------------------------------------------------------------------

# A two-fight paste where the first fight is valid (moneyline-only) and the
# second is incomplete (a lone 'O' totals leg) — the parser returns the two
# valid rows plus one skip warning (mirrors the pure-parser warning test).
_DK_PASTE_WARN_TEXT = (
    "Good A\nvs\nGood B\n-110\n+100\n"
    "Bad A\nvs\nBad B\nO\n2.5\n-120\n"
)


def test_dk_paste_section_renders(isolated_db):
    """The DK paste control (URL field + textarea + Parse button) renders in
    Step 2, with the parse button disabled until text is pasted."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _DK_PASTE_TEXT_KEY in [t.key for t in at.text_area], [
        t.key for t in at.text_area
    ]
    assert _DK_PASTE_URL_KEY in [t.key for t in at.text_input], [
        t.key for t in at.text_input
    ]
    assert _DK_PASTE_BTN_KEY in [b.key for b in at.button], [
        b.key for b in at.button
    ]
    # The collapsed section's caption is rendered (expanders do not hide
    # elements from AppTest).
    assert "copy the visible text" in _text_blob(at), _text_blob(at)
    # Nothing pasted yet → the parse button is disabled (no parse possible).
    assert _button_by_key(at, _DK_PASTE_BTN_KEY).disabled is True
    # No preview yet → no save button (it appears only with preview rows).
    assert _DK_PASTE_SAVE_BTN_KEY not in [b.key for b in at.button]


def test_dk_paste_not_parsed_on_page_load(isolated_db, monkeypatch):
    """Page load must never invoke the parser — the parse is click-gated only.
    A boom-patched parser proves the render path does not call it (design
    §3 Phase 4; docs/DEVELOPMENT_NOTES.md §11)."""
    import src.ingestion.providers.draftkings_paste as dk_mod

    def _boom(*args, **kwargs):  # pragma: no cover - only on regression
        raise AssertionError("DK paste parser must not run on page load")

    monkeypatch.setattr(dk_mod, "parse_draftkings_paste", _boom)

    _seed_groups_no_odds()
    at = _open_page()
    # If the render path had parsed, _boom would have raised → an exception.
    assert not at.exception, [str(e.value) for e in at.exception]
    # The control rendered, but no preview was produced on load.
    assert _DK_PASTE_BTN_KEY in [b.key for b in at.button]
    assert _DK_PASTE_PREVIEW_SESSION_KEY not in at.session_state
    assert len(at.dataframe) == 0


def test_dk_paste_parses_sample_into_24_preview_rows(isolated_db):
    """An explicit Parse click on a representative copied board renders a
    single 24-row preview table (12 fights × 2), every row sourced/booked as
    DraftKings — and writes nothing (design §3 Phase 4)."""
    sid = _seed_slate_with_active_fighters(["Alpha Fighter", "Beta Fighter"])
    before = _db_snapshot()
    at = _open_page()
    at = _set_dk_paste(at, _dk_paste_sample())

    parse_btn = _button_by_key(at, _DK_PASTE_BTN_KEY)
    assert parse_btn.disabled is False
    at = parse_btn.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert any(
        "Parsed 24 DraftKings moneyline(s)" in s.value for s in at.success
    ), [s.value for s in at.success]
    # The DK preview is the only table rendered on this flow.
    assert len(at.dataframe) == 1
    df = at.dataframe[0].value
    assert list(df.columns) == [
        "Fighter",
        "Opponent",
        "Moneyline",
        "Source",
        "Book",
    ]
    assert len(df) == 24
    assert set(df["Source"]) == {"DraftKings"}
    assert set(df["Book"]) == {"DraftKings"}
    # Preview-only: no odds rows persisted, the DB is untouched.
    assert _odds_rows(sid) == []
    assert _db_snapshot() == before, "DK paste preview must not write (Phase 4)"


def test_dk_paste_ignores_over_under_through_preview(isolated_db):
    """The Total Rounds (over/under) prices must never surface as a fighter
    moneyline in the rendered preview. Fight one's real lines are +525 / −750;
    its totals prices were +120 / −154 and must be absent (design §3 Phase 4)."""
    _seed_slate_with_active_fighters(["Alpha Fighter", "Beta Fighter"])
    at = _open_page()
    at = _set_dk_paste(at, _dk_paste_sample())
    at = _button_by_key(at, _DK_PASTE_BTN_KEY).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    df = at.dataframe[0].value
    ml = {f: int(m) for f, m in zip(df["Fighter"], df["Moneyline"])}
    assert ml["Matt Schnell"] == 525
    assert ml["Alessandro Costa"] == -750
    # Neither of fight one's totals prices leaked in for either fighter.
    fight_one = [
        int(m)
        for f, m in zip(df["Fighter"], df["Moneyline"])
        if f in ("Matt Schnell", "Alessandro Costa")
    ]
    assert 120 not in fight_one and -154 not in fight_one, fight_one


def test_dk_paste_renders_warnings_when_parser_warns(isolated_db):
    """When the parser skips an incomplete fight block it returns a warning;
    the preview surfaces it (the valid fight still parses) (design §3 Phase 4)."""
    _seed_slate_with_active_fighters(["Alpha Fighter", "Beta Fighter"])
    at = _open_page()
    at = _set_dk_paste(at, _DK_PASTE_WARN_TEXT)
    at = _button_by_key(at, _DK_PASTE_BTN_KEY).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # The one valid fight parsed (2 rows); the incomplete block is surfaced.
    assert any(
        "Parsed 2 DraftKings moneyline(s)" in s.value for s in at.success
    ), [s.value for s in at.success]
    warns = " ".join(w.value for w in at.warning)
    assert "skipped" in warns.lower(), warns
    assert "Bad A vs Bad B" in warns, warns


def test_dk_paste_parse_failure_shows_error_and_writes_nothing(isolated_db):
    """Text with no fight pairing makes the parser raise; the page shows a
    user-facing error, renders no preview table, does not crash, and writes
    nothing (design §3 Phase 4; docs/DEVELOPMENT_NOTES.md §11)."""
    sid = _seed_slate_with_active_fighters(["Alpha Fighter", "Beta Fighter"])
    before = _db_snapshot()
    at = _open_page()
    at = _set_dk_paste(at, "Total Rounds\nMoneyline\n+120\n-150\n")
    at = _button_by_key(at, _DK_PASTE_BTN_KEY).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    errors = " ".join(e.value for e in at.error)
    assert "Could not parse any DraftKings moneylines" in errors, errors
    # No preview table on failure, and nothing was written.
    assert len(at.dataframe) == 0
    assert _odds_rows(sid) == []
    assert _db_snapshot() == before, "a failed DK paste parse must not write"


# ---------------------------------------------------------------------------
# Step 2 — DraftKings paste SAVE (Phase 3A): explicit, click-gated persistence
#
# The Save button persists the previewed rows through the existing
# ``odds_rows`` → recompute path (``draftkings_paste_save``). It is shown only
# when a preview exists *and* an active slate exists, fires only on an explicit
# click, never on parse / page load, and surfaces a failure as an inline error
# without crashing (design §2 / §3 Phase 3A; ``docs/DEVELOPMENT_NOTES.md`` §11).
# ---------------------------------------------------------------------------

def _dk_save_sample() -> str:
    """A minimal moneyline-only board: one fight → two paired rows
    (Matt Schnell +525 / Alessandro Costa −750). Keeps the save tests small and
    independent of the full 24-row fixture while exercising opponent pairing."""
    return "Matt Schnell\nvs\nAlessandro Costa\n+525\n-750\n"


def test_dk_paste_url_field_renders_and_does_not_fetch(isolated_db, monkeypatch):
    """The optional source-URL field renders, is carried into the preview as
    provenance, and never triggers a fetch — parsing the pasted board with a DK
    URL set never calls any fetcher and writes nothing (design §1.9 / §1.11)."""
    # If the URL path tried to fetch BestFightOdds (the only fetcher), this
    # boom would raise — proving the paste path is offline.
    def _boom(*args, **kwargs):  # pragma: no cover - only on regression
        raise AssertionError("DK paste URL must never be fetched")

    monkeypatch.setattr(bfo_fetch_mod, "fetch_bestfightodds_preview", _boom)

    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    before = _db_snapshot()
    at = _open_page()
    assert _DK_PASTE_URL_KEY in [t.key for t in at.text_input]

    dk_url = (
        "https://sportsbook.draftkings.com/leagues/mma/ufc?category=fights"
        "&subcategory=fight-lines"
    )
    at = _parse_dk_paste(at, _dk_save_sample(), url=dk_url)
    assert not at.exception, [str(e.value) for e in at.exception]

    # Preview produced, URL carried into session provenance, nothing written.
    preview = at.session_state[_DK_PASTE_PREVIEW_SESSION_KEY]
    assert preview["source_url"] == dk_url
    assert _odds_rows(sid) == []
    assert _db_snapshot() == before, "parse with a URL must not fetch or write"


def test_dk_paste_save_button_hidden_without_preview(isolated_db):
    """With an active slate but no preview, the Save button is not shown — it
    appears only once a preview exists."""
    _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    at = _open_page()
    assert _DK_PASTE_SAVE_BTN_KEY not in [b.key for b in at.button]


def test_dk_paste_save_button_appears_with_preview(isolated_db):
    """A successful parse on an active slate reveals the Save button, enabled."""
    _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    at = _open_page()
    at = _parse_dk_paste(at, _dk_save_sample())
    assert not at.exception, [str(e.value) for e in at.exception]
    save_btn = _button_by_key(at, _DK_PASTE_SAVE_BTN_KEY)
    assert save_btn.disabled is False


def test_dk_paste_preview_alone_does_not_write(isolated_db):
    """Parsing + previewing (even with the Save button now visible) must not
    write — only the explicit Save click does (docs/DEVELOPMENT_NOTES.md §11)."""
    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    before = _db_snapshot()
    at = _open_page()
    at = _parse_dk_paste(at, _dk_save_sample())
    assert not at.exception, [str(e.value) for e in at.exception]
    # Save button present but unclicked → nothing persisted.
    assert _DK_PASTE_SAVE_BTN_KEY in [b.key for b in at.button]
    assert _odds_rows(sid) == []
    assert _match_results(sid) == []
    assert _db_snapshot() == before, "DK paste preview must not write (Phase 3A)"


def test_dk_paste_save_persists_rows_to_active_slate(isolated_db):
    """An explicit Save writes source="draftkings_paste" rows (book DraftKings,
    opponent preserved) into the active slate (design §2 / Phase 3A)."""
    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    before = _db_snapshot()
    at = _open_page()
    at = _parse_dk_paste(at, _dk_save_sample())
    assert _db_snapshot() == before  # parse alone wrote nothing

    save_btn = _button_by_key(at, _DK_PASTE_SAVE_BTN_KEY)
    assert save_btn.disabled is False
    at = save_btn.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = _odds_rows(sid)
    assert len(rows) == 2
    assert {r.fighter_name_raw for r in rows} == {
        "Matt Schnell",
        "Alessandro Costa",
    }
    assert all(r.source == "draftkings_paste" for r in rows)
    assert all(r.bookmaker == "DraftKings" for r in rows)
    # Opponent is preserved through the save path.
    by_name = {r.fighter_name_raw: r for r in rows}
    assert by_name["Matt Schnell"].opponent_name_raw == "Alessandro Costa"
    assert by_name["Alessandro Costa"].opponent_name_raw == "Matt Schnell"

    successes = " ".join(s.value for s in at.success)
    assert "Saved 2 DraftKings moneyline(s)" in successes, successes
    assert "draftkings_paste" in successes, successes


def test_dk_paste_save_recomputes_persisted_match_results(isolated_db):
    """A Save chains the existing recompute, persisting odds match results for
    the active slate (design §2 — reuse the storage/match/recompute path)."""
    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    assert _match_results(sid) == []  # none before the save

    at = _open_page()
    at = _parse_dk_paste(at, _dk_save_sample())
    at = _button_by_key(at, _DK_PASTE_SAVE_BTN_KEY).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    results = _match_results(sid)
    assert len(results) == 2, results  # one per saved odds row
    successes = " ".join(s.value for s in at.success)
    assert "Recomputed match results" in successes, successes


def test_dk_paste_save_is_idempotent(isolated_db):
    """Re-saving the identical previewed batch adds no rows and reports the
    idempotent already-existed path (same captured_at → same odds_row_key)."""
    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    at = _open_page()
    at = _parse_dk_paste(at, _dk_save_sample())

    at = _button_by_key(at, _DK_PASTE_SAVE_BTN_KEY).click().run()
    assert len(_odds_rows(sid)) == 2

    at = _button_by_key(at, _DK_PASTE_SAVE_BTN_KEY).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(_odds_rows(sid)) == 2
    assert "already existed" in " ".join(i.value for i in at.info), [
        i.value for i in at.info
    ]


def test_dk_paste_save_does_not_write_to_wrong_slate(isolated_db):
    """The Save writes only to the active slate; a second, non-active slate is
    untouched (design §2; docs/DEVELOPMENT_NOTES.md §11 — slate-scoped writes)."""
    other = _seed_slate_with_active_fighters(
        ["Other A", "Other B"], event="UFC Other"
    )
    active = _seed_slate_with_active_fighters(
        ["Matt Schnell", "Alessandro Costa"], event="UFC Active"
    )
    at = _open_page()
    at = _select_slate(at, active)
    assert at.session_state["active_slate_id"] == active

    at = _parse_dk_paste(at, _dk_save_sample())
    at = _button_by_key(at, _DK_PASTE_SAVE_BTN_KEY).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert len(_odds_rows(active)) == 2
    assert _odds_rows(other) == [], "non-active slate must not be written"
    assert _match_results(other) == []


def test_dk_paste_save_failure_shows_error_and_does_not_crash(
    isolated_db, monkeypatch
):
    """A save-service exception is surfaced as an inline error; the page does
    not crash and nothing is partially written (docs/DEVELOPMENT_NOTES.md §11)."""
    import src.ingestion.draftkings_paste_save as dk_save_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic save failure")

    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    at = _open_page()
    at = _parse_dk_paste(at, _dk_save_sample())

    # Patch the page's module-qualified save call (mirrors the BFO fetch tests).
    monkeypatch.setattr(dk_save_mod, "save_draftkings_paste_rows", _boom)
    before = _db_snapshot()
    at = _button_by_key(at, _DK_PASTE_SAVE_BTN_KEY).click().run()

    assert not at.exception, [str(e.value) for e in at.exception]
    errors = " ".join(e.value for e in at.error)
    assert "Could not save DraftKings odds" in errors, errors
    assert "synthetic save failure" in errors, errors
    assert _db_snapshot() == before, "a failed save must not write"


def test_dk_paste_save_refreshes_step2_status_in_place(isolated_db):
    """A Save reruns so the Step 2 odds-status card reflects the just-saved rows
    in the same interaction — previously the status read a run behind (still
    showed zero rows until the next natural rerun). Proves the rerun fix."""
    _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    at = _open_page()
    at = _parse_dk_paste(at, _dk_save_sample())

    # Before the save, the status card is zeroed (preview wrote nothing).
    assert "Odds loaded:** 0 odds rows" in _text_blob(at)

    at = _button_by_key(at, _DK_PASTE_SAVE_BTN_KEY).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # After the save the same run's status card shows the persisted, matched
    # counts — no manual reload needed.
    blob = _text_blob(at)
    assert "Odds loaded:** 2 odds rows" in blob, blob
    assert "2 matched" in blob, blob
    # The save feedback still surfaces alongside the refreshed status.
    assert "Saved 2 DraftKings moneyline(s)" in " ".join(
        s.value for s in at.success
    )


# ---------------------------------------------------------------------------
# Step 2 — inline single-fighter manual moneyline entry (Slice 3)
#
# Closes the coverage gap the name-match fixer cannot (a fighter with no odds
# row at all). Save persists one manual row + recomputes so the fighter matches
# and counts. Button-only: page load only reads.
# ---------------------------------------------------------------------------

_MANUAL_ODDS_FIGHTER_KEY = "builder_manual_odds_fighter"
_MANUAL_ODDS_ML_KEY = "builder_manual_odds_moneyline"
_MANUAL_ODDS_SAVE_BTN = "builder_manual_odds_save_btn"


def test_manual_odds_form_renders_and_writes_nothing_on_load(isolated_db):
    """The manual-entry form (fighter selectbox, moneyline input, Save button)
    renders, and rendering it persists nothing."""
    _seed_slate_with_active_fighters(["Alpha Fighter", "Beta Fighter"])
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _MANUAL_ODDS_FIGHTER_KEY in [s.key for s in at.selectbox], [
        s.key for s in at.selectbox
    ]
    assert _MANUAL_ODDS_ML_KEY in [n.key for n in at.number_input], [
        n.key for n in at.number_input
    ]
    assert _MANUAL_ODDS_SAVE_BTN in [b.key for b in at.button], [
        b.key for b in at.button
    ]
    assert _db_snapshot() == before, "manual odds form must not write on load"


def test_manual_odds_save_persists_row_and_matches_fighter(isolated_db):
    """Picking a DK fighter, entering a moneyline, and clicking Save writes one
    source='manual' odds row and recomputes so the fighter auto-matches
    (covered)."""
    sid = _seed_slate_with_active_fighters(["Alpha Fighter", "Beta Fighter"])
    alpha = next(f for f in _list_fighters(sid) if f.name == "Alpha Fighter")

    at = _open_page()
    at.selectbox(key=_MANUAL_ODDS_FIGHTER_KEY).set_value(alpha.id)
    _number_input(at, _MANUAL_ODDS_ML_KEY).set_value(-150)
    at = at.run()
    at = _button_by_key(at, _MANUAL_ODDS_SAVE_BTN).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = _odds_rows(sid)
    assert len(rows) == 1
    assert rows[0].source == "manual"
    assert rows[0].american_odds == -150
    assert rows[0].fighter_name_raw == "Alpha Fighter"

    matched = [r for r in _match_results(sid) if r.fighter_id == alpha.id]
    assert len(matched) == 1
    assert matched[0].effective_status == "auto_match"
    successes = " ".join(s.value for s in at.success)
    assert "Alpha Fighter" in successes, successes


def test_manual_odds_save_rejects_zero_moneyline(isolated_db):
    """A zero moneyline is rejected with a warning and writes nothing (the
    schema forbids american_odds == 0)."""
    sid = _seed_slate_with_active_fighters(["Alpha Fighter", "Beta Fighter"])
    alpha = next(f for f in _list_fighters(sid) if f.name == "Alpha Fighter")
    before = _db_snapshot()

    at = _open_page()
    at.selectbox(key=_MANUAL_ODDS_FIGHTER_KEY).set_value(alpha.id)
    _number_input(at, _MANUAL_ODDS_ML_KEY).set_value(0)
    at = at.run()
    at = _button_by_key(at, _MANUAL_ODDS_SAVE_BTN).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    warnings = " ".join(w.value for w in at.warning)
    assert "non-zero" in warnings, warnings
    assert _odds_rows(sid) == []
    assert _db_snapshot() == before, "a rejected entry must not write"


# ---------------------------------------------------------------------------
# Read-only invariants (docs/DEVELOPMENT_NOTES.md §11; design §11.3)
# ---------------------------------------------------------------------------


def test_page_load_does_not_mutate_db_blocked(isolated_db):
    _seed_groups_no_odds()
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _db_snapshot() == before, "builder load must be read-only (§11)"


def test_page_load_does_not_mutate_db_ready(isolated_db):
    _seed_structurally_clean(reviewed=True)
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _db_snapshot() == before, "builder load must be read-only (§11)"


def test_no_optimizer_or_export_run_on_build_load(isolated_db, monkeypatch):
    """The builder must never invoke the solver or the export builder on
    load and renders no lineup tables until an explicit Build click — even on
    a ready slate where the Build button is now enabled (design §7.5 /
    §7.6 / §11.3)."""

    def _boom(*args, **kwargs):  # pragma: no cover - only on regression
        raise AssertionError("builder must not run this on load (§11.3)")

    # Import the export service with the *real* ``run_optimizer`` bound before
    # patching, so patching ``run_optimizer`` here cannot permanently poison
    # ``build_run_log``'s own ``from ... import run_optimizer`` reference for
    # the rest of the session (``build_run_log`` calls ``run_optimizer``
    # internally; B6 reaches it from the Build click).
    import src.exports.export_service  # noqa: F401

    monkeypatch.setattr(
        "src.optimizer.optimizer_service.run_optimizer", _boom, raising=False
    )
    monkeypatch.setattr(
        "src.exports.export_service.build_run_log", _boom, raising=False
    )

    _seed_structurally_clean(reviewed=True)
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    # No lineup / preview tables on a read-only load (no Build click yet).
    assert len(at.dataframe) == 0
    # B5 enables Build on a ready slate, but the solver still only runs on
    # an explicit click — the boom patch above proves it did not run on load.
    assert _build_button(at).disabled is False


# ---------------------------------------------------------------------------
# Build gate — Mark-reviewed write (B5, design §7.4)
# ---------------------------------------------------------------------------


def test_mark_reviewed_not_written_on_page_load(isolated_db):
    """Page load on a ready-to-mark (warning) slate must not mark it reviewed
    — the write is explicit only (design §7.4; docs/DEVELOPMENT_NOTES.md §11)."""
    sid = _seed_structurally_clean(reviewed=False)
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _manual_review_status(sid) != "reviewed"
    assert _db_snapshot() == before, "mark-reviewed must not fire on load (§7.4)"


def test_mark_reviewed_writes_only_on_explicit_click(isolated_db):
    """The Mark-reviewed control writes ``set_manual_review_reviewed`` only on
    an explicit click; afterward the gate flips ready and Build unlocks
    (design §7.4 / §7.2)."""
    sid = _seed_structurally_clean(reviewed=False)
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    # Rendering the control alone does not write.
    assert _manual_review_status(sid) != "reviewed"

    mark_btn = _button_by_key(at, _MARK_REVIEWED_BTN_KEY)
    assert mark_btn.disabled is False
    at = mark_btn.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert _manual_review_status(sid) == "reviewed"
    assert any("manually reviewed" in s.value for s in at.success), [
        s.value for s in at.success
    ]
    # The gate is now ready, so Build is enabled after the rerun.
    assert _verdict_title(sid) == "Ready"
    assert _build_button(at).disabled is False


# ---------------------------------------------------------------------------
# Build gate — gated optimizer run (B5, design §7.1 / §7.5 / §7.6)
# ---------------------------------------------------------------------------


def test_builder_n_lineups_input_bounded_one_to_five(isolated_db):
    """The Build lineup-count control is bounded to [1, 5] (design §7.6;
    the solver owns the bound and the page must not exceed it)."""
    _seed_structurally_clean(reviewed=True)
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    ni = _number_input(at, "builder_n_lineups")
    assert ni.min == 1
    assert ni.max == 5
    assert ni.step == 1
    assert ni.value == 1


def test_ready_slate_build_click_renders_lineups(isolated_db):
    """A ready slate enables Build; an explicit click runs the gated optimizer
    and renders a read-only per-lineup table (design §7.1 / §7.6)."""
    _seed_structurally_clean(reviewed=True)
    before = _db_snapshot()
    at = _open_page()
    build_btn = _build_button(at)
    assert build_btn.disabled is False

    at = build_btn.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    markdown_blob = " ".join(m.value for m in at.markdown)
    assert "Solver status:" in markdown_blob, markdown_blob
    assert "`ok`" in markdown_blob, markdown_blob
    # Exactly one 6-fighter table on a default n_lineups=1 solve.
    assert len(at.dataframe) == 1
    df = at.dataframe[0].value
    assert list(df.columns) == ["Fighter", "Salary"]
    assert len(df) == 6
    captions = " ".join(c.value for c in at.caption)
    assert "Total salary:" in captions
    assert "research lineups, not guaranteed winning lineups" in captions
    # run_optimizer is read-only end to end — the Build click writes nothing.
    assert _db_snapshot() == before, (
        "Build (run_optimizer) is read-only; the click must not write (§7.7)."
    )


def _why_expanders(at: AppTest):
    return [e for e in at.expander if "Why this lineup?" in (e.label or "")]


def test_ready_slate_build_renders_per_lineup_reasoning(isolated_db):
    """B6: a ready-slate Build renders a compact "Why this lineup?" expander
    per lineup, sourced from the read-only ``assemble_reasoning_context`` +
    the pure ``build_lineup_reasoning`` generator (design §8 / §11.1 B6). The
    reasoning cites the lineup roster/totals and at least one fighter-level
    deterministic reason, never asserts an outcome (§8.3), and writes
    nothing (§7.7)."""
    _seed_structurally_clean(reviewed=True)
    before = _db_snapshot()
    at = _open_page()
    at = _build_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # The B6 placeholder is gone; reasoning is now wired.
    assert "wired in the next slice" not in _text_blob(at)

    # Exactly one "Why this lineup?" expander on a default n_lineups=1 solve.
    why = _why_expanders(at)
    assert len(why) == 1, [e.label for e in at.expander]

    # Reasoning lines live inside the expander (isolated from page chrome).
    reasoning = " ".join(m.value for m in why[0].markdown)
    assert "6-fighter roster" in reasoning, reasoning
    assert "total salary" in reasoning, reasoning
    assert "projected points" in reasoning, reasoning
    # At least one fighter-level deterministic reason.
    assert "is the top projection" in reasoning, reasoning
    # No fabricated outcome / lock / finish claims (§8.3).
    low = reasoning.lower()
    for banned in ("lock", "guarantee", "will win", "finish", "itd"):
        assert banned not in low, reasoning

    # Reasoning assembly (build_run_log + assemble + generate) is read-only —
    # the Build click still writes nothing.
    assert _db_snapshot() == before, (
        "reasoning assembly must not write to the DB (§7.7)."
    )


def test_build_renders_reasoning_for_every_lineup(isolated_db):
    """B6: with multiple research lineups, every rendered lineup table carries
    its own "Why this lineup?" expander (one-to-one)."""
    _seed_structurally_clean(reviewed=True)
    at = _open_page()
    at = _number_input(at, "builder_n_lineups").set_value(5).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    at = _build_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    n_tables = len(at.dataframe)
    assert n_tables >= 2, f"expected a multi-lineup solve, saw {n_tables} tables"
    why = _why_expanders(at)
    assert len(why) == n_tables, (
        "expected one reasoning expander per lineup table; "
        f"tables={n_tables}, why labels={[e.label for e in why]}"
    )


def test_reasoning_expander_absent_on_page_load(isolated_db):
    """Reasoning renders only after an explicit Build click — a read-only load
    of a ready slate shows no "Why this lineup?" expander (design §11.3)."""
    _seed_structurally_clean(reviewed=True)
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _why_expanders(at) == []


def test_blocked_build_click_is_noop_and_runs_no_optimizer(
    isolated_db, monkeypatch
):
    """On a blocked slate the Build button is disabled; a (dropped) click must
    not crash, must render no lineups, must not mutate the DB, and must never
    reach the optimizer (design §7.5 / §11.1 B5 defense-in-depth)."""

    def _boom(*args, **kwargs):
        raise AssertionError("run_optimizer must not run while the gate fails")

    monkeypatch.setattr(
        "src.optimizer.optimizer_service.run_optimizer", _boom, raising=False
    )

    _seed_groups_no_odds()  # blocked: odds_unmatched_active fails
    before = _db_snapshot()
    at = _open_page()
    build_btn = _build_button(at)
    assert build_btn.disabled is True

    # Streamlit drops a disabled-button click; lock in the no-op contract.
    at = build_btn.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(at.dataframe) == 0
    assert _db_snapshot() == before


def test_build_click_rechecks_readiness_and_refuses_stale_slate(
    isolated_db, monkeypatch
):
    """Defense in depth (design §7.5): even with the button rendered enabled,
    the click handler re-evaluates Manual Review fresh and aborts before the
    solver when the slate is no longer ready — the optimizer is never called.

    The render sees a genuine ready slate (button enabled, click honored); the
    handler's re-check is forced not-ready, proving the in-handler gate."""
    import src.slate.manual_review_service as mrs

    sid = _seed_structurally_clean(reviewed=True)
    at = _open_page()
    build_btn = _build_button(at)
    assert build_btn.disabled is False

    real = mrs.evaluate_manual_review
    calls: list[int] = []

    def _staged(conn, slate_id, **kwargs):
        calls.append(int(slate_id))
        if len(calls) == 1:
            return real(conn, slate_id, **kwargs)  # render: genuine ready
        # handler re-check: unknown slate → not ready
        return real(conn, 999_999, **kwargs)

    def _boom(*args, **kwargs):
        raise AssertionError(
            "run_optimizer must not be called when the re-check is not ready"
        )

    monkeypatch.setattr(mrs, "evaluate_manual_review", _staged)
    monkeypatch.setattr(
        "src.optimizer.optimizer_service.run_optimizer", _boom, raising=False
    )

    at = build_btn.click().run()
    # _boom never raised → the optimizer was not reached.
    assert not at.exception, [str(e.value) for e in at.exception]
    # The handler aborted with the stale-slate message and rendered no lineups.
    errors = " ".join(e.value for e in at.error)
    assert "no longer ready" in errors, errors
    assert len(at.dataframe) == 0
    # Exactly two readiness reads happened that run: render + handler re-check.
    assert calls == [sid, sid], calls


# ---------------------------------------------------------------------------
# Local slate cleanup — selected-slate delete + full reset (stabilization)
#
# Destructive, local-first, repository-layer-only writes: nothing fires on page
# load; the selected-slate delete is gated behind an explicit confirm checkbox
# and the full reset behind typing RESET; a delete cascades all dependent rows
# and resets the active slate to a survivor (or the empty-DB state) safely.
# ---------------------------------------------------------------------------

_DELETE_CONFIRM_KEY = "builder_delete_confirm"
_RESET_ALL_TEXT_KEY = "builder_reset_all_text"
_DELETE_SLATE_BTN = "builder_delete_slate_btn"
_RESET_ALL_BTN = "builder_reset_all_btn"


def _checkbox_by_key(at: AppTest, key: str):
    matched = [c for c in at.checkbox if c.key == key]
    assert len(matched) == 1, (
        f"Expected exactly one checkbox with key {key!r}; "
        f"saw checkbox keys: {[c.key for c in at.checkbox]}"
    )
    return matched[0]


def _list_fight_groups(slate_id: int):
    conn = get_connection()
    try:
        apply_schema(conn)
        return FightGroupRepository(conn).list_for_slate(slate_id)
    finally:
        conn.close()


def test_cleanup_controls_render_without_writing_on_load(isolated_db):
    """The cleanup section renders its delete + reset controls when a slate
    exists, both disabled until explicitly confirmed, and writes nothing on
    load (docs/DEVELOPMENT_NOTES.md §11)."""
    _seed_groups_no_odds()
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    keys = {b.key for b in at.button}
    assert _DELETE_SLATE_BTN in keys, keys
    assert _RESET_ALL_BTN in keys, keys
    # Destructive actions are disabled until the user confirms.
    assert _button_by_key(at, _DELETE_SLATE_BTN).disabled is True
    assert _button_by_key(at, _RESET_ALL_BTN).disabled is True
    assert _db_snapshot() == before, "cleanup section must not write on load"


def test_delete_selected_slate_requires_confirmation(isolated_db):
    """The Delete button is disabled until the explicit confirm checkbox is
    checked (no one-click destructive delete)."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _button_by_key(at, _DELETE_SLATE_BTN).disabled is True

    at = _checkbox_by_key(at, _DELETE_CONFIRM_KEY).set_value(True).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _button_by_key(at, _DELETE_SLATE_BTN).disabled is False


def test_delete_writes_only_on_explicit_click(isolated_db):
    """Arming the confirm checkbox alone must not delete; only the explicit
    Delete click writes (docs/DEVELOPMENT_NOTES.md §11)."""
    sid = _seed_groups_no_odds()
    before = _db_snapshot()
    at = _open_page()

    # Checking the confirm box (no click) leaves the slate + its rows intact.
    at = _checkbox_by_key(at, _DELETE_CONFIRM_KEY).set_value(True).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any(s.id == sid for s in _list_slates())
    assert _db_snapshot() == before, "arming confirm alone must not delete"

    # The explicit click deletes the slate.
    at = _button_by_key(at, _DELETE_SLATE_BTN).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert all(s.id != sid for s in _list_slates())


def test_delete_selected_slate_cascades_and_resets_active(isolated_db):
    """Deleting the active slate removes it and all dependent rows (fighters,
    fight groups) and resets the active slate to a surviving slate — no stale
    id, no crash."""
    keep = _seed_groups_no_odds(name="UFC Keep")
    target = _seed_groups_no_odds(name="UFC Target")  # newest → active
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["active_slate_id"] == target
    # The active slate carries dependent rows before the delete.
    assert _list_fighters(target)
    assert _list_fight_groups(target)

    at = _checkbox_by_key(at, _DELETE_CONFIRM_KEY).set_value(True).run()
    at = _button_by_key(at, _DELETE_SLATE_BTN).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    ids = {s.id for s in _list_slates()}
    assert target not in ids and keep in ids
    # Dependent rows for the deleted slate cascaded away; the survivor's remain.
    assert _list_fighters(target) == []
    assert _list_fight_groups(target) == []
    assert _list_fighters(keep)
    # The active slate reset to the survivor, safely.
    assert at.session_state["active_slate_id"] == keep
    assert any("Deleted slate" in s.value for s in at.success), [
        s.value for s in at.success
    ]
    # The confirm checkbox is cleared after the reset (no armed re-delete).
    assert _checkbox_by_key(at, _DELETE_CONFIRM_KEY).value is False


def test_delete_last_slate_returns_to_empty_state(isolated_db):
    """Deleting the only slate returns the page to the empty-DB call-to-action
    with the active slate cleared — no crash, no stale selection."""
    _seed_groups_no_odds()
    at = _open_page()
    at = _checkbox_by_key(at, _DELETE_CONFIRM_KEY).set_value(True).run()
    at = _button_by_key(at, _DELETE_SLATE_BTN).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert _list_slates() == []
    assert "No slates yet" in " ".join(i.value for i in at.info)
    # The active-slate selection was cleared (no stale id left behind).
    assert "active_slate_id" not in at.session_state


def test_reset_all_requires_typed_token_and_clears_everything(isolated_db):
    """The full reset is disabled until RESET is typed; on click it deletes
    every slate and all dependent rows, returning the empty-DB state."""
    _seed_groups_no_odds(name="UFC A")
    _seed_groups_no_odds(name="UFC B")
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    # Disabled with no token, and with a wrong token.
    assert _button_by_key(at, _RESET_ALL_BTN).disabled is True
    at = at.text_input(key=_RESET_ALL_TEXT_KEY).set_value("nope").run()
    assert _button_by_key(at, _RESET_ALL_BTN).disabled is True
    assert len(_list_slates()) == 2

    # The exact token enables it; a click clears all slates + dependents.
    at = at.text_input(key=_RESET_ALL_TEXT_KEY).set_value("RESET").run()
    reset_btn = _button_by_key(at, _RESET_ALL_BTN)
    assert reset_btn.disabled is False
    at = reset_btn.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert _list_slates() == []
    assert "No slates yet" in " ".join(i.value for i in at.info)
    assert any("Reset complete" in s.value for s in at.success), [
        s.value for s in at.success
    ]


def test_reset_all_writes_only_on_explicit_click(isolated_db):
    """Typing RESET (no click) must not delete anything (docs/DEVELOPMENT_NOTES.md §11)."""
    _seed_groups_no_odds(name="UFC A")
    _seed_groups_no_odds(name="UFC B")
    before = _db_snapshot()
    at = _open_page()
    at = at.text_input(key=_RESET_ALL_TEXT_KEY).set_value("RESET").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(_list_slates()) == 2
    assert _db_snapshot() == before, "typing RESET alone must not delete"


def test_stale_active_slate_id_falls_back_safely(isolated_db):
    """A stale ``active_slate_id`` (pointing at a slate that no longer exists)
    is handled safely on the next run — the page falls back to a real slate
    without crashing."""
    sid = _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["active_slate_id"] == sid

    at.session_state["active_slate_id"] = 999_999
    at = at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["active_slate_id"] == sid


# ---------------------------------------------------------------------------
# Blocker clarity — one "Next required fix" line from builder_gate_view
# ---------------------------------------------------------------------------


def test_build_panel_shows_single_next_required_fix(isolated_db):
    """The Build panel surfaces one concise 'Next required fix' line, taken
    verbatim from the gate's own ``recommend_next_action`` (no re-derivation)."""
    sid = _seed_groups_no_odds()  # blocked: odds_unmatched_active
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    assert "Next required fix" in blob, blob

    conn = get_connection()
    try:
        apply_schema(conn)
        why = hd.builder_gate_view(
            evaluate_manual_review(conn, sid), has_slates=True
        ).next_action.why
    finally:
        conn.close()
    assert why in blob, (why, blob)


def test_ready_slate_omits_next_required_fix(isolated_db):
    """A ready slate already says 'build lineups' in the status line, so the
    'Next required fix' line is not shown."""
    _seed_structurally_clean(reviewed=True)
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "Next required fix" not in _text_blob(at)


# ---------------------------------------------------------------------------
# Step 2 — explicit BestFightOdds live fetch → preview (Phase 2)
#
# Preview-only: an explicit, user-triggered GET parsed by the pure Phase 1
# parser and rendered for review. No fetch on page load, no DB write, no
# recompute, no override change. The page calls
# ``bestfightodds_fetch.fetch_bestfightodds_preview`` via the module object, so
# these tests monkeypatch that attribute and never touch the real network.
# ---------------------------------------------------------------------------


def _fake_fetch_result() -> BestFightOddsFetchResult:
    rows = [
        AcquiredMoneylineRow(
            fighter_name="Test Fighter One",
            american_moneyline=-350,
            source_url=_BFO_URL,
            fetched_at="2026-06-01T12:00:00Z",
        ),
        AcquiredMoneylineRow(
            fighter_name="Test Fighter Two",
            american_moneyline=280,
            source_url=_BFO_URL,
            fetched_at="2026-06-01T12:00:00Z",
        ),
    ]
    return BestFightOddsFetchResult(
        rows=rows, source_url=_BFO_URL, fetched_at="2026-06-01T12:00:00Z"
    )


def _set_bfo_url(at: AppTest, url: str = _BFO_URL) -> AppTest:
    at.text_input(key=_BFO_URL_KEY).set_value(url)
    return at.run()


def test_bfo_fetch_control_renders_disabled_until_url(isolated_db):
    """The BestFightOdds fetch control renders in Step 2 with the Fetch button
    disabled until a URL is entered — and nothing is fetched on load."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    keys = [b.key for b in at.button]
    assert _BFO_FETCH_BTN in keys, keys
    assert _BFO_URL_KEY in [t.key for t in at.text_input]
    # Empty URL → disabled (so a load / stray run can never fetch).
    assert _button_by_key(at, _BFO_FETCH_BTN).disabled is True


def test_bfo_no_fetch_on_page_load(isolated_db, monkeypatch):
    """Page load must never call the fetch helper — the GET lives strictly in
    the button handler (design §1.9 / §1.11; docs/DEVELOPMENT_NOTES.md §3 no page-load fetch)."""

    def _boom(*args, **kwargs):
        raise AssertionError("BestFightOdds fetch must not run on page load")

    monkeypatch.setattr(
        bfo_fetch_mod, "fetch_bestfightodds_preview", _boom
    )
    # A ready slate (the richest load path) still must not fetch on load.
    _seed_structurally_clean(reviewed=True)
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    # No preview table rendered on load and the DB is untouched.
    assert len(at.dataframe) == 0
    assert _db_snapshot() == before, "fetch path must not write on load"


def test_bfo_fetch_click_renders_preview_without_writing(isolated_db, monkeypatch):
    """An explicit Fetch click renders a read-only DraftKings preview table
    (fighter / moneyline / source / book) and writes nothing to the DB."""
    captured: list[str] = []

    def _fake(url, **kwargs):
        captured.append(url)
        return _fake_fetch_result()

    monkeypatch.setattr(bfo_fetch_mod, "fetch_bestfightodds_preview", _fake)

    sid = _seed_groups_no_odds()
    before = _db_snapshot()
    at = _open_page()
    at = _set_bfo_url(at)
    fetch_btn = _button_by_key(at, _BFO_FETCH_BTN)
    assert fetch_btn.disabled is False
    at = fetch_btn.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # The helper was called once with the entered URL.
    assert captured == [_BFO_URL], captured
    # A preview table rendered with the normalized rows.
    assert len(at.dataframe) == 1, [d.value.to_dict() for d in at.dataframe]
    df = at.dataframe[0].value
    assert list(df.columns) == ["Fighter", "Moneyline", "Source", "Book"]
    assert list(df["Fighter"]) == ["Test Fighter One", "Test Fighter Two"]
    assert list(df["Moneyline"]) == [-350, 280]
    assert set(df["Book"]) == {"DraftKings"}
    # Preview-only banner + provenance caption, and the DB is untouched.
    assert any(
        "preview only, nothing saved" in s.value for s in at.success
    ), [s.value for s in at.success]
    assert any("Not saved to the slate" in c.value for c in at.caption)
    assert _odds_rows(sid) == []
    assert _db_snapshot() == before, "fetch preview must not write (§1.9)"


def test_bfo_fetch_failure_shows_error(isolated_db, monkeypatch):
    """A fetch failure surfaces a user-facing error and renders no preview /
    no write (graceful failure — design Phase 2)."""

    def _fail(url, **kwargs):
        raise BestFightOddsFetchError("connection refused")

    monkeypatch.setattr(bfo_fetch_mod, "fetch_bestfightodds_preview", _fail)

    sid = _seed_groups_no_odds()
    before = _db_snapshot()
    at = _open_page()
    at = _set_bfo_url(at)
    at = _button_by_key(at, _BFO_FETCH_BTN).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    errors = " ".join(e.value for e in at.error)
    assert "Could not fetch BestFightOdds" in errors, errors
    assert len(at.dataframe) == 0
    assert _odds_rows(sid) == []
    assert _db_snapshot() == before, "failed fetch must not write"


def test_bfo_parse_failure_shows_distinct_error(isolated_db, monkeypatch):
    """A parse failure (page fetched but no DraftKings odds) is surfaced with a
    distinct message from a fetch failure, still preview-only / no write."""

    def _fail(url, **kwargs):
        raise BestFightOddsParseError("no DraftKings column")

    monkeypatch.setattr(bfo_fetch_mod, "fetch_bestfightodds_preview", _fail)

    sid = _seed_groups_no_odds()
    before = _db_snapshot()
    at = _open_page()
    at = _set_bfo_url(at)
    at = _button_by_key(at, _BFO_FETCH_BTN).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    errors = " ".join(e.value for e in at.error)
    assert "could not parse" in errors.lower(), errors
    assert len(at.dataframe) == 0
    assert _odds_rows(sid) == []
    assert _db_snapshot() == before, "failed parse must not write"


# ---------------------------------------------------------------------------
# Step 2 — odds-method framing: lead with the DraftKings paste path
#
# UX reframe (copy / order / layout only — no logic, parser, save, recompute,
# schema, gate, or projection change). Step 2 must guide a first-run user to the
# working path (DraftKings paste) and present the BestFightOdds fetch as
# optional / advanced. These tests pin: (1) the guidance line, (2) the
# recommended / not-saved-yet labels, (3) the render order (DK paste →
# BestFightOdds), (4) the DK paste expander opening by default only when the
# slate has no odds, and (5) that opening it by default still performs no parse
# / no write on load.
# ---------------------------------------------------------------------------

_DK_PASTE_LABEL = "Paste DraftKings odds board (recommended)"
_BFO_LABEL = "Fetch from BestFightOdds (preview only — not saved yet)"
_STEP2_GUIDANCE = (
    "Easiest way to add odds: paste the DraftKings board below. BestFightOdds "
    "fetch is optional."
)


def _ordered_texts(at: AppTest) -> list[str]:
    """Pre-order DFS over the rendered main tree, returning each node's label
    or value in document order. Lets a test assert *relative* ordering across
    element types (expander labels vs. markdown bodies)."""
    out: list[str] = []

    def _walk(node) -> None:
        label = getattr(node, "label", None)
        if isinstance(label, str) and label:
            out.append(label)
        value = getattr(node, "value", None)
        if isinstance(value, str) and value:
            out.append(value)
        children = getattr(node, "children", None)
        if isinstance(children, dict):
            children = list(children.values())
        for child in children or []:
            _walk(child)

    _walk(at.main)
    return out


def _first_index(texts: list[str], needle: str) -> int:
    for i, t in enumerate(texts):
        if needle in t:
            return i
    raise AssertionError(f"{needle!r} not found in rendered text order: {texts}")


def _expander_by_label(at: AppTest, needle: str):
    matched = [e for e in at.expander if needle in (e.label or "")]
    assert len(matched) == 1, (
        f"Expected exactly one expander matching {needle!r}; "
        f"saw labels: {[e.label for e in at.expander]}"
    )
    return matched[0]


def test_step2_guidance_line_renders(isolated_db):
    """Step 2 shows the short guidance line steering first-run users to the
    DraftKings paste path (copy only — derives nothing, writes nothing)."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _STEP2_GUIDANCE in _text_blob(at), _text_blob(at)


def test_dk_paste_label_marked_recommended(isolated_db):
    """The DraftKings paste expander is labelled the recommended path."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    labels = [e.label for e in at.expander]
    assert any("recommended" in (lbl or "") for lbl in labels), labels
    assert _DK_PASTE_LABEL in labels, labels


def test_bfo_label_marked_not_saved_yet(isolated_db):
    """The BestFightOdds expander label states it is preview-only / not saved
    yet (so it never reads as a save path)."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    labels = [e.label for e in at.expander]
    assert any("not saved yet" in (lbl or "") for lbl in labels), labels
    assert _BFO_LABEL in labels, labels


def test_step2_method_order_dk_paste_then_bfo(isolated_db):
    """Step 2 renders the DraftKings paste section first, then BestFightOdds
    (requirement: lead with the working path)."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    texts = _ordered_texts(at)
    dk_idx = _first_index(texts, _DK_PASTE_LABEL)
    bfo_idx = _first_index(texts, _BFO_LABEL)
    assert dk_idx < bfo_idx, (
        f"expected DK paste < BFO, got {dk_idx} / {bfo_idx}"
    )
    # The guidance line precedes both method controls.
    assert _first_index(texts, _STEP2_GUIDANCE) < dk_idx


def test_dk_paste_expander_open_by_default_when_no_odds(isolated_db):
    """With zero odds rows the DraftKings paste expander opens by default so a
    first-run user lands on the working path (presentation only)."""
    _seed_groups_no_odds()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _expander_by_label(at, _DK_PASTE_LABEL).proto.expanded is True


def test_dk_paste_expander_collapsed_when_odds_exist(isolated_db):
    """When the slate already has odds rows the DraftKings paste expander is
    collapsed by default (the user has already used the path)."""
    sid = _seed_structurally_clean(reviewed=False)
    assert len(_odds_rows(sid)) == 12  # precondition: odds present
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _expander_by_label(at, _DK_PASTE_LABEL).proto.expanded is False


def test_dk_paste_open_by_default_still_no_parse_or_write_on_load(
    isolated_db, monkeypatch
):
    """Opening the DraftKings paste expander by default (zero-odds slate) is
    presentation only: the parser is never called and nothing is written on
    page load (regression guard for the reframe; docs/DEVELOPMENT_NOTES.md §11)."""
    import src.ingestion.providers.draftkings_paste as dk_mod

    def _boom(*args, **kwargs):  # pragma: no cover - only on regression
        raise AssertionError("DK paste parser must not run on page load")

    monkeypatch.setattr(dk_mod, "parse_draftkings_paste", _boom)

    sid = _seed_groups_no_odds()
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    # Expander open, but no preview produced and no write performed on load.
    assert _expander_by_label(at, _DK_PASTE_LABEL).proto.expanded is True
    assert _DK_PASTE_PREVIEW_SESSION_KEY not in at.session_state
    assert len(at.dataframe) == 0
    assert _odds_rows(sid) == []
    assert _db_snapshot() == before, "open-by-default must not write on load"


# ---------------------------------------------------------------------------
# Step 2 — inline unresolved odds name-match fixer (UX repair)
#
# A user must be able to resolve a sportsbook-vs-DK name mismatch inline on
# Build (odds say "Bruno Gustavo da Silva", the DK salary lists "Bruno Silva")
# without visiting 03 Odds. The fixer reuses the D.5 services verbatim
# (assignable_match_results + record_assign_match_override); these tests pin it
# appears only for unmatched / review_required rows, writes nothing on page
# load, an explicit per-row Assign writes the override (force_pair) through the
# existing service, and the resolved row drops off after the assign. The 03
# Odds advanced workflow is untouched (covered by test_odds_page_assign_action).
# ---------------------------------------------------------------------------

_FIX_TITLE = "Fix odds name matches"


def _seed_fix_match_result(conn, *, slate_id, odds_row, **kwargs):
    """Persist one ``odds_match_results`` row for ``odds_row``.

    Defaults model an ``unmatched`` row (no binding); callers override
    match_status / effective_status / fighter_id / preferred_candidate for the
    review_required and auto_match cases. Mirrors the Odds-page assign test's
    seed helper so both surfaces seed identically."""
    defaults = dict(
        fighter_id=None,
        match_status="unmatched",
        effective_status="unmatched",
        match_stage="none",
        match_score=0,
        preferred_candidate=None,
        opponent_check="not_applicable",
        candidates=(),
        notes=("seeded for Build fixer AppTest",),
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


def _seed_unmatched_odds_slate(
    *,
    event: str = "UFC Fixer",
    dk_name: str = "Bruno Silva",
    book_name: str = "Bruno Gustavo da Silva",
    opponent: str = "Joe Pyfer",
) -> dict:
    """A slate with one active DK fighter and one ``unmatched`` odds row whose
    sportsbook name differs from the DK salary name (the real smoke case).
    Returns the ids the fixer widgets key on."""
    conn = get_connection()
    try:
        apply_schema(conn)
        slate = SlateRepository(conn).create(
            event_name=event,
            salary_csv_status="validated",
            salary_row_count=1,
        )
        fighter_id = _insert_fighter(
            conn, slate_id=slate.id, name=dk_name, salary=7600
        )
        row = OddsRowRepository(conn).create(
            slate_id=slate.id,
            fighter_name_raw=book_name,
            opponent_name_raw=opponent,
            american_odds=-150,
            source="manual",
            captured_at="2026-05-20T00:00:00Z",
        )
        _seed_fix_match_result(conn, slate_id=slate.id, odds_row=row)
    finally:
        conn.close()
    return {
        "slate_id": slate.id,
        "fighter_id": fighter_id,
        "odds_row_id": row.id,
        "odds_row_key": row.odds_row_key,
        "dk_name": dk_name,
        "book_name": book_name,
    }


def _seed_auto_matched_odds_slate(*, event: str = "UFC Clean Fix") -> int:
    """A slate whose single odds row already auto-matched — no assignable rows,
    so the inline fixer must not render."""
    conn = get_connection()
    try:
        apply_schema(conn)
        slate = SlateRepository(conn).create(
            event_name=event,
            salary_csv_status="validated",
            salary_row_count=1,
        )
        fighter_id = _insert_fighter(
            conn, slate_id=slate.id, name="Jon Jones", salary=9500
        )
        row = OddsRowRepository(conn).create(
            slate_id=slate.id,
            fighter_name_raw="Jon Jones",
            american_odds=-300,
            source="manual",
            captured_at="2026-05-20T00:00:00Z",
        )
        _seed_fix_match_result(
            conn,
            slate_id=slate.id,
            odds_row=row,
            fighter_id=fighter_id,
            match_status="auto_match",
            effective_status="auto_match",
            match_stage="exact_conservative",
            match_score=100,
        )
        return slate.id
    finally:
        conn.close()


def _fix_fighter_key(state: dict) -> str:
    return f"builder_odds_fix_fighter_{state['slate_id']}_{state['odds_row_id']}"


def _fix_assign_key(state: dict) -> str:
    return f"builder_odds_fix_assign_{state['slate_id']}_{state['odds_row_id']}"


def test_odds_fixer_appears_for_unmatched_row(isolated_db):
    """An ``unmatched`` odds row whose sportsbook name differs from the DK
    salary name surfaces the inline 'Fix odds name matches' resolver — its
    title, the plain copy, the sportsbook name, a fighter dropdown, and a
    per-row Assign button."""
    state = _seed_unmatched_odds_slate()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    assert _FIX_TITLE in blob, blob
    assert "do not exactly match" in blob, blob
    # The sportsbook raw name (the thing that did not match) is shown.
    assert state["book_name"] in blob, blob
    assert _fix_assign_key(state) in [b.key for b in at.button]
    assert _fix_fighter_key(state) in [s.key for s in at.selectbox]
    # The row's status reads in plain words (no jargon up top); the internal
    # mechanics are confined to a collapsed "Technical details" expander.
    assert "No DK fighter matched yet" in blob, blob
    tech = [e for e in at.expander if "Technical details" in (e.label or "")]
    assert len(tech) == 1, [e.label for e in at.expander]
    assert tech[0].proto.expanded is False
    # The non-expander caption copy carries no internal jargon.
    fixer_caption = next(
        (c.value for c in at.caption if "do not exactly match" in c.value),
        "",
    )
    assert "effective_status" not in fixer_caption
    assert "force_pair" not in fixer_caption


def test_odds_fixer_preselects_suggested_fighter_for_unmatched_row(isolated_db):
    """An unmatched row pre-selects the closest DK name as a suggestion (so the
    user confirms one pick instead of guessing from a blank dropdown), and says
    so — but still requires the explicit Assign click to write (#2)."""
    state = _seed_unmatched_odds_slate()  # book "Bruno Gustavo da Silva" → DK "Bruno Silva"
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    blob = _text_blob(at)
    # The suggestion is surfaced and names the closest DK fighter.
    assert "Suggested match" in blob, blob
    assert state["dk_name"] in blob, blob
    # The dropdown is pre-selected to the suggested fighter (not the sentinel),
    # so a single Assign confirms it. No write has happened yet.
    sel = at.selectbox(key=_fix_fighter_key(state))
    assert sel.value == state["fighter_id"], sel.value
    assert _odds_rows(state["slate_id"]) != []  # the seeded odds row exists
    # Nothing bound yet — the suggestion is a default, not a write.
    assert _active_overrides(state["slate_id"]) == []


def test_odds_fixer_hidden_when_all_resolved(isolated_db):
    """A slate whose odds all auto-matched shows no fixer — no title and no
    Assign button (the resolver is for unmatched / review_required only)."""
    _seed_auto_matched_odds_slate()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _FIX_TITLE not in _text_blob(at)
    assert not [
        b
        for b in at.button
        if b.key and b.key.startswith("builder_odds_fix_assign_")
    ]


def test_odds_fixer_no_write_on_page_load(isolated_db):
    """Rendering the fixer is read-only: loading a slate with an assignable row
    writes no override and leaves the match result unmatched (docs/DEVELOPMENT_NOTES.md §11)."""
    state = _seed_unmatched_odds_slate()
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _db_snapshot() == before, "fixer render must not write on load"
    assert _active_overrides(state["slate_id"]) == []
    [match] = _match_results(state["slate_id"])
    assert match.effective_status == "unmatched"
    assert match.fighter_id is None


def test_odds_fixer_assign_unmatched_writes_force_pair(isolated_db):
    """Picking the DK fighter and clicking Assign on an ``unmatched`` row writes
    a ``force_pair`` override through the existing service and flips the result
    row's effective_status + fighter_id — surfacing the plain success line."""
    state = _seed_unmatched_odds_slate()
    at = _open_page()
    # Unmatched → sentinel default; an explicit fighter pick is required.
    at.selectbox(key=_fix_fighter_key(state)).set_value(
        state["fighter_id"]
    ).run()
    at = _button_by_key(at, _fix_assign_key(state)).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert [e.value for e in at.error] == [], [e.value for e in at.error]

    # Plain success copy: "Assigned <book name> to <DK name>."
    successes = " ".join(s.value for s in at.success)
    assert (
        f"Assigned {state['book_name']} to {state['dk_name']}." in successes
    ), successes

    active = _active_overrides(state["slate_id"])
    assert len(active) == 1
    ov = active[0]
    assert ov.override_type == "force_pair"
    assert ov.odds_row_key == state["odds_row_key"]
    assert ov.fighter_id == state["fighter_id"]

    [match] = _match_results(state["slate_id"])
    assert match.effective_status == "force_pair"
    assert match.fighter_id == state["fighter_id"]


# ---------------------------------------------------------------------------
# Step 1 — read-only Game Info suggested pairings (UX punch-list #1 / slice 1a)
# ---------------------------------------------------------------------------


def _seed_slate_with_game_info(
    fighters: list[tuple[str, str | None]], *, event: str = "UFC GameInfo"
) -> int:
    """A validated slate carrying active fighters with explicit Game Info
    strings — the state the read-only suggested-pairings block reads."""
    conn = get_connection()
    try:
        apply_schema(conn)
        slate = SlateRepository(conn).create(
            event_name=event,
            salary_csv_status="validated",
            salary_row_count=len(fighters),
        )
        for i, (name, game_info) in enumerate(fighters):
            conn.execute(
                "INSERT INTO fighters "
                "(slate_id, name, salary, status, game_info) "
                "VALUES (?, ?, ?, 'active', ?)",
                (slate.id, name, 8000 + i, game_info),
            )
        conn.commit()
        return slate.id
    finally:
        conn.close()


def test_build_page_shows_suggested_pairings_and_main_event(isolated_db):
    """Step 1 renders the DK Game Info pairings read-only and flags the
    latest-starting bout as the auto-detected 5-round main event."""
    slate_id = _seed_slate_with_game_info(
        [
            ("Early A", "Early A@Early B 05/22/2026 06:00PM ET"),
            ("Early B", "Early A@Early B 05/22/2026 06:00PM ET"),
            ("Main C", "Main C@Main D 05/22/2026 10:00PM ET"),
            ("Main D", "Main C@Main D 05/22/2026 10:00PM ET"),
        ]
    )
    at = _open_page()
    at = _select_slate(at, slate_id)
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "Early A vs Early B" in blob, blob
    assert "Main C vs Main D" in blob, blob
    assert "2 DK Game Info pairing(s) detected" in blob, blob
    # The latest-starting bout is named as the auto-detected main event.
    assert "Main event · 5 rounds" in blob, blob
    assert "Auto-detected main event (latest start → 5 rounds):" in blob, blob
    # Slice 1b: the read-only list now carries an explicit Apply control (the
    # ready pairs are not yet grouped). Rendering it still writes nothing — the
    # dedicated page-load-write test below pins that.
    assert "apply them below" in blob, blob
    assert _APPLY_GI_BTN_KEY in [b.key for b in at.button], [
        b.key for b in at.button
    ]


def test_build_page_pairings_no_main_event_when_times_absent(isolated_db):
    """With no parseable start times the pairing list still renders read-only,
    but no bout is auto-flagged as the 5-round main event."""
    slate_id = _seed_slate_with_game_info(
        [
            ("Plain A", "Plain A@Plain B"),
            ("Plain B", "Plain A@Plain B"),
            ("Plain C", "Plain C@Plain D"),
            ("Plain D", "Plain C@Plain D"),
        ]
    )
    at = _open_page()
    at = _select_slate(at, slate_id)
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "Plain A vs Plain B" in blob, blob
    assert "Plain C vs Plain D" in blob, blob
    assert "Main event · 5 rounds" not in blob, blob
    assert "No main event auto-detected" in blob, blob


def test_build_page_no_pairings_message_when_game_info_blank(isolated_db):
    """A slate whose active fighters carry no Game Info shows the empty-state
    note rather than a pairing list."""
    slate_id = _seed_slate_with_game_info(
        [("Lonely A", None), ("Lonely B", None)]
    )
    at = _open_page()
    at = _select_slate(at, slate_id)
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "No DK Game Info pairings detected yet" in blob, blob


def test_odds_fixer_row_drops_off_after_assign(isolated_db):
    """After the only unresolved row is assigned, the fixer no longer lists it:
    the Assign control is gone and the Step 2 status refreshes to 0 need
    review."""
    state = _seed_unmatched_odds_slate()
    at = _open_page()
    # Sanity: one row needs review before the assign.
    assert "1 need review" in _text_blob(at)

    at.selectbox(key=_fix_fighter_key(state)).set_value(
        state["fighter_id"]
    ).run()
    at = _button_by_key(at, _fix_assign_key(state)).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # The resolved row's Assign control is gone, and the status refreshed.
    assert _fix_assign_key(state) not in [b.key for b in at.button]
    assert "0 need review" in _text_blob(at)


# ---------------------------------------------------------------------------
# Step 1 — DK Game Info Apply button (slice 1b, FIGHT_GROUP_APPLY_SERVICE_DESIGN
# §5). The button drives the shared ``apply_game_info_pairings`` service: it
# creates unconfirmed fight groups, auto-sets the latest-starting bout to 5
# rounds, is idempotent, and writes nothing on page load.
# ---------------------------------------------------------------------------

_GI_EARLY = "Early A@Early B 05/22/2026 06:00PM ET"
_GI_MAIN = "Main C@Main D 05/22/2026 10:00PM ET"


def _seed_two_ready_bouts(event: str = "UFC Apply") -> int:
    """Validated slate with two complete, ungrouped DK Game Info bouts — the
    Early bout (6:00 PM) and the Main bout (10:00 PM, latest → main event)."""
    return _seed_slate_with_game_info(
        [
            ("Early A", _GI_EARLY),
            ("Early B", _GI_EARLY),
            ("Main C", _GI_MAIN),
            ("Main D", _GI_MAIN),
        ],
        event=event,
    )


def _fight_groups(slate_id: int) -> list[tuple]:
    conn = get_connection()
    try:
        apply_schema(conn)
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT fighter_1_name, fighter_2_name, scheduled_rounds, status "
                "FROM fight_groups WHERE slate_id = ? ORDER BY id",
                (int(slate_id),),
            ).fetchall()
        ]
    finally:
        conn.close()


def _create_group(slate_id: int, f1: str, f2: str, *, rounds: int = 3) -> None:
    conn = get_connection()
    try:
        apply_schema(conn)
        FightGroupRepository(conn).create(
            slate_id=int(slate_id),
            fighter_1_name=f1,
            fighter_2_name=f2,
            scheduled_rounds=rounds,
        )
    finally:
        conn.close()


def test_apply_gi_button_present_when_pairs_ready(isolated_db):
    slate_id = _seed_two_ready_bouts()
    at = _open_page()
    at = _select_slate(at, slate_id)
    assert not at.exception, [str(e.value) for e in at.exception]
    # Read-only suggestion list still renders alongside the new control.
    assert "2 DK Game Info pairing(s) detected" in _text_blob(at)
    assert _APPLY_GI_BTN_KEY in [b.key for b in at.button]


def test_apply_gi_button_absent_without_game_info(isolated_db):
    # Salary imported (active fighters) but no Game Info → no suggestions.
    _seed_salary_no_groups()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _APPLY_GI_BTN_KEY not in [b.key for b in at.button]
    assert "No DK Game Info pairings detected yet" in _text_blob(at)


def test_apply_gi_button_writes_nothing_on_load(isolated_db):
    slate_id = _seed_two_ready_bouts()
    before = _db_snapshot()
    at = _open_page()
    at = _select_slate(at, slate_id)
    assert not at.exception, [str(e.value) for e in at.exception]
    # The button renders, yet rendering created no groups and changed no table.
    assert _APPLY_GI_BTN_KEY in [b.key for b in at.button]
    assert _fight_groups(slate_id) == []
    assert _db_snapshot() == before


def test_apply_gi_creates_unconfirmed_groups_and_refreshes(isolated_db):
    slate_id = _seed_two_ready_bouts()
    at = _open_page()
    at = _select_slate(at, slate_id)
    assert _fight_groups(slate_id) == []  # nothing before the click

    at = _button_by_key(at, _APPLY_GI_BTN_KEY).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = _fight_groups(slate_id)
    # Early bout (6 PM) is a standard 3-round bout; the Main bout (10 PM, latest
    # start) is auto-detected as the main event and created at 5 rounds.
    assert ("Early A", "Early B", 3, "unconfirmed") in rows
    assert ("Main C", "Main D", 5, "unconfirmed") in rows
    assert len(rows) == 2

    success = " ".join(s.value for s in at.success)
    assert "Applied 2 new fight group(s) from DK Game Info" in success
    info = " ".join(i.value for i in at.info)
    assert "Main C vs Main D" in info and "5 rounds" in info, info

    # Same-run refresh (the on_click callback ran before the body): the Step 1
    # card now shows 2 fights, and the button is replaced by the all-applied
    # note (so a second click cannot duplicate).
    blob = _text_blob(at)
    assert '<div class="tsb-v">2</div><div class="tsb-l">Fights</div>' in blob
    assert _APPLY_GI_BTN_KEY not in [b.key for b in at.button]
    assert "All DK Game Info pairings are already applied" in blob


def test_apply_gi_all_three_rounds_and_warns_without_main_event(isolated_db):
    slate_id = _seed_slate_with_game_info(
        [
            ("Plain A", "Plain A@Plain B"),
            ("Plain B", "Plain A@Plain B"),
            ("Plain C", "Plain C@Plain D"),
            ("Plain D", "Plain C@Plain D"),
        ]
    )
    at = _open_page()
    at = _select_slate(at, slate_id)
    at = _button_by_key(at, _APPLY_GI_BTN_KEY).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = _fight_groups(slate_id)
    assert len(rows) == 2
    assert all(r[2] == 3 for r in rows), rows  # every group is 3 rounds
    warnings = " ".join(w.value for w in at.warning)
    assert "all new groups were created at 3 rounds" in warnings, warnings


def test_apply_gi_idempotent_on_recompute(isolated_db):
    slate_id = _seed_two_ready_bouts()
    at = _open_page()
    at = _select_slate(at, slate_id)
    at = _button_by_key(at, _APPLY_GI_BTN_KEY).click().run()
    assert len(_fight_groups(slate_id)) == 2

    # Re-render under the now-applied state: still two groups, no button.
    at = at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(_fight_groups(slate_id)) == 2
    assert _APPLY_GI_BTN_KEY not in [b.key for b in at.button]


def test_apply_gi_button_absent_when_all_already_applied(isolated_db):
    slate_id = _seed_two_ready_bouts()
    # Pre-create both bouts (as a prior apply / Fight Groups would).
    _create_group(slate_id, "Early A", "Early B", rounds=3)
    _create_group(slate_id, "Main C", "Main D", rounds=5)

    at = _open_page()
    at = _select_slate(at, slate_id)
    assert not at.exception, [str(e.value) for e in at.exception]

    # No new work to do: button replaced by the compact all-applied note, but
    # the read-only suggestion list still renders.
    assert _APPLY_GI_BTN_KEY not in [b.key for b in at.button]
    blob = _text_blob(at)
    assert "All DK Game Info pairings are already applied" in blob
    assert "2 DK Game Info pairing(s) detected" in blob


def test_apply_gi_skips_already_grouped_and_creates_rest(isolated_db):
    slate_id = _seed_two_ready_bouts()
    # Early A is already grouped (with an off-roster name), so the Early
    # suggestion is skipped; the Main bout is still applied.
    _create_group(slate_id, "Early A", "Z Ghost", rounds=3)

    at = _open_page()
    at = _select_slate(at, slate_id)
    assert _APPLY_GI_BTN_KEY in [b.key for b in at.button]
    at = _button_by_key(at, _APPLY_GI_BTN_KEY).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = _fight_groups(slate_id)
    # The pre-existing group is untouched; the Main bout is created (5 rounds,
    # latest start); no second Early A group is created.
    assert ("Early A", "Z Ghost", 3, "unconfirmed") in rows
    assert ("Main C", "Main D", 5, "unconfirmed") in rows
    assert not any(r[0] == "Early A" and r[1] == "Early B" for r in rows)
    assert len(rows) == 2

    warnings = " ".join(w.value for w in at.warning)
    assert "Early A" in warnings and "already grouped" in warnings, warnings


# ---------------------------------------------------------------------------
# Multi-book consensus → preview + save (ODDS_CONSENSUS_DESIGN §5.5 / §8)
# ---------------------------------------------------------------------------

# A confident fight: two fighters, three books each (book_count 3 >= MIN_BOOKS).
_CONSENSUS_PASTE_OK = (
    "Matchup\tDraftKings\tFanDuel\tBetMGM\n"
    "Matt Schnell\t-150\t-160\t-155\n"
    "Alessandro Costa\t+130\t+140\t+135"
)
# A low-confidence fight: one book each (book_count 1 < MIN_BOOKS).
_CONSENSUS_PASTE_LOW = (
    "Matchup\tDraftKings\n"
    "Lonnie Low\t-150\n"
    "Cory Conf\t+130"
)


def _preview_consensus(at: AppTest, *, paste=None, url=None) -> AppTest:
    if paste is not None:
        at.text_area(key=_CONSENSUS_PASTE_KEY).set_value(paste)
    if url is not None:
        at.text_input(key=_CONSENSUS_URL_KEY).set_value(url)
    at = at.run()
    return _button_by_key(at, _CONSENSUS_PREVIEW_BTN_KEY).click().run()


def _has_consensus_df(at: AppTest) -> bool:
    return any(
        "Median no-vig %" in list(d.value.columns) for d in at.dataframe
    )


def test_consensus_blend_notes_bfo_excludes_dk_betmgm(isolated_db):
    """Step 2's consensus area carries the caveat that BestFightOdds static HTML
    may omit DraftKings / BetMGM (they load client-side), pointing the user at
    the DraftKings paste path to include DK in the blend. Copy-only — no behavior
    change; pinned against the rendered caption text (docs/DEVELOPMENT_NOTES.md §11)."""
    _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    at = _open_page()

    captions = " ".join(c.value for c in at.caption)
    assert (
        "BestFightOdds static HTML may exclude DraftKings and BetMGM "
        "because those books load client-side. Use the DraftKings paste "
        "option if you want DK included in the consensus blend."
    ) in captions
    assert not at.exception


def test_consensus_preview_writes_nothing(isolated_db):
    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    at = _open_page()
    before = _db_snapshot()

    at = _preview_consensus(at, paste=_CONSENSUS_PASTE_OK)

    assert not at.exception
    # Preview renders the blended table but persists nothing.
    assert _has_consensus_df(at), "consensus preview table should render"
    assert _odds_rows(sid) == []
    assert _book_lines(sid) == []
    assert _match_results(sid) == []
    assert _db_snapshot() == before, "preview must not write"


def test_consensus_save_persists_consensus_rows(isolated_db):
    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    at = _open_page()
    at = _preview_consensus(at, paste=_CONSENSUS_PASTE_OK)

    at = _button_by_key(at, _CONSENSUS_SAVE_BTN_KEY).click().run()

    consensus = [r for r in _odds_rows(sid) if r.source == "consensus"]
    assert len(consensus) == 2
    assert all(r.bookmaker == "consensus" for r in consensus)
    assert {r.fighter_name_raw for r in consensus} == {
        "Matt Schnell",
        "Alessandro Costa",
    }
    # Provenance: 2 fighters x 3 books, source token lowercase.
    book_lines = _book_lines(sid)
    assert len(book_lines) == 6
    assert {bl.source for bl in book_lines} == {"paste"}


def test_consensus_save_recomputes_and_refreshes_status(isolated_db):
    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    at = _open_page()
    at = _preview_consensus(at, paste=_CONSENSUS_PASTE_OK)

    at = _button_by_key(at, _CONSENSUS_SAVE_BTN_KEY).click().run()

    assert len(_match_results(sid)) == 2
    successes = " ".join(s.value for s in at.success)
    assert "Recomputed match results" in successes
    # In-place Step 2 status refresh (same interaction).
    blob = _text_blob(at)
    assert "Odds loaded:** 2 odds rows" in blob
    assert "2 matched" in blob


def test_consensus_save_is_idempotent(isolated_db):
    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    at = _open_page()
    at = _preview_consensus(at, paste=_CONSENSUS_PASTE_OK)

    at = _button_by_key(at, _CONSENSUS_SAVE_BTN_KEY).click().run()
    # Re-saving the same preview is the idempotent last-write — no duplicates.
    at = _button_by_key(at, _CONSENSUS_SAVE_BTN_KEY).click().run()

    consensus = [r for r in _odds_rows(sid) if r.source == "consensus"]
    assert len(consensus) == 2
    assert len(_book_lines(sid)) == 6


def test_consensus_low_confidence_kept_as_provenance(isolated_db):
    sid = _seed_slate_with_active_fighters(["Lonnie Low", "Cory Conf"])
    at = _open_page()
    at = _preview_consensus(at, paste=_CONSENSUS_PASTE_LOW)

    # The preview surfaces the low-confidence fight, never silently drops it.
    assert "Low-confidence" in _text_blob(at)

    at = _button_by_key(at, _CONSENSUS_SAVE_BTN_KEY).click().run()

    # No consensus odds row for a sub-MIN_BOOKS fight, but provenance is kept.
    assert [r for r in _odds_rows(sid) if r.source == "consensus"] == []
    assert len(_book_lines(sid)) == 2
    warnings = " ".join(w.value for w in at.warning)
    assert "fewer than the minimum books" in warnings


def test_consensus_save_failure_shows_error(isolated_db, monkeypatch):
    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    at = _open_page()
    at = _preview_consensus(at, paste=_CONSENSUS_PASTE_OK)

    def _boom(*a, **k):
        raise RuntimeError("synthetic consensus save failure")

    monkeypatch.setattr(consensus_save_mod, "save_consensus_to_slate", _boom)
    before = _db_snapshot()

    at = _button_by_key(at, _CONSENSUS_SAVE_BTN_KEY).click().run()

    assert not at.exception
    errors = " ".join(e.value for e in at.error)
    assert "Could not save consensus odds" in errors
    assert _db_snapshot() == before


def test_consensus_no_fetch_on_page_load(isolated_db, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("BestFightOdds must not be fetched on page load")

    monkeypatch.setattr(
        bfo_fetch_mod, "fetch_bestfightodds_all_books_preview", _boom
    )
    _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])
    before = _db_snapshot()

    at = _open_page()

    assert not at.exception
    assert not _has_consensus_df(at)
    assert _db_snapshot() == before


def test_consensus_save_does_not_write_to_wrong_slate(isolated_db):
    other = _seed_slate_with_active_fighters(
        ["Other One", "Other Two"], event="UFC Other"
    )
    sid = _seed_slate_with_active_fighters(
        ["Matt Schnell", "Alessandro Costa"], event="UFC Active"
    )
    at = _open_page()
    at = _select_slate(at, sid)
    at = _preview_consensus(at, paste=_CONSENSUS_PASTE_OK)

    at = _button_by_key(at, _CONSENSUS_SAVE_BTN_KEY).click().run()

    assert len([r for r in _odds_rows(sid) if r.source == "consensus"]) == 2
    assert _odds_rows(other) == []
    assert _book_lines(other) == []
    assert _match_results(other) == []


def test_consensus_url_preview_fetches_all_books(isolated_db, monkeypatch):
    sid = _seed_slate_with_active_fighters(["Matt Schnell", "Alessandro Costa"])

    def _fake_fetch(url, **kwargs):
        rows = [
            AllBooksFighterRow(
                fighter_name="Matt Schnell",
                opponent="Alessandro Costa",
                book_lines=(
                    BookLine("DraftKings", -150),
                    BookLine("FanDuel", -160),
                    BookLine("BetMGM", -155),
                ),
            ),
            AllBooksFighterRow(
                fighter_name="Alessandro Costa",
                opponent="Matt Schnell",
                book_lines=(
                    BookLine("DraftKings", 130),
                    BookLine("FanDuel", 140),
                    BookLine("BetMGM", 135),
                ),
            ),
        ]
        return bfo_fetch_mod.BestFightOddsAllBooksFetchResult(
            rows=rows, source_url=url, fetched_at="2026-06-05T00:00:00Z"
        )

    monkeypatch.setattr(
        bfo_fetch_mod, "fetch_bestfightodds_all_books_preview", _fake_fetch
    )
    at = _open_page()
    before = _db_snapshot()

    at = _preview_consensus(
        at, url="https://www.bestfightodds.com/events/test-event-1"
    )

    assert not at.exception
    assert _has_consensus_df(at), "URL fetch should produce a consensus preview"
    # Still preview-only — the fetch writes nothing.
    assert _odds_rows(sid) == []
    assert _db_snapshot() == before
