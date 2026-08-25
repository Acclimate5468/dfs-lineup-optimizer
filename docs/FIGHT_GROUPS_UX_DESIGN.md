# Fight Groups UX Design

Status: design only. No implementation in this slice. Companion to
`docs/DEVELOPMENT_NOTES.md` §2 (v0 scope: UFC DK Classic only), §3 (out-of-scope —
no scraping, no API integration, no DK login / auto-entry), §9
(design-before-implementation rules), §11 (UI write-action
rules), §13 (session / scope control — one slice per session, no
chaining), §14 (do-not quick reference), and the following sibling
design docs:

Project state at the time of writing: the salary importer, odds
ingestion + matching, Fighter Status v1, Manual Review gate,
projection v1, optimizer v1, and Export / Run Log v1 are all
implemented and an end-to-end lineup smoke is complete (latest
pushed commit `114f67b Add export run log page`). The current
active direction is UX streamlining of the pre-optimizer
workflow, starting with the Fight Groups page — this doc is the
first slice of that direction. No earlier checkpoint is paused
or pending against this work.

- `docs/SALARY_PERSISTENCE_DESIGN.md` §5 / §9 — the salary
  importer is the upstream source of the active fighter roster
  that Region A reads from.
- `docs/ODDS_PERSISTENCE_DESIGN.md` §14.12 — `FighterRepository.
  list_for_slate` is the existing Phase C.1 read path and is the
  only fighter read this design uses.
