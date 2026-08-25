"""Method-of-victory (MOV) finish signal for Captain mode (pure).

Realizes `docs/CAPTAIN_MODE_DESIGN.md` §14.1 — the **finish signal**, a
de-vigged estimate of each fighter's probability of *winning inside the
distance*, priced from method-of-victory odds. It is the input the C10
finish-aware method will turn into a projection bonus (``finish_bonus = K *
finish_signal``); this slice computes the **signal only**.

Additive and Classic-safe (`docs/DEVELOPMENT_NOTES.md` §3): a new pure module under
`src/captain/` that **reuses** the Classic odds-conversion helpers by importing
:func:`~src.projections.implied_probability.american_to_implied_probability`
and :func:`~src.projections.implied_probability.american_pair_to_no_vig` — it
never re-implements the American → implied conversion. No I/O: no DB, no
Streamlit, no network, no file writes. Deterministic.

The signal and its fallback ladder (§14.1), tried in order:

  - **Tier 0 — method-of-victory tree (primary).** Each fighter has American
    odds to win by KO/TKO, Submission, or Decision. Convert all *six* outcomes
    in the bout {A, B} to implied probabilities, let ``S`` be their sum (the
    six-way de-vig denominator), and::

        finish_signal(f) = (implied(KO_f) + implied(Sub_f)) / S

    i.e. the de-vigged probability that ``f`` wins *inside the distance*.

  - **Tier 1 — "fight to go the distance".** When a bout lacks the MOV tree,
    de-vig the bout-level No/Yes market and weight by the fighter's moneyline
    win probability::

        finish_signal(f) = P(does NOT go the distance) * win_prob(f)

  - **Tier 2 — round-total Under/Over.** Failing that, de-vig Under/Over and::

        finish_signal(f) = P(Under) * win_prob(f)

**The tier used is recorded** on the result (:class:`FinishSignalTier`, carried
on every :class:`FighterFinishSignal`) so the UI can surface how each bout was
priced. The moneyline ``win_prob`` is taken as **input** — this module never
converts the moneyline into a method-implied figure (design §14.2 keeps
``win_prob`` the moneyline de-vig). If a bout has neither a complete MOV tree
nor a usable fallback market, :class:`FinishSignalError` is raised — the signal
is **never** silently fabricated.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from src.projections.implied_probability import (
    american_pair_to_no_vig,
    american_to_implied_probability,
)


class FinishSignalError(ValueError):
    """Raised on malformed or missing required finish-signal odds (§14.1).

    The module never fabricates a finish signal: if a bout has neither a
    complete method-of-victory tree nor a usable fallback market — or the odds
    it does carry are malformed (zero / non-numeric) — this is raised rather
    than guessing. Subclasses :class:`ValueError` so existing odds-pipeline
    error handling still catches it.
    """


class FinishSignalTier(enum.Enum):
    """Which rung of the §14.1 ladder produced a bout's finish signal.

    Recorded on the result so the UI can show *how* each bout was priced —
    a real per-fighter method tree is far stronger evidence than a bout-level
    fallback weighted by the moneyline.
    """

    METHOD_OF_VICTORY = "method_of_victory"  # tier 0 — the six-outcome MOV tree
    DISTANCE = "distance"  # tier 1 — go-the-distance No/Yes × win_prob
    ROUND_TOTAL = "round_total"  # tier 2 — round-total Under/Over × win_prob


@dataclass(frozen=True)
class MethodOfVictoryOdds:
    """A fighter's American odds to win by KO/TKO, Submission, or Decision.

    A pure value holder; the odds are validated (non-zero, numeric) only when
    consumed by :func:`compute_finish_signals`, so a malformed tree surfaces as
    a :class:`FinishSignalError` at compute time rather than at construction.
    """

    ko_tko: int | float
    submission: int | float
    decision: int | float


@dataclass(frozen=True)
class FinishOddsBout:
    """All finish-signal inputs for one bout {A, B} (§14.1).

    ``win_prob_a`` / ``win_prob_b`` are the already-de-vigged **moneyline** win
    probabilities (this module never de-vigs the moneyline; that is the odds
    pipeline). They are required because the fallback tiers weight by them; the
    primary MOV tier ignores them. The MOV trees are the primary signal; the two
    bout-level fallback markets feed the §14.1 ladder when the MOV trees are
    absent.

    Fields beyond the two names + two win probs are optional; which tier is used
    is decided by which markets are present (highest available wins):

      - ``mov_a`` / ``mov_b`` — the method-of-victory trees (tier 0). **Both**
        must be present to use tier 0; if either is ``None`` the bout "lacks the
        MOV tree" and the ladder falls through.
      - ``distance_no`` / ``distance_yes`` — the "fight to go the distance"
        market (tier 1). Both required to use tier 1.
      - ``round_total_under`` / ``round_total_over`` — the round-total market
        (tier 2). Both required to use tier 2.
    """

    fighter_a: str
    fighter_b: str
    win_prob_a: float
    win_prob_b: float
    mov_a: MethodOfVictoryOdds | None = None
    mov_b: MethodOfVictoryOdds | None = None
    distance_no: int | float | None = None
    distance_yes: int | float | None = None
    round_total_under: int | float | None = None
    round_total_over: int | float | None = None

    def __post_init__(self) -> None:
        for label, name in (
            ("fighter_a", self.fighter_a),
            ("fighter_b", self.fighter_b),
        ):
            if not name or not str(name).strip():
                raise FinishSignalError(
                    f"FinishOddsBout.{label} must be a non-empty fighter name."
                )
        for label, prob in (
            ("win_prob_a", self.win_prob_a),
            ("win_prob_b", self.win_prob_b),
        ):
            try:
                value = float(prob)
            except (TypeError, ValueError) as exc:
                raise FinishSignalError(
                    f"FinishOddsBout.{label} must be numeric; got {prob!r}."
                ) from exc
            if not 0.0 <= value <= 1.0:
                raise FinishSignalError(
                    f"FinishOddsBout.{label} must be a de-vigged win probability "
                    f"in [0, 1]; got {prob!r}."
                )


@dataclass(frozen=True)
class FighterFinishSignal:
    """One fighter's finish signal plus the tier that produced it (§14.1).

    ``finish_signal`` is the de-vigged probability that the fighter wins inside
    the distance; ``tier`` records which rung of the ladder priced it.
    """

    name: str
    finish_signal: float
    tier: FinishSignalTier


@dataclass(frozen=True)
class BoutFinishSignal:
    """Both fighters' finish signals for a bout, sharing the tier used."""

    fighter_a: FighterFinishSignal
    fighter_b: FighterFinishSignal

    @property
    def tier(self) -> FinishSignalTier:
        """The ladder tier used for this bout (shared by both fighters)."""
        return self.fighter_a.tier


