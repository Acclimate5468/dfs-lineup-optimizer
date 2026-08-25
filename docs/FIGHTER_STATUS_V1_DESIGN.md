# Fighter Status v1 Design

Status: design only. No implementation in this slice. Companion to
`docs/DEVELOPMENT_NOTES.md` §2 (v0 scope), §3 (out-of-scope list), §10 (current
checkpoint), §11 (UI write-action rules), §14 (do-not quick reference),
`docs/PROJECTION_V1_DESIGN.md` §5 / §10 (the reserved
`"fighter_status"` tag and the open question about inactive-fighter
gating), `docs/MISMATCH_ALERTS_V1_DESIGN.md` §3.9 / §15 risk #8 (the
reserved `late_news_risk` placeholder), and
`docs/ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7 (the still-inert
`effective_status`).

---

## 1. Purpose

Fighter Status v1 is the first **local, manual, single-purpose** layer
that lets the user mark whether a UFC fighter on a slate is actually
going to fight, and how confident that judgment is, before any
downstream consumer (Projection v1, Mismatch Alerts v1, a future Manual
Review gate, a future optimizer, a future export / run log) commits to
a lineup.

The intent, in one sentence: **a fighter who is not going to fight
should not silently end up in a saved lineup**, and a fighter whose
availability is uncertain should be visibly flagged before the user
hits "build" on anything.

Explicit non-claims:

- Fighter Status v1 is **not** a news / Twitter feed. There is no
  network access, no scrape, no API. Every status transition is the
  user clicking a control in `app/pages/04_fighter_status.py`.
- Fighter Status v1 is **not** an injury / weight-cut model. It does
  not infer probabilities of withdrawal, does not assign volatility,
  and does not predict cut outcomes.
- Fighter Status v1 is **not** a re-pairing engine. Marking one fighter
  `replaced` does not create a fight group, attach a replacement, or
  mutate `fight_groups` rows. The Fight Groups page remains the only
  place pairings change.
- Fighter Status v1 does **not** modify the projection formula in
  `docs/DEVELOPMENT_NOTES.md` §4.
- Fighter Status v1 is **not** the odds-match override layer. It does
  not read, write, or reinterpret `odds_match_results.effective_status`
  or `manual_match_overrides`. Those concern *which odds row matches
  which fighter*; Fighter Status concerns *whether the fighter is
  competing at all*. The two layers are deliberately kept separate
  (§8).
- Fighter Status v1 does **not** change current Projection v1, Mismatch
  Alerts v1, or any UI page's behavior in v1. The integration of
  Fighter Status into downstream consumers is a **separate, later
  design pass** (§7–§11) and is explicitly out of scope here.

## 2. v0 winning-focused rationale

Per `docs/LEGACY_DFS_PROMPT_AUDIT.md` §2 ("features … must either help
build stronger lineups or help prevent bad lineups"), Fighter Status v1
falls squarely on the *prevent bad lineups* side of the bar:

- **Dead-fighter prevention.** A withdrawn or out fighter in a UFC DK
  Classic roster scores zero and burns one of the user's six lineup
  slots. Surfacing availability before projections / optimizer / export
  consume the slate is the cheapest possible defence.
- **Mismatch-against-stale-data prevention.** Salary CSVs and odds
  exports are point-in-time snapshots. When a card changes after either
  was pulled, the importer cannot know — but the user usually can. A
  manual `out` / `withdrawn` / `replaced` mark is the user telling the
  workbench what they already know.
- **Slate-integrity surfacing.** A `duplicate_or_bad_row` status lets
  the user mark a fighter the salary importer created in error
  (mis-parsed name, duplicate row) without re-running the importer or
  hand-editing the DB. That row is then excluded from downstream
  consumers in the same way as `out`.
- **Reviewer trust.** A user who can see *"3 fighters need_review, 1
  questionable"* on a slate before building a lineup is more likely to
  catch a regression than one who only sees a projection number. v1
  optimises for the user's ability to spot a slate they are not yet
  ready to build from.

Fighter Status v1 does **not** claim to *improve* lineups (no edge
modelling, no leverage). It claims only to make it harder to ship a
dead one.

## 3. Manual-first / local-first workflow

All status transitions in v1 are **driven by a single user clicking a
control in the local Streamlit app**. No background job, no scheduled
task, no scrape, no API.

Operational shape:

1. Salary importer runs. `fighters` rows are created or updated with
   the importer-owned base status (`active` for present rows,
   `inactive` for prior-slate rows now absent from the CSV — see
   `src/db/repositories.py::FighterRepository.upsert_for_slate`).
2. User opens `app/pages/04_fighter_status.py` (currently a v0
   placeholder; v1 replaces it — see §11).
3. User scans the fighter list for the selected slate. For any fighter
   whose availability they want to mark, the user picks a status from
   the v1 taxonomy (§4) and writes it.
4. The status write persists locally (SQLite, single-user, single
   machine) per the persistence options in §13.
5. Downstream consumers (Projection v1, Mismatch Alerts v1, etc.)
   continue to behave exactly as they do today in v1. Their reaction
   to non-`active` statuses is a separate, later design pass (§7–§11).

Hard rules:

- **No status auto-inference.** v1 never derives a status from odds
  movement, salary change, news headline, or DK CSV row count.
- **No status from external sources.** No scraping UFCStats, MMA news,
  Twitter/X, books, or any URL. Per `docs/DEVELOPMENT_NOTES.md` §3, §14.
- **No background recompute.** Status is a manual write; the user is
  in the loop for every transition.
- **No cross-slate inference.** A fighter marked `out` on slate #12
  does not propagate to slate #14. Each (slate_id, fighter_id) carries
  its own status (§13).
- **No undo by deletion of history**, when persistence is chosen (§13).
  Corrections are themselves status writes (e.g. `out` → `active`),
  not row deletions, so the audit trail stays linear.

## 4. Proposed status values

The taxonomy below is the v1 vocabulary. Every value is a string
constant; the v1 set is **closed** and any addition requires a paired
design + test update, the same posture `docs/DEVELOPMENT_NOTES.md` §4 takes on the
projection formula.

| Value                  | Meaning                                                                                                                                            |
|------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------|
| `active`               | Default. The fighter is, as far as the user knows, on the card and competing. Importer-set on insert / re-import.                                  |
| `needs_review`         | The user has not yet decided; the row is flagged for follow-up. Functionally equivalent to "I don't know yet, don't trust downstream yet."         |
| `questionable`         | The user has heard / seen a soft signal (weight rumour, late presser absence, etc.) and wants to flag the row without yet excluding it.            |
| `out`                  | The user has confirmed the fighter is not competing on this slate. Withdrawal, illness, failed medical, removed from card.                         |
| `withdrawn`            | Synonymous with `out` from a *downstream* standpoint but tagged with a softer reason — fighter voluntarily withdrew (vs. removed). Optional split. |
| `replaced`             | The user has confirmed the fighter was replaced by a short-notice opponent. The fighter themselves is not competing; a separate row may be added.  |
| `inactive`             | Importer-set. The fighter was present on a previous import of this slate but was absent from the most recent import (see §5, importer interaction). |
| `missed_weight`        | The fighter weighed in over the contracted limit. They are likely still competing; the flag exists to surface the salary / floor risk.             |
| `short_notice`         | The fighter accepted the bout on short notice (e.g. days before the card). Likely competing; the flag exists to surface volatility.                |
| `duplicate_or_bad_row` | The user identified the fighter row as the result of an importer mis-parse / duplicate. Treated as not-competing for downstream consumers.         |

Notes:

- **`active` is the only "no signal" value.** Every other value is the
  user asserting something.
- **`inactive` is importer-owned, not user-owned.** The importer flips
  `active` → `inactive` for rows absent from a re-import
  (`SALARY_PERSISTENCE_DESIGN.md` §5). v1 keeps that behavior. If the
  user wants to assert "this fighter is out" rather than "the importer
  doesn't see them anymore," they pick `out`, not `inactive`.
- **`withdrawn` overlaps with `out`.** v1 keeps both because the user's
  framing ("removed from card by promotion" vs. "voluntarily pulled
  out") is sometimes useful in review. For downstream gating purposes
  the two are equivalent (§5). Open question §17.1 asks whether to
  collapse them.
- **`replaced` does not auto-create a replacement fighter row.** v1
  has no fighter-row INSERT path outside of salary import. Adding a
  short-notice replacement is a future Manual Review concern (§9).
- **`needs_review` and `questionable` differ only in confidence**, and
  both are *warnings*, not blockers (§5). They exist as separate
  values so the user can express "I haven't looked" vs. "I looked and
  it's iffy."
- **`duplicate_or_bad_row`** is the explicit alternative to deleting
  a row from `fighters` by hand. Per `docs/DEVELOPMENT_NOTES.md` §13 ("confirm before
  destructive actions"), v1 prefers a status mark over a DELETE.

## 5. Blocking vs warning-only

For Fighter Status v1, every value belongs to exactly one of three
**downstream categories**. The categories are how a future Phase
F (downstream integration) slice will decide whether to *include*,
*include-with-flag*, or *exclude* a fighter. v1 only *defines* the
categories — it does **not** wire them into projections, alerts,
manual review, optimizer, or exports.

| Category               | Status values                                              | Downstream meaning (future)                                                                                                                       |
|------------------------|-------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------|
| **Active**             | `active`                                                    | Eligible. Projection v1 may compute, alerts may evaluate, optimizer may pick, export may emit.                                                    |
| **Warning** (advisory) | `needs_review`, `questionable`, `missed_weight`, `short_notice` | Eligible but flagged. Downstream consumers should compute as usual but emit a `warn`-severity signal (future hook into Mismatch Alerts §3.9).      |
| **Blocking** (excluded) | `out`, `withdrawn`, `replaced`, `inactive`, `duplicate_or_bad_row` | Not eligible. Future projection / optimizer / export wiring must treat these as non-projectable / non-selectable. v1 only categorises; no wiring. |

Hard rules:

- A status falls into **exactly one** category. There is no "warn
  *and* block."
- The category set is **closed**: three categories, period. v1 does
  not introduce a fourth severity tier. If a future need (e.g.
  "advisory but optimizer-skipped") emerges, it goes in a separate
  design pass.
- The categorisation table above is the **single source of truth**.
  Any future downstream slice that reads Fighter Status must import
  the category mapping from a shared module (§12) rather than
  duplicating the value lists.

## 6. Effect on projections — v1 (deferred), future shape

**v1 behaviour: unchanged.** Projection v1 reads `fighters.status` only
to filter to `'active'` rows
(`src/projections/projection_input_service.py::aggregate_projection_inputs`
filters by `ACTIVE_FIGHTER_STATUS`). Adding new status values to the
column or persisting Fighter Status in a sibling table does not change
Projection v1's behaviour in v1 — non-`active` rows continue to be
silently excluded, exactly as today.

**Future shape (not implemented in v1).** A later Phase F slice
(separately designed) is expected to:

- Replace Phase B's silent filter with an *explicit* exclusion that
  produces a `non_projectable` row carrying the
  `"fighter_status"` tag reserved in `PROJECTION_V1_DESIGN.md` §5.
- Distinguish the **Blocking** category (§5) from the **Warning**
  category. Blocking → `non_projectable` with `"fighter_status"`.
  Warning → projection computed as usual, but with a diagnostic note
  appended (`ProjectionInputBundle.notes`) so the UI can surface
  it without consulting Fighter Status directly.
- Honor `PROJECTION_V1_DESIGN.md` §10 open question #3 (whether
  "inactive" is structural or recoverable) when deciding which
  `projection_status` value to emit.

v1 explicitly does **not** start this work. The integration is gated
on (a) Fighter Status persistence landing first, and (b) a separate
ChatGPT design pass that updates `PROJECTION_V1_DESIGN.md` §5 / §10.

## 7. Effect on alerts — v1 (deferred), future shape

**v1 behaviour: unchanged.** Mismatch Alerts v1 does not consult
Fighter Status. The reserved `late_news_risk` code
(`MISMATCH_ALERTS_V1_DESIGN.md` §3.9, `src/alerts/alert_rules.py`
`ALERT_CODE_LATE_NEWS_RISK`) continues to *never fire* in v1, which
is also the contract its Phase A test pins
(`MISMATCH_ALERTS_V1_DESIGN.md` §15 risk #8).

**Future shape (not implemented in v1).** A later slice in the
Mismatch Alerts design (likely a §3.9 promotion + Phase B integration)
is expected to:

- Activate `late_news_risk` with a trigger of: "fighter has a
  Fighter Status in the **Warning** category (§5)."
- Optionally introduce a second warn-level alert for the **Blocking**
  category (e.g. `late_news_blocking`), so the alerts page can
  distinguish "user marked questionable" from "user marked out."
- Keep severity at `warn` for both — v1's no-`error` rule
  (`MISMATCH_ALERTS_V1_DESIGN.md` §3) still applies.

v1 explicitly does **not** flip §3.9 from reserved to active. That
transition requires a paired update to
`MISMATCH_ALERTS_V1_DESIGN.md` §3.9 and to the Phase A / Phase B test
suite, and lands in its own slice after Fighter Status persistence is
in place.

## 8. `effective_status` separation (locked)

`odds_match_results.effective_status`
(`docs/ODDS_PERSISTENCE_DESIGN.md` §8) and Fighter Status are
**different layers**, addressing different questions, and v1 keeps
them strictly disjoint:

| Concern                                    | Owned by                                                | Vocabulary                                                                                  |
|--------------------------------------------|---------------------------------------------------------|----------------------------------------------------------------------------------------------|
| "Which odds row maps to which fighter?"    | `odds_match_results` + `manual_match_overrides`         | `auto_match`, `review_required`, `unmatched`, `review_rejected`, future D.5 values           |
| "Is the fighter competing on this slate?"  | Fighter Status (this design)                            | `active`, `needs_review`, `questionable`, `out`, `withdrawn`, `replaced`, `inactive`, `missed_weight`, `short_notice`, `duplicate_or_bad_row` |

Hard rules:

- Fighter Status writes must **never** mutate
  `odds_match_results.effective_status` or
  `manual_match_overrides`.
- Fighter Status reads must **never** consult
  `effective_status` to decide a Fighter Status value. A
  `review_rejected` odds match does not imply a fighter is `out`.
- Conversely, marking a fighter `out` must **never** invalidate or
  reject their odds row. The user is asserting "this person is not
  competing," not "their odds match is wrong."
- Per `ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7, `effective_status`
  remains inert downstream until its own separate design pass; that
  posture does not change in this design.
