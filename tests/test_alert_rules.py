"""Mismatch Alerts v1 — Phase A pure rule tests.

Pins design §3 thresholds, §9 ordering, and the §3.9 / §15 risk #8
"never emit late_news_risk in v1" contract.
"""

from __future__ import annotations

import inspect

import pytest

from src.alerts import alert_rules
from src.alerts.alert_rules import (
    ALERT_CODE_FIGHT_GROUP_ISSUE,
    ALERT_CODE_FIVE_ROUND_EDGE,
    ALERT_CODE_LATE_NEWS_RISK,
    ALERT_CODE_MISSING_INPUT,
    ALERT_CODE_ODDS_VS_SALARY_MISMATCH,
    ALERT_CODE_PROJECTION_NON_PROJECTABLE,
    ALERT_CODE_SALARY_INEFFICIENCY_HIGH,
    ALERT_CODE_SALARY_INEFFICIENCY_LOW,
    ALERT_CODE_UNDERDOG_VALUE,
    ALERT_CODE_WEAK_EXPENSIVE_FAVORITE,
    Alert,
    FighterStructuralFlags,
    SCOPE_FIGHTER,
    SCOPE_SLATE,
    SEVERITY_INFO,
    SEVERITY_WARN,
    fight_group_issue,
    five_round_edge,
    missing_input,
    odds_vs_salary_mismatch,
    projection_non_projectable,
    salary_inefficiency_high,
    salary_inefficiency_low,
    sort_alerts,
    underdog_value,
    weak_expensive_favorite,
)


# ---------------------------------------------------------------------------
# Output-shape & code-set invariants
# ---------------------------------------------------------------------------


def test_alert_shape_fields_design_9():
    a = Alert(
        code=ALERT_CODE_MISSING_INPUT,
        severity=SEVERITY_WARN,
        scope=SCOPE_FIGHTER,
        fighter_id=1,
        fighter_name="X",
        message="m",
        tags=("salary",),
    )
    assert a.code == ALERT_CODE_MISSING_INPUT
    assert a.severity == SEVERITY_WARN
    assert a.scope == SCOPE_FIGHTER
    assert a.fighter_id == 1
    assert a.fighter_name == "X"
    assert a.message == "m"
    assert a.tags == ("salary",)


def test_late_news_risk_code_reserved_but_not_emitted_anywhere():
    """Design §3.9 / §15 risk #8: v1 must reserve the code without
    ever emitting it. Drive every rule with a wide grid of inputs and
    assert no Alert with code == 'late_news_risk' is ever produced."""
    emitted_codes: list[str] = []

    salaries = [5000, 6500, 7000, 7500, 7600, 8000, 8500, 8900, 9000, 9499, 9500, 10500]
    pwins = [0.1, 0.3, 0.42, 0.45, 0.48, 0.5, 0.54, 0.55, 0.62, 0.65, 0.7, 0.9]
    points_list = [0.0, 5.0, 15.0, 25.0, 50.0, 75.0]
    rounds_list = [None, 3, 5]
    statuses = ["ok", "missing_inputs", "non_projectable"]
    tags_options = [(), ("salary",), ("win_probability",), ("salary", "win_probability")]

    for s in salaries:
        for p in pwins:
            for status in statuses:
                for pts in points_list:
                    for fn in (
                        salary_inefficiency_high,
                        salary_inefficiency_low,
                    ):
                        a = fn(1, "X", s, pts, status)
                        if a is not None:
                            emitted_codes.append(a.code)
                    a = odds_vs_salary_mismatch(1, "X", s, p, status)
                    if a is not None:
                        emitted_codes.append(a.code)
                    a = underdog_value(1, "X", s, p, status)
                    if a is not None:
                        emitted_codes.append(a.code)
                    a = weak_expensive_favorite(1, "X", s, p, status)
                    if a is not None:
                        emitted_codes.append(a.code)
                for r in rounds_list:
                    for status in statuses:
                        a = five_round_edge(1, "X", r, p, status)
                        if a is not None:
                            emitted_codes.append(a.code)

    for status in statuses:
        for tags in tags_options:
            a = missing_input(1, "X", status, tags)
            if a is not None:
                emitted_codes.append(a.code)
            a = projection_non_projectable(1, "X", status, tags)
            if a is not None:
                emitted_codes.append(a.code)

    flag_grids = [
        [],
        [FighterStructuralFlags("A", True, True)],
        [FighterStructuralFlags("A", False, False)],
        [FighterStructuralFlags("A", True, False)],
        [
            FighterStructuralFlags("A", True, True),
            FighterStructuralFlags("B", False, False),
        ],
    ]
    for flags in flag_grids:
        a = fight_group_issue(flags)
        if a is not None:
            emitted_codes.append(a.code)

    assert ALERT_CODE_LATE_NEWS_RISK not in emitted_codes
    assert ALERT_CODE_LATE_NEWS_RISK == "late_news_risk"


