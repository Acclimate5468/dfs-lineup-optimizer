"""Tests for pure helpers in ``src/ingestion/odds_match_filters.py``.

Phase D.3 helpers used by the Odds page Reject UI. Pure functions on
``OddsMatchResultRecord`` — no DB, no Streamlit.
"""

from __future__ import annotations

from src.ingestion.odds_match_filters import (
    assignable_match_results,
    format_assignable_label,
    format_rejectable_label,
    rejectable_match_results,
)
from src.ingestion.odds_matching_service import OddsMatchResultRecord


def _record(
    *,
    odds_row_id: int = 1,
    odds_row_key: str = "key-abc",
    match_status: str = "review_required",
    match_stage: str = "fuzzy",
    match_score: int = 92,
    preferred_candidate: str | None = None,
    fighter_id: int | None = None,
    opponent_check: str = "not_applicable",
    candidates: tuple = (),
    notes: tuple = (),
    effective_status: str | None = None,
) -> OddsMatchResultRecord:
    return OddsMatchResultRecord(
        slate_id=1,
        odds_row_id=odds_row_id,
        odds_row_key=odds_row_key,
        fighter_id=fighter_id,
        match_status=match_status,
        match_stage=match_stage,
        match_score=match_score,
        preferred_candidate=preferred_candidate,
        opponent_check=opponent_check,
        candidates=candidates,
        notes=notes,
        effective_status=(
            effective_status if effective_status is not None else match_status
        ),
    )


# ---------------------------------------------------------------------------
# rejectable_match_results
# ---------------------------------------------------------------------------


def test_rejectable_filters_only_review_required():
    records = [
        _record(odds_row_id=1, match_status="auto_match"),
        _record(odds_row_id=2, match_status="review_required"),
        _record(odds_row_id=3, match_status="unmatched"),
        _record(odds_row_id=4, match_status="review_required"),
    ]
    out = rejectable_match_results(records)
    assert [r.odds_row_id for r in out] == [2, 4]


def test_rejectable_preserves_input_order():
    records = [
        _record(odds_row_id=10, match_status="review_required"),
        _record(odds_row_id=5, match_status="review_required"),
        _record(odds_row_id=7, match_status="review_required"),
    ]
    out = rejectable_match_results(records)
    assert [r.odds_row_id for r in out] == [10, 5, 7]


def test_rejectable_empty_input_returns_empty_list():
    assert rejectable_match_results([]) == []


def test_rejectable_only_non_review_returns_empty_list():
    records = [
        _record(odds_row_id=1, match_status="auto_match"),
        _record(odds_row_id=2, match_status="unmatched"),
    ]
    assert rejectable_match_results(records) == []


def test_rejectable_returns_same_record_instances():
    """No copying — caller can still match on identity / mutate downstream."""
    rec = _record(odds_row_id=1, match_status="review_required")
    out = rejectable_match_results([rec])
    assert out[0] is rec


# ---------------------------------------------------------------------------
# format_rejectable_label
# ---------------------------------------------------------------------------


def test_label_short_key_not_truncated():
    rec = _record(
        odds_row_key="short",
        preferred_candidate="Jose Aldo",
        match_score=95,
    )
    assert format_rejectable_label(rec) == "short → Jose Aldo (score: 95)"


def test_label_key_at_prefix_length_not_truncated():
    """Boundary: a key whose length equals ``key_prefix_length`` is not
    truncated (no ellipsis)."""
    rec = _record(
        odds_row_key="a" * 16,
        preferred_candidate="Jose Aldo",
        match_score=92,
    )
    assert format_rejectable_label(rec) == (
        ("a" * 16) + " → Jose Aldo (score: 92)"
    )


def test_label_long_key_truncated_with_ellipsis():
    long_key = "a" * 32
    rec = _record(
        odds_row_key=long_key,
        preferred_candidate="Jose Aldo",
        match_score=92,
    )
    assert format_rejectable_label(rec) == (
        ("a" * 16) + "… → Jose Aldo (score: 92)"
    )


