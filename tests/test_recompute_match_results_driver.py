"""Tests for the Phase C.4 driver ``recompute_and_replace_match_results``.

The driver wires the persisted-state path end-to-end:
``OddsRowRepository`` + ``FighterRepository`` + ``FightGroupRepository``
→ ``compute_match_results`` → ``OddsMatchResultRepository.replace_for_slate``.
No UI is involved; these tests drive the service directly.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    FightGroupRepository,
    ManualMatchOverrideRepository,
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching import (
    OPPONENT_FAILED,
    OPPONENT_PASSED,
    STATUS_AUTO,
    STATUS_REVIEW,
)
from src.ingestion.odds_matching_service import (
    ApplyOverridesSummary,
    EmptyDkRosterError,
    RecomputeSummary,
    recompute_and_replace_match_results,
)


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
    opponent_name_raw: str | None = None,
    captured_at: str = "2026-05-20T00:00:00Z",
    source: str = "manual",
):
    return OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source=source,
        captured_at=captured_at,
        opponent_name_raw=opponent_name_raw,
    )


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------


def test_recompute_persists_auto_match_end_to_end(conn, slate_id):
    aldo_id = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")

    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Jose Aldo"
    )

    summary = recompute_and_replace_match_results(conn, slate_id)

    assert isinstance(summary, RecomputeSummary)
    assert summary.slate_id == slate_id
    assert summary.total == 1
    assert summary.status_counts == {STATUS_AUTO: 1}

    [rec] = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert rec.odds_row_id == row.id
    assert rec.odds_row_key == row.odds_row_key
    assert rec.fighter_id == aldo_id
    assert rec.match_status == STATUS_AUTO
    assert rec.effective_status == STATUS_AUTO


def test_recompute_summary_breaks_down_mixed_statuses(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Dan Smith")
    _insert_fighter(conn, slate_id=slate_id, name="Daniel Smith")

    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        captured_at="2026-05-20T00:00:00Z",
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Daniel Smith Jr.",
        captured_at="2026-05-20T00:01:00Z",
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Totally Unrelated Person",
        captured_at="2026-05-20T00:02:00Z",
    )

    summary = recompute_and_replace_match_results(conn, slate_id)

    assert summary.total == 3
    assert summary.status_counts == {
        STATUS_AUTO: 1,
        STATUS_REVIEW: 1,
        "unmatched": 1,
    }


# ---------------------------------------------------------------------------
# Empty odds: clear existing results
# ---------------------------------------------------------------------------


def test_recompute_with_no_odds_rows_clears_existing_results(conn, slate_id):
    """Design §11 reset-behavior table: a recompute with no inputs deletes
    the slate's persisted match results (delete + reinsert with []).

    Seed with one odds row, recompute → 1 persisted row.
    Delete the odds row, recompute → empty.
    """
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Jose Aldo"
    )
    recompute_and_replace_match_results(conn, slate_id)
    repo = OddsMatchResultRepository(conn)
    assert len(repo.list_for_slate(slate_id)) == 1

    conn.execute("DELETE FROM odds_rows WHERE id = ?", (row.id,))
    conn.commit()
    # The cascade FK already cleared the result row; explicitly recompute
    # to confirm the driver writes an empty result set without error.
    summary = recompute_and_replace_match_results(conn, slate_id)
    assert summary.total == 0
    assert summary.status_counts == {}
    assert repo.list_for_slate(slate_id) == []


def test_recompute_with_no_odds_rows_on_fresh_slate_returns_zero(
    conn, slate_id
):
    """Slate has fighters but no odds rows yet — driver returns zero
    counts and persists nothing."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")

    summary = recompute_and_replace_match_results(conn, slate_id)
    assert summary.total == 0
    assert summary.status_counts == {}
    assert OddsMatchResultRepository(conn).list_for_slate(slate_id) == []


# ---------------------------------------------------------------------------
# No active DK fighters → refuse, don't wipe
# ---------------------------------------------------------------------------


