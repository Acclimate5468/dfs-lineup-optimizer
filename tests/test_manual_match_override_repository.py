"""Tests for ``ManualMatchOverrideRepository``.

Covers the Phase D.0 read side (``list_active_for_slate``) and the
Phase D.1 write side (``add_override`` for ``reject_match`` only). The
read-side tests still seed rows via raw SQL because override types
other than ``reject_match`` have no public write API yet — that's
deliberate, not a gap.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.db.repositories import (
    ManualMatchOverrideRecord,
    ManualMatchOverrideRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema


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
    conn: sqlite3.Connection, *, slate_id: int, name: str
) -> int:
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, ?)",
        (int(slate_id), name, 8000, "active"),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_override(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    override_type: str,
    odds_row_key: str | None = None,
    fighter_id: int | None = None,
    payload_json: str | None = None,
    reason: str | None = None,
    created_at: str | None = None,
    superseded_at: str | None = None,
) -> int:
    if created_at is None:
        cur = conn.execute(
            "INSERT INTO manual_match_overrides "
            "(slate_id, odds_row_key, fighter_id, override_type, "
            " payload_json, reason, superseded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                int(slate_id),
                odds_row_key,
                fighter_id,
                override_type,
                payload_json,
                reason,
                superseded_at,
            ),
        )
    else:
        cur = conn.execute(
            "INSERT INTO manual_match_overrides "
            "(slate_id, odds_row_key, fighter_id, override_type, "
            " payload_json, reason, created_at, superseded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(slate_id),
                odds_row_key,
                fighter_id,
                override_type,
                payload_json,
                reason,
                created_at,
                superseded_at,
            ),
        )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Empty slate
# ---------------------------------------------------------------------------


def test_list_active_for_slate_empty(conn, slate_id):
    assert ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id) == []


def test_list_active_for_slate_with_no_matching_slate_rows(
    conn, slate_id, other_slate_id
):
    fid = _insert_fighter(conn, slate_id=other_slate_id, name="Jose Aldo")
    _insert_override(
        conn,
        slate_id=other_slate_id,
        override_type="accept_match",
        fighter_id=fid,
        odds_row_key="key-1",
    )
    assert ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id) == []


# ---------------------------------------------------------------------------
# Active filter
# ---------------------------------------------------------------------------


def test_active_overrides_returned(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_override(
        conn,
        slate_id=slate_id,
        override_type="accept_match",
        fighter_id=fid,
        odds_row_key="key-1",
        reason="looks right",
    )
    [rec] = ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id)
    assert isinstance(rec, ManualMatchOverrideRecord)
    assert rec.slate_id == slate_id
    assert rec.fighter_id == fid
    assert rec.odds_row_key == "key-1"
    assert rec.override_type == "accept_match"
    assert rec.reason == "looks right"
    assert rec.superseded_at is None


def test_superseded_overrides_excluded(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_override(
        conn,
        slate_id=slate_id,
        override_type="reject_match",
        fighter_id=fid,
        odds_row_key="key-old",
        superseded_at="2026-05-20T00:00:00Z",
    )
    _insert_override(
        conn,
        slate_id=slate_id,
        override_type="accept_match",
        fighter_id=fid,
        odds_row_key="key-new",
    )

    listed = ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id)
    assert len(listed) == 1
    assert listed[0].odds_row_key == "key-new"
    assert listed[0].override_type == "accept_match"


def test_only_superseded_overrides_returns_empty(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_override(
        conn,
        slate_id=slate_id,
        override_type="reject_match",
        fighter_id=fid,
        odds_row_key="key-1",
        superseded_at="2026-05-20T00:00:00Z",
    )
    assert ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id) == []


# ---------------------------------------------------------------------------
# Slate scoping
# ---------------------------------------------------------------------------


def test_slate_scoping(conn, slate_id, other_slate_id):
    fid_a = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    fid_b = _insert_fighter(conn, slate_id=other_slate_id, name="Marlon Vera")
    _insert_override(
        conn,
        slate_id=slate_id,
        override_type="accept_match",
        fighter_id=fid_a,
        odds_row_key="key-a",
    )
    _insert_override(
        conn,
        slate_id=other_slate_id,
        override_type="accept_match",
        fighter_id=fid_b,
        odds_row_key="key-b",
    )

    repo = ManualMatchOverrideRepository(conn)
    s1 = repo.list_active_for_slate(slate_id)
    s2 = repo.list_active_for_slate(other_slate_id)
    assert [r.odds_row_key for r in s1] == ["key-a"]
    assert [r.odds_row_key for r in s2] == ["key-b"]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_deterministic_ordering_by_created_at_then_id(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    # Insert out-of-order created_at timestamps; tie on a shared timestamp
    # is broken by id ASC.
    id_late = _insert_override(
        conn,
        slate_id=slate_id,
        override_type="accept_match",
        fighter_id=fid,
        odds_row_key="key-late",
        created_at="2026-05-20T00:02:00Z",
    )
    id_early_a = _insert_override(
        conn,
        slate_id=slate_id,
        override_type="accept_match",
        fighter_id=fid,
        odds_row_key="key-early-a",
        created_at="2026-05-20T00:00:00Z",
    )
    id_tie_b = _insert_override(
        conn,
        slate_id=slate_id,
        override_type="accept_match",
        fighter_id=fid,
        odds_row_key="key-tie-b",
        created_at="2026-05-20T00:01:00Z",
    )
    id_tie_a = _insert_override(
        conn,
        slate_id=slate_id,
        override_type="accept_match",
        fighter_id=fid,
        odds_row_key="key-tie-a",
        created_at="2026-05-20T00:01:00Z",
    )

    listed = ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id)
    # created_at ASC, then id ASC for the two rows that share a timestamp.
    assert [r.id for r in listed] == [id_early_a, id_tie_b, id_tie_a, id_late]


def test_ordering_is_stable_across_calls(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    for k in ("a", "b", "c"):
        _insert_override(
            conn,
            slate_id=slate_id,
            override_type="accept_match",
            fighter_id=fid,
            odds_row_key=f"key-{k}",
            created_at="2026-05-20T00:00:00Z",
        )
    repo = ManualMatchOverrideRepository(conn)
    assert repo.list_active_for_slate(slate_id) == repo.list_active_for_slate(
        slate_id
    )


# ---------------------------------------------------------------------------
# Nullable fields round-trip
# ---------------------------------------------------------------------------


def test_nullable_odds_row_key_round_trips(conn, slate_id):
    """Fighter-scoped overrides (e.g. ``mark_excluded``) carry no
    ``odds_row_key`` — design §5.3."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_override(
        conn,
        slate_id=slate_id,
        override_type="mark_excluded",
        fighter_id=fid,
        odds_row_key=None,
    )
    [rec] = ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id)
    assert rec.odds_row_key is None
    assert rec.fighter_id == fid


