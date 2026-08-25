# Two-Step Builder — Production Port Design (B0)

Status: **design only**. No implementation in this slice (B0). This document
plans the port of the north-star prototype
`docs/ui_prototypes/two_step_builder.html` into the real Streamlit app.

Companion to `docs/DEVELOPMENT_NOTES.md` §1–§3 (identity / v0 scope / out-of-scope), §4
(projection formula), §11 (UI write-action rules), §13 (one slice per
session), §15 (token discipline); and to the existing design docs whose
services this port re-presents rather than rewrites:

- `docs/HOME_DASHBOARD_UX_DESIGN.md` — the current home shell
  (`app/streamlit_app.py`) is a read-only view over the same gate this
  builder consumes.
- `docs/MANUAL_REVIEW_GATE_V1_DESIGN.md` §4 / §5 / §6 — the gate
  aggregator (`evaluate_manual_review`) and the only `manual_review_status`
  write.
- `docs/SALARY_PERSISTENCE_DESIGN.md` §9 — salary validate / import service.
- `docs/DK_GAME_INFO_PAIRING_DESIGN.md` §3 / §4 — Game Info suggested
  pairings (read-only) vs. the explicit Apply on the Fight Groups page.
- `docs/ODDS_MATCHING_DESIGN.md`, `docs/ODDS_NEWS_SNAPSHOT_DESIGN.md`,
  `docs/ODDS_NEWS_SNAPSHOT_PERSISTENCE_DESIGN.md` §14 (S4 preview / S5a save).
- `docs/PROJECTION_V1_DESIGN.md` §4 / §5 — projection facts and statuses.
- `docs/OPTIMIZER_V1_DESIGN.md` §4 / §5.3 — gate-enforced solver service.
- `docs/EXPORT_RUN_LOG_V1_DESIGN.md` §2 / §7 — gate-enforced export bundle.

---

## 1. Purpose and product direction

The app today exposes nine sidebar pages (Slate Setup → Fight Groups → Odds
→ Fighter Status → Alerts → Manual Review → Optimizer → Export, plus
Projections and Source Registry) and a `Lineup Command Center` home
(`app/streamlit_app.py`). The workflow is correct but scattered: a new user
must visit ~7 pages in the right order before a lineup appears.

The product direction is to replace the *primary* user experience with a
single streamlined **two-step builder** as the main app shell:

1. **Step 1 — DraftKings salary CSV** (upload → slate).
2. **Step 2 — Odds + news** (import / check).
3. **Build** → lineups + deterministic reasoning.

The nine existing pages are **kept** as advanced / detail pages — they are
the transparency and override surfaces behind the builder. Nothing is
deleted (`docs/DEVELOPMENT_NOTES.md` §3 "do not delete advanced pages" intent; task §10).

### 1.1 The decisive architectural fact

The prototype's three gate states — **Blocked / Warning / Ready** — are not
new logic to build. They are exactly the existing Manual Review Gate v1
readout:

| Prototype state | Production condition (from `ReviewReadiness`) |
| --- | --- |
| **Blocked** | any Blocking check (other than `manual_review_user_ack`) is `fail` |
| **Warning** | structurally clean, but ≥1 Warning check is `fail` |
| **Ready** | `summary.ready is True` (all Blocking pass, including user ack) |

`src.slate.manual_review_service.evaluate_manual_review(conn, slate_id)`
already composes salary / fight-group / odds-match / projection / alert
state into `ReviewReadiness(checks, summary)` read-only end to end, and is
already enforced as defense-in-depth inside `run_optimizer`
(`ManualReviewGateError`) and `build_run_log` (`gate_blocked` bundle).

**Therefore the two-step builder is overwhelmingly a re-presentation of
services that already exist and are tested.** The only genuinely new pure
logic is the deterministic reasoning generator (§8). Everything else is a
thinner, friendlier surface over `evaluate_manual_review`, the salary import
service, the odds save services, `run_optimizer`, and `build_run_log`.

This is the single most important constraint on the port: **the builder must
not re-derive any gate verdict or business rule.** It reads each check's own
`status` / `category` exactly as the Command Center already does
(`app/streamlit_app.py` `_signal_status` / `_gate_pill`), to avoid drift
(`HOME_DASHBOARD_UX_DESIGN.md` §3.3).

---

## 2. Non-claims

The builder is a friendlier shell, not a new capability. It explicitly does
**not**:

- Generate predictions, "locks", or finish/ITD claims (see §8.3).
- Enter DraftKings contests, log in, or automate DK (`docs/DEVELOPMENT_NOTES.md` §3).
- Scrape, fetch, or call any odds/news API (`docs/DEVELOPMENT_NOTES.md` §3; task §10).
- Change the projection formula, optimizer math, or alert thresholds.
- Auto-acknowledge Manual Review (§7.4).
- Persist news flags / props / line movement (those stay preview-only; §6.4).
- Introduce a second write path for any state a detail page already owns
  (`docs/DEVELOPMENT_NOTES.md` §11; §9.3).