- `docs/ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7 — `effective_
  status` is still inert in downstream consumers. The Fight
  Groups page inherits that caveat: A1 surfaces `fighters.status`
  (importer-owned) and does not consume the manual
  Fighter-Status override layer.
- `docs/FIGHTER_STATUS_V1_DESIGN.md` §8 / §13.2 — manual
  Fighter-Status overrides are persisted but not yet composed
  into a single read-side projection; A1 deliberately stays on
  the importer status column to match the existing optimizer /
  exports posture.
- `docs/EXPORT_RUN_LOG_V1_DESIGN.md` — followed only as a
  style / structure reference for this doc (scope ▸ workflow ▸
  layout ▸ scope ▸ non-goals ▸ edges ▸ tests ▸ future).

---

## 1. Purpose

The Fight Groups page (`app/pages/02_fight_groups.py`) is the
manual gate between salary import and the optimizer. Today it
accepts two free-text fighter names and a rounds value, writes a
`fight_groups` row via `FightGroupRepository.create`, and lists
the persisted pairings underneath. Everything else the user
needs to answer — *which fighters from this slate still need a
pair? which pairs are confirmed? which rounds are set? did I
already pair Smith with somebody?* — lives one or two pages
away (Slate Setup, Fighter Status) and requires manual
cross-reference.

The goals of the Fight Groups UX redesign are:

- **Make the Fight Groups page understandable on its own.** The
  user should be able to answer "what is still incomplete on
  this slate?" without leaving the page.
- **Show the slate roster and current grouping state side by
  side.** Active fighters from the salary import, paired
  opponents from `fight_groups`, scheduled rounds, and
  confirmation state all render on one screen against one
  slate.
- **Reduce typo and pairing mistakes before the optimizer
  runs.** Free-text fighter inputs in the existing add form are
  the largest source of avoidable noise; surfacing the canonical
  roster makes mismatches visible at the point they happen,
  even before the eventual selectbox replacement (§3 Region B,
  §8 A2).

A1 — the slice this doc gates — is the **read-only roster +
coverage view**. No new writes, no new repositories, no schema
changes, no parser, no assisted-pairing automation. A1 is a
visibility change only.

---

## 2. Current UX problems

Behavior observed in `app/pages/02_fight_groups.py` today:

1. **Free-text fighter inputs.** The add form uses two
   `st.text_input` widgets (lines 58–60). The repository accepts
   any non-empty, non-equal strings; nothing requires the names
   to match an active fighter on the slate. Typos persist as
   real `fight_groups` rows and reach the optimizer.
2. **No roster / coverage table.** The page never reads
   `FighterRepository.list_for_slate`. The user cannot see, on
   this page, which fighters are active on the slate, what their
   salary is, or whether they have a pair yet.
3. **No grouped-vs-ungrouped summary.** The bottom metrics row
   ("Total / Confirmed / Unconfirmed", lines 89–95) counts
   `fight_groups` rows, not roster coverage. A slate with 12
   active fighters and 4 saved groups (8 fighters paired) reads
   as "Total 4 / Confirmed 0 / Unconfirmed 4" — accurate about
   groups, silent about the four fighters with no pair.
4. **Pairings must be inferred manually.** Each existing group
   renders as `Fighter A vs Fighter B`, but the user has to
   scan the list and cross-reference against memory or another
   page to answer "did I pair Smith yet, and with whom?".
5. **No "what is incomplete?" surface.** There is no count of
   ungrouped active fighters, no count of confirmed vs total
   pairings against the slate, and no warning for a fighter
   accidentally appearing in two groups (the `create` guard at
   `src/db/repositories.py:176-186` only catches the *exact*
   reversed-order duplicate of the same pair).
6. **Confirmation state isn't tied to roster readiness.** The
   per-group "Mark confirmed / Mark unconfirmed" toggle works,
   but the user has no single line that says "all pairs cover
   all active fighters and all are confirmed — safe to
   optimize."

A1 fixes problems 2, 3, 5, and 6 by adding the read-only
roster + coverage region. Problems 1 and 4 are partially
mitigated (the user can now *see* the canonical roster while
typing) but their full fix lives in §8 A2 (selectbox add form)
and A4 (assisted pairing builder).

---

## 3. Target page layout

The redesigned page is a single Streamlit column with three
regions stacked top-down. The slate selector at the top is
unchanged. Region order is fixed: roster first (what is the
state of the world?), then the add form (what am I changing?),
then the existing groups list (what have I already saved?).

### Region A — Slate roster + coverage view (A1 scope)

A read-only table of every **active** fighter on the selected
slate plus a summary metrics row above it.

Per-fighter columns:

- **Active fighter name** — `fighters.name` as persisted by
  the salary importer.
- **Salary** — `fighters.salary` rendered as `$X,XXX`.
- **Paired opponent** — the other fighter's name from the
  matching `fight_groups` row, or em-dash / "—" if ungrouped.
  Matching uses `normalize_name` from
  `src/utils/text_cleaning.py` so casing / whitespace / accent
  differences between the importer name and the free-text
  group name still join. The display string is the
  importer-owned roster name where the match resolves; the
  raw `fight_groups.fighter_*_name` value is shown only when
  no roster row matches (see §6 edge cases).
- **Fight group id / status** — `fight_groups.id` and
  `fight_groups.status` (`confirmed` / `unconfirmed`) of the
  pair, or blank for ungrouped fighters.
- **Scheduled rounds** — `fight_groups.scheduled_rounds`
  (3 or 5) of the pair, or blank for ungrouped fighters.
- **Coverage status** — one of:
  - `ungrouped` — fighter is active but not in any
    `fight_groups` row;
  - `grouped` — fighter resolves to exactly one
    `fight_groups` row;
  - `duplicate` — fighter resolves to more than one active
    `fight_groups` row (see §6 edge cases).

Summary metrics rendered above the table, in a `st.columns(5)`
row:

- **Total active fighters** — count of rows returned by
  `FighterRepository.list_for_slate` with `status == 'active'`.
- **Grouped fighters** — count of active fighters whose name
  normalize-matches at least one `fight_groups` slot for the
  slate.
- **Ungrouped fighters** — total active minus grouped.
- **Fight groups** — `len(FightGroupRepository.list_for_slate(slate_id))`.
- **Confirmed fight groups** — count of those whose
  `status == 'confirmed'`.

The metrics row is the only place a "ready to optimize?" signal
appears in A1. A1 does **not** add an explicit "ready" badge —
that surface is left to future work (§8 A5).

### Region B — Slate-aware picklist add form (implemented in A2)

The free-text add form was replaced in slice A2 with two
**slate-aware `st.selectbox` widgets** plus the existing rounds
radio. The options are the **full active slate roster**, not
just the unpaired subset:

- **Ungrouped fighters are prioritized** — they sort to the top
  of the list so the common "this fighter still needs an
  opponent" case is one click away.
- **Already-grouped fighters remain visible and selectable**,
  but carry a `"(grouped)"` label so the user can see at a
  glance who already has a pair.

Keeping grouped fighters selectable is intentional: there is no
separate unpair / remove-group workflow yet (see §8 A2), so
re-selecting an already-grouped fighter is the only way to
correct a mistaken pairing until that affordance lands.
Surfacing the canonical roster names this way removes the
largest source of free-text typos while still allowing those
correction workflows. Choosing the **same fighter on both
sides is rejected before any write**.

A2 preserves `FightGroupRepository.create` as the only write
path and keeps the **explicit submit button** — nothing is
written on page load. The roster visibility added by Region A
is what made A2 implementable; A1 shipped first so the user
benefited from the roster view even before the form rewrite.

### Region C — Existing groups list (no change in A1)

The current "Existing fight groups for this slate" section
(`app/pages/02_fight_groups.py` lines 82–110) — the per-group
row with `Mark confirmed` / `Mark unconfirmed` toggle and the
three-metric row above it — is preserved verbatim in A1. The
three-metric row is intentionally redundant with Region A's
"Fight groups" / "Confirmed fight groups" counts; A1 keeps both
so the existing AppTest assertions on the bottom metrics keep
passing. The bottom metrics row may be removed in a later
consolidation slice once Region A is the single source of
truth, but that consolidation is **not** A1.

---

## 4. A1 implementation scope

A1 lands the read-only roster + coverage view (§3 Region A)
and nothing else. The hard limits:

- **Add Region A only.** Regions B and C are untouched.
- **Read-only on page load.** Region A issues two reads
  (`FighterRepository.list_for_slate` and
  `FightGroupRepository.list_for_slate`) and computes the
  derived coverage in pure Python. It performs no INSERT,
  UPDATE, or DELETE. This satisfies `docs/DEVELOPMENT_NOTES.md` §11 "no
  page-load writes."
- **Use existing repositories only.**
  `FighterRepository.list_for_slate` and
  `FightGroupRepository.list_for_slate` already exist
  (`src/db/repositories.py:305` and `:224`). No new repository
  methods, no new arguments to existing methods.
- **No schema or migration changes.** A1 does not touch
  `src/db/schema.py` or `src/db/migrations.py`. The roster
  view is derived entirely from currently persisted columns.
- **No write-path changes.** The existing add form, the
  per-group confirm/unconfirm toggle, and the bottom
  three-metric summary all keep their current behavior and
  their current tests.
- **No new dependency.** Region A is implemented with
  `streamlit`, `pandas` (already a project dependency), and
  `src.utils.text_cleaning.normalize_name`.
- **Inherits the `effective_status` caveat.** A1 reads
  `fighters.status` (the importer-owned column) for the
  "active" filter, matching the existing posture documented in
  `docs/ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7. The manual
  Fighter-Status override layer is *not* consulted. Promoting
  Region A to consume an `effective_status` resolver belongs to
  the same future slice that does the same for projections,
  the optimizer, and exports — out of scope here.
- **Single slate per render.** Region A is scoped to the
  slate selected by the existing `st.selectbox` at the top of
  the page. No cross-slate aggregation.

Implementation guidance (non-prescriptive):

- Compute the roster ▸ group join in Python, not SQL. The
  existing repositories return small lists; a SQL join would
  require a new repository method and is out of A1 scope.
- Build a `pandas.DataFrame` from the joined rows and render
  via `st.dataframe`. Sort by ungrouped first, then by name,
  so the "what's incomplete?" rows surface at the top.