def test_nullable_fighter_id_round_trips(conn, slate_id):
    """Row-scoped overrides may carry no ``fighter_id`` — design §6.3."""
    _insert_override(
        conn,
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key="key-1",
        fighter_id=None,
    )
    [rec] = ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id)
    assert rec.fighter_id is None
    assert rec.odds_row_key == "key-1"


def test_nullable_payload_and_reason_round_trip(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _insert_override(
        conn,
        slate_id=slate_id,
        override_type="accept_match",
        fighter_id=fid,
        odds_row_key="key-1",
        payload_json=None,
        reason=None,
    )
    [rec] = ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id)
    assert rec.payload_json is None
    assert rec.reason is None


def test_payload_and_reason_text_round_trip(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    payload = '{"moneyline": -150}'
    _insert_override(
        conn,
        slate_id=slate_id,
        override_type="manual_moneyline",
        fighter_id=fid,
        odds_row_key=None,
        payload_json=payload,
        reason="line moved",
    )
    [rec] = ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id)
    assert rec.payload_json == payload
    assert rec.reason == "line moved"
    assert rec.override_type == "manual_moneyline"


# ===========================================================================
# Phase D.1 — add_override for reject_match
# ===========================================================================


def _save_odds_row(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    fighter_name_raw: str = "Jose Aldo",
    american_odds: int = -150,
    source: str = "manual",
    captured_at: str = "2026-05-20T00:00:00Z",
):
    return OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source=source,
        captured_at=captured_at,
    )


