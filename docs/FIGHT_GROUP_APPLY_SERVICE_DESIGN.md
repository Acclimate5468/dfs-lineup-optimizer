# Fight Group Apply Service Design

Design-only artifact (`docs/DEVELOPMENT_NOTES.md` §9). No `src/` / `app/` / test edits land with
this doc; implementation follows in the slices named in §9, each its own commit
and review.

Companion docs:

- `docs/DK_GAME_INFO_PAIRING_DESIGN.md` — owns the pairing algorithm and the
  Fight Groups **Region E** apply contract (§3, §4) this service lifts.
- `docs/FIGHT_GROUPS_UX_DESIGN.md` — owns the **Region D** (pasted-card) apply
  rules (§9.6) and scheduled-rounds policy (§9.7) the shared core implements.
- `docs/TWO_STEP_BUILDER_PRODUCTION_DESIGN.md` — owns Build Step 1, where §5.3
  currently keeps Game Info pairing **suggest-only**. This design supersedes
  that "suggest-only" stance for Build Step 1, behind an explicit button.

---

## 1. Problem

Slice 1a shipped read-only DK Game Info suggested pairings + auto-detected main
event on Build Step 1 (`app/pages/00_build.py::_render_suggested_pairings`,
commit `7eaf887`). The natural next step — letting the user *apply* those
suggestions from Build without bouncing to the Fight Groups page — is blocked
because the apply write logic does not exist as a reusable unit.

Today the entire apply write lives in **page-private functions** in
`app/pages/02_fight_groups.py`:

- `_apply_name_pairs` — the shared create loop over canonical `(name_1, name_2)`
  pairs: conflict gating, idempotence backstop, 5-round main-event key, and the
  `FightGroupRepository.create` calls.
- `_apply_game_info_callback` — Region E wrapper: active roster →
  `group_fighters_by_game_info` → suggested pairs → `detect_main_event_pair` →
  conflict sets → core.
- `_apply_pairings` / `_apply_card_callback` — Region D (pasted card) wrapper
  over the same core.
- `_confirm_all_callback`, `_set_five_round_callback` — separate status / rounds
  writes (out of scope for this design; see §8).

The pure helpers `group_fighters_by_game_info` and `detect_main_event_pair`
(`src/slate/fight_grouping.py`) only *suggest* — they write nothing. So Build
cannot reach the apply path without either cross-importing page-private
functions from another `app/pages/` module (fragile, disallowed in spirit) or
re-implementing the write (divergence risk). Both are wrong. The write must
first move into a reusable, Streamlit-free `src/slate/` service.

This is therefore **not a thin wire**. It is: extract the service → repoint
Fight Groups with no behavior change → wire a new Build button to the same
service.

---

## 2. Behavior to preserve (non-negotiable invariants)

The extraction is a refactor. The following must hold byte-for-byte for the
Fight Groups page after the repoint, and the Build button must not violate any
of them either:

1. **DK Game Info pairings create `unconfirmed` fight groups.** Writes go
   through `FightGroupRepository.create` only — create-only, never update or
   delete (`FIGHT_GROUPS_UX_DESIGN.md` §9.6 rule 6).
2. **Existing groups are skipped / idempotent.** An identical saved pair (either
   order) is never re-created; a pair naming an already-grouped active fighter
   is skipped unless the caller opts in. Three independent guards back this:
   the `existing_pairs` frozenset gate, the `grouped_norms` gate, and
   `FightGroupRepository.create`'s reversed-order uniqueness check
   (`repositories.py:223`).
3. **Rounds default to 3** unless explicitly set elsewhere
   (`FIGHT_GROUPS_UX_DESIGN.md` §9.7).
4. **Main-event / 5-round auto-set is the existing Region E behavior only.**
   The Region E apply sets the auto-detected main event (latest Game Info start)
   to 5 rounds and *names it* in the result so the user can confirm or override.
   This design introduces **no new** rounds inference. Whether the Build button
   reuses this same Region-E behavior or defers all rounds to 3 is the one open
   decision in §5.3 — flagged for explicit sign-off, not decided unilaterally.
