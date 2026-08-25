"""Tests for the pure BestFightOdds HTML parser (Odds Acquisition v0 Phase 1).

Covers ``docs/ODDS_ACQUISITION_V0_DESIGN.md`` §3 Phase 1: parse a representative
saved BestFightOdds table into normalized §1.7 rows, prefer the DraftKings
column (decision #3), ignore other books, validate American moneylines, fail
loudly on a missing DraftKings column / no rows / unrecognized structure, and
perform no network I/O.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from src.ingestion.providers.bestfightodds import (
    BOOK_DRAFTKINGS,
    SOURCE_BESTFIGHTODDS,
    AcquiredMoneylineRow,
    BestFightOddsParseError,
    parse_bestfightodds_all_books,
    parse_bestfightodds_html,
)

FIXTURE = Path(__file__).parent / "fixtures" / "bestfightodds_sample.html"
ALL_BOOKS_FIXTURE = (
    Path(__file__).parent / "fixtures" / "bestfightodds_allbooks_sample.html"
)
# Structure-faithful sanitized fixture for real-feed hardening (§10.7): props,
# rotation-number name prefixes, movement-arrow odds spans, promo-suffixed book
# labels, a server-empty DraftKings column, and a trailing props-count cell.
REAL_STRUCTURE_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "bestfightodds_allbooks_real_structure.html"
)


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _load_all_books_fixture() -> str:
    return ALL_BOOKS_FIXTURE.read_text(encoding="utf-8")


def _load_real_structure_fixture() -> str:
    return REAL_STRUCTURE_FIXTURE.read_text(encoding="utf-8")


def test_parses_fighter_names_and_dk_moneylines() -> None:
    rows = parse_bestfightodds_html(
        _load_fixture(),
        source_url="https://www.bestfightodds.com/events/test-event-1",
        fetched_at="2026-06-01T12:00:00Z",
    )

    # Only the two fighters with valid DraftKings lines come back; the second
    # fight's blank DraftKings cells are skipped.
    assert [r.fighter_name for r in rows] == [
        "Test Fighter One",
        "Test Fighter Two",
    ]
    assert [r.american_moneyline for r in rows] == [-350, 280]

    first = rows[0]
    assert first.source == SOURCE_BESTFIGHTODDS
    assert first.book == BOOK_DRAFTKINGS
    assert first.source_url == "https://www.bestfightodds.com/events/test-event-1"
    assert first.fetched_at == "2026-06-01T12:00:00Z"
    assert first.status == "parsed"
    assert first.confidence == 1.0
    assert first.opponent is None


def test_ignores_non_draftkings_book_columns() -> None:
    rows = parse_bestfightodds_html(_load_fixture())

    # The BetMGM column carries -330 / +270; the parser must return the
    # DraftKings values, never the other book's.
    moneylines = {r.fighter_name: r.american_moneyline for r in rows}
    assert moneylines == {
        "Test Fighter One": -350,
        "Test Fighter Two": 280,
    }
    assert -330 not in moneylines.values()
    assert 270 not in moneylines.values()


def test_blank_draftkings_cells_are_skipped_not_errored() -> None:
    rows = parse_bestfightodds_html(_load_fixture())
    names = {r.fighter_name for r in rows}
    # The second fight has blank DraftKings cells (BetMGM-only) and is skipped.
    assert "Test Fighter Three" not in names
    assert "Test Fighter Four" not in names


def test_american_moneylines_are_validated_integers() -> None:
    rows = parse_bestfightodds_html(_load_fixture())
    assert rows  # guard
    for row in rows:
        assert isinstance(row.american_moneyline, int)
        assert not isinstance(row.american_moneyline, bool)
        assert row.american_moneyline != 0
    # The leading '+' on the underdog line is normalized to a plain int.
    assert rows[1].american_moneyline == 280


def test_missing_draftkings_column_fails_loudly() -> None:
    html = """
    <table class="odds-table">
      <thead>
        <tr>
          <th>Event</th>
          <th><a title="BetMGM"><img alt="BetMGM" /></a></th>
          <th><a title="Caesars"><img alt="Caesars" /></a></th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><a href="/f/1">Only BetMGM Fighter</a></td>
          <td><span>-200</span></td>
          <td><span>-210</span></td>
        </tr>
      </tbody>
    </table>
    """
    with pytest.raises(BestFightOddsParseError) as exc:
        parse_bestfightodds_html(html)
    assert "DraftKings" in str(exc.value)


def test_draftkings_column_present_but_no_valid_rows_fails_loudly() -> None:
    # DraftKings header exists, but every DraftKings cell is blank -> no rows.
    html = """
    <table class="odds-table">
      <thead>
        <tr>
          <th>Event</th>
          <th><a title="DraftKings"><img alt="DraftKings" /></a></th>
          <th><a title="BetMGM"><img alt="BetMGM" /></a></th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><a href="/f/1">Blank DK Fighter</a></td>
          <td><span></span></td>
          <td><span>-150</span></td>
        </tr>
      </tbody>
    </table>
    """
    with pytest.raises(BestFightOddsParseError) as exc:
        parse_bestfightodds_html(html)
    assert "no valid" in str(exc.value).lower()


def test_unrecognized_structure_fails_loudly() -> None:
    html = "<html><body><p>No odds table here.</p></body></html>"
    with pytest.raises(BestFightOddsParseError) as exc:
        parse_bestfightodds_html(html)
    assert "not recognized" in str(exc.value).lower()


def test_empty_input_fails_loudly() -> None:
    with pytest.raises(BestFightOddsParseError):
        parse_bestfightodds_html("")


def test_parser_opens_no_network_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pure parser must not open any socket while parsing (design §1.9)."""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("parser attempted to open a network socket")

    monkeypatch.setattr(socket, "socket", _boom)
    rows = parse_bestfightodds_html(_load_fixture())
    assert len(rows) == 2