def test_add_override_creates_reject_match(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")

    rec = ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
        reason="ambiguous fuzzy candidate",
    )

    assert isinstance(rec, ManualMatchOverrideRecord)
    assert rec.id > 0
    assert rec.slate_id == slate_id
    assert rec.odds_row_key == row.odds_row_key
    assert rec.fighter_id == fid
    assert rec.override_type == "reject_match"
    assert rec.payload_json is None
    assert rec.reason == "ambiguous fuzzy candidate"
    assert rec.superseded_at is None
    assert rec.created_at  # populated by the column default

    # Also visible to the read side.
    [active] = ManualMatchOverrideRepository(conn).list_active_for_slate(
        slate_id
    )
    assert active.id == rec.id


def test_add_override_empty_reason_normalized_to_none(conn, slate_id):
    row = _save_odds_row(conn, slate_id=slate_id)
    repo = ManualMatchOverrideRepository(conn)

    rec_blank = repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        reason="",
    )
    assert rec_blank.reason is None

    # Whitespace-only also normalizes to None (and supersedes the previous
    # active reject on the same key).
    rec_ws = repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        reason="   \t  ",
    )
    assert rec_ws.reason is None


# D.5.1: ``accept_match`` / ``force_pair`` are now supported (covered by the
# positive insert tests below); the remaining types stay unimplemented.
@pytest.mark.parametrize(
    "bad_type",
    [
        "mark_excluded",
        "manual_moneyline",
        "manual_projection_low_confidence",
        "REJECT_MATCH",
        "reject",
        "",
        "totally_unknown_type",
    ],
)
def test_add_override_rejects_unsupported_type(conn, slate_id, bad_type):
    row = _save_odds_row(conn, slate_id=slate_id)
    with pytest.raises(NotImplementedError):
        ManualMatchOverrideRepository(conn).add_override(
            slate_id=slate_id,
            override_type=bad_type,
            odds_row_key=row.odds_row_key,
        )


def test_add_override_rejects_missing_odds_row_key(conn, slate_id):
    repo = ManualMatchOverrideRepository(conn)
    for bad_key in (None, "", "   "):
        with pytest.raises(ValueError, match="odds_row_key"):
            repo.add_override(
                slate_id=slate_id,
                override_type="reject_match",
                odds_row_key=bad_key,
            )


def test_add_override_rejects_odds_row_key_not_in_slate(
    conn, slate_id, other_slate_id
):
    # odds_row lives on a different slate.
    other_row = _save_odds_row(conn, slate_id=other_slate_id)
    with pytest.raises(ValueError, match="not found"):
        ManualMatchOverrideRepository(conn).add_override(
            slate_id=slate_id,
            override_type="reject_match",
            odds_row_key=other_row.odds_row_key,
        )

    # Made-up key never inserted into any odds_rows row.
    with pytest.raises(ValueError, match="not found"):
        ManualMatchOverrideRepository(conn).add_override(
            slate_id=slate_id,
            override_type="reject_match",
            odds_row_key="not-a-real-key",
        )


def test_add_override_rejects_fighter_from_another_slate(
    conn, slate_id, other_slate_id
):
    row = _save_odds_row(conn, slate_id=slate_id)
    other_fid = _insert_fighter(
        conn, slate_id=other_slate_id, name="Marlon Vera"
    )

    with pytest.raises(ValueError, match="slate"):
        ManualMatchOverrideRepository(conn).add_override(
            slate_id=slate_id,
            override_type="reject_match",
            odds_row_key=row.odds_row_key,
            fighter_id=other_fid,
        )


def test_add_override_rejects_nonexistent_slate(conn):
    # No slate / odds_row exist yet; key is irrelevant — slate check fires
    # before odds_row lookup.
    with pytest.raises(ValueError, match="slate"):
        ManualMatchOverrideRepository(conn).add_override(
            slate_id=999_999,
            override_type="reject_match",
            odds_row_key="any-key",
        )


