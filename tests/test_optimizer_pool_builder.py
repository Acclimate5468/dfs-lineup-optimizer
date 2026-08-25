"""Tests for Optimizer v1 pool builder (Slice B.2).

Covers ``build_optimizer_pool`` in ``src/optimizer/pool_builder.py``
per ``docs/OPTIMIZER_V1_DESIGN.md`` §5.1 / §5.4. The pool builder is
read-only and downstream of Projection v1 (Phase C) plus the
fight-group + salary persistence layers; every test seeds those
upstream tables and then asserts the pool shape.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    FightGroupRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching_service import (
    recompute_and_replace_match_results,
)
from src.optimizer.pool_builder import (
    ExcludedFighter,
    OptimizerPool,
    OptimizerPoolEntry,
    build_optimizer_pool,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


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


def _save_odds_row(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    fighter_name_raw: str,
    american_odds: int = -150,
    captured_at: str = "2026-05-20T00:00:00Z",
):
    return OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source="manual",
        captured_at=captured_at,
    )


def _seed_eligible_fighter(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    name: str,
    salary: int,
    opponent_name: str,
    american_odds: int = -150,
    scheduled_rounds: int = 3,
) -> int:
    """Insert a fighter + opponent + fight group + auto_match odds row
    so the fighter's Projection v1 status comes out as ``"ok"``.

    Returns the eligible fighter's id. The opponent is left without
    odds so only the named fighter is eligible — this keeps test
    setup small for the single-eligible-side cases.
    """
    fid = _insert_fighter(conn, slate_id=slate_id, name=name, salary=salary)
    _insert_fighter(conn, slate_id=slate_id, name=opponent_name, salary=salary)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name=name,
        fighter_2_name=opponent_name,
        scheduled_rounds=scheduled_rounds,
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw=name,
        american_odds=american_odds,
    )
    recompute_and_replace_match_results(conn, slate_id)
    return fid


def _db_snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Snapshot every table the pool builder touches plus a few it
    must never write to. Used to assert read-only behavior end to end.
    """
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


# ---------------------------------------------------------------------------
# Empty / shape cases
# ---------------------------------------------------------------------------


def test_unknown_slate_returns_empty_pool(conn):
    pool = build_optimizer_pool(conn, 999_999)
    assert isinstance(pool, OptimizerPool)
    assert pool.slate_id == 999_999
    assert pool.entries == ()
    assert pool.same_fight_pairs == frozenset()
    assert pool.excluded == ()


def test_empty_slate_returns_empty_pool(conn, slate_id):
    pool = build_optimizer_pool(conn, slate_id)
    assert pool.slate_id == slate_id
    assert pool.entries == ()
    assert pool.same_fight_pairs == frozenset()
    assert pool.excluded == ()


# ---------------------------------------------------------------------------
# Eligible fighters become entries
# ---------------------------------------------------------------------------


def test_eligible_ok_fighter_becomes_an_entry(conn, slate_id):
    aldo_id = _seed_eligible_fighter(
        conn,
        slate_id=slate_id,
        name="Jose Aldo",
        salary=8800,
        opponent_name="Marlon Vera",
        american_odds=-150,
        scheduled_rounds=5,
    )

    pool = build_optimizer_pool(conn, slate_id)
    by_id = {e.fighter_id: e for e in pool.entries}
    aldo = by_id[aldo_id]
    assert isinstance(aldo, OptimizerPoolEntry)
    assert aldo.slate_id == slate_id
    assert aldo.dk_name == "Jose Aldo"
    assert aldo.dk_salary == 8800
    # 0.6 * 70 = 42 + five_round_bonus 7 = 49 (docs/DEVELOPMENT_NOTES.md §4 formula).
    assert aldo.default_projection == pytest.approx(49.0, abs=1e-9)
    assert isinstance(aldo.fight_group_id, int)


