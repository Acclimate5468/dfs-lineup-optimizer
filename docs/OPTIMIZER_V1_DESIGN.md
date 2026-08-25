# Optimizer v1 Design

Status: design only. No implementation in this slice. Companion to
`docs/DEVELOPMENT_NOTES.md` §2 (v0 scope: UFC DK Classic only), §3 (out-of-scope
list — Showdown, NFL, automation, "complete optimizer" claims), §4
(projection formula), §10 (current checkpoint — D.5+ and downstream
work paused), §11 (UI write-action rules), §14 (do-not quick
reference), and the following sibling design docs:

- `docs/MANUAL_REVIEW_GATE_V1_DESIGN.md` §1 / §5 / §7 — the gate this
  optimizer reads from. The optimizer is the **first downstream
  consumer of `evaluate_manual_review`** and must not run for a slate
  that the gate has not declared reviewed.
- `docs/PROJECTION_V1_DESIGN.md` §4 / §5 / §7 — per-fighter projection
  rows and the `non_projectable` semantics the pool builder must
  honor.
- `docs/MISMATCH_ALERTS_V1_DESIGN.md` §3 / §10 — alert categories
  surfaced through the gate; optimizer does not re-derive them.
- `docs/FIGHTER_STATUS_V1_DESIGN.md` §5 / §9 — status categories. v1
  optimizer **does not** apply `effective_status` independently
  (deferred; see §9 below).
