"""AppTest coverage for the Slate Setup salary-import UI action (Slice D).

Loads ``app/pages/01_slate_setup.py`` via ``streamlit.testing.v1.AppTest``
against an isolated temp SQLite DB and exercises the explicit
``Import salaries into slate`` button. Realizes
``docs/SALARY_PERSISTENCE_DESIGN.md`` §9 Phase D / Phase E:

  - Page load does **not** write fighters, even with a saved slate present.
  - A validated CSV is required before salaries persist; uploading +
    validating alone does not write.
  - Clicking the import button persists fighters and surfaces the
    parsed/inserted/updated/unchanged/deactivated counts.
  - A repeat click against the same CSV is idempotent (unchanged-only).
  - Structural validation failure (missing required column) shows a clear
    error and writes no fighters.
  - Row-level parse failure (non-integer salary) shows a clear error and
    writes no fighters.
  - ``odds_match_results`` and ``manual_match_overrides`` are not touched
    by the import path (design §8).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import get_connection
from src.db.repositories import (
    FighterRepository,
    ManualMatchOverrideRepository,
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.dk_salary_importer import REQUIRED_COLUMNS

REPO_ROOT = Path(__file__).resolve().parent.parent
SLATE_SETUP_PAGE = REPO_ROOT / "app" / "pages" / "01_slate_setup.py"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point the app's ``get_connection()`` at a per-test SQLite file."""
    db_path = tmp_path / "slate_setup_import.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _valid_csv_bytes(rows: list[tuple[str, int]]) -> bytes:
    header = ",".join(REQUIRED_COLUMNS)
    lines = [header]
    for i, (name, salary) in enumerate(rows, start=1):
        lines.append(
            f"F,{name},{i},{salary},Jon Doe@Jane Roe 05/22/2026,ABC"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _missing_salary_csv_bytes() -> bytes:
    # Drop the "Salary" column entirely → structural validation must fail.
    return (
        "Position,Name,ID,Game Info,TeamAbbrev\n"
        "F,Jon Doe,1,Jon Doe@Jane Roe 05/22/2026,JDO\n"
    ).encode("utf-8")


def _non_integer_salary_csv_bytes() -> bytes:
    header = ",".join(REQUIRED_COLUMNS)
    return (
        f"{header}\n"
        "F,Jon Doe,1,nine thousand,Jon Doe@Jane Roe 05/22/2026,JDO\n"
    ).encode("utf-8")


def _open_page() -> AppTest:
    at = AppTest.from_file(str(SLATE_SETUP_PAGE), default_timeout=30)
    at.run()
    return at


def _button(at: AppTest, key: str):
    matched = [b for b in at.button if b.key == key]
    assert len(matched) == 1, (
        f"Expected exactly one button with key {key!r}; "
        f"saw button keys: {[b.key for b in at.button]}"
    )
    return matched[0]


def _upload(at: AppTest, content: bytes) -> AppTest:
    at.file_uploader[0].upload("dk_salary.csv", content, "text/csv")
    return at.run()


def _set_event_name(at: AppTest, name: str) -> AppTest:
    at.text_input(key="event_name").set_value(name)
    return at.run()


def _seed_slate(name: str = "UFC 999") -> int:
    conn = get_connection()
    try:
        apply_schema(conn)
        return SlateRepository(conn).create(event_name=name).id
    finally:
        conn.close()


def _list_fighters(slate_id: int):
    conn = get_connection()
    try:
        return FighterRepository(conn).list_for_slate(slate_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Page load + no-write guarantees
# ---------------------------------------------------------------------------


def test_page_load_with_empty_db_does_not_write(isolated_db):
    at = _open_page()

    assert not at.exception, (
        "Page raised on first render: "
        f"{[str(e.value) for e in at.exception]}"
    )
    assert [e.value for e in at.error] == []

    # Both write actions exist but must be disabled with no upload + no slate.
    assert _button(at, "create_slate_btn").disabled is True
    assert _button(at, "import_salaries_btn").disabled is True

    conn = get_connection()
    try:
        apply_schema(conn)
        assert SlateRepository(conn).list_all() == []
        assert conn.execute("SELECT COUNT(*) FROM fighters").fetchone()[0] == 0
    finally:
        conn.close()


def test_page_load_with_existing_slate_does_not_write_fighters(isolated_db):
    """Opening the page when a slate already exists must not implicitly
    import salaries for it — Slice D requires an explicit button click."""
    slate_id = _seed_slate()

    at = _open_page()
    assert not at.exception

    assert _list_fighters(slate_id) == []


def test_validated_upload_alone_does_not_write_fighters(isolated_db):
    """Validation success must not trigger a write. Only the import button
    click is allowed to persist fighters."""
    slate_id = _seed_slate()

    at = _open_page()
    _upload(at, _valid_csv_bytes([("Jon Doe", 9000), ("Jane Roe", 8500)]))

    # CSV is validated, but no fighters were written.
    assert any(
        "Valid DK UFC Classic salary CSV" in s.value for s in at.success
    ), [s.value for s in at.success]
    assert _list_fighters(slate_id) == []


# ---------------------------------------------------------------------------
# Explicit import click — happy path + idempotence
# ---------------------------------------------------------------------------


def _drive_to_import_ready(at: AppTest, csv: bytes, slate_id: int) -> AppTest:
    """Upload a CSV, ensure the selectbox targets ``slate_id``, and return
    an AppTest poised on the (enabled) import button."""
    _upload(at, csv)
    # The selectbox renders only after a slate exists; default index 0 is
    # the most recent, but to be deterministic across orderings select
    # explicitly by id.
    sb = at.selectbox(key="import_target_slate_id")
    sb.set_value(slate_id)
    return at.run()


def test_import_button_click_persists_fighters_with_counts(isolated_db):
    slate_id = _seed_slate()
    at = _open_page()
    at = _drive_to_import_ready(
        at,
        _valid_csv_bytes([("Jon Doe", 9000), ("Jane Roe", 8500)]),
        slate_id,
    )

    import_btn = _button(at, "import_salaries_btn")
    assert import_btn.disabled is False, (
        "Import button should be enabled once a slate is selected and the "
        "uploaded CSV passes validation"
    )

    at = import_btn.click().run()

    assert not at.exception, [str(e.value) for e in at.exception]
    assert [e.value for e in at.error] == []

    successes = [s.value for s in at.success]
    success = next(
        (s for s in successes if "Imported salaries into slate" in s),
        None,
    )
    assert success is not None, (
        f"Expected import success banner; got success messages: {successes}"
    )
    # Counts surface every field of SalaryImportResult / FighterUpsertResult.
    for fragment in (
        "parsed 2",
        "inserted 2",
        "updated 0",
        "unchanged 0",
        "deactivated 0",
    ):
        assert fragment in success, (
            f"Expected fragment {fragment!r} in success banner; got: {success!r}"
        )

    fighters = _list_fighters(slate_id)
    assert {f.name for f in fighters} == {"Jon Doe", "Jane Roe"}
    assert all(f.status == "active" for f in fighters)
    assert {f.name: f.salary for f in fighters} == {
        "Jon Doe": 9000,
        "Jane Roe": 8500,
    }


def test_repeat_import_is_idempotent_and_reports_unchanged(isolated_db):
    slate_id = _seed_slate()
    csv = _valid_csv_bytes([("Jon Doe", 9000), ("Jane Roe", 8500)])

    at = _open_page()
    at = _drive_to_import_ready(at, csv, slate_id)
    at = _button(at, "import_salaries_btn").click().run()
    snapshot = [
        (f.id, f.name, f.salary, f.status) for f in _list_fighters(slate_id)
    ]
    assert len(snapshot) == 2

    # Second click against the same CSV — must be a no-op apart from counts.
    at = _button(at, "import_salaries_btn").click().run()
    assert not at.exception
    assert [e.value for e in at.error] == []

    success = next(
        (
            s.value
            for s in at.success
            if "Imported salaries into slate" in s.value
            and "unchanged 2" in s.value
        ),
        None,
    )
    assert success is not None, (
        "Expected a second-import success banner with unchanged 2; "
        f"got: {[s.value for s in at.success]}"
    )
    assert "inserted 0" in success
    assert "updated 0" in success
    assert "deactivated 0" in success

    # No row id churn, no status flip, no duplicate rows.
    assert [
        (f.id, f.name, f.salary, f.status) for f in _list_fighters(slate_id)
    ] == snapshot


# ---------------------------------------------------------------------------
# Game Info persistence + suggested-pairing feedback (design §5)
# ---------------------------------------------------------------------------


def _info_messages(at: AppTest) -> list[str]:
    # st.info / st.warning both surface as alerts; collect their text.
    return [a.value for a in at.info] + [a.value for a in at.warning]


def test_import_reports_game_info_capture_and_pairing_count(isolated_db):
    """Both rows of the valid CSV share a byte-identical Game Info string, so
    after import the page reports 2 of 2 active fighters captured and 1
    suggested DK pairing, pointing the user to the Fight Groups page."""
    slate_id = _seed_slate()
    at = _open_page()
    at = _drive_to_import_ready(
        at,
        _valid_csv_bytes([("Jon Doe", 9000), ("Jane Roe", 8500)]),
        slate_id,
    )
    at = _button(at, "import_salaries_btn").click().run()

    assert not at.exception, [str(e.value) for e in at.exception]

    msgs = _info_messages(at)
    capture_msg = next(
        (m for m in msgs if "Game Info captured" in m), None
    )
    assert capture_msg is not None, (
        f"Expected a Game Info capture readout; got info/warning: {msgs}"
    )
    assert "Game Info captured: 2 of 2 active fighters" in capture_msg
    assert "Suggested DK pairings available: 1" in capture_msg
    assert "Fight Groups page" in capture_msg
    assert "5-round main event" in capture_msg


def test_import_with_blank_game_info_warns_no_pairings(isolated_db):
    """A CSV whose Game Info cells are blank persists fighters but yields no
    suggested pairings, and the page warns rather than pointing to apply."""
    slate_id = _seed_slate()
    header = ",".join(REQUIRED_COLUMNS)
    blank_csv = (
        f"{header}\n"
        "F,Jon Doe,1,9000,,ABC\n"
        "F,Jane Roe,2,8500,,ABC\n"
    ).encode("utf-8")

    at = _open_page()
    at = _drive_to_import_ready(at, blank_csv, slate_id)
    at = _button(at, "import_salaries_btn").click().run()

    assert not at.exception, [str(e.value) for e in at.exception]
    # Fighters were persisted...
    assert {f.name for f in _list_fighters(slate_id)} == {
        "Jon Doe",
        "Jane Roe",
    }
    # ...but the Game Info readout reports zero capture and zero pairings.
    msgs = _info_messages(at)
    warn = next(
        (m for m in msgs if "Game Info captured: 0 of 2" in m), None
    )
    assert warn is not None, (
        f"Expected a zero-capture warning; got info/warning: {msgs}"
    )
    assert "no DK pairings" in warn


# ---------------------------------------------------------------------------
# Failure paths — no fighters written
# ---------------------------------------------------------------------------


def test_validation_failure_blocks_import_and_writes_no_fighters(isolated_db):
    """Missing required column → structural validation fails. The import
    button must stay disabled and no fighters are written even if the
    user could otherwise reach the button."""
    slate_id = _seed_slate()
    at = _open_page()
    _upload(at, _missing_salary_csv_bytes())

    # Page surfaces the structural validation error.
    errors = [e.value for e in at.error]
    assert any("Salary" in e for e in errors), (
        f"Expected validation error mentioning the missing Salary column; "
        f"got errors: {errors}"
    )

    # Import button must be disabled because validation did not pass.
    assert _button(at, "import_salaries_btn").disabled is True

    # And, crucially, the page never wrote fighters as a side effect of
    # the upload + validation step.
    assert _list_fighters(slate_id) == []


def test_parse_failure_after_validation_shows_error_and_writes_nothing(
    isolated_db,
):
    """A CSV that passes structural validation but fails row-level parsing
    (non-integer salary) must surface a parse_failed error from the
    service and persist no fighters."""
    slate_id = _seed_slate()
    at = _open_page()
    at = _drive_to_import_ready(at, _non_integer_salary_csv_bytes(), slate_id)

    import_btn = _button(at, "import_salaries_btn")
    assert import_btn.disabled is False, (
        "Structural validation passes for non-integer salaries; the import "
        "button must therefore be reachable so the parse-failure branch "
        "surfaces to the user."
    )
    at = import_btn.click().run()

    assert not at.exception
    errors = [e.value for e in at.error]
    assert any(
        "row-level parsing failed" in e and "not an integer" in e
        for e in errors
    ), f"Expected parse-failure error; got: {errors}"

    # No success banner for the import, and no fighters persisted.
    assert not any(
        "Imported salaries into slate" in s.value for s in at.success
    )
    assert _list_fighters(slate_id) == []


# ---------------------------------------------------------------------------
# Import must not touch odds / overrides
# ---------------------------------------------------------------------------


def test_import_does_not_touch_odds_or_overrides(isolated_db):
    """Design §8: the salary import path must not rewrite
    ``odds_match_results`` or ``manual_match_overrides``."""
    slate_id = _seed_slate()

    # Seed one odds row + one active reject override so any silent
    # mutation by the import path would be visible.
    conn = get_connection()
    try:
        apply_schema(conn)
        odds_row = OddsRowRepository(conn).create(
            slate_id=slate_id,
            fighter_name_raw="Jon Doe",
            american_odds=-150,
            source="manual",
            captured_at="2026-05-22T12:00:00Z",
        )
        ManualMatchOverrideRepository(conn).add_override(
            slate_id=slate_id,
            override_type="reject_match",
            odds_row_key=odds_row.odds_row_key,
            reason="AppTest: seeded before salary import",
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

    at = _open_page()
    at = _drive_to_import_ready(
        at,
        _valid_csv_bytes([("Jon Doe", 9000), ("Jane Roe", 8500)]),
        slate_id,
    )
    at = _button(at, "import_salaries_btn").click().run()

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
    # And the fighters were persisted, confirming the import path itself ran.
    assert {f.name for f in _list_fighters(slate_id)} == {
        "Jon Doe",
        "Jane Roe",
    }
