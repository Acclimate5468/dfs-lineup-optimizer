"""Pure BestFightOdds HTML → normalized moneyline rows (Phase 1).

Realizes ``docs/ODDS_ACQUISITION_V0_DESIGN.md`` §3 Phase 1: a **pure, offline**
parser that turns already-fetched BestFightOdds event-table HTML into the one
normalized moneyline row of §1.7. It prefers the **DraftKings** column per
locked decision #3 and does **not** implement the consensus / median fallback
(decision #4, deferred to a later phase).

Hard boundaries (design §1.9, §1.11, §1.12; ``docs/DEVELOPMENT_NOTES.md`` §3):

  - **No network.** This module parses an HTML string the caller already holds.
    The live fetch is Phase 2; nothing here imports ``requests`` / ``httpx`` /
    ``aiohttp`` / ``socket`` or opens a connection.
  - **No DB, no UI.** It reads nothing and writes nothing. Preview, save, and
    Streamlit wiring are Phases 2–3 and are out of scope here.
  - **DraftKings column only — single-source path.** The §1.7
    :func:`parse_bestfightodds_html` reads the DraftKings column only; a source
    row for any other book column is ignored, and if the DraftKings column is
    absent it fails loudly (decision #3/#4) rather than silently falling back to
    another book.

This module *also* exposes an **additive** all-books parser,
:func:`parse_bestfightodds_all_books`, realizing
``docs/ODDS_CONSENSUS_DESIGN.md`` §5.1: it reads *every* book column the same
event table renders (DraftKings included) for the multi-book consensus path. It
is a sibling of the DraftKings-only parser — same pure / offline / no-DB /
no-UI boundaries, same loud-failure discipline — and it does **not** change the
single-source DraftKings behavior above. It performs no fight-pairing math; it
reports per-fighter book lines with a best-effort opponent (BFO lists a bout as
two consecutive rows). Pairing into the consensus blend and any persistence are
later slices (design §10).

Failure is loud and specific. The parser raises
:class:`BestFightOddsParseError` — never returns empty or guessed rows — when
the HTML has no recognizable odds table, has no DraftKings column, or yields no
valid DraftKings moneyline rows. A blank / missing DraftKings cell for an
otherwise-valid fighter is skipped (the book simply has no line), not an error.

The normalized output is :class:`AcquiredMoneylineRow`, the §1.7 contract. It is
an *input* to the existing odds pipeline (``ODDS_NEWS_SNAPSHOT`` →
``odds_rows`` → match → recompute); Phase 1 only produces it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from src.ingestion.odds_csv_importer import parse_moneyline

# §1.7 source / book labels for every row this provider emits. The whole
# point of Phase 1 is the DraftKings column, so ``book`` is constant.
SOURCE_BESTFIGHTODDS = "BestFightOdds"
BOOK_DRAFTKINGS = "DraftKings"

# §1.7 status / confidence. A clean American line read straight from the
# DraftKings column is a full-confidence, parsed row. Lower-confidence /
# review states belong to the fetch + preview phases, not this pure parse.
STATUS_PARSED = "parsed"
DEFAULT_CONFIDENCE = 1.0

# Token used to recognize the DraftKings column header regardless of spacing,
# punctuation, or casing ("DraftKings", "Draft Kings", "draftkings-sportsbook").
_DK_TOKEN = "draftkings"

# Best-effort positional label for an all-books column whose header carries no
# recognizable book name (design §11 open #4: "store them positionally with
# best-effort labels"). The trailing index is the column's position in the row.
POSITIONAL_BOOK_PREFIX = "Book"

_NONALNUM = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")


class BestFightOddsParseError(ValueError):
    """Raised when BestFightOdds HTML cannot be parsed into DraftKings rows.

    A subclass of :class:`ValueError` so existing ``except ValueError`` import
    handlers catch it, while callers that care can match it specifically.
    """


@dataclass(frozen=True)
class AcquiredMoneylineRow:
    """One normalized acquired moneyline (design §1.7).

    ``fighter_name`` and ``american_moneyline`` are the only always-present
    fields. ``opponent`` is left ``None`` in Phase 1 (fight pairing is not
    inferred here). ``source_url`` and ``fetched_at`` are pass-through
    provenance supplied by the caller — the pure parser has no clock and no
    URL of its own.
    """

    fighter_name: str
    american_moneyline: int
    source: str = SOURCE_BESTFIGHTODDS
    book: str = BOOK_DRAFTKINGS
    opponent: str | None = None
    source_url: str | None = None
    fetched_at: str | None = None
    status: str = STATUS_PARSED
    confidence: float = DEFAULT_CONFIDENCE


@dataclass(frozen=True)
class _Cell:
    """A harvested table cell: visible text plus attribute metadata.

    ``meta`` collects the distinct ``alt`` / ``title`` attributes of inner tags
    (BestFightOdds renders book headers as an ``<img alt="DraftKings">`` or a
    titled anchor), in document order, so column detection can see the book name
    even when the cell has no text. It is a tuple (not a joined string) so the
    all-books parser can recover a single clean book label from the first value
    — a titled anchor wrapping an equally-titled image otherwise yields a
    duplicated ``"DraftKings DraftKings"`` label.

    ``hrefs`` collects the distinct ``href`` of inner ``<a>`` tags, in document
    order. It is additive metadata: the DraftKings-only path reads only ``text``
    / ``meta`` and is unchanged by it. The all-books real-feed hardening uses it
    to tell a moneyline fighter row (whose leading cell links to a ``/fighters/``
    page) apart from the round-total / method / matchup-header rows a real BFO
    event table interleaves (design §10.7).
    """

    text: str
    meta: tuple[str, ...]
    hrefs: tuple[str, ...] = ()


class _TableHarvester(HTMLParser):
    """Collect every ``<table>`` into rows of :class:`_Cell` (text + meta).

    Deliberately small: it does not interpret columns, only captures the cell
    grid. Inner markup (``<a>``, ``<span>``, ``<img>``, ``<b>``) inside a cell
    is flattened into the cell's text, while ``alt`` / ``title`` attributes are
    accumulated into the cell's ``meta`` for header detection.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[_Cell]]] = []
        self._row: list[_Cell] | None = None
        self._cell_text: list[str] | None = None
        self._cell_meta: list[str] | None = None
        self._cell_hrefs: list[str] | None = None

    @property
    def _in_cell(self) -> bool:
        return self._cell_text is not None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.tables.append([])
            self._row = None
            self._cell_text = None
            self._cell_meta = None
            self._cell_hrefs = None
        elif tag == "tr" and self.tables:
            self._row = []
            self.tables[-1].append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._cell_text = []
            self._cell_meta = []
            self._cell_hrefs = []
        elif self._in_cell:
            attr_map = dict(attrs)
            for key in ("alt", "title"):
                value = attr_map.get(key)
                if value:
                    self._cell_meta.append(value)  # type: ignore[union-attr]
            if tag == "a":
                href = attr_map.get("href")
                if href:
                    self._cell_hrefs.append(href)  # type: ignore[union-attr]
            if tag == "br":
                self._cell_text.append(" ")  # type: ignore[union-attr]

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text.append(data)  # type: ignore[union-attr]

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            text = _WS.sub(" ", "".join(self._cell_text)).strip()  # type: ignore[arg-type]
            cleaned = (_WS.sub(" ", value).strip() for value in self._cell_meta)  # type: ignore[union-attr]
            meta = tuple(dict.fromkeys(value for value in cleaned if value))
            hrefs = tuple(
                dict.fromkeys(h for h in self._cell_hrefs if h)  # type: ignore[union-attr]
            )
            if self._row is not None:
                self._row.append(_Cell(text=text, meta=meta, hrefs=hrefs))
            self._cell_text = None
            self._cell_meta = None
            self._cell_hrefs = None
        elif tag == "tr":
            self._row = None


