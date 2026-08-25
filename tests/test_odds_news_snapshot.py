"""Odds / news snapshot parser + validator tests (design S3).

Covers ``src/collection/odds_news_snapshot.py`` against
``docs/ODDS_NEWS_SNAPSHOT_DESIGN.md`` §3–§4.

All fixtures here are **synthetic and inline** (docs/DEVELOPMENT_NOTES.md §7 / §8). These tests
read no real snapshot file, make no network call, and touch no DB. The
staleness reference time is injected (``NOW``) so every check is deterministic.
"""

import inspect
import json

import pytest

from src.collection import odds_news_snapshot as ons
from src.collection.odds_news_snapshot import (
    DEFAULT_WARN_AFTER_HOURS,
    GoesDistance,
    ParsedSnapshot,
    SnapshotEntry,
    SnapshotFormatError,
    SnapshotValidationReport,
    SourceChecked,
    load_snapshot,
    parse_snapshot,
    summarize,
    validate_snapshot,
    validate_snapshot_file,
    validate_snapshot_text,
)

from datetime import datetime, timedelta, timezone

# Fixed "now" so warn_after_hours staleness is reproducible. The default
# snapshot's collected_at (17:30Z) is 0.5h before this → fresh.
NOW = datetime(2026, 5, 29, 18, 0, 0, tzinfo=timezone.utc)


def _snapshot(entries=None, **overrides) -> dict:
    """A minimal, clean, two-sided snapshot, overridable per top-level field."""
    base = {
        "schema_version": 1,
        "snapshot_kind": "odds_news",
        "event": {"name": "UFC 999: A vs B", "date": "2026-05-30"},
        "collected_at": "2026-05-29T17:30:00Z",
        "collected_by": {"method": "manual"},
        "sources_checked": [{"name": "Example Book", "category": "Betting"}],
        "entries": [
            {"fighter_name": "Fighter A", "opponent_name": "Fighter B", "moneyline": -150},
            {"fighter_name": "Fighter B", "opponent_name": "Fighter A", "moneyline": 130},
        ]
        if entries is None
        else entries,
    }
    base.update(overrides)
    return base


def _validate(snapshot: dict, now=NOW) -> SnapshotValidationReport:
    return validate_snapshot_text(json.dumps(snapshot), now=now)


# ---------------------------------------------------------------------------
# valid snapshots
# ---------------------------------------------------------------------------


def test_valid_clean_two_sided_snapshot_has_no_warnings_or_errors():
    report = _validate(_snapshot())
    assert isinstance(report, SnapshotValidationReport)
    assert report.is_valid
    assert report.errors == []
    assert report.warnings == []
    assert len(report.entries_ok) == 2
    assert report.summary.ok_entries == 2
    assert report.summary.odds_entries == 2
    assert all(isinstance(e, SnapshotEntry) for e in report.entries_ok)


def test_valid_minimal_single_entry_is_valid_with_only_warnings():
    # Identity + moneyline is enough to be *valid* (design §3 principle 3).
    snap = _snapshot(
        entries=[{"fighter_name": "Solo", "opponent_name": "Ghost", "moneyline": -120}],
        sources_checked=[],
    )
    report = _validate(snap)
    assert report.is_valid  # no hard errors
    assert report.errors == []
    assert len(report.entries_ok) == 1
    # The soft issues (empty sources, one-sided bout) are warnings, not errors.
    assert any("sources_checked" in w for w in report.warnings)
    assert any("one-sided" in w.lower() for w in report.warnings)
    assert report.entries_ok[0].derived_implied_probability == pytest.approx(120 / 220)


