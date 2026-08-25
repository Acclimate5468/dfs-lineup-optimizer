"""Tests for Projection v1 Phase B read-side aggregation.

Covers ``aggregate_projection_inputs`` in
``src/projections/projection_input_service.py`` per
``docs/PROJECTION_V1_DESIGN.md`` §2 / §5 / §8 Phase B / §9 service
tests. Read-only: every test asserts that the call does not mutate the
DB, and several pin per-fighter input fields against persisted state.
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
from src.projections.projection_input_service import (
    ProjectionInputBundle,
    aggregate_projection_inputs,
)
from src.projections.projection_service import (
    STATUS_MISSING_INPUTS,
    STATUS_NON_PROJECTABLE,
    STATUS_OK,
    ProjectionInputs,
    compute_projection_v1,
)


# ---------------------------------------------------------------------------
# Fixtures + tiny helpers
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


@pytest.fixture
def other_slate_id(conn):
    return SlateRepository(conn).create(event_name="UFC 998").id


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
    opponent_name_raw: str | None = None,
    captured_at: str = "2026-05-20T00:00:00Z",
    source: str = "manual",
):
    return OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source=source,
        captured_at=captured_at,
        opponent_name_raw=opponent_name_raw,
    )


def _db_snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Snapshot every table touched by Phase B's reads (and a few it must
    never write to). Used to assert read-only behavior end to end."""
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
# Slate / fighter base cases
# ---------------------------------------------------------------------------


def test_unknown_slate_returns_empty(conn):
    """Mirrors ``FighterRepository.list_for_slate`` — no fighters means
    no bundles. No "slate not found" error is raised because the read
    layer has no business inventing one."""
    assert aggregate_projection_inputs(conn, 999_999) == []


def test_empty_slate_returns_empty(conn, slate_id):
    """Slate exists but has no fighters yet (e.g. salary not imported)."""
    assert aggregate_projection_inputs(conn, slate_id) == []


def test_one_bundle_per_active_fighter(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)

    bundles = aggregate_projection_inputs(conn, slate_id)
    assert len(bundles) == 2
    for b in bundles:
        assert isinstance(b, ProjectionInputBundle)
        assert isinstance(b.inputs, ProjectionInputs)
        assert b.inputs.slate_id == slate_id
    names = sorted(b.fighter_name for b in bundles)
    assert names == ["Jose Aldo", "Marlon Vera"]


def test_salary_is_carried_into_inputs(conn, slate_id):
    fid = _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", salary=8700
    )
    [bundle] = aggregate_projection_inputs(conn, slate_id)
    assert bundle.inputs.fighter_id == fid
    assert bundle.inputs.salary == 8700


def test_inactive_fighter_is_excluded(conn, slate_id):
    """Phase B chosen behavior: inactive fighters are filtered out of
    the aggregation entirely. Design §5 reserves ``"fighter_status"``
    for a future gating slice, but until that lands the read layer
    omits inactive fighters so they cannot surface as ghost rows."""
    _insert_fighter(
        conn, slate_id=slate_id, name="Active F", salary=8000, status="active"
    )
    _insert_fighter(
        conn,
        slate_id=slate_id,
        name="Inactive F",
        salary=8000,
        status="inactive",
    )
    _insert_fighter(
        conn,
        slate_id=slate_id,
        name="Excluded F",
        salary=8000,
        status="excluded",
    )

    bundles = aggregate_projection_inputs(conn, slate_id)
    assert [b.fighter_name for b in bundles] == ["Active F"]


def test_aggregation_is_slate_scoped(conn, slate_id, other_slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Slate A Fighter")
    _insert_fighter(conn, slate_id=other_slate_id, name="Slate B Fighter")

    a = aggregate_projection_inputs(conn, slate_id)
    b = aggregate_projection_inputs(conn, other_slate_id)
    assert [x.fighter_name for x in a] == ["Slate A Fighter"]
    assert [x.fighter_name for x in b] == ["Slate B Fighter"]


# ---------------------------------------------------------------------------
# Fight group / opponent / scheduled rounds
# ---------------------------------------------------------------------------


def test_missing_fight_group_marks_structural_signals_false(
    conn, slate_id
):
    _insert_fighter(conn, slate_id=slate_id, name="Lonely Fighter")

    [bundle] = aggregate_projection_inputs(conn, slate_id)
    assert bundle.inputs.has_fight_group is False
    assert bundle.inputs.has_opponent is False
    # Not silently defaulted to 3 — design §5 missing rounds policy.
    assert bundle.inputs.scheduled_rounds is None


def test_missing_fight_group_drives_non_projectable_through_phase_a(
    conn, slate_id
):
    """Phase B output feeds Phase A unchanged. A fighter with no fight
    group is structurally non-projectable per design §5; verify the
    composition stays consistent."""
    _insert_fighter(
        conn, slate_id=slate_id, name="Lonely Fighter", salary=8500
    )
    [bundle] = aggregate_projection_inputs(conn, slate_id)
    result = compute_projection_v1(bundle.inputs)
    assert result.projection_status == STATUS_NON_PROJECTABLE
    assert "fight_group" in result.missing_inputs


def test_present_fight_group_sets_rounds_and_opponent_signals(
    conn, slate_id
):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
        scheduled_rounds=3,
    )

    by_name = {
        b.fighter_name: b for b in aggregate_projection_inputs(conn, slate_id)
    }
    for name in ("Jose Aldo", "Marlon Vera"):
        inp = by_name[name].inputs
        assert inp.has_fight_group is True
        assert inp.has_opponent is True
        assert inp.scheduled_rounds == 3


