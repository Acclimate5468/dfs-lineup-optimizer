# Export / Run Log v1 Design

Status: design only. No implementation in this slice. Companion to
`docs/DEVELOPMENT_NOTES.md` §2 (v0 scope: UFC DK Classic only), §3 (out-of-scope —
no DK login, no contest auto-entry, no screen automation, no cloud
backend), §6 (git rules), §7 (data / file safety — exports and
SQLite DB files are ignored and must not be committed), §11 (UI
write-action rules), §13 (session / scope control), §14 (do-not
quick reference), and the following sibling design docs:

- `docs/OPTIMIZER_V1_DESIGN.md` §1 / §5.3 / §5.4 / §10 risk #5 / §11 —
  Optimizer v1 emits a `SolveResult` in memory only. v1 explicitly
  **defers** any persistence, DK upload CSV, or per-run history.
  This doc picks up exactly there.
- `docs/MANUAL_REVIEW_GATE_V1_DESIGN.md` §1 / §5 / §7 — the same
  gate the optimizer enforces is the gate the export/run log
  records a snapshot of.
- `docs/PROJECTION_V1_DESIGN.md` §4 / §5 — the projection rows the
  optimizer pool was built from; the run log reports totals, not
  per-fighter projection inputs.
- `docs/ODDS_PERSISTENCE_DESIGN.md` §15.11 risk #7 — `effective_status`
  is still inert in downstream consumers. The run log inherits this
  caveat (it logs what the optimizer used, not a re-derived view).
- `docs/SALARY_PERSISTENCE_DESIGN.md` §9 — salary import is the
  upstream source of fighter rows; salary CSV contents themselves
  must never appear in an export.

---

## 1. Scope and non-goals

Export / Run Log v1 is the **first internal record** of an
Optimizer v1 solve. It exists so the user, after clicking
**Generate Lineups** on the Optimizer page, can:

- Read back the lineups in a structured, copy-pasteable form
  (separate from the on-screen table).
- Capture a minimal **run log entry** describing what was solved
  (slate, optimizer status, totals, Manual Review snapshot,
  diagnostics).
- Optionally save that entry to a local, gitignored file so they
  can compare runs across a slate without keeping the Streamlit
  session open.

In one sentence: **given an Optimizer v1 `SolveResult`, produce an
internal, research-only export plus a run-log entry, with no DK
upload schema and no contest-entry coupling.**

### 1.1 Explicit non-goals (v1)

If a request would extend this scope into any of the items below,
stop and re-confirm (`docs/DEVELOPMENT_NOTES.md` §3 / §13):

- **No DK-upload-compatible export.** v1 deliberately does NOT
  match the DraftKings UFC Classic upload CSV schema (header names,
  column order, the contest-id / entry-id columns DK expects). A
  DK-upload-compatible export is its own future design slice and
  must land its own design pass first.
- **No contest entry.** No DraftKings login, no Selenium / Puppeteer
  / Playwright, no API hit against `draftkings.com`, no automated
  upload (`docs/DEVELOPMENT_NOTES.md` §3).
- **No actual-results import.** v1 does not ingest finished-event
  scoring, does not compute lineup payouts, and does not score
  past runs against actual DK points. Results ingest is a future
  design pass.
- **No ownership projections.** v1 logs the projection the
  optimizer used; it does not compute or display projected
  ownership.
- **No cloud / external upload.** Exports stay on the local
  machine. No pastebin, no Google Sheets push, no S3, no remote
  LLM playground (`docs/DEVELOPMENT_NOTES.md` §13).
- **No new multi-sport / multi-format abstraction.** UFC DK
  Classic shape only (six fighter ids, ≤ $50,000 salary cap,
  same-fight exclusion).
- **No private data leakage.** A run log entry must not embed raw
  rows from the uploaded DK salary CSV or the uploaded odds CSV
  (see §4 / §7 / §11).
- **No committed exports.** Generated files live under the
  gitignored `exports/` directory only; the run log file (if §5
  picks the file-writing option) lives under a gitignored path
  under `data/` (see §6).

These mirror `docs/DEVELOPMENT_NOTES.md` §3 and `OPTIMIZER_V1_DESIGN.md` §11; this
doc does not relax any of them.

---

## 2. User workflow

The Export / Run Log page sits **downstream** of the Optimizer
page in the existing Streamlit page ordering (slate setup → fight
groups → odds → fighter status → alerts → manual review →
projections → optimizer → export/run log). The end-to-end shape:

1. **Select slate.** Same slate selector pattern as the Optimizer
   page (`app/pages/07_optimizer.py:175-182`). One slate per
   render. The slate id is the join key for everything below.
2. **Confirm the Manual Review gate.** The page calls
   `evaluate_manual_review(conn, slate_id)` and renders the same
   header line as the Optimizer page (gate state + completed-at
   timestamp + Blocking / Warning / Informational counts). If the
   gate is not green, the "Generate export" action is disabled
   with the same reason text the Optimizer page uses. This is
   defense in depth — the optimizer service already enforces the
   gate, but if a user lands here with stale state we surface it
   immediately (§7 / §11).
3. **Generate optimizer lineups.** The export page does NOT
   re-run the optimizer silently on page load. The user clicks
   **Generate lineups for export** (single button, disabled when
   the gate is not green). The handler calls
   `run_optimizer(conn, slate_id=..., n_lineups=N)` once and holds
   the resulting `SolveResult` in Streamlit session state for the
   remainder of the page render. (Rationale: the run log entry
   must reflect the exact lineups shown; we don't want a second
   silent solve between display and export.)
