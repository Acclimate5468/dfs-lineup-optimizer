"""Mismatch Alerts v1 — Phase B slate-level alert service.

Implements design §11 / Phase B from
``docs/MISMATCH_ALERTS_V1_DESIGN.md``: a read-only service that
composes Projection v1 (``project_slate``) and the same
``ProjectionInputs`` view that fed it (``aggregate_projection_inputs``)
with the Phase A rule functions in :mod:`src.alerts.alert_rules`, then
returns a deterministically-ordered ``list[Alert]``.

Hard contracts pinned by design §11 and tested in
``tests/test_alert_service.py``:

- Public function: ``evaluate_alerts(conn, slate_id) -> list[Alert]``.
- Pure read — no INSERT / UPDATE / DELETE on any table.
- ``effective_status`` is NOT consulted (design §2 / §8;
  ``ODDS_PERSISTENCE_DESIGN.md`` §15.11 risk #7).
- The reserved ``late_news_risk`` code is never emitted in v1
  (design §3.9 / §15 risk #8).
- ``project_slate`` is called exactly once per invocation.
- ``aggregate_projection_inputs`` is also called once: Projection v1's
  output (``FighterSlateProjection``) intentionally does not surface
  raw inputs (``salary``, ``implied_win_probability``,
  ``scheduled_rounds``, ``has_fight_group`` / ``has_opponent``), but
  the rule functions in §3.1–§3.5 / §3.8 need those raw values. Re-
  reading the bundle is the documented option in design §11 ("if a
  second query is needed, the implementation slice documents why");
  the alternative would be to widen Projection v1's output, which is
  out of scope for this slice.
- Unknown / empty slate → ``[]`` (mirrors Projection v1).
"""

from __future__ import annotations

import sqlite3

from src.alerts.alert_rules import (
    Alert,
    FighterStructuralFlags,
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
from src.projections.projection_input_service import (
    aggregate_projection_inputs,
)
from src.projections.projection_service import STATUS_OK
from src.projections.slate_projection_service import project_slate


def evaluate_alerts(conn: sqlite3.Connection, slate_id: int) -> list[Alert]:
    """Return v1 mismatch alerts for ``slate_id`` in design §9 order.

    Composition steps (read-only end to end):

    1. ``project_slate(conn, slate_id)`` for the per-fighter Projection
       v1 result (status, missing_inputs, projected_dk_points).
    2. ``aggregate_projection_inputs(conn, slate_id)`` for the raw
       inputs the rule functions need (salary, p_win, scheduled rounds,
       structural flags). Both calls walk the same active-fighter set
       and emit in the same order, so the lists zip by index.
    3. Apply each Phase A fighter-scoped rule per fighter; collect any
       non-``None`` ``Alert``.
    4. Apply the slate-scoped ``fight_group_issue`` rule once over the
       full active-fighter structural flags.
    5. Sort via ``sort_alerts`` to realize design §9.
    """
    slate_id = int(slate_id)

    projections = project_slate(conn, slate_id)
    if not projections:
        return []

    bundles = aggregate_projection_inputs(conn, slate_id)

    alerts: list[Alert] = []
    structural: list[FighterStructuralFlags] = []

    for proj, bundle in zip(projections, bundles, strict=True):
        inputs = bundle.inputs
        fighter_id = proj.fighter_id
        fighter_name = proj.fighter_name
        status = proj.projection_status

        structural.append(
            FighterStructuralFlags(
                fighter_name=fighter_name,
                has_fight_group=bool(inputs.has_fight_group),
                has_opponent=bool(inputs.has_opponent),
            )
        )

        if status == STATUS_OK:
            salary = int(inputs.salary)
            pwin = float(inputs.implied_win_probability)
            points = float(proj.projected_dk_points)
            rounds = inputs.scheduled_rounds

            for rule_alert in (
                salary_inefficiency_high(
                    fighter_id=fighter_id,
                    fighter_name=fighter_name,
                    salary=salary,
                    projected_dk_points=points,
                    projection_status=status,
                ),
                salary_inefficiency_low(
                    fighter_id=fighter_id,
                    fighter_name=fighter_name,
                    salary=salary,
                    projected_dk_points=points,
                    projection_status=status,
                ),
                odds_vs_salary_mismatch(
                    fighter_id=fighter_id,
                    fighter_name=fighter_name,
                    salary=salary,
                    implied_win_probability=pwin,
                    projection_status=status,
                ),
                underdog_value(
                    fighter_id=fighter_id,
                    fighter_name=fighter_name,
                    salary=salary,
                    implied_win_probability=pwin,
                    projection_status=status,
                ),
                weak_expensive_favorite(
                    fighter_id=fighter_id,
                    fighter_name=fighter_name,
                    salary=salary,
                    implied_win_probability=pwin,
                    projection_status=status,
                ),
                five_round_edge(
                    fighter_id=fighter_id,
                    fighter_name=fighter_name,
                    scheduled_rounds=rounds,
                    implied_win_probability=pwin,
                    projection_status=status,
                ),
            ):
                if rule_alert is not None:
                    alerts.append(rule_alert)
            continue

        missing_alert = missing_input(
            fighter_id=fighter_id,
            fighter_name=fighter_name,
            projection_status=status,
            missing_inputs=proj.missing_inputs,
        )
        if missing_alert is not None:
            alerts.append(missing_alert)

        non_proj_alert = projection_non_projectable(
            fighter_id=fighter_id,
            fighter_name=fighter_name,
            projection_status=status,
            missing_inputs=proj.missing_inputs,
        )
        if non_proj_alert is not None:
            alerts.append(non_proj_alert)

    slate_alert = fight_group_issue(structural)
    if slate_alert is not None:
        alerts.append(slate_alert)

    return sort_alerts(alerts)
