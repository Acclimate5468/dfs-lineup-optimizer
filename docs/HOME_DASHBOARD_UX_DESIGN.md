# Home Dashboard UX Design

> **⚠️ SUPERSEDED.** This guided home-dashboard direction has been superseded
> by the **Build-first / two-step builder** direction. The app now opens
> directly into the Build page as a simple 3-action UFC DK Classic lineup
> tool (import salary CSV ▸ import/check odds + research input ▸ build
> research lineups), and the legacy detail pages are prototype-locked behind
> that surface. This document is retained as **historical design context
> only** and does not describe the current product.

Status: design only. No implementation in this slice (UX A5.1). Companion
to `docs/DEVELOPMENT_NOTES.md` §2 (v0 scope: UFC DK Classic only), §3 (out-of-scope — no
scraping, no API integration, no DK login / auto-entry), §9
(design-before-implementation rules), §11 (UI write-action rules), §13
(session / scope control — one slice per session, no chaining), §14
(do-not quick reference), and the following sibling design docs:

Project state at the time of writing: the salary importer, odds ingestion
+ matching, Fighter Status v1, Manual Review gate, Projection v1, Optimizer
v1, and Export / Run Log v1 are all implemented and an end-to-end lineup
smoke is complete. The Fight Groups UX streamlining direction
(`docs/FIGHT_GROUPS_UX_DESIGN.md`) has shipped A1 (roster/coverage view),
A2 (selectbox add form), A3 (rounds / main-event UX), and A4 (pasted-card
assisted pairing builder; real-card smoked). The current active direction
is the next UX streamlining slice: a guided **home dashboard**. No earlier
checkpoint is paused or pending against this work. Latest pushed commit
`3309a20 Add Fight Groups assisted pairing apply action`.

- `docs/MANUAL_REVIEW_GATE_V1_DESIGN.md` §5 / §8 — the Manual Review Gate
  already owns the closed set of readiness checks and the Phase C read
  aggregator (`evaluate_manual_review`) that resolves them from
  repositories. The dashboard is a **consumer** of that aggregator, not a
  re-implementation of its logic.
- `docs/FIGHT_GROUPS_UX_DESIGN.md` — the UX-streamlining sibling; this doc
  follows the same structure (purpose ▸ problems ▸ layout ▸ scope ▸ data
  sources ▸ non-goals ▸ tests ▸ slices ▸ risks) and the same
  read-only-on-load posture.
- `docs/OPTIMIZER_V1_DESIGN.md` §6 / §9 and
  `docs/EXPORT_RUN_LOG_V1_DESIGN.md` §2 / §7 — the optimizer and export
  pages already gate on `readiness.summary.ready`. The dashboard mirrors
  that gate semantically for its "next action", but never invokes the
  solver or the export builder.
