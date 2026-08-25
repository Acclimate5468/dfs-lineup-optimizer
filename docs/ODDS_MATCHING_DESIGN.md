# Odds Matching Design

**Status:** Design only. No matching code is wired up yet. This document covers
how uploaded / manually-entered odds rows will eventually be matched to the
fighter names that come from the DraftKings UFC Classic salary CSV.

**Scope guardrails (carried from `PROJECT_RULES.md`):**

- UFC DraftKings Classic only.
- CSV import / manual entry only — no direct Odds API integration, no scraping.
- Local-only persistence. No cloud, no auth.
- Matching is **read-only relative to the DK salary roster** — the DK roster is
  the source of truth for fighter identity. Odds rows are aligned to it, never
  the other way around.

## 0. Vocabulary

- **DK fighter** — a row from a validated DK UFC Classic salary CSV; identified
  by `(slate_id, name)` (existing `fighters` table) and ultimately by
  `dk_player_id` where available.
- **Odds row** — a row from a validated odds CSV or a `ManualOddsEntry`.
- **Match** — an association from one odds row to exactly one DK fighter on a
  specific slate.
- **Conservative normalization** — what `src/utils/text_cleaning.normalize_name`
  does today: NFKD accent-strip, lowercase, trim, internal-whitespace collapse.
- **Aggressive normalization** — proposed second layer (see §1.2) for
  exact-match fallback before fuzzy.

## 1. Name normalization rules

### 1.1 Conservative normalization (existing — keep as-is)

`normalize_name(s)` from `src/utils/text_cleaning.py`:

1. NFKD-decompose and drop combining marks (e.g. `José` → `Jose`).
2. Lowercase.
3. Strip leading / trailing whitespace.
4. Collapse internal whitespace runs to a single space.

Used for:

- The keys in the DK roster index.
- The fuzzy-match query string passed to `rapidfuzz.process.extractOne`.
- The conservative exact-match lookup (§2).

### 1.2 Aggressive normalization (new helper — `normalize_name_aggressive`)

Applied **only when conservative exact match fails**, before falling back to
fuzzy scoring. It is intentionally lossy and never used as a stored key.

Rules, applied in order:

1. Run conservative normalization first.
2. Drop trailing generational suffix tokens: `jr`, `sr`, `ii`, `iii`, `iv`.
3. Replace punctuation `.`, `'`, `’`, `-`, `_` with a single space; then
   re-collapse whitespace.
4. Drop nickname segments wrapped in `"..."` or `'...'` mid-name
   (`john "the rock" smith` → `john smith`).
5. Drop standalone single-character tokens that look like middle initials
   (`michael j pereira` → `michael pereira`).
6. Apply a small **curated nickname-expansion table** (bidirectional). v0 ships
   *only* this hand-maintained list — no broad heuristics, no prefix-match
   guessing, no learned suggestions. Each entry is a deliberate pair that has
   been observed (or is overwhelmingly likely) in real UFC rosters:
   - `dan` ↔ `daniel`
   - `tom` ↔ `thomas`
   - `mike` ↔ `michael`
   - `rob` ↔ `robert`
   - `chris` ↔ `christopher`
   - `nick` ↔ `nicholas`
   - `alex` ↔ `alexander`
   - `joe` ↔ `joseph`
   - `matt` ↔ `matthew`
   - `tony` ↔ `anthony`
   - `ben` ↔ `benjamin`

   Expansion produces *both* candidate forms; equality is "any-of-A == any-of-B".

   **v0 rule:** any nickname collapse not in this table must not be applied,
   even when fuzzy similarity is high. Broad nickname assumptions (e.g.
   stripping any short prefix, treating any 3-letter token as a nickname for
   the longest roster name that starts with it) are explicitly out of scope
   for v0 — they generate false matches at higher cost than the review queue
   they would be trying to shrink. Additions to this table are a code change
   reviewed like any other.

7. Tokenize and sort alphabetically (covers `zhang weili` vs `weili zhang` —
   relevant for fighters whose surnames precede given names in some feeds).

**Out of scope for v0 normalization** (we lean on fuzzy + manual override for
these): hyphenated surname splits, diacritic-only differences inside
non-Latin scripts, transliteration variants beyond what NFKD covers.