def test_normalized_row_defaults_match_design_contract() -> None:
    row = AcquiredMoneylineRow(fighter_name="X", american_moneyline=-120)
    assert row.source == "BestFightOdds"
    assert row.book == "DraftKings"
    assert row.status == "parsed"
    assert row.confidence == 1.0
    assert row.opponent is None
    assert row.source_url is None
    assert row.fetched_at is None


# ---------------------------------------------------------------------------
# All-books parser (ODDS_CONSENSUS_DESIGN.md §5.1) — additive consensus path
# ---------------------------------------------------------------------------


def _books_for(rows, fighter_name) -> dict[str, int]:
    """Collapse one fighter's all-book lines to {book: american_moneyline}."""
    [row] = [r for r in rows if r.fighter_name == fighter_name]
    return {line.book: line.american_moneyline for line in row.book_lines}


def test_all_books_reads_every_book_column() -> None:
    rows = parse_bestfightodds_all_books(_load_all_books_fixture())

    # Every fighter with at least one line is returned (4 here), in page order.
    assert [r.fighter_name for r in rows] == [
        "Alpha Fighter",
        "Bravo Fighter",
        "Charlie Fighter",
        "Delta Fighter",
    ]
    # Alpha has a valid line in all four columns; DraftKings is just one of them.
    assert _books_for(rows, "Alpha Fighter") == {
        "DraftKings": -200,
        "FanDuel": -210,
        "Caesars": -195,
        "Book4": -205,
    }


def test_all_books_labels_unlabeled_column_positionally() -> None:
    """A header column with no recognizable book name is labelled positionally
    (``Book4``) rather than dropped — open question #4's best-effort fallback."""
    rows = parse_bestfightodds_all_books(_load_all_books_fixture())
    books = _books_for(rows, "Delta Fighter")
    assert "Book4" in books
    assert books["Book4"] == 127


def test_all_books_includes_draftkings_as_a_book() -> None:
    """Unlike the single-source path, DraftKings is one labelled book among
    many here — present, not privileged."""
    rows = parse_bestfightodds_all_books(_load_all_books_fixture())
    assert _books_for(rows, "Alpha Fighter")["DraftKings"] == -200
    assert _books_for(rows, "Bravo Fighter")["DraftKings"] == 170
    assert _books_for(rows, "Delta Fighter")["DraftKings"] == 125


