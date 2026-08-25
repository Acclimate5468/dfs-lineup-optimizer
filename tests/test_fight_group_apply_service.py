"""Unit tests for the reusable fight-group apply service.

Covers docs/FIGHT_GROUP_APPLY_SERVICE_DESIGN.md §3 / §7: the Streamlit-free
``src/slate/fight_group_apply_service.py`` core extracted from the Fight Groups
page. These drive the service directly against an isolated temp SQLite DB
through the repositories — no AppTest, no Streamlit — and pin the invariants the
page regression tests (``test_fight_groups_game_info_region.py`` /
``test_fight_groups_page.py``) exercise through the UI:

  - DK Game Info pairings create 3-round, ``unconfirmed`` groups; the
    auto-detected main event (latest start) is created at 5 rounds.
  - Existing/already-grouped pairings are skipped; a re-call is idempotent.
  - ``include_grouped=True`` opts a second group in; ``auto_set_main_event=False``
    leaves every group at 3 rounds.
  - ``create_groups_for_pairs`` gating (exists / grouped / errors) and
    ``compute_apply_context`` join match the page's former inline logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.db.connection import get_connection
from src.db.repositories import (
    FighterRepository,
    FightGroupRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.dk_salary_importer import ParsedSalaryRow
from src.slate.fight_group_apply_service import (
    GameInfoApplyResult,
    GroupApplyOutcome,
    apply_game_info_pairings,
    compute_apply_context,
    create_groups_for_pairs,
)
from src.utils.text_cleaning import normalize_name

# Synthetic DK-shaped Game Info strings — both rows of a bout share the
# byte-identical value (design §1.1). Test data only; never real feed data.
GI_AB = "A Fighter@B Fighter 05/22/2026 06:00PM ET"
GI_CD = "C Fighter@D Fighter 05/22/2026 09:00PM ET"
GI_EF = "E Fighter@F Fighter 05/23/2026 10:00PM ET"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "fight_group_apply_service.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _seed_slate(name: str = "UFC 800") -> int:
    conn = get_connection()
    try:
        apply_schema(conn)
        return SlateRepository(conn).create(event_name=name).id
    finally:
        conn.close()


def _seed_fighters(slate_id: int, rows: list[tuple[str, int, str | None]]) -> None:
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


def _active_roster(slate_id: int) -> list:
    conn = get_connection()
    try:
        return [
            f
            for f in FighterRepository(conn).list_for_slate(slate_id)
            if f.status == "active"
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# apply_game_info_pairings — Region E / Build entry point (design §3.3)
# ---------------------------------------------------------------------------


def test_apply_game_info_creates_unconfirmed_groups_with_main_event(isolated_db):
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

    conn = get_connection()
    try:
        result = apply_game_info_pairings(conn, slate_id)
    finally:
        conn.close()

    assert isinstance(result, GameInfoApplyResult)
    assert result.slate_id == slate_id
    assert result.eligible == 2
    # C/D (9:00 PM) is the latest start → auto-detected main event at 5 rounds.
    assert result.outcome.five_round == "C Fighter vs D Fighter"
    rows = _group_rows(slate_id)
    assert ("A Fighter", "B Fighter", 3, "unconfirmed") in rows
    assert ("C Fighter", "D Fighter", 5, "unconfirmed") in rows
    assert len(rows) == 2


def test_apply_game_info_is_idempotent_on_recall(isolated_db):
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

    conn = get_connection()
    try:
        apply_game_info_pairings(conn, slate_id)
        assert len(_group_rows(slate_id)) == 2
        # A second call under unchanged roster creates zero groups.
        result = apply_game_info_pairings(conn, slate_id)
    finally:
        conn.close()

    assert result.outcome.created == ()
    assert len(_group_rows(slate_id)) == 2


def test_apply_game_info_skips_already_grouped_by_default(isolated_db):
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
    _seed_group(slate_id, "A Fighter", "Z Ghost")  # A already grouped

    conn = get_connection()
    try:
        result = apply_game_info_pairings(conn, slate_id)
    finally:
        conn.close()

    rows = _group_rows(slate_id)
    assert ("C Fighter", "D Fighter", 5, "unconfirmed") in rows
    assert ("A Fighter", "Z Ghost", 3, "unconfirmed") in rows
    assert not any(r[0] == "A Fighter" and r[1] == "B Fighter" for r in rows)
    assert any("already grouped" in s for s in result.outcome.skipped_grouped)


def test_apply_game_info_include_grouped_adds_second_group(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [("A Fighter", 9000, GI_AB), ("B Fighter", 8500, GI_AB)],
    )
    _seed_group(slate_id, "A Fighter", "Z Ghost")  # A already grouped

    conn = get_connection()
    try:
        result = apply_game_info_pairings(conn, slate_id, include_grouped=True)
    finally:
        conn.close()

    rows = _group_rows(slate_id)
    assert ("A Fighter", "B Fighter", 3, "unconfirmed") in rows
    assert ("A Fighter", "Z Ghost", 3, "unconfirmed") in rows
    assert result.outcome.created == (("A Fighter", "B Fighter"),)


def test_apply_game_info_auto_set_main_event_false_keeps_all_three(isolated_db):
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

    conn = get_connection()
    try:
        result = apply_game_info_pairings(
            conn, slate_id, auto_set_main_event=False
        )
    finally:
        conn.close()

    assert result.outcome.five_round is None
    rows = _group_rows(slate_id)
    assert ("A Fighter", "B Fighter", 3, "unconfirmed") in rows
    assert ("C Fighter", "D Fighter", 3, "unconfirmed") in rows
    assert all(r[2] == 3 for r in rows)


def test_apply_game_info_no_main_event_when_start_times_absent(isolated_db):
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

    conn = get_connection()
    try:
        result = apply_game_info_pairings(conn, slate_id)
    finally:
        conn.close()

    assert result.outcome.five_round is None
    assert all(r[2] == 3 for r in _group_rows(slate_id))


def test_apply_game_info_empty_and_incomplete_rosters_create_nothing(isolated_db):
    slate_id = _seed_slate()
    # One lone fighter for a Game Info value (incomplete), one blank (uncovered).
    _seed_fighters(
        slate_id,
        [("Lone Wolf", 8200, GI_CD), ("Blank Bob", 7000, None)],
    )

    conn = get_connection()
    try:
        result = apply_game_info_pairings(conn, slate_id)
    finally:
        conn.close()

    assert result.eligible == 0
    assert result.outcome.created == ()
    assert _group_rows(slate_id) == []


def test_apply_game_info_anomaly_more_than_two_never_grouped(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [
            ("X One", 8000, GI_EF),
            ("Y Two", 7800, GI_EF),
            ("Z Three", 7600, GI_EF),
        ],
    )

    conn = get_connection()
    try:
        result = apply_game_info_pairings(conn, slate_id)
    finally:
        conn.close()

    assert result.eligible == 0
    assert _group_rows(slate_id) == []


# ---------------------------------------------------------------------------
# create_groups_for_pairs — the shared create loop (design §3.3)
# ---------------------------------------------------------------------------


def test_create_groups_for_pairs_skips_existing_and_self_errors(isolated_db):
    slate_id = _seed_slate()
    conn = get_connection()
    try:
        repo = FightGroupRepository(conn)
        outcome = create_groups_for_pairs(
            repo,
            slate_id,
            pairs=[
                ("A Fighter", "B Fighter"),  # created
                ("C Fighter", "D Fighter"),  # skipped — pair already saved
                ("E Fighter", "E Fighter"),  # error — names must differ
            ],
            grouped_norms=set(),
            existing_pairs={
                frozenset(
                    (normalize_name("C Fighter"), normalize_name("D Fighter"))
                )
            },
            include_grouped=False,
        )
    finally:
        conn.close()

    assert isinstance(outcome, GroupApplyOutcome)
    assert outcome.created == (("A Fighter", "B Fighter"),)
    assert outcome.skipped_exists == ("C Fighter vs D Fighter",)
    assert len(outcome.errors) == 1 and "E Fighter vs E Fighter" in outcome.errors[0]
    assert _group_rows(slate_id) == [("A Fighter", "B Fighter", 3, "unconfirmed")]


def test_create_groups_for_pairs_five_round_key_sets_five_rounds(isolated_db):
    slate_id = _seed_slate()
    conn = get_connection()
    try:
        repo = FightGroupRepository(conn)
        key = frozenset((normalize_name("C Fighter"), normalize_name("D Fighter")))
        outcome = create_groups_for_pairs(
            repo,
            slate_id,
            pairs=[("A Fighter", "B Fighter"), ("C Fighter", "D Fighter")],
            grouped_norms=set(),
            existing_pairs=set(),
            include_grouped=False,
            five_round_pair_key=key,
        )
    finally:
        conn.close()

    assert outcome.five_round == "C Fighter vs D Fighter"
    rows = _group_rows(slate_id)
    assert ("A Fighter", "B Fighter", 3, "unconfirmed") in rows
    assert ("C Fighter", "D Fighter", 5, "unconfirmed") in rows


# ---------------------------------------------------------------------------
# compute_apply_context — conflict-set construction (design §3.3)
# ---------------------------------------------------------------------------


def test_compute_apply_context_matches_active_roster_join(isolated_db):
    slate_id = _seed_slate()
    _seed_fighters(
        slate_id,
        [
            ("A Fighter", 9000, GI_AB),
            ("B Fighter", 8500, GI_AB),
            ("C Fighter", 8000, GI_CD),
        ],
    )
    # A grouped against an off-roster ghost; the ghost must NOT count as grouped.
    _seed_group(slate_id, "A Fighter", "Z Ghost")

    conn = get_connection()
    try:
        repo = FightGroupRepository(conn)
        roster_norms, grouped_norms, existing_pairs = compute_apply_context(
            repo, slate_id, _active_roster(slate_id)
        )
    finally:
        conn.close()

    assert roster_norms == {
        normalize_name("A Fighter"),
        normalize_name("B Fighter"),
        normalize_name("C Fighter"),
    }
    # Only the on-roster A counts as grouped; the off-roster ghost does not.
    assert grouped_norms == {normalize_name("A Fighter")}
    assert existing_pairs == {
        frozenset((normalize_name("A Fighter"), normalize_name("Z Ghost")))
    }
