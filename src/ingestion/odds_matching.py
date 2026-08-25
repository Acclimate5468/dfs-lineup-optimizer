"""In-memory odds → DK fighter name matching pipeline.

First slice of docs/ODDS_MATCHING_DESIGN.md. Pure / in-memory only — no DB
access, no Streamlit, no persistence. Suitable as the algorithmic core that
later layers (manual override service, repositories, UI) will wrap.

Pipeline per odds row:

  1. Conservative exact      (§2.3) — score 100, status ``auto_match``.
  2. Aggressive exact        (§2.4) — score 100, status ``auto_match``.
                                      Multiple roster collisions on the same
                                      aggressive key (§5.2) → ``review_required``.
  3. Fuzzy WRatio fallback   (§3)  — tier thresholds:
       score >= 95                         → ``auto_match``
       REVIEW_MATCH_THRESHOLD .. <95       → ``review_required``
       score <  REVIEW_MATCH_THRESHOLD     → ``unmatched``
     Top-tied fuzzy candidates collapse to ``review_required`` (§5.2).

Opponent context is optional. When provided:

  - ``auto_match`` may be demoted to ``review_required`` if the row's opponent
    disagrees with a *confirmed* expected opponent (§4 decision matrix).
  - Opponent agreement never promotes ``review_required`` to ``auto_match``
    — v0 locked rule, §4 / §11 open-decision 5. This holds for both
    review-band fuzzy matches AND ambiguous-candidate cases: when opponent
    context narrows multiple candidates down to one, the preferred candidate
    is surfaced as supporting context (``preferred_candidate``) but the
    result stays ``review_required``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from src.ingestion.name_matching import (
    AUTO_MATCH_THRESHOLD,
    REVIEW_MATCH_THRESHOLD,
    normalize_name_aggressive,
)
from src.utils.text_cleaning import normalize_name

STATUS_AUTO = "auto_match"
STATUS_REVIEW = "review_required"
STATUS_UNMATCHED = "unmatched"

STAGE_EXACT_CONSERVATIVE = "exact_conservative"
STAGE_EXACT_AGGRESSIVE = "exact_aggressive"
STAGE_FUZZY = "fuzzy"
STAGE_NONE = "none"

OPPONENT_PASSED = "passed"
OPPONENT_FAILED = "failed"
OPPONENT_UNKNOWN = "unknown"
OPPONENT_NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class OddsRowInput:
    """A single odds row going into the matcher."""

    fighter: str
    opponent: str | None = None
    row_id: str | None = None


@dataclass(frozen=True)
class OpponentContext:
    """Expected opponent for a DK fighter on this slate.

    ``confirmed=False`` mirrors the design's ``fight_groups.status =
    'unconfirmed'``: an opponent mismatch is recorded but does NOT demote an
    ``auto_match`` to review (§4: "Fight-group status matters in the other
    direction").
    """

    expected_opponent: str
    confirmed: bool = True


@dataclass
class OddsMatchResult:
    odds_fighter: str
    row_id: str | None
    dk_fighter: str | None
    status: str
    stage: str
    score: int
    opponent_check: str = OPPONENT_NOT_APPLICABLE
    candidates: tuple[str, ...] = ()
    # When the result is ``review_required`` *because* of ambiguity, this
    # field optionally names the single candidate that opponent context
    # points to. It is supporting evidence for the human reviewer — it does
    # NOT promote the match to ``auto_match`` (v0 §4 / §5.2 lock).
    preferred_candidate: str | None = None
    notes: tuple[str, ...] = ()


def match_odds_to_dk(
    dk_fighters: list[str],
    odds_rows: list[OddsRowInput],
    *,
    opponents: dict[str, OpponentContext] | None = None,
) -> list[OddsMatchResult]:
    """Match every odds row against the DK roster. Pure / in-memory.

    Returns one ``OddsMatchResult`` per input row, preserving input order.
    """
    opp_map = opponents or {}

    roster_by_conservative: dict[str, str] = {}
    roster_by_aggressive: dict[str, set[str]] = defaultdict(set)
    for dk in dk_fighters:
        if not dk:
            continue
        c = normalize_name(dk)
        if c:
            roster_by_conservative[c] = dk
        a = normalize_name_aggressive(dk)
        if a:
            roster_by_aggressive[a].add(dk)
    cons_keys = list(roster_by_conservative.keys())

    return [
        _match_one(row, roster_by_conservative, roster_by_aggressive, cons_keys, opp_map)
        for row in odds_rows
    ]


def _match_one(
    row: OddsRowInput,
    cons_idx: dict[str, str],
    agg_idx: dict[str, set[str]],
    cons_keys: list[str],
    opponents: dict[str, OpponentContext],
) -> OddsMatchResult:
    fighter = row.fighter or ""
    if not fighter.strip():
        return OddsMatchResult(
            odds_fighter=fighter,
            row_id=row.row_id,
            dk_fighter=None,
            status=STATUS_UNMATCHED,
            stage=STAGE_NONE,
            score=0,
            notes=("empty_fighter",),
        )

    # Stage 1: conservative exact.
    q_c = normalize_name(fighter)
    if q_c and q_c in cons_idx:
        return _apply_opponent_check(
            OddsMatchResult(
                odds_fighter=fighter,
                row_id=row.row_id,
                dk_fighter=cons_idx[q_c],
                status=STATUS_AUTO,
                stage=STAGE_EXACT_CONSERVATIVE,
                score=100,
            ),
            row,
            opponents,
        )

    # Stage 2: aggressive exact.
    q_a = normalize_name_aggressive(fighter)
    if q_a and q_a in agg_idx:
        candidates = agg_idx[q_a]
        if len(candidates) == 1:
            return _apply_opponent_check(
                OddsMatchResult(
                    odds_fighter=fighter,
                    row_id=row.row_id,
                    dk_fighter=next(iter(candidates)),
                    status=STATUS_AUTO,
                    stage=STAGE_EXACT_AGGRESSIVE,
                    score=100,
                ),
                row,
                opponents,
            )
        return _disambiguate_or_review(
            row=row,
            candidates=tuple(sorted(candidates)),
            stage=STAGE_EXACT_AGGRESSIVE,
            score=100,
            opponents=opponents,
            ambiguity_note="ambiguous_aggressive",
        )

    # Stage 3: fuzzy WRatio against conservative roster keys.
    if not cons_keys or not q_c:
        return OddsMatchResult(
            odds_fighter=fighter,
            row_id=row.row_id,
            dk_fighter=None,
            status=STATUS_UNMATCHED,
            stage=STAGE_NONE,
            score=0,
        )
    extracted = process.extract(q_c, cons_keys, scorer=fuzz.WRatio, limit=3)
    if not extracted:
        return OddsMatchResult(
            odds_fighter=fighter,
            row_id=row.row_id,
            dk_fighter=None,
            status=STATUS_UNMATCHED,
            stage=STAGE_FUZZY,
            score=0,
        )

    top_key, raw_top, _ = extracted[0]
    top_score = int(raw_top)
    if top_score < REVIEW_MATCH_THRESHOLD:
        return OddsMatchResult(
            odds_fighter=fighter,
            row_id=row.row_id,
            dk_fighter=None,
            status=STATUS_UNMATCHED,
            stage=STAGE_FUZZY,
            score=top_score,
        )

    tied_keys = [k for k, s, _ in extracted if int(s) == top_score]
    if len(tied_keys) > 1:
        return _disambiguate_or_review(
            row=row,
            candidates=tuple(sorted(cons_idx[k] for k in tied_keys)),
            stage=STAGE_FUZZY,
            score=top_score,
            opponents=opponents,
            ambiguity_note="ambiguous_fuzzy",
        )

    dk = cons_idx[top_key]
    if top_score >= AUTO_MATCH_THRESHOLD:
        return _apply_opponent_check(
            OddsMatchResult(
                odds_fighter=fighter,
                row_id=row.row_id,
                dk_fighter=dk,
                status=STATUS_AUTO,
                stage=STAGE_FUZZY,
                score=top_score,
            ),
            row,
            opponents,
        )

    # 88..94 — review_required. Opponent agreement records context but is
    # locked out from auto-promotion in v0 (design §4 / §11.5).
    return _apply_opponent_check(
        OddsMatchResult(
            odds_fighter=fighter,
            row_id=row.row_id,
            dk_fighter=dk,
            status=STATUS_REVIEW,
            stage=STAGE_FUZZY,
            score=top_score,
        ),
        row,
        opponents,
        allow_demotion=False,
    )


def _disambiguate_or_review(
    *,
    row: OddsRowInput,
    candidates: tuple[str, ...],
    stage: str,
    score: int,
    opponents: dict[str, OpponentContext],
    ambiguity_note: str,
) -> OddsMatchResult:
    """Ambiguous-candidate case → always ``review_required`` in v0.

    Opponent context may identify a single preferred candidate and is recorded
    as supporting context (``preferred_candidate`` + ``opponent_supported_
    disambiguation`` note + ``opponent_check = 'passed'``) so the reviewer can
    one-click accept it. The status remains ``review_required`` regardless:
    opponent agreement never promotes ambiguous matches in v0 (§4 / §5.2).
    """
    preferred: str | None = None
    extra_notes: list[str] = []
    opponent_check = OPPONENT_NOT_APPLICABLE
    if row.opponent:
        plausible = [
            c for c in candidates if _opponent_agrees(row.opponent, opponents.get(c))
        ]
        if len(plausible) == 1:
            preferred = plausible[0]
            opponent_check = OPPONENT_PASSED
            extra_notes.append("opponent_supported_disambiguation")
        else:
            # Opponent column was present but it either matched none of the
            # candidates or matched more than one — surface that as 'unknown'
            # so the reviewer sees the signal was inconclusive.
            opponent_check = OPPONENT_UNKNOWN
    return OddsMatchResult(
        odds_fighter=row.fighter,
        row_id=row.row_id,
        dk_fighter=None,
        status=STATUS_REVIEW,
        stage=stage,
        score=score,
        opponent_check=opponent_check,
        candidates=candidates,
        preferred_candidate=preferred,
        notes=(ambiguity_note, *extra_notes),
    )


def _opponent_agrees(raw_opponent: str, ctx: OpponentContext | None) -> bool:
    if ctx is None:
        return False
    return _names_match(raw_opponent, ctx.expected_opponent)


def _names_match(a: str, b: str) -> bool:
    """Conservative + aggressive + fuzzy(>= REVIEW threshold) equality.

    Mirrors the row-level pipeline so the opponent column is held to the same
    bar a primary name match would be (design §4).
    """
    if not a or not b:
        return False
    a_c, b_c = normalize_name(a), normalize_name(b)
    if a_c and b_c and a_c == b_c:
        return True
    a_a, b_a = normalize_name_aggressive(a), normalize_name_aggressive(b)
    if a_a and b_a and a_a == b_a:
        return True
    if not a_c or not b_c:
        return False
    return int(fuzz.WRatio(a_c, b_c)) >= REVIEW_MATCH_THRESHOLD


def _apply_opponent_check(
    result: OddsMatchResult,
    row: OddsRowInput,
    opponents: dict[str, OpponentContext],
    *,
    allow_demotion: bool = True,
) -> OddsMatchResult:
    """Resolve opponent_check and (only) demote ``auto_match`` → ``review_required``.

    Per §4: review-band matches always require explicit user action, so
    ``allow_demotion=False`` is used in the fuzzy 88..94 path purely to record
    the opponent_check value without ever changing the status away from
    ``review_required``.
    """
    if not result.dk_fighter:
        return result
    if not row.opponent:
        result.opponent_check = OPPONENT_NOT_APPLICABLE
        return result
    ctx = opponents.get(result.dk_fighter)
    if ctx is None:
        result.opponent_check = OPPONENT_UNKNOWN
        return result
    if _names_match(row.opponent, ctx.expected_opponent):
        result.opponent_check = OPPONENT_PASSED
        return result
    result.opponent_check = OPPONENT_FAILED
    if (
        allow_demotion
        and result.status == STATUS_AUTO
        and ctx.confirmed
    ):
        result.status = STATUS_REVIEW
        result.notes = result.notes + ("opponent_mismatch",)
    return result