- `docs/ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7 — `effective_status`
  is still inert in downstream consumers; optimizer v1 inherits this
  caveat.
- `docs/SALARY_PERSISTENCE_DESIGN.md` §9 / §13 — salary import is the
  upstream source of pool rows; the real-file smoke is a precondition
  to trusting the pool.

---

## 1. Purpose

Optimizer v1 is the first **local, deterministic, manual-trigger**
lineup builder for **UFC DraftKings Classic** contests in this
workbench. It composes the already-persisted slate state — salary
import rows, fight groups, odds matching results, Projection v1
output, and a passing Manual Review Gate — into a small number of
candidate DK Classic lineups that the user can inspect on screen.

In one sentence: **given a slate the user has manually reviewed,
produce 1 to 5 valid UFC DK Classic lineups that maximize total
projection subject to salary and same-fight constraints, and show
them on a Streamlit page.**

Explicit non-claims (these track `docs/DEVELOPMENT_NOTES.md` §3 / §14):

- Optimizer v1 is **not** a multi-sport optimizer. UFC DK Classic
  only. No NFL, no Showdown / Captain, no Pick6.
- Optimizer v1 is **not** a contest entry tool. It does not log in to
  DraftKings, upload a CSV, or automate any entry path.
- Optimizer v1 is **not** an exposure-control engine. Diversity in
  v1 is a side effect of the top-N solve loop, not a guarantee
  (`§5.4`, `§10` risk #4).
- Optimizer v1 is **not** a Fighter Status enforcer. `effective_status`
  remains inert here in v1 (`§9`, `ODDS_PERSISTENCE_DESIGN.md`
  §15.11 risk #7).
- Optimizer v1 is **not** a recompute engine. It reads whatever
  `project_slate` and `evaluate_manual_review` return at call time.
  Staleness between the last gate ack and the current solve is a
  caveat the UI surfaces, not something the optimizer fixes
  (`§7`).
- Optimizer v1 is **not** a full export pipeline. The DK upload CSV
  and the per-run audit log are explicitly **out of scope for v1**
  and tracked in `§10` risk #5.

---

## 2. Contest shape (UFC DK Classic)

Hard rules the optimizer enforces, taken from DK's published UFC
Classic contest format:

- **Roster size: 6 fighters.** All six roster slots are the generic
  "F" (fighter) position. There is no flex, no captain, no MVP.
- **Salary cap: ≤ 50000.** Sum of the six fighters' `dk_salary`
  values must not exceed 50000.
- **At most one fighter per fight.** The two fighters in the same
  `fight_group_id` cannot both appear in the same lineup. (This is
  the same-fight pair constraint already prototyped in
  `src/optimizer/validation.py`.)
- **Pool source.** Eligible fighters are the rows the salary
  importer produced for the active slate, filtered by §5.1.
- **Lineup count.** v1 returns **1 to 5 lineups** per solve, user
  selects N in the UI. Default N = 1.

The above is the entire DK Classic constraint set v1 commits to. Any
additional constraint (locks, excludes, max-from-fight-card,
ownership caps) is **out of scope** and tracked under §10.

---

## 3. Inputs

Optimizer v1 is a pure-Python service. It reads:

1. **`project_slate(conn, slate_id)`** — Projection v1 output
   (`src/projections/slate_projection_service.py`). Provides
   `default_projection`, `non_projectable` flag, and the per-fighter
   identifiers the pool builder needs.
2. **`evaluate_manual_review(conn, slate_id)`** — the Manual Review
   Gate readiness signal. The optimizer service refuses to solve
   when the gate is not "manually reviewed" (§4).
3. **`FightGroupRepository(conn).list_for_slate(slate_id)`** —
   per-slate fight groupings (`src/db/repositories.py:147`). Used to
   build the same-fight exclusion set.

Optimizer v1 does **not** read odds rows, mismatch alerts, fighter
status rows, or override rows directly. It trusts those signals via
Projection v1 and the Manual Review Gate. This keeps the dependency
graph shallow and matches the §9 deferral of `effective_status`
enforcement.

The three inputs above are intentionally the **only** persistence
touchpoints in v1. No new tables, no new columns, no new repository
methods land in B.1–B.6. All read-only.

---

## 4. Manual Review gate (precondition)

Per `MANUAL_REVIEW_GATE_V1_DESIGN.md` §1, the gate exists precisely
to keep unreviewed slates out of downstream consumers. Optimizer v1
is the first such consumer and must enforce the gate explicitly:

- `optimizer_service.solve(conn, slate_id, n_lineups)` calls
  `evaluate_manual_review(conn, slate_id)` first.
- If the result is not in the "manually reviewed" state, the
  service returns a `SolveResult` with `status="gate_blocked"`,
  zero lineups, and a human-readable reason. It does **not** raise.
- The Streamlit page (`§6`) refuses to render the Solve button as
  enabled when the gate is not green.
- Both the service-level refusal and the UI-level disable are
  covered by tests (§8). Defense in depth: the UI is the primary
  UX, the service is the safety net.

The gate is read *each time* `solve` is called. The optimizer does
not cache the gate result across calls.

---

## 5. Architecture

Three new modules under `src/optimizer/`. Each is small, pure where
possible, and individually testable.

### 5.1 `pool_builder.py`

Input: a `Connection`, a `slate_id`.

Output: an `OptimizerPool` value object containing a sequence of
`OptimizerPoolEntry` rows and a `set[frozenset[fighter_id]]` of
same-fight pairs.

Behavior:

1. Call `project_slate(conn, slate_id)` once.
2. Drop any row where `non_projectable is True` or
   `default_projection is None`. These are the rows Projection v1
   already declares unsafe to feed forward
   (`PROJECTION_V1_DESIGN.md` §5 / §7).
3. Drop any row where `dk_salary is None` or `dk_salary <= 0`. The
   salary importer should never produce these; this is defense in
   depth, not a normal path.
4. Call `FightGroupRepository(conn).list_for_slate(slate_id)` and
   build the same-fight pair set. Groups with only one resolved
   fighter (e.g. a TBD opponent or a withdrawal already cleaned up
   upstream) contribute no pair.
5. Return the assembled `OptimizerPool`.

The pool builder does **not** apply `effective_status`. See §9.

Empty / undersized pool handling: returning a pool with fewer than
6 entries is allowed at this layer. The solver (§5.2) is the layer
that turns that into a `status="infeasible_pool_too_small"`
result. This separation keeps `pool_builder.py` pure and easy to
test.

### 5.2 `lineup_solver.py`

Input: an `OptimizerPool`, an integer `n_lineups` in `[1, 5]`.

Output: a `SolveResult` containing zero or more `Lineup` value
objects and a `status` string (see §5.4).

Behavior:

1. Validate `n_lineups in {1, 2, 3, 4, 5}`. Anything else is a
   programmer error: raise `ValueError`. The UI never produces a
   value outside this range, so this is a guard for direct callers.
2. If `len(pool.entries) < 6`, return
   `status="infeasible_pool_too_small"` immediately with zero
   lineups and a reason that names the actual pool size.
3. Build a PuLP integer program:
   - Binary variable `x_i` per pool entry.
   - Objective: maximize `sum(x_i * default_projection_i)`.
   - Constraint: `sum(x_i) == 6`.
   - Constraint: `sum(x_i * salary_i) <= 50000`.
   - For each same-fight pair `(a, b)`:
     `x_a + x_b <= 1`.
4. Solve. If the first solve is infeasible, return
   `status="infeasible_constraints"` with a reason. (With six
   slots, a 50k cap, and a real DK slate, this is unlikely; it is
   plausible on a near-empty post-late-news pool.)
5. For lineups 2..N, add a **diversity cut**: a constraint that
   forbids the exact lineup just solved
   (`sum(x_i for i in last_lineup) <= 5`). Re-solve. Stop early
   with the lineups already accumulated and
   `status="ok_partial"` if a subsequent solve is infeasible.
6. Return `status="ok"` with N lineups when all N solves succeed.

The diversity cut is the only "diversity" v1 offers. It guarantees
no two returned lineups are identical, but it does not bound
overlap, exposure, or stacking. See `§10` risk #4.

PuLP is the dependency choice for v1. Rationale: pure Python,
already familiar in the v0 skeleton (`src/optimizer/`), no native
extension required, and the default CBC solver ships with the
package. Install posture is tracked under §10 risk #1.

### 5.3 `optimizer_service.py`

The orchestration layer the Streamlit page calls into. One public
function:

```
def run_optimizer(
    conn: Connection,
    slate_id: int,
    n_lineups: int = 1,
) -> SolveResult: ...
```

Behavior:

1. Call `evaluate_manual_review(conn, slate_id)`.
2. If gate is not green, return `SolveResult(status="gate_blocked",
   lineups=[], reason=...)`. **Do not** call the pool builder.
3. Build pool via `pool_builder.build_pool(conn, slate_id)`.
4. Solve via `lineup_solver.solve(pool, n_lineups)`.
5. Return the `SolveResult` as-is.

This layer is where future enrichment (audit row insert, last-run
cache, export hand-off) would attach. v1 does none of those things.

### 5.4 Value objects

All immutable. Implemented as `@dataclass(frozen=True)` (or
equivalent) under `src/optimizer/`. The exact module placement is a
slice decision (B.2 / B.3); the shapes are fixed here.

- `OptimizerPoolEntry`
  - `fighter_id: int`
  - `slate_id: int`
  - `dk_name: str`
  - `dk_salary: int`
  - `default_projection: float`
  - `fight_group_id: int | None`

- `OptimizerPool`
  - `slate_id: int`
  - `entries: tuple[OptimizerPoolEntry, ...]`
  - `same_fight_pairs: frozenset[frozenset[int]]`  # fighter_id pairs

- `Lineup`
  - `fighter_ids: tuple[int, ...]`              # length 6
  - `total_salary: int`
  - `total_projection: float`

- `SolveResult`
  - `slate_id: int`
  - `status: str`  # one of `ok`, `ok_partial`, `gate_blocked`,
                   # `infeasible_pool_too_small`,
                   # `infeasible_constraints`
  - `lineups: tuple[Lineup, ...]`
  - `reason: str`  # human-readable; "" when status == "ok"

Status strings are part of the v1 contract. Tests pin them.

---

## 6. UI surface

A single Streamlit page, `app/pages/07_optimizer.py` (number subject
to the existing page ordering at implementation time):

- Read-only header: slate id, Manual Review state, count of
  projectable fighters in the pool.
- Disabled state if `evaluate_manual_review` is not green. The
  disabled state shows the same reason string the service would
  return.
- Numeric input for `n_lineups` (1..5, default 1).
- "Solve" button. Single write-action per page-load (idempotent on
  re-click — re-solving the same slate with the same N is allowed
  and produces the same result given the same inputs).
- Result panel:
  - Status string.
  - Per-lineup table: fighter, salary, projection, fight group.
  - Totals row: salary, projection.
- No CSV download in v1. No "save lineup" button. No history.

UI write-action rules (`docs/DEVELOPMENT_NOTES.md` §11): the Solve handler runs
inside a single read-only transaction (it issues no writes in v1),
the AppTest in B.5 exercises the button and asserts the rendered
text, and re-clicks are explicitly idempotent.

---

## 7. Manual Review staleness caveat

`evaluate_manual_review` returns a per-slate readiness snapshot at
the moment of the call. The optimizer reads that snapshot on every
`run_optimizer` call. But between two consecutive solves, upstream
state can change: a new odds row can land, a salary row can flip
`non_projectable`, an override can be added.

v1 does **not** detect this. v1's posture is:

- The gate ack timestamp (surfaced on the Manual Review page) is the
  contract.
- The optimizer page surfaces that timestamp verbatim alongside the
  Solve button, so the user can decide whether to re-review before
  solving.
- If a future slice wants to auto-invalidate the gate on upstream
  writes, that lives in the Manual Review Gate doc, not here.

This caveat is part of the v1 contract; the AppTest in B.5 pins the
rendered timestamp.

---

## 8. Slice plan

Each slice is independently shippable, has its own commit, its own
tests, and ends with `pytest` green. Per `docs/DEVELOPMENT_NOTES.md` §13, one slice
per session.

- **B.1 — design (this doc).** No code. Single file:
  `docs/OPTIMIZER_V1_DESIGN.md`. Stops here, awaits review.
- **B.2 — value objects + pool builder.**
  Adds `src/optimizer/pool_builder.py`,
  `OptimizerPoolEntry`, `OptimizerPool`. Unit tests under
  `tests/test_optimizer_pool_builder.py`:
  - drops `non_projectable=True` rows
  - drops `default_projection is None` rows
  - same-fight pairs assembled from `FightGroupRepository`
  - empty pool returns empty `OptimizerPool` (does not raise)
- **B.3 — lineup solver.**
  Adds `src/optimizer/lineup_solver.py`,
  `Lineup`, `SolveResult`. Unit tests under
  `tests/test_optimizer_lineup_solver.py`:
  - pool of size 5 → `infeasible_pool_too_small`
  - 6-fighter pool, no conflicts → `ok`, single lineup, totals match
  - same-fight pair excluded
  - salary cap respected
  - N=3 diversity cut produces three distinct lineups
  - N=5 with too-small distinct-lineup space → `ok_partial`
- **B.4 — service orchestration + gate enforcement.**
  Adds `src/optimizer/optimizer_service.py`. Unit tests under
  `tests/test_optimizer_service.py`:
  - gate not green → `gate_blocked`, no pool build attempted
  - gate green + valid pool → `ok`
  - gate green + tiny pool → `infeasible_pool_too_small`
- **B.5 — Streamlit page.**
  Adds `app/pages/0X_optimizer.py` and an AppTest under
  `tests/test_optimizer_page.py`:
  - disabled Solve button when gate not green, with reason text
  - enabled Solve, click, renders lineup table and totals
  - re-click idempotence
  - rendered Manual Review timestamp string is present (per §7)
- **B.6 — real-slate smoke + slice report.**
  Per `docs/DEVELOPMENT_NOTES.md` §8, the optimizer is not "complete" until it is
  exercised against a real DK UFC Classic slate end-to-end:
  ingest salary CSV → ingest odds → match → recompute →
  manual review ack → optimizer solve. Documented in a short
  smoke note appended to this doc. No code change required if the
  smoke passes; bug-fix commits are out-of-scope hot-fix slices.

Slices B.2 through B.5 each touch a single new module plus its
tests. None of them touch existing service / repository / schema
code. If a slice starts to require such a change, stop and confirm
(`docs/DEVELOPMENT_NOTES.md` §13 scope creep rule).

---

## 9. `effective_status` / Fighter Status deferral

`docs/FIGHTER_STATUS_V1_DESIGN.md` §5 / §9 introduces an
`effective_status` concept and the override taxonomy that drives it.
`docs/ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7 records that
`effective_status` is still **inert** in downstream consumers.