def test_no_rule_accepts_effective_status_parameter():
    """Design §8: alerts layer must never read effective_status. Pin
    that no Phase A rule function exposes such a parameter."""
    rule_fns = [
        salary_inefficiency_high,
        salary_inefficiency_low,
        odds_vs_salary_mismatch,
        underdog_value,
        weak_expensive_favorite,
        five_round_edge,
        missing_input,
        projection_non_projectable,
        fight_group_issue,
    ]
    for fn in rule_fns:
        params = inspect.signature(fn).parameters
        assert "effective_status" not in params, fn.__name__


# ---------------------------------------------------------------------------
# §3.1 Salary inefficiency
# ---------------------------------------------------------------------------


def test_salary_inefficiency_high_fires_at_threshold():
    # 5.0 pts/$1k exactly — fires (>=).
    a = salary_inefficiency_high(1, "Fighter A", 8000, 40.0, "ok")
    assert a is not None
    assert a.code == ALERT_CODE_SALARY_INEFFICIENCY_HIGH
    assert a.severity == SEVERITY_INFO
    assert a.scope == SCOPE_FIGHTER
    assert a.fighter_id == 1
    assert a.fighter_name == "Fighter A"
    assert a.tags == ()


def test_salary_inefficiency_high_near_threshold_does_not_fire():
    # 4.99 pts/$1k — does not fire.
    a = salary_inefficiency_high(1, "X", 8000, 39.9, "ok")
    assert a is None


def test_salary_inefficiency_high_skips_non_ok_status():
    a = salary_inefficiency_high(1, "X", 8000, 80.0, "missing_inputs")
    assert a is None
    a = salary_inefficiency_high(1, "X", 8000, 80.0, "non_projectable")
    assert a is None


def test_salary_inefficiency_low_fires_at_pay_up_threshold():
    # salary 8500 exactly, pts/$1k == 2.5 exactly — fires (>=8500, <=2.5).
    a = salary_inefficiency_low(2, "Fighter B", 8500, 21.25, "ok")
    assert a is not None
    assert a.code == ALERT_CODE_SALARY_INEFFICIENCY_LOW
    assert a.severity == SEVERITY_INFO


def test_salary_inefficiency_low_does_not_fire_below_pay_up_floor():
    # ppk well below 2.5 but salary 8499 — design §3.1 excludes cheap
    # low-projection chalk avoids.
    a = salary_inefficiency_low(2, "X", 8499, 10.0, "ok")
    assert a is None


def test_salary_inefficiency_low_near_threshold_does_not_fire():
    # ppk 2.51 — does not fire.
    a = salary_inefficiency_low(2, "X", 8500, 21.34, "ok")
    assert a is None


