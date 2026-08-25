"""Tests for the v0 FightGroupRepository skeleton."""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    FightGroupRepository,
    SlateRepository,
)
from src.db.schema import apply_schema


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def slate_id(conn):
    return SlateRepository(conn).create(event_name="UFC 999").id


def test_create_fight_group_minimal(conn, slate_id):
    repo = FightGroupRepository(conn)
    rec = repo.create(
        slate_id=slate_id,
        fighter_1_name="Jon Jones",
        fighter_2_name="Stipe Miocic",
    )
    assert rec.id > 0
    assert rec.slate_id == slate_id
    assert rec.fighter_1_name == "Jon Jones"
    assert rec.fighter_2_name == "Stipe Miocic"
    assert rec.scheduled_rounds == 3
    assert rec.status == "unconfirmed"
    assert rec.created_at


def test_create_fight_group_five_rounds(conn, slate_id):
    rec = FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="A",
        fighter_2_name="B",
        scheduled_rounds=5,
    )
    assert rec.scheduled_rounds == 5


def test_create_requires_both_names(conn, slate_id):
    repo = FightGroupRepository(conn)
    with pytest.raises(ValueError):
        repo.create(slate_id=slate_id, fighter_1_name="", fighter_2_name="B")
    with pytest.raises(ValueError):
        repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="   ")


def test_create_rejects_same_fighter(conn, slate_id):
    repo = FightGroupRepository(conn)
    with pytest.raises(ValueError):
        repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="a")


def test_create_rejects_invalid_rounds(conn, slate_id):
    repo = FightGroupRepository(conn)
    with pytest.raises(ValueError):
        repo.create(
            slate_id=slate_id,
            fighter_1_name="A",
            fighter_2_name="B",
            scheduled_rounds=4,
        )


def test_list_for_slate_scopes_results(conn):
    slate_repo = SlateRepository(conn)
    s1 = slate_repo.create(event_name="UFC 1").id
    s2 = slate_repo.create(event_name="UFC 2").id
    repo = FightGroupRepository(conn)
    repo.create(slate_id=s1, fighter_1_name="A", fighter_2_name="B")
    repo.create(slate_id=s1, fighter_1_name="C", fighter_2_name="D")
    repo.create(slate_id=s2, fighter_1_name="E", fighter_2_name="F")

    s1_rows = repo.list_for_slate(s1)
    assert [r.fighter_1_name for r in s1_rows] == ["A", "C"]
    s2_rows = repo.list_for_slate(s2)
    assert len(s2_rows) == 1 and s2_rows[0].fighter_1_name == "E"


def test_list_for_slate_empty(conn, slate_id):
    assert FightGroupRepository(conn).list_for_slate(slate_id) == []


def test_rejects_reversed_duplicate_pair(conn, slate_id):
    repo = FightGroupRepository(conn)
    repo.create(slate_id=slate_id, fighter_1_name="Jon Jones", fighter_2_name="Stipe Miocic")
    with pytest.raises(ValueError):
        repo.create(
            slate_id=slate_id,
            fighter_1_name="  stipe miocic ",
            fighter_2_name="JON JONES",
        )


def test_update_status_to_confirmed(conn, slate_id):
    repo = FightGroupRepository(conn)
    rec = repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    assert rec.status == "unconfirmed"
    updated = repo.update_status(rec.id, "confirmed")
    assert updated.id == rec.id
    assert updated.status == "confirmed"


def test_update_status_back_to_unconfirmed(conn, slate_id):
    repo = FightGroupRepository(conn)
    rec = repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    repo.update_status(rec.id, "confirmed")
    updated = repo.update_status(rec.id, "unconfirmed")
    assert updated.status == "unconfirmed"


def test_update_status_rejects_invalid(conn, slate_id):
    repo = FightGroupRepository(conn)
    rec = repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    with pytest.raises(ValueError):
        repo.update_status(rec.id, "maybe")
    with pytest.raises(ValueError):
        repo.update_status(rec.id, "")


def test_list_for_slate_reflects_updated_status(conn, slate_id):
    repo = FightGroupRepository(conn)
    r1 = repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    r2 = repo.create(slate_id=slate_id, fighter_1_name="C", fighter_2_name="D")
    repo.update_status(r1.id, "confirmed")
    rows = {r.id: r.status for r in repo.list_for_slate(slate_id)}
    assert rows[r1.id] == "confirmed"
    assert rows[r2.id] == "unconfirmed"


def test_unique_pair_per_slate(conn, slate_id):
    repo = FightGroupRepository(conn)
    repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")


# ---------------------------------------------------------------------------
# update_scheduled_rounds — set one group's rounds (5-round main-event control)
# ---------------------------------------------------------------------------


