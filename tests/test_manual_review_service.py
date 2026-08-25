"""Phase C tests for ``src/slate/manual_review_service.py``.

Pins ``docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`` §8 / §10 Phase C / §15
(service-tests section). Composition only: Phase A pure evaluator
behavior is already pinned by ``tests/test_manual_review_gate.py`` and
Phase B persistence by ``tests/test_manual_review_repository.py``. The
tests here cover slate-level wiring, read-only invariants, the §14
``effective_status`` deferral, the §13 Fighter Status deferral, and
the §9.2 / §18.6 late-news ack deferral.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.alerts.alert_rules import (
    ALERT_CODE_FIGHT_GROUP_ISSUE,
    ALERT_CODE_LATE_NEWS_RISK,
)
from src.db.repositories import (
    FightGroupRepository,
    ManualMatchOverrideRepository,
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.dk_salary_importer import ParsedSalaryRow
from src.ingestion.odds_matching_service import (
    OddsMatchResultRecord,
    recompute_and_replace_match_results,
)
from src.slate import manual_review as mr
from src.slate.manual_review_service import (
    ReviewReadiness,
    evaluate_manual_review,
)


# ---------------------------------------------------------------------------
# fixtures + helpers
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
    return SlateRepository(conn).create(
        event_name="UFC 999",
        salary_csv_status="validated",
        salary_row_count=26,
    ).id


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
    )
    snap: dict[str, list[tuple]] = {}
    for table in tables:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        ).fetchall()
        snap[table] = [tuple(r) for r in rows]
    return snap


def _by_code(readiness: ReviewReadiness) -> dict[str, mr.ReviewCheckResult]:
    return {c.code: c for c in readiness.checks}


def _seed_clean_two_fighter_slate(conn, slate_id, *, scheduled_rounds: int = 3):
    """A minimum two-fighter slate with confirmed groups, auto-matched odds,
    and a sensible projection. Mirrors the real-feed smoke shape — every
    Blocking check should pass except ``manual_review_user_ack``."""
    a_id = _insert_fighter(
        conn, slate_id=slate_id, name="Cheap Champ", salary=7000
    )
    b_id = _insert_fighter(
        conn, slate_id=slate_id, name="Pricey Dog", salary=9500
    )
    fg = FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Cheap Champ",
        fighter_2_name="Pricey Dog",
        scheduled_rounds=scheduled_rounds,
    )
    FightGroupRepository(conn).update_status(fg.id, "confirmed")
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Cheap Champ",
        american_odds=-200,
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Pricey Dog",
        american_odds=+170,
        captured_at="2026-05-20T00:01:00Z",
    )
    recompute_and_replace_match_results(conn, slate_id)
    return a_id, b_id


# ---------------------------------------------------------------------------
# Return shape + base cases
# ---------------------------------------------------------------------------


def test_returns_review_readiness_value_object(conn, slate_id):
    readout = evaluate_manual_review(conn, slate_id)
    assert isinstance(readout, ReviewReadiness)
    assert readout.slate_id == slate_id
    for c in readout.checks:
        assert isinstance(c, mr.ReviewCheckResult)
    assert isinstance(readout.summary, mr.ManualReviewSummary)


def test_unknown_slate_returns_single_blocking_failure(conn):
    """§15: unknown slate id → ReviewReadiness with the single
    ``salary_imported`` Blocking failure and no other checks."""
    readout = evaluate_manual_review(conn, 999_999)
    assert readout.slate_id == 999_999
    assert readout.manual_review_status is None
    assert readout.manual_review_completed_at is None
    assert len(readout.checks) == 1
    only = readout.checks[0]
    assert only.code == mr.CHECK_SALARY_IMPORTED
    assert only.category == mr.CATEGORY_BLOCKING
    assert only.status == mr.STATUS_FAIL
    assert readout.summary.ready is False
    assert readout.summary.blocking_count == 1
    assert readout.summary.warning_count == 0
    assert readout.summary.info_count == 0


def test_empty_db_with_no_slates_returns_unknown_slate_shape(conn):
    """A slate id of 1 against a fresh DB with zero slates behaves like
    the unknown-slate case — same single Blocking failure surface."""
    readout = evaluate_manual_review(conn, 1)
    assert readout.manual_review_status is None
    assert len(readout.checks) == 1
    assert readout.checks[0].code == mr.CHECK_SALARY_IMPORTED
    assert readout.checks[0].status == mr.STATUS_FAIL


def test_empty_slate_with_no_fighters_fails_salary_imported(conn, slate_id):
    """§15: empty slate (slate exists, no active fighters) — §5.1 fails;
    ``summary.ready`` is False."""
    readout = evaluate_manual_review(conn, slate_id)
    checks = _by_code(readout)
    assert checks[mr.CHECK_SALARY_IMPORTED].status == mr.STATUS_FAIL
    assert readout.summary.ready is False
    # Downstream Phase A evaluators degrade gracefully on empty input
    # (they have explicit ``active_fighter_count <= 0`` branches), so the
    # full check list is still surfaced for the page to render.
    assert mr.CHECK_ODDS_UNMATCHED_ACTIVE in checks
    assert mr.CHECK_FIGHT_GROUP_COVERAGE in checks


def test_inactive_fighters_are_not_counted_as_active(conn, slate_id):
    """Inactive fighters must not contribute to ``active_fighter_count``;
    a slate with only inactive rows is treated as empty for the §5.1
    check."""
    _insert_fighter(
        conn,
        slate_id=slate_id,
        name="Inactive F",
        salary=8000,
        status="inactive",
    )
    readout = evaluate_manual_review(conn, slate_id)
    checks = _by_code(readout)
    assert checks[mr.CHECK_SALARY_IMPORTED].status == mr.STATUS_FAIL


# ---------------------------------------------------------------------------
# Salary import / active fighter checks (§5.1)
# ---------------------------------------------------------------------------


def test_salary_imported_passes_for_validated_slate_with_active_fighters(
    conn, slate_id
):
    _seed_clean_two_fighter_slate(conn, slate_id)
    readout = evaluate_manual_review(conn, slate_id)
    salary = _by_code(readout)[mr.CHECK_SALARY_IMPORTED]
    assert salary.status == mr.STATUS_PASS
    assert salary.category == mr.CATEGORY_BLOCKING


def test_unvalidated_csv_status_fails_salary_imported(conn):
    """A slate whose ``salary_csv_status`` is the default
    ``'unvalidated'`` must fail §5.1 even when fighters are present."""
    sid = SlateRepository(conn).create(event_name="Unvalidated").id
    _insert_fighter(conn, slate_id=sid, name="A", salary=7000)
    _insert_fighter(conn, slate_id=sid, name="B", salary=8000)
    readout = evaluate_manual_review(conn, sid)
    salary = _by_code(readout)[mr.CHECK_SALARY_IMPORTED]
    assert salary.status == mr.STATUS_FAIL


# ---------------------------------------------------------------------------
# Fight groups (§5.2.a / §5.2.b) + scheduled rounds (§5.3)
# ---------------------------------------------------------------------------


def test_no_fight_groups_fails_fight_group_coverage(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Lonely A", salary=8000)
    _insert_fighter(conn, slate_id=slate_id, name="Lonely B", salary=8200)

    readout = evaluate_manual_review(conn, slate_id)
    cov = _by_code(readout)[mr.CHECK_FIGHT_GROUP_COVERAGE]
    assert cov.status == mr.STATUS_FAIL
    assert cov.category == mr.CATEGORY_BLOCKING
    assert "Lonely A" in cov.tags
    assert "Lonely B" in cov.tags
    assert readout.summary.ready is False


def test_odd_active_fighter_count_leaves_at_least_one_uncovered(conn, slate_id):
    """An odd active-fighter count guarantees at least one fighter with
    no fight group; §5.2.a must catch this."""
    _insert_fighter(conn, slate_id=slate_id, name="A", salary=8000)
    _insert_fighter(conn, slate_id=slate_id, name="B", salary=8200)
    _insert_fighter(conn, slate_id=slate_id, name="C", salary=8400)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="A",
        fighter_2_name="B",
        scheduled_rounds=3,
    )
    readout = evaluate_manual_review(conn, slate_id)
    cov = _by_code(readout)[mr.CHECK_FIGHT_GROUP_COVERAGE]
    assert cov.status == mr.STATUS_FAIL
    assert cov.tags == ("C",)


def test_unconfirmed_group_warns_under_fight_group_review(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="A", salary=8000)
    _insert_fighter(conn, slate_id=slate_id, name="B", salary=8200)
    fg = FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="A",
        fighter_2_name="B",
        scheduled_rounds=3,
    )
    # Default status from create is "unconfirmed" — leave it as-is.
    assert fg.status == "unconfirmed"
    readout = evaluate_manual_review(conn, slate_id)
    rev = _by_code(readout)[mr.CHECK_FIGHT_GROUP_REVIEW]
    assert rev.category == mr.CATEGORY_WARNING
    assert rev.status == mr.STATUS_FAIL


def test_confirmed_group_passes_fight_group_review(conn, slate_id):
    _seed_clean_two_fighter_slate(conn, slate_id)
    readout = evaluate_manual_review(conn, slate_id)
    rev = _by_code(readout)[mr.CHECK_FIGHT_GROUP_REVIEW]
    assert rev.status == mr.STATUS_PASS


def test_scheduled_rounds_warns_when_five_round_group_present(conn, slate_id):
    _seed_clean_two_fighter_slate(conn, slate_id, scheduled_rounds=5)
    readout = evaluate_manual_review(conn, slate_id)
    sr = _by_code(readout)[mr.CHECK_SCHEDULED_ROUNDS_REVIEWED]
    assert sr.category == mr.CATEGORY_WARNING
    assert sr.status == mr.STATUS_FAIL


def test_scheduled_rounds_ack_dismisses_warning_for_confirmed_card(
    conn, slate_id
):
    """The session-only ack passed by the page dismisses the §5.3 warning for a
    confirmed 5-round card."""
    _seed_clean_two_fighter_slate(conn, slate_id, scheduled_rounds=5)
    readout = evaluate_manual_review(
        conn, slate_id, scheduled_rounds_acknowledged=True
    )
    sr = _by_code(readout)[mr.CHECK_SCHEDULED_ROUNDS_REVIEWED]
    assert sr.status == mr.STATUS_PASS


def test_scheduled_rounds_passes_when_all_three_round_groups_confirmed(
    conn, slate_id
):
    _seed_clean_two_fighter_slate(conn, slate_id, scheduled_rounds=3)
    readout = evaluate_manual_review(conn, slate_id)
    sr = _by_code(readout)[mr.CHECK_SCHEDULED_ROUNDS_REVIEWED]
    assert sr.status == mr.STATUS_PASS


# ---------------------------------------------------------------------------
# Odds matching (§5.4)
# ---------------------------------------------------------------------------


def test_no_odds_rows_and_no_match_results_fails_unmatched_active(
    conn, slate_id
):
    _insert_fighter(conn, slate_id=slate_id, name="A", salary=8000)
    _insert_fighter(conn, slate_id=slate_id, name="B", salary=8200)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="A",
        fighter_2_name="B",
        scheduled_rounds=3,
    )
    readout = evaluate_manual_review(conn, slate_id)
    unm = _by_code(readout)[mr.CHECK_ODDS_UNMATCHED_ACTIVE]
    assert unm.status == mr.STATUS_FAIL
    assert unm.category == mr.CATEGORY_BLOCKING


def test_majority_unmatched_fails_blocking_threshold(conn, slate_id):
    """100% uncovered fails §5.4.a's 50% threshold."""
    _insert_fighter(conn, slate_id=slate_id, name="A", salary=7000)
    _insert_fighter(conn, slate_id=slate_id, name="B", salary=7000)
    _insert_fighter(conn, slate_id=slate_id, name="C", salary=7000)
    _insert_fighter(conn, slate_id=slate_id, name="D", salary=7000)
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="A",
        fighter_2_name="B",
        scheduled_rounds=3,
    )
    FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="C",
        fighter_2_name="D",
        scheduled_rounds=3,
    )
    # Only one fighter (A) gets an odds row → 1 of 4 auto-matched → 75%
    # uncovered, above the 50% threshold.
    _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="A")
    recompute_and_replace_match_results(conn, slate_id)

    readout = evaluate_manual_review(conn, slate_id)
    unm = _by_code(readout)[mr.CHECK_ODDS_UNMATCHED_ACTIVE]
    assert unm.status == mr.STATUS_FAIL


