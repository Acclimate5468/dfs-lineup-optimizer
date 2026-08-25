# Odds / News Snapshot Contract — Design (S2)

Status: **design only.** No implementation in this slice. No schema change,
no DB write, no network call, no scraping, no API integration, no projection
or optimizer change. Local / manual / human-in-the-loop (HITL) only.

Companion to `docs/DEVELOPMENT_NOTES.md` §2 (v0 scope: UFC DK Classic only), §3 (out of
scope — no direct Odds API, CSV/manual only, no scraping), §7 (data/file
safety — odds feed data must not be committed), §9 (design-before-
implementation), §11 (UI write-action rules), and the sibling docs:

- `docs/ODDS_MATCHING_DESIGN.md` / `docs/ODDS_PERSISTENCE_DESIGN.md` — the
  existing odds row → DK name-match → match-result pipeline this snapshot
  feeds *into*. The snapshot is an **input source**, not a replacement.
- `docs/PROJECTION_V1_DESIGN.md` §4 — the default projection consumes
  exactly one snapshot-derived number: implied win probability. Props
  (ITD / distance) are carried but inert in v0.
- `docs/MISMATCH_ALERTS_V1_DESIGN.md` / `docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`
  — news flags, line movement, and staleness become alert rows / gate
  warnings later.
- `docs/FIGHTER_STATUS_V1_DESIGN.md` — withdrawal / short-notice / injury
  news flags become *suggested* fighter-status changes (HITL).
- `docs/DK_GAME_INFO_PAIRING_DESIGN.md` — DK `Game Info` already pairs the
  card; the snapshot reconciles to those same DK names.

---

## 1. Purpose & scope

Define **one normalized format** for fight-week odds + news so that every
input path — a hand-built manual snapshot, an uploaded file, or a future
*approved* single-source fetcher — produces the **same** structure the app
validates, previews, and (later) imports.

`UFC_DATA.json` is a **source manifest** (a curated list of where to look),
not this snapshot. The snapshot is the *normalized result* of having looked.
The two are different artifacts and stay separate.

### 1.1 In scope (this design)
- The JSON snapshot schema (envelope + per-fighter entries + enrichment).
- A pure parser/validator plan (no I/O beyond reading the given bytes).
- A read-only UI preview/import plan (no writes on load).
- How the snapshot connects to Odds Review, Alerts, Fighter Status, Manual
  Review, and projections **later**.

### 1.2 Explicit non-goals
- **No fetching / scraping / API** (`docs/DEVELOPMENT_NOTES.md` §3). The format is designed
  so a future *approved, licensed, single-source* fetcher could emit it, but
  no fetcher is built or implied here.
- **No DB writes / no schema / no migration** in this slice.
- **No projection or optimizer change.** The formula coefficients
  (`docs/DEVELOPMENT_NOTES.md` §4) are untouched. Props are stored, not consumed.
- **No trust in external math.** The app stores the raw moneyline as the
  source of truth and **derives** implied probability itself, exactly as the
  current odds pipeline does. Snapshot-supplied probabilities are treated as
  *advisory cross-checks*, never as the value fed to projections.
- **No auto-application of news.** News flags only *suggest*; the user
  applies status changes through existing HITL controls.

---

## 2. Design principles

1. **Raw inputs are canonical; derived values are advisory.** Store the
   American moneyline; compute implied probability internally. If the
   snapshot also carries an `implied_probability`, it is compared (and a
   mismatch is surfaced) but never trusted over the app's own derivation.
2. **One entry per fighter-side**, mirroring how `odds_rows` already work,
   so the snapshot drops straight into the existing matcher. A bout is the
   pair of two entries sharing an `opponent_name` (same idea as DK
   `Game Info`).
3. **Required core, optional enrichment.** A snapshot is valid with only
   identity + moneyline. Props, movement, and news degrade gracefully to
   "not provided" rather than failing validation.
4. **Provenance and freshness are first-class.** Every entry can name its
   source and its own `collected_at`; the validator derives staleness so the
   UI and the gate can warn.