5. **Confirm-all stays separate.** Created groups are `unconfirmed`; confirming
   is a distinct, explicit action on the Fight Groups page. The service never
   confirms.
6. **No page-load writes.** Every apply is an explicit button `on_click`
   callback that runs before the body re-renders (`docs/DEVELOPMENT_NOTES.md` §11). Rendering a
   page or Build step must create nothing.
7. **Recompute-from-truth.** Region E recomputes suggestions from the *current*
   persisted roster at click time (not a stale preview snapshot), so a re-click
   under unchanged state is a no-op.

---

## 3. New service module

### 3.1 Location and boundary

`src/slate/fight_group_apply_service.py`.

- **No Streamlit import.** No `st.*`, no session-state, no rendering. Strings
  for the user are returned as data; the page formats them.
- **Repository-only persistence.** Reaches the DB exclusively through
  `FightGroupRepository` and `FighterRepository`. No raw SQL, no schema change,
  no migration.
- **Connection is passed in, never opened or closed by the service.** The page
  callback owns the connection lifecycle (`get_connection` / `bootstrap` /
  `close`), exactly as the current callbacks do — the service receives a live
  `sqlite3.Connection`.

### 3.2 Result dataclasses

```python
@dataclass(frozen=True)
class GroupApplyOutcome:
    """Result of the shared create loop over canonical pairs."""
    created: tuple[tuple[str, str], ...]      # (name_1, name_2) actually inserted
    skipped_grouped: tuple[str, ...]          # "A vs B — A already grouped"
    skipped_exists: tuple[str, ...]           # "A vs B" pair already saved
    errors: tuple[str, ...]                   # "A vs B: <exc>"
    five_round: str | None                    # main event named, or None

@dataclass(frozen=True)
class GameInfoApplyResult:
    """Region E / Build apply result (outcome + suggestion count)."""
    slate_id: int
    eligible: int                             # suggested pairs considered
    outcome: GroupApplyOutcome
```

Region D (pasted card) keeps its extra `blocked` count by composing
`GroupApplyOutcome` with its own page-side wrapper; it does not need a new
service dataclass (Region D is not a Build consumer). Field names mirror the
current dict keys so the page render functions
(`_render_game_info_apply_result`, `_render_apply_result`) change only from
`result["created"]` to `result.outcome.created`-style access — no copy change.

### 3.3 Public functions

```python
def create_groups_for_pairs(
    repo: FightGroupRepository,
    slate_id: int,
    pairs: list[tuple[str, str]],
    grouped_norms: set[str],
    existing_pairs: set[frozenset[str]],
    include_grouped: bool,
    five_round_pair_key: frozenset[str] | None = None,
) -> GroupApplyOutcome:
    """Verbatim move of _apply_name_pairs. Shared by Region D, Region E, Build."""

def compute_apply_context(
    repo: FightGroupRepository,
    active_roster: list,            # active FighterRecords
) -> tuple[set[str], set[str], set[frozenset[str]]]:
    """Return (roster_norms, grouped_norms, existing_pairs) for the slate.
    Lifts the duplicated conflict-set construction from both callbacks
    (02_fight_groups.py:313-329 and :439-454) into one place."""

def apply_game_info_pairings(
    conn: sqlite3.Connection,
    slate_id: int,
    *,
    include_grouped: bool = False,
    auto_set_main_event: bool = True,
) -> GameInfoApplyResult:
    """Region E + Build entry point. Mirrors _apply_game_info_callback minus
    session-state/Streamlit: active roster -> group_fighters_by_game_info ->
    suggested pairs -> (optional) detect_main_event_pair -> compute_apply_context
    -> create_groups_for_pairs. Recomputes from the persisted roster, so a
    re-call under unchanged state creates zero groups."""
```

