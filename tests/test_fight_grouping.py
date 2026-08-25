"""Tests for the pure DK Game Info grouping helper.

B3 of ``docs/DK_GAME_INFO_PAIRING_DESIGN.md`` (§3; test plan tests 7–10 plus
robustness). The helper is Streamlit-free and DB-free, so these tests drive it
with a lightweight ``_Fighter`` stand-in carrying only the four attributes the
helper consumes (``id``, ``name``, ``status``, ``game_info``). One seam test
confirms the real ``FighterRecord`` read surface (B2) flows through unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.slate.fight_grouping import group_fighters_by_game_info


@dataclass(frozen=True)
class _Fighter:
    """Minimal duck-typed roster entry (proves the helper is DB-free)."""

    id: int
    name: str
    status: str = "active"
    game_info: str | None = None


# --- test 7: 13 pairs from 26 fighters / 13 Game Info values ------------


def test_thirteen_pairs_from_twenty_six_fighters():
    fighters: list[_Fighter] = []
    expected: dict[str, set[str]] = {}
    fid = 1
    for b in range(13):
        gi = f"AliasA{b}@AliasB{b} 05/22/2026 {b % 12 + 1}:00PM ET"
        a_name = f"Alpha Fighter {b:02d}"
        b_name = f"Bravo Fighter {b:02d}"
        fighters.append(_Fighter(id=fid, name=a_name, game_info=gi))
        fid += 1
        fighters.append(_Fighter(id=fid, name=b_name, game_info=gi))
        fid += 1
        expected[gi] = {a_name, b_name}

    result = group_fighters_by_game_info(fighters)

    assert result.suggested_count == 13
    assert result.incomplete_count == 0
    assert result.anomaly_count == 0
    assert result.uncovered_count == 0
    # Each suggested pair carries the two canonical DK names that shared the
    # exact Game Info string.
    got = {
        p.game_info: {p.fighter_1_name, p.fighter_2_name}
        for p in result.suggested_pairs
    }
    assert got == expected


# --- test 8: odd group sizes skipped ------------------------------------


def test_one_fighter_is_incomplete_and_three_is_anomaly():
    solo_gi = "Solo@Ghost 05/22/2026 6:00PM ET"
    trio_gi = "Trio collision 05/22/2026 9:00PM ET"
    fighters = [
        _Fighter(id=1, name="Lonely Larry", game_info=solo_gi),
        _Fighter(id=2, name="Trio One", game_info=trio_gi),
        _Fighter(id=3, name="Trio Two", game_info=trio_gi),
        _Fighter(id=4, name="Trio Three", game_info=trio_gi),
    ]

    result = group_fighters_by_game_info(fighters)

    assert result.suggested_count == 0
    assert result.incomplete_count == 1
    assert result.anomaly_count == 1

    inc = result.incomplete[0]
    assert inc.fighter_name == "Lonely Larry"
    assert inc.game_info == solo_gi

    anom = result.anomalies[0]
    assert anom.game_info == trio_gi
    assert len(anom.fighter_names) == 3
    assert set(anom.fighter_names) == {"Trio One", "Trio Two", "Trio Three"}


# --- test 9: blank / NULL Game Info skipped -----------------------------


def test_blank_and_null_game_info_are_uncovered():
    fighters = [
        _Fighter(id=1, name="Null GI", game_info=None),
        _Fighter(id=2, name="Empty GI", game_info=""),
        _Fighter(id=3, name="Spaces GI", game_info="   "),
    ]

    result = group_fighters_by_game_info(fighters)

    assert result.suggested_count == 0
    assert result.incomplete_count == 0
    assert result.anomaly_count == 0
    assert result.uncovered_count == 3
    assert {u.name for u in result.uncovered} == {
        "Null GI",
        "Empty GI",
        "Spaces GI",
    }


# --- test 10: canonical names, deterministic order ----------------------


def test_pair_uses_canonical_names_not_game_info_aliases():
    # The @-aliases inside the string ("Shortname", "Otheralias") are never
    # parsed; the pair uses the DK Name values, ordered by normalized name.
    gi = "Shortname@Otheralias 05/22/2026 7:00PM ET"
    fighters = [
        _Fighter(id=1, name="Charlie Zeta", game_info=gi),
        _Fighter(id=2, name="Bravo Alpha", game_info=gi),
    ]

    result = group_fighters_by_game_info(fighters)

    assert result.suggested_count == 1
    pair = result.suggested_pairs[0]
    assert {pair.fighter_1_name, pair.fighter_2_name} == {
        "Charlie Zeta",
        "Bravo Alpha",
    }
    # normalized "bravo alpha" < "charlie zeta" → fighter_1 is Bravo Alpha.
    assert pair.fighter_1_name == "Bravo Alpha"
    assert pair.fighter_2_name == "Charlie Zeta"


def test_result_is_deterministic_regardless_of_input_order():
    gi1 = "A@B 05/22/2026 1:00PM ET"
    gi2 = "C@D 05/22/2026 2:00PM ET"
    base = [
        _Fighter(id=1, name="Zed", game_info=gi1),
        _Fighter(id=2, name="Amy", game_info=gi1),
        _Fighter(id=3, name="Mike", game_info=gi2),
        _Fighter(id=4, name="Bob", game_info=gi2),
    ]

    forward = group_fighters_by_game_info(base)
    reversed_ = group_fighters_by_game_info(list(reversed(base)))

    assert forward == reversed_
    # List order is stable and name-sorted, not input-dependent.
    assert [
        (p.fighter_1_name, p.fighter_2_name) for p in forward.suggested_pairs
    ] == [("Amy", "Zed"), ("Bob", "Mike")]


# --- robustness ---------------------------------------------------------


def test_only_active_status_is_considered():
    # A non-active fighter sharing the string is filtered out, so what would
    # be a 3-way anomaly resolves to a clean pair of the two active fighters
    # (§6.4: inactive/withdrawn fighters are excluded from grouping).
    gi = "A@B 05/22/2026 8:00PM ET"
    fighters = [
        _Fighter(id=1, name="Active One", status="active", game_info=gi),
        _Fighter(id=2, name="Active Two", status="active", game_info=gi),
        _Fighter(id=3, name="Excluded Three", status="excluded", game_info=gi),
    ]

    result = group_fighters_by_game_info(fighters)

    assert result.suggested_count == 1
    assert result.anomaly_count == 0
    pair = result.suggested_pairs[0]
    assert {pair.fighter_1_name, pair.fighter_2_name} == {
        "Active One",
        "Active Two",
    }


def test_inactive_fighter_leaves_opponent_incomplete():
    gi = "Champ@Challenger 05/22/2026 10:00PM ET"
    fighters = [
        _Fighter(id=1, name="Active Champ", status="active", game_info=gi),
        _Fighter(
            id=2, name="Scratched Challenger", status="inactive", game_info=gi
        ),
    ]

    result = group_fighters_by_game_info(fighters)

    assert result.suggested_count == 0
    assert result.incomplete_count == 1
    assert result.incomplete[0].fighter_name == "Active Champ"
    assert result.uncovered == ()


def test_near_identical_game_info_strings_do_not_merge():
    # Exact-string keys differ by a trailing space → two singletons, never a
    # mis-pair. The helper fails safe and never over-normalizes (§6.6, §6.9).
    fighters = [
        _Fighter(id=1, name="Fighter One", game_info="A@B 7:00PM ET"),
        _Fighter(id=2, name="Fighter Two", game_info="A@B 7:00PM ET "),
    ]

    result = group_fighters_by_game_info(fighters)

    assert result.suggested_count == 0
    assert result.incomplete_count == 2


def test_empty_roster_returns_empty_result():
    result = group_fighters_by_game_info([])
    assert result.suggested_count == 0
    assert result.incomplete == ()
    assert result.anomalies == ()
    assert result.uncovered == ()


def test_skip_reasons_are_exposed():
    fighters = [
        _Fighter(id=1, name="Solo", game_info="GI-solo"),
        _Fighter(id=2, name="T1", game_info="GI-trio"),
        _Fighter(id=3, name="T2", game_info="GI-trio"),
        _Fighter(id=4, name="T3", game_info="GI-trio"),
        _Fighter(id=5, name="Blank", game_info=None),
    ]

    result = group_fighters_by_game_info(fighters)

    assert "one active fighter" in result.incomplete[0].reason
    assert "3 active fighters" in result.anomalies[0].reason
    assert "expected exactly 2" in result.anomalies[0].reason
    assert result.uncovered[0].reason == "no Game Info captured"


def test_accepts_real_fighter_record_instances():
    """The B2 read surface (``FighterRecord``) satisfies the helper's
    structural contract without any DB access."""
    from src.db.repositories import FighterRecord

    gi = "A@B 05/22/2026 5:00PM ET"
    fighters = [
        FighterRecord(
            id=1, slate_id=7, name="Real One", salary=9000,
            status="active", game_info=gi,
        ),
        FighterRecord(
            id=2, slate_id=7, name="Real Two", salary=8000,
            status="active", game_info=gi,
        ),
    ]

    result = group_fighters_by_game_info(fighters)

    assert result.suggested_count == 1
    pair = result.suggested_pairs[0]
    assert {pair.fighter_1_name, pair.fighter_2_name} == {
        "Real One",
        "Real Two",
    }


# ---------------------------------------------------------------------------
# Main-event detection from Game Info start times (5-round auto-set)
# ---------------------------------------------------------------------------

from datetime import datetime  # noqa: E402

from src.slate.fight_grouping import (  # noqa: E402
    SuggestedPair,
    detect_main_event_pair,
    parse_game_info_start,
)


def _pair(game_info: str, a: str = "A", b: str = "B") -> SuggestedPair:
    return SuggestedPair(game_info=game_info, fighter_1_name=a, fighter_2_name=b)


def test_parse_game_info_start_reads_date_and_time():
    dt = parse_game_info_start("Costa@Schnell 06/06/2026 07:00PM ET")
    assert dt == datetime(2026, 6, 6, 19, 0)


def test_parse_game_info_start_none_when_no_timestamp():
    assert parse_game_info_start("Costa@Schnell") is None
    assert parse_game_info_start("") is None
    assert parse_game_info_start(None) is None


def test_detect_main_event_picks_latest_start():
    pairs = [
        _pair("Bonfim@Muhammad 06/06/2026 09:20PM ET", "Belal Muhammad", "Gabriel Bonfim"),
        _pair("Costa@Schnell 06/06/2026 07:00PM ET", "Alessandro Costa", "Matt Schnell"),
        _pair("Carnelossi@Souza 06/06/2026 05:00PM ET", "Ariane Carnelossi", "Ketlen Souza"),
    ]
    main = detect_main_event_pair(pairs)
    assert main is not None
    assert {main.fighter_1_name, main.fighter_2_name} == {
        "Belal Muhammad",
        "Gabriel Bonfim",
    }


def test_detect_main_event_none_for_single_pair():
    assert detect_main_event_pair([_pair("A@B 06/06/2026 09:00PM ET")]) is None


def test_detect_main_event_none_when_no_times():
    pairs = [_pair("Fight One"), _pair("Fight Two")]
    assert detect_main_event_pair(pairs) is None


def test_detect_main_event_none_on_tie_for_latest():
    pairs = [
        _pair("A@B 06/06/2026 09:00PM ET", "A1", "B1"),
        _pair("C@D 06/06/2026 09:00PM ET", "C1", "D1"),
        _pair("E@F 06/06/2026 07:00PM ET", "E1", "F1"),
    ]
    assert detect_main_event_pair(pairs) is None


def test_detect_main_event_ignores_untimed_pairs():
    pairs = [
        _pair("Headliner 06/06/2026 10:00PM ET", "Main A", "Main B"),
        _pair("Earlier 06/06/2026 06:00PM ET", "Early A", "Early B"),
        _pair("No time here", "Mystery A", "Mystery B"),
    ]
    main = detect_main_event_pair(pairs)
    assert main is not None
    assert {main.fighter_1_name, main.fighter_2_name} == {"Main A", "Main B"}