def test_label_missing_preferred_candidate_renders_as_empty_symbol():
    rec = _record(
        odds_row_key="abc",
        preferred_candidate=None,
        match_score=88,
    )
    assert format_rejectable_label(rec) == "abc → ∅ (score: 88)"


def test_label_custom_key_prefix_length_respected():
    rec = _record(
        odds_row_key="abcdefghijklmnopqrstuvwxyz",
        preferred_candidate="Jose Aldo",
        match_score=90,
    )
    assert format_rejectable_label(rec, key_prefix_length=5) == (
        "abcde… → Jose Aldo (score: 90)"
    )


# ---------------------------------------------------------------------------
# assignable_match_results (§16.10 — keyed on effective_status)
# ---------------------------------------------------------------------------


def test_assignable_filters_review_required_and_unmatched():
    records = [
        _record(odds_row_id=1, match_status="auto_match"),
        _record(odds_row_id=2, match_status="review_required"),
        _record(odds_row_id=3, match_status="unmatched"),
        _record(odds_row_id=4, match_status="auto_match"),
    ]
    out = assignable_match_results(records)
    assert [r.odds_row_id for r in out] == [2, 3]


def test_assignable_excludes_review_rejected_effective_status():
    """A row the matcher left ``review_required`` but an active reject flipped
    to ``review_rejected`` is NOT assignable — it must be un-rejected first."""
    records = [
        _record(
            odds_row_id=1,
            match_status="review_required",
            effective_status="review_rejected",
        ),
        _record(odds_row_id=2, match_status="unmatched"),
    ]
    out = assignable_match_results(records)
    assert [r.odds_row_id for r in out] == [2]


def test_assignable_excludes_already_bound_effective_statuses():
    """``review_accepted`` / ``force_pair`` rows already carry a binding."""
    records = [
        _record(
            odds_row_id=1,
            match_status="review_required",
            effective_status="review_accepted",
        ),
        _record(
            odds_row_id=2,
            match_status="unmatched",
            effective_status="force_pair",
        ),
    ]
    assert assignable_match_results(records) == []


def test_assignable_preserves_input_order():
    records = [
        _record(odds_row_id=9, match_status="unmatched"),
        _record(odds_row_id=3, match_status="review_required"),
        _record(odds_row_id=6, match_status="unmatched"),
    ]
    out = assignable_match_results(records)
    assert [r.odds_row_id for r in out] == [9, 3, 6]


def test_assignable_empty_input_returns_empty_list():
    assert assignable_match_results([]) == []


def test_assignable_returns_same_record_instances():
    rec = _record(odds_row_id=1, match_status="unmatched")
    out = assignable_match_results([rec])
    assert out[0] is rec


# ---------------------------------------------------------------------------
# format_assignable_label
# ---------------------------------------------------------------------------


def test_assignable_label_full_context():
    rec = _record(
        odds_row_key="key-xyz",
        match_status="review_required",
        match_score=90,
        preferred_candidate="Bruno Silva",
    )
    label = format_assignable_label(
        rec,
        odds_fighter_raw="Bruno Gustavo da Silva",
        opponent_raw="Joe Pyfer",
        american_odds=-150,
    )
    assert label == (
        "Bruno Gustavo da Silva vs Joe Pyfer @ -150 — review_required "
        "→ proposes Bruno Silva (score: 90)"
    )


def test_assignable_label_positive_moneyline_signed():
    rec = _record(match_status="unmatched", match_score=0)
    label = format_assignable_label(
        rec, odds_fighter_raw="Luan Santiago", american_odds=220
    )
    assert label == "Luan Santiago @ +220 — unmatched (score: 0)"


def test_assignable_label_falls_back_to_truncated_key():
    rec = _record(
        odds_row_key="a" * 32,
        match_status="unmatched",
        match_score=0,
    )
    label = format_assignable_label(rec)
    assert label == ("a" * 16) + "… — unmatched (score: 0)"


def test_assignable_label_omits_missing_optional_fields():
    rec = _record(
        odds_row_key="short-key",
        match_status="unmatched",
        match_score=0,
        preferred_candidate=None,
    )
    label = format_assignable_label(rec, odds_fighter_raw="Santiago Luna")
    assert label == "Santiago Luna — unmatched (score: 0)"