- The `normalize_name`-based join is a *display* join, not a
  persisted link. A1 does **not** write a fighter_id back into
  `fight_groups` — wiring the schema to a real foreign key is
  a separate future design pass.

---

## 5. Non-goals for A1

If a request would extend this slice into any of the items
below, stop and re-confirm scope (`docs/DEVELOPMENT_NOTES.md` §3 / §13):

- **No pasted fight-card parser.** A1 does not accept a pasted
  fight-card blob, does not infer pairings from arbitrary
  text, and does not call any external matching service.
  Assisted pairing is the A4 slice.
- **No assisted "apply pairing" button.** Region A is read-only.
  Even when the roster makes a pairing obvious, A1 does not
  add a one-click "save this pair" affordance. That is part
  of A4.
- **No add-form rewrite.** The free-text → selectbox migration
  is the A2 slice and is explicitly excluded from A1
  (§3 Region B).
- **No round-default logic.** A1 does not infer
  `scheduled_rounds` from event-card position or any external
  signal. The existing radio default (3 rounds) stays.
- **No page reordering.** A1 does not move the Fight Groups
  page in the Streamlit page list or rename it. Page slug,
  title, and order are unchanged.
- **No scraping / API integration.** Per `docs/DEVELOPMENT_NOTES.md` §3, A1
  does not call any external odds, fight-card, or DK endpoint
  to populate or validate the roster.
- **No new write paths.** A1 introduces no new repository
  method, no new override type, no new audit row. The
  `fight_groups` table is read-only from Region A.
- **No promotion of `effective_status`.** A1 does not start
  consuming the Fighter-Status manual override layer. See §4.
- **No fight-group → fighter foreign-key migration.** Linking
  `fight_groups` to the `fighters` table by id (rather than by
  free-text name) is a future schema pass with its own design
  doc; A1 stays on the existing free-text columns and the
  normalize-based join.

---

## 6. Edge cases

Region A must render coherently in all of the following
states. The behavior column describes the A1 expectation only;
each row pairs with a test in §7.

1. **No slate selected.** Reachable only on first load if no
   slates exist. The existing `if not slates: st.warning(...);
   st.stop()` path (lines 38–40) runs *before* Region A is
   reached; Region A is therefore not rendered. The existing
   warning is preserved verbatim.
2. **Slate exists, no fighters imported.**
   `FighterRepository.list_for_slate(slate_id)` returns `[]`.
   Region A renders the metrics row with zeros and a single
   caption such as "No active fighters on this slate yet —
   import a DK salary CSV from the Slate Setup page first."
   No empty `st.dataframe`. Region C continues to render
   whatever fight groups happen to exist (it should be `[]`,
   but A1 does not enforce that).
3. **All active fighters grouped.** Every active fighter
   resolves to exactly one `fight_groups` row. Coverage status
   is `grouped` for every row. "Ungrouped fighters" metric is
   0. No special banner — the metric line is the signal.
4. **Some active fighters ungrouped.** Mixed state. Ungrouped
   fighters sort to the top of the roster table; their
   "Paired opponent", "Fight group", and "Scheduled rounds"
   columns are blank; their coverage status is `ungrouped`.
   "Ungrouped fighters" metric is non-zero.
5. **Fighter appears in more than one active group.** Caused
   today by manual typos or by saving overlapping pairs (the
   `create` guard only blocks exact reversed-order duplicates
   of the *same* pair). Coverage status is `duplicate`. The
   row's "Fight group" and "Paired opponent" columns show the
   first match (lowest `fight_groups.id`) plus a "+N more"
   suffix. A `st.warning` above the table lists the affected
   fighter names so the user can clean up via the existing
   confirm/unconfirm toggle. A1 does **not** auto-resolve
   duplicates — surfacing them is the entire fix.
6. **Fight group references a name not in the active slate
   roster.** Either the importer marked the fighter inactive
   (status flipped to `'inactive'` per
   `SALARY_PERSISTENCE_DESIGN.md` §5) or the group was saved
   with a typo. The group still appears in Region C (no
   change). In Region A, it appears as a separate "Unmatched
   pairings" subsection below the roster table, listing the
   raw `fight_groups.fighter_1_name` / `fighter_2_name` and a
   note "no matching active fighter on this slate." The
   "Grouped fighters" metric counts roster-side matches only,
   so an unmatched pairing does not inflate the grouped
   count.
7. **Case / whitespace / accent variance between roster and
   group.** The `normalize_name` join treats `"Conor
   McGregor"`, `"conor mcgregor"`, and `"Conór McGregor"` as
   the same fighter. The displayed name is the roster row's
   `fighters.name` so the table is internally consistent;
   the raw group value is only surfaced in the unmatched
   subsection above.
8. **Confirmed vs unconfirmed mix on the same slate.** A group
   resolves the same way regardless of its `status`; the
   coverage status column reflects `grouped`, and the
   group-status column reflects `confirmed` / `unconfirmed`
   verbatim. "Confirmed fight groups" metric counts only
   `status == 'confirmed'`.

Edge cases not addressed in A1 (deferred to later slices):

- A `fight_groups` row whose two fighters are the same
  active-roster fighter under the normalize-folded join (e.g.
  a manual entry where both slots are typos of the same
  name). A1 surfaces this as a `duplicate` coverage status on
  that fighter and leaves resolution to the user.
- A slate where every active fighter is also marked
  `manual_status` "out" via Fighter-Status v1. A1 ignores the
  override layer (§4) so these fighters still appear as
  active. The cross-page reconciliation belongs to the same
  future slice that promotes `effective_status` everywhere.

---

## 7. Test plan

A1 lands with AppTest coverage in
`tests/test_fight_groups_page.py` (new file — there is no
existing AppTest for this page; the only fight-group tests
today are repository-level in
`tests/test_fight_group_repository.py`). Each scenario below
maps to one AppTest case unless noted. Per `docs/DEVELOPMENT_NOTES.md` §8
every behavioral change ships with a test, and per §11 every
rendered-derived-state surface needs a backing test that pins
the rendered text.

