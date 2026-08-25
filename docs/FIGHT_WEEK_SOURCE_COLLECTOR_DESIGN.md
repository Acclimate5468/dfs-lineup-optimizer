# Fight-Week Source Collector Design

Status: Foundation slice. This document covers the **source registry** layer
only — parsing and validating the public-source manifest
(`data/uploads/sources/UFC_DATA.json`) into an in-memory registry. No fetching,
no scraping, no network, no DB writes, no Streamlit are implemented or designed
to ship in this slice. Every actual fetcher is deferred to its own later design
pass (see §11).

Companion docs: `docs/DEVELOPMENT_NOTES.md` §§2–3 (scope / out-of-scope), §7 (data safety),
§13 (session control); `docs/FIGHT_WEEK_DATA_COLLECTION_CHECKLIST.md` (the
manual operator playbook this layer is meant to support); `docs/ODDS_MATCHING_DESIGN.md`
and `docs/ODDS_PERSISTENCE_DESIGN.md` (the Odds Review surface a future fetcher
would feed); `docs/MISMATCH_ALERTS_V1_DESIGN.md`, `docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`,
`docs/PROJECTION_V1_DESIGN.md` (downstream consumers).

---

## 1. Purpose

UFC DFS Lineup Optimizer is a **local-first, manual** UFC DFS workbench. During
fight week the operator gathers public information — card composition, scheduled
rounds, moneylines, injury/pull news — from a known set of public sites, today
entirely by hand (see `FIGHT_WEEK_DATA_COLLECTION_CHECKLIST.md`).

That manual process already has a stable input: a curated list of public
sources kept in `data/uploads/sources/UFC_DATA.json`. This design introduces a
small, safe foundation that turns that file into a **validated, normalized,
de-duplicated in-memory registry** the rest of the (future) collector can build
on, plus a structured report (counts + warnings + errors) for human review.

The registry is the *catalogue* of where information could come from. It is not
a fetcher. Nothing in this slice retrieves a single byte from any of those
sites. The goal is to make the catalogue machine-checkable so later, separately
designed slices can add **safe, manual, human-in-the-loop** fetchers on top of a
trusted source list — without re-litigating what the sources are each time.

## 2. Scope

### 2.1 In scope (this slice)

- A pure parser/validator for the source manifest:
  `src/collection/source_manifest.py`.
- Required-field validation, value normalization (category / type / frequency),
  exact-duplicate detection, and a structured result object.
- Unit tests with synthetic inline fixtures only.
- An optional, network-free CLI/summary helper for a quick local sanity check.

### 2.2 Out of scope (this slice, and gated behind future design passes)

These inherit directly from `docs/DEVELOPMENT_NOTES.md` §3 and
`FIGHT_WEEK_DATA_COLLECTION_CHECKLIST.md` §2/§15:

- **Any** web fetching, HTTP client, RSS/Atom reader, or headless browser.
- Source-specific scrapers or parsers (UFCStats, Sherdog, Tapology, ESPN,
  sportsbooks, X/Twitter, Reddit, YouTube, etc.).
- Login / paywall / CAPTCHA / proxy / anti-bot / rate-limit-evasion handling.
- Direct Odds API or any paid/premium feed integration.
- DraftKings login, account automation, contest entry, or screen automation.
- Schema changes, DB writes, migrations.
- Streamlit pages or any UI wiring.
- Promoting collector output into projections, the optimizer, alerts, manual
  review, or exports.

If a request would cross any line above, stop and confirm scope (`docs/DEVELOPMENT_NOTES.md`
§3, §13).

## 3. The source registry

### 3.1 Manifest format

The manifest is a JSON **array** of source objects. Each object has six required
string fields:

| Field       | Meaning                                              | Normalized? |
|-------------|------------------------------------------------------|-------------|
| `sport`     | Sport tag (expected `UFC` for this v0 project).      | preserved   |
| `category`  | Role of the source (see §3.2).                       | yes         |
| `name`      | Human-readable source name.                          | preserved   |
| `type`      | Channel type (`website`, `x`, …).                    | yes         |
| `url`       | Public URL of the source.                            | preserved   |
| `frequency` | Intended fetch cadence (`auto` / `manual`).          | yes         |

