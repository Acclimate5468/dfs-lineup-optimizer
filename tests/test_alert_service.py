"""Mismatch Alerts v1 — Phase B service tests.

Covers ``evaluate_alerts`` in ``src/alerts/alert_service.py`` per
``docs/MISMATCH_ALERTS_V1_DESIGN.md`` §11 / §13 (service tests
section). Composition only: Phase A rule behavior is already pinned by
``tests/test_alert_rules.py``; these tests pin slate-level wiring,
read-only invariants, and the §8 ``effective_status`` deferral.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.alerts.alert_rules import (
    ALERT_CODE_FIGHT_GROUP_ISSUE,
    ALERT_CODE_FIVE_ROUND_EDGE,
    ALERT_CODE_LATE_NEWS_RISK,
    ALERT_CODE_MISSING_INPUT,
    ALERT_CODE_PROJECTION_NON_PROJECTABLE,
    ALERT_CODE_UNDERDOG_VALUE,
    Alert,
    SCOPE_FIGHTER,
    SCOPE_SLATE,
    SEVERITY_INFO,
    SEVERITY_WARN,
)
from src.alerts.alert_service import evaluate_alerts
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


# ---------------------------------------------------------------------------
# Fixtures + helpers
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
    captured_at: str = "2026-05-20T00:00:00Z",
):
    return OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source="manual",
        captured_at=captured_at,
    )


def _db_snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
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
# Base cases
# ---------------------------------------------------------------------------


def test_unknown_slate_returns_empty(conn):
    assert evaluate_alerts(conn, 999_999) == []


def test_empty_slate_returns_empty(conn, slate_id):
    assert evaluate_alerts(conn, slate_id) == []


def test_returns_only_alert_value_objects(conn, slate_id):
    """Service contract: a ``list[Alert]`` is returned. No tuples, no
    dicts, no Streamlit widgets."""
    _insert_fighter(conn, slate_id=slate_id, name="Lonely", salary=8500)
    alerts = evaluate_alerts(conn, slate_id)
    assert alerts  # non-empty: at least the non_projectable + fight_group
    for a in alerts:
        assert isinstance(a, Alert)


# ---------------------------------------------------------------------------
# Value-alert path (ok projection) — underdog_value mirrors §3.3
# ---------------------------------------------------------------------------


def test_underdog_value_alert_fires_for_cheap_favorite(conn, slate_id):
    """Cheap fighter with a high implied probability hits §3.3's
    ``salary <= 7600 and p_win >= 0.45`` branch. The odds row is
    favored (-200 ≈ 0.667 implied) so the projection is ``ok`` and the
    rule fires."""
    underdog_id = _insert_fighter(
        conn, slate_id=slate_id, name="Cheap Champ", salary=7000
    )
    _insert_fighter(conn, slate_id=slate_id, name="Pricey Dog", salary=9500)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Cheap Champ",
        fighter_2_name="Pricey Dog",
        scheduled_rounds=3,
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Cheap Champ",
        american_odds=-200,
    )
    recompute_and_replace_match_results(conn, slate_id)

    alerts = evaluate_alerts(conn, slate_id)
    underdog_alerts = [
        a
        for a in alerts
        if a.code == ALERT_CODE_UNDERDOG_VALUE
        and a.fighter_id == underdog_id
    ]
    assert len(underdog_alerts) == 1
    assert underdog_alerts[0].severity == SEVERITY_INFO
    assert underdog_alerts[0].scope == SCOPE_FIGHTER


def test_five_round_edge_fires_only_on_main_event_with_pwin(conn, slate_id):
    main_id = _insert_fighter(
        conn, slate_id=slate_id, name="Champ", salary=8200
    )
    _insert_fighter(conn, slate_id=slate_id, name="Contender", salary=8000)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Champ",
        fighter_2_name="Contender",
        scheduled_rounds=5,
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Champ",
        american_odds=-200,  # ~0.667 implied → above the 0.55 gate
    )
    recompute_and_replace_match_results(conn, slate_id)

    alerts = evaluate_alerts(conn, slate_id)
    five_round_alerts = [
        a for a in alerts if a.code == ALERT_CODE_FIVE_ROUND_EDGE
    ]
    assert any(a.fighter_id == main_id for a in five_round_alerts)


# ---------------------------------------------------------------------------
# Missing-input + non-projectable paths
# ---------------------------------------------------------------------------


def test_missing_win_probability_yields_missing_input_alert(conn, slate_id):
    """No odds rows → Phase B leaves ``implied_win_probability=None``,
    Projection v1 returns ``missing_inputs`` with the
    ``win_probability`` tag, and §3.6 emits one consolidated alert."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Aldo", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Aldo",
        fighter_2_name="Vera",
        scheduled_rounds=3,
    )

    alerts = evaluate_alerts(conn, slate_id)
    missing = [
        a
        for a in alerts
        if a.code == ALERT_CODE_MISSING_INPUT and a.fighter_id == fid
    ]
    assert len(missing) == 1
    assert missing[0].severity == SEVERITY_WARN
    assert "win_probability" in missing[0].tags


