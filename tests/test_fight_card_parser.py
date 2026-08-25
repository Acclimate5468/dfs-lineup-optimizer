"""Unit tests for the pasted fight-card parser + matcher (A4.2).

Realizes docs/FIGHT_GROUPS_UX_DESIGN.md §9.9 parser/matcher coverage
(tests 1–8) plus the self-pair / duplicate-across-rows eligibility gates from
§9.6. These are pure tests: no Streamlit, no database, no filesystem — the
roster is supplied as a plain list, mirroring how the Region D preview will
call the helper with ``[f.name for f in active_fighters]``.
"""

from __future__ import annotations

from pathlib import Path

from src.slate.fight_card_parser import (
    BAND_AMBIGUOUS,
    BAND_EXACT,
    BAND_HIGH_FUZZY,
    BAND_UNMATCHED,
    PARSE_ERROR,
    REASON_DUPLICATE,
    REASON_SELF_PAIR,
    ParsedBout,
    parse_fight_card,
    summarize,
)

ROSTER = ["Conor McGregor", "Khabib Nurmagomedov"]


def _one(line: str, roster: list[str]) -> ParsedBout:
    bouts = parse_fight_card(line, roster)
    assert len(bouts) == 1, f"expected one bout for {line!r}, got {len(bouts)}"
    return bouts[0]


# ---------------------------------------------------------------------------
# §9.9 test 1 — common separators parse to two raw names
# ---------------------------------------------------------------------------


def test_common_separators_split_into_two_names():
    roster = ["Alpha Fighter", "Bravo Fighter"]
    for line in (
        "Alpha Fighter vs Bravo Fighter",
        "Alpha Fighter vs. Bravo Fighter",
        "Alpha Fighter v Bravo Fighter",
        "Alpha Fighter v. Bravo Fighter",
        "Alpha Fighter versus Bravo Fighter",
        "Alpha Fighter - Bravo Fighter",
    ):
        bout = _one(line, roster)
        assert not bout.parse_error, line
        assert bout.side_1.raw == "Alpha Fighter", line
        assert bout.side_2.raw == "Bravo Fighter", line
        # Both sides exact-match the roster → eligible pair.
        assert bout.side_1.matched_name == "Alpha Fighter", line
        assert bout.side_2.matched_name == "Bravo Fighter", line
        assert bout.eligible, line


def test_separator_is_case_insensitive():
    roster = ["Alpha Fighter", "Bravo Fighter"]
    bout = _one("Alpha Fighter VS Bravo Fighter", roster)
    assert not bout.parse_error
    assert bout.side_1.raw == "Alpha Fighter"
    assert bout.side_2.raw == "Bravo Fighter"


# ---------------------------------------------------------------------------
# §9.9 test 2 — separator false-positives never split inside a name
# ---------------------------------------------------------------------------


def test_embedded_v_does_not_split_name():
    # "Vicente" / "Vera" lead with v; only the standalone "vs" must split.
    bout = _one("Vicente Luque vs Sean Brady", ["Vicente Luque", "Sean Brady"])
    assert not bout.parse_error
    assert bout.side_1.raw == "Vicente Luque"
    assert bout.side_2.raw == "Sean Brady"


def test_spaced_hyphen_splits_but_hyphenated_name_is_preserved():
    bout = _one(
        "Ji-Yeon Kim - Tabatha Ricci", ["Ji-Yeon Kim", "Tabatha Ricci"]
    )
    assert not bout.parse_error
    # The intra-name hyphen in "Ji-Yeon" survives; only the spaced hyphen split.
    assert bout.side_1.raw == "Ji-Yeon Kim"
    assert bout.side_2.raw == "Tabatha Ricci"


def test_word_separator_wins_over_spaced_hyphen():
    # A spaced hyphen on the right side is left intact when a word separator
    # is also present (§9.3 precedence).
    bout = _one("Marlon Vera vs Ji-Yeon Kim", ["Marlon Vera", "Ji-Yeon Kim"])
    assert not bout.parse_error
    assert bout.side_1.raw == "Marlon Vera"
    assert bout.side_2.raw == "Ji-Yeon Kim"


# ---------------------------------------------------------------------------
# §9.9 test 3 — blank lines ignored, line numbers sequential
# ---------------------------------------------------------------------------


def test_blank_lines_are_skipped():
    text = "\n".join(
        [
            "",
            "Conor McGregor vs Khabib Nurmagomedov",
            "   ",
            "",
            "Conor McGregor v Khabib Nurmagomedov",
            "",
        ]
    )
    bouts = parse_fight_card(text, ROSTER)
    assert len(bouts) == 2
    assert [b.line_number for b in bouts] == [1, 2]
    assert all(not b.parse_error for b in bouts)


# ---------------------------------------------------------------------------
# §9.9 test 3 — exact normalized match (case / whitespace / accent)
# ---------------------------------------------------------------------------