def test_add_override_rejects_nonexistent_fighter(conn, slate_id):
    row = _save_odds_row(conn, slate_id=slate_id)
    with pytest.raises(ValueError, match="fighter"):
        ManualMatchOverrideRepository(conn).add_override(
            slate_id=slate_id,
            override_type="reject_match",
            odds_row_key=row.odds_row_key,
            fighter_id=999_999,
        )


def test_add_override_rejects_non_empty_payload(conn, slate_id):
    row = _save_odds_row(conn, slate_id=slate_id)
    repo = ManualMatchOverrideRepository(conn)
    for bad_payload in ({"moneyline": -150}, {"x": 1}, "not-a-dict", [1, 2]):
        with pytest.raises(ValueError, match="payload"):
            repo.add_override(
                slate_id=slate_id,
                override_type="reject_match",
                odds_row_key=row.odds_row_key,
                payload=bad_payload,
            )


def test_add_override_allows_fighter_id_none(conn, slate_id):
    row = _save_odds_row(conn, slate_id=slate_id)
    rec = ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=None,
    )
    assert rec.fighter_id is None
    assert rec.odds_row_key == row.odds_row_key
    assert rec.superseded_at is None


def test_add_override_allows_empty_dict_payload(conn, slate_id):
    """An explicit ``payload={}`` is treated as "no payload" — same as
    ``None`` — and the column is stored as NULL."""
    row = _save_odds_row(conn, slate_id=slate_id)
    rec = ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        payload={},
    )
    assert rec.payload_json is None


def test_add_override_supersedes_prior_active_reject_match(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = ManualMatchOverrideRepository(conn)

    first = repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
        reason="first",
    )
    second = repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
        reason="second",
    )

    persisted = conn.execute(
        "SELECT id, reason, superseded_at FROM manual_match_overrides "
        "WHERE slate_id = ? ORDER BY id ASC",
        (slate_id,),
    ).fetchall()
    assert len(persisted) == 2
    # First row was superseded by the second insert.
    assert persisted[0][0] == first.id
    assert persisted[0][1] == "first"
    assert persisted[0][2] is not None
    # Second row is the new active row.
    assert persisted[1][0] == second.id
    assert persisted[1][1] == "second"
    assert persisted[1][2] is None


def test_add_override_does_not_supersede_reject_on_another_slate(
    conn, slate_id, other_slate_id
):
    row_a = _save_odds_row(conn, slate_id=slate_id)
    row_b = _save_odds_row(conn, slate_id=other_slate_id)

    repo = ManualMatchOverrideRepository(conn)
    other_rec = repo.add_override(
        slate_id=other_slate_id,
        override_type="reject_match",
        odds_row_key=row_b.odds_row_key,
    )
    # Same odds_row_key string in both slates (raw inputs hashed
    # identically), but the supersession scope is per-slate.
    assert other_rec.odds_row_key == row_a.odds_row_key
    repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row_a.odds_row_key,
    )

    still_active = conn.execute(
        "SELECT superseded_at FROM manual_match_overrides WHERE id = ?",
        (other_rec.id,),
    ).fetchone()
    assert still_active[0] is None


