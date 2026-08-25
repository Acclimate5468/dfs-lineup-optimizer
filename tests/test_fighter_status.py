"""Phase A tests for ``src/slate/fighter_status.py``.

Pins every value in FIGHTER_STATUS_V1_DESIGN.md §4, every category
membership in §5, and the resolver / predicate / validation contracts
in §15 Phase A and §16.
"""

from __future__ import annotations

import pytest

from src.slate import fighter_status as fs


V1_STATUS_VALUES = {
    "active",
    "needs_review",
    "questionable",
    "out",
    "withdrawn",
    "replaced",
    "inactive",
    "missed_weight",
    "short_notice",
    "duplicate_or_bad_row",
}


# --- §4 value constants -----------------------------------------------------


def test_status_value_constants_match_design_section_4():
    assert fs.ACTIVE == "active"
    assert fs.NEEDS_REVIEW == "needs_review"
    assert fs.QUESTIONABLE == "questionable"
    assert fs.OUT == "out"
    assert fs.WITHDRAWN == "withdrawn"
    assert fs.REPLACED == "replaced"
    assert fs.INACTIVE == "inactive"
    assert fs.MISSED_WEIGHT == "missed_weight"
    assert fs.SHORT_NOTICE == "short_notice"
    assert fs.DUPLICATE_OR_BAD_ROW == "duplicate_or_bad_row"


def test_allowed_statuses_is_exactly_the_v1_set():
    # `==` (not subset). Any addition / removal must update the design
    # and this assertion in the same slice.
    assert set(fs.ALLOWED_STATUSES) == V1_STATUS_VALUES


def test_allowed_statuses_is_frozen():
    assert isinstance(fs.ALLOWED_STATUSES, frozenset)


# --- §5 category mapping ----------------------------------------------------


def test_allowed_categories_is_exactly_three():
    assert set(fs.ALLOWED_CATEGORIES) == {"active", "warning", "blocking"}


def test_status_category_contains_every_value_with_no_extras():
    assert set(fs.STATUS_CATEGORY.keys()) == V1_STATUS_VALUES


def test_status_category_values_are_in_allowed_categories():
    for status, category in fs.STATUS_CATEGORY.items():
        assert category in fs.ALLOWED_CATEGORIES, status


def test_active_category_membership():
    assert fs.STATUS_CATEGORY["active"] == "active"


def test_warning_category_membership():
    expected = {"needs_review", "questionable", "missed_weight", "short_notice"}
    actual = {s for s, c in fs.STATUS_CATEGORY.items() if c == "warning"}
    assert actual == expected


def test_blocking_category_membership():
    expected = {"out", "withdrawn", "replaced", "inactive", "duplicate_or_bad_row"}
    actual = {s for s, c in fs.STATUS_CATEGORY.items() if c == "blocking"}
    assert actual == expected


def test_each_status_belongs_to_exactly_one_category():
    # The §5 invariant: no value is both warning and blocking.
    for status in V1_STATUS_VALUES:
        category = fs.STATUS_CATEGORY[status]
        # implicit single-key dict lookup already enforces "exactly one";
        # re-state it for the reader.
        assert isinstance(category, str)


# --- predicate helpers ------------------------------------------------------


def test_is_active_positive_and_negative():
    assert fs.is_active("active") is True
    for other in V1_STATUS_VALUES - {"active"}:
        assert fs.is_active(other) is False, other


def test_is_warning_positive_and_negative():
    warnings = {"needs_review", "questionable", "missed_weight", "short_notice"}
    for s in warnings:
        assert fs.is_warning(s) is True, s
    for s in V1_STATUS_VALUES - warnings:
        assert fs.is_warning(s) is False, s


def test_is_blocking_positive_and_negative():
    blocking = {"out", "withdrawn", "replaced", "inactive", "duplicate_or_bad_row"}
    for s in blocking:
        assert fs.is_blocking(s) is True, s
    for s in V1_STATUS_VALUES - blocking:
        assert fs.is_blocking(s) is False, s


def test_category_for_returns_mapped_value():
    assert fs.category_for("active") == "active"
    assert fs.category_for("questionable") == "warning"
    assert fs.category_for("out") == "blocking"


# --- validation -------------------------------------------------------------


def test_validate_status_returns_value_on_known_status():
    for s in V1_STATUS_VALUES:
        assert fs.validate_status(s) == s


def test_validate_status_rejects_unknown_value():
    with pytest.raises(ValueError):
        fs.validate_status("late_replacement")  # old v0 stub value, removed in v1


def test_validate_status_rejects_empty_string():
    with pytest.raises(ValueError):
        fs.validate_status("")


def test_validate_status_rejects_none():
    with pytest.raises(ValueError):
        fs.validate_status(None)  # type: ignore[arg-type]


def test_category_for_rejects_unknown_value():
    with pytest.raises(ValueError):
        fs.category_for("not_a_status")


def test_is_blocking_rejects_unknown_value():
    with pytest.raises(ValueError):
        fs.is_blocking("not_a_status")


# --- resolver (§15 Phase A, §16) -------------------------------------------


def test_resolver_active_no_override():
    assert fs.resolve_effective_fighter_status("active", None) == "active"


def test_resolver_inactive_no_override():
    assert fs.resolve_effective_fighter_status("inactive", None) == "inactive"


def test_resolver_manual_override_wins_over_active_base():
    assert fs.resolve_effective_fighter_status("active", "out") == "out"


def test_resolver_manual_override_wins_over_inactive_base():
    # User can re-mark an importer-deactivated row as active.
    assert fs.resolve_effective_fighter_status("inactive", "active") == "active"


def test_resolver_manual_override_wins_for_every_v1_value():
    for override in V1_STATUS_VALUES:
        assert (
            fs.resolve_effective_fighter_status("active", override) == override
        )


def test_resolver_empty_base_no_override_falls_back_conservatively():
    # Missing importer value AND no user override -> conservative
    # warning-category default. Per the resolver contract: never
    # silently treat an unknown row as active, never silently block it.
    resolved = fs.resolve_effective_fighter_status(None, None)
    assert resolved == fs.DEFAULT_BASE_STATUS
    assert fs.is_warning(resolved)


def test_resolver_empty_string_base_no_override_falls_back_conservatively():
    resolved = fs.resolve_effective_fighter_status("", None)
    assert resolved == fs.DEFAULT_BASE_STATUS


def test_resolver_empty_base_with_manual_override_uses_override():
    # Manual override always wins, even if the importer never wrote
    # a base value.
    assert fs.resolve_effective_fighter_status(None, "out") == "out"


def test_resolver_rejects_unknown_base_status():
    with pytest.raises(ValueError):
        fs.resolve_effective_fighter_status("late_replacement", None)


def test_resolver_rejects_unknown_manual_override():
    with pytest.raises(ValueError):
        fs.resolve_effective_fighter_status("active", "bogus")


def test_default_base_status_is_in_warning_category():
    # Locks the "conservative fallback never silently blocks or
    # silently activates" property of DEFAULT_BASE_STATUS.
    assert fs.DEFAULT_BASE_STATUS in fs.ALLOWED_STATUSES
    assert fs.STATUS_CATEGORY[fs.DEFAULT_BASE_STATUS] == "warning"