def test_valid_rich_snapshot_with_props_news_and_provenance():
    entries = [
        {
            "fighter_name": "Fighter A",
            "opponent_name": "Fighter B",
            "moneyline": -180,
            "implied_probability": 0.643,
            "book": "Example Book",
            "line_open": -150,
            "line_current": -180,
            "line_movement": "toward",
            "itd_odds": 145,
            "decision_odds": -160,
            "goes_distance": {"yes": 120, "no": -150},
            "news_flags": [],
            "confidence": 0.9,
            "status": "ok",
            "source_name": "Example Book",
            "source_url": "https://example.test/odds",
            "collected_at": "2026-05-29T17:25:00Z",
        },
        {
            "fighter_name": "Fighter B",
            "opponent_name": "Fighter A",
            "moneyline": 160,
            "book": "Example Book",
            "news_flags": ["short_notice", "weight_miss"],
            "news_note": "Stepped in on 10 days notice; missed weight.",
            "news_source": "Beat Writer",
            "confidence": 0.6,
            "status": "needs_review",
        },
    ]
    report = _validate(_snapshot(entries=entries))
    assert report.is_valid
    assert report.warnings == []
    a, b = report.entries_ok
    assert a.itd_odds == 145
    assert a.decision_odds == -160
    assert a.goes_distance == GoesDistance(yes=120, no=-150)
    assert a.line_movement == "toward"
    assert a.source_url == "https://example.test/odds"
    assert a.derived_implied_probability == pytest.approx(180 / 280)
    assert b.news_flags == ("short_notice", "weight_miss")
    assert b.status == "needs_review"
    assert b.derived_implied_probability == pytest.approx(100 / 260)


def test_parse_snapshot_returns_gated_parsed_snapshot():
    parsed = parse_snapshot(json.dumps(_snapshot()))
    assert isinstance(parsed, ParsedSnapshot)
    assert parsed.schema_version == 1
    assert parsed.snapshot_kind == "odds_news"
    assert isinstance(parsed.data, dict)


def test_parse_snapshot_accepts_bytes():
    parsed = parse_snapshot(json.dumps(_snapshot()).encode("utf-8"))
    assert parsed.schema_version == 1


# ---------------------------------------------------------------------------
# format gate — document-level failures raise
# ---------------------------------------------------------------------------


def test_malformed_json_raises():
    with pytest.raises(SnapshotFormatError, match="not valid JSON"):
        parse_snapshot("{not valid json")


def test_non_object_top_level_raises():
    with pytest.raises(SnapshotFormatError, match="must be a JSON object"):
        parse_snapshot(json.dumps([1, 2, 3]))


def test_non_utf8_bytes_raise():
    with pytest.raises(SnapshotFormatError, match="UTF-8"):
        parse_snapshot(b"\xff\xfe\x00bad")


def test_missing_schema_version_raises():
    snap = _snapshot()
    del snap["schema_version"]
    with pytest.raises(SnapshotFormatError, match="schema_version"):
        parse_snapshot(json.dumps(snap))


def test_unsupported_schema_version_raises():
    with pytest.raises(SnapshotFormatError, match="schema_version"):
        parse_snapshot(json.dumps(_snapshot(schema_version=2)))


def test_schema_version_wrong_type_raises():
    # "1" (string) and 1.0 (float) are not the supported int version.
    with pytest.raises(SnapshotFormatError, match="schema_version"):
        parse_snapshot(json.dumps(_snapshot(schema_version="1")))
    with pytest.raises(SnapshotFormatError, match="schema_version"):
        parse_snapshot(json.dumps(_snapshot(schema_version=1.0)))


def test_missing_snapshot_kind_raises():
    snap = _snapshot()
    del snap["snapshot_kind"]
    with pytest.raises(SnapshotFormatError, match="snapshot_kind"):
        parse_snapshot(json.dumps(snap))


def test_unknown_snapshot_kind_raises():
    with pytest.raises(SnapshotFormatError, match="snapshot_kind"):
        parse_snapshot(json.dumps(_snapshot(snapshot_kind="mystery")))


# ---------------------------------------------------------------------------
# missing required envelope fields — collected as hard errors (is_valid False)
# ---------------------------------------------------------------------------


def test_missing_event_is_error():
    snap = _snapshot()
    del snap["event"]
    report = _validate(snap)
    assert not report.is_valid
    assert any("event" in e for e in report.errors)