def test_partial_coverage_warns_below_blocking_threshold(conn):
    """3 of 4 covered → 25% uncovered, below the 50% blocking threshold;
    §5.4.b warns, §5.4.a passes."""
    sid = SlateRepository(conn).create(
        event_name="UFC Partial",
        salary_csv_status="validated",
        salary_row_count=4,
    ).id
    _insert_fighter(conn, slate_id=sid, name="A", salary=7000)
    _insert_fighter(conn, slate_id=sid, name="B", salary=7000)
    _insert_fighter(conn, slate_id=sid, name="C", salary=7000)
    _insert_fighter(conn, slate_id=sid, name="D", salary=7000)
    FightGroupRepository(conn).create(
        slate_id=sid,
        fighter_1_name="A",
        fighter_2_name="B",
        scheduled_rounds=3,
    )
    FightGroupRepository(conn).create(
        slate_id=sid,
        fighter_1_name="C",
        fighter_2_name="D",
        scheduled_rounds=3,
    )
    for i, name in enumerate(("A", "B", "C")):
        _save_odds_row(
            conn,
            slate_id=sid,
            fighter_name_raw=name,
            captured_at=f"2026-05-20T00:0{i}:00Z",
        )
    recompute_and_replace_match_results(conn, sid)

    readout = evaluate_manual_review(conn, sid)
    unm = _by_code(readout)[mr.CHECK_ODDS_UNMATCHED_ACTIVE]
    partial = _by_code(readout)[mr.CHECK_ODDS_COVERAGE_PARTIAL]
    assert unm.status == mr.STATUS_PASS
    assert partial.status == mr.STATUS_FAIL
    assert partial.category == mr.CATEGORY_WARNING


