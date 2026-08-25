"""Projection v1 pure service tests (Phase A).

Covers ``compute_projection_v1`` in ``src/projections/projection_service.py``
per ``docs/PROJECTION_V1_DESIGN.md`` §4–§5 / §9. No DB, no Streamlit, no
``effective_status``.
"""

from __future__ import annotations

import math

import pytest

from src.projections.projection_service import (
    STATUS_MISSING_INPUTS,
    STATUS_NON_PROJECTABLE,
    STATUS_OK,
    ProjectionInputs,
    compute_projection_v1,
)


def _inputs(**overrides) -> ProjectionInputs:
    base = dict(
        fighter_id=1,
        slate_id=10,
        salary=8500,
        implied_win_probability=0.55,
        scheduled_rounds=3,
        has_fight_group=True,
        has_opponent=True,
    )
    base.update(overrides)
    return ProjectionInputs(**base)


def test_ok_three_round_matches_default_formula():
    r = compute_projection_v1(_inputs(salary=9000, implied_win_probability=0.5, scheduled_rounds=3))
    assert r.projection_status == STATUS_OK
    assert r.missing_inputs == ()
    assert math.isclose(r.projected_dk_points, 35.0, abs_tol=1e-9)


def test_ok_five_round_applies_round_bonus_only():
    # p=0.5, salary=9000 misses value tiers; 5-round adds +7 -> 0.5*70 + 7 = 42
    r = compute_projection_v1(_inputs(salary=9000, implied_win_probability=0.5, scheduled_rounds=5))
    assert r.projection_status == STATUS_OK
    assert math.isclose(r.projected_dk_points, 42.0, abs_tol=1e-9)


def test_ok_five_round_with_value_bonus():
    # p=0.50, salary=7500 -> 0.5*70 + 8 + 7 = 50
    r = compute_projection_v1(_inputs(salary=7500, implied_win_probability=0.50, scheduled_rounds=5))
    assert r.projection_status == STATUS_OK
    assert math.isclose(r.projected_dk_points, 50.0, abs_tol=1e-9)


def test_missing_salary_reports_tag_and_no_points():
    r = compute_projection_v1(_inputs(salary=None))
    assert r.projection_status == STATUS_MISSING_INPUTS
    assert r.projected_dk_points is None
    assert "salary" in r.missing_inputs


def test_missing_win_probability_reports_tag_and_no_points():
    r = compute_projection_v1(_inputs(implied_win_probability=None))
    assert r.projection_status == STATUS_MISSING_INPUTS
    assert r.projected_dk_points is None
    assert "win_probability" in r.missing_inputs


def test_missing_scheduled_rounds_reports_tag_and_no_points():
    r = compute_projection_v1(_inputs(scheduled_rounds=None))
    assert r.projection_status == STATUS_MISSING_INPUTS
    assert r.projected_dk_points is None
    assert "scheduled_rounds" in r.missing_inputs


def test_invalid_scheduled_rounds_treated_as_missing():
    r = compute_projection_v1(_inputs(scheduled_rounds=4))
    assert r.projection_status == STATUS_MISSING_INPUTS
    assert r.projected_dk_points is None
    assert r.missing_inputs == ("scheduled_rounds",)


def test_multiple_missing_inputs_aggregate():
    r = compute_projection_v1(
        _inputs(salary=None, implied_win_probability=None, scheduled_rounds=None)
    )
    assert r.projection_status == STATUS_MISSING_INPUTS
    assert r.projected_dk_points is None
    assert set(r.missing_inputs) == {"salary", "win_probability", "scheduled_rounds"}


def test_no_fight_group_is_non_projectable_and_dominates_missing_data():
    r = compute_projection_v1(_inputs(has_fight_group=False, salary=None))
    assert r.projection_status == STATUS_NON_PROJECTABLE
    assert r.projected_dk_points is None
    assert "fight_group" in r.missing_inputs
    # structural problem dominates: data tags are not mixed in
    assert "salary" not in r.missing_inputs


def test_no_opponent_is_non_projectable():
    r = compute_projection_v1(_inputs(has_opponent=False))
    assert r.projection_status == STATUS_NON_PROJECTABLE
    assert r.projected_dk_points is None
    assert r.missing_inputs == ("opponent",)


def test_result_carries_fighter_and_slate_ids():
    r = compute_projection_v1(_inputs(fighter_id=42, slate_id=99))
    assert r.fighter_id == 42
    assert r.slate_id == 99


def test_invalid_probability_raises_through_to_default_projection():
    with pytest.raises(ValueError):
        compute_projection_v1(_inputs(implied_win_probability=1.5))


def test_missing_win_probability_is_not_silently_defaulted():
    # No fallback to 0.5; result must be None, not 35.0.
    r = compute_projection_v1(_inputs(salary=9000, implied_win_probability=None, scheduled_rounds=3))
    assert r.projected_dk_points is None
