"""Pasted fight-card parser + matcher (A4.2).

Realizes docs/FIGHT_GROUPS_UX_DESIGN.md §9.3 (supported formats), §9.4
(matching rules), and the §9.10 A4.2 slice: a *pure* helper that turns a
block of pasted, newline-delimited bout text plus the active slate roster
into structured per-line parse + match results.

It is deliberately Streamlit-free and DB-free — it takes the roster names as
a plain iterable supplied by the caller, reads nothing, and writes nothing.
The Region D preview UI (§9.10 A4.3) renders these results; the Apply write
path (§9.10 A4.4) is a separate, later slice.

Matching reuses the odds → DK name-matching infrastructure verbatim (§9):
``normalize_name`` (conservative fold), ``normalize_name_aggressive`` (lossy
exact-match fallback), ``best_match`` (rapidfuzz ``WRatio``), and the
existing ``AUTO_MATCH_THRESHOLD`` / ``REVIEW_MATCH_THRESHOLD`` constants. No
new matching primitive and no new threshold are introduced here.

Eligibility scope for this slice (§9.6 rules 1–4): a row is eligible only
when it parses into two names, *both* sides resolve to an ``exact`` or
``high-confidence fuzzy`` roster fighter, the two sides are different
fighters (no self-pair), and neither resolved fighter appears in more than
one otherwise-eligible pasted row (no duplicate across the batch). The
"already grouped" opt-in gate (§9.6 rule 5) belongs to the A4.4 apply slice
and is intentionally not evaluated here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from src.ingestion.name_matching import (
    AUTO_MATCH_THRESHOLD,
    REVIEW_MATCH_THRESHOLD,
    best_match,
    normalize_name_aggressive,
)
from src.utils.text_cleaning import normalize_name

# Confidence bands (§9.4). ``exact`` and ``high-confidence fuzzy`` are the
# only auto-eligible bands; ``ambiguous`` and ``unmatched`` are never applied.
BAND_EXACT = "exact"
BAND_HIGH_FUZZY = "high-confidence fuzzy"
BAND_AMBIGUOUS = "ambiguous"
BAND_UNMATCHED = "unmatched"

# Row-level status for a line that does not parse into two names (§9.3).
PARSE_ERROR = "parse error"

ELIGIBLE_BANDS: frozenset[str] = frozenset({BAND_EXACT, BAND_HIGH_FUZZY})

# Worse-is-larger rank so a pair is only as strong as its weaker name (§9.5).
_BAND_RANK = {
    BAND_EXACT: 0,
    BAND_HIGH_FUZZY: 1,
    BAND_AMBIGUOUS: 2,
    BAND_UNMATCHED: 3,
}

# Blocking-reason strings (§9.5 / §9.6). Kept as module constants so the UI
# and tests reference one source of truth.
REASON_SELF_PAIR = "same fighter on both sides"
REASON_DUPLICATE = "fighter appears in another pasted row"

# Alphabetic separators matched only as whole tokens (§9.3): ``versus``,
# ``vs``, ``v`` — case-insensitive — with an optional trailing period
# (``vs.`` / ``v.``). The word boundaries keep names like "Vicente Luque" or
# "Marlon Vera" from being split on a leading/embedded ``v``. Longest
# alternative first so "versus" is not partially consumed as "v".
_WORD_SEP = re.compile(r"\b(?:versus|vs|v)\b\.?", re.IGNORECASE)

# Hyphen separator: a hyphen surrounded by whitespace only (§9.3). A bare
# hyphen never splits, so hyphenated / particled surnames ("Ji-Yeon Kim")
# survive intact.
_HYPHEN_SEP = re.compile(r"\s-\s")


@dataclass(frozen=True)
class SideMatch:
    """Match outcome for one parsed raw name (§9.4).

    ``matched_name`` is set only for the auto-eligible bands (``exact`` /
    ``high-confidence fuzzy``) — it is the canonical roster name that an
    Apply would use. ``best_candidate`` is the top roster candidate for
    *display* even when the band is ``ambiguous``; it is ``None`` for an
    unmatched name or an ambiguous collision with no single best guess.
    ``score`` carries the ``WRatio`` when the fuzzy path was taken.
    """

    raw: str
    band: str
    matched_name: Optional[str] = None
    best_candidate: Optional[str] = None
    score: Optional[int] = None


@dataclass(frozen=True)
class ParsedBout:
    """One pasted line's parse + match result (§9.5).

    ``side_1`` / ``side_2`` are ``None`` only when ``parse_error`` is true.
    ``pair_band`` is the worse of the two sides' bands, or ``PARSE_ERROR``.
    ``eligible`` is true only when the row passes every §9.6 gate evaluated
    in this slice; ``blocked_reason`` holds the single primary cause
    otherwise (and is ``None`` when eligible).
    """

    line_number: int
    raw_line: str
    parse_error: bool
    side_1: Optional[SideMatch]
    side_2: Optional[SideMatch]
    pair_band: str
    eligible: bool
    blocked_reason: Optional[str]


@dataclass(frozen=True)
class CardSummary:
    """Aggregate counts over a parsed card, for the preview summary line."""

    total: int
    eligible: int
    blocked: int
    parse_errors: int
    unmatched: int
    ambiguous: int
    self_pairs: int
    duplicates: int


def _split_line(line: str) -> Optional[tuple[str, str]]:
    """Split one line into (left, right) raw names, or ``None`` on a parse error.

    Separator precedence (§9.3): a word separator (``vs`` / ``v`` / ``versus``)
    wins over a spaced hyphen, so a hyphen inside a name is left intact when a
    word separator is also present. More than one separator of the chosen kind
    (e.g. two ``vs``) yields more than two parts and is a parse error rather
    than a silent truncation. An empty side after splitting is also a parse
    error.
    """
    word_matches = list(_WORD_SEP.finditer(line))
    if word_matches:
        if len(word_matches) > 1:
            return None
        match = word_matches[0]
    else:
        hyphen_matches = list(_HYPHEN_SEP.finditer(line))
        if not hyphen_matches:
            return None
        if len(hyphen_matches) > 1:
            return None
        match = hyphen_matches[0]

    left = line[: match.start()].strip()
    right = line[match.end() :].strip()
    if not left or not right:
        return None
    return left, right


def _match_name(
    raw: str,
    conservative_map: dict[str, list[str]],
    aggressive_map: dict[str, list[str]],
    candidates: list[str],
) -> SideMatch:
    """Resolve one raw name against the active roster (§9.4 resolution order)."""
    norm = normalize_name(raw)
    if not norm:
        return SideMatch(raw=raw, band=BAND_UNMATCHED)

    # 1. Exact (conservative) — unique normalized identity.
    conservative = conservative_map.get(norm, [])
    if len(conservative) == 1:
        name = conservative[0]
        return SideMatch(raw, BAND_EXACT, matched_name=name, best_candidate=name)
    if len(conservative) > 1:
        return SideMatch(raw, BAND_AMBIGUOUS)

    # 2. Exact (aggressive fallback) — nickname/suffix/middle-initial fold.
    aggressive = aggressive_map.get(normalize_name_aggressive(raw), [])
    if len(aggressive) == 1:
        name = aggressive[0]
        return SideMatch(raw, BAND_EXACT, matched_name=name, best_candidate=name)
    if len(aggressive) > 1:
        return SideMatch(raw, BAND_AMBIGUOUS)

    # 3. Fuzzy — WRatio with a clear-separation requirement for eligibility.
    top = best_match(raw, candidates, threshold=REVIEW_MATCH_THRESHOLD)
    if top is None:
        return SideMatch(raw, BAND_UNMATCHED)
    candidate, score = top
    remaining = [c for c in candidates if c != candidate]
    runner = best_match(raw, remaining, threshold=REVIEW_MATCH_THRESHOLD)
    second_score = runner[1] if runner is not None else 0
    if score >= AUTO_MATCH_THRESHOLD and second_score < REVIEW_MATCH_THRESHOLD:
        return SideMatch(
            raw, BAND_HIGH_FUZZY, matched_name=candidate,
            best_candidate=candidate, score=score,
        )
    # 88–94, or a >=95 near-tie with another >=88 candidate → ambiguous.
    return SideMatch(raw, BAND_AMBIGUOUS, best_candidate=candidate, score=score)


def _worse_band(band_1: str, band_2: str) -> str:
    return band_1 if _BAND_RANK[band_1] >= _BAND_RANK[band_2] else band_2


def parse_fight_card(text: str, roster_names: Iterable[str]) -> list[ParsedBout]:
    """Parse + match pasted card text against the active roster (§9.3 / §9.4).

    ``roster_names`` is the active slate roster (caller-supplied — typically
    ``[f.name for f in active_fighters]``). Blank lines are skipped entirely;
    every other line yields exactly one :class:`ParsedBout`, in pasted order.
    Pure: reads nothing, writes nothing.
    """
    roster = [str(name) for name in roster_names if str(name).strip()]
    candidates = list(roster)
    conservative_map: dict[str, list[str]] = {}
    aggressive_map: dict[str, list[str]] = {}
    for name in roster:
        conservative_map.setdefault(normalize_name(name), []).append(name)
        aggressive_map.setdefault(normalize_name_aggressive(name), []).append(name)

    # Pass A — parse each non-blank line and match both sides.
    interim: list[dict] = []
    for raw_line in (text or "").splitlines():
        if not raw_line.strip():
            continue
        split = _split_line(raw_line)
        if split is None:
            interim.append(
                {"raw_line": raw_line, "parse_error": True, "s1": None, "s2": None}
            )
            continue
        left, right = split
        interim.append(
            {
                "raw_line": raw_line,
                "parse_error": False,
                "s1": _match_name(left, conservative_map, aggressive_map, candidates),
                "s2": _match_name(right, conservative_map, aggressive_map, candidates),
            }
        )

    # Pass B — §9.6 rules 1–3 (parse error, both names resolved, no self-pair)
    # give each row a tentative reason and, when tentatively eligible, the pair
    # of resolved normalized names used for the cross-row duplicate scan.
    for row in interim:
        if row["parse_error"]:
            row["reason"] = PARSE_ERROR
            row["resolved"] = None
            continue
        s1, s2 = row["s1"], row["s2"]
        if s1.band not in ELIGIBLE_BANDS:
            row["reason"] = f"name 1 {s1.band}"
            row["resolved"] = None
            continue
        if s2.band not in ELIGIBLE_BANDS:
            row["reason"] = f"name 2 {s2.band}"
            row["resolved"] = None
            continue
        n1 = normalize_name(s1.matched_name)
        n2 = normalize_name(s2.matched_name)
        if n1 == n2:
            row["reason"] = REASON_SELF_PAIR
            row["resolved"] = None
            continue
        row["reason"] = None
        row["resolved"] = (n1, n2)

    # Pass C — §9.6 rule 4: a resolved fighter appearing in more than one
    # tentatively-eligible row blocks every row that names that fighter.
    counts: dict[str, int] = {}
    for row in interim:
        if row["resolved"] is None:
            continue
        for norm in row["resolved"]:
            counts[norm] = counts.get(norm, 0) + 1
    duplicate_norms = {norm for norm, count in counts.items() if count > 1}
    for row in interim:
        if row["reason"] is None:
            n1, n2 = row["resolved"]
            if n1 in duplicate_norms or n2 in duplicate_norms:
                row["reason"] = REASON_DUPLICATE

    bouts: list[ParsedBout] = []
    for index, row in enumerate(interim, start=1):
        reason = row["reason"]
        if row["parse_error"]:
            pair_band = PARSE_ERROR
        else:
            pair_band = _worse_band(row["s1"].band, row["s2"].band)
        bouts.append(
            ParsedBout(
                line_number=index,
                raw_line=row["raw_line"],
                parse_error=row["parse_error"],
                side_1=row["s1"],
                side_2=row["s2"],
                pair_band=pair_band,
                eligible=(reason is None),
                blocked_reason=reason,
            )
        )
    return bouts


def summarize(bouts: Iterable[ParsedBout]) -> CardSummary:
    """Aggregate counts for the preview summary line (§9.5).

    Blocked rows are bucketed by their single primary ``blocked_reason`` so
    the buckets are mutually exclusive and sum to ``blocked``.
    """
    bouts = list(bouts)
    total = len(bouts)
    eligible = sum(1 for b in bouts if b.eligible)
    parse_errors = ambiguous = unmatched = self_pairs = duplicates = 0
    for bout in bouts:
        if bout.eligible:
            continue
        reason = bout.blocked_reason or ""
        if bout.parse_error:
            parse_errors += 1
        elif reason == REASON_SELF_PAIR:
            self_pairs += 1
        elif reason == REASON_DUPLICATE:
            duplicates += 1
        elif BAND_UNMATCHED in reason:
            unmatched += 1
        elif BAND_AMBIGUOUS in reason:
            ambiguous += 1
    return CardSummary(
        total=total,
        eligible=eligible,
        blocked=total - eligible,
        parse_errors=parse_errors,
        unmatched=unmatched,
        ambiguous=ambiguous,
        self_pairs=self_pairs,
        duplicates=duplicates,
    )