def test_reject_supersedes_resolution_set_but_not_fighter_scoped_types(
    conn, slate_id
):
    """§16.4: the resolution set (``reject_match`` / ``accept_match`` /
    ``force_pair``) is mutually exclusive on one ``odds_row_key`` — a
    ``reject_match`` write supersedes the active accept/force_pair on that
    key. Fighter-scoped types (``mark_excluded`` / ``manual_moneyline``,
    ``odds_row_key`` NULL) are NOT in the resolution set and are left
    untouched."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")

    # Resolution-set siblings on the same key (raw SQL — exercise the
    # supersession scope, not the write API's own validation).
    accept_id = _insert_override(
        conn,
        slate_id=slate_id,
        override_type="accept_match",
        fighter_id=fid,
        odds_row_key=row.odds_row_key,
    )
    force_id = _insert_override(
        conn,
        slate_id=slate_id,
        override_type="force_pair",
        fighter_id=fid,
        odds_row_key=row.odds_row_key,
    )
    # Fighter-scoped siblings (not in the resolution set).
    exclude_id = _insert_override(
        conn,
        slate_id=slate_id,
        override_type="mark_excluded",
        fighter_id=fid,
        odds_row_key=None,
    )
    moneyline_id = _insert_override(
        conn,
        slate_id=slate_id,
        override_type="manual_moneyline",
        fighter_id=fid,
        odds_row_key=None,
        payload_json='{"moneyline": -150}',
    )

    ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    sibling_states = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT id, superseded_at FROM manual_match_overrides "
            "WHERE id IN (?, ?, ?, ?)",
            (accept_id, force_id, exclude_id, moneyline_id),
        ).fetchall()
    }
    # Resolution-set rows on the key are superseded by the reject.
    assert sibling_states[accept_id] is not None
    assert sibling_states[force_id] is not None
    # Fighter-scoped types are untouched.
    assert sibling_states[exclude_id] is None
    assert sibling_states[moneyline_id] is None


def test_add_override_invalid_input_does_not_supersede_prior_active_row(
    conn, slate_id, other_slate_id
):
    """Pre-DB validation must fire before any UPDATE — a bad input cannot
    silently wipe the active reject on the row."""
    row = _save_odds_row(conn, slate_id=slate_id)
    other_fid = _insert_fighter(
        conn, slate_id=other_slate_id, name="Marlon Vera"
    )

    repo = ManualMatchOverrideRepository(conn)
    first = repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        reason="initial reject",
    )

    # Same scope, but fighter_id belongs to a different slate → ValueError.
    with pytest.raises(ValueError):
        repo.add_override(
            slate_id=slate_id,
            override_type="reject_match",
            odds_row_key=row.odds_row_key,
            fighter_id=other_fid,
            reason="should not land",
        )

    persisted = conn.execute(
        "SELECT id, reason, superseded_at FROM manual_match_overrides "
        "WHERE slate_id = ?",
        (slate_id,),
    ).fetchall()
    assert len(persisted) == 1
    assert persisted[0][0] == first.id
    assert persisted[0][1] == "initial reject"
    assert persisted[0][2] is None  # still active


def test_list_active_for_slate_shows_only_current_active_after_supersession(
    conn, slate_id
):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = ManualMatchOverrideRepository(conn)

    repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
        reason="first",
    )
    repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
        reason="second",
    )
    second_then_third = repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
        reason="third",
    )

    active = repo.list_active_for_slate(slate_id)
    assert len(active) == 1
    assert active[0].id == second_then_third.id
    assert active[0].reason == "third"
    assert active[0].superseded_at is None


# ===========================================================================
# Phase D.2 — Verification path: round-trip through Odds page display shape
# ===========================================================================
#
# The Odds page (`app/pages/03_odds.py`, Active Manual Match Overrides
# panel) renders each `ManualMatchOverrideRecord` via
# `_active_override_display_rows`. That helper lives in a Streamlit page
# module whose filename starts with a digit — it cannot be `import`-ed
# conventionally, and even if it could, importing the page triggers
# `st.set_page_config(...)` and other render-time side effects at module
# load. The projection is therefore mirrored 1:1 below, anchored back to
# its source so any future change to the page must update this helper
# in lock-step.


def _project_active_override_for_display(rec: ManualMatchOverrideRecord) -> dict:
    """Mirror of `_active_override_display_rows` in `app/pages/03_odds.py`.

    Returns the per-row dict the panel hands to `pd.DataFrame(...)`. If
    the page's projection ever gains, renames, or drops a key, update
    this helper and the verification tests below together.
    """
    return {
        "override_type": rec.override_type,
        "odds_row_key": rec.odds_row_key or "",
        "fighter_id": ("" if rec.fighter_id is None else rec.fighter_id),
        "payload_json": rec.payload_json or "",
        "reason": rec.reason or "",
        "created_at": rec.created_at,
    }


def test_active_override_display_projection_pure_edge_cases():
    """Pure-projection coverage: every nullable record field maps to an
    empty string in the display dict, non-null `fighter_id` stays an
    `int`, and `created_at` passes through verbatim."""
    populated = ManualMatchOverrideRecord(
        id=1,
        slate_id=42,
        odds_row_key="abc",
        fighter_id=7,
        override_type="reject_match",
        payload_json=None,
        reason="ambiguous",
        created_at="2026-05-20T00:00:00",
        superseded_at=None,
    )
    assert _project_active_override_for_display(populated) == {
        "override_type": "reject_match",
        "odds_row_key": "abc",
        "fighter_id": 7,
        "payload_json": "",
        "reason": "ambiguous",
        "created_at": "2026-05-20T00:00:00",
    }

    all_null = ManualMatchOverrideRecord(
        id=2,
        slate_id=42,
        odds_row_key=None,
        fighter_id=None,
        override_type="reject_match",
        payload_json=None,
        reason=None,
        created_at="2026-05-20T00:00:01",
        superseded_at=None,
    )
    assert _project_active_override_for_display(all_null) == {
        "override_type": "reject_match",
        "odds_row_key": "",
        "fighter_id": "",
        "payload_json": "",
        "reason": "",
        "created_at": "2026-05-20T00:00:01",
    }


def test_repo_round_trip_projects_to_odds_page_display_shape(conn, slate_id):
    """Insert via `add_override`, read via `list_active_for_slate`, then
    project into the same dict shape the Active Overrides panel renders.
    This is the path the page walks at render time today."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")

    repo = ManualMatchOverrideRepository(conn)
    inserted = repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
        reason="ambiguous fuzzy candidate",
    )

    [active] = repo.list_active_for_slate(slate_id)
    assert active.id == inserted.id

    display = _project_active_override_for_display(active)
    # Exact key set the panel renders — no extras, no missing.
    assert set(display.keys()) == {
        "override_type",
        "odds_row_key",
        "fighter_id",
        "payload_json",
        "reason",
        "created_at",
    }
    assert display["override_type"] == "reject_match"
    assert display["odds_row_key"] == row.odds_row_key
    assert display["fighter_id"] == fid
    assert isinstance(display["fighter_id"], int)
    assert display["payload_json"] == ""
    assert display["reason"] == "ambiguous fuzzy candidate"
    assert isinstance(display["created_at"], str) and display["created_at"]