def _implied(odds: object, *, fighter: str, outcome: str) -> float:
    """Convert one American price to implied prob; raise a typed, labelled error.

    Reuses the Classic conversion helper (never re-implemented); a missing or
    malformed price (``None`` / zero / non-numeric) becomes a
    :class:`FinishSignalError` naming the fighter and outcome, so the signal is
    never fabricated from bad input.
    """
    if odds is None:
        raise FinishSignalError(
            f"missing {outcome} odds for {fighter!r} (method-of-victory tree)."
        )
    try:
        return american_to_implied_probability(odds)  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        raise FinishSignalError(
            f"invalid {outcome} odds for {fighter!r}: {odds!r} ({exc})."
        ) from exc


def _devig_finish_fraction(
    finish_side: object, distance_side: object, *, market: str
) -> float:
    """De-vig a two-way market and return the *finish* side's fair probability.

    ``finish_side`` is the price that implies a finish (go-the-distance **No**,
    or round-total **Under**); ``distance_side`` its complement. Reuses the
    Classic two-way no-vig helper; malformed prices raise a typed error.
    """
    try:
        finish_fair, _distance_fair = american_pair_to_no_vig(
            finish_side,  # type: ignore[arg-type]
            distance_side,  # type: ignore[arg-type]
        )
    except (ValueError, TypeError) as exc:
        raise FinishSignalError(
            f"invalid {market} odds: "
            f"{finish_side!r} / {distance_side!r} ({exc})."
        ) from exc
    return finish_fair


