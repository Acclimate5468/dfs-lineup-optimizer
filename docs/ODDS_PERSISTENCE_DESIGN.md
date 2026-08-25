# Odds Persistence Design

**Status:** Design only. No persistence code is being added in this change.
This document covers how raw odds rows, the results of running the matching
pipeline, and the user's manual match overrides should be stored on disk so
that the work survives app restarts and odds-CSV re-imports.

**Companion docs:**

- `docs/ODDS_MATCHING_DESIGN.md` — the matching algorithm itself (name
  normalization, fuzzy tiers, opponent cross-check, override *types*). This
  document does not redesign any of that; it only specifies how the resulting
  state lives in SQLite.
- `PROJECT_RULES.md` — the v0 hard limits this design respects (UFC Classic
  only, local-first, CSV/manual only, no Odds API, no scraping, no auth).

## 1. Current state

What exists today (as of `256054f`):

- **Schema (`src/db/schema.py`)** defines `odds` (FK to `fighters.id`),
  `fight_groups`, `fighters`, `slates`. The `odds` table is keyed to an
  already-matched DK fighter — it cannot hold a raw odds row whose DK
  counterpart has not been resolved yet.
- **CSV validation (`src/ingestion/odds_csv_importer.py`)** validates
  structure only. Validated rows are not persisted.
- **Manual odds entry (`app/pages/03_odds.py`)** lives in
  `st.session_state.manual_odds_entries`. Lost on browser refresh.
- **Matching (`src/ingestion/odds_matching.py`)** is pure / in-memory.
  `match_odds_to_dk(...)` returns `OddsMatchResult` dataclasses; nothing
  persists.
- **Odds Matching Preview** on the Odds page is read-only and does not write
  back anywhere.
- **No-vig preview** (just added in `256054f`) is computed at render time
  inside the Streamlit page; nothing persists.
- **Repositories (`src/db/repositories.py`)** — `OddsRepository` is a TODO
  stub. Only `SlateRepository` and `FightGroupRepository` are implemented.

Net: every odds artifact the user creates is lost on restart, and re-importing
the same CSV does not preserve prior accept/reject decisions.

## 2. Goals

1. Persist raw odds rows so re-importing the same CSV is idempotent and a
   user's history is auditable per slate.
2. Persist match results so the Odds Matching Preview, Manual Review queue,
   and (eventually) the projection input survive restarts.
3. Persist user decisions (accept, reject, force-pair, manual moneyline,
   exclude, manual low-confidence projection) such that re-running matching
   never silently revives a previously-rejected pair.
4. Keep raw odds rows immutable: re-running the matcher rewrites match
   results; it does not edit the raw data.
5. Support future projection work without leaking projection logic into the
   persistence layer.

## 3. Non-goals

- Implementing any of this. This is design-only.
- Direct Odds API integration. CSV / manual entry only.
- Storing per-bookmaker priority preferences.
- Cross-slate / multi-event sharing of odds rows.
- Persisting the *session-only* manual odds form state (the Streamlit form
  remains a local convenience for one-off entries until §5.2 lands).
- Mismatch alerts — those are a separate doc (see §11.5 of
  `ODDS_MATCHING_DESIGN.md`).
- Changing `default_projection` or `american_to_implied_probability`.

## 4. Vocabulary

- **odds row** — a single fighter / moneyline / source / timestamp record
  coming from either a CSV import or a manual entry. Raw input; not yet
  associated with a DK fighter.
- **match result** — the algorithm's verdict for one (slate, odds_row)
  pair: matched / review / unmatched / etc. Rebuildable from inputs.
- **override** — a user decision recorded explicitly. Survives re-imports.
  Outranks the algorithmic verdict.
- **terminal state** — a per-fighter state that satisfies the Manual Review
  gate (see `ODDS_MATCHING_DESIGN.md` §8.2).

## 5. Proposed tables

The existing `odds` table is *retained but repurposed* — see §5.4. Three new
tables carry the heavy lifting.

### 5.1 `odds_rows` — raw, immutable per import

One row per fighter-line in a CSV or manual entry. No FK to `fighters`: raw
input can exist before the DK salary CSV has been imported, and a row that
fails to match must still live somewhere.

| Column                | Type     | Notes                                                                  |
| --------------------- | -------- | ---------------------------------------------------------------------- |
| `id`                  | INTEGER PK | autoincrement                                                        |
| `slate_id`            | INTEGER  | FK `slates(id)` ON DELETE CASCADE. NULLABLE — see §6.1 (open).         |
| `odds_row_key`        | TEXT     | Stable hash, definition in `ODDS_MATCHING_DESIGN.md` §6.2.             |
| `fighter_name_raw`    | TEXT     | Verbatim from CSV / form input. Required, non-empty.                   |
| `fighter_name_normalized` | TEXT | `normalize_name(fighter_name_raw)`. Indexed.                           |
| `opponent_name_raw`   | TEXT     | Nullable. Optional column in odds CSV; not used by manual entry today. |
| `american_odds`       | INTEGER  | Required, non-zero. Validation §10.                                    |
| `implied_probability` | REAL     | Cached at insert from `american_to_implied_probability`. See §9.       |
| `bookmaker`           | TEXT     | Nullable; preferred optional column in the CSV.                        |
| `source`              | TEXT     | e.g. `csv:oddsapi`, `manual`. Required.                                |
| `captured_at`         | TEXT     | ISO-8601 string from the row's `timestamp` field. Required.            |
| `imported_at`         | TEXT     | `datetime('now')` at insert.                                           |
| `import_batch_id`     | TEXT     | Nullable; groups rows that came from one upload. Optional v0.          |

Constraints / indexes:

- `UNIQUE(slate_id, odds_row_key)` — re-uploading the same CSV is a no-op
  on already-seen rows.
- `INDEX (slate_id, fighter_name_normalized)` — drives Manual Review lookups.
- `CHECK (american_odds <> 0)`.

**Immutability rule.** Once inserted, an `odds_rows` row is never edited.
Newer snapshots from the same bookmaker produce *new* rows (different
`captured_at`); duplicate-handling lives in §5.2.

### 5.2 `odds_match_results` — algorithm verdict per (slate, odds_row)

One row per (slate, odds_row). Rebuildable: a fresh matcher run can `DELETE`
all rows for a slate and re-insert without losing user intent (overrides live
in `manual_match_overrides`).

| Column                | Type     | Notes                                                                  |
| --------------------- | -------- | ---------------------------------------------------------------------- |
| `id`                  | INTEGER PK |                                                                      |
| `slate_id`            | INTEGER  | FK `slates(id)` ON DELETE CASCADE. NOT NULL.                           |
| `odds_row_id`         | INTEGER  | FK `odds_rows(id)` ON DELETE CASCADE. NOT NULL.                        |
| `odds_row_key`        | TEXT     | Denormalized from `odds_rows.odds_row_key` for stable joins to overrides. |
| `fighter_id`          | INTEGER  | FK `fighters(id)` ON DELETE SET NULL. Nullable for `unmatched` / ambiguous. |
| `match_status`        | TEXT     | Algorithm status string from `odds_matching.py`: `auto_match`, `review_required`, `unmatched`. Plus `shadowed` for duplicates (§5.5 of matching design). |
| `match_stage`         | TEXT     | `exact_conservative` / `exact_aggressive` / `fuzzy` / `none`.          |
| `match_score`         | INTEGER  | 0..100. Always 100 for exact-stage.                                    |
| `opponent_check`      | TEXT     | `passed` / `failed` / `unknown` / `not_applicable`.                    |
| `preferred_candidate` | TEXT     | Nullable. Mirrors `OddsMatchResult.preferred_candidate`.               |
| `candidates_json`     | TEXT     | Nullable. JSON array of plausible DK fighter names for ambiguous cases. |
| `notes_json`          | TEXT     | Nullable. JSON array mirroring `OddsMatchResult.notes`.                |
| `effective_status`    | TEXT     | Post-override state — see §8. Computed at write time of the result row, refreshed when overrides change. |
| `computed_at`         | TEXT     | `datetime('now')` at the matcher run that produced this row.           |

Constraints / indexes:

- `UNIQUE(slate_id, odds_row_id)`.
- `INDEX (slate_id, fighter_id)` — Manual Review per-fighter lookups.
- `INDEX (slate_id, effective_status)` — gate computation.

### 5.3 `manual_match_overrides` — user decisions, persistent

Records explicit user actions. Survives matcher re-runs and CSV re-imports
because we key on `odds_row_key` (stable across re-imports), not
`odds_rows.id`. Override types correspond exactly to
`ODDS_MATCHING_DESIGN.md` §6.1 and §7.

| Column            | Type     | Notes                                                                      |
| ----------------- | -------- | -------------------------------------------------------------------------- |
| `id`              | INTEGER PK |                                                                          |
| `slate_id`        | INTEGER  | FK `slates(id)` ON DELETE CASCADE. NOT NULL.                               |
| `odds_row_key`    | TEXT     | Nullable for fighter-level actions (`mark_excluded`, `manual_projection_low_confidence`). |
| `fighter_id`      | INTEGER  | FK `fighters(id)` ON DELETE CASCADE. Nullable when override is purely odds-row scoped (e.g. `reject_match` on an ambiguous row before pairing). |
| `override_type`   | TEXT     | One of: `accept_match`, `reject_match`, `force_pair`, `mark_excluded`, `manual_moneyline`, `manual_projection_low_confidence`. |
| `payload_json`    | TEXT     | Nullable. `{"moneyline": -150}` for `manual_moneyline`; `{"projection": 62.5, "acknowledged": true}` for low-confidence projection; `{"reason": "..."}` for `force_pair`. |
| `reason`          | TEXT     | Nullable free-text typed by the user.                                      |
| `created_at`      | TEXT     | `datetime('now')`.                                                         |
| `superseded_at`   | TEXT     | Nullable. Set when a later override replaces this one (soft-replace history). |

Constraints / indexes:

