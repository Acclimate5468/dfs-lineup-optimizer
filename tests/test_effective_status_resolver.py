"""Tests for the pure ``resolve_effective_status`` resolver (Phase D.4.1).

Scope mirrors ``docs/ODDS_PERSISTENCE_DESIGN.md`` §15.10's pure-function
plan plus the row-scoping checks called out in the D.4.1 task brief.
The resolver is pure: no DB fixture is needed — tests construct
``OddsMatchResultRecord`` and ``ManualMatchOverrideRecord`` objects in
memory.
"""

from __future__ import annotations

from src.db.repositories import ManualMatchOverrideRecord
from src.ingestion.effective_status_resolver import (
    ACCEPT_MATCH,
    AUTO_MATCH,
    FORCE_PAIR,
    MatchBinding,
    REJECT_MATCH,
    REVIEW_ACCEPTED,
    REVIEW_REJECTED,
    is_projection_eligible_effective_status,
    resolve_effective_status,
    resolve_match_binding,
)
from src.ingestion.odds_matching_service import OddsMatchResultRecord


def _result(
    *,
    slate_id: int = 1,
    odds_row_key: str = "row-A",
    fighter_id: int | None = 10,
    match_status: str = "auto_match",
) -> OddsMatchResultRecord:
    return OddsMatchResultRecord(
        slate_id=slate_id,
        odds_row_id=1,
        odds_row_key=odds_row_key,
        fighter_id=fighter_id,
        match_status=match_status,
        effective_status=match_status,
        match_stage="none",
        match_score=0,
        preferred_candidate=None,
        opponent_check="not_applicable",
        candidates=(),
        notes=(),
    )


def _override(
    *,
    id: int = 1,
    slate_id: int = 1,
    odds_row_key: str | None = "row-A",
    fighter_id: int | None = None,
    override_type: str = REJECT_MATCH,
    superseded_at: str | None = None,
) -> ManualMatchOverrideRecord:
    return ManualMatchOverrideRecord(
        id=id,
        slate_id=slate_id,
        odds_row_key=odds_row_key,
        fighter_id=fighter_id,
        override_type=override_type,
        payload_json=None,
        reason=None,
        created_at="2026-05-21T00:00:00",
        superseded_at=superseded_at,
    )


# ---------------------------------------------------------------------
# Rule 7 — no override → mirror match_status
# ---------------------------------------------------------------------

def test_no_overrides_mirrors_auto_match():
    r = _result(match_status="auto_match")
    assert resolve_effective_status(r, []) == "auto_match"


def test_no_overrides_mirrors_review_required():
    r = _result(match_status="review_required")
    assert resolve_effective_status(r, []) == "review_required"


def test_no_overrides_mirrors_unmatched():
    r = _result(match_status="unmatched", fighter_id=None)
    assert resolve_effective_status(r, []) == "unmatched"


# ---------------------------------------------------------------------
# Rule 2 — reject_match
# ---------------------------------------------------------------------

def test_reject_match_same_slate_and_row_returns_review_rejected():
    r = _result(slate_id=1, odds_row_key="row-A", match_status="review_required")
    ov = _override(slate_id=1, odds_row_key="row-A")
    assert resolve_effective_status(r, [ov]) == REVIEW_REJECTED


def test_reject_match_other_slate_does_not_apply():
    r = _result(slate_id=1, odds_row_key="row-A", match_status="review_required")
    ov = _override(slate_id=2, odds_row_key="row-A")
    assert resolve_effective_status(r, [ov]) == "review_required"


def test_reject_match_other_odds_row_key_does_not_apply():
    r = _result(slate_id=1, odds_row_key="row-A", match_status="review_required")
    ov = _override(slate_id=1, odds_row_key="row-B")
    assert resolve_effective_status(r, [ov]) == "review_required"


def test_reject_match_with_matching_fighter_id_applies():
    r = _result(fighter_id=10, match_status="review_required")
    ov = _override(fighter_id=10)
    assert resolve_effective_status(r, [ov]) == REVIEW_REJECTED


def test_reject_match_with_nonmatching_fighter_id_does_not_apply():
    r = _result(fighter_id=10, match_status="review_required")
    ov = _override(fighter_id=99)
    assert resolve_effective_status(r, [ov]) == "review_required"


def test_reject_match_fighter_id_none_is_row_scoped():
    r = _result(fighter_id=10, match_status="review_required")
    ov = _override(fighter_id=None)
    assert resolve_effective_status(r, [ov]) == REVIEW_REJECTED


def test_reject_match_applies_when_result_fighter_id_is_none():
    # §15.11.6: salary re-import can null fighter_id on the result row;
    # the override still applies on (slate_id, odds_row_key) alone.
    r = _result(fighter_id=None, match_status="review_required")
    ov = _override(fighter_id=10)
    assert resolve_effective_status(r, [ov]) == REVIEW_REJECTED


# ---------------------------------------------------------------------
# Defensive — superseded overrides
# ---------------------------------------------------------------------

def test_superseded_reject_match_does_not_apply():
    r = _result(match_status="review_required")
    ov = _override(superseded_at="2026-05-21T00:00:00")
    assert resolve_effective_status(r, [ov]) == "review_required"


# ---------------------------------------------------------------------
# Unsupported override types fall through to rule 7
# ---------------------------------------------------------------------

def test_unsupported_override_type_falls_through_to_match_status():
    r = _result(match_status="review_required")
    ov = _override(override_type="mark_excluded")
    assert resolve_effective_status(r, [ov]) == "review_required"


