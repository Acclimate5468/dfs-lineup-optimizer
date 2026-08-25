# Odds Consensus (multi-book blend) — design

Status: **DRAFT — design only, pending user approval.** No `src/`, `app/`, or
test code is written on the strength of this doc. Implementation is split into
the slices in §10, each its own commit and review (`docs/DEVELOPMENT_NOTES.md` §9 / §13).

This document realizes **`ODDS_ACQUISITION_V0_DESIGN.md` decision #4**, which was
explicitly deferred:

> *"Prefer the DraftKings column when a source exposes per-book lines. A later
> phase may fall back to a consensus / median across major books. This is not in
> Phase 1 and is not implemented on the strength of this doc."*

It does **not** add a new fetcher, a required API, or any source outside the
four approved Odds Acquisition v0 paths. It blends data that already comes from
approved sources (BestFightOdds — the one Green public source — and the
DraftKings paste / manual entry) into a single, more-accurate win-probability
input for the projection.

---

## 1. Why

The projection's accuracy is bounded by the quality of its win-probability
input. Today that input is **one book's moneyline** (DraftKings). A single book
can be shaded by public money or simply stale. The betting-market consensus —
the de-vigged blend across many books — is a sharper estimate of true win
probability, especially close to the event.

A worked example from real fight-week data (UFC June 6 card): DraftKings priced
Edgar Chairez at +102 (~50%), which tripped a value-gap bonus and put him in all
five research lineups. The 8-book consensus had him at ~46% — a clear underdog —
and the bonus vanishes. The single-book input produced a materially different,
less-accurate lineup. Consensus fixes that class of error.

## 2. What "consensus" means here (the math)

Per the approved method (user decision): **median of the per-book de-vigged
probabilities.** Concretely, for each fight (a fighter pair A vs B):

1. **Collect** each book's American moneyline for A and for B.
2. **Convert** each book's line to a raw implied probability
   (`american_to_implied_probability`, already in
   `src/projections/implied_probability.py`).
3. **De-vig per book, per fight.** For each book independently, normalize that
   book's A/B pair to sum to 1 (`no_vig_two_way`). This strips each book's own
   margin *before* combining — the ordering refinement noted in discussion
   (de-vig first, then blend), which is more correct than blending raw lines and
   de-vigging once.
4. **Median across books.** Take the median of A's per-book no-vig
   probabilities, and the median of B's. Median (not mean) is robust to one
   stale or outlier book.
5. **Renormalize the pair.** The two medians need not sum to exactly 1; divide
   each by their sum so the consensus pair is internally consistent (vig-free).
6. **Emit** the consensus probability for each fighter, plus a "fair" American
   line derived from it for storage/compatibility.

Notes / locked choices for v1:

- **Median, equal-weight.** No sharp-book weighting in v1 (user chose median for
  robustness and zero tunable judgment calls). Sharp-weighting toward prediction
  markets (Polymarket / Kalshi) is a **deferred** enhancement (§12), not built
  now.
- **Proportional (multiplicative) de-vig**, reusing the existing
  `no_vig_two_way`. Shin's method is deferred (§12).
- **Never average raw American odds.** All blending happens in probability space.
- **Minimum book count.** A fighter's consensus is only emitted when at least
  `MIN_BOOKS` (proposed default **2**) books have a line for that fight;
  otherwise the row is surfaced as low-confidence / needs attention rather than
  silently blended from one book (§9).

## 3. Scope

### In scope (this design)

- Blend **multiple books' moneylines** for a UFC DK **Classic** slate into one
  consensus win-probability per fighter, feeding the existing projection.
- **Two data sources** for the per-book lines (user chose "both"):
  - **A. BestFightOdds, all books.** Extend the BFO parser to read *every* book
    column it already renders (today it keeps only the DraftKings column — see
    §5.1). Reverses the "DK-column-only" half of decision #3 *for the consensus
    path*; the single-source DK path is unchanged.
  - **B. Multi-book paste fallback.** A new pure parser for a pasted multi-book
    table (the Polymarket / Kalshi / FanDuel / … / DraftKings grid a user can
    copy from an odds-comparison screen), for when the BFO fetch is blocked,
    thin, or missing a book the user wants.