- The two layers may **someday** be combined into a unified
  "eligibility resolver" for the optimizer / export pipeline. That is
  an explicit future-design item (§17.2), not a v1 concern.

## 9. Effect on future Manual Review gate

The Manual Review page is currently a v0 placeholder
(`app/pages/06_manual_review.py`). When it lands, Fighter Status v1's
expected role is:

- **Manual Review surfaces, does not replace, Fighter Status.** The
  Manual Review page becomes the single "before you build" checklist
  surface; it reads Fighter Status, lists every fighter not in the
  **Active** category (§5), and asks the user to acknowledge each.
- **Acknowledgement is its own layer.** Whether the user has
  acknowledged a non-active fighter is a *Manual Review* concern, not
  a Fighter Status concern. v1 does not store acknowledgement state.
- **The gate is downstream-blocking, not status-blocking.** Manual
  Review can refuse to release a slate to the optimizer / export
  pipeline when any **Blocking**-category fighter is present and
  unacknowledged. Fighter Status itself remains a read/write
  workbench layer; the *block* is the gate's contribution.
- **No two-way dependency.** Manual Review reads Fighter Status;
  Fighter Status does not read Manual Review. The dependency is
  one-directional so the Fighter Status page can ship before Manual
  Review and remain useful.

v1 implements **none** of the above. The expected integration is
documented here so the eventual Manual Review design pass has a
stable target, not so it can begin now.

