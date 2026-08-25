"""Phase A tests for ``src/slate/manual_review.py``.

Pins MANUAL_REVIEW_GATE_V1_DESIGN.md §4 categories, the §5 check id /
category mapping, the §5.4.a threshold constant, and every Phase A
pure per-check evaluator. Pure-only: no DB, no Streamlit, no service.
"""

from __future__ import annotations

import pytest

from src.slate import manual_review as mr


V1_CHECK_CODES = {
    "salary_imported",
    "fight_group_coverage",
    "fight_group_review",
    "scheduled_rounds_reviewed",
    "odds_unmatched_active",
    "odds_coverage_partial",
    "odds_match_review",
    "odds_coverage_stat",
    "projection_non_projectable",
    "projection_missing_inputs",
    "mismatch_alerts_warn",
    "mismatch_alerts_info",
    "late_news_risk_locked",
    "fighter_status_review",
    "late_news_acknowledged",
    "manual_review_user_ack",
}


EXPECTED_CATEGORY = {
    "salary_imported": "blocking",
    "fight_group_coverage": "blocking",
    "fight_group_review": "warning",
    "scheduled_rounds_reviewed": "warning",
    "odds_unmatched_active": "blocking",
    "odds_coverage_partial": "warning",
    "odds_match_review": "warning",
    "odds_coverage_stat": "informational",
    "projection_non_projectable": "blocking",
    "projection_missing_inputs": "warning",
    "mismatch_alerts_warn": "warning",
    "mismatch_alerts_info": "informational",
    "late_news_risk_locked": "informational",
    "fighter_status_review": "informational",
    "late_news_acknowledged": "warning",
    "manual_review_user_ack": "blocking",
}


# --- §4 category constants --------------------------------------------------


def test_category_constants():
    assert mr.CATEGORY_BLOCKING == "blocking"
    assert mr.CATEGORY_WARNING == "warning"
    assert mr.CATEGORY_INFORMATIONAL == "informational"


def test_allowed_categories_is_exactly_three():
    assert set(mr.ALLOWED_CATEGORIES) == {"blocking", "warning", "informational"}
    assert isinstance(mr.ALLOWED_CATEGORIES, frozenset)


# --- §5 check code constants + category mapping -----------------------------


def test_check_codes_module_constants():
    for code in V1_CHECK_CODES:
        const_name = "CHECK_" + code.upper()
        assert getattr(mr, const_name) == code, const_name


def test_allowed_checks_is_exactly_the_v1_set():
    assert set(mr.ALLOWED_CHECKS) == V1_CHECK_CODES
    assert isinstance(mr.ALLOWED_CHECKS, frozenset)


def test_check_category_contains_every_check_with_no_extras():
    assert set(mr.CHECK_CATEGORY.keys()) == V1_CHECK_CODES


def test_check_category_values_are_in_allowed_categories():
    for code, cat in mr.CHECK_CATEGORY.items():
        assert cat in mr.ALLOWED_CATEGORIES, code


def test_check_category_matches_design():
    assert mr.CHECK_CATEGORY == EXPECTED_CATEGORY


def test_each_check_belongs_to_exactly_one_category():
    # CHECK_CATEGORY is a dict so single-key lookup already enforces this;
    # restate for the reader.
    for code in V1_CHECK_CODES:
        category = mr.CHECK_CATEGORY[code]
        assert isinstance(category, str)


# --- predicate helpers ------------------------------------------------------


def test_is_blocking_positive_and_negative():
    blocking = {c for c, cat in EXPECTED_CATEGORY.items() if cat == "blocking"}
    for c in blocking:
        assert mr.is_blocking(c) is True, c
    for c in V1_CHECK_CODES - blocking:
        assert mr.is_blocking(c) is False, c


def test_is_warning_positive_and_negative():
    warning = {c for c, cat in EXPECTED_CATEGORY.items() if cat == "warning"}
    for c in warning:
        assert mr.is_warning(c) is True, c
    for c in V1_CHECK_CODES - warning:
        assert mr.is_warning(c) is False, c


