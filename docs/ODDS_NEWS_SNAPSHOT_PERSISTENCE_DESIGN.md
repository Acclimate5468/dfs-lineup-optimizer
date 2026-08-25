# Odds / News Snapshot Persistence — Design (S5)

Status: **design only.** No app/src/test code in this slice. No schema or
migration change. No DB write performed by writing this doc. Inputs were
inspected **read-only**.

Companion to `docs/ODDS_NEWS_SNAPSHOT_DESIGN.md` (the S2 snapshot contract),
`docs/ODDS_MATCHING_DESIGN.md` / `docs/ODDS_PERSISTENCE_DESIGN.md` (the live
odds pipeline this feeds), and `docs/DEVELOPMENT_NOTES.md` §7 (data safety), §8 (tests +
migrations), §11 (UI write-action rules).

This doc answers one gating question before any S5a code:

> **Can the existing `odds_rows` schema safely distinguish snapshot-sourced
> odds from manual/CSV odds — and support replace/upsert without losing
> manual odds or manual match overrides?**

**Short answer: partially.** *Identification* and *same-snapshot
idempotency* are schema-free and safe today. *Replace / supersede of a
fresher snapshot with override preservation* is **not** safe with the
current repository surface and key scheme, so this doc does **not** claim it
is. The smallest safe v0 is **append-only with explicit source labels**
(the current fields fully support this); clean replace and full
provenance/news persistence are deferred to their own slices.

---

## 1. Repo-grounded facts (read-only inspection)

`src/db/schema.py` — `odds_rows`:

```
odds_rows(
  id, slate_id, odds_row_key TEXT NOT NULL,
  fighter_name_raw, fighter_name_normalized, opponent_name_raw,
  american_odds INTEGER NOT NULL, implied_probability REAL,
  bookmaker TEXT, source TEXT NOT NULL,
  captured_at TEXT NOT NULL, imported_at TEXT DEFAULT (datetime('now')),
  import_batch_id TEXT,
  UNIQUE(slate_id, odds_row_key), CHECK(american_odds <> 0)
)
```

- `source TEXT NOT NULL` already carries provenance. The existing writers set
  it explicitly: `src/ingestion/odds_csv_save.py` writes `"csv"` or
  `"csv:<feed>"`; `src/ingestion/manual_odds.py` forces `"manual"`. So a
  snapshot writer can set `source="snapshot"` / `"snapshot:<name>"` with **no
  schema change** and rows are unambiguously attributable.
- `import_batch_id TEXT` already exists to group one save (CSV save accepts an
  `import_batch_id`). A snapshot save can stamp every row with one batch id.
- `UNIQUE(slate_id, odds_row_key)` is the idempotency key.

`src/ingestion/odds_row_key.py`:

- `compute_odds_row_key` = truncated SHA-1 of
  `normalize_name(fighter) | bookmaker | source | captured_at`.
- `compute_manual_odds_row_key` = `manual:<normalized>:<captured_at>`.
- **The key embeds `captured_at` (and `source`).** Identical re-import →
  identical key. A *fresher* snapshot (new `collected_at`) → **different
  key**.

`src/db/repositories.py` — `OddsRowRepository`:

- `create(...)`, `create_or_get(...)` (insert-or-**return existing** by
  `(slate_id, odds_row_key)`), `get_by_key`, `get_by_id`, `list_for_slate`.
- **No update method and no delete/delete-by-source/delete-by-batch method.**
  `create_or_get` cannot mutate an existing row's `american_odds`.

`OddsMatchResultRepository`:

- `replace_for_slate` / `_replace_for_slate_unlocked` delete + reinsert match
  results for a slate (results are rebuildable, not source of truth).

`src/ingestion/odds_matching_service.py`:

- `recompute_and_replace_match_results(...)` rebuilds `odds_match_results`
  from the current `odds_rows` for a slate.