### 1.3 Why a layered normalizer instead of one mega-rule

The conservative form must stay stable because it is the persisted index key
and the canonical form used in alerts / UI. The aggressive form is throw-away:
it exists only to *generate match candidates* — if two strings collide under
aggressive normalization they go through the fuzzy stage anyway. Keeping them
separate means we never silently lose Jr/Sr or middle-name information from
stored data.

## 2. Exact normalized matching

Algorithm per odds row, given a slate's DK roster:

1. Build (or cache) a dict `roster_by_conservative: Dict[str, DkFighterId]`
   keyed by `normalize_name(dk_fighter.name)`.
2. Build a parallel dict `roster_by_aggressive: Dict[str, Set[DkFighterId]]`
   keyed by `normalize_name_aggressive(dk_fighter.name)`. Values are sets to
   handle the rare case of two roster names colliding under aggressive
   normalization (e.g. someone's nickname literally equals another fighter's
   first name).
3. Compute `q_c = normalize_name(odds_row.fighter)`. If `q_c` is in
   `roster_by_conservative`, that is the match. **Score = 100, tier = `auto`,
   stage = `exact_conservative`.**
4. Else compute `q_a = normalize_name_aggressive(odds_row.fighter)`. If `q_a`
   is in `roster_by_aggressive` and the value set has exactly one element,
   that is the match. **Score = 100, tier = `auto`, stage = `exact_aggressive`.**
5. Else if `roster_by_aggressive[q_a]` has >1 element, mark as
   `ambiguous_aggressive` and fall through to fuzzy — fuzzy will be the
   tiebreaker, and §5 (duplicate handling) governs the final state.
6. Else fall through to fuzzy (§3).

Exact-stage matches always beat fuzzy matches: a 100-score exact stage is
never demoted by a higher numeric WRatio against a different candidate.

## 3. Fuzzy matching thresholds

We use `rapidfuzz.fuzz.WRatio` via `src/ingestion/name_matching.best_match`,
because (a) it is already in use and tested, and (b) WRatio is robust to token
reordering and partial matches — the failure modes we actually see in odds
feeds.

Threshold tiers (scored 0–100):

| WRatio score | Tier         | Action                                                        |
| ------------ | ------------ | ------------------------------------------------------------- |
| `>= 95`      | `auto`       | Accept silently. Opponent check (§4) may still demote.        |
| `88–94`      | `review`     | Surface to user for one-click accept/reject. Optimizer-blocked until resolved. |
| `< 88`       | `unmatched`  | No automatic association. Treated like missing odds (§7).     |

Constants live in `src/ingestion/name_matching.py` alongside the existing
`DEFAULT_MATCH_THRESHOLD = 88`. Suggested additions:

```python
AUTO_MATCH_THRESHOLD = 95     # >= auto
REVIEW_MATCH_THRESHOLD = 88   # >= review, < AUTO
# < REVIEW_MATCH_THRESHOLD → unmatched
```

### 3.1 Calibration sanity-checks (measured against real-ish UFC name pairs)

| Pair                                        | WRatio | Tier under proposal |
| ------------------------------------------- | ------ | ------------------- |
| `jose aldo` / `jose aldo`                   | 100    | auto (exact)        |
| `jose aldo` / `jose aldo jr`                | 95     | auto                |
| `jared cannonier` / `jared canonier`        | 96.6   | auto                |
| `michel pereira` / `michael pereira`        | 96.6   | auto                |
| `zhang weili` / `weili zhang`               | 95.0   | auto (also rescued by aggressive token-sort) |
| `terrance mckinney` / `terrence mckinney`   | 94.1   | review              |
| `khabib nurmagomedov` / `khabib n`          | 90.0   | review              |
| `ovince saint preux` / `ovince st preux`    | 90.9   | review              |
| `thomas almeida` / `tom almeida`            | 88.0   | review (rescued to auto by nickname expansion) |
| `jon jones` / `jonathan jones`              | 85.5   | unmatched (rescued by nickname expansion) |
| `dan ige` / `daniel ige`                    | 82.4   | unmatched (rescued by nickname expansion) |