def test_is_informational_positive_and_negative():
    info = {c for c, cat in EXPECTED_CATEGORY.items() if cat == "informational"}
    for c in info:
        assert mr.is_informational(c) is True, c
    for c in V1_CHECK_CODES - info:
        assert mr.is_informational(c) is False, c


def test_category_for_unknown_check_raises():
    with pytest.raises(ValueError):
        mr.category_for("not_a_check")


def test_is_blocking_unknown_check_raises():
    with pytest.raises(ValueError):
        mr.is_blocking("not_a_check")


# --- §5.4.a threshold constant ----------------------------------------------


def test_blocking_threshold_odds_unmatched_pct_is_pinned_at_50pct():
    assert mr.BLOCKING_THRESHOLD_ODDS_UNMATCHED_PCT == 0.5


# --- ReviewCheckResult value object -----------------------------------------


def test_review_check_result_is_frozen_dataclass():
    r = mr.ReviewCheckResult(
        code=mr.CHECK_SALARY_IMPORTED,
        category=mr.CATEGORY_BLOCKING,
        status=mr.STATUS_FAIL,
        message="msg",
    )
    with pytest.raises(Exception):
        r.message = "other"  # type: ignore[misc]
    assert r.tags == ()


# --- summary aggregation + ready bit ----------------------------------------


def _ok_salary():
    return mr.evaluate_salary_imported("validated", 26, 26)


def _failing_salary():
    return mr.evaluate_salary_imported(None, 0, 0)


def test_summary_counts_categories():
    results = [
        _ok_salary(),  # blocking pass
        mr.evaluate_fight_group_review(0),  # warning pass
        mr.evaluate_odds_coverage_stat(26, 26),  # info
        mr.evaluate_late_news_risk_locked(),  # info
    ]
    s = mr.summarize(results)
    assert s.blocking_count == 1
    assert s.warning_count == 1
    assert s.info_count == 2


def test_summary_ready_true_when_no_blocking_failures():
    results = [_ok_salary(), mr.evaluate_fight_group_review(0)]
    assert mr.summarize(results).ready is True


def test_summary_ready_false_when_any_blocking_fails():
    results = [_failing_salary(), mr.evaluate_fight_group_review(0)]
    assert mr.summarize(results).ready is False


def test_summary_warning_only_state_is_ready():
    # A warning-only state must still be "ready" (Mark Reviewed enabled).
    results = [
        _ok_salary(),
        mr.evaluate_fight_group_review(2),  # warning fail
    ]
    s = mr.summarize(results)
    assert s.ready is True
    assert mr.has_warning_findings(results) is True
    assert mr.has_blocking_findings(results) is False


def test_summary_info_only_state_is_ready():
    # Informational rows never affect readiness.
    results = [
        _ok_salary(),
        mr.evaluate_odds_coverage_stat(26, 26),
        mr.evaluate_mismatch_alerts_info(3, ["underdog_value"]),
    ]
    assert mr.summarize(results).ready is True


def test_has_blocking_findings_true_when_blocking_failure_present():
    assert mr.has_blocking_findings([_failing_salary()]) is True
    assert mr.has_blocking_findings([_ok_salary()]) is False


# --- §5.1 salary imported ---------------------------------------------------


def test_salary_imported_passes_when_validated_with_active_fighters():
    r = mr.evaluate_salary_imported("validated", 26, 26)
    assert r.status == mr.STATUS_PASS
    assert r.category == mr.CATEGORY_BLOCKING
    assert r.code == mr.CHECK_SALARY_IMPORTED


def test_salary_imported_fails_when_missing():
    r = mr.evaluate_salary_imported(None, 0, 0)
    assert r.status == mr.STATUS_FAIL


def test_salary_imported_fails_when_row_count_zero():
    r = mr.evaluate_salary_imported("validated", 0, 0)
    assert r.status == mr.STATUS_FAIL


def test_salary_imported_fails_when_no_active_fighters():
    r = mr.evaluate_salary_imported("validated", 26, 0)
    assert r.status == mr.STATUS_FAIL


def test_salary_imported_low_fighter_count_passes_if_at_least_one_active():
    # "Low fighter count" is not by itself a Blocking failure in v1; the
    # downstream §5.4 odds threshold is what catches an under-populated
    # slate. Pin the contract so a future change is intentional.
    r = mr.evaluate_salary_imported("validated", 1, 1)
    assert r.status == mr.STATUS_PASS


