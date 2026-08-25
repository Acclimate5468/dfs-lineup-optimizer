"""Salary importer tests.

NOTE: importer is not claimed complete in v0. These tests cover only the
minimal CSV loading behavior of the stub and the column-validation logic.
The column set is pending verification against a real DK UFC salary CSV.
"""

import pandas as pd
import pytest

from src.ingestion.dk_salary_importer import (
    REQUIRED_COLUMNS,
    ParsedSalaryRow,
    SalaryParseError,
    load_dk_salary_csv,
    parse_dk_salary_rows,
    validate_dk_salary_csv,
)


def _write(path, text):
    path.write_text(text)
    return path


def test_load_dk_salary_csv_basic(tmp_path):
    p = tmp_path / "salaries.csv"
    p.write_text(
        "Position,Name,ID,Salary,Game Info,TeamAbbrev\n"
        "F,Jon Doe,12345,9000,UFC 999,JDO\n"
        "F,Jane Roe,12346,8500,UFC 999,JRO\n"
    )
    df = load_dk_salary_csv(p)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert "Name" in df.columns
    assert "Salary" in df.columns


def test_validate_minimal_valid_csv(tmp_path):
    p = _write(
        tmp_path / "salaries.csv",
        "Position,Name,ID,Salary,Game Info,TeamAbbrev\n"
        "F,Jon Doe,12345,9000,UFC 999,JDO\n"
        "F,Jane Roe,12346,8500,UFC 999,JRO\n",
    )
    result = validate_dk_salary_csv(p)
    assert result.is_valid is True
    assert result.missing_columns == []
    assert result.row_count == 2
    assert result.error_message is None
    for col in REQUIRED_COLUMNS:
        assert col in result.detected_columns


def test_validate_missing_required_column(tmp_path):
    p = _write(
        tmp_path / "salaries.csv",
        "Position,Name,ID,Game Info,TeamAbbrev\n"
        "F,Jon Doe,12345,UFC 999,JDO\n",
    )
    result = validate_dk_salary_csv(p)
    assert result.is_valid is False
    assert "Salary" in result.missing_columns
    assert result.error_message is not None
    assert "Salary" in result.error_message


def test_validate_empty_csv(tmp_path):
    p = _write(tmp_path / "salaries.csv", "")
    result = validate_dk_salary_csv(p)
    assert result.is_valid is False
    assert result.error_message is not None
    assert "empty" in result.error_message.lower()


def test_validate_detected_columns_reported(tmp_path):
    p = _write(
        tmp_path / "salaries.csv",
        "Position,Name,ID,Salary,Game Info,TeamAbbrev,Extra\n"
        "F,Jon Doe,12345,9000,UFC 999,JDO,foo\n",
    )
    result = validate_dk_salary_csv(p)
    assert result.is_valid is True
    assert "Extra" in result.detected_columns
    assert set(REQUIRED_COLUMNS).issubset(set(result.detected_columns))


def test_validate_invalid_file_blocked_with_helpful_message(tmp_path):
    # Looks like some other / custom export, not DK UFC Classic.
    p = _write(
        tmp_path / "not_dk.csv",
        "player,team,price\n"
        "Jon Doe,JDO,9000\n",
    )
    result = validate_dk_salary_csv(p)
    assert result.is_valid is False
    assert result.error_message is not None
    msg = result.error_message
    assert "DK UFC Classic" in msg
    assert "Missing required column" in msg
    for col in REQUIRED_COLUMNS:
        assert col in msg


def test_validate_header_only_zero_rows(tmp_path):
    p = _write(
        tmp_path / "salaries.csv",
        "Position,Name,ID,Salary,Game Info,TeamAbbrev\n",
    )
    result = validate_dk_salary_csv(p)
    assert result.is_valid is False
    assert result.row_count == 0
    assert result.error_message is not None


def test_validate_missing_file(tmp_path):
    result = validate_dk_salary_csv(tmp_path / "does_not_exist.csv")
    assert result.is_valid is False
    assert result.error_message is not None
    assert "not found" in result.error_message.lower()


# ---------------------------------------------------------------------------
# parse_dk_salary_rows — slice A typed-records parser
# ---------------------------------------------------------------------------


def _valid_df(rows: list[dict]) -> pd.DataFrame:
    default_cols = ["Position", "Name", "ID", "Salary", "Game Info", "TeamAbbrev"]
    return pd.DataFrame(rows, columns=default_cols)


def test_parse_minimal_valid_rows():
    df = _valid_df(
        [
            {
                "Position": "F",
                "Name": "Jon Doe",
                "ID": "12345",
                "Salary": "9000",
                "Game Info": "Jon Doe@Jane Roe 05/22/2026",
                "TeamAbbrev": "JDO",
            },
            {
                "Position": "F",
                "Name": "Jane Roe",
                "ID": "12346",
                "Salary": "8500",
                "Game Info": "Jon Doe@Jane Roe 05/22/2026",
                "TeamAbbrev": "JRO",
            },
        ]
    )
    parsed = parse_dk_salary_rows(df)
    assert len(parsed) == 2
    assert all(isinstance(p, ParsedSalaryRow) for p in parsed)
    assert parsed[0].fighter_name == "Jon Doe"
    assert parsed[0].salary == 9000
    assert parsed[0].roster_position == "F"
    assert parsed[0].game_info == "Jon Doe@Jane Roe 05/22/2026"
    assert parsed[0].source_row_number == 1
    assert parsed[1].source_row_number == 2