5. **Version everything.** `schema_version` gates parsing so the format can
   evolve without breaking old snapshots.
6. **No proprietary payloads leak.** Per `docs/DEVELOPMENT_NOTES.md` §7, real snapshot files
   are gitignored and never pasted into docs/commits. All examples in this
   doc use synthetic names.

---

## 3. Proposed schema (v1)

Top-level is a single JSON object: an **envelope** plus an `entries` array.

### 3.1 Envelope

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `schema_version` | int | ✔ | `1`. Gates the parser. |
| `snapshot_kind` | enum | ✔ | `odds_news`. Reserved for future kinds. |
| `event` | object | ✔ | Slate identity — see §3.2. |
| `collected_at` | string | ✔ | ISO-8601 **UTC**, second precision (`2026-05-29T17:30:00Z`). Snapshot-level capture time. |
| `collected_by` | object | ✔ | Provenance — see §3.3. |
| `sources_checked` | array | ✔ | Which manifest sources were consulted — see §3.4. May be empty `[]` (validator warns). |
| `staleness_policy` | object | – | Optional thresholds — see §3.6. App default applies when absent. |
| `notes` | string | – | Free-text, sanitized; slate-level context. |
| `entries` | array | ✔ | Per-fighter records — see §3.5. May be empty (validator warns, not errors). |

### 3.2 `event` (slate identity)

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `name` | string | ✔ | e.g. `"UFC 999: Fighter A vs Fighter B"`. |
| `date` | string | ✔ | `YYYY-MM-DD` (event local date as DK shows it). |
| `dk_game_info_hint` | string | – | A `Game Info`-style token to help bind to a DK slate (e.g. `"FighterA@FighterB 05/30/2026 08:00PM ET"`). Binding to an actual slate id stays a UI step (§5), never hard-coded here. |
| `event_id` | string | – | Optional external id; opaque to the app. |

> The snapshot is **not** itself a slate. Binding a snapshot to a persisted
> DK slate is a deliberate UI action (§5), reconciled by event name/date and
> DK-name matching — never auto-assumed.

### 3.3 `collected_by` (provenance)

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `method` | enum | ✔ | `manual` \| `file_upload` \| `fetcher`. (No fetcher exists yet; the value is reserved.) |
| `agent` | string | – | Free label, e.g. `"hand-entry"`, `"odds_api_v1"`. |
| `tool_version` | string | – | Emitter version, for debugging format drift. |

### 3.4 `sources_checked[]`

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `name` | string | ✔ | Human label, ideally matching a `UFC_DATA.json` source `name`. |
| `url` | string | – | Source URL (stored as text; the app never fetches it). |
| `category` | enum | – | `Betting` \| `News` \| `Analytics` \| `Community` \| `Official` \| `Tool` \| `Insiders` (mirrors the manifest). |
| `checked_at` | string | – | ISO-8601 UTC; when this source was consulted. |

### 3.5 `entries[]` — one per fighter-side

**Identity (required core):**

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `fighter_name` | string | ✔ | Verbatim as the source spells it (raw). |
| `opponent_name` | string | ✔ | Verbatim. The (fighter, opponent) pair defines the bout. |
| `dk_name_hint` | string | – | Optional cleaned DK name; matching still goes through the existing normalizer at import. |

**Odds (core = moneyline; rest optional):**

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `moneyline` | int | ✔* | American odds (e.g. `-220`, `+185`). Non-zero. *Required unless the entry is news-only (see `entry_kind`). |
| `implied_probability` | float | – | `[0,1]`. **Advisory** — app recomputes from `moneyline`. Mismatch beyond tolerance → warning. |
| `no_vig_probability` | float | – | `[0,1]`. Optional fair prob if the source de-vigged the pair. Advisory. |
| `book` | string | – | Sportsbook / source of this line (e.g. `"DK"`, `"Pinnacle"`, `"consensus"`). |
| `line_open` | int | – | Opening American line, if known. |
| `line_current` | int | – | Latest American line (defaults to `moneyline`). |
| `line_movement` | enum | – | `toward` \| `away` \| `flat` \| `unknown` — direction for *this* fighter. Derivable from open/current if both present. |

