"""Odds acquisition providers (Odds Acquisition v0).

Each provider turns one acquisition source's raw payload into the single
normalized moneyline row defined in ``docs/ODDS_ACQUISITION_V0_DESIGN.md``
§1.7. The **parsers** are pure — they parse already-fetched input and never
perform DB writes or UI work. Saving (Phase 3) and the rest of the wiring live
in separate, later slices.

Modules:

- :mod:`src.ingestion.providers.bestfightodds` (Phase 1) — the pure,
  network-free DraftKings-column BestFightOdds HTML parser.
- :mod:`src.ingestion.providers.bestfightodds_fetch` (Phase 2) — the explicit,
  user-triggered live fetch that GETs a public BestFightOdds page and feeds it
  to the Phase 1 parser for **preview only** (no DB write, no save). It owns
  the sole network I/O in the acquisition path; the parser stays pure.
- :mod:`src.ingestion.providers.draftkings_paste` (Phase 4) — the pure,
  network-free parser that turns copied DraftKings UFC odds-board *text* (the
  user views the public board in their own browser and pastes it) into the same
  normalized §1.7 rows. No fetch, no DB, no UI.
- :mod:`src.ingestion.providers.multi_book_paste`
  (``ODDS_CONSENSUS_DESIGN.md`` §5.2) — the pure, network-free parser that turns
  a pasted multi-book odds-comparison *grid* (a header row of book names, one
  row per fighter of American lines) into per-fighter all-book lines for the
  multi-book consensus blend. The paste counterpart of the BFO all-books parser
  (§5.1); no fetch, no DB, no UI, no blend math.
"""