def test_full_coverage_passes_unmatched_and_partial(conn, slate_id):
    _seed_clean_two_fighter_slate(conn, slate_id)
    readout = evaluate_manual_review(conn, slate_id)
    assert _by_code(readout)[mr.CHECK_ODDS_UNMATCHED_ACTIVE].status == mr.STATUS_PASS
    assert _by_code(readout)[mr.CHECK_ODDS_COVERAGE_PARTIAL].status == mr.STATUS_PASS


def test_odds_match_review_warns_on_review_required(conn, slate_id):
    """Seed a ``review_required`` match result directly so §5.4.c warns
    even though §5.4.a / §5.4.b are clean."""
    a_id, _b_id = _seed_clean_two_fighter_slate(conn, slate_id)

    # Add an extra odds row that no auto-matcher can pin (no fighter on
    # the slate matches it) — then directly seed a review_required
    # result row alongside the existing auto_match ones.
    extra = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Stranger Name",
        captured_at="2026-05-20T01:00:00Z",
    )
    existing = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    seeded = list(existing) + [
        OddsMatchResultRecord(
            slate_id=slate_id,
            odds_row_id=extra.id,
            odds_row_key=extra.odds_row_key,
            fighter_id=None,
            match_status="review_required",
            effective_status="review_required",
            match_stage="fuzzy",
            match_score=80,
            opponent_check="unknown",
            preferred_candidate=None,
            candidates=(),
            notes=(),
        )
    ]
    OddsMatchResultRepository(conn).replace_for_slate(slate_id, seeded)

    readout = evaluate_manual_review(conn, slate_id)
    rev = _by_code(readout)[mr.CHECK_ODDS_MATCH_REVIEW]
    assert rev.status == mr.STATUS_FAIL
    assert rev.category == mr.CATEGORY_WARNING
    # Ensure §5.4.c does not promote anything to Blocking on the gate.
    assert readout.summary.ready is False  # only because of user_ack
    assert _by_code(readout)[mr.CHECK_MANUAL_REVIEW_USER_ACK].status == mr.STATUS_FAIL


