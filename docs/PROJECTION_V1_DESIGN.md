# Projection v1 Design

Status: design only. No implementation in this slice. Companion to
`docs/DEVELOPMENT_NOTES.md` §4 (projection formula) and §10 (current checkpoint).

---

## 1. Purpose

Projection v1 is the first **local, conservative, transparent** UFC DK
projection layer for this workbench. It turns the inputs already
persisted locally (salary, fight group, scheduled rounds, odds-derived
win probability) into a single number — `projected_dk_points` per
fighter per slate — plus a status describing whether that number is
trustworthy.

Explicit non-claims:

- Projection v1 is **not** a machine-learning model.
- Projection v1 does **not** introduce new data sources.
- Projection v1 does **not** change the formula in `docs/DEVELOPMENT_NOTES.md` §4.
- Projection v1 is **not** an ownership, finish-rate, or volatility
  model.

The intent is a defensible default that downstream layers (alerts,
optimizer, exports) can consume **after their own design passes**.

## 2. Inputs available or expected

Local inputs Projection v1 may read (read-only):

- **Fighter identity + salary** — from the salary import (`fighters`
  table, populated by Slices A–D of salary persistence).
- **Fight group / opponent context** — fight-group service, so each
  fighter knows their opponent and pairing.
- **Scheduled rounds** — 3 or 5, sourced from the fight-group / slate
  metadata.
- **Odds-derived win probability** — implied (no-vig) win probability
  produced by `src/projections/implied_probability.py` over matched
  odds rows.
- **Manual / uploaded odds rows** — same path, just a different
  ingestion source; Projection v1 does not distinguish.
- **Fighter status** — *eventually*; v1 does not yet gate on it (see
  §5, §10 open question).