# ---------------------------------------------------------------------------
# §3.2 Odds-vs-salary mismatch (tier table)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "salary,p_win,fires",
    [
        # >= 9500 tier: fires if p_win < 0.55
        (9500, 0.54, True),
        (9500, 0.55, False),
        # 9000-9499 tier: fires if p_win < 0.50
        (9000, 0.49, True),
        (9000, 0.50, False),
        (9499, 0.49, True),
        # 8000-8999 tier: fires if p_win <= 0.42 or >= 0.70
        (8500, 0.42, True),
        (8500, 0.43, False),
        (8500, 0.70, True),
        (8500, 0.69, False),
        # 7000-7999 tier: fires if p_win >= 0.62
        (7500, 0.62, True),
        (7500, 0.61, False),
        # < 7000 tier: fires if p_win >= 0.55
        (6500, 0.55, True),
        (6500, 0.54, False),
    ],
)
def test_odds_vs_salary_mismatch_tier_table(salary, p_win, fires):
    a = odds_vs_salary_mismatch(1, "X", salary, p_win, "ok")
    if fires:
        assert a is not None
        assert a.code == ALERT_CODE_ODDS_VS_SALARY_MISMATCH
        assert a.severity == SEVERITY_INFO
        assert a.scope == SCOPE_FIGHTER
    else:
        assert a is None


def test_odds_vs_salary_mismatch_skips_non_ok():
    a = odds_vs_salary_mismatch(1, "X", 9500, 0.4, "missing_inputs")
    assert a is None


# ---------------------------------------------------------------------------
# §3.3 Underdog value (mirrors docs/DEVELOPMENT_NOTES.md §4 value_gap_bonus)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "salary,p_win,fires",
    [
        (7600, 0.45, True),
        (7600, 0.44, False),
        (7601, 0.45, False),  # outside cheapest tier and below middle tier p
        (8000, 0.48, True),
        (8000, 0.47, False),
        (8500, 0.55, True),
        (8500, 0.54, False),
        (8501, 0.99, False),  # above all tiers
    ],
)
def test_underdog_value_mirrors_value_gap_bonus_thresholds(salary, p_win, fires):
    a = underdog_value(1, "X", salary, p_win, "ok")
    if fires:
        assert a is not None
        assert a.code == ALERT_CODE_UNDERDOG_VALUE
        assert a.severity == SEVERITY_INFO
    else:
        assert a is None


def test_underdog_value_pins_value_gap_constants_against_value_bonus_module():
    """Phase A test plan §13 cross-cutting: underdog value triggers
    must match the value_gap_bonus tiers. Encoded as a behavioral
    pin — a drift in either source fails this test."""
    from src.projections.value_bonus import value_gap_bonus

    # Each tuple is a known value_gap_bonus tier boundary.
    cases = [
        (7600, 0.45),
        (8000, 0.48),
        (8500, 0.55),
    ]
    for salary, p_win in cases:
        assert value_gap_bonus(salary, p_win) > 0
        assert underdog_value(1, "X", salary, p_win, "ok") is not None


# ---------------------------------------------------------------------------
# §3.4 Weak expensive favorite
# ---------------------------------------------------------------------------


def test_weak_expensive_favorite_fires_at_design_example():
    a = weak_expensive_favorite(1, "X", 9000, 0.54, "ok")
    assert a is not None
    assert a.code == ALERT_CODE_WEAK_EXPENSIVE_FAVORITE
    assert a.severity == SEVERITY_INFO
    assert a.scope == SCOPE_FIGHTER


def test_weak_expensive_favorite_does_not_fire_at_pwin_threshold():
    a = weak_expensive_favorite(1, "X", 9000, 0.55, "ok")
    assert a is None


def test_weak_expensive_favorite_requires_min_salary():
    a = weak_expensive_favorite(1, "X", 8999, 0.40, "ok")
    assert a is None


def test_weak_expensive_favorite_skips_non_ok():
    a = weak_expensive_favorite(1, "X", 9500, 0.40, "missing_inputs")
    assert a is None


# ---------------------------------------------------------------------------
# §3.5 Five-round edge
# ---------------------------------------------------------------------------


def test_five_round_edge_fires_with_high_pwin():
    a = five_round_edge(1, "X", 5, 0.60, "ok")
    assert a is not None
    assert a.code == ALERT_CODE_FIVE_ROUND_EDGE
    assert a.severity == SEVERITY_INFO


