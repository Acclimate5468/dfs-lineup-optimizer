"""Unit tests for the Captain consensus odds path (slice C8).

Pins the pure wiring in ``app.captain_build`` that realizes
``docs/CAPTAIN_MODE_DESIGN.md`` §13 C8: paste blocks → the **reused** validated
Classic odds modules → de-vigged median consensus win probs → mapped to Captain
fighters by ``normalize_name`` → resolved per fighter (consensus preferred,
manual moneyline fallback). No Streamlit, no DB, no network — every block is
SYNTHETIC (``docs/DEVELOPMENT_NOTES.md`` §7).

Coverage:
- **Parse + consensus wiring** — a synthetic BestFightOdds HTML block + a
  synthetic multi-book grid blend into per-fighter consensus win probs; the
  numbers are asserted **against ``odds_consensus`` output** (not re-derived).
- **Name mapping** — Captain fighters are matched to consensus by
  ``normalize_name``; a consensus fighter that matches no slate fighter and an
  unpaired fighter are both reported, never silently dropped.
- **Resolution precedence** — consensus is used where available, the manual
  moneyline is the fallback, and each fighter's source is reported.
- **Robustness** — an unparseable paste is surfaced as a warning, never raised,
  and the other source still contributes.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.captain_build import (
    ConsensusFighterPrice,
    compute_captain_consensus,
    resolve_win_probs,
)
from src.projections.odds_consensus import (
    BookQuote,
    FightBookOdds,
    compute_fight_consensus,
)
from src.utils.text_cleaning import normalize_name


# ---------------------------------------------------------------------------
# Synthetic paste builders (no real odds — docs/DEVELOPMENT_NOTES.md §7).
# ---------------------------------------------------------------------------


def _bfo_html(rows: list[tuple[str, list[int]]], books: list[str]) -> str:
    """A synthetic BestFightOdds all-books event table.

    Each fighter row's leading cell links to a ``/fighters/`` page (the
    real-feed discriminator the all-books parser keys on) and carries one signed
    American line per book column. The header includes a DraftKings column (the
    grid anchor). Positive odds are signed (``+170``) as a real grid renders.
    """
    head = "".join(f"<th>{b}</th>" for b in books)
    body = ""
    for index, (name, lines) in enumerate(rows):
        cells = "".join(f"<td>{ml:+d}</td>" for ml in lines)
        body += (
            f'<tr><td><a href="/fighters/{index}-x">{name}</a></td>{cells}</tr>'
        )
    return f"<table><tr><th>Matchup</th>{head}</tr>{body}</table>"


def _grid(rows: list[tuple[str, list[int | None]]], books: list[str]) -> str:
    """A synthetic tab-delimited multi-book paste grid (header + fighter rows)."""
    lines = ["Matchup\t" + "\t".join(books)]
    for name, mls in rows:
        cells = "\t".join("" if m is None else f"{m:+d}" for m in mls)
        lines.append(f"{name}\t{cells}")
    return "\n".join(lines)


@dataclass(frozen=True)
class _Fighter:
    """A minimal stand-in for ``CaptainFighter`` (only ``name`` is read)."""

    name: str
    base_salary: int = 8000
    captain_salary: int = 12000
    is_out: bool = False


@dataclass(frozen=True)
class _Bout:
    fighter_1_name: str
    fighter_2_name: str


# ---------------------------------------------------------------------------
# Parse + consensus wiring
# ---------------------------------------------------------------------------


def test_consensus_wiring_matches_odds_consensus_output():
    """A BFO block + a multi-book grid blend into per-fighter consensus win
    probs equal to ``odds_consensus``'s own output (assert against it; the math
    is not re-derived here)."""
    bfo = _bfo_html(
        [("Alpha One", [-200, -210]), ("Beta Two", [170, 175])],
        ["DraftKings", "FanDuel"],
    )
    grid = _grid(
        [("Gamma Three", [-150, -155]), ("Delta Four", [130, 135])],
        ["DraftKings", "BetMGM"],
    )

    result = compute_captain_consensus(bestfightodds_text=bfo, multibook_text=grid)

    # Both fights, both sides, are priced; nothing unpaired or warned.
    assert set(result.by_normalized) == {
        normalize_name(n)
        for n in ("Alpha One", "Beta Two", "Gamma Three", "Delta Four")
    }
    assert result.unpaired == []
    assert result.parse_warnings == []
    assert result.fights_considered == 2

    # Assert the BFO fight's numbers against compute_fight_consensus directly.
    expected = compute_fight_consensus(
        FightBookOdds(
            fighter_a="Alpha One",
            fighter_b="Beta Two",
            quotes=(
                BookQuote("DraftKings", -200, 170),
                BookQuote("FanDuel", -210, 175),
            ),
        )
    )
    alpha = result.by_normalized[normalize_name("Alpha One")]
    beta = result.by_normalized[normalize_name("Beta Two")]
    assert alpha.win_prob == pytest.approx(expected.prob_a)
    assert beta.win_prob == pytest.approx(expected.prob_b)
    assert alpha.book_count == expected.book_count == 2
    assert alpha.low_confidence is False
    assert alpha.dispersion == pytest.approx(expected.dispersion)


def test_single_book_is_priced_but_low_confidence():
    """A one-book grid still yields a consensus value, flagged low_confidence
    (the same posture as the Classic blend — surfaced, not blocked)."""
    grid = _grid(
        [("Bo Nickal", [-150]), ("Kyle Daukaus", [130])],
        ["DraftKings"],
    )
    result = compute_captain_consensus(multibook_text=grid)

    bo = result.by_normalized[normalize_name("Bo Nickal")]
    assert bo.book_count == 1
    assert bo.low_confidence is True
    # Probabilities still sum to ~1 across the pair.
    kyle = result.by_normalized[normalize_name("Kyle Daukaus")]
    assert bo.win_prob + kyle.win_prob == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Name mapping + surfacing (never silently drop)
# ---------------------------------------------------------------------------


def test_unpaired_fighter_is_reported_not_dropped():
    """A fighter with no resolvable opponent in the paste is reported in
    ``unpaired`` and is absent from the priced map (never silently dropped)."""
    grid = _grid(
        [("Bo Nickal", [-150]), ("Kyle Daukaus", [130]), ("Lone Wolf", [200])],
        ["DraftKings"],
    )
    result = compute_captain_consensus(multibook_text=grid)

    assert "Lone Wolf" in result.unpaired
    assert normalize_name("Lone Wolf") not in result.by_normalized


def test_name_mapping_matches_by_normalized_name():
    """Mixed-case / accented slate names map to consensus via normalize_name."""
    bfo = _bfo_html(
        [("José Aldo", [-200]), ("Petr Yan", [170])],
        ["DraftKings"],
    )
    result = compute_captain_consensus(bestfightodds_text=bfo)

    eligible = [_Fighter("JOSE ALDO"), _Fighter("Petr Yan")]
    bouts = [_Bout("JOSE ALDO", "Petr Yan")]
    resolution = resolve_win_probs(eligible, bouts, {}, result.by_normalized)

    assert resolution.uncovered == []
    assert resolution.resolved["JOSE ALDO"].source == "consensus"
    assert resolution.resolved["Petr Yan"].source == "consensus"


# ---------------------------------------------------------------------------
# Resolution precedence (consensus preferred, manual fallback)
# ---------------------------------------------------------------------------


def test_resolution_prefers_consensus_over_manual():
    """When a fighter has BOTH a consensus price and a manual moneyline, the
    consensus value wins; a fighter consensus did not price uses the manual
    moneyline; each fighter's source is reported."""
    consensus = {
        normalize_name("Gamma Three"): ConsensusFighterPrice(
            fighter_name="Gamma Three",
            normalized=normalize_name("Gamma Three"),
            win_prob=0.62,
            book_count=2,
            dispersion=0.01,
            low_confidence=False,
        )
    }
    eligible = [
        _Fighter("Gamma Three"),
        _Fighter("Manual A"),
        _Fighter("Manual B"),
    ]
    bouts = [_Bout("Gamma Three", "Manual A"), _Bout("Manual B", "Gamma Three")]
    # Manual moneylines exist for everyone, including the consensus-priced fighter.
    moneylines = {"Gamma Three": -300, "Manual A": 200, "Manual B": -150}

    resolution = resolve_win_probs(eligible, bouts, moneylines, consensus)

    assert resolution.uncovered == []
    assert resolution.errors == []
    # Consensus wins for Gamma despite the manual -300 present.
    assert resolution.resolved["Gamma Three"].source == "consensus"
    assert resolution.resolved["Gamma Three"].win_prob == pytest.approx(0.62)
    # The others fall back to the manual moneyline.
    assert resolution.resolved["Manual A"].source == "manual"
    assert resolution.resolved["Manual B"].source == "manual"


