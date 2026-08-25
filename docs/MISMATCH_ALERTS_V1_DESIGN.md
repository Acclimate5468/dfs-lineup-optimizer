# Mismatch Value Alerts v1 Design

Status: design only. No implementation in this slice. Companion to
`docs/DEVELOPMENT_NOTES.md` §3 (v0 scope), §4 (projection formula), §10 (current
checkpoint), `docs/PROJECTION_V1_DESIGN.md` (input / output contract),
and `docs/ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7 (`effective_status`
deferral).

---

## 1. Purpose

Mismatch Value Alerts v1 is a **local, read-only, computed-on-read**
layer that surfaces a small, deliberate set of *warning* and *value*
signals over a single slate. Every alert is a function of inputs that
the rest of v0 already persists: salary (salary import), implied win
probability (odds matching), scheduled rounds + opponent (fight groups),
and the per-fighter Projection v1 result.

The intent is twofold and explicitly winning-focused (per
`LEGACY_DFS_PROMPT_AUDIT.md` §2):

- **Help build stronger lineups** by flagging fighters whose
  salary / projection / win-probability relationship deviates from the
  rest of the slate in ways a user might miss while scanning a salary
  CSV by eye (e.g. an underpriced fighter with a real win probability,
  a paid-up favorite without a dominant probability).
- **Help prevent bad lineups** by flagging slate-level *configuration*
  problems (missing odds, missing opponent, no fight group, projection
  unavailable) before they propagate into a future optimizer or export
  path.

Explicit non-claims:

- Mismatch Alerts v1 is **not** a projection model. It consumes
  Projection v1; it does not replace, override, or re-weight it.
- Mismatch Alerts v1 is **not** an optimizer. It produces no lineups,
  no roster suggestions, and no rankings.
- Mismatch Alerts v1 is **not** an ownership model. It never references
  contest exposure, leverage, or field share.
- Mismatch Alerts v1 is **not** a news / late-swap feed. It has no
  network access, no scrape, no API; the late-news category is a
  structural placeholder only (§3.9).
- Mismatch Alerts v1 does **not** persist anything. It is recomputed on
  every page render (§5).

## 2. Inputs available or expected

Local inputs Mismatch Alerts v1 may read (read-only):

- **Per-fighter Projection v1 result** — `FighterSlateProjection` rows
  from `src/projections/slate_projection_service.py::project_slate`.
  This is the canonical source for `projected_dk_points`,
  `projection_status`, `missing_inputs`, `notes`, and the resolved
  `fighter_id` / `fighter_name`.
- **Fighter identity + salary** — already inlined into
  `FighterSlateProjection`'s upstream `ProjectionInputs` (via
  `aggregate_projection_inputs`). The alerts layer must use the value
  that Projection v1 used; it must not re-query `fighters` and risk
  divergence.
- **Implied win probability** — same provenance: the value Projection
  v1 used (`ProjectionInputs.implied_win_probability`). For alerts that
  need the raw number (odds-vs-salary mismatch, underdog value, weak
  expensive favorite), the value comes through the same Phase B
  aggregation, not a fresh odds query.
- **Scheduled rounds + opponent context** — same: from the
  `ProjectionInputs` that fed Projection v1. The five-round-edge alert
  reads `scheduled_rounds`; the fight-group / opponent alerts read
  `has_fight_group` / `has_opponent` (or, more directly, the
  `non_projectable` tags `"fight_group"` / `"opponent"`).
- **Slate identity** — the `slate_id` used to call `project_slate`. No
  cross-slate aggregation in v1.

Inputs Mismatch Alerts v1 must **not** read or fetch:

- `effective_status` (§8). Reject / Accept / Force-Pair / Exclude
  overrides do not change alerts in v1.
- UFCStats, Sherdog, or any scraped historical data.
- Direct Odds API or any remote HTTP odds source.
- DraftKings site state (contests, ownership, late swap, etc.).
- Twitter/X, news feeds, paywalled sources.
- The DK CSV row outside of what the salary importer already persisted
  into `fighters`.

The alerts layer therefore reads the same conceptual input set as
Projection v1, plus the Projection v1 output. It introduces **no new
data sources** in v1.

## 3. Alert categories (v1)

Each category below defines: the signal it expresses, the inputs it
needs, the v1 trigger rule, and the severity. **Trigger thresholds are
v1 defaults only** — they are pinned in tests (§13) and may only be
changed via an explicit user-approved design update, mirroring
`docs/DEVELOPMENT_NOTES.md` §4's lock on the projection formula.

Severity scale in v1 is two-level:

- `info` — a value / leverage signal worth surfacing; never blocking.
- `warn` — a slate-configuration problem the user should resolve before
  trusting downstream output (projection, future optimizer, future
  export).

There is no `error` / `critical` severity in v1. If a condition would
warrant blocking, it belongs in a write path (e.g. optimizer
validation), not in a read-only alerts layer.

### 3.1 Salary inefficiency (value)

- **Signal**: per-dollar projection is unusually high *or* unusually
  low relative to the rest of the slate.
- **Inputs required**: `salary`, `projected_dk_points`,
  `projection_status == "ok"`.
- **v1 trigger**:
  - *High efficiency* (`info`): `projected_dk_points / (salary / 1000)
    >= 5.0`.
  - *Low efficiency* (`info`): `projected_dk_points / (salary / 1000)
    <= 2.5` **and** `salary >= 8500` (only flag the inefficient
    pay-ups; a cheap low-projection fighter is just a chalk avoid,
    not a value alert).
- **Severity**: `info`.
- **Notes**: thresholds are conservative — they are intended to fire on
  ~1–3 fighters per typical UFC Classic slate, not half the pool.
  Subject to recalibration after real-feed validation (§14).

### 3.2 Odds-vs-salary mismatch (value)

- **Signal**: implied win probability is materially out of step with
  the fighter's DK salary tier.
- **Inputs required**: `salary`, `implied_win_probability`,
  `projection_status == "ok"`.
- **v1 trigger**: a tier table, deliberately coarse:

  | Salary tier      | Expected p_win band | Triggers `info` if           |
  |------------------|---------------------|------------------------------|
  | `>= 9500`        | `>= 0.65`           | `p_win < 0.55`               |
  | `9000 – 9499`    | `0.58 – 0.72`       | `p_win < 0.50`               |
  | `8000 – 8999`    | `0.50 – 0.65`       | `p_win <= 0.42` or `>= 0.70` |
  | `7000 – 7999`    | `0.42 – 0.58`       | `p_win >= 0.62`              |
  | `< 7000`         | `<= 0.50`           | `p_win >= 0.55`              |

- **Severity**: `info`.
- **Notes**: the table is **not** a pricing model; it is a coarse
  heuristic for "the price disagrees with the line in a way worth
  looking at." It does not encode any expectation about ownership or
  field share.

### 3.3 Underdog value (value)

- **Signal**: cheap fighter with a non-trivial win probability — the
  same shape that drives `value_gap_bonus` in the projection formula
  (`docs/DEVELOPMENT_NOTES.md` §4), surfaced as an explicit alert rather than only
  inside the projection number.
- **Inputs required**: `salary`, `implied_win_probability`,
  `projection_status == "ok"`.
- **v1 trigger**: any of —
  - `salary <= 7600` and `implied_win_probability >= 0.45`
  - `salary <= 8000` and `implied_win_probability >= 0.48`
  - `salary <= 8500` and `implied_win_probability >= 0.55`
- **Severity**: `info`.
- **Notes**: the triggers intentionally mirror the value-gap thresholds
  in `docs/DEVELOPMENT_NOTES.md` §4 so that the alert "explains" the projection
  bonus rather than introducing a second, divergent value definition.
  If the projection formula's value-gap thresholds change (which
  requires explicit approval per §4), this section must change in the
  same slice.

### 3.4 Weak expensive favorite (warn / value)

- **Signal**: paid-up price without a dominant win probability — the
  inverse of §3.3.
- **Inputs required**: `salary`, `implied_win_probability`,
  `projection_status == "ok"`.
- **v1 trigger**: `salary >= 9000` and `implied_win_probability <
  0.55`.
- **Severity**: `info` in v1 (it is a leverage / construction signal,
  not a data problem). Future v2 may promote to `warn` once
  field-relative leverage is in scope (deferred).

### 3.5 Five-round / rounds edge (value)

- **Signal**: fighter is in a scheduled 5-round bout — the +7
  `five_round_bonus` from `docs/DEVELOPMENT_NOTES.md` §4 surfaced as an explicit
  scan-line so the user can find main-event leverage spots without
  cross-referencing the salary CSV against the fight card.
- **Inputs required**: `scheduled_rounds == 5`,
  `projection_status == "ok"`.
- **v1 trigger**: `scheduled_rounds == 5` and
  `implied_win_probability >= 0.55`. The probability gate keeps the
  alert from firing on every fighter in the main event regardless of
  edge.
- **Severity**: `info`.
- **Notes**: must not fire when `scheduled_rounds` is missing — that
  case is owned by §3.6.

### 3.6 Missing-input alert (warn)

- **Signal**: Projection v1 returned `projection_status ==
  "missing_inputs"` for this fighter — one of `salary`,
  `win_probability`, or `scheduled_rounds` is absent or invalid.
- **Inputs required**: the `FighterSlateProjection` row.
- **v1 trigger**: `projection_status == "missing_inputs"`. Each tag in
  `missing_inputs` is rendered as part of the alert message, but a
  fighter with three missing inputs produces **one** alert, not three —
  consolidation keeps the alerts list scannable.
- **Severity**: `warn`.
- **Notes**: this is the read-only mirror of projection's own
  `missing_inputs`; it adds no new logic, only a slate-level surfacing
  channel (the projection preview page already lists tags inline per
  row).

### 3.7 Projection unavailable / non-projectable (warn)

- **Signal**: Projection v1 returned `projection_status ==
  "non_projectable"` — a structural input is missing (fight group or
  opponent), so the fighter cannot have a projection at all.
- **Inputs required**: the `FighterSlateProjection` row.
- **v1 trigger**: `projection_status == "non_projectable"`. One alert
  per fighter, regardless of how many structural tags are present.
- **Severity**: `warn`.
- **Notes**: distinct from §3.8 because §3.7 is a per-fighter
  consequence framed for the alerts feed, while §3.8 is a slate-config
  framing intended to nudge the user back to Fight Groups setup. Both
  may surface for the same root cause; that duplication is acceptable
  and documented (§14 risk #6).

### 3.8 Fight-group / opponent issue (warn)

- **Signal**: at least one active fighter on the slate lacks a fight
  group, or has a fight group with an empty / unresolved opponent
  string.
- **Inputs required**: structural flags surfaced through the same
  Phase B bundle that fed Projection v1 (`has_fight_group`,
  `has_opponent`), or equivalently the `"fight_group"` / `"opponent"`
  tags on `projection_status == "non_projectable"` rows.
- **v1 trigger**: any active fighter has `has_fight_group == False`
  *or* (`has_fight_group == True` and `has_opponent == False`). The
  alert lists the affected fighter names (no opponent guess).
- **Severity**: `warn`.
- **Notes**: alerts layer must not auto-create or auto-pair fight
  groups (`SALARY_PERSISTENCE_DESIGN.md` §7). It only points the user
  at the Fight Groups page.

### 3.9 Late-news / manual-review risk (structural placeholder)

- **Signal**: reserved for a future hook into Fighter Status (out,
  late replacement, missed weight) and an eventual Manual Review gate.
- **Inputs required (v1)**: none — the data source does not exist yet.
- **v1 trigger**: **never fires in v1.** The category is defined here
  so that (a) the alert output shape (§9) and the UI (§10) reserve a
  stable column / badge for it without rework when the Fighter Status
  and Manual Review designs land, and (b) implementers cannot quietly
  invent a heuristic for it (e.g. scraping news, inferring "short
  notice" from odds movement) — those are explicit non-goals (§11).
- **Severity**: reserved as `warn` for future use.

## 4. Computed-on-read approach

Mismatch Alerts v1 is **computed on every render**:

- The alerts service is called from the alerts page (future
  `app/pages/05_alerts.py`) with a `slate_id`, returns a list of
  `Alert` value objects, and the page renders them. No alert state is
  persisted across renders.
- No background job, no recompute trigger, no invalidation cache. If
  the user changes a salary, accepts new odds, or adjusts a fight
  group, the alerts list updates on the next render because Projection
  v1 itself re-aggregates from persisted state.
- Recompute cost is bounded by `project_slate` (one slate, ≤ ~30
  fighters); no incremental / windowed logic is justified.

Implications:

- The alerts service is pure relative to the DB at call time — given
  the same DB snapshot, it returns the same alerts list. This makes
  it trivially testable with a fixture DB (§13).
- No "I dismissed this alert" / "snooze" state in v1. If users want
  acknowledgement, that is a v2 schema decision (§14 open question
  #2), not a v1 hack with `st.session_state`.

## 5. No-schema-change approach for v1

Mismatch Alerts v1 introduces **no new tables, no new columns, and no
new migrations.** Hard rules:

- No `alerts` table.
- No `alert_acknowledgements` table.
- No new column on `fighters`, `fight_groups`, `odds_rows`,
  `odds_match_results`, `manual_match_overrides`, or `slates`.
- No migration in `src/db/migrations.py`.
- No new schema test in `tests/test_odds_persistence_schema.py` or a
  sibling.

Justification: every alert in §3 is a deterministic function of inputs
already persisted for salary import, fight groups, odds matching, and
Projection v1. Persisting alerts would couple the alerts layer to
projection / odds state without an evident query that requires it.
Persistence is deferred to a future design pass (§14 open question
#2), gated on real evidence of need (history, diffing,
acknowledgement) rather than presumed need.

## 6. Relationship to Projection v1

- **Mismatch Alerts v1 is a consumer of Projection v1.** It calls
  `project_slate(conn, slate_id)` exactly once per evaluation, then
  reads only the returned `FighterSlateProjection` rows plus the same
  `ProjectionInputs` view (salary, p_win, scheduled_rounds, structural
  flags) that fed the projection. It does not reach past the projection
  layer into the odds matcher or the fighters table directly.
- **Mismatch Alerts v1 does not mutate, recompute, or invalidate
  projections.** Per `PROJECTION_V1_DESIGN.md` §9, projection runs are
  read-only end to end. The alerts layer inherits that property; an
  alert evaluation that observes a write is a bug.
- **Mismatch Alerts v1 does not promote a projection tier into a
  blocking signal.** A fighter with `projection_status ==
  "non_projectable"` produces `warn` alerts (§3.7, §3.8) but no
  optimizer hook, no export gate, no DK upload block. Those are
  separate design passes (§11, §14).
- **Mismatch Alerts v1 cannot alter the projection formula.**
  `docs/DEVELOPMENT_NOTES.md` §4 is the single source of truth for the formula
  coefficients and thresholds. Where this design references the
  formula's value-gap thresholds (§3.3) or `five_round_bonus` (§3.5),
  those references are derived, not duplicated; if §4 changes, this
  doc changes in the same slice.

## 7. Relationship to salary, odds, fight groups, rounds

- **Salary import** (`SALARY_PERSISTENCE_DESIGN.md`): supplies the
  `salary` value that every value/leverage alert (§3.1–§3.5) requires.
  The alerts layer does not validate or re-parse the CSV; it trusts
  the importer.
- **Odds matching** (`ODDS_MATCHING_DESIGN.md`,
  `src/ingestion/odds_matching*`): supplies
  `implied_win_probability` via the auto-match path that Projection v1
  already reads. The alerts layer does not perform odds matching, does
  not enqueue recomputes, and does not read manual overrides directly
  (§8).
- **Fight groups** (`src/slate/fight_grouping.py` future,
  `src/db/repositories.py::FightGroupRepository`): supplies
  `scheduled_rounds`, `has_fight_group`, `has_opponent`. The alerts
  layer surfaces missing pairings (§3.8) but never auto-pairs.
- **Rounds**: scheduled rounds reach the alerts layer only through the
  fight-group bundle. Missing rounds → §3.6 missing-input alert (via
  Projection v1's `missing_inputs` tag), never a silent default to 3.

## 8. `effective_status` deferral (locked)

Mismatch Alerts v1 **does not read, evaluate, or surface
`effective_status`.** This is the same posture as Projection v1
(`PROJECTION_V1_DESIGN.md` §2) and is required by
`ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7 and `docs/DEVELOPMENT_NOTES.md` §10.