def test_odds_match_review_warns_on_review_rejected_effective_status(
    conn, slate_id
):
    """Per §5.4.c the warning also fires when a reject_match override
    has flipped ``effective_status`` to ``review_rejected``."""
    a_id, _b_id = _seed_clean_two_fighter_slate(conn, slate_id)
    a_row = next(
        r
        for r in OddsRowRepository(conn).list_for_slate(slate_id)
        if r.fighter_name_normalized == "cheap champ"
    )
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=a_row.odds_row_key,
        fighter_id=a_id,
    )
    recompute_and_replace_match_results(conn, slate_id)

    readout = evaluate_manual_review(conn, slate_id)
    rev = _by_code(readout)[mr.CHECK_ODDS_MATCH_REVIEW]
    assert rev.status == mr.STATUS_FAIL


def _rewrite_fighter_match(conn, slate_id, fighter_id, *, match_status, effective_status):
    """Rewrite the persisted match result for ``fighter_id`` to the given
    statuses (the shape ``record_assign_match_override`` produces), leaving
    every other row untouched."""
    rewritten = []
    for r in OddsMatchResultRepository(conn).list_for_slate(slate_id):
        if r.fighter_id == fighter_id:
            rewritten.append(
                OddsMatchResultRecord(
                    slate_id=r.slate_id,
                    odds_row_id=r.odds_row_id,
                    odds_row_key=r.odds_row_key,
                    fighter_id=r.fighter_id,
                    match_status=match_status,
                    effective_status=effective_status,
                    match_stage=r.match_stage,
                    match_score=r.match_score,
                    preferred_candidate=r.preferred_candidate,
                    opponent_check=r.opponent_check,
                    candidates=r.candidates,
                    notes=r.notes,
                )
            )
        else:
            rewritten.append(r)
    OddsMatchResultRepository(conn).replace_for_slate(slate_id, rewritten)