**Method / prop markets (optional, stored, inert in v0):**

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `itd_odds` | int | – | "Inside the distance" (finish) American odds. |
| `decision_odds` | int | – | Win-by-decision American odds, if quoted. |
| `goes_distance` | object | – | `{ "yes": int, "no": int }` American odds for fight goes/doesn't go distance. |

**News (optional):**

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `news_flags` | array<enum> | – | Zero or more of: `injury` \| `replacement` \| `withdrawal` \| `short_notice` \| `weight_miss` \| `reschedule` \| `other`. |
| `news_note` | string | – | Short free-text, sanitized; the human-readable detail. |
| `news_source` | string | – | Where the news came from (label). |

**Provenance / freshness / trust (per entry):**

| Field | Type | Req | Notes |
| --- | --- | --- | --- |
| `source_name` | string | – | Defaults to envelope-level source. |
| `source_url` | string | – | Text only; never fetched. |
| `collected_at` | string | – | ISO-8601 UTC; overrides envelope `collected_at` for this entry. |
| `confidence` | float | – | `[0,1]` emitter confidence in this entry. |
| `status` | enum | – | `ok` \| `needs_review` \| `conflict` \| `unmatched` \| `stale`. Emitter hint; the validator may *raise* severity (e.g. force `stale`) but the import/matcher owns the final verdict. |
| `entry_kind` | enum | – | `odds` (default) \| `news_only`. `news_only` entries may omit `moneyline`. |

### 3.6 `staleness_policy` (optional)

| Field | Type | Notes |
| --- | --- | --- |
| `warn_after_hours` | number | Entry older than this (vs. its `collected_at`) → ⚠️ stale warning. App default e.g. `12`. |
| `block_after_event_start` | bool | If the snapshot `collected_at` is **after** `event.date` start, flag the whole snapshot stale. Default `true`. |

### 3.7 Synthetic example (illustrative — fake data)

```json
{
  "schema_version": 1,
  "snapshot_kind": "odds_news",
  "event": {
    "name": "UFC 999: Fighter A vs Fighter B",
    "date": "2026-05-30",
    "dk_game_info_hint": "FighterB@FighterA 05/30/2026 08:00PM ET"
  },
  "collected_at": "2026-05-29T17:30:00Z",
  "collected_by": { "method": "manual", "agent": "hand-entry", "tool_version": "snapshot-1" },
  "sources_checked": [
    { "name": "Example Book", "url": "https://example.test", "category": "Betting", "checked_at": "2026-05-29T17:25:00Z" },
    { "name": "Example Beat Writer", "category": "Insiders", "checked_at": "2026-05-29T17:28:00Z" }
  ],
  "staleness_policy": { "warn_after_hours": 12, "block_after_event_start": true },
  "entries": [
    {
      "fighter_name": "Fighter A", "opponent_name": "Fighter B",
      "moneyline": -180, "implied_probability": 0.643, "book": "Example Book",
      "line_open": -150, "line_current": -180, "line_movement": "toward",
      "itd_odds": 145, "goes_distance": { "yes": 120, "no": -150 },
      "news_flags": [], "confidence": 0.9, "status": "ok",
      "source_name": "Example Book", "collected_at": "2026-05-29T17:25:00Z"
    },
    {
      "fighter_name": "Fighter B", "opponent_name": "Fighter A",
      "moneyline": 160, "book": "Example Book",
      "news_flags": ["short_notice", "weight_miss"],
      "news_note": "Stepped in on 10 days notice; missed weight by 2 lbs.",
      "news_source": "Example Beat Writer",
      "confidence": 0.6, "status": "needs_review"
    }
  ]
}
```

---

## 4. Parser / validator plan

A **pure** module (proposed `src/ingestion/odds_news_snapshot.py`), modelled
on the existing `dk_salary_importer` / `odds_csv_importer` validate→load
split. No DB, no network, no file writes — it only consumes bytes/str.

Public surface (proposed):