def test_all_books_skips_blank_cell_for_that_book_only() -> None:
    """Bravo's fourth-book cell is blank: that one book is skipped, but Bravo is
    still returned with their other three lines."""
    rows = parse_bestfightodds_all_books(_load_all_books_fixture())
    books = _books_for(rows, "Bravo Fighter")
    assert "Book4" not in books
    assert books == {"DraftKings": 170, "FanDuel": 175, "Caesars": 168}


def test_all_books_recovers_fighter_with_blank_draftkings() -> None:
    """Charlie's DraftKings cell is blank, so the single-source DraftKings path
    drops him entirely — but the all-books path recovers him from the other
    books. This is the exact reason the consensus path needs its own parser."""
    dk_only = parse_bestfightodds_html(_load_all_books_fixture())
    assert "Charlie Fighter" not in {r.fighter_name for r in dk_only}

    all_books = parse_bestfightodds_all_books(_load_all_books_fixture())
    books = _books_for(all_books, "Charlie Fighter")
    assert "DraftKings" not in books
    assert books == {"FanDuel": -145, "Caesars": -150, "Book4": -148}


def test_all_books_pairs_opponents_by_adjacency() -> None:
    """BFO renders a bout as two consecutive rows; the all-books parser fills a
    best-effort opponent from that adjacency."""
    rows = parse_bestfightodds_all_books(_load_all_books_fixture())
    opponents = {r.fighter_name: r.opponent for r in rows}
    assert opponents == {
        "Alpha Fighter": "Bravo Fighter",
        "Bravo Fighter": "Alpha Fighter",
        "Charlie Fighter": "Delta Fighter",
        "Delta Fighter": "Charlie Fighter",
    }


def test_all_books_moneylines_are_validated_integers() -> None:
    rows = parse_bestfightodds_all_books(_load_all_books_fixture())
    assert rows  # guard
    for row in rows:
        assert row.book_lines  # every emitted fighter has at least one line
        for line in row.book_lines:
            assert isinstance(line.american_moneyline, int)
            assert not isinstance(line.american_moneyline, bool)
            assert line.american_moneyline != 0
            assert isinstance(line.book, str) and line.book


def test_all_books_stamps_provenance_and_source() -> None:
    rows = parse_bestfightodds_all_books(
        _load_all_books_fixture(),
        source_url="https://www.bestfightodds.com/events/test-event-2",
        fetched_at="2026-06-05T12:00:00Z",
    )
    for row in rows:
        assert row.source == SOURCE_BESTFIGHTODDS
        assert row.source_url == "https://www.bestfightodds.com/events/test-event-2"
        assert row.fetched_at == "2026-06-05T12:00:00Z"
        assert row.status == "parsed"


def test_all_books_does_not_alter_the_draftkings_only_path() -> None:
    """The all-books parse is additive: the original DraftKings-only fixture
    still parses to exactly the two DraftKings rows it always did, while the
    all-books parse of the same HTML recovers all four fighters (two of which
    are BetMGM-only)."""
    dk_only = parse_bestfightodds_html(_load_fixture())
    assert [r.fighter_name for r in dk_only] == [
        "Test Fighter One",
        "Test Fighter Two",
    ]

    all_books = parse_bestfightodds_all_books(_load_fixture())
    assert [r.fighter_name for r in all_books] == [
        "Test Fighter One",
        "Test Fighter Two",
        "Test Fighter Three",
        "Test Fighter Four",
    ]
    # The non-DraftKings book column is now read, not ignored.
    assert _books_for(all_books, "Test Fighter Three") == {"BetMGM": -145}


def test_all_books_missing_draftkings_column_fails_loudly() -> None:
    """The all-books parser anchors on the DraftKings column to identify the
    odds grid, so a grid without one fails loudly (same as the DK-only path)."""
    html = """
    <table class="odds-table">
      <thead>
        <tr>
          <th>Event</th>
          <th><a title="BetMGM"><img alt="BetMGM" /></a></th>
          <th><a title="Caesars"><img alt="Caesars" /></a></th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><a href="/f/1">Only BetMGM Fighter</a></td>
          <td><span>-200</span></td>
          <td><span>-210</span></td>
        </tr>
      </tbody>
    </table>
    """
    with pytest.raises(BestFightOddsParseError) as exc:
        parse_bestfightodds_all_books(html)
    assert "DraftKings" in str(exc.value)


