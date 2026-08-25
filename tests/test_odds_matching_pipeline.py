"""Tests for the in-memory odds → DK fighter matching pipeline.

Covers the first slice of docs/ODDS_MATCHING_DESIGN.md:

- Stage tiering (conservative exact, aggressive exact, fuzzy).
- Score thresholds 95 / 88.
- Opponent context demoting auto → review but never promoting review → auto.
- Aggressive collision → review_required.
- No broad / unsafe nickname assumptions.
"""

import pytest

from src.ingestion.odds_matching import (
    OPPONENT_FAILED,
    OPPONENT_NOT_APPLICABLE,
    OPPONENT_PASSED,
    OPPONENT_UNKNOWN,
    STAGE_EXACT_AGGRESSIVE,
    STAGE_EXACT_CONSERVATIVE,
    STAGE_FUZZY,
    STAGE_NONE,
    STATUS_AUTO,
    STATUS_REVIEW,
    STATUS_UNMATCHED,
    OddsRowInput,
    OpponentContext,
    match_odds_to_dk,
)


def _single(dk_fighters, fighter, **kwargs):
    """Convenience helper — run the matcher on one odds row, return that result."""
    row = OddsRowInput(fighter=fighter, **kwargs)
    return match_odds_to_dk(dk_fighters, [row])[0]


# ---------------------------------------------------------------------------
# Conservative exact (design §2.3)
# ---------------------------------------------------------------------------

def test_conservative_exact_auto_match():
    r = _single(["Jose Aldo"], "Jose Aldo")
    assert r.status == STATUS_AUTO
    assert r.stage == STAGE_EXACT_CONSERVATIVE
    assert r.score == 100
    assert r.dk_fighter == "Jose Aldo"
    assert r.opponent_check == OPPONENT_NOT_APPLICABLE
    assert r.candidates == ()


def test_conservative_exact_is_case_and_accent_insensitive():
    """Conservative form (NFKD + lowercase + whitespace collapse) handles these."""
    r = _single(["José Aldo"], "JOSE  aldo")
    assert r.status == STATUS_AUTO
    assert r.stage == STAGE_EXACT_CONSERVATIVE
    assert r.dk_fighter == "José Aldo"


# ---------------------------------------------------------------------------
# Aggressive exact (design §2.4)
# ---------------------------------------------------------------------------

def test_aggressive_exact_jr_suffix_dropped():
    """`jose aldo jr` collapses to `jose aldo` only at the aggressive layer."""
    r = _single(["Jose Aldo"], "Jose Aldo Jr.")
    assert r.status == STATUS_AUTO
    assert r.stage == STAGE_EXACT_AGGRESSIVE
    assert r.score == 100
    assert r.dk_fighter == "Jose Aldo"


def test_aggressive_exact_curated_nickname_expansion():
    """Tom ↔ Thomas via curated table → exact at the aggressive layer."""
    r = _single(["Tom Almeida"], "Thomas Almeida")
    assert r.status == STATUS_AUTO
    assert r.stage == STAGE_EXACT_AGGRESSIVE
    assert r.score == 100
    assert r.dk_fighter == "Tom Almeida"


# ---------------------------------------------------------------------------
# Fuzzy tiers (design §3)
# ---------------------------------------------------------------------------

def test_fuzzy_auto_match_at_or_above_95():
    """jared cannonier / jared canonier — calibration §3.1 ≈ 96.6 → auto."""
    r = _single(["Jared Cannonier"], "Jared Canonier")
    assert r.status == STATUS_AUTO
    assert r.stage == STAGE_FUZZY
    assert r.score >= 95
    assert r.dk_fighter == "Jared Cannonier"


def test_fuzzy_review_required_in_88_to_94_band():
    """terrance/terrence mckinney — calibration §3.1 ≈ 94.1 → review."""
    r = _single(["Terrence McKinney"], "Terrance Mckinney")
    assert r.status == STATUS_REVIEW
    assert r.stage == STAGE_FUZZY
    assert 88 <= r.score < 95
    assert r.dk_fighter == "Terrence McKinney"


def test_fuzzy_below_88_is_unmatched():
    r = _single(["Conor McGregor"], "Totally Different Person")
    assert r.status == STATUS_UNMATCHED
    assert r.dk_fighter is None
    assert r.score < 88


# ---------------------------------------------------------------------------
# Opponent-context handling (design §4)
# ---------------------------------------------------------------------------

def test_opponent_mismatch_demotes_auto_to_review():
    """auto_match + confirmed opponent disagreement → review_required."""
    dk = ["Jose Aldo", "Marlon Vera"]
    row = OddsRowInput(fighter="Jose Aldo", opponent="Conor McGregor")
    opp = {"Jose Aldo": OpponentContext("Marlon Vera", confirmed=True)}
    [r] = match_odds_to_dk(dk, [row], opponents=opp)
    assert r.dk_fighter == "Jose Aldo"
    assert r.status == STATUS_REVIEW
    assert r.opponent_check == OPPONENT_FAILED
    assert "opponent_mismatch" in r.notes


def test_opponent_agreement_does_not_promote_review_to_auto():
    """review-band match + agreeing opponent stays review (v0 §4 lock)."""
    dk = ["Terrence McKinney", "Drew Dober"]
    row = OddsRowInput(fighter="Terrance Mckinney", opponent="Drew Dober")
    opp = {"Terrence McKinney": OpponentContext("Drew Dober", confirmed=True)}
    [r] = match_odds_to_dk(dk, [row], opponents=opp)
    assert r.dk_fighter == "Terrence McKinney"
    assert r.status == STATUS_REVIEW  # NOT auto_match
    assert r.opponent_check == OPPONENT_PASSED
    assert 88 <= r.score < 95
    assert "opponent_mismatch" not in r.notes