def test_recompute_with_no_active_fighters_raises_and_preserves_results(
    conn, slate_id
):
    """Design §14.3 mode B / §13.12: refuse rather than emit throwaway
    ``unmatched`` rows. Critically — existing persisted results for the
    slate must survive the refusal (no partial DELETE)."""
    aldo_id = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")

    # First recompute succeeds with one auto_match row.
    recompute_and_replace_match_results(conn, slate_id)
    repo = OddsMatchResultRepository(conn)
    before = repo.list_for_slate(slate_id)
    assert len(before) == 1

    # Excluding the only active fighter empties the active roster. Next
    # recompute must raise and NOT touch the persisted result.
    conn.execute(
        "UPDATE fighters SET status = 'excluded' WHERE id = ?", (aldo_id,)
    )
    conn.commit()

    with pytest.raises(EmptyDkRosterError) as excinfo:
        recompute_and_replace_match_results(conn, slate_id)
    assert excinfo.value.slate_id == slate_id

    after = repo.list_for_slate(slate_id)
    assert len(after) == 1
    assert after[0].odds_row_id == before[0].odds_row_id
    assert after[0].match_status == STATUS_AUTO


# ---------------------------------------------------------------------------
# Fight group opponent context flows through the driver
# ---------------------------------------------------------------------------


def test_fight_group_opponent_context_flips_opponent_check(conn, slate_id):
    """A confirmed fight group whose opponent disagrees with the odds row's
    ``opponent_name_raw`` should demote the verdict to ``review_required``
    and surface ``opponent_check = failed`` in the persisted row."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")

    fg = FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
    )
    FightGroupRepository(conn).update_status(fg.id, "confirmed")

    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        opponent_name_raw="Conor McGregor",  # disagrees with fight group
    )

    summary = recompute_and_replace_match_results(conn, slate_id)
    assert summary.status_counts == {STATUS_REVIEW: 1}

    [rec] = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert rec.match_status == STATUS_REVIEW
    assert rec.opponent_check == OPPONENT_FAILED


def test_fight_group_opponent_context_passes_when_aligned(conn, slate_id):
    """Sanity counterpart: when the odds row's opponent matches the
    confirmed fight group, opponent_check is ``passed`` and the verdict
    stays at ``auto_match``."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")

    fg = FightGroupRepository(conn).create(
        slate_id=slate_id,
        fighter_1_name="Jose Aldo",
        fighter_2_name="Marlon Vera",
    )
    FightGroupRepository(conn).update_status(fg.id, "confirmed")

    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        opponent_name_raw="Marlon Vera",
    )

    summary = recompute_and_replace_match_results(conn, slate_id)
    assert summary.status_counts == {STATUS_AUTO: 1}

    [rec] = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert rec.match_status == STATUS_AUTO
    assert rec.opponent_check == OPPONENT_PASSED


# ---------------------------------------------------------------------------
# Slate-scoped: recompute does not touch another slate
# ---------------------------------------------------------------------------


def test_recompute_is_slate_scoped(conn, slate_id, other_slate_id):
    """Recomputing slate A must not delete or overwrite slate B's
    persisted match results."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=other_slate_id, name="Conor McGregor")

    _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    _save_odds_row(
        conn, slate_id=other_slate_id, fighter_name_raw="Conor McGregor"
    )

    recompute_and_replace_match_results(conn, slate_id)
    recompute_and_replace_match_results(conn, other_slate_id)

    repo = OddsMatchResultRepository(conn)
    assert len(repo.list_for_slate(slate_id)) == 1
    assert len(repo.list_for_slate(other_slate_id)) == 1

    # Re-running slate A's recompute must leave slate B untouched.
    other_before = repo.list_for_slate(other_slate_id)
    recompute_and_replace_match_results(conn, slate_id)
    other_after = repo.list_for_slate(other_slate_id)
    assert other_after == other_before


def test_recompute_empty_slate_a_does_not_touch_slate_b(
    conn, slate_id, other_slate_id
):
    """Slate A has no odds rows → recompute persists an empty set for
    A; slate B's existing results must remain."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=other_slate_id, name="Conor McGregor")
    _save_odds_row(
        conn, slate_id=other_slate_id, fighter_name_raw="Conor McGregor"
    )
    recompute_and_replace_match_results(conn, other_slate_id)
    repo = OddsMatchResultRepository(conn)
    assert len(repo.list_for_slate(other_slate_id)) == 1

    summary = recompute_and_replace_match_results(conn, slate_id)
    assert summary.total == 0
    assert repo.list_for_slate(slate_id) == []
    assert len(repo.list_for_slate(other_slate_id)) == 1