## 10. Effect on future optimizer

The optimizer is a skeleton (`src/optimizer/validation.py` contains the
same-fight-pair validator; lineup generation is not in v0). When
optimizer construction lands, Fighter Status v1's expected role is:

- **Blocking → exclude from the candidate pool.** Optimizer input
  must filter out every fighter whose Fighter Status falls in the
  **Blocking** category (§5). This is a hard exclude, not a heavy
  penalty.
- **Warning → include but surface.** Warning-category fighters
  remain selectable. The optimizer does not down-weight them in v1
  (no penalty term, no projection adjustment); v1 only ensures they
  appear in any "why was this lineup chosen?" trace.
- **No cross-status coupling with `effective_status`.** Per §8, the
  optimizer's eligibility query must read Fighter Status and
  `effective_status` independently. A future unified resolver is
  out-of-scope here.
- **No optimizer wiring in v1.** Per `docs/DEVELOPMENT_NOTES.md` §10, optimizer
  implementation is not on deck.

## 11. Effect on future export / run log

The Export / Run Log page is a v0 placeholder
(`app/pages/08_export_run_log.py`). When it lands, Fighter Status v1's
expected role is:

- **Blocking → never emitted.** Export must refuse to write a DK CSV
  row for any fighter whose status falls in the **Blocking** category
  (§5), even if the optimizer somehow selected them (defence in
  depth).