- **`effective_status`** — deferred in v1; **promoted by D.5.2**. Phase B
  as shipped reads `match_status == 'auto_match'` only. The
  projection-source promotion to read `effective_status` (so manually
  accepted / force-paired fighters gain a win probability) is designed in
  `ODDS_PERSISTENCE_DESIGN.md` §16.9 and summarized in §11 below. It is
  gated — implementation needs explicit approval per `docs/DEVELOPMENT_NOTES.md` §10.
  Until D.5.2 lands, projections continue to ignore `effective_status`
  (`ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7).

Inputs Projection v1 must **not** read or fetch:

- UFCStats / Sherdog / any scraped historical data.
- Direct Odds API or any remote HTTP odds source.
- DraftKings site state (contests, ownership, late swap, etc.).

## 3. Formula boundary

The arithmetic that produces `projected_dk_points` is **already
specified** in `docs/DEVELOPMENT_NOTES.md` §4 and locked by
`tests/test_projection_formula.py`:

```
default_projection =
    implied_win_probability * 70
    + value_gap_bonus
    + five_round_bonus
```

Rules for this design:

- **Do not change** the coefficients (70, 8 / 5 / 3, 7).
- **Do not change** the value-gap thresholds (7600/0.45, 8000/0.48,
  8500/0.55) or the round threshold (`scheduled_rounds == 5`).
- **Do not introduce** new coefficients, multipliers, dampeners, or
  regression terms in v1.
- **Do not implement** the formula in this slice — the math already
  lives in `src/projections/default_projection.py` and
  `src/projections/value_bonus.py`. Projection v1 wraps it; it does
  not rewrite it.

Any change to the formula requires explicit user approval **and** a
paired test update, per `docs/DEVELOPMENT_NOTES.md` §4.

## 4. Output shape

Projection v1 produces, per (fighter, slate), a row with:

- `fighter_id` — FK to the fighter persisted by salary import.
- `slate_id` — FK to the slate the projection was computed for.
- `projected_dk_points` — float, or `None` when non-projectable.
- `projection_status` — enum-like string, one of:
  - `ok` — all required inputs present, formula applied.
  - `missing_inputs` — at least one required input is absent or
    invalid; `projected_dk_points` is `None`.
  - `non_projectable` — fighter is structurally ineligible (e.g.
    no fight group, inactive once status gating is wired). Distinct
    from `missing_inputs` so the UI can render it differently.
- `missing_inputs` — list of short stable string tags (e.g.
  `"salary"`, `"win_probability"`, `"scheduled_rounds"`,
  `"fight_group"`, `"opponent"`). Empty list when status is `ok`.
- `notes` — free-text, human-readable reasons (e.g. "no odds row
  matched", "fight group has no opponent"). Optional, may be empty.
- `floor` / `ceiling` — **placeholders only**, both `None` in v1.
  Reserved for a future volatility pass (see §6, §10). If included in
  the output shape at all, they must be clearly marked
  `# v2 placeholder` and never populated by v1 logic.

The output is a **value object**, not a DB row. Persistence is an open
question (§10), and v1 may remain computed-on-read.

## 5. Status / gating behavior

Projection v1 must never silently invent inputs. The status field is
the contract:

| Condition                                  | Status            | `missing_inputs` tag        |
|--------------------------------------------|-------------------|-----------------------------|
| Fighter has no salary row for this slate   | `missing_inputs`  | `"salary"`                  |
| Fighter has no matched odds row            | `missing_inputs`  | `"win_probability"`         |
| Scheduled rounds unknown / not in {3, 5}   | `missing_inputs`  | `"scheduled_rounds"`        |
| Fighter has no fight group on this slate   | `non_projectable` | `"fight_group"`             |
| Fight group has no opponent                | `non_projectable` | `"opponent"`                |
| Fighter marked inactive (future, §10)      | `non_projectable` | `"fighter_status"`          |

Rules:

- Missing odds → `projected_dk_points = None`, **not** a fallback to
  0.5 or to the chalk. We do not guess win probability.
- Missing rounds → no projection. We do not default to 3 silently.
- Multiple missing inputs → all tags appear in `missing_inputs`.
- A fighter who is `non_projectable` is never `ok` even if numbers
  exist (a structural problem dominates a data problem).

## 6. Non-goals (v1)

Projection v1 explicitly does **not** ship any of:

- Ownership projections of any kind.
- Machine-learning model training, retraining, or inference.
- UFCStats / external-stats scraping.
- Direct Odds API or any remote odds fetch.
- Odds scraping from books or aggregator sites.
- Finish-probability / method-of-victory modeling.
- Volatility tags, leverage tags, or boom/bust labels.
- Mismatch Value Alerts (separate design).
- Optimizer wiring — the optimizer does not read projections in v1.
- Export behavior — exports do not consume v1 projections.
- D.5 override types (Accept, Force Pair, Exclude, manual moneyline,
  low-confidence ack). Per `docs/DEVELOPMENT_NOTES.md` §10, D.5 stays paused.

Any item above requires a **separate** design doc and explicit
approval before implementation.

## 7. Interaction with existing systems

Read direction only — Projection v1 mutates nothing:

- **Salary import** (`src/db/repositories.py` fighter/salary path) →
  supplies fighter identity and salary.
- **Fight groups** (`src/slate/`) → supply opponent and
  `scheduled_rounds`.
- **Odds matching** (`src/ingestion/`, `src/projections/
  implied_probability.py`) → supplies `implied_win_probability` for
  matched fighters.
- **Fighter status** → *future*; not consumed by v1, but the status
  enum (§5) reserves space so wiring it later is additive.
- **Manual review gate** → *future*; controls optimizer eligibility,
  not projection eligibility. Projection v1 reports the number;
  downstream layers decide whether to use it.
- **Alerts / optimizer / exports** → consumers, not collaborators.
  They are out of scope for v1 implementation and must each pass
  their own design review before reading projections.

## 8. Implementation phase split

The slices below are **future work** — this doc creates none of them.
Each slice gets its own design check-in and its own commit per
`docs/DEVELOPMENT_NOTES.md` §13 ("one slice per session").

- **Phase A — Pure projection service + tests.** Wrap the existing
  `default_projection` math in a service function that takes the
  resolved inputs (salary, p_win, rounds) and returns the §4 output
  shape. Pure-Python, no DB, no Streamlit. Tests only.
- **Phase B — Repository / read model for projection inputs.** A
  thin read-side aggregator that, given a `slate_id`, returns the
  per-fighter input bundle (salary, fight group, opponent, rounds,
  p_win). No new tables in v1 if the existing repositories suffice;
  if a new read view is needed, it goes here.
- **Phase C — Projection service over one slate.** Glue A + B:
  iterate fighters on the slate, produce one v1 output row per
  fighter, populate `projection_status` and `missing_inputs`
  correctly. Still no UI.
- **Phase D — UI preview page / table.** A Streamlit page (or a
  section within an existing page) that renders the per-fighter
  projection rows, including status badges and missing-input tags.
  Read-only; no write actions. Must follow `docs/DEVELOPMENT_NOTES.md` §11 — derived
  state is covered by AppTest with pinned text.
- **Phase E — Downstream integration (alerts, optimizer, exports).**
  Out of scope for v1. Requires separate design approval per layer
  before any wiring.

## 9. Testing plan

Pure unit tests (Phase A) — no DB, no Streamlit:

- Formula passthrough: matches `tests/test_projection_formula.py`
  for representative (p_win, salary, rounds) tuples.
- Missing salary → `missing_inputs` includes `"salary"`, points
  `None`, status `missing_inputs`.
- Missing win probability → tag `"win_probability"`, points `None`.
- Missing rounds → tag `"scheduled_rounds"`, points `None`.
- 3-round vs 5-round → confirm `five_round_bonus` is applied only
  when rounds == 5.
- Multiple missing inputs aggregate into a single result row with
  all tags.
- Non-projectable beats missing_inputs when structural inputs
  (fight group, opponent) are absent.
- (Future) inactive fighter → `non_projectable`,
  `"fighter_status"`. Reserved; not implemented in v1 until status
  gating lands.

Service / repository tests (Phases B–C) — read-only DB fixtures:

- Slate-level iteration returns one row per fighter on the slate.
- Repository read does not mutate the DB (assert row counts /
  timestamps unchanged across the call).

UI tests (Phase D) — AppTest:

- Per `docs/DEVELOPMENT_NOTES.md` §11, the projection preview renders derived state
  (points, status badges, missing-input tags) and the test pins the
  rendered text.
- No write action exists on the projection page in v1, so no
  transactional / idempotence test is required.

Cross-cutting:

- **No DB mutation in the projection service.** Projection v1 is
  read-only end to end. Any test that observes a write is a bug.
- **No optimizer / alerts side effects.** A projection run does not
  enqueue, recompute, or invalidate downstream state.
- Real-feed validation: once Phases A–D are merged, a smoke run
  against the real DK UFC Classic salary CSV + a real odds export
  must be documented before claiming "complete," consistent with
  `docs/DEVELOPMENT_NOTES.md` §8 / §10.

## 10. Open questions

1. **Persistence.** Should projection rows be persisted (new table:
   `projections` keyed on `(slate_id, fighter_id)`), or remain
   computed-on-read? Persistence enables history / diffing across
   recomputes; computed-on-read keeps the surface smaller.
   *Recommendation:* start computed-on-read in v1, persist in v2 if
   diff history becomes useful.
2. **Representing missing odds.** Do we surface "no odds matched" vs
   "odds matched but rejected" vs "odds matched, low confidence"
   distinctly in `missing_inputs` / `notes`, or collapse all three
   to `"win_probability"` in v1? *Recommendation:* collapse in v1;
   refine when low-confidence ack (D.5) lands.
3. **Fighter status gating.** When fighter status persistence lands,
   does an inactive fighter produce `non_projectable` (current
   plan), or is `missing_inputs` more appropriate? Tied to whether
   "inactive" is structural or recoverable.
4. **Floor / ceiling.** Do floor/ceiling belong in the v1 output
   shape (as always-`None` placeholders) or are they introduced
   only in v2? *Recommendation:* leave them out of v1 entirely to
   avoid pinning a shape that v2 will redesign.
5. **Recompute trigger.** Does a salary re-import or odds
   re-import invalidate a persisted projection? Only relevant if
   open question #1 resolves toward persistence.

## 11. Projection-source promotion (D.5.2) — designed, gated

This subsection records the projection-source decision; the full design
and rationale live in `ODDS_PERSISTENCE_DESIGN.md` §16 (esp. §16.9). It
is design only — implementation is gated (`docs/DEVELOPMENT_NOTES.md` §10).

Problem. The Phase B aggregator
(`src/projections/projection_input_service.py`) derives win probability
from `odds_match_results` rows whose `match_status == 'auto_match'`,
keyed on the result row's `fighter_id`. A fighter whose odds row was
only manually accepted / force-paired (name-mismatch case: salary
`Bruno Silva` vs odds `Bruno Gustavo da Silva`) has no `auto_match` row,
so it reports `missing_inputs:win_probability` and Build drops it — even
after the user "fixed" it on the Odds page. That is the fake-fix trap.

Decision. When D.5 lands the `accept_match` / `force_pair` override
types, the aggregator switches from `match_status == 'auto_match'` to an
approved `effective_status` set:

- `auto_match`
- `review_accepted` (D.5 `accept_match`)
- `force_pair` (D.5 `force_pair`)

Blocked (no win probability): `review_required`, `unmatched`,
`review_rejected`, `excluded`, `shadowed`. (`manual_moneyline` /
`manual_projection_low_confidence_ack` enter the eligible set only when
their own override types are implemented.)

Binding. The eligible-set predicate is necessary but not sufficient — a
force-paired `unmatched` row has `fighter_id = NULL` until D.5's apply
pass writes the override's bound fighter onto the result row
(`ODDS_PERSISTENCE_DESIGN.md` §16.5). The aggregator continues to key win
probability off `fighter_id`; D.5 makes that id correct for bound rows.

Why this is the structural fix. One predicate now governs both the Odds
"resolved?" view and the Build / projection pool, so the two cannot
diverge — there is no state where Odds looks fixed but Build still
excludes the fighter. The aggregator's existing active-fighter filter is
retained, so a binding to a since-deactivated fighter (stale override,
§16.12) still yields no projection.

Guardrails unchanged. No formula change (§3). No new input source — win
probability still comes from `odds_rows.implied_probability` via the
matched/bound odds row. No DB writes in the aggregator. Missing odds
still yields `None`, never a guessed 0.5 (§5).

Test deltas (land with D.5.2, see `ODDS_PERSISTENCE_DESIGN.md` §16.15): a
force-paired fighter reports `projection_status = ok` with a real win
probability; `review_rejected` / `review_required` / `unmatched` rows
contribute none; the §16.1 Build-exclusion-removed integration test.
