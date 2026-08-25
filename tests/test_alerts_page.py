"""AppTest coverage for the Mismatch Alerts v1 Phase C preview page.

Loads ``app/pages/05_alerts.py`` via ``streamlit.testing.v1.AppTest``
against an isolated temp SQLite DB and pins the read-only contract per
``docs/MISMATCH_ALERTS_V1_DESIGN.md`` §10 / §13 and ``docs/DEVELOPMENT_NOTES.md`` §11:

  - Empty DB → "No slates yet" info + no alert table.
  - Slate with no fighters → "No alerts for this slate." empty state.
  - Lonely fighter (no fight group) → at least one warn row plus the
    slate-scoped ``fight_group_issue`` warn (design §15 risk #5).
  - Underdog-value fixture → at least one ``info`` row whose code,
    scope, and fighter_name render in the table.
  - Banner text covers the read-only / effective_status / no-downstream
    / Phase D contracts.
  - Summary caption renders ``N alert(s) — X warn · Y info``.
  - Alert rows render in the deterministic order emitted by
    ``evaluate_alerts``.
  - Re-rendering the page does not mutate any persisted state.
  - The page exposes no write affordances (no buttons, no forms).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.alerts.alert_service import evaluate_alerts
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
ALERTS_PAGE = REPO_ROOT / "app" / "pages" / "05_alerts.py"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "alerts_page.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _open_page() -> AppTest:
    at = AppTest.from_file(str(ALERTS_PAGE), default_timeout=30)
    at.run()
    return at


def _seed_slate(name: str = "UFC 999") -> int:
    conn = get_connection()
    try:
        apply_schema(conn)
        return SlateRepository(conn).create(event_name=name).id
    finally:
        conn.close()


def _insert_fighter(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    name: str,
    salary: int = 8000,
    status: str = "active",
) -> int:
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, ?)",
        (int(slate_id), name, int(salary), status),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_underdog_value_fight(slate_id: int) -> None:
    """Cheap favorite fixture that triggers §3.3 underdog_value."""
    conn = get_connection()
    try:
        apply_schema(conn)
        _insert_fighter(
            conn, slate_id=slate_id, name="Cheap Champ", salary=7000
        )
        _insert_fighter(
            conn, slate_id=slate_id, name="Pricey Dog", salary=9500
        )
        FightGroupRepository(conn).create(
            slate_id=slate_id,
            fighter_1_name="Cheap Champ",
            fighter_2_name="Pricey Dog",
            scheduled_rounds=3,
        )
        OddsRowRepository(conn).create(
            slate_id=slate_id,
            fighter_name_raw="Cheap Champ",
            american_odds=-200,
            source="manual",
            captured_at="2026-05-20T00:00:00Z",
        )
        recompute_and_replace_match_results(conn, slate_id)
    finally:
        conn.close()


def _db_snapshot() -> dict[str, list[tuple]]:
    conn = get_connection()
    try:
        apply_schema(conn)
        tables = (
            "slates",
            "fighters",
            "fight_groups",
            "odds_rows",
            "odds_match_results",
            "manual_match_overrides",
            "projections",
        )
        snap: dict[str, list[tuple]] = {}
        for table in tables:
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            snap[table] = [tuple(r) for r in rows]
        return snap
    finally:
        conn.close()


def _alerts_df(at: AppTest):
    assert len(at.dataframe) == 1, (
        f"Expected exactly one dataframe on the page; got {len(at.dataframe)}"
    )
    return at.dataframe[0].value


# ---------------------------------------------------------------------------
# Banner / structural states
# ---------------------------------------------------------------------------


def test_empty_db_shows_no_slates_info_and_no_table(isolated_db):
    at = _open_page()

    assert not at.exception, [str(e.value) for e in at.exception]
    assert [e.value for e in at.error] == []

    infos = [i.value for i in at.info]
    assert any("No slates yet" in msg for msg in infos), infos
    assert len(at.dataframe) == 0
    assert at.selectbox == []


def test_banner_pins_phase_c_contract_text(isolated_db):
    _seed_slate()

    at = _open_page()
    warnings = [w.value for w in at.warning]
    assert warnings, "Expected the Phase C banner warning to render"
    banner = "\n".join(warnings)
    assert "Read-only" in banner
    assert "No alerts are persisted" in banner
    assert "effective_status" in banner
    assert "optimizer" in banner
    assert "exports" in banner
    assert "Phase D" in banner


def test_slate_without_fighters_shows_empty_alerts_message(isolated_db):
    _seed_slate()

    at = _open_page()
    assert not at.exception

    infos = [i.value for i in at.info]
    assert any("No alerts for this slate." in msg for msg in infos), infos
    assert len(at.dataframe) == 0


# ---------------------------------------------------------------------------
# Alert rendering
# ---------------------------------------------------------------------------


def test_lonely_fighter_renders_warn_rows_including_slate_scope(isolated_db):
    slate_id = _seed_slate()
    conn = get_connection()
    try:
        apply_schema(conn)
        _insert_fighter(
            conn, slate_id=slate_id, name="Lonely Fighter", salary=8500
        )
    finally:
        conn.close()

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    df = _alerts_df(at)
    codes = list(df["Code"])
    assert "projection_non_projectable" in codes
    assert "fight_group_issue" in codes

    slate_row = df.loc[df["Code"] == "fight_group_issue"].iloc[0]
    assert slate_row["Scope"] == "slate"
    assert slate_row["Fighter"] == "—"
    assert slate_row["Severity"] == "warn"


def test_underdog_value_fixture_renders_info_row(isolated_db):
    slate_id = _seed_slate()
    _seed_underdog_value_fight(slate_id)

    at = _open_page()
    df = _alerts_df(at)

    underdog = df.loc[df["Code"] == "underdog_value"]
    assert len(underdog) >= 1
    row = underdog.iloc[0]
    assert row["Severity"] == "info"
    assert row["Scope"] == "fighter"
    assert row["Fighter"] == "Cheap Champ"
    assert "Cheap Champ" in row["Message"]


def test_summary_caption_renders_warn_and_info_counts(isolated_db):
    slate_id = _seed_slate()
    _seed_underdog_value_fight(slate_id)

    at = _open_page()
    captions = [c.value for c in at.caption]
    assert any(
        "alert(s)" in c and "warn" in c and "info" in c for c in captions
    ), captions


def test_rows_render_in_evaluate_alerts_order(isolated_db):
    slate_id = _seed_slate()
    _seed_underdog_value_fight(slate_id)

    conn = get_connection()
    try:
        apply_schema(conn)
        expected = [
            (a.severity, a.scope, a.code, a.fighter_name)
            for a in evaluate_alerts(conn, slate_id)
        ]
    finally:
        conn.close()

    at = _open_page()
    df = _alerts_df(at)
    rendered = [
        (
            r["Severity"],
            r["Scope"],
            r["Code"],
            None if r["Fighter"] == "—" else r["Fighter"],
        )
        for _, r in df.iterrows()
    ]
    assert rendered == expected


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


def test_repeated_render_does_not_mutate_db(isolated_db):
    slate_id = _seed_slate()
    _seed_underdog_value_fight(slate_id)

    before = _db_snapshot()
    _open_page()
    _open_page()
    after = _db_snapshot()

    assert before == after, "Page render must not mutate persisted state"


def test_page_exposes_no_write_affordances(isolated_db):
    slate_id = _seed_slate()
    _seed_underdog_value_fight(slate_id)

    at = _open_page()
    assert list(at.button) == [], (
        "Phase C is read-only; no buttons may be rendered. "
        f"Saw button keys: {[b.key for b in at.button]}"
    )