Takeaway: the 95/88 split is the right granularity for raw WRatio, but several
real cases score below 95 *only* because of nickname/initial issues that the
aggressive normalizer rescues. So order matters: **aggressive-exact must run
before fuzzy** or we'd push easy cases into the review queue for no reason.

## 4. Opponent mismatch handling

If the odds CSV includes an `opponent` column (preferred optional column, see
`odds_csv_importer.PREFERRED_OPTIONAL_COLUMNS`) **and** the slate has a
confirmed `fight_groups` row for the matched DK fighter, we cross-check:

1. Look up the matched DK fighter's expected opponent on the slate from
   `fight_groups` (where `status = 'confirmed'`).
2. Conservative + aggressive + fuzzy match the odds row's `opponent` value
   against that expected opponent name using the same pipeline.

Decision matrix:

| Name match tier | Opponent column present | Opponent match | Resulting tier            |
| --------------- | ----------------------- | -------------- | ------------------------- |
| `auto`          | no                      | n/a            | `auto` (no cross-check possible) |
| `auto`          | yes                     | `auto` or `review` | `auto`                |
| `auto`          | yes                     | `unmatched`    | **demoted to `review`**, alert `OPPONENT_MISMATCH` |
| `review`        | yes                     | `auto`         | **stays `review`** in v0 — opponent agreement is recorded as supporting context, never used to auto-promote |
| `review`        | yes                     | `unmatched`    | `review` (kept as-is)     |
| `unmatched`     | any                     | any            | `unmatched` (opponent agreement alone never creates a name match) |

**v0 rule — review-band stays review.** A fuzzy match scoring 88–94 always
requires explicit user action, *even when the odds row's opponent agrees with
the confirmed fight group*. The opponent-agreement signal is shown on the
review row as supporting context ("opponent matches confirmed pairing") and
recorded on the match result for analytics, but it does not auto-promote the
match to `auto`. Rationale: corroboration from a second feed column is not
strong enough on its own to silently bypass user review for a name match the
fuzzy scorer itself was unsure about; the cost of a one-click accept is low
and a wrong silent promotion is high.

Fight-group status matters in the *other* direction: if the `fight_groups`
row for that DK fighter is `unconfirmed`, we **do not** demote `auto` →
`review` on opponent mismatch (the user hasn't told us yet what the real
pairing is). We still record the disagreement on the match record as a hint
for later review.

Manual odds entries (`ManualOddsEntry`) have no opponent field today. They
skip the opponent check entirely; their match record carries
`opponent_check = 'not_applicable'`.

## 5. Duplicate match handling

Three distinct flavors:

### 5.1 Multiple odds rows → one DK fighter

Common case: same fighter in two odds files, or multiple bookmaker snapshots
in one file. Resolution:

1. Sort candidate rows by:
   1. tier (`auto` > `review` > `unmatched`),
   2. WRatio score descending,
   3. `timestamp` descending (freshest wins),
   4. `bookmaker` priority (a future config; v0 treats all bookmakers equal),
   5. row index ascending (stable tiebreak).
2. The first row becomes the **primary** match. The remainder are stored with
   `match_status = 'shadowed'` and surfaced as informational, not blocking.
3. If two rows tie on every key above with materially different moneylines
   (define "materially different" as: implied probability differs by >= 0.03
   after raw conversion), emit a `DUPLICATE_ODDS_ROW` alert at `warning`
   severity. The user can pick which to keep in Manual Review.

### 5.2 One odds row → multiple DK fighter candidates

Happens with very short or very generic names (single-name fighters, or
brothers on the same slate — e.g. Diaz/Diaz, Gracie/Gracie). Resolution:

1. If exactly one of the candidates has a confirmed opponent in
   `fight_groups` that also matches the odds row's `opponent` field, pick it
   and record `disambiguated_by = 'opponent'`. Tier remains as before.
2. Else demote to `review` regardless of score. Alert `AMBIGUOUS_NAME_MATCH`
   at `warning`.
3. Never silently auto-pick when two candidates remain plausible.

### 5.3 Cross-slate collisions

Out of scope. Matching is always scoped to one slate. The DK roster index is
rebuilt per slate.

## 6. Manual override flow