def _detect_token(cell: _Cell) -> str:
    """Collapse a cell's text + meta to lowercase alphanumerics for matching."""
    joined = " ".join((cell.text, *cell.meta))
    return _NONALNUM.sub("", joined.lower())


def _find_dk_column(
    table: list[list[_Cell]],
) -> tuple[int, int] | None:
    """Locate the DraftKings column in a table.

    Returns ``(header_row_index, dk_column_index)`` for the first row whose
    cells include a DraftKings book header, or ``None`` when this table has no
    DraftKings column. Data rows are taken to be the rows *after* the header.
    """
    for row_index, row in enumerate(table):
        for col_index, cell in enumerate(row):
            if _DK_TOKEN in _detect_token(cell):
                return row_index, col_index
    return None


def parse_bestfightodds_html(
    html: str,
    *,
    source_url: str | None = None,
    fetched_at: str | None = None,
) -> list[AcquiredMoneylineRow]:
    """Parse BestFightOdds event HTML into DraftKings moneyline rows (§1.7).

    Identifies the DraftKings sportsbook column from the table headers, reads
    each fighter's name from the leading cell and their American moneyline from
    the DraftKings column, and returns one :class:`AcquiredMoneylineRow` per
    fighter that has a valid DraftKings line. Non-DraftKings book columns are
    ignored (decision #3); the consensus / median fallback is **not** applied
    (decision #4, deferred).

    ``source_url`` and ``fetched_at`` are recorded verbatim on every row as
    §1.7 provenance — the parser supplies neither itself.

    Raises :class:`BestFightOddsParseError` (never an empty/guessed result) when:

      - no recognizable odds table is present (unrecognized structure),
      - no table exposes a DraftKings column, or
      - a DraftKings column exists but yields no valid moneyline rows.

    A blank / missing DraftKings cell on an otherwise-valid fighter row is
    skipped (the book has no posted line), not treated as an error.
    """
    harvester = _TableHarvester()
    harvester.feed(html or "")
    harvester.close()

    tables = [t for t in harvester.tables if any(row for row in t)]
    if not tables:
        raise BestFightOddsParseError(
            "BestFightOdds HTML has no recognizable odds table; "
            "the page structure was not recognized."
        )

    located: tuple[list[list[_Cell]], int, int] | None = None
    for table in tables:
        found = _find_dk_column(table)
        if found is not None:
            header_row_index, dk_col_index = found
            located = (table, header_row_index, dk_col_index)
            break

    if located is None:
        raise BestFightOddsParseError(
            "BestFightOdds HTML has no DraftKings column. Phase 1 reads the "
            "DraftKings line only and does not fall back to another book."
        )

    table, header_row_index, dk_col_index = located
    rows: list[AcquiredMoneylineRow] = []
    for row in table[header_row_index + 1 :]:
        if dk_col_index >= len(row):
            continue
        fighter_name = row[0].text.strip() if row else ""
        if not fighter_name:
            continue
        american = parse_moneyline(row[dk_col_index].text)
        if american is None:
            # Blank / missing DraftKings cell: the book has no line for this
            # fighter. Skip the row rather than emit an invalid moneyline.
            continue
        rows.append(
            AcquiredMoneylineRow(
                fighter_name=fighter_name,
                american_moneyline=american,
                source_url=source_url,
                fetched_at=fetched_at,
            )
        )

    if not rows:
        raise BestFightOddsParseError(
            "BestFightOdds HTML has a DraftKings column but no valid "
            "DraftKings moneyline rows were found."
        )
    return rows