def test_repo_round_trip_handles_nullable_fighter_id_and_reason(
    conn, slate_id
):
    """Reject inserted without `fighter_id` and without `reason` projects
    to empty strings — no `None` leaks into the panel's pandas frame."""
    row = _save_odds_row(conn, slate_id=slate_id)
    repo = ManualMatchOverrideRepository(conn)
    repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
    )

    [active] = repo.list_active_for_slate(slate_id)
    display = _project_active_override_for_display(active)
    assert display["fighter_id"] == ""
    assert display["reason"] == ""
    assert display["payload_json"] == ""
    assert display["odds_row_key"] == row.odds_row_key
    assert display["override_type"] == "reject_match"


def test_repo_round_trip_supersession_shows_only_latest_in_display_shape(
    conn, slate_id
):
    """Two rejects on the same (slate, odds_row_key) — the read API +
    page projection together surface only the latest active row, with
    the new reason and a `created_at` from the second write."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")
    repo = ManualMatchOverrideRepository(conn)

    first = repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
        reason="first attempt",
    )
    second = repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
        reason="revised reason",
    )
    assert first.id != second.id

    active = repo.list_active_for_slate(slate_id)
    displays = [_project_active_override_for_display(r) for r in active]
    assert displays == [
        {
            "override_type": "reject_match",
            "odds_row_key": row.odds_row_key,
            "fighter_id": fid,
            "payload_json": "",
            "reason": "revised reason",
            "created_at": active[0].created_at,
        }
    ]


# ===========================================================================
# Phase D.5.1 — accept_match / force_pair binding inserts (§16.4 / §16.11)
# ===========================================================================


def _seed_auto_match_result(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    fighter_id: int,
    fighter_name_raw: str,
    captured_at: str = "2026-05-20T02:00:00Z",
):
    """Persist one ``auto_match`` ``odds_match_results`` row for a fighter
    on its own odds row (used to exercise the already-bound check)."""
    row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        captured_at=captured_at,
    )
    conn.execute(
        "INSERT INTO odds_match_results "
        "(slate_id, odds_row_id, odds_row_key, fighter_id, match_status, "
        " match_stage, match_score, opponent_check, effective_status) "
        "VALUES (?, ?, ?, ?, 'auto_match', 'exact_conservative', 100, "
        " 'not_applicable', 'auto_match')",
        (int(slate_id), row.id, row.odds_row_key, int(fighter_id)),
    )
    conn.commit()
    return row


def test_add_override_creates_accept_match(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="Jose Aldo")

    rec = ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="accept_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
        reason="confirmed the fuzzy match",
    )

    assert rec.override_type == "accept_match"
    assert rec.fighter_id == fid
    assert rec.odds_row_key == row.odds_row_key
    assert rec.payload_json is None
    assert rec.reason == "confirmed the fuzzy match"
    assert rec.superseded_at is None
    [active] = ManualMatchOverrideRepository(conn).list_active_for_slate(
        slate_id
    )
    assert active.id == rec.id


def test_add_override_creates_force_pair(conn, slate_id):
    fid = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    row = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Bruno Gustavo da Silva"
    )

    rec = ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    assert rec.override_type == "force_pair"
    assert rec.fighter_id == fid
    assert rec.superseded_at is None


def test_binding_override_requires_fighter_id(conn, slate_id):
    row = _save_odds_row(conn, slate_id=slate_id)
    repo = ManualMatchOverrideRepository(conn)
    for ot in ("accept_match", "force_pair"):
        with pytest.raises(ValueError, match="fighter_id is required"):
            repo.add_override(
                slate_id=slate_id,
                override_type=ot,
                odds_row_key=row.odds_row_key,
                fighter_id=None,
            )


def test_binding_override_rejects_inactive_fighter(conn, slate_id):
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, ?)",
        (slate_id, "Inactive Guy", 8000, "excluded"),
    )
    conn.commit()
    inactive_fid = int(cur.lastrowid)
    row = _save_odds_row(conn, slate_id=slate_id)

    with pytest.raises(ValueError, match="not active"):
        ManualMatchOverrideRepository(conn).add_override(
            slate_id=slate_id,
            override_type="force_pair",
            odds_row_key=row.odds_row_key,
            fighter_id=inactive_fid,
        )


def test_binding_override_rejects_wrong_slate_fighter(
    conn, slate_id, other_slate_id
):
    row = _save_odds_row(conn, slate_id=slate_id)
    other_fid = _insert_fighter(
        conn, slate_id=other_slate_id, name="Marlon Vera"
    )
    with pytest.raises(ValueError, match="slate"):
        ManualMatchOverrideRepository(conn).add_override(
            slate_id=slate_id,
            override_type="accept_match",
            odds_row_key=row.odds_row_key,
            fighter_id=other_fid,
        )


def test_binding_override_rejects_already_auto_matched_fighter(conn, slate_id):
    """§16.11 clause (a): a fighter with an active ``auto_match`` result row
    on another key cannot take a second binding."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Jose Aldo")
    _seed_auto_match_result(
        conn, slate_id=slate_id, fighter_id=fid, fighter_name_raw="Jose Aldo"
    )
    # A different odds row the user tries to force-pair to the same fighter.
    other_row = _save_odds_row(
        conn,
        slate_id=slate_id,
        fighter_name_raw="J. Aldo (alt feed)",
        captured_at="2026-05-20T03:00:00Z",
    )

    with pytest.raises(ValueError, match="already auto-matched"):
        ManualMatchOverrideRepository(conn).add_override(
            slate_id=slate_id,
            override_type="force_pair",
            odds_row_key=other_row.odds_row_key,
            fighter_id=fid,
        )