- `parse_snapshot(raw: bytes | str) -> ParsedSnapshot` — JSON decode +
  envelope coercion. Raises `SnapshotFormatError` on unparseable/unsupported
  `schema_version`.
- `validate_snapshot(parsed) -> SnapshotValidationReport` — per-entry
  `errors` (reject) vs `warnings` (keep), plus envelope-level findings.
- Dataclasses: `SnapshotEnvelope`, `SnapshotEntry`, `SourceChecked`,
  `SnapshotValidationReport(entries_ok, errors, warnings, summary)`.

**Reject (hard error) when:**
- `schema_version` missing/unknown; `snapshot_kind` unknown.
- envelope missing `event.name`/`event.date`/`collected_at`/`collected_by.method`.
- `collected_at` not ISO-8601 UTC.
- an `odds` entry missing `fighter_name`/`opponent_name`/`moneyline`, or
  `moneyline == 0` / non-integer.
- `implied_probability`/`no_vig_probability`/`confidence` outside `[0,1]`.
- unknown enum value in `news_flags` / `status` / `line_movement` /
  `entry_kind` / `collected_by.method`.

**Warn (keep, surface) when:**
- `sources_checked` empty, or `entries` empty.
- `implied_probability` disagrees with the value derived from `moneyline`
  beyond a tolerance (e.g. > 0.03) → "source math differs; app value used".
- entry/snapshot `collected_at` older than `warn_after_hours`, or after
  event start (per `staleness_policy`).
- a bout is one-sided (a `fighter`/`opponent` pair with only one entry) or
  the two sides disagree on the bout.
- `news_only` entry carries `news_flags` but no `news_note`.
- duplicate fighter across entries (same normalized name twice).

**Determinism / safety:** no mutation of input; stable ordering; name
normalization for matching reuses the existing odds normalizer
(`normalize_name_aggressive`) but the **raw** names are preserved verbatim in
the parsed entry. Free-text fields are length-capped and stripped of control
characters at parse (defensive, not semantic).

Ships in its **own** slice with unit tests (synthetic fixtures only), per
`docs/DEVELOPMENT_NOTES.md` §8 — no real-feed file committed.

---

## 5. UI preview / import plan (read-only)

Lands on the streamlined **"Odds & news"** surface. Read-only end to end on
load (`docs/DEVELOPMENT_NOTES.md` §11): upload/paste → validate → preview. **No writes.**

1. **Upload / paste** a snapshot JSON (file uploader + a paste box), browser-
   session only. Nothing persists yet.
2. **Envelope summary card:** event name/date, `collected_at` (+ relative
   age), `collected_by.method`, and a `sources_checked` chip list.
3. **Freshness banner:** ✅ fresh / ⚠️ stale (older than threshold) / ⛔ taken
   after event start — derived by the validator, not the file's own `status`.
4. **Validation panel:** counts of OK / warnings / errors, each line
   traceable to an entry. Errors block a (future) save; warnings don't.
5. **Preview table** (one row per entry): Fighter · Opponent · Moneyline ·
   *Implied (app-derived)* · Book · Movement · News flags · Status ·
   Freshness — with the same colored status cells the v2 prototype shows.
   The *app-derived* implied column makes the "raw is canonical" rule visible.
6. **Bind to slate (preview only):** show the candidate DK slate matched by
   `event` + DK-name reconciliation, and which fighters would match /
   wouldn't. Still no write — this previews what a later "Save & match"
   action would do.

A later slice adds an explicit **"Save snapshot to slate"** button (its own
write-path design + AppTest, per §11). This design does not write.

---

## 6. Downstream connections (later slices, not now)

- **Odds Review (`03_odds`):** each `odds` entry becomes an odds-row input to
  the existing `match_odds_to_dk` pipeline. `confidence`/`status` seed the
  review queue; unmatched entries feed `odds_unmatched_active`; the
  app-derived implied prob is what persists — the snapshot's own number is
  only cross-checked.