# ---------------------------------------------------------------------------
# All-books parser (additive — ODDS_CONSENSUS_DESIGN.md §5.1)
# ---------------------------------------------------------------------------
#
# Reads *every* book column the same BFO event table renders, DraftKings
# included, for the multi-book consensus path. Additive: the DraftKings-only
# :func:`parse_bestfightodds_html` above is untouched. This parser does no
# fight-pairing math and no de-vig / blend (that is the consensus service,
# Slice 2); it only reports per-fighter book lines with a best-effort opponent.


@dataclass(frozen=True)
class BookLine:
    """One book's American moneyline for a single fighter (design §5.1).

    The all-books unit: a fighter has one :class:`BookLine` per book that posts
    a valid line for them. ``book`` is the best-effort column label (see
    :func:`_column_label`).
    """

    book: str
    american_moneyline: int


@dataclass(frozen=True)
class AllBooksFighterRow:
    """All books' lines for one fighter (design §5.1 ``{fighter, opponent, …}``).

    ``opponent`` is best-effort: BestFightOdds renders a bout as two consecutive
    rows, so the two fighters of a pair point at each other. It is ``None`` when
    a row has no adjacent partner (e.g. an odd trailing row). ``source_url`` /
    ``fetched_at`` are pass-through provenance the caller supplies — the pure
    parser has no clock and no URL of its own. Pairing these rows into the
    consensus blend and any persistence are later slices (design §10); this
    parser only reports what the page renders.
    """

    fighter_name: str
    book_lines: tuple[BookLine, ...]
    opponent: str | None = None
    source: str = SOURCE_BESTFIGHTODDS
    source_url: str | None = None
    fetched_at: str | None = None
    status: str = STATUS_PARSED


def _column_label(cell: _Cell, col_index: int) -> str:
    """Best-effort book label for a header column (design §11 open #4).

    Prefers the header cell's visible text, then its first ``alt`` / ``title``
    value (BFO renders book headers as a titled anchor / ``<img alt=...>``), and
    finally falls back to a positional ``Book{n}`` label so a column whose header
    carries no recognizable name is still read, never silently dropped.
    """
    if cell.text.strip():
        return cell.text.strip()
    for value in cell.meta:
        if value.strip():
            return value.strip()
    return f"{POSITIONAL_BOOK_PREFIX}{col_index}"