- **Preview-before-save**, explicit user trigger only (repo invariant). Nothing
  fetched, parsed, or blended on page load; nothing written until an explicit
  Save click.
- A synthesized **`source="consensus"` odds row** per fighter that flows through
  the *existing* matcher → `effective_status` → projection path unchanged.
- Per-book line **provenance** so the blend is auditable and recomputable.

### Out of scope (explicitly not this design)

- Any sport other than UFC, or any DK format other than Classic.
- A new fetcher or a *required* API dependency (`docs/DEVELOPMENT_NOTES.md` §3). BFO stays the
  only fetched source; everything else is paste/manual.
- Props, totals, spreads — moneylines only, as in v0.
- Sharp-book weighting, Shin's de-vig, trimmed means, log-pooling (§12,
  deferred — each its own follow-up).
- Promoting consensus into the optimizer / alerts / exports beyond what the
  projection already consumes (the standing `effective_status` gates,
  `docs/DEVELOPMENT_NOTES.md` §10, are untouched).
- Live odds polling, line-movement history, or background refresh (v0 fetches
  public static HTML on explicit trigger only).

## 4. Where it plugs in (architecture)

```
                 ┌─ BFO fetch (all books) ─┐
per-book lines ──┤                          ├─► consensus service ─► consensus
                 └─ multi-book paste ───────┘     (pure, §2)          probability
                                                                          │
   provenance: odds_book_lines table (one row per book per fighter)       │
                                                                          ▼
                              odds_rows (source="consensus", one per fighter)
                                                                          │
                              existing matcher → odds_match_results        │
                                            → effective_status (auto_match)│
                                                                          ▼
                              projection_input_service (UNCHANGED) ─► projection
```

The deliberate win: **the projection and matcher do not change.** A consensus
row is just a normal `odds_rows` row with `source="consensus"` whose
`american_odds` is the fair line for the consensus probability. It matches and
becomes projection-eligible exactly like a DK-paste row does today
(`PROJECTION_V1_DESIGN.md` §11 eligibility predicate). Per-book detail lives in a
separate provenance table so it never competes with the consensus row for the
"one effective odds per fighter" slot.

## 5. Components

### 5.1 BFO all-books parser (extends existing)

`src/ingestion/providers/bestfightodds.py` currently locates only the
DraftKings column (`_find_dk_column`) and ignores every other book. The
extension adds a sibling parse that returns **all** book columns:
`{ fighter, opponent, [(book, american_odds), …] }`. The existing
DK-only function stays for the single-source path; the new function is additive.
No new network behavior — same single user-triggered GET via
`bestfightodds_fetch.py`.

The real-feed hardening of this parser — how it copes with the actual BFO event
markup (interleaved prop / round-total rows, rotation-number name prefixes,
movement-arrow odds spans, promo-suffixed book labels, a server-empty DraftKings
column, a trailing props-count cell) without touching the shared
`_find_dk_column` — is specified in **§10.7**.

### 5.2 Multi-book paste parser (new, pure)

A pure parser, mirroring `draftkings_paste.py`, for a pasted odds-comparison
grid: a header row of book names and one row per fighter of American lines.
Tolerant of blank cells (a book with no line for that fighter), the trailing
non-odds columns (e.g. a "Props" count), and unicode minus signs. Offline,
preview-only, writes nothing.

### 5.3 Consensus service (new, pure)

`src/projections/odds_consensus.py` (proposed). Pure function implementing §2:
takes per-fight book lines, returns per-fighter consensus probability + fair
American line + the book count and the spread/dispersion used (for the
confidence surface in §9). No DB, no Streamlit, no network. Fully unit-testable
with hand-computed fixtures.