def test_binding_override_rejects_already_bound_via_other_override(
    conn, slate_id
):
    """§16.11 clause (b): a fighter already bound by an active accept/force
    override on another key cannot take a second binding."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    row_a = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="Bruno G da Silva",
        captured_at="2026-05-20T00:00:00Z",
    )
    row_b = _save_odds_row(
        conn, slate_id=slate_id, fighter_name_raw="B. Silva (alt)",
        captured_at="2026-05-20T00:01:00Z",
    )
    repo = ManualMatchOverrideRepository(conn)
    repo.add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row_a.odds_row_key,
        fighter_id=fid,
    )

    with pytest.raises(ValueError, match="already bound"):
        repo.add_override(
            slate_id=slate_id,
            override_type="accept_match",
            odds_row_key=row_b.odds_row_key,
            fighter_id=fid,
        )


def test_binding_override_idempotent_same_key_same_fighter(conn, slate_id):
    """Re-binding the same fighter to the SAME key is allowed; it supersedes
    the prior identical binding (§16.11 idempotent row)."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="B Silva")
    repo = ManualMatchOverrideRepository(conn)

    first = repo.add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )
    second = repo.add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    persisted = conn.execute(
        "SELECT id, superseded_at FROM manual_match_overrides "
        "WHERE slate_id = ? ORDER BY id ASC",
        (slate_id,),
    ).fetchall()
    assert len(persisted) == 2
    assert persisted[0][0] == first.id and persisted[0][1] is not None
    assert persisted[1][0] == second.id and persisted[1][1] is None
    [active] = repo.list_active_for_slate(slate_id)
    assert active.id == second.id