`name` and `url` are preserved exactly as authored (whitespace-trimmed only) so
the registry round-trips back to the file. `category`, `type` and `frequency`
are normalized to canonical form so counts and later routing are stable. `sport`
is preserved (trimmed) but not enumerated in this slice.

### 3.2 Source categories

Canonical categories recognized by the parser:

- **Official** — UFC / DraftKings first-party pages and official stats.
- **Analytics** — DFS analytics and modeling sites/APIs.
- **Betting** — odds boards, odds-comparison sites, prediction markets.
- **News** — MMA news outlets.
- **Community** — forums, aggregators, video creators, social platforms.
- **Insiders** — individual reporters' accounts.
- **Tool** — DFS tooling and developer APIs.

Category matching is case-insensitive. An unrecognized category is **kept
verbatim and flagged as a warning** — the field is present and usable, just not
on the known list. This is deliberately lenient: the manifest is operator-owned
and may grow categories before the code learns about them.

### 3.3 Source types

Canonical types: `website`, `x` (the social network; `"X"` normalizes to `"x"`).
Unknown types are lowercased, kept, and warned. Type later decides *how* a
future fetcher would treat a source (a web page vs. a social timeline), but in
this slice it is metadata only.

## 4. Validation & normalization rules (this slice)

The parser is intentionally forgiving about *recoverable* problems and strict
about *missing* data, mirroring the salary importer's "ignore unknown safely,
fail loudly on missing required" philosophy (`SALARY_PERSISTENCE_DESIGN.md` §4).

1. **File-level failures raise.** A missing file, invalid JSON, or a top-level
   value that is not a JSON array raises `SourceManifestError`. The caller gets
   nothing usable, so failing fast is correct.
2. **Per-record problems do not raise.** Every record-level issue is collected
   into the result so the operator can review them all at once:
   - A non-object entry (string, number, null) → **error**, entry skipped.
   - A missing or blank required field → **error**, entry skipped.
   - An unknown `category` / `type` / `frequency` value → **warning**, entry
     kept with the value normalized as far as possible.
3. **Exact duplicates are dropped with a warning.** Two entries with the same
   (`name`, `url`) pair are exact duplicates; the first wins and later copies
   are dropped and reported. Near-duplicates that differ only by scheme or host
   (e.g. `http` vs `https`, `www.` prefix, trailing slash) are **not** merged in
   this slice — that is a fuzzy-match concern deferred to a later review-driven
   slice (§11), to keep "duplicate" meaning *exact* and predictable here.
4. **Structured result.** The parser returns records (the survivors), counts by
   category, by frequency, and by type, plus warnings, errors, and summary
   counts (`total_input`, `valid_count`, `duplicate_count`, `error_count`).
5. **No side effects.** No network, no DB, no file writes, no Streamlit import.
   The pure entry point (`parse_sources`) operates on an in-memory list so tests
   need no fixtures on disk.

## 5. Fetch frequency and fetch statuses

### 5.1 Frequency (present in the manifest today)

- `auto` — the operator considers this source *eligible* for a future safe,
  automated fetcher. **It does not mean anything is fetched today.** This slice
  never fetches; `auto` is forward-looking metadata only.
- `manual` — human-in-the-loop only: the operator reads/transcribes by hand.

### 5.2 Fetch statuses (future, not implemented here)

When a later slice adds a *safe* fetcher, each source's per-run state should be
tracked as an explicit status rather than inferred. Proposed lifecycle (design
target, not built):

- `not_attempted` — no fetch tried this fight week.
- `skipped` — intentionally not fetched (e.g. `manual` source, or operator
  excluded it for this slate).
- `fetched_ok` — fetched successfully; raw payload captured locally.
- `fetch_failed` — attempted and failed (network error, non-200, parse error).
- `blocked` — fetch refused on policy grounds (login wall, paywall, robots
  disallow, anti-bot challenge). **A `blocked` source is never worked around**;
  it falls back to the manual path. This status exists so the workbench records
  *why* a source was not collected, not to trigger evasion.
- `stale` — last successful fetch is older than the operator's freshness window.

Statuses are deliberately observable and conservative: the collector reports
what happened and defers to the human, consistent with the rest of the app
(`MANUAL_REVIEW_GATE_V1_DESIGN.md`).

