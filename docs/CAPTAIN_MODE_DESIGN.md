# Captain Mode + Pluggable Build Methods — Design (C0)

Status: **design only.** No app/src/test code in this slice. No schema change.
This doc does **not** edit `docs/DEVELOPMENT_NOTES.md`; it *proposes* the scope change (§2)
to be applied as its own reviewed commit. Read-only inspection only.

Companion to `docs/DEVELOPMENT_NOTES.md` §1–§3 (scope), §9 (design-before-implementation),
§11 (UI write rules), and: `TWO_STEP_BUILDER_PRODUCTION_DESIGN.md` (the
streamlined Classic flow this extends), `PROJECTION_V1_DESIGN.md` (the
heuristic projection that becomes one selectable *method*),
`OPTIMIZER_V1_DESIGN.md` (the Classic solver Captain sits beside),
`MANUAL_REVIEW_GATE_V1_DESIGN.md` (the gate both formats keep).

---

## 0. One-paragraph summary

Add **DK UFC Captain (Showdown)** as a second contest format *inside* the
existing app, **additively** — Classic is untouched, Captain lives in its own
modules, and a thin **contest router** (Classic | Captain) sits on top. At the
same time, make the **projection/build step a pluggable method** so the
current **Heuristic** engine and a future **Monte Carlo** engine coexist and
are selectable — **nothing is deleted**, so the two can be compared and chosen
between over time. A method-agnostic **paste-in lineup evaluator** (drop in any
lineup → Monte Carlo distribution) is a future surface that works regardless of
how the lineup was built.

---

## 1. Scope expansion (proposed `docs/DEVELOPMENT_NOTES.md` change)

The repo is currently **Classic-only** and lists Captain as out of scope. The
user has approved widening the charter. Proposed edits, to land as their **own
separate, reviewed commit** (not in this doc):

- **§1 Project identity** — change "UFC DraftKings **Classic** contests" to
  "UFC DraftKings **Classic and Captain (Showdown)** contests."
- **§2 v0 scope** — add: "DK UFC **Captain (Showdown)** contests — an additive
  second format behind a contest selector; see `docs/CAPTAIN_MODE_DESIGN.md`.
  Classic behavior is unchanged."
- **§3 Out of scope** — remove "DK Showdown / Captain" from the out-of-scope
  list; **keep Pick6 out**. New wording: "DK **Pick6** format (still out).
  DK Captain/Showdown is now **in scope** under `CAPTAIN_MODE_DESIGN.md`,
  built additively."

Everything else in `docs/DEVELOPMENT_NOTES.md` (Classic projection formula §4, git rules,
data-safety, testing, design-first) stays exactly as is and applies to Captain
too.

---

## 2. Product shape — contest router

A single selector at the top of the workflow: **Classic** | **Captain**.

- **Classic →** the existing flow / two-step builder, byte-for-byte unchanged.
- **Captain →** a parallel section with its own salary parsing, optimizer,
  valuation, and UI — but reusing the shared core (odds entry/matching,
  projection inputs, the Manual Review gate, the UI shell).

The router is a thin layer; it must not alter any existing Classic code path.

---

## 3. Additive architecture (protect the working Classic app)

Reuse is high — most of the engine is format-agnostic:

| Layer | Shared with Classic? | Captain-specific work |
| --- | --- | --- |
| Salary import | Mostly — same CSV columns | Captain CSV has CPT/F duplicate rows (§5) |
| Odds entry + matching | **Yes, unchanged** | none |
| Implied win prob | **Yes, unchanged** | none |
| Base projection (heuristic) | **Yes** (becomes a *method*, §7) | captain valuation / finish equity |
| Manual Review gate | **Yes, unchanged** | none (same `evaluate_manual_review`) |
| Optimizer | **No** | new Captain solver (§6) |
| Reasoning | shared shape | captain-specific drivers |
| UI shell / command center | **Yes** | a Captain section + router |

Rules to keep Classic safe:

- Captain code lives in **new modules** (e.g. `src/captain/`), never inside
  Classic modules. The Classic optimizer, projection, and pages are not edited
  beyond the additive router hook.
- No change to Classic's tests, projection coefficients (`docs/DEVELOPMENT_NOTES.md` §4), or
  the gate's check set.
- The contest router is additive: with no Captain slate selected, the app
  behaves exactly as today.

---

## 4. The gate stays intact for both formats

