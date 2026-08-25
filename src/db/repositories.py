"""Repositories.

v0: only SlateRepository is implemented. Other classes remain stubs.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from src.ingestion.dk_salary_importer import ParsedSalaryRow
from src.ingestion.odds_row_key import compute_odds_row_key
from src.projections.implied_probability import american_to_implied_probability
from src.slate.fighter_status import validate_status as _validate_fighter_status
from src.utils.text_cleaning import normalize_name


@dataclass(frozen=True)
class SlateRecord:
    id: int
    event_name: str
    event_date: str | None
    salary_csv_status: str
    salary_row_count: int
    created_at: str
    # Manual Review Gate v1 (MANUAL_REVIEW_GATE_V1_DESIGN §9.2 Option B).
    # ``manual_review_status`` is the closed v1 value set
    # {'not_reviewed', 'reviewed'}. ``manual_review_completed_at`` is
    # ``None`` until the user clicks Mark Slate Manually Reviewed (§6).
    manual_review_status: str = "not_reviewed"
    manual_review_completed_at: str | None = None


class SlateRepository:
    """Minimal create/list operations for slate records."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        event_name: str,
        event_date: str | None = None,
        salary_csv_status: str = "unvalidated",
        salary_row_count: int = 0,
    ) -> SlateRecord:
        event_name = (event_name or "").strip()
        if not event_name:
            raise ValueError("event_name is required")
        cur = self.conn.execute(
            """
            INSERT INTO slates (event_name, event_date, salary_csv_status, salary_row_count)
            VALUES (?, ?, ?, ?)
            """,
            (event_name, event_date, salary_csv_status, int(salary_row_count)),
        )
        self.conn.commit()
        slate_id = int(cur.lastrowid)
        row = self.conn.execute(
            _SLATE_SELECT_COLUMNS + " FROM slates WHERE id = ?",
            (slate_id,),
        ).fetchone()
        return _row_to_record(row)

    def list_all(self) -> list[SlateRecord]:
        rows = self.conn.execute(
            _SLATE_SELECT_COLUMNS
            + " FROM slates ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def set_manual_review_reviewed(self, slate_id: int) -> SlateRecord:
        """Mark a slate as manually reviewed (Phase B of
        ``docs/MANUAL_REVIEW_GATE_V1_DESIGN.md`` §6 / §8 / §10).

        Flips ``slates.manual_review_status`` to ``'reviewed'`` and sets
        ``slates.manual_review_completed_at`` to ``datetime('now')``.
        The whole write runs inside a single transaction so a missing
        slate raises ``ValueError`` and persists nothing.

        Idempotent per §6: a re-call on an already-reviewed slate is a
        no-op on the status column (the value remains ``'reviewed'``)
        and refreshes ``manual_review_completed_at`` so a "last reviewed
        at" surface stays useful (mirrors
        ``docs/FIGHTER_STATUS_V1_DESIGN.md`` §19.4).

        Per §6 / §14, no other table is touched here: no projection
        recompute, no odds recompute, no override mutation, no fighter
        table edit. ``effective_status`` and ``manual_match_overrides``
        are not consumed or written.
        """
        sid = int(slate_id)
        with self.conn:
            if self.conn.execute(
                "SELECT 1 FROM slates WHERE id = ?", (sid,)
            ).fetchone() is None:
                raise ValueError(f"slate #{sid} does not exist")
            self.conn.execute(
                "UPDATE slates "
                "SET manual_review_status = 'reviewed', "
                "    manual_review_completed_at = datetime('now') "
                "WHERE id = ?",
                (sid,),
            )

        row = self.conn.execute(
            _SLATE_SELECT_COLUMNS + " FROM slates WHERE id = ?",
            (sid,),
        ).fetchone()
        return _row_to_record(row)

    def delete(self, slate_id: int) -> None:
        """Permanently delete a slate and every row that depends on it.

        Local-first cleanup for the two-step builder's "start fresh" controls
        (no Undo). The whole delete runs inside a single transaction; a missing
        slate raises ``ValueError`` and persists nothing.

        Dependent rows are removed by foreign-key ``ON DELETE CASCADE``, which
        every child table in ``src/db/schema.py`` declares on its slate (or
        fighter / optimizer-run) reference: fighters, fights, fight_groups,
        odds, odds_rows, odds_match_results, projections, alerts,
        optimizer_runs, lineups, lineup_fighters, manual_match_overrides, and
        odds_book_lines.
        ``PRAGMA foreign_keys = ON`` is re-asserted here (``get_connection``
        already sets it) so the cascade can never silently orphan child rows.

        Touches only slate-derived DB rows — no file, env, upload, or export is
        affected, and the SQLite file itself is never removed.
        """
        sid = int(slate_id)
        # Outside the transaction so the pragma takes effect (a no-op mid-txn).
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self.conn:
            if (
                self.conn.execute(
                    "SELECT 1 FROM slates WHERE id = ?", (sid,)
                ).fetchone()
                is None
            ):
                raise ValueError(f"slate #{sid} does not exist")
            self.conn.execute("DELETE FROM slates WHERE id = ?", (sid,))

    def delete_all(self) -> int:
        """Delete every slate (and all dependent rows) — the full local reset.

        Same single-transaction cascade contract as :meth:`delete`, applied to
        all slates at once. Returns the number of slate rows removed. Touches
        only slate-derived DB rows; the SQLite file, code, ``.env``, uploads,
        and exports are never affected.
        """
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self.conn:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM slates"
            ).fetchone()[0]
            self.conn.execute("DELETE FROM slates")
        return int(count)


_SLATE_SELECT_COLUMNS = (
    "SELECT id, event_name, event_date, salary_csv_status, "
    "salary_row_count, created_at, manual_review_status, "
    "manual_review_completed_at"
)


def _row_to_record(row) -> SlateRecord:
    return SlateRecord(
        id=int(row[0]),
        event_name=row[1],
        event_date=row[2],
        salary_csv_status=row[3],
        salary_row_count=int(row[4]),
        created_at=row[5],
        manual_review_status=row[6],
        manual_review_completed_at=row[7],
    )


@dataclass(frozen=True)
class FightGroupRecord:
    id: int
    slate_id: int
    fighter_1_name: str
    fighter_2_name: str
    scheduled_rounds: int
    status: str
    created_at: str


