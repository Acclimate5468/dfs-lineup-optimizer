# Salary Persistence Design

Status: Design only. No implementation in this slice.

## 1. Purpose

The app currently validates uploaded DraftKings UFC Classic salary CSVs and records
a structural validation result on the slate, but the individual salary rows are
**not persisted into fighters**. As a result the app cannot run the real v0
workflow:

  uploaded salary CSV → slate-scoped fighters → odds matching → status review →
  later projections / optimizer

Today odds matching only works against seeded or test-fixture fighters. This
design describes how to close the gap by turning a validated salary CSV into
persisted, slate-scoped fighter rows.

This document is design-only. It does not change schema, code, or tests.

## 2. Current State

- Salary CSV structural validation exists.
- Slate creation stores `salary_row_count` and `salary_csv_status="validated"`.
- `FighterRepository.list_for_slate` exists on the read side.
- `odds_matching_service` requires active DK fighters for a slate.
- Current odds matching only produces results when fighters were seeded by tests
  or fixtures, because the upload path never writes fighters.
- Manual override and odds-persistence semantics are documented in
  `ODDS_PERSISTENCE_DESIGN.md`; this design must remain compatible with those
  rules.

## 3. Proposed Data Flow

End-to-end intended flow:

1. User uploads a DK UFC Classic salary CSV for a specific slate.
2. The existing CSV validator confirms structure and required fields.
3. Validated rows are parsed into typed in-memory salary/fighter records.
4. Fighter names are normalized using existing normalization helpers.
5. Records are persisted/updated as slate-scoped fighter rows under an explicit
   user action ("Import salary into slate").
6. After successful import, odds matching can be **manually** recomputed against
   the persisted fighters.

No write should happen as a side effect of page load or simple validation. The
write happens only after explicit user confirmation.

## 4. Column Mapping

The real DK UFC Classic salary CSV has not yet been confirmed against a stored
sample in this repo. Expected fields based on typical DK Classic exports
(treat all of these as candidate names pending real-file confirmation):

- Fighter name (e.g. `Name`)
- Salary (integer, e.g. `Salary`)
- Roster position (e.g. `Roster Position`, expected `F` for UFC Classic)
- Game/Fight info (e.g. `Game Info`) — often holds opponent/event text
- Team / event abbreviation (e.g. `TeamAbbrev`)
- Name + ID composite (e.g. `Name + ID`) used by DK upload format

Rules for unknown columns:

- Unknown columns must be **ignored safely** and never silently mapped to
  behavior such as scoring, fight grouping, or status flags.
- If a column expected by this design is missing, the import must fail loudly
  rather than guess.
- Original column ordering and casing should not be assumed; mapping must be
  by header name.

## 5. Fighter Persistence Rules

- Every imported fighter row is scoped by `slate_id`. Cross-slate writes are
  prohibited.
- `fighter_name` must be non-empty after trim. Empty/whitespace rows are
  rejected at parse time, not silently dropped at the DB layer.
- `salary` must parse to an integer `>= 0`. Non-integer or negative salaries
  are parse errors.
- Duplicate fighter names within the same slate must be either rejected with a
  clear error or resolved deterministically (preferred: reject and surface in
  the validation summary, since DK Classic should not export duplicates).
- Re-importing the same CSV against the same slate must be idempotent. Repeated
  imports of an unchanged file must not create duplicate fighter rows and must
  not flip statuses unnecessarily.
- Changed salary, changed display name (within a normalized identity), or
  changed status (active/inactive) must update the existing row safely rather
  than insert a second row.
- A fighter present in a previous import but absent from the new import must
  not remain silently active. Behavior options (see Open Questions) are:
  mark inactive, soft-delete, or require explicit user confirmation. The
  conservative default is **mark inactive**, not hard delete.
- All writes for one import should occur in a single transaction. Partial
  imports must not leave a slate half-updated.

## 6. Name Normalization / Matching

- Use existing fighter name normalization helpers; do not introduce new
  normalization rules in the write path.
- The salary importer must not perform aggressive or fuzzy matching. Fuzzy
  matching belongs to the odds-matching path, not the write path.
- Persist both the original DK display name and the normalized name **if the
  schema already supports both**. If the schema currently stores only one,
  this design notes a schema decision is required (deferred — see Non-goals
  and Open Questions). Until then, persist whatever the existing fighter row
  contract requires and keep the original name available through the source
  CSV record on the slate.