def test_five_round_edge_does_not_fire_low_pwin():
    a = five_round_edge(1, "X", 5, 0.54, "ok")
    assert a is None


def test_five_round_edge_does_not_fire_three_round_high_pwin():
    a = five_round_edge(1, "X", 3, 0.80, "ok")
    assert a is None


def test_five_round_edge_does_not_fire_when_rounds_missing():
    # design §3.5: must NOT fire when scheduled_rounds is missing —
    # that case is owned by §3.6.
    a = five_round_edge(1, "X", None, 0.80, "ok")
    assert a is None


def test_five_round_edge_skips_non_ok():
    a = five_round_edge(1, "X", 5, 0.80, "missing_inputs")
    assert a is None


# ---------------------------------------------------------------------------
# §3.6 Missing input
# ---------------------------------------------------------------------------


def test_missing_input_emits_one_alert_with_all_tags():
    a = missing_input(
        7, "Fighter F", "missing_inputs", ("salary", "win_probability")
    )
    assert a is not None
    assert a.code == ALERT_CODE_MISSING_INPUT
    assert a.severity == SEVERITY_WARN
    assert a.scope == SCOPE_FIGHTER
    assert a.fighter_id == 7
    assert a.fighter_name == "Fighter F"
    assert a.tags == ("salary", "win_probability")


def test_missing_input_does_not_fire_when_status_ok():
    a = missing_input(7, "X", "ok", ())
    assert a is None


def test_missing_input_does_not_fire_when_status_non_projectable():
    a = missing_input(7, "X", "non_projectable", ("fight_group",))
    assert a is None


# ---------------------------------------------------------------------------
# §3.7 Projection non-projectable
# ---------------------------------------------------------------------------


def test_projection_non_projectable_emits_one_alert_per_fighter():
    a = projection_non_projectable(
        9, "Fighter G", "non_projectable", ("fight_group", "opponent")
    )
    assert a is not None
    assert a.code == ALERT_CODE_PROJECTION_NON_PROJECTABLE
    assert a.severity == SEVERITY_WARN
    assert a.scope == SCOPE_FIGHTER
    assert a.tags == ("fight_group", "opponent")


def test_projection_non_projectable_does_not_fire_when_ok():
    a = projection_non_projectable(1, "X", "ok", ())
    assert a is None


def test_projection_non_projectable_and_fight_group_issue_both_fire_for_same_root_cause():
    """Design §15 risk #5: per-fighter §3.7 and slate-scoped §3.8 are
    expected to both fire when a fighter lacks a fight group. The
    duplication is intentional and pinned here so an 'optimization'
    that hides one cannot land silently."""
    per_fighter = projection_non_projectable(
        1, "A", "non_projectable", ("fight_group",)
    )
    slate = fight_group_issue(
        [FighterStructuralFlags("A", False, False)]
    )
    assert per_fighter is not None
    assert slate is not None


# ---------------------------------------------------------------------------
# §3.8 Fight-group / opponent issue
# ---------------------------------------------------------------------------


def test_fight_group_issue_empty_slate_returns_none():
    assert fight_group_issue([]) is None


def test_fight_group_issue_all_resolved_returns_none():
    a = fight_group_issue(
        [
            FighterStructuralFlags("A", True, True),
            FighterStructuralFlags("B", True, True),
        ]
    )
    assert a is None


def test_fight_group_issue_missing_fight_group_fires_slate_scoped():
    a = fight_group_issue(
        [
            FighterStructuralFlags("A", True, True),
            FighterStructuralFlags("B", False, False),
        ]
    )
    assert a is not None
    assert a.code == ALERT_CODE_FIGHT_GROUP_ISSUE
    assert a.severity == SEVERITY_WARN
    assert a.scope == SCOPE_SLATE
    assert a.fighter_id is None
    assert a.fighter_name is None
    assert a.tags == ("B",)