### 5.4 Persistence (schema — see §6)

- **`odds_book_lines`** (new table): one row per (slate, fighter, book) — the
  raw provenance the blend was computed from. Rebuildable; cleared/rewritten on
  each consensus save.
- **`odds_rows`** (existing, unchanged schema): the synthesized
  `source="consensus"` row per fighter. Uses the existing `american_odds`,
  `implied_probability`, `bookmaker` (= `"consensus"`), `source`, `captured_at`,
  `import_batch_id`, `odds_row_key` columns — no column change needed.

### 5.5 UI (Build Step 2 + 03 Odds)

A "Blend multiple books → consensus" path alongside the existing DK paste:
enter a BestFightOdds URL **and/or** paste a multi-book grid → click to
**preview** the computed consensus table (per fighter: book count, median
no-vig %, fair line, dispersion flag) → explicit **Save** writes the provenance
rows + consensus odds rows and chains the existing recompute. Mirrors the
preview→save discipline and in-place status refresh already used by the DK paste
(`00_build.py`).

## 6. Schema change + migration

One new table, one paired migration (`src/db/migrations.py`) and a schema test
(`tests/test_odds_persistence_schema.py` or analogous), per `docs/DEVELOPMENT_NOTES.md` §8:

```sql
CREATE TABLE IF NOT EXISTS odds_book_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
    fighter_name_raw TEXT NOT NULL,
    fighter_name_normalized TEXT NOT NULL,
    opponent_name_raw TEXT,
    book TEXT NOT NULL,
    american_odds INTEGER NOT NULL,
    source TEXT NOT NULL,            -- 'bestfightodds' | 'paste'
    captured_at TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    import_batch_id TEXT,
    UNIQUE(slate_id, fighter_name_normalized, book),
    CHECK (american_odds <> 0)
);
```

No change to `odds_rows` or `odds_match_results` columns. The consensus row is an
ordinary `odds_rows` insert.

**Resolved decisions (pinned for slice 5; supersede the matching §11 open
questions).**

