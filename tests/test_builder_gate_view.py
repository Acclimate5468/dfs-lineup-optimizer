"""Unit tests for the pure builder Build-gate presenter.

Pins ``docs/TWO_STEP_BUILDER_PRODUCTION_DESIGN.md`` §1.1 / §4 / §7.2:
``builder_gate_view`` maps a ``ReviewReadiness`` into blocked / warning /
ready / not-started display data **without re-deriving any gate rule**.
Pure-only: every ``ReviewReadiness`` is hand-built from the Phase A
evaluators in ``src.slate.manual_review`` (the way ``home_dashboard`` is
unit-tested).
"""

from __future__ import annotations

from src.slate import home_dashboard as hd
from src.slate import manual_review as mr
from src.slate.manual_review_service import ReviewReadiness


# ---------------------------------------------------------------------------
# Fixtures (mirroring tests/test_home_dashboard.py)
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
    """Every Blocking check passing except ``manual_review_user_ack``."""
    return [
        mr.evaluate_salary_imported("validated", 26, 26),
        mr.evaluate_fight_group_coverage([], 26),
        mr.evaluate_odds_unmatched_active(26, 26),
        mr.evaluate_projection_non_projectable([]),
    ]


# ---------------------------------------------------------------------------
# Verdict: blocked
# ---------------------------------------------------------------------------


