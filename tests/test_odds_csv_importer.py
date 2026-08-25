"""Odds CSV importer validation tests.

v0: validation logic only. The importer is not claimed complete until
tested against a real Odds API / Google Sheets export.
"""

from src.ingestion.odds_csv_importer import (
    PREFERRED_OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    parse_moneyline,
    validate_odds_csv,
)


def test_parse_moneyline_accepts_signed_strings_and_ints():
    assert parse_moneyline("-150") == -150
    assert parse_moneyline("+220") == 220
    assert parse_moneyline("220") == 220
    assert parse_moneyline(-110) == -110
    assert parse_moneyline(" +175 ") == 175


def test_parse_moneyline_rejects_invalid():
    assert parse_moneyline("") is None
    assert parse_moneyline(None) is None
    assert parse_moneyline("abc") is None
    assert parse_moneyline(0) is None
    assert parse_moneyline("0") is None
    assert parse_moneyline("+0") is None
    assert parse_moneyline("-0") is None
    assert parse_moneyline(True) is None


def _write(path, text):
    path.write_text(text)
    return path


MIN_HEADER = "fighter,moneyline,source,timestamp\n"
PREFERRED_HEADER = (
    "fighter,opponent,moneyline,bookmaker,source,timestamp\n"
)


def test_valid_minimum_csv(tmp_path):
    p = _write(
        tmp_path / "odds.csv",
        MIN_HEADER
        + "Jon Doe,-150,oddsapi,2026-05-20T12:00:00Z\n"
        + "Jane Roe,+130,oddsapi,2026-05-20T12:00:00Z\n",
    )
    result = validate_odds_csv(p)
    assert result.is_valid is True
    assert result.missing_columns == []
    assert result.row_count == 2
    assert result.error_message is None
    for col in REQUIRED_COLUMNS:
        assert col in result.detected_columns


def test_valid_preferred_csv(tmp_path):
    p = _write(
        tmp_path / "odds.csv",
        PREFERRED_HEADER
        + "Jon Doe,Jane Roe,-150,DraftKings,oddsapi,2026-05-20T12:00:00Z\n"
        + "Jane Roe,Jon Doe,+130,DraftKings,oddsapi,2026-05-20T12:00:00Z\n",
    )
    result = validate_odds_csv(p)
    assert result.is_valid is True
    assert result.row_count == 2
    for col in REQUIRED_COLUMNS + PREFERRED_OPTIONAL_COLUMNS:
        assert col in result.detected_columns
    # No "preferred optional column" warnings when both are present.
    for w in result.warning_messages:
        assert "Preferred optional column" not in w


def test_missing_required_column(tmp_path):
    p = _write(
        tmp_path / "odds.csv",
        "fighter,moneyline,source\n"
        "Jon Doe,-150,oddsapi\n",
    )
    result = validate_odds_csv(p)
    assert result.is_valid is False
    assert "timestamp" in result.missing_columns
    assert result.error_message is not None
    assert "timestamp" in result.error_message


def test_empty_csv(tmp_path):
    p = _write(tmp_path / "odds.csv", "")
    result = validate_odds_csv(p)
    assert result.is_valid is False
    assert result.error_message is not None
    assert "empty" in result.error_message.lower()


def test_header_only_csv(tmp_path):
    p = _write(tmp_path / "odds.csv", MIN_HEADER)
    result = validate_odds_csv(p)
    assert result.is_valid is False
    assert result.row_count == 0
    assert result.error_message is not None


def test_invalid_moneyline_value(tmp_path):
    p = _write(
        tmp_path / "odds.csv",
        MIN_HEADER
        + "Jon Doe,not-a-number,oddsapi,2026-05-20T12:00:00Z\n",
    )
    result = validate_odds_csv(p)
    assert result.is_valid is False
    assert result.error_message is not None
    assert "moneyline" in result.error_message.lower()


def test_missing_moneyline_value(tmp_path):
    p = _write(
        tmp_path / "odds.csv",
        MIN_HEADER
        + "Jon Doe,,oddsapi,2026-05-20T12:00:00Z\n",
    )
    result = validate_odds_csv(p)
    assert result.is_valid is False
    assert result.error_message is not None
    assert "moneyline" in result.error_message.lower()


def test_extra_columns_allowed(tmp_path):
    p = _write(
        tmp_path / "odds.csv",
        "fighter,moneyline,source,timestamp,extra_note\n"
        "Jon Doe,-150,oddsapi,2026-05-20T12:00:00Z,foo\n",
    )
    result = validate_odds_csv(p)
    assert result.is_valid is True
    assert "extra_note" in result.detected_columns
    assert any("Extra column" in w for w in result.warning_messages)


def test_detected_columns_reported(tmp_path):
    p = _write(
        tmp_path / "odds.csv",
        "fighter,opponent,moneyline,source,timestamp\n"
        "Jon Doe,Jane Roe,-150,oddsapi,2026-05-20T12:00:00Z\n",
    )
    result = validate_odds_csv(p)
    assert result.is_valid is True
    assert result.detected_columns == [
        "fighter",
        "opponent",
        "moneyline",
        "source",
        "timestamp",
    ]
    # bookmaker missing → warning
    assert any("bookmaker" in w for w in result.warning_messages)


def test_missing_file():
    result = validate_odds_csv("/tmp/__definitely_does_not_exist_odds__.csv")
    assert result.is_valid is False
    assert result.error_message is not None
    assert "not found" in result.error_message.lower()
