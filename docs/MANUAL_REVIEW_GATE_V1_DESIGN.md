# Manual Review Gate v1 Design

Status: design only. No implementation in this slice. Companion to
`docs/DEVELOPMENT_NOTES.md` §2 (v0 scope), §3 (out-of-scope list), §4 (projection
formula), §10 (current checkpoint), §11 (UI write-action rules), §14
(do-not quick reference), `docs/PROJECTION_V1_DESIGN.md` §5 / §7
(`non_projectable` semantics + future Manual Review hook),
`docs/MISMATCH_ALERTS_V1_DESIGN.md` §3 / §10 (alert categories + UI
contract), `docs/FIGHTER_STATUS_V1_DESIGN.md` §5 / §9 / §17 (status
categories + Manual Review interaction + Phase E smoke),
`docs/ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7 (still-inert
`effective_status`), `docs/SALARY_PERSISTENCE_DESIGN.md` §13 (salary
real-file smoke), and `docs/FIGHT_WEEK_DATA_COLLECTION_CHECKLIST.md`
§11 / §12 (off-app late-news template until this page lands).

---

## 1. Purpose

Manual Review Gate v1 is the first **local, manual, human-in-the-loop
checklist** that asks the user to declare a UFC DK Classic slate safe
to proceed toward any future lineup-building work. It composes the
existing read-only signals on the workbench — salary import results,
fight-group state, odds matching, Projection v1 output, Mismatch
Alerts v1 output, and (once integrated) Fighter Status v1 — into a
single per-slate readiness surface and a single user-owned write:
"Mark slate manually reviewed."

The intent, in one sentence: **a slate that has not been reviewed by a
human should not be allowed to feed a future optimizer or export
path**, and a slate that *has* been reviewed should carry a visible,
timestamped record of that decision.

Explicit non-claims:

- Manual Review Gate v1 is **not** a lineup generator. It produces no
  lineups, no roster suggestions, and no rankings.
- Manual Review Gate v1 is **not** an optimizer wrapper. The optimizer
  is a skeleton in v0 (`docs/DEVELOPMENT_NOTES.md` §3, §10); this gate does not invoke
  it, configure it, or feed it.
- Manual Review Gate v1 is **not** an exporter. It does not write a DK
  upload CSV, send anything to DraftKings, or surface contest entry.
- Manual Review Gate v1 is **not** an automated safety check. Every
  judgment in v1 is the user clicking a control in
  `app/pages/06_manual_review.py`. There is no background job, no
  scrape, no API.
- Manual Review Gate v1 is **not** a recomputation engine. It reads
  whatever salary import, fight groups, odds matching, Projection v1,
  and Mismatch Alerts v1 already persist or compute-on-read. It does
  not enqueue recomputes, mutate match overrides, or change projection
  values.
- Manual Review Gate v1 does **not** change the projection formula
  (`docs/DEVELOPMENT_NOTES.md` §4), the alert thresholds
  (`MISMATCH_ALERTS_V1_DESIGN.md` §3), or the Fighter Status taxonomy
  (`FIGHTER_STATUS_V1_DESIGN.md` §4).
- Manual Review Gate v1 does **not** read or write
  `odds_match_results.effective_status` or
  `manual_match_overrides`. Per `ODDS_PERSISTENCE_DESIGN.md` §15.11
  risk #7 and §14 below, `effective_status` remains inert downstream
  through this design pass.
- Manual Review Gate v1 does **not** unify Fighter Status with
  `effective_status`. That is a separate future "eligibility
  resolver" pass (see `FIGHTER_STATUS_V1_DESIGN.md` §17.2 and §13
  open question below).

## 2. v0 winning-focused rationale

Per `docs/LEGACY_DFS_PROMPT_AUDIT.md` §2 ("features … must either help
build stronger lineups or help prevent bad lineups"), Manual Review
Gate v1 sits squarely on the *prevent bad lineups* side of the bar:

- **No silent skipped check.** Every read-only signal the workbench
  already produces — `non_projectable` projection rows, `warn`-severity
  mismatch alerts, unmatched / rejected odds, missing opponents — is
  collected on one surface, so a user does not need to remember to
  visit five pages before treating a slate as ready.
- **No "build" without an explicit human OK.** The future optimizer
  and the future export / run log will, by contract (§11, §12), refuse
  to act on a slate whose `manual_review_status` is anything other
  than `reviewed`. The cheapest defence against a dead lineup is a
  required human checkpoint.
- **Reviewer trust.** A timestamped "reviewed by user at <when>" badge
  per slate creates an auditable record of when the user blessed the
  slate. If results diverge after the fact, the user knows what they
  acknowledged and when.
- **Late-news containment.** Slates change between salary download
  and contest lock. v1 surfaces a manual late-news / weigh-in
  checklist item the user must acknowledge so a known-stale slate
  cannot accidentally be marked reviewed.

Manual Review Gate v1 does **not** claim to *improve* lineups (no
edge modelling, no leverage). It claims only to make it harder to
ship one before the human has actually looked.

## 3. What the gate protects against

These are the failure modes Manual Review Gate v1 is designed to
prevent. Each maps to a §5 readiness check.

1. **Empty / unimported slate.** A user opens the optimizer / export
   on a slate whose salary CSV was never imported, producing nothing
   or worse (a stale prior slate's fighters). § 5.1 catches this.
2. **Unpaired fighters.** A fighter has no fight group on the slate,
   or has a fight group with no opponent. § 5.2 catches this; it
   mirrors Projection v1's `non_projectable` tag `"fight_group"` /
   `"opponent"` and Mismatch Alerts v1's `fight_group_issue` /
   `projection_non_projectable` codes.
3. **Wrong scheduled-rounds count.** A 5-round main event is marked
   3 rounds (or vice versa), silently distorting `five_round_bonus`
   in the projection (`docs/DEVELOPMENT_NOTES.md` §4). § 5.3 surfaces this explicitly
   for human eyeball; v1 does not auto-correct.
4. **Stale or unresolved odds matching.** Odds rows exist but match
   results are still `review_required` or have been `review_rejected`
   without a follow-up resolution. § 5.4 catches this.
5. **Hidden missing inputs.** A fighter projects as `missing_inputs`
   or `non_projectable` and is silently excluded by downstream
   consumers. § 5.5 catches this by demanding the user see the
   missing-input list before reviewing.
6. **Unacknowledged warn-severity mismatch alerts.** § 5.6 catches
   any `warn`-severity alert from the Mismatch Alerts v1 feed.
7. **Out / withdrawn / replaced fighters silently in the pool.**
   § 5.7 catches this once Fighter Status v1 UI lands (gated,
   not in v1's first implementation slice).
8. **Late news between download and contest lock.** § 5.8 catches
   this with an explicit "late-news checklist completed"
   acknowledgement the user toggles.
9. **No explicit human OK.** § 6 enforces this with the single
   write action that flips `manual_review_status` to `reviewed`.

What the gate does **not** protect against (out of scope, see §17):

- Wrong projection numbers (no model verification).
- Wrong odds (no book scraping, no third-party cross-check).
- Wrong fighter availability beyond what Fighter Status surfaces.
- Late-news the user themselves missed (the gate trusts the user;
  it does not consult any external source).
- Mid-contest swaps that occur after the user clicks Mark Reviewed.

## 4. Readiness check categories

Every readiness check in §5 belongs to exactly one of three
**review categories**. The categories are how the page (§7) and the
review service (§8) decide whether Mark Slate Manually Reviewed is
available and how each row renders.

| Category          | Meaning                                                                                  | Gate behavior                                                                                                                  |
|-------------------|------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| **Blocking**      | Structural problem on the slate. The slate is not safe to mark reviewed.                  | Mark Slate Manually Reviewed is **disabled**. The page renders the failed check in a Blocking section at the top.               |
| **Warning**       | Non-structural condition the user should look at — missing odds, warn-severity alerts.   | Mark Slate Manually Reviewed is **enabled**, but the user is visibly nudged toward the warning list before clicking.            |
| **Informational** | Value / leverage signal or coverage stat. Never affects the gate.                         | Surfaced in a dedicated Informational section. No acknowledgement required.                                                     |

Hard rules:

- A check falls into **exactly one** category. There is no "warn *and*
  block." If a future need ("blocking unless explicitly waived")
  emerges, it goes in a separate design pass — Manual Review Gate v2.
- The category set is **closed**: three categories, period.
- The Blocking → Warning → Informational rank order is the **single
  source of truth** for the page's section ordering. Any future
  consumer that reads the gate must import the category mapping from
  a shared module (§8), not duplicate the lists.
- v1 has **no per-check acknowledgement state**. The user clicks one
  button — Mark Slate Manually Reviewed (§6) — when they are satisfied
  that the Warning list is acceptable. Per-check ack is open
  question §18.2.

## 5. Readiness checks (v1)

Each check below has a stable identifier, a category (§4), the inputs
it consults, the v1 evaluation rule, the message it renders, and any
explicit cross-reference to an existing design.

The set below is the **closed v1 set**. Adding a new check requires
a paired design + test update, the same posture
`docs/FIGHTER_STATUS_V1_DESIGN.md` §4 takes on the status vocabulary
and `docs/DEVELOPMENT_NOTES.md` §4 takes on the projection formula.

### 5.1 Salary import readiness — Blocking

- **Code**: `salary_imported`.
- **Inputs**: `slates.salary_csv_status`, `slates.salary_row_count`,
  `FighterRepository.list_for_slate(slate_id)`.
- **Rule**: passes when (a) `salary_csv_status == "validated"` and
  (b) `salary_row_count > 0` and (c) `list_for_slate(slate_id)`
  returns ≥ 1 fighter with `status='active'`. Fails otherwise.
- **Message on fail**: "Salary CSV has not been imported into this
  slate. Open Slate Setup, validate, and click Import."
- **Why Blocking**: every downstream consumer (Projection v1,
  Mismatch Alerts v1, future optimizer, future export) requires
  active fighter rows. Without them, nothing is meaningful.
- **Cross-reference**: `SALARY_PERSISTENCE_DESIGN.md` §5; the
  "validated" status set by Slate Setup's Import action.

### 5.2 Fight groups & opponents confirmation — Blocking + Warning

This is a **split check** because the two sub-conditions have
different severities. The page renders them as two rows.

#### 5.2.a Fight-group coverage — Blocking

- **Code**: `fight_group_coverage`.
- **Inputs**: `FighterRepository.list_for_slate(slate_id)`,
  `FightGroupRepository.list_for_slate(slate_id)`,
  the same name-key join used by
  `src/projections/projection_input_service.py::_fighter_key`.
- **Rule**: passes when every active fighter on the slate appears as
  `fighter_1_name` or `fighter_2_name` of at least one fight group on
  the same slate (case-insensitive, NFKD-folded match — see §16
  cross-cutting). Fails when any active fighter has no fight group.
- **Message on fail**: "<N> active fighter(s) have no fight group on
  this slate: <list of fighter names, capped at 10, "+ M more" when
  truncated>. Open Fight Groups to add pairings."
- **Why Blocking**: this is the slate-config root cause behind
  Projection v1's `non_projectable` `"fight_group"` tag and Mismatch
  Alerts v1's `fight_group_issue` `warn`. The user must fix it
  before the slate is buildable.
- **Cross-reference**: `PROJECTION_V1_DESIGN.md` §5,
  `MISMATCH_ALERTS_V1_DESIGN.md` §3.7 / §3.8.

#### 5.2.b One-sided / unconfirmed groups — Warning

- **Code**: `fight_group_review`.
- **Inputs**: `FightGroupRepository.list_for_slate(slate_id)`.
- **Rule**: passes when every fight group on the slate has both
  `fighter_1_name` and `fighter_2_name` non-empty after trim, **and**
  has `status == "confirmed"`. Surfaces (Warning) when any group is
  one-sided (empty opponent) or `status == "unconfirmed"`.
- **Message on warn**: "<N> fight group(s) are unconfirmed or
  one-sided. Confirm each on the Fight Groups page before
  building."
- **Why Warning, not Blocking**: an unconfirmed group does not
  prevent projection / alert computation (the existing repos already
  return rows). The user is being nudged to make an explicit decision
  rather than ship on the default.
- **Cross-reference**: `FightGroupRepository.update_status` already
  exposes confirmed / unconfirmed; v1 does not change those semantics.

### 5.3 Scheduled rounds review — Warning

- **Code**: `scheduled_rounds_reviewed`.
- **Inputs**: `FightGroupRepository.list_for_slate(slate_id)` —
  `scheduled_rounds` per group.
- **Rule**: v1 does **not** know what the "right" round count is
  (it has no external schedule source — `docs/DEVELOPMENT_NOTES.md` §3 / §14 forbid
  it). The check therefore surfaces a Warning **if** the slate has
  at least one 5-round group **or** at least one 3-round group whose
  group `status` is `unconfirmed` — i.e. any non-default rounds
  decision the user has not explicitly confirmed. The message is the
  same in both cases.
- **Message on warn**: "Confirm scheduled rounds for every fight on
  this slate. 5 rounds applies to main events and championship
  bouts; 3 rounds applies to every other fight. Mark fight groups
  confirmed on the Fight Groups page once verified."
- **Why Warning**: scheduled rounds drives `+7 five_round_bonus`
  in the projection formula (`docs/DEVELOPMENT_NOTES.md` §4); a silent misclass
  ships a stale projection but does not make the slate
  un-projectable. v1 keeps this advisory and trusts the user to
  cross-check against UFC / DK (see
  `FIGHT_WEEK_DATA_COLLECTION_CHECKLIST.md` §10).
- **Open question** §18.4: should v1 elevate this to Blocking when
  any 5-round group is `unconfirmed`? Deferred to a future slice.

### 5.4 Odds matching status — Blocking + Warning + Informational

This is a **multi-row check** because match-result status splits
across all three categories. The page renders three rows under one
heading.

#### 5.4.a Active fighters without an `auto_match` odds row — Blocking

- **Code**: `odds_unmatched_active`.
- **Inputs**: `FighterRepository.list_for_slate(slate_id)`,
  `OddsMatchResultRepository.list_for_slate(slate_id)`.
- **Rule**: fails when ≥ 50% of active fighters on the slate have
  no `match_status='auto_match'` row pointing at their `fighter_id`.
  The 50% threshold is v1-default; pinned in tests.
- **Message on fail**: "<N> of <M> active fighters have no auto-
  matched odds row. Upload odds (CSV or manual) on the Odds page,
  then Recompute. A slate without majority odds coverage cannot be
  reviewed."
- **Why Blocking at this threshold**: Projection v1 emits
  `missing_inputs` `"win_probability"` for every uncovered fighter
  (`PROJECTION_V1_DESIGN.md` §5). Below 50% coverage the slate is
  not realistically buildable. The threshold is **conservative on
  purpose** so a partially-loaded odds set can still be reviewed
  with the Warning category surfacing the remainder.
- **Open question** §18.5: is 50% the right v1 threshold? Phase D
  real-feed smoke is the first opportunity to recalibrate.

#### 5.4.b Some fighters missing odds (below the §5.4.a threshold) — Warning

- **Code**: `odds_coverage_partial`.
- **Inputs**: same as §5.4.a.
- **Rule**: warns when at least one active fighter has no
  `auto_match` row, but the §5.4.a Blocking threshold is not met.
- **Message on warn**: "<N> active fighter(s) have no auto-matched
  odds row. Their Projection v1 row will read missing_inputs
  ('win_probability') and will not contribute to the optimizer or
  export. Verify the gap is intentional."

#### 5.4.c `review_required` / `review_rejected` review — Warning

- **Code**: `odds_match_review`.
- **Inputs**: `OddsMatchResultRepository.list_for_slate(slate_id)`,
  `ManualMatchOverrideRepository.list_active_for_slate(slate_id)`.
- **Rule**: warns when at least one persisted match result has
  `match_status='review_required'` **or** `effective_status` is
  currently `review_rejected` (an active reject override). Surfaces
  the count of each. v1 does **not** read `effective_status` for
  downstream consumption (§14); the count here is for human eyeball
  only and is sourced from the repository the Odds page already
  uses.
- **Message on warn**: "<N> match result(s) still need review on the
  Odds page (review_required: <X>, review_rejected: <Y>). Open the
  Odds page → 3b/3c to resolve before reviewing the slate."
- **Why Warning, not Blocking**: a `review_required` row does not
  by itself stop Projection v1, which only consumes `auto_match`
  rows. The risk is that the user *intended* that fighter to be
  projected from an odds row that didn't auto-match and never came
  back to fix it. The warning is the nudge.
- **Cross-reference**: `ODDS_PERSISTENCE_DESIGN.md` §5.3 / §15.9;
  `app/pages/03_odds.py` Zone 3.

#### 5.4.d Odds coverage stat — Informational

- **Code**: `odds_coverage_stat`.
- **Inputs**: counts from §5.4.a / §5.4.b inputs.
- **Rule**: always renders. Surfaces "Auto-matched: X of Y active
  fighters (Z%)."
- **Why Informational**: a transparency number; not a pass / fail.

### 5.5 Projection missing inputs / non-projectable — Blocking + Warning

#### 5.5.a Non-projectable fighters — Blocking

- **Code**: `projection_non_projectable`.
- **Inputs**: `project_slate(conn, slate_id)` from
  `src/projections/slate_projection_service.py`.
- **Rule**: fails when any returned `FighterSlateProjection` row has
  `projection_status == "non_projectable"`. The structural inputs the
  fighter is missing (`"fight_group"`, `"opponent"`, future
  `"fighter_status"`) are listed in the failure message.
- **Message on fail**: "<N> fighter(s) are non-projectable: <list
  capped at 10, "+ M more" when truncated, each entry "<name>
  (<tags>)">. Resolve the structural cause (fight group / opponent /
  fighter status) before reviewing the slate."
- **Why Blocking**: `non_projectable` is the contract Projection v1
  uses for a structural data gap, distinct from `missing_inputs`
  (`PROJECTION_V1_DESIGN.md` §5). v1 will not let the user wave it
  away.
- **Note on the future `"fighter_status"` tag**: this tag exists in
  the projection contract but is **not produced** by Projection v1
  today (`PROJECTION_V1_DESIGN.md` §10 open question #3 + Phase F).
  When Fighter Status integration lands (Phase F per
  `FIGHTER_STATUS_V1_DESIGN.md` §15), the same check picks it up
  without code changes here.

#### 5.5.b Missing-input fighters — Warning

- **Code**: `projection_missing_inputs`.
- **Inputs**: same as §5.5.a.
- **Rule**: warns when any `FighterSlateProjection` has
  `projection_status == "missing_inputs"`. The Warning category lets
  the user see the gap (no salary, no win probability, no scheduled
  rounds — see `PROJECTION_V1_DESIGN.md` §5 table) without blocking.
- **Message on warn**: "<N> fighter(s) have missing projection
  inputs: <list capped at 10>. The optimizer will exclude these
  rows; verify the gap is intentional before reviewing."

### 5.6 Mismatch alerts review — Warning + Informational

#### 5.6.a Warn-severity alerts — Warning

- **Code**: `mismatch_alerts_warn`.
- **Inputs**: `evaluate_alerts(conn, slate_id)` from
  `src/alerts/alert_service.py`.
- **Rule**: warns when at least one `Alert` has
  `severity == "warn"`. The count is rendered; the message includes
  the unique `code` values that contributed.
- **Message on warn**: "<N> warn-severity mismatch alert(s) on this
  slate (<comma-joined codes>). Open the Alerts page to review."
- **Why Warning, not Blocking**: by design (`MISMATCH_ALERTS_V1_DESIGN.md`
  §3) the v1 alert severity scale is **two-level** and `warn` is
  defined as "advisory" — it never blocks. The Manual Review Gate
  honors that scale: if a future v2 introduces a higher severity,
  it lands in the Alerts design first, then propagates here in a
  separate slice.

#### 5.6.b Info-severity alerts — Informational

- **Code**: `mismatch_alerts_info`.
- **Inputs**: same as §5.6.a.
- **Rule**: always renders. Surfaces the count of `info` alerts and
  the unique codes.
- **Why Informational**: `info` is a value / leverage signal
  (`MISMATCH_ALERTS_V1_DESIGN.md` §3.1 / §3.3 / §3.5). The user
  benefits from seeing it but it does not affect the gate.

#### 5.6.c `late_news_risk` non-emission — Informational (locked)

- **Code**: `late_news_risk_locked`.
- **Inputs**: `evaluate_alerts(conn, slate_id)`.
- **Rule**: pins the contract that the reserved
  `late_news_risk` code (`MISMATCH_ALERTS_V1_DESIGN.md` §3.9) is
  **never emitted in v1**. Surfaces "late-news alert is reserved —
  not active in v1; use the manual checklist (§5.8) instead."
- **Why Informational, why pinned at all**: this is the alerts-side
  cross-check for the manual late-news checklist (§5.8). The two
  share a future hook (`MISMATCH_ALERTS_V1_DESIGN.md` §15 risk #8 +
  `FIGHTER_STATUS_V1_DESIGN.md` §7). Pinning the contract here keeps
  an accidental promotion of `late_news_risk` from invisibly
  bypassing the manual checklist.

### 5.7 Fighter Status review — gated, deferred

- **Code**: `fighter_status_review`.
- **Inputs (future)**: a read aggregator equivalent to
  `FighterRepository.list_with_status(slate_id)` per
  `FIGHTER_STATUS_V1_DESIGN.md` §12 / Phase C, returning each
  fighter's `(importer_status, manual_status, effective_status,
  category)`.
- **Rule (future)**:
  - **Blocking row**: at least one fighter has `category == "blocking"`
    (resolved). Message: "<N> fighter(s) are blocking-category (out /
    withdrawn / replaced / duplicate_or_bad_row / inactive). Open
    Fighter Status before reviewing."
  - **Warning row**: at least one fighter has
    `category == "warning"` (needs_review / questionable /
    missed_weight / short_notice). Message: "<N> fighter(s) are
    warning-category. Verify on Fighter Status."
- **v1 behavior**: **the row renders but always evaluates to
  "Fighter Status integration not yet active. Manual Review v1
  does not consult Fighter Status; use the manual late-news
  checklist (§5.8) for now."** It is categorised **Informational** in
  v1 and ungated.
- **Activation**: only when (a) Fighter Status v1 Phases A–E are
  documented complete (`FIGHTER_STATUS_V1_DESIGN.md` §15 / §17)
  **and** (b) a separate Phase F design slice promotes Fighter
  Status into Manual Review (and into Projection v1 §10 open
  question #3, and into Mismatch Alerts v1 §3.9, in a coordinated
  pass). Until that slice lands, this check stays Informational.
- **Cross-reference**: `FIGHTER_STATUS_V1_DESIGN.md` §9 ("Effect on
  future Manual Review gate") is the contract this section
  implements; the categorisation table here is **derived** from
  `FIGHTER_STATUS_V1_DESIGN.md` §5, not duplicated.

### 5.8 Late-news / dead-lineup risk — Warning (manual toggle)

- **Code**: `late_news_acknowledged`.
- **Inputs**: a per-(slate, "late_news") session toggle plus, when
  persistence lands (§9), a per-slate "last acknowledged at"
  timestamp.
- **Rule**: in v1, warns until the user clicks the explicit
  acknowledgement toggle on the page (§7 step 5). When acknowledged,
  the row renders as passed.
- **Message on warn**: "Confirm you have completed the off-app
  late-news / weigh-in checklist
  (`docs/FIGHT_WEEK_DATA_COLLECTION_CHECKLIST.md` §11 / §12) for
  this slate. Manual Review will not auto-detect a pulled fighter."
- **Message on pass**: "Late-news / weigh-in checklist acknowledged
  by user at <when>."
- **Why Warning + toggle**: there is no automated late-news source
  in v0 (`docs/DEVELOPMENT_NOTES.md` §3 / §14). The acknowledgement is the user
  asserting "I have done the off-app §11 / §12 pass." This is the
  one place the gate accepts an explicit "I have done the offline
  thing" toggle, because the off-app checklist is itself documented
  and the alternative is an unguarded gap.
- **Behaviour on slate change**: any salary / odds / fight-group /
  match-override write should re-arm this check to "not
  acknowledged" so the user does not carry a stale ack across a
  reissue. v1 implements this by resetting the toggle whenever the
  manual review write itself runs (see §6 idempotence rules) and by
  surfacing a visible "last acked at" timestamp the user can
  compare against the salary import time. A schema-level "reset on
  upstream write" hook is **out of scope** for v1 (§18.3 open
  question).

### 5.9 Explicit Mark Manually Reviewed — Blocking until user clicks

- **Code**: `manual_review_user_ack`.
- **Inputs**: `slates.manual_review_status` (v1 schema, §9.2).
- **Rule**: passes when `manual_review_status == "reviewed"`.
  Otherwise blocks.
- **Message on fail**: "Slate has not yet been marked manually
  reviewed. Click Mark Slate Manually Reviewed when the Blocking and
  Warning lists above are acceptable."
- **Why Blocking**: this is the gate's only required user write
  (§6). Without it, the future optimizer / export refuse to act
  (§11, §12).

## 6. Explicit user action — "Mark Slate Manually Reviewed"

The page exposes exactly one write action: a single button that
transitions `slates.manual_review_status` from `"not_reviewed"` to
`"reviewed"` (and records `manual_review_completed_at`).

Hard rules (mirror `docs/DEVELOPMENT_NOTES.md` §11 write-action contract):

- **Enablement**. The button is enabled only when every Blocking
  check in §5 passes. The Warning list does **not** block enablement
  — the user is trusted to read it and decide. Per-warning ack is
  open question §18.2.
- **One transaction**. The button click runs inside a single DB
  transaction owned by the page handler.
- **AppTest coverage**. Per `docs/DEVELOPMENT_NOTES.md` §11.1.2, the button is
  covered by an AppTest that exercises the click and asserts the
  persisted `manual_review_status`.
- **Idempotence**. Clicking the button when the slate is already
  `reviewed` is a no-op on the status value column. The
  `manual_review_completed_at` timestamp **does** refresh (mirroring
  the Fighter Status idempotence-timestamp policy in
  `FIGHTER_STATUS_V1_DESIGN.md` §19.4) so a "last reviewed at"
  surface stays useful.
- **No Undo, but supersede**. v1 does **not** add an Undo / Unmark
  Reviewed button. The supersede story is: when the underlying state
  changes (salary re-import, odds save, recompute, override write,
  fight-group edit, Fighter Status change once integrated), the user
  must explicitly re-review. v1 does not implement automatic
  invalidation across writes — see §18.3.
- **No bypass**. The button is the **only** path that flips
  `manual_review_status`. Per `docs/DEVELOPMENT_NOTES.md` §11, the page must not
  execute SQL directly; it goes through the repository layer (§8).
- **No write side-effects beyond the slate row**. No projection
  recompute, no odds recompute, no override mutation, no fighter
  table edit. Per §14, `effective_status` and
  `manual_match_overrides` are not touched.

The button label is fixed: `Mark Slate Manually Reviewed`. The
disabled-state caption is fixed: `Resolve the Blocking list before
marking this slate reviewed.` Both strings are pinned by AppTest
(§15) so a future contributor cannot soften them without breaking
the test.

## 7. UI concept for `app/pages/06_manual_review.py`

The page replaces the current v0 placeholder
(`app/pages/06_manual_review.py` renders only a title and a "Locked
in v0" warning). It is the **only** UI surface that writes
`manual_review_status` in v1. Per `docs/DEVELOPMENT_NOTES.md` §11, every write
action runs inside a single DB transaction owned by the page handler
and is covered by an AppTest.

Page elements (future implementation):

1. **Slate selector** — `st.selectbox` over
   `SlateRepository.list_all`, mirroring `app/pages/09_projections.py`
   and `app/pages/05_alerts.py`. Same `format_func` shape
   (`#<id> — <event_name> (<event_date>)`).