def test_inline_accepted_row_counts_as_covered_and_clears_review(conn, slate_id):
    """Slice 1 (D.5.2 / §16.9 alignment): the odds coverage + review checks
    source from ``effective_status``, so a row bound by an accept_match
    override (``effective_status='review_accepted'`` while ``match_status``
    stays ``review_required``) counts its fighter as covered and is no longer
    pending review — the same predicate projections use. An inline Assign on
    Build therefore clears the gate, not just projections."""
    a_id, _b_id = _seed_clean_two_fighter_slate(conn, slate_id)
    _rewrite_fighter_match(
        conn,
        slate_id,
        a_id,
        match_status="review_required",
        effective_status="review_accepted",
    )

    bc = _by_code(evaluate_manual_review(conn, slate_id))
    # A (review_accepted) + B (auto_match) → both covered.
    assert bc[mr.CHECK_ODDS_UNMATCHED_ACTIVE].status == mr.STATUS_PASS
    assert bc[mr.CHECK_ODDS_COVERAGE_PARTIAL].status == mr.STATUS_PASS
    # The accepted row is not pending review (sourced from effective_status).
    assert bc[mr.CHECK_ODDS_MATCH_REVIEW].status == mr.STATUS_PASS


def test_force_pair_row_counts_as_covered(conn, slate_id):
    """A force_pair binding (``effective_status='force_pair'`` over a matcher
    ``unmatched`` row) also counts its fighter as covered — force_pair is in
    the projection-eligible set the gate now reads."""
    a_id, _b_id = _seed_clean_two_fighter_slate(conn, slate_id)
    _rewrite_fighter_match(
        conn,
        slate_id,
        a_id,
        match_status="unmatched",
        effective_status="force_pair",
    )

    bc = _by_code(evaluate_manual_review(conn, slate_id))
    assert bc[mr.CHECK_ODDS_UNMATCHED_ACTIVE].status == mr.STATUS_PASS
    assert bc[mr.CHECK_ODDS_COVERAGE_PARTIAL].status == mr.STATUS_PASS
    assert bc[mr.CHECK_ODDS_MATCH_REVIEW].status == mr.STATUS_PASS


def test_odds_coverage_stat_is_informational(conn, slate_id):
    _seed_clean_two_fighter_slate(conn, slate_id)
    readout = evaluate_manual_review(conn, slate_id)
    stat = _by_code(readout)[mr.CHECK_ODDS_COVERAGE_STAT]
    assert stat.category == mr.CATEGORY_INFORMATIONAL
    assert stat.status == mr.STATUS_INFO


# ---------------------------------------------------------------------------
# Projection (§5.5)
# ---------------------------------------------------------------------------


def test_lonely_fighter_surfaces_projection_non_projectable(conn, slate_id):
    """A fighter with no fight group projects as ``non_projectable`` —
    §5.5.a must fire (Blocking)."""
    _insert_fighter(
        conn, slate_id=slate_id, name="Lonely", salary=8500
    )
    readout = evaluate_manual_review(conn, slate_id)
    non_proj = _by_code(readout)[mr.CHECK_PROJECTION_NON_PROJECTABLE]
    assert non_proj.status == mr.STATUS_FAIL
    assert non_proj.category == mr.CATEGORY_BLOCKING
    assert "Lonely" in non_proj.tags


def test_missing_win_probability_surfaces_projection_missing_inputs(
    conn, slate_id
):
    """A fighter with a confirmed fight group but no odds row projects
    as ``missing_inputs`` (``win_probability``) — §5.5.b warns."""
    _insert_fighter(conn, slate_id=slate_id, name="Aldo", salary=8800)
    _insert_fighter(conn, slate_id=slate_id, name="Vera", salary=8200)
    fg = FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Aldo",
        fighter_2_name="Vera",
        scheduled_rounds=3,
    )
    FightGroupRepository(conn).update_status(fg.id, "confirmed")

    readout = evaluate_manual_review(conn, slate_id)
    missing = _by_code(readout)[mr.CHECK_PROJECTION_MISSING_INPUTS]
    assert missing.status == mr.STATUS_FAIL
    assert missing.category == mr.CATEGORY_WARNING
    # Both fighters should be tagged.
    assert "Aldo" in missing.tags
    assert "Vera" in missing.tags


# ---------------------------------------------------------------------------
# Mismatch alerts (§5.6)
# ---------------------------------------------------------------------------


