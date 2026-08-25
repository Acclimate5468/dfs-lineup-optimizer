"""Reusable fight-group apply/write core (Streamlit-free).

Realizes ``docs/FIGHT_GROUP_APPLY_SERVICE_DESIGN.md`` §3 / Slice B (§4): the
fight-group apply write was previously page-private in
``app/pages/02_fight_groups.py``. This module lifts it verbatim into a
reusable, Streamlit-free ``src/slate`` service so the Fight Groups page
(Region D pasted card + Region E DK Game Info) and a future Build Step 1
button all drive the *same* create primitive and cannot diverge.

Boundary (design §3.1):

- No Streamlit import — strings for the user are returned as data; the page
  formats them.
- Persistence is repository-only (``FightGroupRepository`` /
  ``FighterRepository``); no raw SQL, no schema change, no migration.
- The caller owns the connection lifecycle and passes a live
  ``sqlite3.Connection`` in; the service never opens or closes it.

Transaction boundary is preserved exactly as it was on the page:
``FightGroupRepository.create`` commits per row (design §3.4). The win of the
extraction is that the service is now the single owner of the write, so a
later, separately designed slice can make the batch atomic without touching
any caller. Not done here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.db.repositories import FighterRepository, FightGroupRepository
from src.slate.fight_grouping import (
    detect_main_event_pair,
    group_fighters_by_game_info,
)
from src.utils.text_cleaning import normalize_name


@dataclass(frozen=True)
class GroupApplyOutcome:
    """Result of the shared create loop over canonical ``(name_1, name_2)`` pairs.

    Field names mirror the dict keys the page render functions previously read,
    so the repoint is a field-access change only (design §3.2). ``five_round``
    names the auto-detected main event created at 5 rounds, or ``None``.
    """

    created: tuple[tuple[str, str], ...]
    skipped_grouped: tuple[str, ...]
    skipped_exists: tuple[str, ...]
    errors: tuple[str, ...]
    five_round: str | None


@dataclass(frozen=True)
class GameInfoApplyResult:
    """Region E / Build apply result — the outcome plus the suggestion count."""

    slate_id: int
    eligible: int
    outcome: GroupApplyOutcome


def create_groups_for_pairs(
    repo: FightGroupRepository,
    slate_id: int,
    pairs: list,
    grouped_norms: set,
    existing_pairs: set,
    include_grouped: bool,
    five_round_pair_key: frozenset | None = None,
) -> GroupApplyOutcome:
    """Create one unconfirmed group per eligible, non-conflicting pair.

    The shared write core behind Region D (pasted card), Region E (DK Game Info
    suggestions), and a future Build button — design §3.3,
    ``docs/DK_GAME_INFO_PAIRING_DESIGN.md`` §4.3 /
    ``docs/FIGHT_GROUPS_UX_DESIGN.md`` §9.6 / §9.7. Both regions reduce their
    domain object to canonical ``(name_1, name_2)`` pairs and feed this single
    primitive, so the apply paths cannot diverge. Each pair is gated against the
    *current* slate state passed in by the caller: an identical saved pair
    (either order) is skipped (idempotent re-click), and a pair naming an
    already-grouped fighter is skipped unless ``include_grouped`` is set. Writes
    go through ``FightGroupRepository.create`` only (``status='unconfirmed'``) —
    create-only, never an update or delete (§9.6 rule 6). Each group defaults to
    ``scheduled_rounds=3`` except the pair whose normalized-name key equals
    ``five_round_pair_key`` (the auto-detected main event; ``None`` when absent
    or ambiguous), which is created at 5 rounds and named in
    ``GroupApplyOutcome.five_round``. Callers pre-filter to valid pairs, so no
    ``create`` fails mid-batch and the eligible pairs apply all-or-nothing in
    practice.
    """
    created: list = []
    skipped_grouped: list = []
    skipped_exists: list = []
    errors: list = []
    five_round: str | None = None

    # Mutable copies so a create within this batch updates the conflict sets.
    grouped = set(grouped_norms)
    pair_set = set(existing_pairs)

    for f1, f2 in pairs:
        n1 = normalize_name(f1)
        n2 = normalize_name(f2)
        pair_key = frozenset((n1, n2))
        # Idempotence backstop: an identical pair already saved (either order)
        # is never re-created, regardless of the opt-in.
        if pair_key in pair_set:
            skipped_exists.append(f"{f1} vs {f2}")
            continue
        conflicts = [name for name, norm in ((f1, n1), (f2, n2)) if norm in grouped]
        if conflicts and not include_grouped:
            skipped_grouped.append(
                f"{f1} vs {f2} — {', '.join(conflicts)} already grouped"
            )
            continue
        is_main_event = (
            five_round_pair_key is not None and pair_key == five_round_pair_key
        )
        try:
            rec = repo.create(
                slate_id=slate_id,
                fighter_1_name=f1,
                fighter_2_name=f2,
                scheduled_rounds=5 if is_main_event else 3,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced in the apply summary
            errors.append(f"{f1} vs {f2}: {exc}")
            continue
        created.append((rec.fighter_1_name, rec.fighter_2_name))
        if is_main_event:
            five_round = f"{rec.fighter_1_name} vs {rec.fighter_2_name}"
        pair_set.add(pair_key)
        grouped.update({n1, n2})

    return GroupApplyOutcome(
        created=tuple(created),
        skipped_grouped=tuple(skipped_grouped),
        skipped_exists=tuple(skipped_exists),
        errors=tuple(errors),
        five_round=five_round,
    )


def compute_apply_context(
    repo: FightGroupRepository,
    slate_id: int,
    active_roster: list,
) -> tuple[set, set, set]:
    """Build the conflict sets for a slate's create loop (design §3.3).

    Lifts the construction duplicated across the two page callbacks into one
    place. Returns ``(roster_norms, grouped_norms, existing_pairs)``:

    - ``roster_norms`` — normalized names of the active roster.
    - ``grouped_norms`` — a fighter is "already grouped" only when a group slot
      normalize-matches an active roster fighter — the same join Region A uses,
      so an off-roster typo slot never blocks an apply.
    - ``existing_pairs`` — every saved pair as a ``frozenset`` of normalized
      names, for the idempotence gate.
    """
    roster_norms = {normalize_name(f.name) for f in active_roster}
    groups = repo.list_for_slate(slate_id)
    grouped_norms = {
        normalize_name(nm)
        for grp in groups
        for nm in (grp.fighter_1_name, grp.fighter_2_name)
        if normalize_name(nm) in roster_norms
    }
    existing_pairs = {
        frozenset(
            (normalize_name(grp.fighter_1_name), normalize_name(grp.fighter_2_name))
        )
        for grp in groups
    }
    return roster_norms, grouped_norms, existing_pairs


def apply_game_info_pairings(
    conn: sqlite3.Connection,
    slate_id: int,
    *,
    include_grouped: bool = False,
    auto_set_main_event: bool = True,
) -> GameInfoApplyResult:
    """Apply DK Game Info suggested pairings for a slate (design §3.3).

    The Region E (and future Build) entry point. Recomputes the suggestions from
    the *current* persisted roster (the source of truth, so a re-call under
    unchanged state creates zero groups), reduces them to canonical
    ``(name_1, name_2)`` pairs, optionally auto-detects the main event (latest
    Game Info start) to create at 5 rounds, builds the conflict sets via
    :func:`compute_apply_context`, and defers the create loop to
    :func:`create_groups_for_pairs`. ``auto_set_main_event=False`` creates every
    group at 3 rounds and leaves ``five_round`` ``None``. Streamlit-free; the
    caller owns the connection.
    """
    repo = FightGroupRepository(conn)
    active = [
        f
        for f in FighterRepository(conn).list_for_slate(slate_id)
        if f.status == "active"
    ]
    suggestions = group_fighters_by_game_info(active)
    pairs = [
        (p.fighter_1_name, p.fighter_2_name) for p in suggestions.suggested_pairs
    ]
    five_round_pair_key: frozenset | None = None
    if auto_set_main_event:
        main_event = detect_main_event_pair(suggestions.suggested_pairs)
        if main_event is not None:
            five_round_pair_key = frozenset(
                (
                    normalize_name(main_event.fighter_1_name),
                    normalize_name(main_event.fighter_2_name),
                )
            )
    _roster_norms, grouped_norms, existing_pairs = compute_apply_context(
        repo, slate_id, active
    )
    outcome = create_groups_for_pairs(
        repo,
        slate_id,
        pairs,
        grouped_norms,
        existing_pairs,
        include_grouped,
        five_round_pair_key=five_round_pair_key,
    )
    return GameInfoApplyResult(
        slate_id=slate_id, eligible=len(pairs), outcome=outcome
    )
