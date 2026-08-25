"""Multi-book odds consensus — pure blend math (Slice 2).

Implements the blending method of ``docs/ODDS_CONSENSUS_DESIGN.md`` §2: for a
fight (fighter pair A vs B), take each book's two-sided American moneylines,
**de-vig each book independently** (strip that book's own margin first), then
take the **median** across books per fighter and renormalize the pair to sum to
1. Median (not mean) is robust to one stale / outlier book; de-vig-first (not
blend-then-de-vig) is the more correct ordering.

Pure end to end: no DB, no Streamlit, no network. Reuses the existing
``american_pair_to_no_vig`` so the de-vig is identical to the rest of the
pipeline. Persistence, parsing, and UI are separate slices (§10) and import this
module; nothing here writes or fetches anything.

Confirmed design decisions realized here:

- ``MIN_BOOKS`` default **2**: a fight with fewer usable books still produces a
  value but is flagged ``low_confidence`` (user decision: "require >=2, else
  flag" — surfaced, not silently treated as consensus, not hard-blocked).
- **Equal-weight median**; sharp-book weighting and Shin's de-vig are deferred
  (design §12).
- Blending happens in probability space only — raw American odds are never
  averaged (design §2).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from src.projections.implied_probability import american_pair_to_no_vig

# Minimum number of books with a two-sided price required before a fight's
# consensus is considered full-confidence (design §2 / §11, user-confirmed).
MIN_BOOKS_DEFAULT = 2


@dataclass(frozen=True)
class BookQuote:
    """One book's two-sided American moneyline for a single fight.

    ``american_a`` / ``american_b`` are that book's lines for fighter A and
    fighter B. Either may be ``None`` when the book has no line for that side;
    a quote missing either side cannot be de-vigged and is excluded from the
    blend (counted in :attr:`ConsensusResult.books_dropped`).
    """

    book: str
    american_a: int | None
    american_b: int | None


@dataclass(frozen=True)
class FightBookOdds:
    """All collected book quotes for one fight (fighter A vs fighter B)."""

    fighter_a: str
    fighter_b: str
    quotes: tuple[BookQuote, ...] = ()


@dataclass(frozen=True)
class ConsensusResult:
    """Per-fight consensus output (design §2 step 6).

    ``prob_a`` / ``prob_b`` are the consensus no-vig win probabilities (summing
    to 1) or ``None`` when no book had a two-sided price. ``fair_american_*`` is
    the fair line for each probability, for downstream storage in
    ``odds_rows.american_odds`` (Slice 5). ``book_count`` is the number of books
    that contributed (had both sides). ``dispersion`` is the spread (max - min)
    of the per-book no-vig probabilities for fighter A — a book-disagreement
    signal, symmetric for B. ``low_confidence`` is ``True`` when fewer than
    ``min_books`` books contributed.
    """

    fighter_a: str
    fighter_b: str
    prob_a: float | None
    prob_b: float | None
    fair_american_a: int | None
    fair_american_b: int | None
    book_count: int
    books_dropped: int
    dispersion: float | None
    low_confidence: bool
    notes: tuple[str, ...] = field(default_factory=tuple)


def probability_to_fair_american(probability: float) -> int:
    """Convert a win probability in (0, 1) to a fair (no-vig) American line.

    Inverse of ``american_to_implied_probability`` for the vig-free case:
        p >= 0.5 -> negative line  -100 * p / (1 - p)
        p <  0.5 -> positive line  +100 * (1 - p) / p

    Rounded to the nearest integer. Raises if ``probability`` is not strictly
    inside (0, 1) — a consensus probability is always a renormalized blend and
    never exactly 0 or 1.
    """
    p = float(probability)
    if not (0.0 < p < 1.0):
        raise ValueError("probability must be strictly between 0 and 1")
    if p >= 0.5:
        return round(-100.0 * p / (1.0 - p))
    return round(100.0 * (1.0 - p) / p)


def compute_fight_consensus(
    fight: FightBookOdds, *, min_books: int = MIN_BOOKS_DEFAULT
) -> ConsensusResult:
    """Blend one fight's book quotes into a consensus (design §2).

    Steps: de-vig each two-sided book quote independently, take the median of
    the per-book no-vig probabilities for each fighter, then renormalize the
    pair to sum to 1. Quotes missing either side are dropped (not de-viggable).
    """
    probs_a: list[float] = []
    probs_b: list[float] = []
    dropped = 0
    for q in fight.quotes:
        if q.american_a is None or q.american_b is None:
            dropped += 1
            continue
        # De-vig this book's pair on its own, before any cross-book blend.
        p_a, p_b = american_pair_to_no_vig(q.american_a, q.american_b)
        probs_a.append(p_a)
        probs_b.append(p_b)

    book_count = len(probs_a)
    notes: list[str] = []
    if dropped:
        notes.append(
            f"{dropped} book quote(s) dropped (only one side priced)."
        )

    if book_count == 0:
        notes.append("No book had a two-sided price; no consensus computed.")
        return ConsensusResult(
            fighter_a=fight.fighter_a,
            fighter_b=fight.fighter_b,
            prob_a=None,
            prob_b=None,
            fair_american_a=None,
            fair_american_b=None,
            book_count=0,
            books_dropped=dropped,
            dispersion=None,
            low_confidence=True,
            notes=tuple(notes),
        )

    median_a = statistics.median(probs_a)
    median_b = statistics.median(probs_b)
    # The two medians need not sum to 1 (each is a median of a different set);
    # renormalize so the consensus pair is internally consistent / vig-free.
    total = median_a + median_b
    prob_a = median_a / total
    prob_b = median_b / total
    dispersion = max(probs_a) - min(probs_a)

    low_confidence = book_count < min_books
    if low_confidence:
        notes.append(
            f"Low confidence: {book_count} book(s) priced both sides "
            f"(min {min_books}). Treat as needs-attention, not consensus."
        )

    return ConsensusResult(
        fighter_a=fight.fighter_a,
        fighter_b=fight.fighter_b,
        prob_a=prob_a,
        prob_b=prob_b,
        fair_american_a=probability_to_fair_american(prob_a),
        fair_american_b=probability_to_fair_american(prob_b),
        book_count=book_count,
        books_dropped=dropped,
        dispersion=dispersion,
        low_confidence=low_confidence,
        notes=tuple(notes),
    )


def compute_slate_consensus(
    fights: list[FightBookOdds], *, min_books: int = MIN_BOOKS_DEFAULT
) -> list[ConsensusResult]:
    """Convenience: consensus for every fight on a slate (pure, order-preserving)."""
    return [compute_fight_consensus(f, min_books=min_books) for f in fights]