# --- §5.2.a fight-group coverage --------------------------------------------


def test_fight_group_coverage_passes_when_every_fighter_paired():
    r = mr.evaluate_fight_group_coverage([], 26)
    assert r.status == mr.STATUS_PASS


def test_fight_group_coverage_fails_when_a_fighter_has_no_group():
    r = mr.evaluate_fight_group_coverage(["Khabib"], 26)
    assert r.status == mr.STATUS_FAIL
    assert "Khabib" in r.message
    assert r.tags == ("Khabib",)


def test_fight_group_coverage_fails_when_active_count_is_odd():
    # Odd active count means at least one fighter is unpaired — caller
    # surfaces them via the missing-group list. The check fails.
    r = mr.evaluate_fight_group_coverage(["A"], 27)
    assert r.status == mr.STATUS_FAIL


def test_fight_group_coverage_truncates_long_lists():
    names = [f"f{i}" for i in range(15)]
    r = mr.evaluate_fight_group_coverage(names, 30)
    assert r.status == mr.STATUS_FAIL
    assert "+ 5 more" in r.message


# --- §5.2.b fight-group review ----------------------------------------------


def test_fight_group_review_passes_when_zero():
    assert mr.evaluate_fight_group_review(0).status == mr.STATUS_PASS


def test_fight_group_review_fails_when_any_unconfirmed_or_one_sided():
    r = mr.evaluate_fight_group_review(3)
    assert r.status == mr.STATUS_FAIL
    assert "3" in r.message


# --- §5.3 scheduled rounds review -------------------------------------------


def test_scheduled_rounds_passes_when_no_five_round_and_no_unconfirmed_three():
    r = mr.evaluate_scheduled_rounds_reviewed(False, 0)
    assert r.status == mr.STATUS_PASS


def test_scheduled_rounds_warns_when_five_round_group_present():
    r = mr.evaluate_scheduled_rounds_reviewed(True, 0)
    assert r.status == mr.STATUS_FAIL


def test_scheduled_rounds_warns_when_unconfirmed_three_round_present():
    r = mr.evaluate_scheduled_rounds_reviewed(False, 2)
    assert r.status == mr.STATUS_FAIL


def test_scheduled_rounds_acknowledged_dismisses_five_round_warning():
    # A confirmed card with a 5-round main event: the warning is dismissed once
    # the user ticks the rounds-reviewed ack.
    assert (
        mr.evaluate_scheduled_rounds_reviewed(True, 0, acknowledged=True).status
        == mr.STATUS_PASS
    )


def test_scheduled_rounds_ack_does_not_override_unconfirmed_groups():
    # The ack only dismisses the 5-round nudge; unconfirmed groups still warn.
    assert (
        mr.evaluate_scheduled_rounds_reviewed(True, 2, acknowledged=True).status
        == mr.STATUS_FAIL
    )
    assert (
        mr.evaluate_scheduled_rounds_reviewed(False, 2, acknowledged=True).status
        == mr.STATUS_FAIL
    )


# --- §5.4.a / §5.4.b odds coverage ------------------------------------------


def test_odds_unmatched_active_passes_when_full_coverage():
    r = mr.evaluate_odds_unmatched_active(26, 26)
    assert r.status == mr.STATUS_PASS


def test_odds_unmatched_active_passes_just_below_threshold():
    # 26 active, 14 matched -> 12 uncovered (~46%) -> below 50%, pass.
    r = mr.evaluate_odds_unmatched_active(26, 14)
    assert r.status == mr.STATUS_PASS


def test_odds_unmatched_active_fails_at_threshold():
    # 26 active, 13 matched -> 13 uncovered (50%) -> fail.
    r = mr.evaluate_odds_unmatched_active(26, 13)
    assert r.status == mr.STATUS_FAIL


def test_odds_unmatched_active_fails_above_threshold():
    r = mr.evaluate_odds_unmatched_active(26, 5)
    assert r.status == mr.STATUS_FAIL