`auto_set_main_event=True` reproduces today's Region E behavior exactly (the
default). Setting it `False` creates every group at 3 rounds and returns
`five_round=None`. This flag is the lever §5.3 uses to settle the Build rounds
question without forking the code path.

### 3.4 Transaction boundary

Today `FightGroupRepository.create` commits **per row** (`repositories.py:242`).
The shipped Region D/E apply is therefore N commits, not one transaction; the
existing code accepts this because callers pre-filter to valid pairs so no
`create` fails mid-batch ("all-or-nothing in practice", `_apply_name_pairs`
docstring).

The extraction **preserves this exact semantics** (a true single transaction
would be a behavior change, forbidden in the refactor slice). The win is that
the service becomes the *single owner* of the write, so a later, separately
designed slice can make the batch atomic (a non-committing `create` variant or
explicit `BEGIN`/`COMMIT` in the service) without touching any caller. Logged as
a residual risk in §10, not done here.

---

## 4. Slice B — extract service, repoint Fight Groups (no behavior change)

1. Add `src/slate/fight_group_apply_service.py` with the three functions and two
   dataclasses from §3.
2. `app/pages/02_fight_groups.py`:
   - Replace page-private `_apply_name_pairs` with an import of
     `create_groups_for_pairs`.
   - `_apply_game_info_callback` becomes a thin shell: read `slate_id` and
     `include_grouped` from session-state, call
     `apply_game_info_pairings(conn, slate_id, include_grouped=...)`, stash the
     result. The roster/suggestion/main-event/conflict logic moves into the
     service.
   - `_apply_card_callback` (Region D) keeps its pasted-card → pairs reduction
     and its `blocked` count, but calls `create_groups_for_pairs` +
     `compute_apply_context` instead of the local core.
   - Render functions read `result.outcome.*` / `result.eligible` instead of
     dict keys.
3. **No behavior change.** Same groups created, same skips, same 5-round
   main-event auto-set, same messages, same idempotence. The page's public
   behavior is identical; only the write's *home* moved.
4. Tests: see §7. The existing `tests/test_fight_groups_game_info_region.py`
   references the page-private `_apply_name_pairs` and must be updated to import
   the service (or re-pointed to drive the page wrapper). Call that out in the
   slice report (`docs/DEVELOPMENT_NOTES.md` §8).
5. **No Build write in this slice.**

---

## 5. Slice C — Build Step 1 "Apply Suggested DK Pairings" button

### 5.1 Where it renders

In `_render_suggested_pairings` (`app/pages/00_build.py`), directly under the
existing read-only suggested-pairings block. The button renders **only when
ready-to-apply suggestions exist** — i.e. `group_fighters_by_game_info` returns
≥1 suggested pair AND at least one such pair is not already saved as a group for
the slate. When every suggestion is already grouped, render an
"all suggestions applied" info line and **no button**. When there are no
suggestions, keep the current suggest-only empty state and **no button**.

### 5.2 Write contract

- A Build-owned `on_click` callback (`_apply_game_info_callback` in
  `00_build.py`, distinct from the Fight Groups one) that calls
  `apply_game_info_pairings(conn, slate_id, include_grouped=False, ...)` and
  stashes a result for the body to render once.
- **Button-only.** No write on page load / step render. The callback runs before
  the body, so the new groups and the refreshed fight-group count appear on the
  same run with no `st.rerun` (matches the Fight Groups pattern, `docs/DEVELOPMENT_NOTES.md`
  §11).
- On success: the Step 1 status / fight-group count updates (Build re-reads
  groups for the slate after the callback). A short success line names the
  created bouts; a skipped line names already-grouped pairs left untouched.
- **No duplicate groups** — the service's three idempotence guards (§2 item 2)
  apply unchanged.

### 5.3 Rounds behavior — DECIDED (Option A with a strict confidence guard)

