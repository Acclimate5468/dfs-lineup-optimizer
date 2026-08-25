# Odds Acquisition v0 — Design / Scope

**Status:** design + scope only. No implementation in this doc. No app code,
no fetcher, no UI, no schema change, no test, no DB write, no network call.

**Why this exists.** The app can build lineups *once odds exist*, but the user
currently has no practical way to get UFC sportsbook moneylines into it — the
prior scope wording ("CSV / manual entry only, no scraping") left the user
holding a file they have no realistic way to produce. Odds Acquisition v0 is
therefore promoted to **core v0 scope**: the app must help the user *acquire*
UFC sportsbook moneylines, not merely accept them if already on hand.

A live-source feasibility spike against `data/uploads/sources/UFC_DATA.json`
found **BestFightOdds** is the only "Green" public sportsbook-moneyline source:
static HTML, plain fighter names, American moneylines, and a DraftKings column.
Every other betting source there was JS-rendered, blocked, keyed, paywalled, or
better treated as a paste / manual fallback.

**Companions.** This design feeds the *existing* odds pipeline; it does not
replace it.

- `docs/ODDS_NEWS_SNAPSHOT_DESIGN.md` — the one normalized fight-week format.
  An acquired moneyline row is an **input source** into that contract, exactly
  like a manual snapshot or an uploaded file.
- `docs/ODDS_MATCHING_DESIGN.md` — odds row → DK name-match → match result.
- `docs/ODDS_PERSISTENCE_DESIGN.md` — `odds_rows` (immutable per import) →
  `odds_match_results` → `manual_match_overrides` → recompute. Acquisition
  reuses this flow unchanged; it adds **input paths**, not storage.
- `docs/PROJECTION_V1_DESIGN.md` §4 — the projection consumes exactly one
  acquired number: implied win probability from the moneyline.
- `docs/DEVELOPMENT_NOTES.md` §2/§3 (narrowed scope), §9 (design-first), §11 (UI write rules).

---

## 1. Locked decisions

These are approved and may not be silently changed (per `docs/DEVELOPMENT_NOTES.md` §4-style
discipline — any change needs explicit user approval).

1. **Target.** v0 acquires **UFC sportsbook moneylines only.** No props, no
   totals, no spreads, no non-UFC sport.
2. **First public fetcher candidate: BestFightOdds.** It is the only Green
   source from the spike. No other fetcher ships in v0 without its own approval.
3. **Prefer the DraftKings column** when a source exposes per-book lines.
4. **DK-column fallback is deferred.** If the DK column is missing, a *later*
   phase may fall back to a consensus / median across major books. This is
   **not** in Phase 1 and is not implemented on the strength of this doc.
5. **Four acquisition modes are approved**, all producing the same normalized
   row (§2):
   - **public-source fetchers** (BestFightOdds first),
   - **optional API providers** (only if a user supplies a key; never required),
   - **paste / table parser** (copy a book/aggregator table in),
   - **manual odds entry** (type a moneyline directly).
6. **Polymarket is explicitly out of sportsbook odds ingestion.** It is a
   prediction market, not a sportsbook moneyline. It belongs later under
   analytics / market-signal tooling only, never mixed into the sportsbook
   moneyline rows that feed the projection.
7. **One normalized output row** for *every* acquisition path:

   | field                     | required | notes                                            |
   | ------------------------- | -------- | ------------------------------------------------ |
   | `fighter_name`            | yes      | as printed by the source; DK-matched downstream  |
   | `opponent`                | if avail | other side of the fight, when the source pairs it |
   | `american_moneyline`      | yes      | e.g. `-150`, `+130`                              |
   | `source`                  | yes      | logical source, e.g. `bestfightodds`             |
   | `book`                    | if avail | e.g. `draftkings` (preferred per #3)             |
   | `source_url`              | yes      | exact URL the row came from                      |
   | `collected_at` / `fetched_at` | yes  | UTC timestamp of acquisition                     |
   | `status` / `confidence`   | yes      | parse confidence + review state                  |

   This row is an **input** to the `ODDS_NEWS_SNAPSHOT` contract and the
   existing `odds_rows` ingestion — it does not introduce a parallel store.
8. **Preview before save, always.** Every path renders the normalized rows for
   human review before anything is written.
9. **No automatic DB writes from a fetch.** Fetch and save are separate user
   actions. A fetch alone never touches SQLite.
10. **Reuse the existing flow.** Saving acquired rows goes through the existing
    odds snapshot / `odds_rows` / match / recompute path. No new persistence
    layer, no new match logic.
11. **Explicit user trigger only.** Every fetch is an explicit, user-initiated
    action (a button press), never implicit.
12. **Hard no's in v0:** no background jobs, no page-load / on-open fetching,
    no CAPTCHA / proxy / paywall bypass, no login or authenticated scraping,
    no premium / keyed scraping beyond approved Green sources, no browser
    automation / headless-browser rendering. v0 fetches plain static HTML from
    a public URL, on demand, and nothing more.

---

## 2. Normalized row → existing pipeline

```
acquisition path                       reuse (unchanged)
─────────────────                      ─────────────────
public fetcher  ┐
api provider    ├─► normalized          ─► ODDS_NEWS_SNAPSHOT format
paste parser    │   moneyline rows  ──►    ─► odds_rows (immutable per import)
manual entry    ┘   (§1.7)          preview ─► odds_match_results (DK name-match)
                                     + save  ─► manual_match_overrides / recompute
                                              ─► implied win prob → projection
```

The four modes differ only in *how the normalized rows are produced*. From the
preview onward they are identical, and the storage / match / recompute path is
the one already designed in `ODDS_PERSISTENCE_DESIGN.md`.

---

## 3. Implementation phases (each its own slice, design-first per `docs/DEVELOPMENT_NOTES.md` §9)

| phase | scope | network | DB | UI |
| ----- | ----- | ------- | -- | -- |
| **1** | Pure BestFightOdds HTML parser from a **saved fixture**. Parses static HTML → normalized rows (§1.7). Unit-tested against the fixture. | none | none | none |
| **2** | Explicit **live fetch → preview**. One user-triggered GET of the public URL, parsed via Phase 1, rendered for review. | yes (on demand) | none | preview only |
| **3** | **Save** previewed moneylines through the existing odds save / recompute path (§2). | — | via existing repos | save action (§11 rules) |
| **4** | **Paste / table parser** — same normalized output from pasted text. | none | reuse Phase 3 save | paste box + preview |
| **5** | **Manual odds entry** cleanup — tidy the type-a-moneyline path onto the same normalized row. | none | reuse Phase 3 save | form |
| later | Polymarket / analytics / **market-signal tab**. Separate prediction-market signal, never folded into sportsbook moneyline rows. **Not now.** | — | — | — |

Each phase is a separate design-doc-referencing slice. No phase is approved for
code on the strength of *this* doc — this doc approves the **scope and ordering
only**. Phase 1 is the first eligible implementation slice and still requires
its own go-ahead.

Optional **API providers** (decision #5) slot in after Phase 3 as an alternate
producer of the same normalized rows; they are only ever exercised when the
user supplies a key and are never a required dependency.

---

## 4. Out of scope for Odds Acquisition v0

- The DK-column → consensus/median fallback (decision #4, deferred).
- Any second fetcher beyond BestFightOdds without its own approval.
- Props / totals / spreads / non-UFC anything.
- Polymarket or any prediction-market source in the sportsbook moneyline path.
- Login, paywall/CAPTCHA/proxy bypass, premium scraping, browser automation,
  background or page-load fetching (decision #12).
- Any change to projection coefficients, optimizer, exports, or B6 reasoning.
