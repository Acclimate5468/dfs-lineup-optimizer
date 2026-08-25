"""AppTest coverage for the Optimizer v1 Slice B.5 Streamlit page.

Loads ``app/pages/07_optimizer.py`` via ``streamlit.testing.v1.AppTest``
against an isolated temp SQLite DB and pins
``docs/OPTIMIZER_V1_DESIGN.md`` §4 / §6 / §8 / §10 plus ``docs/DEVELOPMENT_NOTES.md`` §11:

- Empty DB → "No slates yet" info; no slate selector, no Generate button.
- Banner covers the §6 / §10 contract (research only, no contest entry,
  no DK upload export, no run persistence, gate required, no
  ``effective_status``).
- Slate selector renders when slates exist.
- Page load does NOT run the optimizer (no lineup tables on first
  render).
- Not-ready slate: gate-blocked banner is rendered, the
  ``Generate Lineups`` button is disabled, and a no-op click leaves
  persisted state untouched.
- Ready slate: a single-lineup solve renders a per-lineup table of
  fighter names + salaries and a totals caption.
- Ready slate: multi-lineup solves render N tables.
- Undersized-pool slate: the page renders the solver's diagnostic
  (``infeasible_pool_too_small``) instead of crashing.
- Page load and Generate click do NOT mutate persisted state
  (docs/DEVELOPMENT_NOTES.md §11 — read-only, no audit row, no lineup persistence).
- ``n_lineups`` control is bounded to ``[1, 5]`` per design §2 / §5.2.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = REPO_ROOT / "app" / "pages" / "07_optimizer.py"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "optimizer_page.sqlite3"
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


def _seed_not_ready_slate(name: str = "UFC NotReady") -> int:
    """Slate with no salary import / no fighters — Blocking checks fail
    and ``summary.ready`` is False."""
    conn = get_connection()
    try:
        apply_schema(conn)
        return SlateRepository(conn).create(event_name=name).id
    finally:
        conn.close()


READY_FIGHTS: tuple[tuple[str, str], ...] = (
    ("Aldo", "Vera"),
    ("Holloway", "Topuria"),
    ("Pereira", "Hill"),
    ("Adesanya", "Strickland"),
    ("Volkanovski", "Makhachev"),
    ("Oliveira", "Gaethje"),
)


def _seed_ready_optimizer_slate(name: str = "UFC Ready") -> int:
    """Six confirmed fights → twelve active fighters with full odds
    coverage; slate is marked manually reviewed so the gate is green
    and the optimizer service can solve a 6-fighter lineup.
    """
    conn = get_connection()
    try:
        apply_schema(conn)
        sid = SlateRepository(conn).create(
            event_name=name,
            salary_csv_status="validated",
            salary_row_count=2 * len(READY_FIGHTS),
        ).id
        for fav, dog in READY_FIGHTS:
            _insert_fighter(conn, slate_id=sid, name=fav, salary=8500)
            _insert_fighter(conn, slate_id=sid, name=dog, salary=7500)
        for fav, dog in READY_FIGHTS:
            fg = FightGroupRepository(conn).create(
                slate_id=sid,
                fighter_1_name=fav,
                fighter_2_name=dog,
                scheduled_rounds=3,
            )
            FightGroupRepository(conn).update_status(fg.id, "confirmed")
        for i, (fav, dog) in enumerate(READY_FIGHTS):
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
        SlateRepository(conn).set_manual_review_reviewed(sid)
        return sid
    finally:
        conn.close()


UNDERSIZED_FIGHTS: tuple[tuple[str, str], ...] = (
    ("U1_A", "U1_B"),
    ("U2_A", "U2_B"),
    ("U3_A", "U3_B"),
    ("U4_A", "U4_B"),
)


def _seed_undersized_ready_slate(name: str = "UFC Undersized") -> int:
    """Four confirmed fights → eight active fighters, but only five
    have matched odds, so the optimizer pool ends up below the
    6-fighter ``lineup_size`` precondition. The slate is still marked
    manually reviewed; per design the service then short-circuits the
    solver into ``infeasible_pool_too_small``.
    """
    conn = get_connection()
    try:
        apply_schema(conn)
        sid = SlateRepository(conn).create(
            event_name=name,
            salary_csv_status="validated",
            salary_row_count=2 * len(UNDERSIZED_FIGHTS),
        ).id
        for a, b in UNDERSIZED_FIGHTS:
            _insert_fighter(conn, slate_id=sid, name=a, salary=8000)
            _insert_fighter(conn, slate_id=sid, name=b, salary=8000)
        for a, b in UNDERSIZED_FIGHTS:
            fg = FightGroupRepository(conn).create(
                slate_id=sid,
                fighter_1_name=a,
                fighter_2_name=b,
                scheduled_rounds=3,
            )
            FightGroupRepository(conn).update_status(fg.id, "confirmed")
        # Five matched odds rows — three opponents stay uncovered so
        # the projection layer marks them ``missing_inputs`` and they
        # drop from the pool. Five-of-eight is 37.5% uncovered, below
        # the §5.4.a 50% Blocking threshold, so the gate stays green.
        covered = (
            ("U1_A", -150, 0),
            ("U1_B", +130, 1),
            ("U2_A", -120, 2),
            ("U2_B", +110, 3),
            ("U3_A", -200, 4),
        )
        for fighter_name, odds, i in covered:
            _save_odds_row(
                conn,
                slate_id=sid,
                fighter_name_raw=fighter_name,
                american_odds=odds,
                captured_at=f"2026-05-20T00:00:{i:02d}Z",
            )
        recompute_and_replace_match_results(conn, sid)
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


def _button(at: AppTest, key: str):
    matched = [b for b in at.button if b.key == key]
    assert len(matched) == 1, (
        f"Expected exactly one button with key {key!r}; "
        f"saw button keys: {[b.key for b in at.button]}"
    )
    return matched[0]


def _number_input(at: AppTest, key: str):
    matched = [n for n in at.number_input if n.key == key]
    assert len(matched) == 1, (
        f"Expected exactly one number_input with key {key!r}; "
        f"saw number_input keys: {[n.key for n in at.number_input]}"
    )
    return matched[0]


# ---------------------------------------------------------------------------
# Empty state + banner
# ---------------------------------------------------------------------------


def test_empty_db_shows_no_slates_info_and_no_generate_button(isolated_db):
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    infos = [i.value for i in at.info]
    assert any("No slates yet" in s for s in infos), infos

    # Short-circuit before any selector / button render.
    assert [b.key for b in at.button] == []
    assert [s.key for s in at.selectbox] == []


def test_banner_covers_v1_contract_fragments(isolated_db):
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    warnings = " ".join(w.value for w in at.warning)
    for fragment in (
        "research lineups only",
        "does NOT enter DraftKings",
        "DK upload CSV",
        "does NOT persist",
        "Manual Review Gate",
        "effective_status",
    ):
        assert fragment in warnings, (
            f"Expected banner to mention {fragment!r}; got: {warnings!r}"
        )


# ---------------------------------------------------------------------------
# Slate selector + gate readout
# ---------------------------------------------------------------------------


def test_slate_selector_renders_when_slates_exist(isolated_db):
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception

    selectbox_keys = [s.key for s in at.selectbox]
    assert "optimizer_slate_id" in selectbox_keys


def test_gate_readout_caption_renders_blocking_warning_info_counts(
    isolated_db,
):
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception

    captions = " ".join(c.value for c in at.caption)
    assert "Blocking:" in captions
    assert "Warning:" in captions
    assert "Informational:" in captions
    assert "Ready:" in captions


# ---------------------------------------------------------------------------
# Page-load does not run the optimizer
# ---------------------------------------------------------------------------


def test_page_load_does_not_run_optimizer_on_ready_slate(isolated_db):
    """The optimizer must only run on explicit click (design §6).
    On a ready slate the Generate button is enabled, but until it is
    clicked no lineup tables, solver-status markdown, or totals
    captions should appear.
    """
    _seed_ready_optimizer_slate()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # No lineup tables on page load.
    assert len(at.dataframe) == 0

    # No solver-status markdown was rendered.
    markdown_blob = " ".join(m.value for m in at.markdown)
    assert "Solver status:" not in markdown_blob

    # No totals caption was rendered.
    captions = " ".join(c.value for c in at.caption)
    assert "Total salary:" not in captions
    assert "Total projection:" not in captions


# ---------------------------------------------------------------------------
# Not-ready gate: blocked banner + disabled button + no-op click
# ---------------------------------------------------------------------------


def test_not_ready_slate_renders_blocked_banner_and_disables_button(
    isolated_db,
):
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    warnings = " ".join(w.value for w in at.warning)
    assert "Manual Review Gate is not green" in warnings, warnings

    btn = _button(at, "optimizer_generate_btn")
    assert btn.disabled is True


def test_not_ready_slate_click_does_not_crash_or_mutate(isolated_db):
    """A click on the disabled Generate button must leave the page
    exception-free and the DB untouched."""
    _seed_not_ready_slate()
    before = _db_snapshot()

    at = _open_page()
    assert not at.exception

    btn = _button(at, "optimizer_generate_btn")
    assert btn.disabled is True

    # Streamlit's disabled-click semantics drop the action, but try
    # the click anyway to lock in the defense-in-depth contract: the
    # page must not crash and must keep the gate banner visible.
    btn.click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    warnings = " ".join(w.value for w in at.warning)
    assert "Manual Review Gate is not green" in warnings

    after = _db_snapshot()
    assert before == after


# ---------------------------------------------------------------------------
# Ready slate: one-lineup generate
# ---------------------------------------------------------------------------


def test_ready_slate_generates_one_lineup(isolated_db):
    _seed_ready_optimizer_slate()
    at = _open_page()
    assert not at.exception

    btn = _button(at, "optimizer_generate_btn")
    assert btn.disabled is False

    btn.click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    markdown_blob = " ".join(m.value for m in at.markdown)
    assert "Solver status:" in markdown_blob
    assert "`ok`" in markdown_blob, markdown_blob

    # Exactly one per-lineup table on a default n_lineups=1 solve.
    assert len(at.dataframe) == 1


def test_lineup_table_displays_fighter_names_and_totals(isolated_db):
    _seed_ready_optimizer_slate()
    at = _open_page()
    _button(at, "optimizer_generate_btn").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert len(at.dataframe) == 1
    df = at.dataframe[0].value
    assert list(df.columns) == ["Fighter", "Salary"]
    assert len(df) == 6

    rendered_names = set(df["Fighter"].tolist())
    expected_names = {n for pair in READY_FIGHTS for n in pair}
    assert rendered_names.issubset(expected_names), (
        f"Lineup contained unexpected names: "
        f"{rendered_names - expected_names}"
    )

    captions = " ".join(c.value for c in at.caption)
    assert "Total salary:" in captions
    assert "Total projection:" in captions
    # 6 fighters, all at $8,500 or $7,500 → total fits under the 50k cap.
    assert "DK pts" in captions


def test_lineup_count_wording_clarifies_roster_size(isolated_db):
    """The page must make clear that "N lineups" means N six-fighter
    rosters, not a single N-fighter roster (design §3 terminology note
    / §11 risk #8)."""
    _seed_ready_optimizer_slate()
    at = _open_page()
    assert not at.exception

    # Clarifier caption is present on load, before any solve.
    load_captions = " ".join(c.value for c in at.caption)
    assert "full 6-fighter DK Classic roster" in load_captions, load_captions

    _button(at, "optimizer_generate_btn").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    success_blob = " ".join(s.value for s in at.success)
    assert "6 fighters each" in success_blob, success_blob

    subheader_values = [s.value for s in at.subheader]
    assert any(
        v.startswith("Lineup 1 of 1") for v in subheader_values
    ), subheader_values


# ---------------------------------------------------------------------------
# Ready slate: multi-lineup generate
# ---------------------------------------------------------------------------


def test_ready_slate_can_generate_multiple_lineups(isolated_db):
    _seed_ready_optimizer_slate()
    at = _open_page()

    _number_input(at, "optimizer_n_lineups").set_value(3)
    at.run()
    assert not at.exception

    _button(at, "optimizer_generate_btn").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    markdown_blob = " ".join(m.value for m in at.markdown)
    assert "Solver status:" in markdown_blob
    # ``ok`` covers the all-3-feasible path; ``ok_partial`` covers the
    # diversity-cut-exhausted edge case. Either is acceptable as long
    # as the page renders multiple lineup tables.
    assert ("`ok`" in markdown_blob) or ("`ok_partial`" in markdown_blob)

    # Each lineup renders its own dataframe.
    assert len(at.dataframe) >= 2

    subheader_values = [s.value for s in at.subheader]
    # Subheaders use the "Lineup {index} of {total}" form so a
    # multi-lineup run is never misread as a single roster
    # (design §3 terminology note / §11 risk #8).
    n_rendered = len(at.dataframe)
    assert any(
        v.startswith(f"Lineup 1 of {n_rendered}") for v in subheader_values
    ), subheader_values
    assert any(
        v.startswith(f"Lineup 2 of {n_rendered}") for v in subheader_values
    ), subheader_values


# ---------------------------------------------------------------------------
# Undersized pool: diagnostics, no crash
# ---------------------------------------------------------------------------


def test_undersized_pool_renders_diagnostic_instead_of_crashing(isolated_db):
    _seed_undersized_ready_slate()
    at = _open_page()
    assert not at.exception

    btn = _button(at, "optimizer_generate_btn")
    assert btn.disabled is False
    btn.click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    markdown_blob = " ".join(m.value for m in at.markdown)
    assert "Solver status:" in markdown_blob
    assert "`infeasible_pool_too_small`" in markdown_blob, markdown_blob

    errors = " ".join(e.value for e in at.error)
    assert "pool is too small" in errors, errors
    # Diagnostic should name the actual pool size (5) and lineup size (6).
    assert "5" in errors and "6" in errors

    # No lineup tables on an infeasible-pool result.
    assert len(at.dataframe) == 0


# ---------------------------------------------------------------------------
# Read-only invariants (docs/DEVELOPMENT_NOTES.md §11 — page must not write)
# ---------------------------------------------------------------------------


def test_page_load_does_not_mutate_db(isolated_db):
    _seed_ready_optimizer_slate()
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception
    after = _db_snapshot()
    assert before == after


def test_generate_click_does_not_mutate_db(isolated_db):
    _seed_ready_optimizer_slate()
    before = _db_snapshot()

    at = _open_page()
    _button(at, "optimizer_generate_btn").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    after = _db_snapshot()
    assert before == after, (
        "run_optimizer is read-only end to end; the Generate click "
        "must not write to any table (design §5.3 / docs/DEVELOPMENT_NOTES.md §11)."
    )


# ---------------------------------------------------------------------------
# n_lineups control is bounded to [1, 5]
# ---------------------------------------------------------------------------


def test_n_lineups_input_is_bounded_one_to_five(isolated_db):
    _seed_ready_optimizer_slate()
    at = _open_page()
    assert not at.exception

    ni = _number_input(at, "optimizer_n_lineups")
    assert ni.min == 1
    assert ni.max == 5
    assert ni.step == 1
    assert ni.value == 1
