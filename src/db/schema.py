"""SQLite schema for DK Lineup Lab MVP.

v0: schema is defined here but NOT yet wired into full app persistence.
Call `apply_schema(conn)` from migrations or a future bootstrap step.
"""

from __future__ import annotations

import sqlite3

SCHEMA_STATEMENTS: list[str] = [
    # A DK slate = one contest set on one date.
    # ``manual_review_status`` / ``manual_review_completed_at`` are the
    # Manual Review Gate v1 per-slate readiness columns
    # (MANUAL_REVIEW_GATE_V1_DESIGN §9.2 Option B). Status is closed at
    # {'not_reviewed', 'reviewed'} in v1; the timestamp is set when the
    # user clicks Mark Slate Manually Reviewed (§6) and is NULL until
    # then.
    """
    CREATE TABLE IF NOT EXISTS slates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sport TEXT NOT NULL DEFAULT 'UFC',
        contest_type TEXT NOT NULL DEFAULT 'CLASSIC',
        event_name TEXT NOT NULL,
        event_date TEXT,
        dk_draft_group_id TEXT,
        salary_csv_status TEXT NOT NULL DEFAULT 'unvalidated',
        salary_row_count INTEGER NOT NULL DEFAULT 0,
        manual_review_status TEXT NOT NULL DEFAULT 'not_reviewed',
        manual_review_completed_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # Fighters as they appear on a given slate (salary, position, dk_id).
    # `status` is importer-owned (SALARY_PERSISTENCE_DESIGN §5: 'active' on
    # import, 'inactive' when absent from a re-import). `game_info` is the
    # importer-owned verbatim DK "Game Info" string (DK_GAME_INFO_PAIRING_DESIGN
    # §2.1); NULL means "not captured" (pre-feature row or blank cell). Both
    # rows of a bout carry the byte-identical string, so grouping the roster by
    # exact `game_info` reconstructs pairings without parsing the `@`-aliases.
    # `manual_status` / `manual_status_set_at` are the user-owned Fighter
    # Status v1 override surface (FIGHTER_STATUS_V1_DESIGN §13.2, Option B). The
    # importer and override columns are deliberately decoupled so the importer
    # never silently clobbers a user override; the resolver in
    # src/slate/fighter_status.py picks the effective value.
    """
    CREATE TABLE IF NOT EXISTS fighters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
        dk_player_id TEXT,
        name TEXT NOT NULL,
        salary INTEGER NOT NULL,
        position TEXT NOT NULL DEFAULT 'F',
        team_abbrev TEXT,
        opponent_abbrev TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        game_info TEXT,
        manual_status TEXT,
        manual_status_set_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(slate_id, name)
    )
    """,
    # Fight groupings — two fighters per fight on a slate
    """
    CREATE TABLE IF NOT EXISTS fights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
        fighter_a_id INTEGER NOT NULL REFERENCES fighters(id) ON DELETE CASCADE,
        fighter_b_id INTEGER NOT NULL REFERENCES fighters(id) ON DELETE CASCADE,
        scheduled_rounds INTEGER NOT NULL DEFAULT 3,
        is_main_event INTEGER NOT NULL DEFAULT 0,
        is_title_fight INTEGER NOT NULL DEFAULT 0,
        UNIQUE(slate_id, fighter_a_id, fighter_b_id)
    )
    """,
    # Manual fight-group skeleton — name-only pairings before fighters
    # are imported from the DK salary CSV. Independent of `fights` so it
    # does not require fighter_id FKs yet.
    """
    CREATE TABLE IF NOT EXISTS fight_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
        fighter_1_name TEXT NOT NULL,
        fighter_2_name TEXT NOT NULL,
        scheduled_rounds INTEGER NOT NULL DEFAULT 3,
        status TEXT NOT NULL DEFAULT 'unconfirmed',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(slate_id, fighter_1_name, fighter_2_name)
    )
    """,
    # Odds snapshots per fighter
    """
    CREATE TABLE IF NOT EXISTS odds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fighter_id INTEGER NOT NULL REFERENCES fighters(id) ON DELETE CASCADE,
        american_odds INTEGER NOT NULL,
        implied_probability REAL,
        no_vig_probability REAL,
        source TEXT,
        captured_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # Projections (default + any manual overrides)
    """
    CREATE TABLE IF NOT EXISTS projections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fighter_id INTEGER NOT NULL REFERENCES fighters(id) ON DELETE CASCADE,
        projection REAL NOT NULL,
        value_gap_bonus REAL NOT NULL DEFAULT 0,
        five_round_bonus REAL NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'default',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # Alerts surfaced for manual review
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
        fighter_id INTEGER REFERENCES fighters(id) ON DELETE CASCADE,
        rule TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'info',
        message TEXT NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # Optimizer runs and generated lineups (skeleton)
    """
    CREATE TABLE IF NOT EXISTS optimizer_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
        config_json TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lineups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES optimizer_runs(id) ON DELETE CASCADE,
        total_salary INTEGER NOT NULL,
        projected_points REAL NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lineup_fighters (
        lineup_id INTEGER NOT NULL REFERENCES lineups(id) ON DELETE CASCADE,
        fighter_id INTEGER NOT NULL REFERENCES fighters(id) ON DELETE CASCADE,
        PRIMARY KEY (lineup_id, fighter_id)
    )
    """,
    # --- Odds persistence (Phase A: schema only) ---------------------------
    # See docs/ODDS_PERSISTENCE_DESIGN.md §5. Raw odds rows (immutable),
    # algorithmic match verdicts, and persistent user overrides. No
    # repository / write-path code is wired up yet.
    #
    # Raw odds rows. One row per fighter-line in a CSV or manual entry.
    # Decoupled from `fighters` so unmatched lines still have a home.
    """
    CREATE TABLE IF NOT EXISTS odds_rows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
        odds_row_key TEXT NOT NULL,
        fighter_name_raw TEXT NOT NULL,
        fighter_name_normalized TEXT NOT NULL,
        opponent_name_raw TEXT,
        american_odds INTEGER NOT NULL,
        implied_probability REAL,
        bookmaker TEXT,
        source TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        imported_at TEXT NOT NULL DEFAULT (datetime('now')),
        import_batch_id TEXT,
        UNIQUE(slate_id, odds_row_key),
        CHECK (american_odds <> 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_odds_rows_slate_normalized_name
        ON odds_rows (slate_id, fighter_name_normalized)
    """,
    # Algorithm verdict per (slate, odds_row). Rebuildable: a re-match
    # deletes + reinserts rows for a slate. `effective_status` is the
    # post-override view that the slate gate reads (§8 of the design doc).
    """
    CREATE TABLE IF NOT EXISTS odds_match_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
        odds_row_id INTEGER NOT NULL REFERENCES odds_rows(id) ON DELETE CASCADE,
        odds_row_key TEXT NOT NULL,
        fighter_id INTEGER REFERENCES fighters(id) ON DELETE SET NULL,
        match_status TEXT NOT NULL,
        match_stage TEXT NOT NULL,
        match_score INTEGER NOT NULL,
        opponent_check TEXT NOT NULL,
        preferred_candidate TEXT,
        candidates_json TEXT,
        notes_json TEXT,
        effective_status TEXT NOT NULL,
        computed_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(slate_id, odds_row_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_odds_match_results_slate_fighter
        ON odds_match_results (slate_id, fighter_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_odds_match_results_slate_effective_status
        ON odds_match_results (slate_id, effective_status)
    """,
    # Persistent user decisions. Soft-replaced via `superseded_at` so the
    # audit trail stays intact; active rows are those with
    # `superseded_at IS NULL` (filtered by partial indexes below).
    """
    CREATE TABLE IF NOT EXISTS manual_match_overrides (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
        odds_row_key TEXT,
        fighter_id INTEGER REFERENCES fighters(id) ON DELETE CASCADE,
        override_type TEXT NOT NULL,
        payload_json TEXT,
        reason TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        superseded_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_manual_match_overrides_active_fighter
        ON manual_match_overrides (slate_id, fighter_id)
        WHERE superseded_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_manual_match_overrides_active_odds_row_key
        ON manual_match_overrides (slate_id, odds_row_key)
        WHERE superseded_at IS NULL
    """,
    # --- Multi-book consensus provenance (ODDS_CONSENSUS_DESIGN §5.4 / §6) --
    # One row per (slate, fighter, book): the raw per-book lines the consensus
    # blend was computed from. Rebuildable — cleared/rewritten on each consensus
    # save (design §5.4). The synthesized source="consensus" row lives in
    # odds_rows (no odds_rows schema change). `book` (not `bookmaker`) is the
    # per-book column label; `source` is the provenance origin token
    # ('bestfightodds' | 'paste'), enforced in the repository (no schema CHECK).
    """
    CREATE TABLE IF NOT EXISTS odds_book_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slate_id INTEGER NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
        fighter_name_raw TEXT NOT NULL,
        fighter_name_normalized TEXT NOT NULL,
        opponent_name_raw TEXT,
        book TEXT NOT NULL,
        american_odds INTEGER NOT NULL,
        source TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        imported_at TEXT NOT NULL DEFAULT (datetime('now')),
        import_batch_id TEXT,
        UNIQUE(slate_id, fighter_name_normalized, book),
        CHECK (american_odds <> 0)
    )
    """,
]


def apply_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for stmt in SCHEMA_STATEMENTS:
        cur.execute(stmt)
    conn.commit()