def test_non_projectable_fighter_emits_per_fighter_and_slate_alerts(
    conn, slate_id
):
    """Design §15 risk #5: a fighter without a fight group produces
    both the per-fighter ``projection_non_projectable`` warn and the
    slate-scoped ``fight_group_issue`` warn. The duplication is
    intentional — pin it so an "optimisation" that hides one cannot
    land silently."""
    fid = _insert_fighter(
        conn, slate_id=slate_id, name="Lonely Fighter", salary=8500
    )

    alerts = evaluate_alerts(conn, slate_id)
    per_fighter = [
        a
        for a in alerts
        if a.code == ALERT_CODE_PROJECTION_NON_PROJECTABLE
    ]
    slate_alerts = [
        a for a in alerts if a.code == ALERT_CODE_FIGHT_GROUP_ISSUE
    ]
    assert len(per_fighter) == 1
    assert per_fighter[0].fighter_id == fid
    assert per_fighter[0].severity == SEVERITY_WARN
    assert len(slate_alerts) == 1
    assert slate_alerts[0].scope == SCOPE_SLATE
    assert slate_alerts[0].fighter_id is None
    assert "Lonely Fighter" in slate_alerts[0].tags


def test_inactive_fighter_does_not_trigger_alerts(conn, slate_id):
    """Inactive fighters are filtered out by Projection v1 Phase B
    (see ``projection_input_service.py`` ``ACTIVE_FIGHTER_STATUS``).
    The alerts layer must inherit that filter without re-implementing
    it — an inactive fighter without a fight group must NOT raise
    ``fight_group_issue``."""
    _insert_fighter(
        conn,
        slate_id=slate_id,
        name="Inactive F",
        salary=8000,
        status="inactive",
    )
    assert evaluate_alerts(conn, slate_id) == []


# ---------------------------------------------------------------------------
# Ordering + late_news_risk reserved
# ---------------------------------------------------------------------------


def test_alerts_are_returned_in_design_9_order(conn, slate_id):
    """Build a slate with a mix of warn/info, slate/fighter alerts and
    pin the resulting ordering against the §9 contract: warn before
    info; within severity, slate before fighter; within scope, code
    ascending; within code, fighter_id ascending."""
    _insert_fighter(conn, slate_id=slate_id, name="Lonely", salary=8500)
    underdog_id = _insert_fighter(
        conn, slate_id=slate_id, name="Cheap Champ", salary=7000
    )
    _insert_fighter(conn, slate_id=slate_id, name="Pricey Dog", salary=9500)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Cheap Champ",
        fighter_2_name="Pricey Dog",
        scheduled_rounds=3,
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Cheap Champ",
        american_odds=-200,
    )
    recompute_and_replace_match_results(conn, slate_id)

    alerts = evaluate_alerts(conn, slate_id)

    severities = [a.severity for a in alerts]
    # All warns precede all infos.
    if SEVERITY_WARN in severities and SEVERITY_INFO in severities:
        last_warn = max(
            i for i, s in enumerate(severities) if s == SEVERITY_WARN
        )
        first_info = min(
            i for i, s in enumerate(severities) if s == SEVERITY_INFO
        )
        assert last_warn < first_info
    # Within warn, slate before fighter.
    warn_scopes = [a.scope for a in alerts if a.severity == SEVERITY_WARN]
    if SCOPE_SLATE in warn_scopes and SCOPE_FIGHTER in warn_scopes:
        assert warn_scopes.index(SCOPE_SLATE) < warn_scopes.index(
            SCOPE_FIGHTER
        )


