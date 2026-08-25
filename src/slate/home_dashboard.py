"""Home Dashboard UX A5.2 — pure presentation helpers.

Realizes ``docs/HOME_DASHBOARD_UX_DESIGN.md`` §3.4 (workflow checklist)
and §5 (next-action logic). This module is pure-Python: it imports no
Streamlit, opens no database connection, calls no optimizer / export /
recompute, and re-derives **no** pass/fail rule. It only reads a
``ReviewReadiness`` value object handed to it by the page (produced by
the Manual Review Gate Phase C aggregator,
``src.slate.manual_review_service.evaluate_manual_review``) and the
shared category / status constants from :mod:`src.slate.manual_review`.

Two surfaces are exposed:

- :func:`recommend_next_action` — the §5 precedence mapping from a
  ``ReviewReadiness`` (plus a ``has_slates`` flag) to a single
  :class:`NextAction`.
- :func:`build_workflow_checklist` — the §3.4 per-page checklist, one
  :class:`ChecklistRow` per workflow step, each carrying a derived
  pass / warn / block / not-started status icon.

Both are unit-tested directly against hand-built ``ReviewReadiness`` /
``ReviewCheckResult`` fixtures, mirroring how
``src/slate/manual_review.py`` is unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.slate import manual_review as mr

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids DB import at runtime
    from src.slate.manual_review_service import ReviewReadiness


# ---------------------------------------------------------------------------
# Page references (workflow order — §3.4). Page *files keep their NN_
# numbers*; this slice only reflects the real workflow order in the
# dashboard's display. No page is renamed (design §7 / A5.5).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageRef:
    """A single Streamlit page the dashboard points at."""

    label: str
    path: str


PAGE_SLATE_SETUP = PageRef("01 Slate Setup", "pages/01_slate_setup.py")
PAGE_FIGHT_GROUPS = PageRef("02 Fight Groups", "pages/02_fight_groups.py")
PAGE_ODDS = PageRef("03 Odds", "pages/03_odds.py")
PAGE_PROJECTIONS = PageRef("09 Projections", "pages/09_projections.py")
PAGE_ALERTS = PageRef("05 Alerts", "pages/05_alerts.py")
PAGE_FIGHTER_STATUS = PageRef("04 Fighter Status", "pages/04_fighter_status.py")
PAGE_MANUAL_REVIEW = PageRef("06 Manual Review", "pages/06_manual_review.py")
PAGE_OPTIMIZER = PageRef("07 Optimizer", "pages/07_optimizer.py")
PAGE_EXPORT = PageRef("08 Export & Run Log", "pages/08_export_run_log.py")


# ---------------------------------------------------------------------------
# Checklist row status (presentation only — distinct from the gate's
# pass / fail / info per-check status, which it is *derived from*).
# ---------------------------------------------------------------------------

ROW_PASS = "pass"
ROW_WARN = "warn"
ROW_BLOCK = "block"
ROW_NOT_STARTED = "not_started"

ROW_ICON: dict[str, str] = {
    ROW_PASS: "✅",
    ROW_WARN: "⚠️",
    ROW_BLOCK: "⛔",
    ROW_NOT_STARTED: "◻️",
}


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NextAction:
    """The single recommended next action (design §3.5 / §5).

    - ``code``: a stable identifier for the recommendation
      (``"slate_setup"`` / ``"fight_groups"`` / ``"odds"`` /
      ``"projections"`` / ``"manual_review"`` / ``"optimizer"``).
    - ``page``: the :class:`PageRef` the user should visit next.
    - ``why``: a one-sentence rationale. Advisory only — the panel that
      renders this never writes, recomputes, or runs a downstream action.
    """

    code: str
    page: PageRef
    why: str


@dataclass(frozen=True)
class ChecklistRow:
    """One workflow-checklist row (design §3.4)."""

    page: PageRef
    status: str
    message: str
    why_it_matters: str
    governing_codes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def icon(self) -> str:
        return ROW_ICON.get(self.status, ROW_ICON[ROW_NOT_STARTED])


# ---------------------------------------------------------------------------
# §5 — next-action precedence
# ---------------------------------------------------------------------------

# First-failing Blocking check → next-action page mapping (design §5
# table). ``manual_review_user_ack`` is handled separately (step 4),
# never via this table.
_BLOCKING_NEXT_ACTION: dict[str, tuple[str, PageRef, str]] = {
    mr.CHECK_SALARY_IMPORTED: (
        "slate_setup",
        PAGE_SLATE_SETUP,
        "No salaries imported yet — create a slate and import a DK UFC "
        "Classic salary CSV on Slate Setup.",
    ),
    mr.CHECK_FIGHT_GROUP_COVERAGE: (
        "fight_groups",
        PAGE_FIGHT_GROUPS,
        "Active fighters are unpaired — every active fighter needs an "
        "opponent before projections and the optimizer can use them.",
    ),
    mr.CHECK_ODDS_UNMATCHED_ACTIVE: (
        "odds",
        PAGE_ODDS,
        "Most active fighters have no matched odds — upload odds (CSV or "
        "manual) on the Odds page, then Recompute.",
    ),
    mr.CHECK_PROJECTION_NON_PROJECTABLE: (
        "projections",
        PAGE_PROJECTIONS,
        "Some fighters are non-projectable — open Projections to see which "
        "fighters and why (the cause is usually a missing fight group / "
        "opponent / fighter status upstream).",
    ),
}


def recommend_next_action(
    readiness: "ReviewReadiness", *, has_slates: bool
) -> NextAction:
    """Map a ``ReviewReadiness`` to the single recommended next action.

    Precedence (design §5; first match wins):

    1. No slates exist → **01 Slate Setup**.
    2. ``summary.ready`` is True → **07 Optimizer**.
    3. Every Blocking check passes *except* ``manual_review_user_ack``
       (structurally clean, just unacked) → **06 Manual Review**.
    4. Otherwise → the first failing Blocking check in the gate's rank
       order maps to its page.

    This function re-derives no check verdict — it only reads
    ``readiness.summary.ready`` and the ``status`` / ``category`` /
    ``code`` of the checks the aggregator already produced (themselves
    already ``mr.sort_results``-ordered).
    """
    if not has_slates:
        return NextAction(
            code="slate_setup",
            page=PAGE_SLATE_SETUP,
            why=(
                "Create a slate and import a DK UFC Classic salary CSV to "
                "start the workflow."
            ),
        )

    if readiness.summary.ready:
        return NextAction(
            code="optimizer",
            page=PAGE_OPTIMIZER,
            why=(
                "Slate is reviewed and ready — generate research lineups, "
                "then build the internal Export & Run Log."
            ),
        )

    # Failing Blocking checks, in the gate's already-applied rank order.
    blocking_fails = [
        r
        for r in readiness.checks
        if r.category == mr.CATEGORY_BLOCKING and r.status == mr.STATUS_FAIL
    ]
    structural_fails = [
        r for r in blocking_fails if r.code != mr.CHECK_MANUAL_REVIEW_USER_ACK
    ]

    if not structural_fails:
        # Only ``manual_review_user_ack`` remains (the slate is
        # structurally clean but not yet marked reviewed).
        return NextAction(
            code="manual_review",
            page=PAGE_MANUAL_REVIEW,
            why=(
                "All structural checks pass — review the checklist and click "
                "Mark Slate Manually Reviewed."
            ),
        )

    first = structural_fails[0]
    mapped = _BLOCKING_NEXT_ACTION.get(first.code)
    if mapped is None:
        # Defensive default: an unmapped Blocking failure still routes the
        # user to Manual Review, which lists every Blocking finding.
        return NextAction(
            code="manual_review",
            page=PAGE_MANUAL_REVIEW,
            why=(
                "Resolve the Blocking findings listed on Manual Review before "
                "building."
            ),
        )
    code, page, why = mapped
    return NextAction(code=code, page=page, why=why)


# ---------------------------------------------------------------------------
# §3.4 — workflow checklist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RowSpec:
    page: PageRef
    blocking_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    why_it_matters: str
    requires_salary: bool = True
    info_only: bool = False
    gate_row: bool = False


# Workflow order (design §3.4) — intentionally differs from the NN_
# filename order.
_ROW_SPECS: tuple[_RowSpec, ...] = (
    _RowSpec(
        page=PAGE_SLATE_SETUP,
        blocking_codes=(mr.CHECK_SALARY_IMPORTED,),
        warning_codes=(),
        why_it_matters=(
            "Import a DK UFC Classic salary CSV — every later step depends on "
            "the fighter list it creates."
        ),
        requires_salary=False,
    ),
    _RowSpec(
        page=PAGE_FIGHT_GROUPS,
        blocking_codes=(mr.CHECK_FIGHT_GROUP_COVERAGE,),
        warning_codes=(
            mr.CHECK_FIGHT_GROUP_REVIEW,
            mr.CHECK_SCHEDULED_ROUNDS_REVIEWED,
        ),
        why_it_matters=(
            "Every active fighter needs an opponent and confirmed rounds "
            "before projections and the optimizer can use them."
        ),
    ),
    _RowSpec(
        page=PAGE_ODDS,
        blocking_codes=(mr.CHECK_ODDS_UNMATCHED_ACTIVE,),
        warning_codes=(
            mr.CHECK_ODDS_COVERAGE_PARTIAL,
            mr.CHECK_ODDS_MATCH_REVIEW,
        ),
        why_it_matters=(
            "Matched odds drive the implied win probability at the core of "
            "every projection."
        ),
    ),
    _RowSpec(
        page=PAGE_PROJECTIONS,
        blocking_codes=(mr.CHECK_PROJECTION_NON_PROJECTABLE,),
        warning_codes=(mr.CHECK_PROJECTION_MISSING_INPUTS,),
        why_it_matters=(
            "Projections turn odds + salary into the points the optimizer "
            "maximizes; non-projectable fighters drop from the pool."
        ),
    ),
    _RowSpec(
        page=PAGE_ALERTS,
        blocking_codes=(),
        warning_codes=(mr.CHECK_MISMATCH_ALERTS_WARN,),
        why_it_matters=(
            "Mismatch alerts flag rows worth a second look before you trust "
            "the slate."
        ),
    ),
    _RowSpec(
        page=PAGE_FIGHTER_STATUS,
        blocking_codes=(),
        warning_codes=(),
        why_it_matters=(
            "Track active / out fighters. Not yet integrated into the Manual "
            "Review gate (informational in v1)."
        ),
        info_only=True,
    ),
    _RowSpec(
        page=PAGE_MANUAL_REVIEW,
        blocking_codes=(mr.CHECK_MANUAL_REVIEW_USER_ACK,),
        warning_codes=(mr.CHECK_LATE_NEWS_ACKNOWLEDGED,),
        why_it_matters=(
            "The gate that unlocks the optimizer and export — mark the slate "
            "reviewed once the Blocking and Warning lists are acceptable."
        ),
        requires_salary=False,
    ),
    _RowSpec(
        page=PAGE_OPTIMIZER,
        blocking_codes=(),
        warning_codes=(),
        why_it_matters=(
            "Generate research lineups. Unlocks once the slate is structurally "
            "clean and marked reviewed."
        ),
        gate_row=True,
    ),
    _RowSpec(
        page=PAGE_EXPORT,
        blocking_codes=(),
        warning_codes=(),
        why_it_matters=(
            "Build the internal research export / run log. Unlocks with the "
            "optimizer once the slate is ready."
        ),
        gate_row=True,
    ),
)


def _row_status(
    spec: _RowSpec, checks_by_code: dict[str, mr.ReviewCheckResult]
) -> str:
    salary = checks_by_code.get(mr.CHECK_SALARY_IMPORTED)
    salary_ok = salary is not None and salary.status == mr.STATUS_PASS

    if spec.gate_row:
        # Optimizer / Export report *availability*, not completion: they
        # have no governing check, only the readiness gate. Resolved by
        # the caller via the gate flag (passed through ``checks_by_code``
        # is insufficient), so the caller patches this — see
        # :func:`build_workflow_checklist`.
        return ROW_NOT_STARTED

    if spec.requires_salary and not salary_ok:
        # Upstream prerequisite (salary import) has not run — downstream
        # structural rows read not-started rather than a spurious pass.
        return ROW_NOT_STARTED

    blocking = [checks_by_code.get(c) for c in spec.blocking_codes]
    if any(c is not None and c.status == mr.STATUS_FAIL for c in blocking):
        return ROW_BLOCK

    warning = [checks_by_code.get(c) for c in spec.warning_codes]
    if any(c is not None and c.status == mr.STATUS_FAIL for c in warning):
        return ROW_WARN

    present = [c for c in (blocking + warning) if c is not None]
    if not present and not spec.info_only:
        # No governing check was surfaced for this row (e.g. unknown-slate
        # readiness only carries ``salary_imported``).
        return ROW_NOT_STARTED
    return ROW_PASS


def _row_message(
    spec: _RowSpec, status: str, checks_by_code: dict[str, mr.ReviewCheckResult]
) -> str:
    if spec.gate_row:
        return (
            "Unlocked — slate is reviewed and ready."
            if status == ROW_PASS
            else "Locked until the slate is structurally clean and reviewed."
        )
    if spec.info_only:
        return "Informational — not yet integrated into the Manual Review gate."
    if status == ROW_NOT_STARTED:
        return "Not started — complete the earlier steps first."

    # Surface the most severe governing check's own message (truncated),
    # which the gate already wrote.
    severity_order = (
        (mr.STATUS_FAIL, spec.blocking_codes),
        (mr.STATUS_FAIL, spec.warning_codes),
        (mr.STATUS_PASS, spec.blocking_codes + spec.warning_codes),
    )
    for want_status, codes in severity_order:
        for code in codes:
            result = checks_by_code.get(code)
            if result is not None and result.status == want_status:
                return _truncate(result.message)
    return "OK."


def _truncate(text: str, cap: int = 160) -> str:
    text = " ".join(text.split())
    if len(text) <= cap:
        return text
    return text[: cap - 1].rstrip() + "…"


def build_workflow_checklist(readiness: "ReviewReadiness") -> list[ChecklistRow]:
    """Return the §3.4 workflow checklist for ``readiness``.

    One row per workflow page, in workflow order. Each row's status is
    the most severe governing check (``block`` beats ``warn`` beats
    ``pass``), with ``not_started`` when the upstream salary import has
    not run or no governing check was surfaced. The Optimizer / Export
    gate rows report ``pass`` (unlocked) iff ``summary.ready`` else
    ``not_started`` (locked) — they have no governing check and report
    availability, not completion.
    """
    checks_by_code = {c.code: c for c in readiness.checks}
    rows: list[ChecklistRow] = []
    for spec in _ROW_SPECS:
        if spec.gate_row:
            status = ROW_PASS if readiness.summary.ready else ROW_NOT_STARTED
        else:
            status = _row_status(spec, checks_by_code)
        rows.append(
            ChecklistRow(
                page=spec.page,
                status=status,
                message=_row_message(spec, status, checks_by_code),
                why_it_matters=spec.why_it_matters,
                governing_codes=spec.blocking_codes + spec.warning_codes,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Builder gate presenter (TWO_STEP_BUILDER_PRODUCTION_DESIGN §1.1 / §4 / §7.2)
#
# A single pure presenter that maps a ``ReviewReadiness`` into the
# blocked / warning / ready / not-started display data the two-step
# builder's Build panel renders. It re-derives **no** gate rule: the
# verdict reads ``summary.ready`` and the existing ``ready_to_mark``
# predicate (the same one ``app/pages/06_manual_review.py`` uses); each
# chip reads a governing check's own ``status`` / ``category`` /
# ``message`` (the same severity rule as :func:`_signal_status` /
# :func:`_row_status`). This keeps the builder from drifting away from
# Manual Review / Optimizer / Export (design §13 risk #1).
# ---------------------------------------------------------------------------

# Builder gate verdicts (display vocabulary — the page maps these to its
# own CSS classes; the prototype's ready / warn / block / call-to-action).
GATE_BLOCKED = "blocked"
GATE_WARNING = "warning"
GATE_READY = "ready"
GATE_NOT_STARTED = "not_started"

_GATE_TITLE: dict[str, str] = {
    GATE_BLOCKED: "Blocked",
    GATE_WARNING: "Needs review",
    GATE_READY: "Ready",
    GATE_NOT_STARTED: "Not started",
}

_GATE_SUMMARY: dict[str, str] = {
    GATE_BLOCKED: (
        "Resolve the blocking checks below before this slate can be marked "
        "reviewed or built."
    ),
    GATE_WARNING: (
        "No blocking issues remain. Review the warnings, then explicitly mark "
        "the slate reviewed to unlock Build."
    ),
    GATE_READY: "Slate is reviewed and ready — Build is unlocked.",
    GATE_NOT_STARTED: (
        "Create a slate and import a DK UFC Classic salary CSV to begin."
    ),
}

# Short, display-only chip labels keyed by check code, mirroring the
# Manual Review page's ``_CHECK_LABELS`` (``app/pages/06_manual_review.py``).
# Presentation only — the chip's status / severity / message all come
# from the underlying check, never from this map.
_GATE_CHIP_LABELS: dict[str, str] = {
    mr.CHECK_SALARY_IMPORTED: "Salary import",
    mr.CHECK_FIGHT_GROUP_COVERAGE: "Fight-group coverage",
    mr.CHECK_FIGHT_GROUP_REVIEW: "Fight-group review",
    mr.CHECK_SCHEDULED_ROUNDS_REVIEWED: "Scheduled rounds",
    mr.CHECK_ODDS_UNMATCHED_ACTIVE: "Odds coverage (blocking)",
    mr.CHECK_ODDS_COVERAGE_PARTIAL: "Odds coverage (partial)",
    mr.CHECK_ODDS_MATCH_REVIEW: "Odds match review",
    mr.CHECK_PROJECTION_NON_PROJECTABLE: "Projection coverage",
    mr.CHECK_PROJECTION_MISSING_INPUTS: "Projection inputs",
    mr.CHECK_MISMATCH_ALERTS_WARN: "Mismatch alerts",
    mr.CHECK_LATE_NEWS_ACKNOWLEDGED: "Late-news acknowledgement",
}


def _gate_chip_label(code: str) -> str:
    """Plain-English chip label for a check ``code`` (display-only)."""
    return _GATE_CHIP_LABELS.get(code, code.replace("_", " ").capitalize())


@dataclass(frozen=True)
class GateChip:
    """One Build-gate chip, read straight off a governing check.

    - ``code``: the §5 check identifier the chip mirrors.
    - ``label``: a short display label for ``code`` (presentation only).
    - ``status``: the checklist presentation status — :data:`ROW_PASS` /
      :data:`ROW_WARN` / :data:`ROW_BLOCK` — derived from the check's own
      ``status`` / ``category`` by the same block-beats-warn-beats-pass
      rule the workflow checklist uses. Never re-derived from raw inputs.
    - ``severity``: the check's §4 category (Blocking / Warning).
    - ``message``: the check's own ``message`` (verbatim) — the gate, not
      the builder, owns this copy.
    """

    code: str
    label: str
    status: str
    severity: str
    message: str

    @property
    def icon(self) -> str:
        return ROW_ICON.get(self.status, ROW_ICON[ROW_NOT_STARTED])


@dataclass(frozen=True)
class BuilderGateView:
    """Presenter object for the two-step builder's Build gate (design §4).

    - ``verdict``: :data:`GATE_BLOCKED` / :data:`GATE_WARNING` /
      :data:`GATE_READY` / :data:`GATE_NOT_STARTED`.
    - ``title`` / ``summary``: short verdict-keyed display copy.
    - ``chips``: the governing Blocking + Warning checks as
      :class:`GateChip` rows (the reviewer-acknowledgement check is not a
      chip — it is the Build-enable gate, surfaced via ``ready_to_mark`` /
      ``ready_to_build`` and the page's explicit mark-reviewed control).
    - ``blocking_fails``: the structural Blocking chips that fail (the
      "fix these first" list). Excludes the reviewer ack by construction.
    - ``ready_to_mark``: True iff every Blocking check except the reviewer
      ack passes — the same predicate ``06_manual_review.py`` gates its
      Mark-reviewed button on. Controls whether the mark-reviewed
      affordance is offered. Never auto-acknowledges (design §7.4).
    - ``ready_to_build``: ``readiness.summary.ready`` verbatim — the same
      predicate the Optimizer / Export pages gate Build on.
    - ``next_action``: the single recommended next action
      (:func:`recommend_next_action`), reused unchanged.
    """

    verdict: str
    title: str
    summary: str
    chips: tuple[GateChip, ...]
    blocking_fails: tuple[GateChip, ...]
    ready_to_mark: bool
    ready_to_build: bool
    next_action: NextAction


def _chip_status(check: "mr.ReviewCheckResult") -> str:
    """Map a check's pass/fail/info onto a checklist presentation status."""
    if check.status == mr.STATUS_PASS:
        return ROW_PASS
    if check.status == mr.STATUS_FAIL:
        return ROW_BLOCK if check.category == mr.CATEGORY_BLOCKING else ROW_WARN
    return ROW_NOT_STARTED  # informational checks never become chips


def builder_gate_view(
    readiness: "ReviewReadiness", *, has_slates: bool = True
) -> BuilderGateView:
    """Map a ``ReviewReadiness`` to the builder Build-gate presenter.

    Verdict precedence (design §1.1 / §7.2; no rule re-derived):

    1. ``has_slates`` is False → :data:`GATE_NOT_STARTED` (the empty-DB
       call-to-action; nothing to evaluate yet).
    2. ``readiness.summary.ready`` is True → :data:`GATE_READY`.
    3. A structural Blocking check fails (``ready_to_mark`` is False) →
       :data:`GATE_BLOCKED`.
    4. Otherwise (structurally clean but Warnings fail and/or the slate is
       not yet marked reviewed) → :data:`GATE_WARNING`.

    ``ready_to_mark`` and ``ready_to_build`` are the two enablement bits
    the page needs; for a real slate (``has_slates`` True) both are read
    straight off the readiness, never re-derived — ``ready_to_build`` is
    ``summary.ready`` verbatim. When ``has_slates`` is False (the empty-DB
    call-to-action) both are forced False: there is no slate to mark
    reviewed or build, and an empty readiness is only vacuously "ready".
    Marking reviewed always stays an explicit user action — this presenter
    only reports state, it never acknowledges (design §7.4).
    """
    ready_to_build = bool(readiness.summary.ready) and has_slates
    ready_to_mark = has_slates and not any(
        r.category == mr.CATEGORY_BLOCKING
        and r.status == mr.STATUS_FAIL
        and r.code != mr.CHECK_MANUAL_REVIEW_USER_ACK
        for r in readiness.checks
    )

    if not has_slates:
        verdict = GATE_NOT_STARTED
    elif ready_to_build:
        verdict = GATE_READY
    elif not ready_to_mark:
        verdict = GATE_BLOCKED
    else:
        verdict = GATE_WARNING

    chips = tuple(
        GateChip(
            code=c.code,
            label=_gate_chip_label(c.code),
            status=_chip_status(c),
            severity=c.category,
            message=c.message,
        )
        for c in readiness.checks
        if c.category in (mr.CATEGORY_BLOCKING, mr.CATEGORY_WARNING)
        and c.code != mr.CHECK_MANUAL_REVIEW_USER_ACK
    )
    blocking_fails = tuple(
        chip
        for chip in chips
        if chip.severity == mr.CATEGORY_BLOCKING and chip.status == ROW_BLOCK
    )

    action = recommend_next_action(readiness, has_slates=has_slates)
    return BuilderGateView(
        verdict=verdict,
        title=_GATE_TITLE[verdict],
        summary=_GATE_SUMMARY[verdict],
        chips=chips,
        blocking_fails=blocking_fails,
        ready_to_mark=ready_to_mark,
        ready_to_build=ready_to_build,
        next_action=action,
    )