def test_odds_unmatched_active_skips_when_no_active_fighters():
    r = mr.evaluate_odds_unmatched_active(0, 0)
    assert r.status == mr.STATUS_PASS


def test_odds_coverage_partial_passes_at_full_coverage():
    assert mr.evaluate_odds_coverage_partial(26, 26).status == mr.STATUS_PASS


def test_odds_coverage_partial_warns_below_threshold_with_some_gap():
    # 26 active, 24 matched -> 2 uncovered (~7.7%) -> partial warning.
    r = mr.evaluate_odds_coverage_partial(26, 24)
    assert r.status == mr.STATUS_FAIL


def test_odds_coverage_partial_defers_to_blocking_at_or_above_threshold():
    # When the §5.4.a Blocking threshold is met, §5.4.b is the no-op
    # (the failure surface belongs to §5.4.a).
    r = mr.evaluate_odds_coverage_partial(26, 13)
    assert r.status == mr.STATUS_PASS


# --- §5.4.c odds match review ----------------------------------------------


def test_odds_match_review_passes_when_no_pending_or_rejected():
    assert mr.evaluate_odds_match_review(0, 0).status == mr.STATUS_PASS


def test_odds_match_review_warns_on_review_required():
    r = mr.evaluate_odds_match_review(2, 0)
    assert r.status == mr.STATUS_FAIL
    assert "review_required: 2" in r.message
    assert "review_rejected: 0" in r.message


def test_odds_match_review_warns_on_review_rejected():
    r = mr.evaluate_odds_match_review(0, 1)
    assert r.status == mr.STATUS_FAIL


# --- §5.4.d odds coverage stat (informational) ------------------------------


def test_odds_coverage_stat_is_always_info():
    r = mr.evaluate_odds_coverage_stat(26, 26)
    assert r.status == mr.STATUS_INFO
    assert r.category == mr.CATEGORY_INFORMATIONAL
    assert "100%" in r.message


def test_odds_coverage_stat_handles_zero_active():
    r = mr.evaluate_odds_coverage_stat(0, 0)
    assert r.status == mr.STATUS_INFO


# --- §5.5.a non-projectable -------------------------------------------------


def test_projection_non_projectable_passes_when_none():
    assert (
        mr.evaluate_projection_non_projectable([]).status == mr.STATUS_PASS
    )


def test_projection_non_projectable_fails_with_tags():
    r = mr.evaluate_projection_non_projectable(
        [("Khabib", ("fight_group",)), ("Conor", ("opponent",))]
    )
    assert r.status == mr.STATUS_FAIL
    assert "Conor" in r.message  # alphabetical: Conor before Khabib
    assert "Khabib" in r.message
    assert r.tags == ("Conor", "Khabib")


# --- §5.5.b missing inputs --------------------------------------------------


def test_projection_missing_inputs_passes_when_empty():
    assert (
        mr.evaluate_projection_missing_inputs([]).status == mr.STATUS_PASS
    )


def test_projection_missing_inputs_warns_with_names():
    r = mr.evaluate_projection_missing_inputs(["Conor", "Khabib"])
    assert r.status == mr.STATUS_FAIL
    assert r.tags == ("Conor", "Khabib")


# --- §5.6.a / §5.6.b alert checks -------------------------------------------


def test_mismatch_alerts_warn_passes_when_zero():
    r = mr.evaluate_mismatch_alerts_warn(0, [])
    assert r.status == mr.STATUS_PASS


def test_mismatch_alerts_warn_fails_with_codes():
    r = mr.evaluate_mismatch_alerts_warn(2, ["missing_input", "fight_group_issue"])
    assert r.status == mr.STATUS_FAIL
    assert r.tags == ("fight_group_issue", "missing_input")


def test_mismatch_alerts_info_is_informational():
    r = mr.evaluate_mismatch_alerts_info(3, ["underdog_value", "underdog_value"])
    assert r.status == mr.STATUS_INFO
    assert r.tags == ("underdog_value",)


# --- §5.6.c late_news_risk reserved -----------------------------------------


def test_late_news_risk_locked_is_informational():
    r = mr.evaluate_late_news_risk_locked()
    assert r.status == mr.STATUS_INFO
    assert r.category == mr.CATEGORY_INFORMATIONAL
    assert "reserved" in r.message