def test_update_scheduled_rounds_to_five(conn, slate_id):
    repo = FightGroupRepository(conn)
    rec = repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    assert rec.scheduled_rounds == 3
    updated = repo.update_scheduled_rounds(rec.id, 5)
    assert updated.id == rec.id
    assert updated.scheduled_rounds == 5
    # Status is preserved — only rounds change.
    assert updated.status == rec.status


def test_update_scheduled_rounds_does_not_touch_status_or_names(conn, slate_id):
    repo = FightGroupRepository(conn)
    rec = repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    repo.update_status(rec.id, "confirmed")
    updated = repo.update_scheduled_rounds(rec.id, 5)
    assert updated.status == "confirmed"
    assert updated.fighter_1_name == "A"
    assert updated.fighter_2_name == "B"


def test_update_scheduled_rounds_only_affects_target_group(conn, slate_id):
    repo = FightGroupRepository(conn)
    r1 = repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    r2 = repo.create(slate_id=slate_id, fighter_1_name="C", fighter_2_name="D")
    repo.update_scheduled_rounds(r2.id, 5)
    rows = {r.id: r.scheduled_rounds for r in repo.list_for_slate(slate_id)}
    assert rows[r1.id] == 3  # untouched
    assert rows[r2.id] == 5


def test_update_scheduled_rounds_rejects_invalid(conn, slate_id):
    repo = FightGroupRepository(conn)
    rec = repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    with pytest.raises(ValueError):
        repo.update_scheduled_rounds(rec.id, 4)


def test_update_scheduled_rounds_missing_group_raises(conn, slate_id):
    repo = FightGroupRepository(conn)
    with pytest.raises(ValueError):
        repo.update_scheduled_rounds(999999, 5)


# ---------------------------------------------------------------------------
# confirm_all_for_slate — bulk confirm (Fight Groups "confirm all groups")
# ---------------------------------------------------------------------------


def test_confirm_all_for_slate_confirms_unconfirmed_and_counts(conn, slate_id):
    repo = FightGroupRepository(conn)
    r1 = repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    r2 = repo.create(slate_id=slate_id, fighter_1_name="C", fighter_2_name="D")
    r3 = repo.create(slate_id=slate_id, fighter_1_name="E", fighter_2_name="F")
    repo.update_status(r2.id, "confirmed")  # already confirmed

    count = repo.confirm_all_for_slate(slate_id)
    # Only the two unconfirmed groups were updated.
    assert count == 2
    statuses = {r.id: r.status for r in repo.list_for_slate(slate_id)}
    assert statuses[r1.id] == "confirmed"
    assert statuses[r2.id] == "confirmed"
    assert statuses[r3.id] == "confirmed"


def test_confirm_all_for_slate_does_not_change_scheduled_rounds(conn, slate_id):
    repo = FightGroupRepository(conn)
    r1 = repo.create(
        slate_id=slate_id, fighter_1_name="A", fighter_2_name="B", scheduled_rounds=5
    )
    r2 = repo.create(
        slate_id=slate_id, fighter_1_name="C", fighter_2_name="D", scheduled_rounds=3
    )
    repo.confirm_all_for_slate(slate_id)
    rounds = {r.id: r.scheduled_rounds for r in repo.list_for_slate(slate_id)}
    assert rounds[r1.id] == 5  # unchanged
    assert rounds[r2.id] == 3


def test_confirm_all_for_slate_idempotent(conn, slate_id):
    repo = FightGroupRepository(conn)
    repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    assert repo.confirm_all_for_slate(slate_id) == 1
    # Second run: nothing left to confirm.
    assert repo.confirm_all_for_slate(slate_id) == 0


def test_confirm_all_for_slate_is_slate_scoped(conn):
    repo = FightGroupRepository(conn)
    s1 = SlateRepository(conn).create(event_name="UFC A").id
    s2 = SlateRepository(conn).create(event_name="UFC B").id
    repo.create(slate_id=s1, fighter_1_name="A", fighter_2_name="B")
    r2 = repo.create(slate_id=s2, fighter_1_name="C", fighter_2_name="D")
    repo.confirm_all_for_slate(s1)
    # The other slate's group is untouched.
    assert {r.id: r.status for r in repo.list_for_slate(s2)}[r2.id] == "unconfirmed"


def test_confirm_all_does_not_create_groups(conn, slate_id):
    repo = FightGroupRepository(conn)
    repo.create(slate_id=slate_id, fighter_1_name="A", fighter_2_name="B")
    repo.confirm_all_for_slate(slate_id)
    assert len(repo.list_for_slate(slate_id)) == 1