Optimizer v1 inherits this posture deliberately:

- Pool builder filters on `non_projectable` (a Projection v1 concept)
  and not on `effective_status`.
- The optimizer service does not read fighter status rows.
- A fighter the user has manually marked OUT via Fighter Status v1
  is **not** automatically excluded from the optimizer pool in v1.
  The expectation is that the user catches this on the Manual Review
  page before clicking the gate ack.

This is a known gap. The fix lives in a future "promote
`effective_status` into Projection v1 / Optimizer v1" slice, which
must land its own design pass first (`docs/DEVELOPMENT_NOTES.md` §10).

---

## 10. Risks

1. **PuLP not installed locally.** The v0 optimizer skeleton already
   imports PuLP, but the local environment may not have it on every
   workstation / venv. B.2/B.3 must verify `pulp` resolves before
   the solver lands; if it does not, the import + install posture
   becomes a B.1.1 sub-slice (`requirements.txt` edit), not a
   silent dependency bump.
2. **Pool < 6 fighters.** Real slates can produce a pool below six
   after `non_projectable` filtering (small cards, missing odds,
   heavy withdrawals). v1 returns
   `status="infeasible_pool_too_small"` with the actual pool size
   in the reason. The UI surfaces this verbatim. No silent zero
   lineups.
3. **Fight-pair name mapping.** Same-fight exclusion relies on
   `fight_group_id` being correctly assigned to both fighters in a
   bout. This is upstream (`src/slate/fight_grouping.py`) and not
   re-validated here. If the grouping is wrong, the optimizer can
   in principle return a lineup with both halves of a fight.
   Mismatch Alerts v1 surfaces this risk on the Manual Review page;
   v1 trusts that signal.
4. **Diversity is not exposure control.** The N>1 diversity cut
   only forbids exact duplicates. Returned lineups can share five
   of six fighters. v1 does not bound overlap; users who need
   exposure control will need a future slice (out of scope).
5. **Exports and run log are separate.** v1 does **not** write a DK
   upload CSV, does **not** persist a per-run audit row, and does
   **not** keep a history of past solves. Each of those is its own
   future design slice. v1 is screen-only output.

---

## 11. Out of scope for v1

Tracking this list explicitly so that future requests can be
checked against it (`docs/DEVELOPMENT_NOTES.md` §3, §13):

- DK upload CSV export.
- Per-run audit / history persistence.
- Lock / exclude / force-include controls on individual fighters.
- Ownership projections or exposure caps across the N lineups.
- Showdown / Captain formats.
- NFL or any non-UFC sport.
- `effective_status` enforcement in the pool (deferred, §9).
- Auto-invalidating the Manual Review gate on upstream writes
  (deferred, §7).
- Any background or scheduled solve. v1 is button-press only
  (`docs/DEVELOPMENT_NOTES.md` §13: no autonomous loops in this repo).