- `INDEX (slate_id, fighter_id) WHERE superseded_at IS NULL`.
- `INDEX (slate_id, odds_row_key) WHERE superseded_at IS NULL`.
- No hard `UNIQUE` on (slate, fighter, type) — soft-replace via
  `superseded_at` so the audit trail is intact. Effective row is "most
  recent where `superseded_at IS NULL`".

### 5.4 The existing `odds` table

Two options. The design leans (a).

**(a) Repurpose as the "current projection input" view.** Populated by the
projection service only for fighters whose effective state is terminal and
not `excluded`. One row per (slate, fighter) chosen from `odds_rows` via
match results + overrides. `no_vig_probability` materialized here at write
time. The raw history lives in `odds_rows`.

**(b) Drop the existing `odds` table.** Build the projection input from
`odds_rows + odds_match_results + manual_match_overrides` at read time. No
materialization.

(a) keeps `odds.no_vig_probability` meaningful (computed once per gate
pass) and survives the projection refactor without a join from three tables
each render. (b) is purer but pushes recomputation into the projection
service. Decide when the projection service is built; until then, both raw
rows and match results are kept independent of `odds`.

### 5.5 What is *not* a new table

- **No** `no_vig_pairs` table. No-vig probabilities are derived (§9).
- **No** `match_runs` history table. The matcher is deterministic for a
  given (slate roster, odds_rows, normalization version); replays are
  cheap. If we later need a history of runs (e.g. to debug "this used to
  auto-match"), add then.
- **No** `manual_odds_session` table. Manual entries promote into
  `odds_rows` on save (§5.1). The Streamlit session form remains a
  staging UI on top of the same table.

## 6. Relationships

### 6.1 odds rows ↔ slates

Every odds row is scoped to one slate. The slate is the natural boundary for
"a UFC card I am building lineups for." Cross-slate sharing is a non-goal.

**Open:** should `odds_rows.slate_id` be nullable to support uploading odds
*before* creating a slate? v0 lean: **no** — require a slate to be picked
before upload, mirroring how the Odds Matching Preview already requires a
slate to enrich opponent context. Simpler invariants, smaller code path.

### 6.2 match results ↔ DK fighters

`odds_match_results.fighter_id` is FK to `fighters(id)`. The DK roster is
the source of truth — match results dangle off it, not the other way around.
On a salary re-import that replaces fighters, `ON DELETE SET NULL` keeps the
result row but blanks the FK; the matcher should `DELETE WHERE fighter_id
IS NULL OR computed_at < salary_imported_at` and rerun (§11).

`odds_row_key` is denormalized onto the result row so `manual_match_overrides`
can be joined without going through `odds_rows.id`, which would be unstable
across re-imports.

### 6.3 overrides ↔ everything else

An override is keyed by `(slate_id, fighter_id, odds_row_key)` with
nullability so it can represent:

- "accept this match" → all three set.
- "reject this match" → all three set.
- "force this pair" → all three set.
- "exclude this fighter" → `fighter_id` set; `odds_row_key` NULL.
- "manual moneyline for this fighter" → `fighter_id` set; `odds_row_key`
  NULL; `payload_json.moneyline` filled.
- "manual low-confidence projection" → `fighter_id` set; `odds_row_key` NULL;
  `payload_json.projection` and `payload_json.acknowledged` filled.

## 7. Manual override flow (persistence-shaped)

For action semantics see `ODDS_MATCHING_DESIGN.md` §6. Persistence rules:

1. New override action → insert a new row in `manual_match_overrides`. If
   an active row exists for the same `(slate_id, fighter_id, override_type)`
   (or `(slate_id, odds_row_key, override_type)` for row-scoped types), set
   its `superseded_at`. Never UPDATE the older row's content.
2. Recompute `odds_match_results.effective_status` for affected slate / rows
   (§8). This is a small targeted recompute, not a full re-match.
3. A `mark_excluded` clears the fighter from the projection pool regardless
   of what other overrides exist; later switching away requires inserting a
   new override that supersedes the exclude.
4. `manual_moneyline` does **not** create a new `odds_rows` row — moneyline
   data lives in `payload_json`. The projection service reads from
   overrides when `effective_status = 'manual_moneyline'`.

   Rationale: keeps `odds_rows` literally "what came from a CSV / form" and
   avoids inventing a synthetic `odds_row_key` for a value that has no
   bookmaker / timestamp provenance.

   *Counter-option (deferred):* normalize manual moneylines into `odds_rows`
   with `source = 'manual_override'`. Easier to project but blurs raw vs.
   override. Revisit if the projection service ends up wanting one
   uniform input table.

5. A `force_pair` override implies an `accept_match` for the same pair;
   storing both is redundant. Use `force_pair` whenever the score is below
   `REVIEW_MATCH_THRESHOLD`, `accept_match` otherwise.

## 8. Statuses: algorithm vs. effective

Two layers, persisted separately, both deterministic.

- `odds_match_results.match_status` — the matcher's raw verdict:
  `auto_match`, `review_required`, `unmatched`, `shadowed`. Set at match
  time, untouched by overrides.
- `odds_match_results.effective_status` — what Manual Review and the slate
  gate read: the matcher verdict *after* applying any active overrides.
  Recomputed whenever overrides change.

Resolution rule, top-down:

1. `mark_excluded` for the candidate fighter → `excluded`.
2. `reject_match` for this (fighter, odds_row_key) → `review_rejected`.
3. `force_pair` for this (fighter, odds_row_key) → `force_pair`.
4. `accept_match` for this (fighter, odds_row_key) → `review_accepted`.
5. `manual_moneyline` for this fighter → `manual_moneyline`.
6. `manual_projection_low_confidence` for this fighter with
   `acknowledged=true` → `manual_projection_low_confidence_ack`; without →
   `manual_projection_low_confidence_pending`.
7. No active override → mirror `match_status`.

`effective_status` is what the slate-gate query reads to decide
"optimizer-ready." That keeps the gate a single SELECT predicate per slate
instead of a stored procedure replaying overrides.

Persistence shape: `effective_status` is a column on
`odds_match_results` rather than a view, because (a) the override-set is
small but the gate is read often, and (b) it lets us index on
`(slate_id, effective_status)` cheaply.

## 9. No-vig probabilities — store or recalculate?

**Lean: recalculate, do not store as a separate column on `odds_rows`.**

Reasoning:

- No-vig depends on both sides of a fight. At insert time of one
  `odds_rows` row the opposite side may not exist yet.
- Recompute is `O(1)`: two `american_to_implied_probability` calls plus
  `no_vig_two_way`. Cheaper than the index maintenance cost of keeping
  pairs in sync.
- Raw rows stay immutable.

Where it gets cached:

- The `odds` table (if kept per §5.4 option (a)) materializes
  `no_vig_probability` once per gate-pass / projection refresh — that's
  the natural cache because the projection service is the only consumer.
- The Streamlit preview (already shipped in `256054f`) keeps doing
  in-render computation, no cache needed.

Pairing rule for compute: two `odds_rows` rows are a no-vig pair when
their `effective_status`-attached `fighter_id`s sit on the same row of
`fight_groups` (or `fights`, once that table is populated). For fights
where only one side has odds, fall back to raw implied probability and
emit `SINGLE_SIDE_ODDS` (deferred — see open question §13.4 below and
matching design §11.7).

`fight_groups` opponent-name matching is the same pipeline as the
matcher uses elsewhere (`normalize_name` + aggressive fallback) — no new
helper required.

## 10. Validation rules

Hard validation (reject on insert):

- `odds_rows.fighter_name_raw` non-empty after `.strip()`.
- `odds_rows.american_odds` integer ≠ 0.
- `odds_rows.captured_at` parseable as ISO-8601 (UTC preferred but not
  required; store as-is).
- `odds_rows.source` non-empty.
- `odds_rows.implied_probability` in `(0.0, 1.0)` after computation.
  Anything outside is a corrupt moneyline; reject.
- `manual_match_overrides.override_type` ∈ the allowed set in §5.3.
- `manual_match_overrides.payload_json` shape per override type:
  - `manual_moneyline` requires `payload_json.moneyline` integer ≠ 0.
  - `manual_projection_low_confidence` requires `payload_json.projection`
    finite float ≥ 0 and `payload_json.acknowledged` boolean.
  - `force_pair` MAY include `payload_json.reason`; not required.

Soft validation (warn, don't block):

- Mixed bookmakers for one fighter with widely different implied
  probabilities — same threshold as `DUPLICATE_ODDS_ROW` in the matching
  design (0.03 absolute, also open).
- Two overrides of conflicting types active at once (e.g. `mark_excluded`
  while a `manual_moneyline` is also active). Conflict resolution falls
  out of §8; the warning is purely UI hygiene so users can clean up stale
  overrides.

Cross-table invariants checked at gate-evaluation time, not insert time:

- Every `manual_match_overrides` row with `odds_row_key` should resolve to
  at least one `odds_rows.odds_row_key` on the same slate, OR be a
  fighter-level override (`odds_row_key IS NULL`). If neither, surface as
  a stale override.
- Every `odds_match_results.fighter_id` must point at a current row in
  `fighters`. On salary re-import this is what the `ON DELETE SET NULL`
  + recompute pass enforces.

## 11. Reset behavior

What survives, what gets blown away.

| Event                                | `odds_rows` | `odds_match_results` | `manual_match_overrides` |
| ------------------------------------ | ----------- | -------------------- | ------------------------- |
| Slate deleted                        | cascade del | cascade del          | cascade del               |
| Salary CSV re-imported (same slate)  | kept        | recomputed (delete + reinsert) | kept; stale `fighter_id` references invalidated on next gate eval |
| Odds CSV re-uploaded (same rows)     | no-ops via `UNIQUE(slate_id, odds_row_key)` | recomputed         | kept                      |
| Odds CSV re-uploaded (new rows)      | insert new  | recomputed for new rows; existing rows untouched unless roster changed | kept |
| Fight group status flipped (`unconfirmed` → `confirmed` or vice versa) | kept | targeted recompute of `effective_status` for affected fighter pair; opponent_check may flip | kept |
| Fight group deleted                  | kept        | targeted recompute: pair loses opponent context, `opponent_check` may flip to `unknown` | kept |
| Manual override added / superseded   | kept        | targeted recompute of affected rows' `effective_status` | the override itself is the change |
| Normalization rules version bumped (`name_matching` constants change) | kept | full recompute for all affected slates | kept |

**Targeted recompute** = update one or more `odds_match_results` rows for a
single slate; do not touch other slates' data. Implementation lives in the
match service layer, not in SQL triggers.

**Salary re-import recompute** is the only "blow away match results"
operation. It happens because fighter IDs may change. Overrides are
referenced by `odds_row_key` (stable) and `fighter_id` (potentially
unstable across re-import) — the recompute pass must re-resolve
overrides keyed by name when fighter IDs shift. See §13.3 (open).

## 12. Suggested implementation phases

Smallest useful slices, each individually shippable. Each phase passes its
own tests before the next begins.

**Phase A — schema migrations only.** Add `odds_rows`, `odds_match_results`,
`manual_match_overrides` to `src/db/schema.py` and `src/db/migrations.py`.
No repositories, no app wiring. Adds unit tests that `apply_schema`
succeeds and the tables accept the documented columns. **Does not** touch
the existing `odds` table.

**Phase B — `odds_rows` write path.** Implement `OddsRowRepository.insert`
and wire the CSV importer + manual entry form to persist via that
repository, scoped to a selected slate. Validation rules from §10 enforced
here. CSV re-upload remains idempotent via `UNIQUE(slate_id, odds_row_key)`.
The session-state staging in the Streamlit form stays as today; it just
flushes to the repository on save.

**Phase C — `odds_match_results` write path.** A small match service that
takes a slate, loads its DK roster + `odds_rows`, runs
`match_odds_to_dk(...)`, and persists the results. Idempotent: delete +
reinsert per slate. No overrides applied yet — `effective_status` mirrors
`match_status` exactly. **See §14 for the detailed flow** — trigger model,
source-row restriction, pre-salary behavior, JSON encoding, gate
implications, and a finer phase split (C.1–C.6).

**Phase D — `manual_match_overrides` + `effective_status`.** Implement
override insertion (with supersession) and the recompute pass that updates
`effective_status` on affected `odds_match_results` rows. Wire a minimal
Manual Review surface on the existing page to insert overrides.

**Phase E — projection input view.** Decide §5.4 (a) vs (b), implement
the chosen path. No-vig materialization (or recompute) lives here. This is
the phase that lets the projection service start consuming odds.

**Phase F — slate gate.** Implement the gate predicate over
`effective_status` and wire optimizer + export entry points to refuse to
run when the gate fails. Pulls in the matching-design §8.2 list.

Each phase is one PR at most. Phases A–C can run before the projection
service exists; D–F can wait until the user has actually wanted overrides
in real data.

## 13. Risks and open questions

1. **Existing `odds` table fate.** §5.4 leans (a) repurpose, but the
   decision can wait until the projection service is being built. Phases
   A–D do not touch it.

2. **Slate-less odds upload.** §6.1 makes `slate_id` required. If a user
   wants to upload odds before creating a slate, they'd have to wait. v0
   accepts that ergonomics cost in exchange for simpler invariants. Revisit
   if it becomes a real friction point.

3. **Override migration across salary re-imports.** When the DK salary CSV
   is re-imported and `fighters.id` changes, overrides keyed by
   `fighter_id` need to be re-pointed. Two approaches:
   - **(i)** Resolve overrides at read time by `(slate_id,
     normalize_name(fighter_name))` instead of `fighter_id`. Cheap, but
     forces every read path through a join on the normalized name.
   - **(ii)** Run a one-shot remap pass during salary import that updates
     `fighter_id` on active override rows using a name-match lookup.
     Cleaner queries downstream, but a name change between imports could
     orphan an override.

   v0 lean: **(ii)**, with orphaned overrides surfaced as a UI warning the
   user can resolve in Manual Review.

4. **No-vig for single-side fights.** Already an open item in
   `ODDS_MATCHING_DESIGN.md` §11.7. Persistence implication: if we choose
   to emit a `SINGLE_SIDE_ODDS` value, we need a column for "no-vig was
   skipped, raw implied used" somewhere — likely on the
   projection-input row (§5.4) rather than on the raw `odds_rows`.

5. **`odds_row_key` collisions across slates.** The key formula includes
   no slate component (matching design §6.2). Within one slate the
   `UNIQUE(slate_id, odds_row_key)` constraint is fine; across slates two
   slates uploading the same source / timestamp / fighter would produce
   identical keys but separate rows. The `(slate_id, odds_row_key)`
   composite is the canonical handle everywhere — guard against any
   accidental cross-slate key use in repository code.

6. **`captured_at` time zones.** The CSV `timestamp` field is whatever the
   source emits. We store as-is. If two snapshots from different sources
   disagree on offset format, duplicate detection across them is unreliable.
   v0 lean: document the limitation, leave normalization for later.

7. **Index versioning for normalization changes.** `name_matching`
   constants and `normalize_name_aggressive` are code; the
   `fighter_name_normalized` column is data computed from them. A change
   to the normalizer changes the data layer. Either (a) recompute the
   column on app start when a `normalization_version` constant bumps,
   (b) derive the column at query time and drop the cache. v0 lean: (a)
   with the version constant stored in `slates` row or a tiny
   `app_metadata` key/value table — design that when it actually matters,
   not now.

8. **Audit / undo for overrides.** Soft-replace via `superseded_at` keeps
   history but there's no UI for it. Acceptable for v0 — the history is
   inspectable via SQL if a user reports an unexpected match.

9. **Manual moneyline storage location.** §7 keeps `manual_moneyline`
   inside `payload_json` rather than promoting it into `odds_rows`. The
   counter-option (synthetic `odds_row` with `source = 'manual_override'`)
   is noted in §7. Either is workable; the lean is "keep raw and override
   tables semantically distinct."

10. **Shadowed-duplicate deferral.** Phase C does NOT emit
    `match_status = 'shadowed'` even though `ODDS_MATCHING_DESIGN.md` §5.1
    defines it. The current matcher (`src/ingestion/odds_matching.py`)
    returns one independent result per odds row; the duplicate-resolution
    pass that would mark losers as `shadowed` is not implemented. Until
    Manual Review (Phase D) lands, two odds rows targeting the same DK
    fighter both persist with their own `match_status` and `fighter_id`,
    and downstream UI must group by `fighter_id` before display.

11. **Phase C dependency on a read-side `FighterRepository`.**
    `src/db/repositories.py` currently leaves `FighterRepository` as a
    TODO. Phase C cannot run without at minimum a
    `FighterRepository.list_for_slate(slate_id)` read-side that returns
    `(name, status)` tuples. Either ship that read-side as a "Phase B.5"
    or fold it into Phase C.1 (see §14.12). Either choice is fine; the
    risk is silently coupling Phase C to whichever salary-importer
    write-path PR lands first.

12. **Empty DK roster behavior.** §14.3 leans "refuse to recompute when
    `fighters` is empty for the slate." The counter-position — persist a
    bag of `unmatched` results and let the user see "12 odds rows have no
    DK roster yet" — is also workable but writes throwaway rows. Decide
    when the user actually hits the friction. v0 lean: refuse, point the
    user at the salary importer.

13. **Staleness between recompute clicks.** Phase C uses an explicit
    user trigger (§14.1). Between clicks the persisted match results may
    be older than the inputs (new odds_rows saved, fight group flipped,
    salary re-imported). v0 lean: surface a "results last computed at X
    / latest input at Y" caption on the persisted-view panel rather than
    auto-recomputing. Auto-recompute is reserved for Phase D+.

14. **Asymmetry between manual and CSV-origin odds rows.** Phase B saves
    both into `odds_rows`, but only CSV rows carry `opponent_name_raw`
    today. Manual entries therefore always land at
    `opponent_check = 'not_applicable'` after Phase C runs. The Phase D
    Manual Review surface will need an "edit opponent for this manual
    odds row" affordance to let the user supply the cross-check input
    after the fact — or accept that manual entries never contribute to
    opponent verification.

## 14. Phase C — `odds_match_results` write path: detailed flow

This section drills into the Phase C bullet from §12. It is design only
— no code changes are landing with this update. The current Odds
Matching Preview (`app/pages/03_odds.py`) remains preview-only,
in-memory, and unaffected until §14.6 is explicitly approved.

### 14.1 When match results are generated

**Explicit user trigger only.** The flow is `User clicks "Recompute
match results for slate" → service runs → DB write happens.` No
automatic generation on:

- Odds CSV upload (validated rows are saved to `odds_rows` but the
  matcher does not run).
- Manual odds save (the Save Manual Odds button writes to `odds_rows`
  but does not run the matcher).
- Fight group create / status flip.
- Salary CSV re-import.
- Streamlit page load.

Rationale: a per-save matcher run would (a) rebuild N times on a single
multi-row CSV upload, (b) silently mutate persisted state under the
user's feet, and (c) make it impossible to keep the preview-only odds
matching behavior intact while the write path is being landed. Phase
C's user-visible contract is "you click; we recompute the whole slate."

Allowed programmatic triggers in v0:

- A test fixture or CLI helper invoking the persistence service
  directly. Required for §14.12 phases C.2–C.4 even before any UI
  trigger lands.

Disallowed in v0:

- Background schedulers.
- Streamlit `st.rerun()`-triggered recomputes.
- File-watcher / inotify auto-runs on the SQLite file.

### 14.2 Source rows: `odds_rows` only

The matcher service reads exclusively from
`OddsRowRepository.list_for_slate(slate_id)`. The session-only
`st.session_state.manual_odds_entries` list is **not** an input. A user
who wants their hand-typed line to feed the persisted matcher must
click the existing Save Manual Odds button first to promote it into
`odds_rows`.

Reasons:

- `odds_rows` is durable, slate-scoped, and idempotent across re-imports
  (Phase B established this). Session state is transient and
  per-browser; leaking it into persisted match results would create
  artifacts that cannot be reproduced.
- It keeps the data contract simple: "everything the matcher knows is
  in the DB."
- It preserves the read-only nature of the preview pane — that pane can
  continue to operate on session entries without ever touching the
  write path.

Load order for the service:

1. `odds_rows` for the slate, sorted by `(imported_at ASC, id ASC)`
   (already the repository's order).
2. DK fighter names for the slate (see §14.3).
3. Fight groups for the slate (see §14.5).

### 14.3 DK fighter candidates before persisted salary rows exist

Two operating modes:

**Mode A — salary-imported (normal case).** The matcher's `dk_fighters`
list is `[f.name for f in FighterRepository.list_for_slate(slate_id)
where status='active']`. Excluded fighters never appear as candidates
(they cannot win a match).

**Mode B — pre-salary / no `fighters` rows for the slate.** v0 lean:
**refuse to compute.** The service raises a structured "no DK roster"
error; the UI surfaces "Import the salary CSV before recomputing." The
preview still works because the user can paste DK names by hand — the
persisted path requires the durable roster.

Rejected alternatives:

- Persisting a slate full of `unmatched` rows in Mode B. Cheap to do but
  buys nothing the user can act on, and forces a DELETE + rebuild on
  the next run. Throwaway writes.
- Using `fight_groups.fighter_1_name` / `fighter_2_name` as a stand-in
  roster. Those names are user-entered free text intended for the
  *odds-row* opponent cross-check, not authoritative DK identity. Using
  them as candidates would muddle the matcher's invariant that "DK
  roster is source of truth" (matching design §0).

Open: cross-mode behavior when a slate has fighters but is missing some
DK names that exist in `odds_rows`. Those odds rows resolve to
`unmatched` per the matcher today — no special handling.

### 14.4 Status representation

The matcher already emits the three persisted strings used by §5.2:

- `auto_match` (matcher constant `STATUS_AUTO`),
- `review_required` (matcher constant `STATUS_REVIEW`),
- `unmatched` (matcher constant `STATUS_UNMATCHED`).

Phase C writes those verbatim into `odds_match_results.match_status`.
No renames; no enum table; no string remapping.

`excluded` is **never** a Phase C value. It exists only as an
`effective_status` produced by an active `mark_excluded` override
(§8 rule 1). Phase C has no overrides → `effective_status = match_status`
→ `excluded` does not appear.

`shadowed` is **deferred** (§13.10). Phase C persists every odds row's
verdict independently; duplicate-resolution is Phase D's problem.

### 14.5 Opponent context from fight groups

Phase C delegates to the same logic the Odds Matching Preview already
uses: `_build_opponent_context(...)` currently lives in
`app/pages/03_odds.py` (lines 48–92). Phase C **lifts** that helper
into a service module (e.g., `src/ingestion/odds_matching_service.py`)
so the persistence pass and the preview share one implementation. The
preview keeps calling it identically; no behavior change in the
preview.

Inputs to the helper:

- DK fighter names (§14.3).
- `FightGroupRepository.list_for_slate(slate_id)` rows.

Rules already baked into the matcher (per `ODDS_MATCHING_DESIGN.md`
§4 / `odds_matching.py`):

- Only `status = 'confirmed'` fight groups can demote `auto_match` to
  `review_required` on opponent mismatch.
- Unconfirmed fight groups record the disagreement in `opponent_check`
  but do not demote.
- When the odds row has no `opponent_name_raw`, `opponent_check` is
  `not_applicable`.
- When fight groups don't cover the matched DK fighter,
  `opponent_check` is `unknown`.

No Phase C-only logic on top of this. The service just plumbs context
in and stores what the matcher returns.

### 14.6 `match_status` vs `effective_status` in Phase C

Phase C populates **both** columns; they always carry the same value
inside Phase C. Specifically, the service computes once per row:

```
effective_status := match_status
```

Why store the redundant column:

- The schema declares `effective_status NOT NULL` already (§5.2), and
  the index `idx_odds_match_results_slate_effective_status` exists.
  Filling it now keeps the gate query a single column SELECT both now
  and after Phase D ships.
- Phase D needs only to alter the recompute pass — no schema change,
  no migration, no read-path refactor.
- Storage cost is one TEXT column per match result row. Negligible.

Rejected alternative: `effective_status` left NULL until Phase D. Forces
COALESCE in every gate query, breaks the NOT NULL constraint, and adds
a no-op migration when Phase D arrives.

### 14.7 Recomputation behavior in Phase C

The §11 reset-behavior table is the *eventual* policy. Phase C
implements only the manual-trigger row of that table:

| Event                            | Phase C behavior                |
| -------------------------------- | ------------------------------- |
| User clicks "Recompute"          | Full DELETE + reinsert per slate. |
| Odds CSV uploaded / saved        | No automatic recompute. Persisted results may go stale (§13.13). |
| Manual odds saved                | Same — no automatic recompute.  |
| Fight group created / flipped    | No automatic recompute.         |
| Salary CSV re-imported           | No automatic recompute. `fighter_id` FKs may be set NULL by `ON DELETE SET NULL`; user must click Recompute. |
| Normalization rules version bump | Out of scope.                   |

Targeted / incremental recompute (single-row updates when one input
changes) requires override-awareness, which is Phase D. Phase C ships
the simplest correct thing: full rebuild per click.

### 14.8 Write strategy: delete + rebuild per slate

For each "Recompute" trigger, the service runs in one transaction:

1. `DELETE FROM odds_match_results WHERE slate_id = ?`.
2. Compute the matcher verdict for every odds_row.
3. `INSERT` one row per (odds_row, verdict). `effective_status =
   match_status` per §14.6.
4. `COMMIT`. On any error, the transaction rolls back and the prior
   persisted results remain intact.

Rejected alternatives:

- **Soft-replace with a `computed_at` "active row wins" filter.** Adds
  read-path complexity for an audit trail nobody is asking for. Phase C
  is deterministic — re-running gives identical output for unchanged
  inputs. If we ever need history of runs, add then (per §5.5).
- **UPDATE-by-(slate, odds_row_id) overwrite.** Identical end state but
  more code, more partial-state risk if an `odds_row` was deleted
  between runs.

Idempotency property worth preserving: re-clicking Recompute with no
input changes produces byte-identical rows except for `computed_at`.

### 14.9 `candidates_json` and `notes_json` encoding

Both columns already exist on `odds_match_results`. Phase C writes:

- **`candidates_json`** — JSON-serialized array of DK fighter name
  strings when the matcher's `OddsMatchResult.candidates` tuple is
  non-empty (ambiguous aggressive-exact or tied-fuzzy cases). Format:
  `["Conor McGregor", "Conor McKenna"]`. NULL when the matcher produced
  no candidate set (the common case — exact + single fuzzy top result).
- **`notes_json`** — JSON-serialized array of note strings from
  `OddsMatchResult.notes`. Format examples: `["empty_fighter"]`,
  `["ambiguous_fuzzy", "opponent_supported_disambiguation"]`,
  `["opponent_mismatch"]`. NULL when empty.

Why JSON arrays:

- The matcher already returns tuples of strings; `json.dumps(list(t))`
  is the entire serializer.
- pandas / Streamlit can `json.loads(...)` once and render columns
  natively for the Manual Review UI.
- No schema change required; the columns are already TEXT.

Stable ordering: the matcher emits candidates and notes in a
deterministic order. Phase C preserves that order; the service does
NOT re-sort. This keeps `computed_at` the only field that drifts
between identical runs.

Not duplicated:

- `preferred_candidate` already has its own TEXT column; it is NOT
  copied into `candidates_json`.
- `match_status` / `match_stage` / `match_score` / `opponent_check`
  remain their own top-level columns; do not embed them in notes_json.

### 14.10 Gate consequences during Phase C

Per `ODDS_MATCHING_DESIGN.md` §8.2, the slate gate requires every
active fighter to be in a terminal state. Phase C can produce only
three statuses: `auto_match`, `review_required`, `unmatched`. Of those,
only `auto_match` is terminal.

Consequence: until Phase D ships the override types (`accept_match`,
`force_pair`, `manual_moneyline`, etc.), the gate can pass for a slate
only if 100% of active fighters fall into `auto_match`. That is rare
in real data. Phase C explicitly does NOT implement the gate
predicate, NOT block the optimizer, and NOT feed projections — those
are downstream of Phase D.

What Phase C does NOT do:

- Render an "optimizer-ready" badge.
- Populate the `odds` table (§5.4 decision is still deferred).
- Compute no-vig probabilities or projection inputs.
- Block the optimizer entry point. (The optimizer itself is still a
  stub — there is nothing to block.)
- Emit mismatch alerts. Alerts are a separate doc (§3 non-goals;
  `ODDS_MATCHING_DESIGN.md` §8.3).

What Phase C *does* deliver to downstream phases:

- A canonical, durable, slate-scoped table of matcher verdicts that
  Phase D's override pass can layer on top of.
- A stable `(slate_id, odds_row_key)` join target for
  `manual_match_overrides`.
- Persisted `candidates_json` / `notes_json` so the Manual Review UI
  can render ambiguity context without re-running the matcher.

### 14.11 Preview vs persisted: coexistence

The Odds Matching Preview (lower section of `app/pages/03_odds.py`)
stays exactly as it is until Phase C.6:

- Reads `st.session_state.manual_odds_entries` (not `odds_rows`).
- Computes match results in memory via `match_odds_to_dk(...)`.
- Renders results in a dataframe.
- Writes nothing.
- Is clearly labeled "Preview only — match results are NOT persisted."

The Phase C UI (Phase C.5+) is a *separate panel* on the same page that
reads persisted `odds_match_results` for a selected slate. The two
panels intentionally show different things: the preview reflects the
in-browser typing the user is doing; the persisted panel reflects the
last "Recompute" click. Operators read both: one for hypothesis ("what
if I added these names?") and one for the durable record ("what did
the matcher decide last time?").

Naming proposal for the persisted panel:
**"Persisted Match Results — read-only"** (mirrors the
already-shipped "Persisted Odds Rows — read-only" panel).

### 14.12 Suggested phase split (refines §12's Phase C bullet)

Each slice is individually shippable, has its own tests, and changes a
known small surface. Slices C.1–C.4 do not touch
`app/pages/03_odds.py`.

**C.1 — `FighterRepository` read-side.** Implement
`FighterRepository.list_for_slate(slate_id) -> list[FighterRecord]`
returning `(id, name, status)`. No write path here (salary importer
write side is its own work). Tested in isolation against an in-memory
DB with seeded fighter rows. Dependency for §13.11.

**C.2 — Pure persistence service.** New module
`src/ingestion/odds_matching_service.py` exporting
`compute_match_results(slate_id, *, conn) -> list[OddsMatchResultRecord]`.
Loads odds_rows, fighters, fight groups; calls `match_odds_to_dk(...)`;
builds in-memory `OddsMatchResultRecord` dataclasses. Does NOT write to
the DB. The lifted `_build_opponent_context(...)` helper lives here.
Tested end-to-end with seeded inputs (no Streamlit involved).

**C.3 — `OddsMatchResultRepository` write path.** Implement
`replace_for_slate(slate_id, records)` (DELETE + INSERT in one
transaction). Idempotent per slate. Validation rules: `match_status` ∈
the matcher's three constants, `match_stage` ∈ the documented set,
`match_score` ∈ [0, 100], JSON columns parse as arrays or are NULL.
Tested against an in-memory DB.

**C.4 — Test driver / CLI helper.** A pytest fixture or small script
that wires C.2 → C.3 end-to-end against a seeded slate. Lets us
validate full persistence without any UI changes. No Streamlit
modifications.

**C.5 — Read-only persisted display.** Add a read-only panel
("Persisted Match Results — read-only") to `app/pages/03_odds.py`
that selects a slate and renders the persisted
`odds_match_results` rows as a dataframe. **No** Recompute button
yet. The preview pane is untouched. Mirrors the Persisted Odds Rows
display already shipped.

**C.6 — Recompute trigger (gated, explicit approval required).** Add a
"Recompute match results for this slate" button next to the persisted
view. The button is disabled when (a) the slate has zero
`odds_rows`, (b) the slate has zero active `fighters`. A confirmation
dialog ("Will delete N existing match results and rebuild") is shown
on click. **This is the first slice that writes to
`odds_match_results` from the UI** — pause for explicit user approval
before landing.

Downstream phases (already in §12) remain:

- **Phase D** — `manual_match_overrides` + `effective_status`
  recomputation. First slice where `effective_status` diverges from
  `match_status`.
- **Phase E** — projection input view (§5.4 decision).
- **Phase F** — slate gate predicate + optimizer / export refusal.

### 14.13 Invariants Phase C must preserve

A checklist the reviewer can grep for when Phase C lands:

1. `app/pages/03_odds.py` Odds Matching Preview behavior is byte-identical
   until C.5. C.5 only adds a new panel; it does not alter the preview.
2. `odds_rows` table is read-only from the matcher's perspective. The
   service NEVER mutates raw odds rows (consistent with §5.1
   immutability).
3. `odds_match_results` is written only through
   `OddsMatchResultRepository.replace_for_slate(...)`. No direct
   INSERTs from page code.
4. `effective_status` equals `match_status` for every row Phase C
   writes. The first divergence is Phase D.
5. No row in `odds_match_results` is written without a matching
   `odds_rows.id` (FK + `ON DELETE CASCADE` already enforce this; the
   service must respect it).
6. No row in `odds_match_results` is written with
   `fighter_id` pointing at a fighter on a *different* slate. The
   service constructs candidates only from `FighterRepository.
   list_for_slate(slate_id)`.
7. No projection / optimizer / export code reads `odds_match_results`
   in Phase C. Those readers come online in Phases E and F.

## 15. Phase D.4 — `effective_status` override integration

This section drills into the `effective_status` portion of the Phase D
bullet from §12. It is design only — no code changes are landing with
this update.

Background. The earlier D.x slices already shipped:

- D.1 — `manual_match_overrides` schema + `ManualMatchOverrideRepository`
  read/write (supersession via `superseded_at`).
- D.2 — `reject_match` write path on `review_required`
  `odds_match_results` rows.
- D.3 — Active Overrides panel on the Odds page rendering active
  reject rows.

Today the reject override is **inert**: it lands in the DB and renders
in the Active Overrides panel, but `odds_match_results.effective_status`
still equals `match_status` (Phase C's invariant, §14.6). D.4 wires the
override into `effective_status` so the data layer reflects the user's
intent. Projections, optimizer, alerts, and exports remain unwired —
those are Phases E and F.

Only `reject_match` is implemented in code today. The §8 rule table is
the long-term target. D.4 designs an integration shape that accommodates
the other override types as later slices (D.5+) without further schema
or repository churn.

### 15.1 Resolution rule order

D.4 implements a subset of §8 — specifically rules 2 and 7:

2. `reject_match` for this (slate_id, odds_row_key) → `review_rejected`.
7. No active override for this row → mirror `match_status`.

Rules 1 (`mark_excluded`), 3 (`force_pair`), 4 (`accept_match`),
5 (`manual_moneyline`), 6 (`manual_projection_low_confidence`) remain
unimplemented and reserved for D.5+. The resolver must take the form of
a top-down dispatch ordered exactly as §8 lists, with unimplemented
rules as explicit TODO branches that fall through to rule 7's
mirror-`match_status` behavior. Adding a new override type later is
purely additive: implement its branch, the precedence is already
correct.

Out of scope for D.4: testing inter-rule precedence — only one rule is
exercisable until D.5+ lands a second override type.

### 15.2 Recompute trigger

D.4 introduces a **targeted recompute** (per §11's "manual override
added / superseded" row). The trigger is the override write path
itself. Every successful insert (or supersession) of a `reject_match`
row in `manual_match_overrides` causes the same service call to
recompute and persist `effective_status` for the affected
`odds_match_results` row(s) inside the same transaction.

Disallowed in D.4:

- Background recompute on app start.
- Recompute on odds CSV import / manual odds save / fight group flip
  (those remain Phase C's explicit "Recompute match results" surface;
  D.4 does not alter Phase C's user-visible trigger).
- SQL triggers.
- Streamlit `st.rerun()`-bound auto-recomputes outside the override
  service call.

Rationale: the override service becomes the single writer of
`effective_status`, preserving the invariant "every `effective_status`
change has a corresponding override insertion."

### 15.3 Recompute scope

A reject override targets exactly one (slate_id, odds_row_key,
fighter_id) tuple. D.4's recompute updates only the matching
`odds_match_results` row(s) — typically one, but the implementation
must not assume a singleton in case the same `odds_row_key` ever
resolves to multiple result rows on a slate.

Out of scope:

- Recomputing `effective_status` for unrelated rows on the same slate.
- Recomputing matcher verdicts (`match_status`, `match_stage`, etc.) —
  those are Phase C's outputs and D.4's inputs.
- Cross-slate cascades — overrides are slate-scoped (§5.3).

### 15.4 Stale override handling

A reject override is **stale** when it cannot be projected onto a
current `odds_match_results` row. Causes:

- Phase C's DELETE + reinsert (§14.8) produced no result for the
  override's `odds_row_key` (e.g. the `odds_rows` row was removed by
  re-uploading a different CSV).
- A salary re-import nulled `fighter_id` on the matching result row
  (§6.2) — the result row still exists, the override still applies
  by `odds_row_key`, but the fighter linkage is gone (see §15.11.6).
- The override was inserted against an `odds_row_key` with no
  corresponding result row (shouldn't happen via the D.3 UI which gates
  inserts to `review_required` rows, but the data layer permits it).

D.4 behavior: stale overrides are **not** auto-superseded.
`apply_overrides(slate_id)` returns the list of stale override IDs so
the Active Overrides panel can render a "stale — no matching result"
badge next to them. The user decides whether to clear them; D.4 does
not invent silent cleanup.

Open: dedicated stale lane vs. inline badge in the existing panel —
D.4 lean is inline badge; revisit if D.5's additional override types
crowd the panel.

### 15.5 Overrides before match results

A slate can hold an active reject override while having zero
`odds_match_results` rows (Phase C never run, or wiped). D.4 contract:

- The override row is independent of `odds_match_results` (§5.3) and
  remains untouched.
- The recompute pass is a no-op — there is no row whose
  `effective_status` could be updated.
- The Active Overrides panel still surfaces the override (D.3 behavior)
  and tags it stale (§15.4).

D.4 does NOT synthesize an `odds_match_results` row to host the
override, and does NOT auto-trigger Phase C. The user must click
Phase C's "Recompute match results" first; D.4's apply step then layers
the override's effect inside that same transaction.

### 15.6 Slate recompute behavior

Per §14.7 / §14.8, Phase C's "Recompute" trigger is DELETE + reinsert
per slate in one transaction. D.4 extends that flow:

1. (Phase C, unchanged) `DELETE FROM odds_match_results WHERE slate_id = ?`.
2. (Phase C, unchanged) Compute matcher verdicts and INSERT rows with
   `effective_status = match_status`.
3. (**D.4 new**) Load active reject overrides:
   `SELECT … FROM manual_match_overrides WHERE slate_id = ?
    AND override_type = 'reject_match' AND superseded_at IS NULL`.
4. (**D.4 new**) For each override, locate the matching result row via
   `(slate_id, odds_row_key)` and
   `UPDATE odds_match_results SET effective_status = 'review_rejected'
    WHERE id = ?`. Overrides with no matching row are tracked as stale.
5. COMMIT. On error, the entire sequence rolls back; prior persisted
   results survive intact.

§14.8's idempotency property is preserved: re-clicking Recompute with
no input and no override changes yields identical `effective_status`
values (only `computed_at` drifts).

Rejected alternative: run the override-apply pass in a second
transaction after Phase C commits. Widens the window where
`effective_status` is wrong and risks a visible flash of pre-override
state.

### 15.7 Projection-eligible statuses

D.4 does not implement projections. It does pin down which
`effective_status` values Phase E should treat as projection-eligible,
so the predicate doesn't need re-litigation:

- `auto_match`
- `review_accepted` (D.5+)
- `force_pair` (D.5+)
- `manual_moneyline` (D.5+)
- `manual_projection_low_confidence_ack` (D.5+)

Only `auto_match` is reachable in D.4 — no override type implemented
yet promotes a row into the other terminal states. The same predicate
gates optimizer entry, exports, and the §8.2 slate gate (matching
design); D.4 does not enforce any of them.

### 15.8 Projection-blocked statuses

Symmetric set — values Phase E must refuse and Phase F must block:

- `review_required`
- `unmatched`
- `review_rejected` (D.4 introduces this value to the persisted set)
- `excluded` (D.5+)
- `manual_projection_low_confidence_pending` (D.5+)
- `shadowed` (deferred, §13.10)

D.4's only contribution to this list is making `review_rejected` a
reachable persisted value.

### 15.9 Implementation phase split

Three thin slices, each individually shippable, each pausing for
explicit user approval before merge. None modify Phase C's matcher
service or the D.2 override write path.

**D.4.1 — Pure resolver.** New helper (likely
`src/ingestion/effective_status.py`, or co-located with
`odds_matching_service.py` — see §15.11.4) exporting
`resolve_effective_status(match_status, active_overrides_for_row) -> str`.
Pure function. No DB, no Streamlit. Implements §8 rules 2 and 7;
rules 1, 3, 4, 5, 6 are explicit TODO branches that fall through to
rule 7. Tested in isolation with table-driven cases.

**D.4.2 — Repository apply pass.** Extend
`OddsMatchResultRepository` (or the persistence service that wraps it)
with `apply_overrides(slate_id, *, conn) -> StaleOverrides` running the
§15.6 step 3–4 sequence inside an externally-provided transaction.
Returns the list of stale overrides so callers can surface them.
Tested against an in-memory DB with seeded results + overrides.

**D.4.3 — Wire into Phase C recompute + override insert.** Two
integration points:

1. Phase C's "Recompute" service call invokes `apply_overrides(slate_id)`
   inside its existing transaction, after the matcher-result inserts.
2. The D.2 override insert path invokes a targeted single-row apply
   for the affected `(slate_id, odds_row_key)` inside the override
   insert transaction.

Active Overrides panel (D.3) gains a stale badge derived from the
apply pass's stale list. No new UI panels, no new buttons. D.4.3 is
the first slice that flips reject overrides from inert to load-bearing
and requires explicit user approval before merge.

### 15.10 Test plan

Pure-function (D.4.1):

- `resolve_effective_status('review_required', [reject_match])`
  → `review_rejected`.
- `resolve_effective_status('auto_match', [reject_match])`
  → `review_rejected`. (The D.3 UI gates inserts to `review_required`
  rows, but the resolver must honor the §8 rule regardless of source
  `match_status` — this guards future UI relaxation.)
- `resolve_effective_status('unmatched', [reject_match])`
  → `review_rejected`.
- `resolve_effective_status(<any>, [])` → input `match_status`.
- `resolve_effective_status(<any>, [unimplemented_type])` → input
  `match_status` (fallthrough to rule 7 verified).

Repository (D.4.2):

- Seeded slate with three result rows (`review_required`, `auto_match`,
  `unmatched`) + one active reject override on the `review_required`
  row → `apply_overrides` updates exactly that row, leaves the other
  two at `effective_status = match_status`.
- Seeded slate with a superseded reject override (non-NULL
  `superseded_at`) → no row updated.
- Seeded slate with a reject override whose `odds_row_key` has no
  result row → returned stale list contains it; no row updated.
- Re-running `apply_overrides` is idempotent — second run is a no-op
  on byte-equal data.

Integration (D.4.3):

- Phase C recompute on a clean slate: assert
  `effective_status = match_status` on every row.
- Insert a reject override via the existing D.2 path: assert
  `effective_status = 'review_rejected'` on the affected row without
  re-clicking Recompute.
- Re-click Recompute after the override exists: assert the row stays
  `review_rejected` (the override is reapplied in step 3–4 of §15.6).
- Supersede the reject (manually set `superseded_at` in the fixture
  until D.5 lands a real superseder) and re-run `apply_overrides`:
  assert the row returns to `match_status`.
- AppTest: Active Overrides panel renders the "stale" badge when an
  override's `odds_row_key` is absent from `odds_match_results`.

Explicitly NOT in D.4's test plan:

- Multi-override-type precedence (no second type yet).
- Projection / optimizer / export changes.
- Cross-slate behavior (single-slate by construction).
- SQL-trigger correctness (none introduced).

### 15.11 Risks and open questions

1. **Single override type today.** D.4 exercises rules 2 and 7 only.
   The inter-rule precedence in §8 stays unverified end-to-end until
   D.5+. Mitigation: ship the resolver dispatch in §8 order so adding
   later rules is purely additive.

2. **Stale-override UX.** §15.4 leans inline badge in the existing
   Active Overrides panel. Counter-option: dedicated stale lane.
   Revisit if D.5's additional override types crowd the panel.

3. **Targeted vs. full-slate recompute on override insert.** §15.2
   picks targeted apply on insert to preserve the audit invariant
   (every `effective_status` change ↔ one override insertion). The
   simpler alternative — re-run `apply_overrides(slate_id)` for the
   whole slate on every insert — is cheaper to implement and harder
   to get subtly wrong, but blurs the invariant. Lean is targeted;
   revisit if drift bugs surface.

4. **Resolver module location.** D.4.1 leaves "co-locate with
   `odds_matching_service.py` vs. new `effective_status.py`" open.
   Deciding factor is whether the projection service (Phase E) will
   import the resolver — if yes, a dedicated module avoids an import
   cycle. Decide when D.5 or Phase E gets closer.

5. **`effective_status` enum enforcement.** Schema (§5.2) declares
   `effective_status` as TEXT with no CHECK constraint. D.4 writes
   only `auto_match`, `review_required`, `unmatched`,
   `review_rejected`. Open: add a CHECK constraint listing the full
   §8 value set, or rely on the resolver as the single point of
   truth? Lean: resolver-only; revisit if a stray INSERT pattern
   emerges outside the repository.

6. **`ON DELETE SET NULL` on `fighter_id` vs. active reject.** When a
   salary re-import nulls `fighter_id` on a result row (§6.2), an
   active reject override keyed on `(slate_id, odds_row_key)` still
   applies — the row's `effective_status` correctly becomes
   `review_rejected` independent of fighter linkage. But the Active
   Overrides panel may show the override against a fighter the panel
   can no longer name. Defer to §13.3's "orphaned overrides"
   remediation; D.4 does not invent its own.

7. **Downstream wiring still inert post-D.4.** Even after D.4,
   projections, optimizer, alerts, and exports continue to ignore
   `effective_status`. The user-visible change is limited to the
   persisted-results panel showing `review_rejected` and the Active
   Overrides panel reflecting the override's effect. Call this out
   in the D.4.3 PR description so the change is not over-claimed.

## 16. Phase D.5 — accept / force-pair override + projection-source promotion

Design only — no code lands with this section. It realizes the
`accept_match` / `force_pair` rows of §8's resolution table and the
`review_accepted` / `force_pair` entries of §15.7's projection-eligible
set. It also makes the **projection-source decision** that §15.7
deferred: which `effective_status` values Build / the projection input
layer actually read.

### 16.1 Motivation — the fake-fix trap (real smoke)

A manual B6 build exposed the gap. Two active DK fighters were dropped
from the optimizer pool with `projection_status =
missing_inputs:win_probability`:

- DK salary `Bruno Silva` vs odds paste `Bruno Gustavo da Silva`.
- DK salary `Santiago Luna` vs odds paste `Luan Santiago`.

Step 2 showed 24 odds rows, 22 auto-matched, 2 needing review. Those two
landed `review_required` / `unmatched` because the names diverge beyond
the matcher's auto bar (`odds_matching.py` §3). Today the Odds page
offers only `reject_match` (D.2/D.4) — there is **no** path to say "this
odds row *is* that fighter."

The trap: even if a user could flip `effective_status` for those rows,
the projection input layer
(`src/projections/projection_input_service.py`) reads
`match_status == 'auto_match'` **only** and keys win probability off the
result row's `fighter_id`. So the Odds page would look fixed while Build
still excluded the fighter, because (a) the projection predicate never
consults `effective_status`, and (b) for an `unmatched` row the result
row's `fighter_id` is `NULL` — there is no binding to project against.

D.5 closes both halves: a binding override **and** a projection-source
predicate that reads it. Shipping one without the other re-creates the
trap (see §16.14 warning).

### 16.2 Override types introduced

Two — both already in §5.3's allowed set and §8's rule table. D.5 does
not invent a third synonym; `assign_odds_row_to_fighter` is **not** a
new type — that action *is* `force_pair` (§7.5).

- **`accept_match`** — confirm a matcher-proposed pairing the matcher
  left at `review_required` (a fuzzy 88–94 row, or an ambiguous row the
  user resolves to the matcher's `preferred_candidate`). The bound
  fighter equals what the matcher already proposed.
- **`force_pair`** — bind an odds row to a fighter the matcher did
  **not** confidently propose: an `unmatched` row, or a
  `review_required` row where the user picks a fighter other than the
  matcher's preferred candidate. The Bruno Silva / Santiago Luna case.

Both resolve to a **projection-eligible** terminal state and behave
identically downstream (§16.9). The split is preserved for audit
fidelity and to honor §7.5's score-based boundary; the service derives
the type (§16.10), so the UI presents one control.

Out of scope for D.5 (still deferred, unchanged from §10): `mark_excluded`,
`manual_moneyline`, `manual_projection_low_confidence`. Their §8 rules
stay TODO fall-throughs.

### 16.3 What the override references (§5.3 columns)

| Column          | accept_match / force_pair                                            |
| --------------- | ------------------------------------------------------------------- |
| `slate_id`      | NOT NULL. The slate being built.                                    |
| `odds_row_key`  | NOT NULL. Row-scoped — the stable key of the odds row being bound.  |
| `fighter_id`    | NOT NULL. The **selected active DK fighter** to bind the row to.    |
| `override_type` | `accept_match` or `force_pair` (derived, §16.10).                   |
| `payload_json`  | NULL. No payload in D.5 (§7.5 allows an optional `reason` for       |
|                 | force_pair, but the dedicated `reason` column already carries it).  |
| `reason`        | Optional free-text audit note. Already supported.                  |

Opponent / fight-group context is **not** enforced at write time. The
user's explicit pick is authoritative — opponent agreement never
promotes or blocks a binding (mirrors `odds_matching.py`'s v0 lock:
"opponent agreement never promotes"). The UI MAY surface a soft,
non-blocking warning when the chosen fighter's fight-group opponent
disagrees with the odds row's opponent column (§16.10); it does not
gate the write.

### 16.4 Mutual exclusion across resolution overrides

`reject_match`, `accept_match`, and `force_pair` are **mutually
exclusive on one `odds_row_key`** — a single odds row cannot be both
rejected and bound. This refines §7's supersession scope, which in D.2
was per-`(slate_id, odds_row_key, override_type)` (same-type only).

D.5 rule: inserting any one of the three resolution overrides for a
`(slate_id, odds_row_key)` supersedes **every** active override of the
*resolution set* on that key, regardless of type. So:

- Assigning an odds row that was previously rejected supersedes the
  `reject_match` (this is the recovery path, not an error).
- Re-assigning to a different fighter supersedes the prior
  accept/force_pair.
- A later `reject_match` supersedes an active accept/force_pair.

At most one resolution override is therefore active per `odds_row_key`,
which makes §8's precedence ordering (reject > force_pair > accept) a
defensive belt rather than a live conflict path. The §8 order is kept as
the resolver's tiebreak in case a future writer leaves two active.

`mark_excluded` / `manual_moneyline` (fighter-scoped, `odds_row_key
NULL`) are **not** in this set and are not superseded by a row binding.

### 16.5 The binding write — apply pass sets `fighter_id`

This is the load-bearing change and the one place D.5 amends a D.4
invariant. D.4's `_apply_overrides_unlocked` writes `effective_status`
**only** and never touches `fighter_id` (correct for reject — no
binding changes). For accept/force_pair the *entire point* is to bind a
fighter the matcher missed, so the apply pass must also write
`fighter_id` for those rows.

Amended invariant (D.5): the apply pass writes `effective_status` and,
for `accept_match` / `force_pair`, `fighter_id`. It still never touches
`match_status`, `match_stage`, `match_score`, `computed_at`, the raw
`odds_rows`, or any other column, and never inserts/deletes result rows.
`reject_match` and the no-override mirror path still leave `fighter_id`
exactly as the matcher wrote it.

Uniform shape: the resolver returns a *binding* `(effective_status,
fighter_id)` for every row; the apply pass writes both columns when they
differ from the persisted row. For reject and mirror the binding's
`fighter_id` is the matcher's own value, so the write is a no-op on that
column — the uniform path stays idempotent.

### 16.6 Resolver extension

Keep `resolve_effective_status(match_result, active_overrides) -> str`
working (D.4 tests pin it). Introduce a richer sibling that is the new
single source of precedence:

```
resolve_match_binding(match_result, active_overrides) -> MatchBinding
MatchBinding(effective_status: str, fighter_id: int | None)
```

`resolve_effective_status` becomes a thin wrapper returning
`resolve_match_binding(...).effective_status`, so existing callers and
the D.4 table-driven tests are untouched. The §8 dispatch order is
unchanged; D.5 fills rule 3 (`force_pair` → `effective_status =
'force_pair'`, `fighter_id =` override's fighter) and rule 4
(`accept_match` → `effective_status = 'review_accepted'`, `fighter_id =`
override's fighter). For every non-binding rule the binding carries
`match_result.fighter_id` unchanged.

### 16.7 Recompute consumption (raw stays raw, match_status stays matcher's)

§15.6's recompute flow is unchanged in shape; D.5 only enriches step 4:

1. (unchanged) `DELETE FROM odds_match_results WHERE slate_id = ?`.
2. (unchanged) matcher verdicts INSERTed with `effective_status =
   match_status` and the matcher's `fighter_id` (NULL for `unmatched`).
3. (unchanged) load active overrides for the slate.
4. (**D.5**) for each row, `resolve_match_binding` yields
   `(effective_status, fighter_id)`; UPDATE the row when either differs.
   accept/force_pair rows get their bound `fighter_id` written here.
5. COMMIT; on error the whole sequence rolls back.

Properties preserved:

- **Raw `odds_rows` never mutated** (immutability, §5.1).
- **`match_status` stays the matcher's verdict** — a force_pair'd
  `unmatched` row keeps `match_status = 'unmatched'`; only
  `effective_status` / `fighter_id` move. The disjoint-set invariant
  (`ALLOWED_MATCH_STATUSES` vs `ALLOWED_EFFECTIVE_STATUSES`, §5.2 repo)
  holds.
- **Idempotent / self-healing**: re-running recompute re-derives the
  binding from the same overrides; superseding the override reverts the
  row to the matcher's `fighter_id` and `match_status` on the next pass.
- **`reject_match` behavior unchanged** — its branch still writes
  `effective_status = 'review_rejected'` and leaves `fighter_id` alone.

### 16.8 `match_status` vs `effective_status` decision

Decision: **do not promote bindings into `match_status`.** A manually
accepted/forced pairing becomes `effective_status = 'review_accepted'`
or `'force_pair'`; `match_status` stays `review_required` / `unmatched`.

Rationale:

- `match_status` is, by §14.6 and the repo's disjoint-set invariant, the
  matcher's raw verdict. Overwriting it would erase the "the algorithm
  didn't get this on its own" signal and break the `auto_match` count
  the Odds review banner shows.
- Every downstream consumer that should honor the override already has a
  field to read: `effective_status`. Promoting into `match_status` would
  give two redundant truth sources.

Downstream contract: **all override-aware consumers read
`effective_status`; only the matcher and the review banner read
`match_status`.** D.5 adds `review_accepted` and `force_pair` to the
repository's `ALLOWED_EFFECTIVE_STATUSES` constant (code, not schema —
§16.13).

### 16.9 Projection-source decision

This resolves the question §15.7 pinned but did not wire. **Projections
switch from `match_status == 'auto_match'` to `effective_status` in an
approved set, keyed on the result row's (now binding-corrected)
`fighter_id`.**

Projection-eligible `effective_status` (subset of §15.7 reachable in
D.5):

- `auto_match`
- `review_accepted` (D.5)
- `force_pair` (D.5)

(`manual_moneyline`, `manual_projection_low_confidence_ack` stay
deferred — their override types are not implemented, so the predicate
includes them only when those slices land.)

Projection-blocked (unchanged from §15.8): `review_required`,
`unmatched`, `review_rejected`, `excluded`, `shadowed`.

`projection_input_service.aggregate_projection_inputs` changes from
indexing `match_status == 'auto_match'` rows to indexing rows whose
`effective_status` is in the eligible set, still joining win probability
through `fighter_id` → `odds_row_id` → `odds_rows.implied_probability`.
Because §16.5 sets `fighter_id` on accept/force_pair rows, the
previously-`NULL` binding is now populated and the fighter gains a win
probability.

What keeps rejected / unresolved rows out: the **same predicate** now
governs both surfaces. `review_rejected`, `review_required`, and
`unmatched` are all outside the eligible set, so they never feed
projections — and a row the user explicitly bound is *in* the set on
both the Odds view and the Build pool. There is one predicate, so "looks
fixed in Odds" and "included in Build" cannot diverge. That is the
structural fix for §16.1's trap. The projection-source half is also
documented in `PROJECTION_V1_DESIGN.md` §11.

### 16.10 UI behavior on 03 Odds

A new bordered write-action container (sibling of 3c Reject / 3d
Recompute), e.g. **"3f. Assign / Accept a Match — Writes to
`manual_match_overrides`."** It mirrors the §11 / D.2 reject pattern
exactly:

- **Source list** — result rows whose `effective_status` is in the
  *assignable* set `{review_required, unmatched}`. A new pure filter
  `assignable_match_results(records)` in `odds_match_filters.py`
  (sibling of `rejectable_match_results`) gates this; `auto_match`
  rows are already bound and `review_rejected` rows must be un-rejected
  first (assigning supersedes the reject, §16.4 — but surface that via
  the reject panel, not here).
- **Odds-row selectbox** — pick the review/unmatched row.
- **Fighter selectbox** — options are the slate's **active** fighters.
  For a `review_required` row carrying a `preferred_candidate`, default
  to it (one-click accept); otherwise no default (force a pick).
- **Reason** — optional `text_input`, stored in `reason`.
- **Button** — "Assign `<key>` → `<fighter>` on slate #N".
- **On click** — call a new service
  `record_assign_match_override(conn, *, slate_id, odds_row_key,
  fighter_id, reason)` that, in one transaction: derives the type
  (`accept_match` when `match_status == 'review_required'` **and** the
  chosen fighter equals the matcher's `preferred_candidate`; else
  `force_pair`), supersedes any active resolution override on the key
  (§16.4), inserts the new override, and runs the apply pass. Mirrors
  `record_reject_match_override`'s composition.
- **Feedback** — reload the persisted-results list and show
  success/error on the same render, exactly like 3c.

Hard rules (from `docs/DEVELOPMENT_NOTES.md` §11 + the task brief):

- No page-load writes — the write happens only on the button click.
- No fuzzy auto-assignment — the user picks the fighter; D.5 never
  guesses past the matcher's existing bar.
- No Build inline editor in this slice — the assignment lives on the
  Odds page; Build consumes the result via projections (§16.9).
- Single DB transaction owned by the page handler; idempotent on
  re-click; covered by AppTest (§16.15).

### 16.11 Safety and invalid cases

Validation in the new write path (raise `ValueError` before any DB
write, mirroring `_add_override_unlocked`):

| Case                                            | Behavior                                                                 |
| ----------------------------------------------- | ------------------------------------------------------------------------ |
| Selected fighter **inactive**                   | Reject. Only `status='active'` fighters are assignable.                  |
| Odds row belongs to a **different slate**       | Reject — `odds_row_key` must exist on the slate (existing check).        |
| Selected fighter already inactive on re-import  | See §16.12 (stale, not a write-time error).                              |
| Fighter **already bound** on this slate         | Reject. A fighter with an active `auto_match` result row, or an active accept/force_pair override binding another `odds_row_key`, cannot take a second binding — two odds rows for one fighter would yield two win probabilities. The user must reject/clear the other binding first. (Lean: hard reject. Auto-supersede is the deferred alternative — riskier, easy to get subtly wrong.) |
| Odds row **already rejected**                   | Not an error — the assign **supersedes** the active `reject_match` (§16.4). This is the recovery path.                                            |
| **Duplicate** assignment (same key→fighter)     | Idempotent: the second insert supersedes the prior identical one; the apply pass writes zero changed rows.                                        |
| Odds row already has an active accept/force_pair | The new assign supersedes it (§16.4).                                    |

Auditability: every action is a new `manual_match_overrides` row with
`reason` and `created_at`; supersession is soft (`superseded_at`), so
the full decision history is preserved (§5.3). No hard deletes.

### 16.12 Stale override handling

Builds on §15.4. Because the salary importer **upserts by
`(slate_id, name)` and preserves fighter ids** (it deactivates rather
than hard-deletes — `FighterRepository.upsert_for_slate`), the
`fighter_id` an accept/force_pair override carries is stable across a
re-import *for fighters that remain on the slate*. Cases:

- **Fighter persists (same id)** → override survives, re-binds on the
  next recompute. No action.
- **Fighter dropped from the import → flips to `inactive`** (the common
  case; no row delete, so `ON DELETE CASCADE` does **not** fire). The
  override is still active but now points to an inactive fighter. The
  apply pass treats this as **stale**: it does **not** write the binding
  (leaves `effective_status` / `fighter_id` at the matcher's values), and
  reports the override id in the stale list so the Active Overrides panel
  can badge it "stale — fighter inactive." Because the projection input
  layer already filters to active fighters (§16.9), a stale binding can
  never leak a win probability for an inactive fighter.
- **Fighter row hard-deleted** (not done by the importer today, but
  possible via slate teardown) → `manual_match_overrides.fighter_id ON
  DELETE CASCADE` removes the override. Fail-safe: no dangling binding,
  but user intent is lost. Re-resolving a binding by fighter *name*
  across a hard delete is deferred (ties into §13.3's orphaned-override
  remediation); D.5 does not invent it.

Clearing / superseding: handled by §16.4 — a later resolution override
(including a `reject_match`) on the same key supersedes; the Active
Overrides panel’s existing supersede story (D.5+ per `docs/DEVELOPMENT_NOTES.md` §11)
covers explicit clears. D.5 does not add silent auto-cleanup.

### 16.13 Schema impact — none

`override_type` and `effective_status` are unconstrained `TEXT NOT NULL`
columns (schema §5.2 / §5.3; verified in `src/db/schema.py`). Persisting
`accept_match` / `force_pair` overrides and `review_accepted` /
`force_pair` effective statuses needs **no migration and no schema
test** — D.5 is schema-free. The only allow-list changes are in code:

- `OddsMatchResultRepository.ALLOWED_EFFECTIVE_STATUSES` gains
  `review_accepted`, `force_pair`.
- `ManualMatchOverrideRepository._add_override_unlocked` lifts its
  `override_type != 'reject_match'` guard to accept the two new types
  with their own validation (§16.11).

This keeps §15.11.5 open question (CHECK constraint vs resolver-only) in
its current "resolver-only" lean; D.5 does not add a CHECK.

### 16.14 Implementation phase split (≤ 3 slices)

Each slice is individually shippable, gets its own commit + review, and
**pauses for explicit user approval** (`docs/DEVELOPMENT_NOTES.md` §10 / §13). None
change the matcher service or the raw `odds_rows` write path.

- **D.5.1 — Resolver + repository + service (no UI, no projection
  change).** Add `resolve_match_binding` + §8 rules 3/4 to
  `effective_status_resolver.py`; extend
  `ManualMatchOverrideRepository` to insert `accept_match` / `force_pair`
  with §16.11 validation and §16.4 cross-type supersession; teach the
  apply pass to write `fighter_id` for bound rows (§16.5); add
  `record_assign_match_override` (§16.10). Unit + repository + recompute
  preservation tests; reject_match regression stays green.
- **D.5.2 — Projection-source promotion.** Switch
  `aggregate_projection_inputs` to the §16.9 eligible-set predicate
  keyed on the bound `fighter_id`; update `PROJECTION_V1_DESIGN.md` §11.
  Projection-input tests + the Build-exclusion-removed integration test.
  **This is the load-bearing slice** — it is what actually un-excludes
  Bruno Silva / Santiago Luna from Build.
- **D.5.3 — 03 Odds Assign/Accept UI.** New bordered container +
  `assignable_match_results` filter + AppTest. Wires the D.5.1 service.

**Sequencing warning.** D.5.1 alone flips `effective_status` while Build
still reads `auto_match`-only — i.e. it **re-creates §16.1's fake-fix
trap** (Odds looks fixed, Build still excludes). D.5.2 is what closes it.
Ship D.5.1 and D.5.2 close together and review them as a pair; do not
present D.5.1 on its own as "the assignment fix." D.5.3 (UI) can land
after both, or D.5.1→D.5.3→D.5.2 if you want the button visible before
the projection wiring — but in that ordering the button is explicitly a
"persisted but not yet projected" preview, and the PR must say so.

### 16.15 Test plan (for the future implementation slices)

Resolver / pure (D.5.1):

- `resolve_match_binding('review_required', [accept_match→F])` →
  `('review_accepted', F)`.
- `resolve_match_binding('unmatched', [force_pair→F])` →
  `('force_pair', F)`.
- Precedence (first real multi-type test): `reject_match` + a leaked
  active `force_pair` on one key → reject wins (`review_rejected`,
  matcher `fighter_id`).
- No override → `(match_status, match_result.fighter_id)` unchanged;
  `resolve_effective_status` wrapper still returns the string.

Repository / service (D.5.1):

- Insert `accept_match` / `force_pair`; assert row + active scope.
- Reject inactive fighter, wrong-slate fighter, already-bound fighter.
- Cross-type supersession: assign supersedes an active reject on the key;
  a later reject supersedes the assign; re-assign to a new fighter
  supersedes the prior.
- Idempotent re-click writes zero changed result rows.

Apply / recompute preservation (D.5.1):

- accept/force_pair survives `recompute_and_replace_match_results`:
  `effective_status` **and** `fighter_id` reapplied; `match_status`
  unchanged.
- Stale: override → inactive fighter ⇒ binding not written, override id
  in stale list.
- `reject_match` regression: existing reject tests unchanged and green.

Projection input (D.5.2):

- A force_pair'd fighter now reports `implied_win_probability` and
  `projection_status = ok` (no `win_probability` missing tag).
- `review_rejected` / `review_required` / `unmatched` rows contribute
  no win probability.

Build exclusion removed (D.5.2):

- Integration: seed the §16.1 scenario (active fighter, name-mismatched
  `unmatched` odds row), force_pair + recompute, assert the fighter is
  in the projection pool with a non-`None` projection and B6 reasoning
  includes them.

03 Odds AppTest (D.5.3):

- Assign button writes the override, flips `effective_status`, sets
  `fighter_id`; success path pins the rendered text.
- Inactive / already-bound fighter → error text, no DB write.
- No page-load write (assert the result set is unchanged before any
  button click).

### 16.16 Risks and open questions

1. **Two-surface predicate drift.** §16.9 deliberately routes both Odds
   and Build through one eligible-set predicate. If a future consumer
   (optimizer, exports, alerts) re-implements its own status filter
   instead of importing the shared predicate, the trap can return. Lean:
   factor the eligible/blocked sets into one module both layers import.
2. **Already-bound = hard reject.** §16.11 blocks a second binding for
   one fighter rather than auto-superseding the first. Simpler and safe,
   but a user who genuinely wants to move a fighter's odds to a different
   row must reject the old binding first. Revisit if that two-step proves
   annoying in real use.
3. **Type derivation vs. explicit choice.** §16.10 derives
   `accept_match` vs `force_pair` from `(match_status, preferred
   candidate)`. The alternative is to persist `force_pair` for every
   user-driven binding and reserve `accept_match` for a future
   one-click-confirm affordance. Downstream treats them identically, so
   this is an audit-fidelity preference, not a correctness one.
4. **Hard-delete loses intent.** §16.12: a fighter hard-delete
   `CASCADE`s the override away. Acceptable for v0 (the importer never
   hard-deletes), but a slate teardown + rebuild would silently drop
   bindings. Re-resolve-by-name remediation stays deferred to §13.3.
5. **`manual_moneyline` interaction.** D.5 binds a fighter to a *real*
   odds row, so win probability comes from `odds_rows`. The deferred
   `manual_moneyline` path injects a probability with no odds row; the
   two must not both be active for one fighter (a §10 soft-conflict
   warning already anticipates this). Out of scope until that type lands.
6. **Projection-source change is gated.** D.5.2 promotes
   `effective_status` into projections — exactly the wiring
   `docs/DEVELOPMENT_NOTES.md` §10 / §15.11.7 says stays out of scope without explicit
   instruction. This section designs it; it does not authorize the
   build. Implementation needs its own go-ahead.
