"""AppTest coverage for the Fighter Status v1 Phase D Streamlit page.

Loads ``app/pages/04_fighter_status.py`` via ``streamlit.testing.v1.AppTest``
against an isolated temp SQLite DB and pins
``docs/FIGHTER_STATUS_V1_DESIGN.md`` §14 / §15 Phase D plus the
``docs/DEVELOPMENT_NOTES.md`` §11 write-action contract:

  - Empty DB → "No slates yet" info, no write affordances.
  - Slate with no fighters → empty state info, no write affordances.
  - Banner covers the local-only / no-downstream / no
    ``effective_status`` / Phase E contracts (design §2, §6, §7, §8).
  - Fighter table renders one row per fighter with the §14 columns.
  - Summary caption renders the §14 active / warning / blocking line.
  - Page load does NOT mutate persisted state, even with rows present.
  - Clicking Set manual status writes via
    ``FighterRepository.set_manual_status`` (warning + blocking).
  - Clicking Clear manual status writes via
    ``FighterRepository.clear_manual_status``.
  - No buttons / forms target projections, alerts, Manual Review, the
    optimizer, exports, or anything beyond the manual override write
    surface.
  - No ``odds_match_results`` / ``manual_match_overrides`` rows are
    touched by any Fighter Status write (design §8).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import get_connection
from src.db.repositories import (
    FighterRepository,
    ManualMatchOverrideRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.dk_salary_importer import ParsedSalaryRow
from src.slate import fighter_status as fs

REPO_ROOT = Path(__file__).resolve().parent.parent
FIGHTER_STATUS_PAGE = REPO_ROOT / "app" / "pages" / "04_fighter_status.py"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "fighter_status_page.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _open_page() -> AppTest:
    at = AppTest.from_file(str(FIGHTER_STATUS_PAGE), default_timeout=30)
    at.run()
    return at


def _seed_slate(name: str = "UFC 777") -> int:
    conn = get_connection()
    try:
        apply_schema(conn)
        return SlateRepository(conn).create(event_name=name).id
    finally:
        conn.close()


def _parsed(name: str, salary: int, *, source_row_number: int = 1) -> ParsedSalaryRow:
    return ParsedSalaryRow(
        fighter_name=name,
        salary=salary,
        roster_position="F",
        game_info="Jon Doe@Jane Roe 05/22/2026",
        source_row_number=source_row_number,
    )


def _seed_fighters(slate_id: int, names_salaries: list[tuple[str, int]]) -> dict[str, int]:
    conn = get_connection()
    try:
        apply_schema(conn)
        FighterRepository(conn).upsert_for_slate(
            slate_id=slate_id,
            parsed_rows=[
                _parsed(name, salary, source_row_number=i + 1)
                for i, (name, salary) in enumerate(names_salaries)
            ],
        )
        return {
            r.name: r.id
            for r in FighterRepository(conn).list_for_slate(slate_id)
        }
    finally:
        conn.close()


def _read_manual(slate_id: int, fighter_id: int) -> tuple[str | None, str | None]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT manual_status, manual_status_set_at "
            "FROM fighters WHERE id = ? AND slate_id = ?",
            (int(fighter_id), int(slate_id)),
        ).fetchone()
        return (row[0], row[1])
    finally:
        conn.close()


def _snapshot_db(slate_id: int) -> dict:
    conn = get_connection()
    try:
        return {
            "fighters": conn.execute(
                "SELECT id, slate_id, name, salary, status, "
                "       manual_status, manual_status_set_at "
                "FROM fighters ORDER BY id"
            ).fetchall(),
            "odds_match_results": conn.execute(
                "SELECT COUNT(*) FROM odds_match_results"
            ).fetchone()[0],
            "manual_match_overrides": conn.execute(
                "SELECT COUNT(*) FROM manual_match_overrides"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def _button(at: AppTest, key: str):
    matched = [b for b in at.button if b.key == key]
    assert len(matched) == 1, (
        f"Expected exactly one button with key {key!r}; "
        f"saw button keys: {[b.key for b in at.button]}"
    )
    return matched[0]


def _selectbox(at: AppTest, key: str):
    matched = [s for s in at.selectbox if s.key == key]
    assert len(matched) == 1, (
        f"Expected exactly one selectbox with key {key!r}; "
        f"saw selectbox keys: {[s.key for s in at.selectbox]}"
    )
    return matched[0]


# ---------------------------------------------------------------------------
# Empty states + banner
# ---------------------------------------------------------------------------


def test_empty_db_shows_no_slates_info_and_no_write_affordances(isolated_db):
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    infos = [i.value for i in at.info]
    assert any("No slates yet" in s for s in infos), infos

    # No write buttons / write selectboxes ever appear without a slate + rows.
    assert [b.key for b in at.button] == []
    assert [s.key for s in at.selectbox] == []


def test_slate_with_no_fighters_shows_empty_state(isolated_db):
    _seed_slate()
    at = _open_page()
    assert not at.exception

    infos = [i.value for i in at.info]
    assert any(
        "No fighters on this slate yet" in s for s in infos
    ), infos

    # Only the slate selector is present — no write affordances.
    assert [b.key for b in at.button] == []
    selectbox_keys = [s.key for s in at.selectbox]
    assert selectbox_keys == ["fighter_status_slate_id"]


def test_banner_describes_local_only_and_no_downstream_contract(isolated_db):
    _seed_slate()
    at = _open_page()
    assert not at.exception

    warnings = " ".join(w.value for w in at.warning)
    for fragment in (
        "manual",
        "manual fighter-status overrides",
        "does NOT change odds_match_results.effective_status",
        "does NOT yet feed projections, alerts, the Manual Review gate, "
        "the optimizer, exports",
        "Phase E manual real-feed smoke",
    ):
        assert fragment in warnings, (
            f"Expected banner to mention {fragment!r}; got: {warnings!r}"
        )


# ---------------------------------------------------------------------------
# Table + summary
# ---------------------------------------------------------------------------


def test_status_table_renders_one_row_per_fighter_with_expected_columns(
    isolated_db,
):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("Alpha Fighter", 9000), ("Bravo Fighter", 8500)])

    at = _open_page()
    assert not at.exception

    # The page renders a single dataframe with the §14 columns.
    assert len(at.dataframe) == 1
    df = at.dataframe[0].value
    assert list(df.columns) == [
        "Fighter",
        "Salary",
        "Importer/Base Status",
        "Manual Override",
        "Manual Override Set At",
        "Resolved Status",
        "Category",
    ]
    assert list(df["Fighter"]) == ["Alpha Fighter", "Bravo Fighter"]
    assert list(df["Salary"]) == [9000, 8500]
    assert list(df["Importer/Base Status"]) == ["active", "active"]
    assert list(df["Manual Override"]) == ["—", "—"]
    assert list(df["Manual Override Set At"]) == ["—", "—"]
    assert list(df["Resolved Status"]) == ["active", "active"]
    assert list(df["Category"]) == ["active", "active"]


def test_summary_caption_renders_category_counts(isolated_db):
    slate_id = _seed_slate()
    ids = _seed_fighters(
        slate_id,
        [
            ("A Fighter", 9000),
            ("B Fighter", 8500),
            ("C Fighter", 8000),
            ("D Fighter", 7500),
        ],
    )
    # Pre-seed a warning and a blocking override via the repository so the
    # caption renders 2 active · 1 warning · 1 blocking on first paint.
    conn = get_connection()
    try:
        repo = FighterRepository(conn)
        repo.set_manual_status(
            slate_id=slate_id, fighter_id=ids["B Fighter"], status=fs.QUESTIONABLE
        )
        repo.set_manual_status(
            slate_id=slate_id, fighter_id=ids["C Fighter"], status=fs.OUT
        )
    finally:
        conn.close()

    at = _open_page()
    assert not at.exception

    captions = [c.value for c in at.caption]
    assert any(
        "4 fighter(s)" in c and "2 active · 1 warning · 1 blocking" in c
        for c in captions
    ), captions


# ---------------------------------------------------------------------------
# Page-load read-only contract
# ---------------------------------------------------------------------------


def test_page_load_does_not_mutate_db(isolated_db):
    slate_id = _seed_slate()
    ids = _seed_fighters(slate_id, [("Alpha Fighter", 9000), ("Bravo Fighter", 8500)])
    conn = get_connection()
    try:
        FighterRepository(conn).set_manual_status(
            slate_id=slate_id, fighter_id=ids["Alpha Fighter"], status=fs.OUT
        )
    finally:
        conn.close()

    before = _snapshot_db(slate_id)
    at = _open_page()
    assert not at.exception
    after = _snapshot_db(slate_id)
    assert before == after


# ---------------------------------------------------------------------------
# Explicit set / clear write actions
# ---------------------------------------------------------------------------


def _drive_to_rows(slate_id: int) -> AppTest:
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    _selectbox(at, "fighter_status_slate_id").set_value(slate_id)
    return at.run()


def _select_fighter(at: AppTest, key: str, fighter_id: int) -> AppTest:
    _selectbox(at, key).set_value(fighter_id)
    return at.run()


def _select_status(at: AppTest, status: str) -> AppTest:
    _selectbox(at, "fighter_status_set_status_value").set_value(status)
    return at.run()


def test_set_warning_status_persists_via_repository(isolated_db):
    slate_id = _seed_slate()
    ids = _seed_fighters(slate_id, [("Alpha Fighter", 9000)])
    fid = ids["Alpha Fighter"]

    at = _drive_to_rows(slate_id)
    at = _select_fighter(at, "fighter_status_set_fighter_id", fid)
    at = _select_status(at, fs.QUESTIONABLE)
    at = _button(at, "fighter_status_set_btn").click().run()

    assert not at.exception, [str(e.value) for e in at.exception]
    manual, set_at = _read_manual(slate_id, fid)
    assert manual == "questionable"
    assert set_at is not None and set_at != ""


def test_set_blocking_status_persists_via_repository(isolated_db):
    slate_id = _seed_slate()
    ids = _seed_fighters(slate_id, [("Alpha Fighter", 9000)])
    fid = ids["Alpha Fighter"]

    at = _drive_to_rows(slate_id)
    at = _select_fighter(at, "fighter_status_set_fighter_id", fid)
    at = _select_status(at, fs.OUT)
    at = _button(at, "fighter_status_set_btn").click().run()

    assert not at.exception
    manual, _ = _read_manual(slate_id, fid)
    assert manual == "out"


def test_clear_manual_status_resets_columns_to_null(isolated_db):
    slate_id = _seed_slate()
    ids = _seed_fighters(slate_id, [("Alpha Fighter", 9000)])
    fid = ids["Alpha Fighter"]

    conn = get_connection()
    try:
        FighterRepository(conn).set_manual_status(
            slate_id=slate_id, fighter_id=fid, status=fs.OUT
        )
    finally:
        conn.close()
    assert _read_manual(slate_id, fid)[0] == "out"

    at = _drive_to_rows(slate_id)
    at = _select_fighter(at, "fighter_status_clear_fighter_id", fid)
    at = _button(at, "fighter_status_clear_btn").click().run()

    assert not at.exception
    assert _read_manual(slate_id, fid) == (None, None)


# ---------------------------------------------------------------------------
# Disjoint from odds-match override layer (design §8)
# ---------------------------------------------------------------------------


def test_set_and_clear_do_not_touch_odds_or_overrides(isolated_db):
    slate_id = _seed_slate()
    ids = _seed_fighters(slate_id, [("Alpha Fighter", 9000)])
    fid = ids["Alpha Fighter"]

    # Seed an odds row + reject override so any silent mutation by the page
    # would be visible.
    conn = get_connection()
    try:
        odds = OddsRowRepository(conn).create(
            slate_id=slate_id,
            fighter_name_raw="Alpha Fighter",
            american_odds=-150,
            source="manual",
            captured_at="2026-05-22T12:00:00Z",
        )
        ManualMatchOverrideRepository(conn).add_override(
            slate_id=slate_id,
            override_type="reject_match",
            odds_row_key=odds.odds_row_key,
            reason="AppTest: seeded before fighter status write",
        )
        odds_before = OddsRowRepository(conn).list_for_slate(slate_id)
        overrides_before = (
            ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id)
        )
        match_count_before = conn.execute(
            "SELECT COUNT(*) FROM odds_match_results WHERE slate_id = ?",
            (slate_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    # Set then clear via the page.
    at = _drive_to_rows(slate_id)
    at = _select_fighter(at, "fighter_status_set_fighter_id", fid)
    at = _select_status(at, fs.OUT)
    at = _button(at, "fighter_status_set_btn").click().run()
    assert not at.exception

    at = _drive_to_rows(slate_id)
    at = _select_fighter(at, "fighter_status_clear_fighter_id", fid)
    at = _button(at, "fighter_status_clear_btn").click().run()
    assert not at.exception

    conn = get_connection()
    try:
        assert OddsRowRepository(conn).list_for_slate(slate_id) == odds_before
        assert (
            ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id)
            == overrides_before
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM odds_match_results WHERE slate_id = ?",
                (slate_id,),
            ).fetchone()[0]
            == match_count_before
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Surface limits — no projections / alerts / manual-review / optimizer /
# export affordances on this page.
# ---------------------------------------------------------------------------


def test_page_exposes_only_fighter_status_write_buttons(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("Alpha Fighter", 9000)])

    at = _drive_to_rows(slate_id)
    button_keys = sorted(b.key for b in at.button)
    assert button_keys == sorted(
        ["fighter_status_set_btn", "fighter_status_clear_btn"]
    )

    selectbox_keys = sorted(s.key for s in at.selectbox)
    assert selectbox_keys == sorted(
        [
            "fighter_status_slate_id",
            "fighter_status_set_fighter_id",
            "fighter_status_set_status_value",
            "fighter_status_clear_fighter_id",
        ]
    )


def test_page_source_does_not_reference_effective_status_as_data_source():
    """Pin design §2 / §8: this page must not read or write
    ``odds_match_results.effective_status``. Banner text mentioning the
    column name is allowed (it explicitly tells the user the column is
    untouched), but the page must never SELECT or UPDATE it."""
    source = FIGHTER_STATUS_PAGE.read_text()
    # No SQL against the odds-match tables from the page itself.
    assert "FROM odds_match_results" not in source
    assert "UPDATE odds_match_results" not in source
    assert "FROM manual_match_overrides" not in source
    assert "UPDATE manual_match_overrides" not in source
    # Banner-only mention permitted; pin the banner intent so a future
    # regression that turns this into a data lookup trips.
    assert "odds_match_results.effective_status" in source
    assert "does NOT" in source


def test_page_source_uses_repository_write_methods_not_direct_sql():
    """Per ``docs/DEVELOPMENT_NOTES.md`` §11 the page must go through the repository
    layer for writes. Pin by source inspection: the only write surfaces
    are ``FighterRepository.set_manual_status`` /
    ``clear_manual_status``, and no ``UPDATE fighters`` SQL appears in
    the page module."""
    source = FIGHTER_STATUS_PAGE.read_text()
    assert "FighterRepository(conn).set_manual_status" in source
    assert "FighterRepository(conn).clear_manual_status" in source
    # No direct fighter mutation SQL.
    assert "UPDATE fighters" not in source
    assert "INSERT INTO fighters" not in source
    assert "DELETE FROM fighters" not in source
