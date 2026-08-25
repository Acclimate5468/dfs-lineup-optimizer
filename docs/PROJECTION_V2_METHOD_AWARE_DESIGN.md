# Projection v2: Finish-Aware (Outcome-Modeled) Projection Design

Status: **design only.** No implementation in this slice. This revision (rev 2)
folds in a six-lens adversarial review (modeling math, scope compliance,
codebase grounding, completeness, DK-scoring fact-check, red-team). Companion to
`PROJECTION_V1_DESIGN.md` (the scalar v1 it extends), `docs/DEVELOPMENT_NOTES.md` §4 (the v0
formula, which this does **not** change), and `OPTIMIZER_V1_DESIGN.md`.

> **Naming note.** Rev 1 of this doc called the feature "method-aware." That
> framing was factually wrong (see §2): DraftKings' fight-resolution bonus keys
> on the **round of finish** and a quick-win timer, **not** on KO-vs-submission.
> The feature is **finish-aware** (finish vs decision, and which round), not
> method-of-victory-aware. The rename is deliberate.

---

## 1. Purpose

Projection v1 turns one input — odds-implied win probability — into one number
per fighter via the `docs/DEVELOPMENT_NOTES.md` §4 formula
(`implied_win_probability * 70 + value_gap_bonus + five_round_bonus`). That
formula is a defensible **baseline**, but it is blind to the largest driver of DK
UFC fantasy scoring: **whether the fight is finished and in which round**, not
just who wins.

Projection v2 introduces a **finish-aware** projection engine that estimates a
fighter's expected DraftKings points — and an explicit per-outcome distribution —
from:

- win probability (already available — v1's input),
- a **finish signal** (how likely the fight ends inside the distance, and how it
  splits between the two fighters), and
- the **DraftKings UFC Classic scoring table** applied to those outcomes.

### Two honesty notes up front (do not bury these)

1. **On day one, only the scalar mean changes the user's lineups.** v2 also
   produces a per-outcome distribution (branch points + floor/ceiling), but the
   current optimizer reads **only the scalar mean** and ignores the distribution
   (`src/optimizer/pool_builder.py` reads `projected_dk_points`; floor/ceiling
   are not consumed). The distribution is **computed but unconsumed** until a
   separate future optimizer/sim design opts in (§17). v2's value *today* is a
   better-grounded **point estimate**; its value *later* is being the input the
   Monte-Carlo lever needs.

2. **v2 is not validated to beat v0, and this design treats validating it as a
   first-class, gating step (§12).** "Better-grounded" means "built from the
   scoring table instead of one linear coefficient," **not** "measured to be more
   accurate." No backtest/calibration harness exists in this repo today; §12
   builds a minimal one and makes it a precondition for ever promoting v2 to the
   default.

### Explicit non-claims

- v2 is **not** a machine-learning model. It is a transparent arithmetic
  decomposition of expected points over fight outcomes.
- v2 does **not** change or remove the `docs/DEVELOPMENT_NOTES.md` §4 v0 formula. v0 remains the
  default engine (§10) until v2 is *measured* better and explicitly promoted.
- v2 does **not** ship ownership, leverage, a simulation optimizer, or any
  optimizer/alerts/export wiring (§11).
- v2's per-fighter outcome branches are **not** a valid *joint* fight model — see
  the shared-fight-length caveat in §6 and §17.
- v2 does **not** claim to produce winning lineups.

## 2. The scoring insight (corrected)

DK UFC Classic scoring is **outcome-weighted**. The fight-resolution bonus is
largest for an early finish and smallest for a decision, and per-action scoring
(strikes, knockdowns, takedowns, control time) accumulates differently across
fight length and archetype.

**Correction from rev 1 (fact-checked against current DK rules):** the
resolution bonus keys on the **round of finish** plus a **quick-win timer**, and
is **method-agnostic** — a round-1 KO and a round-1 submission earn the *same*
bonus. So the separation v2 wants does **not** come from a KO-vs-sub signal; it
comes from **finish-vs-decision, which round, and accumulated action by
archetype**. The input must therefore be a *finish-likelihood* signal, not a
"method" signal.

The consequence the v0 formula cannot see is still real:

> Two fighters with the **same 60% win probability** can have **materially
> different fantasy ceilings and floors** — a fighter in a likely-finish fight
> has a higher ceiling (early-finish bonus) but also a lower floor (if *they* are
> the one finished); a durable decision fighter has a tighter distribution and a
> higher floor (accumulated strikes/control even in a loss).

v1 assigns both the same `0.60 * 70 = 42` base. v2 separates them by routing win
probability and a finish signal through the scoring table.

Concretely, v2 estimates expected DK points over **four** mutually-exclusive
outcomes (rev 1 used three and wrongly collapsed all losses — see §6):

```
E[DKpts] =
      P(win by finish)     * E[pts | finish win]
    + P(win by decision)   * E[pts | decision win]
    + P(loss by finish)    * E[pts | finish loss]      # ~0 action pts: finished early
    + P(loss by decision)  * E[pts | decision loss]    # can be 40-80 pts: full-fight volume
```

The loss split matters: a fighter finished in round 1 scores almost nothing,
while a fighter who loses a competitive decision can post 40–80 DK points from
accumulated action. Collapsing them (rev 1) corrupted the floor — the very thing
the model exists to surface.

## 3. Why this lever first (vs the alternatives) — honest weighing

The red-team correctly challenged the rev-1 assertion that finish-aware
projection is automatically "lever #1." Three candidate first levers, weighed by
input cost and *consumed* output:

| Candidate | New per-slate input? | Scope expansion? | Consumed by today's optimizer? |
|-----------|----------------------|------------------|-------------------------------|
| **v2 Tier 0** (finish-aware, league-average finish rate) | **None** (a constant) | **None** (§4) | **Yes** — scalar mean flows through the existing seam |
| v2 Tier 1/2 (per-fighter / market finish signal) | Yes (manual or props) | **Yes** (§4 / `docs/DEVELOPMENT_NOTES.md` §3) | Yes (mean); distribution still inert |
| Monte-Carlo sim on v1 projections | None | None | Would need a new optimizer design to consume |
| Win-probability calibration (no-vig quality) | None | None | Yes — improves *every* engine incl. v0 |

Conclusion that this design commits to:

- **v2 Tier 0 is a legitimate, cheap first step** — it needs no new user input and
  no scope expansion, and its scalar mean is consumed today. That makes it
  buildable now and a real foundation for the sim lever.
- **But "finish-aware projection" is not unambiguously higher-leverage than
  Monte-Carlo-on-v1 or win-prob calibration.** Those are also cheap and one of
  them (calibration) lifts v0 too. This doc does **not** claim v2 dominates them;
  it claims v2 Tier 0 is a sound, low-cost, in-scope place to start *if the user
  wants finish-aware projections*, and it sequences validation (§12) so the user
  can compare before investing further. The lever ordering is the user's call,
  surfaced explicitly rather than asserted.

## 4. Input tiers & the scope decision (restructured)

The finish signal can come from four tiers. **Rev 1's biggest framing error was
implying any finish-aware work requires a `docs/DEVELOPMENT_NOTES.md` §3 scope expansion. It does
not** — Tier 0 introduces no new data source.

| Tier | Finish signal source | Scope status | Recommendation |
|------|----------------------|--------------|----------------|
| **Tier 0 — League-average finish rate** | A single pinned constant (UFC fights finish inside the distance ~55–60% of the time; a literature/historical value), applied uniformly. No per-fighter signal. | **In v0 scope.** No new data source, no props, no scraping. Pure-Python math in `src/projections/` (`docs/DEVELOPMENT_NOTES.md` §1). Still needs normal design approval to build (this doc), and is **not** a §4 formula change (it's a parallel engine). | **Build this first.** Isolates the scoring-table effect from user guesswork; ships + validates with zero per-slate burden. |
| **Tier 1 — Manual finish prior** | User hand-enters a coarse finish likelihood per fight (or fighter). | **`docs/DEVELOPMENT_NOTES.md` §3 expansion** — a new *input meaning*. Needs explicit user sign-off recorded in `CURRENT_STATUS.md`. **Caveat:** unlike manual moneyline entry (which transcribes an external market number), a hand-entered finish prior has **no external referent** — it is the user's intuition re-encoded. v2's accuracy is then bounded by that intuition (garbage-in risk). | Optional **override** on top of Tier 0, *after* Tier 0 is validated — not the load-bearing input. |
| **Tier 2 — Method / ITD market odds** | Parse sportsbook **inside-the-distance / method-of-victory** prop markets into a finish probability. | **`docs/DEVELOPMENT_NOTES.md` §3 expansion** — adds a **props market** to odds acquisition; needs its own acquisition design (sibling to `ODDS_ACQUISITION_V0_DESIGN.md`). | The *principled* external finish source; defer to v2.1 once Tier 0 proves the model. |
| **Tier 3 — Historical finish-rate model** | Derive finish rates from fighter fight history. | **Out of scope.** `docs/DEVELOPMENT_NOTES.md` §3 excludes UFCStats/Sherdog scraping. | Own project; not v2. |

Decisions required from the user:

1. **Build finish-aware projections at all?** (yes / no.)
2. **If yes, confirm the entry point is Tier 0** (league-average constant,
   in-scope, validation-first), with Tier 1/2 as later, separately-approved
   refinements. Tier 1 and Tier 2 each need an explicit §3 scope sign-off
   **recorded in `CURRENT_STATUS.md`** before their slices may begin.

Everything below is written for **Tier 0 first**, with the model parameterized so
Tier 1/2 slot in later as alternative *sources* of the same finish signal without
re-architecting.

## 5. Inputs

Inputs v2 reads (read-only), beyond v1's set:

- **All v1 inputs** — salary, fight group / opponent, `scheduled_rounds`,
  odds-implied win probability via the projection-eligible `effective_status`
  predicate. **(Path correction from rev 1:)** that predicate is
  `is_projection_eligible_effective_status` in
  `src/ingestion/effective_status_resolver.py`, *consumed by*
  `src/projections/projection_input_service.py` (`aggregate_projection_inputs`).
- **A finish signal.** To avoid the rev-1 definitional bug (conflating
  `P(finish | win)` with an unconditional ITD number), v2 pins **one** canonical
  parameterization:
  - `p_fight_finishes` — **unconditional** probability the fight ends inside the
    distance (what books price as ITD, and what a league-average constant
    expresses). Tier 0: the pinned constant. Tier 1/2: per-fight input.
  - `finish_share` — given a finish, the probability **this** fighter is the
    finisher (defaults to splitting by relative win probability if unknown).
  - All four branch probabilities are **derived** from `(p_win,
    p_fight_finishes, finish_share)`, with a locked invariant
    `P(finish win) + P(decision win) = p_win` (and the loss side mirrors it).
    `finish_probability` is **never invented**: absent in a tier that needs it →
    Tier 0 fallback or a `missing_finish_signal` status (§9), exactly as v1
    refuses to guess win probability.
- **DK scoring constants** (static) — see §6. **The existing
  `src/config/scoring.py` is the home**, not a new file.

The projection input bundle (`ProjectionInputs`, returned by
`aggregate_projection_inputs`) is extended **additively** to carry the finish
signal; the bundle type is otherwise unchanged.

Inputs v2 must **not** read or fetch (unchanged from v1; restated because the
temptation is higher here): no UFCStats/Sherdog/scraped history (Tier 3, out of
scope); no remote props API or background fetch (Tier 2, if ever approved, is an
explicit user-triggered acquisition with its own design); no DraftKings site
state (ownership, contests, late swap).

## 6. The model

A new **pure** module (proposed `src/projections/finish_model.py`) computes, from
resolved inputs, an outcome-distribution value object. Pure-Python, no DB, no
Streamlit — mirroring `default_projection.py`.

### 6.1 Branch probabilities

From `(p_win, p_fight_finishes, finish_share)`:

```
p_finish_win    = p_fight_finishes * finish_share
p_decision_win  = p_win - p_finish_win
p_finish_loss   = p_fight_finishes * (1 - finish_share)
p_decision_loss = (1 - p_win) - p_finish_loss
```

with locked tests asserting all four are in `[0, 1]`, sum to 1, and that
`p_finish_win + p_decision_win == p_win` exactly. (Degenerate inputs — e.g.
`finish_share` so extreme that `p_decision_win` goes negative — must clamp with a
recorded warning, not silently.)

### 6.2 Per-branch expected points — the no-double-count invariant

```
E[pts | finish win]    = finish_resolution_bonus(finish_round_dist, rounds)
                         + accumulated_pts(style="finish",  expected_finish_length)
E[pts | decision win]  = WIN_DECISION
                         + accumulated_pts(style="decision", full_length(rounds))
E[pts | finish loss]   = accumulated_pts(style="finish_loss",  short_length)   # ~0
E[pts | decision loss] = accumulated_pts(style="decision_loss", full_length(rounds))
```

**The binding invariant (rev-1's blocker):** the finish branches' accumulated
points must be computed over the **expected finish length**, derived from the
**same** `finish_round_dist` that drives `finish_resolution_bonus` — **not** over
`scheduled_rounds`. A single shared latent fight-length governs both the bonus
and the accumulation so the two cannot be double-counted (big early-finish bonus
**and** a full-fight accumulation total). This is enforced by §12 tests, not
prose:

- `accumulated_pts(finish) < accumulated_pts(decision)` by a margin reflecting the
  round ratio;
- an **upper-bound test**: `E[pts | finish win] ≤ max_finish_bonus +
  accumulation_consistent_with_finish_length` (pins "no double-count");
- a later assumed finish round ⇒ **lower** bonus but **higher** accumulation (the
  trade-off is monotone).

### 6.3 Output: outcome branches are first-class (rev-1's other blocker)

The §17 sim lever needs the **per-branch parameters**, not collapsed scalars.
v2's output therefore exposes them as first-class:

- `outcome_branches` — a tuple of `(label, probability, expected_points)` for the
  four branches, summing to `P = 1`.
- `projected_dk_points` (mean) = `Σ probability · expected_points` — **derived**
  from the branches.
- `best_branch_pts` / `worst_branch_pts` — the **conditional mean of the
  best/worst branch** (typically finish-win / finish-loss). **These replace
  rev-1's "floor/ceiling as percentiles" framing, which was statistically
  dishonest for a 4-atom mixture** (a percentile of 4 point masses is unstable
  and discontinuous). They are explicitly *branch conditional means*, not
  calibrated quantiles. A true within-branch-variance percentile is deferred to
  the sim lever (§17), where it becomes well-defined.

`floor`/`ceiling` as *labels* may be retained in the output **only** if defined
as these branch values and documented as such — otherwise use the
`best/worst_branch_pts` names. The rev-1 test `floor ≤ mean ≤ ceiling always` is
**dropped**: it was trivially true only because branches were hand-ordered, and
it can fail once losses split (a high-volume decision loss can exceed a quiet
decision win).

### 6.4 Shared-fight-length caveat (joint model)

The model is **per-fighter** and treats the two fighters in a bout as
independent, but fight length is shared (if A finishes B in round 1, B's fight is
also one round). For v2's **mean**, this independence approximation is acceptable
and is stated as a non-claim. It becomes **first-order wrong** for the §17 sim,
which must reparameterize at the **fight (paired)** level — two independent
per-fighter branch sets are *not* a sufficient statistic for a coupled fight.
§17 records this so a future implementer does not wire the sim off these
parameters and inherit the coupling bug.

## 7. DK scoring constants — Phase 0 (audit + lock the EXISTING table)

**Rev-1 error:** the doc proposed *creating* `src/projections/dk_scoring.py`. A
scoring table **already exists** at `src/config/scoring.py` — currently
**unreferenced (dead) and untested**. Phase 0 is therefore **"audit, reconcile,
lock, and wire in,"** not "create."

The existing `src/config/scoring.py` holds: `SIG_STRIKE=0.5`, `ADVANCE=1.0`,
`TAKEDOWN=5.0`, `REVERSAL_SWEEP=5.0`, `KNOCKDOWN=10.0`, the round-win bonuses
`WIN_FIRST_ROUND=90 … WIN_DECISION=30`, and `QUICK_WIN_BONUS_R1=25`. Its own
docstring says "verify before use."

**The fact-check raised specific, unresolved discrepancies against current DK
Classic rules that Phase 0 MUST reconcile before any model code — do not assume
the existing constants are correct:**

1. **Significant strike value.** Existing file: `SIG_STRIKE=0.5`. Secondary
   sources (RotoWire, Establish The Run) suggest current DK Classic may score a
   significant strike at **+0.2**, *plus* a separate **Strike +0.2** (≈ +0.4
   effective). This is a 0.5-vs-0.4 *and* a single-vs-double-count question — a
   large striking-output swing. **Unverified; reconcile against the official
   page.**
2. **`ADVANCE`.** Existing file has `ADVANCE=1.0`. Secondary sources say
   "Advances" were **removed in the 2021 revamp**. If so, `ADVANCE` is a phantom
   constant that must be deleted, not modeled.
3. **Control time is missing entirely.** Current DK Classic scores **control
   time** (≈ **+0.03 per second** = +1.8/min) — one of the two largest
   accumulation drivers, and the dominant one for grappler/decision archetypes.
   Omitting it would systematically under-project grapplers and over-tilt toward
   strikers, defeating the model's purpose. Phase 0 must **add** a control-time
   constant (with an explicit per-second unit comment to avoid a 60× error).
4. **Quick-win bonus** (`QUICK_WIN_BONUS_R1=25`, additive on the R1 win, ≤60s) is
   real and present — the model's highest-ceiling atom — and must be carried into
   the finish-round distribution (§6), not averaged away.
5. **Round-win bonus shape.** `90/70/45/40/40/30` is **non-increasing, not
   strictly decreasing** — R4 and R5 are **tied at 40**. Pin the six exact values
   (the steps are irregular); do **not** encode a formula.

Phase 0 deliverables and provenance (strengthened — a self-locking test proves
the module matches *itself*, not that the values are *right*):

- The user (human-in-the-loop) sources the **official DK Classic MMA rules**
  (`https://www.draftkings.com/help/rules/mma`). **Warning: `pick6.draftkings.com`
  is a different product with different scoring — do not use it.** The official
  page is JS/bot-protected and resisted automated fetch during review, so this is
  a **manual** sourcing step.
- Reconcile the five items above; **cross-check each constant against a second
  statement** of DK Classic scoring (the locking test pins the *agreed* values,
  it does not validate them).
- Record the source URL + a `DK_SCORING_VERIFIED_ON` dated constant in the module
  docstring and surface it in the Phase 0 slice report (`docs/DEVELOPMENT_NOTES.md` §12). Add a
  standing reminder to re-verify periodically — **the locking test catches
  *edits*, never DK-side drift**, so do not claim it does.
- Add the locking test (mirrors `tests/test_projection_formula.py`). If a move
  from `src/config/` to `src/projections/` is wanted, treat it as a **rename**,
  not a new file, so two competing tables never coexist.

No model code lands until these constants are reconciled, dated, and locked.

## 8. Output shape

v2 extends the v1 value object **additively** — but note the v1 *code* does not
contain the fields rev 1 assumed:

- `fighter_id`, `slate_id` — unchanged.
- `projected_dk_points` — float, the §6 mean in v2 mode (or the v0 formula value
  in v0 mode, §10).
- `projection_status`, `missing_inputs` — unchanged enum plus the §9 tag.
- `outcome_branches` — **NEW.** The first-class four-branch
  `(label, probability, expected_points)` structure (§6.3).
- `best_branch_pts` / `worst_branch_pts` (or `floor`/`ceiling` if defined as
  branch values) — **NEW.** **Correction:** `PROJECTION_V1_DESIGN.md` §4
  *reserved* floor/ceiling, but the v1 **code never implemented them** —
  `ProjectionResult` (`src/projections/projection_service.py`) and
  `FighterSlateProjection` (`src/projections/slate_projection_service.py`) have no
  such fields today. v2 **adds** them; it does not "populate placeholders."
- `projection_mode` — **NEW** string: `"v0_formula"` or `"v2_finish"`, so no
  consumer silently mixes engines.

**Persistence.** Correction: a `projections` table **already exists** in
`src/db/schema.py` (columns `projection`, `value_gap_bonus`, `five_round_bonus`,
`source`, `created_at`) but is **currently unused by the read-path services**
(no `INSERT INTO projections`). Default remains **computed-on-read**. **Any** v2
column added to that table (`finish_signal`, `projection_mode`,
`outcome_branches` blob, `best/worst_branch_pts`) triggers the `docs/DEVELOPMENT_NOTES.md` §8
gate: a paired migration in `src/db/migrations.py` **and** a schema test. No
schema change ships without that gate; the default for v2 is no persistence until
validation (§12) justifies it.

## 9. Status / gating behavior

v2 keeps v1's "never silently invent inputs" contract and adds one condition,
**which only applies in Tier 1/2** (Tier 0's league-average constant is always
present, so there is no missing-finish-signal state under Tier 0):

| Condition | Status | `missing_inputs` tag | Behavior |
|-----------|--------|----------------------|----------|
| Tier 1/2 mode, no finish signal for the fight | `missing_inputs` | `"finish_signal"` | mean and branches = `None`. **Never** assume a finish rate, and **never** silently fall back to v0 inside a v2 result (mode is explicit, §8). |

**Optimizer pool-shrink consequence (must be surfaced, not latent):** under the
"emit `None`" policy, a fighter with a real win probability but a missing finish
signal projects `None`, and `pool_builder.py` **drops** `None`-projection
fighters from the pool (`OPTIMIZER_V1_DESIGN.md` §5.1). So turning on a Tier-1 v2
mode with incomplete finish entry can **silently shrink the optimizer pool below
6** and flip a solvable slate to `infeasible_pool_too_small`. The intended escape
hatch is the §16 Q2 "fill gaps with v0 (Tier 0)" opt-in — explicit, never silent.
All v1 status conditions and the `non_projectable` precedence carry over
unchanged.

## 10. Coexistence with the v0 formula (do not replace)

v2 honors `docs/DEVELOPMENT_NOTES.md` §4 by **not touching the v0 formula**: `default_projection`,
`value_gap_bonus`, `five_round_bonus` (`src/projections/default_projection.py`,
`value_bonus.py` — all verified to exist) are unchanged. v2 is a **parallel,
selectable engine** (`finish_model.py` + the §7 constants), tagged via
`projection_mode`. v2 is therefore an *additional* engine, **not** a change to the
§4 formula; v0 stays the **default** until v2 is validated (§12) and explicitly
promoted (a separate decision recorded in `CURRENT_STATUS.md`).

- **Homogeneous-pool invariant.** The optimizer is a mode-blind scalar consumer
  (it filters only on `projection_status == "ok"`). A single projection run /
  optimizer pool must be **all one mode**; mixing v0 and v2 rows is out of scope
  and is the **service's** responsibility (`project_slate` emits one
  `projection_mode` per run), not the optimizer's.
- **On dropping `value_gap_bonus`.** **Reworded from rev 1**, which over-claimed
  v2 "models the value-gap effect directly." It does not: v2's mean is
  **salary-independent** (a pure points estimate) and **delegates** value/leverage
  to the optimizer's salary-cap constraint (and a future ownership lever). That is
  a cleaner separation than baking a salary heuristic into the points number — but
  it removes the explicit cheap-but-live nudge v0 gave underdogs. §12 validation
  must confirm v2-mode lineups surface cheap-but-live underdogs at least as well
  as v0; until then, keep v0 intact as the default.

## 11. Robustness / overfitting risk (new — from the red-team)

v2 has **strictly more unvalidated free parameters** than v0: a finish signal × a
finish-round distribution × per-style accumulation constants, over a thin,
un-backtested local model. More free parameters ⇒ more variance and more ways to
be systematically wrong — concentrated in exactly the high-ceiling finishers the
optimizer will then over-select. v2 can therefore plausibly produce **less robust
lineups than v0**, especially via systematic over-projection of finishers.
Mitigations, all enforced elsewhere in this doc: Tier 0 first (no user-guess
input); the §6 no-double-count invariant; and the §12 validation gate, which must
show v2 does **not** increase projection-error variance vs v0 before v2 is
eligible for promotion. Until validated, the user should **not** trust v2 lineups
over v0.

## 12. Validation & calibration (new — a gating prerequisite, not an afterthought)

Rev 1's "real-feed validation" only checked that v2 numbers **differ** from v0 —
which is guaranteed by construction and proves nothing about accuracy. An "edge"
claim with no falsification path is not engineering. v2 therefore ships a
**minimal calibration harness** and makes it a **precondition for promotion**:

- A small, local, read-only harness to **hand-enter realized DK points** for a
  handful (~3–5) of *past* slates (fighter → actual DK score), stored locally
  (not committed — `docs/DEVELOPMENT_NOTES.md` §7), and to report **mean-absolute-error** of v0
  vs v2 per fighter, plus a check that v2 does not inflate error variance (§11).
- This harness is its **own slice** and is a **gate**: v2 is **not eligible** to
  become the default projection mode until the harness shows v2 is at least
  competitive with v0 on realized points. Promotion remains a separate, explicit
  user decision on top of passing this gate.
- Explicit non-claim retained in §1: v2 is not validated to outperform v0; this
  design *builds the means* to find out, and refuses to assert "edge" until it
  does.

Standard read-only test coverage (per `docs/DEVELOPMENT_NOTES.md` §8) still applies to every
phase; note v2 is a projection **engine**, not an importer, so the §8
*importer* real-file gate does not bind it — v2 ingests nothing new from a feed.
Its real-data validation is the model-sanity + calibration step above.

### 12.1 Phase A′ — locked calibration decisions

These resolve the A′ open questions listed in `CURRENT_STATUS.md` (the harness was
inspected/planned but blocked on them). They pin the harness contract; the harness
stays **read-only, pure, no schema / UI / service wiring** — two new files,
`src/projections/mae_calibration.py` + `tests/test_mae_calibration.py`. None of
this promotes v2 or changes the §4 v0 formula: v0 remains the default and promotion
stays a separate, explicit user decision (§12).

- **D1 — Error-variance gate (tolerance).** Define the per-row **residual** as
  `projection − realized_dk_points` (signed). Pool residuals across all included
  rows (D3). The gate statistic is
  `variance_ratio = Var(residual_v2) / Var(residual_v0)`, using **sample variance
  (ddof = 1)** over the pooled residuals. **v2 passes the variance arm iff
  `variance_ratio <= 1.05`** — v2 may not inflate pooled error variance by more
  than 5% (realizes the §11 mitigation). The harness reports `variance_ratio` and a
  `passes_variance_gate` boolean.
- **D2 — MAE mode (pooled-first; per-slate diagnostic) + hard MAE gate.** Report
  **both** a pooled MAE and a per-slate MAE for each engine.
  - **Pooled MAE** = mean of `|residual|` over all included rows (all slates
    flattened). This is the **gating lens**.
  - **Per-slate MAE** = mean of `|residual|` within each slate, emitted as a
    per-slate table. **Diagnostic only** (surfaces a slate where one engine is
    pathological); it does **not** gate.
  The harness emits `mae_v0_pooled`, `mae_v2_pooled`, their delta (always — pass or
  fail), and `mae_v2_le_v0` (is `pooled_mae_v2 <= pooled_mae_v0`). The MAE arm is a
  **hard gate**: if v2's pooled MAE is worse, the MAE arm fails. There is **no
  subjective MAE allowance**.
- **D2′ — Combined hard A′ gate.** **A′ passes iff
  `pooled_mae_v2 <= pooled_mae_v0` AND `variance_ratio <= 1.05`** (D1). Both arms
  are objective and binding. A **failing** A′ **blocks promotion**: v2 must **not**
  be promoted if it loses on pooled MAE or inflates error variance past tolerance.
  A **passing** A′ is a **necessary precondition**, not an auto-promotion — the
  actual switch of the default mode to v2 remains a separate, explicit user step
  (§12), but it may only be taken on a passing gate. The harness reports the delta
  **either way** so a fail is fully legible.
- **D3 — Missing-row policy (apples-to-apples; skip-unless-both).** A CSV row is
  **included** only when **all** hold:
  - v0 yields a valid projection — `implied_win_probability ∈ [0, 1]`, `salary`
    present/numeric, `scheduled_rounds` present;
  - v2 yields `projection_status == "ok"` — `implied_win_probability ∈ [0, 1]` and
    `scheduled_rounds ∈ {3, 5}` (Tier 0 supplies the `p_fight_finishes` /
    `finish_share` defaults, so no extra inputs are required);
  - `realized_dk_points` is present/numeric.

  The binding net predicate is therefore: `implied_win_probability ∈ [0, 1]`,
  `salary` numeric, `scheduled_rounds ∈ {3, 5}`, `realized_dk_points` numeric. Any
  row failing it is **skipped, counted, and reported with a reason** — never
  silently dropped. Both engines are scored on the **same** included set.
- **D4 — Input format (CSV; committed template only).** Input is **CSV, not JSON**.
  Required header:
  `slate, fighter, implied_win_probability, salary, scheduled_rounds, realized_dk_points`.
  `slate` keys the per-slate breakdown; `fighter` is for join/debug and
  skip-reason readability. Optional finish-signal columns (`p_fight_finishes`,
  `finish_share`) **may** appear but are **ignored at Tier 0** (the model uses
  league defaults); they are reserved for a future Tier-1 calibration. **Only a
  committed synthetic sample/template** — the header plus 1–2 **synthetic**
  illustrative rows (no real results) — is checked in.

  **Locations (finalized):**
  - **Real calibration data:** `data/calibration/realized_points.csv` — **must
    remain gitignored / local**, never committed (`docs/DEVELOPMENT_NOTES.md` §7).
  - **Committed synthetic sample:** `data/calibration/realized_points.sample.csv`.
    `data/` is **not** broadly ignored today (only `data/database`,
    `data/uploads/*`, `data/raw`, `data/processed`, `data/uploads/sources`), so a
    sample under a new `data/calibration/` dir is committable. The implementation
    slice must, **in the same change**, add a `.gitignore` rule that ignores the
    real CSV while keeping the sample (e.g. `data/calibration/*.csv` +
    `!data/calibration/realized_points.sample.csv`, plus a `.gitkeep`) —
    `docs/DEVELOPMENT_NOTES.md` §7 forbids a new data dir without a paired `.gitignore` update.
  - **Fallback** if those `data/` ignore rules prove awkward: commit the synthetic
    fixture at `tests/fixtures/realized_points_sample.csv` and **document** the real
    local path (`data/calibration/realized_points.csv`) in the module/test docstring
    instead. Either committed-sample home is acceptable; the real CSV path and its
    gitignored status are fixed.
- **D5 — Invocation (pure module first).** A′ ships as a **pure module +
  pytest-covered functions only**: `src/projections/mae_calibration.py` exposing
  (a) a CSV loader/validator returning typed rows + skip records, and (b) a pure
  comparator that runs both engines over the included rows and returns the
  structured report below. `tests/test_mae_calibration.py` covers the MAE math,
  the skip policy, the variance-gate boundary, and degenerate inputs. An optional
  thin CLI / `__main__` wrapper is **deferred** and **not required for A′**.

**Output contract.** A frozen `CalibrationReport` dataclass carrying:
`mae_v0_pooled`, `mae_v2_pooled`, `mae_delta` (v2 − v0), `mae_v2_le_v0`,
`variance_ratio`, `passes_variance_gate`, **`passes_gate`** (the D2′ combined hard
gate = `mae_v2_le_v0 AND passes_variance_gate`), `per_slate` (slate →
`{mae_v0, mae_v2, n}`), `n_included`, `n_skipped`, and `skipped` (per-row reason
records). It is **data only** — no printing, no I/O, no promotion side effect.

## 13. Interaction with existing systems

Read direction only — v2 mutates nothing (same posture as v1):

- **Projection input aggregator** (`aggregate_projection_inputs`) → supplies
  salary, rounds, opponent, projection-eligible win probability; v2 adds the
  finish signal additively to the bundle.
- **Slate projection service** (`project_slate` /
  `slate_projection_service.py`) → gains a `projection_mode` parameter; in v2 mode
  calls `finish_model` and emits the §8 shape with one mode per run (§10).
- **Optimizer** (`pool_builder.py`, `lineup_solver.py`) → **unchanged consumer**;
  reads the scalar mean, filters `projection_status == "ok"`. v2's branches /
  best/worst-branch fields are **ignored** until a future optimizer design opts
  in. The §9 pool-shrink interaction is cross-referenced to
  `OPTIMIZER_V1_DESIGN.md` §5.1.
- **Manual Review gate** (`OPTIMIZER_V1_DESIGN.md` §4) → **open interaction
  (§16 Q3):** a Tier-1 finish-signal entry is a new user input that materially
  changes projections; the design should decide whether entering/editing it
  **invalidates the review ack** (recommend yes, or at least raise a staleness
  flag), and whether a "win-prob present / finish-signal missing" fighter surfaces
  as a **mismatch-alert / review item** rather than silently dropping. Not "out of
  scope" — the gate's job is to keep unreviewed inputs out of the optimizer.
- **`finish_signal` is a projection INPUT, not an `effective_status`/match
  override** — it is outside the D.5 override framework and the `docs/DEVELOPMENT_NOTES.md` §10
  "undesigned override types" gate. But as a user-entered (Tier 1) value it still
  inherits the §11/§14 UI write-action discipline.
- **Alerts / exports** → not collaborators; out of scope.

## 14. UI write-action discipline (Tier 1 only)

A Tier-1 finish-signal **write** must satisfy `docs/DEVELOPMENT_NOTES.md` §11 in full, and rev 1
under-specified this:

- It requires its **own repository method** — do **not** overload
  `OddsRowRepository` / `odds_rows` (the manual-moneyline home in
  `src/ingestion/manual_odds.py`); a finish signal is not a moneyline.
- It must have an **Undo / supersede / overwrite-in-place** story spelled out in
  the design **before** implementation (`docs/DEVELOPMENT_NOTES.md` §11 forbids a write with no
  supersede story unless the design explicitly accepts it).
- The write runs in a single page-owned transaction, is AppTest-covered asserting
  persisted state, and is idempotent on re-click where designed.
- **Tier 0 needs none of this** (no user input). **Phase B as scoped authorizes
  only a session-only, non-persisted Tier-1 input**; any *persisted* finish signal
  is a separate, design-gated slice that also triggers the §8 schema gate.

## 15. Implementation phase split

All slices are **future work**. Tier 0 phases need normal design approval (this
doc); **Tier 1/2 phases additionally need the §4 scope sign-off recorded in
`CURRENT_STATUS.md` before they may begin — Phase 0 included for Tier 1/2, but
Phase 0 itself is Tier-0/in-scope.** Each phase is its own slice and its own
commit (`docs/DEVELOPMENT_NOTES.md` §13). **Phase 0 is the first independently-shippable,
reviewable slice (constants + locking test, no user-visible behavior); no
user-visible v2 projection exists until Phase D.** Phases A/A′/B are internal-only
increments.

- **Phase 0 — Scoring constants audit + lock (Tier 0, in-scope).** Reconcile
  `src/config/scoring.py` against the official DK Classic rules (§7), add control
  time, delete `ADVANCE` if confirmed removed, date + cross-check + lock with a
  test, wire it in. Pure data + test.
- **Phase A — Pure finish model + tests (Tier 0).** `finish_model.py`: the §6
  four-branch decomposition driven by `(p_win, p_fight_finishes=league_const,
  finish_share)` → `(outcome_branches, mean, best/worst_branch_pts)`. Pure-Python,
  no DB/UI. Tests include the §6 invariants and the §16 degenerate cases.
- **Phase A′ — Calibration harness + first comparison (Tier 0, gating).** The §12
  MAE-vs-v0 harness over a few past slates; **locked decisions in §12.1.** **Gate:**
  the result decides whether v2 is worth carrying further. A throwaway spike
  (existing `scoring.py` + league constant over one real slate, eyeballed vs
  v0/actual) may precede Phase 0 to cheaply test the premise before the ceremony.
- **Phase B — (Tier 1, §4-gated) finish-signal input plumbing.** Read-side bundle
  extension + the §14 input control. Session-only by default; persisted variant is
  a separate §8/§11-gated slice.
- **Phase C — v2 mode in the slate projection service.** Glue 0+A(+B): per-fighter
  v2 rows with `projection_mode`, branches, and the §9 missing-signal handling.
  No optimizer change.
- **Phase D — UI: mode toggle + v2 columns.** Read-only view of mode, mean,
  best/worst branch, branches, finish signal, status. AppTest pins rendered text
  (`docs/DEVELOPMENT_NOTES.md` §11).
- **Phase E — (separate designs) downstream.** Optimizer/sim/ownership/exports
  consumption of v2 — not in this design; each needs its own approval.

## 16. Open questions (revised)

1. **Finish-signal granularity (Tier 1/2).** Per-**fight** `p_fight_finishes` +
   `finish_share` (recommended — matches how books price ITD and halves entry
   burden) vs per-**fighter** numbers (more expressive, more burden). Rev-1
   recommended per-fighter; **flipped to per-fight.**
2. **Missing-signal service policy.** Emit `missing_inputs:"finish_signal"` with a
   `None` v2 number (honest; but shrinks the optimizer pool, §9), or substitute a
   **Tier-0** row for that fighter so the optimizer still sees a scalar?
   *Recommendation:* honest `None` by default, with an explicit "fill gaps with
   Tier 0" opt-in — never silent.
3. **Manual Review gate interaction.** Does entering/editing a finish signal
   invalidate the review ack or raise a staleness flag (§13)? *Recommendation:*
   invalidate-or-flag; do not let a projection-changing input bypass review.
4. **Persistence.** Computed-on-read (default) vs persist into the existing
   `projections` table (triggers the §8 migration + schema test gate, §8).
   *Recommendation:* session-only / computed-on-read until §12 validation
   justifies persistence.
5. **Accumulation coarseness.** Per-style constants (transparent, testable) vs a
   small function of `p_win`. *Recommendation:* constants first; refine only with
   evidence from §12.
6. **League-average finish rate value + 5-round handling.** The exact Tier-0
   constant (~0.55–0.60 ITD) must be sourced and pinned like the scoring table.
   Separately, confirm via test that a 5-round fighter outprojects the same
   3-round fighter at equal inputs, and that the effect is driven by
   **accumulation** (more rounds), **not** a higher finish bonus — recall R4/R5
   bonuses (40/40) are *lower* than R2 (70).

## 17. Connection to the later levers (context, not scope)

This design is a candidate **lever #1**, deliberately scoped to be useful and
validatable on its own. None of this section is in scope here.

- **Monte-Carlo / ceiling-aware optimizer (lever #2)** consumes v2's
  **`outcome_branches`** (the first-class per-branch probabilities + point values
  from §6.3 — *this* is the field it reads, not the collapsed mean/floor/ceiling).
  **Two prerequisites the sim must add, not inherit:** (a) **intra-branch
  variance** — without it every simulated slate collapses to four point masses per
  fighter; v2 emits branch *means*, and the sim needs a within-branch spread
  model; (b) **fight-level (paired) reparameterization** — per §6.4, two
  independent per-fighter branch sets are not a valid joint fight model (they would
  double-count shared fight time, e.g. both fighters logging full-fight control in
  the same bout). Separate `OPTIMIZER_V*` design; needs v2 first.
- **Ownership / leverage (lever #3)** is orthogonal to projections — its own
  subsystem and design.
- **Win-probability calibration** (a cheap, v0-lifting alternative first lever,
  §3) is also its own design and is **not** subsumed by v2.

Recording these dependencies makes sequencing explicit and prevents a future
author from wiring the sim off an insufficient statistic.