2. **Top-of-page warning banner** — pinned text covering:
   - "Manual review is local. It does NOT call any external service
     and does NOT auto-detect fighter availability."
   - "Marking a slate reviewed does NOT invalidate when underlying
     data changes — re-review after any salary re-import, odds
     save, recompute, override, or fight-group edit."
   - "Manual review is the gate that will block the future optimizer
     and the future export / run log from running on an
     un-reviewed slate. Neither is implemented in v0."
   - "`effective_status` is not consulted; Fighter Status is not
     yet integrated. See `docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`
     §14 / §13."
3. **Slate header line** — pinned text rendering the current
   `manual_review_status`, e.g. `Status: not_reviewed` or
   `Status: reviewed (at <YYYY-MM-DD HH:MM:SS> UTC)`. Pinned by
   AppTest.
4. **Blocking checks section** — heading `Blocking`. One row per
   Blocking-category check from §5 in the §4 stable order. Each row
   shows the check code, a check name, a pass / fail badge, and the
   §5 message (truncated to a single line; expandable detail
   optional in v2 only — not v1).
5. **Warning checks section** — heading `Warning`. Same shape as
   Blocking. Empty section renders "No warnings."
6. **Informational checks section** — heading `Informational`. Same
   shape, no pass / fail badge — just the message.
