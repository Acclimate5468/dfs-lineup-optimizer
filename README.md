# UFC DFS Lineup Optimizer

A local-first Streamlit research workbench for UFC daily fantasy lineups. The primary workflow imports a DraftKings Classic salary file, assembles and reviews fight/odds inputs, computes explainable projections, and generates salary-cap-constrained research lineups.

The codebase also contains an additive, experimental Captain workflow. It is kept separate from the persisted Classic pipeline.

## Implemented components

- DraftKings salary CSV parsing and validation
- SQLite-backed slates, fighters, odds rows, match results, and review overrides
- Fight grouping and scheduled-round handling
- Manual, CSV, paste/table, snapshot, and explicit public-page odds inputs
- Name normalization, fuzzy matching, review queues, and operator overrides
- Implied-probability and consensus calculations
- Default and finish-aware projection modules
- Alert rules for missing inputs, salary/odds mismatches, value, and five-round context
- PuLP-based Classic lineup solving with roster, salary-cap, uniqueness, and same-fight constraints
- Separate pure-Python Captain candidate/optimizer modules
- Read-only reasoning and export previews
- Streamlit pages plus a broad pytest/AppTest suite

## Boundaries and limitations

- UFC only; no multi-sport support
- No DraftKings login, contest entry, screen automation, or account integration
- No authenticated, paywalled, CAPTCHA-bypassing, proxy-bypassing, or background scraping
- Public-page fetching is explicit and user-triggered; external HTML changes can break parsers
- Real salary, odds, database, upload, and export files are intentionally excluded
- Importers and experimental Captain handling should be validated against current official files before operational use
- Projections are heuristics and do not promise accuracy, winnings, or profitability
- Internal exports are research artifacts, not guaranteed DraftKings-upload-compatible files

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Run the app:

```bash
streamlit run app/streamlit_app.py
```

Run the test suite:

```bash
pytest
```

## Local data

The default SQLite database is `data/database/dfs_lineup_optimizer.sqlite3`. Uploaded salary/odds files, source manifests, generated data, databases, logs, and exports are ignored by Git. Only synthetic fixtures and empty directory placeholders are tracked.

## Project structure

- `app/` — Streamlit UI and workflow pages
- `src/ingestion/` — salary and odds parsers, matching, and persistence services
- `src/projections/` — projection and calibration logic
- `src/optimizer/` — Classic lineup constraints and solver
- `src/captain/` — separate Captain research/optimizer logic
- `src/db/` — schema, migrations, and repositories
- `src/slate/`, `src/alerts/`, `src/exports/` — workflow services
- `tests/` — unit and Streamlit AppTest coverage
- `docs/` — retained technical design material

## Responsible use

This project is for software demonstration and personal research. Confirm contest rules, data licenses, and local law, and set personal limits before using any DFS tool.
