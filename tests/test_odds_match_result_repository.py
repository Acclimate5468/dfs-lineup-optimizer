"""Tests for the Phase C.3 ``OddsMatchResultRepository`` write/read path.

Covers ``replace_for_slate`` (atomic DELETE + INSERT) and ``list_for_slate``
read-back. The repository operates on already-computed
``OddsMatchResultRecord`` instances — calling the matcher service itself is
out of scope here (see ``test_odds_matching_service.py`` for that).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.db.repositories import (
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching_service import OddsMatchResultRecord


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def slate_id(conn):
    return SlateRepository(conn).create(event_name="UFC 999").id


@pytest.fixture
def other_slate_id(conn):
    return SlateRepository(conn).create(event_name="UFC 998").id


def _insert_fighter(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    name: str,
    salary: int = 8000,
    status: str = "active",
) -> int:
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, ?)",
        (int(slate_id), name, int(salary), status),
    )
    conn.commit()
    return int(cur.lastrowid)


def _save_odds_row(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    fighter_name_raw: str,
    american_odds: int = -150,
    captured_at: str = "2026-05-20T00:00:00Z",
    source: str = "manual",
):
    return OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source=source,
        captured_at=captured_at,
    )


def _result(
    *,
    slate_id: int,
    odds_row_id: int,
    odds_row_key: str,
    fighter_id: int | None = None,
    match_status: str = "auto_match",
    effective_status: str | None = None,
    match_stage: str = "exact_conservative",
    match_score: int = 100,
    preferred_candidate: str | None = None,
    opponent_check: str = "not_applicable",
    candidates: tuple = (),
    notes: tuple = (),
) -> OddsMatchResultRecord:
    return OddsMatchResultRecord(
        slate_id=slate_id,
        odds_row_id=odds_row_id,
        odds_row_key=odds_row_key,
        fighter_id=fighter_id,
        match_status=match_status,
        effective_status=effective_status or match_status,
        match_stage=match_stage,
        match_score=match_score,
        preferred_candidate=preferred_candidate,
        opponent_check=opponent_check,
        candidates=candidates,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Basic insert + read
# ---------------------------------------------------------------------------


def test_replace_for_slate_inserts_records(conn, slate_id):
    fighter_id = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")

    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fighter_id,
            )
        ],
    )

    rows = repo.list_for_slate(slate_id)
    assert len(rows) == 1
    rec = rows[0]
    assert rec.slate_id == slate_id
    assert rec.odds_row_id == row.id
    assert rec.odds_row_key == row.odds_row_key
    assert rec.fighter_id == fighter_id
    assert rec.match_status == "auto_match"
    assert rec.effective_status == "auto_match"
    assert rec.match_stage == "exact_conservative"
    assert rec.match_score == 100
    assert rec.opponent_check == "not_applicable"
    assert rec.preferred_candidate is None
    assert rec.candidates == ()
    assert rec.notes == ()


def test_replace_for_slate_with_empty_list_clears_results(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")

    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fid,
            )
        ],
    )
    assert len(repo.list_for_slate(slate_id)) == 1

    repo.replace_for_slate(slate_id, [])
    assert repo.list_for_slate(slate_id) == []


def test_list_for_slate_empty(conn, slate_id):
    assert OddsMatchResultRepository(conn).list_for_slate(slate_id) == []


def test_list_for_slate_orders_by_odds_row_id_ascending(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")
    _insert_fighter(conn, slate_id=slate_id, name="Conor McGregor")

    rows = [
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw="Jose Aldo",
            captured_at="2026-05-20T00:00:00Z",
        ),
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw="Marlon Vera",
            captured_at="2026-05-20T00:01:00Z",
        ),
        _save_odds_row(
            conn,
            slate_id=slate_id,
            fighter_name_raw="Conor McGregor",
            captured_at="2026-05-20T00:02:00Z",
        ),
    ]
    repo = OddsMatchResultRepository(conn)
    # Pass in reverse order — the repository must still surface ascending
    # by odds_row_id (deterministic regardless of caller order).
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=r.id,
                odds_row_key=r.odds_row_key,
            )
            for r in reversed(rows)
        ],
    )

    listed = repo.list_for_slate(slate_id)
    assert [r.odds_row_id for r in listed] == [rows[0].id, rows[1].id, rows[2].id]


def test_list_for_slate_is_deterministic_across_calls(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
            )
        ],
    )
    assert repo.list_for_slate(slate_id) == repo.list_for_slate(slate_id)


# ---------------------------------------------------------------------------
# Replace semantics — DELETE + INSERT per slate
# ---------------------------------------------------------------------------


def test_second_replace_deletes_old_rows_and_inserts_new(conn, slate_id):
    aldo_id = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    vera_id = _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")

    row_a = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Jose Aldo",
        captured_at="2026-05-20T00:00:00Z",
    )
    row_b = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Marlon Vera",
        captured_at="2026-05-20T00:01:00Z",
    )

    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row_a.id,
                odds_row_key=row_a.odds_row_key,
                fighter_id=aldo_id,
            ),
        ],
    )
    assert [r.odds_row_id for r in repo.list_for_slate(slate_id)] == [row_a.id]

    # Recompute: now both rows resolve. First run's record must be gone.
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row_a.id,
                odds_row_key=row_a.odds_row_key,
                fighter_id=aldo_id,
                match_status="review_required",
                effective_status="review_required",
                match_stage="fuzzy",
                match_score=92,
            ),
            _result(
                slate_id=slate_id,
                odds_row_id=row_b.id,
                odds_row_key=row_b.odds_row_key,
                fighter_id=vera_id,
            ),
        ],
    )

    listed = repo.list_for_slate(slate_id)
    assert [r.odds_row_id for r in listed] == [row_a.id, row_b.id]
    # The first row's prior auto_match was overwritten by review_required.
    a = next(r for r in listed if r.odds_row_id == row_a.id)
    assert a.match_status == "review_required"
    assert a.match_score == 92


def test_replace_one_slate_does_not_affect_other_slate(conn, slate_id, other_slate_id):
    fid1 = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    fid2 = _insert_fighter(conn, slate_id=other_slate_id, name="Jose Aldo")
    r1 = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    r2 = _save_odds_row(conn, slate_id=other_slate_id, fighter_name_raw="Jose Aldo")

    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [_result(slate_id=slate_id, odds_row_id=r1.id, odds_row_key=r1.odds_row_key, fighter_id=fid1)],
    )
    repo.replace_for_slate(
        other_slate_id,
        [_result(slate_id=other_slate_id, odds_row_id=r2.id, odds_row_key=r2.odds_row_key, fighter_id=fid2)],
    )

    # Re-replacing slate_id must NOT touch the other slate's row.
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=r1.id,
                odds_row_key=r1.odds_row_key,
                fighter_id=fid1,
                match_status="review_required",
                effective_status="review_required",
                match_stage="fuzzy",
                match_score=90,
            )
        ],
    )

    s1 = repo.list_for_slate(slate_id)
    s2 = repo.list_for_slate(other_slate_id)
    assert [r.odds_row_id for r in s1] == [r1.id]
    assert s1[0].match_status == "review_required"
    assert [r.odds_row_id for r in s2] == [r2.id]
    assert s2[0].match_status == "auto_match"
    assert s2[0].fighter_id == fid2


def test_replace_for_slate_with_empty_list_does_not_touch_other_slate(
    conn, slate_id, other_slate_id
):
    fid = _insert_fighter(conn, slate_id=other_slate_id, name="Jose Aldo")
    r2 = _save_odds_row(conn, slate_id=other_slate_id, fighter_name_raw="Jose Aldo")

    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        other_slate_id,
        [_result(slate_id=other_slate_id, odds_row_id=r2.id, odds_row_key=r2.odds_row_key, fighter_id=fid)],
    )

    repo.replace_for_slate(slate_id, [])

    assert repo.list_for_slate(slate_id) == []
    assert len(repo.list_for_slate(other_slate_id)) == 1


# ---------------------------------------------------------------------------
# Cross-slate input is rejected
# ---------------------------------------------------------------------------


def test_cross_slate_result_input_is_rejected(conn, slate_id, other_slate_id):
    _insert_fighter(conn, slate_id=other_slate_id, name="Jose Aldo")
    other_row = _save_odds_row(
        conn, slate_id=other_slate_id, fighter_name_raw="Jose Aldo"
    )

    repo = OddsMatchResultRepository(conn)
    with pytest.raises(ValueError, match="slate_id"):
        repo.replace_for_slate(
            slate_id,
            [
                _result(
                    slate_id=other_slate_id,  # belongs to a different slate
                    odds_row_id=other_row.id,
                    odds_row_key=other_row.odds_row_key,
                )
            ],
        )


def test_cross_slate_input_does_not_partially_write(conn, slate_id, other_slate_id):
    """Validation must fire before the DELETE runs — a bad input cannot
    silently wipe persisted state."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    good_row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Jose Aldo"
    )
    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=good_row.id,
                odds_row_key=good_row.odds_row_key,
                fighter_id=fid,
            )
        ],
    )
    assert len(repo.list_for_slate(slate_id)) == 1

    _insert_fighter(conn, slate_id=other_slate_id, name="Jose Aldo")
    other_row = _save_odds_row(
        conn, slate_id=other_slate_id, fighter_name_raw="Jose Aldo"
    )

    with pytest.raises(ValueError):
        repo.replace_for_slate(
            slate_id,
            [
                _result(
                    slate_id=slate_id,
                    odds_row_id=good_row.id,
                    odds_row_key=good_row.odds_row_key,
                    fighter_id=fid,
                ),
                _result(
                    slate_id=other_slate_id,  # bad
                    odds_row_id=other_row.id,
                    odds_row_key=other_row.odds_row_key,
                ),
            ],
        )

    # Previously-persisted row is still there — the DELETE never ran.
    listed = repo.list_for_slate(slate_id)
    assert len(listed) == 1
    assert listed[0].odds_row_id == good_row.id