def test_warn_severity_alerts_surface_mismatch_alerts_warn(conn, slate_id):
    """Lonely-fighter slate yields the ``projection_non_projectable``
    + ``fight_group_issue`` warn alerts; §5.6.a should warn and surface
    the contributing codes."""
    _insert_fighter(
        conn, slate_id=slate_id, name="Lonely", salary=8500
    )
    readout = evaluate_manual_review(conn, slate_id)
    warn = _by_code(readout)[mr.CHECK_MISMATCH_ALERTS_WARN]
    assert warn.status == mr.STATUS_FAIL
    assert warn.category == mr.CATEGORY_WARNING
    assert ALERT_CODE_FIGHT_GROUP_ISSUE in warn.tags


def test_info_severity_alerts_render_informational(conn, slate_id):
    """The clean two-fighter slate produces value-info alerts (e.g.
    underdog_value). §5.6.b is Informational regardless of count."""
    _seed_clean_two_fighter_slate(conn, slate_id)
    readout = evaluate_manual_review(conn, slate_id)
    info = _by_code(readout)[mr.CHECK_MISMATCH_ALERTS_INFO]
    assert info.category == mr.CATEGORY_INFORMATIONAL
    assert info.status == mr.STATUS_INFO


def test_late_news_risk_locked_is_always_present_and_informational(
    conn, slate_id
):
    """§5.6.c: ``late_news_risk_locked`` is informational and always
    rendered. Mismatch Alerts v1 never emits the reserved
    ``late_news_risk`` code, so the warn row's tags must not contain
    it either (cross-cutting test from design §15)."""
    _seed_clean_two_fighter_slate(conn, slate_id)
    readout = evaluate_manual_review(conn, slate_id)
    locked = _by_code(readout)[mr.CHECK_LATE_NEWS_RISK_LOCKED]
    assert locked.category == mr.CATEGORY_INFORMATIONAL
    assert locked.status == mr.STATUS_INFO
    warn = _by_code(readout)[mr.CHECK_MISMATCH_ALERTS_WARN]
    assert ALERT_CODE_LATE_NEWS_RISK not in warn.tags


# ---------------------------------------------------------------------------
# Fighter Status deferral (§5.7 / §13)
# ---------------------------------------------------------------------------


def test_fighter_status_review_is_locked_to_informational_in_v1(
    conn, slate_id
):
    """§5.7 / §13: v1 must not promote Fighter Status to Blocking /
    Warning. The row is always Informational and surfaces a fixed
    "not yet active" message even if a fighter manual_status is set
    to a blocking-category value."""
    a_id, _b_id = _seed_clean_two_fighter_slate(conn, slate_id)
    # Seed a Blocking-category Fighter Status manual override.
    conn.execute(
        "UPDATE fighters SET manual_status = 'out', "
        "manual_status_set_at = datetime('now') WHERE id = ?",
        (a_id,),
    )
    conn.commit()

    readout = evaluate_manual_review(conn, slate_id)
    fs_check = _by_code(readout)[mr.CHECK_FIGHTER_STATUS_REVIEW]
    assert fs_check.category == mr.CATEGORY_INFORMATIONAL
    assert fs_check.status == mr.STATUS_INFO
    # And the gate must not be flipped to non-ready by Fighter Status.
    # (manual_review_user_ack is still blocking, but Fighter Status
    # itself must not have added a Blocking failure.)
    blocking_fails = [
        c
        for c in readout.checks
        if c.category == mr.CATEGORY_BLOCKING and c.status == mr.STATUS_FAIL
    ]
    assert all(
        c.code != mr.CHECK_FIGHTER_STATUS_REVIEW for c in blocking_fails
    )


# ---------------------------------------------------------------------------
# Late-news ack deferral (§5.8 / §9.2 / §18.6)
# ---------------------------------------------------------------------------


def test_late_news_acknowledged_warns_in_phase_c_until_ui_lands(
    conn, slate_id
):
    """§9.2 / §18.6: ``late_news_acknowledged_at`` persistence is
    deferred to a follow-up slice. Phase C reports the ack as
    unconditionally unset; the Phase D Streamlit page owns the
    session-scoped toggle."""
    _seed_clean_two_fighter_slate(conn, slate_id)
    readout = evaluate_manual_review(conn, slate_id)
    late = _by_code(readout)[mr.CHECK_LATE_NEWS_ACKNOWLEDGED]
    assert late.category == mr.CATEGORY_WARNING
    assert late.status == mr.STATUS_FAIL


# ---------------------------------------------------------------------------
# Manual review status (§5.9) — pre / post reviewed
# ---------------------------------------------------------------------------