def _result(
    bout: FinishOddsBout,
    finish_a: float,
    finish_b: float,
    tier: FinishSignalTier,
) -> BoutFinishSignal:
    return BoutFinishSignal(
        fighter_a=FighterFinishSignal(bout.fighter_a, finish_a, tier),
        fighter_b=FighterFinishSignal(bout.fighter_b, finish_b, tier),
    )


def compute_finish_signals(bout: FinishOddsBout) -> BoutFinishSignal:
    """Compute both fighters' finish signals for a bout via the §14.1 ladder.

    Selects the highest available tier — a complete method-of-victory tree
    (tier 0) before the go-the-distance market (tier 1) before the round-total
    market (tier 2) — records which tier was used, and never fabricates a
    signal: a bout with no usable market raises :class:`FinishSignalError`.
    """
    if bout.mov_a is not None and bout.mov_b is not None:
        return _tier_method_of_victory(bout)
    if bout.distance_no is not None and bout.distance_yes is not None:
        return _tier_distance(bout)
    if bout.round_total_under is not None and bout.round_total_over is not None:
        return _tier_round_total(bout)
    raise FinishSignalError(
        f"no finish-signal odds for bout {bout.fighter_a!r} vs "
        f"{bout.fighter_b!r}: provide a method-of-victory tree (both fighters), "
        f"a go-the-distance market, or a round-total market."
    )


def _tier_method_of_victory(bout: FinishOddsBout) -> BoutFinishSignal:
    """Tier 0 — six-outcome MOV de-vig: (KO_f + Sub_f) / S (§14.1)."""
    a, b = bout.mov_a, bout.mov_b
    assert a is not None and b is not None  # guaranteed by the caller's guard
    ko_a = _implied(a.ko_tko, fighter=bout.fighter_a, outcome="KO/TKO")
    sub_a = _implied(a.submission, fighter=bout.fighter_a, outcome="Submission")
    dec_a = _implied(a.decision, fighter=bout.fighter_a, outcome="Decision")
    ko_b = _implied(b.ko_tko, fighter=bout.fighter_b, outcome="KO/TKO")
    sub_b = _implied(b.submission, fighter=bout.fighter_b, outcome="Submission")
    dec_b = _implied(b.decision, fighter=bout.fighter_b, outcome="Decision")

    total = ko_a + sub_a + dec_a + ko_b + sub_b + dec_b
    if total <= 0:
        # Defensive: each implied prob is > 0, so the sum is always positive.
        raise FinishSignalError(
            f"method-of-victory odds for {bout.fighter_a!r} vs "
            f"{bout.fighter_b!r} sum to a non-positive total."
        )
    finish_a = (ko_a + sub_a) / total
    finish_b = (ko_b + sub_b) / total
    return _result(bout, finish_a, finish_b, FinishSignalTier.METHOD_OF_VICTORY)


def _tier_distance(bout: FinishOddsBout) -> BoutFinishSignal:
    """Tier 1 — P(does NOT go the distance) × win_prob(f) (§14.1)."""
    p_finish = _devig_finish_fraction(
        bout.distance_no,
        bout.distance_yes,
        market="go-the-distance (No / Yes)",
    )
    return _result(
        bout,
        p_finish * float(bout.win_prob_a),
        p_finish * float(bout.win_prob_b),
        FinishSignalTier.DISTANCE,
    )


def _tier_round_total(bout: FinishOddsBout) -> BoutFinishSignal:
    """Tier 2 — P(round-total Under) × win_prob(f) (§14.1)."""
    p_under = _devig_finish_fraction(
        bout.round_total_under,
        bout.round_total_over,
        market="round-total (Under / Over)",
    )
    return _result(
        bout,
        p_under * float(bout.win_prob_a),
        p_under * float(bout.win_prob_b),
        FinishSignalTier.ROUND_TOTAL,
    )