- Matching DK salary rows to odds rows remains the responsibility of
  `odds_matching_service`, not the importer.

## 7. Fight Group / Opponent Handling

- DK Classic salary CSVs typically carry opponent/event info in a `Game Info`
  style column. This may help bootstrap fight groups, but the importer must
  not auto-create fight groups silently.
- If opponent info is parsed, it should be surfaced as a suggested pairing for
  fight group setup, never written directly as a confirmed fight group during
  salary import.
- Existing rules that block reversed duplicate fight groups must remain in
  force. The salary importer does not override fight group validation.
- If opponent parsing is ambiguous (missing, malformed, or unmatched to known
  fighters in the same slate), the importer must skip fight group hints rather
  than guess.
- **Realized by the B-series design.** `docs/DK_GAME_INFO_PAIRING_DESIGN.md`
  implements this section: it persists the `Game Info` string on import
  (a new nullable `fighters.game_info` column) and surfaces suggested
  pairings behind an explicit Apply on the Fight Groups page — never an
  auto-create during import. Pairing groups active fighters by the exact
  shared `Game Info` value (both bout rows carry the identical string), so
  it needs no `@`-alias parsing and no fuzzy matching. This supersedes the
  "does not infer fight groups from `Game Info`" note in the current import
  service / `upsert_for_slate` docstrings once the B-series lands.

## 8. Interaction With Existing Odds Data

- Inserting or updating fighters can invalidate previously computed
  `odds_match_results`. The importer must not silently rewrite odds matches.
- After a salary import, odds matching recompute is **manual only**, triggered
  by an explicit user action on the odds matching page.
- `manual_match_overrides` may reference fighter IDs. If an import changes a
  fighter row in a way that keeps the same fighter ID, overrides remain
  intact. If an import would otherwise require deleting and re-inserting a
  fighter (changing the ID), the importer must prefer in-place update to
  avoid orphaning overrides.
- The override remap / orphaning risk described in
  `ODDS_PERSISTENCE_DESIGN.md` applies here. This design defers any automatic
  remap behavior; orphaned overrides should be surfaced to the user, not
  silently fixed.
- No automatic page-load writes. Recompute and remap happen only on explicit
  user action.

## 9. Phase Split

Small, reviewable slices:

- **A. Parser / typed records only.** Pure functions: validated CSV → typed
  salary/fighter records. No DB writes. Unit tests only.
- **B. Repository write surface.** Add `FighterRepository.upsert_for_slate`
  (or equivalent) with repository-level tests only. No service or UI wiring.
- **C. Service-layer import transaction.** Compose validator + parser +
  repository in a single transactional service call. Preserve existing
  validation boundaries. Service-level tests.
- **D. Streamlit page wiring behind explicit user action.** A button such as
  "Import salary into slate" guarded by validated status. No writes on page
  load.
- **E. AppTest coverage** for the upload → validate → import flow, including
  the explicit-action guard.
- **F. Manual smoke** with a real DK UFC Classic salary CSV before the
  importer is considered complete.

Each slice should land independently and be revertable.

## 10. Testing Plan

- Parser tests: well-formed CSV, missing required fields, extra unknown
  columns, header casing variants.
- Duplicate fighter names within a slate: rejected with a clear error.
- Bad salary values: non-numeric, negative, blank → parse errors.
- Missing required fields: rejected at parse time, no DB write attempted.
- Idempotent re-import: importing the same CSV twice produces the same fighter
  set and does not duplicate rows.
- Changed salary update: re-import with a modified salary updates in place.
- Stale / removed fighter handling: fighters absent from a new import are
  marked inactive (per conservative default), not deleted.
- Odds recompute after import: existing `odds_match_results` are not silently
  rewritten; recompute requires explicit action.
- No automatic writes on page load: AppTest confirms that simply opening the
  slate page does not insert or update fighters.
- AppTest for explicit import action: button click triggers the import and
  surfaces success / failure clearly.

## 11. Non-goals

Explicitly out of scope for this design slice:

- Schema changes.
- Implementation code (this is design only).
- Projections.
- Optimizer.
- Mismatch alerts.
- Exports.
- True DK-upload-compatible export.
- Direct Odds API integration.
- Scraping.
- UFCStats scraping.
- DraftKings login / account automation.
- Contest entry automation.
- NFL support.
- Accept / Force Pair / Exclude override types.

## 12. Open Questions

- The exact column set of the real DK UFC Classic salary CSV still needs to be
  confirmed against a real file checked against this design.
- Should fighter rows carry an `import_batch_id` to make idempotent re-import
  and stale-fighter handling cleaner?
- Should fighters absent from a new import be marked inactive, soft-deleted,
  or hard-deleted? Conservative default in this design is **mark inactive**.
- Should the original DK salary row be retained verbatim (e.g. on the slate or
  in an import audit table) for later debugging and audit?
- How should reimport behave when `manual_match_overrides` already exist for
  the slate? Options: block, warn-and-continue, or require an explicit
  confirmation step. Pending decision.
- Whether the schema should store both original DK display name and a
  normalized name on the fighter row, or whether normalization stays
  computed-on-read.

## 13. Slice F — Real DK UFC Salary CSV Smoke Checklist

Slice F is the manual validation step that gates calling the salary importer
"complete" (see `docs/DEVELOPMENT_NOTES.md` §8 and §14, and `AI_BUILD_WORKFLOW.md` §9). It is
not a code slice. It is a checklist executed against an officially-downloaded
DK UFC Classic salary CSV. Until every section below passes and is documented
in a slice report, the salary importer remains "tested in isolation, not yet
validated against real feed."

This checklist references the current app state at HEAD `bbcab7c Wire salary
import into slate setup`. Slices A–E (parser, repository, service, UI wiring,
AppTest coverage) are merged; only this manual run remains.

### 13.1 Pre-test git safety

- `git status` clean before the smoke run begins.
- The real DK salary CSV must live **outside the repo working tree**, or
  inside a path already covered by `.gitignore` (`data/uploads/salaries/`
  is ignored — see `docs/DEVELOPMENT_NOTES.md` §7). Confirm with `git status` after placing
  the file; it must not appear as untracked.
- Do **not** stage, commit, or push the CSV, the resulting SQLite DB,
  anything under `data/uploads/`, `data/database/`, `data/raw/`,
  `data/processed/`, or `exports/`. The `.gitkeep` files in those
  directories must remain.
- Do not paste raw CSV rows, fighter names + salaries, or DB row dumps into
  the slice report, commit messages, or any tracked file. Anomaly notes
  should describe the **shape** of an issue, not the data itself.
- Do not paste the CSV or its contents into any external web tool (diagram
  renderer, pastebin, remote LLM playground) per `docs/DEVELOPMENT_NOTES.md` §13.

### 13.2 Real file identification

Record in the slice report (no raw rows):

- Event name and event date (e.g. "UFC Fight Night: <main event>, YYYY-MM-DD").
- Source: **official DraftKings UFC Classic salary CSV, manually downloaded
  from draftkings.com**. No scraped, screenshot-OCR'd, or third-party
  re-hosted file is acceptable.
- Date and approximate time of download (DK reissues files when lineups
  change; the timestamp matters for reproducibility).
- Filename only if it does not embed proprietary identifiers; otherwise a
  redacted form (e.g. `DKSalaries-ufc-YYYY-MM-DD.csv`).
- File path only if it is outside the repo or inside an ignored data
  directory.
- The raw file itself is **not** committed under any circumstances.

### 13.3 App flow to test

Execute through the real Streamlit UI, not by calling services directly:

1. Launch Streamlit (`streamlit run app/streamlit_app.py`) from a clean
   working tree.
2. Open **Slate Setup** (`app/pages/01_slate_setup.py`).
3. Create or select the target slate for the event from §13.2.
4. Upload the official DK UFC Classic salary CSV through the page's
   uploader.
5. Confirm the structural validation result reported by the page (status
   `validated`, expected row count, no validation errors).
6. Click the explicit **Import salaries into slate** button. There must
   be no fighter writes before this click (per design §3 and §5).
7. Record the counts surfaced by the import action: parsed, inserted,
   updated, unchanged, deactivated (or whichever counts the current
   service exposes). If a count is not surfaced today, note that as an
   anomaly rather than inferring it from the DB.