Concrete consequences:

- A Reject Match override on an odds row does **not** suppress an
  alert that was driven by the underlying auto-match. The alerts feed
  reflects the same `auto_match`-derived implied probability that
  Projection v1 used.
- A future Accept / Force-Pair / Exclude override (D.5, currently
  paused) does **not** alter the alerts feed in v1.
- An "inactive" fighter (future Fighter Status work) is already
  excluded by Projection v1's Phase B (`status == 'active'` filter in
  `projection_input_service.py`); the alerts layer inherits that
  exclusion without re-implementing it.

Promoting `effective_status` into the alerts feed requires a separate
design pass and explicit user approval. It is not part of v1.

## 9. Alert output shape

The alerts service returns a `list[Alert]` of value objects. The shape
is fixed in v1 so the future UI (§10) and tests (§13) can pin against
it without churn.

Per-alert fields:

- `code` — short stable identifier, lowercase + underscore. v1 codes
  are the only legal values (one per category in §3):
  - `salary_inefficiency_high`
  - `salary_inefficiency_low`
  - `odds_vs_salary_mismatch`
  - `underdog_value`
  - `weak_expensive_favorite`
  - `five_round_edge`
  - `missing_input`
  - `projection_non_projectable`
  - `fight_group_issue`
  - `late_news_risk` (reserved, never emitted in v1 — see §3.9)