The user's overrides must be (a) reproducible across re-imports of the same
odds CSV, and (b) explicit — never inferred from a previous fuzzy decision.

### 6.1 Override types

| Type              | Effect                                                                 |
| ----------------- | ---------------------------------------------------------------------- |
| `accept_match`    | Confirms a `review`-tier suggestion. Pinned to a specific `(odds_row_key, dk_fighter_id)`. |
| `reject_match`    | Rejects a `review`- or `auto`-tier suggestion. Future matches for that `odds_row_key` cannot land on `dk_fighter_id` automatically; they must be re-confirmed. |
| `force_pair`      | Explicitly pair an odds row with a DK fighter regardless of score (including below 88). Records the user-typed reason. |
| `mark_excluded`   | The DK fighter is excluded from the lineup pool entirely (see §7).     |
| `manual_moneyline`| The DK fighter has no usable odds row; user supplied an American moneyline directly (see §7). |
| `manual_projection_low_confidence` | The DK fighter has no usable odds and no manual moneyline; user supplied a projection directly and acknowledged low-confidence status (see §7). |

### 6.2 `odds_row_key`

The canonical key for an odds row must survive re-import of the same CSV. v0
proposal: `sha1(normalize_name(fighter) + '|' + bookmaker + '|' + source + '|' + timestamp_iso)`
truncated to 16 hex chars. For `ManualOddsEntry`, the key is `manual:<normalize_name(fighter)>:<timestamp>`.

### 6.3 UI flow (eventual Manual Review page)

For each slate, surface three tables:

1. **`auto` matches** — collapsed by default, scroll to inspect.
2. **`review` matches** — one-click `Accept` / `Reject` / `Force-pair` buttons
   per row. Optimizer is gated until this list is empty *or* every remaining
   row has a non-blocking action state.
3. **`unmatched` DK fighters** — one row per DK fighter whose odds did not
   land. Each row offers the four action states from §7.

Overrides persist into a `manual_match_overrides` table (§9). The matching
service applies overrides as a post-pass on top of the algorithmic result, so
re-running the importer never silently revives a previously rejected pair.

## 7. Missing-odds action states

A DK fighter is "missing odds" when, after §2 + §3 + §5, no odds row has
`match_status = 'matched'` to that fighter. The user picks exactly one of
four actions per fighter:

| Action                                | Slug                       | Downstream behavior |
| ------------------------------------- | -------------------------- | ------------------- |
| Manually enter moneyline              | `manual_moneyline`         | Stored as an odds row with `source = 'manual'` and `match_status = 'manual'`. Implied probability computed via `american_to_implied_probability`. Projection uses the default formula (§8). |
| Manually match an existing odds row   | `manual_match`             | Equivalent to a `force_pair` override targeting an existing `unmatched` odds row. Projection uses the default formula. |
| Hard-exclude fighter                  | `excluded`                 | Fighter is set to `status = 'excluded'` on this slate. Optimizer skips entirely. No projection emitted. |
| Manually enter projection (low conf.) | `manual_projection_low_confidence` | A projection row is written with `source = 'manual_low_confidence'`. Slate gate (§8) cannot pass until the user explicitly acknowledges the low-confidence flag in Manual Review. |

Each fighter sits in exactly one of these states at any time. Switching
between states is allowed but always re-prompts acknowledgement for the
low-confidence path.

## 8. Effect on projections and the Manual Review gate

### 8.1 Projections

`default_projection` (`src/projections/default_projection.py`) requires
`implied_win_probability ∈ [0, 1]`. There is no fallback path inside that
function — by design (pure math, no policy). So policy lives one layer up in
the projection service:

- A DK fighter resolves to a projection input only if it has either
  - one `matched` or `manual` odds row (use `american_to_implied_probability`,
    then `no_vig_two_way` if both opponents have odds), **or**
  - a `manual_projection_low_confidence` projection row (bypasses the formula
    entirely; uses the user-supplied number verbatim).
- Fighters in `excluded` produce **no** projection.
- Fighters with only `review`-tier or `unmatched` candidates and no manual
  action also produce no projection. The projection service treats them the
  same as `excluded` from a "missing input" standpoint, but the slate gate
  treats them differently (see §8.2).

### 8.2 Manual Review gate