7. **Late-news acknowledgement toggle** — inside the Warning
   section, the §5.8 row carries a `st.checkbox` (or
   `st.toggle`). Its state for v1 is **session-only**; the
   persisted-acknowledgement field is gated on §9.2's
   `late_news_acknowledged_at` column being added in Phase B
   (§10). Until then, the toggle resets on every page render
   (open question §18.6).
8. **Mark Slate Manually Reviewed button** — the only write action
   (§6). Disabled until every Blocking check passes. Label is fixed.
9. **Disabled caption** — pinned text when the button is disabled
   (§6).
10. **Empty state** — when no slates exist, a single info line ("No
    slates yet. Create a slate and import a DK UFC Classic salary
    CSV on the Slate Setup page first."), with the banner above it.
    Pinned by AppTest.

Write-action contract (`docs/DEVELOPMENT_NOTES.md` §11):

- One transaction per Mark Slate Manually Reviewed click, owned by
  the page handler.
- AppTest covers: selecting a slate, clicking Mark Slate Manually
  Reviewed, asserting the persisted `manual_review_status` and
  timestamp.
- Idempotence test: clicking the button twice produces identical
  persisted state on the value column (timestamp refresh per §6).
- Disabled-state test: a slate with at least one Blocking failure
  has the button disabled and the disabled caption rendered.
- "Re-review after upstream change" test: after a salary re-import
  the test asserts that the page **continues to show
  `reviewed`** (because v1 does not auto-invalidate) and that the
  banner's "re-review after re-import" text is rendered (pinned).
  This is the explicit acknowledgement of §18.3.

What the page must **not** contain in v1:

- No Unmark / Reset Review button. v1 has no Undo path; supersede
  is a re-click after the user changes upstream state.
- No "apply to all slates" controls. Per-slate writes only.
- No filter / search controls beyond the slate selector.
- No history view of prior review events. Persistence option B
  (§9.2) stores only the latest review.
- No links that mutate other tables (no "and also recompute odds,"
  no "and also confirm fight groups"). The page writes
  `manual_review_status` only.
- No charts, plots, or distribution renders.
- No raw odds row contents, raw DK CSV row contents, or fighter
  salaries beyond what is already on the other pages
  (`docs/DEVELOPMENT_NOTES.md` §7).

## 8. Repository / service concept

Module layout (future, not implemented in this slice):

- `src/slate/manual_review.py` — pure module. Owns:
  - The §5 check identifier constants
    (`CHECK_SALARY_IMPORTED`, `CHECK_FIGHT_GROUP_COVERAGE`,
    `CHECK_FIGHT_GROUP_REVIEW`,
    `CHECK_SCHEDULED_ROUNDS_REVIEWED`,
    `CHECK_ODDS_UNMATCHED_ACTIVE`,
    `CHECK_ODDS_COVERAGE_PARTIAL`,
    `CHECK_ODDS_MATCH_REVIEW`,
    `CHECK_ODDS_COVERAGE_STAT`,
    `CHECK_PROJECTION_NON_PROJECTABLE`,
    `CHECK_PROJECTION_MISSING_INPUTS`,
    `CHECK_MISMATCH_ALERTS_WARN`,
    `CHECK_MISMATCH_ALERTS_INFO`,
    `CHECK_LATE_NEWS_RISK_LOCKED`,
    `CHECK_FIGHTER_STATUS_REVIEW`,
    `CHECK_LATE_NEWS_ACKNOWLEDGED`,
    `CHECK_MANUAL_REVIEW_USER_ACK`).
  - The category mapping
    (`CHECK_CATEGORY: dict[str, "blocking" | "warning" | "informational"]`).
  - The `ReviewCheckResult` value object (id, category, status,
    message, tags).
  - Pure predicate helpers (`is_blocking(check_id) -> bool`,
    `is_warning(check_id) -> bool`, `is_informational(check_id)
    -> bool`).
  - The `BLOCKING_THRESHOLD_ODDS_UNMATCHED_PCT = 0.5` constant for
    §5.4.a (test-pinned).
- `src/slate/manual_review_service.py` (or extend
  `src/slate/manual_review.py` if the file stays small) —
  composition layer:
  - `evaluate_readiness(conn, slate_id) -> ReviewReadiness` —
    returns a value object with `(slate_id, manual_review_status,
    manual_review_completed_at, checks: list[ReviewCheckResult],
    blocking_pass: bool, warning_count: int,
    informational_count: int)`. Pure read end to end (no DB
    mutation, mirrors `evaluate_alerts` and `project_slate`).
  - Composition order: `project_slate` first, then
    `evaluate_alerts` (the alerts call already runs
    `project_slate` once — design §11 of
    `MISMATCH_ALERTS_V1_DESIGN.md` — so the service must reuse the
    projection list rather than re-evaluating; see Phase B in §10).
  - The repository reads for §5.1 / §5.2 / §5.3 / §5.4 use the
    existing repository methods directly
    (`FighterRepository.list_for_slate`,
    `FightGroupRepository.list_for_slate`,
    `OddsMatchResultRepository.list_for_slate`,
    `ManualMatchOverrideRepository.list_active_for_slate`,
    `OddsRowRepository.list_for_slate`).
- `src/db/repositories.py` — extend `SlateRepository` with:
  - `set_manual_review_reviewed(slate_id) -> SlateRecord` — write
    side. Single transaction (`with self.conn:`), idempotent on
    re-call (refreshes timestamp per §6), validates that the slate
    exists. **Does not** read any check state; check evaluation is
    the page handler / service concern. The repository's only job
    here is the row write.
  - The existing `SlateRecord` dataclass gains
    `manual_review_status: str` and
    `manual_review_completed_at: str | None` fields when the §9.2
    columns land.

Hard contracts:

- The repository layer is the **only** path that writes
  `manual_review_status`. The Streamlit page must not execute SQL
  directly (`docs/DEVELOPMENT_NOTES.md` §11).
- The service layer (`evaluate_readiness`) is **pure read**. It
  performs **no DB writes**. An integration test that observes a
  write is a bug (mirrors `MISMATCH_ALERTS_V1_DESIGN.md` §11 and
  `PROJECTION_V1_DESIGN.md` §9 cross-cutting).
- The service layer **does not** read or write `odds_match_results`
  / `manual_match_overrides` / `fighters.manual_status`
  (Fighter Status integration is deferred — §13 below). It reads
  `odds_match_results.match_status` and `effective_status` only for
  the §5.4.c row, and only to count statuses, not to consume them
  downstream.
- The service signature is `(conn, slate_id) -> ReviewReadiness`.
  No optional threshold parameter, no optional `effective_status`
  parameter — extension is a v2 concern.
- The service must call `project_slate` and `evaluate_alerts`
  **at most once each** per invocation. v1 calls both because the
  alerts service already calls projection internally
  (`MISMATCH_ALERTS_V1_DESIGN.md` §11), so re-using the alerts
  output is the cheapest path; v1 documents this in the Phase B
  slice report.

## 9. Schema / persistence design options

Manual Review Gate v1 must choose between three persistence shapes.
The shape determines whether a schema change is required. All three
options are presented; **option B is the recommendation** and is
called out below.

### 9.1 Option A — session-state only

- Hold `manual_review_status` and `manual_review_completed_at` in
  `st.session_state`.
- No new column, no new table, no migration.

Pros:

- Zero schema change. Smallest possible diff.
- No re-import invalidation concerns; the state evaporates anyway.

Cons:

- **Lost on app reopen.** The user's review decision does not survive
  closing the browser tab or restarting Streamlit. The whole point of
  the gate is an auditable record of when the slate was reviewed;
  session-state can't carry that across runs.
- **No downstream contract.** Future optimizer / export need a
  durable signal to refuse to run; `st.session_state` is invisible
  to a non-Streamlit caller.
- **No audit trail.** No timestamp survives.

Verdict: **rejected for v1.** Documenting it so a future
contributor does not reach for it as "the cheap option."

### 9.2 Option B — new columns on `slates` (recommended)

- Add to `slates`:
  - `manual_review_status TEXT NOT NULL DEFAULT 'not_reviewed'`
    — value set `{'not_reviewed', 'reviewed'}`. v1 set is closed.
  - `manual_review_completed_at TEXT NULL` — ISO-8601 UTC timestamp
    set when status flips to `reviewed`.
- (Optional, paired) `late_news_acknowledged_at TEXT NULL` — to make
  the §5.8 toggle survive page reload. This column is **opt-in for
  Phase B** — see §18.6 open question. v1 may ship without it and
  treat the toggle as session-only.
- The repository layer (`SlateRepository.set_manual_review_reviewed`)
  is the only writer. Reads come through the existing
  `SlateRepository.list_all` (extended to return the two new
  fields).

Schema change required: **yes** — one migration in
`src/db/migrations.py`, paired with a schema test
(`tests/test_odds_persistence_schema.py` or a sibling) per
`docs/DEVELOPMENT_NOTES.md` §8.

Pros:

- Survives app reopen. Auditable.
- One-row read; no join.
- Latest review only — matches v1's "no per-event history"
  requirement.
- Mirrors the existing per-slate denormalised columns
  (`salary_csv_status`, `salary_row_count`) so the SQL shape is
  familiar.

Cons:

- No history. A user who toggles between `not_reviewed` and
  `reviewed` (after re-imports) leaves only the latest value.
  Mitigation: §18.7 open question on whether a per-slate audit
  table is justified; v1 does not ship one.
- Re-arming on upstream writes is not automatic. v1 deliberately
  defers automatic invalidation (§18.3).

Verdict: **recommended for v1.** Option B is the minimum-overhead
shape that survives reopens, gives the future optimizer / export a
queryable column, and keeps the surface narrow. The optional
`late_news_acknowledged_at` may be deferred to a follow-up slice
without changing the shape of the main columns.

### 9.3 Option C — dedicated `manual_review_runs` table

- Add a new table:
  ```
  CREATE TABLE manual_review_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
      status TEXT NOT NULL,
      source TEXT NOT NULL DEFAULT 'user',
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      superseded_at TEXT NULL,
      notes TEXT NULL
  );
  CREATE INDEX idx_manual_review_runs_slate
      ON manual_review_runs (slate_id)
      WHERE superseded_at IS NULL;
  ```
- The effective `manual_review_status` is the latest
  non-superseded row per `slate_id`, falling back to
  `'not_reviewed'` when no row exists.
- Mirrors the `manual_match_overrides` / `fighter_status_overrides`
  shape.

Schema change required: **yes** — one migration plus a paired
schema test.

Pros:

- Full history per slate. Every review event is preserved.
- Provenance via the `source` column (room for `'user'`,
  `'system'`, future `'auto_invalidation'`).
- Consistent shape with the override-history tables the repo
  already understands.

Cons:

- Higher implementation cost. Every read path runs a correlated
  subquery / `WHERE superseded_at IS NULL ORDER BY id DESC LIMIT 1`
  to resolve the latest event.
- v1 has no concrete review workflow that needs history (the gate
  is a single "yes / no" decision); Option C ships an audit table
  without an audit consumer.
- Risks looking like an override-history table without the
  competing-override semantics that justify the shape elsewhere.

Verdict: **deferred to a future design pass.** If a real review
workflow ever needs history (e.g. "show me every review event in
the last month"), promoting from B to C is additive (the column on
`slates` can stay as a denormalised cache, or be dropped) and
lossless. Reaching for C in v1 is over-design.

### 9.4 Recommendation summary

**Option B is recommended.** It requires a small schema change
(two new columns on `slates`, one migration, one schema test) and
gives the future optimizer / export a stable queryable signal at
minimum cost. Option A is rejected. Option C is deferred behind a
real history-requiring workflow.

The optional `late_news_acknowledged_at` column (§9.2) may ship in
the same Phase B migration or be split into a follow-up Phase B'
slice without changing the rest of the design.

## 10. Implementation phase split

The slices below are **future work** — this doc creates none of
them. Each slice gets its own design check-in and its own commit per
`docs/DEVELOPMENT_NOTES.md` §13 ("one slice per session") and
`AI_BUILD_WORKFLOW.md` §3–§4. Each slice is sized to fit the §3
limits (≤ ~150 net lines, ≤ ~5 files, one layer per slice).

- **Phase A — Pure check resolvers + types.** Extend
  `src/slate/manual_review.py` with the §5 check identifier
  constants, the §4 category mapping, the `ReviewCheckResult`
  value object, the predicate helpers, the §5.4.a threshold
  constant, and **pure** per-check evaluation functions that take
  already-resolved inputs (fighter list, fight-group list,
  projection list, alert list, match-result list) and return a
  `ReviewCheckResult`. Pure-Python, no DB, no Streamlit. Unit tests
  pin every category mapping in §4 and every threshold in §5.
- **Phase B — Schema + migration + repository write helper.** Add
  the §9.2 columns to `slates` (Option B). Write the migration in
  `src/db/migrations.py`. Add a schema test under
  `tests/test_odds_persistence_schema.py` (or a sibling). Extend
  `SlateRepository` with `set_manual_review_reviewed` only — read
  still comes from `list_all` (extended to return the new fields).
  Unit / repository tests cover write validation, idempotence (the
  timestamp-refresh policy), and that the repository never reads
  `evaluate_readiness` (the page composes the two).
- **Phase C — Read aggregator service for the UI.** Add
  `src/slate/manual_review_service.py::evaluate_readiness` per §8.
  Read-only; composes `project_slate` + `evaluate_alerts` + the
  repository reads in §5.1 / §5.2 / §5.3 / §5.4. Returns a
  `ReviewReadiness` value object with the §4 category split. Unit /
  integration tests against a fixture DB cover every Blocking /
  Warning / Informational path in §5.
- **Phase D — Streamlit page wiring.** Replace
  `app/pages/06_manual_review.py` with the §7 page. AppTest pins
  the banner, the slate header line, the Blocking / Warning /
  Informational sections, the Mark Slate Manually Reviewed
  enable / disable logic, the click write path, and the empty state.
- **Phase E — Real / manual smoke.** A manual checklist run against
  the same real DK UFC Classic salary CSV used for the salary
  importer smoke (`SALARY_PERSISTENCE_DESIGN.md` §13). No new code;
  this is a documented validation step, not a slice.
- **Phase F — Downstream consumer wiring (separate, gated).**
  Promoting `manual_review_status` into the future optimizer (§11)
  and the future export / run log (§12), and Fighter Status v1
  integration into the §5.7 row (§13). Each promotion is its
  **own** design pass and its own slice, requires explicit user
  instruction per `docs/DEVELOPMENT_NOTES.md` §10, and lands only after Phases A–E
  are documented complete.

**Phase ordering is strict.** Phase B depends on Phase A's types
and category mapping. Phase C depends on Phase B's persistence (so
the readiness service can read `manual_review_status`). Phase D
depends on Phase C's read aggregator. Phase E depends on Phase D
being merged. Phase F has no v1 dependency and is gated on its own
design.