8. Do not navigate to Fight Groups, Odds, Fighter Status, Manual Review,
   Optimizer, or Export during this smoke run. Slice F only validates
   salary import. Downstream consumers remain out of scope per `docs/DEVELOPMENT_NOTES.md`
   §10 and §14.

### 13.4 Data checks

For each item, record pass/fail and a short shape-only note (no raw rows):

- Row count surfaced by the importer matches the DK CSV's expected fighter
  count for that slate (typically equal to the number of `F`-positioned
  rows in the source).
- Fighter display names look correct against the public card (spot-check
  a handful — names with accents, hyphens, and apostrophes should round-trip
  without mojibake). Do **not** transcribe names into the slice report;
  report only "looks correct" / "anomalies seen in N rows".
- Salaries parse to non-negative integers. Confirm the min and max salary
  fall within the expected DK Classic range (shape check only).
- No duplicate fighter rows for the slate after import.
- No blank, whitespace-only, or `None` fighter names persisted.
- Optional / unknown columns (`Roster Position`, `Game Info`, `TeamAbbrev`,
  `Name + ID`) are handled per design §4: ignored safely if unknown,
  failed loudly if a required field is missing. Note observed behavior; do
  not "fix" handling during the smoke test.

### 13.5 Persistence checks

- Persisted fighters are visible through the app's read path (e.g. the
  slate or fight-groups page listing). If a UI read surface is not yet in
  place, a read-only repository call via a Python REPL is acceptable for
  the smoke run; do not add app code to enable inspection.
- Repeat import: re-upload the **same** CSV and click Import again. Result
  must be idempotent — no duplicate fighter rows, no spurious status flips,
  counts reflect "unchanged" rather than "inserted" where appropriate
  (per design §5 and §10).
- Changed-file behavior is **not required** for the smoke run unless a
  second, genuinely different official DK CSV for the same event is
  already available (e.g. a DK reissue). Do not hand-edit a CSV to
  synthesize a diff; that is a parser/repository test concern, not a
  smoke concern.
- Salary import must not modify `odds_match_results` or
  `manual_match_overrides`. Confirm via a read-only DB inspection or by
  noting that no recompute action was taken during the run (per design
  §8 and the existing AppTest
  `test_import_does_not_touch_odds_or_overrides`).
- No writes to fight groups, odds rows, overrides, projections, or any
  table outside the fighter write surface defined in Slices B/C.

### 13.6 Failure / anomaly logging

Anomalies are expected and are the point of this smoke run. For each
anomaly observed, record:

- Where it surfaced (validation, parse, import, persistence, post-import
  read).
- Shape of the problem (e.g. "column header casing differs from design
  §4 candidate list", "row N has a blank `Game Info`", "salary parsed
  but display name contains an unexpected suffix"). **No raw data.**
- Whether the importer failed loudly (acceptable per design §4) or
  silently degraded (not acceptable — must be filed as a follow-up bug).
- Repro steps sufficient for a follow-up slice to reproduce without the
  original CSV (e.g. "header `Game info ` had a trailing space").

Do **not** edit code, tests, or schema mid-smoke to "fix" an anomaly.
Each fix is a separate, design-approved slice with its own tests
(`docs/DEVELOPMENT_NOTES.md` §8, §9, §13). Stop the smoke run, file the anomaly, and wait
for the user to schedule the follow-up.

### 13.7 Completion criteria

The salary importer may be described as **real-file smoke tested** only
when **all** of the following are true in the same run, against the same
official CSV, on the current `master` HEAD:

- §13.1 pre-test git safety satisfied throughout.
- §13.2 file identification recorded in the slice report.
- §13.3 app flow executed end-to-end via the Streamlit UI, including the
  explicit Import button click.
- §13.4 data checks all pass with no unresolved anomalies.
- §13.5 persistence checks all pass, including the idempotent repeat
  import.
- §13.6 anomaly log is either empty or contains only items already
  triaged to follow-up slices that do not block this run.

If any anomaly remains open at the end of the run, the importer stays
"not complete." A follow-up bug slice is required, and Slice F must be
re-run after that slice lands. Partial passes do not count.

This section does not unlock D.5, projections, optimizer wiring,
mismatch alerts, or exports. Each of those requires its own design pass
per `docs/DEVELOPMENT_NOTES.md` §10 and §14.