def test_fighter_priced_by_neither_source_is_uncovered():
    """A fighter with no consensus price and no de-viggable manual bout is
    reported uncovered (gates the build), never silently zeroed."""
    eligible = [_Fighter("Solo")]
    bouts = [_Bout("Solo", "Absent Partner")]
    # Only Solo has a moneyline → the bout cannot de-vig (partner missing).
    resolution = resolve_win_probs(eligible, bouts, {"Solo": -150}, {})

    assert resolution.uncovered == ["Solo"]
    assert "Solo" not in resolution.resolved


# ---------------------------------------------------------------------------
# Robustness: a bad paste is surfaced, not raised
# ---------------------------------------------------------------------------


def test_unparseable_bfo_is_warned_and_other_source_survives():
    """An unparseable BestFightOdds paste is recorded as a warning (never
    raised) and the multi-book grid still produces consensus."""
    grid = _grid(
        [("Gamma Three", [-150, -155]), ("Delta Four", [130, 135])],
        ["DraftKings", "BetMGM"],
    )
    result = compute_captain_consensus(
        bestfightodds_text="this is not odds HTML", multibook_text=grid
    )

    assert any("BestFightOdds" in w for w in result.parse_warnings)
    assert normalize_name("Gamma Three") in result.by_normalized
    assert normalize_name("Delta Four") in result.by_normalized


def test_empty_paste_yields_empty_result_no_error():
    """No paste text at all yields an empty (but well-formed) result."""
    result = compute_captain_consensus(bestfightodds_text="", multibook_text="  ")
    assert result.by_normalized == {}
    assert result.unpaired == []
    assert result.parse_warnings == []
    assert result.fights_considered == 0
