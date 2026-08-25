"""Unit tests for the multi-book odds consensus blend (Slice 2).

Hand-computed fixtures for ``src/projections/odds_consensus.py``
(``docs/ODDS_CONSENSUS_DESIGN.md`` §2 / §13). Pure math — no DB, no UI.
"""

from __future__ import annotations

import pytest

from src.projections.implied_probability import american_to_implied_probability
from src.projections.odds_consensus import (
    MIN_BOOKS_DEFAULT,
    BookQuote,
    FightBookOdds,
    compute_fight_consensus,
    compute_slate_consensus,
    probability_to_fair_american,
)


# ---------------------------------------------------------------------------
# probability_to_fair_american
# ---------------------------------------------------------------------------


def test_probability_to_fair_american_known_values():
    assert probability_to_fair_american(0.5) == -100
    assert probability_to_fair_american(0.6) == -150
    assert probability_to_fair_american(0.4) == 150


def test_probability_to_fair_american_round_trips():
    for p in (0.25, 0.4789, 0.52, 0.7707):
        ml = probability_to_fair_american(p)
        assert american_to_implied_probability(ml) == pytest.approx(p, abs=5e-3)


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.1])
def test_probability_to_fair_american_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        probability_to_fair_american(bad)


# ---------------------------------------------------------------------------
# compute_fight_consensus — core blend
# ---------------------------------------------------------------------------


def test_pickem_two_books():
    """Two near-even books blend to ~0.49/0.51; full confidence at 2 books."""
    fight = FightBookOdds(
        fighter_a="A",
        fighter_b="B",
        quotes=(
            BookQuote("book1", -110, -110),
            BookQuote("book2", +100, -120),
        ),
    )
    r = compute_fight_consensus(fight)
    assert r.book_count == 2
    assert r.books_dropped == 0
    assert r.low_confidence is False
    assert r.prob_a == pytest.approx(0.4891, abs=2e-3)
    assert r.prob_a + r.prob_b == pytest.approx(1.0)
    # p < 0.5 -> positive fair line, round-trips to the consensus prob.
    assert r.fair_american_a > 0
    assert american_to_implied_probability(r.fair_american_a) == pytest.approx(
        r.prob_a, abs=5e-3
    )


def test_median_ignores_outlier_book():
    """Median (not mean) drives the blend: one book pricing A as a much bigger
    favorite must not drag the consensus up to the mean."""
    fight = FightBookOdds(
        fighter_a="Fav",
        fighter_b="Dog",
        quotes=(
            BookQuote("b1", -200, +170),
            BookQuote("b2", -210, +175),
            BookQuote("b3", -400, +320),  # outlier favorite
        ),
    )
    r = compute_fight_consensus(fight)
    # Per-book no-vig A probs ~ [0.6429, 0.6507, 0.7706]; median 0.6507,
    # mean ~0.6881. The result must track the median, not the mean.
    assert r.prob_a == pytest.approx(0.6507, abs=2e-3)
    assert r.prob_a < 0.67, "should sit at the median, well below the 0.688 mean"
    assert r.dispersion == pytest.approx(0.7706 - 0.6429, abs=3e-3)


def test_devig_is_per_book_before_blend():
    """Each book is de-vigged on its own before the median, so a symmetric
    -110/-110 book contributes exactly 0.5, not its 0.524 raw implied prob."""
    fight = FightBookOdds(
        "A", "B", quotes=(BookQuote("b1", -110, -110), BookQuote("b2", -110, -110))
    )
    r = compute_fight_consensus(fight)
    assert r.prob_a == pytest.approx(0.5, abs=1e-9)
    assert r.dispersion == pytest.approx(0.0, abs=1e-9)


def test_real_card_chairez_is_an_underdog():
    """7-book real fight-week data: DraftKings alone had Chairez ~+102 (~50%),
    but the multi-book consensus puts him ~0.466 — a clear underdog. This is the
    exact single-book-vs-consensus discrepancy the feature exists to fix."""
    fight = FightBookOdds(
        fighter_a="Edgar Chairez",
        fighter_b="Bruno Silva",
        quotes=(
            BookQuote("Polymarket", +108, -113),
            BookQuote("Kalshi", +101, -126),
            BookQuote("FanDuel", +104, -128),
            BookQuote("Caesars", +105, -125),
            BookQuote("BetRivers", +110, -137),
            BookQuote("BetWay", +110, -138),
            BookQuote("BetMGM", +110, -137),
        ),
    )
    r = compute_fight_consensus(fight)
    assert r.book_count == 7
    assert r.low_confidence is False
    assert r.prob_a == pytest.approx(0.466, abs=3e-3)
    assert r.prob_a < 0.48, "consensus is below the +5 value-gap threshold"
    assert r.prob_a + r.prob_b == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# MIN_BOOKS / confidence / dropped quotes
# ---------------------------------------------------------------------------


def test_single_book_is_low_confidence_but_still_computed():
    fight = FightBookOdds(
        "A", "B", quotes=(BookQuote("only", -150, +130),)
    )
    r = compute_fight_consensus(fight)
    assert r.book_count == 1
    assert r.low_confidence is True  # 1 < MIN_BOOKS_DEFAULT (2)
    assert r.prob_a == pytest.approx(0.5798, abs=2e-3)
    assert any("Low confidence" in n for n in r.notes)


def test_one_sided_quotes_are_dropped_not_blended():
    fight = FightBookOdds(
        "A",
        "B",
        quotes=(
            BookQuote("full", -150, +130),
            BookQuote("a_only", -150, None),
            BookQuote("b_only", None, +140),
        ),
    )
    r = compute_fight_consensus(fight)
    assert r.book_count == 1
    assert r.books_dropped == 2
    assert r.low_confidence is True
    assert any("dropped" in n for n in r.notes)


def test_no_two_sided_book_yields_no_consensus():
    fight = FightBookOdds(
        "A", "B", quotes=(BookQuote("a_only", -150, None),)
    )
    r = compute_fight_consensus(fight)
    assert r.book_count == 0
    assert r.prob_a is None and r.prob_b is None
    assert r.fair_american_a is None and r.fair_american_b is None
    assert r.low_confidence is True


def test_empty_quotes_yields_no_consensus():
    r = compute_fight_consensus(FightBookOdds("A", "B", quotes=()))
    assert r.book_count == 0
    assert r.prob_a is None


def test_min_books_is_configurable():
    fight = FightBookOdds(
        "A", "B", quotes=(BookQuote("b1", -110, -110), BookQuote("b2", -110, -110))
    )
    # 2 books clears the default, but a stricter min flags it.
    assert compute_fight_consensus(fight).low_confidence is False
    assert compute_fight_consensus(fight, min_books=3).low_confidence is True


def test_min_books_default_is_two():
    assert MIN_BOOKS_DEFAULT == 2


# ---------------------------------------------------------------------------
# slate convenience
# ---------------------------------------------------------------------------


def test_compute_slate_consensus_preserves_order():
    fights = [
        FightBookOdds("A1", "B1", quotes=(BookQuote("x", -200, +170),)),
        FightBookOdds("A2", "B2", quotes=(BookQuote("x", -110, -110),
                                          BookQuote("y", -120, +100))),
    ]
    results = compute_slate_consensus(fights)
    assert [r.fighter_a for r in results] == ["A1", "A2"]
    assert results[0].low_confidence is True   # 1 book
    assert results[1].low_confidence is False  # 2 books