def test_blocked_readiness_produces_blocked_verdict():
    checks = [
        mr.evaluate_salary_imported("validated", 26, 26),
        mr.evaluate_fight_group_coverage(["Khabib"], 26),  # blocking fail
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    view = hd.builder_gate_view(_readiness(checks), has_slates=True)
    assert view.verdict == hd.GATE_BLOCKED
    assert view.title == "Blocked"
    assert view.ready_to_build is False
    assert view.ready_to_mark is False
    # The structural blocker is surfaced; the reviewer-ack is not a chip
    # and never appears in the "fix these first" list.
    fail_codes = {c.code for c in view.blocking_fails}
    assert mr.CHECK_FIGHT_GROUP_COVERAGE in fail_codes
    assert mr.CHECK_MANUAL_REVIEW_USER_ACK not in fail_codes


def test_empty_db_produces_not_started_verdict():
    # No slates at all → call-to-action, not a blocked slate.
    view = hd.builder_gate_view(_readiness([]), has_slates=False)
    assert view.verdict == hd.GATE_NOT_STARTED
    assert view.ready_to_build is False
    assert view.next_action.code == "slate_setup"


def test_existing_but_empty_slate_is_blocked_not_not_started():
    # A slate exists but salary was never imported → blocked (structural).
    checks = [
        mr.evaluate_salary_imported(None, 0, 0),
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    view = hd.builder_gate_view(_readiness(checks), has_slates=True)
    assert view.verdict == hd.GATE_BLOCKED
    assert view.ready_to_build is False


# ---------------------------------------------------------------------------
# Verdict: warning (no blockers, but needs explicit review)
# ---------------------------------------------------------------------------


def test_warning_readiness_produces_warning_and_requires_explicit_review():
    # Structurally clean (ready_to_mark True) with an outstanding Warning
    # and the slate not yet acked → warning, Build still locked.
    checks = _all_blocking_pass() + [
        mr.evaluate_fight_group_review(2),  # warning fail
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    view = hd.builder_gate_view(_readiness(checks), has_slates=True)
    assert view.verdict == hd.GATE_WARNING
    assert view.ready_to_mark is True  # mark-reviewed affordance is offered
    assert view.ready_to_build is False  # but Build stays locked until marked
    # The explicit review path is Manual Review (warnings never auto-clear).
    assert view.next_action.code == "manual_review"


def test_structurally_clean_unacked_with_no_warnings_is_warning():
    # No warnings failing, just not yet marked reviewed → warning, not ready.
    checks = _all_blocking_pass() + [
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    view = hd.builder_gate_view(_readiness(checks), has_slates=True)
    assert view.verdict == hd.GATE_WARNING
    assert view.ready_to_mark is True
    assert view.ready_to_build is False


# ---------------------------------------------------------------------------
# Verdict: ready
# ---------------------------------------------------------------------------


def test_ready_readiness_produces_ready_and_build_enabled():
    checks = _all_blocking_pass() + [
        mr.evaluate_manual_review_user_ack("reviewed", "2026-05-27 18:00:00"),
    ]
    readiness = _readiness(checks, manual_review_status="reviewed")
    assert readiness.summary.ready is True
    view = hd.builder_gate_view(readiness, has_slates=True)
    assert view.verdict == hd.GATE_READY
    assert view.title == "Ready"
    assert view.ready_to_build is True
    assert view.ready_to_mark is True
    assert view.blocking_fails == ()
    assert view.next_action.code == "optimizer"


def test_ready_with_warnings_still_ready():
    # ``summary.ready`` only cares about Blocking checks (design §4).
    checks = _all_blocking_pass() + [
        mr.evaluate_fight_group_review(1),  # warning fail
        mr.evaluate_manual_review_user_ack("reviewed", "2026-05-27 18:00:00"),
    ]
    view = hd.builder_gate_view(
        _readiness(checks, manual_review_status="reviewed"), has_slates=True
    )
    assert view.verdict == hd.GATE_READY
    assert view.ready_to_build is True


# ---------------------------------------------------------------------------
# Chips come from the checks, not invented logic
# ---------------------------------------------------------------------------


def test_chips_mirror_their_governing_checks():
    checks = [
        mr.evaluate_salary_imported("validated", 26, 26),
        mr.evaluate_fight_group_coverage(["Khabib"], 26),  # blocking fail
        mr.evaluate_fight_group_review(2),  # warning fail
        mr.evaluate_odds_unmatched_active(26, 26),  # blocking pass
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    readiness = _readiness(checks)
    view = hd.builder_gate_view(readiness, has_slates=True)

    chips_by_code = {c.code: c for c in view.chips}
    checks_by_code = {c.code: c for c in readiness.checks}

    # Reviewer acknowledgement is the Build gate, not a domain chip.
    assert mr.CHECK_MANUAL_REVIEW_USER_ACK not in chips_by_code

    # Every chip's status / severity / message is read straight off its
    # check — nothing fabricated.
    for code, chip in chips_by_code.items():
        check = checks_by_code[code]
        assert chip.severity == check.category
        assert chip.message == check.message  # verbatim gate copy
        if check.status == mr.STATUS_PASS:
            assert chip.status == hd.ROW_PASS
        elif check.category == mr.CATEGORY_BLOCKING:
            assert chip.status == hd.ROW_BLOCK
        else:
            assert chip.status == hd.ROW_WARN

    # A failing blocking check renders a BLOCK chip; a failing warning a WARN.
    assert chips_by_code[mr.CHECK_FIGHT_GROUP_COVERAGE].status == hd.ROW_BLOCK
    assert chips_by_code[mr.CHECK_FIGHT_GROUP_REVIEW].status == hd.ROW_WARN
    assert chips_by_code[mr.CHECK_SALARY_IMPORTED].status == hd.ROW_PASS


def test_chip_labels_are_display_only_and_present():
    view = hd.builder_gate_view(
        _readiness(
            _all_blocking_pass()
            + [mr.evaluate_manual_review_user_ack("not_reviewed")]
        ),
        has_slates=True,
    )
    for chip in view.chips:
        assert chip.label  # non-empty display label
        assert chip.icon in hd.ROW_ICON.values()


def test_only_blocking_and_warning_checks_become_chips():
    # Informational checks (coverage stat, info alerts, fighter status,
    # late-news-risk) must not appear as gate chips.
    checks = _all_blocking_pass() + [
        mr.evaluate_odds_coverage_stat(26, 26),  # informational
        mr.evaluate_mismatch_alerts_info(3, ["odds_gap"]),  # informational
        mr.evaluate_fighter_status_review(),  # informational
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    view = hd.builder_gate_view(_readiness(checks), has_slates=True)
    severities = {chip.severity for chip in view.chips}
    assert mr.CATEGORY_INFORMATIONAL not in severities
    assert severities <= {mr.CATEGORY_BLOCKING, mr.CATEGORY_WARNING}


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_builder_gate_view_is_deterministic():
    checks = _all_blocking_pass() + [
        mr.evaluate_fight_group_review(2),
        mr.evaluate_manual_review_user_ack("not_reviewed"),
    ]
    a = hd.builder_gate_view(_readiness(checks), has_slates=True)
    b = hd.builder_gate_view(_readiness(checks), has_slates=True)
    assert a == b
