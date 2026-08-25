"""Source manifest parser tests.

Foundation slice for the public-source fight-week collector
(``docs/FIGHT_WEEK_SOURCE_COLLECTOR_DESIGN.md``). The parser is the catalogue
layer only: it validates/normalizes/de-duplicates the source registry and never
fetches anything.

All fixtures here are **synthetic and inline**. These tests must not read the
real ``data/uploads/sources/UFC_DATA.json`` — it is operator data (design §12).
"""

import json

import pytest

from src.collection.source_manifest import (
    CANONICAL_CATEGORIES,
    REQUIRED_FIELDS,
    SourceManifestError,
    SourceManifestResult,
    SourceRecord,
    load_source_manifest,
    normalize_category,
    normalize_frequency,
    normalize_type,
    parse_source_manifest,
    parse_source_manifest_text,
    parse_sources,
    summarize,
)


def _source(**overrides) -> dict:
    """A minimal valid source entry, overridable per field."""
    base = {
        "sport": "UFC",
        "category": "Betting",
        "name": "Best Fight Odds",
        "type": "website",
        "url": "https://www.bestfightodds.com/",
        "frequency": "manual",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# parse_sources — happy path
# ---------------------------------------------------------------------------


def test_parse_minimal_valid_sources():
    data = [
        _source(name="Best Fight Odds", url="https://a.example/"),
        _source(
            name="UFC Stats",
            url="https://b.example/",
            category="Official",
            frequency="auto",
        ),
    ]
    result = parse_sources(data)
    assert isinstance(result, SourceManifestResult)
    assert result.total_input == 2
    assert result.valid_count == 2
    assert result.duplicate_count == 0
    assert result.error_count == 0
    assert result.warnings == []
    assert result.errors == []
    assert all(isinstance(r, SourceRecord) for r in result.records)


def test_source_index_is_one_based_and_in_order():
    data = [
        _source(name="A", url="https://a.example/"),
        _source(name="B", url="https://b.example/"),
        _source(name="C", url="https://c.example/"),
    ]
    result = parse_sources(data)
    assert [r.source_index for r in result.records] == [1, 2, 3]
    assert [r.name for r in result.records] == ["A", "B", "C"]


def test_counts_by_category_frequency_and_type():
    data = [
        _source(category="Betting", frequency="manual", type="website"),
        _source(
            name="X2",
            url="https://x2.example/",
            category="Betting",
            frequency="auto",
            type="website",
        ),
        _source(
            name="Helwani",
            url="https://x.example/helwani",
            category="Insiders",
            frequency="manual",
            type="X",
        ),
    ]
    result = parse_sources(data)
    assert result.counts_by_category == {"Betting": 2, "Insiders": 1}
    assert result.counts_by_frequency == {"manual": 2, "auto": 1}
    assert result.counts_by_type == {"website": 2, "x": 1}


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


def test_normalize_category_is_case_insensitive():
    assert normalize_category("betting") == "Betting"
    assert normalize_category("BETTING") == "Betting"
    assert normalize_category("  Betting  ") == "Betting"
    for canonical in CANONICAL_CATEGORIES:
        assert normalize_category(canonical.lower()) == canonical


def test_normalize_type_lowercases_and_maps_x():
    assert normalize_type("X") == "x"
    assert normalize_type("Website") == "website"
    assert normalize_type("  WEBSITE ") == "website"


def test_normalize_frequency_lowercases():
    assert normalize_frequency("AUTO") == "auto"
    assert normalize_frequency(" Manual ") == "manual"


def test_record_normalizes_category_type_frequency():
    data = [
        _source(category="analytics", type="X", frequency="AUTO"),
    ]
    record = parse_sources(data).records[0]
    assert record.category == "Analytics"
    assert record.type == "x"
    assert record.frequency == "auto"


def test_record_preserves_original_name_and_url():
    # Case and punctuation in name/url must round-trip (only whitespace trimmed).
    data = [
        _source(
            name="  BestFightOdds.com  ",
            url="  https://www.BestFightOdds.com/MMA  ",
        ),
    ]
    record = parse_sources(data).records[0]
    assert record.name == "BestFightOdds.com"
    assert record.url == "https://www.BestFightOdds.com/MMA"


# ---------------------------------------------------------------------------
# required-field validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field_name", REQUIRED_FIELDS)
def test_missing_required_field_is_error_and_excluded(field_name):
    bad = _source()
    del bad[field_name]
    result = parse_sources([bad])
    assert result.records == []
    assert result.valid_count == 0
    assert result.error_count == 1
    assert field_name in result.errors[0]


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_required_field_is_error(blank):
    result = parse_sources([_source(name=blank)])
    assert result.records == []
    assert result.error_count == 1
    assert "name" in result.errors[0]


def test_non_string_required_field_is_error():
    # A number where text is required is treated as missing/blank.
    result = parse_sources([_source(url=12345)])
    assert result.records == []
    assert result.error_count == 1
    assert "url" in result.errors[0]


def test_non_object_entry_is_error_and_skipped():
    data = [_source(name="Good", url="https://good.example/"), "not-an-object", 42]
    result = parse_sources(data)
    assert result.valid_count == 1
    assert result.error_count == 2
    assert result.records[0].name == "Good"


def test_mixed_valid_and_invalid_entries():
    data = [
        _source(name="Good", url="https://good.example/"),
        _source(name="", url="https://blank.example/"),  # error: blank name
        _source(name="AlsoGood", url="https://also.example/"),
    ]
    result = parse_sources(data)
    assert result.total_input == 3
    assert result.valid_count == 2
    assert result.error_count == 1
    assert [r.name for r in result.records] == ["Good", "AlsoGood"]


# ---------------------------------------------------------------------------
# duplicate detection
# ---------------------------------------------------------------------------


def test_exact_duplicate_is_dropped_with_warning():
    data = [
        _source(name="Dup", url="https://dup.example/"),
        _source(name="Dup", url="https://dup.example/"),
    ]
    result = parse_sources(data)
    assert result.valid_count == 1
    assert result.duplicate_count == 1
    assert len(result.warnings) == 1
    assert "duplicate" in result.warnings[0].lower()
    # First occurrence wins; its index is referenced.
    assert "#1" in result.warnings[0]


def test_same_name_different_url_is_not_a_duplicate():
    data = [
        _source(name="Sherdog", url="https://www.sherdog.com/"),
        _source(name="Sherdog", url="https://sherdog.com/"),
    ]
    result = parse_sources(data)
    assert result.valid_count == 2
    assert result.duplicate_count == 0


def test_near_duplicate_scheme_host_is_kept_separate():
    # http vs https / www are NOT merged in this slice (design §4 #3).
    data = [
        _source(name="Same Name", url="http://example.com/"),
        _source(name="Same Name", url="https://example.com/"),
    ]
    result = parse_sources(data)
    assert result.valid_count == 2
    assert result.duplicate_count == 0


# ---------------------------------------------------------------------------
# unknown enum values -> warning, kept
# ---------------------------------------------------------------------------


def test_unknown_category_is_warned_and_kept():
    result = parse_sources([_source(category="Podcast")])
    assert result.valid_count == 1
    assert result.records[0].category == "Podcast"
    assert any("unknown category" in w for w in result.warnings)
    assert result.counts_by_category == {"Podcast": 1}


def test_unknown_type_is_warned_and_kept():
    result = parse_sources([_source(type="rss")])
    assert result.valid_count == 1
    assert result.records[0].type == "rss"
    assert any("unknown type" in w for w in result.warnings)


def test_unknown_frequency_is_warned_and_kept():
    result = parse_sources([_source(frequency="hourly")])
    assert result.valid_count == 1
    assert result.records[0].frequency == "hourly"
    assert any("unknown frequency" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# empty manifest
# ---------------------------------------------------------------------------


def test_empty_manifest_is_valid_with_warning():
    result = parse_sources([])
    assert result.total_input == 0
    assert result.valid_count == 0
    assert result.records == []
    assert result.errors == []
    assert any("empty" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# load_source_manifest — file-level failures raise
# ---------------------------------------------------------------------------


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(SourceManifestError, match="not found"):
        load_source_manifest(tmp_path / "nope.json")


def test_load_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SourceManifestError, match="not valid JSON"):
        load_source_manifest(p)


def test_load_non_array_top_level_raises(tmp_path):
    p = tmp_path / "obj.json"
    p.write_text(json.dumps({"sport": "UFC"}), encoding="utf-8")
    with pytest.raises(SourceManifestError, match="JSON array"):
        load_source_manifest(p)


# ---------------------------------------------------------------------------
# parse_source_manifest — file round-trip
# ---------------------------------------------------------------------------


def test_parse_source_manifest_file_round_trip(tmp_path):
    data = [
        _source(name="A", url="https://a.example/", category="news", type="X"),
        _source(name="B", url="https://b.example/", frequency="auto"),
    ]
    p = tmp_path / "sources.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    result = parse_source_manifest(p)
    assert result.valid_count == 2
    assert result.records[0].category == "News"
    assert result.records[0].type == "x"
    assert result.records[1].frequency == "auto"


def test_parse_source_manifest_reports_records_not_raises_on_bad_entry(tmp_path):
    # A bad *record* must not raise from the file entry point; only file-level
    # problems raise. The bad record shows up as an error in the result.
    data = [_source(name="Good", url="https://good.example/"), {"sport": "UFC"}]
    p = tmp_path / "sources.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    result = parse_source_manifest(p)
    assert result.valid_count == 1
    assert result.error_count == 1


# ---------------------------------------------------------------------------
# parse_source_manifest_text — in-memory string entry point (uploads)
# ---------------------------------------------------------------------------


def test_parse_text_happy_path_normalizes_like_file_path():
    data = [
        _source(name="A", url="https://a.example/", category="news", type="X"),
        _source(name="B", url="https://b.example/", frequency="auto"),
    ]
    result = parse_source_manifest_text(json.dumps(data))
    assert isinstance(result, SourceManifestResult)
    assert result.valid_count == 2
    assert result.records[0].category == "News"
    assert result.records[0].type == "x"
    assert result.records[1].frequency == "auto"


def test_parse_text_invalid_json_raises():
    with pytest.raises(SourceManifestError, match="not valid JSON"):
        parse_source_manifest_text("{not valid json")


def test_parse_text_non_array_top_level_raises():
    with pytest.raises(SourceManifestError, match="JSON array"):
        parse_source_manifest_text(json.dumps({"sport": "UFC"}))


def test_parse_text_reports_bad_record_not_raises():
    # A bad *record* is reported in the result, not raised — same contract as
    # the file entry point.
    data = [_source(name="Good", url="https://good.example/"), {"sport": "UFC"}]
    result = parse_source_manifest_text(json.dumps(data))
    assert result.valid_count == 1
    assert result.error_count == 1


# ---------------------------------------------------------------------------
# summarize — data-safe text report
# ---------------------------------------------------------------------------


def test_summarize_includes_counts_and_issue_sections():
    data = [
        _source(name="A", url="https://a.example/", category="Podcast"),  # warn
        _source(name="A", url="https://a.example/", category="Podcast"),  # dup
        {"sport": "UFC"},  # error
    ]
    text = summarize(parse_sources(data))
    assert "Sources:" in text
    assert "By category:" in text
    assert "By frequency:" in text
    assert "By type:" in text
    assert "Warnings" in text
    assert "Errors" in text