- `severity` — `"info"` or `"warn"` (§3).
- `scope` — `"fighter"` or `"slate"`. v1 has one slate-scoped category
  (§3.8); all others are fighter-scoped.
- `fighter_id` — `int | None`. Required when `scope == "fighter"`,
  always `None` when `scope == "slate"`.
- `fighter_name` — `str | None`. Mirrors `fighter_id`; used for UI
  rendering without re-querying the read model.
- `message` — human-readable one-line description. Stable enough to be
  pinned by AppTest (§13) but does not embed raw odds rows or full DK
  CSV lines.
- `tags` — `tuple[str, ...]`. Diagnostic tags carried through from
  Projection v1 where applicable (e.g. the projection's
  `missing_inputs` tags for `missing_input` alerts). Empty tuple
  otherwise.

The list is returned in a deterministic order:

1. `severity == "warn"` before `severity == "info"`.
2. Within a severity, `scope == "slate"` before `scope == "fighter"`.
3. Within a scope, by `code` lexicographic.
4. Within a code, by `fighter_id` ascending (slate-scoped alerts have
   no fighter id and sort first by step 2).

Deterministic ordering is required so that AppTest can pin the
rendered list (`docs/DEVELOPMENT_NOTES.md` §11). It is **not** a ranking. The alerts
layer does not assign priority beyond severity.