def test_five_round_fight_group_surfaces_rounds_five(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Champ", salary=9500)
    _insert_fighter(conn, slate_id=slate_id, name="Contender", salary=9300)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Champ",
        fighter_2_name="Contender",
        scheduled_rounds=5,
    )
    bundles = aggregate_projection_inputs(conn, slate_id)
    for b in bundles:
        assert b.inputs.scheduled_rounds == 5


def test_fight_group_join_uses_conservative_name_normalization(
    conn, slate_id
):
    """Casing / whitespace folding on the join key only. Fuzzy /
    nickname matching is explicitly out of scope for Phase B."""
    _insert_fighter(conn, slate_id=slate_id, name="JOSE ALDO", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name=" jose aldo ",
        fighter_2_name="Marlon Vera",
        scheduled_rounds=3,
    )

    by_name = {
        b.fighter_name: b for b in aggregate_projection_inputs(conn, slate_id)
    }
    assert by_name["JOSE ALDO"].inputs.has_fight_group is True
    assert by_name["Marlon Vera"].inputs.has_fight_group is True


def test_multiple_fight_groups_for_one_fighter_notes_choice(
    conn, slate_id
):
    """Phase B picks the lowest-id fight group deterministically and
    records a diagnostic note so a later UI can surface the data error."""
    _insert_fighter(conn, slate_id=slate_id, name="Ambiguous", salary=8500)
    _insert_fighter(conn, slate_id=slate_id, name="Foe One", salary=8000)
    _insert_fighter(conn, slate_id=slate_id, name="Foe Two", salary=8000)

    fg_repo = FightGroupRepository(conn)
    g1 = fg_repo.create(
        slate_id=slate_id,
        fighter_1_name="Ambiguous",
        fighter_2_name="Foe One",
        scheduled_rounds=3,
    )
    fg_repo.create(
        slate_id=slate_id,
        fighter_1_name="Ambiguous",
        fighter_2_name="Foe Two",
        scheduled_rounds=5,
    )

    by_name = {
        b.fighter_name: b for b in aggregate_projection_inputs(conn, slate_id)
    }
    amb = by_name["Ambiguous"]
    assert amb.inputs.has_fight_group is True
    # Chosen group is the lowest-id one (3 rounds), not the 5-round one.
    assert amb.inputs.scheduled_rounds == 3
    assert any(
        f"fight_group_id={g1.id}" in note for note in amb.notes
    ), amb.notes


# ---------------------------------------------------------------------------
# Win probability (odds match results → odds row implied_probability)
# ---------------------------------------------------------------------------


def test_missing_odds_means_implied_probability_none(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
    )

    for b in aggregate_projection_inputs(conn, slate_id):
        assert b.inputs.implied_win_probability is None


