"""Manual Review Gate v1 — Phase C read aggregator service.

Phase C of ``docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`` (§8 / §10 Phase C).
Composes existing repository reads and the pure Phase A check
evaluators from :mod:`src.slate.manual_review` into a single per-slate
readiness surface.

Hard contracts pinned by design §8 / §14 / §15 and the Phase C tests:

- Public function: :func:`evaluate_manual_review`. Returns a
  :class:`ReviewReadiness` value object — pure read end to end.
- No INSERT / UPDATE / DELETE on any table.
- ``project_slate`` is called at most once per invocation.
  ``evaluate_alerts`` is called at most once per invocation. (The
  alerts service calls ``project_slate`` again internally; this is
  documented in design §8 / Phase C and is the cheapest read path
  available without widening either contract.)
- ``odds_match_results.effective_status`` is the source for the odds
  coverage and review checks (§5.4.a/b/c): a fighter counts as covered
  when an eligible row (``auto_match`` / ``review_accepted`` /
  ``force_pair``) binds it, mirroring projection sourcing (D.5.2 /
  §16.9) so an inline Assign on Build clears the gate the same way it
  un-blocks projections. ``review_required`` / ``review_rejected`` rows
  count as pending review.
- Fighter Status is NOT promoted into Manual Review in v1 — the §5.7
  row stays Informational with a locked-in deferred message (design
  §5.7 / §13 / §15). Counts are not surfaced even informationally.
- ``late_news_acknowledged_at`` persistence is deferred (design §9.2 /
  §18.6). Phase C surfaces the §5.8 toggle as always-unacknowledged;
  the Phase D Streamlit page owns the session-scoped toggle.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.alerts.alert_rules import SEVERITY_INFO, SEVERITY_WARN
from src.alerts.alert_service import evaluate_alerts
from src.db.repositories import (
    FightGroupRepository,
    FighterRepository,
    OddsMatchResultRepository,
    SlateRecord,
    SlateRepository,
)
from src.projections.projection_service import (
    STATUS_MISSING_INPUTS,
    STATUS_NON_PROJECTABLE,
)
from src.projections.slate_projection_service import project_slate
from src.ingestion.effective_status_resolver import (
    is_projection_eligible_effective_status,
)
from src.slate import manual_review as mr
from src.utils.text_cleaning import normalize_name

_ACTIVE_FIGHTER_STATUS = "active"
_REVIEW_REQUIRED_STATUS = "review_required"
_REVIEW_REJECTED_STATUS = "review_rejected"
_CONFIRMED_GROUP_STATUS = "confirmed"


@dataclass(frozen=True)
class ReviewReadiness:
    """Composed Manual Review Gate v1 readout for a single slate (design §8).

    - ``slate_id``: the slate this readiness was computed for (always
      the integer passed in, regardless of whether the slate exists).
    - ``manual_review_status``: the persisted slate column. ``None`` only
      when the slate id does not exist (mirrors how :func:`project_slate`
      and :func:`evaluate_alerts` short-circuit on unknown slates).
    - ``manual_review_completed_at``: the persisted slate column, or
      ``None`` when the slate has not yet been marked reviewed (or does
      not exist).
    - ``checks``: every check the Phase A evaluators produced, in the
      deterministic order defined by :func:`mr.sort_results` (Blocking
      rank, then Warning, then Informational, with stable intra-category
      order per §5 / §8).
    - ``summary``: the aggregate :class:`mr.ManualReviewSummary`. The
      ``ready`` flag is the gate-enablement signal (True iff every
      Blocking check passes — Warning / Informational rows never affect
      it, per design §4 / §6).
    """

    slate_id: int
    manual_review_status: str | None
    manual_review_completed_at: str | None
    checks: tuple[mr.ReviewCheckResult, ...]
    summary: mr.ManualReviewSummary


def evaluate_manual_review(
    conn: sqlite3.Connection,
    slate_id: int,
    *,
    scheduled_rounds_acknowledged: bool = False,
) -> ReviewReadiness:
    """Return the Manual Review Gate v1 readout for ``slate_id``.

    ``scheduled_rounds_acknowledged`` is the session-only toggle the Streamlit
    pages own (the gate is pure / DB-free for this ack, mirroring the deferred
    late-news ack): when True it dismisses the §5.3 scheduled-rounds Warning for
    a fully-confirmed card, so a 5-round main event no longer nags forever.

    Composition order (read-only end to end):

    1. ``SlateRepository.list_all`` — locate the target slate to read its
       ``manual_review_status`` / ``manual_review_completed_at`` /
       ``salary_csv_status`` / ``salary_row_count``.
    2. ``FighterRepository.list_for_slate`` — active-fighter list.
    3. ``FightGroupRepository.list_for_slate`` — fight groups for §5.2 /
       §5.3.
    4. ``OddsMatchResultRepository.list_for_slate`` — match results for
       §5.4.
    5. ``project_slate`` — per-fighter Projection v1 rows for §5.5.
    6. ``evaluate_alerts`` — Mismatch Alerts v1 rows for §5.6.

    Per design §15:

    - Unknown slate id → ``ReviewReadiness`` with the single
      ``salary_imported`` Blocking failure and ``manual_review_status``
      / ``manual_review_completed_at`` ``None``. No other checks are
      surfaced (mirrors ``evaluate_alerts`` returning ``[]``).
    - Empty slate (slate exists, zero active fighters) →
      ``salary_imported`` fails (Blocking); ``summary.ready`` is False.
      Downstream checks are still surfaced so the page renders a
      complete section list; their Phase A evaluators degrade
      gracefully on empty inputs.
    """
    sid = int(slate_id)
    slate = _find_slate(conn, sid)

    if slate is None:
        # Unknown slate: §5.1 fails, no other checks. Mirrors the
        # "return [] on unknown slate" posture of project_slate /
        # evaluate_alerts, just with one Blocking row instead of empty
        # so the page surface still renders something explanatory.
        failure = mr.evaluate_salary_imported(
            salary_csv_status=None,
            salary_row_count=0,
            active_fighter_count=0,
        )
        return ReviewReadiness(
            slate_id=sid,
            manual_review_status=None,
            manual_review_completed_at=None,
            checks=(failure,),
            summary=mr.summarize([failure]),
        )

    fighters = FighterRepository(conn).list_for_slate(sid)
    active_fighters = [
        f for f in fighters if f.status == _ACTIVE_FIGHTER_STATUS
    ]
    active_fighter_count = len(active_fighters)

    fight_groups = FightGroupRepository(conn).list_for_slate(sid)
    match_results = OddsMatchResultRepository(conn).list_for_slate(sid)

    active_fighter_ids = {int(f.id) for f in active_fighters}
    group_member_keys = _group_member_keys(fight_groups)
    fighters_without_group = sorted(
        f.name
        for f in active_fighters
        if normalize_name(f.name) not in group_member_keys
    )
    unconfirmed_or_one_sided = _count_unconfirmed_or_one_sided(fight_groups)
    has_five_round_groups = any(
        int(g.scheduled_rounds) == 5 for g in fight_groups
    )
    unconfirmed_three_round_groups = sum(
        1
        for g in fight_groups
        if int(g.scheduled_rounds) == 3
        and g.status != _CONFIRMED_GROUP_STATUS
    )

    covered_active_ids = {
        int(r.fighter_id)
        for r in match_results
        if is_projection_eligible_effective_status(r.effective_status)
        and r.fighter_id is not None
        and int(r.fighter_id) in active_fighter_ids
    }
    covered_count = len(covered_active_ids)
    review_required_count = sum(
        1
        for r in match_results
        if r.effective_status == _REVIEW_REQUIRED_STATUS
    )
    review_rejected_count = sum(
        1
        for r in match_results
        if r.effective_status == _REVIEW_REJECTED_STATUS
    )

    projections = project_slate(conn, sid)
    non_projectable_pairs = [
        (p.fighter_name, tuple(p.missing_inputs))
        for p in projections
        if p.projection_status == STATUS_NON_PROJECTABLE
    ]
    missing_input_names = [
        p.fighter_name
        for p in projections
        if p.projection_status == STATUS_MISSING_INPUTS
    ]

    alerts = evaluate_alerts(conn, sid)
    warn_alerts = [a for a in alerts if a.severity == SEVERITY_WARN]
    info_alerts = [a for a in alerts if a.severity == SEVERITY_INFO]
    warn_codes = [a.code for a in warn_alerts]
    info_codes = [a.code for a in info_alerts]

    checks: list[mr.ReviewCheckResult] = [
        mr.evaluate_salary_imported(
            salary_csv_status=slate.salary_csv_status,
            salary_row_count=int(slate.salary_row_count),
            active_fighter_count=active_fighter_count,
        ),
        mr.evaluate_fight_group_coverage(
            active_fighters_without_group=fighters_without_group,
            active_fighter_count=active_fighter_count,
        ),
        mr.evaluate_fight_group_review(
            unconfirmed_or_one_sided_count=unconfirmed_or_one_sided,
        ),
        mr.evaluate_scheduled_rounds_reviewed(
            has_five_round_groups=has_five_round_groups,
            unconfirmed_three_round_groups=unconfirmed_three_round_groups,
            acknowledged=scheduled_rounds_acknowledged,
        ),
        mr.evaluate_odds_unmatched_active(
            active_fighter_count=active_fighter_count,
            covered_count=covered_count,
        ),
        mr.evaluate_odds_coverage_partial(
            active_fighter_count=active_fighter_count,
            covered_count=covered_count,
        ),
        mr.evaluate_odds_match_review(
            review_required_count=review_required_count,
            review_rejected_count=review_rejected_count,
        ),
        mr.evaluate_odds_coverage_stat(
            active_fighter_count=active_fighter_count,
            covered_count=covered_count,
        ),
        mr.evaluate_projection_non_projectable(
            non_projectable_fighters=non_projectable_pairs,
        ),
        mr.evaluate_projection_missing_inputs(
            missing_input_fighters=missing_input_names,
        ),
        mr.evaluate_mismatch_alerts_warn(
            warn_alert_count=len(warn_alerts),
            warn_alert_codes=warn_codes,
        ),
        mr.evaluate_mismatch_alerts_info(
            info_alert_count=len(info_alerts),
            info_alert_codes=info_codes,
        ),
        mr.evaluate_late_news_risk_locked(),
        mr.evaluate_fighter_status_review(),
        # §5.8: persistence of the ack is deferred (design §9.2 / §18.6).
        # Phase C always reports unacknowledged — the Phase D page owns
        # the session-scoped toggle.
        mr.evaluate_late_news_acknowledged(
            acknowledged=False, acknowledged_at=None
        ),
        mr.evaluate_manual_review_user_ack(
            manual_review_status=slate.manual_review_status,
            completed_at=slate.manual_review_completed_at,
        ),
    ]

    ordered = mr.sort_results(checks)
    summary = mr.summarize(ordered)
    return ReviewReadiness(
        slate_id=sid,
        manual_review_status=slate.manual_review_status,
        manual_review_completed_at=slate.manual_review_completed_at,
        checks=tuple(ordered),
        summary=summary,
    )


def _find_slate(
    conn: sqlite3.Connection, slate_id: int
) -> SlateRecord | None:
    for record in SlateRepository(conn).list_all():
        if record.id == slate_id:
            return record
    return None


def _group_member_keys(fight_groups) -> set[str]:
    keys: set[str] = set()
    for g in fight_groups:
        for name in (g.fighter_1_name, g.fighter_2_name):
            if not isinstance(name, str):
                continue
            key = normalize_name(name)
            if key:
                keys.add(key)
    return keys


def _count_unconfirmed_or_one_sided(fight_groups) -> int:
    n = 0
    for g in fight_groups:
        one_sided = not (g.fighter_1_name or "").strip() or not (
            g.fighter_2_name or ""
        ).strip()
        if one_sided or g.status != _CONFIRMED_GROUP_STATUS:
            n += 1
    return n
