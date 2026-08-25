"""Unit tests for the pure Home Dashboard helpers.

Pins ``docs/HOME_DASHBOARD_UX_DESIGN.md`` §5 (next-action precedence)
and §3.4 (workflow checklist). Pure-only: no DB, no Streamlit, no
service composition — every ``ReviewReadiness`` is hand-built from the
Phase A evaluators in ``src.slate.manual_review`` (the way
``manual_review.py`` itself is unit-tested).
"""

from __future__ import annotations

from src.slate import home_dashboard as hd
from src.slate import manual_review as mr
from src.slate.manual_review_service import ReviewReadiness


# ---------------------------------------------------------------------------
# ReviewReadiness builder from a list of Phase A check results
# ---------------------------------------------------------------------------


def _readiness(
    checks: list[mr.ReviewCheckResult],
    *,
    slate_id: int = 1,
    manual_review_status: str = "not_reviewed",
) -> ReviewReadiness:
    ordered = mr.sort_results(checks)
    return ReviewReadiness(
        slate_id=slate_id,
        manual_review_status=manual_review_status,
        manual_review_completed_at=None,
        checks=tuple(ordered),
        summary=mr.summarize(ordered),
    )


def _all_blocking_pass() -> list[mr.ReviewCheckResult]:
    """Every Blocking check passing except ``manual_review_user_ack``,
    which the caller flips depending on the scenario."""
    return [
        mr.evaluate_salary_imported("validated", 26, 26),
        mr.evaluate_fight_group_coverage([], 26),
        mr.evaluate_odds_unmatched_active(26, 26),
        mr.evaluate_projection_non_projectable([]),
    ]


# ---------------------------------------------------------------------------
# §5 — next-action precedence
# ---------------------------------------------------------------------------


def test_no_slates_recommends_slate_setup():
    empty = _readiness([])
    action = hd.recommend_next_action(empty, has_slates=False)
    assert action.code == "slate_setup"
    assert action.page is hd.PAGE_SLATE_SETUP