## 11. Effect on future optimizer

The optimizer is a skeleton (`src/optimizer/validation.py` contains
the same-fight-pair validator; lineup generation is not in v0).
When optimizer construction lands, Manual Review Gate v1's expected
role is:

- **Refuse to run** on a slate whose `manual_review_status` is not
  `'reviewed'`. The check is in optimizer input assembly, not in
  the Manual Review page. The Manual Review page does not call the
  optimizer; the optimizer reads the slate.
- **Surface the reason**. An optimizer error in the unreviewed-slate
  case must reference Manual Review explicitly so the user knows
  where to go.
- **No back-channel write**. The optimizer does not flip
  `manual_review_status` on success or on failure. The status is
  user-owned (§6).
- **No optimizer wiring in v1.** Per `docs/DEVELOPMENT_NOTES.md` §3 and §10,
  optimizer implementation is not on deck.

## 12. Effect on future export / run log

The Export / Run Log page is a v0 placeholder
(`app/pages/08_export_run_log.py`). When it lands, Manual Review
Gate v1's expected role is:

- **Refuse to emit** a DK upload CSV for a slate whose
  `manual_review_status` is not `'reviewed'`. The refusal is in the
  exporter, not in the Manual Review page.
- **Run-log annotation**. Every emitted lineup row in the run log
  must reference the `manual_review_completed_at` of the slate it
  was built from, so a post-event review can see *when* the human
  blessed the slate.