def test_all_books_present_but_no_valid_rows_fails_loudly() -> None:
    # DraftKings header exists, but every book cell is blank -> no fighter line.
    html = """
    <table class="odds-table">
      <thead>
        <tr>
          <th>Event</th>
          <th><a title="DraftKings"><img alt="DraftKings" /></a></th>
          <th><a title="BetMGM"><img alt="BetMGM" /></a></th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><a href="/f/1">Blank Fighter</a></td>
          <td><span></span></td>
          <td><span></span></td>
        </tr>
      </tbody>
    </table>
    """
    with pytest.raises(BestFightOddsParseError) as exc:
        parse_bestfightodds_all_books(html)
    assert "valid moneyline" in str(exc.value).lower()


def test_all_books_unrecognized_structure_fails_loudly() -> None:
    html = "<html><body><p>No odds table here.</p></body></html>"
    with pytest.raises(BestFightOddsParseError) as exc:
        parse_bestfightodds_all_books(html)
    assert "not recognized" in str(exc.value).lower()


def test_all_books_empty_input_fails_loudly() -> None:
    with pytest.raises(BestFightOddsParseError):
        parse_bestfightodds_all_books("")


def test_all_books_parser_opens_no_network_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The all-books parse must also stay pure — no socket while parsing."""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("parser attempted to open a network socket")

    monkeypatch.setattr(socket, "socket", _boom)
    rows = parse_bestfightodds_all_books(_load_all_books_fixture())
    assert len(rows) == 4


def test_all_books_no_book_columns_fails_loudly() -> None:
    """A grid whose only column is the fighter column (DraftKings header but no
    column to its right) exposes no books and fails loudly — distinct from the
    'no valid rows' path where book columns exist but every cell is blank."""
    html = """
    <table class="odds-table">
      <tr><th><a title="DraftKings"><img alt="DraftKings" /></a></th></tr>
      <tr><td>Lonely Fighter</td></tr>
    </table>
    """
    with pytest.raises(BestFightOddsParseError) as exc:
        parse_bestfightodds_all_books(html)
    assert "book columns" in str(exc.value).lower()


def test_all_books_fighter_with_no_lines_is_skipped_opponent_preserved() -> None:
    """A fighter blank across every book is not emitted, but their opponent —
    paired by page order before the line-less drop — still points at them."""
    html = """
    <table class="odds-table">
      <thead>
        <tr>
          <th>Event</th>
          <th><a title="DraftKings"><img alt="DraftKings" /></a></th>
          <th><a title="FanDuel"><img alt="FanDuel" /></a></th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><a href="/fighters/Has-Lines">Has Lines</a></td>
          <td><span>-160</span></td>
          <td><span>-165</span></td>
        </tr>
        <tr>
          <td><a href="/fighters/No-Lines-Anywhere">No Lines Anywhere</a></td>
          <td><span></span></td>
          <td><span></span></td>
        </tr>
      </tbody>
    </table>
    """
    rows = parse_bestfightodds_all_books(html)
    names = [r.fighter_name for r in rows]
    assert names == ["Has Lines"]  # the line-less fighter is dropped
    # ...but the emitted fighter keeps the correct (dropped) opponent.
    assert rows[0].opponent == "No Lines Anywhere"


def test_all_books_odd_number_of_fighters_leaves_last_opponent_none() -> None:
    """With an odd fighter count the trailing row has no adjacent partner, so
    its opponent is None while the leading pair is matched."""
    html = """
    <table class="odds-table">
      <thead>
        <tr>
          <th>Event</th>
          <th><a title="DraftKings"><img alt="DraftKings" /></a></th>
        </tr>
      </thead>
      <tbody>
        <tr><td><a href="/fighters/First">First</a></td><td><span>-150</span></td></tr>
        <tr><td><a href="/fighters/Second">Second</a></td><td><span>+130</span></td></tr>
        <tr><td><a href="/fighters/Third-Unpaired">Third Unpaired</a></td><td><span>-110</span></td></tr>
      </tbody>
    </table>
    """
    rows = parse_bestfightodds_all_books(html)
    opponents = {r.fighter_name: r.opponent for r in rows}
    assert opponents == {
        "First": "Second",
        "Second": "First",
        "Third Unpaired": None,
    }


def test_all_books_short_row_skips_missing_columns() -> None:
    """A data row with fewer cells than the header (a book column absent for
    that fighter) skips the missing book and still emits the fighter."""
    html = """
    <table class="odds-table">
      <thead>
        <tr>
          <th>Event</th>
          <th><a title="DraftKings"><img alt="DraftKings" /></a></th>
          <th><a title="FanDuel"><img alt="FanDuel" /></a></th>
        </tr>
      </thead>
      <tbody>
        <tr><td><a href="/fighters/Full-Row">Full Row</a></td><td><span>-150</span></td><td><span>-155</span></td></tr>
        <tr><td><a href="/fighters/Short-Row">Short Row</a></td><td><span>+130</span></td></tr>
      </tbody>
    </table>
    """
    rows = parse_bestfightodds_all_books(html)
    short = next(r for r in rows if r.fighter_name == "Short Row")
    # The FanDuel column is simply absent from this row; only DraftKings is read.
    assert {line.book for line in short.book_lines} == {"DraftKings"}
    assert short.book_lines[0].american_moneyline == 130


def test_all_books_duplicate_book_labels_are_disambiguated() -> None:
    """Two columns resolving to the same label (here a real 'DraftKings' column
    plus a second column whose header also reads 'DraftKings') are kept as
    distinct books — the second is suffixed by column index, never silently
    merged into one."""
    html = """
    <table class="odds-table">
      <thead>
        <tr>
          <th>Event</th>
          <th><a title="DraftKings"><img alt="DraftKings" /></a></th>
          <th><a title="DraftKings"><img alt="DraftKings" /></a></th>
        </tr>
      </thead>
      <tbody>
        <tr><td><a href="/fighters/Dupe-Fighter">Dupe Fighter</a></td><td><span>-150</span></td><td><span>-160</span></td></tr>
      </tbody>
    </table>
    """
    rows = parse_bestfightodds_all_books(html)
    books = _books_for(rows, "Dupe Fighter")
    # Both columns survive as separate keys; neither moneyline is lost.
    assert len(books) == 2
    assert books["DraftKings"] == -150
    assert books["DraftKings (col 2)"] == -160


# ---------------------------------------------------------------------------
# Real-feed hardening (ODDS_CONSENSUS_DESIGN §10.7)
#
# These pin the slice-7 failure modes against a structure-faithful fixture that
# reproduces the real BestFightOdds markup the synthetic fixture omitted: prop
# rows, rotation-number name prefixes, movement-arrow odds spans, a server-empty
# DraftKings column, and a trailing props-count cell. Each test would FAIL on the
# pre-hardening parser (which read every row as a fighter).
# ---------------------------------------------------------------------------

_REAL_FIGHTERS = ["Aiden Stone", "Brody Vance", "Cole Reyes", "Diego Marsh"]


def test_real_structure_row_count_bounded_to_fighter_rows() -> None:
    """Slice-7: 617 prop/total rows were emitted as fighters. Only the four
    head-to-head fighter rows (those with a /fighters/ link) come back."""
    rows = parse_bestfightodds_all_books(_load_real_structure_fixture())
    assert len(rows) == 4


def test_real_structure_excludes_prop_and_total_rows() -> None:
    """Round-total / method rows ('Under 1½ rounds', 'Aiden Stone wins by
    KO/TKO', 'Over 2½ rounds') and the matchup-header row are dropped, even
    though they carry odds cells of their own."""
    rows = parse_bestfightodds_all_books(_load_real_structure_fixture())
    names = [r.fighter_name for r in rows]
    for name in names:
        lowered = name.lower()
        for marker in ("round", "wins", "over", "under", "matchup", "draw"):
            assert marker not in lowered, f"prop row leaked as fighter: {name!r}"


def test_real_structure_strips_rotation_number_prefix() -> None:
    """Slice-7: names came back as '43417Belal Muhammad'. The leading
    rotation/pictureid number is stripped, leaving the clean fighter name."""
    rows = parse_bestfightodds_all_books(_load_real_structure_fixture())
    names = {r.fighter_name for r in rows}
    assert names == set(_REAL_FIGHTERS)
    for r in rows:
        assert not r.fighter_name[:1].isdigit(), r.fighter_name
        if r.opponent is not None:
            assert not r.opponent[:1].isdigit(), r.opponent


def test_real_structure_parses_expected_fighters_in_order() -> None:
    rows = parse_bestfightodds_all_books(_load_real_structure_fixture())
    assert [r.fighter_name for r in rows] == _REAL_FIGHTERS


def test_real_structure_pairs_opponents_after_filtering() -> None:
    """Opponent adjacency is correct because pairing happens AFTER the prop rows
    between fighters are filtered out (else a fighter pairs with a prop row)."""
    rows = parse_bestfightodds_all_books(_load_real_structure_fixture())
    assert {r.fighter_name: r.opponent for r in rows} == {
        "Aiden Stone": "Brody Vance",
        "Brody Vance": "Aiden Stone",
        "Cole Reyes": "Diego Marsh",
        "Diego Marsh": "Cole Reyes",
    }


def test_real_structure_recovers_materially_more_than_three_books() -> None:
    """Slice-7: real fighters showed only 1-3 books because the movement-arrow
    span corrupted the moneyline parse. Each fighter now carries >=4 books, and
    the arrow-decorated values parse to the right integers."""
    rows = parse_bestfightodds_all_books(_load_real_structure_fixture())
    for r in rows:
        assert len(r.book_lines) >= 4, (r.fighter_name, len(r.book_lines))
    # Aiden's first cell is "-150▲" — the arrow must not break the parse.
    assert _books_for(rows, "Aiden Stone") == {
        "Polymarket $50 Bonus": -150,
        "Kalshi $10 Free": -145,
        "FanDuel": -160,
        "Caesars": -152,
    }


def test_real_structure_reads_draftkings_when_present_skips_when_empty() -> None:
    """The DraftKings column is the grid anchor and is present in the header, but
    its VALUES are server-empty for the first fight (BFO loads DraftKings /
    BetMGM client-side) and present for the second. The parser reads it when
    present and simply omits it when blank — never inventing a line."""
    rows = parse_bestfightodds_all_books(_load_real_structure_fixture())
    # Fight 1: DraftKings / BetMGM cells are blank -> not in the book set.
    assert "DraftKings" not in _books_for(rows, "Aiden Stone")
    assert "BetMGM" not in _books_for(rows, "Brody Vance")
    # Fight 2: DraftKings is populated -> read as a labelled book.
    assert _books_for(rows, "Cole Reyes")["DraftKings"] == -202
    assert _books_for(rows, "Diego Marsh")["DraftKings"] == 171


def test_real_structure_ignores_trailing_props_count_cell() -> None:
    """The trailing 'Props' count (e.g. 92) is unsigned and below the American
    magnitude floor, so it is never misread as a phantom book line."""
    rows = parse_bestfightodds_all_books(_load_real_structure_fixture())
    for r in rows:
        for line in r.book_lines:
            assert abs(line.american_moneyline) >= 100
        assert "Props" not in {line.book for line in r.book_lines}
        # No fighter carries one of the props-count integers as a line.
        assert all(
            line.american_moneyline not in (92, 88, 77, 70)
            for line in r.book_lines
        )


def test_real_structure_does_not_change_synthetic_fixture_output() -> None:
    """The hardening must not regress the original synthetic fixture: its
    fighter cells already carry /fighters/ hrefs and arrow-free odds, so it
    parses to exactly the same four fighters as before."""
    rows = parse_bestfightodds_all_books(_load_all_books_fixture())
    assert [r.fighter_name for r in rows] == [
        "Alpha Fighter",
        "Bravo Fighter",
        "Charlie Fighter",
        "Delta Fighter",
    ]
    assert _books_for(rows, "Alpha Fighter") == {
        "DraftKings": -200,
        "FanDuel": -210,
        "Caesars": -195,
        "Book4": -205,
    }