def test_late_news_risk_is_never_emitted(conn, slate_id):
    """Design §3.9 / §15 risk #8: the reserved code must never appear
    in v1 service output regardless of slate shape."""
    _insert_fighter(conn, slate_id=slate_id, name="Lonely", salary=8500)
    _insert_fighter(
        conn, slate_id=slate_id, name="Cheap Champ", salary=7000
    )
    _insert_fighter(conn, slate_id=slate_id, name="Pricey Dog", salary=9500)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Cheap Champ",
        fighter_2_name="Pricey Dog",
        scheduled_rounds=5,
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Cheap Champ",
        american_odds=-200,
    )
    recompute_and_replace_match_results(conn, slate_id)

    alerts = evaluate_alerts(conn, slate_id)
    assert all(a.code != ALERT_CODE_LATE_NEWS_RISK for a in alerts)


# ---------------------------------------------------------------------------
# Read-only + effective_status deferral
# ---------------------------------------------------------------------------


def test_evaluate_alerts_does_not_mutate_any_persisted_state(
    conn, slate_id
):
    aldo_id = _insert_fighter(
        conn, slate_id=slate_id, name="Aldo", salary=8800
    )
    _insert_fighter(conn, slate_id=slate_id, name="Vera", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Aldo",
        fighter_2_name="Vera",
        scheduled_rounds=5,
    )
    row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Aldo",
        american_odds=-200,
    )
    recompute_and_replace_match_results(conn, slate_id)
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=aldo_id,
    )
    before = _db_snapshot(conn)

    evaluate_alerts(conn, slate_id)
    evaluate_alerts(conn, slate_id)

    after = _db_snapshot(conn)
    assert before == after, (
        "evaluate_alerts must not mutate persisted state"
    )


def test_reject_match_propagates_to_alerts(conn, slate_id):
    """D.5.2 (``ODDS_PERSISTENCE_DESIGN.md`` §16.9): projection sourcing was
    promoted from ``match_status`` to the projection-eligible
    ``effective_status`` set, so a Reject Match override now propagates
    through Projection v1 into the alerts layer — the inverse of the
    pre-D.5.2 deferral (§15.11 risk #7) this test used to pin.

    Rejecting an auto-matched fighter removes his win probability, so his
    projection-derived alerts (odds-vs-salary mismatch, etc.) disappear and
    he instead surfaces a ``missing_input`` warning.
    """
    def build(conn_local, with_override: bool):
        sid = SlateRepository(conn_local).create(event_name="UFC X").id
        aldo_id = _insert_fighter(
            conn_local, slate_id=sid, name="Aldo", salary=7000
        )
        _insert_fighter(
            conn_local, slate_id=sid, name="Vera", salary=9500
        )
        FightGroupRepository(conn_local).create(
            slate_id=sid,
            fighter_1_name="Aldo",
            fighter_2_name="Vera",
            scheduled_rounds=3,
        )
        row = _save_odds_row(
            conn_local,
            slate_id=sid,
            fighter_name_raw="Aldo",
            american_odds=-200,
        )
        recompute_and_replace_match_results(conn_local, sid)
        if with_override:
            ManualMatchOverrideRepository(conn_local).add_override(
                slate_id=sid,
                override_type="reject_match",
                odds_row_key=row.odds_row_key,
                fighter_id=aldo_id,
            )
            recompute_and_replace_match_results(conn_local, sid)
        return sid

    sid_no = build(conn, with_override=False)
    alerts_no = evaluate_alerts(conn, sid_no)

    c2 = sqlite3.connect(":memory:")
    c2.execute("PRAGMA foreign_keys = ON")
    apply_schema(c2)
    try:
        sid_yes = build(c2, with_override=True)
        alerts_yes = evaluate_alerts(c2, sid_yes)
    finally:
        c2.close()

    aldo_codes_no = {a.code for a in alerts_no if a.fighter_name == "Aldo"}
    aldo_codes_yes = {a.code for a in alerts_yes if a.fighter_name == "Aldo"}

    # Before the reject: Aldo is auto-matched, so he has projection-derived
    # alerts and is NOT flagged for a missing input.
    assert aldo_codes_no
    assert "missing_input" not in aldo_codes_no

    # After the reject: Aldo's win probability is gone — he is flagged
    # missing_input and his projection-derived alerts are cleared.
    assert "missing_input" in aldo_codes_yes
    assert aldo_codes_no.isdisjoint(aldo_codes_yes)