def test_missing_event_name_and_date_are_errors():
    report = _validate(_snapshot(event={}))
    assert not report.is_valid
    assert any("event.name" in e for e in report.errors)
    assert any("event.date" in e for e in report.errors)


def test_malformed_event_date_is_error():
    report = _validate(_snapshot(event={"name": "X", "date": "30-05-2026"}))
    assert not report.is_valid
    assert any("event.date" in e for e in report.errors)


def test_missing_collected_at_is_error():
    snap = _snapshot()
    del snap["collected_at"]
    report = _validate(snap)
    assert not report.is_valid
    assert any("collected_at" in e for e in report.errors)


def test_collected_at_not_utc_is_error():
    # Naive (no offset) and non-UTC offset both reject (design §3.1).
    naive = _validate(_snapshot(collected_at="2026-05-29T17:30:00"))
    assert any("collected_at" in e for e in naive.errors)
    offset = _validate(_snapshot(collected_at="2026-05-29T17:30:00+02:00"))
    assert any("collected_at" in e and "UTC" in e for e in offset.errors)


def test_collected_at_accepts_explicit_zero_offset():
    report = _validate(_snapshot(collected_at="2026-05-29T17:30:00+00:00"))
    assert report.is_valid
    assert report.envelope.collected_at_dt is not None


def test_missing_collected_by_is_error():
    snap = _snapshot()
    del snap["collected_by"]
    report = _validate(snap)
    assert not report.is_valid
    assert any("collected_by" in e for e in report.errors)


def test_missing_collected_by_method_is_error():
    report = _validate(_snapshot(collected_by={"agent": "x"}))
    assert not report.is_valid
    assert any("collected_by.method" in e for e in report.errors)


def test_unknown_collected_by_method_is_error():
    report = _validate(_snapshot(collected_by={"method": "telepathy"}))
    assert not report.is_valid
    assert any("collected_by.method" in e for e in report.errors)


def test_missing_entries_key_is_error():
    snap = _snapshot()
    del snap["entries"]
    report = _validate(snap)
    assert not report.is_valid
    assert any("entries" in e for e in report.errors)


# ---------------------------------------------------------------------------
# empty entries / sources — warnings, not errors
# ---------------------------------------------------------------------------


def test_empty_entries_warns_but_is_valid():
    report = _validate(_snapshot(entries=[]))
    assert report.is_valid
    assert report.entries_ok == []
    assert any("no entries" in w.lower() for w in report.warnings)


def test_empty_sources_checked_warns():
    report = _validate(_snapshot(sources_checked=[]))
    assert report.is_valid
    assert any("sources_checked" in w and "empty" in w for w in report.warnings)