---

## 3. Production page placement (task §1)

**Recommendation: the two-step builder becomes the default app shell
(`app/streamlit_app.py`), and the current Lineup Command Center is relocated
to an advanced page.** This matches the product direction ("main app shell")
and Streamlit's convention that `streamlit_app.py` is the landing page.

But the cutover is the *last* step, not the first, so every intermediate
slice keeps the app bootable and the suite green:

- **During B2–B6**, the builder is implemented as a **new page**
  `app/pages/00_build.py`. The `00_` prefix sorts it to the top of the
  sidebar; the existing home and all nine pages are untouched, so
  `tests/test_home_dashboard_page.py` and every other AppTest stay green
  while the builder is assembled and validated.
- **In B7 (cutover)**, the builder body moves into `app/streamlit_app.py`,
  and the current Command Center body moves to a new advanced page
  `app/pages/00_command_center.py` (or `11_…`; numbering finalized in B7).
  `tests/test_home_dashboard_page.py` is re-pointed at the relocated page;
  the builder AppTests become the home-page tests.

Rationale for staging rather than replacing `streamlit_app.py` directly:
you cannot half-replace the home page — a partial edit would break the
landing experience mid-slice. A new page is fully shippable at every step;
the promotion is a single, reviewable cutover (`docs/DEVELOPMENT_NOTES.md` §9.4
smallest-shippable-slice rule).

Existing pages remain accessible via the sidebar (multipage nav) and via
explicit in-builder `st.page_link` "open detail page →" links (§10).

---

## 4. The gate, in production terms

`ReviewReadiness` carries, per slate:

- `checks: tuple[ReviewCheckResult, …]` — each has `code` (e.g.
  `salary_imported`), `category` (`blocking` / `warning` / `informational`),
  `status` (`pass` / `fail` / `info`), `message`, `tags`.
- `summary.ready: bool` — True iff every Blocking check passes (the
  optimizer/export enablement bit).
- `manual_review_status` / `manual_review_completed_at`.

Two enablement predicates the builder needs (both already exist verbatim and
must be reused, not re-derived):

- **`ready_to_mark`** (from `app/pages/06_manual_review.py`): all Blocking
  checks except `manual_review_user_ack` pass. Controls whether the
  "Mark slate reviewed" affordance is offered. This is the prototype's
  "show the review checkbox" condition.
- **`summary.ready`** (from `app/pages/07_optimizer.py` /
  `08_export_run_log.py`): all Blocking including the user ack pass.
  Controls whether **Build** runs. This is the prototype's "Build button
  enabled" condition.

So the prototype's two-stage gate (check the box → then Build) maps exactly:
`ready_to_mark` enables the checkbox; clicking it persists the ack; that
flips `summary.ready` to True; which enables Build.