- **Projections:** unchanged. Only `moneyline → implied_win_probability`
  flows into the §4 formula. `itd_odds`/`goes_distance`/`decision_odds` are
  stored for a *future* projection variant and remain **inert** in v0 (the
  same "carried but not consumed" posture as `effective_status`).
- **Alerts (`05_alerts`):** `news_flags`, strong `line_movement`, and
  implied-vs-salary divergence become mismatch-alert rows
  (`mismatch_alerts_warn` / `_info`). News flags like `withdrawal` raise an
  alert immediately.
- **Fighter Status (`04_fighter_status`):** `withdrawal` → suggest *inactive*;
  `injury`/`short_notice`/`weight_miss` → suggest *warning*. **Suggestions
  only** — the user applies via existing HITL controls; the snapshot never
  writes status.
- **Manual Review gate (`06_manual_review`):** add (later, its own design) a
  freshness/snapshot warning check — e.g. "odds snapshot is N h old" or
  "taken after event start", and "unreviewed `needs_review`/`conflict`
  entries remain". These are gate *warnings*, consistent with the existing
  closed check set; no Blocking semantics are added without their own design.

---

## 7. Risks / open questions

1. **Vig handling.** Raw moneyline is canonical; the app derives implied. Open
   q: surface a no-vig pair probability in the preview, or keep it advisory
   only? (Lean: advisory only in v0.)
2. **Per-fighter vs per-bout duplication.** Two entries per bout can disagree.
   The validator flags one-sided/contradictory bouts; open q: auto-pair by
   `opponent_name` or require an explicit `bout_id`? (Lean: pair by name now,
   reconsider if collisions appear.)
3. **DK-name reconciliation.** External sources spell names differently;
   reuse the odds matcher, but aliases/handles may still miss. Snapshot binding
   stays a HITL step.
4. **Multiple books per fighter.** v1 stores one quote per entry. Open q:
   allow `quotes[]` with a designated primary for consensus/line-shopping, or
   keep one and let the emitter pick? (Lean: one in v1; `quotes[]` is a v2
   additive field.)
5. **Staleness thresholds.** What counts as "stale" on fight week (hours)?
   Configurable via `staleness_policy`; needs a sensible app default.
6. **News free-text safety.** `news_note` is uncontrolled text — cap length,
   strip control chars, and (per `docs/DEVELOPMENT_NOTES.md` §7) keep real snapshots
   gitignored; never commit feed content.
7. **Trust semantics of `confidence`/`status`.** Emitter-supplied; the app
   must be able to override (raise) severity and must not let a file mark
   itself "ok" to bypass review.
8. **Merging snapshots.** Incremental refresh (merge a newer partial snapshot
   into an existing one) is deferred — union/override semantics are a v2
   question.
9. **Schema evolution.** `schema_version` gating is in; a migration/upgrade
   path for old snapshots is deferred until a v2 field actually lands.

---

## 8. Recommended implementation slices

- **S2 (this doc).** The contract. Review/approve before any code. ◀ here
- **S3 — pure parser + validator** (`src/ingestion/odds_news_snapshot.py`)
  + unit tests on synthetic fixtures. No DB, no UI, no network.
- **S4 — read-only UI preview/import** on the Odds & news surface
  (upload/paste → validate → preview + freshness/validation panels) + AppTest.
  No writes.
- **S5 — Save snapshot to slate** (its own write-path + persistence design;
  decides reuse of `odds_rows` vs. a new table; schema change gated and
  reviewed separately).
- **S6 — news_flags → Fighter Status suggestions** (HITL; suggest-only).
- **S7 — staleness / unreviewed-entries → Manual Review warning check**
  (own design; warnings only).
- **(later) Approved single-source fetcher** that *emits this same format*.
  Out of current scope; gated on an explicit lift of `docs/DEVELOPMENT_NOTES.md` §3 and a
  key-secure, local-only, single-licensed-source design — never bulk
  scraping.

Each code slice ships with its tests in the same slice (`docs/DEVELOPMENT_NOTES.md` §8), and
no real-feed snapshot file is ever committed (`docs/DEVELOPMENT_NOTES.md` §7).
