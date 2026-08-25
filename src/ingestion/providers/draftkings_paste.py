"""Pure DraftKings copied-board text → normalized moneyline rows (Phase 4).

Realizes ``docs/ODDS_ACQUISITION_V0_DESIGN.md`` §3 Phase 4 (the **paste / table
parser**): the user views the public DraftKings UFC odds board in their own
browser, copies the visible text, and pastes it in. This module turns that
copied text into the §1.7 normalized moneyline rows that feed the existing odds
pipeline — exactly like the BestFightOdds parser, but reading pasted plain text
rather than fetched HTML.

Why a paste path exists (design §1 note, ``docs/DEVELOPMENT_NOTES.md`` §3): DraftKings' public
odds JSON is WAF/403-blocked and browser / login automation is forbidden in v0.
The user *can* see the board in their own browser; this parser spares them
hand-converting that copied text into CSV/JSON before the app can use it.

Hard boundaries (design §1.9, §1.11, §1.12; ``docs/DEVELOPMENT_NOTES.md`` §3):

  - **No network.** This module parses a string the caller already holds. It
    imports nothing that opens a socket.
  - **No DB, no UI.** It reads nothing and writes nothing. Preview, save, and
    Streamlit wiring reuse the Phase 3 path and are out of scope here.
  - **DraftKings only.** Every emitted row's ``source`` and ``book`` is
    ``"DraftKings"`` — the copied board *is* the DraftKings line.

Failure is loud and specific. The parser raises
:class:`DraftKingsPasteParseError` — never returns silent empty rows — when the
text contains no fights or no recognizable moneylines at all. An individual
fight block that is present but malformed (e.g. a totals layout missing one
side's moneyline) is **skipped with a recorded warning** *provided at least one
other fight parsed*; the skip is surfaced on the result's ``warnings`` so it is
never silent.

The normalized output is :class:`DraftKingsParsedRow`, the §1.7 contract. It is
an *input* to the existing odds pipeline (``ODDS_NEWS_SNAPSHOT`` → ``odds_rows``
→ match → recompute); this pure parse only produces it.

Board layout assumptions (observed from a real copied DraftKings UFC board)
-------------------------------------------------------------------------------
Each fight is anchored by a ``vs`` line: the line above it is fighter A, the
line below it is fighter B. After the pairing, a fight block is one of:

* **Totals + moneyline** — the "Total Rounds" market is shown first::

      O
      1.5            <- over/under total (ignored)
      +120           <- price on the OVER (a totals bet, ignored)
      +525           <- fighter A moneyline
      U
      1.5            <- total (ignored)
      −154           <- price on the UNDER (a totals bet, ignored)
      −750           <- fighter B moneyline

  i.e. within the ``O`` segment the fighter's moneyline is the *second*
  American price (the first is the over/under total price); likewise within the
  ``U`` segment.

* **Moneyline only** — no totals offered for the fight::

      −142           <- fighter A moneyline
      +120           <- fighter B moneyline

  the two American prices following the pairing are A's then B's moneyline.

A fight that matches neither shape (e.g. only an ``O`` segment, or a totals
segment with a single price) is treated as incomplete and skipped with a
warning rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.ingestion.odds_csv_importer import parse_moneyline

# §1.7 source / book labels. A pasted DraftKings board *is* the DraftKings
# line, so both are constant.
SOURCE_DRAFTKINGS = "DraftKings"
BOOK_DRAFTKINGS = "DraftKings"

# §1.7 status / confidence. A clean American line read deterministically from
# the copied board is a full-confidence, parsed row.
STATUS_PARSED = "parsed"
DEFAULT_CONFIDENCE = 1.0

# Unicode minus (U+2212) — DraftKings renders negative odds with it rather than
# the ASCII hyphen-minus. ``parse_moneyline`` only understands ASCII, so we
# normalize before delegating to it.
_UNICODE_MINUS = "−"

# A pairing anchor: the literal vs token between the two fighter names.
_VS_TOKEN = "vs"

# The two markers that introduce the over / under legs of the Total Rounds
# market. They precede a totals price *and* the fighter moneyline.
_OVER_MARKER = "O"
_UNDER_MARKER = "U"

# An American moneyline / price token: an explicit sign (DraftKings always
# shows one) followed by digits, with no decimal point. The explicit-sign
# requirement is what separates a price ("+120", "−154") from a round total
# ("1.5", "2.5"), so totals are never mistaken for prices.
_AMERICAN_TOKEN = re.compile(rf"^[+\-{_UNICODE_MINUS}]\d+$")

# Lines that are board scaffolding, never a fighter name.
_NON_NAME_TOKENS = frozenset(
    {
        _VS_TOKEN,
        _OVER_MARKER,
        _UNDER_MARKER,
        "More Bets",
        "Moneyline",
        "Total Rounds",
    }
)


class DraftKingsPasteParseError(ValueError):
    """Raised when copied DraftKings text yields no usable moneyline rows.

    A subclass of :class:`ValueError` so existing ``except ValueError`` import
    handlers catch it, while callers that care can match it specifically.
    """


@dataclass(frozen=True)
class DraftKingsParsedRow:
    """One normalized moneyline parsed from a copied DraftKings board (§1.7).

    ``fighter_name`` and ``american_moneyline`` are always present. ``opponent``
    is the other side of the fight (the paste always pairs fighters via ``vs``).
    ``source_url`` and ``collected_at`` are pass-through provenance supplied by
    the caller — a pure paste parse has no URL and no clock of its own.
    """

    fighter_name: str
    american_moneyline: int
    opponent: str | None = None
    source: str = SOURCE_DRAFTKINGS
    book: str = BOOK_DRAFTKINGS
    source_url: str | None = None
    collected_at: str | None = None
    status: str = STATUS_PARSED
    confidence: float = DEFAULT_CONFIDENCE


@dataclass(frozen=True)
class DraftKingsPasteParseResult:
    """The outcome of a paste parse: the normalized rows plus any skip warnings.

    ``rows`` is the list of normalized §1.7 rows (two per successfully parsed
    fight, fighter A then fighter B). ``warnings`` records every fight block
    that was present but skipped as incomplete, so a partial parse is never
    silent (design "fail loudly / expose skips").
    """

    rows: list[DraftKingsParsedRow]
    warnings: list[str] = field(default_factory=list)


def _american(token: str) -> int | None:
    """Return the int moneyline for an American price token, else ``None``.

    Recognizes only explicitly-signed integers (so round totals like ``2.5``
    are rejected), normalizes the unicode minus to ASCII, and reuses the
    canonical :func:`parse_moneyline` to produce the int.
    """
    if not _AMERICAN_TOKEN.match(token):
        return None
    return parse_moneyline(token.replace(_UNICODE_MINUS, "-"))


def _is_name(token: str) -> bool:
    """True when ``token`` is plausibly a fighter name (not scaffolding/price)."""
    if not token or token in _NON_NAME_TOKENS:
        return False
    return _american(token) is None


def _moneylines_for_block(region: list[str]) -> tuple[int, int] | None:
    """Extract ``(fighter_a_ml, fighter_b_ml)`` from one fight's odds region.

    ``region`` is the lines after fighter B up to (but not including) the next
    fight's leading fighter name — it may carry trailing date / "More Bets"
    scaffolding, which is ignored. Returns ``None`` when the block is present
    but does not match either supported layout (caller records a warning).
    """
    pre: list[int] = []
    after_over: list[int] = []
    after_under: list[int] = []
    bucket = pre
    saw_over = False
    saw_under = False

    for line in region:
        if line == _OVER_MARKER:
            saw_over = True
            bucket = after_over
            continue
        if line == _UNDER_MARKER:
            saw_under = True
            bucket = after_under
            continue
        price = _american(line)
        if price is not None:
            bucket.append(price)

    if saw_over and saw_under:
        # Totals + moneyline: within each leg the fighter moneyline is the
        # second price (first price is the over/under total price).
        if len(after_over) >= 2 and len(after_under) >= 2:
            return after_over[1], after_under[1]
        return None

    if not saw_over and not saw_under:
        # Moneyline-only: the two prices after the pairing are A then B.
        if len(pre) >= 2:
            return pre[0], pre[1]
        return None

    # Exactly one of the totals markers present — a malformed/partial block.
    return None


def parse_draftkings_paste(
    text: str,
    *,
    source_url: str | None = None,
    collected_at: str | None = None,
) -> DraftKingsPasteParseResult:
    """Parse copied DraftKings UFC board text into normalized moneyline rows.

    Splits the text into fight blocks anchored on each ``vs`` line (fighter A is
    the line above, fighter B the line below), then reads each fighter's
    American moneyline per the layout assumptions documented in the module
    docstring. Ignores the Total Rounds (O/U) prices, supports both the
    totals+moneyline and moneyline-only layouts, supports unicode and ASCII
    minus signs, and pairs each fighter's ``opponent``.

    ``source_url`` and ``collected_at`` are recorded verbatim on every row as
    §1.7 provenance — the parser supplies neither itself.

    Returns a :class:`DraftKingsPasteParseResult` whose ``rows`` are the
    normalized §1.7 rows (two per parsed fight, fighter A then B) and whose
    ``warnings`` list every present-but-incomplete fight block that was skipped.

    Raises :class:`DraftKingsPasteParseError` (never a silent empty result)
    when the text has no fight pairings at all, or when every fight block was
    incomplete so that no valid moneyline row could be produced.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]

    vs_indices = [i for i, ln in enumerate(lines) if ln == _VS_TOKEN]
    if not vs_indices:
        raise DraftKingsPasteParseError(
            "Copied DraftKings text has no fight pairings; expected lines of "
            "the form '<Fighter A> / vs / <Fighter B>'. Make sure the visible "
            "odds board was copied, not an empty selection."
        )

    rows: list[DraftKingsParsedRow] = []
    warnings: list[str] = []

    for k, i in enumerate(vs_indices):
        fighter_a = lines[i - 1] if i - 1 >= 0 else ""
        fighter_b = lines[i + 1] if i + 1 < len(lines) else ""

        if not _is_name(fighter_a) or not _is_name(fighter_b):
            warnings.append(
                f"Skipped a 'vs' near '{fighter_a or '?'} vs {fighter_b or '?'}'"
                ": could not read both fighter names around the pairing."
            )
            continue

        # The block's odds run from after fighter B up to the next fight's
        # leading fighter name (the line just before the next 'vs').
        block_end = len(lines)
        if k + 1 < len(vs_indices):
            block_end = vs_indices[k + 1] - 1
        region = lines[i + 2 : block_end]

        moneylines = _moneylines_for_block(region)
        if moneylines is None:
            warnings.append(
                f"Skipped fight '{fighter_a} vs {fighter_b}': could not read "
                "both fighter moneylines (the odds block did not match the "
                "expected totals+moneyline or moneyline-only layout)."
            )
            continue

        ml_a, ml_b = moneylines
        rows.append(
            DraftKingsParsedRow(
                fighter_name=fighter_a,
                american_moneyline=ml_a,
                opponent=fighter_b,
                source_url=source_url,
                collected_at=collected_at,
            )
        )
        rows.append(
            DraftKingsParsedRow(
                fighter_name=fighter_b,
                american_moneyline=ml_b,
                opponent=fighter_a,
                source_url=source_url,
                collected_at=collected_at,
            )
        )

    if not rows:
        raise DraftKingsPasteParseError(
            "Copied DraftKings text had fight pairings but no readable "
            "moneylines; every fight block was incomplete. "
            + (" ".join(warnings) if warnings else "")
        )

    return DraftKingsPasteParseResult(rows=rows, warnings=warnings)