# --- §5.7 fighter status (deferred) -----------------------------------------


def test_fighter_status_review_is_informational_in_v1_regardless_of_counts():
    # v1 ignores the counts entirely. Pin the contract so the Phase F
    # promotion is a deliberate code change.
    for blocking, warning in [(0, 0), (5, 2), (0, 9)]:
        r = mr.evaluate_fighter_status_review(blocking, warning)
        assert r.status == mr.STATUS_INFO
        assert r.category == mr.CATEGORY_INFORMATIONAL
        assert "not yet active" in r.message


# --- §5.8 late-news acknowledged --------------------------------------------


def test_late_news_unchecked_warns():
    r = mr.evaluate_late_news_acknowledged(False)
    assert r.status == mr.STATUS_FAIL
    assert r.category == mr.CATEGORY_WARNING


def test_late_news_checked_passes():
    r = mr.evaluate_late_news_acknowledged(True, "2026-05-27T18:00:00Z")
    assert r.status == mr.STATUS_PASS
    assert "2026-05-27T18:00:00Z" in r.message


# --- §5.9 manual review user ack --------------------------------------------


def test_manual_review_user_ack_fails_when_not_reviewed():
    r = mr.evaluate_manual_review_user_ack("not_reviewed")
    assert r.status == mr.STATUS_FAIL
    assert r.category == mr.CATEGORY_BLOCKING


def test_manual_review_user_ack_passes_when_reviewed():
    r = mr.evaluate_manual_review_user_ack("reviewed", "2026-05-27 18:00:00")
    assert r.status == mr.STATUS_PASS
    assert "2026-05-27 18:00:00" in r.message


def test_manual_review_user_ack_is_blocking_until_clicked():
    # Without explicit completion, the slate cannot be ready.
    findings = [mr.evaluate_manual_review_user_ack("not_reviewed")]
    assert mr.summarize(findings).ready is False


# --- deterministic ordering -------------------------------------------------


def test_sort_results_orders_blocking_before_warning_before_info():
    results = [
        mr.evaluate_odds_coverage_stat(26, 26),  # info
        mr.evaluate_fight_group_review(2),  # warning
        _failing_salary(),  # blocking
    ]
    sorted_codes = [r.code for r in mr.sort_results(results)]
    cat_order = [mr.CHECK_CATEGORY[c] for c in sorted_codes]
    # First category must be blocking, then warning, then informational.
    assert cat_order == ["blocking", "warning", "informational"]


def test_sort_results_is_deterministic_across_call_order():
    a = [
        mr.evaluate_fight_group_review(1),
        mr.evaluate_salary_imported("validated", 26, 26),
        mr.evaluate_odds_coverage_stat(26, 26),
    ]
    b = list(reversed(a))
    assert [r.code for r in mr.sort_results(a)] == [
        r.code for r in mr.sort_results(b)
    ]


# --- pure-module invariants -------------------------------------------------


def test_module_has_no_db_or_streamlit_imports():
    import src.slate.manual_review as module

    src = module.__file__
    assert src is not None
    with open(src, "r", encoding="utf-8") as fh:
        text = fh.read()
    forbidden = [
        "import sqlite3",
        "from sqlite3",
        "import streamlit",
        "from streamlit",
        "from src.db",
        "import src.db",
    ]
    for needle in forbidden:
        assert needle not in text, f"manual_review.py must not contain {needle!r}"


def test_module_does_not_import_odds_match_results_repository():
    # The §14 separation: this module must not pull in the odds match
    # repository or any concept that would require an effective_status
    # read. The pure evaluators receive primitive counts only.
    import src.slate.manual_review as module

    with open(module.__file__, "r", encoding="utf-8") as fh:
        text = fh.read()
    forbidden = [
        "OddsMatchResultRepository",
        "ManualMatchOverrideRepository",
        "from src.slate.fighter_status_service",
        "from src.alerts.alert_service",
        "from src.projections.slate_projection_service",
    ]
    for needle in forbidden:
        assert needle not in text, f"manual_review.py must not contain {needle!r}"