def test_entry_carries_fight_group_id_from_fight_groups(conn, slate_id):
    aldo_id = _seed_eligible_fighter(
        conn,
        slate_id=slate_id,
        name="Jose Aldo",
        salary=8800,
        opponent_name="Marlon Vera",
    )
    [group] = FightGroupRepository(conn).list_for_slate(slate_id)

    pool = build_optimizer_pool(conn, slate_id)
    aldo = next(e for e in pool.entries if e.fighter_id == aldo_id)
    assert aldo.fight_group_id == group.id


# ---------------------------------------------------------------------------
# Non-ok projection rows are excluded with reasons
# ---------------------------------------------------------------------------


def test_non_projectable_row_is_excluded_with_reason(conn, slate_id):
    """A fighter with no fight group is structurally non-projectable
    (PROJECTION_V1_DESIGN §5). Pool builder must drop and record the
    reason.
    """
    fid = _insert_fighter(
        conn, slate_id=slate_id, name="Lonely F", salary=8000
    )

    pool = build_optimizer_pool(conn, slate_id)
    assert pool.entries == ()
    assert len(pool.excluded) == 1
    ex = pool.excluded[0]
    assert isinstance(ex, ExcludedFighter)
    assert ex.fighter_id == fid
    assert ex.name == "Lonely F"
    assert "non_projectable" in ex.reason


def test_missing_inputs_row_is_excluded_with_reason(conn, slate_id):
    """Fight group present but no auto_match odds → missing_inputs
    with tag ``win_probability`` (PROJECTION_V1_DESIGN §5).
    """
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
    )

    pool = build_optimizer_pool(conn, slate_id)
    assert pool.entries == ()
    assert {ex.fighter_id for ex in pool.excluded} == {fid, fid + 1}
    aldo_ex = next(ex for ex in pool.excluded if ex.fighter_id == fid)
    assert "missing_inputs" in aldo_ex.reason
    assert "win_probability" in aldo_ex.reason


def test_excluded_does_not_include_eligible_fighters(conn, slate_id):
    aldo_id = _seed_eligible_fighter(
        conn,
        slate_id=slate_id,
        name="Jose Aldo",
        salary=8800,
        opponent_name="Marlon Vera",
    )

    pool = build_optimizer_pool(conn, slate_id)
    excluded_ids = {ex.fighter_id for ex in pool.excluded}
    assert aldo_id not in excluded_ids
    # Vera has no odds → missing_inputs.
    assert pool.excluded, "expected the opponent without odds to be excluded"


# ---------------------------------------------------------------------------
# fight_pairs are fighter_id pairs, not names
# ---------------------------------------------------------------------------


def test_same_fight_pair_uses_fighter_ids(conn, slate_id):
    """Both sides eligible → a single ``frozenset({aid, bid})`` entry."""
    aldo_id = _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", salary=8800
    )
    vera_id = _insert_fighter(
        conn, slate_id=slate_id, name="Marlon Vera", salary=8200
    )
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
    )
    _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Jose Aldo", american_odds=-150
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Marlon Vera",
        american_odds=+130,
    )
    recompute_and_replace_match_results(conn, slate_id)

    pool = build_optimizer_pool(conn, slate_id)
    assert pool.same_fight_pairs == frozenset({frozenset({aldo_id, vera_id})})

    # Pair elements are ints, not strings.
    [pair] = list(pool.same_fight_pairs)
    for x in pair:
        assert isinstance(x, int)


# ---------------------------------------------------------------------------
# Pair dropped when one side is ineligible
# ---------------------------------------------------------------------------


def test_pair_dropped_when_one_side_ineligible_but_other_remains_selectable(
    conn, slate_id
):
    """The eligible fighter must still appear in ``entries`` even when
    the opposing side of their fight is filtered out.
    """
    aldo_id = _seed_eligible_fighter(
        conn,
        slate_id=slate_id,
        name="Jose Aldo",
        salary=8800,
        opponent_name="Marlon Vera",
        american_odds=-150,
    )

    pool = build_optimizer_pool(conn, slate_id)
    entry_ids = {e.fighter_id for e in pool.entries}
    assert aldo_id in entry_ids
    # No pair is emitted because Vera has no odds → missing_inputs.
    assert pool.same_fight_pairs == frozenset()


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------