- `apply_effective_status_overrides_for_slate(...)` reapplies overrides by
  **`odds_row_key`** (`result_keys = {r.odds_row_key …}`; matches
  `ov.odds_row_key`). `record_reject_match_override(...)` is keyed
  `(slate_id, odds_row_key)`.

`ManualMatchOverrideRepository` (schema + repo):

- `manual_match_overrides` uses **soft-supersede** (`superseded_at`);
  `list_active_for_slate` returns active rows; `add_override` supersedes the
  prior active one. Overrides reference `odds_row_key` (and optionally
  `fighter_id`), and the reapply path matches on **`odds_row_key`**.

### 1.1 What these facts imply

- ✅ **Identification is schema-free.** `source` distinguishes snapshot vs
  csv vs manual; `import_batch_id` groups a save.
- ✅ **Same-snapshot re-save is idempotent.** Re-importing the identical
  snapshot yields identical `odds_row_key`s → `create_or_get` dedupes on
  `UNIQUE(slate_id, odds_row_key)`.
- ⛔ **Clean replace of a fresher snapshot is *not* safe today**, because:
  1. there is **no delete-by-source/batch** method, so old snapshot rows
     can't be cleared through the repository;
  2. `create_or_get` **cannot update** an existing row's odds value;
  3. `captured_at` is in the key, so a fresher line for the same fighter is a
     **new** row → two snapshot lines per fighter → the matcher produces
     duplicate/conflicting match results;
  4. overrides are reapplied by **`odds_row_key`**, so fresher-keyed rows
     **orphan** any existing manual override for that fighter.

Therefore this doc **does not** claim a schema-free replace/upsert is safe.

---

## 2. How snapshot entries become `odds_rows`

A validated snapshot (S2/S4) maps **one odds entry → one `odds_rows` row**,
reusing the existing save path shape:

| Snapshot entry field | `odds_rows` column | Notes |
| --- | --- | --- |
| `fighter_name` | `fighter_name_raw` (+ normalized) | normalize via existing helper |
| `opponent_name` | `opponent_name_raw` | |
| `moneyline` | `american_odds` | **canonical input**; `CHECK(<>0)` enforced |
| (derived) | `implied_probability` | app-derived; snapshot's own prob stays advisory |
| `book` | `bookmaker` | |
| — | `source` | set to `"snapshot:<name>"` (new value, no schema change) |
| `collected_at` (entry or envelope) | `captured_at` | |
| (per save) | `import_batch_id` | one id stamped on the whole save |
| (computed) | `odds_row_key` | via `compute_odds_row_key(fighter, bookmaker, source, captured_at)` |

`news_flags`, `news_note`, props (`itd_odds`, `goes_distance`, …),
`line_movement`, `confidence`, `status`, `sources_checked`, `collected_by`
have **no column** in `odds_rows` and are **not** persisted by this path
(see §8–§9).

After insert, the existing `recompute_and_replace_match_results(...)` rebuilds
match results so the new lines flow into Odds Review and downstream.

---

## 3. Can the schema identify snapshot-sourced rows? — **Yes (schema-free)**

Use `source="snapshot:<name>"` (mirroring `"csv:<feed>"`). This is a free-text
column already written distinctly by the two existing writers, so:

- snapshot rows are selectable as `source LIKE 'snapshot%'`;
- `source="manual"` and `source IN ('csv', 'csv:%')` rows are never confused
  with snapshot rows;
- `import_batch_id` additionally identifies exactly one save.

No migration, no new column. This is the load-bearing affirmative finding.

---

## 4. Replace-all vs upsert vs append — **append-only in v0**

Given §1.1, the only behavior that is **safe with today's code** is:

**Append-only, single-snapshot-per-slate, idempotent on identical re-save.**

- Snapshot rows are inserted via `create_or_get` tagged
  `source="snapshot:<name>"` + a per-save `import_batch_id`.
