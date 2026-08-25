"""Tests for the pure multi-book paste-grid parser (consensus slice 4).

Covers ``docs/ODDS_CONSENSUS_DESIGN.md`` §5.2 (the multi-book paste parser):
parse a representative pasted odds-comparison grid into per-fighter all-book
lines, tolerate blank cells, a trailing non-odds column, and the unicode minus,
read every book column (with positional + disambiguated labels), pair opponents
best-effort, surface skipped line-less rows as warnings, fail loudly on input
with no grid / no books / no readable lines, and perform no network I/O. The
output shape mirrors the BFO all-books parser so both consensus sources feed the
later slices uniformly.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from src.ingestion.providers.multi_book_paste import (
    MIN_AMERICAN_MAGNITUDE,
    SOURCE_PASTE,
    STATUS_PARSED,
    MultiBookPasteParseError,
    MultiBookPasteParseResult,
    MultiBookPasteRow,
    PasteBookLine,
    parse_multi_book_paste,
)

FIXTURE = Path(__file__).parent / "fixtures" / "multi_book_paste_sample.txt"


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# Fighter order the sample grid must yield (3 bouts, top to bottom).
EXPECTED_FIGHTERS = [
    "Jon Jones",
    "Stipe Miocic",
    "Israel Adesanya",
    "Alex Pereira",
    "Sean O'Malley",
    "Merab Dvalishvili",
]

# Each fighter's full set of (book, american) lines the sample must yield. The
# trailing "Props" counts (14, 8, 22, ...) are sub-100 and must never appear;
# Stipe's Caesars cell is blank and must be absent for him only.
EXPECTED_LINES = {
    "Jon Jones": [
        ("DraftKings", -250),
        ("FanDuel", -245),
        ("BetMGM", -260),
        ("Caesars", -255),
    ],
    "Stipe Miocic": [
        ("DraftKings", 210),
        ("FanDuel", 205),
        ("BetMGM", 220),
    ],
    "Israel Adesanya": [
        ("DraftKings", -160),
        ("FanDuel", -155),
        ("BetMGM", -165),
        ("Caesars", -158),
    ],
    "Alex Pereira": [
        ("DraftKings", 140),
        ("FanDuel", 135),
        ("BetMGM", 145),
        ("Caesars", 138),
    ],
    "Sean O'Malley": [
        ("DraftKings", -120),
        ("FanDuel", -118),
        ("BetMGM", -122),
        ("Caesars", -119),
    ],
    "Merab Dvalishvili": [
        ("DraftKings", 100),
        ("FanDuel", 102),
        ("BetMGM", -101),
        ("Caesars", 101),
    ],
}


def _lines(row: MultiBookPasteRow) -> list[tuple[str, int]]:
    return [(bl.book, bl.american_moneyline) for bl in row.book_lines]


def test_parses_full_sample() -> None:
    result = parse_multi_book_paste(_load_fixture())

    assert isinstance(result, MultiBookPasteParseResult)
    assert result.warnings == []
    assert [r.fighter_name for r in result.rows] == EXPECTED_FIGHTERS

    for row in result.rows:
        assert _lines(row) == EXPECTED_LINES[row.fighter_name], row.fighter_name


def test_blank_cell_drops_only_that_book() -> None:
    # Stipe's Caesars cell is blank: he keeps his other three books and carries
    # no Caesars line, while everyone else has Caesars.
    by_name = {r.fighter_name: r for r in parse_multi_book_paste(_load_fixture()).rows}
    stipe_books = [bl.book for bl in by_name["Stipe Miocic"].book_lines]
    assert "Caesars" not in stipe_books
    assert len(stipe_books) == 3
    assert "Caesars" in [bl.book for bl in by_name["Jon Jones"].book_lines]


def test_trailing_non_odds_column_is_ignored() -> None:
    # The "Props" column (counts 8..22, all sub-100 magnitude) must never
    # surface as a phantom book on any fighter.
    rows = parse_multi_book_paste(_load_fixture()).rows
    all_books = {bl.book for row in rows for bl in row.book_lines}
    assert "Props" not in all_books
    assert all_books == {"DraftKings", "FanDuel", "BetMGM", "Caesars"}


def test_reads_unicode_and_ascii_minus() -> None:
    by_name = {r.fighter_name: r for r in parse_multi_book_paste(_load_fixture()).rows}
    jones = {bl.book: bl.american_moneyline for bl in by_name["Jon Jones"].book_lines}
    # DraftKings (−250) and BetMGM (−260) use the unicode minus; FanDuel (-245)
    # and Caesars (-255) use the ASCII hyphen-minus. All must read negative.
    assert jones == {
        "DraftKings": -250,
        "FanDuel": -245,
        "BetMGM": -260,
        "Caesars": -255,
    }


def test_pairs_opponents_symmetrically() -> None:
    rows = parse_multi_book_paste(_load_fixture()).rows
    by_name = {r.fighter_name: r for r in rows}
    for r in rows:
        assert r.opponent in by_name, f"opponent {r.opponent!r} not in slate"
        assert by_name[r.opponent].opponent == r.fighter_name
    assert by_name["Jon Jones"].opponent == "Stipe Miocic"
    assert by_name["Israel Adesanya"].opponent == "Alex Pereira"
    assert by_name["Sean O'Malley"].opponent == "Merab Dvalishvili"


def test_row_carries_normalized_metadata() -> None:
    result = parse_multi_book_paste(
        _load_fixture(),
        source_url="https://example.test/ufc-odds-grid",
        collected_at="2026-06-05T12:00:00Z",
    )
    row = result.rows[0]
    assert isinstance(row, MultiBookPasteRow)
    assert isinstance(row.book_lines[0], PasteBookLine)
    assert row.source == SOURCE_PASTE == "Paste"
    assert row.status == STATUS_PARSED == "parsed"
    assert row.source_url == "https://example.test/ufc-odds-grid"
    assert row.collected_at == "2026-06-05T12:00:00Z"


def test_provenance_defaults_to_none() -> None:
    row = parse_multi_book_paste(_load_fixture()).rows[0]
    assert row.source_url is None
    assert row.collected_at is None


def test_space_aligned_grid_without_tabs() -> None:
    # A paste with no tabs at all falls back to splitting on runs of 2+ spaces,
    # leaving single-space fighter names intact.
    text = (
        "Fighter   DraftKings   FanDuel\n"
        "Jon Jones   -250   -245\n"
        "Stipe Miocic   +210   +205\n"
    )
    result = parse_multi_book_paste(text)
    assert [r.fighter_name for r in result.rows] == ["Jon Jones", "Stipe Miocic"]
    by_name = {r.fighter_name: r for r in result.rows}
    assert _lines(by_name["Jon Jones"]) == [("DraftKings", -250), ("FanDuel", -245)]
    assert by_name["Jon Jones"].opponent == "Stipe Miocic"


def test_duplicate_book_labels_are_disambiguated() -> None:
    text = (
        "Fighter\tDraftKings\tDraftKings\n"
        "A Fighter\t-200\t-210\n"
        "B Fighter\t+170\t+180\n"
    )
    rows = parse_multi_book_paste(text).rows
    by_name = {r.fighter_name: r for r in rows}
    assert _lines(by_name["A Fighter"]) == [
        ("DraftKings", -200),
        ("DraftKings (col 2)", -210),
    ]


def test_blank_header_cell_gets_positional_label() -> None:
    text = (
        "Fighter\tDraftKings\t\tFanDuel\n"
        "A Fighter\t-200\t-210\t-150\n"
        "B Fighter\t+170\t+180\t+130\n"
    )
    rows = parse_multi_book_paste(text).rows
    by_name = {r.fighter_name: r for r in rows}
    assert _lines(by_name["A Fighter"]) == [
        ("DraftKings", -200),
        ("Book2", -210),
        ("FanDuel", -150),
    ]


def test_line_less_fighter_is_skipped_with_warning_when_others_parse() -> None:
    text = (
        "Fighter\tDraftKings\tFanDuel\n"
        "Good A\t-200\t-210\n"
        "Good B\t+170\t+180\n"
        "Blank Guy\t\t\n"
    )
    result = parse_multi_book_paste(text)
    assert [r.fighter_name for r in result.rows] == ["Good A", "Good B"]
    assert len(result.warnings) == 1
    assert "Blank Guy" in result.warnings[0]


def test_magnitude_floor_boundary() -> None:
    # ±100 is a real line (pick'em); ±99 is below the floor and is not.
    at_floor = parse_multi_book_paste(
        "Fighter\tBookX\nA Fighter\t+100\nB Fighter\t-100\n"
    )
    assert all(len(r.book_lines) == 1 for r in at_floor.rows)
    assert {r.fighter_name: r.book_lines[0].american_moneyline for r in at_floor.rows} == {
        "A Fighter": 100,
        "B Fighter": -100,
    }
    assert MIN_AMERICAN_MAGNITUDE == 100
    with pytest.raises(MultiBookPasteParseError):
        parse_multi_book_paste("Fighter\tBookX\nA Fighter\t+99\nB Fighter\t-99\n")


def test_unsigned_non_odds_column_is_never_a_line_even_above_floor() -> None:
    # The magnitude floor alone cannot tell a Props count of 120 from odds +120;
    # the explicit-sign requirement is what rejects an *unsigned* count of any
    # size, so a "Props" column with values >= 100 never becomes a phantom book.
    text = (
        "Fighter\tDraftKings\tProps\n"
        "A Fighter\t-200\t120\n"
        "B Fighter\t+170\t140\n"
    )
    rows = parse_multi_book_paste(text).rows
    all_books = {bl.book for row in rows for bl in row.book_lines}
    assert all_books == {"DraftKings"}
    assert "Props" not in all_books
    by_name = {r.fighter_name: r for r in rows}
    assert _lines(by_name["A Fighter"]) == [("DraftKings", -200)]
    assert _lines(by_name["B Fighter"]) == [("DraftKings", 170)]


def test_present_fighter_keeps_dropped_partner_as_opponent() -> None:
    # A line-less fighter (Bravo) is dropped from the output, but their present
    # partner (Alpha) keeps "Bravo" as `opponent` — a name reference for the
    # downstream matcher, mirroring the BFO all-books parser. This is intentional,
    # not an orphan bug: pairing on page order *before* the drop also keeps the
    # untouched bout (Charlie/Delta) correctly paired, which filtering-then-
    # pairing would break.
    text = (
        "Fighter\tDraftKings\tFanDuel\n"
        "Alpha\t-200\t-210\n"
        "Bravo\t\t\n"  # no readable line in any book -> dropped, with a warning
        "Charlie\t+150\t+160\n"
        "Delta\t-130\t-140\n"
    )
    result = parse_multi_book_paste(text)
    by_name = {r.fighter_name: r for r in result.rows}
    assert [r.fighter_name for r in result.rows] == ["Alpha", "Charlie", "Delta"]
    assert by_name["Alpha"].opponent == "Bravo"  # kept though Bravo was dropped
    assert "Bravo" not in by_name
    assert by_name["Charlie"].opponent == "Delta"  # untouched bout still correct
    assert by_name["Delta"].opponent == "Charlie"
    assert any("Bravo" in w for w in result.warnings)


def test_blank_fighter_name_row_is_skipped_with_warning() -> None:
    # A data row with a blank first column (a stray leading tab / separator) is
    # dropped, but warned — never silently — honoring the no-silent-truncation
    # contract. The surrounding fighters still parse and pair.
    text = (
        "Fighter\tDraftKings\tFanDuel\n"
        "Good A\t-200\t-210\n"
        "\t+100\t+110\n"  # blank fighter name in column 0
        "Good B\t+170\t+180\n"
    )
    result = parse_multi_book_paste(text)
    assert [r.fighter_name for r in result.rows] == ["Good A", "Good B"]
    assert any("fighter name" in w.lower() for w in result.warnings)


def test_odd_fighter_count_leaves_last_unpaired() -> None:
    text = (
        "Fighter\tDraftKings\n"
        "A Fighter\t-200\n"
        "B Fighter\t+170\n"
        "C Fighter\t-150\n"
    )
    by_name = {r.fighter_name: r for r in parse_multi_book_paste(text).rows}
    assert by_name["A Fighter"].opponent == "B Fighter"
    assert by_name["B Fighter"].opponent == "A Fighter"
    assert by_name["C Fighter"].opponent is None  # no adjacent partner


def test_single_fighter_has_no_opponent() -> None:
    result = parse_multi_book_paste("Fighter\tDraftKings\nSolo Fighter\t-200\n")
    assert len(result.rows) == 1
    assert result.rows[0].opponent is None
    assert _lines(result.rows[0]) == [("DraftKings", -200)]


def test_multiple_blank_cells_for_one_fighter() -> None:
    # A fighter missing lines in 2+ books keeps only the books that posted one,
    # with no phantom entries for the blank columns.
    text = (
        "Fighter\tDraftKings\tFanDuel\tBetMGM\tCaesars\n"
        "Patchy\t-200\t\t-205\t\n"  # only DraftKings + BetMGM present
        "Full\t+170\t+175\t+180\t+178\n"
    )
    by_name = {r.fighter_name: r for r in parse_multi_book_paste(text).rows}
    assert _lines(by_name["Patchy"]) == [("DraftKings", -200), ("BetMGM", -205)]
    assert len(by_name["Full"].book_lines) == 4


def test_all_blank_book_column_is_silently_dropped() -> None:
    # A book column blank for every fighter never appears as a phantom book, and
    # nothing is warned (no fighter row was skipped — only empty cells).
    text = (
        "Fighter\tDraftKings\tGhostBook\tFanDuel\n"
        "A Fighter\t-200\t\t-210\n"
        "B Fighter\t+170\t\t+180\n"
    )
    result = parse_multi_book_paste(text)
    all_books = {bl.book for row in result.rows for bl in row.book_lines}
    assert all_books == {"DraftKings", "FanDuel"}
    assert "GhostBook" not in all_books
    assert result.warnings == []


def test_ragged_grid_with_short_rows() -> None:
    # A data row truncated to fewer columns than the header reads the books it
    # does have and does not crash on the missing trailing columns.
    text = (
        "Fighter\tDraftKings\tFanDuel\tBetMGM\n"
        "A Fighter\t-200\t-210\n"  # missing BetMGM column entirely
        "B Fighter\t+170\t+180\t+185\n"
    )
    by_name = {r.fighter_name: r for r in parse_multi_book_paste(text).rows}
    assert _lines(by_name["A Fighter"]) == [("DraftKings", -200), ("FanDuel", -210)]
    assert len(by_name["B Fighter"].book_lines) == 3


def test_space_aligned_non_uniform_spacing() -> None:
    # The 2+-whitespace fallback tolerates irregular gaps between columns.
    text = (
        "Fighter  DraftKings    FanDuel\n"
        "Jon Jones   -250  -245\n"
        "Stipe Miocic    +210     +205\n"
    )
    by_name = {r.fighter_name: r for r in parse_multi_book_paste(text).rows}
    assert _lines(by_name["Jon Jones"]) == [("DraftKings", -250), ("FanDuel", -245)]
    assert _lines(by_name["Stipe Miocic"]) == [("DraftKings", 210), ("FanDuel", 205)]


def test_error_messages_are_specific() -> None:
    # The four loud-failure modes each carry a distinct, specific message.
    with pytest.raises(MultiBookPasteParseError, match="empty"):
        parse_multi_book_paste("")
    with pytest.raises(MultiBookPasteParseError, match="no book columns"):
        parse_multi_book_paste("Fighter\nJon Jones\n")
    with pytest.raises(MultiBookPasteParseError, match="no fighter rows"):
        parse_multi_book_paste("Fighter\tDraftKings\tFanDuel\n")
    with pytest.raises(MultiBookPasteParseError, match="no readable American lines"):
        parse_multi_book_paste("Fighter\tDraftKings\nA Fighter\t12\nB Fighter\t8\n")


def test_raises_on_empty_input() -> None:
    with pytest.raises(MultiBookPasteParseError):
        parse_multi_book_paste("")
    with pytest.raises(MultiBookPasteParseError):
        parse_multi_book_paste("   \n\n  \n")


def test_raises_when_header_has_no_book_columns() -> None:
    with pytest.raises(MultiBookPasteParseError):
        parse_multi_book_paste("Fighter\nJon Jones\nStipe Miocic\n")


def test_raises_when_no_fighter_rows() -> None:
    with pytest.raises(MultiBookPasteParseError):
        parse_multi_book_paste("Fighter\tDraftKings\tFanDuel\n")


def test_raises_when_no_readable_lines_anywhere() -> None:
    # Header books exist but every data cell is a sub-100 non-odds count.
    text = "Fighter\tDraftKings\tFanDuel\nA Fighter\t12\t8\nB Fighter\t2.5\t14\n"
    with pytest.raises(MultiBookPasteParseError):
        parse_multi_book_paste(text)


def test_parser_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("parser must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    result = parse_multi_book_paste(_load_fixture())
    assert len(result.rows) == len(EXPECTED_FIGHTERS)