# --- Real-feed row/cell normalization (ODDS_CONSENSUS_DESIGN §10.7) ----------
#
# A real BestFightOdds event table is one large grid that interleaves, per
# fight: a matchup-header row, the two head-to-head moneyline rows, then many
# round-total / method / prop rows ("Under 1½ rounds", "Wins by KO/TKO", …).
# Only a moneyline fighter row's leading cell links to a ``/fighters/`` page, so
# that anchor is the structural discriminator that isolates the ~2 fighters per
# fight from the hundreds of prop rows. (The hand-written synthetic fixture
# never modelled the props / rotation numbers / movement arrows, so the original
# parser — built against it — read every row as a fighter.)
_FIGHTER_HREF_MARKER = "/fighters/"

# BFO prefixes the name cell with a rotation / pictureid number rendered as its
# own anchor, so the flattened cell text reads e.g. "43417Belal Muhammad".
_LEADING_ROTATION_RE = re.compile(r"^\s*\d+\s*")

# A live BFO odds cell renders the line and then an up/down movement arrow in a
# second span, so the flattened text reads e.g. "-108▲". Read only the
# explicitly-signed American token (mirroring the multi-book paste parser's
# discriminator) so the arrow and any other decoration are ignored, and require
# the American magnitude floor so a stray small number in a non-odds cell is
# never misread as a line.
_AMERICAN_TOKEN_RE = re.compile(r"[+\-]\d+")
_MIN_AMERICAN_MAGNITUDE = 100


def _row_is_fighter_row(row: list[_Cell]) -> bool:
    """True when a row's leading cell links to a ``/fighters/`` page (§10.7).

    The structural test that separates a head-to-head moneyline row from the
    round-total / method / matchup-header rows a real BFO event table
    interleaves. The synthetic fixture's fighter cells carry the same
    ``/fighters/`` anchor, so it stays a fighter row under this test.
    """
    return bool(row) and any(
        _FIGHTER_HREF_MARKER in href for href in row[0].hrefs
    )


def _clean_fighter_name(text: str) -> str:
    """Strip the leading rotation/pictureid number BFO renders before the name."""
    return _LEADING_ROTATION_RE.sub("", text or "").strip()


def _parse_book_moneyline(text: str) -> int | None:
    """American line from an odds cell, tolerant of the BFO movement-arrow span.

    Reads the first explicitly-signed integer token (so a trailing ``▲`` / ``▼``
    arrow or any non-odds decoration is ignored) and applies the American-odds
    magnitude floor, so a stray small number in a non-odds cell is never misread
    as a line. The synthetic fixture's clean ``-200`` cells parse identically.
    """
    cleaned = (text or "").replace("−", "-")
    match = _AMERICAN_TOKEN_RE.search(cleaned)
    if match is None:
        return None
    american = parse_moneyline(match.group(0))
    if american is None or abs(american) < _MIN_AMERICAN_MAGNITUDE:
        return None
    return american


