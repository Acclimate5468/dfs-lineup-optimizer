"""Unit tests for the deterministic lineup reasoning generator.

Pins ``docs/TWO_STEP_BUILDER_PRODUCTION_DESIGN.md`` §8 (the only new pure
logic in the two-step builder port). Pure-only: every input is a
hand-built :class:`ReasoningContext`. The generator may cite only the
facts supplied, must never assert a fight outcome / finish / "lock" /
"safe favorite", and must be deterministic.
"""

from __future__ import annotations

import src.exports.lineup_reasoning as lr
from src.exports.lineup_reasoning import (
    ExcludedFighterNote,
    FighterReasoningInput,
    LineupReasoningInput,
    ReasoningContext,
    WarningNote,
    build_lineup_reasoning,
)


# ---------------------------------------------------------------------------
# Banned vocabulary (design §8.3 / §8.4 + task guardrails). Checked as
# case-insensitive substrings against every generated line.
# ---------------------------------------------------------------------------

BANNED_SUBSTRINGS = (
    "lock",
    "guarantee",
    "guaranteed",
    "safe",  # also catches "safest" / "safe favorite"
    "finish",
    "ko",
    "itd",
    "will win",
)


def _assert_no_banned(result: lr.LineupReasoningResult) -> None:
    for item in result.items:
        low = item.text.lower()
        for banned in BANNED_SUBSTRINGS:
            assert banned not in low, (
                f"banned substring {banned!r} found in reasoning: {item.text!r}"
            )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fighter(
    name: str,
    salary: int,
    projection: float,
    **kw,
) -> FighterReasoningInput:
    return FighterReasoningInput(
        name=name, salary=salary, projection=projection, **kw
    )