def test_manual_review_user_ack_fails_when_status_is_not_reviewed(
    conn, slate_id
):
    _seed_clean_two_fighter_slate(conn, slate_id)
    readout = evaluate_manual_review(conn, slate_id)
    ack = _by_code(readout)[mr.CHECK_MANUAL_REVIEW_USER_ACK]
    assert ack.status == mr.STATUS_FAIL
    assert ack.category == mr.CATEGORY_BLOCKING
    assert readout.manual_review_status == "not_reviewed"
    assert readout.manual_review_completed_at is None


def test_manual_review_user_ack_passes_after_repository_marks_reviewed(
    conn, slate_id
):
    """After ``set_manual_review_reviewed`` flips the slate column,
    the service must reflect ``reviewed`` and surface the timestamp."""
    _seed_clean_two_fighter_slate(conn, slate_id)
    SlateRepository(conn).set_manual_review_reviewed(slate_id)

    readout = evaluate_manual_review(conn, slate_id)
    ack = _by_code(readout)[mr.CHECK_MANUAL_REVIEW_USER_ACK]
    assert ack.status == mr.STATUS_PASS
    assert readout.manual_review_status == "reviewed"
    assert readout.manual_review_completed_at
    # All other Blocking checks are clean on this fixture; the gate is
    # now ready.
    assert readout.summary.ready is True


def test_summary_ready_requires_every_blocking_check_to_pass(
    conn, slate_id
):
    """A slate that is otherwise clean but has not been marked
    reviewed must have ``summary.ready == False``; the ack flip is the
    only step left."""
    _seed_clean_two_fighter_slate(conn, slate_id)
    pre = evaluate_manual_review(conn, slate_id)
    assert pre.summary.ready is False  # user_ack blocking
    SlateRepository(conn).set_manual_review_reviewed(slate_id)
    post = evaluate_manual_review(conn, slate_id)
    assert post.summary.ready is True


# ---------------------------------------------------------------------------
# Read-only invariants + no side effects (§15 cross-cutting)
# ---------------------------------------------------------------------------


def test_evaluate_manual_review_does_not_mutate_persisted_state(
    conn, slate_id
):
    a_id, _b_id = _seed_clean_two_fighter_slate(conn, slate_id)
    # Add a reject_match override so the snapshot includes the override
    # table and the effective_status column.
    a_row = next(
        r
        for r in OddsRowRepository(conn).list_for_slate(slate_id)
        if r.fighter_name_normalized == "cheap champ"
    )
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=a_row.odds_row_key,
        fighter_id=a_id,
    )
    recompute_and_replace_match_results(conn, slate_id)

    before = _db_snapshot(conn)
    evaluate_manual_review(conn, slate_id)
    evaluate_manual_review(conn, slate_id)
    after = _db_snapshot(conn)
    assert before == after


def test_evaluate_manual_review_does_not_change_slate_columns(
    conn, slate_id
):
    """A Phase C call must not flip ``manual_review_status`` or
    ``manual_review_completed_at`` on the slate row — the gate is
    user-owned (§6, §15 cross-cutting)."""
    _seed_clean_two_fighter_slate(conn, slate_id)
    before = conn.execute(
        "SELECT manual_review_status, manual_review_completed_at "
        "FROM slates WHERE id = ?",
        (slate_id,),
    ).fetchone()
    evaluate_manual_review(conn, slate_id)
    after = conn.execute(
        "SELECT manual_review_status, manual_review_completed_at "
        "FROM slates WHERE id = ?",
        (slate_id,),
    ).fetchone()
    assert before == after == ("not_reviewed", None)


