"""Tests for Projection v1 Phase C slate-level service.

Covers ``project_slate`` in
``src/projections/slate_projection_service.py`` per
``docs/PROJECTION_V1_DESIGN.md`` §8 Phase C and §9. Composition only —
Phase A and Phase B already have their own focused suites. These tests
pin the composition contract: one row per active aggregated fighter,
correct status propagation, aggregation notes preserved, deterministic
ordering, and read-only behavior end to end.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    FightGroupRepository,
    ManualMatchOverrideRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching_service import (
    recompute_and_replace_match_results,
)
from src.projections.finish_model import (
    LEAGUE_FINISH_RATE,
    compute_finish_projection,
)
from src.projections.projection_service import (
    STATUS_MISSING_INPUTS,
    STATUS_NON_PROJECTABLE,
    STATUS_OK,
)
from src.projections.slate_projection_service import (
    PROJECTION_MODE_V0,
    PROJECTION_MODE_V2,
    FighterSlateProjection,
    project_slate,
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


def _db_snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
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
# Base cases
# ---------------------------------------------------------------------------


def test_unknown_slate_returns_empty(conn):
    """Mirrors Phase B's unknown-slate behavior — no rows, no error."""
    assert project_slate(conn, 999_999) == []


def test_empty_slate_returns_empty(conn, slate_id):
    assert project_slate(conn, slate_id) == []


def test_one_row_per_active_aggregated_fighter(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    _insert_fighter(
        conn,
        slate_id=slate_id,
        name="Inactive F",
        salary=8000,
        status="inactive",
    )

    projections = project_slate(conn, slate_id)
    assert len(projections) == 2
    names = {p.fighter_name for p in projections}
    assert names == {"Jose Aldo", "Marlon Vera"}
    for p in projections:
        assert isinstance(p, FighterSlateProjection)
        assert p.slate_id == slate_id


# ---------------------------------------------------------------------------
# Status propagation through the composition
# ---------------------------------------------------------------------------


def test_valid_inputs_produce_ok_with_numeric_points(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
        scheduled_rounds=3,
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        american_odds=-150,
    )
    recompute_and_replace_match_results(conn, slate_id)

    by_name = {p.fighter_name: p for p in project_slate(conn, slate_id)}
    aldo = by_name["Jose Aldo"]
    assert aldo.projection_status == STATUS_OK
    assert aldo.projected_dk_points is not None
    assert aldo.projected_dk_points > 0
    assert aldo.missing_inputs == ()


def test_missing_win_probability_yields_missing_inputs(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
        scheduled_rounds=3,
    )

    for p in project_slate(conn, slate_id):
        assert p.projection_status == STATUS_MISSING_INPUTS
        assert "win_probability" in p.missing_inputs
        assert p.projected_dk_points is None


def test_missing_fight_group_yields_non_projectable(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Lonely Fighter", salary=8500)

    [proj] = project_slate(conn, slate_id)
    assert proj.projection_status == STATUS_NON_PROJECTABLE
    assert "fight_group" in proj.missing_inputs
    assert proj.projected_dk_points is None


# ---------------------------------------------------------------------------
# Notes / ordering
# ---------------------------------------------------------------------------


def test_aggregation_notes_are_preserved_in_output(conn, slate_id):
    """Phase B emits diagnostic notes (e.g. multiple auto_match odds
    rows for one fighter); Phase C must surface them on the composed
    output so a UI can render the data error without re-querying."""
    aldo_id = _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", salary=8800
    )
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
        scheduled_rounds=3,
    )
    first = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        american_odds=-150,
        captured_at="2026-05-20T00:00:00Z",
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        american_odds=-300,
        captured_at="2026-05-20T00:01:00Z",
    )
    recompute_and_replace_match_results(conn, slate_id)

    by_id = {p.fighter_id: p for p in project_slate(conn, slate_id)}
    aldo = by_id[aldo_id]
    assert any(
        f"odds_row_id={first.id}" in note for note in aldo.notes
    ), aldo.notes


