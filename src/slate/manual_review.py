"""Manual Review Gate v1 — Phase A pure types, constants, and check helpers.

Implements Phase A of ``docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`` (§10
Phase A). This module is pure-Python: no DB, no Streamlit, no
repository, no service composition. It owns:

- The §5 check identifier constants and the closed v1 check set.
- The §4 category mapping (blocking / warning / informational) and
  the predicate helpers.
- The :class:`ReviewCheckResult` value object (design §8).
- The :class:`ManualReviewSummary` aggregate (counts + ready bit).
- The §5.4.a odds-coverage Blocking threshold constant.
- Pure per-check evaluators that take already-resolved inputs and
  return a :class:`ReviewCheckResult`. The evaluators do **not**
  read the database, the Streamlit session, or the repository layer;
  inputs are passed in as primitives / small dataclasses by the
  caller. The Phase C read aggregator service is the one that will
  resolve those inputs from repositories.

This module deliberately does not consume ``effective_status`` for
downstream decisions (design §14) and does not consume Fighter Status
service directly (design §13). Inputs for those concerns are passed
in as pure counts / flags so this module stays disjoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# §4 — review categories (closed set: blocking, warning, informational)
# ---------------------------------------------------------------------------

CATEGORY_BLOCKING = "blocking"
CATEGORY_WARNING = "warning"
CATEGORY_INFORMATIONAL = "informational"

ALLOWED_CATEGORIES: frozenset[str] = frozenset(
    {CATEGORY_BLOCKING, CATEGORY_WARNING, CATEGORY_INFORMATIONAL}
)

# Single rank used for deterministic ordering: Blocking, Warning,
# Informational. Page section ordering must mirror this rank (design §4).
_CATEGORY_RANK = {
    CATEGORY_BLOCKING: 0,
    CATEGORY_WARNING: 1,
    CATEGORY_INFORMATIONAL: 2,
}


# ---------------------------------------------------------------------------
# Per-result status (pass / fail / info)
# ---------------------------------------------------------------------------

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_INFO = "info"

ALLOWED_RESULT_STATUSES: frozenset[str] = frozenset(
    {STATUS_PASS, STATUS_FAIL, STATUS_INFO}
)


# ---------------------------------------------------------------------------
# §5 — readiness check identifier constants (closed v1 set, §8)
# ---------------------------------------------------------------------------

CHECK_SALARY_IMPORTED = "salary_imported"
CHECK_FIGHT_GROUP_COVERAGE = "fight_group_coverage"
CHECK_FIGHT_GROUP_REVIEW = "fight_group_review"
CHECK_SCHEDULED_ROUNDS_REVIEWED = "scheduled_rounds_reviewed"
CHECK_ODDS_UNMATCHED_ACTIVE = "odds_unmatched_active"
CHECK_ODDS_COVERAGE_PARTIAL = "odds_coverage_partial"
CHECK_ODDS_MATCH_REVIEW = "odds_match_review"
CHECK_ODDS_COVERAGE_STAT = "odds_coverage_stat"
CHECK_PROJECTION_NON_PROJECTABLE = "projection_non_projectable"
CHECK_PROJECTION_MISSING_INPUTS = "projection_missing_inputs"
CHECK_MISMATCH_ALERTS_WARN = "mismatch_alerts_warn"
CHECK_MISMATCH_ALERTS_INFO = "mismatch_alerts_info"
CHECK_LATE_NEWS_RISK_LOCKED = "late_news_risk_locked"
CHECK_FIGHTER_STATUS_REVIEW = "fighter_status_review"
CHECK_LATE_NEWS_ACKNOWLEDGED = "late_news_acknowledged"
CHECK_MANUAL_REVIEW_USER_ACK = "manual_review_user_ack"

# §5 — single source of truth for the category mapping. Any consumer
# (future Phase C service, Phase D Streamlit page) MUST import this
# mapping rather than duplicating the lists.
CHECK_CATEGORY: dict[str, str] = {
    CHECK_SALARY_IMPORTED: CATEGORY_BLOCKING,
    CHECK_FIGHT_GROUP_COVERAGE: CATEGORY_BLOCKING,
    CHECK_FIGHT_GROUP_REVIEW: CATEGORY_WARNING,
    CHECK_SCHEDULED_ROUNDS_REVIEWED: CATEGORY_WARNING,
    CHECK_ODDS_UNMATCHED_ACTIVE: CATEGORY_BLOCKING,
    CHECK_ODDS_COVERAGE_PARTIAL: CATEGORY_WARNING,
    CHECK_ODDS_MATCH_REVIEW: CATEGORY_WARNING,
    CHECK_ODDS_COVERAGE_STAT: CATEGORY_INFORMATIONAL,
    CHECK_PROJECTION_NON_PROJECTABLE: CATEGORY_BLOCKING,
    CHECK_PROJECTION_MISSING_INPUTS: CATEGORY_WARNING,
    CHECK_MISMATCH_ALERTS_WARN: CATEGORY_WARNING,
    CHECK_MISMATCH_ALERTS_INFO: CATEGORY_INFORMATIONAL,
    CHECK_LATE_NEWS_RISK_LOCKED: CATEGORY_INFORMATIONAL,
    CHECK_FIGHTER_STATUS_REVIEW: CATEGORY_INFORMATIONAL,
    CHECK_LATE_NEWS_ACKNOWLEDGED: CATEGORY_WARNING,
    CHECK_MANUAL_REVIEW_USER_ACK: CATEGORY_BLOCKING,
}

ALLOWED_CHECKS: frozenset[str] = frozenset(CHECK_CATEGORY.keys())


# ---------------------------------------------------------------------------
# §5.4.a — fixed v1 threshold (test-pinned per design §15 Phase A)
# ---------------------------------------------------------------------------

BLOCKING_THRESHOLD_ODDS_UNMATCHED_PCT = 0.5


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewCheckResult:
    """A single readiness check outcome (design §8).

    - ``code``: the §5 check identifier (one of :data:`ALLOWED_CHECKS`).
    - ``category``: the §4 category (one of :data:`ALLOWED_CATEGORIES`).
      Always equals ``CHECK_CATEGORY[code]``; carried inline so callers
      do not need to re-look it up.
    - ``status``: ``"pass"`` / ``"fail"`` / ``"info"``. Informational
      checks always carry ``"info"`` — they have no pass / fail axis.
    - ``message``: human-readable message string per the §5 design.
    - ``tags``: optional structured details (e.g. affected fighter
      names or contributing alert codes). Empty tuple when unused.
    """

    code: str
    category: str
    status: str
    message: str
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ManualReviewSummary:
    """Aggregate across a list of :class:`ReviewCheckResult` (design §4).

    ``ready`` is the gate-enablement signal: True iff every Blocking
    check has ``status == 'pass'``. Warning and Informational rows
    never affect ``ready`` in v1.
    """

    blocking_count: int
    warning_count: int
    info_count: int
    ready: bool


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------


def _validate_check(code: str) -> str:
    if code not in ALLOWED_CHECKS:
        raise ValueError(
            f"unknown manual review check {code!r}; allowed: {sorted(ALLOWED_CHECKS)}"
        )
    return code


def category_for(code: str) -> str:
    """Return the §4 category for ``code``."""
    return CHECK_CATEGORY[_validate_check(code)]


def is_blocking(code: str) -> bool:
    """True iff ``code`` is a Blocking-category check (§4)."""
    return category_for(code) == CATEGORY_BLOCKING


def is_warning(code: str) -> bool:
    """True iff ``code`` is a Warning-category check (§4)."""
    return category_for(code) == CATEGORY_WARNING


def is_informational(code: str) -> bool:
    """True iff ``code`` is an Informational-category check (§4)."""
    return category_for(code) == CATEGORY_INFORMATIONAL


def has_blocking_findings(results: Iterable[ReviewCheckResult]) -> bool:
    """True iff any Blocking-category result has ``status == 'fail'``."""
    return any(
        r.category == CATEGORY_BLOCKING and r.status == STATUS_FAIL
        for r in results
    )


def has_warning_findings(results: Iterable[ReviewCheckResult]) -> bool:
    """True iff any Warning-category result has ``status == 'fail'``."""
    return any(
        r.category == CATEGORY_WARNING and r.status == STATUS_FAIL
        for r in results
    )


# ---------------------------------------------------------------------------
# Summary + deterministic ordering
# ---------------------------------------------------------------------------


# Stable ordering of check codes inside each category, mirroring the
# §5 / §8 section order so the page renders rows in a predictable order
# regardless of evaluator call order.
_CHECK_RANK: dict[str, int] = {
    code: idx
    for idx, code in enumerate(
        (
            CHECK_SALARY_IMPORTED,
            CHECK_FIGHT_GROUP_COVERAGE,
            CHECK_ODDS_UNMATCHED_ACTIVE,
            CHECK_PROJECTION_NON_PROJECTABLE,
            CHECK_MANUAL_REVIEW_USER_ACK,
            CHECK_FIGHT_GROUP_REVIEW,
            CHECK_SCHEDULED_ROUNDS_REVIEWED,
            CHECK_ODDS_COVERAGE_PARTIAL,
            CHECK_ODDS_MATCH_REVIEW,
            CHECK_PROJECTION_MISSING_INPUTS,
            CHECK_MISMATCH_ALERTS_WARN,
            CHECK_LATE_NEWS_ACKNOWLEDGED,
            CHECK_ODDS_COVERAGE_STAT,
            CHECK_MISMATCH_ALERTS_INFO,
            CHECK_LATE_NEWS_RISK_LOCKED,
            CHECK_FIGHTER_STATUS_REVIEW,
        )
    )
}


def sort_results(results: Iterable[ReviewCheckResult]) -> list[ReviewCheckResult]:
    """Sort by (category rank, intra-category code rank) — deterministic."""

    def key(r: ReviewCheckResult) -> tuple[int, int, str]:
        return (
            _CATEGORY_RANK.get(r.category, 99),
            _CHECK_RANK.get(r.code, 99),
            r.code,
        )

    return sorted(results, key=key)


def summarize(results: Iterable[ReviewCheckResult]) -> ManualReviewSummary:
    """Aggregate a result list into a :class:`ManualReviewSummary`.

    ``ready`` is True iff there are no Blocking failures. Per design
    §4 / §6, Warning failures never disable the Mark Slate Manually
    Reviewed button.
    """
    blocking = warning = info = 0
    has_blocking_fail = False
    for r in results:
        if r.category == CATEGORY_BLOCKING:
            blocking += 1
            if r.status == STATUS_FAIL:
                has_blocking_fail = True
        elif r.category == CATEGORY_WARNING:
            warning += 1
        elif r.category == CATEGORY_INFORMATIONAL:
            info += 1
        else:  # pragma: no cover - guarded by ReviewCheckResult construction
            raise ValueError(f"unknown category on result: {r.category!r}")
    return ManualReviewSummary(
        blocking_count=blocking,
        warning_count=warning,
        info_count=info,
        ready=not has_blocking_fail,
    )


# ---------------------------------------------------------------------------
# Per-check pure evaluators (design §5)
#
# Each evaluator takes already-resolved primitive inputs and returns a
# single ReviewCheckResult. The Phase C read aggregator (future) is the
# one that resolves these inputs from repositories.
# ---------------------------------------------------------------------------


def _pass(code: str, message: str, tags: tuple[str, ...] = ()) -> ReviewCheckResult:
    return ReviewCheckResult(
        code=code,
        category=CHECK_CATEGORY[code],
        status=STATUS_PASS,
        message=message,
        tags=tags,
    )


def _fail(code: str, message: str, tags: tuple[str, ...] = ()) -> ReviewCheckResult:
    return ReviewCheckResult(
        code=code,
        category=CHECK_CATEGORY[code],
        status=STATUS_FAIL,
        message=message,
        tags=tags,
    )


def _info(code: str, message: str, tags: tuple[str, ...] = ()) -> ReviewCheckResult:
    return ReviewCheckResult(
        code=code,
        category=CHECK_CATEGORY[code],
        status=STATUS_INFO,
        message=message,
        tags=tags,
    )


def _truncate_names(names: list[str], cap: int = 10) -> str:
    if len(names) <= cap:
        return ", ".join(names)
    head = ", ".join(names[:cap])
    return f"{head}, + {len(names) - cap} more"


# §5.1 -----------------------------------------------------------------------


def evaluate_salary_imported(
    salary_csv_status: Optional[str],
    salary_row_count: int,
    active_fighter_count: int,
) -> ReviewCheckResult:
    """§5.1 salary import readiness — Blocking."""
    passed = (
        salary_csv_status == "validated"
        and salary_row_count > 0
        and active_fighter_count >= 1
    )
    if passed:
        return _pass(
            CHECK_SALARY_IMPORTED,
            f"Salary CSV imported ({active_fighter_count} active fighter(s)).",
        )
    return _fail(
        CHECK_SALARY_IMPORTED,
        "Salary CSV has not been imported into this slate. Open Slate Setup, "
        "validate, and click Import.",
    )


# §5.2.a ---------------------------------------------------------------------


def evaluate_fight_group_coverage(
    active_fighters_without_group: list[str],
    active_fighter_count: int,
) -> ReviewCheckResult:
    """§5.2.a fight-group coverage — Blocking.

    An odd ``active_fighter_count`` necessarily implies at least one
    unpaired fighter (the caller surfaces that via
    ``active_fighters_without_group``). This check fails when the list
    of uncovered fighters is non-empty.
    """
    if not active_fighters_without_group:
        return _pass(
            CHECK_FIGHT_GROUP_COVERAGE,
            f"All {active_fighter_count} active fighter(s) have a fight group.",
        )
    names = sorted(active_fighters_without_group)
    n = len(names)
    return _fail(
        CHECK_FIGHT_GROUP_COVERAGE,
        f"{n} active fighter(s) have no fight group on this slate: "
        f"{_truncate_names(names)}. Open Fight Groups to add pairings.",
        tags=tuple(names),
    )


# §5.2.b ---------------------------------------------------------------------


def evaluate_fight_group_review(
    unconfirmed_or_one_sided_count: int,
) -> ReviewCheckResult:
    """§5.2.b one-sided / unconfirmed groups — Warning."""
    if unconfirmed_or_one_sided_count <= 0:
        return _pass(
            CHECK_FIGHT_GROUP_REVIEW,
            "All fight groups are confirmed and two-sided.",
        )
    return _fail(
        CHECK_FIGHT_GROUP_REVIEW,
        f"{unconfirmed_or_one_sided_count} fight group(s) are unconfirmed or "
        "one-sided. Confirm each on the Fight Groups page before building.",
    )


# §5.3 -----------------------------------------------------------------------


def evaluate_scheduled_rounds_reviewed(
    has_five_round_groups: bool,
    unconfirmed_three_round_groups: int,
    acknowledged: bool = False,
) -> ReviewCheckResult:
    """§5.3 scheduled rounds review — Warning.

    v1 cannot know the "right" rounds count, so it nudges the user to verify
    that 5-round fights (main events / championship bouts) are marked and the
    rest are 3. The nudge clears when there is nothing left to verify:

    - no 5-round group *and* no unconfirmed 3-round group (passes outright), or
    - the groups are all confirmed (``unconfirmed_three_round_groups == 0``) and
      the user has explicitly acknowledged the rounds (``acknowledged``).

    A 5-round group with the rounds unacknowledged still warns — but the user
    can dismiss it once they have confirmed the card, rather than the warning
    nagging forever (the auto-detected main event pairs with this ack so a
    correct card is one glance + one tick).
    """
    if unconfirmed_three_round_groups == 0 and (
        not has_five_round_groups or acknowledged
    ):
        return _pass(
            CHECK_SCHEDULED_ROUNDS_REVIEWED,
            "Scheduled rounds confirmed for every fight on this slate.",
        )
    return _fail(
        CHECK_SCHEDULED_ROUNDS_REVIEWED,
        "Confirm scheduled rounds for every fight on this slate. 5 rounds "
        "applies to main events and championship bouts; 3 rounds applies to "
        "every other fight. Mark fight groups confirmed on the Fight Groups "
        "page, then tick the rounds-reviewed box to dismiss this reminder.",
    )


# §5.4.a / §5.4.b / §5.4.d ----------------------------------------------------


def evaluate_odds_unmatched_active(
    active_fighter_count: int,
    covered_count: int,
    threshold: float = BLOCKING_THRESHOLD_ODDS_UNMATCHED_PCT,
) -> ReviewCheckResult:
    """§5.4.a — fails when uncovered fraction strictly exceeds the threshold.

    Per design §5.4.a the rule is "≥ 50% of active fighters … have no
    usable odds row" fails. We render that as: fails when the uncovered
    fraction is ``> 0.5`` (50%+1 of a 2-fighter card; strictly over half).
    A fighter is *covered* when an odds row binds it with a projection-eligible
    ``effective_status`` (auto-matched or manually assigned), so an inline
    Assign on Build clears this the same way it un-blocks projections. The
    threshold constant is the v1 default.
    """
    if active_fighter_count <= 0:
        return _pass(
            CHECK_ODDS_UNMATCHED_ACTIVE,
            "No active fighters yet — odds coverage check skipped.",
        )
    uncovered = max(active_fighter_count - covered_count, 0)
    uncovered_pct = uncovered / active_fighter_count
    if uncovered_pct >= threshold:
        return _fail(
            CHECK_ODDS_UNMATCHED_ACTIVE,
            f"{uncovered} of {active_fighter_count} active fighters have no "
            "usable odds yet. Add odds on Build (Step 2 — paste the DraftKings "
            "board) and assign any unmatched names below, then they count. A "
            "slate without majority odds coverage cannot be reviewed.",
        )
    return _pass(
        CHECK_ODDS_UNMATCHED_ACTIVE,
        f"{covered_count} of {active_fighter_count} active fighters have "
        "usable odds — above the v1 minimum.",
    )


def evaluate_odds_coverage_partial(
    active_fighter_count: int,
    covered_count: int,
    threshold: float = BLOCKING_THRESHOLD_ODDS_UNMATCHED_PCT,
) -> ReviewCheckResult:
    """§5.4.b — warns when *some* coverage missing but Blocking threshold not met."""
    if active_fighter_count <= 0:
        return _pass(
            CHECK_ODDS_COVERAGE_PARTIAL,
            "No active fighters yet — partial-coverage check skipped.",
        )
    uncovered = max(active_fighter_count - covered_count, 0)
    if uncovered == 0:
        return _pass(
            CHECK_ODDS_COVERAGE_PARTIAL,
            f"All {active_fighter_count} active fighter(s) have usable odds.",
        )
    uncovered_pct = uncovered / active_fighter_count
    if uncovered_pct >= threshold:
        # Blocking threshold met — §5.4.a owns the failure surface.
        return _pass(
            CHECK_ODDS_COVERAGE_PARTIAL,
            "Coverage gap large enough to be Blocking — see "
            "odds_unmatched_active.",
        )
    return _fail(
        CHECK_ODDS_COVERAGE_PARTIAL,
        f"{uncovered} active fighter(s) have no usable odds. Their "
        "Projection v1 row will read missing_inputs ('win_probability') and "
        "will not contribute to the optimizer or export. Verify the gap is "
        "intentional.",
    )


def evaluate_odds_match_review(
    review_required_count: int,
    review_rejected_count: int,
) -> ReviewCheckResult:
    """§5.4.c — warns when review_required or review_rejected rows remain.

    Note: ``review_rejected_count`` is sourced from the same repository
    the Odds page already uses for the row count surface; per design
    §14 the gate must not consume ``effective_status`` downstream.
    """
    total = max(review_required_count, 0) + max(review_rejected_count, 0)
    if total <= 0:
        return _pass(
            CHECK_ODDS_MATCH_REVIEW,
            "No odds match results pending review.",
        )
    return _fail(
        CHECK_ODDS_MATCH_REVIEW,
        f"{total} odds match result(s) still need review "
        f"(review_required: {review_required_count}, "
        f"review_rejected: {review_rejected_count}). Assign or reject them "
        "inline on Build (Step 2), or use the Odds page, before reviewing "
        "the slate.",
    )


def evaluate_odds_coverage_stat(
    active_fighter_count: int,
    covered_count: int,
) -> ReviewCheckResult:
    """§5.4.d — informational coverage stat."""
    if active_fighter_count <= 0:
        pct = 0.0
    else:
        pct = (covered_count / active_fighter_count) * 100.0
    return _info(
        CHECK_ODDS_COVERAGE_STAT,
        f"Odds coverage: {covered_count} of {active_fighter_count} active "
        f"fighters ({pct:.0f}%).",
    )


# §5.5.a / §5.5.b ------------------------------------------------------------


def evaluate_projection_non_projectable(
    non_projectable_fighters: list[tuple[str, tuple[str, ...]]],
) -> ReviewCheckResult:
    """§5.5.a — Blocking on any ``non_projectable`` projection row.

    Each tuple is ``(fighter_name, missing_input_tags)`` carried
    through verbatim from the projection layer.
    """
    if not non_projectable_fighters:
        return _pass(
            CHECK_PROJECTION_NON_PROJECTABLE,
            "All projectable fighters have structural inputs.",
        )
    sorted_pairs = sorted(non_projectable_fighters, key=lambda p: p[0])
    rendered = [
        f"{name} ({', '.join(tags)})" if tags else name
        for name, tags in sorted_pairs
    ]
    n = len(sorted_pairs)
    return _fail(
        CHECK_PROJECTION_NON_PROJECTABLE,
        f"{n} fighter(s) are non-projectable: {_truncate_names(rendered)}. "
        "Resolve the structural cause (fight group / opponent / fighter "
        "status) before reviewing the slate.",
        tags=tuple(name for name, _ in sorted_pairs),
    )


def evaluate_projection_missing_inputs(
    missing_input_fighters: list[str],
) -> ReviewCheckResult:
    """§5.5.b — Warning on any ``missing_inputs`` projection row."""
    if not missing_input_fighters:
        return _pass(
            CHECK_PROJECTION_MISSING_INPUTS,
            "All active fighters have projection inputs.",
        )
    names = sorted(missing_input_fighters)
    n = len(names)
    return _fail(
        CHECK_PROJECTION_MISSING_INPUTS,
        f"{n} fighter(s) have missing projection inputs: "
        f"{_truncate_names(names)}. The optimizer will exclude these rows; "
        "verify the gap is intentional before reviewing.",
        tags=tuple(names),
    )


# §5.6.a / §5.6.b / §5.6.c ---------------------------------------------------


def evaluate_mismatch_alerts_warn(
    warn_alert_count: int,
    warn_alert_codes: Iterable[str],
) -> ReviewCheckResult:
    """§5.6.a — Warning when any warn-severity mismatch alert is present."""
    codes_sorted = sorted(set(warn_alert_codes))
    if warn_alert_count <= 0:
        return _pass(
            CHECK_MISMATCH_ALERTS_WARN,
            "No warn-severity mismatch alerts on this slate.",
        )
    code_str = ", ".join(codes_sorted) if codes_sorted else "none"
    return _fail(
        CHECK_MISMATCH_ALERTS_WARN,
        f"{warn_alert_count} warn-severity mismatch alert(s) on this slate "
        f"({code_str}). Open the Alerts page to review.",
        tags=tuple(codes_sorted),
    )


def evaluate_mismatch_alerts_info(
    info_alert_count: int,
    info_alert_codes: Iterable[str],
) -> ReviewCheckResult:
    """§5.6.b — informational info-severity alert count."""
    codes_sorted = sorted(set(info_alert_codes))
    code_str = ", ".join(codes_sorted) if codes_sorted else "none"
    return _info(
        CHECK_MISMATCH_ALERTS_INFO,
        f"{info_alert_count} info-severity mismatch alert(s) ({code_str}).",
        tags=tuple(codes_sorted),
    )


def evaluate_late_news_risk_locked() -> ReviewCheckResult:
    """§5.6.c — locks the reserved-but-never-emitted contract.

    Pure helper; takes no input. The Phase C aggregator may cross-check
    that no warn alert with code ``late_news_risk`` appears in
    :func:`evaluate_mismatch_alerts_warn` input.
    """
    return _info(
        CHECK_LATE_NEWS_RISK_LOCKED,
        "late-news alert is reserved — not active in v1; use the manual "
        "checklist instead.",
    )


# §5.7 -----------------------------------------------------------------------


def evaluate_fighter_status_review(
    blocking_count: int = 0,
    warning_count: int = 0,
) -> ReviewCheckResult:
    """§5.7 — Informational in v1 (Fighter Status integration deferred).

    Counts are accepted so the Phase F slice that promotes Fighter
    Status into Manual Review can flip this function without changing
    the call shape. In v1 the row is always Informational and renders
    a fixed message; counts are ignored.
    """
    _ = (blocking_count, warning_count)  # locked-out in v1
    return _info(
        CHECK_FIGHTER_STATUS_REVIEW,
        "Fighter Status integration not yet active. Manual Review v1 does "
        "not consult Fighter Status; use the manual late-news checklist "
        "for now.",
    )


# §5.8 -----------------------------------------------------------------------


def evaluate_late_news_acknowledged(
    acknowledged: bool,
    acknowledged_at: Optional[str] = None,
) -> ReviewCheckResult:
    """§5.8 — Warning until the user toggles the late-news ack."""
    if acknowledged:
        when = f" by user at {acknowledged_at}" if acknowledged_at else ""
        return _pass(
            CHECK_LATE_NEWS_ACKNOWLEDGED,
            f"Late-news / weigh-in checklist acknowledged{when}.",
        )
    return _fail(
        CHECK_LATE_NEWS_ACKNOWLEDGED,
        "Confirm you have completed the off-app late-news / weigh-in "
        "checklist for this slate. Manual Review will not auto-detect a "
        "pulled fighter.",
    )


# §5.9 -----------------------------------------------------------------------


def evaluate_manual_review_user_ack(
    manual_review_status: str,
    completed_at: Optional[str] = None,
) -> ReviewCheckResult:
    """§5.9 — Blocking until the slate is explicitly marked reviewed."""
    if manual_review_status == "reviewed":
        when = f" (at {completed_at})" if completed_at else ""
        return _pass(
            CHECK_MANUAL_REVIEW_USER_ACK,
            f"Slate manually reviewed{when}.",
        )
    return _fail(
        CHECK_MANUAL_REVIEW_USER_ACK,
        "Slate has not yet been marked manually reviewed. Click Mark Slate "
        "Manually Reviewed when the Blocking and Warning lists above are "
        "acceptable.",
    )