The Build button mirrors Region E: it calls
`apply_game_info_pairings(conn, slate_id, include_grouped=False,
auto_set_main_event=True)`. Rounds are assigned by this rule:

- If `detect_main_event_pair` returns **one clear latest-starting bout**, the
  service creates that fight group with `scheduled_rounds=5`.
- **All other created groups default to `scheduled_rounds=3`.**
- If **no clear main event** is detected because Game Info start times are
  missing or ambiguous, every group is created at 3 rounds and the result
  surfaces a warning/instruction to set the 5-round bout manually on the Fight
  Groups page.
- This 5-round auto-set is allowed **only on an explicit Apply click.** No
  page-load writes (§2 item 6).
- The Fight Groups page remains the advanced / manual correction surface for
  overriding rounds after the fact.

The strict confidence guard is already enforced by `detect_main_event_pair`
(`src/slate/fight_grouping.py`): it returns `None` — no guess — when fewer than
two pairs carry a parseable start time or when two share the latest time. So
"one clear latest-starting bout" is the only condition under which a 5-round
group is created; everything ambiguous degrades safely to 3 rounds plus a
manual-set nudge.

Rationale: this is **already-approved behavior** (`DK_GAME_INFO_PAIRING_DESIGN`
§4.3 / §3.1), not new inference; the codebase's stated principle is "both
regions feed this single primitive, so the two apply paths cannot diverge"
(`_apply_name_pairs` docstring). Build becomes a third caller of the same
primitive with the same arguments, so Build and Fight Groups stay identical and
the same suggestion can never yield different rounds depending on which button
was pressed.

Option B (3 rounds only, `auto_set_main_event=False`) was considered and
**rejected**: it would split rounds behavior across the two apply surfaces for
no safety gain, since the confidence guard above already prevents a speculative
5-round assignment.

### 5.4 Keep Build minimal

The Build button uses `include_grouped=False` always. The rare "add a second
group for an already-grouped fighter" case is left to the Fight Groups page —
which remains the **advanced / manual correction surface** (selectboxes, set
5-round, confirm-all, include-grouped opt-in, Region D pasted card). Build Step
1 offers exactly one forward action: apply the clean DK Game Info suggestions.

---

## 6. Safety analysis

- **Salary roster changes after groups applied.** Suggestions are recomputed
  from the current persisted roster at click time; existing groups are never
  touched (create-only). A fighter scratched after grouping leaves its group row
  intact (existing Region E behavior, preserved) — the Fight Groups page is
  where the user reconciles. A re-import that flips a fighter inactive drops it
  from the active-roster join, so it no longer counts as "already grouped," but
  the stored group is untouched.
- **Idempotence.** Re-click / re-call under unchanged roster creates zero
  groups: `existing_pairs` skips saved pairs, and `create`'s reversed-order
  uniqueness check is the backstop. Verified by a dedicated test (§7).
- **Duplicate fighter / group avoidance.** Triple-guarded: `grouped_norms`
  (fighter already in a group), `existing_pairs` (exact pair already saved),
  and the repository's DB-level reversed-order uniqueness check. The in-batch
  conflict sets are mutated as each create lands, so two suggestions naming the
  same fighter within one apply cannot both insert.
- **Unconfirmed group review.** Every created group is `status='unconfirmed'`.
  Confirmation is a separate explicit action; Build never confirms. Downstream
  consumers that read groups (optimizer pool, projections) see the same
  unconfirmed groups they already see from a Fight Groups apply — Build creates
  nothing new in kind, only via a second button.
- **Why Build apply is safe for the one-page workflow.** It is button-only,
  recompute-from-truth, create-only, idempotent, and emits `unconfirmed` rows
  through the *same* reviewed primitive as the Fight Groups apply. It changes no
  schema, confirms nothing, and infers no new rounds (§5.3-A reuses approved
  behavior). The Fight Groups page stays the place to correct, override rounds,
  include-grouped, or delete.

---

