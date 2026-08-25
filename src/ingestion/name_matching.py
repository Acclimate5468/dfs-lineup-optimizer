"""Fuzzy fighter-name matching between odds source and DK salary names."""

from __future__ import annotations

import re

from rapidfuzz import fuzz, process

from src.utils.text_cleaning import normalize_name

DEFAULT_MATCH_THRESHOLD = 88

# Tiering thresholds used by the odds → DK matching pipeline (design §3).
# Kept here so other modules don't have to redefine them, and to make the
# 95/88 split easy to find/grep when calibration is revisited.
AUTO_MATCH_THRESHOLD = 95
REVIEW_MATCH_THRESHOLD = 88


# Curated nickname → expanded form. Mapping is one-way short→full, but the
# effect is bidirectional for matching: both forms collapse onto the same
# canonical token, so `normalize_name_aggressive("dan ige")` and
# `normalize_name_aggressive("daniel ige")` compare equal.
#
# Intentionally small and curated (design §1.2). No broad nickname inference,
# no prefix-match guessing, no learned suggestions. Additions are a code
# change reviewed like any other.
NICKNAME_EXPANSIONS: dict[str, str] = {
    "dan": "daniel",
    "tom": "thomas",
    "mike": "michael",
    "rob": "robert",
    "chris": "christopher",
    "nick": "nicholas",
    "alex": "alexander",
    "joe": "joseph",
    "matt": "matthew",
    "tony": "anthony",
    "ben": "benjamin",
}


SUFFIX_TOKENS: frozenset[str] = frozenset({"jr", "sr", "ii", "iii", "iv"})


# Single-letter tokens that are NOT middle initials — they are surname
# particles that survive in the middle position. Keeps `Sean O'Malley`,
# `Sean O-Malley`, and `Sean O Malley` from being reduced to `Sean Malley`,
# and analogously for D'... constructions when they appear after a given
# name (e.g. `John D'Angelo Smith`). Intentionally narrow: any additions
# are a code change reviewed like the nickname table.
SURNAME_PARTICLES: frozenset[str] = frozenset({"o", "d"})


# Punctuation that should split a token: period, hyphen, underscore,
# ASCII apostrophe, and Unicode left/right single quotation marks.
_PUNCT_TO_SPACE = re.compile(r"[.\-_'‘’]")

# Paired double quotes (ASCII or smart). Always treated as a nickname wrapper.
_DOUBLE_QUOTED = re.compile(r"[\"“”][^\"“”]*[\"“”]")

# Paired single quotes only counted as a nickname wrapper when the content
# contains whitespace — this avoids stripping intra-word apostrophes
# (e.g. D'Angelo) as if they were paired quotes.
_SINGLE_QUOTED_WITH_SPACE = re.compile(
    r"['‘’][^'‘’]*\s[^'‘’]*['‘’]"
)

_WHITESPACE = re.compile(r"\s+")


def normalize_name_aggressive(name: str) -> str:
    """Lossy normalization used only as an exact-match fallback (design §1.2).

    Never used as a stored key. Applies, in order: conservative normalize,
    drop quoted nickname segments, replace separator-style punctuation with
    spaces, drop trailing generational suffixes (jr/sr/ii/iii/iv), drop
    standalone single-character middle-initial tokens, apply the curated
    nickname-expansion table, then token-sort alphabetically.
    """
    s = normalize_name(name)
    if not s:
        return ""

    s = _DOUBLE_QUOTED.sub(" ", s)
    s = _SINGLE_QUOTED_WITH_SPACE.sub(" ", s)
    s = _PUNCT_TO_SPACE.sub(" ", s)
    s = _WHITESPACE.sub(" ", s).strip()
    if not s:
        return ""

    tokens = s.split()

    while len(tokens) > 1 and tokens[-1] in SUFFIX_TOKENS:
        tokens.pop()

    if len(tokens) > 2:
        tokens = (
            [tokens[0]]
            + [
                t
                for t in tokens[1:-1]
                if not (
                    len(t) == 1
                    and t.isalpha()
                    and t not in SURNAME_PARTICLES
                )
            ]
            + [tokens[-1]]
        )

    tokens = [NICKNAME_EXPANSIONS.get(t, t) for t in tokens]
    tokens.sort()
    return " ".join(tokens)


def best_match(
    query: str,
    candidates: list[str],
    threshold: int = DEFAULT_MATCH_THRESHOLD,
) -> tuple[str, int] | None:
    """Return (best_candidate, score) if score >= threshold, else None."""
    if not query or not candidates:
        return None
    match = process.extractOne(query, candidates, scorer=fuzz.WRatio)
    if match is None:
        return None
    candidate, score, _ = match
    if score < threshold:
        return None
    return candidate, int(score)


# A low-confidence *hint* threshold for the manual fixer only — strictly below
# REVIEW_MATCH_THRESHOLD (88), so it surfaces near-misses the matcher refused to
# auto/review-match (e.g. "Bruno Gustavo da Silva" → "Bruno Silva" scores ~85).
# This never binds anything: it only pre-selects the dropdown for a human to
# confirm. Kept above the noise floor (~40 for unrelated names) so a wrong
# suggestion is rare.
SUGGEST_MATCH_THRESHOLD = 75


def suggest_best_fighter(
    raw_name: str,
    fighter_names: list[str],
    threshold: int = SUGGEST_MATCH_THRESHOLD,
) -> str | None:
    """Return the DK fighter name most likely to be ``raw_name``, or ``None``.

    A low-confidence suggestion for the manual odds fixer — never an auto-match.
    Compares aggressively-normalized names (nickname-folded, suffix-stripped)
    via :func:`best_match`, then maps the winning normalized form back to the
    original DK fighter name. Returns ``None`` when nothing clears ``threshold``
    so the fixer falls back to "pick a fighter" rather than guessing wildly.
    """
    query = normalize_name_aggressive(raw_name)
    norm_to_name: dict[str, str] = {}
    for name in fighter_names:
        norm_to_name.setdefault(normalize_name_aggressive(name), name)
    result = best_match(query, list(norm_to_name.keys()), threshold=threshold)
    if result is None:
        return None
    return norm_to_name.get(result[0])