def test_missing_sources_checked_warns():
    snap = _snapshot()
    del snap["sources_checked"]
    report = _validate(snap)
    assert any("sources_checked" in w and "missing" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# missing required entry fields — entry rejected, others kept
# ---------------------------------------------------------------------------


def test_entry_missing_fighter_name_is_rejected():
    report = _validate(
        _snapshot(entries=[{"opponent_name": "B", "moneyline": -150}])
    )
    assert report.entries_ok == []
    assert not report.is_valid
    assert any("fighter_name" in e for e in report.errors)


def test_entry_missing_opponent_name_is_rejected():
    report = _validate(
        _snapshot(entries=[{"fighter_name": "A", "moneyline": -150}])
    )
    assert report.entries_ok == []
    assert any("opponent_name" in e for e in report.errors)


def test_odds_entry_missing_moneyline_is_rejected():
    report = _validate(
        _snapshot(entries=[{"fighter_name": "A", "opponent_name": "B"}])
    )
    assert report.entries_ok == []
    assert any("moneyline" in e for e in report.errors)


def test_news_only_entry_may_omit_moneyline():
    snap = _snapshot(
        entries=[
            {
                "fighter_name": "A",
                "opponent_name": "B",
                "entry_kind": "news_only",
                "news_flags": ["withdrawal"],
                "news_note": "Pulled out with injury.",
            }
        ]
    )
    report = _validate(snap)
    assert report.is_valid
    assert len(report.entries_ok) == 1
    entry = report.entries_ok[0]
    assert entry.entry_kind == "news_only"
    assert entry.moneyline is None
    assert entry.derived_implied_probability is None
    assert report.summary.news_only_entries == 1


def test_news_only_with_flags_but_no_note_warns():
    snap = _snapshot(
        entries=[
            {
                "fighter_name": "A",
                "opponent_name": "B",
                "entry_kind": "news_only",
                "news_flags": ["injury"],
            }
        ]
    )
    report = _validate(snap)
    assert report.is_valid
    assert any("news_only" in w and "news_note" in w for w in report.warnings)


def test_non_object_entry_is_rejected_others_kept():
    snap = _snapshot(
        entries=[
            {"fighter_name": "A", "opponent_name": "B", "moneyline": -150},
            "not-an-object",
        ]
    )
    report = _validate(snap)
    assert len(report.entries_ok) == 1
    assert report.summary.rejected_entries == 1
    assert any("JSON object" in e for e in report.errors)


# ---------------------------------------------------------------------------
# invalid moneyline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_ml", [0, "+150", 1.5, True])
def test_invalid_moneyline_is_rejected(bad_ml):
    report = _validate(
        _snapshot(entries=[{"fighter_name": "A", "opponent_name": "B", "moneyline": bad_ml}])
    )
    assert report.entries_ok == []
    assert any("moneyline" in e for e in report.errors)


# ---------------------------------------------------------------------------
# implied probability derivation (raw is canonical)
# ---------------------------------------------------------------------------


def test_implied_probability_derived_from_positive_american_odds():
    report = _validate(
        _snapshot(entries=[{"fighter_name": "A", "opponent_name": "B", "moneyline": 150}])
    )
    assert report.entries_ok[0].derived_implied_probability == pytest.approx(0.40)


def test_implied_probability_derived_from_negative_american_odds():
    report = _validate(
        _snapshot(entries=[{"fighter_name": "A", "opponent_name": "B", "moneyline": -200}])
    )
    assert report.entries_ok[0].derived_implied_probability == pytest.approx(2 / 3)


def test_derived_probability_ignores_supplied_advisory_value():
    # Even a wildly wrong advisory value does not change the derived number.
    report = _validate(
        _snapshot(
            entries=[
                {
                    "fighter_name": "A",
                    "opponent_name": "B",
                    "moneyline": -200,
                    "implied_probability": 0.10,
                }
            ]
        )
    )
    entry = report.entries_ok[0]
    assert entry.derived_implied_probability == pytest.approx(2 / 3)
    assert entry.implied_probability == 0.10  # carried, advisory only


# ---------------------------------------------------------------------------
# advisory implied-probability mismatch warning
# ---------------------------------------------------------------------------


def test_advisory_implied_probability_mismatch_warns():
    report = _validate(
        _snapshot(
            entries=[
                {
                    "fighter_name": "A",
                    "opponent_name": "B",
                    "moneyline": -180,  # derived ~0.643
                    "implied_probability": 0.50,
                }
            ]
        )
    )
    assert report.is_valid  # mismatch is a warning, not a rejection
    assert any("differs from app-derived" in w for w in report.warnings)


def test_advisory_implied_probability_within_tolerance_no_warning():
    report = _validate(
        _snapshot(
            entries=[
                {
                    "fighter_name": "A",
                    "opponent_name": "B",
                    "moneyline": -180,  # derived ~0.643
                    "implied_probability": 0.64,
                }
            ]
        )
    )
    assert not any("differs from app-derived" in w for w in report.warnings)


@pytest.mark.parametrize("field_name", ["implied_probability", "no_vig_probability", "confidence"])
@pytest.mark.parametrize("bad", [-0.1, 1.5, "high"])
def test_probabilities_out_of_range_are_errors(field_name, bad):
    report = _validate(
        _snapshot(
            entries=[
                {"fighter_name": "A", "opponent_name": "B", "moneyline": -150, field_name: bad}
            ]
        )
    )
    assert report.entries_ok == []
    assert any(field_name in e for e in report.errors)


# ---------------------------------------------------------------------------
# unknown enum values — hard errors (design §4 reject list)
# ---------------------------------------------------------------------------


def test_unknown_status_is_error():
    report = _validate(
        _snapshot(
            entries=[
                {"fighter_name": "A", "opponent_name": "B", "moneyline": -150, "status": "great"}
            ]
        )
    )
    assert report.entries_ok == []
    assert any("status" in e for e in report.errors)


def test_unknown_line_movement_is_error():
    report = _validate(
        _snapshot(
            entries=[
                {
                    "fighter_name": "A",
                    "opponent_name": "B",
                    "moneyline": -150,
                    "line_movement": "sideways",
                }
            ]
        )
    )
    assert report.entries_ok == []
    assert any("line_movement" in e for e in report.errors)


def test_unknown_entry_kind_is_error():
    report = _validate(
        _snapshot(
            entries=[
                {"fighter_name": "A", "opponent_name": "B", "moneyline": -150, "entry_kind": "guess"}
            ]
        )
    )
    assert report.entries_ok == []
    assert any("entry_kind" in e for e in report.errors)


def test_unknown_news_flag_is_error():
    report = _validate(
        _snapshot(
            entries=[
                {
                    "fighter_name": "A",
                    "opponent_name": "B",
                    "moneyline": -150,
                    "news_flags": ["injury", "abducted"],
                }
            ]
        )
    )
    assert report.entries_ok == []
    assert any("news flag" in e for e in report.errors)


def test_news_flags_not_a_list_is_error():
    report = _validate(
        _snapshot(
            entries=[
                {"fighter_name": "A", "opponent_name": "B", "moneyline": -150, "news_flags": "injury"}
            ]
        )
    )
    assert report.entries_ok == []
    assert any("news_flags must be a list" in e for e in report.errors)


# ---------------------------------------------------------------------------
# optional odds / props — invalid values warn (kept, dropped)
# ---------------------------------------------------------------------------


def test_invalid_prop_odds_warn_but_keep_entry():
    report = _validate(
        _snapshot(
            entries=[
                {
                    "fighter_name": "A",
                    "opponent_name": "B",
                    "moneyline": -150,
                    "itd_odds": "lots",
                    "goes_distance": {"yes": 120, "no": "nope"},
                }
            ]
        )
    )
    assert len(report.entries_ok) == 1  # entry kept
    entry = report.entries_ok[0]
    assert entry.itd_odds is None
    assert entry.goes_distance == GoesDistance(yes=120, no=None)
    assert any("itd_odds" in w for w in report.warnings)
    assert any("goes_distance.no" in w for w in report.warnings)


def test_goes_distance_not_an_object_warns():
    report = _validate(
        _snapshot(
            entries=[
                {"fighter_name": "A", "opponent_name": "B", "moneyline": -150, "goes_distance": 120}
            ]
        )
    )
    assert report.entries_ok[0].goes_distance is None
    assert any("goes_distance" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# line movement derivation
# ---------------------------------------------------------------------------


def test_line_movement_derived_toward_when_favorite_shortens():
    # -150 -> -200: implied prob rose, money came toward this fighter.
    report = _validate(
        _snapshot(
            entries=[
                {
                    "fighter_name": "A",
                    "opponent_name": "B",
                    "moneyline": -200,
                    "line_open": -150,
                    "line_current": -200,
                }
            ]
        )
    )
    assert report.entries_ok[0].line_movement == "toward"


def test_line_movement_derived_away_when_line_drifts():
    report = _validate(
        _snapshot(
            entries=[
                {
                    "fighter_name": "A",
                    "opponent_name": "B",
                    "moneyline": 150,
                    "line_open": -150,
                    "line_current": 150,
                }
            ]
        )
    )
    assert report.entries_ok[0].line_movement == "away"


def test_line_current_defaults_to_moneyline():
    report = _validate(
        _snapshot(
            entries=[{"fighter_name": "A", "opponent_name": "B", "moneyline": -150}]
        )
    )
    assert report.entries_ok[0].line_current == -150


# ---------------------------------------------------------------------------
# bout pairing + duplicate fighter (normalized)
# ---------------------------------------------------------------------------


def test_one_sided_bout_warns():
    report = _validate(
        _snapshot(
            entries=[{"fighter_name": "Solo", "opponent_name": "Ghost", "moneyline": -150}]
        )
    )
    assert report.is_valid
    assert any("one-sided" in w.lower() for w in report.warnings)


def test_two_sided_bout_matches_via_aggressive_normalizer():
    # Different spellings of the same fighters still pair (Daniel/Dan).
    report = _validate(
        _snapshot(
            entries=[
                {"fighter_name": "Daniel Ige", "opponent_name": "Dan Hooker", "moneyline": -150},
                {"fighter_name": "Dan Hooker", "opponent_name": "Daniel Ige", "moneyline": 130},
            ]
        )
    )
    assert not any("one-sided" in w.lower() for w in report.warnings)


def test_duplicate_fighter_warns():
    report = _validate(
        _snapshot(
            entries=[
                {"fighter_name": "Fighter A", "opponent_name": "Fighter B", "moneyline": -150},
                {"fighter_name": "Fighter A", "opponent_name": "Fighter B", "moneyline": -150},
            ]
        )
    )
    assert any("duplicate fighter" in w.lower() for w in report.warnings)


# ---------------------------------------------------------------------------
# staleness (design §3.6 / §4) — deterministic via injected NOW
# ---------------------------------------------------------------------------


def test_snapshot_older_than_warn_threshold_is_stale():
    old = (NOW - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = _validate(_snapshot(collected_at=old))
    assert report.is_valid
    assert any("Snapshot collected_at is" in w and "old" in w for w in report.warnings)


def test_custom_warn_after_hours_policy_is_respected():
    older = (NOW - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = _validate(_snapshot(collected_at=older))  # default 12h -> fresh
    assert not any("Snapshot collected_at is" in w for w in fresh.warnings)
    strict = _validate(
        _snapshot(collected_at=older, staleness_policy={"warn_after_hours": 2})
    )
    assert any("Snapshot collected_at is" in w for w in strict.warnings)


def test_entry_level_collected_at_staleness_warns_and_counts():
    old = (NOW - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = _validate(
        _snapshot(
            entries=[
                {
                    "fighter_name": "Solo",
                    "opponent_name": "Ghost",
                    "moneyline": -150,
                    "collected_at": old,
                }
            ]
        )
    )
    assert report.entries_ok[0].is_stale
    assert report.summary.stale_entries == 1
    assert any("entry collected_at is" in w for w in report.warnings)


def test_collected_after_event_date_warns():
    report = _validate(
        _snapshot(collected_at="2026-06-01T12:00:00Z"),  # event date 2026-05-30
        now=datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone.utc),
    )
    assert any("after the event date" in w for w in report.warnings)


def test_default_warn_after_hours_constant_is_twelve():
    assert DEFAULT_WARN_AFTER_HOURS == 12.0


# ---------------------------------------------------------------------------
# sources_checked validation (provenance warnings, never rejects snapshot)
# ---------------------------------------------------------------------------


def test_sources_checked_unknown_category_warns_but_keeps_source():
    report = _validate(
        _snapshot(sources_checked=[{"name": "Pod", "category": "Podcast"}])
    )
    assert report.is_valid
    assert report.envelope.sources_checked == (
        SourceChecked(name="Pod", url=None, category="Podcast", checked_at=None),
    )
    assert any("unknown category" in w for w in report.warnings)


def test_sources_checked_missing_name_is_skipped_with_warning():
    report = _validate(_snapshot(sources_checked=[{"url": "https://x.test"}]))
    assert report.is_valid
    assert report.envelope.sources_checked == ()
    assert any("missing 'name'" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# free-text safety (design §4 / §7 #6)
# ---------------------------------------------------------------------------


def test_news_note_control_chars_stripped_and_whitespace_collapsed():
    report = _validate(
        _snapshot(
            entries=[
                {
                    "fighter_name": "A",
                    "opponent_name": "B",
                    "moneyline": -150,
                    "entry_kind": "news_only",
                    "news_flags": ["injury"],
                    "news_note": "line1\x00\x07\nline2\t  spaced",
                }
            ]
        )
    )
    assert report.entries_ok[0].news_note == "line1 line2 spaced"


def test_fighter_name_preserved_verbatim_apart_from_control_chars():
    report = _validate(
        _snapshot(
            entries=[
                {"fighter_name": "  O'Malley\x00  ", "opponent_name": "B", "moneyline": -150}
            ]
        )
    )
    assert report.entries_ok[0].fighter_name == "O'Malley"


# ---------------------------------------------------------------------------
# warning / error aggregation
# ---------------------------------------------------------------------------


def test_warning_and_error_aggregation_and_summary_counts():
    snap = _snapshot(
        sources_checked=[],  # warning
        entries=[
            {"fighter_name": "A", "opponent_name": "B", "moneyline": -150},  # ok, one-sided
            {"fighter_name": "C", "opponent_name": "D"},  # error: missing moneyline
            {"fighter_name": "E", "opponent_name": "F", "moneyline": -150, "status": "weird"},  # error
        ],
    )
    report = _validate(snap)
    assert not report.is_valid
    assert report.summary.total_entries == 3
    assert report.summary.ok_entries == 1
    assert report.summary.rejected_entries == 2
    assert report.summary.error_count == len(report.errors) == 2
    assert report.summary.warning_count == len(report.warnings)
    # The empty-sources warning is present alongside per-entry one-sided warnings.
    assert any("sources_checked" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# file entry points
# ---------------------------------------------------------------------------


def test_load_snapshot_file_round_trip(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(_snapshot()), encoding="utf-8")
    report = validate_snapshot_file(path, now=NOW)
    assert report.is_valid
    assert len(report.entries_ok) == 2


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(SnapshotFormatError, match="not found"):
        load_snapshot(tmp_path / "nope.json")


def test_load_file_reports_content_errors_not_raises(tmp_path):
    snap = _snapshot()
    del snap["collected_at"]
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    report = validate_snapshot_file(path, now=NOW)  # must not raise
    assert not report.is_valid
    assert any("collected_at" in e for e in report.errors)


# ---------------------------------------------------------------------------
# summarize — data-safe text report
# ---------------------------------------------------------------------------


def test_summarize_includes_counts_validity_and_issue_sections():
    snap = _snapshot(
        sources_checked=[],
        entries=[{"fighter_name": "C", "opponent_name": "D"}],  # error
    )
    text = summarize(_validate(snap))
    assert "Event:" in text
    assert "Entries:" in text
    assert "Issues:" in text
    assert "Valid: False" in text
    assert "Warnings" in text
    assert "Errors" in text


# ---------------------------------------------------------------------------
# determinism / no network or DB dependency
# ---------------------------------------------------------------------------


def test_validate_is_pure_no_path_or_io_needed():
    # Same input + same injected now -> identical errors/warnings, no I/O.
    payload = json.dumps(_snapshot(sources_checked=[]))
    first = validate_snapshot_text(payload, now=NOW)
    second = validate_snapshot_text(payload, now=NOW)
    assert first.errors == second.errors
    assert first.warnings == second.warnings


def test_default_now_runs_without_injection():
    report = validate_snapshot_text(json.dumps(_snapshot()))
    assert isinstance(report, SnapshotValidationReport)


def test_module_imports_no_network_or_db_libraries():
    source = inspect.getsource(ons)
    for forbidden in ("requests", "urllib", "http.client", "socket", "sqlite3", "streamlit"):
        assert forbidden not in source, f"unexpected dependency: {forbidden}"


def test_validate_does_not_mutate_parsed_data():
    parsed = parse_snapshot(json.dumps(_snapshot()))
    before = json.dumps(parsed.data, sort_keys=True)
    validate_snapshot(parsed, now=NOW)
    assert json.dumps(parsed.data, sort_keys=True) == before
