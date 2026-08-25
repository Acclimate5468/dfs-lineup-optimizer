"""Fight Groups — manual pairing skeleton (v0)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.prototype_mode import lock_to_build_page  # noqa: E402

from src.db.connection import get_connection  # noqa: E402
from src.db.migrations import bootstrap_database  # noqa: E402
from src.db.repositories import (  # noqa: E402
    FighterRepository,
    FightGroupRepository,
    SlateRepository,
)
from src.slate.fight_card_parser import (  # noqa: E402
    BAND_AMBIGUOUS,
    PARSE_ERROR,
    parse_fight_card,
    summarize,
)
from src.slate.fight_group_apply_service import (  # noqa: E402
    GameInfoApplyResult,
    GroupApplyOutcome,
    apply_game_info_pairings,
    compute_apply_context,
    create_groups_for_pairs,
)
from src.slate.fight_grouping import (  # noqa: E402
    group_fighters_by_game_info,
)
from src.slate.fighter_status_service import (  # noqa: E402
    list_fighter_status_rows,
)
from src.utils.text_cleaning import normalize_name  # noqa: E402

st.set_page_config(page_title="Fight Groups — DK Lineup Lab", layout="wide")
# Required setup page: reachable from the Build gate's "Fix fight groups" jump
# even in prototype mode (nav stays hidden; a Back-to-Build control is added).
lock_to_build_page("Fight Groups", allow_in_prototype=True)
st.title("Fight Groups")

st.caption(
    "Review and confirm the fight card for a slate. Nothing is grouped "
    "automatically — you apply DK Game Info suggestions and confirm groups "
    "explicitly."
)


def _conn():
    conn = get_connection()
    bootstrap_database(conn)
    return conn


def _coverage_status(match_count: int) -> str:
    """Coverage label for one active fighter given how many groups match.

    Implements docs/FIGHT_GROUPS_UX_DESIGN.md §3 Region A: ``ungrouped`` (no
    matching fight group), ``grouped`` (exactly one), ``duplicate`` (two or
    more — a fighter assigned to multiple active groups).
    """
    if match_count == 0:
        return "ungrouped"
    if match_count == 1:
        return "grouped"
    return "duplicate"


# A3 — scheduled-rounds UX (docs/FIGHT_GROUPS_UX_DESIGN.md §8 A3). Most UFC
# bouts are 3 rounds; the main event and any title fight run 5. Five rounds
# feeds five_round_bonus in the projection formula (docs/DEVELOPMENT_NOTES.md §4), so an
# unmarked 5-round main event silently undersells that fighter. These labels
# make the 3-vs-5 choice explicit in the add form, and _rounds_cell makes
# 5-round fights stand out in the roster / groups displays. Display + form copy
# only — no schema, migration, repository, or projection change.
_ROUNDS_RADIO_LABELS = {
    3: "3 rounds — standard bout",
    5: "5 rounds — main event / title bout",
}


def _rounds_cell(scheduled_rounds: int) -> str:
    """Display label for a group's scheduled rounds.

    Five-round bouts are spelled out so they are obvious against the uniform
    ``3 rd`` rows; three-round bouts keep the terse form.
    """
    if int(scheduled_rounds) == 5:
        return "5 rd — main event/title"
    return f"{int(scheduled_rounds)} rd"


# Region D session-state keys (docs/FIGHT_GROUPS_UX_DESIGN.md §9.5 / §9.6,
# A4.4). The Parse / Preview snapshot is stashed so the Apply click acts on the
# same parsed rows; the Apply result is stashed by the on_click handler for the
# body to render. No DB connection is ever stored. The Apply runs in an
# on_click callback (before the script body) so Region A / Region C re-read the
# new groups on the same run — no st.rerun, which keeps the rendered tree clean.
_PREVIEW_STATE_KEY = "assisted_preview"
_APPLY_RESULT_KEY = "assisted_apply_result"
_INCLUDE_GROUPED_KEY = "assisted_include_grouped"

# Region E session-state keys (docs/DK_GAME_INFO_PAIRING_DESIGN.md §4). The
# selected slate is stashed so the Apply on_click handler (which runs before the
# body) knows which slate to act on; the Apply result is stashed for the body to
# render once. Suggestions are recomputed from the persisted roster on every
# render and at click time, so — unlike Region D — there is no preview snapshot
# that can go stale. No DB connection is ever stored.
_GI_SLATE_KEY = "game_info_apply_slate"
_GI_APPLY_RESULT_KEY = "game_info_apply_result"
_GI_INCLUDE_GROUPED_KEY = "game_info_include_grouped"

# Fight-card quick actions (this slice). "Confirm all groups" and "set 5-round
# main event" are explicit-click writes run in on_click callbacks before the
# body re-renders — same write-safety contract as Region D / E: no page-load
# write, one page-owned transaction per click via FightGroupRepository, and a
# result stashed for the body to render once. The selectbox key stores the
# chosen group id for the set-5-round handler.
_CONFIRM_ALL_SLATE_KEY = "confirm_all_slate"
_CONFIRM_ALL_RESULT_KEY = "confirm_all_result"
_FIVE_ROUND_SLATE_KEY = "five_round_slate"
_FIVE_ROUND_SELECT_KEY = "five_round_group_select"
_FIVE_ROUND_RESULT_KEY = "five_round_result"


@dataclass(frozen=True)
class _CardApplyResult:
    """Region D (pasted card) apply result — outcome plus the parser counts.

    Composes the shared :class:`GroupApplyOutcome` with Region D's extra
    ``blocked`` count (design §3.2). Region D is not a Build consumer, so it
    keeps this page-side wrapper rather than a service dataclass.
    """

    slate_id: int
    eligible: int
    blocked: int
    outcome: GroupApplyOutcome


def _apply_pairings(
    repo: FightGroupRepository,
    slate_id: int,
    bouts: list,
    grouped_norms: set,
    existing_pairs: set,
    include_grouped: bool,
) -> _CardApplyResult:
    """Region D adapter over the shared :func:`create_groups_for_pairs` core.

    Implements docs/FIGHT_GROUPS_UX_DESIGN.md §9.6 (apply rules) and §9.7
    (scheduled rounds). Only parser-eligible bouts (rules 1–4: parsed, both
    sides resolved, distinct fighters, no cross-row duplicate) become candidate
    ``(name_1, name_2)`` pairs; the already-grouped gate (rule 5) and the
    idempotence backstop live in the shared core. ``blocked`` counts the
    parser-ineligible rows, which are never applied.
    """
    pairs = [
        (b.side_1.matched_name, b.side_2.matched_name) for b in bouts if b.eligible
    ]
    outcome = create_groups_for_pairs(
        repo, slate_id, pairs, grouped_norms, existing_pairs, include_grouped
    )
    return _CardApplyResult(
        slate_id=slate_id,
        eligible=len(pairs),
        # Parser-blocked rows (rules 1–4) are never applied.
        blocked=sum(1 for b in bouts if not b.eligible),
        outcome=outcome,
    )


def _render_apply_result(result: _CardApplyResult) -> None:
    """Render the summary of the most recent Apply (§9.6 result feedback).

    Created groups default to 3 rounds (§9.7); the reminder nudges the user to
    set any 5-round main event / title bout manually. Skipped rows name a
    fighter already in a group (or an identical pair already saved) and leave
    existing groups untouched.
    """
    created = result.outcome.created
    skip_lines = list(result.outcome.skipped_grouped) + [
        f"{pair} — pair already saved" for pair in result.outcome.skipped_exists
    ]
    if created:
        st.success(
            f"Applied {len(created)} new fight group(s): "
            + "; ".join(f"{a} vs {b}" for a, b in created)
            + ". New groups default to 3 rounds — set any 5-round main event / "
            "title bout manually below before Manual Review."
        )
        if skip_lines:
            st.warning(
                f"Skipped {len(skip_lines)} already-grouped pairing(s): "
                + "; ".join(skip_lines)
                + ". Existing groups were left unchanged (no overwrite, no delete)."
            )
    elif result.eligible:
        st.info(
            f"No new fight groups created — all {result.eligible} eligible "
            "pairing(s) name fighters that are already grouped, so they were "
            "skipped and existing groups were left unchanged. Tick “Include "
            "rows whose fighters are already grouped” above to add a second "
            "group anyway."
        )
        if skip_lines:
            st.caption("Skipped: " + "; ".join(skip_lines))
    else:
        st.info("No eligible pairings to apply.")
    if result.blocked:
        st.caption(
            f"{result.blocked} pasted row(s) were not eligible (parse error, "
            "unmatched, ambiguous, self-pair, or duplicate) and were not applied."
        )
    if result.outcome.errors:
        st.error("Could not save some pairings: " + "; ".join(result.outcome.errors))


def _apply_card_callback() -> None:
    """on_click handler for Apply Valid Pairings — the only Region D write.

    Runs at the start of the Apply rerun, before the script body renders, so
    the freshly created groups show up in Region A / Region C on the same run
    without an explicit st.rerun (docs/DEVELOPMENT_NOTES.md §11: one page-owned write per
    click). It reads the stashed parser snapshot and the *current*
    already-grouped state from the DB (so a re-click is idempotent), defers the
    create loop to :func:`_apply_pairings`, and stashes the result for the body
    to render once. The Apply button only renders for a fresh preview, so the
    snapshot is never stale at click time.
    """
    stored = st.session_state.get(_PREVIEW_STATE_KEY)
    if not stored or not stored.get("bouts"):
        return
    slate_id = stored["slate_id"]
    include_grouped = bool(st.session_state.get(_INCLUDE_GROUPED_KEY, False))
    conn = get_connection()
    bootstrap_database(conn)
    try:
        repo = FightGroupRepository(conn)
        active = [
            f
            for f in FighterRepository(conn).list_for_slate(slate_id)
            if f.status == "active"
        ]
        _roster_norms, grouped_norms, existing_pairs = compute_apply_context(
            repo, slate_id, active
        )
        st.session_state[_APPLY_RESULT_KEY] = _apply_pairings(
            repo,
            slate_id,
            stored["bouts"],
            grouped_norms,
            existing_pairs,
            include_grouped,
        )
    finally:
        conn.close()


def _render_game_info_apply_result(result: GameInfoApplyResult) -> None:
    """Render the summary of the most recent Region E Apply (design §4.3).

    The auto-detected main event (latest Game Info start) is created at 5 rounds
    and called out so the user can confirm or override it; every other group
    defaults to 3 rounds. Skipped lines name a fighter already in a group (or a
    pair already saved) and leave existing groups untouched.
    """
    created = result.outcome.created
    five_round = result.outcome.five_round
    skip_lines = list(result.outcome.skipped_grouped) + [
        f"{pair} — pair already saved" for pair in result.outcome.skipped_exists
    ]
    if created:
        st.success(
            f"Applied {len(created)} new fight group(s) from DK Game Info: "
            + "; ".join(f"{a} vs {b}" for a, b in created)
            + "."
        )
        if five_round:
            st.info(
                f"Detected main event from the latest Game Info start time: "
                f"**{five_round}** — set to **5 rounds** automatically. If a "
                "different bout is the 5-round fight (e.g. a title bout below "
                "the main event), change it below."
            )
        else:
            st.caption(
                "No main event auto-detected (Game Info start times missing or "
                "ambiguous) — new groups are 3 rounds; set the 5-round main "
                "event / title bout manually below before Manual Review."
            )
        if skip_lines:
            st.warning(
                f"Skipped {len(skip_lines)} pairing(s): "
                + "; ".join(skip_lines)
                + ". Existing groups were left unchanged (no overwrite, no delete)."
            )
    elif result.eligible:
        st.info(
            f"No new fight groups created — all {result.eligible} suggested "
            "pairing(s) name fighters that are already grouped, so they were "
            "skipped and existing groups were left unchanged. Tick “Include "
            "suggestions whose fighters are already grouped” above to add a "
            "second group anyway."
        )
        if skip_lines:
            st.caption("Skipped: " + "; ".join(skip_lines))
    else:
        st.info("No suggested DK pairings to apply.")
    if result.outcome.errors:
        st.error("Could not save some pairings: " + "; ".join(result.outcome.errors))


def _apply_game_info_callback() -> None:
    """on_click handler for Apply Suggested DK Pairings — the only Region E write.

    Mirrors :func:`_apply_card_callback` (docs/DK_GAME_INFO_PAIRING_DESIGN.md
    §4.3): runs at the start of the Apply rerun, before the script body, so the
    freshly created groups show up in Region A / Region C on the same run
    without an explicit ``st.rerun``. A thin shell over
    ``apply_game_info_pairings`` (src/slate/fight_group_apply_service.py), which
    recomputes the suggestions from the *current* persisted roster (so a
    re-click is idempotent), auto-detects the main event, and defers the create
    loop to the shared service core. The slate id comes from session state
    (stashed by the Region E body) rather than the selectbox.
    """
    slate_id = st.session_state.get(_GI_SLATE_KEY)
    if slate_id is None:
        return
    include_grouped = bool(st.session_state.get(_GI_INCLUDE_GROUPED_KEY, False))
    conn = get_connection()
    bootstrap_database(conn)
    try:
        st.session_state[_GI_APPLY_RESULT_KEY] = apply_game_info_pairings(
            conn, slate_id, include_grouped=include_grouped
        )
    finally:
        conn.close()


def _confirm_all_callback() -> None:
    """on_click handler for "Confirm all groups" — the confirm-all write path.

    Marks every unconfirmed fight group on the slate confirmed in one
    transaction via ``FightGroupRepository.confirm_all_for_slate``. Runs before
    the body re-renders (docs/DEVELOPMENT_NOTES.md §11) so the fight-card metrics and the
    top-of-page banner refresh on the same run without an explicit ``st.rerun``.
    It never creates a group and never touches ``scheduled_rounds`` (status-only
    UPDATE), and is idempotent — a re-click confirms zero rows. The slate id
    comes from session state (stashed by the body), not the selectbox.
    """
    slate_id = st.session_state.get(_CONFIRM_ALL_SLATE_KEY)
    if slate_id is None:
        return
    conn = get_connection()
    bootstrap_database(conn)
    try:
        count = FightGroupRepository(conn).confirm_all_for_slate(slate_id)
        st.session_state[_CONFIRM_ALL_RESULT_KEY] = {
            "slate_id": slate_id,
            "count": count,
        }
    finally:
        conn.close()


def _set_five_round_callback() -> None:
    """on_click handler for "Set selected fight to 5 rounds".

    Sets exactly the user-selected group's ``scheduled_rounds`` to 5 via
    ``FightGroupRepository.update_scheduled_rounds`` and leaves every other
    group — and all confirmed statuses — untouched. Rounds are never inferred:
    the user names the main event / title bout explicitly (FIGHT_GROUPS_UX_DESIGN
    §9.7 / §8 A3). Runs before the body so the change shows on the same run; the
    selected group id is read from the selectbox's session-state key.
    """
    slate_id = st.session_state.get(_FIVE_ROUND_SLATE_KEY)
    group_id = st.session_state.get(_FIVE_ROUND_SELECT_KEY)
    if slate_id is None or group_id is None:
        return
    conn = get_connection()
    bootstrap_database(conn)
    try:
        rec = FightGroupRepository(conn).update_scheduled_rounds(group_id, 5)
        st.session_state[_FIVE_ROUND_RESULT_KEY] = {
            "slate_id": slate_id,
            "label": f"{rec.fighter_1_name} vs {rec.fighter_2_name}",
        }
    except Exception as exc:  # noqa: BLE001 — surfaced in the body
        st.session_state[_FIVE_ROUND_RESULT_KEY] = {
            "slate_id": slate_id,
            "error": str(exc),
        }
    finally:
        conn.close()


conn = _conn()
slate_repo = SlateRepository(conn)
fg_repo = FightGroupRepository(conn)

slates = slate_repo.list_all()
if not slates:
    st.warning(
        "No slates saved yet. Create or import a slate on the Build page, Step 1."
    )
    st.stop()

slate_label = {
    s.id: f"#{s.id} — {s.event_name}" + (f" ({s.event_date})" if s.event_date else "")
    for s in slates
}
selected_id = st.selectbox(
    "Slate",
    options=[s.id for s in slates],
    format_func=lambda sid: slate_label[sid],
)

# ---------------------------------------------------------------------------
# Fight card status banner (read-only). A single top-of-slate summary of how
# complete the fight card is, with the counts the workflow cares about and a
# clear next step. Pure reads + an in-Python normalized-name join — no
# INSERT / UPDATE / DELETE on page load (docs/DEVELOPMENT_NOTES.md §11). The detailed
# roster/coverage table and per-group controls render below.
# ---------------------------------------------------------------------------
roster = FighterRepository(conn).list_for_slate(selected_id)
roster_groups = fg_repo.list_for_slate(selected_id)
active_fighters = [f for f in roster if f.status == "active"]

# Normalized active-roster name -> first-seen fighter (display + match key).
roster_by_norm: dict = {}
for fighter in active_fighters:
    roster_by_norm.setdefault(normalize_name(fighter.name), fighter)


def _opponent_display(raw_name: str) -> str:
    """Roster name where the opponent slot resolves; raw group value otherwise."""
    match = roster_by_norm.get(normalize_name(raw_name))
    return match.name if match is not None else raw_name


# Per active fighter (by normalized name): the groups they appear in. Groups
# referencing a name not on the active roster collect into unmatched_groups.
matches_by_norm: dict = {}
unmatched_groups = []
for grp in roster_groups:
    n1 = normalize_name(grp.fighter_1_name)
    n2 = normalize_name(grp.fighter_2_name)
    matched_slots = 0
    if n1 in roster_by_norm:
        matches_by_norm.setdefault(n1, []).append(
            (grp, _opponent_display(grp.fighter_2_name))
        )
        matched_slots += 1
    if n2 in roster_by_norm:
        matches_by_norm.setdefault(n2, []).append(
            (grp, _opponent_display(grp.fighter_1_name))
        )
        matched_slots += 1
    if matched_slots < 2:
        unmatched_groups.append(grp)

total_active = len(active_fighters)
grouped_count = sum(
    1 for fighter in active_fighters if matches_by_norm.get(normalize_name(fighter.name))
)
five_round_count = sum(1 for grp in roster_groups if grp.scheduled_rounds == 5)
confirmed_count = sum(1 for grp in roster_groups if grp.status == "confirmed")

# Coverage rows, ungrouped first, then duplicate, then grouped; name tiebreak,
# so incomplete / problematic rows surface at the top of the roster table.
rank_order = {"ungrouped": 0, "duplicate": 1, "grouped": 2}
roster_rows = []
duplicate_names = []
for fighter in active_fighters:
    fmatches = sorted(
        matches_by_norm.get(normalize_name(fighter.name), []),
        key=lambda m: m[0].id,
    )
    coverage = _coverage_status(len(fmatches))
    if not fmatches:
        opponent = group_cell = group_status = rounds_cell = "—"
    else:
        first_group, first_opponent = fmatches[0]
        opponent = first_opponent
        group_cell = f"#{first_group.id}"
        group_status = first_group.status
        rounds_cell = _rounds_cell(first_group.scheduled_rounds)
        if coverage == "duplicate":
            group_cell = f"#{first_group.id} (+{len(fmatches) - 1} more)"
            duplicate_names.append(fighter.name)
    roster_rows.append(
        {
            "Fighter": fighter.name,
            "Salary": f"${fighter.salary:,}",
            "Paired Opponent": opponent,
            "Fight Group": group_cell,
            "Group Status": group_status,
            "Scheduled Rounds": rounds_cell,
            "Coverage": coverage,
            "_rank": rank_order[coverage],
            "_norm": normalize_name(fighter.name),
        }
    )
roster_rows.sort(key=lambda r: (r["_rank"], r["_norm"]))

# Per-fighter Fighter Status v1 category (active / warning / blocking) by
# normalized name, for display in the fight-card table below. Read-only via the
# Phase C aggregator; this is a display join only and does NOT promote
# effective_status into projections, the optimizer, alerts, or exports
# (FIGHTER_STATUS_V1_DESIGN §15 / docs/DEVELOPMENT_NOTES.md §10).
_status_by_norm: dict = {}
for _srow in list_fighter_status_rows(conn, selected_id):
    _status_by_norm.setdefault(normalize_name(_srow.name), _srow.category)

# DK Game Info suggestions (read-only) — the primary pairing workflow below. A
# pair is "ready" when neither fighter already resolves to a group; that is the
# same gate the Region E preview and Apply use.
game_info_present = any(f.game_info and f.game_info.strip() for f in active_fighters)
suggestions = group_fighters_by_game_info(active_fighters)


def _pair_already_grouped(pair) -> bool:
    """True when either side of a suggested pair already resolves to a group.

    Reuses the active-fighter→groups join (``matches_by_norm``) so the
    preview's skipped count matches what Apply will actually skip.
    """
    return (
        normalize_name(pair.fighter_1_name) in matches_by_norm
        or normalize_name(pair.fighter_2_name) in matches_by_norm
    )


grouped_pairs = [p for p in suggestions.suggested_pairs if _pair_already_grouped(p)]
ready_pairs = [p for p in suggestions.suggested_pairs if not _pair_already_grouped(p)]

# ---------------------------------------------------------------------------
# Status banner render. Derived purely from the read-only values above.
# ---------------------------------------------------------------------------
ungrouped_count = total_active - grouped_count
unconfirmed_count = len(roster_groups) - confirmed_count
duplicate_count = len(set(duplicate_names))
unmatched_count = len(unmatched_groups)
ready_count = len(ready_pairs)

banner_counts = (
    f"Active fighters: {total_active} · Grouped: {grouped_count} · "
    f"Ungrouped: {ungrouped_count} · Fight groups: {len(roster_groups)} · "
    f"Confirmed groups: {confirmed_count} · 5-round fights: {five_round_count}"
)
# A card is "complete" only when it is also clean: every active fighter grouped
# exactly once, no ungrouped fighters, all groups confirmed, no duplicate
# assignments, no off-roster slots, and at least one valid group.
card_clean = duplicate_count == 0 and unmatched_count == 0
card_complete = (
    total_active > 0
    and len(roster_groups) > 0
    and ungrouped_count == 0
    and unconfirmed_count == 0
    and card_clean
)
# Manual add/pasted-card tools open only when the card is unfinished AND there
# are no DK Game Info suggestions left to apply (primary workflow exhausted). A
# complete card keeps them collapsed so the page does not invite adding groups.
_advanced_open = not card_complete and ready_count == 0 and total_active >= 2
if card_complete:
    st.success(
        "**Fight card complete.** Every active fighter is grouped exactly "
        "once, every fight group is confirmed, and no group references an "
        "off-roster name.\n\n"
        f"{banner_counts}\n\n"
        "Next: Review Odds, then run Manual Review."
    )
elif not card_clean:
    # Integrity problems take priority over mere incompleteness.
    _issues: list[str] = []
    if duplicate_count > 0:
        _issues.append(f"{duplicate_count} fighter(s) assigned to more than one group")
    if unmatched_count > 0:
        _issues.append(f"{unmatched_count} group(s) reference an off-roster name")
    st.warning(
        "**Fight card needs review.** Resolve the issues below before this "
        "card is usable.\n\n"
        f"{banner_counts}\n\n"
        "Next: fix " + "; ".join(_issues) + " (see the fight card and roster "
        "coverage below), then confirm any remaining groups."
    )
else:
    _todo: list[str] = []
    if total_active == 0:
        _todo.append("create a slate and import a DK salary CSV on Build, Step 1")
    elif ready_count > 0:
        # DK Game Info is the primary workflow — point there before manual add.
        _todo.append(f"apply {ready_count} DK Game Info suggestion(s) below")
    elif ungrouped_count > 0:
        _todo.append(
            f"group {ungrouped_count} ungrouped fighter(s) "
            "(Advanced manual corrections)"
        )
    if unconfirmed_count > 0:
        _todo.append(f"confirm {unconfirmed_count} fight group(s)")
    _next = (
        "Next: " + "; ".join(_todo) + "."
        if _todo
        else "Next: review the fight card below."
    )
    st.info("**Fight card in progress.**\n\n" f"{banner_counts}\n\n" f"{_next}")

# ---------------------------------------------------------------------------
# DK Game Info suggestions — the primary pairing workflow (design §4).
#
# Renders directly under the banner, above the roster/coverage context. Both
# fighters in a DK bout carry the byte-identical Game Info string (§1.1), so
# group_fighters_by_game_info reconstructs the canonical pairs by exact-string
# grouping (computed once above). The preview is read-only; the explicit
# "Apply Suggested DK Pairings" button is the only write path (§4.3), sharing
# the create_groups_for_pairs service core with the pasted card. Scheduled
# rounds are never inferred —
# created groups default to 3 rounds / unconfirmed and the user sets any
# 5-round main event manually (§3.1). Renders only when at least one active
# fighter carries a Game Info value (§4.1).
# ---------------------------------------------------------------------------
if game_info_present:
    st.divider()
    st.subheader("Suggested DK pairings")

    # Stash the selected slate so the Apply on_click handler (which runs before
    # this body on the apply rerun) acts on the right slate.
    st.session_state[_GI_SLATE_KEY] = selected_id

    # Show the result of the most recent Apply (stashed by the on_click handler
    # that ran before this body), once, scoped to the current slate.
    _gi_result = st.session_state.pop(_GI_APPLY_RESULT_KEY, None)
    if _gi_result is not None and _gi_result.slate_id == selected_id:
        _render_game_info_apply_result(_gi_result)

    st.caption(
        "Imported fight-card suggestions built from the DK salary “Game Info” "
        "column — nothing is written on load. “Apply Suggested DK Pairings” "
        "creates one 3-round, unconfirmed fight group per suggested pair whose "
        "fighters are not already grouped; set any 5-round main event / title "
        "bout manually afterward. Rounds are never inferred from the salary file."
    )

    # When every suggested DK pairing already resolves to a saved group, say so
    # plainly so the user knows the primary workflow is done for this slate.
    if suggestions.suggested_pairs and not ready_pairs:
        st.success("All DK Game Info pairings are already applied.")

    st.markdown(
        f"**{suggestions.suggested_count} suggested pairing(s)** from DK Game "
        f"Info · {len(ready_pairs)} ready to apply · {len(grouped_pairs)} "
        f"already grouped (skipped) · {suggestions.incomplete_count} incomplete "
        f"· {suggestions.anomaly_count} anomaly · {suggestions.uncovered_count} "
        "uncovered (blank Game Info)."
    )

    if suggestions.suggested_pairs:
        preview_df = pd.DataFrame(
            [
                {
                    "Fighter 1": p.fighter_1_name,
                    "Fighter 2": p.fighter_2_name,
                    "Status": (
                        "already grouped" if _pair_already_grouped(p) else "ready"
                    ),
                    # Verbatim DK "Game Info" string the pair was reconstructed
                    # from, so the user can verify the imported bout.
                    "Game Info (reference)": p.game_info,
                }
                for p in suggestions.suggested_pairs
            ]
        )
        st.caption(
            "“Game Info (reference)” is the verbatim DK salary Game Info value "
            "each pair was built from — use it to verify the bout."
        )
        st.dataframe(preview_df, hide_index=True, width="stretch")
    else:
        st.caption(
            "No complete two-fighter suggestions on this slate — review the "
            "incomplete / anomaly / uncovered notes below, or use Advanced "
            "manual corrections."
        )

    # Incomplete / anomaly / uncovered are surfaced for transparency but never
    # grouped (design §6 #1 / #2 / #3); rounds are never inferred (§3.1).
    if suggestions.incomplete:
        st.markdown(
            "**Incomplete — only one active fighter for a Game Info value "
            "(opponent unimported or inactive):**"
        )
        for g in suggestions.incomplete:
            st.markdown(f"- {g.fighter_name} — {g.reason}")
    if suggestions.anomalies:
        st.markdown(
            "**Anomaly — more than two active fighters share a Game Info value "
            "(never auto-paired):**"
        )
        for g in suggestions.anomalies:
            st.markdown(f"- {', '.join(g.fighter_names)} — {g.reason}")
    if suggestions.uncovered:
        st.caption(
            f"{suggestions.uncovered_count} active fighter(s) have no Game Info "
            "and cannot be auto-suggested: "
            + ", ".join(u.name for u in suggestions.uncovered)
            + ". Pair them with Advanced manual corrections."
        )

    # Apply — the only Region E write path (design §4.3). The on_click handler
    # applies before the body re-renders, so new groups appear this same run
    # (no st.rerun). Already-grouped fighters are skipped unless the opt-in
    # below is ticked; an identical saved pair is always skipped, so a re-click
    # creates nothing. The button shows whenever there is at least one suggested
    # pair (even if all are currently grouped, so the opt-in can add a second).
    if suggestions.suggested_pairs:
        st.checkbox(
            "Include suggestions whose fighters are already grouped",
            key=_GI_INCLUDE_GROUPED_KEY,
            value=False,
            help=(
                "Off (default): a suggested pair naming a fighter who already "
                "has a group is skipped. On: a new group is created anyway and "
                "that fighter then shows as a duplicate. Existing groups are "
                "never edited or deleted either way."
            ),
        )
        st.button(
            "Apply Suggested DK Pairings",
            key="apply_game_info",
            on_click=_apply_game_info_callback,
        )

# ---------------------------------------------------------------------------
# Slate roster & coverage (read-only context). Supporting detail beneath the
# DK Game Info suggestions: which active fighters are / aren't grouped. All
# values were computed once above; this section only renders them. No INSERT /
# UPDATE / DELETE on page load (docs/DEVELOPMENT_NOTES.md §11).
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Slate roster & coverage")
st.caption(
    "Read-only. Active fighters from the latest salary import, joined to the "
    "fight groups by normalized name. Nothing is written on load."
)

cov_cols = st.columns(6)
cov_cols[0].metric("Total active fighters", total_active)
cov_cols[1].metric("Grouped fighters", grouped_count)
cov_cols[2].metric("Ungrouped fighters", ungrouped_count)
cov_cols[3].metric("Fight groups", len(roster_groups))
cov_cols[4].metric("Confirmed fight groups", confirmed_count)
cov_cols[5].metric("5-round fights", five_round_count)

if duplicate_names:
    st.warning(
        "Assigned to more than one fight group: "
        + ", ".join(sorted(set(duplicate_names)))
        + ". Resolve via the confirm/unconfirm controls in the fight card below."
    )
if unmatched_groups:
    st.warning(
        f"{len(unmatched_groups)} fight group(s) reference a name not on the "
        "active roster — see Unmatched pairings below."
    )

# A3 — non-blocking reminder to verify the main event. Fires only once at least
# one group exists (so a brand-new slate is not nagged) and no group is marked 5
# rounds. UFC cards almost always have a 5-round main event / title fight;
# forgetting to mark it suppresses that fighter's five_round_bonus (docs/DEVELOPMENT_NOTES.md
# §4). This is advisory only — it never writes or auto-changes a group.
if roster_groups and five_round_count == 0:
    st.info(
        "No 5-round fight is marked on this slate. UFC cards almost always "
        "have a 5-round main event (and every title fight is 5 rounds). "
        "Verify whether one of these groups is the main event / title bout "
        "and should be set to 5 rounds before Manual Review."
    )

if not active_fighters:
    st.caption(
        "No active fighters on this slate yet — use Build Step 1 to create a "
        "slate and import a DK salary CSV first."
    )
else:
    roster_df = pd.DataFrame(
        [{k: v for k, v in row.items() if not k.startswith("_")} for row in roster_rows]
    )
    st.dataframe(roster_df, hide_index=True, width="stretch")

if unmatched_groups:
    st.markdown(
        "**Unmatched pairings** — no matching active fighter on this slate:"
    )
    for grp in unmatched_groups:
        st.markdown(
            f"- #{grp.id}: {grp.fighter_1_name} vs {grp.fighter_2_name} "
            f"({_rounds_cell(grp.scheduled_rounds)}, {grp.status})"
        )

# ---------------------------------------------------------------------------
# Fight card — compact one-row-per-fight table (replaces the old long vertical
# list). One row per fight: fight #, both fighters with their Fighter Status
# category (when set), scheduled rounds, confirmed/unconfirmed, and the confirm
# toggle. The toggle is the only write path here and is unchanged: an explicit
# click calls FightGroupRepository.update_status then st.rerun (docs/DEVELOPMENT_NOTES.md §11).
# Fighter status is a display-only join and does not promote effective_status
# downstream (FIGHTER_STATUS_V1_DESIGN §15).
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Fight card")

if not roster_groups:
    st.caption("No fight groups saved for this slate yet.")
else:
    _total_groups = len(roster_groups)
    _unconfirmed_groups = _total_groups - confirmed_count
    fc1, fc2, fc3 = st.columns(3)
    fc1.metric("Total", _total_groups)
    fc2.metric("Confirmed", confirmed_count)
    fc3.metric("Unconfirmed", _unconfirmed_groups)

    # -----------------------------------------------------------------------
    # Quick actions — "Confirm all groups" + "Set 5-round main event". Both are
    # explicit one-click writes (on_click callbacks defined above) that run
    # before this body re-renders; neither runs on page load (docs/DEVELOPMENT_NOTES.md §11).
    # "Confirm all groups" shows only while an unconfirmed group exists; the
    # 5-round selector shows whenever a group exists and changes only the chosen
    # group's rounds. Stash the slate id so the callbacks act on the right slate.
    # -----------------------------------------------------------------------
    st.session_state[_CONFIRM_ALL_SLATE_KEY] = selected_id
    st.session_state[_FIVE_ROUND_SLATE_KEY] = selected_id

    # Render the result of the most recent quick-action click, once, scoped to
    # the current slate (stashed by the callbacks that ran before this body).
    _ca_result = st.session_state.pop(_CONFIRM_ALL_RESULT_KEY, None)
    if _ca_result is not None and _ca_result["slate_id"] == selected_id:
        if _ca_result["count"]:
            st.success(f"Confirmed {_ca_result['count']} fight group(s).")
        else:
            st.info("All fight groups were already confirmed — nothing to confirm.")
    _fr_result = st.session_state.pop(_FIVE_ROUND_RESULT_KEY, None)
    if _fr_result is not None and _fr_result["slate_id"] == selected_id:
        if _fr_result.get("error"):
            st.error(f"Could not set 5 rounds: {_fr_result['error']}")
        else:
            st.success(
                f"Set {_fr_result['label']} to 5 rounds "
                "(main event / title bout)."
            )

    # Group labels for the 5-round selector; already-5-round groups are flagged
    # so the user can see which bout is already the main event.
    _group_select_label = {
        g.id: f"{g.fighter_1_name} vs {g.fighter_2_name}"
        + (" — already 5 rd" if g.scheduled_rounds == 5 else "")
        for g in roster_groups
    }

    qa_confirm, qa_rounds = st.columns(2)
    with qa_confirm:
        if _unconfirmed_groups > 0:
            st.button(
                f"Confirm all groups ({_unconfirmed_groups})",
                key="confirm_all_groups",
                on_click=_confirm_all_callback,
                help=(
                    "Mark every unconfirmed fight group on this slate confirmed "
                    "in one step. Does not create groups or change scheduled "
                    "rounds."
                ),
            )
        else:
            st.caption("All fight groups are confirmed.")
    with qa_rounds:
        # Set-5-round selector — the user names the main event explicitly; rounds
        # are never inferred (§9.7 / §8 A3). The selectbox stores the chosen
        # group id under its key, read by the on_click handler.
        st.selectbox(
            "Set 5-round main event",
            options=[g.id for g in roster_groups],
            format_func=lambda gid: _group_select_label[gid],
            key=_FIVE_ROUND_SELECT_KEY,
            help=(
                "Pick the main event / title bout and set it to 5 rounds. Five "
                "rounds raises that fighter's projection. Other groups stay at "
                "their current rounds."
            ),
        )
        st.button(
            "Set selected fight to 5 rounds",
            key="set_five_round",
            on_click=_set_five_round_callback,
        )

    st.caption(
        "One row per fight. Fighter status shows the Fighter Status category "
        "(active / warning / blocking) when set, or “—” if the slot is not on "
        "the active roster. If a bout changes, edit the existing group rather "
        "than adding a duplicate."
    )

    def _fc_status(name: str) -> str:
        return _status_by_norm.get(normalize_name(name), "—")

    _fc_header = st.columns([1, 3, 2, 3, 2, 2, 2, 2])
    for _hc, _hlabel in zip(
        _fc_header,
        [
            "Fight",
            "Fighter 1",
            "F1 status",
            "Fighter 2",
            "F2 status",
            "Rounds",
            "Confirmed?",
            "",
        ],
    ):
        _hc.caption(_hlabel)

    for g in roster_groups:
        fc_row = st.columns([1, 3, 2, 3, 2, 2, 2, 2])
        fc_row[0].write(f"#{g.id}")
        fc_row[1].write(g.fighter_1_name)
        fc_row[2].write(_fc_status(g.fighter_1_name))
        fc_row[3].write(g.fighter_2_name)
        fc_row[4].write(_fc_status(g.fighter_2_name))
        if g.scheduled_rounds == 5:
            fc_row[5].markdown(f"**{_rounds_cell(g.scheduled_rounds)}**")
        else:
            fc_row[5].write(_rounds_cell(g.scheduled_rounds))
        fc_row[6].write("confirmed" if g.status == "confirmed" else "unconfirmed")
        _is_confirmed = g.status == "confirmed"
        _btn_label = "Mark unconfirmed" if _is_confirmed else "Mark confirmed"
        _new_status = "unconfirmed" if _is_confirmed else "confirmed"
        if fc_row[7].button(_btn_label, key=f"toggle_{g.id}"):
            try:
                fg_repo.update_status(g.id, _new_status)
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not update status: {exc}")

st.divider()
st.subheader("Advanced manual corrections")
st.caption(
    "Backup tools for bouts the DK Game Info suggestions above can't cover "
    "(for example a fighter with no Game Info). Prefer the suggestions above. "
    "If a bout changes, edit the existing group in the fight card above rather "
    "than adding a duplicate."
)

# Region B — slate-aware manual add form. Implements
# docs/FIGHT_GROUPS_UX_DESIGN.md §3 Region B / §8 A2: the two free-text fighter
# inputs are replaced with selectboxes scoped to the active slate roster,
# ungrouped fighters first so the common case (pairing someone who still needs
# an opponent) is one click away. Grouped fighters stay selectable but are
# labeled "(grouped)" — there is no unpair affordance yet, so keeping them
# selectable is how a mis-pair gets corrected. The write path is unchanged:
# FightGroupRepository.create, behind an explicit submit button, with no
# page-load writes. Now collapsed under "Advanced manual corrections" so it is
# no longer a primary surface; it expands only when the card is unfinished and
# there are no DK Game Info suggestions left to apply (_advanced_open).


def _is_grouped_name(name: str) -> bool:
    """True when an active fighter already resolves to a fight group (Region A)."""
    return normalize_name(name) in matches_by_norm


def _add_option_label(name: str) -> str:
    return f"{name} (grouped)" if _is_grouped_name(name) else name


# Ungrouped active fighters first, then grouped, each preserving the roster's
# name order (FighterRepository.list_for_slate sorts by name).
fighter_options = [f.name for f in active_fighters if not _is_grouped_name(f.name)]
fighter_options += [f.name for f in active_fighters if _is_grouped_name(f.name)]

with st.expander("Add a fight group manually", expanded=_advanced_open):
    if len(active_fighters) < 2:
        st.info(
            "Need at least two active fighters on this slate to add a fight group — "
            "use Build Step 1 to create a slate and import a DK salary CSV first."
        )
    else:
        st.caption(
            "Ungrouped fighters are listed first; fighters already in a group are "
            "marked “(grouped)” and stay selectable."
        )
        if all(_is_grouped_name(f.name) for f in active_fighters):
            st.info(
                "All active fighters are already grouped. Adding another group "
                "for a fighter creates a duplicate — only do this if a fighter "
                "genuinely has two bouts on the slate. If a bout changed, edit "
                "the existing group below instead of adding a duplicate."
            )
        with st.form("add_fight_group", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                fighter_1 = st.selectbox(
                    "Fighter 1",
                    options=fighter_options,
                    index=None,
                    placeholder="Select a fighter…",
                    format_func=_add_option_label,
                )
            with col2:
                fighter_2 = st.selectbox(
                    "Fighter 2",
                    options=fighter_options,
                    index=None,
                    placeholder="Select a fighter…",
                    format_func=_add_option_label,
                )
            st.caption(
                "Most UFC bouts are 3 rounds; the main event and any title fight "
                "are usually 5. Choose “5 rounds — main event / title bout” for "
                "the headliner — five rounds raises that fighter's projection. "
                "Verify rounds before Manual Review."
            )
            scheduled_rounds = st.radio(
                "Scheduled rounds",
                options=[3, 5],
                horizontal=True,
                index=0,
                format_func=lambda r: _ROUNDS_RADIO_LABELS[r],
            )
            submitted = st.form_submit_button("Save fight group")

        if submitted:
            if fighter_1 is None or fighter_2 is None:
                st.error("Select a fighter for both sides before saving.")
            elif normalize_name(fighter_1) == normalize_name(fighter_2):
                st.error("A fighter cannot be matched against themselves.")
            else:
                try:
                    rec = fg_repo.create(
                        slate_id=selected_id,
                        fighter_1_name=fighter_1,
                        fighter_2_name=fighter_2,
                        scheduled_rounds=int(scheduled_rounds),
                    )
                    st.success(
                        f"Saved fight group #{rec.id}: "
                        f"{rec.fighter_1_name} vs {rec.fighter_2_name} "
                        f"({_rounds_cell(rec.scheduled_rounds)})"
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not save fight group: {exc}")

# ---------------------------------------------------------------------------
# Region D — assisted pairing from a pasted card (A4.3 preview + A4.4 apply).
#
# Implements docs/FIGHT_GROUPS_UX_DESIGN.md §9: a collapsible section that
# parses pasted, newline-delimited bout text, matches each name against the
# active slate roster (src.slate.fight_card_parser, reused odds name-matching),
# renders a read-only preview (§9.5, A4.3), and — only on an explicit
# "Apply Valid Pairings" click — creates one 3-round, unconfirmed fight group
# per eligible pair via FightGroupRepository.create (§9.6 / §9.7, A4.4).
#
# Write-safety (docs/DEVELOPMENT_NOTES.md §11, design §9.6): paste and Parse / Preview write
# nothing; only Apply writes. The parser snapshot is stashed in session_state
# so Apply acts on the same parsed rows, and is treated as stale (never
# applied) if the slate or the text-area contents change. Apply only ever
# creates: already-grouped fighters are skipped by default (opt-in checkbox to
# add a second group anyway), an identical saved pair is skipped (idempotent
# re-click), and no existing group is ever updated or deleted.
# ---------------------------------------------------------------------------
st.divider()
with st.expander("Assisted pairing from pasted card", expanded=False):
    # Show the result of the most recent Apply (stashed by the on_click
    # handler that ran before this body) once, scoped to the current slate,
    # then clear it.
    _apply_result = st.session_state.pop(_APPLY_RESULT_KEY, None)
    if _apply_result is not None and _apply_result.slate_id == selected_id:
        _render_apply_result(_apply_result)

    st.caption(
        "Paste a newline-delimited fight card (one bout per line, e.g. "
        "“Fighter A vs Fighter B”). Click Parse / Preview to match the pasted "
        "names against this slate's active roster — nothing is written. "
        "Apply Valid Pairings then creates one 3-round, unconfirmed fight "
        "group per eligible pair; set any 5-round main event / title bout "
        "manually afterward."
    )
    pasted_card = st.text_area(
        "Pasted fight card",
        key="assisted_card_text",
        height=160,
        placeholder="Fighter A vs Fighter B\nFighter C vs Fighter D",
    )
    parse_preview = st.button("Parse / Preview", key="parse_preview_card")

    # Parse / Preview (read-only): recompute from the current text + active
    # roster and stash the snapshot (with the slate + text it was computed
    # against) so the later Apply rerun can act on the same parsed rows.
    if parse_preview:
        st.session_state[_PREVIEW_STATE_KEY] = {
            "slate_id": selected_id,
            "text": pasted_card,
            "bouts": parse_fight_card(pasted_card, [f.name for f in active_fighters]),
        }

    # Only act on a preview that still matches the current slate and text-area
    # contents — never apply a stale preview (design §9.6 write-path).
    _stored = st.session_state.get(_PREVIEW_STATE_KEY)
    _preview_fresh = (
        _stored is not None
        and _stored["slate_id"] == selected_id
        and _stored["text"] == pasted_card
    )
    if _stored is not None and not _preview_fresh:
        st.caption(
            "Slate or pasted text changed since the last preview — click "
            "Parse / Preview again before applying."
        )

    if _preview_fresh:
        bouts = _stored["bouts"]
        if not bouts:
            st.caption(
                "No fight-card lines to preview — paste some bouts above and "
                "click Parse / Preview."
            )
        else:
            summary = summarize(bouts)
            st.markdown(
                f"**{summary.total} parsed line(s)** · {summary.eligible} "
                f"eligible · {summary.blocked} blocked "
                f"({summary.unmatched} unmatched, {summary.ambiguous} "
                f"ambiguous, {summary.duplicates} duplicate, "
                f"{summary.self_pairs} self-pair, {summary.parse_errors} "
                "parse error)."
            )

            def _raw_cell(side) -> str:
                return side.raw if side is not None else ""

            def _matched_cell(side) -> str:
                if side is None:
                    return ""
                if side.matched_name:
                    return side.matched_name
                if side.band == BAND_AMBIGUOUS and side.best_candidate:
                    return f"{side.best_candidate}? (ambiguous)"
                if side.band == BAND_AMBIGUOUS:
                    return "(ambiguous)"
                return "—"  # unmatched

            preview_df = pd.DataFrame(
                [
                    {
                        "Pasted Line": b.raw_line,
                        "Fighter 1 (raw)": _raw_cell(b.side_1),
                        "Fighter 2 (raw)": _raw_cell(b.side_2),
                        "Matched Fighter 1": _matched_cell(b.side_1),
                        "Matched Fighter 2": _matched_cell(b.side_2),
                        "Status": b.pair_band,
                        "Eligible": "yes" if b.eligible else "no",
                        "Blocked Reason": b.blocked_reason or "",
                    }
                    for b in bouts
                ]
            )
            st.dataframe(preview_df, hide_index=True, width="stretch")

            # Apply step — the only write path (§9.6, A4.4). "Eligible" here is
            # the parser verdict (rules 1–4); the already-grouped gate (rule 5)
            # and the identical-pair idempotence backstop are applied at write
            # time, so a row shown as eligible may still be skipped on Apply.
            if summary.eligible == 0:
                st.caption(
                    "Nothing eligible to apply — fix the blocked rows above "
                    f"({PARSE_ERROR} / unmatched / ambiguous / self-pair / "
                    "duplicate are never auto-applied), or use the add form."
                )
            else:
                st.checkbox(
                    "Include rows whose fighters are already grouped",
                    key=_INCLUDE_GROUPED_KEY,
                    value=False,
                    help=(
                        "Off (default): a pasted pair naming a fighter who "
                        "already has a group is skipped. On: a new group is "
                        "created anyway and that fighter then shows as a "
                        "duplicate in the roster above. Existing groups are "
                        "never edited or deleted either way."
                    ),
                )
                # The only write path: the on_click handler applies before the
                # body re-renders, so the new groups appear in Region A / C this
                # same run (no st.rerun). Already-grouped fighters are skipped
                # unless the opt-in above is ticked; an identical saved pair is
                # always skipped, so a re-click creates nothing.
                st.button(
                    "Apply Valid Pairings",
                    key="apply_card",
                    on_click=_apply_card_callback,
                )

# The fight-card table above (subheader "Fight card") replaces the former
# "Existing fight groups for this slate" vertical list; the per-group confirm
# toggle lives there now.