4. **Review final validation.** Before any export control is
   enabled, the page renders the §7 validation panel for every
   lineup in the `SolveResult`: exactly 6 fighter ids, salary
   ≤ 50,000, no same-fight conflicts, Manual Review gate green,
   optimizer status (`ok` / `ok_partial` / `infeasible_*` /
   `gate_blocked`). If any lineup fails any rule, the export
   controls stay disabled and the failure is surfaced verbatim.
5. **Create internal export / run-log entry.** The user picks an
   **export format** (§3) — tidy CSV summary, JSON summary,
   Markdown summary, or the optional wide (one-row-per-lineup) CSV
   — and clicks **Build export**. v1 returns the bytes via
   `st.download_button`. Whether v1 also writes a persistent
   run-log entry to disk is the §5 decision.
6. **Warning banner.** On every page render, a persistent banner
   states: *"Internal research export only. Not a DraftKings
   upload file. Do not commit. Do not upload to DraftKings or any
   external service."* (matches the Optimizer page warning at
   `app/pages/07_optimizer.py:70-81`).

The page issues **no writes that touch existing tables**. The §5
decision determines whether a new tiny `optimizer_run_log` table
is introduced or whether file-writing is the only persistence path.

---

## 3. Export types

v1 ships three core export formats (§3.1 tidy CSV, §3.2 JSON, §3.3
Markdown) plus one **optional** fourth format (§3.1.1 wide CSV).
All of them are **internal research formats** — none of them match
the DK upload CSV schema.

