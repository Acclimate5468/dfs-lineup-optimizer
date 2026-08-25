"""AppTest coverage for Fight Groups Region E — suggested DK pairings.

Realizes docs/DK_GAME_INFO_PAIRING_DESIGN.md §4 (Region E) and its test plan
(§7 tests 11–16). Loads ``app/pages/02_fight_groups.py`` via
``streamlit.testing.v1.AppTest`` against an isolated temp SQLite DB and pins:

  - Visibility: the section renders only when at least one active fighter
    carries a non-blank ``game_info`` (§4.1).
  - Preview is automatic and read-only — it surfaces the suggested pairs (plus
    incomplete / anomaly / uncovered notes) and writes nothing on load
    (§4.2.1, docs/DEVELOPMENT_NOTES.md §11).
  - Apply Suggested DK Pairings is the only write path: on the explicit click it
    creates one 3-round, ``unconfirmed`` group per eligible pair (§4.3, §3.1).
  - Already-grouped fighters are skipped by default; the opt-in adds a second
    group (§4.3). A re-click is idempotent (§4.3).
  - Incomplete (one active fighter), anomaly (>2), and blank ``game_info``
    (uncovered) cases are surfaced and never grouped (§6).

The shared ``create_groups_for_pairs`` write core (now in
``src/slate/fight_group_apply_service.py``) is exercised by both Region D
(``tests/test_fight_groups_page.py``) and Region E here (design §4.3); its
direct unit tests live in ``tests/test_fight_group_apply_service.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import get_connection
from src.db.repositories import (
    FighterRepository,
    FightGroupRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.dk_salary_importer import ParsedSalaryRow

REPO_ROOT = Path(__file__).resolve().parent.parent
FIGHT_GROUPS_PAGE = REPO_ROOT / "app" / "pages" / "02_fight_groups.py"

APPLY_BUTTON = "Apply Suggested DK Pairings"

# Synthetic DK-shaped Game Info strings — both rows of a bout share the
# byte-identical value (design §1.1). Test data only; never real feed data.
GI_AB = "A Fighter@B Fighter 05/22/2026 06:00PM ET"
GI_CD = "C Fighter@D Fighter 05/22/2026 09:00PM ET"
GI_EF = "E Fighter@F Fighter 05/23/2026 10:00PM ET"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "fight_groups_game_info.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _open_page() -> AppTest:
    at = AppTest.from_file(str(FIGHT_GROUPS_PAGE), default_timeout=60)
    at.run()
    return at


def _seed_slate(name: str = "UFC 800") -> int:
    conn = get_connection()
    try:
        apply_schema(conn)
        return SlateRepository(conn).create(event_name=name).id
    finally:
        conn.close()


def _seed_fighters(slate_id: int, rows: list[tuple[str, int, str | None]]) -> dict[str, int]:
    """Seed fighters with explicit per-fighter ``game_info``.

    ``rows`` is ``[(name, salary, game_info)]``. Unlike the shared helper in
    ``test_fight_groups_page.py`` (which gives every fighter the same value),
    this lets a test build distinct bouts and blank/uncovered rows.
    """
    conn = get_connection()
    try:
        apply_schema(conn)
        FighterRepository(conn).upsert_for_slate(
            slate_id=slate_id,
            parsed_rows=[
                ParsedSalaryRow(
                    fighter_name=name,
                    salary=salary,
                    roster_position="F",
                    game_info=game_info,
                    source_row_number=i + 1,
                )
                for i, (name, salary, game_info) in enumerate(rows)
            ],
        )
        return {r.name: r.id for r in FighterRepository(conn).list_for_slate(slate_id)}
    finally:
        conn.close()


def _seed_group(
    slate_id: int,
    f1: str,
    f2: str,
    *,
    scheduled_rounds: int = 3,
    status: str = "unconfirmed",
) -> int:
    conn = get_connection()
    try:
        apply_schema(conn)
        return FightGroupRepository(conn).create(
            slate_id=slate_id,
            fighter_1_name=f1,
            fighter_2_name=f2,
            scheduled_rounds=scheduled_rounds,
            status=status,
        ).id
    finally:
        conn.close()


def _group_rows(slate_id: int) -> list[tuple]:
    conn = get_connection()
    try:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT fighter_1_name, fighter_2_name, scheduled_rounds, status "
                "FROM fight_groups WHERE slate_id = ? ORDER BY id",
                (slate_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def _snapshot() -> dict:
    conn = get_connection()
    try:
        return {
            "fight_groups": conn.execute(
                "SELECT id, slate_id, fighter_1_name, fighter_2_name, "
                "scheduled_rounds, status FROM fight_groups ORDER BY id"
            ).fetchall(),
            "fighters": conn.execute(
                "SELECT id, slate_id, name, salary, status, game_info "
                "FROM fighters ORDER BY id"
            ).fetchall(),
            "manual_match_overrides": conn.execute(
                "SELECT COUNT(*) FROM manual_match_overrides"
            ).fetchone()[0],
            "slates": conn.execute("SELECT COUNT(*) FROM slates").fetchone()[0],
        }
    finally:
        conn.close()


def _gi_preview_df(at: AppTest):
    """The Region E suggested-pairs dataframe (Fighter 1 / Fighter 2 / Status).

    Disambiguated from Region A ("Coverage") and the Region D pasted-card
    preview ("Blocked Reason" / "Matched Fighter 1") by its exact column set.
    """
    wanted = {"Fighter 1", "Fighter 2", "Status"}
    matched = [d for d in at.dataframe if wanted <= set(d.value.columns)]
    assert len(matched) == 1, (
        f"Expected one Region E preview dataframe; "
        f"saw {[list(d.value.columns) for d in at.dataframe]}"
    )
    return matched[0].value


def _has_gi_preview_df(at: AppTest) -> bool:
    wanted = {"Fighter 1", "Fighter 2", "Status"}
    return any(wanted <= set(d.value.columns) for d in at.dataframe)


def _apply_button(at: AppTest):
    matched = [b for b in at.button if b.label == APPLY_BUTTON]
    assert len(matched) == 1, f"Expected the {APPLY_BUTTON!r} button"
    return matched[0]


def _optin_checkbox(at: AppTest):
    matched = [c for c in at.checkbox if c.label.startswith("Include suggestions")]
    assert len(matched) == 1, (
        f"Expected the Region E opt-in checkbox; saw {[c.label for c in at.checkbox]}"
    )
    return matched[0]


def _summary_md(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown)


def _metric(at: AppTest, label: str) -> int:
    matched = [m for m in at.metric if m.label == label]
    assert len(matched) == 1, f"Expected one metric {label!r}"
    return int(matched[0].value)


# ---------------------------------------------------------------------------
# Visibility (design §4.1)
# ---------------------------------------------------------------------------


def test_section_hidden_when_no_active_fighter_has_game_info(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000, None), ("B Fighter", 8500, None)]
    )

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # No Region E surfaces at all when nothing carries a Game Info value.
    assert APPLY_BUTTON not in [b.label for b in at.button]
    assert not _has_gi_preview_df(at)
    assert "from DK Game Info" not in _summary_md(at)


def test_section_hidden_for_blank_string_game_info(isolated_db):
    """A blank (whitespace-only) Game Info is treated as not captured."""
    slate_id = _seed_slate()
    # _optional_text folds blanks to None at import, but defend the UI gate too.
    _seed_fighters(
        slate_id, [("A Fighter", 9000, "   "), ("B Fighter", 8500, "")]
    )

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert APPLY_BUTTON not in [b.label for b in at.button]
    assert not _has_gi_preview_df(at)


# ---------------------------------------------------------------------------
# Preview (read-only) — design §4.2, tests 11 / 12
# ---------------------------------------------------------------------------


def test_preview_shows_valid_suggestions(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [
            ("A Fighter", 9000, GI_AB),
            ("B Fighter", 8500, GI_AB),
            ("C Fighter", 8000, GI_CD),
            ("D Fighter", 7500, GI_CD),
        ],
    )

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    df = _gi_preview_df(at)
    pairs = {(r["Fighter 1"], r["Fighter 2"]) for _, r in df.iterrows()}
    assert pairs == {("A Fighter", "B Fighter"), ("C Fighter", "D Fighter")}
    # Nothing grouped yet, so every suggested pair is "ready".
    assert set(df["Status"]) == {"ready"}

    assert "2 suggested pairing(s)" in _summary_md(at)
    assert "from DK Game Info" in _summary_md(at)
    # The Apply affordance is present but has not written anything.
    assert APPLY_BUTTON in [b.label for b in at.button]


def test_preview_writes_nothing_on_load(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [
            ("A Fighter", 9000, GI_AB),
            ("B Fighter", 8500, GI_AB),
            ("C Fighter", 8000, GI_CD),
            ("D Fighter", 7500, GI_CD),
        ],
    )

    before = _snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # The preview rendered yet the DB is byte-for-byte unchanged.
    assert _has_gi_preview_df(at)
    assert _group_rows(slate_id) == []
    assert before == _snapshot()


# ---------------------------------------------------------------------------
# Apply — design §4.3, tests 13 / 14 / 15
# ---------------------------------------------------------------------------


def test_apply_creates_unconfirmed_three_round_groups(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [
            ("A Fighter", 9000, GI_AB),
            ("B Fighter", 8500, GI_AB),
            ("C Fighter", 8000, GI_CD),
            ("D Fighter", 7500, GI_CD),
        ],
    )

    at = _open_page()
    assert _group_rows(slate_id) == []  # nothing written before the click

    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = _group_rows(slate_id)
    # A/B (6:00 PM) is a standard 3-round bout; C/D (9:00 PM) is the latest
    # start, so it is auto-detected as the main event and created at 5 rounds.
    assert ("A Fighter", "B Fighter", 3, "unconfirmed") in rows
    assert ("C Fighter", "D Fighter", 5, "unconfirmed") in rows
    assert len(rows) == 2

    # Region A coverage refreshes on the same (no-rerun) post-apply run.
    assert _metric(at, "Grouped fighters") == 4
    assert _metric(at, "Fight groups") == 2

    success = " ".join(s.value for s in at.success)
    assert "Applied 2 new fight group(s) from DK Game Info" in success
    # The auto-detected main event is surfaced (info) so the user can override.
    info = " ".join(i.value for i in at.info)
    assert "Detected main event" in info, info
    assert "C Fighter vs D Fighter" in info, info
    assert "5 rounds" in info, info


def test_apply_falls_back_to_all_three_rounds_without_start_times(isolated_db):
    """When Game Info carries no parseable start times, no main event is
    auto-detected: every group stays 3 rounds and a caption says so."""
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [
            ("A Fighter", 9000, "A Fighter@B Fighter"),
            ("B Fighter", 8500, "A Fighter@B Fighter"),
            ("C Fighter", 8000, "C Fighter@D Fighter"),
            ("D Fighter", 7500, "C Fighter@D Fighter"),
        ],
    )

    at = _open_page()
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = _group_rows(slate_id)
    assert ("A Fighter", "B Fighter", 3, "unconfirmed") in rows
    assert ("C Fighter", "D Fighter", 3, "unconfirmed") in rows
    blob = " ".join(c.value for c in at.caption) if hasattr(at, "caption") else ""
    # The "no main event auto-detected" guidance renders (caption fallback).
    assert "No main event auto-detected" in _summary_md(at) or (
        "No main event auto-detected" in blob
    )


def test_apply_only_mutates_fight_groups_table(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000, GI_AB), ("B Fighter", 8500, GI_AB)],
    )

    at = _open_page()
    before = _snapshot()
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    after = _snapshot()

    assert after["fighters"] == before["fighters"]
    assert after["slates"] == before["slates"]
    assert after["manual_match_overrides"] == before["manual_match_overrides"]
    new_rows = [r for r in after["fight_groups"] if r not in before["fight_groups"]]
    assert len(new_rows) == 1
    _id, _sid, f1, f2, rounds, status = new_rows[0]
    assert (f1, f2, rounds, status) == ("A Fighter", "B Fighter", 3, "unconfirmed")


def test_apply_skips_already_grouped_by_default(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [
            ("A Fighter", 9000, GI_AB),
            ("B Fighter", 8500, GI_AB),
            ("C Fighter", 8000, GI_CD),
            ("D Fighter", 7500, GI_CD),
        ],
    )
    # A is already grouped (with an off-roster name), so the (A,B) suggestion is
    # skipped by default; (C,D) is created.
    _seed_group(slate_id, "A Fighter", "Z Ghost")

    at = _open_page()
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = _group_rows(slate_id)
    # C/D (9:00 PM) is the latest start → auto-detected main event at 5 rounds.
    assert ("C Fighter", "D Fighter", 5, "unconfirmed") in rows
    # The pre-existing A group is untouched; no second A group was created.
    assert ("A Fighter", "Z Ghost", 3, "unconfirmed") in rows
    assert not any(r[0] == "A Fighter" and r[1] == "B Fighter" for r in rows)
    assert len(rows) == 2

    warnings = " ".join(w.value for w in at.warning)
    assert "A Fighter" in warnings and "already grouped" in warnings


def test_apply_skips_duplicates_and_is_idempotent_on_reclick(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [
            ("A Fighter", 9000, GI_AB),
            ("B Fighter", 8500, GI_AB),
            ("C Fighter", 8000, GI_CD),
            ("D Fighter", 7500, GI_CD),
        ],
    )

    at = _open_page()
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(_group_rows(slate_id)) == 2

    # A second click on the same suggestions creates nothing: the fighters are
    # now grouped and the pairs already exist, so every row is skipped.
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(_group_rows(slate_id)) == 2
    assert "No new fight groups created" in " ".join(i.value for i in at.info)


def test_apply_optin_creates_second_group_for_already_grouped(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000, GI_AB), ("B Fighter", 8500, GI_AB)],
    )
    _seed_group(slate_id, "A Fighter", "Z Ghost")  # A already grouped

    at = _open_page()

    # Opt-in OFF → the (A,B) suggestion is skipped, nothing new created.
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(_group_rows(slate_id)) == 1

    # Opt-in ON → a second group for A is created; A becomes a duplicate.
    _optin_checkbox(at).set_value(True)
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = _group_rows(slate_id)
    assert ("A Fighter", "B Fighter", 3, "unconfirmed") in rows
    assert ("A Fighter", "Z Ghost", 3, "unconfirmed") in rows
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Incomplete / anomaly / uncovered are safe (design §6 #1 / #2 / #3)
# ---------------------------------------------------------------------------


def test_incomplete_anomaly_uncovered_surfaced_and_never_grouped(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [
            ("A Fighter", 9000, GI_AB),   # clean pair with B
            ("B Fighter", 8500, GI_AB),
            ("Lone Wolf", 8200, GI_CD),   # only one active fighter for GI_CD
            ("X One", 8000, GI_EF),       # three share GI_EF -> anomaly
            ("Y Two", 7800, GI_EF),
            ("Z Three", 7600, GI_EF),
            ("Blank Bob", 7000, None),    # no Game Info -> uncovered
        ],
    )

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    md = _summary_md(at)
    # Summary counts every bucket.
    assert "1 suggested pairing(s)" in md
    assert "1 incomplete" in md
    assert "1 anomaly" in md
    assert "1 uncovered" in md
    # Incomplete + anomaly fighters are named in the surfaced notes.
    assert "Lone Wolf" in md
    assert "X One" in md and "Y Two" in md and "Z Three" in md
    # Uncovered fighter is surfaced in a caption.
    captions = " ".join(c.value for c in at.caption)
    assert "Blank Bob" in captions

    # Only the clean pair is suggested in the preview table.
    df = _gi_preview_df(at)
    assert {(r["Fighter 1"], r["Fighter 2"]) for _, r in df.iterrows()} == {
        ("A Fighter", "B Fighter")
    }

    # Apply creates exactly the one clean pair — nothing else is grouped.
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _group_rows(slate_id) == [("A Fighter", "B Fighter", 3, "unconfirmed")]