Captain uses the **same** `evaluate_manual_review(conn, slate_id)` →
`ReviewReadiness` gate, with the same Blocked / Warning / Ready semantics the
Classic two-step builder uses. `run_optimizer`'s defense-in-depth gate is
mirrored by the Captain solver: **no lineup builds until the slate is clean and
explicitly marked reviewed.** No gate logic is removed or weakened for Captain.

**C5 MVP note (decided at C5).** The full `evaluate_manual_review` gate reads
*persisted* slate odds/match data, which requires Captain persistence
(deferred — §13 **C6**). So the **C5 MVP** ships a **self-contained, read-only**
readiness in the gate's **spirit**: upload the Captain CSV → manual moneyline
entry → de-vig (reuse `implied_probability`) → an explicit "review & build"
acknowledgement, with **no build until the user acknowledges**. The gate
philosophy (explicit human sign-off before lineups) is preserved and never
weakened; C5 writes **nothing** to the DB (read-only, like the two-step builder
prototype). Wiring the real `evaluate_manual_review` gate + Blocked/Warning/Ready
checks is the C6 persistence slice.

---

## 5. Captain data contract (salary CSV)

DK Captain salary CSVs use the same columns as Classic but list **each fighter
twice**:

- a `Roster Position = CPT` row whose `Salary` is **1.5×** the base, and
- a `Roster Position = F` row at the base salary.

Parser (new, pure, `src/captain/`): collapse the two rows to **one fighter
record** carrying `base_salary` and `captain_salary`; pair opponents via
`Game Info` (`Opp@Fighter …`); fighters flagged `O` (out) are excluded /
zeroed. Scheduled rounds are not in the file — the user flags 5-round bouts
(title/main events); default 3.

No new table or schema is required to *parse/preview*; persistence (if any)
reuses the existing additive `source`-tagged pattern and is its own later,
gated slice — not part of C0.

---

## 6. Captain optimizer (new, beside Classic)

Hard rules from the DK Captain ruleset:

- Lineup = **6 fighters: 1 Captain + 5 Fighters**, salary cap **$50,000**.
- Captain earns **1.5× points** and costs **captain_salary** (the 1.5× row).
- **No same-fight exclusion** — both fighters of a bout *may* be rostered
  (unlike Classic). This is a real structural difference.

Implementation: a brute-force search is sufficient (pool ≈ 14): all 6-fighter
combinations × each captain choice, cost ≤ \$50k, maximize total; return the
top N. It is a **separate solver** from the Classic PuLP path; the Classic
solver is untouched.

---

## 7. Pluggable build methods — nothing is deleted

The projection/build step is an **interface**, not a hardwired formula. Two
engines share the same input contract (odds + salary + rounds) and output
contract (per-fighter projection → lineups):

- **Heuristic** — the existing `default_projection`
  (`win_prob×70 + value_gap_bonus + five_round_bonus`). Fast, transparent,
  deterministic. The **default** method.
- **Finish-aware v2 (Experimental)** — the existing
  `finish_model.compute_finish_projection`: expected DK points across four
  outcomes (finish/decision × win/loss), pricing the **real** locked DK scoring
  table weighted by finish probability. Registered as a second selectable
  Captain method (§13 **C7**). **Unvalidated:** its constants are explicit
  assumptions and the Phase-A′ MAE gate has **never been evaluated against
  realized DK points** (no results data — `realized_dk_points` is unfilled), so
  it is labeled **Experimental** and is **never the default**. **SUPERSEDED &
  RETIRED (§14):** the MOV finish-aware method prices a *real per-fighter*
  finish signal from method-of-victory odds, whereas v2 used a league-average
  `finish_share = win_prob` that can't tell a finisher from a decision machine.
  The C7 Captain-side v2 *method* is retired; the `finish_model` *module* is
  kept as the scaffold the Tier-2 market-implied method (§14) feeds.
- **Monte Carlo** *(future)* — simulates fight outcomes (round, method) from
  the probabilities and returns a score **distribution**. Heavier, assumption-
  driven, but captures variance/ceiling — which matters most in Captain mode.