- **Warning → emitted with a run-log annotation.** A warning-category
  fighter in an exported lineup must be recorded in the run log
  alongside their status value, so a post-event review can see *"we
  shipped a lineup with a `questionable` fighter."*
- **Manual Review gate runs first.** Per §9, the gate prevents an
  unacknowledged blocker from ever reaching export. The export-side
  refusal is a belt-and-braces last check.
- **No export wiring in v1.** Per `docs/DEVELOPMENT_NOTES.md` §3 and §10, export is
  not on deck.

## 12. Repository / service concept

Module layout (future, not implemented in this slice):

- `src/slate/fighter_status.py` — extend the existing v0 stub. The
  module becomes the single source of truth for:
  - Status value constants (`ACTIVE`, `NEEDS_REVIEW`, `QUESTIONABLE`,
    `OUT`, `WITHDRAWN`, `REPLACED`, `INACTIVE`, `MISSED_WEIGHT`,
    `SHORT_NOTICE`, `DUPLICATE_OR_BAD_ROW`).
  - The `STATUS_CATEGORY` mapping (§5) — `{value: "active" |
    "warning" | "blocking"}`.
  - Pure predicate helpers (`is_blocking(status) -> bool`,
    `is_warning(status) -> bool`, `is_active(status) -> bool`).
  - The `ALLOWED_STATUSES` frozen set used by the repository layer
    to validate writes.
- `src/db/repositories.py` — extend `FighterRepository` (or add a
  sibling `FighterStatusRepository`, depending on the persistence
  decision in §13) with:
  - `list_with_status(slate_id) -> list[FighterStatusRow]` — read
    side. Returns one row per active-on-slate fighter with the
    effective Fighter Status value (importer base + manual override
    if persisted separately).
  - `set_fighter_status(slate_id, fighter_id, status, *, source) ->
    FighterStatusRecord` — write side. Validates the value against
    `ALLOWED_STATUSES`, runs inside one DB transaction
    (`with self.conn:`), and is idempotent on re-write of the same
    value.
- `src/slate/slate_service.py` (or a new
  `src/slate/fighter_status_service.py`) — composition layer that the
  Streamlit page calls. Owns transaction boundaries for the UI write
  (per `docs/DEVELOPMENT_NOTES.md` §11.1).

Hard contracts:

- The repository layer is the **only** path that writes Fighter
  Status. The Streamlit page must not execute SQL directly
  (`docs/DEVELOPMENT_NOTES.md` §11).
- All status writes are idempotent: writing `out` over `out` for the
  same `(slate_id, fighter_id)` is a no-op for downstream consumers
  (`docs/DEVELOPMENT_NOTES.md` §11.1.3, mirroring D.4.2 idempotence).
- The repository layer **does not** read or write
  `odds_match_results` / `manual_match_overrides` (§8). A future
  unified resolver is out-of-scope.
- The service layer is **pure read** when called by Projection v1 /
  Mismatch Alerts v1 / Manual Review (in their future integration
  slices). Only the Fighter Status page itself writes.

## 13. Schema / persistence design options

Fighter Status v1 must choose between three persistence shapes. The
shape determines whether a schema change is required. All three
options are presented; **option B is the recommendation** and is
called out below.

### 13.1 Option A — expand the existing `fighters.status` column

- Use the existing `fighters.status TEXT NOT NULL DEFAULT 'active'`
  column to hold any value from §4.
- No new table, no new column, no migration.

Pros:

- Zero schema change. Smallest possible diff.
- One column to read; no join.

Cons:

- **Importer collision.** `FighterRepository.upsert_for_slate` already
  writes the column on every re-import (`active` for present rows,
  `inactive` for absent rows). A user-set `out` would be silently
  overwritten to `active` the next time the salary CSV is re-imported.
  Fixing this requires the importer to learn the difference between
  "importer-owned base" and "user-owned override," which is exactly
  the separation Option B / C introduce explicitly.
- **No audit / provenance.** There is no way to tell *who* wrote a
  given value (importer vs. user) or *when* the user wrote it.
- **No history.** A user who changes `questionable` → `out` →
  `active` leaves no trace.

Verdict: **rejected for v1.** The importer-overwrite hazard alone is
enough to disqualify A. Documenting it here so future contributors
do not reach for it as "the cheap option."

### 13.2 Option B — new `manual_status` column on `fighters` (recommended)

- Add `manual_status TEXT NULL` and `manual_status_set_at TEXT NULL`
  to `fighters`. `NULL` means "no user override" — the effective
  status is the existing `status` column (importer-owned).
- The effective Fighter Status is a *resolver*:
  `manual_status if manual_status is not None else status`. Compute
  in the repository layer / a pure helper in
  `src/slate/fighter_status.py`; **do not** add a generated column.
- Importer remains untouched. It still writes `status='active'` /
  `status='inactive'` and never touches `manual_status`.
- A user "clearing" their override re-sets `manual_status = NULL` (no
  delete needed); the effective value reverts to the importer-owned
  base.

Schema change required: **yes** — one migration in
`src/db/migrations.py`, paired with a schema test
(`tests/test_odds_persistence_schema.py` or a sibling) per
`docs/DEVELOPMENT_NOTES.md` §8.

Pros:

- Importer logic is untouched. The existing
  `SALARY_PERSISTENCE_DESIGN.md` §5 contract holds verbatim.
- The "importer base vs. user override" split is explicit and
  testable.
- Single-row read; no join; the projection input service's existing
  `WHERE status = 'active'` filter can be one-line-extended to read
  the effective status without changing query shape.
- Reverting a mistake is a single UPDATE setting `manual_status =
  NULL` — no row delete, no audit confusion.

Cons:

- Still no history. A user oscillating `questionable` ↔ `active`
  leaves only the final value. Mitigation: §17.3 open question on
  whether a per-slate audit table is justified; v1 does not ship one.
