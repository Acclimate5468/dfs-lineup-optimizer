"""Fighter Status v1 — pure taxonomy, category mapping, and resolver.

Phase A of ``docs/FIGHTER_STATUS_V1_DESIGN.md``: this module is
pure-Python. It owns the v1 status vocabulary (§4), the value →
downstream category mapping (§5), and the resolver that picks between
the importer-owned base status and an optional user-owned manual
override (§13.2, §15 Phase A).

This module does NOT touch the database, the repository layer, the
Streamlit UI, projections, alerts, the optimizer, the manual review
gate, or exports. Per §6 / §7 / §8, Fighter Status is kept strictly
disjoint from ``odds_match_results.effective_status``.
"""

from __future__ import annotations

from typing import Optional

# §4 — status value constants. The v1 set is closed.
ACTIVE = "active"
NEEDS_REVIEW = "needs_review"
QUESTIONABLE = "questionable"
OUT = "out"
WITHDRAWN = "withdrawn"
REPLACED = "replaced"
INACTIVE = "inactive"
MISSED_WEIGHT = "missed_weight"
SHORT_NOTICE = "short_notice"
DUPLICATE_OR_BAD_ROW = "duplicate_or_bad_row"

# §5 — downstream category constants.
CATEGORY_ACTIVE = "active"
CATEGORY_WARNING = "warning"
CATEGORY_BLOCKING = "blocking"

ALLOWED_CATEGORIES: frozenset[str] = frozenset(
    {CATEGORY_ACTIVE, CATEGORY_WARNING, CATEGORY_BLOCKING}
)

# §5 — single source of truth for the value → category mapping. Any
# future downstream consumer (Projection v1, Mismatch Alerts v1, Manual
# Review, optimizer, export) MUST import this mapping rather than
# duplicating the value lists.
STATUS_CATEGORY: dict[str, str] = {
    ACTIVE: CATEGORY_ACTIVE,
    NEEDS_REVIEW: CATEGORY_WARNING,
    QUESTIONABLE: CATEGORY_WARNING,
    MISSED_WEIGHT: CATEGORY_WARNING,
    SHORT_NOTICE: CATEGORY_WARNING,
    OUT: CATEGORY_BLOCKING,
    WITHDRAWN: CATEGORY_BLOCKING,
    REPLACED: CATEGORY_BLOCKING,
    INACTIVE: CATEGORY_BLOCKING,
    DUPLICATE_OR_BAD_ROW: CATEGORY_BLOCKING,
}

ALLOWED_STATUSES: frozenset[str] = frozenset(STATUS_CATEGORY.keys())

# Conservative fallback when the importer base status is missing
# (None / empty). The user has not had a chance to assert anything and
# the importer did not provide a value, so surface the row for review
# rather than silently treating it as active. The fallback stays in
# the Warning category — never Blocking — because v1 must not silently
# exclude a row that simply lacks importer metadata.
DEFAULT_BASE_STATUS = NEEDS_REVIEW


def validate_status(status: str) -> str:
    """Return ``status`` if it is in ``ALLOWED_STATUSES``, else raise.

    Raises ``ValueError`` for unknown values. Empty string and ``None``
    are also rejected — callers that may legitimately have no status
    yet should use the resolver, which handles the empty base case
    explicitly.
    """
    if not isinstance(status, str) or status == "":
        raise ValueError(f"fighter status must be a non-empty string, got {status!r}")
    if status not in ALLOWED_STATUSES:
        raise ValueError(
            f"unknown fighter status {status!r}; allowed values: "
            f"{sorted(ALLOWED_STATUSES)}"
        )
    return status


def category_for(status: str) -> str:
    """Return the downstream category for ``status`` (§5)."""
    validate_status(status)
    return STATUS_CATEGORY[status]


def is_active(status: str) -> bool:
    """True iff ``status`` is in the Active downstream category (§5)."""
    return category_for(status) == CATEGORY_ACTIVE


def is_warning(status: str) -> bool:
    """True iff ``status`` is in the Warning downstream category (§5)."""
    return category_for(status) == CATEGORY_WARNING


def is_blocking(status: str) -> bool:
    """True iff ``status`` is in the Blocking downstream category (§5)."""
    return category_for(status) == CATEGORY_BLOCKING


def resolve_effective_fighter_status(
    importer_status: Optional[str],
    manual_status: Optional[str],
) -> str:
    """Resolve the effective Fighter Status for a (fighter, slate) pair.

    Rules (§13.2):

    - The manual override wins when present. ``manual_status`` is the
      user-owned value from ``fighters.manual_status`` once persistence
      lands in Phase B; here it is a pure input.
    - When ``manual_status`` is ``None``, the importer-owned base
      ``importer_status`` is the effective value.
    - When both are missing (importer never wrote a value AND no user
      override), fall back to ``DEFAULT_BASE_STATUS`` (``needs_review``)
      so the row is surfaced for review rather than silently treated as
      active.
    - Any non-``None`` value passed in is validated against
      ``ALLOWED_STATUSES``; unknown values raise ``ValueError``.
    """
    if manual_status is not None:
        return validate_status(manual_status)
    if importer_status is None or importer_status == "":
        return DEFAULT_BASE_STATUS
    return validate_status(importer_status)