def parse_bestfightodds_all_books(
    html: str,
    *,
    source_url: str | None = None,
    fetched_at: str | None = None,
) -> list[AllBooksFighterRow]:
    """Parse BestFightOdds event HTML into per-fighter all-book lines (§5.1).

    The additive consensus-path sibling of :func:`parse_bestfightodds_html`.
    Where that function keeps only the DraftKings column, this one reads **every**
    book column the event table renders — DraftKings included — and returns one
    :class:`AllBooksFighterRow` per fighter that has at least one valid book line.

    The odds table and its header row are located exactly as the single-source
    path locates them: by the DraftKings column, which is always present on the
    approved BestFightOdds event grid (decision #2/#3). From that header row,
    every column after the leading fighter column is treated as a book and
    labelled via :func:`_column_label` (positional fallback per open #4); two
    columns that resolve to the same label are disambiguated by column index so
    the blend never silently merges them.

    Real-feed hardening (design §10.7): a live BFO event table is one large grid
    that interleaves, per fight, a matchup-header row, the two head-to-head
    moneyline rows, and many round-total / method / prop rows. Only a moneyline
    fighter row's leading cell links to a ``/fighters/`` page, so that anchor
    (:func:`_row_is_fighter_row`) isolates the fighters from the prop rows; the
    name has its leading rotation/pictureid number stripped
    (:func:`_clean_fighter_name`); and each book line is read tolerant of the
    up/down movement-arrow span (:func:`_parse_book_moneyline`). A blank / invalid
    cell means that one book simply has no posted line for that fighter and is
    skipped (a fighter with no line in *any* book is dropped). ``opponent`` is
    filled best-effort from consecutive fighter-row adjacency (the BFO bout
    layout), after the prop rows are filtered out so adjacency is between real
    fighters.

    ``source_url`` and ``fetched_at`` are recorded verbatim on every row as
    provenance — the parser supplies neither itself.

    Raises :class:`BestFightOddsParseError` (never an empty/guessed result) when:

      - no recognizable odds table is present (unrecognized structure),
      - no table exposes a DraftKings column (the grid anchor is absent),
      - the located header row exposes no book columns, or
      - book columns exist but yield no fighter with a single valid line.
    """
    harvester = _TableHarvester()
    harvester.feed(html or "")
    harvester.close()

    tables = [t for t in harvester.tables if any(row for row in t)]
    if not tables:
        raise BestFightOddsParseError(
            "BestFightOdds HTML has no recognizable odds table; "
            "the page structure was not recognized."
        )

    located: tuple[list[list[_Cell]], int] | None = None
    for table in tables:
        found = _find_dk_column(table)
        if found is not None:
            header_row_index, _dk_col_index = found
            located = (table, header_row_index)
            break

    if located is None:
        raise BestFightOddsParseError(
            "BestFightOdds HTML has no DraftKings column; the all-books parser "
            "anchors on the DraftKings column to identify the odds grid."
        )

    table, header_row_index = located
    header = table[header_row_index]

    # Every column after the leading fighter column (index 0) is a book column.
    # Disambiguate any two columns that resolve to the same label (a positional
    # fallback colliding with a real book, or a book rendered in two columns) by
    # suffixing the column index, so a fighter never carries two same-named books
    # and the downstream blend never silently treats them as one.
    book_columns: list[tuple[int, str]] = []
    seen_labels: set[str] = set()
    for col_index in range(1, len(header)):
        label = _column_label(header[col_index], col_index)
        if label in seen_labels:
            label = f"{label} (col {col_index})"
        seen_labels.add(label)
        book_columns.append((col_index, label))
    if not book_columns:
        raise BestFightOddsParseError(
            "BestFightOdds HTML located the odds grid but its header row exposes "
            "no book columns to read."
        )

    # Pass 1: one (fighter name, that row's valid book lines) per FIGHTER row.
    # A real BFO event table interleaves the head-to-head moneyline rows with
    # round-total / method / matchup-header rows; only a moneyline row's leading
    # cell links to a ``/fighters/`` page, so that anchor isolates the fighters
    # from the props (``_row_is_fighter_row``, design §10.7). The name has its
    # leading rotation/pictureid number stripped, and each book cell is read
    # tolerant of the up/down movement-arrow span BFO appends to a live line.
    parsed_rows: list[tuple[str, tuple[BookLine, ...]]] = []
    for row in table[header_row_index + 1 :]:
        if not _row_is_fighter_row(row):
            # A round-total / method / matchup-header row, not a fighter.
            continue
        fighter_name = _clean_fighter_name(row[0].text)
        if not fighter_name:
            continue
        lines: list[BookLine] = []
        for col_index, book in book_columns:
            if col_index >= len(row):
                continue
            american = _parse_book_moneyline(row[col_index].text)
            if american is None:
                # Blank / missing cell: this one book has no posted line for the
                # fighter. Skip the book, not the fighter.
                continue
            lines.append(BookLine(book=book, american_moneyline=american))
        parsed_rows.append((fighter_name, tuple(lines)))

    # Pass 2: best-effort opponent via consecutive-row adjacency. Pairing is on
    # page order (before dropping line-less fighters) so a fighter keeps the
    # right opponent even if their partner is blank across every book.
    opponents: list[str | None] = [None] * len(parsed_rows)
    for index in range(0, len(parsed_rows) - 1, 2):
        opponents[index] = parsed_rows[index + 1][0]
        opponents[index + 1] = parsed_rows[index][0]

    rows: list[AllBooksFighterRow] = []
    for (fighter_name, lines), opponent in zip(parsed_rows, opponents):
        if not lines:
            # No book posted a line for this fighter; it still held a pairing
            # slot above so its opponent is correct, but there is nothing to emit.
            continue
        rows.append(
            AllBooksFighterRow(
                fighter_name=fighter_name,
                book_lines=lines,
                opponent=opponent,
                source_url=source_url,
                fetched_at=fetched_at,
            )
        )

    if not rows:
        raise BestFightOddsParseError(
            "BestFightOdds HTML has book columns but no fighter with a single "
            "valid moneyline was found."
        )
    return rows
