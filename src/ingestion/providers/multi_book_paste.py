"""Pure multi-book paste grid → per-fighter all-book lines (consensus slice 4).

Realizes ``docs/ODDS_CONSENSUS_DESIGN.md`` §5.2 (the **multi-book paste
parser**): the user views a public odds-comparison grid in their own browser,
copies the visible table, and pastes it in. This module turns that pasted grid
into per-fighter all-book lines for the multi-book consensus blend — the paste
counterpart of the BestFightOdds all-books parser (§5.1,
:func:`src.ingestion.providers.bestfightodds.parse_bestfightodds_all_books`).

It mirrors ``draftkings_paste.py`` in spirit (a pure, offline, preview-only
parser of pasted text with a loud-failure / surfaced-skip discipline) but reads
a **2-D grid** rather than a vertical board: a header row of book names and one
row per fighter of American lines. Its output shape mirrors the BFO all-books
parser so both consensus sources feed slices 5–6 uniformly — a per-fighter row
carrying every book's line plus a best-effort ``opponent``.

Hard boundaries (design §5.2, §14; ``docs/DEVELOPMENT_NOTES.md`` §3):

  - **No network.** This module parses a string the caller already holds. It
    imports nothing that opens a socket.
  - **No DB, no UI.** It reads nothing and writes nothing. Preview, save, and
    Streamlit wiring are slices 5–6 and out of scope here.
  - **No blend math.** It reports the raw per-book lines only; de-vig / median
    is the consensus service (slice 2, ``src/projections/odds_consensus.py``).

Grid layout assumptions
-----------------------------------------------------------------------------
When a user copies an HTML odds-comparison table to the clipboard, columns
arrive **tab-separated** and rows **newline-separated** — the universal
copy-a-table-into-a-spreadsheet shape. That is the canonical input:

    Matchup<TAB>DraftKings<TAB>FanDuel<TAB>BetMGM<TAB>Props
    Jon Jones<TAB>−250<TAB>-245<TAB>−260<TAB>14
    Stipe Miocic<TAB>+210<TAB>+205<TAB>+220<TAB>8

  - The first row is the **header**; its first cell is the fighter/matchup
    column label (ignored) and each remaining cell is a book name.
  - The leading column (index 0) of every data row is the fighter name.
  - Each remaining cell is that book's American line for that fighter, or blank
    when the book posts no line for them.

Tolerances (design §5.2):

  - **Blank cells** — a book with no line for a fighter is an empty cell
    (``a<TAB><TAB>b``); the fighter keeps their other books. Reliable
    blank-cell detection needs the tab-delimited form (empty cells survive the
    split); a space-aligned paste cannot represent a blank interior cell.
  - **Trailing (or any) non-odds column** — e.g. a "Props" count of ``14`` (or
    even ``120``). A cell is read as a line only when it is an
    *explicitly-signed* American token of magnitude ≥ 100, mirroring
    ``draftkings_paste.py``'s ``_AMERICAN_TOKEN``. A bare, unsigned count is
    ignored **regardless of size** (a signed grid never writes a count with a
    leading ``+``/``−``), so a non-odds column contributes no book line and
    never surfaces as a phantom book — even when the count is ≥ 100. Both the
    sign requirement and the magnitude floor (``MIN_AMERICAN_MAGNITUDE``) matter
    because the canonical :func:`parse_moneyline` accepts any nonzero integer
    (``12`` → ``12``, ``120`` → ``120``) and so alone would mistake a count for
    a line.
  - **Unicode minus** (U+2212, ``−``) — many grids render favorites with it
    rather than the ASCII hyphen-minus; it is normalized before parsing, as in
    ``draftkings_paste.py``.

As a convenience for a space-aligned paste (no tabs at all), columns are split
on runs of 2+ whitespace so single-space fighter names stay intact; that path
is best-effort and cannot represent blank interior cells (see above). A single
paste is assumed to use one delimiter throughout: mixing tabs and 2+-space gaps
within one grid is not auto-detected (a 2-space gap cannot be told apart from a
2-space fighter name) and is a real-feed hardening concern (design §10.7), not
handled here.

Failure is loud and specific. The parser raises
:class:`MultiBookPasteParseError` — never a silent empty result — when the text
has no grid, no header books, no fighter rows, or no readable line anywhere. A
single fighter row with no readable line in any book is **skipped with a
recorded warning** (surfaced on the result's ``warnings``) provided at least one
other fighter parsed, so a partial parse is never silent.

``opponent`` is best-effort, filled from consecutive-row adjacency (the bout
layout most comparison grids use), exactly as the BFO all-books parser does;
name-matching and review against the DK roster are slice 6, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.ingestion.odds_csv_importer import parse_moneyline

# §6 source label. A pasted multi-book grid's provenance is "Paste"; the
# lowercase ``'paste'`` token the ``odds_book_lines.source`` column stores
# (design §6) is mapped at persistence time (slice 5), mirroring how the BFO
# parser emits ``"BestFightOdds"`` rather than the schema's ``'bestfightodds'``.
SOURCE_PASTE = "Paste"

# §1.7 status. A clean American line read deterministically from a pasted grid
# cell is a full-confidence, parsed line.
STATUS_PARSED = "parsed"

# Unicode minus (U+2212) — rendered by many books for negative odds. The
# canonical ``parse_moneyline`` only understands the ASCII hyphen-minus, so we
# normalize before delegating to it (as ``draftkings_paste.py`` does).
_UNICODE_MINUS = "−"

# The American-odds magnitude floor. Real American moneylines are always
# ``>= +100`` or ``<= -100`` (pick'em is ±100); a value below this magnitude is
# not a line. With the explicit-sign requirement below, the floor rejects a
# signed-but-sub-100 cell (e.g. ``+50``); the sign requirement rejects an
# unsigned count of any size (a "Props" column), so the two together separate an
# odds cell from a non-odds one even when the count is ≥ 100.
MIN_AMERICAN_MAGNITUDE = 100

# An American moneyline token: an explicit sign (a real odds grid always shows
# one) followed by digits, no decimal point — mirroring ``draftkings_paste.py``'s
# ``_AMERICAN_TOKEN``. Requiring the sign is what lets an unsigned non-odds
# count be ignored regardless of its magnitude (``120`` total props is not read
# as ``+120``); the magnitude floor then rejects a signed-but-sub-100 value.
_AMERICAN_TOKEN = re.compile(rf"^[+\-{_UNICODE_MINUS}]\d+$")

# Best-effort positional label for a book column whose header cell is blank
# (mirrors the BFO all-books parser's ``Book{n}`` fallback, design §11 open #4).
POSITIONAL_BOOK_PREFIX = "Book"

# Split a space-aligned row on runs of 2+ whitespace so single-space fighter
# names ("Jon Jones") are never split. Only used when the text has no tabs.
_MULTISPACE = re.compile(r"\s{2,}")


class MultiBookPasteParseError(ValueError):
    """Raised when a pasted multi-book grid yields no usable book lines.

    A subclass of :class:`ValueError` so existing ``except ValueError`` handlers
    catch it, while callers that care can match it specifically.
    """


@dataclass(frozen=True)
class PasteBookLine:
    """One book's American moneyline for a single fighter (design §5.2).

    The all-books unit, structurally mirroring the BFO parser's ``BookLine``: a
    fighter has one :class:`PasteBookLine` per book column that posts a valid
    American line for them. ``book`` is the header label (positional fallback
    for a blank header — see :func:`_book_label`).
    """

    book: str
    american_moneyline: int


@dataclass(frozen=True)
class MultiBookPasteRow:
    """All books' lines for one fighter from a pasted grid (design §5.2).

    Mirrors the BFO all-books ``AllBooksFighterRow`` so both consensus sources
    feed slices 5–6 uniformly. ``opponent`` is best-effort (consecutive-row
    adjacency) and ``None`` when a row has no adjacent partner. ``source_url`` /
    ``collected_at`` are pass-through provenance the caller supplies — the pure
    parser has no URL and no clock of its own. Pairing these rows into the
    per-fight consensus blend and any persistence are later slices (design §10).
    """

    fighter_name: str
    book_lines: tuple[PasteBookLine, ...]
    opponent: str | None = None
    source: str = SOURCE_PASTE
    source_url: str | None = None
    collected_at: str | None = None
    status: str = STATUS_PARSED


@dataclass(frozen=True)
class MultiBookPasteParseResult:
    """The outcome of a paste-grid parse: per-fighter rows plus skip warnings.

    ``rows`` is one :class:`MultiBookPasteRow` per fighter that has at least one
    readable book line, in page order. ``warnings`` records every fighter row
    that was present but had no readable line in any book and was skipped, so a
    partial parse is never silent (design §9 / "no silent truncation"). The
    result wrapper + ``warnings`` mirror ``draftkings_paste.py``.
    """

    rows: list[MultiBookPasteRow]
    warnings: list[str] = field(default_factory=list)


def _american_line(token: str) -> int | None:
    """Return the int American moneyline for a grid cell, else ``None``.

    A cell is a line only when it is an *explicitly-signed* integer
    (``_AMERICAN_TOKEN``) whose magnitude clears the American-odds floor
    (``MIN_AMERICAN_MAGNITUDE``). Everything else yields ``None`` — meaning "this
    book posts no line for this fighter": a blank cell, a bare count in a
    non-odds column (``14``, or even ``120``), a round total (``2.5``), a
    non-numeric cell. The unicode minus is normalized before the int parse, as
    in ``draftkings_paste.py``.
    """
    if not _AMERICAN_TOKEN.match(token):
        return None
    n = parse_moneyline(token.replace(_UNICODE_MINUS, "-"))
    if n is None or abs(n) < MIN_AMERICAN_MAGNITUDE:
        return None
    return n


def _book_label(cell_text: str, col_index: int) -> str:
    """Best-effort book label for a header column (mirrors BFO's fallback).

    ``cell_text`` is already stripped by :func:`_split_row`. Prefers it; falls
    back to a positional ``Book{n}`` label so a column whose header is blank is
    still read, never silently dropped.
    """
    if cell_text:
        return cell_text
    return f"{POSITIONAL_BOOK_PREFIX}{col_index}"


def _split_row(line: str, *, use_tabs: bool) -> list[str]:
    """Split one grid line into stripped cells.

    Tab-delimited (the canonical copied-table form) preserves blank interior
    cells; the space-aligned fallback (runs of 2+ whitespace) keeps single-space
    fighter names intact but cannot represent a blank interior cell.
    """
    if use_tabs:
        cells = line.split("\t")
    else:
        cells = _MULTISPACE.split(line.strip())
    return [cell.strip() for cell in cells]


def parse_multi_book_paste(
    text: str,
    *,
    source_url: str | None = None,
    collected_at: str | None = None,
) -> MultiBookPasteParseResult:
    """Parse a pasted multi-book odds grid into per-fighter all-book lines (§5.2).

    Reads a tab-delimited grid (or a space-aligned one when no tabs are present)
    whose first row is a header of book names and whose every later row is a
    fighter and their American line per book. Returns one
    :class:`MultiBookPasteRow` per fighter that has at least one readable line,
    each carrying every book that posted a line for them plus a best-effort
    ``opponent`` from consecutive-row adjacency.

    Tolerates blank cells (a book with no line), a trailing/any non-odds column
    (a sub-100-magnitude count is not a line), and the unicode minus. Ignores
    no book by default — every header column after the fighter column is a book,
    and two columns resolving to the same label are disambiguated by column
    index so the downstream blend never silently merges them.

    ``source_url`` and ``collected_at`` are recorded verbatim on every row as
    provenance — the parser supplies neither itself.

    Raises :class:`MultiBookPasteParseError` (never a silent empty result) when
    the text has no grid rows, the header exposes no book columns, there are no
    fighter rows, or every fighter row was line-less.
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        raise MultiBookPasteParseError(
            "Pasted text is empty; expected a multi-book odds grid with a "
            "header row of book names and one row per fighter."
        )

    # Tab is the canonical column delimiter (copied tables paste as TSV); fall
    # back to space-alignment only when the whole paste has no tab at all.
    use_tabs = "\t" in text
    header = _split_row(lines[0], use_tabs=use_tabs)

    # Every column after the leading fighter column (index 0) is a book column.
    # Disambiguate any two columns resolving to the same label (a positional
    # fallback colliding with a real book, or a book rendered twice) by suffixing
    # the column index, so a fighter never carries two same-named books and the
    # blend never silently treats them as one (mirrors the BFO all-books parser).
    book_columns: list[tuple[int, str]] = []
    seen_labels: set[str] = set()
    for col_index in range(1, len(header)):
        label = _book_label(header[col_index], col_index)
        if label in seen_labels:
            label = f"{label} (col {col_index})"
        seen_labels.add(label)
        book_columns.append((col_index, label))
    if not book_columns:
        raise MultiBookPasteParseError(
            "Pasted grid header exposes no book columns; expected the first row "
            "to be a fighter/matchup label followed by one or more book names."
        )

    data_lines = lines[1:]
    if not data_lines:
        raise MultiBookPasteParseError(
            "Pasted grid has a header of book names but no fighter rows."
        )

    # Pass 1: one (fighter name, that row's readable book lines) per data row.
    # A row whose first column (the fighter name) is blank — an accidental
    # leading tab or a separator line — has no identity to match on, so it is
    # dropped; but the skip is warned, never silent (design §9 / "no silent
    # truncation"). ``_split_row`` always returns at least one stripped cell, so
    # ``cells[0]`` is safe and pre-stripped.
    warnings: list[str] = []
    parsed_rows: list[tuple[str, tuple[PasteBookLine, ...]]] = []
    for raw_line in data_lines:
        cells = _split_row(raw_line, use_tabs=use_tabs)
        fighter_name = cells[0]
        if not fighter_name:
            warnings.append(
                "Skipped a data row whose first column (the fighter name) was "
                "blank; any odds on that row were dropped."
            )
            continue
        lines_for_fighter: list[PasteBookLine] = []
        for col_index, book in book_columns:
            if col_index >= len(cells):
                continue
            american = _american_line(cells[col_index])
            if american is None:
                # Blank / non-odds cell: this one book has no posted line for the
                # fighter. Skip the book, not the fighter.
                continue
            lines_for_fighter.append(
                PasteBookLine(book=book, american_moneyline=american)
            )
        parsed_rows.append((fighter_name, tuple(lines_for_fighter)))

    if not parsed_rows:
        raise MultiBookPasteParseError(
            "Pasted grid has a header of book names but no readable fighter "
            "rows (every data row was missing a fighter name). "
            + (" ".join(warnings) if warnings else "")
        )

    # Pass 2: best-effort opponent via consecutive-row adjacency. Pairing is on
    # page order *before* dropping line-less fighters, exactly as the BFO
    # all-books parser pairs a bout's two consecutive rows. This is deliberate
    # and load-bearing: it keeps a present fighter's ``opponent`` set to the
    # correct name even when their partner posted no line (and is therefore
    # dropped below) — ``opponent`` is a name reference for the downstream
    # matcher, not a pointer into the emitted rows. Pairing *after* the drop
    # would instead shift indices and mis-pair the survivors.
    opponents: list[str | None] = [None] * len(parsed_rows)
    for index in range(0, len(parsed_rows) - 1, 2):
        opponents[index] = parsed_rows[index + 1][0]
        opponents[index + 1] = parsed_rows[index][0]

    rows: list[MultiBookPasteRow] = []
    for (fighter_name, lines_for_fighter), opponent in zip(parsed_rows, opponents):
        if not lines_for_fighter:
            warnings.append(
                f"Skipped '{fighter_name}': no readable American line in any "
                "book column (all cells blank or non-odds)."
            )
            continue
        rows.append(
            MultiBookPasteRow(
                fighter_name=fighter_name,
                book_lines=lines_for_fighter,
                opponent=opponent,
                source_url=source_url,
                collected_at=collected_at,
            )
        )

    if not rows:
        raise MultiBookPasteParseError(
            "Pasted grid had fighter rows but no readable American lines; every "
            "row's cells were blank or non-odds. "
            + (" ".join(warnings) if warnings else "")
        )

    return MultiBookPasteParseResult(rows=rows, warnings=warnings)