def test_ordering_matches_phase_b(conn, slate_id):
    """Phase C preserves Phase B's deterministic emission order so
    consumers can rely on a stable per-fighter sequence."""
    from src.projections.projection_input_service import (
        aggregate_projection_inputs,
    )

    for name in ("Charlie", "Alpha", "Bravo"):
        _insert_fighter(conn, slate_id=slate_id, name=name, salary=8000)

    phase_b_order = [
        b.inputs.fighter_id for b in aggregate_projection_inputs(conn, slate_id)
    ]
    phase_c_order = [p.fighter_id for p in project_slate(conn, slate_id)]
    assert phase_c_order == phase_b_order


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


def test_project_slate_does_not_mutate_any_persisted_state(conn, slate_id):
    """Belt-and-braces: snapshot every relevant table before/after the
    call. Phase C composes Phase B + Phase A — neither writes — but the
    composition must also remain read-only."""
    aldo_id = _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", salary=8800
    )
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
        scheduled_rounds=3,
    )
    row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        american_odds=-150,
    )
    recompute_and_replace_match_results(conn, slate_id)
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=aldo_id,
    )
    before = _db_snapshot(conn)

    project_slate(conn, slate_id)
    project_slate(conn, slate_id)  # second call also a no-op writer

    after = _db_snapshot(conn)
    assert before == after, (
        "project_slate must not mutate persisted state"
    )
    # Projections table specifically must remain empty — Phase C does
    # not persist projections (design §8, open question §10.1).
    assert after["projections"] == []


def test_review_rejected_drops_fighter_to_missing_inputs(conn, slate_id):
    """D.5.2 (§16.9): an active ``reject_match`` flips ``effective_status``
    to ``review_rejected`` (``match_status`` stays ``auto_match``). Phase C
    composes Phase B, which now sources win probability from
    ``effective_status`` — a rejected row is not eligible, so the fighter
    loses its win probability and Phase A reports ``missing_inputs`` with
    the ``win_probability`` tag.

    Inverse of the pre-D.5.2 contract (ODDS_PERSISTENCE §15.11 risk #7).
    """
    aldo_id = _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", salary=8800
    )
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
        scheduled_rounds=3,
    )
    row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        american_odds=-150,
    )
    recompute_and_replace_match_results(conn, slate_id)
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=aldo_id,
    )
    recompute_and_replace_match_results(conn, slate_id)

    by_id = {p.fighter_id: p for p in project_slate(conn, slate_id)}
    aldo = by_id[aldo_id]
    assert aldo.projection_status == STATUS_MISSING_INPUTS
    assert "win_probability" in aldo.missing_inputs
    assert aldo.projected_dk_points is None


def test_force_pair_fighter_enters_projection_pool(conn, slate_id):
    """The §16.1 Build-exclusion-removed integration test. A name-mismatched
    odds row the matcher left ``unmatched`` is force-paired to an active
    fighter; after recompute that fighter is in the projection pool with a
    non-``None`` projected score and ``ok`` status — the fake-fix trap is
    closed end to end through Phase C."""
    bruno_id = _insert_fighter(
        conn, slate_id=slate_id, name="Bruno Silva", salary=7600
    )
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Bruno Silva",
        fighter_2_name="Marlon Vera",
        scheduled_rounds=3,
    )
    odds_row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Bruno Gustavo da Silva",
        american_odds=-150,
    )
    summary = recompute_and_replace_match_results(conn, slate_id)
    # The matcher could not bind the name-mismatched row on its own.
    assert summary.status_counts.get("auto_match", 0) == 0

    # Before the binding, Build excludes Bruno (no win probability).
    before = {p.fighter_id: p for p in project_slate(conn, slate_id)}
    assert before[bruno_id].projection_status == STATUS_MISSING_INPUTS
    assert "win_probability" in before[bruno_id].missing_inputs

    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=odds_row.odds_row_key,
        fighter_id=bruno_id,
    )
    recompute_and_replace_match_results(conn, slate_id)

    after = {p.fighter_id: p for p in project_slate(conn, slate_id)}
    assert after[bruno_id].projection_status == STATUS_OK
    assert after[bruno_id].projected_dk_points is not None