# ---------------------------------------------------------------------------
# Phase D.4.3.b — effective_status override apply pass composed with replace
# ---------------------------------------------------------------------------


def test_recompute_summary_includes_apply_overrides_summary(conn, slate_id):
    """``RecomputeSummary.apply`` is an ``ApplyOverridesSummary`` for the
    same slate. On a fresh slate with no overrides, ``rows_updated`` is 0
    and ``stale_override_ids`` is empty."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")

    summary = recompute_and_replace_match_results(conn, slate_id)

    assert isinstance(summary.apply, ApplyOverridesSummary)
    assert summary.apply.slate_id == slate_id
    assert summary.apply.rows_updated == 0
    assert summary.apply.stale_override_ids == []


def test_recompute_with_no_overrides_apply_rows_updated_zero(conn, slate_id):
    """Mixed-status slate with zero active overrides — replace writes
    every row with ``effective_status == match_status`` so the in-tx
    apply has nothing to update."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_fighter(conn, slate_id=slate_id, name="Dan Smith")
    _insert_fighter(conn, slate_id=slate_id, name="Daniel Smith")
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Jose Aldo",
        captured_at="2026-05-20T00:00:00Z",
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Daniel Smith Jr.",
        captured_at="2026-05-20T00:01:00Z",
    )
    _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="Totally Unrelated Person",
        captured_at="2026-05-20T00:02:00Z",
    )

    summary = recompute_and_replace_match_results(conn, slate_id)

    assert summary.apply.rows_updated == 0
    assert summary.apply.stale_override_ids == []
    for rec in OddsMatchResultRepository(conn).list_for_slate(slate_id):
        assert rec.effective_status == rec.match_status


def test_recompute_applies_active_reject_after_replace(conn, slate_id):
    """An active ``reject_match`` override on a slate flips
    ``effective_status`` to ``review_rejected`` inside the same
    transaction as the replace — composers do not need a follow-up
    apply call."""
    aldo_id = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Jose Aldo"
    )

    # Seed an initial result row + register the active reject.
    recompute_and_replace_match_results(conn, slate_id)
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=aldo_id,
    )

    summary = recompute_and_replace_match_results(conn, slate_id)

    assert summary.apply.rows_updated == 1
    assert summary.apply.stale_override_ids == []
    [rec] = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert rec.match_status == STATUS_AUTO
    assert rec.effective_status == "review_rejected"


def test_recompute_keeps_match_status_unchanged_under_active_reject(
    conn, slate_id
):
    """The apply pass must touch ``effective_status`` only — even when
    an active reject is in place, ``match_status`` mirrors the matcher
    verdict from the just-completed replace."""
    aldo_id = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Jose Aldo"
    )
    recompute_and_replace_match_results(conn, slate_id)
    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=aldo_id,
    )

    recompute_and_replace_match_results(conn, slate_id)

    [rec] = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert rec.match_status == STATUS_AUTO
    # Raw column read — belt-and-braces against record-projection bugs.
    [(persisted_match_status,)] = conn.execute(
        "SELECT match_status FROM odds_match_results WHERE slate_id = ?",
        (slate_id,),
    ).fetchall()
    assert persisted_match_status == STATUS_AUTO


def test_recompute_with_empty_odds_lists_stale_override(conn, slate_id):
    """Recompute that clears the result set still applies overrides; an
    active reject whose ``odds_row_key`` has no surviving result row
    surfaces in ``apply.stale_override_ids``."""
    aldo_id = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Jose Aldo"
    )
    recompute_and_replace_match_results(conn, slate_id)
    rec = ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=aldo_id,
    )

    # Delete the odds row; cascade clears the persisted result.
    conn.execute("DELETE FROM odds_rows WHERE id = ?", (row.id,))
    conn.commit()

    summary = recompute_and_replace_match_results(conn, slate_id)

    assert summary.total == 0
    assert summary.apply.rows_updated == 0
    assert summary.apply.stale_override_ids == [rec.id]