Page-load coverage:

1. **No slate exists.** AppTest runs the page against an
   empty DB; the existing `st.warning("No slates saved yet
   ...")` and `st.stop()` path fire and Region A is never
   reached. Assert the warning text and that no
   `st.dataframe` was rendered.
2. **Slate with no active fighters.** AppTest seeds one
   slate, zero fighters, zero groups. Assert the five summary
   metrics render with values `0 / 0 / 0 / 0 / 0` and that the
   "No active fighters on this slate yet …" caption appears.
   No `st.dataframe`.
3. **Slate with active fighters, no groups.** AppTest seeds N
   active fighters, 0 groups. Assert "Total active fighters"
   = N, "Grouped fighters" = 0, "Ungrouped fighters" = N,
   "Fight groups" = 0, "Confirmed fight groups" = 0. Assert
   every roster row's coverage status reads `ungrouped` and
   the paired-opponent column is blank.
4. **Paired fighters render opponent / status / rounds.**
   AppTest seeds 4 active fighters and 2 groups (one
   confirmed, one unconfirmed, one 3-round, one 5-round).
   Assert each fighter row shows the right opponent name (from
   the roster), the right group id, the right group status,
   and the right `scheduled_rounds` value.
5. **Ungrouped fighters are visible alongside grouped.**
   AppTest seeds 6 active fighters and 2 groups (covering 4
   of them). Assert the table sorts ungrouped rows first,
   that the two ungrouped fighters' opponent columns are
   blank with coverage status `ungrouped`, and that
   "Ungrouped fighters" metric equals 2.
6. **Summary metrics render and are correct.** Bundled into
   tests 3–5; each asserts the full 5-metric row, not just
   one value.
7. **Duplicate-assignment warning renders.** AppTest seeds 4
   active fighters and 3 groups where fighter A appears in two
   active groups (the third group is the corrupt one). Assert
   coverage status for fighter A is `duplicate`, the
   `st.warning` lists fighter A's name, and "Grouped fighters"
   metric still counts fighter A exactly once.
8. **Unmatched-pairing subsection renders.** AppTest seeds 2
   active fighters and 1 group whose
   `fight_groups.fighter_1_name` is a typo not matching any
   active fighter. Assert the roster table omits that name,
   the "Unmatched pairings" subsection lists the raw group
   value, and the "Grouped fighters" metric does not count
   the typo'd slot.
9. **Page-load writes are forbidden.** AppTest seeds a known
   DB state, runs the page, and asserts via the connection
   that no row count changed in `fight_groups`, `fighters`,
   `manual_match_overrides`, or `slates`. This pins the
   "Region A is read-only" invariant from §4.
10. **Existing group controls still visible.** AppTest seeds
    1 group and asserts the existing "Mark confirmed" / "Mark
    unconfirmed" button still renders with the same key
    pattern (`toggle_{group_id}`). Region C's existing
    `tests/test_fight_group_repository.py` coverage continues
    to pass unchanged.

The full suite (`pytest`) must be green before A1 is reported
complete; per `docs/DEVELOPMENT_NOTES.md` §8 a green suite is necessary but not
sufficient. A1 is a UI-only visibility slice with no new
ingestion code, so it does **not** require a real-file dry
run; salary import, odds ingest, projections, the optimizer,
and Export / Run Log are already exercised end-to-end against
real feeds upstream of this work and are not re-validated
here.

---

## 8. Future slices

A1 is the first of a sequence aimed at making the
pre-optimizer workflow self-explanatory. Each future slice
gets its own design pass before any code lands (`docs/DEVELOPMENT_NOTES.md`
§9).

- **A1.2 — Implement roster + coverage view.** The
  implementation slice for this design (`app/pages/02_fight_
  groups.py` edits, new AppTest file, no repository changes).
- **A2 — Replace free-text add form with slate-aware
  selectboxes (implemented).** Region B (§3). Two
  `st.selectbox` widgets scoped to the active slate roster —
  ungrouped fighters prioritized at the top, already-grouped
  fighters still selectable but labeled `"(grouped)"` — plus
  the unchanged rounds radio and the explicit submit button.
  The write path remains `FightGroupRepository.create` and
  nothing is written on page load. Same-fighter selection is
  rejected before create. Grouped fighters were kept
  selectable on purpose: there is no separate unpair /
  remove-group affordance yet, so re-selecting is the only
  correction workflow until that future slice lands. That
  remove/unpair affordance remains a candidate for a later
  slice with its own design pass.