- `docs/ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7 — `effective_status` is
  still inert in downstream consumers. The dashboard inherits that caveat:
  it consumes only what `evaluate_manual_review` already surfaces and adds
  no new `effective_status` read.

---

## 1. Purpose

The home page (`app/streamlit_app.py`) is the first screen the user sees
and currently the least useful. It is a static "v0 skeleton" placeholder:
a title, a sidebar list of pages each annotated "(locked)", and an info
box stating the app is a skeleton with "all pages visible as locked
placeholders". None of that is true any more — every page works, the
optimizer and export build real lineups end to end, and the Fight Groups
page has been substantially streamlined.

The home dashboard redesign has these goals:

- **Make the app feel guided instead of clunky.** A first-time-this-week
  user should land on home and immediately know *where they are in the
  workflow* and *what to do next*, without bouncing between nine pages to
  reconstruct it.
- **Show current slate status at a glance.** One slate-summary card:
  which slate, how many fighters, salary / fight-group / odds / projection
  / manual-review state.
- **Show what is complete and what is incomplete.** A per-page workflow
  checklist with a pass / warn / block / not-started status on each row.
- **Tell the user the single next recommended action.** One panel, one
  recommendation, one page to visit.
- **Avoid duplicating business logic.** The Manual Review Gate's Phase C
  aggregator (`src/slate/manual_review_service.py::evaluate_manual_review`)
  already resolves the entire readiness check set from repositories,
  read-only, in deterministic order. The dashboard renders *that* result;
  it does not re-derive pass/fail decisions.

A5.1 — the slice this doc gates — is **design only**. No `app/`, `src/`,
`tests/`, schema, migration, or repository changes land here.

---

## 2. Current home / page UX problems

1. **The home page is stale placeholder copy.** `app/streamlit_app.py`
   still calls the app a "v0 skeleton", lists every page as "(locked)",
   and claims "Optimizer is a skeleton — not yet implemented". All three
   statements are now false. The home screen actively misinforms.
2. **The user has to bounce between pages to reconstruct workflow state.**
   "Did I import salaries? Are all fighters paired? Did odds match? Is the
   slate marked reviewed?" Each answer lives on a different page. Only the
   Manual Review page aggregates them — and only as a long check table,
   not a workflow map.
3. **There is no global progress overview.** Nothing tells the user "you
   are 4 of 8 steps in".
4. **There is no next-step guidance.** Even the Manual Review page lists
   *what is wrong* but not *which page fixes it next*.
5. **Per-page slate selectors are disconnected.** Every page renders its
   own `st.selectbox` with its own `session_state` key
   (`manual_review_slate_id`, `optimizer_slate_id`, `export_slate_id`,
   `fighter_status_slate_id`, `alerts_slate_id`, `projections_slate_id`,
   Slate Setup's `import_target_slate_id`, Odds' three selectors, and an
   unkeyed selector on Fight Groups). Switching slate on one page does not
   carry to the next, so the user re-selects the slate on every hop.
6. **Important blockers are buried.** The Manual Review page is the only
   place the Blocking / Warning lists surface, and it sits at position 06
   of 09. A new user does not know it is the place to look.

---

## 3. Target dashboard layout

Top to bottom on `app/streamlit_app.py` (replacing the placeholder body):

### 3.1 Title + plain-English purpose

- `st.title("UFC DFS Lineup Optimizer")` (kept).
- One caption explaining what the app is and that home is the workflow
  map: e.g. *"Local UFC DraftKings Classic research workbench. This page
  shows where the selected slate is in the build workflow and what to do
  next. Pick a slate below."*
- The stale "v0 skeleton / locked placeholders" info box and the
  "Optimizer is a skeleton" line are **removed**.

### 3.2 Active slate selector

- A single `st.selectbox` over `SlateRepository.list_all()`, labelled
  consistently with the other pages (`#{id} — {event_name} ({date})`).
- Default index: the current `st.session_state["active_slate_id"]` if it
  still resolves to an existing slate, else the first slate. On change it
  writes `active_slate_id` (see §6).