## 6. Safety boundaries

These are hard limits, restated so a future slice cannot quietly cross them:

- **No evasion, ever.** No login automation, CAPTCHA solving, proxy rotation,
  user-agent spoofing for the purpose of bypassing blocks, or anti-bot
  defeats. A source that cannot be fetched politely is a `manual` source.
- **Respect site policy.** Any future fetcher must honor `robots.txt`, terms of
  service, and rate limits, and must prefer official/public endpoints. A source
  whose terms forbid automated access is `manual`-only regardless of its
  manifest `frequency`.
- **No DraftKings automation.** No DK login, no contest entry, no DK screen
  scraping. DK salary data continues to arrive via the manual CSV download path
  (`SALARY_PERSISTENCE_DESIGN.md`).
- **Public + free only.** No paid feeds, no premium-source scraping
  (`FIGHT_WEEK_DATA_COLLECTION_CHECKLIST.md` §15).
- **Data stays local.** Fetched payloads and derived rows are local-only and
  follow `docs/DEVELOPMENT_NOTES.md` §7: never committed, never pasted into external tools.
- **Human-in-the-loop on every promotion.** Nothing the collector produces is
  written into projections, the optimizer, alerts, or exports without an
  explicit, separately designed review step (§9).

## 7. Future normalized collector output (design target)

A later slice — once safe fetchers exist — would normalize raw source payloads
into a small set of typed, reviewable shapes. These are **targets**, not built
here, and each gets its own design + tests:

- **Fight card.** Event identity (name, date, venue), the bout list (both
  fighters as the source lists them, weight class), fight order, and scheduled
  rounds (5 for main event / title bouts, 3 otherwise). Feeds fight-group review
  (`app/pages/02_fight_groups.py`) as *suggestions only*, never auto-applied
  pairings (mirrors `DK_GAME_INFO_PAIRING_DESIGN.md`).
- **Odds rows.** Per-fighter moneylines normalized into the existing odds-row
  shape (`src/ingestion/odds_csv_importer.py`: `fighter`, `moneyline`,
  `source`, `timestamp`, optional `opponent` / `bookmaker`) so they enter the
  *existing* Odds Review / matching pipeline rather than a new path. Provenance
  (which manifest source, fetched when) travels with each row.
- **News flags.** Lightweight, structured injury / pull / replacement / weigh-in
  signals tied to a fighter name and a source + timestamp. These are *flags for
  the operator*, not automatic status changes — Fighter Status v1
  (`FIGHTER_STATUS_V1_DESIGN.md`) remains the human-driven override surface.
- **Source status.** The per-source fetch status from §5.2, surfaced so the
  operator can see coverage and gaps at a glance before a slate is trusted.

Each of these normalized shapes must map onto an **existing** review/override
surface; the collector does not invent a parallel data path that bypasses the
established repositories (`docs/DEVELOPMENT_NOTES.md` §11).

## 8. Manual / human-in-the-loop workflow

Even with future fetchers, the workflow stays HITL end to end:

1. **Registry review.** Parse the manifest (this slice). Resolve any errors
   (missing fields) and review warnings (unknown values, duplicates) before the
   source list is trusted for a fight week.
2. **Collect.** For `manual` sources, the operator gathers by hand per the
   checklist. For `auto`-eligible sources, a future safe fetcher *may* retrieve
   raw payloads — politely, respecting §6 — into local-only storage.
3. **Normalize + review.** Raw payloads become the §7 shapes, each landing in a
   review queue (odds matching, fight-group suggestions, news flags) where the
   operator accepts/rejects, exactly as odds matching works today.
4. **Promote on explicit action only.** Reviewed data flows into projections /
   alerts only through the existing explicit user actions; no page-load writes,
   no silent promotion.

## 9. How this feeds downstream

This foundation is upstream of, and must not bypass, the existing surfaces:

- **Odds Review / matching** (`docs/ODDS_MATCHING_DESIGN.md`,
  `docs/ODDS_PERSISTENCE_DESIGN.md`): future odds-row output enters the same
  name-matching + review/override queue used for CSV/manual odds today. The
  collector adds rows to that queue; it never writes match results directly.