The slate is "optimizer-ready" only when every active fighter
(`status = 'active'`) on the slate has a **terminal** match state:

- `auto` (post opponent-check),
- `review_accepted`,
- `force_pair`,
- `manual_moneyline`,
- `manual_match`,
- `excluded`,
- `manual_projection_low_confidence` **with explicit acknowledgement**.

Non-terminal states that block the gate:

- `review` (un-actioned suggestion),
- `unmatched` (no action picked),
- `manual_projection_low_confidence` **without** acknowledgement,
- `ambiguous_name_match` (multiple candidates remain plausible),
- `opponent_mismatch` on an `auto` that was demoted to `review`.

The optimizer should hard-refuse to run on a slate whose gate has not passed.
The export step should hard-refuse to write a DK upload CSV under the same
condition.

### 8.3 Alerts

New alert codes the design implies (to be wired through the `Alert` value
object in `src/alerts/alert_rules.py` / the alerts service when they are
built):

| Code                       | Severity   | When |
| -------------------------- | ---------- | ---- |
| `MISSING_ODDS`             | warning    | No matched / manual odds for an active DK fighter. |
| `ODDS_MATCH_REVIEW`        | info       | A match is in the `review` tier and needs user action. |
| `OPPONENT_MISMATCH`        | warning    | Name match was `auto` but opponent disagreed against a confirmed fight group. |
| `AMBIGUOUS_NAME_MATCH`     | warning    | One odds row plausibly matches >1 DK fighter on the slate. |
| `DUPLICATE_ODDS_ROW`       | info       | Multiple odds rows for the same DK fighter, with non-trivial implied-probability disagreement. |
| `MANUAL_PROJECTION_USED`   | warning    | A fighter is using a `manual_projection_low_confidence` projection. Surfaces every time the slate is loaded; never auto-acknowledged. |
| `FORCED_PAIR`              | info       | A `force_pair` override is active. Surfaces so the user can sanity-check before optimizer run. |

## 9. Minimal future data structures

These are *proposed* — none of them exist yet. They are the smallest set that
lets §2–§7 round-trip across app restarts.

### 9.1 `odds_match_results` table

Per-slate, per-odds-row. Survives re-imports of the same odds CSV.

```sql
CREATE TABLE IF NOT EXISTS odds_match_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
    odds_row_key TEXT NOT NULL,                  -- §6.2
    fighter_id INTEGER REFERENCES fighters(id) ON DELETE SET NULL,
    match_status TEXT NOT NULL,                  -- auto | review | unmatched | shadowed
                                                 --   | manual | review_accepted
                                                 --   | review_rejected | force_pair
    match_stage TEXT NOT NULL,                   -- exact_conservative | exact_aggressive
                                                 --   | fuzzy | manual
    match_score INTEGER NOT NULL DEFAULT 0,      -- WRatio 0..100, 100 for exact / manual
    opponent_check TEXT NOT NULL DEFAULT 'not_applicable',
                                                 -- not_applicable | passed | failed | unknown
    disambiguated_by TEXT,                       -- nullable: opponent | manual
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(slate_id, odds_row_key)
);
```

### 9.2 `manual_match_overrides` table

Records user decisions explicitly so they survive odds-CSV re-imports.

```sql
CREATE TABLE IF NOT EXISTS manual_match_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
    odds_row_key TEXT,                           -- null for fighter-level overrides
    fighter_id INTEGER REFERENCES fighters(id) ON DELETE CASCADE,
    override_type TEXT NOT NULL,                 -- accept_match | reject_match | force_pair
                                                 --   | mark_excluded | manual_moneyline
                                                 --   | manual_projection_low_confidence
    payload_json TEXT,                           -- e.g. {"moneyline": -150}, {"projection": 62.5, "acknowledged": true}
    reason TEXT,                                 -- optional free-text
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_overrides_slate_fighter
    ON manual_match_overrides (slate_id, fighter_id);
CREATE INDEX IF NOT EXISTS idx_overrides_slate_row
    ON manual_match_overrides (slate_id, odds_row_key);
```

### 9.3 Extension to `odds` row

The `odds` table already exists. Add (when the importer is wired in):