- Empty-DB branch: no selector; render a single call-to-action ("No slates
  yet — create one on Slate Setup and import a DK UFC Classic salary
  CSV.") and the next-action panel pointing at Slate Setup. The dashboard
  still renders (it does not `st.stop()` before the next-action panel).

### 3.3 Slate summary card

A compact card (a row of `st.metric` / `st.columns` plus short status
lines) for the selected slate:

| Field | Source | Shown as |
| --- | --- | --- |
| Slate name / date / id | `SlateRecord` (`event_name`, `event_date`, `id`) | header line |
| Fighter count | `len(active fighters)` (see §4) | metric chip |
| Salary status | `salary_imported` check status | PASS / BLOCK |
| Fight group status | `fight_group_coverage` (+ `fight_group_review`) check status | PASS / WARN / BLOCK |
| Odds match status | `odds_unmatched_active` (+ `odds_coverage_partial`, `odds_match_review`) | PASS / WARN / BLOCK |
| Projection status | `projection_non_projectable` (+ `projection_missing_inputs`) | PASS / WARN / BLOCK |
| Manual review status | `readiness.manual_review_status` + `manual_review_user_ack` check | reviewed / not reviewed |

Each domain status is derived **only** from the corresponding
`ReviewCheckResult.status` the aggregator already produced (§4). The card
does not recompute any pass/fail rule. Numeric chips (fighter count,
fight-group count) come from cheap `len()` reads, never from parsing check
messages.

### 3.4 Workflow checklist

One row per page, in workflow order:

1. **01 Slate Setup**
2. **02 Fight Groups**
3. **03 Odds**
4. **09 Projections**
5. **05 Alerts**
6. **04 Fighter Status**
7. **06 Manual Review**
8. **07 Optimizer**
9. **08 Export & Run Log**

(Workflow order intentionally differs from the page-number order — 09
Projections and 05 Alerts/04 Fighter Status are consulted *before* Manual
Review. Page *files are not renamed* in this slice; only the dashboard's
display order reflects the real workflow. See §7 non-goals.)

Each checklist row shows:

- **Status icon** — one of: ✅ pass · ⚠️ warn · ⛔ block · ◻️ not-started.
- **Short status message** — a one-line human summary (derived from the
  governing check's `message`, truncated, or a fixed per-page string when
  no single check governs the page — e.g. Optimizer/Export).
- **Linked page (or page name)** — `st.page_link("pages/NN_*.py", …)` if
  AppTest-compatible; otherwise the plain page name as text in A5.2 and a
  `page_link` upgrade in A5.4 (see §10 / §9).
- **Why it matters** — a fixed, short rationale (e.g. Fight Groups: *"Every
  active fighter needs an opponent before projections and the optimizer
  can use them."*).

Row → status mapping (the page's status is the most severe governing
check; block beats warn beats pass; not-started when the upstream
prerequisite has not run):

| Checklist row | Governing check(s) | Page link |
| --- | --- | --- |
| 01 Slate Setup | `salary_imported` (Blocking) | `pages/01_slate_setup.py` |
| 02 Fight Groups | `fight_group_coverage` (B), `fight_group_review` (W), `scheduled_rounds_reviewed` (W) | `pages/02_fight_groups.py` |
| 03 Odds | `odds_unmatched_active` (B), `odds_coverage_partial` (W), `odds_match_review` (W) | `pages/03_odds.py` |
| 09 Projections | `projection_non_projectable` (B), `projection_missing_inputs` (W) | `pages/09_projections.py` |
| 05 Alerts | `mismatch_alerts_warn` (W); `mismatch_alerts_info` (info) | `pages/05_alerts.py` |
| 04 Fighter Status | `fighter_status_review` (info — deferred in v1) | `pages/04_fighter_status.py` |
| 06 Manual Review | `late_news_acknowledged` (W), `manual_review_user_ack` (B) | `pages/06_manual_review.py` |
| 07 Optimizer | `readiness.summary.ready` gate (not a check) | `pages/07_optimizer.py` |
| 08 Export & Run Log | `readiness.summary.ready` gate (not a check) | `pages/08_export_run_log.py` |

- **04 Fighter Status** is purely informational in Manual Review v1 (the
  `fighter_status_review` check is locked to `info` per
  `MANUAL_REVIEW_GATE_V1_DESIGN.md` §5.7 / §13). Its checklist row reflects
  that — it never shows block/warn from the gate. It is rendered so the
  user knows the page exists, with a status of ✅/◻️ and the standing
  "not yet integrated into the gate" caveat.
- **07 Optimizer** and **08 Export** have no governing check. Their rows
  read ◻️ *not-started / locked* until `readiness.summary.ready` is True
  (slate structurally clean **and** marked reviewed), then ✅ *unlocked*.
  The dashboard cannot detect whether the user actually ran the optimizer
  or built an export — neither persists state (Optimizer v1 §10 risk #5;
  Export v1 §1.1 Option A) — so these rows report *availability*, not
  *completion*.

### 3.5 Next recommended action panel

A single, prominent panel: one recommendation, the page to visit, and one
sentence of why. It is **advisory only** — it renders text + (optionally) a
`st.page_link`; it never performs a write, never recomputes, never runs the
optimizer or builds an export. See §5 for the precedence logic.

---

## 4. Data sources

The dashboard uses **existing read paths only**, and prefers a single
aggregator call over hand-rolled repository fan-out.

Primary source — one call per render for the selected slate:

```
readiness = evaluate_manual_review(conn, active_slate_id)   # ReviewReadiness
```

`evaluate_manual_review` (`src/slate/manual_review_service.py`) is the
Phase C read aggregator. It is **read-only end to end** (asserted by the
Phase C contract and tests; `MANUAL_REVIEW_GATE_V1_DESIGN.md` §8 / §14) and
internally composes exactly the reads the dashboard needs:

- `SlateRepository.list_all` → locate the slate, read
  `manual_review_status` / `manual_review_completed_at` /
  `salary_csv_status` / `salary_row_count`.
- `FighterRepository.list_for_slate` → active fighters.
- `FightGroupRepository.list_for_slate` → fight groups.
- `OddsMatchResultRepository.list_for_slate` → odds match results.
- `project_slate` → Projection v1 rows.
- `evaluate_alerts` → Mismatch Alerts v1 rows.

It returns `ReviewReadiness` with:

- `.slate_id`, `.manual_review_status`, `.manual_review_completed_at`
- `.checks: tuple[ReviewCheckResult, …]` (already `sort_results`-ordered:
  Blocking, then Warning, then Informational)
- `.summary: ManualReviewSummary` (`blocking_count`, `warning_count`,
  `info_count`, `ready`)

The dashboard indexes `.checks` by `code` and reads each check's
`status` / `message` for the summary card and checklist. It reuses the
shared category constants (`mr.CATEGORY_BLOCKING`, `STATUS_FAIL`, etc.) —
it does **not** re-classify checks.

Secondary source — slate list and small count chips:

- `SlateRepository.list_all()` for the §3.2 selector.
- For the §3.3 fighter-count / fight-group-count chips the dashboard may
  do two cheap direct reads (`FighterRepository.list_for_slate`,
  `FightGroupRepository.list_for_slate`) and take `len(...)`. These are
  `len()` over a list, not a recomputation of any check verdict. This is a
  deliberate, accepted small double-read; see the optional optimization in
  §9 (widen `ReviewReadiness` to carry counts) if even that is later judged
  wasteful.

Cost / safety notes:

- `evaluate_manual_review` calls `project_slate` directly **and**
  `evaluate_alerts` (which calls `project_slate` again internally) — i.e.
  two projection passes per render. For a single ~26-fighter UFC slate this
  is cheap pure-Python/pandas. The dashboard runs it for the **selected
  slate only** — never in a loop over all slates.
- It does **not** call `run_optimizer` (PuLP solve) or `build_run_log`
  (export). Those remain button-only on their own pages. Reusing the
  aggregator is therefore the cheapest correct path and structurally rules
  out the "expensive run on page load" risk (§10).

Read-only end to end: page load, slate switch, and the active-slate
selector are all reads. No INSERT / UPDATE / DELETE on any table
(`docs/DEVELOPMENT_NOTES.md` §11).

---

## 5. Next-action logic

The recommendation is a pure mapping from `ReviewReadiness` → a single
`NextAction` (label, page path, page label, one-sentence why). To keep it
unit-testable (mirroring how `manual_review.py` is pure and unit-tested),
the mapping is a pure function, e.g.
`recommend_next_action(readiness) -> NextAction`, placed in a small
`src/slate/` module (proposed `src/slate/home_dashboard.py`) and rendered
by the home page. It contains **no repository access and no check
re-derivation** — it only reads the `ReviewReadiness` it is handed.

Two derived predicates (both expressed against the existing check set, not
re-implemented):

- `gate_ready = readiness.summary.ready` — every Blocking check passes
  (including `manual_review_user_ack`). This is exactly what the Optimizer
  and Export pages gate on.
- `structural_blocking_ok` = no Blocking check is failing **except**
  `manual_review_user_ack`. This mirrors the Manual Review page's
  `ready_to_mark` predicate (`06_manual_review.py`), i.e. "the slate is
  structurally clean and only the explicit review-acknowledgement is
  outstanding".

Precedence (first match wins):

1. **No slates exist** → **01 Slate Setup** — *"Create a slate and import a
   DK UFC Classic salary CSV."*
2. Otherwise, evaluate `readiness` for the active slate, then:
3. **`gate_ready` is True** → **07 Optimizer** — *"Slate is reviewed and
   ready — generate research lineups."* (08 Export is named as the
   following step in the panel's secondary line; the dashboard cannot
   detect whether the optimizer was actually run, so it does not branch on
   it.)
4. **`structural_blocking_ok` and not `gate_ready`** (only
   `manual_review_user_ack` is failing) → **06 Manual Review** — *"All
   structural checks pass — review the checklist and Mark Slate Manually
   Reviewed."*
5. **Otherwise** → the **first failing Blocking check in rank order**
   (`mr.sort_results` order is already applied to `.checks`) maps to its
   page:

| First failing Blocking check | Next action page | Why |
| --- | --- | --- |
| `salary_imported` | 01 Slate Setup | No salaries imported yet |
| `fight_group_coverage` | 02 Fight Groups | Active fighters are unpaired |
| `odds_unmatched_active` | 03 Odds | Majority of fighters have no matched odds |
| `projection_non_projectable` | 09 Projections | Fighters are non-projectable (structural cause upstream) |

Notes:

- Warnings never *block* the recommendation. When all Blocking checks pass
  but Warnings remain, the slate is still routed to Manual Review (step 4),
  where the user reviews the Warning list and acknowledges. The dashboard
  surfaces the Warning count in the summary line so the user knows to look
  (e.g. *"3 warning(s) to review on the way"*), and the relevant checklist
  rows (Odds / Projections / Alerts / Fight Groups) carry the ⚠️ badge so
  the user can jump straight there if they prefer. Routing Warnings to
  "Alerts / Fighter Status as appropriate" is therefore handled by the
  checklist badges, not by overriding the single recommendation.
- `projection_non_projectable` is structural — its true cause is usually a
  missing fight group / opponent / fighter status. The next-action points
  at **09 Projections** as the *inspection* surface (it lists exactly which
  fighters and why), and the why-line names the upstream cause. This keeps
  the recommendation deterministic without the dashboard re-deriving the
  root cause.
- The recommendation is **advisory**. The panel never disables anything,
  never writes, and never runs a downstream action.

---

## 6. Global active slate

Today every page owns an independent slate selector (§2 problem 5).
Streamlit `st.session_state` is shared across pages within a session, so a
single shared key is the natural fix.

Design:

- Introduce `st.session_state["active_slate_id"]`.
- The home dashboard's selector (§3.2) is the canonical writer: selecting a
  slate on home sets `active_slate_id`. The home dashboard reads
  `active_slate_id` to choose its default slate (falling back to the first
  slate when unset or stale — e.g. the slate was deleted).
- **A5.2 ships home-only.** The existing per-page selectors are **not
  touched** in the dashboard slice. This is the lowest-risk path: home
  becomes the workflow map and writes the shared key, but no page changes
  its behaviour, so no existing AppTest can regress.
- **A5.3 (optional, separate slice) lets pages opt in gradually.** A page
  opts in by defaulting its own selector's index to `active_slate_id` when
  that id is present in its options (and writing it back on change). Each
  opt-in is one page, one commit, one AppTest update — pages migrate one at
  a time, never in a big-bang. A page that has not opted in keeps its
  current independent selector. No page is forced.

This keeps the slate selectors disconnected exactly as today after A5.2,
and converges them only as deliberately-tested opt-ins.

---

## 7. Non-goals

- **No schema changes.** No new columns, tables, or migrations.
- **No persistence of dashboard state.** The dashboard renders from live
  reads each load; nothing about "where you are in the workflow" is
  persisted. `active_slate_id` lives in session state only (not the DB).
- **No page-load writes.** Read-only end to end (`docs/DEVELOPMENT_NOTES.md` §11).
- **No automatic recompute.** The dashboard never triggers odds recompute;
  that stays button-only on the Odds page.
- **No optimizer run** on home load (no `run_optimizer`).
- **No export build** on home load (no `build_run_log`).
- **No odds source redesign**, no new ingestion, no API/scraping.
- **No page reordering / renaming in this slice.** The checklist shows a
  workflow order that differs from the `NN_` filename order, but the page
  files keep their numbers. Renaming pages is a separate optional slice
  (A5.5) with its own review.
- **No new write actions.** The dashboard adds zero buttons that mutate
  state; its only interactive control is the read-only slate selector and
  (optionally) navigation links.
- **No promotion of `effective_status` or Fighter Status into the gate.**
  The dashboard renders the gate as-is; it does not advance the deferred
  integrations (`MANUAL_REVIEW_GATE_V1_DESIGN.md` §13 / §14).

---

## 8. Test plan

AppTest coverage mirrors the existing page tests
(`tests/test_manual_review_page.py`, `tests/test_optimizer_page.py`):
`AppTest.from_file("app/streamlit_app.py")` against an isolated temp
SQLite DB via the `DK_LAB_DB_PATH` + `monkeypatch` `DB_PATH` fixture, with
`apply_schema` / `bootstrap_database` parity. The pure `recommend_next_action`
mapping (§5) gets direct unit tests with hand-built `ReviewReadiness` /
`ReviewCheckResult` fixtures (no DB), the way `manual_review.py` is
unit-tested.

Page (AppTest) tests:

1. **Empty DB** — home loads without error; shows the "no slates yet"
   call-to-action; no slate selector; next-action panel points at **Slate
   Setup**.
2. **Slates exist** — the active-slate selector renders with the seeded
   slates.
3. **Stale placeholder removed** — assert the old strings are **absent**:
   `"v0 skeleton"`, `"locked placeholders"`, `"(locked)"`, `"Optimizer is
   a skeleton"`.
4. **Dashboard renders checklist rows** — the nine workflow rows render for
   a seeded slate (assert the page-name labels are present).
5. **Summary card renders** — slate id/name, fighter count, and the
   domain status chips render for a seeded slate.
6. **Next action = Slate Setup** when no slate exists / salary not imported
   (slate row with `salary_csv_status` unset, no fighters).
7. **Next action = Fight Groups** when salary is imported but
   fight-group coverage is incomplete (active fighters, no/partial groups).
8. **Next action = Odds** when fighters are paired but odds are
   missing / unmatched above the Blocking threshold.
9. **Next action = Manual Review** when all structural Blocking checks
   pass but `manual_review_user_ack` is still failing (slate not marked
   reviewed).
10. **Next action = Optimizer** when the slate is marked reviewed and
    `readiness.summary.ready` is True.
11. **Page load does not mutate the DB** — snapshot row counts of
    `slates` / `fighters` / `fight_groups` / `odds_rows` /
    `odds_match_results` / `manual_match_overrides` before and after
    `at.run()`; assert unchanged. (Reuse the seed helpers from the existing
    page tests.)
12. **No optimizer / export run on home load** — monkeypatch
    `src.optimizer.optimizer_service.run_optimizer` and
    `src.exports.export_service.build_run_log` to raise if called; assert
    `at.run()` completes without invoking either.

Pure-function (`recommend_next_action`) unit tests:

- No-slate readiness → Slate Setup.
- Each first-failing-Blocking case (`salary_imported`,
  `fight_group_coverage`, `odds_unmatched_active`,
  `projection_non_projectable`) → its mapped page, asserting precedence
  order (e.g. a readiness with both `salary_imported` and
  `fight_group_coverage` failing routes to Slate Setup).
- Structural-clean-but-unacked → Manual Review.
- `summary.ready` True → Optimizer.

Per `docs/DEVELOPMENT_NOTES.md` §8: the new pure module ships with its unit test, and the
page ships with its AppTest, in the same slice. No real-feed smoke is
required for the dashboard itself (it adds no importer/matcher), but it
inherits the standing "salary / odds importers not yet real-file validated"
caveats and must not over-claim them.

---

## 9. Implementation slices

Smallest-shippable order (`docs/DEVELOPMENT_NOTES.md` §13 — one slice per session):

- **A5.2 — Home dashboard read-only v1 + tests.** Replace the
  `app/streamlit_app.py` placeholder body with: active-slate selector,
  slate-summary card, workflow checklist, and next-action panel — all
  driven by `evaluate_manual_review`. Add the pure
  `recommend_next_action` helper (`src/slate/home_dashboard.py`) with unit
  tests, and the home AppTest. Page links rendered as plain page-name text
  (no `st.page_link` yet) to keep AppTest behaviour simple. Sets
  `active_slate_id` in session state (home-only; no other page reads it
  yet). This is the shippable core.
- **A5.3 — (optional) Global `active_slate_id` page opt-in.** One page per
  commit defaults its selector to `active_slate_id`. Each opt-in updates
  that page's AppTest.
- **A5.4 — (optional) Deep-link / copy polish.** Upgrade checklist rows
  and the next-action panel to `st.page_link("pages/NN_*.py", …)` once
  AppTest compatibility is confirmed (fallback to text if not); tighten
  the why-it-matters and status copy.
- **A5.5 — (optional) Page order / filename cleanup.** Renumber/rename
  page files so the on-disk order matches the workflow order shown by the
  dashboard. Higher blast radius (touches every page's AppTest path and the
  Streamlit sidebar) — deferred and reviewed on its own.

Optional later optimization (not required): widen `ReviewReadiness` to
carry the active-fighter and fight-group counts so the dashboard needs zero
direct reads beyond the aggregator. This is a `manual_review_service`
change with its own Phase C test update and is out of scope for A5.2.

---

## 10. Risks

1. **Duplicating Manual Review logic.** *Mitigation:* the dashboard
   consumes `evaluate_manual_review`'s `ReviewCheckResult`s by `code` and
   reuses the shared `mr` category/status constants. The only new logic is
   the thin, pure `recommend_next_action` presentation mapping — itself
   unit-tested and forbidden from re-classifying checks.
2. **Accidentally running expensive work on page load.** *Mitigation:*
   `evaluate_manual_review` is read-only and never calls the solver or the
   export builder; test 12 monkeypatches both to fail if invoked. The
   aggregator runs for the selected slate only, never in a loop over all
   slates.
3. **Stale slate state after edits on other pages.** The dashboard reads
   live on each render, so navigating back to home after an edit reflects
   current state. Risk is limited to the user not realising a re-review is
   needed after a late edit — the summary card's "reviewed" line plus the
   Manual Review page's own re-review warning cover this; the dashboard
   does not weaken it.
4. **Too much visual clutter.** *Mitigation:* one summary card + one
   checklist + one next-action panel; per-domain detail stays on the
   owning pages. Status is conveyed by a single icon + one short line per
   row.
5. **`st.page_link` / AppTest compatibility.** Navigation links may not be
   fully exercisable under `AppTest`. *Mitigation:* A5.2 renders page names
   as plain text and asserts on those strings; `st.page_link` is deferred
   to A5.4 behind a confirmed-compatible check, with a text fallback.
6. **`active_slate_id` drift.** A stored id may reference a deleted slate.
   *Mitigation:* the selector validates `active_slate_id` against the live
   slate list and falls back to the first slate when it no longer resolves.
7. **Workflow order vs. file order confusion.** The dashboard shows a
   workflow order that differs from the `NN_` filenames. *Mitigation:* each
   checklist row is labelled with its real page name/number; renaming is
   explicitly deferred to A5.5 so this slice never moves files.