## 7. Tests required

- **Service unit tests** — new `tests/test_fight_group_apply_service.py`:
  `apply_game_info_pairings` creates unconfirmed groups from Game Info; idempotent
  re-call creates zero; skips already-grouped (and `include_grouped=True` opt-in
  adds the second group); `auto_set_main_event=True` sets the detected main event
  to 5 rounds and `False` leaves all at 3; empty / single-fighter / anomaly
  rosters apply nothing; surfaced `errors`; `compute_apply_context` join matches
  the active roster. Port the `create_groups_for_pairs` core assertions over from
  `test_fight_groups_game_info_region.py`.
- **Fight Groups page regression** — `tests/test_fight_groups_game_info_region.py`
  and `tests/test_fight_groups_page.py` stay green after the repoint; update the
  import of `_apply_name_pairs` to the service. No assertion on created groups,
  skips, rounds, or messages may change (proves "no behavior change").
- **Build AppTest** — `tests/test_build_page.py`: button renders only with
  ready-to-apply suggestions; absent when none / all already grouped; click
  creates the expected `unconfirmed` groups and the Step 1 count updates;
  rendering the step **without** clicking creates nothing (no page-load write);
  re-click is idempotent.
- **Full suite** — `pytest` (no cherry-pick, `docs/DEVELOPMENT_NOTES.md` §8).
- **Compile** — `python -m py_compile` on every changed page/module.

---

## 8. Non-goals

- `_confirm_all_callback`, `_set_five_round_callback` stay on the Fight Groups
  page. Confirm-all and explicit rounds-override are separate, already-shipped
  writes; not part of this extraction.
- No new override type, no schema/migration, no `effective_status` change.
- No optimizer / projection / odds / alerts / export change.
- No Region D pasted-card UI on Build — Build gets the Game Info apply only.
- No `include_grouped` toggle on Build (deferred to the Fight Groups surface).
- No automatic / page-load group creation anywhere.

---

## 9. Implementation slices

1. **This design doc** — docs-only commit. Pause for review (`docs/DEVELOPMENT_NOTES.md` §9.1).
2. **Slice B — extract service + repoint Fight Groups.** Add
   `src/slate/fight_group_apply_service.py`; repoint Region E and Region D
   callbacks; update render-function field access; update/port tests. No Build
   write. No behavior change. Its own commit + review.
3. **Slice C — Build Step 1 Apply button.** Render the gated button, add the
   Build-owned callback calling the service, refresh the count, add the Build
   AppTest. Settle §5.3 per sign-off. Its own commit + review.
4. **Optional polish (later, only if asked).** e.g. surface the 5-round main
   event note on Build, or an `include_grouped` affordance — each its own slice.

---

## 10. Warnings / residual risks (for the report)

- **`CURRENT_STATUS.md` is stale.** It pins HEAD at `1b6c993`; live HEAD and
  `origin/master` are both `7eaf887` (two newer commits: `27b0d0c`, `7eaf887`).
  Not fixed in this design slice — it is unrelated to this doc and updating it
  here would mix concerns. Fix it in the next committed code slice's status
  update.
- **Transaction boundary stays per-row** in Slice B to preserve behavior; true
  atomic batching is a possible later hardening once the write lives in the
  service (§3.4).
- **§5.3 is decided** (Option A with a strict confidence guard): the Build
  button auto-sets exactly one clear latest-starting bout to 5 rounds on explicit
  Apply, defaults everything else to 3, and degrades to all-3-rounds-plus-manual
  nudge when the main event is ambiguous. No longer an open question.
- **Build apply makes optimizer/projection-visible (unconfirmed) groups** — the
  same kind the Fight Groups apply already creates, now reachable from a second
  button. Not a new downstream promotion, but noted so it is a conscious choice.
- **Region D shares the extracted core**; Slice B must keep the pasted-card apply
  working even though Build never uses it. The Region D regression tests are the
  guard.