- **A3 — Main-event / rounds UX (implemented).** The add
  form's rounds radio now carries descriptive labels
  ("3 rounds — standard bout" / "5 rounds — main event /
  title bout") plus helper copy reminding the user that most
  UFC bouts are 3 rounds, the main event / title fight is
  usually 5, and rounds must be verified before Manual
  Review. The roster + coverage view gains a "5-round fights"
  metric and spells out 5-round bouts in the Scheduled Rounds
  column; the existing-groups list bolds them. When a slate
  has fight groups but none are marked 5 rounds, a
  non-blocking reminder asks the user to confirm whether one
  is the main event / title bout. This slice is display +
  form copy only: it adds no schema, migration, repository,
  or projection change, does not auto-detect the main event
  from salary, and writes nothing (and auto-changes no
  existing group) on page load. A persisted per-slate "main
  event group id" pointer — a real link rather than a display
  hint — is deferred to a future slice that will need its own
  schema / migration design.
- **A4 — Pasted fight-card assisted pairing builder.** Paste a
  newline-delimited card, normalize names against the roster,
  surface a per-pair "apply" affordance. Pure assistant — no
  external API calls, no scraping (`docs/DEVELOPMENT_NOTES.md` §3). Designed in
  full in §9 below; implementation is split into the A4.2 /
  A4.3 / A4.4 slices defined in §9.10.
- **A5 — Home / workflow dashboard.** A landing page that
  aggregates the "ready to optimize?" signal across slate
  setup, fight groups, odds, fighter status, alerts, and
  manual review. The Fight Groups metrics defined in §3
  Region A become one tile on that dashboard.
- **B-series — Suggested DK pairings from salary Game Info.**
  Persist the DK salary CSV `Game Info` string on import and
  surface a one-click **Region E** "Apply Suggested DK
  Pairings" preview on this page, grouping active fighters by
  the exact shared `Game Info` value (no `@`-alias parsing, no
  fuzzy match). It reuses the §9 preview → explicit-Apply write
  contract verbatim and sits above the Region D pasted-card
  builder as the primary suggested method. Designed in full in
  `docs/DK_GAME_INFO_PAIRING_DESIGN.md`; implementation is split
  into the B1–B5 slices defined there.

Promoting `effective_status` into the Fight Groups view (and
into projections, optimizer, alerts, and exports) remains
gated on its own design pass per
`docs/ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7 and
`docs/DEVELOPMENT_NOTES.md` §10. No A-series slice promotes it implicitly.

---

## 9. A4 — Pasted fight-card assisted pairing builder

This is the design gate for the A4 slice referenced in §5,
§8, and the §3 Region B note. It is **design only** — no code
lands in this pass (`docs/DEVELOPMENT_NOTES.md` §9). A4 sits *between* Region B
(the selectbox add form) and Region C (the existing groups
list) as a new, collapsible **Region D** on
`app/pages/02_fight_groups.py`. It is an *assistant*: it turns
a block of pasted fight-card text into reviewed, one-click
group creations, but it never writes without an explicit user
action and never reaches outside the local DB.

A4 reuses the matching infrastructure that already ships for
odds → DK name matching, rather than inventing a second
matcher:

- `src/utils/text_cleaning.normalize_name` — conservative
  fold (NFKD accent strip, lowercase, whitespace collapse).
  Already the join key for Region A.
- `src/ingestion/name_matching.normalize_name_aggressive` —
  lossy exact-match fallback (drops quoted nicknames, splits
  separator punctuation, drops generational suffixes and
  middle initials, applies the curated nickname table,
  token-sorts).
- `src/ingestion/name_matching.best_match(query, candidates,
  threshold)` — `rapidfuzz` `WRatio` best candidate, returning
  `(candidate, score) | None`.
- The existing tier constants
  `AUTO_MATCH_THRESHOLD = 95` and
  `REVIEW_MATCH_THRESHOLD = 88` from the same module.

A4 introduces **no new matching primitive, no new threshold
constant, and no new dependency.** If the existing helpers
prove insufficient, that is a separate design conversation —
A4 does not fork them.

### 9.1 Purpose

- **Reduce manual pairing work.** Pasting a 6–13-bout card and
  clicking once is faster than 13 trips through the two-select
  add form.
- **Avoid typing mistakes.** The pasted names are matched
  against the canonical active roster; the user confirms a
  *matched roster name*, never re-types one. This is the same
  motivation as the Region B selectbox migration (§3), applied
  to the bulk-entry path.
- **Keep human confirmation.** A4 is explicitly *not* an
  auto-pairer. Every group it creates is the result of a user
  clicking an apply button after reviewing a preview. Ambiguous
  and unmatched rows are never applied.
- **No scraping or automatic source fetching.** A4 takes text
  the user already has on their clipboard. It never fetches a
  card from UFC.com, Tapology, BestFightOdds, an odds feed, or
  any network source (`docs/DEVELOPMENT_NOTES.md` §3 / §14). The text origin is
  entirely the user's responsibility.

### 9.2 User workflow

1. **Paste.** The user expands Region D ("Assisted pairing from
   pasted card") and pastes newline-delimited bout text into an
   `st.text_area`. Nothing happens on paste.
2. **Parse / Preview.** The user clicks **Parse / Preview**.
   A4 splits the text into lines, parses each line into two raw
   fighter names (§9.3), matches each raw name against the
   active slate roster (§9.4), and renders the preview table
   (§9.5). This step is **pure** — it reads the roster and
   computes match results, but writes nothing.
3. **Review.** The user reads the preview: which lines parsed,
   which names matched and at what confidence, which rows are
   eligible to apply, and why any row is blocked.
4. **Apply.** The user clicks **Apply Valid Pairings**. A4
   creates a `fight_groups` row for each *eligible* row only
   (§9.6), inside a single page-owned transaction, then
   re-renders Region A's roster + coverage view and Region C's
   group list so the new pairs appear immediately.
5. **Unresolved rows stay put.** Rows that did not reach
   eligible status (unmatched, ambiguous, duplicate, self-pair,
   already-grouped-without-opt-in) are **not** applied. They
   remain visible in the preview with their blocking reason so
   the user can fix the text and re-parse, or fall back to the
   Region B add form for those bouts.

The Parse/Preview and Apply steps are deliberately separated
into two buttons: preview is read-only and idempotent, apply is
the only write. This mirrors the odds Manual Review gate's
"review, then commit" shape.

### 9.3 Supported pasted formats

Each non-blank line is expected to encode exactly one bout as
`<fighter A> <separator> <fighter B>`. The parser is
**separator-driven** and case-insensitive. Recognized
separators, in priority order:

- `vs.` / `vs` — the canonical DK / media form.
- `v.` / `v` — abbreviated form.
- `versus` — spelled out.
- ` - ` — a hyphen **surrounded by whitespace** only.

Parsing rules and the reasoning behind them:

- **Word-boundary match for alphabetic separators.** `vs`,
  `v`, and `versus` are matched only as whole tokens
  (`\b(?:vs?|versus)\b`, case-insensitive), so a fighter named
  `"Vicente Luque"` or `"Vera"` is never split on its leading
  `v`, and `"Alexa Grasso"` is never split inside a word. The
  trailing period in `vs.` / `v.` is consumed if present.
- **Hyphen separator requires surrounding spaces.** ` - `
  (space-hyphen-space) splits, but a bare `-` does **not**.
  This protects hyphenated and particled surnames
  (`"Ji-Yeon Kim"`, `"Wood-Ortega"`) and matches how cards are
  typically pasted (`"Fighter A - Fighter B"`). If both a word
  separator and a spaced hyphen are present, the word separator
  wins (a hyphen inside a name is then left intact).
- **Extra whitespace and case are tolerated.** Leading/trailing
  whitespace on the line and around each name is stripped;
  internal runs of whitespace are collapsed before matching
  (consistent with `normalize_name`). Case is irrelevant — all
  comparison runs through the normalizers.
- **At most one split per line.** The parser splits on the
  first separator occurrence only. A line that yields more than
  two non-empty parts (e.g. two separators) is flagged as a
  **parse error** row (§9.5), not silently truncated.
- **Lines that do not parse.** A blank line is skipped
  entirely (not shown). A non-blank line with no recognized
  separator, or with an empty side after splitting, is shown as
  a **parse error** row with the raw line preserved and no
  matching attempted. Parse-error rows are never eligible to
  apply.

The parser does **not** strip records / weight-class / time
annotations that some cards include (e.g. a trailing
`"(Lightweight)"` or `"- 155 lbs"`). The first version treats
those as part of the raw name and lets the matcher's tolerance
absorb minor noise; if a real-card smoke test (§9.9) shows this
hurts match rates, a follow-up design pass adds annotation
stripping. A4 does **not** guess at it speculatively.

### 9.4 Matching rules

Each parsed raw name is matched independently against the
**active** slate roster (`FighterRepository.list_for_slate`,
`status == 'active'`), reusing the §9 helpers. The roster is
read once per Parse/Preview click and the candidate list is the
set of active `fighters.name` values.

Resolution order for a single raw name:

1. **Exact (conservative).** `normalize_name(raw)` equals
   `normalize_name(roster_name)` for exactly one roster
   fighter → **exact**.
2. **Exact (aggressive fallback).** If no conservative exact
   match, `normalize_name_aggressive(raw)` equals
   `normalize_name_aggressive(roster_name)` for exactly one
   roster fighter → **exact**. (This absorbs nickname-table,
   suffix, and middle-initial differences without dropping into
   fuzzy scoring.) If the aggressive fold collides on more than
   one roster fighter, the name is **ambiguous**, not exact.
3. **Fuzzy.** Otherwise call `best_match(raw, candidates,
   threshold=REVIEW_MATCH_THRESHOLD)`:
   - score `>= AUTO_MATCH_THRESHOLD` (95) **and** the
     second-best candidate is below `REVIEW_MATCH_THRESHOLD`
     (i.e. the top match is clearly separated) →
     **high-confidence fuzzy**.
   - score in `[REVIEW_MATCH_THRESHOLD, AUTO_MATCH_THRESHOLD)`
     (88–94), **or** score `>= 95` but a second candidate also
     scores `>= REVIEW_MATCH_THRESHOLD` (a near-tie) →
     **ambiguous**.
   - no candidate at or above `REVIEW_MATCH_THRESHOLD`, or an
     empty roster → **unmatched**.

Confidence bands, summarized:

| Band | Meaning | Auto-eligible? |
| --- | --- | --- |
| `exact` | normalized identity (conservative or aggressive), unique | yes |
| `high-confidence fuzzy` | `WRatio >= 95`, clearly separated from runner-up | yes |
| `ambiguous` | 88–94, or a high-score near-tie, or multi-candidate aggressive collision | **no** |
| `unmatched` | nothing `>= 88`, or empty roster | **no** |

Hard rule: **A4 never auto-applies an `ambiguous` or
`unmatched` name.** Only `exact` and `high-confidence fuzzy`
contribute to an eligible pair. The thresholds are reused from
`name_matching.py` verbatim; A4 does not recalibrate them.

The matcher is a **display / suggestion** computation only.
Like Region A's join (§4), it does **not** write a
`fighter_id` back into `fight_groups`. The fight-group →
fighter foreign-key migration remains a separate future design
(§5).

### 9.5 Preview table

After Parse/Preview, Region D renders one row per parsed line
(in pasted order) with these columns:

- **Pasted line** — the raw line, verbatim, for orientation.
- **Parsed fighter 1 (raw)** — the left side after splitting,
  whitespace-trimmed; blank for a parse-error row.
- **Parsed fighter 2 (raw)** — the right side; blank for a
  parse-error row.
- **Matched slate fighter 1** — the resolved canonical roster
  name, or "—" if unmatched / parse error. For ambiguous, the
  best candidate is shown with an "ambiguous" marker, not
  selected.
- **Matched slate fighter 2** — same, for the right side.
- **Confidence / status** — the worse of the two sides' bands
  (a pair is only as strong as its weaker name), or
  `parse error`. Surfaced per side too, so the user can see
  which name is the weak one.
- **Action eligibility** — `eligible` (will be created on
  Apply) or `blocked`.
- **Reason if blocked** — a short human-readable cause:
  `parse error`, `name 1 unmatched`, `name 2 ambiguous`,
  `same fighter on both sides`, `fighter appears in another
  pasted row`, `<fighter> already grouped`, etc. Exactly one
  primary reason is shown (the first that applies, in the
  precedence order of §9.6).

A short summary line sits above the table — e.g. *"7 lines · 5
eligible · 2 blocked (1 ambiguous, 1 already grouped)."* — so
the user knows what Apply will do before clicking. The
**Apply Valid Pairings** button is disabled (or renders a
"nothing to apply" caption) when the eligible count is zero.

The preview is rendered from in-memory parse/match state held
for the current interaction (e.g. `st.session_state`), not from
a persisted table. Re-clicking Parse/Preview recomputes from
the current text-area contents and replaces the preview.

### 9.6 Apply rules

Applying is gated behind the explicit **Apply Valid Pairings**
button. On click, A4 iterates the eligible rows and calls
`FightGroupRepository.create` for each, inside a single
page-owned DB transaction (`docs/DEVELOPMENT_NOTES.md` §11.1). The eligibility
gate, evaluated in this precedence order:

1. **Parse error → blocked.** A row that did not parse into two
   names is never applied.
2. **Both names resolved → required.** A pair is eligible only
   when *both* sides are `exact` or `high-confidence fuzzy`. If
   either side is `ambiguous` or `unmatched`, the row is
   blocked (`name N <band>`).
3. **No self-pairing.** If both resolved names are the *same*
   roster fighter (normalize-folded), the row is blocked
   (`same fighter on both sides`). This mirrors the Region B
   same-fighter rejection (§3) and the repository's own
   non-equal guard.
4. **No duplicate fighter across the pasted batch.** If a
   resolved roster fighter appears in **more than one** eligible
   pasted row, *all* rows containing that fighter are blocked
   (`fighter appears in another pasted row`). A4 does not guess
   which pairing the user meant; it surfaces the conflict and
   lets the user fix the text. This is the batch analogue of
   Region A's `duplicate` coverage status (§6 edge case 5).
5. **Already-grouped fighters are skipped by default.** If a
   resolved fighter already belongs to a `fight_groups` row on
   this slate (normalize-matched, the same join Region A uses),
   the row is blocked by default with reason
   `<fighter> already grouped`. A single **"Include rows whose
   fighters are already grouped"** checkbox lets the user
   opt in; even then, A4 only *creates a new group* — it never
   edits or deletes the existing one. Because there is no
   unpair / remove-group affordance yet (§3 Region B, §8 A2),
   the opt-in path can produce a fighter in two groups, which
   Region A will then surface as a `duplicate`. The opt-in is a
   conscious "I know what I'm doing" switch, defaulted off.
6. **Existing groups are preserved.** A4 only ever *creates*.
   It performs no UPDATE or DELETE on `fight_groups`. Any
   overwrite / replace-existing-pair behavior is explicitly out
   of scope and would need its own design pass with an undo /
   supersede story (`docs/DEVELOPMENT_NOTES.md` §11).

Write-path constraints:

- **Explicit button only — no page-load writes.** Neither
  paste nor Parse/Preview writes anything. Only the Apply click
  writes (`docs/DEVELOPMENT_NOTES.md` §11, §14).
- **Single transaction.** All eligible creates for one Apply
  click commit or roll back together, owned by the page
  handler. A partial failure does not leave half a card
  applied.
- **Repository-only writes.** A4 calls
  `FightGroupRepository.create` (default `scheduled_rounds=3`,
  default `status='unconfirmed'`) and nothing else. It does
  **not** bypass the repository (`docs/DEVELOPMENT_NOTES.md` §11).
- **Idempotent on re-click.** After a successful Apply, the
  just-created pairs make their fighters "already grouped", so
  a second Apply on the same preview blocks those rows by rule
  5 (opt-in off) and creates nothing. The repository's existing
  exact reversed-order duplicate guard
  (`src/db/repositories.py`) is the backstop. Re-clicking Apply
  must never double-create.

### 9.7 Scheduled rounds

- **The parser never infers rounds.** Fight-card text rarely
  encodes round count reliably, and inferring "this is the main
  event so it's 5 rounds" from paste order would be a guess.
  A4 makes no such guess (consistent with §5's "no round-default
  logic" and the A3 stance that 5-round status stays manual).
- **Default to the add-form default.** Every group A4 creates
  uses `scheduled_rounds=3` — the `FightGroupRepository.create`
  default and the Region B radio's default (`index=0`). The
  user sets 5-round bouts afterward via the existing per-group
  controls, exactly as A3 documents.
- **Main-event / title 5-round handling stays manual.** A4 does
  not detect or mark the main event. The A3 non-blocking
  "did you set a 5-round main event?" reminder (§8 A3) still
  applies to the groups A4 creates, nudging the user to fix
  rounds after a bulk apply.
- **Optional future enhancement (not A4).** A per-row rounds
  selector in the preview table — letting the user set 5 rounds
  for the headliner before applying — is a reasonable later
  addition. It is deliberately deferred so the first A4 cut
  stays small; it would be its own slice.

### 9.8 Non-goals

A4 stays inside the same scope fence as the rest of this doc
(`docs/DEVELOPMENT_NOTES.md` §3 / §14). It does **not**:

- **Scrape or fetch any source.** No UFC.com, Tapology,
  BestFightOdds, ESPN, Wikipedia, or odds-feed fetch. The
  pasted text is the only input.
- **Auto-detect the event.** No "which card is this?" lookup,
  no event-name parsing, no date inference.
- **Import odds.** A4 touches neither `odds_*` tables nor the
  matching/review queue. It is roster ↔ fight-group only.
- **Change the schema.** No new table, column, index, or
  migration (`src/db/schema.py`, `src/db/migrations.py`
  untouched). A4 is built from existing columns and existing
  repository methods.
- **Touch the optimizer or projections.** No change to
  `src/optimizer/` or `src/projections/`. A4 produces ordinary
  `fight_groups` rows the existing pipeline already consumes.
- **Add a new matcher or threshold.** It reuses
  `name_matching.py` verbatim (§9).
- **Promote `effective_status`.** A4 reads `fighters.status`
  for the active filter, same caveat as Region A (§4, §8).
- **Write a fighter foreign key.** Matching is display-only;
  no `fighter_id` is persisted into `fight_groups` (§9.4).
- **Edit or delete existing groups.** Create-only (§9.6).

### 9.9 Test plan

A4 lands with AppTest coverage extending
`tests/test_fight_groups_page.py` and, if a pure
parser/matcher helper is extracted (§9.10 A4.2), unit tests in
a new `tests/test_fight_card_parser.py`. Per `docs/DEVELOPMENT_NOTES.md` §8
every behavioral change ships with a test; per §11 every
rendered-derived-state surface and every write action needs a
backing test.

Parser / matcher unit coverage (pure, no Streamlit):

1. **Common separators parse.** `"A vs B"`, `"A vs. B"`,
   `"A v B"`, `"A v. B"`, `"A versus B"`, and `"A - B"` each
   parse to `("A", "B")`.
2. **Separator false-positives don't split names.**
   `"Vicente Luque vs Sean Brady"` splits only on the standalone
   `vs`; a hyphenated name like `"Ji-Yeon Kim - Tabatha Ricci"`
   splits only on the spaced hyphen and preserves `"Ji-Yeon
   Kim"`.
3. **Exact normalized matches.** A pasted name differing from
   the roster only by case / whitespace / accent resolves to
   the canonical roster name with band `exact`.
4. **Aggressive-fallback exact.** A nickname/suffix/middle-
   initial variant (covered by `normalize_name_aggressive`)
   resolves to `exact`, and a name that aggressive-folds onto
   two roster fighters resolves to `ambiguous`, not `exact`.
5. **High-confidence fuzzy.** A near-miss spelling that scores
   `>= 95` with a clear runner-up gap resolves to
   `high-confidence fuzzy` and is eligible.
6. **Ambiguous is shown and not applied.** A name scoring
   88–94, or with a high-score near-tie, resolves to
   `ambiguous`, is rendered with its best guess, and is
   `blocked`.
7. **Unmatched is shown and not applied.** A name with no
   candidate `>= 88` resolves to `unmatched` and is `blocked`.
8. **Parse-error rows.** A line with no separator, an empty
   side, or two separators is a `parse error` row, blocked, raw
   line preserved.

Page (AppTest) coverage:

9. **Parse/Preview is read-only.** Seed a known DB, paste text,
   click Parse/Preview, assert the preview renders and that no
   row count changed in `fight_groups` / `fighters` /
   `slates`.
10. **Eligible rows applied only after explicit button.** Seed
    a roster, paste a card whose lines all match exactly, assert
    *no* group exists after Parse/Preview, then click
    **Apply Valid Pairings** and assert exactly the eligible
    groups now exist with `scheduled_rounds == 3` and
    `status == 'unconfirmed'`.
11. **Self-pair conflict blocked.** A line resolving both sides
    to the same roster fighter is blocked
    (`same fighter on both sides`) and creates nothing on Apply.
12. **Duplicate fighter across rows blocked.** Two pasted rows
    naming the same roster fighter block *all* rows containing
    that fighter; Apply creates none of them.
13. **Already-grouped fighter skipped by default; opt-in
    creates a duplicate.** With the opt-in off, a row whose
    fighter already has a group is blocked. With the opt-in on,
    Apply creates the new group and Region A then reports that
    fighter as `duplicate`. Both branches asserted.
14. **Idempotent re-apply.** Click Apply twice on the same
    all-exact preview; assert the second click creates nothing
    (the now-grouped fighters are skipped) and no row is
    duplicated.
15. **Existing A1 / A2 / A3 behavior preserved.** The Region A
    metrics + coverage table, the Region B selectboxes and
    same-fighter rejection, and the A3 rounds copy / 5-round
    metric / main-event reminder all continue to pass their
    existing assertions with Region D present.

Per `docs/DEVELOPMENT_NOTES.md` §8 the full `pytest` suite must be green before
A4 is reported complete. A4 adds no ingestion-from-feed code
(the parser consumes user-pasted text, not a vendor file), so
it does not itself require a real-feed dry run; however, a
**real-card smoke** — pasting one genuine UFC card and
confirming match rates and the annotation tolerance of §9.3 —
should be documented before A4 is called complete, analogous to
the importer smoke discipline. Until that smoke is recorded,
A4 is "tested in isolation, not yet validated against a real
pasted card."

### 9.10 Implementation slices

A4 is split so each slice is independently shippable and
reviewable (`docs/DEVELOPMENT_NOTES.md` §13 / §15). The pure logic lands and is
tested before any UI, and the read-only preview lands before
the write action — the same "preview before commit" staging the
rest of this page follows.

- **A4.2 — Pure parser + matcher helper.** Extract a pure
  module (e.g. `src/slate/fight_card_parser.py`) that takes
  pasted text plus the active roster and returns structured
  per-line parse + match results (raw names, matched roster
  names, confidence band, eligibility, blocking reason). No
  Streamlit, no DB writes, no new threshold constants — it
  composes `normalize_name`, `normalize_name_aggressive`, and
  `best_match`. Ships with `tests/test_fight_card_parser.py`
  (§9.9 tests 1–8). This is the slice that must land first.
- **A4.3 — Region D preview UI.** Add the collapsible Region D
  to `app/pages/02_fight_groups.py`: the text area, the
  **Parse / Preview** button, the preview table (§9.5), and the
  summary line. Read-only — it calls A4.2 and renders, and
  writes nothing. Ships with AppTest coverage (§9.9 tests 9 and
  the rendering halves of 6–8, plus 15's "A1/A2/A3 preserved").
- **A4.4 — Apply action.** Add the **Apply Valid Pairings**
  button, the single-transaction create loop over eligible
  rows, the already-grouped opt-in checkbox, and the
  re-render. Ships with AppTest coverage for the write
  behavior (§9.9 tests 10–14). This is the only slice that
  introduces a write path, and it must satisfy `docs/DEVELOPMENT_NOTES.md` §11
  in full (single transaction, idempotent re-click,
  repository-only).

A smaller single-slice cut is **not** recommended: A4 crosses
the pure-logic ↔ UI ↔ write-path boundaries, and `docs/DEVELOPMENT_NOTES.md`
§11 / §13 specifically want the write action isolated behind
its own AppTest and review. Collapsing A4.3 and A4.4 into one
slice would bundle a read-only render and a new write path into
a single review, which the UI write-action rules discourage.
A4.2 could in principle be folded into A4.3, but keeping the
parser pure and separately unit-tested is cheap insurance for
the trickiest part of the feature (separator handling and the
confidence bands) and is kept independent.
