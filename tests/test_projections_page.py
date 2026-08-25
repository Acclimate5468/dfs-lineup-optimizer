"""AppTest coverage for the Projection v1 Phase D preview page.

Loads ``app/pages/09_projections.py`` via ``streamlit.testing.v1.AppTest``
against an isolated temp SQLite DB and pins the read-only contract per
``docs/PROJECTION_V1_DESIGN.md`` §8 Phase D / §9 and ``docs/DEVELOPMENT_NOTES.md`` §11:

  - Empty DB → "No slates yet" info + no projection table.
  - Slate without fighters → friendly info + no projection table.
  - One ``ok`` fighter → numeric points and an empty missing-inputs cell.
  - Fighter missing odds → ``missing_inputs`` row with ``win_probability``
    tag and a ``—`` projection cell.
  - Fighter without a fight group → ``non_projectable`` with
    ``fight_group`` tag.
  - Multiple fighters render in Phase B (deterministic) order.
  - Re-rendering the page does not mutate any persisted state.
  - The page exposes no write affordances (no buttons, no forms).
"""

from __future__ import annotations

import sqlite3
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
from src.projections.projection_input_service import (
    aggregate_projection_inputs,
)
from src.projections.slate_projection_service import (
    PROJECTION_MODE_V0,
    PROJECTION_MODE_V2,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECTIONS_PAGE = REPO_ROOT / "app" / "pages" / "09_projections.py"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "projections_page.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _open_page() -> AppTest:
    at = AppTest.from_file(str(PROJECTIONS_PAGE), default_timeout=30)
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


def _seed_full_fight(slate_id: int) -> None:
    """One slate, one 3-round fight, one matched odds row for Aldo."""
    conn = get_connection()
    try:
        apply_schema(conn)
        _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
        _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
        FightGroupRepository(conn).create(
            slate_id=slate_id,
            fighter_1_name="Jose Aldo",
            fighter_2_name="Marlon Vera",
            scheduled_rounds=3,
        )
        OddsRowRepository(conn).create(
            slate_id=slate_id,
            fighter_name_raw="Jose Aldo",
            american_odds=-150,
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


def _projection_df(at: AppTest):
    assert len(at.dataframe) == 1, (
        f"Expected exactly one dataframe on the page; got {len(at.dataframe)}"
    )
    return at.dataframe[0].value


# ---------------------------------------------------------------------------
# Empty / structural states
# ---------------------------------------------------------------------------


def test_empty_db_shows_no_slates_info_and_no_table(isolated_db):
    at = _open_page()

    assert not at.exception, [str(e.value) for e in at.exception]
    assert [e.value for e in at.error] == []

    infos = [i.value for i in at.info]
    assert any("No slates yet" in msg for msg in infos), infos
    assert len(at.dataframe) == 0
    assert at.selectbox == []


def test_slate_without_fighters_shows_info_and_no_table(isolated_db):
    _seed_slate()

    at = _open_page()
    assert not at.exception

    infos = [i.value for i in at.info]
    assert any("No active fighters" in msg for msg in infos), infos
    assert len(at.dataframe) == 0


# ---------------------------------------------------------------------------
# Status rendering
# ---------------------------------------------------------------------------


def test_ok_fighter_renders_numeric_points_and_empty_missing(isolated_db):
    slate_id = _seed_slate()
    _seed_full_fight(slate_id)

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    df = _projection_df(at)
    aldo = df.loc[df["Fighter"] == "Jose Aldo"].iloc[0]
    assert aldo["Status"] == "ok"
    assert aldo["Projected DK pts"] != "—"
    # Render uses two-decimal format.
    assert float(aldo["Projected DK pts"]) > 0
    assert aldo["Missing inputs"] == ""


def test_fighter_missing_odds_renders_missing_inputs(isolated_db):
    slate_id = _seed_slate()
    conn = get_connection()
    try:
        apply_schema(conn)
        _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
        _insert_fighter(
            conn, slate_id=slate_id, name="Marlon Vera", salary=8200
        )
        FightGroupRepository(conn).create(
            slate_id=slate_id,
            fighter_1_name="Jose Aldo",
            fighter_2_name="Marlon Vera",
            scheduled_rounds=3,
        )
    finally:
        conn.close()

    at = _open_page()
    df = _projection_df(at)
    for _, row in df.iterrows():
        assert row["Status"] == "missing_inputs"
        assert row["Projected DK pts"] == "—"
        assert "win_probability" in row["Missing inputs"]


def test_fighter_without_fight_group_is_non_projectable(isolated_db):
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
    df = _projection_df(at)
    [row] = list(df.to_dict(orient="records"))
    assert row["Fighter"] == "Lonely Fighter"
    assert row["Status"] == "non_projectable"
    assert row["Projected DK pts"] == "—"
    assert "fight_group" in row["Missing inputs"]


# ---------------------------------------------------------------------------
# Ordering / summary / read-only invariant
# ---------------------------------------------------------------------------


def test_rows_render_in_phase_b_order(isolated_db):
    slate_id = _seed_slate()
    conn = get_connection()
    try:
        apply_schema(conn)
        for name in ("Charlie", "Alpha", "Bravo"):
            _insert_fighter(conn, slate_id=slate_id, name=name, salary=8000)
        phase_b_names = [
            b.fighter_name
            for b in aggregate_projection_inputs(conn, slate_id)
        ]
    finally:
        conn.close()

    at = _open_page()
    df = _projection_df(at)
    assert list(df["Fighter"]) == phase_b_names


def test_status_summary_caption_renders(isolated_db):
    slate_id = _seed_slate()
    _seed_full_fight(slate_id)

    at = _open_page()
    captions = [c.value for c in at.caption]
    assert any(
        "fighter(s)" in c and "ok" in c and "missing_inputs" in c
        and "non_projectable" in c
        for c in captions
    ), captions


def test_repeated_render_does_not_mutate_db(isolated_db):
    slate_id = _seed_slate()
    _seed_full_fight(slate_id)

    before = _db_snapshot()
    _open_page()
    _open_page()
    after = _db_snapshot()

    assert before == after, "Page render must not mutate persisted state"
    assert after["projections"] == []


def test_page_exposes_no_write_affordances(isolated_db):
    slate_id = _seed_slate()
    _seed_full_fight(slate_id)

    at = _open_page()
    assert list(at.button) == [], (
        "Phase D is read-only; no buttons may be rendered. "
        f"Saw button keys: {[b.key for b in at.button]}"
    )


# ---------------------------------------------------------------------------
# Projection-mode toggle (Phase D — PROJECTION_V2_METHOD_AWARE_DESIGN §15)
# ---------------------------------------------------------------------------


def _mode_radio(at: AppTest):
    assert len(at.radio) == 1, (
        f"Expected exactly one projection-mode radio; got {len(at.radio)}"
    )
    return at.radio[0]


def test_mode_defaults_to_v0_and_table_columns_unchanged(isolated_db):
    """Default mode is v0; the v0 view is materially unchanged (same columns,
    no experimental banner)."""
    slate_id = _seed_slate()
    _seed_full_fight(slate_id)

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert _mode_radio(at).value == PROJECTION_MODE_V0

    df = _projection_df(at)
    assert list(df.columns) == [
        "Fighter",
        "Status",
        "Projected DK pts",
        "Missing inputs",
        "Notes",
    ]
    assert "Mode" not in df.columns
    assert "Branches" not in df.columns

    warnings = [w.value for w in at.warning]
    assert not any("EXPERIMENTAL" in w for w in warnings), warnings


def test_v2_mode_renders_experimental_label_and_v2_columns(isolated_db):
    """Selecting v2 shows the Experimental banner and the additive v2-only
    columns with real (non-invented) values for an ``ok`` fighter."""
    slate_id = _seed_slate()
    _seed_full_fight(slate_id)

    at = _open_page()
    _mode_radio(at).set_value(PROJECTION_MODE_V2).run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # Selection persists on the widget but is session-only (not in the DB).
    assert _mode_radio(at).value == PROJECTION_MODE_V2

    warnings = [w.value for w in at.warning]
    assert any("EXPERIMENTAL" in w for w in warnings), warnings
    assert any("NOT promoted" in w for w in warnings), warnings

    df = _projection_df(at)
    # Pin the exact v2 column sequence (mirrors the v0-column assertion above) so
    # an accidental reorder, rename, or dropped v2 column is caught — not merely
    # membership.
    assert list(df.columns) == [
        "Fighter",
        "Status",
        "Mode",
        "Projected DK pts (experimental)",
        "Best branch pts",
        "Worst branch pts",
        "P(fight finishes)",
        "Finish share",
        "Branches",
        "Missing inputs",
        "Notes",
    ]

    aldo = df.loc[df["Fighter"] == "Jose Aldo"].iloc[0]
    assert aldo["Status"] == "ok"
    assert aldo["Mode"] == PROJECTION_MODE_V2
    assert aldo["Branches"] == "4"
    # Tier-0 league finish constant (0.57) echoed back verbatim.
    assert aldo["P(fight finishes)"] == "0.570"
    assert aldo["Projected DK pts (experimental)"] != "—"
    assert float(aldo["Projected DK pts (experimental)"]) > 0
    assert float(aldo["Best branch pts"]) > 0
    assert float(aldo["Worst branch pts"]) > 0
    assert 0.0 <= float(aldo["Finish share"]) <= 1.0


def test_v2_mode_preserves_non_projectable_without_inventing(isolated_db):
    """A non-projectable fighter keeps its status in v2 mode and shows no
    invented v2 values (v2 design §9 never-invent contract)."""
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
    _mode_radio(at).set_value(PROJECTION_MODE_V2).run()
    assert not at.exception, [str(e.value) for e in at.exception]

    df = _projection_df(at)
    [row] = list(df.to_dict(orient="records"))
    assert row["Fighter"] == "Lonely Fighter"
    assert row["Status"] == "non_projectable"
    assert row["Projected DK pts (experimental)"] == "—"
    assert row["Best branch pts"] == "—"
    assert row["Worst branch pts"] == "—"
    assert row["P(fight finishes)"] == "—"
    assert row["Finish share"] == "—"
    assert row["Branches"] == "—"
    assert "fight_group" in row["Missing inputs"]


def test_v2_mode_preserves_missing_inputs_without_inventing(isolated_db):
    """A structurally-projectable fighter with no odds is ``missing_inputs`` in
    v2 mode too — status preserved, no invented v2 values (v2 design §9)."""
    slate_id = _seed_slate()
    conn = get_connection()
    try:
        apply_schema(conn)
        _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
        _insert_fighter(
            conn, slate_id=slate_id, name="Marlon Vera", salary=8200
        )
        FightGroupRepository(conn).create(
            slate_id=slate_id,
            fighter_1_name="Jose Aldo",
            fighter_2_name="Marlon Vera",
            scheduled_rounds=3,
        )
    finally:
        conn.close()

    at = _open_page()
    _mode_radio(at).set_value(PROJECTION_MODE_V2).run()
    assert not at.exception, [str(e.value) for e in at.exception]

    df = _projection_df(at)
    for _, row in df.iterrows():
        assert row["Status"] == "missing_inputs"
        assert "win_probability" in row["Missing inputs"]
        assert row["Projected DK pts (experimental)"] == "—"
        assert row["Best branch pts"] == "—"
        assert row["Worst branch pts"] == "—"
        assert row["P(fight finishes)"] == "—"
        assert row["Finish share"] == "—"
        assert row["Branches"] == "—"


def test_changing_mode_does_not_mutate_db(isolated_db):
    """Toggling to the read-only v2 preview persists nothing (no projections
    table writes, no other mutation)."""
    slate_id = _seed_slate()
    _seed_full_fight(slate_id)

    before = _db_snapshot()
    at = _open_page()
    _mode_radio(at).set_value(PROJECTION_MODE_V2).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    after = _db_snapshot()

    assert before == after, "Selecting v2 mode must not mutate persisted state"
    assert after["projections"] == []


def test_mode_toggle_adds_no_write_affordance(isolated_db):
    """The mode toggle is a read-only selector — selecting v2 adds no button."""
    slate_id = _seed_slate()
    _seed_full_fight(slate_id)

    at = _open_page()
    _mode_radio(at).set_value(PROJECTION_MODE_V2).run()
    assert list(at.button) == [], [b.key for b in at.button]