def test_parse_accepts_dollar_and_comma_salary_formatting():
    df = _valid_df(
        [
            {
                "Position": "F",
                "Name": "Jon Doe",
                "ID": "1",
                "Salary": "$9,000",
                "Game Info": "x",
                "TeamAbbrev": "JDO",
            },
        ]
    )
    parsed = parse_dk_salary_rows(df)
    assert parsed[0].salary == 9000


def test_parse_blank_fighter_name_fails():
    df = _valid_df(
        [
            {
                "Position": "F",
                "Name": "   ",
                "ID": "1",
                "Salary": "9000",
                "Game Info": "x",
                "TeamAbbrev": "JDO",
            },
        ]
    )
    with pytest.raises(SalaryParseError, match="fighter name"):
        parse_dk_salary_rows(df)


def test_parse_non_integer_salary_fails():
    df = _valid_df(
        [
            {
                "Position": "F",
                "Name": "Jon Doe",
                "ID": "1",
                "Salary": "nine thousand",
                "Game Info": "x",
                "TeamAbbrev": "JDO",
            },
        ]
    )
    with pytest.raises(SalaryParseError, match="not an integer"):
        parse_dk_salary_rows(df)


def test_parse_fractional_salary_fails():
    df = _valid_df(
        [
            {
                "Position": "F",
                "Name": "Jon Doe",
                "ID": "1",
                "Salary": "9000.5",
                "Game Info": "x",
                "TeamAbbrev": "JDO",
            },
        ]
    )
    with pytest.raises(SalaryParseError, match="not an integer"):
        parse_dk_salary_rows(df)


def test_parse_negative_salary_fails():
    df = _valid_df(
        [
            {
                "Position": "F",
                "Name": "Jon Doe",
                "ID": "1",
                "Salary": "-1",
                "Game Info": "x",
                "TeamAbbrev": "JDO",
            },
        ]
    )
    with pytest.raises(SalaryParseError, match="negative"):
        parse_dk_salary_rows(df)


def test_parse_duplicate_fighter_names_fail():
    df = _valid_df(
        [
            {
                "Position": "F",
                "Name": "Jon Doe",
                "ID": "1",
                "Salary": "9000",
                "Game Info": "x",
                "TeamAbbrev": "JDO",
            },
            {
                "Position": "F",
                "Name": "Jon Doe",
                "ID": "2",
                "Salary": "8500",
                "Game Info": "x",
                "TeamAbbrev": "JDO",
            },
        ]
    )
    with pytest.raises(SalaryParseError, match="duplicate"):
        parse_dk_salary_rows(df)


def test_parse_preserves_optional_game_info_when_present():
    df = _valid_df(
        [
            {
                "Position": "F",
                "Name": "Jon Doe",
                "ID": "1",
                "Salary": "9000",
                "Game Info": "Jon Doe@Jane Roe 05/22/2026",
                "TeamAbbrev": "JDO",
            },
        ]
    )
    parsed = parse_dk_salary_rows(df)
    assert parsed[0].game_info == "Jon Doe@Jane Roe 05/22/2026"
    assert parsed[0].roster_position == "F"


def test_parse_optional_fields_become_none_when_blank():
    df = _valid_df(
        [
            {
                "Position": "",
                "Name": "Jon Doe",
                "ID": "1",
                "Salary": "9000",
                "Game Info": "",
                "TeamAbbrev": "JDO",
            },
        ]
    )
    parsed = parse_dk_salary_rows(df)
    assert parsed[0].roster_position is None
    assert parsed[0].game_info is None


def test_parse_ignores_unknown_columns():
    df = pd.DataFrame(
        [
            {
                "Position": "F",
                "Name": "Jon Doe",
                "ID": "1",
                "Salary": "9000",
                "Game Info": "x",
                "TeamAbbrev": "JDO",
                "MysteryColumn": "should be ignored",
            },
        ]
    )
    parsed = parse_dk_salary_rows(df)
    assert len(parsed) == 1
    assert parsed[0].fighter_name == "Jon Doe"


def test_parse_does_not_require_db_access(tmp_path):
    # Sanity check: parser works purely against an in-memory DataFrame and
    # never touches any database. We assert by calling the parser with no
    # DB fixture in scope and confirming a clean result.
    df = _valid_df(
        [
            {
                "Position": "F",
                "Name": "Jon Doe",
                "ID": "1",
                "Salary": "9000",
                "Game Info": "x",
                "TeamAbbrev": "JDO",
            },
        ]
    )
    parsed = parse_dk_salary_rows(df)
    assert len(parsed) == 1


def test_parse_rejects_dataframe_missing_required_columns():
    df = pd.DataFrame([{"Name": "Jon Doe"}])  # no Salary
    with pytest.raises(SalaryParseError, match="validate_dk_salary_csv"):
        parse_dk_salary_rows(df)