def test_missing_odds_drives_missing_inputs_through_phase_a(
    conn, slate_id
):
    """Composition smoke test: missing odds with a present fight group
    yields ``missing_inputs`` with tag ``"win_probability"`` (§5)."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
    )

    for b in aggregate_projection_inputs(conn, slate_id):
        r = compute_projection_v1(b.inputs)
        assert r.projection_status == STATUS_MISSING_INPUTS
        assert "win_probability" in r.missing_inputs


def test_auto_match_passes_through_implied_probability(conn, slate_id):
    """An ``auto_match`` row resolves the fighter's win probability via
    the underlying odds row's persisted ``implied_probability``. Raw
    (with-vig) implied is passed through in Phase B — no two-way no-vig
    pairing is performed yet (design §2 / Phase B docstring)."""
    aldo_id = _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", salary=8800
    )
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
    )
    # -150 → 0.6 implied.
    odds_row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        american_odds=-150,
    )
    recompute_and_replace_match_results(conn, slate_id)

    by_id = {
        b.inputs.fighter_id: b
        for b in aggregate_projection_inputs(conn, slate_id)
    }
    aldo = by_id[aldo_id]
    assert aldo.inputs.implied_win_probability == pytest.approx(
        odds_row.implied_probability, abs=1e-9
    )


def test_full_inputs_produce_ok_status_through_phase_a(conn, slate_id):
    """End-to-end composition: salary + fight group + auto_match odds →
    Phase A returns ``ok`` with a numeric ``projected_dk_points``."""
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

    bundles_by_name = {
        b.fighter_name: b for b in aggregate_projection_inputs(conn, slate_id)
    }
    aldo_result = compute_projection_v1(bundles_by_name["Jose Aldo"].inputs)
    assert aldo_result.projection_status == STATUS_OK
    assert aldo_result.projected_dk_points is not None
    assert aldo_result.missing_inputs == ()

    # Opponent has no odds — still missing_inputs.
    vera_result = compute_projection_v1(bundles_by_name["Marlon Vera"].inputs)
    assert vera_result.projection_status == STATUS_MISSING_INPUTS
    assert "win_probability" in vera_result.missing_inputs


def test_review_required_match_does_not_contribute_win_probability(
    conn, slate_id
):
    """A ``review_required`` row leaves ``implied_win_probability=None``.
    Only ``auto_match`` results feed Phase B's win probability — the
    matcher's reviewable verdicts are not silently treated as resolved."""
    _insert_fighter(conn, slate_id=slate_id, name="Dan Smith", salary=8500)
    _insert_fighter(conn, slate_id=slate_id, name="Daniel Smith", salary=8500)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Dan Smith",
        fighter_2_name="Daniel Smith",
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Daniel Smith Jr.",
        american_odds=-200,
    )
    summary = recompute_and_replace_match_results(conn, slate_id)
    # Confirm the seed produced a review_required row, not auto_match.
    assert summary.status_counts.get("review_required") == 1

    for b in aggregate_projection_inputs(conn, slate_id):
        assert b.inputs.implied_win_probability is None


def test_unmatched_row_does_not_contribute_win_probability(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo", salary=8800)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Other Person",
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Totally Unrelated Person",
        american_odds=-150,
    )
    summary = recompute_and_replace_match_results(conn, slate_id)
    assert summary.status_counts.get("unmatched") == 1

    [bundle] = aggregate_projection_inputs(conn, slate_id)
    assert bundle.inputs.implied_win_probability is None


def test_multiple_auto_matches_pick_lowest_odds_row_id_and_note(
    conn, slate_id
):
    """Two ``auto_match`` rows for the same fighter (e.g. multiple
    bookmaker snapshots). Phase B chooses the lowest ``odds_row_id`` for
    determinism and emits a diagnostic note."""
    aldo_id = _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", salary=8800
    )
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
    )
    first = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        american_odds=-150,
        captured_at="2026-05-20T00:00:00Z",
    )
    second = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        american_odds=-300,
        captured_at="2026-05-20T00:01:00Z",
    )
    assert first.id < second.id  # sanity check on insert order
    recompute_and_replace_match_results(conn, slate_id)

    by_id = {
        b.inputs.fighter_id: b
        for b in aggregate_projection_inputs(conn, slate_id)
    }
    aldo = by_id[aldo_id]
    # Lowest-id row's implied (-150 → 0.6), not -300's (0.75).
    assert aldo.inputs.implied_win_probability == pytest.approx(
        first.implied_probability, abs=1e-9
    )
    assert any(
        f"odds_row_id={first.id}" in note for note in aldo.notes
    ), aldo.notes


def test_review_rejected_excludes_win_probability(conn, slate_id):
    """D.5.2 (§16.9): Phase B now reads ``effective_status``. An active
    ``reject_match`` flips ``effective_status`` to ``review_rejected``
    while ``match_status`` stays ``auto_match`` — and a rejected row is
    NOT projection-eligible, so the fighter's win probability drops to
    ``None``.

    This is the inverse of the pre-D.5.2 contract (a reject used to leave
    Build untouched, ODDS_PERSISTENCE §15.11 risk #7); the promotion makes
    reject actually take effect on the projection pool.
    """
    aldo_id = _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", salary=8800
    )
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
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
    # Re-run so effective_status flips to review_rejected (Phase D.4.3.b).
    summary = recompute_and_replace_match_results(conn, slate_id)
    # Sanity: match_status is still the matcher's auto_match (audit-only).
    assert summary.status_counts.get("auto_match") == 1

    by_id = {
        b.inputs.fighter_id: b
        for b in aggregate_projection_inputs(conn, slate_id)
    }
    aldo = by_id[aldo_id]
    # effective_status is review_rejected → not eligible → no win prob.
    assert aldo.inputs.implied_win_probability is None