# ---------------------------------------------------------------------------
# Phase C — finish-aware (v2) mode (PROJECTION_V2_METHOD_AWARE_DESIGN §13/§15)
#
# v0 stays the default engine; v2 is EXPERIMENTAL, selectable, not promoted.
# ---------------------------------------------------------------------------


def _seed_projectable_bout(conn, slate_id, *, american_odds=-150):
    """Insert a fully-projectable Aldo/Vera bout (group + matched odds) and
    return Aldo's fighter id."""
    aldo_id = _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", salary=8800
    )
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
        scheduled_rounds=3,
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        american_odds=american_odds,
    )
    recompute_and_replace_match_results(conn, slate_id)
    return aldo_id


def test_default_mode_is_v0_formula(conn, slate_id):
    """The default engine is v0: rows are tagged ``v0_formula`` and the
    additive finish-aware fields stay empty/None so every existing consumer
    (optimizer, alerts, exports) is unchanged."""
    _seed_projectable_bout(conn, slate_id)

    for p in project_slate(conn, slate_id):
        assert p.projection_mode == PROJECTION_MODE_V0
        assert p.outcome_branches == ()
        assert p.best_branch_pts is None
        assert p.worst_branch_pts is None
        assert p.p_fight_finishes is None
        assert p.finish_share is None


def test_explicit_v0_mode_matches_default(conn, slate_id):
    """Passing the v0 mode explicitly is identical to the default."""
    _seed_projectable_bout(conn, slate_id)
    assert project_slate(conn, slate_id) == project_slate(
        conn, slate_id, projection_mode=PROJECTION_MODE_V0
    )


def test_unsupported_projection_mode_raises(conn, slate_id):
    with pytest.raises(ValueError, match="unsupported projection_mode"):
        project_slate(conn, slate_id, projection_mode="v9_bogus")


def test_v2_mode_ok_row_has_branches_and_mean(conn, slate_id):
    """A fully-resolved fighter in v2 mode yields an ``ok`` finish-aware row:
    a numeric mean, four outcome branches summing to probability 1, ordered
    best/worst branch means, and the resolved Tier-0 finish signal echoed."""
    aldo_id = _seed_projectable_bout(conn, slate_id)

    by_id = {
        p.fighter_id: p
        for p in project_slate(conn, slate_id, projection_mode=PROJECTION_MODE_V2)
    }
    aldo = by_id[aldo_id]
    assert aldo.projection_mode == PROJECTION_MODE_V2
    assert aldo.projection_status == STATUS_OK
    assert aldo.projected_dk_points is not None and aldo.projected_dk_points > 0
    assert len(aldo.outcome_branches) == 4
    assert sum(b.probability for b in aldo.outcome_branches) == pytest.approx(1.0)
    assert aldo.best_branch_pts is not None and aldo.worst_branch_pts is not None
    assert aldo.best_branch_pts >= aldo.worst_branch_pts
    # Tier 0: league finish constant is always present; share splits by win prob.
    assert aldo.p_fight_finishes == pytest.approx(LEAGUE_FINISH_RATE)
    assert 0.0 <= aldo.finish_share <= 1.0


def test_v2_mode_mean_matches_finish_model(conn, slate_id):
    """The service is pure glue: its v2 mean + branches equal the finish model
    called directly on the same resolved inputs (no extra transformation)."""
    from src.projections.projection_input_service import (
        aggregate_projection_inputs,
    )

    aldo_id = _seed_projectable_bout(conn, slate_id)
    bundle = {
        b.inputs.fighter_id: b
        for b in aggregate_projection_inputs(conn, slate_id)
    }[aldo_id]
    expected = compute_finish_projection(
        p_win=bundle.inputs.implied_win_probability,
        scheduled_rounds=bundle.inputs.scheduled_rounds,
    )

    [row] = [
        p
        for p in project_slate(conn, slate_id, projection_mode=PROJECTION_MODE_V2)
        if p.fighter_id == aldo_id
    ]
    assert row.projected_dk_points == pytest.approx(expected.projected_dk_points)
    assert row.outcome_branches == expected.outcome_branches
    assert row.best_branch_pts == pytest.approx(expected.best_branch_pts)
    assert row.worst_branch_pts == pytest.approx(expected.worst_branch_pts)