def test_reject_match_propagates_to_projection_and_alert_checks(
    conn, slate_id
):
    """D.5.2 (``ODDS_PERSISTENCE_DESIGN.md`` §16.9): projection sourcing was
    promoted from ``match_status`` to the projection-eligible
    ``effective_status`` set. The Manual Review service composes
    ``project_slate`` / the alerts layer, so a ``reject_match`` (which flips
    ``effective_status`` to ``review_rejected``) now ripples into the
    projection and mismatch-alert checks — the inverse of the pre-D.5.2
    deferral (§15.11 risk #7) the old test pinned.

    Checks genuinely independent of odds win probability (salary, fight
    group, scheduled rounds, fighter status, late news, user ack, and the
    non-projectable check — the fighter still has a fight group) stay
    identical across the override.
    """
    a_id, _b_id = _seed_clean_two_fighter_slate(conn, slate_id)
    no_override = evaluate_manual_review(conn, slate_id)

    # Apply the override and recompute; gather a second readout.
    a_row = next(
        r
        for r in OddsRowRepository(conn).list_for_slate(slate_id)
        if r.fighter_name_normalized == "cheap champ"
    )
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=a_row.odds_row_key,
        fighter_id=a_id,
    )
    recompute_and_replace_match_results(conn, slate_id)
    with_override = evaluate_manual_review(conn, slate_id)

    a_map = {c.code: (c.status, c.tags) for c in no_override.checks}
    b_map = {c.code: (c.status, c.tags) for c in with_override.checks}

    # D.5.2 ripple: the rejected fighter loses its win probability, so the
    # projection missing-inputs check now flags it (and the mismatch-alert
    # checks shift with the changed projection). §5.4.c (odds_match_review)
    # already legitimately consumed effective_status pre-D.5.2.
    assert a_map[mr.CHECK_PROJECTION_MISSING_INPUTS][0] == "pass"
    assert b_map[mr.CHECK_PROJECTION_MISSING_INPUTS][0] == "fail"
    assert b_map[mr.CHECK_PROJECTION_MISSING_INPUTS][1]  # rejected fighter tagged
    assert a_map[mr.CHECK_MISMATCH_ALERTS_WARN] != b_map[mr.CHECK_MISMATCH_ALERTS_WARN]

    # Checks independent of odds win probability are unaffected.
    untouched_codes = {
        mr.CHECK_PROJECTION_NON_PROJECTABLE,
        mr.CHECK_LATE_NEWS_RISK_LOCKED,
        mr.CHECK_FIGHTER_STATUS_REVIEW,
        mr.CHECK_LATE_NEWS_ACKNOWLEDGED,
        mr.CHECK_MANUAL_REVIEW_USER_ACK,
        mr.CHECK_SALARY_IMPORTED,
        mr.CHECK_FIGHT_GROUP_COVERAGE,
        mr.CHECK_FIGHT_GROUP_REVIEW,
        mr.CHECK_SCHEDULED_ROUNDS_REVIEWED,
    }
    for code in untouched_codes:
        assert a_map[code] == b_map[code], code


# ---------------------------------------------------------------------------
# Ordering + closed check set (§4 / §5)
# ---------------------------------------------------------------------------


def test_full_check_set_is_emitted_for_a_known_slate(conn, slate_id):
    _seed_clean_two_fighter_slate(conn, slate_id)
    readout = evaluate_manual_review(conn, slate_id)
    codes = {c.code for c in readout.checks}
    assert codes == mr.ALLOWED_CHECKS


def test_checks_are_in_deterministic_category_order(conn, slate_id):
    """Blocking → Warning → Informational, with intra-category ordering
    stable on multiple calls."""
    _seed_clean_two_fighter_slate(conn, slate_id)
    readout = evaluate_manual_review(conn, slate_id)

    categories = [c.category for c in readout.checks]
    if mr.CATEGORY_BLOCKING in categories:
        last_blocking = max(
            i
            for i, c in enumerate(categories)
            if c == mr.CATEGORY_BLOCKING
        )
        first_warning = min(
            (
                i
                for i, c in enumerate(categories)
                if c == mr.CATEGORY_WARNING
            ),
            default=len(categories),
        )
        assert last_blocking < first_warning
    if mr.CATEGORY_WARNING in categories:
        last_warning = max(
            i
            for i, c in enumerate(categories)
            if c == mr.CATEGORY_WARNING
        )
        first_info = min(
            (
                i
                for i, c in enumerate(categories)
                if c == mr.CATEGORY_INFORMATIONAL
            ),
            default=len(categories),
        )
        assert last_warning < first_info


def test_repeated_calls_return_identical_check_lists(conn, slate_id):
    _seed_clean_two_fighter_slate(conn, slate_id)
    first = evaluate_manual_review(conn, slate_id)
    second = evaluate_manual_review(conn, slate_id)
    assert first.checks == second.checks
    assert first.summary == second.summary


def test_summary_counts_match_category_split(conn, slate_id):
    _seed_clean_two_fighter_slate(conn, slate_id)
    readout = evaluate_manual_review(conn, slate_id)
    counted = {
        mr.CATEGORY_BLOCKING: 0,
        mr.CATEGORY_WARNING: 0,
        mr.CATEGORY_INFORMATIONAL: 0,
    }
    for c in readout.checks:
        counted[c.category] += 1
    assert readout.summary.blocking_count == counted[mr.CATEGORY_BLOCKING]
    assert readout.summary.warning_count == counted[mr.CATEGORY_WARNING]
    assert readout.summary.info_count == counted[mr.CATEGORY_INFORMATIONAL]