# ---------------------------------------------------------------------------
# Nullable fields
# ---------------------------------------------------------------------------


def test_nullable_fighter_id_allowed(conn, slate_id):
    """``unmatched`` and ambiguous verdicts persist with ``fighter_id``
    NULL — the FK is ON DELETE SET NULL, not NOT NULL."""
    _insert_fighter(conn, slate_id=slate_id, name="Some Fighter")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Unrelated Person"
    )
    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=None,
                match_status="unmatched",
                effective_status="unmatched",
                match_stage="fuzzy",
                match_score=40,
            )
        ],
    )
    [rec] = repo.list_for_slate(slate_id)
    assert rec.fighter_id is None
    assert rec.match_status == "unmatched"


def test_nullable_preferred_candidate_allowed(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                preferred_candidate=None,
            )
        ],
    )
    [rec] = repo.list_for_slate(slate_id)
    assert rec.preferred_candidate is None


# ---------------------------------------------------------------------------
# JSON encoding for candidates / notes
# ---------------------------------------------------------------------------


def test_empty_candidates_and_notes_stored_as_null(conn, slate_id):
    """Design §14.9: empty → NULL in the DB. ``list_for_slate`` reads them
    back as ``()`` to match the matcher's tuple shape."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                candidates=(),
                notes=(),
            )
        ],
    )

    raw = conn.execute(
        "SELECT candidates_json, notes_json FROM odds_match_results "
        "WHERE slate_id = ?",
        (slate_id,),
    ).fetchone()
    assert raw[0] is None
    assert raw[1] is None

    [rec] = repo.list_for_slate(slate_id)
    assert rec.candidates == ()
    assert rec.notes == ()


def test_candidates_and_notes_round_trip_as_json_arrays(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Dan Smith")
    _insert_fighter(conn, slate_id=slate_id, name="Daniel Smith")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Daniel Smith Jr."
    )
    repo = OddsMatchResultRepository(conn)
    candidates = ("Dan Smith", "Daniel Smith")
    notes = ("ambiguous_aggressive", "opponent_supported_disambiguation")
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=None,
                match_status="review_required",
                effective_status="review_required",
                match_stage="exact_aggressive",
                match_score=100,
                preferred_candidate="Daniel Smith",
                opponent_check="passed",
                candidates=candidates,
                notes=notes,
            )
        ],
    )

    raw = conn.execute(
        "SELECT candidates_json, notes_json FROM odds_match_results "
        "WHERE slate_id = ?",
        (slate_id,),
    ).fetchone()
    assert json.loads(raw[0]) == list(candidates)
    assert json.loads(raw[1]) == list(notes)

    [rec] = repo.list_for_slate(slate_id)
    assert rec.candidates == candidates
    assert rec.notes == notes
    assert rec.preferred_candidate == "Daniel Smith"
    assert rec.opponent_check == "passed"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_replace_rejects_unknown_match_status(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    with pytest.raises(ValueError, match="match_status"):
        repo.replace_for_slate(
            slate_id,
            [
                _result(
                    slate_id=slate_id,
                    odds_row_id=row.id,
                    odds_row_key=row.odds_row_key,
                    match_status="shadowed",  # not allowed in Phase C
                    effective_status="shadowed",
                )
            ],
        )


def test_replace_rejects_unknown_match_stage(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    with pytest.raises(ValueError, match="match_stage"):
        repo.replace_for_slate(
            slate_id,
            [
                _result(
                    slate_id=slate_id,
                    odds_row_id=row.id,
                    odds_row_key=row.odds_row_key,
                    match_stage="phonetic",
                )
            ],
        )


def test_replace_rejects_match_score_out_of_range(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    with pytest.raises(ValueError, match="match_score"):
        repo.replace_for_slate(
            slate_id,
            [
                _result(
                    slate_id=slate_id,
                    odds_row_id=row.id,
                    odds_row_key=row.odds_row_key,
                    match_score=120,
                )
            ],
        )


def test_replace_rejects_review_rejected_as_match_status(conn, slate_id):
    """``review_rejected`` is a Phase D.4 resolver output and must never
    appear in ``match_status``. The two allowed-status sets are disjoint
    for exactly this case."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    with pytest.raises(ValueError, match="match_status"):
        repo.replace_for_slate(
            slate_id,
            [
                _result(
                    slate_id=slate_id,
                    odds_row_id=row.id,
                    odds_row_key=row.odds_row_key,
                    match_status="review_rejected",
                    effective_status="review_rejected",
                )
            ],
        )