- Re-saving the **identical** snapshot is a no-op (same keys → dedup).
- To avoid duplicate-per-fighter lines, v0 **guards** against importing a
  *second* snapshot while snapshot rows already exist for the slate: the Save
  button surfaces "Snapshot odds already saved for this slate — clear them
  before importing a fresher snapshot" rather than silently appending a
  conflicting second line. (Clearing requires the deferred delete method in
  §11; until then v0 supports one snapshot per slate, replaceable only by the
  existing manual data-reset paths.)

**Replace-all** (`delete WHERE source LIKE 'snapshot%'` then insert) and
**upsert** (update an existing row's odds) are **deferred** — both require new
repository code (§11), and replace additionally requires the override
re-keying fix (§6). They are explicitly **out of v0**.

> This maps to the user's "if no" branch, option 1: *append-only with
> explicit source labels, which the current fields support.* Full
> provenance/news persistence is option 2 (§9), schema-gated.

---

## 5. Manual odds preservation

Safe in v0. Snapshot save only ever inserts `source="snapshot:<name>"` rows.
Rows with `source="manual"` (and `"csv"`) are a different `source` and a
different `odds_row_key` namespace, are never touched by the snapshot insert,
and remain in `odds_rows` untouched. The deferred snapshot-clear (§11) is
scoped `source LIKE 'snapshot%'`, so it can never delete manual or CSV rows.

---

## 6. Manual override preservation

- **First snapshot save (v0): not at risk.** Overrides are created by the user
  *after* matching, against the snapshot rows' keys; an initial import has no
  prior overrides to lose.
- **Across a fresher re-snapshot: at risk — and this is why replace is
  deferred.** Overrides reapply by `odds_row_key`
  (`apply_effective_status_overrides_for_slate`), and a fresher snapshot mints
  new keys (captured_at in the key), so an override keyed to the old row would
  be **orphaned**. v0 sidesteps this by not supporting in-place re-snapshot
  (§4 guard).
- **For the deferred replace slice (§11):** preservation requires one of —
  (a) reapply overrides by `fighter_id` instead of `odds_row_key` on replace,
  or (b) re-point active overrides to the new `odds_row_key` for the same
  fighter as part of the replace transaction. Either is schema-free logic but
  must ship with tests; neither is attempted in v0. The existing
  soft-supersede (`superseded_at`) audit trail is preserved regardless.

---

## 7. Explicit Save button behavior

- One **"Save odds to slate"** button on the Step 2 / Odds & news surface;
  disabled until validation (S4) has **zero hard errors**. No page-load write
  (`docs/DEVELOPMENT_NOTES.md` §11).
- **Slate binding confirm** first (snapshot `event` vs the slate + name-match
  preview) — a wrong-slate save is the worst failure.
- **Pre-commit diff:** "N snapshot rows will be inserted · 0 manual rows
  touched · 0 CSV rows touched · K entries skipped (invalid/unmatched)."
  If snapshot rows already exist, show the §4 guard instead of appending.
- **Stale-but-allowed:** if the snapshot is stale / post-event, require an
  explicit "save anyway" confirm (warning, not block — readiness is the gate's
  job, not the save's).

---

## 8. Transaction / rollback / idempotency

- The whole save runs in **one transaction owned by the page handler**
  (`docs/DEVELOPMENT_NOTES.md` §11): insert snapshot rows, then
  `recompute_and_replace_match_results`, then reapply active overrides — commit
  or roll back as a unit.
- All writes go through repositories (`OddsRowRepository`, the recompute
  service, override repo). **No raw SQL from the UI.**
- **Idempotency:** identical re-save is a no-op via `UNIQUE(slate_id,
  odds_row_key)` + `create_or_get`. Re-running recompute is already
  idempotent (`_replace_for_slate_unlocked`).

---

## 9. `news_flags` handling

Not persisted by this path (no column). In v0 they are **preview-only** and
route to **suggested** Fighter Status changes via the existing HITL Fighter
Status write path in a later slice (S6, snapshot doc backlog) — suggest-only,
never auto-applied, never written by the odds save.

---

## 10. Props / line movement handling

`itd_odds`, `decision_odds`, `goes_distance`, `line_open/current`,
`line_movement` have no `odds_rows` column. In v0 they are **preview-only and
inert** (carried in the snapshot preview, consumed by nothing — the same
posture as `effective_status` downstream). Persisting them is part of the
schema-gated S5b (§11 / §12).

---

## 11. Audit / source metadata: preserved vs deferred

**Preserved schema-free in v0 (already-existing columns):**
`source` (snapshot label), `import_batch_id` (one save), `captured_at`
(line capture time), `bookmaker`, `imported_at`.

**Deferred (no column today → schema-gated S5b):** snapshot-level
`collected_at` vs per-entry, `collected_by` (method/agent/version),
`sources_checked[]`, `news_flags`/`news_note`, props, `confidence`/`status`,
freshness history. Persisting these needs new tables
(`odds_snapshots` / `odds_snapshot_entries`) with a **paired migration + schema
test** (`docs/DEVELOPMENT_NOTES.md` §8) — its own reviewed design. **Honest limitation:** in
v0, once saved, only the odds lines + their `source`/`captured_at`/`batch`
survive; the snapshot's news and provenance live only in the (unsaved) preview.

---

## 12. Manual Review warning impact

S5 itself adds **no** gate logic. Saved snapshot rows affect the gate only
**indirectly**, through `recompute_and_replace_match_results` changing the
existing odds checks (`odds_unmatched_active`, `odds_coverage_partial`,
`odds_match_review`). Turning snapshot **staleness** (or unreviewed
`needs_review` / `conflict` entries) into an explicit Manual Review **warning**
check is **S7** (snapshot doc backlog), its own design — warnings only, never
new Blocking semantics without their own pass.

---

## 13. Risks / open questions

1. **Fresher re-snapshot** is the hard case: needs delete-by-source +
   override re-keying (§4 / §6 / §11). Out of v0; do not ship replace until
   both land with tests.
2. **`captured_at`-in-key** is the root cause of (1). Open q for S5b: a
   stable per-(fighter, source, bookmaker) snapshot key (excluding
   captured_at) plus an explicit value-update method — but that trades away
   the "every capture is a distinct row" property; decide deliberately.
3. **Single-snapshot-per-slate guard** is a real v0 UX limitation; acceptable
   for first ship, removed by the §11 replace slice.
4. **Slate binding** — explicit confirm + name-match preview; never inferred.
5. **Data safety** (`docs/DEVELOPMENT_NOTES.md` §7) — real snapshot files and saved odds stay
   gitignored; this doc and any commit contain only synthetic examples.

---

## 14. Implementation slices

- **S5.0 — this doc.** The persistence contract + the explicit answer above.
- **S5a — append-only snapshot save (schema-free).** Snapshot odds →
  `odds_rows` (`source="snapshot:<name>"` + `import_batch_id`) via
  `create_or_get`, then `recompute_and_replace_match_results`; slate-binding
  confirm; pre-commit diff; stale confirm; single-snapshot-per-slate guard;
  one transaction; AppTest. **No schema change. No replace. No override
  re-keying** (none needed — first import only).
- **S5b — fresher-snapshot replace + override preservation (schema-free
  logic).** Add `OddsRowRepository.delete_for_slate_by_source(...)` (or by
  `import_batch_id`), reapply overrides by `fighter_id` (or re-point keys),
  remove the single-snapshot guard. Ships with override-preservation tests.
- **S5c — full snapshot provenance/news/props persistence (schema-gated).**
  New `odds_snapshots` / `odds_snapshot_entries` tables + paired migration +
  schema test. Only if the audit/history value justifies the schema cost.
- (S6 news→Fighter Status suggestions, S7 staleness→Manual Review warning —
  remain queued from `ODDS_NEWS_SNAPSHOT_DESIGN.md`.)

Each code slice ships its tests in the same slice (`docs/DEVELOPMENT_NOTES.md` §8); no
real-feed snapshot file is ever committed (`docs/DEVELOPMENT_NOTES.md` §7).