- **Manual Review runs first**. By §11 the optimizer cannot produce
  a lineup for an unreviewed slate; the export-side refusal is a
  belt-and-braces last check.
- **No export wiring in v1.** Per `docs/DEVELOPMENT_NOTES.md` §3 and §10, export is
  not on deck.

## 13. Effect on Fighter Status v1 integration

`docs/FIGHTER_STATUS_V1_DESIGN.md` §9 ("Effect on future Manual
Review gate") sets the contract this design honors:

- **Manual Review surfaces Fighter Status, does not replace it.**
  The §5.7 row reads Fighter Status, categorises per
  `FIGHTER_STATUS_V1_DESIGN.md` §5, and asks the user to
  acknowledge the count.
- **Acknowledgement is a Manual Review concern.** Whether the user
  has acknowledged a non-`active` fighter is **not** a Fighter
  Status concern. v1 stores only the per-slate
  `manual_review_status`, not per-fighter ack state. Per-fighter
  ack is a v2 question (§18.2).
- **One-directional dependency.** Manual Review reads Fighter
  Status; Fighter Status does not read Manual Review. This matches
  `FIGHTER_STATUS_V1_DESIGN.md` §9.
- **§5.7 is Informational in v1.** Per §5.7, the row renders but
  evaluates to "Fighter Status integration not yet active." The
  promotion from Informational to Blocking / Warning is gated on a
  Phase F design slice that updates this doc, Fighter Status §5,
  Projection v1 §10 question #3, and Mismatch Alerts v1 §3.9 in a
  coordinated pass.

v1 implements **none** of the Phase F integration. The
documentation here is so the eventual integration slice has a stable
target.

## 14. `effective_status` separation (locked)

`odds_match_results.effective_status`
(`docs/ODDS_PERSISTENCE_DESIGN.md` §8) and the Manual Review Gate
remain **different layers** in v1:

- The Manual Review service may **read** `effective_status` for the
  §5.4.c "review_required / review_rejected" Warning count, but
  must **never** consume `effective_status` to alter Projection v1
  output, Mismatch Alerts v1 output, or the gate's eligibility logic.
- The Manual Review write (§6) must **never** mutate
  `odds_match_results.effective_status` or
  `manual_match_overrides`.
- Per `ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7,
  `effective_status` remains inert downstream until its own
  separate design pass; that posture does not change in this
  design.
- The eventual "eligibility resolver" that unifies Fighter Status
  and `effective_status` for the optimizer / export
  (`FIGHTER_STATUS_V1_DESIGN.md` §17.2) is **out of scope** for
  Manual Review v1; the gate is a workflow checkpoint, not an
  eligibility resolver.

## 15. Test plan (for the future implementation)

Pure unit tests (Phase A) — no DB, no Streamlit:

- Check id constants: every check in §5 is exported as a module
  constant; the constants tuple / set contains exactly the v1 set
  (assertion uses `==`, not subset).
- Category mapping: each check belongs to exactly one category;
  every check in §5 appears in the mapping; no extra keys.
- Predicate helpers: `is_blocking(CHECK_SALARY_IMPORTED)`,
  `is_warning(CHECK_ODDS_MATCH_REVIEW)`,
  `is_informational(CHECK_MISMATCH_ALERTS_INFO)`; negative cases for
  each.
- Threshold constants: `BLOCKING_THRESHOLD_ODDS_UNMATCHED_PCT ==
  0.5`. A change to this constant must require a paired test
  update.
- Per-check pure evaluators: each takes a fixture input (e.g.
  `(fighter_list, fight_group_list, projection_list, alert_list,
  match_result_list)`) and returns the expected
  `ReviewCheckResult`. Boundary cases: empty inputs; just-above and
  just-below thresholds for §5.4.a; mixed projection statuses for
  §5.5; mixed alert severities for §5.6.

Schema / migration tests (Phase B):

- Schema test asserts the `manual_review_status` and
  `manual_review_completed_at` columns exist on `slates` with the
  correct types, nullability, and DEFAULT after `apply_schema`.
  Mirrors the existing pattern in
  `tests/test_odds_persistence_schema.py`.
- Migration test asserts that running the new migration on a DB
  created from the pre-migration schema preserves all existing
  `slates` rows and sets `manual_review_status` to
  `'not_reviewed'` and `manual_review_completed_at` to `NULL` for
  every prior row.

Repository tests (Phase B):

- Write validation: an attempt to set the status on a non-existent
  slate raises `ValueError` and persists nothing.
- Idempotence: calling `set_manual_review_reviewed` twice on the
  same slate produces a `'reviewed'` value column and a refreshed
  `manual_review_completed_at` timestamp (per §6).
- No side effects: row counts / timestamps on `fighters`,
  `fight_groups`, `odds_rows`, `odds_match_results`,
  `manual_match_overrides` are unchanged across a
  `set_manual_review_reviewed` call (mirrors
  `FIGHTER_STATUS_V1_DESIGN.md` §16 cross-cutting).
- Re-import safety: setting `manual_review_status='reviewed'`, then
  running `FighterRepository.upsert_for_slate` with new rows, must
  leave `manual_review_status` and `manual_review_completed_at`
  unchanged (v1 does not auto-invalidate — §18.3 — and the test
  pins the contract so a future contributor cannot silently flip
  it).

Service / repository tests (Phase C):

- Slate with mixed fixture state (some `non_projectable`, some
  `missing_inputs`, some `ok`; at least one warn alert; an
  unmatched odds count just above and just below the §5.4.a
  threshold; an unconfirmed fight group): assert the returned
  `ReviewReadiness` lists every expected check id with the expected
  category and pass / fail.
- Empty slate (no active fighters): returns a `ReviewReadiness`
  whose §5.1 check fails (Blocking) and whose downstream checks
  short-circuit. The blocking_pass flag is `False`.
- Slate with no fight groups: §5.2.a fails (Blocking).
- Slate with full odds coverage and all fight groups confirmed: all
  Blocking checks pass; `blocking_pass == True`.
- Unknown slate id: returns a `ReviewReadiness` with a single
  Blocking failure (§5.1) and no other checks (mirrors
  `evaluate_alerts` / `project_slate` returning `[]`).
- **No DB mutation**. Assert row counts / timestamps unchanged
  across the call.
- **`effective_status` not consumed downstream**. Build a fixture
  with a Reject Match override and assert that the
  `ReviewReadiness.checks` for §5.5 / §5.6 are identical to the
  no-override case (the only place `effective_status` legitimately
  surfaces is the §5.4.c Warning count).
- **`late_news_risk` stays reserved**. Pin that no
  `evaluate_alerts` output ever drives a §5.6.a Warning whose code
  list contains `late_news_risk` in v1 (mirrors
  `FIGHTER_STATUS_V1_DESIGN.md` §16 cross-cutting and
  `MISMATCH_ALERTS_V1_DESIGN.md` §15 risk #8).

UI tests (Phase D) — AppTest:

- Page renders the banner text from §7 item 2 verbatim.
- Page renders the slate header line from §7 item 3 with correct
  status (initially `not_reviewed`) and no timestamp.
- Page renders the Blocking / Warning / Informational sections in
  that order; pass / fail badges match the service output.
- Mark Slate Manually Reviewed button is disabled when any Blocking
  check fails. Disabled caption is rendered (pinned text).
- Mark Slate Manually Reviewed button is enabled when every
  Blocking check passes.
- Write action: clicking the button flips
  `manual_review_status` to `'reviewed'`, sets
  `manual_review_completed_at`, and re-renders the slate header
  with the new status and timestamp.
- Idempotence: clicking the button twice produces a single
  `'reviewed'` status (no error) and a refreshed timestamp on the
  second click.
- "Re-review after re-import" pin: after a salary re-import the
  page continues to show `reviewed` and the banner's
  re-review-after-re-import text is rendered.
- Empty state pins the "No slates yet." text.
- Per `docs/DEVELOPMENT_NOTES.md` §11: no write action bypasses the repository
  layer (assert via repository spy or by introspection of executed
  SQL in the fixture connection).
- No write action exists besides Mark Slate Manually Reviewed —
  AppTest confirms no other buttons or forms are present.

Cross-cutting:

- **No projection / alerts side effects.** A Manual Review write
  must not change the output of `project_slate(...)` or
  `evaluate_alerts(...)` for the same slate. This is the explicit
  test that v1 does not silently change current behavior.
- **No `effective_status` side effects.** A Manual Review write
  must not change any row in `odds_match_results` or
  `manual_match_overrides`. Mirrors §14.
- **No fighter status side effects.** A Manual Review write must
  not change any column on `fighters` (including the future
  `manual_status` once Fighter Status persistence is wired).
  Mirrors `FIGHTER_STATUS_V1_DESIGN.md` §8.

## 16. Real-feed / manual smoke plan (Phase E)

The smoke is a documented manual checklist, not a code slice. It
runs after Phase D is merged and reuses the real DK UFC Classic
salary CSV from the salary importer smoke
(`SALARY_PERSISTENCE_DESIGN.md` §13) plus the real odds export and
fight-group / Projection v1 / Mismatch Alerts v1 state from the
prior smokes. No CSV contents land in the design doc, in git, or in
any external service (`docs/DEVELOPMENT_NOTES.md` §7, §13).

Checklist (run locally; document outcome only):

1. **Pre-smoke git safety.** Working tree clean. Salary smoke,
   Projection v1 smoke, and Mismatch Alerts Phase D smoke already
   documented. Confirm `.gitignore` still excludes the real CSV
   under `data/uploads/salaries/*` and the odds CSV under
   `data/uploads/odds/*`.
2. **Slate setup.** Re-use the existing import flow on
   `app/pages/01_slate_setup.py` for the real DK UFC Classic
   salary CSV (already-validated path from the salary smoke). Note
   the slate id.
3. **Initial Manual Review read.** Open
   `app/pages/06_manual_review.py`, pick the slate, confirm:
   - The banner from §7 item 2 renders verbatim.
   - The slate header reads `Status: not_reviewed`.
   - Every Blocking row from §5 renders, with pass / fail badges
     consistent with the salary import state.
   - The Mark Slate Manually Reviewed button is disabled iff at
     least one Blocking check fails.
4. **Blocking-pass smoke.** Walk through the §5 Blocking rows: open
   the relevant page for each failure, fix the underlying state,
   return to Manual Review, confirm the row now shows pass and the
   button becomes enabled.
5. **Warning section smoke.** Confirm the Warning list mirrors the
   off-app expectations from
   `FIGHT_WEEK_DATA_COLLECTION_CHECKLIST.md` §10 / §11.1 / §11.2.
   The late-news (§5.8) row shows the unchecked toggle.
6. **Late-news ack smoke.** Click the §5.8 toggle. Confirm the row
   flips to passed with a "<acked at>" caption. If
   `late_news_acknowledged_at` shipped in Phase B, reload the page
   and confirm the ack survives; otherwise note that it is
   session-only (open question §18.6).
7. **Mark Reviewed smoke.** Click Mark Slate Manually Reviewed.
   Confirm:
   - The slate header now reads
     `Status: reviewed (at <YYYY-MM-DD HH:MM:SS> UTC)`.
   - The Blocking section's §5.9 row shows passed.
   - The Mark Slate Manually Reviewed button remains visible and
     enabled (per §6, re-clicking refreshes the timestamp).
8. **Idempotence smoke.** Re-click Mark Slate Manually Reviewed.
   Confirm the timestamp refreshes; the rest is unchanged.
9. **Re-import smoke.** Re-run the salary CSV import on the same
   slate. Open Manual Review again. Confirm:
   - `Status: reviewed (at <prior timestamp>)` — v1 does **not**
     auto-invalidate (§18.3).
   - The banner's "re-review after re-import" text is rendered.
   - Manually re-click Mark Slate Manually Reviewed to refresh the
     timestamp, simulating the user's explicit re-review.
10. **Cross-page non-leak smoke.** Open
    `app/pages/03_odds.py`,
    `app/pages/05_alerts.py`,
    `app/pages/09_projections.py`. Confirm none of them changed
    behaviour as a result of the Manual Review writes (per §14 and
    §15 cross-cutting).
11. **Failure / anomaly logging.** Note any divergence from steps
    3–10 in the slice report. Do **not** paste fighter names from
    the real CSV (`docs/DEVELOPMENT_NOTES.md` §7); use row indices or counts
    instead.

Completion criterion: all eleven steps pass and the slice report
documents the smoke outcome. Until then, Manual Review Gate v1 must
not be described as complete (`docs/DEVELOPMENT_NOTES.md` §8, §14).

## 17. Non-goals

Manual Review Gate v1 explicitly does **not** ship any of:

- Lineup generation, lineup ranking, lineup scoring.
- Optimizer construction of any kind. The gate produces a status
  the optimizer will read; it does not invoke the optimizer.
- DK upload export. The gate produces a status the exporter will
  read; it does not write a DK CSV.
- Automatic DK contest entry, late-swap automation, DraftKings
  login automation, or any DK screen automation.
- Ownership projections, leverage, field-share calculations.
- News / Twitter / X / RSS scraping or any external source of
  fighter availability. The §5.8 late-news check is a user-driven
  acknowledgement only.
- UFCStats / Sherdog / Tapology / ESPN scraping of any kind.
- Direct Odds API or any remote HTTP fetch.
- Per-fighter acknowledgement state (e.g. "I have ack'd that
  Fighter X is questionable"). Per-fighter ack is v2 open question
  §18.2.
- Per-readiness-check acknowledgement state. v1 has one ack: Mark
  Slate Manually Reviewed (§6).
- Multiple concurrent review sessions / multi-user review queues.
  The repo is single-user / single-machine (`docs/DEVELOPMENT_NOTES.md` §1).
- Recompute of any kind. The page reads what the existing services
  produce; it does not invoke a recompute path. The user is
  responsible for clicking Recompute on the Odds page if needed
  before reviewing.
- Modification of any other page's behaviour. The Odds page does
  not learn about Manual Review; the Projections page does not;
  the Alerts page does not. The contract is one-way:
  Manual Review reads, others do not read Manual Review (until
  Phase F).
- Automatic invalidation of `manual_review_status` when salary,
  odds, fight groups, overrides, or fighter status change (§18.3
  open question).
- Per-slate audit / history table in v1 (Option C in §9 is
  deferred).
- Threshold configurability (the §5.4.a 50% threshold is fixed in
  v1; reconfiguration requires an explicit design + test update).
- Unification of Fighter Status and `effective_status` into a
  single eligibility resolver
  (`FIGHTER_STATUS_V1_DESIGN.md` §17.2; out of scope here).
- D.5 odds-match override types (Accept, Force Pair, Exclude,
  manual moneyline, low-confidence ack) —
  `docs/DEVELOPMENT_NOTES.md` §10 keeps D.5 paused, and Manual Review v1 has no
  dependency on it.
- NFL, Showdown, Pick6, or any non-UFC-Classic format
  (`docs/DEVELOPMENT_NOTES.md` §3).
- Cross-slate aggregation ("which slates tend to fail Blocking
  checks"). v1 evaluates one slate at a time.

Any item above requires a **separate** design doc and explicit
approval before implementation.

## 18. Risks and open questions

1. **Threshold calibration (open).** The §5.4.a 50% odds-coverage
   threshold is intuition-based and has not been validated against
   a real slate. The Phase E smoke (§16) is the first time real
   coverage numbers will be observed. Risk: the threshold blocks
   slates a user would have happily reviewed, or fails to block
   slates with too little coverage. Mitigation: the threshold is a
   single named constant pinned by the Phase A test; adjustment
   requires an explicit design + test update.
2. **Per-warning acknowledgement (open).** v1 has one ack: Mark
   Slate Manually Reviewed (§6). A future v2 may add per-warning
   ack state ("I have ack'd that fighter X has missing inputs").
   Recommendation: stay slate-level in v1; revisit only if Phase E
   surfaces a concrete workflow where the user wants to "save
   progress" mid-review.
3. **Auto-invalidation on upstream writes (open).** v1 does **not**
   automatically flip `manual_review_status` back to
   `'not_reviewed'` when salary, odds, fight-group, override, or
   Fighter Status state changes. The banner text and the Phase D
   AppTest pin this contract. Risk: a user re-imports salary,
   forgets to re-review, and a future optimizer trusts the stale
   `'reviewed'` value. Mitigation: the banner is loud, the "last
   reviewed at" timestamp is prominent, and the future optimizer
   (§11) reads only the current status anyway — but a tighter
   contract is genuinely better. Promotion to auto-invalidation is
   a separate slice with its own design pass.
4. **Scheduled-rounds blocking elevation (open).** §5.3 is Warning
   in v1. If a real slate ships a 5-round bout silently
   un-confirmed and the user misses it, the projection's
   `+7 five_round_bonus` is wrong. Recommendation: leave as Warning
   in v1 (no automated rounds source — the gate cannot verify) and
   rely on the user. Phase E may surface a need to elevate.
5. **Odds-coverage threshold (open).** See risk #1. Same posture.
6. **`late_news_acknowledged_at` persistence (open).** §9.2 lists
   it as opt-in for Phase B. If v1 ships without it, the §5.8
   toggle is session-only and resets on every page reload, which
   may surprise the user. Recommendation: ship the column in Phase
   B if it can land in the same migration without extending the
   slice past `AI_BUILD_WORKFLOW.md` §3 limits; otherwise split it
   into Phase B'.
7. **History (open).** Option C (§9.3) would persist every review
   event. v1 picks B (no history) because no concrete workflow
   demands history yet. Risk: a future workflow needs history and
   we have to migrate. Mitigation: B → C migration is additive
   (add the table, treat the column as denormalised latest), so
   the cost of deferring is low.
8. **Idempotence timestamp policy (open).** When the user re-clicks
   Mark Slate Manually Reviewed, should `manual_review_completed_at`
   refresh, or should the write no-op entirely? Recommendation:
   refresh the timestamp, no-op the value column. Mirrors
   `FIGHTER_STATUS_V1_DESIGN.md` §19.4. Pin both behaviours in the
   Phase B test.
9. **Re-import behaviour with `manual_review_status='reviewed'`
   (open).** The importer flips `status` to `inactive` for absent
   rows. If the user marked the slate reviewed and the importer
   subsequently flips a fighter to `inactive`, the slate may now
   include a non-projectable fighter that was not present when the
   slate was reviewed. Recommendation: per §18.3 do nothing in v1
   (the user must re-review); pin a Phase B / Phase D test that
   asserts the importer does not silently flip
   `manual_review_status`.
10. **UI noise on large cards (open).** A full UFC PPV may carry
    28+ fighters. The Blocking / Warning / Informational sections
    can grow long if the user has not yet resolved §5.5 /  §5.6.
    Mitigation: each section's row count is one row per **check
    code**, not one row per fighter; per-fighter detail is collapsed
    into a single message string capped at 10 names. If Phase E
    surfaces ergonomic pain (e.g. the user wants the per-fighter
    list inline), introduce an expander in a follow-up slice rather
    than re-shape v1.
11. **Coupling to `MISMATCH_ALERTS_V1_DESIGN.md` §3.9 reserved
    code (`late_news_risk`).** Same posture as
    `FIGHTER_STATUS_V1_DESIGN.md` §19.8. Mitigation: the cross-
    cutting test in §15 pins the "never emitted" contract so an
    accidental promotion fails CI. A Phase F design pass that flips
    §3.9 from reserved to active must update this doc, Fighter
    Status §7, Mismatch Alerts §3.9, and the cross-cutting tests in
    the same slice.
12. **Coupling to `PROJECTION_V1_DESIGN.md` §5 reserved tag
    (`"fighter_status"`).** The §5.5.a Blocking message lists
    structural tags. When the future Phase F slice flips Projection
    v1 to emit the `"fighter_status"` tag, the §5.5.a message picks
    it up without code changes here, but the Phase F design must
    update this doc in the same slice to confirm the wiring is
    intentional.
13. **Coupling to `FIGHTER_STATUS_V1_DESIGN.md` §5 categorisation.**
    The §5.7 future row imports
    `STATUS_CATEGORY` from `src/slate/fighter_status.py` (Phase A
    of the Fighter Status design). Risk: a future change to that
    mapping silently re-categorises rows on this page. Mitigation:
    Phase F's Fighter-Status-into-Manual-Review slice must update
    both docs and the relevant cross-cutting test in the same
    slice.
14. **Schema-change discoverability.** Option B introduces two new
    columns on `slates`; downstream code that reads `slates`
    without going through `SlateRepository` may miss the new
    columns. Mitigation: per `docs/DEVELOPMENT_NOTES.md` §11, the repository is the
    only legal path; any direct-SQL read of `slates` outside the
    repository is an existing violation regardless of this design.
15. **Manual Review duplication (open).** The Manual Review page,
    the Alerts page, and (when integrated) the Fighter Status page
    will surface overlapping information about non-`active`
    fighters, missing inputs, and warn alerts. Risk: the surfaces
    drift over time. Mitigation: the Manual Review service
    (§8) imports from `alert_rules`, `slate_projection_service`,
    and (future) `fighter_status` rather than duplicating
    classification logic. The duplication of *rendering* is
    intentional — Alerts is the **feed**, Fighter Status is the
    **workbench**, Manual Review is the **gate**.
16. **No real-feed signal for the smoke (open).** Phase E (§16) is
    a manual click-through; there is no automated signal that the
    smoke ran. Mitigation: completion is documented in the slice
    report under `docs/DEVELOPMENT_NOTES.md` §12's Reporting Format, matching the
    salary importer's Slice F precedent and Fighter Status v1's
    Phase E.
17. **Smoke-CSV residue.** Phase E re-uses the real DK UFC Classic
    salary CSV. Risk: someone runs the smoke and accidentally
    commits the CSV. Mitigation: §16 step 1 calls out the
    `.gitignore` check explicitly, and `docs/DEVELOPMENT_NOTES.md` §7 / §14
    already forbid the commit.