# ---------------------------------------------------------------------------
# Phase D.5.1 — accept_match / force_pair bindings survive recompute (§16.7)
# ---------------------------------------------------------------------------


def test_recompute_applies_force_pair_binding_to_unmatched_row(conn, slate_id):
    """The §16.1 scenario: an active fighter whose name-mismatched odds row
    the matcher left ``unmatched``. A force_pair survives recompute —
    ``effective_status`` flips to ``force_pair`` and ``fighter_id`` is
    written, while ``match_status`` stays the matcher's raw ``unmatched``."""
    bruno_id = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Totally Unrelated Person"
    )

    recompute_and_replace_match_results(conn, slate_id)
    [rec0] = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert rec0.match_status == "unmatched"
    assert rec0.fighter_id is None

    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=bruno_id,
    )

    summary = recompute_and_replace_match_results(conn, slate_id)

    assert summary.apply.rows_updated == 1
    assert summary.apply.stale_override_ids == []
    [rec] = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert rec.match_status == "unmatched"
    assert rec.effective_status == "force_pair"
    assert rec.fighter_id == bruno_id
    # Raw column read — belt-and-braces against record-projection bugs.
    [(raw_status,)] = conn.execute(
        "SELECT match_status FROM odds_match_results WHERE slate_id = ?",
        (slate_id,),
    ).fetchall()
    assert raw_status == "unmatched"


def test_recompute_applies_accept_match_binding_to_review_row(conn, slate_id):
    _insert_fighter(conn, slate_id=slate_id, name="Dan Smith")
    daniel_id = _insert_fighter(conn, slate_id=slate_id, name="Daniel Smith")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Daniel Smith Jr."
    )

    recompute_and_replace_match_results(conn, slate_id)
    [rec0] = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert rec0.match_status == STATUS_REVIEW

    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="accept_match",
        odds_row_key=row.odds_row_key,
        fighter_id=daniel_id,
    )

    summary = recompute_and_replace_match_results(conn, slate_id)

    assert summary.apply.rows_updated == 1
    [rec] = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert rec.match_status == STATUS_REVIEW
    assert rec.effective_status == "review_accepted"
    assert rec.fighter_id == daniel_id


def test_recompute_force_pair_to_inactive_fighter_is_stale(conn, slate_id):
    """If the bound fighter has since been deactivated, recompute treats the
    binding as stale: not written, override id surfaced (§16.12)."""
    bruno_id = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    _insert_fighter(conn, slate_id=slate_id, name="Marlon Vera")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Totally Unrelated Person"
    )
    recompute_and_replace_match_results(conn, slate_id)
    rec = ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=bruno_id,
    )
    # Deactivate Bruno after the binding was recorded.
    conn.execute(
        "UPDATE fighters SET status = 'excluded' WHERE id = ?", (bruno_id,)
    )
    conn.commit()

    summary = recompute_and_replace_match_results(conn, slate_id)

    assert summary.apply.rows_updated == 0
    assert summary.apply.stale_override_ids == [rec.id]
    [result] = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert result.effective_status == "unmatched"
    assert result.fighter_id is None


def test_recompute_rolls_back_replace_when_apply_fails(
    conn, slate_id, monkeypatch
):
    """If ``_apply_overrides_unlocked`` raises after the unlocked replace
    has DELETEd + INSERTed inside the recompute transaction, both the
    new INSERTs and the DELETE must roll back — the slate's prior
    persisted results survive the failed recompute intact."""
    _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Jose Aldo"
    )
    recompute_and_replace_match_results(conn, slate_id)
    before = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert len(before) == 1

    import src.ingestion.odds_matching_service as svc

    def _boom(*_args, **_kwargs):
        raise RuntimeError("forced apply failure for rollback test")

    monkeypatch.setattr(svc, "_apply_overrides_unlocked", _boom)

    with pytest.raises(RuntimeError, match="forced apply failure"):
        recompute_and_replace_match_results(conn, slate_id)

    # Prior persisted result survives — replace was rolled back with apply.
    after = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert after == before