## 10. UI concept for future `app/pages/05_alerts.py`

The page is **read-only** and follows `docs/DEVELOPMENT_NOTES.md` §11 rules for
derived-state pages (no write actions, no transactional handlers, no
session-state mutation outside `st.selectbox` selection state).

Page elements (future implementation):

1. **Slate selector** — `st.selectbox` over `SlateRepository.list_all`,
   mirroring `app/pages/09_projections.py`. Same `format_func` shape.
2. **Top-of-page warning banner** — pinned text covering:
   - "Read-only. No alerts are persisted."
   - "`effective_status` is not consulted; reject/accept overrides do
     not affect these alerts."
   - "These alerts do NOT feed the optimizer, exports, or any external
     system."
   - "The salary importer still requires a real DK UFC Classic salary
     CSV smoke validation before it is considered complete."
3. **Summary line** — counts per severity (e.g. `3 warn · 7 info`),
   pinnable by AppTest.
4. **Alert list / table** — one row per `Alert`, columns:
   `Severity`, `Scope`, `Fighter`, `Code`, `Message`. Sorted per §9.
5. **Empty state** — when no alerts fire, a single info line ("No
   alerts for this slate.") with the same banner above it. AppTest
   pins this text.

What the page must **not** contain in v1:

- No "dismiss" / "snooze" / "acknowledge" buttons.
- No "recompute alerts" button — alerts recompute on render by
  construction (§4).
- No filter / threshold controls — thresholds in §3 are fixed in v1.
- No links to mutate fight groups, odds matches, or overrides from
  inside an alert row. The user navigates to the existing page
  (Slate Setup, Fight Groups, Odds) themselves.
- No charts, graphs, or distribution plots.
- No raw odds row contents or raw DK CSV row contents.

## 11. Service / function concept for future implementation

Module layout (future, not implemented in this slice):

- `src/alerts/alert_rules.py` — pure rule definitions; one function
  per category in §3, each taking the resolved inputs it needs and
  returning `Alert | None`. The `Alert` value object and the Phase A
  pure rule functions already live here; the future implementation
  slice extends this module with the remaining rule categories.
- `src/alerts/alert_service.py` — `evaluate_alerts(conn, slate_id) ->
  list[Alert]`. Composes Projection v1's `project_slate` output (and
  the same `ProjectionInputs` view via the existing
  `aggregate_projection_inputs` if needed) with the rule functions in
  `alert_rules.py`. Sorts per §9. Pure read end to end.