class FightGroupRepository:
    """Minimal create/list operations for manual fight-group pairings.

    Skeleton only — does not yet link to imported fighter rows.
    """

    ALLOWED_ROUNDS = (3, 5)
    ALLOWED_STATUSES = ("confirmed", "unconfirmed")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        slate_id: int,
        fighter_1_name: str,
        fighter_2_name: str,
        scheduled_rounds: int = 3,
        status: str = "unconfirmed",
    ) -> FightGroupRecord:
        f1 = (fighter_1_name or "").strip()
        f2 = (fighter_2_name or "").strip()
        if not f1 or not f2:
            raise ValueError("both fighter names are required")
        if f1.lower() == f2.lower():
            raise ValueError("fighter names must differ")
        if scheduled_rounds not in self.ALLOWED_ROUNDS:
            raise ValueError("scheduled_rounds must be 3 or 5")
        existing = self.conn.execute(
            "SELECT 1 FROM fight_groups "
            "WHERE slate_id = ? "
            "AND LOWER(TRIM(fighter_1_name)) = ? "
            "AND LOWER(TRIM(fighter_2_name)) = ?",
            (int(slate_id), f2.lower(), f1.lower()),
        ).fetchone()
        if existing is not None:
            raise ValueError(
                "fight group already exists for this slate (reversed order)"
            )
        cur = self.conn.execute(
            """
            INSERT INTO fight_groups
                (slate_id, fighter_1_name, fighter_2_name, scheduled_rounds, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(slate_id), f1, f2, int(scheduled_rounds), status),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id, slate_id, fighter_1_name, fighter_2_name, "
            "scheduled_rounds, status, created_at "
            "FROM fight_groups WHERE id = ?",
            (int(cur.lastrowid),),
        ).fetchone()
        return _row_to_fight_group(row)

    def update_status(self, fight_group_id: int, status: str) -> FightGroupRecord:
        if status not in self.ALLOWED_STATUSES:
            raise ValueError(
                f"status must be one of {self.ALLOWED_STATUSES}"
            )
        cur = self.conn.execute(
            "UPDATE fight_groups SET status = ? WHERE id = ?",
            (status, int(fight_group_id)),
        )
        if cur.rowcount == 0:
            raise ValueError(f"fight group #{fight_group_id} not found")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id, slate_id, fighter_1_name, fighter_2_name, "
            "scheduled_rounds, status, created_at "
            "FROM fight_groups WHERE id = ?",
            (int(fight_group_id),),
        ).fetchone()
        return _row_to_fight_group(row)

    def update_scheduled_rounds(
        self, fight_group_id: int, scheduled_rounds: int
    ) -> FightGroupRecord:
        """Set one existing group's scheduled rounds (3 or 5).

        Status-preserving counterpart to :meth:`update_status`: it touches only
        ``scheduled_rounds`` and never the bout names or confirmed status. Used
        by the Fight Groups "set 5-round main event" control — the user marks
        the main event / title bout explicitly; rounds are never inferred.
        """
        if scheduled_rounds not in self.ALLOWED_ROUNDS:
            raise ValueError("scheduled_rounds must be 3 or 5")
        cur = self.conn.execute(
            "UPDATE fight_groups SET scheduled_rounds = ? WHERE id = ?",
            (int(scheduled_rounds), int(fight_group_id)),
        )
        if cur.rowcount == 0:
            raise ValueError(f"fight group #{fight_group_id} not found")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id, slate_id, fighter_1_name, fighter_2_name, "
            "scheduled_rounds, status, created_at "
            "FROM fight_groups WHERE id = ?",
            (int(fight_group_id),),
        ).fetchone()
        return _row_to_fight_group(row)

    def confirm_all_for_slate(self, slate_id: int) -> int:
        """Mark every unconfirmed group on the slate confirmed; return the count.

        One status-only ``UPDATE`` in a single transaction (docs/DEVELOPMENT_NOTES.md §11):
        backs the Fight Groups "confirm all groups" action. Never creates a
        group and never touches ``scheduled_rounds``. Idempotent — a re-run when
        all groups are already confirmed updates zero rows and returns 0.
        """
        cur = self.conn.execute(
            "UPDATE fight_groups SET status = 'confirmed' "
            "WHERE slate_id = ? AND status != 'confirmed'",
            (int(slate_id),),
        )
        self.conn.commit()
        return int(cur.rowcount)

    def list_for_slate(self, slate_id: int) -> list[FightGroupRecord]:
        rows = self.conn.execute(
            "SELECT id, slate_id, fighter_1_name, fighter_2_name, "
            "scheduled_rounds, status, created_at "
            "FROM fight_groups WHERE slate_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (int(slate_id),),
        ).fetchall()
        return [_row_to_fight_group(r) for r in rows]


def _row_to_fight_group(row) -> FightGroupRecord:
    return FightGroupRecord(
        id=int(row[0]),
        slate_id=int(row[1]),
        fighter_1_name=row[2],
        fighter_2_name=row[3],
        scheduled_rounds=int(row[4]),
        status=row[5],
        created_at=row[6],
    )


@dataclass(frozen=True)
class FighterRecord:
    id: int
    slate_id: int
    name: str
    salary: int
    status: str
    # Verbatim DK "Game Info" string captured at salary import
    # (DK_GAME_INFO_PAIRING_DESIGN §2.3). ``None`` means "not captured" — a
    # pre-feature row or a blank cell. Additive and optional so existing
    # ``FighterRecord`` consumers (projection_input_service,
    # optimizer/pool_builder, the Fight Groups Region A join) are unaffected.
    game_info: str | None = None


@dataclass(frozen=True)
class FighterManualStatusRecord:
    """Snapshot of a fighter's Fighter Status v1 override columns.

    Returned by ``FighterRepository.set_manual_status`` /
    ``clear_manual_status``. Phase B persistence only — the resolved
    effective status (importer base vs. user override) is left to the
    Phase A resolver / Phase C read aggregator per
    ``docs/FIGHTER_STATUS_V1_DESIGN.md`` §13.2, §15.
    """

    fighter_id: int
    slate_id: int
    manual_status: str | None
    manual_status_set_at: str | None


@dataclass(frozen=True)
class FighterUpsertResult:
    """Summary of one ``upsert_for_slate`` pass.

    ``unchanged`` is the count of incoming rows whose persisted twin already
    matched on salary, position, and active status — useful for asserting
    idempotence in tests. ``deactivated`` is the count of slate fighters that
    were previously active but absent from the new import; per
    ``docs/SALARY_PERSISTENCE_DESIGN.md`` §5 the conservative default is to
    mark them inactive rather than delete.
    """

    inserted: int
    updated: int
    unchanged: int
    deactivated: int


class FighterRepository:
    """Slate-scoped DK fighter persistence.

    Read side: ``list_for_slate`` (Phase C.1 of
    ``docs/ODDS_PERSISTENCE_DESIGN.md`` §14.12). Write side:
    ``upsert_for_slate`` (Phase B of ``docs/SALARY_PERSISTENCE_DESIGN.md``
    §9) — composes parsed DK salary rows into idempotent INSERT/UPDATE
    statements against the ``fighters`` table. Service-layer composition
    and UI wiring live in later phases.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list_for_slate(self, slate_id: int) -> list[FighterRecord]:
        rows = self.conn.execute(
            "SELECT id, slate_id, name, salary, status, game_info "
            "FROM fighters WHERE slate_id = ? "
            "ORDER BY name COLLATE NOCASE ASC, id ASC",
            (int(slate_id),),
        ).fetchall()
        return [_row_to_fighter(r) for r in rows]

    def upsert_for_slate(
        self,
        *,
        slate_id: int,
        parsed_rows: list[ParsedSalaryRow],
    ) -> FighterUpsertResult:
        """Persist parsed DK salary rows as slate-scoped fighters.

        Realizes ``docs/SALARY_PERSISTENCE_DESIGN.md`` §5 and the B2
        persistence half of ``docs/DK_GAME_INFO_PAIRING_DESIGN.md`` §2.4:

          - New rows are INSERTed with ``status='active'`` and the row's
            captured ``game_info`` string (``None`` for a blank cell).
          - Existing rows (matched by ``(slate_id, name)``) are UPDATEd in
            place when ``salary``, ``position``, ``status``, or
            ``game_info`` differs; the existing fighter id is preserved so
            any ``manual_match_overrides`` referencing it are not orphaned.
            A row whose ``game_info`` was ``NULL`` and now has a value is
            counted ``updated`` — the intended one-click backfill (§2.4).
          - Slate fighters absent from this import flip from ``'active'``
            to ``'inactive'`` (conservative default — no hard delete).
          - Re-importing an unchanged set (same ``game_info`` included) is
            a no-op: zero inserts, updates, and deactivations.

        The whole pass runs inside one transaction (``with self.conn:``)
        so a partial failure leaves the prior persisted state intact.

        Out of scope for this slice (see ``docs/DEVELOPMENT_NOTES.md`` §10 and the
        design doc §8): no recompute of ``odds_match_results``, no
        rewrite of ``manual_match_overrides``. ``game_info`` is persisted
        here but no ``fight_groups`` rows are inferred from it — group
        creation is the Fight Groups page's explicit Apply (design §4, §8).
        """
        sid = int(slate_id)

        rows = list(parsed_rows)
        if not rows:
            raise ValueError(
                "parsed_rows must not be empty; refusing to silently "
                "deactivate every fighter on the slate"
            )
        for r in rows:
            if not isinstance(r, ParsedSalaryRow):
                raise ValueError(
                    "parsed_rows must contain ParsedSalaryRow instances; "
                    f"got {type(r).__name__}"
                )

        # Parser already rejects duplicates, but defend the repo layer too
        # so a hand-built list cannot create two active rows for the same
        # (slate, name).
        seen_names: dict[str, int] = {}
        for r in rows:
            if r.fighter_name in seen_names:
                raise ValueError(
                    f"duplicate fighter name in parsed_rows: "
                    f"{r.fighter_name!r} (rows "
                    f"{seen_names[r.fighter_name]} and {r.source_row_number})"
                )
            seen_names[r.fighter_name] = r.source_row_number

        if self.conn.execute(
            "SELECT 1 FROM slates WHERE id = ?", (sid,)
        ).fetchone() is None:
            raise ValueError(f"slate #{sid} does not exist")

        inserted = 0
        updated = 0
        unchanged = 0
        deactivated = 0

        with self.conn:
            existing_rows = self.conn.execute(
                "SELECT name, salary, position, status, game_info "
                "FROM fighters WHERE slate_id = ?",
                (sid,),
            ).fetchall()
            existing_by_name = {
                row[0]: (int(row[1]), row[2], row[3], row[4])
                for row in existing_rows
            }

            incoming_names: set[str] = set()
            for r in rows:
                name = r.fighter_name
                incoming_names.add(name)
                pos = r.roster_position or "F"
                sal = int(r.salary)
                game_info = r.game_info

                if name in existing_by_name:
                    cur_salary, cur_position, cur_status, cur_game_info = (
                        existing_by_name[name]
                    )
                    # game_info is part of change detection so a re-import
                    # backfills a NULL row to its captured value (counted as
                    # ``updated``, not ``unchanged`` — design §2.4).
                    needs_update = (
                        cur_salary != sal
                        or cur_position != pos
                        or cur_status != "active"
                        or cur_game_info != game_info
                    )
                    if needs_update:
                        self.conn.execute(
                            "UPDATE fighters "
                            "SET salary = ?, position = ?, status = 'active', "
                            "    game_info = ? "
                            "WHERE slate_id = ? AND name = ?",
                            (sal, pos, game_info, sid, name),
                        )
                        updated += 1
                    else:
                        unchanged += 1
                else:
                    self.conn.execute(
                        "INSERT INTO fighters "
                        "(slate_id, name, salary, position, status, game_info) "
                        "VALUES (?, ?, ?, ?, 'active', ?)",
                        (sid, name, sal, pos, game_info),
                    )
                    inserted += 1

            for name, (_sal, _pos, status, _gi) in existing_by_name.items():
                if name not in incoming_names and status == "active":
                    self.conn.execute(
                        "UPDATE fighters SET status = 'inactive' "
                        "WHERE slate_id = ? AND name = ?",
                        (sid, name),
                    )
                    deactivated += 1

        return FighterUpsertResult(
            inserted=inserted,
            updated=updated,
            unchanged=unchanged,
            deactivated=deactivated,
        )

    def set_manual_status(
        self,
        *,
        slate_id: int,
        fighter_id: int,
        status: str,
    ) -> FighterManualStatusRecord:
        """Persist a manual Fighter Status v1 override for one fighter row.

        Phase B of ``docs/FIGHTER_STATUS_V1_DESIGN.md`` (§13.2 Option B,
        §15 Phase B). The value is validated against
        ``ALLOWED_STATUSES`` via the Phase A taxonomy module; unknown
        values raise ``ValueError`` and no row is touched.

        The importer-owned ``fighters.status`` column and any
        ``odds_match_results`` / ``manual_match_overrides`` rows are NOT
        touched: per §8 Fighter Status is strictly disjoint from the
        odds-match override layer. Re-import safety (§13.2, §19.5) holds
        because ``upsert_for_slate`` never writes ``manual_status``.

        Idempotence (§19.4): re-submitting the same value is a no-op on
        the value column and refreshes ``manual_status_set_at`` so a
        "last touched" surface remains useful. The whole write runs
        inside one transaction so a failed validation cannot half-commit.
        """
        validated = _validate_fighter_status(status)
        sid = int(slate_id)
        fid = int(fighter_id)

        with self.conn:
            self._require_fighter_on_slate(fighter_id=fid, slate_id=sid)
            self.conn.execute(
                "UPDATE fighters "
                "SET manual_status = ?, "
                "    manual_status_set_at = datetime('now') "
                "WHERE id = ?",
                (validated, fid),
            )

        return self._fetch_manual_status(fid)

    def clear_manual_status(
        self,
        *,
        slate_id: int,
        fighter_id: int,
    ) -> FighterManualStatusRecord:
        """Clear any manual Fighter Status override on one fighter row.

        Resets ``manual_status`` and ``manual_status_set_at`` to ``NULL``
        per ``docs/FIGHTER_STATUS_V1_DESIGN.md`` §13.2: clearing returns
        the effective status to the importer-owned base. Idempotent —
        clearing a row with no override is a no-op on persisted state.
        """
        sid = int(slate_id)
        fid = int(fighter_id)

        with self.conn:
            self._require_fighter_on_slate(fighter_id=fid, slate_id=sid)
            self.conn.execute(
                "UPDATE fighters "
                "SET manual_status = NULL, manual_status_set_at = NULL "
                "WHERE id = ?",
                (fid,),
            )

        return self._fetch_manual_status(fid)

    def _require_fighter_on_slate(
        self, *, fighter_id: int, slate_id: int
    ) -> None:
        row = self.conn.execute(
            "SELECT slate_id FROM fighters WHERE id = ?",
            (int(fighter_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"fighter #{fighter_id} does not exist")
        if int(row[0]) != int(slate_id):
            raise ValueError(
                f"fighter #{fighter_id} belongs to slate "
                f"#{int(row[0])}, not slate #{int(slate_id)}"
            )

    def _fetch_manual_status(
        self, fighter_id: int
    ) -> FighterManualStatusRecord:
        row = self.conn.execute(
            "SELECT id, slate_id, manual_status, manual_status_set_at "
            "FROM fighters WHERE id = ?",
            (int(fighter_id),),
        ).fetchone()
        return FighterManualStatusRecord(
            fighter_id=int(row[0]),
            slate_id=int(row[1]),
            manual_status=row[2],
            manual_status_set_at=row[3],
        )


def _row_to_fighter(row) -> FighterRecord:
    return FighterRecord(
        id=int(row[0]),
        slate_id=int(row[1]),
        name=row[2],
        salary=int(row[3]),
        status=row[4],
        game_info=row[5],
    )


@dataclass(frozen=True)
class OddsRowRecord:
    id: int
    slate_id: int
    odds_row_key: str
    fighter_name_raw: str
    fighter_name_normalized: str
    opponent_name_raw: str | None
    american_odds: int
    implied_probability: float | None
    bookmaker: str | None
    source: str
    captured_at: str
    imported_at: str
    import_batch_id: str | None


class OddsRowRepository:
    """Raw immutable odds rows (Phase B of docs/ODDS_PERSISTENCE_DESIGN.md).

    Writes into ``odds_rows`` only. Match results and overrides live in
    later phases. Validation rules follow design §10 (hard validation):
    non-empty fighter name, non-zero American odds, parseable ISO-8601
    timestamp, non-empty source, implied probability in (0, 1).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        slate_id: int,
        fighter_name_raw: str,
        american_odds: int,
        source: str,
        captured_at: str,
        bookmaker: str | None = None,
        opponent_name_raw: str | None = None,
        import_batch_id: str | None = None,
        odds_row_key: str | None = None,
        implied_probability: float | None = None,
    ) -> OddsRowRecord:
        """Insert one ``odds_rows`` row (per-row commit) and return it.

        ``implied_probability`` defaults to ``None`` → derived from
        ``american_odds`` (the historical behavior). When supplied — the
        consensus path (ODDS_CONSENSUS_DESIGN §6) — the exact value is stored
        verbatim instead of the round-tripped implied of the rounded fair line.
        Either way it is validated to lie in (0, 1).
        """
        params = _prepare_odds_row_params(
            slate_id=slate_id,
            fighter_name_raw=fighter_name_raw,
            american_odds=american_odds,
            source=source,
            captured_at=captured_at,
            bookmaker=bookmaker,
            opponent_name_raw=opponent_name_raw,
            import_batch_id=import_batch_id,
            odds_row_key=odds_row_key,
            implied_probability=implied_probability,
        )
        cur = self.conn.execute(_INSERT_ODDS_ROW_SQL, params)
        self.conn.commit()
        return self._fetch_by_id(int(cur.lastrowid))

    def create_or_get(self, **kwargs) -> OddsRowRecord:
        """Idempotent insert for CSV re-imports (design §11).

        Returns the existing record if a row with the same
        ``(slate_id, odds_row_key)`` already exists. Raw odds rows are
        immutable (design §5.1), so no fields are ever updated through this
        path.
        """
        slate_id = int(kwargs.get("slate_id"))
        key = kwargs.get("odds_row_key")
        if not key:
            key = compute_odds_row_key(
                fighter_name=kwargs.get("fighter_name_raw", "") or "",
                bookmaker=kwargs.get("bookmaker"),
                source=kwargs.get("source", "") or "",
                captured_at=kwargs.get("captured_at", "") or "",
            )
        existing = self.get_by_key(slate_id=slate_id, odds_row_key=key)
        if existing is not None:
            return existing
        kwargs["odds_row_key"] = key
        return self.create(**kwargs)

    def get_by_id(self, odds_row_id: int) -> OddsRowRecord | None:
        row = self.conn.execute(
            _SELECT_COLUMNS + " FROM odds_rows WHERE id = ?",
            (int(odds_row_id),),
        ).fetchone()
        return _row_to_odds_row(row) if row is not None else None

    def get_by_key(
        self, *, slate_id: int, odds_row_key: str
    ) -> OddsRowRecord | None:
        row = self.conn.execute(
            _SELECT_COLUMNS
            + " FROM odds_rows WHERE slate_id = ? AND odds_row_key = ?",
            (int(slate_id), odds_row_key),
        ).fetchone()
        return _row_to_odds_row(row) if row is not None else None

    def list_for_slate(self, slate_id: int) -> list[OddsRowRecord]:
        rows = self.conn.execute(
            _SELECT_COLUMNS
            + " FROM odds_rows WHERE slate_id = ? "
            + "ORDER BY imported_at ASC, id ASC",
            (int(slate_id),),
        ).fetchall()
        return [_row_to_odds_row(r) for r in rows]

    def list_for_slate_source(
        self, slate_id: int, source: str
    ) -> list[OddsRowRecord]:
        rows = self.conn.execute(
            _SELECT_COLUMNS
            + " FROM odds_rows WHERE slate_id = ? AND source = ? "
            + "ORDER BY imported_at ASC, id ASC",
            (int(slate_id), (source or "").strip()),
        ).fetchall()
        return [_row_to_odds_row(r) for r in rows]

    def delete_for_slate_source(self, slate_id: int, source: str) -> int:
        """Delete every ``odds_rows`` row for ``(slate, source)``; return count.

        The one scoped delete path on the otherwise append-only ``odds_rows``
        (ODDS_CONSENSUS_DESIGN §7), giving the synthesized ``source="consensus"``
        rows their "last save wins" semantics. Cascades to ``odds_match_results``
        via ``ON DELETE CASCADE``. Owns its transaction.
        """
        src = (source or "").strip()
        if not src:
            raise ValueError("source is required")
        # Outside the transaction so the pragma takes effect (a no-op mid-txn);
        # mirrors SlateRepository.delete so the documented cascade to
        # odds_match_results can never silently orphan rows.
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM odds_rows WHERE slate_id = ? AND source = ?",
                (int(slate_id), src),
            )
        return cur.rowcount

    def replace_for_slate_source(
        self, slate_id: int, source: str, rows
    ) -> list[OddsRowRecord]:
        """Atomically replace all ``(slate, source)`` rows (delete + re-insert).

        One transaction: DELETE the slate's rows for ``source`` then INSERT each
        of ``rows`` (a kwargs mapping for :func:`_prepare_odds_row_params`, minus
        ``source`` which is bound here). Either the whole new set lands or the
        prior state survives intact — unlike the per-row :meth:`create` path.
        The chained recompute runs separately afterward (ODDS_CONSENSUS_DESIGN
        §7). Returns the persisted rows in insert order.
        """
        slate_id = int(slate_id)
        src = (source or "").strip()
        if not src:
            raise ValueError("source is required")
        params = [
            _prepare_odds_row_params(slate_id=slate_id, source=src, **row)
            for row in rows
        ]
        # Outside the transaction so the pragma takes effect (a no-op mid-txn);
        # the DELETE's cascade to odds_match_results relies on it (design §7).
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self.conn:
            self.conn.execute(
                "DELETE FROM odds_rows WHERE slate_id = ? AND source = ?",
                (slate_id, src),
            )
            if params:
                self.conn.executemany(_INSERT_ODDS_ROW_SQL, params)
        return self.list_for_slate_source(slate_id, src)

    def _fetch_by_id(self, odds_row_id: int) -> OddsRowRecord:
        row = self.conn.execute(
            _SELECT_COLUMNS + " FROM odds_rows WHERE id = ?",
            (odds_row_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"odds_rows row #{odds_row_id} not found immediately after insert"
            )
        return _row_to_odds_row(row)


_SELECT_COLUMNS = (
    "SELECT id, slate_id, odds_row_key, fighter_name_raw, "
    "fighter_name_normalized, opponent_name_raw, american_odds, "
    "implied_probability, bookmaker, source, captured_at, imported_at, "
    "import_batch_id"
)


def _row_to_odds_row(row) -> OddsRowRecord:
    return OddsRowRecord(
        id=int(row[0]),
        slate_id=int(row[1]),
        odds_row_key=row[2],
        fighter_name_raw=row[3],
        fighter_name_normalized=row[4],
        opponent_name_raw=row[5],
        american_odds=int(row[6]),
        implied_probability=(float(row[7]) if row[7] is not None else None),
        bookmaker=row[8],
        source=row[9],
        captured_at=row[10],
        imported_at=row[11],
        import_batch_id=row[12],
    )


def _require_iso8601(value: str) -> None:
    # Trailing 'Z' is valid ISO-8601; accept both that and explicit offsets.
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(
            f"captured_at must be ISO-8601 parseable; got {value!r}"
        ) from exc


_INSERT_ODDS_ROW_SQL = """
    INSERT INTO odds_rows (
        slate_id, odds_row_key, fighter_name_raw, fighter_name_normalized,
        opponent_name_raw, american_odds, implied_probability, bookmaker,
        source, captured_at, import_batch_id
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _prepare_odds_row_params(
    *,
    slate_id: int,
    fighter_name_raw: str,
    american_odds: int,
    source: str,
    captured_at: str,
    bookmaker: str | None = None,
    opponent_name_raw: str | None = None,
    import_batch_id: str | None = None,
    odds_row_key: str | None = None,
    implied_probability: float | None = None,
) -> tuple:
    """Validate + normalize one ``odds_rows`` insert into its bound-param tuple.

    Shared by :meth:`OddsRowRepository.create` (per-row commit) and
    :meth:`OddsRowRepository.replace_for_slate_source` (atomic batch). Validation
    follows design §10: non-empty fighter name (and non-empty normalization),
    non-zero American odds, non-empty source, ISO-8601 ``captured_at``, implied
    probability in (0, 1). ``implied_probability`` is derived from the American
    line when ``None`` and stored verbatim when supplied (ODDS_CONSENSUS_DESIGN
    §6); both are range-checked.
    """
    slate_id = int(slate_id)

    name = (fighter_name_raw or "").strip()
    if not name:
        raise ValueError("fighter_name_raw is required")

    try:
        ml = int(american_odds)
    except (TypeError, ValueError) as exc:
        raise ValueError("american_odds must be an integer") from exc
    if ml == 0:
        raise ValueError("american_odds must be non-zero")

    src = (source or "").strip()
    if not src:
        raise ValueError("source is required")

    captured = (captured_at or "").strip()
    if not captured:
        raise ValueError("captured_at is required")
    _require_iso8601(captured)

    normalized = normalize_name(name)
    if not normalized:
        raise ValueError("fighter_name_raw normalizes to empty string")

    if implied_probability is None:
        implied = american_to_implied_probability(ml)
        if not (0.0 < implied < 1.0):
            raise ValueError(
                "implied_probability must be in (0, 1); "
                f"got {implied!r} from american_odds={ml!r}"
            )
    else:
        implied = float(implied_probability)
        if not (0.0 < implied < 1.0):
            raise ValueError(
                f"implied_probability must be in (0, 1); got {implied!r}"
            )

    key = odds_row_key or compute_odds_row_key(
        fighter_name=name,
        bookmaker=bookmaker,
        source=src,
        captured_at=captured,
    )

    bk = bookmaker.strip() if isinstance(bookmaker, str) else bookmaker
    if bk == "":
        bk = None
    opp = (
        opponent_name_raw.strip()
        if isinstance(opponent_name_raw, str)
        else opponent_name_raw
    )
    if opp == "":
        opp = None
    batch = (
        import_batch_id.strip()
        if isinstance(import_batch_id, str)
        else import_batch_id
    )
    if batch == "":
        batch = None

    return (slate_id, key, name, normalized, opp, ml, implied, bk, src, captured, batch)


@dataclass(frozen=True)
class OddsBookLineRecord:
    """One book's American line for one fighter — consensus provenance (Slice 5).

    Persisted in ``odds_book_lines`` (ODDS_CONSENSUS_DESIGN §5.4 / §6): the raw
    per-book lines the ``source="consensus"`` ``odds_rows`` row was blended from.
    ``book`` is the per-book column label; ``source`` is the provenance origin
    (``'bestfightodds'`` | ``'paste'``).
    """

    id: int
    slate_id: int
    fighter_name_raw: str
    fighter_name_normalized: str
    opponent_name_raw: str | None
    book: str
    american_odds: int
    source: str
    captured_at: str
    imported_at: str
    import_batch_id: str | None


_BOOK_LINE_SELECT_COLUMNS = (
    "SELECT id, slate_id, fighter_name_raw, fighter_name_normalized, "
    "opponent_name_raw, book, american_odds, source, captured_at, "
    "imported_at, import_batch_id"
)


def _row_to_book_line(row) -> OddsBookLineRecord:
    return OddsBookLineRecord(
        id=int(row[0]),
        slate_id=int(row[1]),
        fighter_name_raw=row[2],
        fighter_name_normalized=row[3],
        opponent_name_raw=row[4],
        book=row[5],
        american_odds=int(row[6]),
        source=row[7],
        captured_at=row[8],
        imported_at=row[9],
        import_batch_id=row[10],
    )


class OddsBookLineRepository:
    """Per-book provenance for the multi-book consensus blend (Slice 5).

    Realizes ODDS_CONSENSUS_DESIGN §5.4 / §6: one row per (slate, fighter, book)
    — the raw lines the consensus was computed from. ``replace_for_slate`` is the
    only write path: like :class:`OddsMatchResultRepository` it DELETEs the
    slate's rows and re-INSERTs the supplied set in a single transaction (the
    provenance is "cleared/rewritten on each consensus save"). ``source`` is
    constrained to the closed provenance-origin set as a code allow-list (no
    schema CHECK), matching the odds-persistence convention.
    """

    ALLOWED_SOURCES = frozenset({"bestfightodds", "paste"})

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def replace_for_slate(self, slate_id: int, rows) -> list[OddsBookLineRecord]:
        slate_id = int(slate_id)
        payload = [self._to_params(slate_id, row) for row in rows]
        with self.conn:
            self.conn.execute(
                "DELETE FROM odds_book_lines WHERE slate_id = ?",
                (slate_id,),
            )
            if payload:
                self.conn.executemany(
                    """
                    INSERT INTO odds_book_lines (
                        slate_id, fighter_name_raw, fighter_name_normalized,
                        opponent_name_raw, book, american_odds, source,
                        captured_at, import_batch_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
        return self.list_for_slate(slate_id)

    def list_for_slate(self, slate_id: int) -> list[OddsBookLineRecord]:
        rows = self.conn.execute(
            _BOOK_LINE_SELECT_COLUMNS
            + " FROM odds_book_lines WHERE slate_id = ? ORDER BY id ASC",
            (int(slate_id),),
        ).fetchall()
        return [_row_to_book_line(r) for r in rows]

    def _to_params(self, slate_id: int, row) -> tuple:
        """Validate one provenance row (a kwargs mapping) into its bound tuple."""
        raw = (row.get("fighter_name_raw") or "").strip()
        if not raw:
            raise ValueError("fighter_name_raw is required")
        normalized = normalize_name(raw)
        if not normalized:
            raise ValueError("fighter_name_raw normalizes to empty string")
        book = (row.get("book") or "").strip()
        if not book:
            raise ValueError("book is required")
        try:
            ml = int(row["american_odds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("american_odds must be an integer") from exc
        if ml == 0:
            raise ValueError("american_odds must be non-zero")
        source = (row.get("source") or "").strip()
        if source not in self.ALLOWED_SOURCES:
            raise ValueError(
                f"source must be one of {sorted(self.ALLOWED_SOURCES)}; "
                f"got {source!r}"
            )
        captured = (row.get("captured_at") or "").strip()
        if not captured:
            raise ValueError("captured_at is required")
        _require_iso8601(captured)
        opp = row.get("opponent_name_raw")
        opp = opp.strip() if isinstance(opp, str) else opp
        if opp == "":
            opp = None
        batch = row.get("import_batch_id")
        batch = batch.strip() if isinstance(batch, str) else batch
        if batch == "":
            batch = None
        return (slate_id, raw, normalized, opp, book, ml, source, captured, batch)


class OddsMatchResultRepository:
    """Persistence for odds match results (Phase C.3 of
    ``docs/ODDS_PERSISTENCE_DESIGN.md``).

    ``replace_for_slate`` is the only write path: it DELETEs every row for
    the slate and re-INSERTs the supplied records in a single transaction.
    Either every row from the new run is committed or none are; the prior
    persisted state survives a failure intact.

    ``compute_match_results`` (the pure service in
    ``src/ingestion/odds_matching_service.py``) is intentionally NOT called
    from here — the persistence pass and the matcher service are kept apart
    so the repository can be tested without seeding a full slate.
    """

    ALLOWED_MATCH_STATUSES = frozenset({
        "auto_match", "review_required", "unmatched",
    })
    # ``review_rejected`` (D.4), ``review_accepted`` / ``force_pair`` (D.5.1)
    # are reachable only as resolver outputs. They must never appear in
    # ``match_status``; the disjoint sets encode that invariant (§16.13 —
    # code allow-list only, no schema CHECK).
    ALLOWED_EFFECTIVE_STATUSES = ALLOWED_MATCH_STATUSES | frozenset({
        "review_rejected",
        "review_accepted",
        "force_pair",
    })
    ALLOWED_STAGES = frozenset({
        "exact_conservative", "exact_aggressive", "fuzzy", "none",
    })
    ALLOWED_OPPONENT_CHECKS = frozenset({
        "passed", "failed", "unknown", "not_applicable",
    })

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def replace_for_slate(self, slate_id: int, results) -> None:
        with self.conn:
            self._replace_for_slate_unlocked(slate_id, results)

    def _replace_for_slate_unlocked(self, slate_id: int, results) -> None:
        """Worker for ``replace_for_slate``.

        Runs the validation + DELETE + re-INSERT sequence without managing
        the transaction so Phase D.4.3 composers can invoke it from inside
        an externally-provided ``with self.conn:`` block alongside other
        writes.
        """
        slate_id = int(slate_id)
        records = list(results)
        for r in records:
            if int(r.slate_id) != slate_id:
                raise ValueError(
                    f"result for odds_row_id={r.odds_row_id} has "
                    f"slate_id={r.slate_id}, expected {slate_id}"
                )
            self._validate(r)

        payload = [self._to_row(slate_id, r) for r in records]

        self.conn.execute(
            "DELETE FROM odds_match_results WHERE slate_id = ?",
            (slate_id,),
        )
        if payload:
            self.conn.executemany(
                """
                INSERT INTO odds_match_results (
                    slate_id, odds_row_id, odds_row_key, fighter_id,
                    match_status, match_stage, match_score, opponent_check,
                    preferred_candidate, candidates_json, notes_json,
                    effective_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )

    def list_for_slate(self, slate_id: int) -> list:
        # Local import: the service module imports record types from this
        # module, so a top-level import here would create a cycle.
        from src.ingestion.odds_matching_service import OddsMatchResultRecord

        rows = self.conn.execute(
            """
            SELECT slate_id, odds_row_id, odds_row_key, fighter_id,
                   match_status, match_stage, match_score, opponent_check,
                   preferred_candidate, candidates_json, notes_json,
                   effective_status
            FROM odds_match_results
            WHERE slate_id = ?
            ORDER BY odds_row_id ASC
            """,
            (int(slate_id),),
        ).fetchall()
        return [
            OddsMatchResultRecord(
                slate_id=int(r[0]),
                odds_row_id=int(r[1]),
                odds_row_key=r[2],
                fighter_id=(int(r[3]) if r[3] is not None else None),
                match_status=r[4],
                match_stage=r[5],
                match_score=int(r[6]),
                opponent_check=r[7],
                preferred_candidate=r[8],
                candidates=_loads_json_tuple(r[9]),
                notes=_loads_json_tuple(r[10]),
                effective_status=r[11],
            )
            for r in rows
        ]

    def _validate(self, r) -> None:
        if r.match_status not in self.ALLOWED_MATCH_STATUSES:
            raise ValueError(
                "match_status must be one of "
                f"{sorted(self.ALLOWED_MATCH_STATUSES)}; got {r.match_status!r}"
            )
        if r.match_stage not in self.ALLOWED_STAGES:
            raise ValueError(
                f"match_stage must be one of {sorted(self.ALLOWED_STAGES)}; "
                f"got {r.match_stage!r}"
            )
        try:
            score = int(r.match_score)
        except (TypeError, ValueError) as exc:
            raise ValueError("match_score must be an integer") from exc
        if not (0 <= score <= 100):
            raise ValueError(
                f"match_score must be in [0, 100]; got {r.match_score!r}"
            )
        if r.opponent_check not in self.ALLOWED_OPPONENT_CHECKS:
            raise ValueError(
                "opponent_check must be one of "
                f"{sorted(self.ALLOWED_OPPONENT_CHECKS)}; got {r.opponent_check!r}"
            )
        if r.effective_status not in self.ALLOWED_EFFECTIVE_STATUSES:
            raise ValueError(
                "effective_status must be one of "
                f"{sorted(self.ALLOWED_EFFECTIVE_STATUSES)}; "
                f"got {r.effective_status!r}"
            )

    def _to_row(self, slate_id: int, r) -> tuple:
        return (
            slate_id,
            int(r.odds_row_id),
            r.odds_row_key,
            (int(r.fighter_id) if r.fighter_id is not None else None),
            r.match_status,
            r.match_stage,
            int(r.match_score),
            r.opponent_check,
            r.preferred_candidate,
            _dumps_json_tuple(r.candidates),
            _dumps_json_tuple(r.notes),
            r.effective_status,
        )


def _dumps_json_tuple(value) -> str | None:
    """Empty → NULL, non-empty → JSON array (design §14.9)."""
    if not value:
        return None
    return json.dumps(list(value))


def _loads_json_tuple(value) -> tuple:
    if value is None:
        return ()
    return tuple(json.loads(value))


@dataclass(frozen=True)
class ManualMatchOverrideRecord:
    """Phase D.0 read-side projection of a ``manual_match_overrides`` row.

    ``payload_json`` is surfaced as the raw stored TEXT (or ``None``). Parsing
    and shape validation belong to the eventual write-side / projection
    service, not this scaffold.
    """

    id: int
    slate_id: int
    odds_row_key: str | None
    fighter_id: int | None
    override_type: str
    payload_json: str | None
    reason: str | None
    created_at: str
    superseded_at: str | None


class ManualMatchOverrideRepository:
    """Manual match overrides repository.

    Read side: ``list_active_for_slate`` (Phase D.0). Write side:
    ``add_override`` for the resolution set ``reject_match`` (Phase D.1),
    ``accept_match`` / ``force_pair`` (Phase D.5.1). The remaining types
    (``mark_excluded``, ``manual_moneyline``,
    ``manual_projection_low_confidence``) are intentionally not
    implemented yet. "Active" means ``superseded_at IS NULL`` per
    ``docs/ODDS_PERSISTENCE_DESIGN.md`` §5.3.
    """

    # The mutually-exclusive resolution set (§16.4): inserting any one of
    # these for a ``(slate_id, odds_row_key)`` supersedes every active row
    # of the set on that key, regardless of type. Fighter-scoped types
    # (``mark_excluded`` / ``manual_moneyline``) are NOT in this set.
    RESOLUTION_OVERRIDE_TYPES = ("reject_match", "accept_match", "force_pair")
    # Binding types require a NOT-NULL active fighter and rebind the row to
    # it (§16.3); ``reject_match`` does neither.
    BINDING_OVERRIDE_TYPES = ("accept_match", "force_pair")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list_active_for_slate(
        self, slate_id: int
    ) -> list[ManualMatchOverrideRecord]:
        rows = self.conn.execute(
            "SELECT id, slate_id, odds_row_key, fighter_id, override_type, "
            "payload_json, reason, created_at, superseded_at "
            "FROM manual_match_overrides "
            "WHERE slate_id = ? AND superseded_at IS NULL "
            "ORDER BY created_at ASC, id ASC",
            (int(slate_id),),
        ).fetchall()
        return [_row_to_manual_match_override(r) for r in rows]

    def add_override(
        self,
        *,
        slate_id: int,
        override_type: str,
        odds_row_key: str | None = None,
        fighter_id: int | None = None,
        payload: dict | None = None,
        reason: str | None = None,
    ) -> ManualMatchOverrideRecord:
        """Insert a new override, soft-superseding any active row in its
        scope.

        Phase D.1 / D.5.1 of ``docs/ODDS_PERSISTENCE_DESIGN.md`` §12 / §16.
        Supports the resolution set ``reject_match`` / ``accept_match`` /
        ``force_pair``. Supersession scope is the whole resolution set on
        the ``(slate_id, odds_row_key)`` (§16.4): inserting any one
        supersedes the active rows of the other two on that key —
        fighter-scoped types (``mark_excluded`` / ``manual_moneyline``)
        are left untouched. Pre-validates every input before any DB
        write, then runs the UPDATE-supersede + INSERT in a single
        transaction so a failed write cannot orphan the prior active
        row. ``odds_match_results`` is NOT recomputed here — the
        effective_status / fighter_id apply pass lives in
        ``odds_matching_service`` and is composed by the service layer.
        """
        with self.conn:
            new_id = self._add_override_unlocked(
                slate_id=slate_id,
                override_type=override_type,
                odds_row_key=odds_row_key,
                fighter_id=fighter_id,
                payload=payload,
                reason=reason,
            )

        row = self.conn.execute(
            "SELECT id, slate_id, odds_row_key, fighter_id, override_type, "
            "payload_json, reason, created_at, superseded_at "
            "FROM manual_match_overrides WHERE id = ?",
            (new_id,),
        ).fetchone()
        return _row_to_manual_match_override(row)

    def _add_override_unlocked(
        self,
        *,
        slate_id: int,
        override_type: str,
        odds_row_key: str | None = None,
        fighter_id: int | None = None,
        payload: dict | None = None,
        reason: str | None = None,
    ) -> int:
        """Worker for ``add_override``.

        Runs the validation + UPDATE-supersede + INSERT sequence without
        managing the transaction so Phase D.4.3 / D.5.1 composers can
        invoke it from inside an externally-provided ``with self.conn:``
        block alongside other writes. Returns the new
        ``manual_match_overrides`` row id; callers fetch the full record
        themselves.
        """
        if override_type not in self.RESOLUTION_OVERRIDE_TYPES:
            raise NotImplementedError(
                "add_override currently supports override_type in "
                f"{self.RESOLUTION_OVERRIDE_TYPES}; got {override_type!r}"
            )

        slate_id = int(slate_id)
        is_binding = override_type in self.BINDING_OVERRIDE_TYPES

        # --- Pre-DB validation -----------------------------------------
        if not isinstance(odds_row_key, str) or not odds_row_key.strip():
            raise ValueError(
                f"odds_row_key is required for {override_type} and must be "
                "a non-empty string"
            )
        key = odds_row_key.strip()

        if payload not in (None, {}):
            raise ValueError(
                f"{override_type} overrides must not carry a payload; "
                f"got {payload!r}"
            )

        if reason is None:
            reason_clean: str | None = None
        elif isinstance(reason, str):
            stripped = reason.strip()
            reason_clean = stripped or None
        else:
            raise ValueError("reason must be a string or None")

        if self.conn.execute(
            "SELECT 1 FROM slates WHERE id = ?", (slate_id,)
        ).fetchone() is None:
            raise ValueError(f"slate #{slate_id} does not exist")

        if self.conn.execute(
            "SELECT 1 FROM odds_rows WHERE slate_id = ? AND odds_row_key = ?",
            (slate_id, key),
        ).fetchone() is None:
            raise ValueError(
                f"odds_row_key={key!r} not found on slate #{slate_id}"
            )

        # Binding overrides require a NOT-NULL, active, same-slate fighter
        # (§16.3 / §16.11); reject_match's fighter_id is optional and not
        # active-checked — that branch stays exactly as D.1 shipped.
        if is_binding and fighter_id is None:
            raise ValueError(
                f"fighter_id is required for {override_type}"
            )

        fid: int | None = None
        if fighter_id is not None:
            fid = int(fighter_id)
            f_row = self.conn.execute(
                "SELECT slate_id, status FROM fighters WHERE id = ?", (fid,)
            ).fetchone()
            if f_row is None:
                raise ValueError(f"fighter #{fid} does not exist")
            if int(f_row[0]) != slate_id:
                raise ValueError(
                    f"fighter #{fid} belongs to slate "
                    f"#{int(f_row[0])}, not slate #{slate_id}"
                )
            if is_binding and f_row[1] != "active":
                raise ValueError(
                    f"fighter #{fid} is not active (status={f_row[1]!r}); "
                    "only active fighters can be bound"
                )

        # A fighter already bound elsewhere cannot take a second binding
        # (§16.11). Re-binding the same fighter to the SAME key is
        # idempotent (it supersedes the prior binding below), so a binding
        # on THIS key never counts as a conflict.
        if is_binding:
            self._reject_if_fighter_already_bound(
                slate_id=slate_id, fighter_id=fid, odds_row_key=key
            )

        # --- Supersede + insert ---------------------------------------
        # §16.4: inserting any resolution override supersedes every active
        # resolution-set row on the key, regardless of type.
        self.conn.execute(
            "UPDATE manual_match_overrides "
            "SET superseded_at = datetime('now') "
            "WHERE slate_id = ? AND odds_row_key = ? "
            "AND override_type IN ('reject_match', 'accept_match', 'force_pair') "
            "AND superseded_at IS NULL",
            (slate_id, key),
        )
        cur = self.conn.execute(
            "INSERT INTO manual_match_overrides "
            "(slate_id, odds_row_key, fighter_id, override_type, "
            " payload_json, reason) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (slate_id, key, fid, override_type, reason_clean),
        )
        return int(cur.lastrowid)

    def _reject_if_fighter_already_bound(
        self, *, slate_id: int, fighter_id: int, odds_row_key: str
    ) -> None:
        """Raise ``ValueError`` if ``fighter_id`` already holds a binding on
        another odds row of ``slate_id`` (§16.11).

        Two binding sources count, both scoped to a *different*
        ``odds_row_key`` so an idempotent same-key re-assign is allowed:

        - an active ``auto_match`` ``odds_match_results`` row (the matcher
          already bound this fighter — a rejected ``auto_match`` row has
          ``effective_status != 'auto_match'`` and does not count), or
        - an active ``accept_match`` / ``force_pair`` override binding
          another row.
        """
        auto = self.conn.execute(
            "SELECT odds_row_key FROM odds_match_results "
            "WHERE slate_id = ? AND fighter_id = ? "
            "AND effective_status = 'auto_match' AND odds_row_key <> ? "
            "LIMIT 1",
            (slate_id, fighter_id, odds_row_key),
        ).fetchone()
        if auto is not None:
            raise ValueError(
                f"fighter #{fighter_id} is already auto-matched to odds row "
                f"{auto[0]!r} on slate #{slate_id}; reject that binding "
                "before assigning the fighter to another row"
            )
        other = self.conn.execute(
            "SELECT odds_row_key FROM manual_match_overrides "
            "WHERE slate_id = ? AND fighter_id = ? "
            "AND override_type IN ('accept_match', 'force_pair') "
            "AND superseded_at IS NULL AND odds_row_key <> ? "
            "LIMIT 1",
            (slate_id, fighter_id, odds_row_key),
        ).fetchone()
        if other is not None:
            raise ValueError(
                f"fighter #{fighter_id} is already bound to odds row "
                f"{other[0]!r} on slate #{slate_id}; reject that binding "
                "before assigning the fighter to another row"
            )


def _row_to_manual_match_override(row) -> ManualMatchOverrideRecord:
    return ManualMatchOverrideRecord(
        id=int(row[0]),
        slate_id=int(row[1]),
        odds_row_key=row[2],
        fighter_id=(int(row[3]) if row[3] is not None else None),
        override_type=row[4],
        payload_json=row[5],
        reason=row[6],
        created_at=row[7],
        superseded_at=row[8],
    )


class OddsRepository:
    """TODO: implement in a later milestone."""


class ProjectionRepository:
    """TODO: implement in a later milestone."""


class AlertRepository:
    """TODO: implement in a later milestone."""


class LineupRepository:
    """TODO: implement in a later milestone."""