**Phase-1 finding (why this shape).** Swapping the *mean* projection (v0 → v2)
does **not** change the captain pick on a salary-capped slate: captaining the
cheapest strong fighter to free cap is correct **mean-EV** behavior, not a
projection flaw, and both engines agree on fighter ordering. "Captain a
finish-likely favorite" is a **ceiling / tournament** objective — it belongs to
the Monte-Carlo **M-track** (which consumes v2's outcome branches), not to
swapping the mean engine. So v2 is registered for *accuracy* and as the basis
for the M-track, not as a captain-pick fix.

A **method selector** in the UI chooses the engine per build. **Neither method
is ever removed** — keeping both is the whole point: it preserves the ability
to *choose* per slate and to *compare* the methods over time (§9). The Captain
section is built against this interface **from day one** (with Heuristic as the
first and only implementation initially), so adding Monte Carlo later just
registers a second engine — no rewrite, no deletion.

> Correction to an earlier framing: Monte Carlo does **not** replace the
> heuristic. They coexist as selectable methods.

---

## 8. Monte Carlo (future track) — two distinct surfaces

Both are explicitly **future** (after the Captain section ships). The C0
architecture only commits to *not blocking* them.

1. **MC as a build engine** — registered behind the §7 method interface;
   builds lineups by optimizing on simulated outcomes (e.g. ceiling / win-rate
   / mean), for **either** Classic or Captain.
2. **MC as a paste-in evaluator (method-agnostic)** — a surface where you drop
   in *any* lineup (heuristic-built, MC-built, or hand-made) and it runs the
   sims and returns a distribution (mean / floor / ceiling / win-rate / bust-%)
   so you can decide what to enter. Because it scores an arbitrary lineup, it is
   the natural **A/B comparison tool** for the two build methods.

**Determinism:** Monte Carlo is **seeded** — same inputs + same seed → same
sims → same numbers. That keeps it auditable and keeps method comparisons fair
(comparing methods, not random noise), consistent with the app's deterministic,
fact-backed posture.

---

## 9. Comparing methods "over time" — honest dependency

Truly judging *which* method is better long-term requires scoring lineups
against **actual fight results** — a results-ingestion / backtest layer the app
deliberately does **not** have yet (`EXPORT_RUN_LOG_V1_DESIGN.md` §1.1 — "no
actual-results import"). So:

- Keeping both methods (and the paste-in evaluator) is the right **foundation**
  and lets you choose per slate and eyeball them side by side **today**.
- **Auto-grading "which wins over time"** is a separate future track (results
  logging + backtest). This doc does not build it; it just keeps the door open
  by making methods selectable and lineups evaluable.

We should not over-promise the "test over time" outcome on day one — it needs
that results layer to be rigorous.

## 10. Reasoning (both formats, both methods)

Deterministic and fact-backed, as Classic already is:

- **Heuristic** reasoning cites win prob, projection components, value/5-round
  bonuses, captain leverage, exclusions, constraint satisfaction.
- **Monte Carlo** reasoning cites the simulated distribution (mean / ceiling /
  win-rate) and the seed.
- Neither may invent a finish / KO / "lock" / predicted winner beyond the
  numbers supplied (odds-derived, or sim-derived under a stated seed).

---

## 11. What stays unchanged

Classic's projection formula and coefficients (`docs/DEVELOPMENT_NOTES.md` §4), the Classic
optimizer and its validation, odds matching, the salary importer, the Manual
Review gate and `evaluate_manual_review`, all repositories, and the DB schema.
Captain is additive; Classic AppTests are untouched.

---

## 12. Risks / open questions

1. **Scope-charter change** is real — `docs/DEVELOPMENT_NOTES.md` §1–§3 must be updated in its
   own reviewed commit before Captain code lands (design-first, §9).
2. **Keeping Classic safe** — Captain must be new modules + a thin router; any
   edit that reaches into Classic paths is a red flag.
3. **Captain valuation (finish equity)** is still being locked in Phase 1; the
   method interface lets it slot in without reshaping the optimizer.
4. **Monte Carlo assumptions** — finish/method distributions need defensible,
   stated inputs; seeded for reproducibility; clearly labeled as model output.
5. **"Which method is better over time"** depends on a results/backtest layer
   that doesn't exist yet (§9) — don't over-promise it.
6. **Captain persistence** (saving Captain odds/lineups) is deferred and, if
   added, reuses the existing additive `source`-tagged pattern — its own slice.

---

## 13. Slice plan (additive, design-first)

- **C0 — this design** + the `docs/DEVELOPMENT_NOTES.md` scope edit (separate reviewed commit).
- **C1 — contest router** (Classic | Captain); Classic untouched; AppTest that
  Classic still renders unchanged.
- **C2 — Captain salary CSV parser** (CPT/F collapse, pairing, out-flag) + unit
  tests.
- **C3 — Captain optimizer** (1 CPT×1.5 + 5, \$50k, no same-fight exclusion,
  brute force, top N) + unit tests.
- **C4 — method interface** (`build_method`) with **Heuristic** as first impl
  (reuses `default_projection`; the Captain **finish-equity valuation is
  deferred** to the Phase-1 pass — likely registering the existing Classic
  *finish-aware projection v2* as a second method) + tests.
- **C5 — Captain UI section — self-contained read-only MVP** (upload Captain
  CSV → manual moneyline entry / de-vig → explicit **review-&-build** readiness
  in the gate's spirit → lineups + deterministic reasoning) reusing the shared
  core + AppTest. **No DB writes** (see §4 C5 MVP note).
- **C6 (deferred) — Captain persistence + full `evaluate_manual_review` gate
  integration** (its own design/persistence slice; reuses the additive
  `source`-tagged odds pattern). Promotes C5's read-only readiness to the real
  Blocked/Warning/Ready gate.
- **C7 — register finish-aware v2 as a second selectable Captain method**
  (wraps `finish_model.compute_finish_projection` behind the C4 interface) + a
  UI **method selector** (Heuristic *default* | Finish-aware *Experimental*) +
  reasoning that names the method + tests. Additive; v2 labeled **Experimental**
  (unvalidated — §7). Independent of the deferred C6.
- **C8 — Captain consensus odds input** (replaces the C5 manual-entry-only path).
  Paste a **BestFightOdds** block and/or a **DraftKings / multi-book** paste →
  **reuse** the existing validated Classic odds modules
  (`providers.bestfightodds.parse_bestfightodds_all_books`,
  `providers.multi_book_paste.parse_multi_book_paste`,
  `consensus_assembly.merge_sources` / `assemble_fights`,
  `odds_consensus.compute_slate_consensus`) to compute **de-vigged median
  consensus** win probabilities per fighter, mapped to the Captain fighters by
  `normalize_name`, and feed them into the build. **Manual entry stays as a
  fallback / override** for any fighter the consensus didn't price. Read-only:
  **no network fetch** (paste only — the BFO *fetcher* is out), **no DB writes**.
  Surfaces `low_confidence` / `unpaired` / book `dispersion`. Reuses Classic
  modules by **import, never edit** (§3). **Prioritized ahead of M1 / C6** —
  it's a real usability blocker surfaced by live testing. Within `docs/DEVELOPMENT_NOTES.md` §2's
  approved "paste/table parser" odds path.
- **C9 — method-of-victory (MOV) odds input + finish signal** (§14.1): paste
  KO/Sub/Dec per fighter → de-vig the 6 per-bout outcomes →
  `finish_signal = P(win inside distance)`; fallback ladder (go-the-distance →
  round-total Under), record the tier used. Pure, read-only, manual paste + tests.
- **C10 — MOV finish-aware method + retire v2** (§14.2 / §14.6): new
  `adjProj = baseProj + K·finish_signal` (K default 20) registered as the
  **Finish-aware** method; **remove** the C7 Captain-side v2 method + its UI
  option + tests (keep the `finish_model` module). Heuristic stays default;
  anchor at K=0 ≡ heuristic. + tests.
- **C11 — optimizer stack toggle + captain-leverage rule** (§14.3 / §14.4):
  `stack_mode` (GPP = reject same-fight pairs, tournament default | cash =
  allow, current); captain selection ranks by `CPTproj = 1.5·adjProj` (default
  top, expose ranked list — **not** a win% floor). Encode **both** GPP fixtures
  (§14.5) + UI wiring + tests.
- **C12 (future, Tier 2) — market-implied projection** (§14.7): feed
  Round-and-Method + Sig-Strikes + Takedowns props into the `finish_model`
  structure → real finish bonus by round + real activity, removing `K` and the
  assumed constants. Control time / knockdowns stay unpriced (no prop).
- **M1 (future) — Monte Carlo build engine** registered behind the C4 interface
  (Classic + Captain).
- **M2 (future) — paste-in MC evaluator** (method-agnostic lineup scoring).
- **R1 (future) — results / backtest layer** to actually grade methods over
  time (§9).

Each code slice ships its tests in the same slice (`docs/DEVELOPMENT_NOTES.md` §8); Pick6,
other sports, scraping/API, live betting/auto-entry, and results ingestion
(until R1) remain out of scope.

---

## 14. Finish-aware (MOV) method, stack toggle, captain leverage

Originated in a parallel analyst session, reconciled and verified here against
the optimizer. Deterministic; every term traces to a pasted number; manual
paste only, no network. Additive — the Heuristic stays the default and is
byte-for-byte unchanged.

### 14.1 Finish signal (from method-of-victory odds)
Paste per-fighter KO/TKO, Submission, Decision American odds. Per bout {A,B}
take the six outcomes, convert each to implied prob, `S = sum of all 6`, and
```
finish_signal(f) = (implied(KO_f) + implied(Sub_f)) / S   # de-vigged P(f wins inside distance)
```
Fallback ladder when a bout lacks the method tree: (1) "fight to go the
distance: No" (de-vig vs Yes) × win_prob, then (2) round-total Under (de-vig vs
Over) × win_prob. **Record which tier each bout used and surface it.** This
ladder already beats the retired v2 league-average model (§14.6).

### 14.2 Finish-adjusted projection
`win_prob` stays the **moneyline** de-vig (NOT method-implied — preserves the v0
base and the 294.3 anchor). `baseProj` is unchanged (`docs/DEVELOPMENT_NOTES.md` §4).
```
finish_bonus(f) = K * finish_signal(f)      # K default 20, a single editable, UNVALIDATED knob
adjProj(f)      = baseProj(f) + finish_bonus(f)
CPTproj(f)      = 1.5 * adjProj(f)
```
Registered as the **Finish-aware** method (Heuristic stays default). `K = 0`
reproduces the Heuristic exactly (regression anchor).

### 14.3 Stack toggle (optimizer)
`stack_mode`: **GPP** rejects any lineup with both fighters of a bout (no
same-fight pairs; the tournament default) · **cash** allows them (the current
C3 behavior). Additive parameter on the existing optimizer.

### 14.4 Captain-leverage rule (the missing spec piece)
Pure EV in GPP captains the *cheapest* viable fighter (it frees salary), not the
finisher. To play the finish-favorite captain, add an explicit rule: **rank
captains by `CPTproj = 1.5·adjProj`, default to the top, and expose the ranked
list** for pivots. A simple win% floor does **not** reproduce it (at θ=0.55 the
EV-max eligible captain is Nickal, who's cheaper) — the `CPTproj` ranking is
required. Conflict is **GPP-only**: in cash the EV optimum already captains the
favorite.

### 14.5 Verified fixtures (reference slate, K=20)
- **Anchor** (Heuristic ≡ Finish-aware at K=0): CPT Gane/Pereira ≈ 294.3
  ($49.5–49.6k); CPT-Topuria ≈ 291.9.
- **Finish signals:** Topuria 72.2%, Hokit 67.2%, Ruffy 63.9%, Nickal 45.8%,
  Lopes 40.4%, Pereira 37.3%, O'Malley 34.9%, Gane 26.6%.
- **adjProj:** Topuria 76.84, Ruffy 69.07, Hokit 67.44, O'Malley 61.71, Nickal
  59.70, Pereira 57.16, Gane 55.62.
- **GPP — pure EV (no captain rule):** CPT **Garcia 331.37**, $49,700.
- **GPP — captain-leverage (CPTproj top):** CPT **Topuria 323.96**, $50,000.
- **Cash optimum:** CPT **Topuria 347.79**, $50,000.

Encode **both** GPP fixtures, not one. (Independently reproduced here:
GPP captain ranking is Garcia 331.37 > Pereira 326.90 > Gaethje 326.53 >
Gane 324.59 > Nickal 324.58 > Topuria 323.96.)

### 14.6 v2 retirement
The C7 Captain-side **Finish-aware v2** method is **retired** — the MOV signal
prices a real per-fighter finish, where v2's `finish_share = win_prob` gave
identical treatment to a finisher and a decision machine. The `finish_model`
*module* is **kept** as the scaffold for §14.7.

### 14.7 Tier 2 (future) — market-implied projection
Available props per fight: Fight Lines, Method of Victory, **Round & Method of
Victory**, Fight to go the Distance, **Significant Strikes Landed (O/U)**,
**Takedowns Landed (O/U)**. These replace the assumed constants:
- Round & Method → **real expected finish bonus = Σ P(win in round r)·DK_bonus(r)**
  (removes `K` and the assumed round weights).
- Sig Strikes O/U → real activity points (× ~0.4); Takedowns O/U → +5 each.
Feed these into the `finish_model` E[DK-points] structure. **Honest gaps:**
control time (+0.03/sec) and knockdowns have no prop → stay unpriced; still
unvalidated against realized results (R1). Bigger paste/parse effort; improves
*accuracy*, not the captain-leverage decision (§14.4).