- `match_status TEXT NOT NULL DEFAULT 'unmatched'`
- `match_score INTEGER NOT NULL DEFAULT 0`
- `odds_row_key TEXT` (for cross-reference to override rows)

Or, keep `odds` clean and route matching metadata through
`odds_match_results` only. Either is workable — see open decisions §11.

### 9.4 Module layout

```
src/ingestion/
    name_matching.py            # add AUTO_MATCH_THRESHOLD, REVIEW_MATCH_THRESHOLD,
                                # normalize_name_aggressive, candidate generation
    odds_matching.py            # NEW — orchestrates §2..§5 against a DK roster +
                                # a list of odds rows, returns a list of MatchResult
                                # dataclasses. Pure / dict-in dict-out.
    odds_match_overrides.py     # NEW — applies §6 overrides on top of MatchResult.
```

Pure functions stay in `name_matching` / `odds_matching`. Persistence sits in
new repositories under `src/db/repositories.py` (`OddsMatchResultRepository`,
`ManualMatchOverrideRepository`) to fit the existing pattern.

## 10. Tests to write when implemented

These are the tests that should exist to lock the design in. Group / order
flexible; the list is exhaustive for §1–§8.

### 10.1 Normalization (extend `tests/test_odds_matching.py`)

1. `normalize_name_aggressive` drops trailing `jr` / `sr` / `ii` / `iii` / `iv`.
2. `normalize_name_aggressive` collapses punctuation: `D'Angelo`, `O-Malley`,
   `St. Preux`.
3. `normalize_name_aggressive` drops bracketed nicknames inside a name.
4. `normalize_name_aggressive` drops standalone middle initials.
5. `normalize_name_aggressive` applies bidirectional nickname expansion for
   each pair in §1.2 (parametrized).
6. `normalize_name_aggressive` token-sorts to handle `weili zhang` vs
   `zhang weili`.
7. `normalize_name` (existing) is unchanged — regression guard for stored keys.

### 10.2 Exact match (new `tests/test_odds_matching_exact.py`)

1. Conservative-exact wins over a higher-scoring fuzzy candidate.
2. Aggressive-exact runs only when conservative fails.
3. Aggressive-exact with multiple roster collisions falls through to fuzzy
   and tags the result `ambiguous_aggressive`.
4. Score for exact-stage results is always 100.

### 10.3 Fuzzy tiering (new `tests/test_odds_matching_fuzzy.py`)

1. WRatio >= 95 → `auto`.
2. WRatio in 88..94 → `review`.
3. WRatio < 88 → `unmatched`.
4. Boundary exactly 95 → `auto`; exactly 88 → `review`; exactly 87 → `unmatched`.
5. Calibration table (§3.1) parametrized as a single test — each pair lands
   in the documented tier after the full pipeline (exact then fuzzy).

### 10.4 Opponent mismatch (new `tests/test_odds_matching_opponent.py`)

1. `auto` name + `auto` opponent → `auto`.
2. `auto` name + `unmatched` opponent on a *confirmed* fight group → demoted
   to `review`, alert `OPPONENT_MISMATCH` recorded.
3. `auto` name + `unmatched` opponent on an *unconfirmed* fight group →
   stays `auto`, no demotion, hint recorded on match record.
4. `review` name + `auto` opponent → **stays `review`** in v0. Match record
   carries `opponent_check = 'passed'` for display as supporting context; the
   tier is not promoted.
5. `unmatched` name + `auto` opponent → still `unmatched` (opponent never
   creates name matches).
6. No opponent column in odds row → `opponent_check = 'not_applicable'`.
7. `ManualOddsEntry` → `opponent_check = 'not_applicable'` even when fight
   groups exist.

### 10.5 Duplicate handling (new `tests/test_odds_matching_duplicates.py`)

1. Two rows for one fighter, both `auto`: freshest timestamp wins, other is
   `shadowed`.
2. Two rows tied on tier/score/timestamp with material implied-prob delta →
   `DUPLICATE_ODDS_ROW` alert at `warning`.
3. One row, two DK candidates, one matches opponent: that candidate wins,
   tag `disambiguated_by = 'opponent'`.
4. One row, two DK candidates, neither resolved by opponent → demoted to
   `review`, `AMBIGUOUS_NAME_MATCH` alert.

