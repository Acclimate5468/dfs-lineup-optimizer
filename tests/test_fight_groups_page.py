"""AppTest coverage for the Fight Groups page Region A (roster + coverage).

Realizes docs/FIGHT_GROUPS_UX_DESIGN.md §7 (A1 test plan). Loads
``app/pages/02_fight_groups.py`` via ``streamlit.testing.v1.AppTest`` against
an isolated temp SQLite DB and pins the read-only roster + coverage view:

  - Empty DB → existing "No slates saved yet" warning, Region A not reached,
    no ``st.dataframe`` (design §6 #1 / test #1).
  - Slate with no active fighters → five zero metrics + empty-state caption,
    no dataframe (§6 #2 / test #2).
  - Active fighters, no groups → all ``ungrouped`` (§6 #4 / test #3).
  - Paired fighters render opponent / group status / scheduled rounds
    (§6 #8 / test #4).
  - Ungrouped fighters sort first and stay visible (§6 #4 / test #5).
  - Duplicate assignment → ``duplicate`` coverage + warning, grouped count
    counts the fighter once (§6 #5 / test #7).
  - Group referencing an off-roster name → "Unmatched pairings" subsection,
    grouped count excludes the typo slot (§6 #6 / test #8).
  - Page load mutates nothing (§4 / test #9; docs/DEVELOPMENT_NOTES.md §11).
  - Existing Region C confirm/unconfirm controls stay visible and unchanged
    (§3 Region C / test #10).

Also covers the §3 Region B / §8 A2 add-form rewrite: the two free-text
fighter inputs are replaced with slate-aware selectboxes (options = active
roster, ungrouped first, grouped ones labeled "(grouped)" but still
selectable), same-fighter selection is rejected, submit still writes via
FightGroupRepository.create, and the <2-active / all-grouped edge states
surface helpful messages without writing on load.
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

DASH = "—"  # em dash used for blank roster cells


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "fight_groups_page.sqlite3"
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


def _parsed(name: str, salary: int, *, source_row_number: int) -> ParsedSalaryRow:
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
        rec = FightGroupRepository(conn).create(
            slate_id=slate_id,
            fighter_1_name=f1,
            fighter_2_name=f2,
            scheduled_rounds=scheduled_rounds,
            status=status,
        )
        return rec.id
    finally:
        conn.close()


def _metric(at: AppTest, label: str) -> int:
    matched = [m for m in at.metric if m.label == label]
    assert len(matched) == 1, (
        f"Expected exactly one metric {label!r}; "
        f"saw {[m.label for m in at.metric]}"
    )
    return int(matched[0].value)


def _roster_df(at: AppTest):
    # Region A's roster table is the one carrying a ``Coverage`` column; the
    # Region D pasted-card preview ("Blocked Reason") and the Region E Game Info
    # preview ("Fighter 1"/"Fighter 2"/"Status") may legitimately coexist on the
    # same page, so select by column rather than asserting a single dataframe.
    matched = [d for d in at.dataframe if "Coverage" in list(d.value.columns)]
    assert len(matched) == 1, (
        f"Expected exactly one roster (coverage) dataframe; "
        f"saw {[list(d.value.columns) for d in at.dataframe]}"
    )
    return matched[0].value


def _row(df, fighter: str):
    sub = df[df["Fighter"] == fighter]
    assert len(sub) == 1, f"Expected one row for {fighter!r}; got {len(sub)}"
    return sub.iloc[0]


def _snapshot() -> dict:
    conn = get_connection()
    try:
        return {
            "fight_groups": conn.execute(
                "SELECT id, slate_id, fighter_1_name, fighter_2_name, "
                "scheduled_rounds, status FROM fight_groups ORDER BY id"
            ).fetchall(),
            "fighters": conn.execute(
                "SELECT id, slate_id, name, salary, status FROM fighters ORDER BY id"
            ).fetchall(),
            "manual_match_overrides": conn.execute(
                "SELECT COUNT(*) FROM manual_match_overrides"
            ).fetchone()[0],
            "slates": conn.execute("SELECT COUNT(*) FROM slates").fetchone()[0],
        }
    finally:
        conn.close()


def _assert_metrics(at, *, total, grouped, ungrouped, groups, confirmed):
    assert _metric(at, "Total active fighters") == total
    assert _metric(at, "Grouped fighters") == grouped
    assert _metric(at, "Ungrouped fighters") == ungrouped
    assert _metric(at, "Fight groups") == groups
    assert _metric(at, "Confirmed fight groups") == confirmed


# ---------------------------------------------------------------------------
# Empty states (design §6 #1 / #2)
# ---------------------------------------------------------------------------


def test_no_slate_shows_warning_and_no_dataframe(isolated_db):
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    warnings = [w.value for w in at.warning]
    assert any("No slates saved yet" in w for w in warnings), warnings
    # Region A is never reached — st.stop() fires before any roster table.
    assert at.dataframe == []


def test_slate_with_no_active_fighters_shows_zero_metrics_and_caption(isolated_db):
    _seed_slate()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    _assert_metrics(at, total=0, grouped=0, ungrouped=0, groups=0, confirmed=0)
    captions = [c.value for c in at.caption]
    assert any(
        "No active fighters on this slate yet" in c for c in captions
    ), captions
    # No empty roster dataframe is rendered.
    assert at.dataframe == []


# ---------------------------------------------------------------------------
# Roster coverage (design §6 #3 / #4 / #8)
# ---------------------------------------------------------------------------


def test_active_fighters_without_groups_are_all_ungrouped(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    _assert_metrics(at, total=3, grouped=0, ungrouped=3, groups=0, confirmed=0)
    df = _roster_df(at)
    assert sorted(df["Fighter"]) == ["A Fighter", "B Fighter", "C Fighter"]
    assert set(df["Coverage"]) == {"ungrouped"}
    assert set(df["Paired Opponent"]) == {DASH}
    assert set(df["Fight Group"]) == {DASH}
    assert set(df["Scheduled Rounds"]) == {DASH}
    # Salary renders as $X,XXX (design §3).
    assert _row(df, "A Fighter")["Salary"] == "$9,000"


def test_paired_fighters_render_opponent_status_and_rounds(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    g1 = _seed_group(slate_id, "A Fighter", "B Fighter", scheduled_rounds=5, status="confirmed")
    g2 = _seed_group(slate_id, "C Fighter", "D Fighter", scheduled_rounds=3, status="unconfirmed")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    _assert_metrics(at, total=4, grouped=4, ungrouped=0, groups=2, confirmed=1)
    df = _roster_df(at)

    a = _row(df, "A Fighter")
    assert a["Paired Opponent"] == "B Fighter"
    assert a["Fight Group"] == f"#{g1}"
    assert a["Group Status"] == "confirmed"
    assert a["Scheduled Rounds"] == "5 rd — main event/title"
    assert a["Coverage"] == "grouped"

    b = _row(df, "B Fighter")
    assert b["Paired Opponent"] == "A Fighter"
    assert b["Fight Group"] == f"#{g1}"
    assert b["Scheduled Rounds"] == "5 rd — main event/title"

    c = _row(df, "C Fighter")
    assert c["Paired Opponent"] == "D Fighter"
    assert c["Fight Group"] == f"#{g2}"
    assert c["Group Status"] == "unconfirmed"
    assert c["Scheduled Rounds"] == "3 rd"
    assert c["Coverage"] == "grouped"


def test_ungrouped_fighters_sort_first_and_stay_visible(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [
            ("A Fighter", 9000),
            ("B Fighter", 8500),
            ("C Fighter", 8000),
            ("D Fighter", 7500),
            ("E Fighter", 7000),
            ("F Fighter", 6500),
        ],
    )
    _seed_group(slate_id, "A Fighter", "B Fighter")
    _seed_group(slate_id, "C Fighter", "D Fighter")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    _assert_metrics(at, total=6, grouped=4, ungrouped=2, groups=2, confirmed=0)
    df = _roster_df(at)
    # Two ungrouped fighters (E, F) sort to the top of the table.
    assert list(df["Coverage"])[:2] == ["ungrouped", "ungrouped"]
    assert set(df["Coverage"][2:]) == {"grouped"}
    ungrouped = df[df["Coverage"] == "ungrouped"]
    assert set(ungrouped["Fighter"]) == {"E Fighter", "F Fighter"}
    assert set(ungrouped["Paired Opponent"]) == {DASH}


# ---------------------------------------------------------------------------
# Duplicate assignment (design §6 #5 / test #7)
# ---------------------------------------------------------------------------


def test_duplicate_assignment_warns_and_counts_once(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    g1 = _seed_group(slate_id, "A Fighter", "B Fighter")
    _seed_group(slate_id, "A Fighter", "C Fighter")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    df = _roster_df(at)
    a = _row(df, "A Fighter")
    assert a["Coverage"] == "duplicate"
    # First match (lowest id) plus a "+N more" suffix (design §6 #5).
    assert a["Fight Group"] == f"#{g1} (+1 more)"

    warnings = " ".join(w.value for w in at.warning)
    assert "more than one fight group" in warnings
    assert "A Fighter" in warnings

    # A is grouped, counted exactly once: grouped = {A, B, C}, D ungrouped.
    _assert_metrics(at, total=4, grouped=3, ungrouped=1, groups=2, confirmed=0)


# ---------------------------------------------------------------------------
# Off-roster reference / unmatched pairings (design §6 #6 / test #8)
# ---------------------------------------------------------------------------


def test_unmatched_pairing_subsection_and_grouped_count(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    # fighter_1 is a typo with no active-roster match; fighter_2 resolves to A.
    g1 = _seed_group(slate_id, "Typo McGhost", "A Fighter")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    df = _roster_df(at)
    # Roster table omits the off-roster typo name entirely.
    assert set(df["Fighter"]) == {"A Fighter", "B Fighter"}
    # A resolves to the typo'd opponent (raw group value) and is grouped.
    a = _row(df, "A Fighter")
    assert a["Coverage"] == "grouped"
    assert a["Paired Opponent"] == "Typo McGhost"
    assert _row(df, "B Fighter")["Coverage"] == "ungrouped"

    # Grouped count excludes the off-roster slot: only A counts.
    _assert_metrics(at, total=2, grouped=1, ungrouped=1, groups=1, confirmed=0)

    warnings = " ".join(w.value for w in at.warning)
    assert "reference a name not on the active roster" in warnings

    markdown = " ".join(m.value for m in at.markdown)
    assert "Unmatched pairings" in markdown
    assert f"#{g1}: Typo McGhost vs A Fighter" in markdown


# ---------------------------------------------------------------------------
# Read-only page load (design §4 / test #9; docs/DEVELOPMENT_NOTES.md §11)
# ---------------------------------------------------------------------------


def test_page_load_does_not_mutate_db(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)],
    )
    _seed_group(slate_id, "A Fighter", "B Fighter", scheduled_rounds=5, status="confirmed")
    _seed_group(slate_id, "Typo McGhost", "C Fighter")

    before = _snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    after = _snapshot()
    assert before == after


# ---------------------------------------------------------------------------
# Region B / C preserved (design §3 / test #10)
# ---------------------------------------------------------------------------


def test_existing_add_and_confirm_controls_still_visible(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    gid = _seed_group(
        slate_id, "A Fighter", "B Fighter", scheduled_rounds=5, status="confirmed"
    )

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # Region B — add form now uses selectboxes (A2), not free-text inputs, plus
    # the unchanged rounds radio and submit button.
    text_labels = [t.label for t in at.text_input]
    assert "Fighter 1" not in text_labels
    assert "Fighter 2" not in text_labels
    select_labels = [s.label for s in at.selectbox]
    assert "Fighter 1" in select_labels
    assert "Fighter 2" in select_labels
    assert any(r.label == "Scheduled rounds" for r in at.radio)
    assert "Save fight group" in [b.label for b in at.button]

    # Fight-card table — per-group confirm/unconfirm toggle is unchanged
    # (same widget key and label contract as the former vertical list).
    toggle = [b for b in at.button if b.key == f"toggle_{gid}"]
    assert len(toggle) == 1
    assert toggle[0].label == "Mark unconfirmed"  # confirmed group → offer unconfirm

    # Fight-card table metrics still render.
    assert _metric(at, "Total") == 1
    assert _metric(at, "Confirmed") == 1
    assert _metric(at, "Unconfirmed") == 0

    # The fight-card table still lists both fighters of the group. The compact
    # table renders each fighter in its own cell, so the names appear as
    # separate text elements rather than the old "A vs B" string.
    rendered = " ".join(m.value for m in at.markdown)
    assert "A Fighter" in rendered
    assert "B Fighter" in rendered


def test_existing_confirm_toggle_still_writes_status(isolated_db):
    """Region C confirm/unconfirm write path is untouched by the A2 add-form rewrite."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    gid = _seed_group(
        slate_id, "A Fighter", "B Fighter", scheduled_rounds=3, status="unconfirmed"
    )

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    toggle = [b for b in at.button if b.key == f"toggle_{gid}"][0]
    assert toggle.label == "Mark confirmed"  # unconfirmed group → offer confirm
    toggle.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    conn = get_connection()
    try:
        status = conn.execute(
            "SELECT status FROM fight_groups WHERE id = ?", (gid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "confirmed"


# ---------------------------------------------------------------------------
# Region B — slate-aware selectbox add form (design §3 Region B / §8 A2)
# ---------------------------------------------------------------------------


def _add_selectbox(at: AppTest, label: str):
    matched = [s for s in at.selectbox if s.label == label]
    assert len(matched) == 1, f"Expected one {label!r} selectbox; saw {[s.label for s in at.selectbox]}"
    return matched[0]


def _save_button(at: AppTest):
    matched = [b for b in at.button if b.label == "Save fight group"]
    assert len(matched) == 1, "Expected the 'Save fight group' submit button"
    return matched[0]


def _group_rows(slate_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT fighter_1_name, fighter_2_name, scheduled_rounds "
            "FROM fight_groups WHERE slate_id = ? ORDER BY id",
            (slate_id,),
        ).fetchall()
    finally:
        conn.close()


def test_add_form_uses_selectboxes_with_active_fighters(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # No free-text fighter inputs remain.
    assert "Fighter 1" not in [t.label for t in at.text_input]
    assert "Fighter 2" not in [t.label for t in at.text_input]

    # Two fighter selectboxes whose options are the active slate roster.
    opts = _add_selectbox(at, "Fighter 1").options
    assert set(opts) == {"A Fighter", "B Fighter", "C Fighter"}
    assert _add_selectbox(at, "Fighter 2").options == opts


def test_ungrouped_fighters_listed_before_grouped_in_add_form(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    _seed_group(slate_id, "A Fighter", "B Fighter")  # A + B grouped, C + D not

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # Ungrouped fighters surface first; grouped ones follow, clearly labeled.
    assert _add_selectbox(at, "Fighter 1").options == [
        "C Fighter",
        "D Fighter",
        "A Fighter (grouped)",
        "B Fighter (grouped)",
    ]


def test_grouped_fighters_labeled_and_still_selectable(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )
    _seed_group(slate_id, "A Fighter", "B Fighter")  # A + B grouped, C not

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    opts = _add_selectbox(at, "Fighter 1").options
    assert "A Fighter (grouped)" in opts
    assert "B Fighter (grouped)" in opts
    assert "C Fighter" in opts  # ungrouped → no suffix

    # A grouped fighter is still selectable: re-using one creates another group.
    _add_selectbox(at, "Fighter 1").set_value("A Fighter")
    _add_selectbox(at, "Fighter 2").set_value("C Fighter")
    _save_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    success = " ".join(s.value for s in at.success)
    assert "A Fighter vs C Fighter" in success
    assert ("A Fighter", "C Fighter", 3) in [tuple(r) for r in _group_rows(slate_id)]


def test_submit_creates_group_from_selected_fighters(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _group_rows(slate_id) == []  # nothing written on load

    _add_selectbox(at, "Fighter 1").set_value("A Fighter")
    _add_selectbox(at, "Fighter 2").set_value("B Fighter")
    _save_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    success = " ".join(s.value for s in at.success)
    assert "A Fighter vs B Fighter" in success
    assert ("A Fighter", "B Fighter", 3) in [tuple(r) for r in _group_rows(slate_id)]


def test_cannot_create_group_with_same_fighter_both_sides(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    _add_selectbox(at, "Fighter 1").set_value("A Fighter")
    _add_selectbox(at, "Fighter 2").set_value("A Fighter")
    _save_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    errors = " ".join(e.value for e in at.error)
    assert "cannot be matched against themselves" in errors
    # No fight group was written.
    assert _group_rows(slate_id) == []


def test_fewer_than_two_active_fighters_prevents_create(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("Solo Fighter", 9000)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    infos = " ".join(i.value for i in at.info)
    assert "at least two active fighters" in infos
    # The add form is not rendered → create is impossible.
    assert "Fighter 1" not in [s.label for s in at.selectbox]
    assert "Save fight group" not in [b.label for b in at.button]


def test_all_grouped_slate_shows_helpful_message(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    _seed_group(slate_id, "A Fighter", "B Fighter")  # both active fighters grouped

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    infos = " ".join(i.value for i in at.info)
    assert "All active fighters are already grouped" in infos
    # Form still renders — grouped fighters remain selectable.
    assert "Save fight group" in [b.label for b in at.button]
    assert _add_selectbox(at, "Fighter 1").options == [
        "A Fighter (grouped)",
        "B Fighter (grouped)",
    ]


def test_add_form_selectboxes_do_not_write_on_load(isolated_db):
    """Rendering the selectbox add form performs no INSERT (docs/DEVELOPMENT_NOTES.md §11)."""
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )

    before = _snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert before == _snapshot()


# ---------------------------------------------------------------------------
# A3 — scheduled-rounds / main-event UX (FIGHT_GROUPS_UX_DESIGN §8 A3)
#
# Display + form copy only: clearer 3-vs-5 choice in the add form, a "5-round
# fights" metric, a spelled-out 5-round label in the roster / groups displays,
# and a non-blocking reminder when no 5-round fight is marked. No schema,
# repository, or projection change; nothing is written or auto-changed on load.
# ---------------------------------------------------------------------------

FIVE_ROUND_CELL = "5 rd — main event/title"
REMINDER_TEXT = "No 5-round fight is marked on this slate"


def _rounds_radio(at: AppTest):
    matched = [r for r in at.radio if r.label == "Scheduled rounds"]
    assert len(matched) == 1, (
        f"Expected one 'Scheduled rounds' radio; saw {[r.label for r in at.radio]}"
    )
    return matched[0]


def test_add_form_rounds_radio_has_descriptive_labels(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # The rounds choice spells out standard-vs-main-event instead of bare 3 / 5.
    assert _rounds_radio(at).options == [
        "3 rounds — standard bout",
        "5 rounds — main event / title bout",
    ]


def test_rounds_helper_copy_renders_in_add_form(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    captions = " ".join(c.value for c in at.caption)
    assert "Most UFC bouts are 3 rounds" in captions
    assert "main event" in captions
    assert "Verify rounds before Manual Review" in captions


def test_creating_five_round_group_persists_scheduled_rounds_5(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _group_rows(slate_id) == []  # nothing written on load

    _add_selectbox(at, "Fighter 1").set_value("A Fighter")
    _add_selectbox(at, "Fighter 2").set_value("B Fighter")
    _rounds_radio(at).set_value(5)  # select the main-event / title option
    _save_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # The integer create path is unchanged: the descriptive label persists as 5.
    assert ("A Fighter", "B Fighter", 5) in [tuple(r) for r in _group_rows(slate_id)]


def test_three_round_group_still_persists_as_default(isolated_db):
    """The default (no radio change) still writes a 3-round group."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    _add_selectbox(at, "Fighter 1").set_value("A Fighter")
    _add_selectbox(at, "Fighter 2").set_value("B Fighter")
    _save_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert ("A Fighter", "B Fighter", 3) in [tuple(r) for r in _group_rows(slate_id)]


def test_five_round_group_is_marked_in_roster_groups_and_metric(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    _seed_group(slate_id, "A Fighter", "B Fighter", scheduled_rounds=5)
    _seed_group(slate_id, "C Fighter", "D Fighter", scheduled_rounds=3)

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # Dedicated metric surfaces the 5-round count alongside the A1 metrics.
    assert _metric(at, "5-round fights") == 1

    # Roster table spells out the 5-round bout; the 3-round bout stays terse.
    df = _roster_df(at)
    assert _row(df, "A Fighter")["Scheduled Rounds"] == FIVE_ROUND_CELL
    assert _row(df, "B Fighter")["Scheduled Rounds"] == FIVE_ROUND_CELL
    assert _row(df, "C Fighter")["Scheduled Rounds"] == "3 rd"

    # Region C bolds the 5-round group so it stands out in the list.
    markdown = " ".join(m.value for m in at.markdown)
    assert f"**{FIVE_ROUND_CELL}**" in markdown


def test_missing_five_round_fight_shows_reminder(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    _seed_group(slate_id, "A Fighter", "B Fighter", scheduled_rounds=3)
    _seed_group(slate_id, "C Fighter", "D Fighter", scheduled_rounds=3)

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert _metric(at, "5-round fights") == 0
    infos = " ".join(i.value for i in at.info)
    assert REMINDER_TEXT in infos


def test_reminder_hidden_when_a_five_round_fight_exists(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    _seed_group(slate_id, "A Fighter", "B Fighter", scheduled_rounds=5)
    _seed_group(slate_id, "C Fighter", "D Fighter", scheduled_rounds=3)

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    infos = " ".join(i.value for i in at.info)
    assert REMINDER_TEXT not in infos


def test_reminder_hidden_when_no_groups_yet(isolated_db):
    """A brand-new slate with no groups is not nagged about 5-round fights."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    infos = " ".join(i.value for i in at.info)
    assert REMINDER_TEXT not in infos


def test_a3_reminder_path_does_not_write_or_change_groups_on_load(isolated_db):
    """The reminder fires on load (all 3-round groups) yet mutates nothing.

    Pins that A3 is advisory: no INSERT/UPDATE and no auto-change to an
    existing group's scheduled_rounds on page load (docs/DEVELOPMENT_NOTES.md §11).
    """
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )
    _seed_group(slate_id, "A Fighter", "B Fighter", scheduled_rounds=3)

    before = _snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    # Reminder did fire (no 5-round group) but the DB — including the group's
    # scheduled_rounds — is byte-for-byte unchanged.
    assert REMINDER_TEXT in " ".join(i.value for i in at.info)
    assert before == _snapshot()


# ---------------------------------------------------------------------------
# Region D — assisted pairing from a pasted card (A4.3 preview + A4.4 apply)
#
# Realizes docs/FIGHT_GROUPS_UX_DESIGN.md §9. Parse / Preview (A4.3) is
# read-only: it computes a preview from the pure parser
# (src.slate.fight_card_parser) and renders it, writing nothing. Apply Valid
# Pairings (A4.4, §9.6 / §9.9 tests 10–14) is the only write path — on an
# explicit click it creates one 3-round, unconfirmed fight group per eligible
# pair via FightGroupRepository.create, skips already-grouped fighters by
# default (opt-in checkbox to add a second group anyway), is idempotent on
# re-click, and never updates or deletes an existing group.
# ---------------------------------------------------------------------------

PASTE_LABEL = "Pasted fight card"
PARSE_BUTTON = "Parse / Preview"
APPLY_BUTTON = "Apply Valid Pairings"


def _paste_area(at: AppTest):
    matched = [t for t in at.text_area if t.label == PASTE_LABEL]
    assert len(matched) == 1, (
        f"Expected one {PASTE_LABEL!r} text area; saw {[t.label for t in at.text_area]}"
    )
    return matched[0]


def _parse_button(at: AppTest):
    matched = [b for b in at.button if b.label == PARSE_BUTTON]
    assert len(matched) == 1, "Expected the 'Parse / Preview' button"
    return matched[0]


def _preview_df(at: AppTest):
    matched = [d for d in at.dataframe if "Blocked Reason" in list(d.value.columns)]
    assert len(matched) == 1, "Expected exactly one assisted-pairing preview dataframe"
    return matched[0].value


def _has_preview_df(at: AppTest) -> bool:
    return any("Blocked Reason" in list(d.value.columns) for d in at.dataframe)


def _preview_row(df, pasted_line: str):
    sub = df[df["Pasted Line"] == pasted_line]
    assert len(sub) == 1, f"Expected one preview row for {pasted_line!r}; got {len(sub)}"
    return sub.iloc[0]


def _apply_button(at: AppTest):
    matched = [b for b in at.button if b.label == APPLY_BUTTON]
    assert len(matched) == 1, "Expected the 'Apply Valid Pairings' button"
    return matched[0]


def _optin_checkbox(at: AppTest):
    matched = [c for c in at.checkbox if "already grouped" in c.label]
    assert len(matched) == 1, (
        f"Expected the already-grouped opt-in checkbox; "
        f"saw {[c.label for c in at.checkbox]}"
    )
    return matched[0]


def _roster_df_any(at: AppTest):
    """The Region A roster dataframe, even when the preview dataframe coexists.

    Region A's table carries a ``Coverage`` column; the preview table does not,
    so this disambiguates the two when both render on a post-apply rerun.
    """
    matched = [d for d in at.dataframe if "Coverage" in list(d.value.columns)]
    assert len(matched) == 1, "Expected exactly one roster (coverage) dataframe"
    return matched[0].value


def test_assisted_builder_section_renders_on_load(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # The text area + explicit Parse / Preview button are present (inside the
    # collapsible section); the write-on-apply-only intent is stated in copy.
    assert PASTE_LABEL in [t.label for t in at.text_area]
    assert PARSE_BUTTON in [b.label for b in at.button]
    captions = " ".join(c.value for c in at.caption)
    assert "nothing is written" in captions.lower()
    # The Apply write button does not appear until a preview is computed.
    assert APPLY_BUTTON not in [b.label for b in at.button]


def test_no_preview_or_write_on_page_load(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    before = _snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # Nothing is parsed or rendered until the user clicks Parse / Preview.
    assert not _has_preview_df(at)
    assert not any("parsed line(s)" in m.value for m in at.markdown)
    # And the page load writes nothing (docs/DEVELOPMENT_NOTES.md §11).
    assert before == _snapshot()


def test_no_apply_button_before_preview(isolated_db):
    """The Apply write button only appears once a preview has been computed."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert APPLY_BUTTON not in [b.label for b in at.button]


def test_apply_button_appears_after_preview_with_eligible_rows(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    _paste_area(at).set_value("A Fighter vs B Fighter")
    _parse_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # An eligible preview surfaces the Apply button plus the opt-in checkbox.
    assert APPLY_BUTTON in [b.label for b in at.button]
    assert any("already grouped" in c.label for c in at.checkbox)


def test_no_apply_button_when_no_eligible_rows(isolated_db):
    """Zero-eligible preview shows a 'nothing to apply' caption, no button."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    _paste_area(at).set_value("Nobody Here vs Ghost Person")
    _parse_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert APPLY_BUTTON not in [b.label for b in at.button]
    captions = " ".join(c.value for c in at.caption)
    assert "Nothing eligible to apply" in captions


def test_parse_preview_renders_eligible_pair(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    _paste_area(at).set_value("A Fighter vs B Fighter")
    _parse_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    df = _preview_df(at)
    row = _preview_row(df, "A Fighter vs B Fighter")
    assert row["Matched Fighter 1"] == "A Fighter"
    assert row["Matched Fighter 2"] == "B Fighter"
    assert row["Status"] == "exact"
    assert row["Eligible"] == "yes"
    assert row["Blocked Reason"] == ""

    # Summary line reflects the eligible count.
    assert any(
        "parsed line(s)" in m.value and "1 eligible" in m.value for m in at.markdown
    )


def test_parse_preview_shows_unmatched_pasted_fighter_as_blocked(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    _paste_area(at).set_value("A Fighter vs Nobody Here")
    _parse_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    df = _preview_df(at)
    row = _preview_row(df, "A Fighter vs Nobody Here")
    assert row["Eligible"] == "no"
    assert row["Blocked Reason"] == "name 2 unmatched"
    assert row["Matched Fighter 2"] == "—"


def test_parse_preview_shows_duplicate_pasted_fighter_as_blocked(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )

    at = _open_page()
    _paste_area(at).set_value("A Fighter vs B Fighter\nA Fighter vs C Fighter")
    _parse_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    df = _preview_df(at)
    # A Fighter appears in both pasted rows → all such rows are blocked.
    r1 = _preview_row(df, "A Fighter vs B Fighter")
    r2 = _preview_row(df, "A Fighter vs C Fighter")
    assert r1["Eligible"] == "no"
    assert r2["Eligible"] == "no"
    assert r1["Blocked Reason"] == "fighter appears in another pasted row"
    assert r2["Blocked Reason"] == "fighter appears in another pasted row"


def test_parse_preview_does_not_mutate_db(isolated_db):
    """Parse / Preview is pure: it renders but writes nothing (docs/DEVELOPMENT_NOTES.md §11)."""
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )

    before = _snapshot()
    at = _open_page()
    _paste_area(at).set_value("A Fighter vs B Fighter\nC Fighter vs Nobody Here")
    _parse_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # The preview rendered (a row exists) yet no fight group / fighter row changed.
    assert _has_preview_df(at)
    assert before == _snapshot()


# ---------------------------------------------------------------------------
# Region D — Apply Valid Pairings write action (A4.4, design §9.6 / §9.9 10–14)
#
# The only write path in Region D. Each test parses a card (read-only), then
# clicks Apply and pins the persisted fight_groups state, the result summary,
# and the post-apply Region A refresh.
# ---------------------------------------------------------------------------


def _created_status_and_rounds(slate_id: int) -> tuple[set, set]:
    conn = get_connection()
    try:
        statuses = {
            r[0]
            for r in conn.execute(
                "SELECT status FROM fight_groups WHERE slate_id = ?", (slate_id,)
            ).fetchall()
        }
        rounds = {
            r[0]
            for r in conn.execute(
                "SELECT scheduled_rounds FROM fight_groups WHERE slate_id = ?",
                (slate_id,),
            ).fetchall()
        }
        return statuses, rounds
    finally:
        conn.close()


def test_apply_creates_groups_for_eligible_rows_only(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )

    at = _open_page()
    _paste_area(at).set_value("A Fighter vs B Fighter\nC Fighter vs D Fighter")
    _parse_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _group_rows(slate_id) == []  # Parse / Preview wrote nothing

    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = [tuple(r) for r in _group_rows(slate_id)]
    assert ("A Fighter", "B Fighter", 3) in rows
    assert ("C Fighter", "D Fighter", 3) in rows
    assert len(rows) == 2

    # Created groups default to 3 rounds / unconfirmed (design §9.7).
    statuses, rounds = _created_status_and_rounds(slate_id)
    assert statuses == {"unconfirmed"}
    assert rounds == {3}

    # Region A coverage refreshes on the post-apply rerun.
    _assert_metrics(at, total=4, grouped=4, ungrouped=0, groups=2, confirmed=0)

    success = " ".join(s.value for s in at.success)
    assert "Applied 2 new fight group(s)" in success
    # Reminder to set 5-round bouts by hand (rounds are never inferred).
    assert "5-round" in success


def test_apply_skips_blocked_rows_unmatched_selfpair_parseerror(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )

    at = _open_page()
    _paste_area(at).set_value(
        "A Fighter vs B Fighter\n"   # eligible
        "C Fighter vs Nobody Here\n"  # name 2 unmatched
        "C Fighter vs c fighter\n"    # self-pair
        "Just One Name"               # parse error
    )
    _parse_button(at).click().run()
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # Only the eligible pair was created; the three blocked rows were not.
    assert [tuple(r) for r in _group_rows(slate_id)] == [("A Fighter", "B Fighter", 3)]
    captions = " ".join(c.value for c in at.caption)
    assert "were not eligible" in captions


def test_apply_skips_duplicate_fighter_across_rows(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [
            ("A Fighter", 9000),
            ("B Fighter", 8500),
            ("C Fighter", 8000),
            ("D Fighter", 7500),
            ("E Fighter", 7000),
        ],
    )

    at = _open_page()
    _paste_area(at).set_value(
        "A Fighter vs B Fighter\n"   # eligible
        "C Fighter vs D Fighter\n"   # C duplicated below → both C rows blocked
        "C Fighter vs E Fighter"
    )
    _parse_button(at).click().run()
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert [tuple(r) for r in _group_rows(slate_id)] == [("A Fighter", "B Fighter", 3)]


def test_apply_skips_already_grouped_fighter_by_default(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )
    _seed_group(slate_id, "A Fighter", "C Fighter")  # A already grouped

    at = _open_page()
    _paste_area(at).set_value("A Fighter vs B Fighter")
    _parse_button(at).click().run()
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # No new group; the pre-existing A–C group is untouched (no overwrite).
    assert [tuple(r) for r in _group_rows(slate_id)] == [("A Fighter", "C Fighter", 3)]

    infos = " ".join(i.value for i in at.info)
    assert "No new fight groups created" in infos
    assert "already grouped" in infos


def test_apply_optin_creates_second_group_for_already_grouped(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )
    _seed_group(slate_id, "A Fighter", "C Fighter")  # A grouped with C

    at = _open_page()
    _paste_area(at).set_value("A Fighter vs B Fighter")
    _parse_button(at).click().run()

    # Opt-in OFF → nothing created.
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len([tuple(r) for r in _group_rows(slate_id)]) == 1

    # Opt-in ON → a second group for A is created; A becomes a duplicate.
    _optin_checkbox(at).set_value(True)
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rows = [tuple(r) for r in _group_rows(slate_id)]
    assert ("A Fighter", "B Fighter", 3) in rows
    assert ("A Fighter", "C Fighter", 3) in rows
    assert len(rows) == 2

    df = _roster_df_any(at)
    assert _row(df, "A Fighter")["Coverage"] == "duplicate"


def test_apply_is_idempotent_on_repeat_click(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )

    at = _open_page()
    _paste_area(at).set_value("A Fighter vs B Fighter\nC Fighter vs D Fighter")
    _parse_button(at).click().run()

    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len([tuple(r) for r in _group_rows(slate_id)]) == 2

    # A second click on the same preview creates nothing — the fighters are now
    # grouped, so every row is skipped and no row is duplicated.
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len([tuple(r) for r in _group_rows(slate_id)]) == 2
    assert "No new fight groups created" in " ".join(i.value for i in at.info)


def test_apply_only_mutates_fight_groups_table(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )

    at = _open_page()
    _paste_area(at).set_value("A Fighter vs B Fighter")
    _parse_button(at).click().run()

    before = _snapshot()
    _apply_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    after = _snapshot()

    # Only fight_groups changed: exactly one new row, every other table identical.
    assert after["fighters"] == before["fighters"]
    assert after["slates"] == before["slates"]
    assert after["manual_match_overrides"] == before["manual_match_overrides"]
    new_rows = [r for r in after["fight_groups"] if r not in before["fight_groups"]]
    assert len(new_rows) == 1
    assert len(after["fight_groups"]) == len(before["fight_groups"]) + 1
    _id, _sid, f1, f2, rounds, status = new_rows[0]
    assert (f1, f2, rounds, status) == ("A Fighter", "B Fighter", 3, "unconfirmed")


def test_editing_text_after_preview_withdraws_stale_apply(isolated_db):
    """A stale preview (text changed since Parse) is never applied."""
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )

    at = _open_page()
    _paste_area(at).set_value("A Fighter vs B Fighter")
    _parse_button(at).click().run()
    assert APPLY_BUTTON in [b.label for b in at.button]

    # Edit the text without re-parsing → the Apply affordance is withdrawn.
    _paste_area(at).set_value("A Fighter vs C Fighter")
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert APPLY_BUTTON not in [b.label for b in at.button]
    captions = " ".join(c.value for c in at.caption)
    assert "changed since the last preview" in captions
    assert _group_rows(slate_id) == []


# ---------------------------------------------------------------------------
# Pack 2 — status banner, section order, fight-card table, advanced collapse
# ---------------------------------------------------------------------------


def _subheaders(at: AppTest) -> list[str]:
    return [s.value for s in at.subheader]


def test_banner_complete_state_for_clean_confirmed_card(isolated_db):
    """A clean, fully-grouped, fully-confirmed card reports complete and
    points to Review Odds then Manual Review."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    _seed_group(slate_id, "A Fighter", "B Fighter", status="confirmed")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    success = " ".join(s.value for s in at.success)
    assert "Fight card complete" in success, success
    assert "Review Odds" in success
    assert "Manual Review" in success


def test_banner_needs_review_when_a_fighter_is_duplicated(isolated_db):
    """A fighter assigned to two groups makes the card unclean — the banner
    says needs review rather than complete/in-progress."""
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id, [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000)]
    )
    _seed_group(slate_id, "A Fighter", "B Fighter", status="confirmed")
    _seed_group(slate_id, "A Fighter", "C Fighter", status="confirmed")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    warnings = " ".join(w.value for w in at.warning)
    assert "Fight card needs review" in warnings, warnings
    assert "more than one group" in warnings


def test_banner_in_progress_points_to_dk_game_info(isolated_db):
    """With unapplied DK Game Info suggestions and no groups, the banner's
    next step is to apply the suggestions, not manual add."""
    slate_id = _seed_slate()
    # The shared _seed_fighters gives both fighters the same Game Info string,
    # so they form one ready (ungrouped) suggested pair.
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    infos = " ".join(i.value for i in at.info)
    assert "Fight card in progress" in infos, infos
    assert "apply 1 DK Game Info suggestion(s)" in infos, infos


def test_all_dk_game_info_pairings_already_applied_message(isolated_db):
    """When every suggested pair already resolves to a group, Region E states
    the primary workflow is done."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    # Unconfirmed so the card is not "complete", but the suggested pair is
    # already grouped → "all applied".
    _seed_group(slate_id, "A Fighter", "B Fighter", status="unconfirmed")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    success = " ".join(s.value for s in at.success)
    assert "All DK Game Info pairings are already applied." in success, success


def test_section_order_suggestions_then_roster_then_card_then_advanced(isolated_db):
    """DK Game Info suggestions are the first actionable section, roster is
    supporting context after it, the fight-card table is above the collapsed
    advanced tools."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    _seed_group(slate_id, "A Fighter", "B Fighter")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    subs = _subheaders(at)
    for label in (
        "Suggested DK pairings",
        "Slate roster & coverage",
        "Fight card",
        "Advanced manual corrections",
    ):
        assert label in subs, subs
    assert (
        subs.index("Suggested DK pairings")
        < subs.index("Slate roster & coverage")
        < subs.index("Fight card")
        < subs.index("Advanced manual corrections")
    ), subs


def test_fight_card_table_shows_status_rounds_and_confirm_toggle(isolated_db):
    """The compact fight-card table renders per-fighter status columns, the
    rounds cell, and preserves the confirm toggle (key + label)."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    gid = _seed_group(
        slate_id, "A Fighter", "B Fighter", scheduled_rounds=5, status="unconfirmed"
    )

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert "Fight card" in _subheaders(at)

    captions = " ".join(c.value for c in at.caption)
    assert "F1 status" in captions
    assert "F2 status" in captions

    # The 5-round cell and the resolved per-fighter status render as text.
    rendered = " ".join(m.value for m in at.markdown)
    assert FIVE_ROUND_CELL in rendered
    assert "active" in rendered  # Fighter Status category for an imported fighter

    toggle = [b for b in at.button if b.key == f"toggle_{gid}"]
    assert len(toggle) == 1
    assert toggle[0].label == "Mark confirmed"


def test_fight_card_confirm_toggle_writes_status(isolated_db):
    """The fight-card table's confirm toggle still persists via the
    repository (write path unchanged from the old vertical list)."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    gid = _seed_group(
        slate_id, "A Fighter", "B Fighter", scheduled_rounds=3, status="unconfirmed"
    )

    at = _open_page()
    toggle = [b for b in at.button if b.key == f"toggle_{gid}"][0]
    toggle.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    conn = get_connection()
    try:
        status = conn.execute(
            "SELECT status FROM fight_groups WHERE id = ?", (gid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "confirmed"


def test_game_info_reference_column_in_suggestions_preview(isolated_db):
    """The suggestions preview surfaces the verbatim Game Info string so the
    user can verify the imported bout."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    gi_df = [
        d.value
        for d in at.dataframe
        if {"Fighter 1", "Fighter 2", "Status"} <= set(d.value.columns)
    ]
    assert len(gi_df) == 1, [list(d.value.columns) for d in at.dataframe]
    assert "Game Info (reference)" in list(gi_df[0].columns)
    # _seed_fighters seeds this exact Game Info string for both fighters.
    assert "Jon Doe@Jane Roe 05/22/2026" in set(gi_df[0]["Game Info (reference)"])


def test_advanced_add_form_collapsed_when_card_complete(isolated_db):
    """A complete card keeps the manual add form available but does not
    visually invite adds; the form widgets remain reachable for AppTest."""
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    _seed_group(slate_id, "A Fighter", "B Fighter", status="confirmed")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # Advanced section present; the manual add form is still defined (its
    # widgets are reachable even when the expander is collapsed).
    assert "Advanced manual corrections" in _subheaders(at)
    assert "Save fight group" in [b.label for b in at.button]
    assert "Fighter 1" in [s.label for s in at.selectbox]


# ---------------------------------------------------------------------------
# Fight-card quick actions — "Confirm all groups" + "Set 5-round main event"
#
# Both are explicit one-click writes run in on_click callbacks: confirm-all
# marks every unconfirmed group confirmed (status only, never rounds, never a
# new group); the 5-round selector sets exactly the chosen group to 5 rounds.
# Neither runs on page load (docs/DEVELOPMENT_NOTES.md §11).
# ---------------------------------------------------------------------------

CONFIRM_ALL_PREFIX = "Confirm all groups"
SET_FIVE_LABEL = "Set selected fight to 5 rounds"
FIVE_ROUND_SELECT_LABEL = "Set 5-round main event"


def _confirm_all_button(at: AppTest):
    matched = [b for b in at.button if b.label.startswith(CONFIRM_ALL_PREFIX)]
    assert len(matched) == 1, (
        f"Expected one confirm-all button; saw {[b.label for b in at.button]}"
    )
    return matched[0]


def _five_round_select(at: AppTest):
    matched = [s for s in at.selectbox if s.label == FIVE_ROUND_SELECT_LABEL]
    assert len(matched) == 1, (
        f"Expected one {FIVE_ROUND_SELECT_LABEL!r} selectbox; "
        f"saw {[s.label for s in at.selectbox]}"
    )
    return matched[0]


def _set_five_button(at: AppTest):
    matched = [b for b in at.button if b.label == SET_FIVE_LABEL]
    assert len(matched) == 1, "Expected the 'Set selected fight to 5 rounds' button"
    return matched[0]


def _statuses(slate_id: int) -> dict:
    conn = get_connection()
    try:
        return {
            int(r[0]): r[1]
            for r in conn.execute(
                "SELECT id, status FROM fight_groups WHERE slate_id = ?", (slate_id,)
            ).fetchall()
        }
    finally:
        conn.close()


def _rounds(slate_id: int) -> dict:
    conn = get_connection()
    try:
        return {
            int(r[0]): int(r[1])
            for r in conn.execute(
                "SELECT id, scheduled_rounds FROM fight_groups WHERE slate_id = ?",
                (slate_id,),
            ).fetchall()
        }
    finally:
        conn.close()


def test_confirm_all_button_shown_when_unconfirmed_groups_exist(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    _seed_group(slate_id, "A Fighter", "B Fighter", status="unconfirmed")
    _seed_group(slate_id, "C Fighter", "D Fighter", status="unconfirmed")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    btn = _confirm_all_button(at)
    # Label carries the unconfirmed count.
    assert "(2)" in btn.label


def test_confirm_all_click_confirms_all_groups_with_success(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    g1 = _seed_group(slate_id, "A Fighter", "B Fighter", status="unconfirmed")
    g2 = _seed_group(slate_id, "C Fighter", "D Fighter", status="unconfirmed")

    at = _open_page()
    _confirm_all_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    statuses = _statuses(slate_id)
    assert statuses[g1] == "confirmed"
    assert statuses[g2] == "confirmed"

    success = " ".join(s.value for s in at.success)
    assert "Confirmed 2 fight group(s)." in success
    # After confirming, the button is gone and the "all confirmed" caption shows.
    assert not any(b.label.startswith(CONFIRM_ALL_PREFIX) for b in at.button)
    assert "All fight groups are confirmed." in " ".join(c.value for c in at.caption)


def test_confirm_all_does_not_alter_scheduled_rounds(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    g1 = _seed_group(
        slate_id, "A Fighter", "B Fighter", scheduled_rounds=5, status="unconfirmed"
    )
    g2 = _seed_group(
        slate_id, "C Fighter", "D Fighter", scheduled_rounds=3, status="unconfirmed"
    )

    at = _open_page()
    _confirm_all_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rounds = _rounds(slate_id)
    assert rounds[g1] == 5  # unchanged by confirm-all
    assert rounds[g2] == 3


def test_confirm_all_button_hidden_when_all_confirmed(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    _seed_group(slate_id, "A Fighter", "B Fighter", status="confirmed")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert not any(b.label.startswith(CONFIRM_ALL_PREFIX) for b in at.button)
    assert "All fight groups are confirmed." in " ".join(c.value for c in at.caption)


def test_five_round_selector_appears_when_groups_exist(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])
    _seed_group(slate_id, "A Fighter", "B Fighter")

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    sel = _five_round_select(at)
    # AppTest exposes the format_func'd display labels via .options; they read
    # like "F1 vs F2".
    assert "A Fighter vs B Fighter" in sel.options
    assert SET_FIVE_LABEL in [b.label for b in at.button]


def test_five_round_selector_hidden_when_no_groups(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(slate_id, [("A Fighter", 9000), ("B Fighter", 8500)])

    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert FIVE_ROUND_SELECT_LABEL not in [s.label for s in at.selectbox]
    assert SET_FIVE_LABEL not in [b.label for b in at.button]


def test_set_five_round_changes_only_selected_group(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    g1 = _seed_group(slate_id, "A Fighter", "B Fighter", scheduled_rounds=3)
    g2 = _seed_group(slate_id, "C Fighter", "D Fighter", scheduled_rounds=3)

    at = _open_page()
    _five_round_select(at).set_value(g2)  # pick the second bout as the main event
    _set_five_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    rounds = _rounds(slate_id)
    assert rounds[g2] == 5  # selected group set to 5
    assert rounds[g1] == 3  # the other stays at 3

    success = " ".join(s.value for s in at.success)
    assert "C Fighter vs D Fighter" in success
    assert "5 rounds" in success


def test_set_five_round_preserves_statuses(isolated_db):
    """Setting rounds never confirms/unconfirms a group."""
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    g1 = _seed_group(slate_id, "A Fighter", "B Fighter", status="confirmed")
    g2 = _seed_group(slate_id, "C Fighter", "D Fighter", status="unconfirmed")

    at = _open_page()
    _five_round_select(at).set_value(g1)
    _set_five_button(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    statuses = _statuses(slate_id)
    assert statuses[g1] == "confirmed"  # unchanged
    assert statuses[g2] == "unconfirmed"


def test_quick_actions_do_not_write_on_page_load(isolated_db):
    """Rendering the confirm-all button + 5-round selector writes nothing."""
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000), ("B Fighter", 8500), ("C Fighter", 8000), ("D Fighter", 7500)],
    )
    _seed_group(slate_id, "A Fighter", "B Fighter", scheduled_rounds=3, status="unconfirmed")
    _seed_group(slate_id, "C Fighter", "D Fighter", scheduled_rounds=5, status="unconfirmed")

    before = _snapshot()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]
    # Controls rendered (button + selector present) but the DB is unchanged.
    assert any(b.label.startswith(CONFIRM_ALL_PREFIX) for b in at.button)
    assert FIVE_ROUND_SELECT_LABEL in [s.label for s in at.selectbox]
    assert before == _snapshot()
