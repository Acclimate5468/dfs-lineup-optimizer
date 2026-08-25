"""Tests for the odds/news snapshot → ``odds_rows`` save service (slice S5a).

Covers ``src/ingestion/snapshot_odds_save.save_snapshot_odds_to_slate``: the
append-only, moneyline-only write path described in
``docs/ODDS_NEWS_SNAPSHOT_PERSISTENCE_DESIGN.md`` §2, §4, §5, §14 (S5a).

Pins the S5a contracts:
  - valid moneylines become ``source="snapshot:<slug>"`` rows;
  - identical re-save is idempotent (no duplicates);
  - match results are recomputed after the save;
  - manual / CSV odds rows are never deleted or replaced;
  - an active manual override survives an idempotent re-save (and is applied);
  - hard validation errors block the save (raise, write nothing);
  - warnings do NOT block the save;
  - the single-snapshot-per-slate guard blocks a different/fresher snapshot;
  - news flags, props, and line movement are NOT persisted.

All fixtures are synthetic — no real feed file is read or committed
(``docs/DEVELOPMENT_NOTES.md`` §7 / §8).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import pytest

from src.collection.odds_news_snapshot import validate_snapshot_text
from src.db.repositories import (
    ManualMatchOverrideRepository,
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.manual_odds import save_manual_odds_entries
from src.ingestion.odds_csv_save import save_csv_odds_rows
from src.ingestion.snapshot_odds_save import (
    SnapshotOddsSaveResult,
    is_snapshot_source,
    save_snapshot_odds_to_slate,
    source_label_for,
)

# Fixed wall-clock reference so staleness is deterministic across runs.
FIXED_NOW = datetime(2026, 5, 20, 18, 0, 0, tzinfo=timezone.utc)
COLLECTED_AT = "2026-05-20T12:00:00Z"  # 6h before FIXED_NOW → not stale
EVENT_DATE = "2026-05-21"
EVENT_NAME = "UFC 999: Alpha vs Beta"
EXPECTED_SOURCE = "snapshot:ufc-999-alpha-vs-beta"


# ---------------------------------------------------------------------------
# Fixtures + builders
# ---------------------------------------------------------------------------


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


def _seed_active_fighters(conn: sqlite3.Connection, slate_id: int, names: list[str]) -> None:
    """Seed ACTIVE DK fighters so the chained recompute can run."""
    for i, name in enumerate(names):
        conn.execute(
            "INSERT INTO fighters (slate_id, name, salary, status) "
            "VALUES (?, ?, ?, 'active')",
            (slate_id, name, 8000 + i),
        )
    conn.commit()


def _two_sided_entries() -> list[dict]:
    return [
        {
            "fighter_name": "Alpha Fighter",
            "opponent_name": "Beta Fighter",
            "moneyline": -180,
            "book": "Example Book",
        },
        {
            "fighter_name": "Beta Fighter",
            "opponent_name": "Alpha Fighter",
            "moneyline": 160,
            "book": "Example Book",
        },
    ]


def _doc(*, collected_at: str = COLLECTED_AT, entries: list[dict] | None = None,
         event: str = EVENT_NAME) -> dict:
    return {
        "schema_version": 1,
        "snapshot_kind": "odds_news",
        "event": {"name": event, "date": EVENT_DATE},
        "collected_at": collected_at,
        "collected_by": {"method": "manual"},
        "sources_checked": [{"name": "Example Book"}],
        "entries": _two_sided_entries() if entries is None else entries,
    }


def _report(doc: dict, *, now: datetime = FIXED_NOW):
    return validate_snapshot_text(json.dumps(doc), now=now)


# ---------------------------------------------------------------------------
# Happy path + source labelling
# ---------------------------------------------------------------------------


def test_saves_moneylines_with_snapshot_source_label(conn, slate_id):
    report = _report(_doc())
    assert report.is_valid and not report.warnings

    result = save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=report)

    assert isinstance(result, SnapshotOddsSaveResult)
    assert result.blocked is False
    assert result.saved_count == 2
    assert result.existing_count == 0
    assert result.source_label == EXPECTED_SOURCE
    assert result.import_batch_id.startswith("snap-")

    stored = OddsRowRepository(conn).list_for_slate(slate_id)
    assert {r.fighter_name_raw for r in stored} == {"Alpha Fighter", "Beta Fighter"}
    assert {r.source for r in stored} == {EXPECTED_SOURCE}
    assert all(is_snapshot_source(r.source) for r in stored)
    assert {r.import_batch_id for r in stored} == {result.import_batch_id}
    # implied_probability is app-derived from the moneyline, not the snapshot.
    alpha = next(r for r in stored if r.fighter_name_raw == "Alpha Fighter")
    assert alpha.american_odds == -180
    assert alpha.implied_probability == pytest.approx(180 / 280, abs=1e-9)
    assert alpha.bookmaker == "Example Book"
    assert alpha.opponent_name_raw == "Beta Fighter"


def test_source_label_for_slugifies_event(conn, slate_id):
    report = _report(_doc())
    assert source_label_for(report) == EXPECTED_SOURCE


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_identical_resave_is_idempotent(conn, slate_id):
    report = _report(_doc())
    first = save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=report)
    second = save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=_report(_doc()))

    assert first.saved_count == 2 and first.existing_count == 0
    assert second.blocked is False
    assert second.saved_count == 0 and second.existing_count == 2
    assert first.import_batch_id == second.import_batch_id
    # Only two physical rows total — no duplicates.
    assert len(OddsRowRepository(conn).list_for_slate(slate_id)) == 2


# ---------------------------------------------------------------------------
# Recompute after save
# ---------------------------------------------------------------------------


def test_recompute_runs_after_save(conn, slate_id):
    _seed_active_fighters(conn, slate_id, ["Alpha Fighter", "Beta Fighter"])
    report = _report(_doc())

    result = save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=report)

    assert result.recompute is not None
    assert result.recompute_error is None
    assert result.recompute.total == 2
    # Match results were materialized from the just-saved snapshot rows.
    persisted = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    assert len(persisted) == 2


def test_save_succeeds_when_recompute_has_no_fighters(conn, slate_id):
    """No active DK roster yet → odds rows still save; recompute is deferred."""
    report = _report(_doc())
    result = save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=report)

    assert result.saved_count == 2
    assert result.recompute is None
    assert result.recompute_error is not None
    assert "no active DK fighters" in result.recompute_error
    # The odds rows are durable despite the deferred recompute.
    assert len(OddsRowRepository(conn).list_for_slate(slate_id)) == 2


# ---------------------------------------------------------------------------
# Manual / CSV preservation
# ---------------------------------------------------------------------------


def test_manual_and_csv_rows_are_not_deleted_or_replaced(conn, slate_id):
    _seed_active_fighters(conn, slate_id, ["Alpha Fighter", "Beta Fighter"])
    repo = OddsRowRepository(conn)

    save_manual_odds_entries(
        repo,
        slate_id=slate_id,
        entries=[
            {"fighter": "Manual Mike", "moneyline": -120,
             "timestamp": "2026-05-20T10:00:00Z", "opponent": "Manual Opp"}
        ],
    )
    save_csv_odds_rows(
        repo,
        slate_id=slate_id,
        df=pd.DataFrame(
            [{"fighter": "Csv Carl", "moneyline": 200, "source": "oddsapi",
              "timestamp": "2026-05-20T11:00:00Z"}]
        ),
    )
    before = {r.odds_row_key: r for r in repo.list_for_slate(slate_id)}
    assert {r.source for r in before.values()} == {"manual", "csv:oddsapi"}

    save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=_report(_doc()))

    after = {r.odds_row_key: r for r in repo.list_for_slate(slate_id)}
    # Manual + CSV rows survive byte-for-byte; snapshot rows are added alongside.
    for key, original in before.items():
        assert key in after, f"row {key} ({original.source}) was dropped"
        assert after[key].source == original.source
        assert after[key].american_odds == original.american_odds
    assert sum(1 for r in after.values() if is_snapshot_source(r.source)) == 2
    assert sum(1 for r in after.values() if r.source == "manual") == 1
    assert sum(1 for r in after.values() if r.source == "csv:oddsapi") == 1


# ---------------------------------------------------------------------------
# Manual override preservation
# ---------------------------------------------------------------------------


def test_active_manual_override_survives_resave_and_is_applied(conn, slate_id):
    _seed_active_fighters(conn, slate_id, ["Alpha Fighter", "Beta Fighter"])
    first = save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=_report(_doc()))
    target_key = first.saved[0].odds_row_key

    override = ManualMatchOverrideRepository(conn).add_override(
        slate_id=slate_id,
        override_type="reject_match",
        odds_row_key=target_key,
        reason="manual reject for test",
    )
    assert override.superseded_at is None

    # Idempotent re-save runs recompute, which must REAPPLY (not wipe) the override.
    save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=_report(_doc()))

    active = ManualMatchOverrideRepository(conn).list_active_for_slate(slate_id)
    assert [o.id for o in active] == [override.id]

    results = {r.odds_row_key: r for r in OddsMatchResultRepository(conn).list_for_slate(slate_id)}
    assert results[target_key].effective_status == "review_rejected"


# ---------------------------------------------------------------------------
# Hard errors block; warnings allow
# ---------------------------------------------------------------------------


def test_hard_validation_errors_block_save(conn, slate_id):
    bad = _doc(entries=[
        {"fighter_name": "Bad Line", "opponent_name": "Someone", "moneyline": 0},
        {"fighter_name": "Good Line", "opponent_name": "Bad Line", "moneyline": -150},
    ])
    report = _report(bad)
    assert not report.is_valid

    with pytest.raises(ValueError):
        save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=report)

    # Nothing was written.
    assert OddsRowRepository(conn).list_for_slate(slate_id) == []


def test_warnings_do_not_block_save(conn, slate_id):
    # Advisory implied_probability that disagrees with the derived value →
    # warning, not error. The save must still proceed.
    warned = _doc(entries=[
        {"fighter_name": "Alpha Fighter", "opponent_name": "Beta Fighter",
         "moneyline": -180, "implied_probability": 0.20, "book": "Example Book"},
        {"fighter_name": "Beta Fighter", "opponent_name": "Alpha Fighter",
         "moneyline": 160, "book": "Example Book"},
    ])
    report = _report(warned)
    assert report.is_valid
    assert report.warnings  # at least the implied-probability mismatch

    result = save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=report)
    assert result.blocked is False
    assert result.saved_count == 2
    assert result.warnings  # surfaced for the UI to show, but non-blocking


# ---------------------------------------------------------------------------
# Single-snapshot-per-slate guard
# ---------------------------------------------------------------------------


def test_guard_blocks_a_different_snapshot(conn, slate_id):
    first = save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=_report(_doc()))
    assert first.saved_count == 2

    # A fresher capture of the same event: same source label, new collected_at
    # → different batch id → must be blocked (S5b territory).
    fresher = _report(_doc(collected_at="2026-05-20T17:00:00Z"))
    blocked = save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=fresher)

    assert blocked.blocked is True
    assert blocked.blocked_reason and "different snapshot" in blocked.blocked_reason
    assert blocked.saved_count == 0
    assert blocked.import_batch_id != first.import_batch_id
    # Only the first snapshot's rows remain; nothing from the second was written.
    stored = OddsRowRepository(conn).list_for_slate(slate_id)
    assert len(stored) == 2
    assert {r.import_batch_id for r in stored} == {first.import_batch_id}


def test_guard_allows_first_save_when_only_manual_csv_present(conn, slate_id):
    """Manual / CSV rows must NOT trip the snapshot guard."""
    repo = OddsRowRepository(conn)
    save_manual_odds_entries(
        repo, slate_id=slate_id,
        entries=[{"fighter": "Manual Mike", "moneyline": -120,
                  "timestamp": "2026-05-20T10:00:00Z"}],
    )
    result = save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=_report(_doc()))
    assert result.blocked is False
    assert result.saved_count == 2


# ---------------------------------------------------------------------------
# News / props / line movement are not persisted
# ---------------------------------------------------------------------------


def test_news_props_and_line_movement_are_not_persisted(conn, slate_id):
    entries = [
        {
            "fighter_name": "Alpha Fighter",
            "opponent_name": "Beta Fighter",
            "moneyline": -180,
            "book": "Example Book",
            "line_open": -150,
            "line_current": -180,
            "line_movement": "toward",
            "itd_odds": -110,
            "goes_distance": {"yes": 120, "no": -140},
            "news_flags": ["injury", "short_notice"],
            "news_note": "Tweaked knee in camp.",
        },
        # A news-only entry (no moneyline) must NOT be saved.
        {
            "entry_kind": "news_only",
            "fighter_name": "Beta Fighter",
            "opponent_name": "Alpha Fighter",
            "news_flags": ["weight_miss"],
            "news_note": "Missed weight by 2 lbs.",
        },
    ]
    report = _report(_doc(entries=entries))
    assert report.is_valid

    result = save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=report)

    # Only the moneyline entry is persisted; the news-only entry is skipped.
    assert result.saved_count == 1
    assert any("Beta Fighter" == label for label, _reason in result.skipped)

    stored = OddsRowRepository(conn).list_for_slate(slate_id)
    assert len(stored) == 1
    row = stored[0]
    assert row.fighter_name_raw == "Alpha Fighter"
    assert row.american_odds == -180
    # ``odds_rows`` has no column for news flags / props / line movement, so
    # they cannot be persisted. Confirm none leaked into a serialized field.
    persisted_blob = json.dumps(row.__dict__, default=str).lower()
    for token in ("injury", "short_notice", "weight_miss", "knee", "itd",
                  "goes_distance", "toward", "line_movement"):
        assert token not in persisted_blob, f"{token!r} leaked into odds_rows"


def test_news_only_snapshot_has_no_moneylines_to_save(conn, slate_id):
    news_only = _doc(entries=[
        {"entry_kind": "news_only", "fighter_name": "Alpha Fighter",
         "opponent_name": "Beta Fighter", "news_flags": ["injury"],
         "news_note": "Out with injury."},
    ])
    report = _report(news_only)
    assert report.is_valid

    with pytest.raises(ValueError):
        save_snapshot_odds_to_slate(conn, slate_id=slate_id, report=report)
    assert OddsRowRepository(conn).list_for_slate(slate_id) == []