def test_fight_group_issue_has_group_but_missing_opponent_fires():
    a = fight_group_issue(
        [FighterStructuralFlags("Solo Fighter", True, False)]
    )
    assert a is not None
    assert a.scope == SCOPE_SLATE
    assert a.tags == ("Solo Fighter",)


def test_fight_group_issue_lists_affected_names_deterministically():
    a = fight_group_issue(
        [
            FighterStructuralFlags("Charlie", False, False),
            FighterStructuralFlags("Alpha", True, False),
            FighterStructuralFlags("Bravo", True, True),
        ]
    )
    assert a is not None
    assert a.tags == ("Alpha", "Charlie")
    assert "Alpha" in a.message and "Charlie" in a.message
    assert "Bravo" not in a.message


# ---------------------------------------------------------------------------
# §9 deterministic ordering
# ---------------------------------------------------------------------------


def _mk(code, severity, scope, fid=None):
    return Alert(
        code=code,
        severity=severity,
        scope=scope,
        fighter_id=fid,
        fighter_name=None if scope == SCOPE_SLATE else f"F{fid}",
        message="m",
        tags=(),
    )


def test_sort_alerts_pins_design_9_ordering():
    inputs = [
        _mk(ALERT_CODE_UNDERDOG_VALUE, SEVERITY_INFO, SCOPE_FIGHTER, fid=3),
        _mk(ALERT_CODE_MISSING_INPUT, SEVERITY_WARN, SCOPE_FIGHTER, fid=2),
        _mk(ALERT_CODE_FIGHT_GROUP_ISSUE, SEVERITY_WARN, SCOPE_SLATE),
        _mk(ALERT_CODE_UNDERDOG_VALUE, SEVERITY_INFO, SCOPE_FIGHTER, fid=1),
        _mk(
            ALERT_CODE_PROJECTION_NON_PROJECTABLE,
            SEVERITY_WARN,
            SCOPE_FIGHTER,
            fid=5,
        ),
        _mk(ALERT_CODE_FIVE_ROUND_EDGE, SEVERITY_INFO, SCOPE_FIGHTER, fid=4),
    ]
    sorted_alerts = sort_alerts(inputs)
    sequence = [(a.severity, a.scope, a.code, a.fighter_id) for a in sorted_alerts]
    assert sequence == [
        (SEVERITY_WARN, SCOPE_SLATE, ALERT_CODE_FIGHT_GROUP_ISSUE, None),
        (SEVERITY_WARN, SCOPE_FIGHTER, ALERT_CODE_MISSING_INPUT, 2),
        (
            SEVERITY_WARN,
            SCOPE_FIGHTER,
            ALERT_CODE_PROJECTION_NON_PROJECTABLE,
            5,
        ),
        (SEVERITY_INFO, SCOPE_FIGHTER, ALERT_CODE_FIVE_ROUND_EDGE, 4),
        (SEVERITY_INFO, SCOPE_FIGHTER, ALERT_CODE_UNDERDOG_VALUE, 1),
        (SEVERITY_INFO, SCOPE_FIGHTER, ALERT_CODE_UNDERDOG_VALUE, 3),
    ]


def test_sort_alerts_is_stable_for_equal_keys():
    a1 = _mk(ALERT_CODE_UNDERDOG_VALUE, SEVERITY_INFO, SCOPE_FIGHTER, fid=1)
    a2 = _mk(ALERT_CODE_UNDERDOG_VALUE, SEVERITY_INFO, SCOPE_FIGHTER, fid=1)
    result = sort_alerts([a1, a2])
    assert result[0] is a1
    assert result[1] is a2


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_v1_alert_code_set_matches_design_9():
    expected = {
        "salary_inefficiency_high",
        "salary_inefficiency_low",
        "odds_vs_salary_mismatch",
        "underdog_value",
        "weak_expensive_favorite",
        "five_round_edge",
        "missing_input",
        "projection_non_projectable",
        "fight_group_issue",
        "late_news_risk",
    }
    declared = {
        getattr(alert_rules, name)
        for name in dir(alert_rules)
        if name.startswith("ALERT_CODE_")
    }
    assert declared == expected