def test_case_and_whitespace_differences_resolve_exact():
    bout = _one("conor    mcgregor vs KHABIB NURMAGOMEDOV", ROSTER)
    assert bout.side_1.band == BAND_EXACT
    assert bout.side_1.matched_name == "Conor McGregor"
    assert bout.side_2.band == BAND_EXACT
    assert bout.side_2.matched_name == "Khabib Nurmagomedov"
    assert bout.pair_band == BAND_EXACT
    assert bout.eligible


def test_accent_differences_resolve_exact():
    bout = _one("José Aldo vs Conor McGregor", ["Jose Aldo", "Conor McGregor"])
    assert bout.side_1.band == BAND_EXACT
    assert bout.side_1.matched_name == "Jose Aldo"


# ---------------------------------------------------------------------------
# §9.9 test 4 — aggressive-fallback exact + aggressive collision ambiguous
# ---------------------------------------------------------------------------


def test_aggressive_fallback_resolves_exact():
    # "Dan" expands to "Daniel" only in the aggressive fold, not the
    # conservative one — so this exercises resolution step 2.
    bout = _one("Dan Ige vs Conor McGregor", ["Daniel Ige", "Conor McGregor"])
    assert bout.side_1.band == BAND_EXACT
    assert bout.side_1.matched_name == "Daniel Ige"


def test_aggressive_collision_resolves_ambiguous():
    # "Dan Ige Jr" aggressive-folds onto both roster fighters → ambiguous,
    # never exact.
    bout = _one("Dan Ige Jr vs Conor McGregor", ["Dan Ige", "Daniel Ige", "Conor McGregor"])
    assert bout.side_1.band == BAND_AMBIGUOUS
    assert bout.side_1.matched_name is None
    assert not bout.eligible
    assert bout.blocked_reason == "name 1 ambiguous"


# ---------------------------------------------------------------------------
# §9.9 test 5 — high-confidence fuzzy is eligible
# ---------------------------------------------------------------------------


def test_high_confidence_fuzzy_is_eligible():
    bout = _one("Conor McGreggor vs Khabib Nurmagomedov", ROSTER)
    assert bout.side_1.band == BAND_HIGH_FUZZY
    assert bout.side_1.matched_name == "Conor McGregor"
    assert bout.side_1.score is not None and bout.side_1.score >= 95
    assert bout.side_2.band == BAND_EXACT
    assert bout.pair_band == BAND_HIGH_FUZZY
    assert bout.eligible


# ---------------------------------------------------------------------------
# §9.9 test 6 — ambiguous is shown with a best guess but never eligible
# ---------------------------------------------------------------------------


def test_ambiguous_near_tie_is_shown_but_blocked():
    # Two near-identical surnames → a near-tie the matcher refuses to pick.
    roster = ["Marlon Vera", "Marlon Vega", "Conor McGregor"]
    bout = _one("Marlon Veta vs Conor McGregor", roster)
    assert bout.side_1.band == BAND_AMBIGUOUS
    assert bout.side_1.matched_name is None
    # Best guess is surfaced for display even though it is not selected.
    assert bout.side_1.best_candidate in {"Marlon Vera", "Marlon Vega"}
    assert not bout.eligible
    assert bout.blocked_reason == "name 1 ambiguous"


def test_ambiguous_when_two_candidates_tie():
    roster = ["Yair Rodriguez", "Daniel Rodriguez", "Conor McGregor"]
    bout = _one("Rodriguez vs Conor McGregor", roster)
    assert bout.side_1.band == BAND_AMBIGUOUS
    assert not bout.eligible


# ---------------------------------------------------------------------------
# §9.9 test 7 — unmatched is shown but never eligible
# ---------------------------------------------------------------------------


def test_unmatched_name_is_blocked():
    bout = _one("Totally Different Person vs Conor McGregor", ROSTER)
    assert bout.side_1.band == BAND_UNMATCHED
    assert bout.side_1.matched_name is None
    assert not bout.eligible
    assert bout.blocked_reason == "name 1 unmatched"


def test_empty_roster_makes_every_name_unmatched():
    bout = _one("Conor McGregor vs Khabib Nurmagomedov", [])
    assert bout.side_1.band == BAND_UNMATCHED
    assert bout.side_2.band == BAND_UNMATCHED
    assert not bout.eligible
    # name 1 is the first failing side.
    assert bout.blocked_reason == "name 1 unmatched"


# ---------------------------------------------------------------------------
# §9.9 test 8 — parse-error rows
# ---------------------------------------------------------------------------


def test_line_without_separator_is_parse_error():
    bout = _one("Just One Name", ROSTER)
    assert bout.parse_error
    assert bout.side_1 is None and bout.side_2 is None
    assert bout.pair_band == PARSE_ERROR
    assert not bout.eligible
    assert bout.blocked_reason == PARSE_ERROR
    # Raw line is preserved verbatim for orientation.
    assert bout.raw_line == "Just One Name"