def _rich_context() -> ReasoningContext:
    fighters = (
        _fighter(
            "Pavlovich",
            9800,
            70.0,
            implied_win_probability=0.76,
            value_gap_bonus=0.0,
            five_round_bonus=7.0,
            scheduled_rounds=5,
            fight_group_id=1,
        ),
        _fighter(
            "Aoriqileng",
            6000,
            55.0,
            implied_win_probability=0.46,
            value_gap_bonus=8.0,
            five_round_bonus=0.0,
            scheduled_rounds=3,
            fight_group_id=2,
        ),
        _fighter(
            "Yadong Song",
            8800,
            60.0,
            implied_win_probability=0.60,
            value_gap_bonus=0.0,
            five_round_bonus=0.0,
            scheduled_rounds=3,
            fight_group_id=3,
        ),
    )
    lineup = LineupReasoningInput(
        lineup_index=1,
        fighters=fighters,
        total_salary=24600,
        total_projection=185.0,
    )
    return ReasoningContext(
        lineups=(lineup,),
        salary_cap=50000,
        roster_size=3,
        excluded=(
            ExcludedFighterNote("Jones", "no matched moneyline → non-projectable"),
        ),
        warnings=(
            WarningNote(
                "odds_coverage_partial",
                "1 active fighter has no usable odds.",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Lineup summary
# ---------------------------------------------------------------------------


def test_lineup_summary_cites_count_and_totals():
    result = build_lineup_reasoning(_rich_context())
    summaries = [i for i in result.items if i.kind == lr.KIND_LINEUP_SUMMARY]
    assert len(summaries) == 1
    text = summaries[0].text
    assert "Lineup 1" in text
    assert "3-fighter roster" in text  # roster_size supplied + matches count
    assert "$24,600" in text
    assert "185.0" in text


def test_lineup_summary_omits_totals_when_not_supplied():
    lineup = LineupReasoningInput(
        lineup_index=1,
        fighters=(_fighter("A", 5000, 40.0),),
    )
    result = build_lineup_reasoning(ReasoningContext(lineups=(lineup,)))
    summary = next(
        i for i in result.items if i.kind == lr.KIND_LINEUP_SUMMARY
    )
    assert "total salary" not in summary.text
    assert "projected points" not in summary.text
    assert "Lineup 1: 1 fighter(s)." == summary.text


# ---------------------------------------------------------------------------
# Implied probability cited only when supplied
# ---------------------------------------------------------------------------


def test_implied_probability_cited_when_supplied():
    result = build_lineup_reasoning(_rich_context())
    drivers = [
        i for i in result.items if i.kind == lr.KIND_PROJECTION_DRIVER
    ]
    text = " ".join(i.text for i in drivers)
    # Highest implied prob is Pavlovich at 76%.
    assert "implied win probability" in text
    assert "76%" in text
    assert "Pavlovich" in text


def test_implied_probability_absent_when_not_supplied():
    fighters = (
        _fighter("A", 9000, 60.0),
        _fighter("B", 8000, 50.0),
    )
    lineup = LineupReasoningInput(lineup_index=1, fighters=fighters)
    result = build_lineup_reasoning(ReasoningContext(lineups=(lineup,)))
    joined = " ".join(i.text for i in result.items).lower()
    assert "implied win probability" not in joined
    assert "%" not in joined
    # The top projection driver still renders, anchored on points only.
    drivers = [
        i for i in result.items if i.kind == lr.KIND_PROJECTION_DRIVER
    ]
    assert len(drivers) == 1
    assert "A is the top projection" in drivers[0].text


# ---------------------------------------------------------------------------
# Value-gap bonus cited only when supplied + positive
# ---------------------------------------------------------------------------


def test_value_driver_cited_when_bonus_supplied():
    result = build_lineup_reasoning(_rich_context())
    values = [i for i in result.items if i.kind == lr.KIND_VALUE_DRIVER]
    # Only Aoriqileng carries a positive value-gap bonus.
    assert len(values) == 1
    assert values[0].fighter_names == ("Aoriqileng",)
    assert "+8" in values[0].text
    assert "$6,000" in values[0].text


def test_value_driver_absent_when_bonus_zero_or_missing():
    fighters = (
        _fighter("A", 9000, 60.0, value_gap_bonus=0.0),
        _fighter("B", 8000, 50.0),  # value_gap_bonus None
    )
    lineup = LineupReasoningInput(lineup_index=1, fighters=fighters)
    result = build_lineup_reasoning(ReasoningContext(lineups=(lineup,)))
    assert not [i for i in result.items if i.kind == lr.KIND_VALUE_DRIVER]


# ---------------------------------------------------------------------------
# Five-round bonus cited only when supplied + positive
# ---------------------------------------------------------------------------


def test_five_round_cited_only_when_bonus_supplied():
    result = build_lineup_reasoning(_rich_context())
    five = [i for i in result.items if i.kind == lr.KIND_FIVE_ROUND_CONTEXT]
    # Only Pavlovich carries the +7 five-round bonus.
    assert len(five) == 1
    assert five[0].fighter_names == ("Pavlovich",)
    assert "+7" in five[0].text
    assert "5 rounds" in five[0].text


def test_five_round_absent_when_bonus_zero_or_missing():
    fighters = (
        _fighter("A", 9000, 60.0, five_round_bonus=0.0, scheduled_rounds=3),
        _fighter("B", 8000, 50.0, scheduled_rounds=3),  # five_round_bonus None
    )
    lineup = LineupReasoningInput(lineup_index=1, fighters=fighters)
    result = build_lineup_reasoning(ReasoningContext(lineups=(lineup,)))
    assert not [
        i for i in result.items if i.kind == lr.KIND_FIVE_ROUND_CONTEXT
    ]


# ---------------------------------------------------------------------------
# Constraints cited only from supplied facts
# ---------------------------------------------------------------------------


def test_constraints_cited_from_supplied_facts():
    result = build_lineup_reasoning(_rich_context())
    constraints = [
        i for i in result.items if i.kind == lr.KIND_CONSTRAINT_CHECK
    ]
    text = " ".join(i.text for i in constraints)
    # All three fighters in distinct fights → same-fight-pair satisfied.
    assert "no-same-fight-pair constraint is satisfied" in text
    assert "3 distinct fights" in text
    # Under-cap stated from total_salary + salary_cap.
    assert "$24,600" in text
    assert "within the $50,000 salary cap" in text


def test_same_fight_pair_omitted_without_fight_group_ids():
    fighters = (
        _fighter("A", 9000, 60.0),  # no fight_group_id
        _fighter("B", 8000, 50.0, fight_group_id=2),
    )
    lineup = LineupReasoningInput(
        lineup_index=1, fighters=fighters, total_salary=17000
    )
    # salary_cap omitted too → neither constraint line should appear.
    result = build_lineup_reasoning(ReasoningContext(lineups=(lineup,)))
    constraints = [
        i for i in result.items if i.kind == lr.KIND_CONSTRAINT_CHECK
    ]
    assert constraints == []


def test_under_cap_omitted_without_cap():
    fighters = (
        _fighter("A", 9000, 60.0, fight_group_id=1),
        _fighter("B", 8000, 50.0, fight_group_id=2),
    )
    lineup = LineupReasoningInput(
        lineup_index=1, fighters=fighters, total_salary=17000
    )
    result = build_lineup_reasoning(ReasoningContext(lineups=(lineup,)))
    text = " ".join(
        i.text for i in result.items if i.kind == lr.KIND_CONSTRAINT_CHECK
    )
    # Same-fight-pair is present (ids supplied) but cap line is not.
    assert "no-same-fight-pair" in text
    assert "salary cap" not in text


# ---------------------------------------------------------------------------
# Exclusions / warnings included only when supplied
# ---------------------------------------------------------------------------


def test_exclusions_and_warnings_included_when_supplied():
    result = build_lineup_reasoning(_rich_context())
    notes = [
        i for i in result.items if i.kind == lr.KIND_EXCLUSION_OR_WARNING
    ]
    text = " ".join(i.text for i in notes)
    assert "Jones" in text
    assert "non-projectable" in text
    assert "odds_coverage_partial" in text


def test_no_exclusions_or_warnings_when_absent():
    lineup = LineupReasoningInput(
        lineup_index=1, fighters=(_fighter("A", 5000, 40.0),)
    )
    result = build_lineup_reasoning(ReasoningContext(lineups=(lineup,)))
    assert not [
        i for i in result.items if i.kind == lr.KIND_EXCLUSION_OR_WARNING
    ]


# ---------------------------------------------------------------------------
# Banned vocabulary — never emitted (design §8.3 / §8.4)
# ---------------------------------------------------------------------------


def test_no_banned_words_in_rich_context():
    # The rich fixture exercises implied prob, value, five-round,
    # constraints, exclusions and warnings — none may produce a banned
    # claim. (There is no prop field on the input model, so a finish /
    # ITD claim is structurally impossible — design §8.3.)
    _assert_no_banned(build_lineup_reasoning(_rich_context()))


def test_no_banned_words_in_empty_result():
    _assert_no_banned(build_lineup_reasoning(ReasoningContext()))


def test_only_allowed_kinds_are_emitted():
    result = build_lineup_reasoning(_rich_context())
    for item in result.items:
        assert item.kind in lr.ALLOWED_REASONING_KINDS


# ---------------------------------------------------------------------------
# Missing optional data is handled honestly (no crash, no invented facts)
# ---------------------------------------------------------------------------


def test_minimal_fighter_only_required_fields():
    lineup = LineupReasoningInput(
        lineup_index=1, fighters=(_fighter("Solo", 5000, 42.0),)
    )
    result = build_lineup_reasoning(ReasoningContext(lineups=(lineup,)))
    kinds = {i.kind for i in result.items}
    # Only the summary + the points-anchored projection driver — no
    # probability, value, five-round, or constraint claims.
    assert kinds == {lr.KIND_LINEUP_SUMMARY, lr.KIND_PROJECTION_DRIVER}
    _assert_no_banned(result)


def test_empty_lineups_yields_diagnostics_only_no_crash():
    ctx = ReasoningContext(
        lineups=(),
        excluded=(ExcludedFighterNote("X", "non_projectable"),),
        warnings=(
            WarningNote(
                "scheduled_rounds_reviewed",
                "Confirm scheduled rounds for every fight.",
            ),
        ),
    )
    result = build_lineup_reasoning(ctx)
    assert result.items  # something is emitted
    summary = result.items[0]
    assert summary.kind == lr.KIND_LINEUP_SUMMARY
    assert "No lineups were generated" in summary.text
    # Exclusions / warnings still surface so nothing is silently hidden.
    notes = [
        i for i in result.items if i.kind == lr.KIND_EXCLUSION_OR_WARNING
    ]
    assert len(notes) == 2
    _assert_no_banned(result)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic_output_for_same_input():
    a = build_lineup_reasoning(_rich_context())
    b = build_lineup_reasoning(_rich_context())
    assert a == b
    assert [i.text for i in a.items] == [i.text for i in b.items]


def test_multiple_lineups_grouped_in_order():
    l1 = LineupReasoningInput(
        lineup_index=1, fighters=(_fighter("A", 9000, 60.0),)
    )
    l2 = LineupReasoningInput(
        lineup_index=2, fighters=(_fighter("B", 8000, 50.0),)
    )
    result = build_lineup_reasoning(ReasoningContext(lineups=(l1, l2)))
    summaries = [
        i for i in result.items if i.kind == lr.KIND_LINEUP_SUMMARY
    ]
    assert [s.lineup_index for s in summaries] == [1, 2]


# ---------------------------------------------------------------------------
# Purity invariant (mirrors tests/test_home_dashboard.py)
# ---------------------------------------------------------------------------


def test_module_has_no_streamlit_or_db_import():
    with open(lr.__file__, "r", encoding="utf-8") as fh:
        text = fh.read()
    forbidden = [
        "import streamlit",
        "from streamlit",
        "import sqlite3",
        "from sqlite3",
        "from src.db",
        "import src.db",
        "import requests",
    ]
    for needle in forbidden:
        assert needle not in text, f"lineup_reasoning.py must not contain {needle!r}"