def test_rebind_different_fighter_same_key_supersedes_prior(conn, slate_id):
    """Assigning a different fighter to the same key supersedes the prior
    binding (the prior fighter's only binding was on this key)."""
    fid_a = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    fid_b = _insert_fighter(conn, slate_id=slate_id, name="Bruno Souza")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="B Silva")
    repo = ManualMatchOverrideRepository(conn)

    first = repo.add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=fid_a,
    )
    second = repo.add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=fid_b,
    )

    [active] = repo.list_active_for_slate(slate_id)
    assert active.id == second.id
    assert active.fighter_id == fid_b
    superseded = conn.execute(
        "SELECT superseded_at FROM manual_match_overrides WHERE id = ?",
        (first.id,),
    ).fetchone()
    assert superseded[0] is not None


def test_assign_supersedes_active_reject_on_key(conn, slate_id):
    """§16.4 recovery path: a force_pair on a previously-rejected key
    supersedes the active reject."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="B Silva")
    repo = ManualMatchOverrideRepository(conn)

    rejected = repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )
    assigned = repo.add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    [active] = repo.list_active_for_slate(slate_id)
    assert active.id == assigned.id
    assert active.override_type == "force_pair"
    superseded = conn.execute(
        "SELECT superseded_at FROM manual_match_overrides WHERE id = ?",
        (rejected.id,),
    ).fetchone()
    assert superseded[0] is not None


def test_reject_supersedes_active_assign_on_key(conn, slate_id):
    """§16.4: a later reject_match supersedes an active force_pair on the
    same key."""
    fid = _insert_fighter(conn, slate_id=slate_id, name="Bruno Silva")
    row = _save_odds_row(conn, slate_id=slate_id, fighter_name_raw="B Silva")
    repo = ManualMatchOverrideRepository(conn)

    assigned = repo.add_override(
        slate_id=slate_id,
        override_type="force_pair",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )
    rejected = repo.add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=row.odds_row_key,
        fighter_id=fid,
    )

    [active] = repo.list_active_for_slate(slate_id)
    assert active.id == rejected.id
    assert active.override_type == "reject_match"
    superseded = conn.execute(
        "SELECT superseded_at FROM manual_match_overrides WHERE id = ?",
        (assigned.id,),
    ).fetchone()
    assert superseded[0] is not None
