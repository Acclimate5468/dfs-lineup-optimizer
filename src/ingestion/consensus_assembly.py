"""Assemble parsed multi-book rows into consensus inputs + provenance (Slice 5).

Bridges the two parsers (``parse_bestfightodds_all_books`` and
``parse_multi_book_paste``, design §5.1 / §5.2) — which emit **per-fighter,
one-sided** rows — into the **per-fight, two-sided** ``FightBookOdds`` the pure
consensus service (§5.3 / Slice 2) consumes, and into the flat per-(fighter,
book) provenance that ``odds_book_lines`` stores (§5.4).

Pure: no DB, no network, no clock. Two steps:

1. :func:`merge_sources` — normalize both sources' rows and merge them per
   fighter (keyed on the persistence ``normalize_name``). A book that appears in
   both sources for the same fighter is deduplicated **paste-wins** (the paste
   is the user's explicit fallback), so a (fighter, book) pair is unique — which
   is exactly the ``odds_book_lines`` UNIQUE(slate, fighter_normalized, book).
2. :func:`assemble_fights` — pair the merged fighters into fights by resolving
   each fighter's best-effort ``opponent`` to the partner whose normalized name
   matches, then build one :class:`~src.projections.odds_consensus.BookQuote`
   per book (each side's line, or ``None`` when a book priced only one fighter —
   the consensus service drops those). A fighter with no resolvable partner is
   reported in :attr:`AssemblyResult.unpaired`, never silently dropped.

Cross-source reconciliation is intentionally limited to exact normalized-name
equality; richer name matching is the existing odds matcher's job (run on the
synthesized consensus rows downstream), not this assembly step.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.projections.odds_consensus import BookQuote, FightBookOdds
from src.utils.text_cleaning import normalize_name

# Storage tokens for ``odds_book_lines.source`` (lowercase provenance origin),
# distinct from the parsers' display labels ("BestFightOdds" / "Paste") and from
# the synthesized ``odds_rows.source = "consensus"``.
SOURCE_BESTFIGHTODDS = "bestfightodds"
SOURCE_PASTE = "paste"


@dataclass(frozen=True)
class BookLineEntry:
    """One merged book line for a fighter, tagged with its winning source."""

    book: str
    american_odds: int
    source: str


@dataclass(frozen=True)
class FighterBookLines:
    """All merged book lines for one fighter (provenance + blend input)."""

    fighter_name: str
    fighter_name_normalized: str
    opponent: str | None
    lines: tuple[BookLineEntry, ...]


@dataclass(frozen=True)
class AssemblyResult:
    """Output of :func:`assemble_fights`.

    ``fights`` feeds ``compute_slate_consensus``; ``unpaired`` lists the raw
    names of fighters with no resolvable partner (surfaced, not silently
    dropped — design §9).
    """

    fights: list[FightBookOdds]
    unpaired: list[str]


def _merge_one_source(acc: dict, order: list, rows, source_token: str) -> None:
    """Fold one source's parser rows into the per-fighter accumulator.

    Later sources overwrite earlier ones for a shared (fighter, book) — so
    calling this with BestFightOdds first and paste second yields paste-wins
    dedup. ``acc`` maps normalized name -> {raw, opponent, books: {book: entry}}.
    """
    for row in rows:
        raw = (getattr(row, "fighter_name", "") or "").strip()
        norm = normalize_name(raw)
        if not norm:
            continue
        opponent = getattr(row, "opponent", None) or None
        entry = acc.get(norm)
        if entry is None:
            entry = {"raw": raw, "opponent": opponent, "books": {}}
            acc[norm] = entry
            order.append(norm)
        elif entry["opponent"] is None and opponent:
            entry["opponent"] = opponent
        for line in getattr(row, "book_lines", ()):
            book = (getattr(line, "book", "") or "").strip()
            if not book:
                continue
            entry["books"][book] = BookLineEntry(
                book=book,
                american_odds=int(line.american_moneyline),
                source=source_token,
            )


def merge_sources(
    *,
    bestfightodds_rows=(),
    paste_rows=(),
) -> list[FighterBookLines]:
    """Merge both consensus sources into per-fighter book lines (page order).

    Accepts the ``AllBooksFighterRow`` / ``MultiBookPasteRow`` lists from the two
    parsers (either may be empty). Fighters are keyed by ``normalize_name`` so
    the same fighter from both sources is one entry; a (fighter, book) collision
    is resolved paste-wins.
    """
    acc: dict = {}
    order: list = []
    _merge_one_source(acc, order, bestfightodds_rows or (), SOURCE_BESTFIGHTODDS)
    _merge_one_source(acc, order, paste_rows or (), SOURCE_PASTE)
    out: list[FighterBookLines] = []
    for norm in order:
        e = acc[norm]
        out.append(
            FighterBookLines(
                fighter_name=e["raw"],
                fighter_name_normalized=norm,
                opponent=e["opponent"],
                lines=tuple(e["books"].values()),
            )
        )
    return out


def _build_fight(a: FighterBookLines, b: FighterBookLines) -> FightBookOdds:
    a_by_book = {e.book: e.american_odds for e in a.lines}
    b_by_book = {e.book: e.american_odds for e in b.lines}
    # Union of books, A's order first then B-only books, stable + deterministic.
    books = list(a_by_book) + [bk for bk in b_by_book if bk not in a_by_book]
    quotes = tuple(
        BookQuote(
            book=bk,
            american_a=a_by_book.get(bk),
            american_b=b_by_book.get(bk),
        )
        for bk in books
    )
    return FightBookOdds(
        fighter_a=a.fighter_name,
        fighter_b=b.fighter_name,
        quotes=quotes,
    )


def assemble_fights(fighters: list[FighterBookLines]) -> AssemblyResult:
    """Pair merged fighters into fights via best-effort ``opponent`` adjacency.

    Each fighter's ``opponent`` is resolved to the partner whose normalized name
    matches; the pair becomes one :class:`FightBookOdds`. A fighter with no
    ``opponent``, or one whose opponent does not resolve to an available partner,
    is reported in :attr:`AssemblyResult.unpaired`.
    """
    by_norm = {f.fighter_name_normalized: f for f in fighters}
    used: set[str] = set()
    fights: list[FightBookOdds] = []
    unpaired: list[str] = []
    for f in fighters:
        if f.fighter_name_normalized in used:
            continue
        partner = None
        if f.opponent:
            partner = by_norm.get(normalize_name(f.opponent))
        if (
            partner is None
            or partner.fighter_name_normalized in used
            or partner.fighter_name_normalized == f.fighter_name_normalized
        ):
            used.add(f.fighter_name_normalized)
            unpaired.append(f.fighter_name)
            continue
        used.add(f.fighter_name_normalized)
        used.add(partner.fighter_name_normalized)
        fights.append(_build_fight(f, partner))
    return AssemblyResult(fights=fights, unpaired=unpaired)
