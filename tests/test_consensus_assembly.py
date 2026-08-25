"""Tests for the consensus pairing/merge adapter (Slice 5, pure).

``merge_sources`` folds the two parsers' per-fighter rows into merged per-fighter
book lines (paste-wins on collisions); ``assemble_fights`` pairs them into the
two-sided ``FightBookOdds`` the consensus service consumes.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.ingestion.consensus_assembly import (
    SOURCE_BESTFIGHTODDS,
    SOURCE_PASTE,
    assemble_fights,
    merge_sources,
)


def _row(fighter_name, opponent=None, **books):
    """A parser-row double: ``books`` is ``book=american_moneyline`` kwargs."""
    return SimpleNamespace(
        fighter_name=fighter_name,
        opponent=opponent,
        book_lines=[
            SimpleNamespace(book=b, american_moneyline=ml)
            for b, ml in books.items()
        ],
    )


# --- merge_sources ------------------------------------------------------


def test_merge_single_source_collects_all_books():
    merged = merge_sources(
        bestfightodds_rows=[_row("Jon Jones", "Stipe Miocic",
                                 DraftKings=-250, FanDuel=-240)]
    )
    assert len(merged) == 1
    f = merged[0]
    assert f.fighter_name == "Jon Jones"
    assert f.opponent == "Stipe Miocic"
    assert {e.book: e.american_odds for e in f.lines} == {
        "DraftKings": -250,
        "FanDuel": -240,
    }
    assert all(e.source == SOURCE_BESTFIGHTODDS for e in f.lines)


def test_merge_paste_wins_on_book_collision():
    merged = merge_sources(
        bestfightodds_rows=[_row("Jon Jones", "Stipe Miocic", DraftKings=-250)],
        paste_rows=[_row("Jon Jones", "Stipe Miocic", DraftKings=-260)],
    )
    assert len(merged) == 1
    by_book = {e.book: e for e in merged[0].lines}
    assert by_book["DraftKings"].american_odds == -260
    assert by_book["DraftKings"].source == SOURCE_PASTE


def test_merge_unions_distinct_books_across_sources():
    merged = merge_sources(
        bestfightodds_rows=[_row("Jon Jones", "Stipe", DraftKings=-250)],
        paste_rows=[_row("Jon Jones", "Stipe", Polymarket=-300)],
    )
    by_book = {e.book: e for e in merged[0].lines}
    assert by_book["DraftKings"].source == SOURCE_BESTFIGHTODDS
    assert by_book["Polymarket"].source == SOURCE_PASTE


def test_merge_keys_by_normalized_name():
    merged = merge_sources(
        bestfightodds_rows=[_row("Jon Jones", "Stipe", DraftKings=-250)],
        paste_rows=[_row("jon  jones", "Stipe", FanDuel=-240)],
    )
    assert len(merged) == 1
    assert {e.book for e in merged[0].lines} == {"DraftKings", "FanDuel"}


def test_merge_skips_blank_fighter_and_blank_book():
    merged = merge_sources(
        bestfightodds_rows=[
            _row("   ", "X", DraftKings=-250),
            _row("Real Fighter", "X", **{"": -200, "FanDuel": -150}),
        ]
    )
    assert [f.fighter_name for f in merged] == ["Real Fighter"]
    assert {e.book for e in merged[0].lines} == {"FanDuel"}


def test_merge_backfills_missing_opponent_from_second_source():
    # First source knew the books but not the opponent; the second supplies it.
    merged = merge_sources(
        bestfightodds_rows=[_row("Jon Jones", None, DraftKings=-250)],
        paste_rows=[_row("Jon Jones", "Stipe Miocic", FanDuel=-240)],
    )
    assert len(merged) == 1
    assert merged[0].opponent == "Stipe Miocic"
    assert {e.book for e in merged[0].lines} == {"DraftKings", "FanDuel"}


def test_merge_does_not_overwrite_present_opponent_with_none():
    merged = merge_sources(
        bestfightodds_rows=[_row("Jon Jones", "Stipe Miocic", DraftKings=-250)],
        paste_rows=[_row("Jon Jones", None, FanDuel=-240)],
    )
    assert merged[0].opponent == "Stipe Miocic"


# --- assemble_fights ----------------------------------------------------


def test_assemble_pairs_by_opponent():
    merged = merge_sources(
        bestfightodds_rows=[
            _row("Alice", "Bob", DraftKings=-150, FanDuel=-160),
            _row("Bob", "Alice", DraftKings=+130, FanDuel=+140),
        ]
    )
    result = assemble_fights(merged)
    assert result.unpaired == []
    assert len(result.fights) == 1
    fight = result.fights[0]
    assert (fight.fighter_a, fight.fighter_b) == ("Alice", "Bob")
    quotes = {q.book: (q.american_a, q.american_b) for q in fight.quotes}
    assert quotes == {"DraftKings": (-150, +130), "FanDuel": (-160, +140)}


def test_assemble_one_sided_book_becomes_none_on_missing_side():
    merged = merge_sources(
        bestfightodds_rows=[
            _row("Alice", "Bob", DraftKings=-150, FanDuel=-160),
            _row("Bob", "Alice", DraftKings=+130),  # no FanDuel for Bob
        ]
    )
    fight = assemble_fights(merged).fights[0]
    quotes = {q.book: (q.american_a, q.american_b) for q in fight.quotes}
    assert quotes["FanDuel"] == (-160, None)


def test_assemble_reports_unpaired_when_no_partner():
    merged = merge_sources(
        bestfightodds_rows=[_row("Lonely", "Ghost", DraftKings=-150)]
    )
    result = assemble_fights(merged)
    assert result.fights == []
    assert result.unpaired == ["Lonely"]


def test_assemble_reports_unpaired_when_opponent_missing():
    merged = merge_sources(
        bestfightodds_rows=[_row("NoOpp", None, DraftKings=-150)]
    )
    result = assemble_fights(merged)
    assert result.unpaired == ["NoOpp"]


def test_assemble_does_not_double_pair():
    merged = merge_sources(
        bestfightodds_rows=[
            _row("Alice", "Bob", DraftKings=-150),
            _row("Bob", "Alice", DraftKings=+130),
            _row("Carol", "Bob", DraftKings=-110),  # also claims Bob
        ]
    )
    result = assemble_fights(merged)
    assert len(result.fights) == 1
    assert result.unpaired == ["Carol"]


def test_assemble_merges_then_pairs_across_sources():
    merged = merge_sources(
        bestfightodds_rows=[
            _row("Alice", "Bob", DraftKings=-150),
            _row("Bob", "Alice", DraftKings=+130),
        ],
        paste_rows=[
            _row("Alice", "Bob", Polymarket=-170),
            _row("Bob", "Alice", Polymarket=+150),
        ],
    )
    fight = assemble_fights(merged).fights[0]
    books = {q.book for q in fight.quotes}
    assert books == {"DraftKings", "Polymarket"}


def test_assemble_self_referential_opponent_is_unpaired():
    # A fighter whose opponent normalizes to their own name must NOT build a
    # degenerate self-fight (the self-pair guard) — it routes to unpaired.
    merged = merge_sources(
        bestfightodds_rows=[_row("Jon Jones", "jon  jones", DraftKings=-150)]
    )
    result = assemble_fights(merged)
    assert result.fights == []
    assert result.unpaired == ["Jon Jones"]