def test_multiple_overrides_mix_picks_reject_match():
    r = _result(match_status="review_required")
    ovs = [
        _override(id=1, override_type="mark_excluded"),
        _override(id=2, override_type=REJECT_MATCH),
    ]
    assert resolve_effective_status(r, ovs) == REVIEW_REJECTED


# =====================================================================
# D.5.1 — resolve_match_binding (effective_status + fighter_id)
# =====================================================================


def test_binding_no_override_mirrors_status_and_keeps_fighter_id():
    r = _result(match_status="auto_match", fighter_id=10)
    assert resolve_match_binding(r, []) == MatchBinding(
        effective_status="auto_match", fighter_id=10
    )


def test_binding_no_override_unmatched_keeps_none_fighter():
    r = _result(match_status="unmatched", fighter_id=None)
    assert resolve_match_binding(r, []) == MatchBinding("unmatched", None)


def test_binding_reject_match_keeps_matcher_fighter_id():
    r = _result(match_status="review_required", fighter_id=10)
    ov = _override(override_type=REJECT_MATCH, fighter_id=None)
    assert resolve_match_binding(r, [ov]) == MatchBinding(REVIEW_REJECTED, 10)


def test_binding_accept_match_yields_review_accepted_and_override_fighter():
    # An ambiguous review_required row (no result fighter_id); accepting it
    # binds the override's chosen fighter.
    r = _result(match_status="review_required", fighter_id=None)
    ov = _override(override_type=ACCEPT_MATCH, fighter_id=42)
    assert resolve_match_binding(r, [ov]) == MatchBinding(REVIEW_ACCEPTED, 42)


def test_binding_force_pair_yields_force_pair_and_override_fighter():
    r = _result(match_status="unmatched", fighter_id=None)
    ov = _override(override_type=FORCE_PAIR, fighter_id=7)
    assert resolve_match_binding(r, [ov]) == MatchBinding(FORCE_PAIR, 7)


def test_binding_force_pair_rebinds_even_when_result_has_other_fighter():
    # §16.3: binding overrides are never filtered on the result row's current
    # fighter_id — the override's fighter_id is the target. The matcher had
    # 10; the user force-pairs 99.
    r = _result(match_status="review_required", fighter_id=10)
    ov = _override(override_type=FORCE_PAIR, fighter_id=99)
    assert resolve_match_binding(r, [ov]) == MatchBinding(FORCE_PAIR, 99)


def test_binding_reject_precedence_over_leaked_force_pair():
    # §16.15: reject + a leaked active force_pair on one key → reject wins,
    # review_rejected with the matcher's fighter_id (here None).
    r = _result(match_status="unmatched", fighter_id=None)
    ovs = [
        _override(id=1, override_type=FORCE_PAIR, fighter_id=99),
        _override(id=2, override_type=REJECT_MATCH, fighter_id=None),
    ]
    assert resolve_match_binding(r, ovs) == MatchBinding(REVIEW_REJECTED, None)


def test_binding_force_pair_precedence_over_leaked_accept():
    r = _result(match_status="review_required", fighter_id=None)
    ovs = [
        _override(id=1, override_type=ACCEPT_MATCH, fighter_id=5),
        _override(id=2, override_type=FORCE_PAIR, fighter_id=8),
    ]
    assert resolve_match_binding(r, ovs) == MatchBinding(FORCE_PAIR, 8)


def test_binding_other_slate_force_pair_does_not_apply():
    r = _result(
        slate_id=1, odds_row_key="row-A", match_status="unmatched",
        fighter_id=None,
    )
    ov = _override(
        slate_id=2, odds_row_key="row-A", override_type=FORCE_PAIR,
        fighter_id=7,
    )
    assert resolve_match_binding(r, [ov]) == MatchBinding("unmatched", None)


def test_binding_superseded_force_pair_ignored():
    r = _result(match_status="unmatched", fighter_id=None)
    ov = _override(
        override_type=FORCE_PAIR, fighter_id=7,
        superseded_at="2026-05-21T00:00:00",
    )
    assert resolve_match_binding(r, [ov]) == MatchBinding("unmatched", None)


def test_resolve_effective_status_wrapper_returns_binding_string():
    r = _result(match_status="unmatched", fighter_id=None)
    ov = _override(override_type=FORCE_PAIR, fighter_id=7)
    assert resolve_effective_status(r, [ov]) == FORCE_PAIR
    assert resolve_effective_status(r, []) == "unmatched"


# =====================================================================
# D.5.2 — projection eligibility predicate (§16.9)
# =====================================================================


def test_projection_eligibility_predicate():
    # Eligible: auto_match + the two D.5 binding outputs.
    assert is_projection_eligible_effective_status(AUTO_MATCH)
    assert is_projection_eligible_effective_status(REVIEW_ACCEPTED)
    assert is_projection_eligible_effective_status(FORCE_PAIR)
    # Blocked: matcher review/unmatched and the reject output.
    assert not is_projection_eligible_effective_status("review_required")
    assert not is_projection_eligible_effective_status("unmatched")
    assert not is_projection_eligible_effective_status(REVIEW_REJECTED)
    # Unknown / not-yet-implemented statuses are blocked by default.
    assert not is_projection_eligible_effective_status("excluded")
    assert not is_projection_eligible_effective_status("shadowed")
    assert not is_projection_eligible_effective_status("totally_unknown")