def test_review_accepted_contributes_win_probability(conn, slate_id):
    """D.5.2 (§16.9): an ``accept_match`` override flips a review row to
    ``effective_status='review_accepted'`` and binds the fighter, so the
    fighter now receives the odds row's win probability."""
    _insert_fighter(conn, slate_id=slate_id, name="Dan Smith", salary=8500)
    daniel_id = _insert_fighter(
        conn, slate_id=slate_id, name="Daniel Smith", salary=8500
    )
    odds_row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Daniel Smith Jr.",
        american_odds=-200,
    )
    summary = recompute_and_replace_match_results(conn, slate_id)
    assert summary.status_counts.get("review_required") == 1

    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="accept_match",
        odds_row_key=odds_row.odds_row_key,
        fighter_id=daniel_id,
    )
    recompute_and_replace_match_results(conn, slate_id)

    by_id = {
        b.inputs.fighter_id: b
        for b in aggregate_projection_inputs(conn, slate_id)
    }
    assert by_id[daniel_id].inputs.implied_win_probability == pytest.approx(
        odds_row.implied_probability, abs=1e-9
    )


def test_force_pair_contributes_win_probability(conn, slate_id):
    """D.5.2 / §16.1: the name-mismatch case. The matcher leaves the odds
    row ``unmatched``; a ``force_pair`` binds it to an active fighter and
    that fighter now receives the win probability — the structural Build
    fix."""
    bruno_id = _insert_fighter(
        conn, slate_id=slate_id, name="Bruno Silva", salary=7600
    )
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    odds_row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Totally Unrelated Person",
        american_odds=-150,
    )
    summary = recompute_and_replace_match_results(conn, slate_id)
    assert summary.status_counts.get("unmatched") == 1
    # Pre-binding: the fighter has no win probability (the trap).
    pre = {
        b.inputs.fighter_id: b
        for b in aggregate_projection_inputs(conn, slate_id)
    }
    assert pre[bruno_id].inputs.implied_win_probability is None

    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=odds_row.odds_row_key,
        fighter_id=bruno_id,
    )
    recompute_and_replace_match_results(conn, slate_id)

    post = {
        b.inputs.fighter_id: b
        for b in aggregate_projection_inputs(conn, slate_id)
    }
    assert post[bruno_id].inputs.implied_win_probability == pytest.approx(
        odds_row.implied_probability, abs=1e-9
    )


def test_binding_survives_recompute_and_still_feeds_projection(conn, slate_id):
    """G: a force_pair binding is re-derived on every recompute, so the
    fighter keeps its projection win probability across repeated runs."""
    bruno_id = _insert_fighter(
        conn, slate_id=slate_id, name="Bruno Silva", salary=7600
    )
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    odds_row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Totally Unrelated Person",
        american_odds=-150,
    )
    recompute_and_replace_match_results(conn, slate_id)
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=odds_row.odds_row_key,
        fighter_id=bruno_id,
    )
    # Two further recomputes — the binding must persist across both.
    recompute_and_replace_match_results(conn, slate_id)
    recompute_and_replace_match_results(conn, slate_id)

    by_id = {
        b.inputs.fighter_id: b
        for b in aggregate_projection_inputs(conn, slate_id)
    }
    assert by_id[bruno_id].inputs.implied_win_probability == pytest.approx(
        odds_row.implied_probability, abs=1e-9
    )


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


def test_aggregation_does_not_mutate_any_persisted_state(conn, slate_id):
    """Belt-and-braces: snapshot every relevant table before and after
    the call, including odds_match_results and manual_match_overrides
    which Phase B explicitly must not touch."""
    aldo_id = _insert_fighter(
        conn, slate_id=slate_id, name="Jose Aldo", salary=8800
    )
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
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

    bundles = aggregate_projection_inputs(conn, slate_id)
    # Run twice to confirm second call is also a no-op writer.
    aggregate_projection_inputs(conn, slate_id)

    after = _db_snapshot(conn)
    assert before == after, (
        "aggregate_projection_inputs must not mutate persisted state"
    )
    # Sanity: aggregator still returned the rows it should have.
    assert {b.fighter_name for b in bundles} == {"Jose Aldo", "Marlon Vera"}
