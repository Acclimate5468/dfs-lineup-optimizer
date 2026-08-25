"""UFC DraftKings Classic scoring reference values (Phase 0 — locked).

Realizes ``docs/PROJECTION_V2_METHOD_AWARE_DESIGN.md`` §7 ("audit, reconcile,
lock, and wire in" the EXISTING table — not create a new one). These are the
per-action and fight-resolution scoring constants DK applies to UFC **Classic**
contests. Reconciled against the DK January 2021 scoring overhaul.

CONFIDENCE / SOURCING (fork A — see the scratch research note):
  - HIGH, read verbatim from the official DK rules page *interactively in a
    browser* (that page renders for a human but NOT for automated capture — see
    the note below): WIN_FIRST_ROUND (+90) and QUICK_WIN_BONUS_R1 (+25).
  - HIGH, cross-checked across multiple dated secondary sources:
      STRIKE, SIG_STRIKE, TAKEDOWN, REVERSAL_SWEEP, KNOCKDOWN,
      CONTROL_TIME_PER_SEC, and the removal of "Advancing Position".
  - SECONDARY-SOURCED, PENDING in-app DK confirmation (do NOT claim official):
      WIN_SECOND_ROUND (+70), WIN_THIRD_ROUND (+45), WIN_FOURTH_ROUND (+40),
      WIN_FIFTH_ROUND (+40), WIN_DECISION (+30) — see SECONDARY_SOURCED_BONUSES.

The official source page (https://www.draftkings.com/help/rules/mma) is a
JavaScript single-page app whose scoring legend hydrates at runtime and would not
render for automated capture; the five values above rest on unanimous, dated
(Jan 2021) secondary DFS sources. Re-verify them against the in-app DK scoring
legend during a live slate, then set ``DK_SCORING_VERIFIED_ON`` to that date.
WARNING: ``pick6.draftkings.com`` is a DIFFERENT product with different scoring —
do not use it to verify these.

NOTE: a locking test (``tests/test_scoring_config.py``) pins these values; it
catches accidental *edits*, never DK-side rule *drift*. Re-verify periodically.
"""

from __future__ import annotations

# --- Provenance ---------------------------------------------------------------
# Date this table was researched / reconciled (NOT a primary-source verification).
DK_SCORING_RESEARCHED_ON = "2026-06-06"
# Set to an ISO date ONLY after the five SECONDARY_SOURCED_BONUSES are confirmed
# against the in-app DK scoring legend. ``None`` => not yet officially verified.
DK_SCORING_VERIFIED_ON = None
# Intended PRIMARY source to re-verify against. Only +90 / +25 were captured
# verbatim here (read interactively in a browser); the rest is secondary-sourced.
DK_SCORING_SOURCE = "https://www.draftkings.com/help/rules/mma"

# --- Per-action ("offense") scoring -------------------------------------------
# A *significant* strike scores BOTH the base strike AND the significant bonus,
# i.e. +0.2 + 0.2 = +0.4 total. Encode them as two separate constants so a
# consumer that already counts the base STRIKE adds only SIG_STRIKE on top.
STRIKE = 0.2            # base strike (any strike landed)
SIG_STRIKE = 0.2        # significant-strike BONUS, on top of STRIKE (=> +0.4 total)
TAKEDOWN = 5.0
REVERSAL_SWEEP = 5.0    # DK combines reversal and sweep into one category
KNOCKDOWN = 10.0
# Control time is scored PER SECOND (not per minute): +0.03/sec = +1.8/min.
# Keep the unit explicit to avoid a 60x error.
CONTROL_TIME_PER_SEC = 0.03

# NOTE: "Advancing Position" / ADVANCE was REMOVED in the Jan 2021 overhaul and is
# intentionally absent. Do not re-add it. (The locking test asserts it is gone.)
# NOTE: there is no submission-attempt line item and no KO-vs-submission method
# bonus — the win bonus is round-based only. Do not add either.

# --- Fight-resolution (win) bonuses -------------------------------------------
# Round-of-finish win bonuses are non-increasing but NOT strictly decreasing
# (R4 and R5 are tied at 40). Pin the six exact values; do not encode a formula.
WIN_FIRST_ROUND = 90.0   # verbatim from official DK page
WIN_SECOND_ROUND = 70.0  # secondary-sourced (see SECONDARY_SOURCED_BONUSES)
WIN_THIRD_ROUND = 45.0   # secondary-sourced
WIN_FOURTH_ROUND = 40.0  # secondary-sourced
WIN_FIFTH_ROUND = 40.0   # secondary-sourced
WIN_DECISION = 30.0      # secondary-sourced

# Quick-win bonus: awarded for a 1st-round finish in <= 60 seconds. Additive on
# the R1 win (so a sub-60s R1 finish is +90 + 25 = +115 in resolution bonuses).
QUICK_WIN_BONUS_R1 = 25.0

# Names of the constants whose values are secondary-sourced and pending in-app DK
# confirmation. Consumers / reports may surface this caveat; the locking test
# asserts the set is documented.
SECONDARY_SOURCED_BONUSES = (
    "WIN_SECOND_ROUND",
    "WIN_THIRD_ROUND",
    "WIN_FOURTH_ROUND",
    "WIN_FIFTH_ROUND",
    "WIN_DECISION",
)
