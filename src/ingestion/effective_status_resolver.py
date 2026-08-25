"""Pure resolver for ``odds_match_results`` bindings.

Phase D.4.1 (``docs/ODDS_PERSISTENCE_DESIGN.md`` §15.9) introduced the
``effective_status`` half; Phase D.5.1 (§16.6) adds the *binding* half so
``accept_match`` / ``force_pair`` overrides can rebind a row to a fighter
the matcher missed.

The resolver realizes §8's rule dispatch as a pure function so the
repository apply pass (D.4.2/D.5.1) and the recompute wiring (D.4.3) can
compose it without re-encoding the rule order. The §8 precedence is
``reject_match`` > ``force_pair`` > ``accept_match``; supersession
(§16.4) keeps at most one resolution override active per
``odds_row_key``, so the precedence ordering is a defensive tiebreak
rather than a live conflict path.

Implemented today: rule 2 (``reject_match`` → ``review_rejected``),
rule 3 (``force_pair`` → ``force_pair``), rule 4 (``accept_match`` →
``review_accepted``). Rules 1, 5, 6 (``mark_excluded``,
``manual_moneyline``, ``manual_projection_low_confidence``) are explicit
positional comments that fall through to rule 7 (mirror ``match_status``,
keep the matcher's ``fighter_id``).

The caller is expected to pass the *active* override set
(``ManualMatchOverrideRepository.list_active_for_slate`` already filters
``superseded_at IS NULL``). As a belt-and-braces defense the resolver
also drops any override whose ``superseded_at`` is non-null so a stale
list cannot leak through.

The resolver is *purely* a mirror of override state: it never consults
fighter active-status (a binding to a since-deactivated fighter is a
*stale* case handled by the DB-aware apply pass, §16.12) and never reads
the DB. ``resolve_effective_status`` stays available as a thin string
wrapper so D.4 callers and table-driven tests are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# Effective-status values the resolver can emit.
REVIEW_REJECTED = "review_rejected"
REVIEW_ACCEPTED = "review_accepted"
# The matcher's own positive verdict; also a (mirrored) effective_status.
AUTO_MATCH = "auto_match"

# Override types in the mutually-exclusive resolution set (§16.4).
REJECT_MATCH = "reject_match"
ACCEPT_MATCH = "accept_match"
# ``force_pair`` is both an override_type and its resulting effective_status
# (§16.6) — one string serves both roles.
FORCE_PAIR = "force_pair"

# Binding override types — they rebind ``fighter_id`` to the override's
# chosen fighter rather than just flipping ``effective_status`` (§16.5).
BINDING_OVERRIDE_TYPES = (ACCEPT_MATCH, FORCE_PAIR)
# The full mutually-exclusive resolution set (§16.4 supersession scope).
RESOLUTION_OVERRIDE_TYPES = (REJECT_MATCH, ACCEPT_MATCH, FORCE_PAIR)

# Effective-status values that feed Projection v1 / Build (§16.9 /
# PROJECTION_V1_DESIGN §11). The matcher's ``auto_match`` plus the two D.5
# manual-binding outputs. This is the single predicate the projection
# aggregator reads (D.5.2) and that the Odds "resolved?" view will read
# (D.5.3), so the two surfaces cannot diverge (§16.16 risk #1). Blocked
# (no win probability): ``review_required``, ``unmatched``,
# ``review_rejected``, and any future status not listed here.
PROJECTION_ELIGIBLE_EFFECTIVE_STATUSES = frozenset({
    AUTO_MATCH,
    REVIEW_ACCEPTED,
    FORCE_PAIR,
})


def is_projection_eligible_effective_status(status: str) -> bool:
    """True when an ``odds_match_results.effective_status`` feeds projections.

    Eligible (§16.9): ``auto_match``, ``review_accepted`` (from
    ``accept_match``), ``force_pair``. Everything else — ``review_required``,
    ``unmatched``, ``review_rejected``, and any not-yet-implemented status —
    is blocked and contributes no win probability.
    """
    return status in PROJECTION_ELIGIBLE_EFFECTIVE_STATUSES


@dataclass(frozen=True)
class MatchBinding:
    """The resolved ``(effective_status, fighter_id)`` for one result row.

    ``fighter_id`` is the matcher's own value for the reject and mirror
    rules (the apply pass writes a no-op on that column), and the
    override's chosen fighter for ``accept_match`` / ``force_pair``
    (§16.5/§16.6).
    """

    effective_status: str
    fighter_id: int | None


def resolve_match_binding(match_result, active_overrides: Iterable) -> MatchBinding:
    """Resolve the ``(effective_status, fighter_id)`` binding for one row.

    ``match_result`` must expose ``slate_id``, ``odds_row_key``,
    ``fighter_id`` (``int | None``), and ``match_status``.
    ``active_overrides`` is an iterable of ``ManualMatchOverrideRecord``-
    shaped objects exposing ``id``, ``slate_id``, ``odds_row_key``,
    ``fighter_id``, ``override_type``, and ``superseded_at``.

    Scoping (§15.6 step 3–4):

    - Slate mismatch → ignore.
    - ``odds_row_key`` mismatch → ignore.
    - ``reject_match`` with both sides carrying a ``fighter_id`` →
      require equality (defensive row-scoping; §15.11.6 keeps the check
      off when either side is ``None``).
    - ``accept_match`` / ``force_pair`` → never filtered on the result
      row's current ``fighter_id``: the whole point is to rebind to a
      fighter the matcher did *not* choose (§16.3), so the override's
      ``fighter_id`` is the *target*, not a filter.
    - ``superseded_at`` not ``None`` → ignore (defensive; callers should
      already filter).

    Precedence (§8 / §16.4): ``reject_match`` > ``force_pair`` >
    ``accept_match``. Unsupported override types fall through to rule 7
    (mirror ``match_status``, keep the matcher's ``fighter_id``).
    """
    applicable = [
        ov for ov in active_overrides if _override_applies(ov, match_result)
    ]

    # Rule 1 — mark_excluded (D.5+): TODO, fall through.
    # Rule 2 — reject_match → review_rejected (fighter_id left as matcher's).
    if any(ov.override_type == REJECT_MATCH for ov in applicable):
        return MatchBinding(REVIEW_REJECTED, match_result.fighter_id)
    # Rule 3 — force_pair → force_pair, bind the override's chosen fighter.
    force = _select_binding_override(applicable, FORCE_PAIR)
    if force is not None:
        return MatchBinding(FORCE_PAIR, _override_fighter_id(force))
    # Rule 4 — accept_match → review_accepted, bind the override's fighter.
    accept = _select_binding_override(applicable, ACCEPT_MATCH)
    if accept is not None:
        return MatchBinding(REVIEW_ACCEPTED, _override_fighter_id(accept))
    # Rule 5 — manual_moneyline (D.5+): TODO, fall through.
    # Rule 6 — manual_projection_low_confidence (D.5+): TODO, fall through.
    # Rule 7 — no applicable override → mirror match_status, keep fighter_id.
    return MatchBinding(match_result.match_status, match_result.fighter_id)


def resolve_effective_status(match_result, active_overrides: Iterable) -> str:
    """Thin wrapper returning only the binding's ``effective_status``.

    Kept so D.4 callers and the table-driven D.4.1 tests that pin the
    string contract are untouched (§16.6).
    """
    return resolve_match_binding(match_result, active_overrides).effective_status


def _override_applies(ov, match_result) -> bool:
    if getattr(ov, "superseded_at", None) is not None:
        return False
    if int(ov.slate_id) != int(match_result.slate_id):
        return False
    if ov.odds_row_key != match_result.odds_row_key:
        return False
    # Binding overrides rebind to a fighter the matcher did not choose, so
    # the override's fighter_id is the target — never a filter against the
    # result row's current fighter_id (§16.3).
    if ov.override_type in BINDING_OVERRIDE_TYPES:
        return True
    # reject_match (and any other row-scoped type): enforce fighter equality
    # only when BOTH sides carry a fighter_id (§15.11.6).
    if (
        ov.fighter_id is not None
        and match_result.fighter_id is not None
        and int(ov.fighter_id) != int(match_result.fighter_id)
    ):
        return False
    return True


def _select_binding_override(applicable, override_type):
    """Pick the binding override of ``override_type`` to apply.

    Supersession (§16.4) guarantees at most one active resolution override
    per ``odds_row_key``, so this normally sees zero or one match. If a
    future writer ever leaves two active, the most-recently-created
    (max ``id``) wins — deterministic regardless of input order.
    """
    matching = [ov for ov in applicable if ov.override_type == override_type]
    if not matching:
        return None
    return max(matching, key=lambda ov: int(ov.id))


def _override_fighter_id(ov) -> int | None:
    return int(ov.fighter_id) if ov.fighter_id is not None else None