def test_unconfirmed_opponent_mismatch_does_not_demote():
    """auto_match with mismatched opponent on unconfirmed pairing stays auto."""
    dk = ["Jose Aldo", "Marlon Vera"]
    row = OddsRowInput(fighter="Jose Aldo", opponent="Conor McGregor")
    opp = {"Jose Aldo": OpponentContext("Marlon Vera", confirmed=False)}
    [r] = match_odds_to_dk(dk, [row], opponents=opp)
    assert r.status == STATUS_AUTO
    assert r.opponent_check == OPPONENT_FAILED
    assert "opponent_mismatch" not in r.notes


def test_no_expected_opponent_records_unknown():
    """When the slate has no expected pairing on file, no demotion happens."""
    dk = ["Jose Aldo"]
    row = OddsRowInput(fighter="Jose Aldo", opponent="Conor McGregor")
    [r] = match_odds_to_dk(dk, [row])
    assert r.status == STATUS_AUTO
    assert r.opponent_check == OPPONENT_UNKNOWN


# ---------------------------------------------------------------------------
# Ambiguity / duplicate handling (design §5.2)
# ---------------------------------------------------------------------------

def test_ambiguous_aggressive_candidates_require_review():
    """`Daniel Smith Jr.` collapses to `daniel smith` aggressive → both DK
    fighters match. No opponent context → review_required with both listed.
    """
    dk = ["Dan Smith", "Daniel Smith"]
    r = _single(dk, "Daniel Smith Jr.")
    assert r.status == STATUS_REVIEW
    assert r.dk_fighter is None
    assert set(r.candidates) == {"Dan Smith", "Daniel Smith"}
    assert r.stage == STAGE_EXACT_AGGRESSIVE
    assert "ambiguous_aggressive" in r.notes


def test_ambiguous_aggressive_opponent_context_does_not_promote_to_auto():
    """v0 lock (§4 / §5.2): even when exactly one ambiguous candidate's
    expected opponent matches the odds row's opponent column, the match
    stays ``review_required``. The preferred candidate is surfaced as
    supporting context so the human reviewer can one-click accept it.
    """
    dk = ["Dan Smith", "Daniel Smith"]
    row = OddsRowInput(fighter="Daniel Smith Jr.", opponent="Drew Dober")
    opp = {
        "Dan Smith": OpponentContext("Conor McGregor", confirmed=True),
        "Daniel Smith": OpponentContext("Drew Dober", confirmed=True),
    }
    [r] = match_odds_to_dk(dk, [row], opponents=opp)
    assert r.status == STATUS_REVIEW  # NOT auto_match
    assert r.dk_fighter is None
    assert r.preferred_candidate == "Daniel Smith"
    assert r.opponent_check == OPPONENT_PASSED
    assert set(r.candidates) == {"Dan Smith", "Daniel Smith"}
    assert "ambiguous_aggressive" in r.notes
    assert "opponent_supported_disambiguation" in r.notes


def test_ambiguous_aggressive_with_no_opponent_help_has_no_preferred_candidate():
    """No opponent column → review_required, no preferred candidate recorded."""
    dk = ["Dan Smith", "Daniel Smith"]
    r = _single(dk, "Daniel Smith Jr.")
    assert r.status == STATUS_REVIEW
    assert r.preferred_candidate is None
    assert r.opponent_check == OPPONENT_NOT_APPLICABLE
    assert "opponent_supported_disambiguation" not in r.notes


# ---------------------------------------------------------------------------
# Guards against broad / unsafe assumptions
# ---------------------------------------------------------------------------

def test_no_broad_nickname_assumption_jon_jonathan():
    """`jon` is not in the curated nickname table — pipeline must not silently
    auto-match it to `jonathan`. Calibration §3.1: WRatio ≈ 85.5 → unmatched.
    """
    r = _single(["Jonathan Jones"], "Jon Jones")
    assert r.status != STATUS_AUTO
    # Aggressive-exact would imply an unintended nickname expansion. Reject.
    assert r.stage != STAGE_EXACT_AGGRESSIVE


def test_no_broad_prefix_match_danny_daniel():
    """`danny` is not `dan` and not in the curated table — must not silently
    collapse to `daniel`.
    """
    r = _single(["Daniel Ige"], "Danny Ige")
    assert r.stage != STAGE_EXACT_AGGRESSIVE


def test_empty_fighter_string_is_unmatched_without_crash():
    r = _single(["Jose Aldo"], "")
    assert r.status == STATUS_UNMATCHED
    assert r.stage == STAGE_NONE
    assert "empty_fighter" in r.notes


def test_empty_dk_roster_yields_unmatched_results():
    rows = [OddsRowInput(fighter="Jose Aldo"), OddsRowInput(fighter="Conor McGregor")]
    results = match_odds_to_dk([], rows)
    assert [r.status for r in results] == [STATUS_UNMATCHED, STATUS_UNMATCHED]
    assert all(r.dk_fighter is None for r in results)


def test_preserves_input_order_and_row_id():
    dk = ["Jose Aldo", "Conor McGregor"]
    rows = [
        OddsRowInput(fighter="Conor McGregor", row_id="r1"),
        OddsRowInput(fighter="Jose Aldo", row_id="r2"),
    ]
    results = match_odds_to_dk(dk, rows)
    assert [r.row_id for r in results] == ["r1", "r2"]
    assert [r.dk_fighter for r in results] == ["Conor McGregor", "Jose Aldo"]
