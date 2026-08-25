# DK Game Info Fight Pairing Design

Status: **design only** — no code lands in this pass (`docs/DEVELOPMENT_NOTES.md` §9).
This document is the B1 design gate for the B-series slices defined in
§10. It is the required pre-implementation artifact for a change that
adds a column to a core table (`fighters`), so it must be reviewed and
approved before any B2 code is written (`docs/DEVELOPMENT_NOTES.md` §9, §10, §15).

Companion docs:

- `docs/SALARY_PERSISTENCE_DESIGN.md` §7 ("Fight Group / Opponent
  Handling") already anticipated this feature: it states the importer
  should surface opponent info as a *suggested pairing for fight group
  setup, never written directly as a confirmed fight group during salary
  import.* This design realizes that anticipated behavior and is
  cross-referenced from that section.
- `docs/FIGHT_GROUPS_UX_DESIGN.md` §9 (the A4 pasted-card assisted
  pairing builder, "Region D") defines the preview → explicit-Apply
  write pattern this design reuses verbatim. The new salary-CSV
  suggested-pairing surface is a sibling **Region E** on the same page
  and follows the same write-safety contract.

---

## 1. Problem

The Fight Groups page on a freshly imported slate shows every fighter
ungrouped, forcing the user to hand-build all ~13 bouts through the
add form even though the matchups are already known.

- **The user should not manually build every fight.** A 26-fighter
  slate is 13 trips through the two-select add form, or a paste +
  preview through Region D — both avoidable when the matchups are
  already in the imported file.
- **The DK salary CSV already encodes the matchups.** The `Game Info`
  column carries one string per fighter row of the shape
  `Name1@Name2 MM/DD/YYYY HH:MMxM ET`, and **both fighters in a bout
  carry the byte-identical string.** This was confirmed against three
  stored sample exports (`data/uploads/salaries/*.csv`); each file's
  `Game Info` values group cleanly into pairs.
- **The current import drops `Game Info`.** The parser extracts it into
  `ParsedSalaryRow.game_info`, but `FighterRepository.upsert_for_slate`
  writes only `(slate_id, name, salary, position, status)`, so the
  string never reaches the database and no downstream code can use it.
  As a result slate #4 (26 active fighters, 0 fight groups) cannot
  reconstruct its pairings from persisted data at all.

The fix has two halves: **persist** the `Game Info` string at import so
the data survives, and **suggest** pairings from it on the Fight Groups
page behind an explicit Apply — never auto-create during import.

### 1.1 Why exact-string grouping, not `@`-alias parsing

The `@`-delimited names inside `Game Info` are short forms
(`Figueiredo`, `Yadong`), not the canonical DK `Name` field. Parsing
them and matching back to the roster would re-introduce the fuzzy
matching that Region D needs (§9.4 of the UX doc) and its ambiguity
risk. That work is unnecessary here: because **both rows of a bout share
the identical string**, grouping the roster by the exact `Game Info`
value yields the two canonical DK names directly, with no parsing and no
fuzzy match. The `@`-segment is never split in this path. This makes
the salary-CSV path higher-confidence and lower-risk than the pasted-card
path, which is why it becomes the *primary* suggested method (§4).

---

## 2. Data model

### 2.1 New column

- Add a nullable `game_info TEXT` column to the `fighters` table
  (`src/db/schema.py`). `NULL` means "not captured" — either a row
  imported before this feature shipped, or a blank `Game Info` cell in
  the CSV. A non-null value is the verbatim, persist-time-stripped DK
  string (the parser's `_optional_text` already trims surrounding
  whitespace and folds blanks to `None`).
- No new index. The grouping read is a single `list_for_slate` scan in
  Python (§3); the slate is small (≤ ~30 rows). An index would be
  premature (`docs/DEVELOPMENT_NOTES.md` §15, "no premature abstraction").
- The dead `team_abbrev` / `opponent_abbrev` columns are **not** reused
  or touched. They remain unwired; repurposing them is out of scope.

### 2.2 Migration (idempotent)

- Add `_ensure_fighter_game_info_column(conn)` to `src/db/migrations.py`,
  following the existing `_ensure_fighter_manual_status_columns` pattern
  exactly:
  - read `PRAGMA table_info(fighters)`;
  - **partial-fixture safety**: if the table does not exist yet
    (empty pragma), return without error (a test seed may run
    `apply_pending_migrations` before `apply_schema`);
  - if `game_info` is absent, `ALTER TABLE fighters ADD COLUMN
    game_info TEXT` and commit;
  - if present, do nothing.
- Register it in `apply_pending_migrations` alongside the existing
  helpers. Re-running on a fresh DB (where `apply_schema` already
  created the column) is a no-op.

### 2.3 Read surface

- `FighterRecord` currently carries `(id, slate_id, name, salary,
  status)` and `FighterRepository.list_for_slate` selects exactly those.
  Add `game_info` to both: extend the `SELECT`, add the field to the
  frozen dataclass, and update `_row_to_fighter`.
- The added field is **additive and optional** (defaults conceptually to
  `None`). Existing `FighterRecord` consumers
  (`projection_input_service`, `optimizer/pool_builder`, the Fight
  Groups page Region A join) access named attributes and are unaffected;
  the B2 test plan pins that they still behave identically.

### 2.4 Write surface

`FighterRepository.upsert_for_slate` already receives `ParsedSalaryRow`
instances, which already carry `game_info`. The change is small but has
one subtlety that must not be missed:

- **INSERT** writes `game_info = r.game_info`.
- **UPDATE** must include `game_info` in **both** the change-detection
  comparison and the `SET` clause. Today `needs_update` compares
  `(salary, position, status)`; it must also compare the stored
  `game_info` against `r.game_info`. The existing-row `SELECT` that
  builds `existing_by_name` must therefore also read `game_info`.
- **Idempotence / unchanged accounting** (`SALARY_PERSISTENCE_DESIGN.md`
  §5): comparison is plain equality of two `None`-or-`str` values (the
  parser yields `None` for blank, never `""`). A re-import of an
  unchanged file is still a no-op (`unchanged += 1`).
- **Backfill semantics**: a row that predates this column has
  `game_info IS NULL`. On the next re-import with a non-null value, the
  comparison detects a change and the row is counted as `updated`, not
  `unchanged`. **This is the intended backfill mechanism** and must be
  asserted in tests, not treated as a regression in the unchanged count.

### 2.5 Backfill / existing slates

`game_info` is **not** retroactive. Existing slates (including slate #4)
have `NULL` for every row until the user re-imports the salary CSV.
Re-import is idempotent (`SALARY_PERSISTENCE_DESIGN.md` §5), so the
backfill is one click: re-importing the same official CSV into slate #4
flips its 26 rows to `updated` with `game_info` populated, after which
the 13 suggestions appear on the Fight Groups page. B5 (§10) validates
exactly this.

---

## 3. Pairing algorithm

A pure helper — finally filling in the `src/slate/fight_grouping.py`
stub. Streamlit-free, DB-free: it takes the active roster (each entry
carrying `name` and `game_info`) as a plain iterable supplied by the
caller, reads nothing, writes nothing. It introduces **no fuzzy matching
and no new matching primitive** (contrast Region D / UX §9.4).

Algorithm:

1. Consider only **active** fighters (`status == 'active'`), consistent
   with the Region A coverage join and the projection input filter.
2. Drop fighters whose `game_info` is `NULL` or blank — they are
   *uncovered* and reported separately, never grouped.
3. Group the remaining fighters by **exact** `game_info` string. No
   normalization beyond the persist-time strip already applied (§2.1):
   over-normalizing could merge two genuinely distinct bouts, and the
   two rows of one bout are already byte-identical so exact match is
   sufficient and safe.
4. Classify each group by size:
   - **exactly 2 active fighters → a suggested pair.** Use the two
     canonical DK `Name` values (not the `@`-aliases). Order the two
     sides deterministically (normalized-name ascending, fighter `id`
     tiebreak) so the preview is stable and an Apply is idempotent.
   - **exactly 1 → incomplete.** The opponent is missing from the active
     roster (not imported, or marked inactive — e.g. a scratched
     fighter). Skip; surface the lone fighter and the `Game Info` value.
   - **more than 2 → anomaly.** A `Game Info` collision (should not
     happen for a clean DK export). Skip; surface the value and the
     fighters. Never guess a sub-pairing.
5. Rounds are **not** inferred (see §3.1).

Output is a structured, count-bearing result (suggested pairs;
incomplete groups; anomaly groups; uncovered/blank fighters), suitable
for both the preview table and the apply loop. It carries canonical
names only; it does not persist a `fighter_id` foreign key (consistent
with the name-only `fight_groups` model and UX §9.4).

### 3.1 Scheduled rounds

- **Never inferred.** `Game Info` encodes only the bout's date/time, not
  its round count, and "latest start time = main event = 5 rounds" is a
  guess. This path makes no such guess, consistent with UX §9.7 and the
  A3 stance that 5-round status stays manual.
- **Created groups default to `scheduled_rounds = 3`** (the
  `FightGroupRepository.create` default and the Region B radio default)
  and **`status = 'unconfirmed'`**.
- The existing A3 non-blocking "did you set a 5-round main event?"
  reminder (UX §8 A3) already fires when a slate has groups but none are
  5 rounds, and applies unchanged to groups created by this path.

---

## 4. UX — Region E: Suggested DK pairings from salary Game Info

A new section on `app/pages/02_fight_groups.py`, **Region E**, sibling to
the Region D pasted-card builder (UX §9). Because the salary-CSV path is
zero-typing and higher-confidence, Region E is the **primary** suggested
method and is presented above the Region D pasted-card backup.

### 4.1 Visibility and empty-state

- Region E renders only when at least one active fighter on the selected
  slate has a non-null `game_info` (i.e. there is something to suggest).
- When the slate has `game_info` available **and** no/incomplete fight
  groups, Region E is the headline of the empty-state: the page surfaces
  a "No fight groups yet — pair fighters using one of these methods"
  framing with Region E first, the Region D pasted-card builder second,
  and the Region B manual selectboxes third. (The pure-copy reordering
  of that empty-state panel can ship with B4 or as a separable
  copy-only change; it carries no new write path.)
- The stale "Automatic opponent inference from the DK salary CSV is not
  implemented yet" banner (`02_fight_groups.py` top) is replaced/softened
  in B4 once this feature lands; until B4, it stays accurate.

### 4.2 Workflow (read → preview → explicit apply)

1. **Preview is automatic and read-only.** Region E computes suggestions
   from the persisted roster on render and shows them. Computing and
   rendering suggestions **writes nothing** (`docs/DEVELOPMENT_NOTES.md` §11).
2. **Preview table** lists each suggested pair (Fighter 1, Fighter 2),
   plus the incomplete and anomaly rows with their reason, mirroring the
   Region D preview (UX §9.5).
3. **Summary counts**: suggested/eligible, already-grouped (skipped),
   incomplete, anomaly, and uncovered (blank `Game Info`) — mutually
   exclusive buckets, same spirit as UX §9.5 `summarize`.
4. **Explicit Apply.** A single **"Apply Suggested DK Pairings"** button
   is the only write path. On click it creates one group per eligible,
   non-conflicting pair.

### 4.3 Apply rules (reuse Region D's contract verbatim)

The apply contract is identical to UX §9.6, and the implementation
should **share** the existing apply primitive rather than fork it: given
a list of `(name_1, name_2)` pairs plus the current already-grouped set
and existing-pair set, create the eligible ones. (Region D's
`_apply_pairings` is already shaped this way around `ParsedBout`; B4
extracts or generalizes the shared name-pair core so both regions feed
it and cannot diverge.)

- **Explicit button only — no page-load writes, no preview writes.**
- **Single page-owned transaction**; all eligible creates commit or roll
  back together (`docs/DEVELOPMENT_NOTES.md` §11.1).
- **Repository-only**: `FightGroupRepository.create`
  (`scheduled_rounds=3`, `status='unconfirmed'`), nothing else.
- **Create-only**: never UPDATE or DELETE an existing group. No
  overwrite/replace story here (would need its own design + undo story,
  `docs/DEVELOPMENT_NOTES.md` §11).
- **Already-grouped fighters skipped by default**, with the same
  "Include rows whose fighters are already grouped" opt-in checkbox as
  Region D; even opted-in, it only *creates* a second group (which
  Region A then flags as a `duplicate`), never edits the existing one.
- **Idempotent on re-click**: a pair already saved (either order) is
  skipped via the pair-key backstop and the repository's reversed-order
  duplicate guard; a second Apply creates nothing.
- **User sets 5-round main event / title bout manually** afterward via
  the existing per-group controls (§3.1, A3 reminder).

### 4.4 Coexistence with existing regions

- Region D (pasted-card builder) is **preserved** as the backup method
  for slates lacking `Game Info` or with incomplete suggestions.
- Region B (manual selectboxes) is **preserved** unchanged.
- Region A (roster + coverage) and Region C (existing groups) are
  unchanged; groups created by Region E flow into them like any other.

---

## 5. Salary import UI feedback

On the Slate Setup page (`app/pages/01_slate_setup.py`), the existing
import success message is extended (copy + counts only — no new write,
import still creates **no** `fight_groups`):

- fighters parsed / inserted / updated / unchanged / deactivated (as
  today);
- **`game_info` captured: N of M rows** (how many rows carried a
  non-blank `Game Info`);
- **Suggested DK pairings available: K** (the count the grouping helper
  would produce from the just-imported roster), phrased as a pointer:
  "review and apply on the Fight Groups page";
- a reminder to **set the 5-round main event / title bout manually**
  after applying.

Import remains persist-only: it writes fighter rows (now including
`game_info`) and never creates fight groups. The preview → Apply flow on
the Fight Groups page is the only path that creates groups. This is the
explicitly preferred design over auto-creating during import
(`SALARY_PERSISTENCE_DESIGN.md` §7, §8; `docs/DEVELOPMENT_NOTES.md` §11).

---

## 6. Edge cases

1. **Blank / NULL `Game Info`** → fighter is uncovered; never grouped;
   counted and surfaced so the user knows to use Region D / Region B.
2. **One active fighter for a `Game Info` value** → incomplete; the
   opponent is unimported or inactive (e.g. scratched and marked
   inactive on re-import). Surfaced with the lone fighter; not grouped.
3. **More than two fighters for a `Game Info` value** → anomaly; never
   grouped; surfaced with all involved fighters and the value.
4. **Inactive fighters** are excluded from grouping (Region A active
   filter). If a withdrawn fighter is marked inactive on re-import, its
   former opponent becomes an *incomplete* group — correct and surfaced.
5. **Already-grouped fighters** are skipped by default; opt-in creates a
   second group (Region A then shows a `duplicate`), never edits the
   existing one (§4.3).
6. **Whitespace / case in `Game Info`** → exact match on the
   persist-time-stripped stored value. The two rows of one bout are
   byte-identical in the DK export, so exact match pairs them; the
   design deliberately does **not** further normalize, to avoid merging
   distinct bouts.
7. **Opponent name in `Game Info` differs from the DK `Name`** →
   irrelevant: this path keys on the shared string, never on the
   `@`-aliases, so a short-form alias never blocks a pair.
8. **Re-import changes `Game Info`** (DK reissues the CSV with a
   corrected time) → the fighter row's `game_info` updates in place, but
   previously-created `fight_groups` are **not** auto-updated
   (create-only, §4.3). A resulting mismatch surfaces through Region A
   coverage; correcting a saved group stays manual until a remove/unpair
   slice lands (UX §8).
9. **`Game Info` format drift** (a future DK export where the two sides
   do *not* share an identical string) → the affected bouts degrade to
   *incomplete* (two singletons) rather than mis-pairing. The string-key
   approach fails safe: it never invents a pair. Flagged as a residual
   risk (§9).

---

## 7. Test plan

Per `docs/DEVELOPMENT_NOTES.md` §8 every behavioral change ships with a test; per §11
every write action and every rendered derived-state surface needs a
backing test. Tests map to slices (§10):

Schema / migration (B2):

1. **Migration adds `game_info` and is idempotent.** After
   `apply_schema` + `apply_pending_migrations`, `fighters` has a
   `game_info` column; running the migration set again is a no-op.
   Mirror `tests/test_odds_persistence_schema.py` /
   `tests/test_page_bootstrap_pre_migration.py`.
2. **Pre-migration bootstrap safety.** `_ensure_fighter_game_info_column`
   is a no-op when `fighters` does not yet exist (partial-fixture guard).

Repository / import persistence (B2):

3. **Upsert persists `game_info` on insert.** A parsed row with a
   `Game Info` value lands in the column.
4. **Upsert updates `game_info` on re-import.** A changed value updates
   in place; a row that was `NULL` and now has a value is counted
   `updated` (the backfill case), not `unchanged`.
5. **Unchanged accounting includes `game_info`.** Re-importing an
   identical file (same `game_info`) is a no-op: `unchanged` covers all
   rows, `updated == 0`.
6. **Existing `FighterRecord` consumers unaffected.** `list_for_slate`
   now returns `game_info`; existing assertions in
   `tests/test_fighter_repository.py` and downstream consumers still
   pass.

Grouping helper (B3, pure):

7. **13 suggestions from 26 rows / 13 `game_info` values.** A roster of
   13 distinct values × 2 fighters yields 13 suggested pairs with
   canonical names.
8. **Odd group sizes skipped.** A 1-fighter value → incomplete; a
   3-fighter value → anomaly; neither produces a pair.
9. **Blank `game_info` skipped.** `NULL`/blank fighters are uncovered,
   never grouped.
10. **Canonical names, deterministic order.** Pairs use DK `Name`
    values, not `@`-aliases, in a stable order.

Page (AppTest, B4):

11. **Preview is read-only.** Rendering Region E changes no row count in
    `fight_groups` / `fighters` / `slates`.
12. **No page-load writes.** Opening the page writes nothing.
13. **Apply creates unconfirmed 3-round groups only after the explicit
    button.** Before click: no groups. After **Apply Suggested DK
    Pairings**: exactly the eligible pairs exist, each
    `scheduled_rounds == 3`, `status == 'unconfirmed'`.
14. **Existing groups skipped.** A fighter already in a group is skipped
    by default; existing groups are never edited or deleted.
15. **Idempotent re-click.** A second Apply creates nothing and
    duplicates no row.
16. **Existing Region A/B/C/D behavior preserved** with Region E present.

Backfill / smoke (B5):

17. **Slate #4 requires re-import to populate `game_info`.** Documented:
    the feature does not retroactively fill existing rows; a real-CSV
    re-import into slate #4 (or a temp slate) populates `game_info` and
    yields 13 suggestions.

Per `docs/DEVELOPMENT_NOTES.md` §8 the full `pytest` suite must be green before any
slice is reported complete.

---

## 8. Non-goals

This design stays inside the project scope fence (`docs/DEVELOPMENT_NOTES.md` §3 / §14).
It does **not**:

- **Auto-create fight groups during salary import.** Import persists
  `game_info` only; group creation is the Fight Groups page's explicit
  Apply (§5, §4.3).
- **Parse the `@`-aliases or add fuzzy matching** on this path (§1.1,
  §3).
- **Infer scheduled rounds or detect the main event** (§3.1).
- **Scrape or fetch any source.** The only input is the user's imported
  DK CSV (`docs/DEVELOPMENT_NOTES.md` §3 / §14).
- **Touch projections, the optimizer, exports, alerts, odds matching,
  Fighter Status, or Manual Review.** It produces ordinary
  `fight_groups` rows the existing pipeline already consumes; no
  downstream behavior changes.
- **Write a `fighter_id` foreign key** into `fight_groups` (name-only
  model preserved, UX §9.4).
- **Edit or delete existing groups** (create-only, §4.3).
- **Reuse or populate `team_abbrev` / `opponent_abbrev`** (§2.1).
- **Add an index** on `game_info` (§2.1).
- **Promote `effective_status`** anywhere (`docs/DEVELOPMENT_NOTES.md` §10; UX §8).

---

## 9. Blockers and residual risks

- **Schema gate.** B2 adds a column to a core table (`fighters`). Per
  `docs/DEVELOPMENT_NOTES.md` §9 / §15 this design (B1) is the required gate and must be
  approved before B2 code is written.
- **`FighterRecord` blast radius.** Adding `game_info` to
  `FighterRecord` / `list_for_slate` touches a widely-consumed record.
  The change is additive (named-attribute access), but B2's test plan
  must confirm `projection_input_service`, `optimizer/pool_builder`, and
  the Region A join are unaffected (§2.3, test 6).
- **Slate #4 does not self-heal.** Existing slates need a re-import to
  populate `game_info` (§2.5). The 26-row `DKSalaries_current.csv` is
  present and re-import is idempotent, so this is one click — but it must
  be done before slate #4 shows suggestions.
- **Format not contractually guaranteed.** The shared-identical-string
  property holds across the three stored sample exports but is not a
  documented DK contract. The string-key algorithm **fails safe** — a
  non-shared string degrades to *incomplete*, never a mis-pair (§6.9) —
  but the suggestion rate depends on DK's continued behavior. Keep the
  feature behind the preview; never blind-apply.
- **Importer real-file smoke still pending.** `docs/DEVELOPMENT_NOTES.md` §10 /
  `SALARY_PERSISTENCE_DESIGN.md` §13 (Slice F) note the salary importer
  is not yet validated against a real file. B5 overlaps with and extends
  that smoke: the same real-CSV run should confirm both the `game_info`
  persistence and the suggestion output.

---

## 10. Implementation slices

Each slice is independently shippable and reviewable; one slice per
session, design-first (`docs/DEVELOPMENT_NOTES.md` §9 / §13 / §15). Pure logic and
persistence land and are tested before any UI, and the read-only preview
lands before the write action — the same "preview before commit" staging
the Fight Groups page already follows (UX §9.10).

- **B1 — This design doc.** The design gate. No code. Its own commit,
  reviewed and approved before B2.
- **B2 — Persistence: schema + migration + repository/import.** Add
  `fighters.game_info` (schema.py), the idempotent
  `_ensure_fighter_game_info_column` migration, the `FighterRecord` /
  `list_for_slate` read extension, and the `upsert_for_slate`
  write + `needs_update` comparison change. Ships with tests 1–6.
  No UI, no grouping helper. (References §2; realizes
  `SALARY_PERSISTENCE_DESIGN.md` §5, §7.)
- **B3 — Pure grouping helper.** Fill in `src/slate/fight_grouping.py`:
  roster (with `game_info`) → structured suggestions + counts, by exact
  shared-string grouping (§3). No Streamlit, no DB, no fuzzy match.
  Ships with tests 7–10. Must land before B4.
- **B4 — Region E preview + Apply (+ import-feedback copy).** Add
  Region E to `02_fight_groups.py`: the auto-rendered read-only preview,
  summary counts, the already-grouped opt-in, and the explicit
  **Apply Suggested DK Pairings** button sharing Region D's apply
  primitive (§4.3). Update the Slate Setup import-success copy/counts
  (§5) and soften the stale Region-top banner (§4.1). Ships with
  AppTest coverage (tests 11–16). This is the only slice that introduces
  a write path; it must satisfy `docs/DEVELOPMENT_NOTES.md` §11 in full.
- **B5 — Real DK CSV re-import / backfill smoke.** Re-import an official
  DK UFC Classic salary CSV into slate #4 (or a temp slate), confirm
  `game_info` populates and the expected suggestions appear, and apply
  them. Documents test 17 and extends the Slice F importer smoke
  (`SALARY_PERSISTENCE_DESIGN.md` §13). Not a code slice.

A smaller cut is not recommended: this work crosses
schema → repository → pure-logic → UI ↔ write-path boundaries, and
`docs/DEVELOPMENT_NOTES.md` §11 / §13 want the write action isolated behind its own
AppTest and review.