### 10.6 Manual override flow (new `tests/test_manual_match_overrides.py`)

1. `accept_match` flips a `review` to `review_accepted`; idempotent.
2. `reject_match` flips a `review` to `review_rejected` and a future
   re-import does not auto-revive the pair.
3. `force_pair` works even for scores below 88.
4. `manual_moneyline` produces an odds-like input that drives the projection
   service successfully.
5. `manual_projection_low_confidence` *without* acknowledgement does NOT
   pass the slate gate.
6. `manual_projection_low_confidence` *with* acknowledgement passes the
   gate; alert `MANUAL_PROJECTION_USED` still fires.
7. `mark_excluded` removes the fighter from optimizer input and emits no
   projection.
8. Overrides survive a simulated odds-CSV re-import (same `odds_row_key`).

### 10.7 Gate / downstream (new `tests/test_slate_gate.py`)

1. Slate with any active fighter in `review` / `unmatched` / `ambiguous`
   blocks the gate.
2. Slate with all active fighters in terminal states passes.
3. Optimizer entry-point refuses to run when gate fails (call the existing
   optimizer skeleton with a synthetic blocked slate; assert it raises).
4. Export entry-point refuses to write a DK CSV when gate fails.

### 10.8 Edge cases

1. Empty DK roster → every odds row is `unmatched`; no crash.
2. Empty odds list → every active DK fighter is missing-odds; no crash.
3. Odds row with empty `fighter` string → ignored with a structured warning,
   not a hard error.
4. Slate with no `fight_groups` rows at all → opponent check is fully
   skipped, all `opponent_check = 'unknown'`, no demotions on opponent.

## 11. Open decisions

These deliberately stay open until we hit real data. Calling them out so
they do not get silently decided in code.

1. **Where match metadata lives.** Either extend the existing `odds` table
   with `match_status` / `match_score` / `odds_row_key`, **or** keep `odds`
   clean and put everything in `odds_match_results`. The clean split keeps
   `odds` semantically "raw input", which matches the rest of the schema.
   Leaning toward §9.1 / §9.2 only.
2. **Nickname-expansion table source of truth.** v0 ships the small *curated*
   list in §1.2 as a Python constant — no fuzzy nickname inference, no
   prefix-match heuristics. Real cases will demand additions; do we keep it
   as code, or move it to a YAML / JSON file under `data/` so non-engineer
   edits are easier? v0 default: keep as code.
3. **Material implied-probability delta for `DUPLICATE_ODDS_ROW`.** Proposed
   0.03 absolute. Could be relative (e.g. ratio of fair-prob estimates) once
   we see how noisy real bookmaker spreads are.
4. **Bookmaker priority.** v0 treats all bookmakers equal. If the user
   regularly uploads multi-book CSVs, a configurable preference list
   becomes useful.
5. **`auto` opponent agreement promoting a `review`.** Locked for v0: no
   auto-promotion. Review-band matches (88–94) always require explicit user
   action regardless of opponent agreement; the opponent signal is shown as
   supporting context only. Revisit only after we have real-data evidence
   that the review queue is too noisy.
6. **`force_pair` from below-threshold scores.** Always allowed in §6, but we
   may want a "you sure?" confirm and a captured reason string. UI concern,
   not algorithm concern.
7. **No-vig handling when only one side of a fight has odds.** The
   projection service currently does no-vig only via
   `american_pair_to_no_vig`. If only one fighter on a fight has odds, do we
   use the raw implied probability for that fighter and skip no-vig, or
   refuse to project? v0 lean: use raw implied probability and emit a
   `SINGLE_SIDE_ODDS` warning (new alert code; out of scope here but worth
   noting as a follow-up).

## 12. Non-goals for this design

To avoid scope creep:

- No direct Odds API client. CSV / manual only.
- No multi-slate / multi-event matching state.
- No persistence of per-bookmaker priority preferences.
- No automated nickname-table learning ("user accepted X→Y last time, add it").
- No fuzzy on opponent-only fields used as a *primary* match key (only as
  a corroborating signal per §4).
- No projection logic changes beyond the routing described in §8.1. The
  pure `default_projection` function stays untouched.