- Two columns to write for the timestamp (`manual_status`,
  `manual_status_set_at`). Acceptable cost.

Verdict: **recommended for v1.** Option B is the minimum-overhead
shape that respects the importer-base / user-override split,
matches the conservative patterns already in use elsewhere in the
repo (the existing `status` column already carries an
importer-owned semantic), and keeps every downstream read to a
single `fighters` row.

### 13.3 Option C — dedicated `fighter_status_overrides` table

- Add a new table:
  ```
  CREATE TABLE fighter_status_overrides (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
      fighter_id INTEGER NOT NULL REFERENCES fighters(id) ON DELETE CASCADE,
      status TEXT NOT NULL,
      source TEXT NOT NULL DEFAULT 'user',
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      superseded_at TEXT NULL
  );
  CREATE INDEX idx_fighter_status_overrides_slate_fighter
      ON fighter_status_overrides (slate_id, fighter_id)
      WHERE superseded_at IS NULL;
  ```
- The effective Fighter Status is a resolver over the latest
  non-superseded override per `(slate_id, fighter_id)`, falling back
  to `fighters.status` when no override exists. Mirrors the
  `manual_match_overrides` shape in
  `ODDS_PERSISTENCE_DESIGN.md` §5.3.

Schema change required: **yes** — one migration plus a paired schema
test.

Pros:

- Full history per fighter per slate. Every status transition is
  preserved.
- Provenance via the `source` column (room for `'user'`, `'system'`,
  future `'review_ack'`).
- Consistent shape with `manual_match_overrides`, which the repo
  already understands.

Cons:

- Higher implementation cost. Every read path joins or runs a
  correlated subquery to resolve the effective status.
- History introduces "which row is current?" semantics that the
  Fighter Status page does not actually need in v1 — there is no
  pending review of override transitions.
- Risks looking like an audit table without one being justified. The
  same shape exists for `manual_match_overrides` because overrides
  there can be *contested* (different override types compete via the
  §15.9 resolver). Fighter Status has no such competition.

Verdict: **deferred to a future design pass.** If a real review
workflow ever needs history (e.g. "show me every status change in
the last hour"), promoting from B to C is additive (the column on
`fighters` can stay as a denormalised "latest" cache, or be
dropped) and lossless. Reaching for C in v1 is over-design.

### 13.4 Recommendation summary

**Option B is recommended.** It requires a small schema change
(one new column + one timestamp column on `fighters`, one
migration, one schema test) and resolves the importer-overwrite
problem at minimum cost. Option A is rejected. Option C is
deferred behind a real history-requiring workflow.

## 14. UI concept for `app/pages/04_fighter_status.py`

The page replaces the current v0 placeholder (which renders only a
title and a "Locked in v0" warning). It is the **only** UI surface
that writes Fighter Status in v1. Per `docs/DEVELOPMENT_NOTES.md` §11, every write
action runs inside a single DB transaction owned by the page handler
and is covered by an AppTest.

Page elements (future implementation):

1. **Slate selector** — `st.selectbox` over `SlateRepository.list_all`,
   mirroring `app/pages/09_projections.py` and `app/pages/05_alerts.py`.
   Same `format_func` shape (`#<id> — <event_name> (<event_date>)`).
2. **Top-of-page warning banner** — pinned text covering:
   - "Status writes are local. They do NOT call any external service."
   - "Fighter Status does NOT yet feed projections, alerts, the manual
     review gate, the optimizer, or exports. Those integrations land in
     separate design passes."
   - "Status is independent of odds-match `effective_status`. Marking a
     fighter `out` does not reject their odds row, and vice versa."
3. **Summary line** — counts per Fighter Status category (§5):
   `N active · M warning · K blocking`. Pinnable by AppTest.
4. **Fighter table** — one row per active-on-slate fighter (i.e.
   `fighters.status != 'inactive'` OR `manual_status IS NOT NULL`
   so importer-deactivated rows the user has manually re-marked are
   still visible). Columns:
   - `Fighter`
   - `Salary`
   - `Importer status` (the existing `fighters.status` value:
     `active` / `inactive`)
   - `Manual status` (the override value, or `—` when `NULL`)
   - `Effective status` (the resolved value used by future
     downstream consumers — §13.2 resolver output)
   - `Category` (`active` / `warning` / `blocking`)
   - `Set status` — a per-row `st.selectbox` populated from the §4
     value set plus a `"Clear override"` option that writes
     `manual_status = NULL`.
   - `Set` — a per-row `st.form_submit_button` (inside an
     `st.form` so changes batch per row click) that writes the
     selected status via the repository layer.