- **Mismatch Alerts** (`docs/MISMATCH_ALERTS_V1_DESIGN.md`): alerts remain a
  read-only layer over Projection v1. Better source coverage simply means fewer
  `missing_inputs` rows; the collector changes inputs, not alert logic.
- **Manual Review** (`docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`): collector-derived
  data is subject to the same review gate as any other input — it does not get a
  fast path around human review.
- **Projections** (`docs/PROJECTION_V1_DESIGN.md`): Projection v1 consumes only
  already-persisted inputs (salary + odds + fight group). The collector's role
  is to make those inputs easier to gather; it does not touch the §4 projection
  formula or feed projections directly.

In short: the collector widens the top of the funnel. Every existing gate
between raw data and a lineup stays exactly where it is.

## 10. Testing

This slice is unit-tested with **synthetic inline fixtures only** — Python
lists/dicts and small JSON strings written to `tmp_path`. The tests never read
the real `UFC_DATA.json` (it is operator data, untracked per §12). Coverage:

- Minimal valid manifest → correct records and counts.
- Normalization of `category` / `type` / `frequency` (casing, `X` → `x`).
- Original `name` / `url` preserved (case and punctuation) after trim.
- Missing / blank required field → error, record excluded.
- Non-object entry → error, record excluded.
- Exact duplicate (`name`, `url`) → warning, deduped.
- Unknown category / type / frequency → warning, record kept.
- Empty manifest → valid result, zero records, warning.
- `load_source_manifest` raises on missing file, bad JSON, non-array top level.
- File round-trip via `parse_source_manifest`.
- Counts by category / frequency / type across a mixed manifest.

## 11. Future slice plan (each its own design pass)

Strictly ordered, each gated on the previous and on its own approved design:

- **S0 (this slice).** Source manifest parser/validator + registry types.
- **S1.** Source registry *review surface* — a read-only way to see categories,
  frequencies, duplicates, and unknown values (could be CLI-only first).
- **S2.** A single, safe, polite fetcher for **one** clearly-permissible public
  source (robots-respecting, rate-limited, official/public endpoint), writing
  only to local raw storage. No parsing into app shapes yet. Its own design.
- **S3.** Normalizers for one output shape at a time (start with odds rows into
  the existing Odds Review queue), each with its own design + tests.
- **S4.** Source status tracking (§5.2) surfaced to the operator.

No fetcher slice begins without an explicit user instruction and an approved
design that re-confirms the §6 safety boundaries.

## 12. Data safety for the manifest

`data/uploads/sources/UFC_DATA.json` is **operator-owned input data**. Per
`docs/DEVELOPMENT_NOTES.md` §7 it is not committed. Note that, unlike `data/uploads/salaries/`
and `data/uploads/odds/`, the `data/uploads/sources/` path is **not yet in
`.gitignore`**; until a separate, in-scope change adds it (with the matching
`.gitkeep` rule), this file must be kept out of every `git add` manually. Tests
must not depend on it; they use synthetic fixtures only.

## 13. Open questions

- Should `data/uploads/sources/` be added to `.gitignore` (with a `.gitkeep`),
  like salaries/odds? (Recommended, but a separate change — see §12.)
- Should `sport` be enumerated/validated against `UFC` and non-UFC sources
  warned, given the project is UFC-only (`docs/DEVELOPMENT_NOTES.md` §2)?
- Should near-duplicate detection (scheme/host normalization) become a warning
  in a later review slice, or stay out to avoid false positives?
- Where should fetched raw payloads live locally, and under what retention
  policy, once S2 exists? (Likely a new ignored `data/raw/sources/` path,
  requiring a `.gitignore` update in the same change per `docs/DEVELOPMENT_NOTES.md` §7.)

## 14. This slice — summary of what lands

- `docs/FIGHT_WEEK_SOURCE_COLLECTOR_DESIGN.md` (this document).
- `src/collection/__init__.py` (new package).
- `src/collection/source_manifest.py` — pure parser/validator + summary CLI.
- `tests/test_source_manifest.py` — unit tests, synthetic fixtures only.

No schema change, no DB write, no Streamlit change, no network call, and no
source-specific fetcher. The salary/odds importers and all downstream
consumers are untouched.
