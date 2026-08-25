"""Finish-aware projection model (Phase A, Tier 0) — pure Python.

Realizes ``docs/PROJECTION_V2_METHOD_AWARE_DESIGN.md`` §6: a transparent
arithmetic decomposition of a fighter's expected DraftKings points over FOUR
mutually-exclusive outcomes::

    E[DKpts] =   P(win by finish)    * E[pts | finish win]
               + P(win by decision)  * E[pts | decision win]
               + P(loss by finish)   * E[pts | finish loss]
               + P(loss by decision) * E[pts | decision loss]

This module is PURE: no DB, no Streamlit, no service/optimizer wiring (that is
Phase C), no manual finish-signal input (that is the gated Phase B), no
props/ITD ingestion. It is the FIRST legitimate consumer of the locked DK
scoring table (``src.config.scoring``) — see design §7 "wire it in".

It is NOT validated to beat the v0 formula; the §12 / Phase A′ calibration
harness decides that. v0 (``default_projection``) remains the default engine.

------------------------------------------------------------------------------
Tier-0 modeling ASSUMPTIONS (NOT DK facts — unvalidated free parameters).
Every constant below is a transparent placeholder pinned by the locking test and
subject to §12 / Phase A′ calibration (design §11 warns v2 has more free
parameters than v0). These describe league-average fight behavior, which the DK
scoring table then prices. They live here (not in ``scoring.py``) precisely
because they are our assumptions, not DraftKings' published rules.
------------------------------------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import scoring

# --- Status / mode constants (mirror projection_service.py) -------------------
STATUS_OK = "ok"
STATUS_MISSING_INPUTS = "missing_inputs"
PROJECTION_MODE = "v2_finish"
VALID_SCHEDULED_ROUNDS = (3, 5)

# --- Tier-0 modeling assumptions (see header caveat) --------------------------
# Unconditional probability a UFC fight ends inside the distance. Design §16 Q6
# (~0.55-0.60). ASSUMPTION pending sourcing/calibration.
LEAGUE_FINISH_RATE = 0.57

# Conditional distribution of WHICH round a finish lands in, given a finish.
# Finishes skew early. Each tuple sums to 1.0. ASSUMPTION pending calibration.
FINISH_ROUND_WEIGHTS = {
    3: (0.55, 0.30, 0.15),
    5: (0.40, 0.25, 0.15, 0.12, 0.08),
}

# Fraction of 1st-round finishes that occur in <=60s (and so earn the quick-win
# bonus). ASSUMPTION pending calibration.
QUICK_WIN_R1_FRACTION = 0.15

# Expected per-round DK action points (strikes/sig/control/takedowns) accrued by
# the eventual winner vs loser. The branch differentiation comes from LENGTH and
# the resolution bonus, not from per-round rate differences (design §16 Q5:
# "constants first"). ASSUMPTION pending calibration.
WINNER_ACC_RATE_PER_ROUND = 18.0
LOSER_ACC_RATE_PER_ROUND = 14.0

# Per-round win (resolution) bonuses, sourced from the locked DK scoring table —
# this is the model's wiring of scoring.py. Index r-1 => finishing round r.
_ROUND_WIN_BONUSES = (
    scoring.WIN_FIRST_ROUND,   # R1
    scoring.WIN_SECOND_ROUND,  # R2
    scoring.WIN_THIRD_ROUND,   # R3
    scoring.WIN_FOURTH_ROUND,  # R4
    scoring.WIN_FIFTH_ROUND,   # R5
)

# Branch labels.
FINISH_WIN = "finish_win"
DECISION_WIN = "decision_win"
FINISH_LOSS = "finish_loss"
DECISION_LOSS = "decision_loss"


@dataclass(frozen=True)
class OutcomeBranch:
    """One of the four mutually-exclusive fight outcomes for this fighter."""

    label: str
    probability: float
    expected_points: float


@dataclass(frozen=True)
class FinishProjection:
    """Per-fighter finish-aware (v2) projection output (design §6.3 / §8).

    ``best_branch_pts`` / ``worst_branch_pts`` are the conditional means of the
    highest- / lowest-scoring branch (NOT calibrated percentiles — design §6.3).
    """

    projected_dk_points: float | None
    outcome_branches: tuple[OutcomeBranch, ...]
    best_branch_pts: float | None
    worst_branch_pts: float | None
    projection_status: str
    projection_mode: str = PROJECTION_MODE
    missing_inputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    # Resolved finish signal echoed back for transparency.
    p_fight_finishes: float | None = None
    finish_share: float | None = None


# --- Length / bonus helpers ---------------------------------------------------

def win_bonus_for_finish_round(round_number: int) -> float:
    """DK win-resolution bonus for a finish in ``round_number`` (1-indexed)."""
    if not 1 <= int(round_number) <= len(_ROUND_WIN_BONUSES):
        raise ValueError(f"round_number out of range: {round_number}")
    return _ROUND_WIN_BONUSES[int(round_number) - 1]


def full_length(scheduled_rounds: int) -> float:
    """Length (in rounds) of a fight that goes the distance."""
    return float(int(scheduled_rounds))


def finish_action_length(round_number: int) -> float:
    """Completed fight length (rounds) when a finish lands in ``round_number``.

    ~(round-1) complete rounds plus ~half of the finishing round = round - 0.5.
    This is the ONE shared latent length that couples the resolution bonus and the
    action accumulation (design §6.2 no-double-count): both
    :func:`expected_finish_length` and :func:`expected_points_for_finish_in_round`
    derive from it, so they cannot diverge.
    """
    return float(int(round_number)) - 0.5


def expected_finish_length(scheduled_rounds: int) -> float:
    """Expected completed fight length (rounds) GIVEN a finish — the
    weight-averaged :func:`finish_action_length` over the finish-round
    distribution."""
    weights = _weights_for(scheduled_rounds)
    return sum(w * finish_action_length(r + 1) for r, w in enumerate(weights))


def expected_finish_bonus(scheduled_rounds: int) -> float:
    """Expected resolution bonus GIVEN a finish: weighted round bonus + the
    expected quick-win contribution from sub-60s 1st-round finishes."""
    weights = _weights_for(scheduled_rounds)
    weighted = sum(w * _ROUND_WIN_BONUSES[r] for r, w in enumerate(weights))
    quick = weights[0] * QUICK_WIN_R1_FRACTION * scoring.QUICK_WIN_BONUS_R1
    return weighted + quick


def expected_points_for_finish_in_round(round_number: int) -> float:
    """E[pts | the winner finishes in ``round_number``] = bonus + winner action
    over (r-0.5) rounds (+ expected quick-win in R1). Sums (weighted) to
    ``E[pts | finish win]``."""
    r = int(round_number)
    bonus = win_bonus_for_finish_round(r)
    acc = WINNER_ACC_RATE_PER_ROUND * finish_action_length(r)
    quick = QUICK_WIN_R1_FRACTION * scoring.QUICK_WIN_BONUS_R1 if r == 1 else 0.0
    return bonus + acc + quick


def accumulated_points(rate_per_round: float, length_rounds: float) -> float:
    """Action points = per-round rate * fight length (rounds)."""
    return float(rate_per_round) * float(length_rounds)


def _weights_for(scheduled_rounds: int) -> tuple[float, ...]:
    rounds = int(scheduled_rounds)
    if rounds not in FINISH_ROUND_WEIGHTS:
        raise ValueError(f"unsupported scheduled_rounds: {scheduled_rounds}")
    return FINISH_ROUND_WEIGHTS[rounds]


# --- Per-branch expected points -----------------------------------------------

def branch_expected_points(scheduled_rounds: int) -> dict[str, float]:
    """The four branches' conditional expected points (design §6.2).

    These depend only on fight length + the scoring/assumption constants — NOT on
    win probability or finish share (those drive the branch *probabilities*).
    """
    fin_len = expected_finish_length(scheduled_rounds)
    dist_len = full_length(scheduled_rounds)
    return {
        FINISH_WIN: expected_finish_bonus(scheduled_rounds)
        + accumulated_points(WINNER_ACC_RATE_PER_ROUND, fin_len),
        DECISION_WIN: scoring.WIN_DECISION
        + accumulated_points(WINNER_ACC_RATE_PER_ROUND, dist_len),
        # Loser finished early: small action over the (short) finish length.
        FINISH_LOSS: accumulated_points(LOSER_ACC_RATE_PER_ROUND, fin_len),
        # Loser who went the distance: substantial full-fight action volume.
        DECISION_LOSS: accumulated_points(LOSER_ACC_RATE_PER_ROUND, dist_len),
    }


# --- Branch probabilities -----------------------------------------------------

def _branch_probabilities(
    p_win: float, p_fight_finishes: float, finish_share: float
) -> tuple[dict[str, float], tuple[str, ...]]:
    """Derive the four branch probabilities from (p_win, p_fight_finishes,
    finish_share), clamping degenerate inputs with a recorded warning (design
    §6.1). Guarantees: all four in [0,1], sum to 1, and
    P(finish win) + P(decision win) == p_win exactly.
    """
    warnings: list[str] = []

    raw_finish_win = p_fight_finishes * finish_share
    finish_win = min(raw_finish_win, p_win)
    if raw_finish_win > p_win + 1e-12:
        warnings.append(
            "clamped p_finish_win to p_win (finish signal exceeded win prob)"
        )
    decision_win = p_win - finish_win

    loss = 1.0 - p_win
    raw_finish_loss = p_fight_finishes * (1.0 - finish_share)
    finish_loss = min(raw_finish_loss, loss)
    if raw_finish_loss > loss + 1e-12:
        warnings.append(
            "clamped p_finish_loss to loss prob (finish signal exceeded loss prob)"
        )
    decision_loss = loss - finish_loss

    probs = {
        FINISH_WIN: finish_win,
        DECISION_WIN: decision_win,
        FINISH_LOSS: finish_loss,
        DECISION_LOSS: decision_loss,
    }
    return probs, tuple(warnings)


# --- Public API ---------------------------------------------------------------

def compute_finish_projection(
    p_win: float | None,
    scheduled_rounds: int | None,
    p_fight_finishes: float | None = None,
    finish_share: float | None = None,
) -> FinishProjection:
    """Compute the Tier-0 finish-aware projection for one fighter.

    Args:
        p_win: odds-implied win probability in [0, 1]. ``None`` => missing.
        scheduled_rounds: 3 or 5. ``None`` / other => missing.
        p_fight_finishes: unconditional P(fight ends inside the distance). Tier 0
            defaults to ``LEAGUE_FINISH_RATE``.
        finish_share: P(this fighter is the finisher | a finish). Defaults to
            ``p_win`` (split by relative win probability, design §5).

    Returns a :class:`FinishProjection`. Missing required inputs yield
    ``projection_status == "missing_inputs"`` with ``None`` outputs — the model
    never invents a win probability (design §9). Out-of-range numeric inputs
    raise ``ValueError`` (mirrors ``default_projection``).
    """
    missing: list[str] = []
    if p_win is None:
        missing.append("win_probability")
    if scheduled_rounds is None or int(scheduled_rounds) not in VALID_SCHEDULED_ROUNDS:
        missing.append("scheduled_rounds")
    if missing:
        return FinishProjection(
            projected_dk_points=None,
            outcome_branches=(),
            best_branch_pts=None,
            worst_branch_pts=None,
            projection_status=STATUS_MISSING_INPUTS,
            missing_inputs=tuple(missing),
        )

    p_win_f = float(p_win)
    if not 0.0 <= p_win_f <= 1.0:
        raise ValueError("p_win must be in [0, 1]")

    p_finish = LEAGUE_FINISH_RATE if p_fight_finishes is None else float(p_fight_finishes)
    if not 0.0 <= p_finish <= 1.0:
        raise ValueError("p_fight_finishes must be in [0, 1]")

    share = p_win_f if finish_share is None else float(finish_share)
    if not 0.0 <= share <= 1.0:
        raise ValueError("finish_share must be in [0, 1]")

    probs, warnings = _branch_probabilities(p_win_f, p_finish, share)
    pts = branch_expected_points(scheduled_rounds)

    branches = tuple(
        OutcomeBranch(label=label, probability=probs[label], expected_points=pts[label])
        for label in (FINISH_WIN, DECISION_WIN, FINISH_LOSS, DECISION_LOSS)
    )
    mean = sum(b.probability * b.expected_points for b in branches)
    branch_values = [b.expected_points for b in branches]

    return FinishProjection(
        projected_dk_points=mean,
        outcome_branches=branches,
        best_branch_pts=max(branch_values),
        worst_branch_pts=min(branch_values),
        projection_status=STATUS_OK,
        missing_inputs=(),
        warnings=warnings,
        p_fight_finishes=p_finish,
        finish_share=share,
    )