A note on terminology used throughout this section: **"number of
lineups" always means the count of distinct six-fighter UFC DK
Classic lineups produced by a single solve (1..5), never the roster
size of a single lineup.** Every lineup is a full six-fighter roster.
The tidy CSV (§3.1) carries one row per fighter, so a five-lineup run
is 30 data rows; the wide CSV (§3.1.1) carries one row per lineup, so
the same run is 5 data rows. Both describe the same five six-fighter
lineups. The optimizer and export UIs must make this unambiguous by
labelling each lineup **"Lineup {index} of {total}"** rather than a
bare "Lineup {index}" (see §11 risk #8).

### 3.1 CSV summary (per-lineup tidy table)

One row per fighter per lineup. Columns, in this exact order:

- `run_id` — see §4 for the id source.
- `slate_id`
- `lineup_index` (1-based within the run)
- `roster_slot` (1..6 within the lineup)
- `fighter_name` — DK name as persisted, no raw-CSV columns.
- `dk_salary` — integer.
- `default_projection` — float, four decimal places.
- `fight_group_id` — integer or empty.

Footer rows are **not** added. Per-lineup totals live in the JSON
and Markdown formats; the CSV stays tidy so a downstream
spreadsheet pivot is the natural shape.

This CSV is **not** a DK upload file. Header row uses snake_case
column names that DK's upload format does not. There is no
contest-id column, no entry-id column, no "F" position label,
no fighter-id column in DK's numeric id space. §11 risk #1 calls
this out explicitly.

The tidy CSV defined in this subsection is **unchanged** by the
addition of the §3.1.1 wide CSV. Its column set, column order,
"no footer rows" rule, and one-row-per-fighter shape all stay
exactly as specified above. The wide CSV is an additional,
independently downloadable file, never a replacement.

### 3.1.1 Wide CSV (optional — one row per lineup)

The tidy CSV (§3.1) is the natural shape for a spreadsheet pivot
but is hard to **skim**: a five-lineup run spreads each lineup
across six non-adjacent rows, and the long shape can make
"5 lineups" read like a single five-row roster. The wide CSV is
an optional companion that puts **one lineup per row** so the user
can see Lineup 1..N at a glance.

It is offered as a **separate, additional** download alongside the
tidy CSV / JSON / Markdown. It does not replace any existing format
and is never emitted in place of the tidy CSV.

Columns, in this exact order:

- `run_id` — same id source as §4.
- `slate_id`
- `lineup_index` (1-based within the run)
- `fighter_1` — DK name as persisted (roster slot 1).
- `fighter_2`
- `fighter_3`
- `fighter_4`
- `fighter_5`
- `fighter_6` — roster slots 1..6 in the solver's per-lineup order
  (the same ascending order §3.1 uses for `roster_slot`).
- `total_salary` — integer; the solver's per-lineup total.
- `total_projection` — float, two decimal places; the solver's
  per-lineup total.
- `num_fighters` — integer count of fighters actually present in the
  row (normally 6). Emitted so a truncated or partial lineup is
  self-evident and a reader never assumes "wide row == full roster"
  without checking.

Rules:

- **snake_case, deliberately non-DK.** The `fighter_1..fighter_6`
  naming is snake_case and does **not** match DK's upload header
  vocabulary (no "F" position labels, no `Entry ID` / `Contest ID`
  / `Contest Name` / `Lineup Name` columns, no DK numeric fighter-id
  column). This keeps the file from being mistaken for — or used as
  — a contest-entry upload (§1.1, §11 risk #1, §11 risk #9).
- **Self-labelling warning preserved.** Like the tidy CSV, the wide
  CSV's first line is the verbatim internal-only warning comment
  (`# Internal research export only. Not a DraftKings upload file.
  ...`) so the file is self-labelling outside the app (§11 risk #5).
- **One row per lineup; no footer rows.** Per-fighter detail still
  lives in the tidy CSV / JSON / Markdown; the wide CSV carries only
  the per-lineup roster and totals.
- **Empty / diagnostics-only run.** A run with no lineups
  (`gate_blocked` / `infeasible_*`, §7 rule 5) yields the warning
  comment plus the header row only — no data rows — mirroring the
  tidy CSV's empty-run behaviour.
- **Fighter names with special characters** (commas, quotes,
  pipes) are quoted per RFC 4180 by the CSV writer, same as §3.1.

This wide CSV is **not** a DK upload file for the same reasons the
tidy CSV is not (see the paragraph above and §11 risk #1 / #9).

### 3.2 JSON summary (one document per run)

A single JSON object with this top-level shape:

```
{
  "run_id": "...",
  "generated_at_utc": "YYYY-MM-DDTHH:MM:SSZ",
  "slate": { "id": int, "name": str, "event_date": str | null },
  "optimizer": {
    "status": "ok" | "ok_partial" | "infeasible_pool_too_small"
              | "infeasible_constraints" | "gate_blocked",
    "n_lineups_requested": int,
    "n_lineups_generated": int,
    "reason": str
  },
  "manual_review": {
    "ready": bool,
    "status": "reviewed" | "not_reviewed" | ...,
    "completed_at_utc": str | null,
    "blocking_count": int,
    "warning_count": int,
    "informational_count": int
  },
  "lineups": [
    {
      "lineup_index": int,
      "total_salary": int,
      "total_projection": float,
      "fighters": [
        { "fighter_name": str, "dk_salary": int,
          "default_projection": float, "fight_group_id": int | null }
      ]
    }
  ],
  "diagnostics": {
    "pool_size": int,
    "excluded": [ { "name": str, "reason": str } ]
  },
  "warning": "Internal research export only. Not a DraftKings upload file."
}
```

Notes:

- The fighter object intentionally uses **`fighter_name`**, not
  `fighter_id`, as the user-facing key. Internal `fighter_id`
  values are an implementation detail; the run log entry should
  be readable without a DB join.
- `diagnostics.excluded` is the same `ExcludedFighter` shape the
  pool builder already emits (`src/optimizer/pool_builder.py:53`).
  It does **not** include any raw odds row payload — just the
  name and the short reason string.
- The `warning` key is a literal string emitted verbatim on every
  export so the file is self-labelling even outside the app.

### 3.3 Markdown summary (human-readable run log entry)

A short Markdown document, intended to be pasted into a research
notes file or shown to the user verbatim. Sections:

```
# Optimizer run {run_id}

- Generated: {generated_at_utc}
- Slate: #{slate.id} — {slate.name} ({slate.event_date})
- Optimizer status: `{status}` — {reason}
- Lineups requested: {n_lineups_requested}; generated: {n_lineups_generated}
- Manual Review: {status} (gate ready: {ready};
  completed at: {completed_at_utc};
  blocking: {blocking_count}, warning: {warning_count},
  informational: {informational_count})

## Lineup 1
| Fighter | Salary | Projection | Fight group |
| --- | ---:| ---:| ---:|
| ... | ... | ... | ... |
Total salary: ${total_salary:,} · Total projection: {total_projection:.2f}

(repeat per lineup)

## Diagnostics
Pool size: {pool_size}
Excluded:
- {name} — {reason}

> Internal research export only. Not a DraftKings upload file.
```

The Markdown format is the recommended **default** for the run
log because it is the most human-skimmable and least likely to be
mistaken for a DK upload file.

### 3.4 Out of v1

- DK upload CSV schema (header names, column order, contest-id
  column, entry-id column).
- Per-fighter projection input dump (implied probability,
  five-round bonus, value-gap bonus). The default projection is
  the single projection number that ships.
- Per-fighter raw odds rows.
- Salary CSV passthrough columns (e.g., DK roster position id).

---

## 4. Run log fields

Whether persisted to DB, persisted to a gitignored file, or only
embedded in the JSON / Markdown export, every run log entry must
carry **exactly these fields**:

- `run_id` — opaque string. **Recommended**: ISO-8601 UTC timestamp
  with second precision plus the slate id, e.g.
  `2026-05-28T14:32:11Z-slate7`. Rationale: human-skimmable,
  trivially unique on a single machine, no UUID dependency, and
  the timestamp doubles as a sort key.
- `generated_at_utc` — ISO-8601 UTC, second precision. Captured
  inside the export service so the page render time and the
  export time do not drift.
- `slate.id`, `slate.name`, `slate.event_date` — read from
  `SlateRepository`. Slate name is the user-visible label, not a
  raw filename.
- `optimizer.status` — verbatim from `SolveResult.status`. v1
  pins these strings (see `src/optimizer/lineup_solver.py:31-34`
  and `OPTIMIZER_V1_DESIGN.md` §5.4); a status outside that set
  is treated as a logic error and surfaced in the page.
- `optimizer.n_lineups_requested` — the value passed to
  `run_optimizer`.
- `optimizer.n_lineups_generated` — `len(result.lineups)`.
- `optimizer.reason` — verbatim from `SolveResult.reason`. Empty
  when status is `ok`.
- `lineups[].fighter_name` — DK name from `FighterRepository`.
  **Not** the raw salary-CSV row.
- `lineups[].dk_salary` — integer salary the optimizer used.
- `lineups[].default_projection` — float projection the optimizer
  used (the formula at `docs/DEVELOPMENT_NOTES.md` §4 — `default_projection`).
  v1 does NOT log the individual `value_gap_bonus` /
  `five_round_bonus` / implied-win components.
- `lineups[].fight_group_id` — integer or null. Used downstream
  to spot-check same-fight integrity.
- `lineups[].total_salary`, `lineups[].total_projection` — taken
  from `Lineup.total_salary` / `Lineup.total_projection`
  (`src/optimizer/lineup_solver.py:40-50`).
- `diagnostics.pool_size` — `len(pool.entries)`.
- `diagnostics.excluded` — list of `{name, reason}` from
  `OptimizerPool.excluded`. Names only — no raw odds payload, no
  raw salary payload.
- `manual_review.ready`, `manual_review.status`,
  `manual_review.completed_at_utc`,
  `manual_review.blocking_count`, `manual_review.warning_count`,
  `manual_review.informational_count` — all from a single
  `evaluate_manual_review(conn, slate_id)` call captured at solve
  time. **This snapshot is the canonical "gate state when the
  optimizer ran"** for this run.
- `warning` — literal string: *"Internal research export only.
  Not a DraftKings upload file."*

### 4.1 What the run log must NOT contain

These rules are non-negotiable per `docs/DEVELOPMENT_NOTES.md` §7 / §14:

- No raw rows from the uploaded DK salary CSV. Only the persisted
  `Fighter` and `Salary` columns the app already surfaces in the
  UI may appear.
- No raw rows from the uploaded odds CSV. The run log does not
  embed moneyline numbers, implied probability per fighter, or
  per-source pricing. Projection is the aggregated output; the
  run log carries the aggregate, not the input feed.
- No file paths from the user's local filesystem. The slate name
  is fine; the upload path is not.
- No DK contest ids, no DK entry ids, no DK user identifiers.
- No environment / secret values (`.env`).

---

## 5. Persistence decision

v1 picks **option A: in-memory build + `st.download_button`
delivery only.** No new SQLite table. No new write path against
the local DB. The user is responsible for saving the downloaded
file under the already-gitignored `exports/` directory if they
want a persistent copy.

### 5.1 Options considered

**Option A — in-memory only (recommended for v1).**

- The export service builds the CSV / JSON / Markdown bytes in
  memory from the live `SolveResult` + readiness snapshot.
- The page hands the bytes to `st.download_button`. No file is
  written by the app; no DB row is inserted.
- Pros:
  - Smallest blast radius. Zero schema change, zero new
    repository, zero new migration. Aligns with
    `OPTIMIZER_V1_DESIGN.md` §10 risk #5 (per-run history is its
    own design slice — this slice does not pre-empt it).
  - No risk of leaking private salary / odds rows into a
    long-lived DB table.
  - No DB schema creep (§11 risk #4).
  - Trivially reversible — turning off the page deletes nothing.
- Cons:
  - No queryable run history inside the app. If the user wants to
    compare runs across a slate, they compare files.
  - Lineup persistence (saving a lineup the user likes) is a
    separate future slice.

**Option B — write JSON / Markdown to `data/runs/<slate>/<run_id>.{json,md}`.**

- The export service additionally writes the run log files to a
  new gitignored path under `data/`.
- Pros: persistent record without a DB change.
- Cons:
  - Needs a new `.gitignore` entry under §7 and a new directory
    convention. `docs/DEVELOPMENT_NOTES.md` §7 forbids new top-level data
    directories without an explicit gitignore update — the slice
    would have to land the gitignore change first.
  - File-system writes from a Streamlit page introduce an error
    path (disk full, permission denied) that v1 doesn't need to
    own yet.
  - Easy to leak private data if the writer ever serializes a raw
    upload row by accident.

**Option C — new `optimizer_run_log` SQLite table.**

- Insert one row per run with the §4 fields (as JSON in a `payload`
  TEXT column).
- Pros: queryable, joinable with slate / fighter rows.
- Cons:
  - Schema change requires a paired migration in
    `src/db/migrations.py` and a schema test
    (`docs/DEVELOPMENT_NOTES.md` §8). That is the largest of the three options
    and not the smallest individually shippable slice.
  - `OPTIMIZER_V1_DESIGN.md` §10 risk #5 explicitly defers
    per-run audit persistence. Picking C here would silently
    pull that deferred work into this slice.
  - Risk of leaking private optimizer inputs into a long-lived
    DB column.

### 5.2 Recommendation

**Adopt Option A for v1.** Option B is acceptable as a v1.1
follow-up if the user explicitly asks for persistent run logs
without a DB table; Option C must wait for a dedicated design pass
that explicitly takes on `OPTIMIZER_V1_DESIGN.md` §10 risk #5.

The remainder of this doc (§6, §8, §9, §10) assumes Option A.
Sections that would change under B / C are flagged inline.

---

## 6. File naming and location policy

Under Option A, the app itself writes no files. The download
button names are the only policy v1 owns:

- CSV download filename: `optimizer_run_{run_id}.csv`
- JSON download filename: `optimizer_run_{run_id}.json`
- Markdown download filename: `optimizer_run_{run_id}.md`

Where `{run_id}` is the §4 id (e.g.,
`2026-05-28T14-32-11Z-slate7`; colons are replaced with hyphens
so the filename is portable across operating systems).

User-facing guidance in the page text: *"Save downloads under
the project's `exports/` directory if you want them to live
alongside the repo. Do NOT commit them — `exports/*` is
gitignored."*

If the user wants files saved automatically (Option B), the
target path is `data/runs/<slate_id>/<run_id>.{json,md,csv}` and
the `.gitignore` update must land in the same slice that turns
on the writer. No top-level new directories are introduced
without that gitignore change (`docs/DEVELOPMENT_NOTES.md` §7).

`.gitignore` posture today (verified in the working tree):

- `data/uploads/salaries/*`, `data/uploads/odds/*`,
  `data/database/*.db|*.sqlite|*.sqlite3`, `data/raw/*`,
  `data/processed/*`, `exports/*` are already ignored.
- `exports/.gitkeep` exists and must remain.

v1 introduces no new ignored paths.

---

## 7. Validation rules

Every lineup in the `SolveResult` is re-validated **inside the
export orchestration service** before any bytes are emitted. This
is defense in depth — the optimizer service already enforces these
— and it also covers the case where a future caller hands the
service a hand-constructed `SolveResult`.

The C.2 pure-formatter module does **not** enforce §7. It is a
read-only formatter that trusts the `SolveResult` it is handed and
propagates per-lineup `total_salary` / `total_projection` verbatim
from the solver. The §7 rules below describe the C.3 orchestration
service surface; C.2 simply guarantees that whatever lineups land
in `InternalExportBundle.lineups` are byte-for-byte the lineups
that came out of the solver.

For each `Lineup` in `result.lineups`:

1. **Exactly 6 fighter ids.** `len(lineup.fighter_ids) == 6`.
2. **Salary cap.** `lineup.total_salary <= 50000`.
3. **No same-fight conflicts.** No two `fighter_ids` in the
   lineup may share a `fight_group_id`. Validated against the
   `OptimizerPool.same_fight_pairs` the orchestration service
   received (or, if invoked without a pool, against
   `FightGroupRepository.list_for_slate` for the slate).
4. **Manual Review gate ready.** `readiness.summary.ready` must
   be true at solve time. The orchestration service re-reads the
   gate when invoked (matching the optimizer's per-call re-read;
   see `OPTIMIZER_V1_DESIGN.md` §4) and refuses to emit if the
   gate has flipped to not-ready between solve and export.
5. **Optimizer result status.** The orchestration service renders
   the `SolveResult.status` verbatim and the page surfaces it
   prominently. Statuses `gate_blocked`,
   `infeasible_pool_too_small`, and `infeasible_constraints`
   produce a **diagnostics-only export** (slate header,
   readiness snapshot, status, reason, no lineup rows) — this is
   still useful as a research record of "here is why no lineup
   exists today". The C.2 formatter already emits gracefully in
   this case: `bundle.lineups` is empty and the CSV / JSON /
   Markdown skip the per-lineup sections.

If any of rules 1–3 fail for any lineup, the C.3 orchestration
service raises an `ExportValidationError`; the page surfaces the
failure verbatim and the download button does not appear.
`ExportValidationError` is a future C.3 symbol — it does not
exist in C.2. Rules 4 / 5 are surfaced as warnings, not errors —
they are still recorded in the run log.

---

## 8. Modules / files (as built)

The implementation collapses the originally proposed three-module
split (`run_log_builder` / `run_log_formatters` / `export_service`)
into a single pure-formatter module for C.2. The orchestration
service and Streamlit page wiring are deferred to C.3. The actual
layout in flight:

- `src/exports/internal_export.py` (**C.2 — implemented**).
  - Single pure module. No DB access, no I/O, no file writes, no
    persistence. The whole module imports nothing from
    `src/db/` and takes no `conn` argument.
  - Constants: `INTERNAL_EXPORT_WARNING` (the literal §4 warning
    string), `CSV_HEADERS` (the §3.1 column tuple).
  - Frozen dataclasses:
    - `ManualReviewSnapshot` — the §4 gate snapshot.
    - `ExcludedFighterEntry` — `{name, reason}` only; no
      `fighter_id`, no raw odds payload (§4.1).
    - `ExportDiagnostics` — `pool_size` + tuple of
      `ExcludedFighterEntry`.
    - `ExportRunMetadata` — caller-supplied run context
      (`run_id`, `generated_at_utc`, `n_lineups_requested`,
      optional slate id/name/event_date, optional
      `manual_review`).
    - `BundleFighter` — `fighter_name`, `dk_salary`,
      `default_projection`, `fight_group_id`.
    - `BundleLineup` — `lineup_index`, totals, tuple of
      `BundleFighter`.
    - `InternalExportBundle` — the serializable union of metadata
      + optimizer status / reason / generated count + lineups +
      diagnostics + warning.
  - `build_internal_export_bundle(solve_result, *, metadata,
    fighter_name_by_id, fighter_salary_by_id,
    fighter_projection_by_id=None, fighter_fight_group_by_id=None,
    diagnostics=None) -> InternalExportBundle`. Pure: no DB, no
    I/O. Reads per-id maps the caller supplies; missing ids fall
    back to `#<id>` / `0` / `None` so the export is never
    silently truncated.
  - Three formatters, each taking the bundle and returning
    `bytes` for `st.download_button`:
    - `format_lineups_csv(bundle) -> bytes` — leading
      `#`-prefixed warning comment + §3.1 header + per-fighter
      data rows; `csv` module handles RFC 4180 quoting.
    - `format_lineups_json(bundle) -> bytes` — §3.2 shape with
      hand-ordered keys (`sort_keys=False`,
      `ensure_ascii=False`, 2-space indent); deterministic.
    - `format_markdown_summary(bundle) -> bytes` — §3.3 sections
      with pipe-escaped fighter names inside the per-lineup
      tables and the trailing `> Internal research export only.`
      blockquote.
- `src/exports/export_service.py` (**C.3 — future orchestration**).
  - Currently a one-line placeholder stub (it predates C.1 and
    is not on the C.2 import path).
  - C.3 will add the single public function:
    `build_run_log(conn, *, slate_id, n_lineups) ->
    InternalExportBundle`. It re-reads the Manual Review Gate,
    calls `run_optimizer` once, validates per §7, and returns
    the bundle built by the C.2 module. Read-only: no writes.
  - Raises `ExportValidationError` on §7 rule 1–3 failure.
    `ExportValidationError` is a C.3 symbol; it does not exist
    in C.2.
- `app/pages/08_export_run_log.py` (**C.3 — future page**).
  - Replaces the existing placeholder at
    `app/pages/08_export_run_log.py:1-10`.
  - Slate selector, gate readout, `Generate lineups for export`
    button, validation panel, three `st.download_button`s wired
    to the C.2 formatters via the C.3 service.
  - Single Streamlit page; no new tabs, no nested forms.
- `src/exports/__init__.py` — package marker, already exists as
  an empty file; C.2 did not modify it.

No new repository under `src/db/repositories.py`. No new schema
under `src/db/schema.py`. No new migration under
`src/db/migrations.py`. (Option B / C would change this.)

---

## 9. Tests / AppTest coverage

Per `docs/DEVELOPMENT_NOTES.md` §8, every behavioral change ships with a test. The
builder and the three formatters were unified into a single C.2
test file; the orchestration / page tests stay deferred to C.3.

- `tests/test_internal_export.py` (**C.2 — implemented**, 23
  cases). Covers the pure builder and the three formatters
  together against a synthetic in-memory `SolveResult`:
  - Builder: `optimizer.status` and `reason` are propagated
    verbatim from `SolveResult.status` / `.reason`; per-lineup
    `total_salary` / `total_projection` are propagated verbatim
    from `Lineup`; missing fighter ids fall back to `#<id>` /
    `0` / `None`; `warning` is the canonical
    `INTERNAL_EXPORT_WARNING`.
  - CSV: leading `#` warning comment carries
    `INTERNAL_EXPORT_WARNING` verbatim; header row exactly
    matches §3.1 column order; one row per fighter per lineup;
    per-fighter salary rows reconstruct the lineup total.
  - CSV negative: the header + data rows contain none of `Entry
    ID` / `Contest Name` / `Contest ID` / `DraftKings` /
    `Lineup Name`; all header columns are snake_case with no
    embedded whitespace; the warning comment may contain the
    word `DraftKings` because §11 risk #5 requires the file to
    flag itself as NOT a contest entry.
  - CSV special characters: fighter names containing commas,
    double-quotes, newlines, pipes, and apostrophes round-trip
    bit-for-bit through `csv.reader`.
  - CSV diagnostics-only: an empty `SolveResult.lineups` yields
    a CSV of warning + header + zero data rows.
  - JSON: top-level keys appear in the §3.2 order (`run_id`,
    `generated_at_utc`, `slate`, `optimizer`, `manual_review`,
    `lineups`, `diagnostics`, `warning`); the metadata, lineup,
    diagnostics, and warning payloads round-trip through
    `json.loads`; running the formatter twice on the same bundle
    produces byte-identical output; DK upload markers (`Entry
    ID` etc.) do not appear; the diagnostics field renders as
    `{"pool_size": 0, "excluded": []}` when no diagnostics were
    supplied.
  - Markdown: contains the `# Optimizer run <run_id>` title, the
    slate / status / lineups-requested-vs-generated bullets, the
    `## Lineup N` table for each lineup, the totals line, and
    the trailing `> Internal research export only.` blockquote;
    the `## Diagnostics` section appears iff diagnostics were
    supplied; pipe characters in fighter names are
    backslash-escaped inside the per-lineup table.
  - Manual Review snapshot: when supplied, the JSON
    `manual_review` dict carries every §4 field and the
    Markdown bullet renders `Manual Review: <status> (gate
    ready: yes/no; completed at: <ts>; blocking: N, warning: N,
    informational: N)`; when omitted the JSON
    `manual_review` is `null` and the Markdown bullet reads
    `Manual Review: (snapshot not supplied)`.
  - Diagnostics-only export across all three formats:
    `infeasible_pool_too_small` solve renders the status / reason
    prominently, lists excluded fighters, and skips per-lineup
    sections.
  - Pure-function guarantees: every public function's
    `inspect.signature` rejects a `conn` parameter; a
    monkeypatched `builtins.open` that raises on any write mode
    is not triggered by running all three formatters; the
    `tmp_path` working directory remains empty; the module does
    not expose `sqlite3` or `get_connection`.
- `tests/test_export_service.py` (**C.3 — future**).
  - Gate not green → raises `ExportValidationError` or returns a
    diagnostics-only bundle per §7 rule 5; pick one and pin it.
  - Lineup with 7 fighter ids (synthetic) →
    `ExportValidationError`.
  - Lineup with salary > 50,000 (synthetic) →
    `ExportValidationError`.
  - Same-fight pair present in a lineup (synthetic) →
    `ExportValidationError`.
  - `infeasible_pool_too_small` solve → diagnostics-only bundle,
    no lineup rows.
- `tests/test_export_run_log_page.py` (**C.3 — future**)
  Streamlit AppTest:
  - Slate selector renders. Gate-not-green slate disables the
    Generate button with the same reason text as the Optimizer
    page (§2 / `app/pages/07_optimizer.py:196-201`).
  - Gate-green slate enables the Generate button; clicking it
    populates the validation panel and the three download
    buttons.
  - Re-click is idempotent on the same slate / N — same `run_id`
    is regenerated within a single page-load lifetime (or a new
    `run_id` is generated; pin the behavior either way).
  - The persistent warning banner ("Internal research export
    only. Not a DraftKings upload file...") is rendered on every
    page load, even before any click.
  - A download button never carries the substring `DraftKings`
    in its filename, label, or MIME type (negative test
    enforcing §1.1 / §11).

All tests run as part of `pytest`. Real-file smoke (C.4) is not
covered by automated tests; it is documented per `docs/DEVELOPMENT_NOTES.md` §8.

---

## 10. Slice plan

Each slice is independently shippable, has its own commit, its
own tests, and ends with `pytest` green. Per `docs/DEVELOPMENT_NOTES.md` §13,
one slice per session.

- **C.1 — design (this doc).** Merged.
- **C.2 — internal export formatter module + tests.** Merged.
  Adds a single pure module `src/exports/internal_export.py` and
  a single test file `tests/test_internal_export.py` (23 cases).
  Public surface: `ExportRunMetadata`, `InternalExportBundle`,
  `build_internal_export_bundle`, `format_lineups_csv`,
  `format_lineups_json`, `format_markdown_summary`, plus the
  supporting frozen dataclasses listed in §8. No DB access, no
  I/O, no file writes, no persistence. No Streamlit page change.
  Existing placeholder at `app/pages/08_export_run_log.py:1-10`
  stays untouched. `src/exports/__init__.py` was already an empty
  package marker and was not modified. The originally proposed
  three-module split (`run_log_builder` / `run_log_formatters` /
  `export_service`) was collapsed into a single module; the
  orchestration service and `ExportValidationError` were
  intentionally deferred to C.3 to keep C.2 free of DB and
  solver dependencies.
- **C.3 — orchestration service + Streamlit page + AppTest.**
  Adds the orchestration surface to `src/exports/export_service.py`
  (currently a one-line placeholder stub):
  `build_run_log(conn, *, slate_id, n_lineups) ->
  InternalExportBundle` plus `ExportValidationError`. Re-reads
  the Manual Review gate via `evaluate_manual_review`, calls
  `run_optimizer` once, applies §7 validation rules 1–3,
  delegates the actual byte-shaping to the C.2 formatters, and
  returns the bundle. Read-only — no DB writes. Replaces
  `app/pages/08_export_run_log.py` with the §2 page (slate
  selector, gate readout, single `Generate lineups for export`
  button, validation panel, three `st.download_button`s). Adds
  `tests/test_export_service.py` and
  `tests/test_export_run_log_page.py` per §9. No new repository
  or schema change.
- **C.4 — real-feed / manual smoke.** Per `docs/DEVELOPMENT_NOTES.md` §8, the
  export is not "complete" until it is exercised against a real
  DK UFC Classic slate end-to-end: ingest salary CSV → ingest
  odds → match → recompute → manual review ack → optimizer
  solve → export. Documented in a short smoke note appended to
  this doc. No code change required if the smoke passes;
  bug-fix commits are out-of-scope hot-fix slices.
- **C.5 — full end-to-end lineup smoke.** A single user-driven
  walkthrough from a fresh DB through to a downloaded run log
  entry, signed off in the doc. C.5 is **not** a C.4 repeat —
  C.4 verifies the export against the optimizer's output; C.5
  verifies the entire app shape (every page load, every gate
  hand-off, every download button) against a real slate.

C.2 touched only the new module + its test file. C.3 will touch
the placeholder `export_service.py`, the placeholder page, and
two new test files. Neither slice modifies existing
service / repository / schema code. If C.3 starts to require
such a change, stop and confirm (`docs/DEVELOPMENT_NOTES.md` §13 scope creep
rule).

---

## 11. Risks

1. **Accidental DK-upload-compatible export.** Easiest mistake:
   a future contributor renames the CSV columns to match the DK
   upload schema "for convenience" and the export becomes a
   contest-entry file in disguise. Mitigations: §3.1 fixes
   column order and snake_case naming; the §9 negative test
   pins the absence of DK upload column names; the §2 banner
   states the file is research-only on every render.
2. **Persisting private salary / odds data.** §4.1 forbids raw
   upload-row passthrough; §9 includes a negative test against
   raw column names; Option A (§5) avoids a long-lived DB
   column entirely.
3. **Stale optimizer output.** A user could click Generate,
   wander off, add an odds override, then download — making the
   downloaded run log entry reflect a gate snapshot that no
   longer matches the DB. Mitigation: §7 rule 4 re-reads the
   gate in the export service; if the gate has flipped between
   solve and export, the page surfaces the change and refuses
   to emit a non-diagnostic export until the user re-solves.
4. **DB schema creep.** Picking Option C (new
   `optimizer_run_log` table) would silently pull
   `OPTIMIZER_V1_DESIGN.md` §10 risk #5 into this slice.
   Mitigation: §5.2 picks Option A; Option C is gated on its own
   future design pass.
5. **Confusing "export" with "contest entry".** Users may read
   "export" as "send to DraftKings". Mitigations: the page
   warning banner (§2), the `warning` field embedded in the
   JSON / Markdown exports (§3.2 / §3.3), the
   `optimizer_run_*` filename prefix (§6), and the explicit
   §1.1 non-goal list.
6. **Diagnostic-only export misread as a successful run.** When
   the optimizer returns `gate_blocked` /
   `infeasible_pool_too_small` / `infeasible_constraints`, the
   export still renders (per §7 rule 5) but contains no
   lineups. Mitigation: the Markdown / JSON / CSV all carry the
   `optimizer.status` field prominently; the page title and the
   AppTest pin the status string.
7. **PuLP not installed.** Same risk inherited from
   `OPTIMIZER_V1_DESIGN.md` §10 risk #1 — the C.3 orchestration
   service re-runs the optimizer once, so a missing PuLP install
   breaks C.3. C.2 is pure (no solver call) and is unaffected.
   Mitigation: C.3 must verify `pulp` resolves before the
   service lands.
8. **"N lineups" misread as roster size.** The long/tidy CSV and
   the bare "Lineup {index}" labels can make a five-lineup run look
   like a single five-fighter (or five-row) lineup, when in fact it
   is five distinct six-fighter rosters. Mitigations: the §3
   terminology note fixes the meaning of "number of lineups"; the
   §3.1.1 wide CSV gives a one-row-per-lineup view with an explicit
   `num_fighters` column; and the optimizer / export UIs label each
   lineup **"Lineup {index} of {total}"** and state that each lineup
   is a full six-fighter roster.
9. **Wide CSV mistaken for a DK upload.** Because the wide CSV
   spreads six fighters across columns, it is structurally closer to
   the DK upload shape than the tidy CSV and is the most likely of
   the four formats to be mistaken for a contest-entry file.
   Mitigations: §3.1.1 fixes snake_case `fighter_1..fighter_6`
   headers that do not match DK's upload vocabulary, forbids the DK
   contest/entry columns, and keeps the verbatim internal-only
   warning comment as the file's first line; the §9 negative test
   must cover the wide CSV's header set the same way it covers the
   tidy CSV.

---

## 12. Exact next implementation prompt (C.3 only)

C.2 is merged. The prompt below is the next slice — orchestration
service + Streamlit page + AppTest. Do not start C.4 in the same
session.

```
Task: Export / Run Log v1 — Slice C.3 only.
Orchestration service + Streamlit export / run-log page + AppTest.

Repo:
<repository-root>

Run:
pwd
git status
git log --oneline -5

Hard stop:
- If pwd is not <repository-root>, STOP.
- If git status is not clean, STOP.
- Latest pushed commit must include the C.2 internal-export
  formatter (src/exports/internal_export.py +
  tests/test_internal_export.py). If not, STOP.

Current checkpoint:
- Optimizer v1 is implemented through B.6 real-feed/manual smoke.
- Export / Run Log v1 design (C.1) merged.
- C.2 internal export formatter merged
  (src/exports/internal_export.py, tests/test_internal_export.py,
  23 cases). Pure module — no DB access, no I/O, no file writes.
- The src/exports/export_service.py placeholder is still a
  one-line stub.
- The app/pages/08_export_run_log.py placeholder is still the
  pre-C.1 stub.

Scope (C.3 only):
Implement the orchestration service and the Streamlit page per
docs/EXPORT_RUN_LOG_V1_DESIGN.md §2, §7 (validation), and §8
(C.3 module list). Wire st.download_button to the C.2
formatters via the new service.

Allowed write paths:
- src/exports/export_service.py
- app/pages/08_export_run_log.py
- tests/test_export_service.py
- tests/test_export_run_log_page.py

Forbidden:
- No edits to src/exports/internal_export.py
  (the C.2 module is frozen).
- No edits to tests/test_internal_export.py.
- No src/db/ schema, migration, or repository changes.
- No changes to src/optimizer/ or src/slate/.
- No new top-level data directory.
- No DK-upload-compatible CSV schema, no contest entry, no
  DraftKings login or screen automation.
- No persistence (no DB write, no file write — Option A).
- No staging, commit, or push.

Inspect only as needed:
- docs/EXPORT_RUN_LOG_V1_DESIGN.md (this doc)
- src/exports/internal_export.py (read-only — C.2 surface)
- src/optimizer/optimizer_service.py
- src/optimizer/lineup_solver.py
- src/optimizer/pool_builder.py
- src/slate/manual_review_service.py
- src/db/repositories.py (read-only, for SlateRecord / Fighter)
- app/pages/07_optimizer.py (page shape reference)

Implementation requirements:
1. ExportValidationError — module-level exception in
   src/exports/export_service.py. Carries a short reason
   string and (where applicable) the offending lineup.
2. export_service.build_run_log(conn, *, slate_id, n_lineups)
   -> InternalExportBundle.
   - Calls evaluate_manual_review(conn, slate_id) once. If
     summary.ready is False, return a diagnostics-only bundle
     (status="gate_blocked") via the C.2 builder — do not raise.
   - Otherwise calls run_optimizer(conn, slate_id=...,
     n_lineups=...) once.
   - Builds OptimizerPool diagnostics (pool_size + excluded) and
     hands them to the C.2 builder.
   - Validates per §7 rules 1–3 against every returned lineup.
     On failure, raises ExportValidationError verbatim — no
     partial emit.
   - Builds the ManualReviewSnapshot from the readiness object
     and the ExportRunMetadata (run_id, generated_at_utc,
     slate fields) inside the service so the page does not have
     to.
   - Read-only end to end. No INSERT / UPDATE / DELETE.
3. app/pages/08_export_run_log.py — replace the placeholder
   with the §2 page. Slate selector + gate readout + one
   `Generate lineups for export` button + validation panel +
   three st.download_button widgets (CSV / JSON / Markdown)
   fed by the C.2 formatters. Persistent research-only warning
   banner on every render. No new tabs, no nested forms.
4. Tests per §9 (test_export_service.py,
   test_export_run_log_page.py). pytest must be green before
   reporting C.3 complete.

Reporting:
Use the docs/DEVELOPMENT_NOTES.md §12 slice report shape.
```

Anything beyond the above belongs to C.4 or later.