- **Migration placement.** The `odds_book_lines` `CREATE TABLE` is added to
  `SCHEMA_STATEMENTS` in `src/db/schema.py` (how every prior table landed, and how
  the `apply_schema`-only schema-test fixture and fresh DBs receive it) **and**
  paired with an idempotent `_ensure_odds_book_lines_table(conn)` in
  `src/db/migrations.py` (guarded on `sqlite_master`, `CREATE TABLE IF NOT EXISTS`,
  registered in `apply_pending_migrations`) so an existing on-disk DB receives it
  via `bootstrap_database`. This is the repo's first CREATE-TABLE-in-`migrations.py`
  helper; it satisfies `docs/DEVELOPMENT_NOTES.md` §8 ("new tables require a paired migration in
  `src/db/migrations.py`") without diverging from the established `schema.py`
  mechanism. A migration test (mirroring
  `tests/test_page_bootstrap_pre_migration.py`) pins that a pre-migration DB gains
  the table after `bootstrap_database`.
- **`source` value.** Stored lowercase as `'bestfightodds' | 'paste'` (the per-book
  origin), enforced in the repository as a code allow-list — **not** a DDL `CHECK` —
  matching the existing odds-persistence convention (`odds_rows.source` carries no
  schema `CHECK`).
- **Exact consensus probability.** `odds_rows.implied_probability` carries the
  **exact** consensus `prob_a`, not the round-tripped implied of the integer fair
  line. Because `OddsRowRepository.create` today re-derives `implied` from
  `american_odds`, slice 5 adds an **optional** `implied_probability: float | None =
  None` parameter to `create` (additive; still validated `0 < p < 1`; the default
  preserves every existing caller). The consensus writer passes `prob_a`. The
  projection is unaffected either way because it reads the de-vigged `american_odds`
  pair, not `implied_probability`.

## 7. How consensus becomes the effective odds

The matcher writes one `odds_match_results` row per `odds_rows` row, with
`effective_status`. For the projection to use the consensus and *not* a stray
per-book line, the design keeps per-book lines **out of `odds_rows`** entirely
(they live in `odds_book_lines`). Therefore the only `odds_rows` entry per
fighter is the consensus row → it auto-matches → `effective_status=auto_match`
→ projection-eligible (`PROJECTION_V1_DESIGN.md` §11). No ambiguity, no
projection-side change, no new `effective_status` value.

**Last save wins (delete-then-insert).** Because `odds_rows` is otherwise
append-only — raw rows are immutable (§5.1) and `create_or_get` is
insert-or-noop — a re-blend that changes the fair line cannot be applied by
reusing the same `odds_row_key`. Slice 5 therefore introduces one scoped delete
path, `OddsRowRepository.delete_for_slate_source(slate_id, source="consensus")`,
and an atomic `replace_for_slate_source` built on it. The consensus save
**deletes the slate's prior `source="consensus"` rows and inserts the freshly
computed consensus rows in one transaction**, then **chains the recompute
afterward in its own transaction** (as every save path does — the recompute
reapplies active overrides). `ON DELETE CASCADE` clears the stale
`odds_match_results` rows; the recompute rebuilds them. This is the single odds-source identity per fighter for
the consensus path (**resolves §11 #1**); its supersede/undo story is the re-save
itself (`docs/DEVELOPMENT_NOTES.md` §11). The consensus path keys on `bookmaker = source =
"consensus"`, distinct from the DK-paste key, so a DK-paste row and a consensus row
can coexist on separate slates for A/B comparison.

## 8. UI flow (preview → save), step by step

1. User enters a BFO event URL and/or pastes a multi-book grid (either or both).
2. Explicit **"Preview consensus"** click: fetch (if URL) + parse + blend (§2),
   render a read-only table — per fighter: # books, median no-vig %, fair line,
   and a dispersion flag when books disagree widely (§9). Nothing saved.
3. Name-match preview against the DK roster, reusing the existing matcher /
   closest-name suggester so mismatches (e.g. "Bruno Gustavo da Silva" →
   "Bruno Silva") are surfaced before save.
4. Explicit **"Save consensus to slate"**: write `odds_book_lines` (provenance)
   + the per-fighter `source="consensus"` `odds_rows`, then chain the existing
   recompute. Step 2 odds-status / gate refresh in place (the rerun pattern just
   added).

## 9. Edge cases

- **One book only for a fight** (< `MIN_BOOKS`): do **not** silently treat as
  consensus. Surface as low-confidence; user can accept it as a single-book line
  or fill in more. No silent truncation (a `log`/inline note states it). At write
  time (slice 5) the per-book lines are still persisted to `odds_book_lines`, but
  **no `source="consensus"` `odds_rows` row is written** for a sub-`MIN_BOOKS`
  fight; it is returned in the save result for the UI (slice 6) to surface for
  accept-as-single-book / fill-more. This keeps a one-book line out of the
  projection path (**resolves the second half of §11 #2**).
- **Wide disagreement between books** (high dispersion): still blend (median is
  robust), but flag it in the preview so the user can eyeball it.
- **A fighter missing from one source**: use whatever books do have a line, as
  long as ≥ `MIN_BOOKS`.
- **Name mismatches** between a book grid and the DK roster: handled by the
  existing match/override queue before save — no new matching logic.
- **Pick'em / both sides near +100**: median + renormalize handles it; the fair
  line may be ±100/±101.
- **Stale or crossed lines** (A and B both favorites in one book): de-vig per
  book normalizes within that book; an impossible book is flagged, optionally
  dropped from the median.
- **Re-save / idempotence**: a second save **replaces** the slate's provenance
  rows (clear-and-rewrite of `odds_book_lines`) and its `source="consensus"`
  `odds_rows` rows (delete-for-slate-and-source, then re-insert — §7) without
  duplicating, **regardless of whether the blend value changed**. Idempotence does
  not depend on a stable `captured_at`; the delete makes the prior rows go away.

## 10. Slice plan (each its own commit + review)

1. **This design doc** (commit alone, pause for approval). ← current step.
2. **Consensus math module** — `odds_consensus.py`, pure, with hand-computed
   unit tests (de-vig-per-book → median → renormalize; the `MIN_BOOKS` rule;
   dispersion). No DB, no UI.
3. **BFO all-books parser** — additive function in `bestfightodds.py` + parser
   tests against saved HTML fixtures. (No change to the DK-only path.)
4. **Multi-book paste parser** — pure parser + tests (incl. the trailing
   non-odds column and blank cells).
5. **Schema + persistence (full vertical)** — `odds_book_lines` migration
   (`schema.py` `SCHEMA_STATEMENTS` entry **+** paired `_ensure_*` helper in
   `migrations.py`) + schema test + migration test + `OddsBookLineRepository`
   (clear-and-rewrite per slate); the parser-rows → `FightBookOdds` **pairing/merge
   adapter** plus the `compute_slate_consensus` call; the synthesized
   `source="consensus"` `odds_rows` writer with **delete-then-insert** idempotent
   replace (§7) and the optional `implied_probability` param (§6); and a
   `save_consensus_to_slate` service that persists provenance + consensus and chains
   the existing recompute. Parser-rows-to-DB and independently testable. **No
   Streamlit/UI and no fetch trigger** — those are slice 6.
6. **Wire save → recompute** and surface in the UI (BFO url + paste → preview →
   save), with AppTest coverage; confirm projection consumes the consensus row
   with no projection-code change.
7. **Real-feed validation**: a documented dry run (BFO fetch of a real event +
   a real pasted grid) producing consensus lineups, compared to the single-book
   build — analogous to the salary importer's real-file gate (`docs/DEVELOPMENT_NOTES.md` §8).

Each slice is independently shippable and testable; none promotes
`effective_status` into the optimizer/alerts/exports (those gates stay closed).

### 10.7 Real-feed parser hardening (BFO all-books)

Slice 7's real-feed run (CURRENT_STATUS, "Smoke / validation") proved the
slice-3 all-books parser (`parse_bestfightodds_all_books`) **does not parse a
real BestFightOdds event page.** It had only ever been exercised against the
hand-written synthetic fixture `bestfightodds_allbooks_sample.html`, which
modelled an idealized "header row + one clean row per fighter" grid. A live BFO
event page is structurally different in ways that fixture omitted, and every
omission became a failure mode (~617–626 rows, props read as fighters,
`"43417Belal Muhammad"` names, 3/24 fighters matched, 1–3 books each). This
section specifies the real structure and the hardening; it is the design behind
the slice that turns the broken parser green. **Scope: the BFO all-books HTML
parser only** — the multi-book paste path is the working consensus fallback in
the meantime, and the DraftKings-only `parse_bestfightodds_html` path is
untouched.

**Real BFO event-table structure (observed; captured sanitized as the fixture
`tests/fixtures/bestfightodds_allbooks_real_structure.html`).** The page renders
one large `<table>` whose header row exposes the book columns (the grid anchor),
then a `<tbody>` that, *per fight*, interleaves:

1. **A matchup-header row** whose leading cell links to a `/cnadm/matchups/…`
   page (not a fighter).
2. **The two head-to-head moneyline rows** — the only rows that are actually
   fighters. Each leading name cell is a `<th scope="row">` holding a
   rotation/pictureid anchor (e.g. `9001`) **followed by** the real fighter
   anchor `/fighters/…` whose name sits in a `<span>`. The flattened cell text
   is therefore `"9001Aiden Stone"` — the source of the `"43417Belal Muhammad"`
   mangling.
3. **Many round-total / method / prop rows** ("Under 1½ rounds", "Aiden Stone
   wins by KO/TKO", "Over 2½ rounds") that *also* carry odds cells of their own.
   The slice-3 parser, having no way to tell them from fighters, emitted every
   one — the ~617-row blowup.

Three further real-markup details the synthetic fixture omitted:

- **Movement-arrow odds spans.** A live odds cell is `<td class="but-sg">` with
  a value `<span>` and a trailing up/down arrow `<span>`, flattening to
  `"-150▲"`. The old `parse_moneyline(cell.text)` choked on the arrow, dropping
  books — why real fighters showed only 1–3.
- **Promo-suffixed book headers** ("Polymarket $50 Bonus", "Kalshi $10 Free")
  and a **server-empty DraftKings / BetMGM column** — present in the header (so
  it still anchors the grid) but blank in the cells for some fighters, because
  BFO loads those books client-side. The parser must read the column when the
  cell has a value and simply skip it when blank, never inventing a line.
- **A trailing "Props" count cell** (e.g. `92`) that is an unsigned integer, not
  a book line.

**Parsing approach (the hardening).** Five changes, all additive and confined to
the all-books path:

1. **Row isolation by the `/fighters/` anchor.** `_Cell` gains an additive
   `hrefs` tuple (the cell's `<a href>` set); the harvester collects it. A row
   is a fighter row **iff its leading cell links to `/fighters/`**
   (`_row_is_fighter_row`). Matchup-header, round-total, method, and prop rows
   carry no such link and are dropped structurally. This is the fix for the
   617-row problem — and it is a *row-level* discriminator added **alongside**
   the shared column locator, not a change to it (see point 5).
2. **Name cleaning.** `_clean_fighter_name` strips the leading rotation/pictureid
   digits (`^\s*\d+\s*`) from the flattened name cell: `"9001Aiden Stone"` →
   `"Aiden Stone"`.
3. **Arrow-tolerant odds parse.** `_parse_book_moneyline` reads the first
   explicitly-signed American token and requires magnitude ≥ 100 — mirroring the
   multi-book paste parser's discriminator (`_AMERICAN_TOKEN` + a magnitude
   floor). The trailing arrow span is ignored, the unsigned "Props" count is
   never misread as a phantom book, and a server-empty cell yields `None` so
   that one book is skipped for that fighter.
4. **Opponent pairing after filtering.** Consecutive-row adjacency runs over the
   already-filtered fighter rows, so a fighter pairs with the next *real*
   fighter rather than an interleaved prop row.
5. **The grid anchor is unchanged.** The table and its header row are still
   located by `_find_dk_column` exactly as the DK-only path locates them
   (decision #2/#3); the shared function is **not** modified. This honors
   `docs/DEVELOPMENT_NOTES.md` / the slice constraint "do not hack the shared `_find_dk_column`
   ad hoc," and it leaves the "Surfaced, not yet designed" `_find_dk_column`
   concern (no `<th>`/`<td>` structural check — a fighter literally named
   "DraftKings…" could be mis-read as the header) **out of scope here**; that
   remains its own future design pass.

**Boundaries.** No new network behavior (same single user-triggered GET); the
DraftKings-only `parse_bestfightodds_html` is untouched (it reads `text`/`meta`
only, and `hrefs` is additive with a `()` default); no schema change, no
projection change, no `effective_status` promotion; still pure / offline (no
de-vig or blend — that is the consensus service).

**Fixture discipline (`docs/DEVELOPMENT_NOTES.md` §7).** The new fixture is **hand-written,
sanitized, and structure-faithful** with *invented* fighters and lines — not a
copied page dump. It reproduces the markup shapes above (two fights / four
fighters so opponent pairing is exercised) and is the regression anchor for the
real-feed failure modes.

**Tests.** The real-structure fixture pins each slice-7 failure mode: row count
bounded to the four fighter rows, prop/total/matchup rows excluded, the rotation
prefix stripped, opponents paired *after* filtering, ≥ 4 books per fighter with
arrow-decorated values parsed correctly, DraftKings read-when-present /
skip-when-empty, and the trailing props-count ignored — plus a regression test
that the original synthetic fixture still parses to the same four fighters. The
pre-existing all-books synthetic tests gain `/fighters/` hrefs on their fighter
cells to satisfy the new structural discriminator (a real page always carries
them). Run the full suite with `.venv/bin/python -m pytest`.

**Real-feed close-out.** Turning the parser green against the saved fixture is
necessary but, per `docs/DEVELOPMENT_NOTES.md` §8, not sufficient: the slice-7 gate stays open
until a one-click **BFO → consensus** dry run against a live event page is
documented in CURRENT_STATUS (no raw page data committed — fixture only).

## 11. Open questions (resolve before the slice that hits them)

1. **Per-fighter source identity.** ✅ **RESOLVED (slice 5, §7).** The consensus
   path is its own odds-source identity, keyed on `bookmaker = source =
   "consensus"`; a re-save is **last-write-wins via delete-then-insert** of the
   slate's `source="consensus"` rows. DK-paste and consensus rows use distinct keys
   and are intended to be A/B-compared on separate slates.
2. **`MIN_BOOKS` default + single-book handling.** ✅ **RESOLVED.**
   `MIN_BOOKS_DEFAULT = 2` is implemented and tested (slice 2). A sub-`MIN_BOOKS`
   fight is **accepted-with-warning, not blocked**: its provenance is persisted but
   no consensus `odds_rows` row is written, and it is surfaced in the save result
   (slice 5, §9).
3. **Which books count.** ✅ **RESOLVED (slice 2).** Equal-weight median over *all*
   parsed books; no book excluded by default. Sharp-weighting stays deferred (§12).
4. **BFO column identification.** ✅ **RESOLVED (slice 3).** The all-books parser
   labels columns where it can and falls back to positional `Book{n}` labels with
   same-label disambiguation. Real-page fidelity remains the slice-7 gate.
5. **Provenance retention.** ✅ **RESOLVED (slice 5).** `odds_book_lines` is **kept
   and rewritten on each consensus save** (clear-and-rewrite per slate); it is the
   auditable, rebuildable record the blend was computed from.

## 12. Deferred / future (not this design)

- Sharp-book weighting (favor Polymarket / Kalshi / Pinnacle).
- Shin's de-vig (reduces favorite-longshot bias on favorite-heavy cards).
- Trimmed mean / outlier rejection beyond the median's robustness.
- Logarithmic opinion pooling.
- Line-movement / closing-line capture over time.
- Using BFO's own published consensus column directly (alternative to computing
  our own).

## 13. Test plan (per slice)

- **Math** (`odds_consensus.py`): hand-computed fixtures — known book grids →
  known consensus; the de-vig-first ordering; median vs mean divergence on an
  outlier; `MIN_BOOKS` behavior; pick'em and lopsided-favorite cases.
- **Parsers**: BFO all-books against saved HTML fixtures (incl. a missing book
  cell); paste parser against a real grid fixture incl. trailing non-odds
  column and unicode minus.
- **Schema**: `odds_book_lines` migration present, columns/constraints pinned.
- **Persistence**: provenance + consensus rows written; idempotent re-save;
  cascade delete with the slate.
- **Projection integration**: a slate with consensus rows yields the expected
  per-fighter win prob with **no change** to `projection_input_service`.
- **UI** (AppTest): preview writes nothing; save persists + recomputes + refreshes
  status in place; name-mismatch surfaces in the queue.
- **Real-feed** (§10.7): documented dry run before the importer is called
  "complete."

## 14. Non-goals restated (guardrails)

- No NFL / Showdown / Pick6; UFC Classic only.
- No required API, no second fetcher, no background/page-load fetching.
- No props/totals/spreads.
- No `effective_status` promotion into optimizer / alerts / exports.
- No projection-formula coefficient change (the blend changes the *input*
  win probability, never the `* 70` / bonus structure — `docs/DEVELOPMENT_NOTES.md` §4).