def test_v2_mode_missing_win_probability_yields_missing_inputs(conn, slate_id):
    """v2 keeps v1's never-invent contract: a fighter with a fight group but no
    projection-eligible odds row reports ``missing_inputs`` (win_probability),
    a ``None`` mean, and no branches — tagged v2 (never a silent v0 fallback)."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
        scheduled_rounds=3,
    )

    for p in project_slate(conn, slate_id, projection_mode=PROJECTION_MODE_V2):
        assert p.projection_mode == PROJECTION_MODE_V2
        assert p.projection_status == STATUS_MISSING_INPUTS
        assert "win_probability" in p.missing_inputs
        assert p.projected_dk_points is None
        assert p.outcome_branches == ()
        assert p.best_branch_pts is None


def test_v2_mode_missing_fight_group_non_projectable(conn, slate_id):
    """The structural ``non_projectable`` precedence carries over to v2
    unchanged (design §9): no fight group → non_projectable, no branches."""
    _insert_fighter(conn, slate_id=slate_id, name="Lonely Fighter", salary=8500)

    [proj] = project_slate(conn, slate_id, projection_mode=PROJECTION_MODE_V2)
    assert proj.projection_mode == PROJECTION_MODE_V2
    assert proj.projection_status == STATUS_NON_PROJECTABLE
    assert "fight_group" in proj.missing_inputs
    assert proj.projected_dk_points is None
    assert proj.outcome_branches == ()


def test_v2_mode_preserves_phase_b_order(conn, slate_id):
    """v2 mode preserves Phase B's deterministic emission order, like v0."""
    from src.projections.projection_input_service import (
        aggregate_projection_inputs,
    )

    for name in ("Charlie", "Alpha", "Bravo"):
        _insert_fighter(conn, slate_id=slate_id, name=name, salary=8000)

    phase_b_order = [
        b.inputs.fighter_id for b in aggregate_projection_inputs(conn, slate_id)
    ]
    v2_order = [
        p.fighter_id
        for p in project_slate(conn, slate_id, projection_mode=PROJECTION_MODE_V2)
    ]
    assert v2_order == phase_b_order


def test_v0_and_v2_agree_on_projectable_set(conn, slate_id):
    """Homogeneous-pool / apples-to-apples: both engines classify the same
    fighters the same way (status + ids); only the numbers differ. Pins that
    v2 does not silently shrink or grow the projectable set in real data."""
    _seed_projectable_bout(conn, slate_id)
    _insert_fighter(conn, slate_id=slate_id, name="Lonely Fighter", salary=8500)

    v0 = {p.fighter_id: p for p in project_slate(conn, slate_id)}
    v2 = {
        p.fighter_id: p
        for p in project_slate(conn, slate_id, projection_mode=PROJECTION_MODE_V2)
    }
    assert v0.keys() == v2.keys()
    for fid in v0:
        assert v0[fid].projection_status == v2[fid].projection_status
        assert v0[fid].projection_mode == PROJECTION_MODE_V0
        assert v2[fid].projection_mode == PROJECTION_MODE_V2


def test_v2_mode_does_not_mutate_persisted_state(conn, slate_id):
    """v2 mode is read-only end to end, same as v0."""
    _seed_projectable_bout(conn, slate_id)
    before = _db_snapshot(conn)

    project_slate(conn, slate_id, projection_mode=PROJECTION_MODE_V2)
    project_slate(conn, slate_id, projection_mode=PROJECTION_MODE_V2)

    after = _db_snapshot(conn)
    assert before == after, "v2 mode must not mutate persisted state"
    assert after["projections"] == []