def test_replace_accepts_review_rejected_as_effective_status(conn, slate_id):
    """``review_rejected`` is a legal ``effective_status`` so the Phase
    D.4.2 apply pass can persist it (and so a future direct-INSERT
    callpath can land it without a no-op migration)."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fid,
                match_status="review_required",
                effective_status="review_rejected",
            )
        ],
    )
    [rec] = repo.list_for_slate(slate_id)
    assert rec.match_status == "review_required"
    assert rec.effective_status == "review_rejected"


def test_replace_rejects_unknown_opponent_check(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    with pytest.raises(ValueError, match="opponent_check"):
        repo.replace_for_slate(
            slate_id,
            [
                _result(
                    slate_id=slate_id,
                    odds_row_id=row.id,
                    odds_row_key=row.odds_row_key,
                    opponent_check="maybe",
                )
            ],
        )


# ---------------------------------------------------------------------------
# FK behavior
# ---------------------------------------------------------------------------


def test_cascade_delete_from_odds_row_removes_match_result(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
            )
        ],
    )
    conn.execute("DELETE FROM odds_rows WHERE id = ?", (row.id,))
    conn.commit()
    assert repo.list_for_slate(slate_id) == []


def test_cascade_delete_from_slate_removes_match_results(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
            )
        ],
    )
    conn.execute("DELETE FROM slates WHERE id = ?", (slate_id,))
    conn.commit()
    assert repo.list_for_slate(slate_id) == []


def test_fighter_delete_sets_fighter_id_null(conn, slate_id):
    """Schema declares ``fighter_id ... ON DELETE SET NULL``. A salary
    re-import that drops a fighter must NOT cascade-delete the match
    result row; it just blanks the FK so the next recompute can re-resolve."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
                fighter_id=fid,
            )
        ],
    )
    conn.execute("DELETE FROM fighters WHERE id = ?", (fid,))
    conn.commit()
    [rec] = repo.list_for_slate(slate_id)
    assert rec.fighter_id is None
    assert rec.odds_row_id == row.id


def test_duplicate_odds_row_in_one_replace_raises(conn, slate_id):
    """UNIQUE(slate_id, odds_row_id) must trip if the caller hands the
    repository two records for the same odds_row in a single call.
    Importantly: the prior persisted state survives the failure intact."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = OddsMatchResultRepository(conn)
    repo.replace_for_slate(
        slate_id,
        [
            _result(
                slate_id=slate_id,
                odds_row_id=row.id,
                odds_row_key=row.odds_row_key,
            )
        ],
    )
    assert len(repo.list_for_slate(slate_id)) == 1

    dup = _result(
        slate_id=slate_id,
        odds_row_id=row.id,
        odds_row_key=row.odds_row_key,
        match_status="review_required",
        effective_status="review_required",
        match_stage="fuzzy",
        match_score=90,
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.replace_for_slate(slate_id, [dup, dup])

    # Transaction rolled back — original row is still there.
    listed = repo.list_for_slate(slate_id)
    assert len(listed) == 1
    assert listed[0].match_status == "auto_match"