def test_name_normalization_handles_case_and_spacing(conn, slate_id):
    """Mirrors Phase B's conservative join: case-insensitive, whitespace
    folded. The fight-group fighter names appear with different
    casing / extra whitespace from the persisted DK fighter rows; the
    pair must still resolve to fighter ids.
    """
    aldo_id = _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", salary=8800
    )
    vera_id = _insert_fighter(
        conn, slate_id=slate_id, name="Marlon Vera", salary=8200
    )
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="  jose   aldo  ",
        fighter_2_name="MARLON VERA",
    )
    _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Jose Aldo", american_odds=-150
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Marlon Vera",
        american_odds=+130,
    )
    recompute_and_replace_match_results(conn, slate_id)

    pool = build_optimizer_pool(conn, slate_id)
    by_id = {e.fighter_id: e for e in pool.entries}
    assert set(by_id) == {aldo_id, vera_id}
    # Both entries carry a fight_group_id (the same one).
    assert by_id[aldo_id].fight_group_id == by_id[vera_id].fight_group_id
    assert by_id[aldo_id].fight_group_id is not None
    # And the same-fight pair set resolves to the two fighter ids.
    assert pool.same_fight_pairs == frozenset({frozenset({aldo_id, vera_id})})


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


def test_build_optimizer_pool_does_not_mutate_persisted_state(conn, slate_id):
    """Belt-and-braces: snapshot every relevant table before and after
    the call. The pool builder is strictly a read.
    """
    _seed_eligible_fighter(
        conn,
        slate_id=slate_id,
        name="Jose Aldo",
        salary=8800,
        opponent_name="Marlon Vera",
    )
    before = _db_snapshot(conn)

    build_optimizer_pool(conn, slate_id)
    # Twice — second call must also be a no-op writer.
    build_optimizer_pool(conn, slate_id)

    after = _db_snapshot(conn)
    assert before == after, (
        "build_optimizer_pool must not mutate persisted state"
    )


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------


def test_entries_ordering_is_deterministic_across_calls(conn, slate_id):
    """Two eligible fighters → entries come back in the same order on
    repeated calls. Pool builder inherits Projection v1's deterministic
    emission order (fighter name COLLATE NOCASE ASC, id ASC).
    """
    _insert_fighter(conn, slate_id=slate_id, name="Bravo", salary=8000)
    _insert_fighter(conn, slate_id=slate_id, name="alpha", salary=8000)
    _insert_fighter(conn, slate_id=slate_id, name="Charlie", salary=8000)
    _insert_fighter(conn, slate_id=slate_id, name="Delta", salary=8000)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="alpha",
        fighter_2_name="Bravo",
    )
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Charlie",
        fighter_2_name="Delta",
    )
    for name in ("alpha", "Bravo", "Charlie", "Delta"):
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw=name,
            american_odds=-150,
            captured_at=f"2026-05-20T00:00:0{['alpha','Bravo','Charlie','Delta'].index(name)}Z",
        )
    recompute_and_replace_match_results(conn, slate_id)

    first = build_optimizer_pool(conn, slate_id)
    second = build_optimizer_pool(conn, slate_id)
    assert [e.fighter_id for e in first.entries] == [
        e.fighter_id for e in second.entries
    ]
    # Sanity: all four fighters made the pool.
    assert len(first.entries) == 4
    # Case-insensitive name ordering: alpha, Bravo, Charlie, Delta.
    assert [e.dk_name for e in first.entries] == [
        "alpha",
        "Bravo",
        "Charlie",
        "Delta",
    ]