def test_salary_missing_recommends_slate_setup():
    checks = [
        mr.evaluate_salary_imported(None, 0, 0),
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    action = hd.recommend_next_action(_readiness(checks), has_slates=True)
    assert action.code == "slate_setup"
    assert action.page is hd.PAGE_SLATE_SETUP


def test_fight_group_issue_recommends_fight_groups():
    checks = [
        mr.evaluate_salary_imported("validated", 26, 26),
        mr.evaluate_fight_group_coverage(["Khabib"], 26),
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    action = hd.recommend_next_action(_readiness(checks), has_slates=True)
    assert action.code == "fight_groups"
    assert action.page is hd.PAGE_FIGHT_GROUPS


def test_odds_issue_recommends_odds():
    checks = [
        mr.evaluate_salary_imported("validated", 26, 26),
        mr.evaluate_fight_group_coverage([], 26),
        mr.evaluate_odds_unmatched_active(26, 5),  # well above the threshold
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    action = hd.recommend_next_action(_readiness(checks), has_slates=True)
    assert action.code == "odds"
    assert action.page is hd.PAGE_ODDS


def test_projection_issue_recommends_projections():
    checks = [
        mr.evaluate_salary_imported("validated", 26, 26),
        mr.evaluate_fight_group_coverage([], 26),
        mr.evaluate_odds_unmatched_active(26, 26),
        mr.evaluate_projection_non_projectable([("Khabib", ("fight_group",))]),
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    action = hd.recommend_next_action(_readiness(checks), has_slates=True)
    assert action.code == "projections"
    assert action.page is hd.PAGE_PROJECTIONS


def test_precedence_salary_beats_fight_group():
    # Both Blocking checks fail — salary import wins by rank order.
    checks = [
        mr.evaluate_salary_imported(None, 0, 0),
        mr.evaluate_fight_group_coverage(["Khabib"], 26),
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    action = hd.recommend_next_action(_readiness(checks), has_slates=True)
    assert action.code == "slate_setup"


def test_manual_review_ack_missing_recommends_manual_review():
    # Every structural Blocking check passes; only the user-ack remains.
    checks = _all_blocking_pass() + [
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    action = hd.recommend_next_action(_readiness(checks), has_slates=True)
    assert action.code == "manual_review"
    assert action.page is hd.PAGE_MANUAL_REVIEW


def test_warnings_do_not_override_manual_review_routing():
    # Structural Blocking clean + outstanding Warnings → still Manual
    # Review (warnings never block the recommendation, design §5 notes).
    checks = _all_blocking_pass() + [
        mr.evaluate_fight_group_review(2),  # warning fail
        mr.evaluate_projection_missing_inputs(["Conor"]),  # warning fail
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    readiness = _readiness(checks)
    assert readiness.summary.ready is False
    action = hd.recommend_next_action(readiness, has_slates=True)
    assert action.code == "manual_review"


def test_ready_slate_recommends_optimizer():
    checks = _all_blocking_pass() + [
        mr.evaluate_manual_review_user_ack("reviewed", "2026-05-27 18:00:00"),
    ]
    readiness = _readiness(checks, manual_review_status="reviewed")
    assert readiness.summary.ready is True
    action = hd.recommend_next_action(readiness, has_slates=True)
    assert action.code == "optimizer"
    assert action.page is hd.PAGE_OPTIMIZER


def test_ready_with_warnings_still_recommends_optimizer():
    # ``summary.ready`` only cares about Blocking checks; Warnings present
    # but the gate is green → Optimizer.
    checks = _all_blocking_pass() + [
        mr.evaluate_fight_group_review(1),  # warning fail
        mr.evaluate_manual_review_user_ack("reviewed", "2026-05-27 18:00:00"),
    ]
    readiness = _readiness(checks, manual_review_status="reviewed")
    assert readiness.summary.ready is True
    action = hd.recommend_next_action(readiness, has_slates=True)
    assert action.code == "optimizer"


# ---------------------------------------------------------------------------
# §3.4 — workflow checklist
# ---------------------------------------------------------------------------


def test_checklist_has_one_row_per_workflow_page_in_order():
    rows = hd.build_workflow_checklist(_readiness(_all_blocking_pass()))
    labels = [r.page.label for r in rows]
    assert labels == [
        "01 Slate Setup",
        "02 Fight Groups",
        "03 Odds",
        "09 Projections",
        "05 Alerts",
        "04 Fighter Status",
        "06 Manual Review",
        "07 Optimizer",
        "08 Export & Run Log",
    ]


def test_checklist_salary_block_marks_downstream_not_started():
    checks = [
        mr.evaluate_salary_imported(None, 0, 0),
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    rows = {r.page.label: r for r in hd.build_workflow_checklist(_readiness(checks))}
    assert rows["01 Slate Setup"].status == hd.ROW_BLOCK
    # Downstream structural rows must not show a spurious pass when the
    # salary import (their prerequisite) has not run.
    assert rows["02 Fight Groups"].status == hd.ROW_NOT_STARTED
    assert rows["03 Odds"].status == hd.ROW_NOT_STARTED
    assert rows["09 Projections"].status == hd.ROW_NOT_STARTED


def test_checklist_fight_group_block_after_salary_import():
    checks = [
        mr.evaluate_salary_imported("validated", 26, 26),
        mr.evaluate_fight_group_coverage(["Khabib"], 26),
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    rows = {r.page.label: r for r in hd.build_workflow_checklist(_readiness(checks))}
    assert rows["01 Slate Setup"].status == hd.ROW_PASS
    assert rows["02 Fight Groups"].status == hd.ROW_BLOCK


def test_checklist_warning_row_shows_warn():
    checks = _all_blocking_pass() + [
        mr.evaluate_fight_group_review(2),  # warning fail
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    rows = {r.page.label: r for r in hd.build_workflow_checklist(_readiness(checks))}
    assert rows["02 Fight Groups"].status == hd.ROW_WARN


def test_checklist_gate_rows_locked_until_ready():
    not_ready = _all_blocking_pass() + [
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    rows = {r.page.label: r for r in hd.build_workflow_checklist(_readiness(not_ready))}
    assert rows["07 Optimizer"].status == hd.ROW_NOT_STARTED
    assert rows["08 Export & Run Log"].status == hd.ROW_NOT_STARTED

    ready = _all_blocking_pass() + [
        mr.evaluate_manual_review_user_ack("reviewed", "2026-05-27 18:00:00"),
    ]
    ready_rows = {
        r.page.label: r
        for r in hd.build_workflow_checklist(
            _readiness(ready, manual_review_status="reviewed")
        )
    }
    assert ready_rows["07 Optimizer"].status == hd.ROW_PASS
    assert ready_rows["08 Export & Run Log"].status == hd.ROW_PASS


def test_checklist_fighter_status_row_is_informational_pass():
    rows = {
        r.page.label: r
        for r in hd.build_workflow_checklist(_readiness(_all_blocking_pass()))
    }
    fs = rows["04 Fighter Status"]
    assert fs.status == hd.ROW_PASS
    assert "not yet integrated" in fs.message.lower()


def test_checklist_rows_carry_icons_and_why():
    rows = hd.build_workflow_checklist(_readiness(_all_blocking_pass()))
    for r in rows:
        assert r.icon in hd.ROW_ICON.values()
        assert r.why_it_matters  # non-empty rationale


# ---------------------------------------------------------------------------
# Purity invariant
# ---------------------------------------------------------------------------


def test_module_has_no_streamlit_or_db_import():
    import src.slate.home_dashboard as module

    with open(module.__file__, "r", encoding="utf-8") as fh:
        text = fh.read()
    forbidden = [
        "import streamlit",
        "from streamlit",
        "import sqlite3",
        "from sqlite3",
        "from src.db",
        "import src.db",
    ]
    for needle in forbidden:
        assert needle not in text, f"home_dashboard.py must not contain {needle!r}"