To keep this DRY and unit-testable, B1/B2 add **one pure presenter**,
`builder_gate_view(readiness) -> BuilderGateView`, that maps a
`ReviewReadiness` to: the verdict (`ready`/`warn`/`block`), an ordered list
of gate "chips" (each a `(status, label)` read straight off a governing
check — the prototype's `✓ Salary · 26 fighters`, `⛔ 2 unmatched`), the
blocking-fail list, and the recommended next page (reusing
`home_dashboard.recommend_next_action`). The builder page renders this; it
never classifies a check itself.

---

## 5. Step 1 — Salary upload / slate setup (task §2)

### 5.1 Reuse of existing validation/import

The prototype's Step 1 card maps directly onto the Slate Setup page
(`app/pages/01_slate_setup.py`) services — **no new salary logic**:

| Need | Existing function |
| --- | --- |
| Parse uploaded CSV | `pandas.read_csv` (page-level, as today) |
| Structural validation | `dk_salary_importer.validate_dk_salary_dataframe` |
| Create the slate | `SlateRepository.create(event_name, event_date, salary_csv_status="validated", salary_row_count=…)` |
| Persist fighters | `dk_salary_import_service.import_dk_salary_dataframe(conn, slate_id, df)` |
| Read back roster | `FighterRepository.list_for_slate` |

The page's existing "Importer is NOT complete — not yet validated against a
real official DK CSV" warning (`docs/DEVELOPMENT_NOTES.md` §8 / §10 Slice F) must be carried
into the builder verbatim until the real-file smoke test is documented.

### 5.2 Slate creation / loading

- **Create**: the builder offers event name + optional date + the upload,
  then a `Create Slate` button → `SlateRepository.create`, then an
  `Import salaries` button → `import_dk_salary_dataframe`. Both remain
  **explicit button writes** (§5.6).
- **Load existing**: a slate selector at the top of the builder, reusing the
  shared `st.session_state["active_slate_id"]` key the Command Center
  currently owns (`HOME_DASHBOARD_UX_DESIGN.md` §6). After cutover (B7) the
  builder becomes the canonical writer of that key. Default = most recent /
  first slate, matching the current home.
- **v0 simplification (recommended):** keep Create + Import as two clicks, as
  Slate Setup does today, rather than fusing them — it preserves the existing
  idempotent re-import semantics and the existing AppTest contract. Fusing
  into one "Set up slate" click is a later enhancement, out of scope for the
  port.

### 5.3 DK Game Info fight-pairing

Game Info pairing stays **suggest-only in the builder**:

- After import, call `fight_grouping.group_fighters_by_game_info(roster)`
  (pure, read-only) to show `suggested_count` pairs, `uncovered_count`,
  incomplete, and anomaly buckets — exactly the readout
  `01_slate_setup.py` already renders post-import.
- The builder does **not** create fight groups. Group creation is the
  explicit Apply on the Fight Card Review (Fight Groups) detail page
  (`DK_GAME_INFO_PAIRING_DESIGN.md` §4). The builder links there.
- Rationale: a single write path. `docs/DEVELOPMENT_NOTES.md` §11 forbids adding a second
  write action that bypasses the page that owns it; Apply has confirm /
  idempotence semantics that belong on Fight Groups. Folding Apply into the
  builder is explicitly deferred.

The prototype's "Fight card auto-detected from the salary Game Info column"
note is honest only as *suggested* pairings; the builder copy must say
"suggested — confirm on Fight Card Review", not "applied".

### 5.4 Scheduled rounds / 5-round main event

Never inferred (`docs/DEVELOPMENT_NOTES.md` §4; `DK_GAME_INFO_PAIRING_DESIGN.md` §3.1). The
gate's `scheduled_rounds_reviewed` Warning check fails until the user
confirms rounds on Fight Groups. The builder:

- surfaces that Warning in the Build gate panel (§7.2), and
- links to Fight Card Review with the copy "set the 5-round main event /
  title bout, then mark fight groups confirmed."

No rounds write happens in the builder.

### 5.5 Step 1 status card (prototype stats)

`Fighters` = active fighter count (`FighterRepository.list_for_slate`,
`status == "active"`). `Fights` = `FightGroupRepository.list_for_slate`
count. `Cap` = `config.constants.SALARY_CAP` (50,000) — a constant, not a
read. These are read-only and recomputed each render.

### 5.6 Explicit writes in Step 1

Exactly two, both button-gated, both through repositories/services:

1. `Create Slate` → `SlateRepository.create`.
2. `Import salaries` → `import_dk_salary_dataframe` (→
   `FighterRepository.upsert_for_slate`, one transaction).

Page load, file upload, and the Game Info readout write **nothing**
(`docs/DEVELOPMENT_NOTES.md` §11; §11.3 test).

---

## 6. Step 2 — Odds + news (task §3)

The prototype's Step 2 card maps onto the Odds page
(`app/pages/03_odds.py`) zones. The builder surfaces the *common path*
(snapshot or manual/CSV → save → recompute) and links to the full Odds page
for review-by-exception and overrides.

### 6.1 Odds CSV / manual flow

Reuse the Odds page's Zone 1 (validate/preview, no writes) + Zone 2
(explicit save) services unchanged:

- CSV: `odds_csv_importer` (validate) → `odds_csv_save` (Zone 2 write).
- Manual: `manual_odds` (session preview) → `manual_odds` save (Zone 2 write).
- No-vig and matching previews are session-only and read-only.

### 6.2 S4 snapshot preview

Upload snapshot JSON → `collection.odds_news_snapshot.validate_snapshot_text`
→ `SnapshotValidationReport`. The report is preview-only; the builder shows
`summary.ok_entries / total_entries`, errors/warnings, and the app-derived
implied probabilities, exactly as Odds zone 1e does. Parsing follows no
URLs and writes nothing.

### 6.3 S5a save snapshot odds to slate

When the report has no hard errors and ≥1 moneyline entry, the builder shows
an explicit **Save snapshot odds to slate** button →
`snapshot_odds_save.save_snapshot_odds_to_slate(conn, slate_id, report)`.
This is the append-only, moneylines-only save validated by the S5a temp-DB
smoke. The builder must surface its `SnapshotOddsSaveResult` faithfully:
`saved` / `already_existed` / `skipped`, the chained `recompute` summary, and
crucially the **single-snapshot-per-slate guard** (`blocked=True` +
`blocked_reason`) so the user learns nothing was written when a *different*
snapshot already exists (S5b replacement is deferred).

### 6.4 Match / review status

The prototype's "Matched 26/26" maps to the gate's odds coverage:
`auto_matched_count / active_fighter_count` (the same numbers
`odds_unmatched_active` / `odds_coverage_stat` already compute). Review
state (review_required / review_rejected) maps to `odds_match_review`. The
builder shows these read straight from `ReviewReadiness` checks; the full
review-by-exception table and the Reject action stay on the Odds detail page
(they are existing write actions with their own AppTests; not duplicated).

### 6.5 Snapshot warnings / errors and "snapshot age"

- Errors: `SnapshotValidationReport.errors` (block the save; surfaced inline).
- Warnings: `report.warnings`, including snapshot/entry staleness (the
  collection layer already computes `age_hours` from `collected_at`).
- "Snapshot age" = now − `envelope.collected_at_dt`, recomputed each render
  (display-only, never persisted).

### 6.6 Preview-only — never persisted (task §3)

`news_flags`, `news_note`, props (`itd_odds` / `decision_odds` /
`goes_distance`), `line_movement`, and full provenance have **no `odds_rows`
column** and are **preview-only** (`ODDS_NEWS_SNAPSHOT_PERSISTENCE_DESIGN.md`
§9–§11). The builder may *display* a news-flag count and movement in the
preview, clearly labelled "preview — not saved", but must not persist them
and must not let the reasoning generator cite them as stored facts (§8.3).
The prototype's "News flags: 1" stat is such a preview count.

### 6.7 No scraping / API / fetching

The builder ingests only user-supplied CSV / manual entry / snapshot JSON
files, exactly as the Odds page does. No network calls (`docs/DEVELOPMENT_NOTES.md` §3).

### 6.8 Source-registry array upload — accept + summarize (read-only)

Step 2's snapshot uploader (`_render_snapshot_save`) accepts only the S5a
*object* envelope for saving (§6.2 / §6.3). A bare top-level JSON **array** is
not that envelope; `_diagnose_list_snapshot` classifies it (pure / view-only)
into three shapes, two of which are rejected and one of which is now accepted
as a read-only preview:

1. **Source-registry array** — `_looks_like_source_manifest`: rows of `name` +
   `url` + `category` / `type` and no `moneyline` (e.g.
   `data/uploads/sources/UFC_DATA.json`). **Accepted as a registry preview.**
   The builder parses it with
   `src.collection.source_manifest.parse_source_manifest_text` (pure stdlib —
   no network, no file write, no DB connection) and renders a read-only
   summary: valid / total source count and counts by category / type /
   frequency, plus any parse warnings / errors, reusing the
   `SourceManifestResult` already rendered on the **10 Source Registry** page.
   It writes nothing and follows no URLs. It does **not** fetch, scrape, or
   call any odds source — pulling public moneylines from registry sources
   stays out of scope (`docs/DEVELOPMENT_NOTES.md` §3; §6.7;
   `FIGHT_WEEK_SOURCE_COLLECTOR_DESIGN.md` §2.2 / §6 / §11, where every fetcher
   is a separately-designed, instruction-gated future slice). The preview
   points the user to the 10 Source Registry page for the full table, and to
   §6.2 / §6.3 (snapshot) or the 03 Odds CSV / manual flow for actually
   getting moneylines into the slate.

2. **Bare odds-entries array** — `_looks_like_odds_entries`: a `moneyline` plus
   a fighter name, but no envelope. Still **rejected** with the precise "wrap
   your rows under an `entries` key" message: the right data, the wrong shape;
   nothing is saved.

3. **Any other top-level array.** Still **rejected** with the precise "Step 2
   needs a JSON object" message.

The S5a object path (validate → save → recompute) and its guards (§6.3) are
unchanged; the registry preview is the only new branch and is purely
presentational — it derives no rule and persists nothing (§1.1). Why accept
rather than reject the registry array: an operator assembling a slate often has
`UFC_DATA.json` to hand and may drop it into Step 2; summarizing what sources
they hold (and by category) is more useful than a flat rejection, and it stays
inside §6.7's no-network contract. The registry is still *managed* on page 10;
Step 2 only previews it.

---

## 7. Build section (task §4)

The prototype folds the Manual Review gate into the Build bar. In production
this is a re-presentation of `evaluate_manual_review` + the Manual Review
page's mark-reviewed write + the gated `run_optimizer` / `build_run_log`.

### 7.1 What blocks Build

`Build` (the optimizer/export run) is enabled **only when
`readiness.summary.ready is True`** — identical to the Optimizer and Export
pages. Until then the button is disabled. The blocking causes are the
Blocking-category checks: salary not imported, unpaired active fighters,
majority-unmatched odds, non-projectable fighters, and the reviewer ack
itself.

### 7.2 Blocked / Warning / Ready states

Driven entirely by `builder_gate_view(readiness)` (§4):

- **Blocked** (`block`): structural Blocking fail present. No mark-reviewed
  affordance is shown; the panel lists the failing checks (each check's own
  `message`) and links to the page that fixes the first one (via
  `recommend_next_action`). Build disabled.
- **Warning** (`warn`): structurally clean (`ready_to_mark` True) but Warning
  checks fail. The mark-reviewed control is shown; warnings are listed and
  require **explicit acknowledgement** (§7.3). Build disabled until marked.
- **Ready** (`ready`): `summary.ready` True. Build enabled.

### 7.3 Warnings require explicit review acknowledgement

Warnings never auto-clear and never silently allow Build. They are cleared
only by the user consciously clicking **Mark slate reviewed** while the
warnings are visible — exactly the Manual Review page's contract
(`MANUAL_REVIEW_GATE_V1_DESIGN.md` §4 / §6: Warning failures do not disable
the mark button, but the user must act). The session-only late-news /
weigh-in checkbox (`manual_review_late_news_ack`, not persisted in v1) is
carried over unchanged.

### 7.4 "Mark slate reviewed" stays explicit and never automatic

The single write is `SlateRepository.set_manual_review_reviewed(slate_id)` —
the same call `06_manual_review.py` makes, gated on `ready_to_mark`. The
builder must:

- never call it on page load or as a side effect of Build;
- keep it a discrete, user-clicked control;
- carry the standing warning that marking reviewed does **not** invalidate
  on later data changes — re-review after any salary re-import, odds save,
  recompute, override, or fight-group edit.

### 7.5 Optimizer / export service gates remain defense-in-depth

Even though the builder disables Build until `summary.ready`, the click
handler still wraps `run_optimizer` in `try/except ManualReviewGateError`
and treats a `build_run_log` `gate_blocked` bundle as a hard stop —
identical to the Optimizer/Export pages ("UI is the primary UX, the service
is the safety net", `OPTIMIZER_V1_DESIGN.md` §4). The builder must not call
the solver or pool builder directly; it goes through the gated services.

### 7.6 How Build runs projections / optimizer / export preview safely

On an explicit Build click (read-only end to end):

1. `run_optimizer(conn, slate_id, n_lineups)` → `SolveResult` (in-memory
   lineups) for the on-screen "Lineups" section. This internally re-reads the
   gate, builds the pool (projection_status == "ok" only), and solves.
2. For the reasoning + downloads section (§8, B6), `build_run_log(conn,
   slate_id, n_lineups)` → `InternalExportBundle` (lineups + diagnostics +
   per-fighter projection + manual-review snapshot). The four
   `format_*` byte payloads feed `st.download_button` only — no file writes.

Both paths re-read `project_slate` internally; the builder never recomputes
projections or mutates anything.

### 7.7 Read-only vs. writes in Build

- **Writes:** only `set_manual_review_reviewed` (§7.4).
- **Read-only:** `evaluate_manual_review`, `run_optimizer`, `build_run_log`,
  `project_slate`, the reasoning generator, and every download. No INSERT /
  UPDATE / DELETE, no projection recompute, no override mutation, no
  `effective_status` consumption (`ODDS_PERSISTENCE_DESIGN.md` §15.11 #7).

---

## 8. Deterministic reasoning generator (task §5)

This is the only genuinely new pure logic in the port.

### 8.1 Module location and shape

- **Pure module (B1): `src/exports/lineup_reasoning.py`.** No DB, no
  Streamlit, mirroring `src/slate/manual_review.py` and
  `src/slate/home_dashboard.py`. It exposes:
  - `ReasoningContext` — a pure input dataclass (lineups, a per-fighter facts
    map, excluded-fighter list, warning/blocking summaries, salary cap).
  - `ReasoningItem(kind, text, fighter_names)` — one structured line, `kind`
    from a closed set (`anchor`, `value`, `five_round`, `excluded`,
    `constraint`, `total`, `warning`).
  - `build_lineup_reasoning(context) -> tuple[ReasoningItem, …]` —
    deterministic, side-effect-free.
- **Read-only assembler (B6): `assemble_reasoning_context(conn, slate_id,
  bundle)`** in `src/exports/` (e.g. a `reasoning_service.py` or a function
  beside `build_run_log`). It composes `build_run_log`'s
  `InternalExportBundle` with `project_slate` and
  `aggregate_projection_inputs` to populate the per-fighter facts, then calls
  the pure generator. Keeping the DB read here leaves the generator trivially
  unit-testable (the §11 pattern).

Per-fighter facts available (all already computed, no new math):

| Fact | Source |
| --- | --- |
| implied win probability | `aggregate_projection_inputs` → `ProjectionInputs.implied_win_probability` |
| salary | `BundleFighter.dk_salary` / `FighterRepository` |
| projection (DK pts) | `project_slate` → `projected_dk_points` (= p·70 + bonuses) |
| value-gap bonus + tier | pure `value_bonus.value_gap_bonus(salary, p)` |
| five-round bonus | pure `value_bonus.five_round_bonus(scheduled_rounds)` |
| projection status / missing inputs | `project_slate` |
| excluded + reason | `OptimizerPool.excluded` / `ExportDiagnostics.excluded` |
| same-fight pairing | each lineup's distinct `fight_group_id`s |
| totals | `BundleLineup.total_salary` / `total_projection` |
| warnings/blocks | `ReviewReadiness` Warning/Blocking checks |

### 8.2 What it MAY explain (closed list — task §5)

Only facts already persisted or deterministically derivable from the formula
(`docs/DEVELOPMENT_NOTES.md` §4):

- implied probability (e.g. "highest implied win probability in the pool");
- salary and the value-gap bonus tier it triggers (the exact +8/+5/+3 rule);
- five-round bonus (+7 when `scheduled_rounds == 5`);
- the projected DK points and *why the formula produced them* (base + bonuses);
- the no-same-fight-pair constraint and the 6-fighter / ≤ $50k cap facts;
- unmatched-odds / non-projectable / missing-input fighters left out of the
  pool, with the stored reason;
- active Warning / Blocking flags from the gate.

Every claim must be traceable to a value in `ReasoningContext`.

### 8.3 What it must NEVER invent (hard guardrails — task §5)

- **No fight predictions** beyond the stored implied probability. "Higher
  implied probability than X" is allowed (it is arithmetic on stored odds);
  "will win" / "should beat" is not.
- **No "safe lock" / "lock" claims.** Implied probability is a number, not a
  guarantee.
- **No finish / ITD / "likely finish" / round / method claims.** Props
  (`itd_odds`, `decision_odds`, `goes_distance`) are **preview-only and not
  persisted** in v0 (§6.6), so there is *never* persisted prop data to back a
  finish claim in this version. This guardrail is unconditional for v0.
- **No undocumented odds / news.** Only the moneyline-derived implied
  probability is a stored fact; news flags and line movement are preview-only
  and must not be asserted as facts.

**Note on the prototype copy:** the prototype's reasoning lines — e.g.
"safest favorite … anchors Lineup 1 for a likely finish (high ITD odds)" —
are illustrative fake text and **violate** §8.3 (a "safe" claim plus an ITD
finish claim with no persisted prop data). They must **not** be reproduced
verbatim. The production generator states the implied probability and the
formula contribution instead (e.g. "Pavlovich — highest implied win
probability in the pool (X%); anchors Lineup 1 on projection, not a
predicted finish").

### 8.4 Tests (B1)

Pure unit tests over hand-built `ReasoningContext` fixtures (mirroring
`tests/test_manual_review_gate.py` / `test_home_dashboard.py`):

- each fact kind renders the right text from known inputs;
- value-gap tier boundaries (7600/0.45, 8000/0.48, 8500/0.55) render the
  correct tier;
- five-round bonus appears only for `scheduled_rounds == 5`;
- excluded / unmatched / non-projectable fighters are explained, never hidden;
- **banned-phrase test**: assert no output contains "lock", "finish", "KO",
  "ITD", "will win", or "guarantee" for any fixture, including a fixture that
  carries preview-only prop data (it must be ignored);
- determinism: identical input → identical ordered output;
- empty lineups (gate_blocked / infeasible) → a safe diagnostics-only
  explanation, no crash.

---

## 9. Data flow and service map (task §6)

### 9.1 Prototype section → existing service

| Prototype section | Reuses (no new business logic) |
| --- | --- |
| Step 1 card upload/validate | `validate_dk_salary_dataframe` |
| Step 1 Create Slate | `SlateRepository.create` |
| Step 1 Import | `import_dk_salary_dataframe` → `FighterRepository.upsert_for_slate` |
| Step 1 "fight card detected" | `group_fighters_by_game_info` (suggest-only) |
| Step 1 stats | `FighterRepository` / `FightGroupRepository` counts, `SALARY_CAP` |
| Step 2 CSV / manual | `odds_csv_importer` + `odds_csv_save`; `manual_odds` |
| Step 2 snapshot preview | `validate_snapshot_text` → `SnapshotValidationReport` |
| Step 2 Save snapshot | `save_snapshot_odds_to_slate` (→ `recompute_and_replace_match_results`) |
| Step 2 matched / review stats | `evaluate_manual_review` odds checks |
| Build gate (blocked/warn/ready) | `evaluate_manual_review` → `ReviewReadiness` |
| Build gate chips / verdict | **new** pure `builder_gate_view` (reads checks; no re-derivation) |
| "I reviewed this slate" | `SlateRepository.set_manual_review_reviewed` |
| Build → Lineups | `run_optimizer` (gated) |
| Build → downloads | `build_run_log` + `format_lineups_*` |
| Reasoning | **new** pure `build_lineup_reasoning` + read-only `assemble_reasoning_context` |

### 9.2 New presenter / helper functions (the only additions)

1. `src/exports/lineup_reasoning.py` — pure generator (§8). **B1.**
2. `builder_gate_view(readiness) -> BuilderGateView` — pure verdict/chips
   presenter (§4). Location: `src/slate/home_dashboard.py` (extends the
   existing pure presenter that already maps `ReviewReadiness` → display) or
   a sibling `src/slate/builder_view.py`. **B1 or B2.**
3. `assemble_reasoning_context(conn, slate_id, bundle)` — read-only context
   assembler (§8.1). **B6.**

No new repository, no new schema, no new math.

### 9.3 Keeping Streamlit from bypassing repositories

- Every write goes through a repository/service: `SlateRepository.create`,
  `import_dk_salary_dataframe`, `odds_csv_save` / `manual_odds`,
  `save_snapshot_odds_to_slate`, `set_manual_review_reviewed`. No raw SQL in
  the page (`docs/DEVELOPMENT_NOTES.md` §11).
- Each write runs in a single handler-owned transaction (the services/repos
  already own theirs).
- Business logic is not duplicated: Manual Review verdicts, projections,
  optimizer, odds matching, exports, and the Reject/Apply write actions all
  stay in their existing layers; the builder only *reads* and *re-presents*.

---

## 10. UX / page hierarchy (task §7)

**First view:** the two-step builder — slate selector, two input cards
(Salary, Odds), the folded Build gate, and (after Build) Lineups + Reasoning.
A first-time user sees two inputs and one button.

**Advanced / detail pages** stay reachable (sidebar + in-builder
`st.page_link` "open detail page →"). Recommended grouping/labels:

| Builder touchpoint | Detail page |
| --- | --- |
| Step 1 pairings / rounds | Slate Setup; **Fight Card Review** (Fight Groups) |
| Step 2 review-by-exception, overrides | **Odds Review** (Odds); Source Registry |
| Projection detail | Projections |
| Mismatch flags | Alerts |
| Full gate checklist | Manual Review |
| Lineups detail | Optimizer |
| Downloads / run log | Export & Run Log |

**Reducing clutter while preserving transparency:** the builder shows
*summaries* (counts, verdict, the single next action) and a link to the page
that shows the *detail*. Nothing is hidden — every derived number on the
builder has a detail page that explains it, and the builder cites each
gate check's own message rather than inventing a softer summary. The
relocated Command Center (§3) remains available for users who want the full
9-signal dashboard.

`st.page_link` was deferred on the home dashboard (A5.4); the builder is the
right place to introduce it, since navigation *is* the builder's job.

---

## 11. Testing strategy (task §8)

### 11.1 AppTest coverage (per slice)

Using `streamlit.testing.v1.AppTest` against an isolated temp SQLite DB
(the established pattern in `tests/test_optimizer_page.py`,
`test_export_run_log_page.py`, `test_home_dashboard_page.py`):

- **B2 shell:** empty DB → call-to-action, no Build; with slates → cards +
  gate render; the rendered verdict equals `builder_gate_view(readiness)`.
- **B3 Step 1:** Create Slate and Import are explicit writes; Game Info
  readout is suggest-only; the "importer not validated against real CSV"
  warning is present.
- **B4 Step 2:** snapshot preview is read-only; Save snapshot writes and the
  result (saved / existed / **blocked** guard / recompute) is surfaced.
- **B5 Build gate:** blocked → Build disabled, no mark affordance; warning →
  mark shown, Build disabled until marked; ready → Build enabled; a no-op
  Build click on a not-ready slate leaves persisted state untouched
  (defense-in-depth).
- **B6 reasoning:** a known ready slate renders pinned reasoning text and
  passes the banned-phrase assertion.

### 11.2 Unit tests for deterministic reasoning

Pure tests for `build_lineup_reasoning` and `builder_gate_view` over
hand-built fixtures (§8.4). These carry the correctness weight; AppTests just
confirm wiring.

### 11.3 No-page-load-write tests

For the builder page (and after cutover, the home), assert that page render
— and slate-switch — perform **no** INSERT/UPDATE/DELETE (snapshot the DB
before/after), per `docs/DEVELOPMENT_NOTES.md` §11. The only session mutation allowed is
`active_slate_id`.

### 11.4 Gate-state tests

Reuse the `ReviewReadiness` fixture style from `test_manual_review_gate.py` /
`test_home_dashboard.py` to drive `builder_gate_view` through
block/warn/ready and assert verdict, chips, and the next-action target.

### 11.5 Already-covered / out-of-band

- S5a snapshot save is covered by its service-level integration + temp-DB
  smoke; B4 only adds the button + surfaced-result AppTest, not new save
  logic.
- **Visual/prototype check (manual, documented):** diff the rendered builder
  against `two_step_builder.html` for the three gate states, and confirm the
  reasoning copy obeys §8.3 (no "lock"/finish claims). Record the check in
  the slice report; it is not an automated test.
- Real-feed validation: the salary importer is still "tested in isolation,
  not yet validated against a real DK CSV" (`docs/DEVELOPMENT_NOTES.md` §8 / §10 Slice F).
  The builder must not claim otherwise.

---

## 12. Implementation slice plan (task §9)

One slice per session (`docs/DEVELOPMENT_NOTES.md` §13 / §15). Each is independently
shippable and keeps the suite green. Order chosen so the builder lives as a
new page until the final cutover.

| Slice | Scope | Writes? | Tests |
| --- | --- | --- | --- |
| **B1** | Pure `src/exports/lineup_reasoning.py` (generator) **+** pure `builder_gate_view` presenter. No app change. | none | unit only (§8.4, §11.4) |
| **B2** | New page `app/pages/00_build.py`: slate selector + two **read-only** status cards + folded gate panel from `builder_gate_view`. Build button present but disabled (not wired). | none | AppTest §11.1 B2; no-load-write §11.3 |
| **B3** | Wire Step 1: upload/validate/Create Slate/Import + Game Info suggestion readout. | Create, Import | AppTest §11.1 B3 |
| **B4** | Wire Step 2: CSV/manual/snapshot preview + S5a Save + recompute surfacing. | odds saves, recompute | AppTest §11.1 B4 |
| **B5** | Wire Build gate: explicit Mark-reviewed (via repo), Build enablement on `summary.ready`, `run_optimizer` call, defense-in-depth. | mark-reviewed only | AppTest §11.1 B5; gate-state |
| **B6** | Reasoning presentation: `assemble_reasoning_context` (read-only) + render `build_lineup_reasoning`; optional `build_run_log` downloads. | none | AppTest §11.1 B6 |
| **B7** | Cutover: promote builder to `app/streamlit_app.py`; relocate Command Center to a page; sidebar/`page_link` cleanup; re-point home AppTest. | none | re-point + smoke |

Each implementation commit cites the design § it realizes (`docs/DEVELOPMENT_NOTES.md` §9.2).
A real-file smoke of the full Step 1 → Build path is recommended after B6 and
must be documented before the builder is called "complete" (it inherits the
salary importer's pending Slice F smoke).

---

## 13. Risks and open questions

**Risks**

1. **Verdict drift.** If the builder re-derives block/warn/ready instead of
   reading `ReviewReadiness` checks, it can disagree with Manual Review /
   Optimizer / Export. *Mitigation:* the single pure `builder_gate_view`
   presenter; never classify a check in the page (§4).
2. **Banned reasoning claims.** The prototype copy includes finish/lock
   language. *Mitigation:* fact-bounded generator + banned-phrase test (§8).
3. **Preview vs. persisted confusion.** News flags / props / line movement /
   snapshot age are preview-only. *Mitigation:* label them "preview — not
   saved"; the reasoning generator never cites them (§6.6, §8.3).
4. **Page-load writes.** *Mitigation:* read-only render contract + §11.3
   tests; only `active_slate_id` session write.
5. **Cutover breakage (B7).** Moving the home changes the default page.
   *Mitigation:* relocate (don't delete) the Command Center; re-point its
   AppTest; land the cutover as its own reviewed slice.
6. **Second write path.** Folding Fight Groups Apply or odds Reject into the
   builder would duplicate write actions. *Mitigation:* builder links out;
   writes stay on the owning detail page (§5.3, §6.4).

**Open questions (for ChatGPT planning / user)**

- **Q1.** Build → on-screen lineups via `run_optimizer`, *and* downloads via
  `build_run_log`? (Recommended: yes — optimizer for the screen, run-log for
  reasoning context + downloads. Both gated.)
- **Q2.** Final home name / page numbers after B7 cutover (e.g.
  `00_command_center.py` vs `11_command_center.py`).
- **Q3.** Exact home of `builder_gate_view` — extend `home_dashboard.py`
  (reuse its presenters) or new `src/slate/builder_view.py`. (Recommended:
  extend `home_dashboard.py` to avoid a parallel presenter.)
- **Q4.** Keep Create + Import as two clicks (recommended, preserves existing
  semantics) or fuse into one "Set up slate" action (later enhancement)?
- **Q5.** Should the builder offer the Fight Groups Apply inline later? Out of
  scope for the port; would need its own design pass and a single-write-path
  story.

---

## 14. Out of scope (task §10 — explicit)

The port keeps **all** of the following out, with no exceptions:

- Scraping / fetching / any odds or news **API integration**.
- Auto contest entry; DraftKings login automation; screen automation.
- **Schema changes** (no new tables/columns) unless separately approved.
- Projection / optimizer / value-gap / five-round **math changes**
  (`docs/DEVELOPMENT_NOTES.md` §4).
- Ownership projections.
- **Automatic** Manual Review acknowledgement (it stays an explicit user
  write — §7.4).
- News-flags implementation / persistence (S6); props / line-movement
  persistence — all stay preview-only.
- **Deleting** advanced pages — they are relocated/relinked, never removed.
- Promoting `effective_status` into projections / optimizer / alerts /
  exports (`ODDS_PERSISTENCE_DESIGN.md` §15.11 #7 stands).