5. **Empty state** — when no fighters exist for the slate, a single
   info line ("No fighters on this slate yet. Import salaries on
   Slate Setup first.") with the banner above it. AppTest pins this
   text.

Write-action contract (`docs/DEVELOPMENT_NOTES.md` §11):

- One transaction per row submit, owned by the page handler.
- AppTest covers: selecting a value, clicking Set, asserting the
  persisted row reflects the new `manual_status`.
- Idempotence test: submitting the same value twice must produce
  identical persisted state (matches `manual_status` resolver
  behavior).
- "Clear override" test: writing `Clear override` after an `out`
  must set `manual_status` back to `NULL` and the resolved status
  back to the importer value.
- Re-import safety test: setting `manual_status = 'out'`, then
  re-running the salary importer (the importer flips
  `status='active' → 'active'` or no-op), must leave `manual_status`
  unchanged.

What the page must **not** contain in v1:

- No "apply to all" / bulk write controls. v1 writes are per-row only.
- No filter / search controls beyond the slate selector.
- No history view. Per §13.2 v1 does not persist history.
- No links that mutate other tables (no "and also reject odds match,"
  no "and also remove from fight group"). The page writes Fighter
  Status only.
- No automatic acknowledgement, no "I've reviewed this fighter"
  checkbox. Acknowledgement is a future Manual Review concern (§9).
- No charts, plots, or distribution renders.

## 15. Implementation phase split

The slices below are **future work** — this doc creates none of them.
Each slice gets its own design check-in and its own commit per
`docs/DEVELOPMENT_NOTES.md` §13 ("one slice per session") and `AI_BUILD_WORKFLOW.md`
§3–§4. Each slice is sized to fit the §3 limits (≤ ~150 net lines,
≤ ~5 files, one layer per slice).

- **Phase A — Pure status taxonomy + resolver.** Extend
  `src/slate/fighter_status.py` with the §4 value constants, the
  §5 category mapping, the resolver helper
  (`resolve_effective_fighter_status(importer_status, manual_status)`),
  and the `ALLOWED_STATUSES` frozen set. Pure-Python, no DB, no
  Streamlit. Unit tests pin every value in §4 and every category
  membership in §5.
- **Phase B — Schema + migration + repository write helper.** Add the
  `manual_status` / `manual_status_set_at` columns to `fighters`
  (Option B, §13.2). Write the migration in
  `src/db/migrations.py`. Add a schema test under
  `tests/test_odds_persistence_schema.py` (or a sibling). Extend
  `FighterRepository` with `set_fighter_status` only — read still
  comes from `list_for_slate` (with the resolver applied at the
  call site). Unit / repository tests cover write validation,
  idempotence, the `NULL`-clear path, and re-import safety.
- **Phase C — Read aggregator for the UI.** Add a thin read helper
  (likely `FighterRepository.list_with_status(slate_id)` or a service
  in `src/slate/`) that returns one row per fighter with
  `(importer_status, manual_status, effective_status, category)`
  fields, sorted deterministically (name COLLATE NOCASE ASC, id
  ASC, mirroring `list_for_slate`). Read-only; pure of DB writes.
- **Phase D — Streamlit page wiring.** Replace
  `app/pages/04_fighter_status.py` with the §14 page. AppTest pins
  the banner, the summary line, the empty state, the row table, and
  exercises the write action per `docs/DEVELOPMENT_NOTES.md` §11.
- **Phase E — Real / manual smoke.** A manual checklist run against
  the real DK UFC Classic salary CSV used for the salary importer
  smoke (`SALARY_PERSISTENCE_DESIGN.md` §13) plus a manual sequence
  of status writes that exercises the §16 plan. No new code; this is
  a documented validation step, not a slice.
- **Phase F — Downstream integration (separate, gated).** Promoting
  Fighter Status into Projection v1 (§6), Mismatch Alerts v1
  `late_news_risk` (§7), Manual Review (§9), the optimizer (§10),
  and the export / run log (§11). Each promotion is its **own**
  design pass and its own slice, requires explicit user instruction
  per `docs/DEVELOPMENT_NOTES.md` §10, and lands only after Phases A–E are
  documented complete.

**Phase ordering is strict.** Phase B depends on Phase A's
taxonomy and resolver. Phase C depends on Phase B's persistence.
Phase D depends on Phase C's read aggregator. Phase E depends on
Phase D being merged. Phase F has no v1 dependency and is gated on
its own design.

## 16. Test plan (for the future implementation)

Pure unit tests (Phase A) — no DB, no Streamlit:

- Value constants: every value in §4 is exported as a module
  constant; `ALLOWED_STATUSES` contains exactly the v1 set
  (assertion uses `==`, not subset).
- Category mapping: each value belongs to exactly one category;
  every value in §4 appears in the mapping; no extra keys.
- Resolver: `resolve_effective_fighter_status('active', None) ==
  'active'`; `resolve_effective_fighter_status('inactive', None)
  == 'inactive'`; `resolve_effective_fighter_status('active',
  'out') == 'out'`; `resolve_effective_fighter_status('inactive',
  'active') == 'active'`.
- Predicate helpers: `is_blocking('out')`, `is_warning(
  'questionable')`, `is_active('active')`; negative cases for each.

Schema / migration tests (Phase B):

- Schema test asserts the `manual_status` and
  `manual_status_set_at` columns exist with the correct types and
  nullability after `apply_schema`. Mirrors the existing pattern in
  `tests/test_odds_persistence_schema.py`.
- Migration test asserts that running the new migration on a DB
  created from the pre-migration schema preserves all existing
  `fighters` rows and sets `manual_status` to `NULL`.

Repository tests (Phase B):

- Write validation: an attempt to set a status not in
  `ALLOWED_STATUSES` raises `ValueError` and persists nothing.
- Idempotence: writing `out` over `out` for the same
  `(slate_id, fighter_id)` produces zero net change in row state
  besides timestamp refresh (or, by design choice, also no-ops the
  timestamp — pinned either way by the test).
- `NULL`-clear: writing the special "clear override" sentinel sets
  `manual_status = NULL` and `manual_status_set_at = NULL`.
- Re-import safety: setting `manual_status = 'out'`, then running
  `FighterRepository.upsert_for_slate` with the same parsed rows,
  must leave `manual_status` and `manual_status_set_at` unchanged.
- Resolver via read API: `list_with_status` returns the resolved
  `effective_status` and `category` for every fighter, matching the
  Phase A resolver / category mapping.
- No mutation on read: row counts / timestamps unchanged across a
  `list_with_status` call (mirrors
  `PROJECTION_V1_DESIGN.md` §9 cross-cutting).

UI tests (Phase D) — AppTest:

- Page renders the banner text from §14 item 2 verbatim.
- Page renders the summary line from §14 item 3 with correct counts
  for the fixture slate.
- Page renders one row per fighter in deterministic order.
- Write action: selecting `out` on a row, clicking Set, then
  re-reading the persisted state shows `manual_status = 'out'` and
  the row's `Effective status` cell now reads `out`.
- Idempotence: re-submitting `out` after `out` keeps state
  identical (besides the timestamp policy decided in the
  repository test).
- Clear override: selecting `Clear override` after `out` returns
  the effective status to the importer value and `manual_status`
  to `NULL`.
- Empty state pins the "No fighters on this slate yet." text.
- Per `docs/DEVELOPMENT_NOTES.md` §11: no write action bypasses the repository
  layer (assert via repository spy or by introspection of
  executed SQL in the fixture connection).

Cross-cutting:

- **No effective_status side effects.** A Fighter Status write
  must not change any row in `odds_match_results` or
  `manual_match_overrides` (assert row counts / timestamps
  unchanged). Mirrors §8.
- **No projection / alerts side effects in v1.** A Fighter Status
  write must not change the output of `project_slate(...)` or
  `evaluate_alerts(...)` for the same slate. This is the explicit
  test that v1 does not silently change current behavior.
- **`late_news_risk` stays reserved.** Add (or extend) an
  assertion that, after any sequence of Fighter Status writes,
  `evaluate_alerts(...)` never returns an alert with
  `code == "late_news_risk"`. Pins
  `MISMATCH_ALERTS_V1_DESIGN.md` §15 risk #8 against accidental
  promotion.

## 17. Real-feed / manual smoke plan (Phase E)

The smoke is a documented manual checklist, not a code slice. It
runs after Phase D is merged and reuses the real DK UFC Classic
salary CSV from the salary importer smoke
(`SALARY_PERSISTENCE_DESIGN.md` §13). No CSV contents land in the
design doc, in git, or in any external service
(`docs/DEVELOPMENT_NOTES.md` §7, §13).

Checklist (run locally; document outcome only):

1. **Pre-smoke git safety.** Working tree clean. Salary smoke
   already documented. Confirm `.gitignore` still excludes the
   real CSV under `data/uploads/salaries/*`.
2. **Slate setup.** Run the existing import flow on
   `app/pages/01_slate_setup.py` for the real DK UFC Classic
   salary CSV (already-validated path from the salary smoke). Note
   the slate id.
3. **Initial Fighter Status read.** Open
   `app/pages/04_fighter_status.py`, pick the slate, confirm:
   - One row per imported fighter.
   - Every `Importer status` reads `active`.
   - Every `Manual status` reads `—` (NULL).
   - Every `Effective status` reads `active`.
   - Summary line reads `<N> active · 0 warning · 0 blocking`.
4. **Single-write smoke.** Pick one fighter. Set status to
   `questionable`. Confirm:
   - The row's `Manual status` cell now reads `questionable`.
   - The row's `Effective status` cell now reads `questionable`.
   - The row's `Category` cell now reads `warning`.
   - The summary line counts shift: `N−1 active · 1 warning · 0
     blocking`.
5. **Blocking-category smoke.** Pick a second fighter. Set status
   to `out`. Confirm category shifts and summary line updates.
6. **Idempotence smoke.** Re-submit `out` on the second fighter.
   Confirm no visible change (the persisted state may refresh
   `manual_status_set_at` per the Phase B decision; the user does
   not see it).
7. **Clear-override smoke.** Pick the first fighter (still
   `questionable`). Set status to `Clear override`. Confirm:
   - `Manual status` cell returns to `—`.
   - `Effective status` cell returns to `active`.
   - Summary line returns to one fewer warning.
8. **Re-import safety smoke.** Re-run the salary CSV import for
   the same slate. Confirm:
   - The fighter still marked `out` (step 5) keeps
     `manual_status = 'out'` and the `Effective status` cell
     still reads `out`.
   - No other fighter's `manual_status` changes.
9. **Downstream-no-op smoke.** Open
   `app/pages/09_projections.py` and `app/pages/05_alerts.py` for
   the same slate. Confirm:
   - The projection row for the `out`-marked fighter is **the
     same** as it was before Fighter Status writes (per §6,
     Projection v1 still filters by importer `status` and is
     unaware of `manual_status`).
   - No alert with `code == late_news_risk` appears (per §7).
   - The alerts and projections lists are otherwise unchanged
     vs. the pre-Fighter-Status-write baseline.
10. **Cross-page non-leak smoke.** Open
    `app/pages/03_odds.py`. Confirm that no odds-match row's
    `effective_status` changed as a result of the Fighter Status
    writes (per §8).
11. **Failure / anomaly logging.** Note any divergence from steps
    3–10 in the slice report. Do **not** paste fighter names from
    the real CSV into the report (`docs/DEVELOPMENT_NOTES.md` §7); use row
    indices or counts instead.

Completion criterion: all eleven steps pass and the slice report
documents the smoke outcome. Until then, Fighter Status v1 must
not be described as complete (`docs/DEVELOPMENT_NOTES.md` §8, §14).

## 18. Non-goals

Fighter Status v1 explicitly does **not** ship any of:

- News / Twitter / X / RSS scraping or any external source of
  fighter availability.
- UFCStats / Sherdog scraping of any kind.
- Direct Odds API or any remote HTTP fetch.
- DraftKings login, contest auto-entry, screen automation.
- Auto-inference of status from odds movement, salary change, or
  any other signal.
- Probabilistic / model-based "likely to withdraw" prediction.
- Re-pairing of fight groups when a fighter is marked `replaced`.
- Auto-creation of a replacement fighter row on `replaced`.
- DELETE of `fighters` rows via the Fighter Status page (the
  `duplicate_or_bad_row` status is the alternative;
  `docs/DEVELOPMENT_NOTES.md` §13 confirm-before-destructive applies).
- Per-slate audit / history table in v1 (Option C in §13 is
  deferred).
- Bulk / multi-fighter writes from the Fighter Status page.
- Acknowledgement state ("I have reviewed this fighter"). That is
  a future Manual Review concern (§9).
- Integration of Fighter Status into Projection v1 outputs (§6).
- Promotion of `late_news_risk` from reserved to active (§7).
- Wiring into the Manual Review gate, optimizer, or export / run
  log (§9–§11).
- Any read of `odds_match_results.effective_status` or
  `manual_match_overrides` (§8).
- D.5 odds-match override types (Accept, Force Pair, Exclude,
  manual moneyline, low-confidence ack) — `docs/DEVELOPMENT_NOTES.md` §10 keeps
  D.5 paused, and Fighter Status v1 has no dependency on it.
- NFL, Showdown, Pick6, or any non-UFC-Classic format
  (`docs/DEVELOPMENT_NOTES.md` §3).
- Cross-slate aggregation ("which fighters tend to be marked
  `questionable` across recent slates"). v1 evaluates one slate
  at a time.
- User-configurable categories, custom status values, or
  per-slate vocabulary overrides. The §4 set is closed.

Any item above requires a **separate** design doc and explicit
approval before implementation.

## 19. Risks and open questions

1. **`withdrawn` vs. `out` collapse (open).** Both fall into the
   Blocking category and have identical downstream meaning (§5).
   Keeping them split preserves the user's reason-framing; collapsing
   to a single value reduces taxonomy surface. Recommendation: keep
   split in v1, revisit after the Phase E smoke if the distinction
   never surfaces in real review.
2. **Eligibility resolver unification (open).** The optimizer and
   the export gate eventually need a single "is this fighter
   eligible?" function that consults both Fighter Status and
   `odds_match_results.effective_status`. Where that resolver lives
   (a `src/slate/eligibility.py` module? a future
   `src/optimizer/eligibility.py`?) is a Phase F decision, not a v1
   decision. v1 intentionally keeps the two layers disjoint (§8) so
   neither prejudices the resolver's eventual shape.
3. **Audit / history (open).** Option C (§13.3) would persist every
   status transition. v1 picks B (no history) because no concrete
   review workflow demands history yet. Risk: a future workflow
   needs history and we have to migrate. Mitigation: B → C migration
   is additive (add the table, treat the column as denormalised
   latest), so the cost of deferring is low.
4. **Idempotence timestamp policy (open).** When the user re-submits
   the same status value, should `manual_status_set_at` refresh, or
   should the write no-op entirely? Refreshing the timestamp makes
   the "last touched" date useful; no-op preserves strict
   idempotence in row state. Recommendation: refresh the timestamp,
   no-op the value column; pin both behaviours in the Phase B test
   so future readers see the decision.
5. **Re-import behavior with manual `inactive` parallel (open).** The
   importer flips `status` to `inactive` when a fighter is absent
   from a re-import. If a user has separately set
   `manual_status = 'out'` (or any blocking value), the importer's
   flip is silent. Recommendation: do nothing in v1 — both the
   importer base and the user override are blocking-category, so the
   effective state is unambiguous. Note: if the user later clears the
   override (`manual_status = NULL`), the importer's
   `status = 'inactive'` resurfaces — that is correct, but should be
   pinned by a Phase B test so future readers do not "fix" it.
6. **UI noise on large cards (open).** A full UFC PPV may carry 28+
   fighters. A flat table is acceptable for v1 (the slate selector
   gates per-event), but if a user runs multi-day fight weeks
   eventually, sectioning by Fight Group becomes useful. Out of
   scope for v1; revisit if Phase E surfaces ergonomic pain.
7. **Coupling to `PROJECTION_V1_DESIGN.md` §5 reserved tag
   (`"fighter_status"`).** The reserved tag and Fighter Status v1
   are designed to mate cleanly in a later Phase F slice (§6).
   Risk: §5 changes shape (e.g. the tag name changes, the
   `non_projectable` semantics change) without §6 here being
   updated. Mitigation: the Phase F design pass that promotes
   Fighter Status into projections must update both docs in the
   same slice, and the relevant test should fail loudly when either
   side drifts.
8. **Coupling to `MISMATCH_ALERTS_V1_DESIGN.md` §3.9 reserved code
   (`late_news_risk`).** Same posture as risk 7. Mitigation: the
   Phase F design pass that flips §3.9 from reserved to active
   must update both docs and the Phase A test
   (`tests/test_alert_rules.py`) in the same slice. v1's
   cross-cutting test (§16) pins the "never emitted" contract so an
   accidental promotion fails CI.
9. **Manual Review duplication (open).** When Manual Review lands,
   the Fighter Status page and the Manual Review page will both
   surface non-`active` fighters. Risk: the two pages render the
   same information with different framing and drift over time.
   Mitigation: the Manual Review design pass is responsible for
   declaring the single source of truth (Manual Review reads
   Fighter Status), and the duplication is intentional — Fighter
   Status is the *workbench*, Manual Review is the *gate*.
10. **No real-feed signal for the smoke (open).** Phase E (§17) is a
    manual click-through; there is no automated signal that the
    smoke ran. Mitigation: completion is documented in the slice
    report under `docs/DEVELOPMENT_NOTES.md` §12's Reporting Format, matching the
    salary importer's Slice F precedent.
11. **Smoke-CSV residue.** Phase E re-uses the real DK UFC Classic
    salary CSV. Risk: someone runs the smoke and accidentally
    commits the CSV. Mitigation: §17 step 1 calls out the
    `.gitignore` check explicitly, and `docs/DEVELOPMENT_NOTES.md` §7 / §14 already
    forbid the commit.
12. **Schema-change discoverability.** Option B introduces a new
    column; downstream code that reads `fighters` without going
    through the repository may miss the new column. Mitigation:
    Phase B's repository helper is the only path that *needs* the
    column; the resolver is in `src/slate/fighter_status.py` and is
    the single place to read it. Any future direct-SQL read of
    `fighters` outside the repository is an existing
    `docs/DEVELOPMENT_NOTES.md` §11 violation regardless of this design.