Hard contracts for the future implementation:

- The service signature ends in `(conn, slate_id) -> list[Alert]`. No
  optional `effective_status` parameter, no optional `threshold`
  parameter — extension is a v2 concern (§14).
- The service must call `project_slate` **exactly once** per invocation
  and must not re-call `aggregate_projection_inputs` if the same
  bundle data is already exposed through projections. If a second
  query is needed, the implementation slice documents why in the slice
  report.
- The service performs **no DB writes**. An integration test that
  observes a write is a bug (`docs/DEVELOPMENT_NOTES.md` §11, §14, mirrored from
  Projection v1's contract).
- The service is independent of UI: it returns value objects, never
  Streamlit widgets, never `st.write` calls.

## 12. Implementation phase split

The slices below are **future work** — this doc creates none of them.
Each slice gets its own design check-in and its own commit per
`docs/DEVELOPMENT_NOTES.md` §13 ("one slice per session"). Each slice is sized per
`AI_BUILD_WORKFLOW.md` §3–§4.

- **Phase A — Pure rule functions + tests.** Implement one function
  per §3 category (skipping §3.9, which never fires in v1) in
  `src/alerts/alert_rules.py`. Pure-Python, no DB, no Streamlit.
  Unit tests pin every threshold in §3.
- **Phase B — Slate-level alert service.** Implement
  `evaluate_alerts(conn, slate_id)` in `src/alerts/alert_service.py`.
  Compose `project_slate` + Phase A rules; apply §9 ordering. Unit /
  integration tests against a fixture DB.
- **Phase C — Streamlit page wiring.** Implement
  `app/pages/05_alerts.py` per §10. AppTest pins the banner, the
  summary line, the alert rows, and the empty state.
- **Phase D — Real-feed smoke.** Manual validation against the same
  real DK UFC Classic salary CSV used for the salary importer smoke
  (`SALARY_PERSISTENCE_DESIGN.md` §13) plus a real odds export. Until
  this run is documented, Mismatch Alerts v1 must not be described as
  complete (`docs/DEVELOPMENT_NOTES.md` §8). Phase D is a checklist run, not a code
  slice.

**Phase D depends on** both the salary importer smoke and Projection
v1's own real-feed smoke (`PROJECTION_V1_DESIGN.md` §9 cross-cutting)
having been documented first. If either upstream gate is still open,
Phase D does not start.

## 13. Testing plan (for the future implementation)

Pure unit tests (Phase A) — no DB, no Streamlit:

- Salary inefficiency: representative `(salary, projected_dk_points)`
  tuples for both `info` triggers in §3.1, plus a non-trigger near
  each threshold.
- Odds-vs-salary mismatch: at least one tuple per row of the §3.2
  tier table, both inside and just outside each band.
- Underdog value: triple-mirror the §3.3 / `docs/DEVELOPMENT_NOTES.md` §4 value-gap
  thresholds. A change to either source must break the other test.
- Weak expensive favorite: a fighter at `salary == 9000`,
  `p_win == 0.54` (fires) and `p_win == 0.55` (does not).
- Five-round edge: 5-round + high p_win (fires), 5-round + low p_win
  (does not), 3-round + high p_win (does not), missing rounds (does
  not — owned by §3.6).
- Missing-input alert: one fighter with `missing_inputs == ("salary",
  "win_probability")` produces exactly one alert with both tags in
  `tags`.
- Non-projectable: one fighter with `projection_status ==
  "non_projectable"` produces exactly one alert; the duplicate vs §3.8
  is asserted explicitly (§14 risk #6).
- Late-news placeholder: assertion that no rule emits
  `code == "late_news_risk"` for any input in v1.
- Sort order: a hand-built mixed list of `Alert` values exercises every
  step of §9's ordering and the test pins the resulting sequence.

Service / repository tests (Phase B) — read-only DB fixtures:

- Slate with mixed fighter states: some `ok`, some `missing_inputs`,
  some `non_projectable`, at least one underdog-value trigger, at
  least one tier-mismatch trigger. Assert the returned list matches
  expected codes / severities / fighter ids.
- Empty slate (no active fighters): returns `[]`.
- Unknown slate id: returns `[]`, mirroring `project_slate` and
  `aggregate_projection_inputs`.
- **No DB mutation.** Assert row counts / timestamps unchanged across
  the call (mirroring `PROJECTION_V1_DESIGN.md` §9).
- **`effective_status` ignored.** Build a fixture with a Reject Match
  override against an auto-match row and assert that the alerts list
  is identical to the no-override case (per §8).

UI tests (Phase C) — AppTest:

- Page renders the banner text from §10 item 2 verbatim.
- Page renders the summary line from §10 item 3 with correct counts
  for the fixture slate.
- Page renders one row per alert with the correct ordering per §9.
- Empty state pins the "No alerts for this slate." text.
- No write actions exist on the page; AppTest confirms no buttons or
  forms are present (or, equivalently, that the DB is unchanged after
  interacting with the slate selector).

Cross-cutting:

- **No alerts side effects.** An alert evaluation does not enqueue
  odds recomputes, projection invalidations, or override mutations.
- **Pinning the §4 formula link.** The Phase A test for underdog value
  imports the same threshold constants used by the value-gap bonus
  if they have been factored into a shared module; if not, the test
  encodes the duplication explicitly and the implementation slice
  decides whether to extract a constant (one-line refactor, separate
  commit per `AI_BUILD_WORKFLOW.md` §3).

## 14. Non-goals

Mismatch Alerts v1 explicitly does **not** ship any of:

- Ownership projections of any kind.
- Field-leverage / leverage-vs-field calculations.
- Lineup generation, lineup ranking, lineup scoring.
- Optimizer wiring of any kind. Alerts do not feed, gate, or filter
  the optimizer in v1.
- Export wiring. Alerts do not block or annotate any DK upload export.
- Machine-learning model training or inference.
- Finish-probability or method-of-victory modeling. Future work; not
  v1.
- Volatility tags, boom/bust labels, leverage scores.
- Direct Odds API or any remote HTTP fetch.
- Odds scraping from books, aggregator sites, or third parties.
- UFCStats / Sherdog / any external stats scrape.
- News scraping (Twitter/X, press releases, RSS, paywalled feeds).
- DraftKings login, contest auto-entry, screen automation.
- Persistence of any alert: no `alerts` table, no acknowledgement
  table, no migration (§5).
- Reading `effective_status` (§8). Reject / Accept / Force-Pair /
  Exclude overrides do not change alerts.
- D.5 override types (Accept, Force Pair, Exclude, manual moneyline,
  low-confidence ack). Per `docs/DEVELOPMENT_NOTES.md` §10, D.5 stays paused.
- User-configurable thresholds, per-slate threshold overrides, or
  alert-rule toggles. All thresholds are fixed in v1.
- Cross-slate aggregation ("which fighters tend to trigger underdog
  value alerts across recent slates"). v1 evaluates one slate at a
  time.
- NFL, Showdown, Pick6, or any non-UFC-Classic format (`docs/DEVELOPMENT_NOTES.md`
  §3).

Any item above requires a **separate** design doc and explicit
approval before implementation.

## 15. Risks and open questions

1. **Threshold calibration.** The §3 thresholds are intuition-based
   and have not been validated against a real DK UFC Classic slate.
   The Phase D smoke (§12) is the first time real fighters will be
   scored. Risk: alerts fire too often (noise) or too rarely
   (irrelevant). Mitigation: thresholds are pinned in Phase A tests,
   so adjustment requires an explicit design + test update.
2. **Persistence (open).** Should alerts ever be persisted (history,
   diffing across recomputes, "I saw this" acknowledgement)?
   Recommendation: stay computed-on-read in v1; revisit only if Phase
   D surfaces a concrete review workflow that requires history.
3. **Severity scale (open).** v1 has two levels. If Phase D reveals
   that warn-level slate-config problems and warn-level missing inputs
   should be split (e.g. "blocking for optimizer" vs "advisory"), the
   right move is to introduce that distinction in the optimizer
   design, not by adding a third severity here.
4. **Overlap with Projection v1's own `missing_inputs` rendering.**
   The projection preview page already lists missing-input tags per
   fighter. The alerts page re-surfaces them as §3.6 alerts. Risk:
   users see the same information twice and the two surfaces drift.
   Mitigation: alerts derive directly from
   `FighterSlateProjection.missing_inputs` (§9 `tags`); the projection
   page is the single source of truth for the underlying state, and
   alerts only add a slate-level scan view.
5. **Overlap between §3.7 and §3.8.** A slate with a fighter missing
   a fight group will produce both a per-fighter `warn`
   (`projection_non_projectable`) and a slate-scoped `warn`
   (`fight_group_issue`). This is intentional — the per-fighter
   alert points at the projection consequence, the slate-scoped
   alert points at the fix surface — and Phase B tests pin the
   duplication explicitly so an "optimisation" that hides one cannot
   land silently.
6. **Tier-mismatch heuristic vs real DK pricing.** The §3.2 tier
   table is a coarse guess. Risk: DK pricing for a given event
   doesn't fit the table (e.g. a champ vs. severe underdog with
   asymmetric pricing). Mitigation: §3.2 is `info`-only and never
   blocks anything; Phase D documents miscalibration as a follow-up
   slice, not a hot-fix.
7. **Coupling to `docs/DEVELOPMENT_NOTES.md` §4 thresholds (§3.3).** Underdog value
   intentionally mirrors the value-gap bonus thresholds. If §4 ever
   changes (which itself requires explicit approval), this design and
   its Phase A tests must change in the same slice. Risk: drift if
   §4 is updated without touching this doc. Mitigation: §3.3
   names the dependency and Phase A tests assert the constants.
8. **Late-news placeholder mis-use (§3.9).** A future contributor
   may interpret "reserved" as "implement a heuristic." Mitigation:
   the v1 test plan (§13) asserts that no rule emits
   `code == "late_news_risk"` for any input in v1. Promoting the
   category to an active alert requires a Fighter Status / Manual
   Review design pass and an explicit threshold definition.
9. **Recompute cost.** Alerts run on every page render. For ≤ ~30
   fighters per slate this is trivially fast, but if Projection v1
   ever grows expensive (e.g. multi-slate aggregation), the alerts
   page inherits that cost. Mitigation: alerts service performs no
   work beyond reading `project_slate` and applying the §3 rules; any
   future cost regression in Projection v1 is a Projection v2 concern.
10. **Real-feed gate inheritance.** Mismatch Alerts v1 cannot be
    described as complete until both the salary smoke
    (`SALARY_PERSISTENCE_DESIGN.md` §13) and a Projection v1 real-feed
    smoke (`PROJECTION_V1_DESIGN.md` §9 cross-cutting) have been
    documented. Until then, all Phase A–C tests passing means the
    alerts layer is "tested in isolation," not "validated against real
    feed" (`docs/DEVELOPMENT_NOTES.md` §8, §14).
