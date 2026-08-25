"""Tests for the pure DraftKings copied-board text parser (Phase 4).

Covers ``docs/ODDS_ACQUISITION_V0_DESIGN.md`` §3 Phase 4 (paste / table parser):
parse a representative copied DraftKings UFC board into normalized §1.7 rows,
ignore the Total Rounds (O/U) prices, support both totals+moneyline and
moneyline-only fight layouts, handle the unicode minus sign, pair opponents,
fail loudly on input with no fights / no moneylines, and perform no network I/O.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from src.ingestion.providers.draftkings_paste import (
    BOOK_DRAFTKINGS,
    SOURCE_DRAFTKINGS,
    DraftKingsParsedRow,
    DraftKingsPasteParseError,
    DraftKingsPasteParseResult,
    parse_draftkings_paste,
)

FIXTURE = Path(__file__).parent / "fixtures" / "draftkings_paste_sample.txt"


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# (fighter, moneyline, opponent) for every row the sample board must yield,
# in order (fighter A then fighter B, fights top to bottom).
EXPECTED = [
    ("Matt Schnell", 525, "Alessandro Costa"),
    ("Alessandro Costa", -750, "Matt Schnell"),
    ("Ketlen Souza", -270, "Ariane Carnelossi"),
    ("Ariane Carnelossi", 220, "Ketlen Souza"),
    ("Jeisla Chaves", -395, "Yuneisy Duben"),
    ("Yuneisy Duben", 310, "Jeisla Chaves"),
    ("Jordan Leavitt", 150, "Joanderson Brito"),
    ("Joanderson Brito", -180, "Jordan Leavitt"),
    ("Priscila Cachoeira", 105, "Chelsea Chandler"),
    ("Chelsea Chandler", -125, "Priscila Cachoeira"),
    ("Bruno Gustavo da Silva", -130, "Edgar Chairez"),
    ("Edgar Chairez", 110, "Bruno Gustavo da Silva"),
    ("Marcus McGhee", -500, "John Yannis"),
    ("John Yannis", 380, "Marcus McGhee"),
    ("Iwo Baraniewski", -310, "Junior Tafa"),
    ("Junior Tafa", 250, "Iwo Baraniewski"),
    ("Bryce Mitchell", -142, "Luan Santiago"),
    ("Luan Santiago", 120, "Bryce Mitchell"),
    ("Fares Ziam", -325, "Tom Nolan"),
    ("Tom Nolan", 260, "Fares Ziam"),
    ("Brendan Allen", -218, "Edmen Shahbazyan"),
    ("Edmen Shahbazyan", 180, "Brendan Allen"),
    ("Belal Muhammad", -125, "Gabriel Bonfim"),
    ("Gabriel Bonfim", 105, "Belal Muhammad"),
]


def test_parses_full_sample_into_24_rows() -> None:
    result = parse_draftkings_paste(_load_fixture())

    assert isinstance(result, DraftKingsPasteParseResult)
    assert len(result.rows) == 24
    assert result.warnings == []

    actual = [(r.fighter_name, r.american_moneyline, r.opponent) for r in result.rows]
    assert actual == EXPECTED


def test_row_carries_normalized_metadata() -> None:
    result = parse_draftkings_paste(
        _load_fixture(),
        source_url="https://sportsbook.draftkings.com/leagues/mma/ufc",
        collected_at="2026-06-01T12:00:00Z",
    )

    row = result.rows[0]
    assert isinstance(row, DraftKingsParsedRow)
    assert row.source == SOURCE_DRAFTKINGS == "DraftKings"
    assert row.book == BOOK_DRAFTKINGS == "DraftKings"
    assert row.status == "parsed"
    assert row.confidence == 1.0
    assert row.source_url == "https://sportsbook.draftkings.com/leagues/mma/ufc"
    assert row.collected_at == "2026-06-01T12:00:00Z"


def test_provenance_defaults_to_none() -> None:
    row = parse_draftkings_paste(_load_fixture()).rows[0]
    assert row.source_url is None
    assert row.collected_at is None


def test_handles_unicode_minus_sign() -> None:
    # The unicode minus (U+2212) actually appears in the fixture; confirm it
    # is read as a real negative moneyline, not dropped or misparsed.
    text = (
        "Conor McGregor\n"
        "vs\n"
        "Dustin Poirier\n"
        "−137\n"  # unicode minus
        "+115\n"
    )
    result = parse_draftkings_paste(text)
    assert [(r.fighter_name, r.american_moneyline) for r in result.rows] == [
        ("Conor McGregor", -137),
        ("Dustin Poirier", 115),
    ]
    # And the parsed sample contains plenty of unicode-minus favorites.
    sample = {r.fighter_name: r.american_moneyline for r in parse_draftkings_paste(_load_fixture()).rows}
    assert sample["Alessandro Costa"] == -750


def test_handles_ascii_minus_sign() -> None:
    text = "A Fighter\nvs\nB Fighter\n-137\n+115\n"
    result = parse_draftkings_paste(text)
    assert [(r.fighter_name, r.american_moneyline) for r in result.rows] == [
        ("A Fighter", -137),
        ("B Fighter", 115),
    ]


def test_ignores_total_rounds_over_under_prices() -> None:
    # The over/under totals prices (+120 / −154 etc.) must never surface as a
    # fighter moneyline. Spot-check the first fight: the only moneylines are
    # +525 and −750; the totals prices +120 / −154 must be absent.
    rows = parse_draftkings_paste(_load_fixture()).rows
    schnell = next(r for r in rows if r.fighter_name == "Matt Schnell")
    costa = next(r for r in rows if r.fighter_name == "Alessandro Costa")
    assert schnell.american_moneyline == 525
    assert costa.american_moneyline == -750
    # The totals prices for fight one were +120 and -154; ensure neither leaked
    # in as a moneyline anywhere in the slate.
    assert all(r.american_moneyline not in (120, -154) for r in rows
               if r.fighter_name in ("Matt Schnell", "Alessandro Costa"))


def test_handles_moneyline_only_fight_block() -> None:
    rows = parse_draftkings_paste(_load_fixture()).rows
    mitchell = next(r for r in rows if r.fighter_name == "Bryce Mitchell")
    santiago = next(r for r in rows if r.fighter_name == "Luan Santiago")
    assert mitchell.american_moneyline == -142
    assert mitchell.opponent == "Luan Santiago"
    assert santiago.american_moneyline == 120
    assert santiago.opponent == "Bryce Mitchell"


def test_assigns_opponents_symmetrically() -> None:
    rows = parse_draftkings_paste(_load_fixture()).rows
    by_name = {r.fighter_name: r for r in rows}
    for r in rows:
        assert r.opponent in by_name, f"opponent {r.opponent!r} not in slate"
        assert by_name[r.opponent].opponent == r.fighter_name


def test_incomplete_block_is_skipped_with_warning_when_others_parse() -> None:
    # First fight is moneyline-only and valid; second fight has only an 'O'
    # leg (no 'U'), so it is incomplete and must be skipped with a warning
    # rather than guessed at — while the valid fight still parses.
    text = (
        "Good A\n"
        "vs\n"
        "Good B\n"
        "-110\n"
        "+100\n"
        "Bad A\n"
        "vs\n"
        "Bad B\n"
        "O\n"
        "2.5\n"
        "-120\n"  # only one price after the lone 'O' marker
    )
    result = parse_draftkings_paste(text)
    assert [r.fighter_name for r in result.rows] == ["Good A", "Good B"]
    assert len(result.warnings) == 1
    assert "Bad A vs Bad B" in result.warnings[0]


def test_raises_on_empty_input() -> None:
    with pytest.raises(DraftKingsPasteParseError):
        parse_draftkings_paste("")
    with pytest.raises(DraftKingsPasteParseError):
        parse_draftkings_paste("   \n\n  \n")


def test_raises_when_no_fight_pairings() -> None:
    with pytest.raises(DraftKingsPasteParseError):
        parse_draftkings_paste("Total Rounds\nMoneyline\n+120\n-150\n")


def test_raises_when_every_block_incomplete() -> None:
    # A 'vs' pairing exists but no readable moneylines at all → loud failure.
    text = "Solo A\nvs\nSolo B\nO\n2.5\n-120\n"
    with pytest.raises(DraftKingsPasteParseError):
        parse_draftkings_paste(text)


def test_parser_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("parser must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    result = parse_draftkings_paste(_load_fixture())
    assert len(result.rows) == 24