def test_line_with_empty_side_is_parse_error():
    assert _one("Conor McGregor vs", ROSTER).parse_error
    assert _one("vs Conor McGregor", ROSTER).parse_error


def test_line_with_two_separators_is_parse_error():
    bout = _one("Conor McGregor vs Khabib vs Dustin", ROSTER)
    assert bout.parse_error
    assert bout.blocked_reason == PARSE_ERROR


def test_bare_hyphen_without_spaces_is_parse_error():
    # A hyphen without surrounding spaces is not a separator, so a lone
    # hyphenated token has no opponent and is a parse error.
    assert _one("Ji-Yeon Kim", ["Ji-Yeon Kim"]).parse_error


# ---------------------------------------------------------------------------
# §9.6 rule 3 — self-pair blocked
# ---------------------------------------------------------------------------


def test_self_pair_is_blocked():
    bout = _one("Conor McGregor vs conor mcgregor", ROSTER)
    assert bout.side_1.matched_name == "Conor McGregor"
    assert bout.side_2.matched_name == "Conor McGregor"
    assert not bout.eligible
    assert bout.blocked_reason == REASON_SELF_PAIR


# ---------------------------------------------------------------------------
# §9.6 rule 4 — duplicate fighter across pasted rows blocks all such rows
# ---------------------------------------------------------------------------


def test_duplicate_fighter_across_rows_blocks_all_rows():
    roster = ["Alpha Fighter", "Bravo Fighter", "Charlie Fighter"]
    text = "Alpha Fighter vs Bravo Fighter\nAlpha Fighter vs Charlie Fighter"
    bouts = parse_fight_card(text, roster)
    assert len(bouts) == 2
    assert all(not b.eligible for b in bouts)
    assert all(b.blocked_reason == REASON_DUPLICATE for b in bouts)


def test_distinct_rows_are_each_eligible():
    roster = ["Alpha Fighter", "Bravo Fighter", "Charlie Fighter", "Delta Fighter"]
    text = "Alpha Fighter vs Bravo Fighter\nCharlie Fighter vs Delta Fighter"
    bouts = parse_fight_card(text, roster)
    assert len(bouts) == 2
    assert all(b.eligible for b in bouts)
    assert all(b.blocked_reason is None for b in bouts)


def test_duplicate_only_counts_eligible_rows():
    # A name resolved in a blocked (unmatched-other-side) row does not make a
    # matching name in an eligible row a duplicate (§9.6 rule 4: "another
    # eligible pasted row").
    roster = ["Alpha Fighter", "Bravo Fighter"]
    text = "Alpha Fighter vs Nonexistent Person\nAlpha Fighter vs Bravo Fighter"
    bouts = parse_fight_card(text, roster)
    assert bouts[0].blocked_reason == "name 2 unmatched"  # blocked, not eligible
    assert bouts[1].eligible  # Alpha appears in only one eligible row


# ---------------------------------------------------------------------------
# summarize() counts (§9.5 summary line)
# ---------------------------------------------------------------------------


def test_summarize_buckets_are_mutually_exclusive_and_sum():
    roster = ["Alpha Fighter", "Bravo Fighter", "Charlie Fighter"]
    text = "\n".join(
        [
            "Alpha Fighter vs Bravo Fighter",          # eligible
            "Alpha Fighter vs Nonexistent Person",     # name 2 unmatched
            "Just One Name",                           # parse error
            "Charlie Fighter vs charlie fighter",      # self-pair
        ]
    )
    bouts = parse_fight_card(text, roster)
    summary = summarize(bouts)
    assert summary.total == 4
    assert summary.eligible == 1
    assert summary.blocked == 3
    assert summary.parse_errors == 1
    assert summary.unmatched == 1
    assert summary.self_pairs == 1
    assert summary.ambiguous == 0
    assert summary.duplicates == 0
    # Buckets partition the blocked rows.
    bucketed = (
        summary.parse_errors
        + summary.unmatched
        + summary.ambiguous
        + summary.self_pairs
        + summary.duplicates
    )
    assert bucketed == summary.blocked


def test_empty_text_returns_no_bouts():
    assert parse_fight_card("", ROSTER) == []
    assert parse_fight_card("\n   \n", ROSTER) == []
    assert summarize(parse_fight_card("", ROSTER)).total == 0


# ---------------------------------------------------------------------------
# Purity — no Streamlit / DB / filesystem dependency
# ---------------------------------------------------------------------------


def test_module_has_no_streamlit_or_db_imports():
    source = Path(
        __file__
    ).resolve().parent.parent.joinpath("src", "slate", "fight_card_parser.py").read_text()
    assert "streamlit" not in source
    assert "sqlite3" not in source
    assert "src.db" not in source


def test_parse_runs_without_any_database():
    # Smoke: the helper computes purely from its arguments.
    bouts = parse_fight_card("Conor McGregor vs Khabib Nurmagomedov", ROSTER)
    assert bouts[0].eligible
