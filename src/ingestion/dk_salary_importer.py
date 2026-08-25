"""DraftKings UFC salary CSV importer.

STATUS: v0 skeleton. Not validated against a real DK UFC salary export yet.
Do not claim complete until tested with a real official DK UFC salary CSV.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


class SalaryParseError(ValueError):
    """Raised when a validated DK salary CSV cannot be parsed into typed rows.

    Carries an explicit message describing the first offending row so callers
    can surface a clear error rather than silently dropping data.
    """

# Tentative column names DK has historically used for UFC Classic exports.
# Real-file verification is required before relying on these.
EXPECTED_COLUMNS_HINT = {"Position", "Name", "ID", "Salary", "Game Info", "TeamAbbrev"}

# Required columns for v0 validation. Conservative subset of the hint set —
# these are the fields the optimizer/projection pipeline cannot function
# without. PENDING verification against a real official DK UFC Classic
# salary CSV; column names may need adjustment (e.g. "Name + ID" vs "Name").
REQUIRED_COLUMNS: tuple[str, ...] = (
    "Position",
    "Name",
    "ID",
    "Salary",
    "Game Info",
    "TeamAbbrev",
)


@dataclass
class SalaryCsvValidationResult:
    is_valid: bool
    missing_columns: list[str] = field(default_factory=list)
    detected_columns: list[str] = field(default_factory=list)
    row_count: int = 0
    error_message: str | None = None


def load_dk_salary_csv(path: str | Path) -> pd.DataFrame:
    """Load a DK UFC Classic salary CSV into a DataFrame.

    v0: returns the raw DataFrame with minimal whitespace cleanup. Schema
    normalization and validation are deferred until tested against a real file.
    """
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    return df


def validate_dk_salary_dataframe(df: pd.DataFrame) -> SalaryCsvValidationResult:
    """Structural validation for an already-loaded DK UFC Classic salary
    DataFrame.

    Same column / row-count checks ``validate_dk_salary_csv`` runs after the
    file-loading step, exposed as a pure function so Slice C service-layer
    composition (``docs/SALARY_PERSISTENCE_DESIGN.md`` §9) can validate a
    DataFrame in hand without re-serializing to disk. Existing
    error-message text is preserved verbatim so the path-based entry point
    behaves identically.
    """
    detected = [c.strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in detected]
    row_count = int(len(df))

    if missing:
        return SalaryCsvValidationResult(
            is_valid=False,
            missing_columns=missing,
            detected_columns=detected,
            row_count=row_count,
            error_message=(
                "File does not look like an official DK UFC Classic salary "
                f"CSV. Missing required column(s): {', '.join(missing)}. "
                f"Expected: {', '.join(REQUIRED_COLUMNS)}."
            ),
        )

    if row_count == 0:
        return SalaryCsvValidationResult(
            is_valid=False,
            detected_columns=detected,
            row_count=0,
            error_message="CSV header is valid but contains zero data rows.",
        )

    return SalaryCsvValidationResult(
        is_valid=True,
        missing_columns=[],
        detected_columns=detected,
        row_count=row_count,
    )


def validate_dk_salary_csv(path: str | Path) -> SalaryCsvValidationResult:
    """Validate that a CSV at ``path`` looks like an official DK UFC Classic
    salary export.

    v0 policy: no custom column mapping. The file must contain the expected
    DK column header names verbatim (after whitespace trim). Non-DK / custom
    files are rejected with a helpful error message.
    """
    p = Path(path)
    if not p.exists():
        return SalaryCsvValidationResult(
            is_valid=False,
            error_message=f"File not found: {p}",
        )

    try:
        df = pd.read_csv(p)
    except pd.errors.EmptyDataError:
        return SalaryCsvValidationResult(
            is_valid=False,
            error_message=(
                "CSV is empty. Expected an official DK UFC Classic salary "
                "export with columns: " + ", ".join(REQUIRED_COLUMNS)
            ),
        )
    except Exception as exc:
        return SalaryCsvValidationResult(
            is_valid=False,
            error_message=f"Could not parse CSV: {exc}",
        )

    return validate_dk_salary_dataframe(df)


@dataclass(frozen=True)
class ParsedSalaryRow:
    """Typed representation of a single DK UFC Classic salary CSV row.

    Slice A: parser output only. Not yet wired to any repository write path.
    """

    fighter_name: str
    salary: int
    roster_position: str | None
    game_info: str | None
    source_row_number: int


def _coerce_salary(raw: object, source_row_number: int) -> int:
    if raw is None:
        raise SalaryParseError(
            f"Row {source_row_number}: salary is missing."
        )
    if isinstance(raw, float):
        # pandas NaN check
        if raw != raw:  # noqa: PLR0124
            raise SalaryParseError(
                f"Row {source_row_number}: salary is missing."
            )
    text = str(raw).strip()
    if text == "" or text.lower() == "nan":
        raise SalaryParseError(
            f"Row {source_row_number}: salary is missing."
        )
    # Permit common DK-adjacent formatting ($, commas) without inferring units.
    cleaned = text.replace("$", "").replace(",", "").strip()
    try:
        # Reject floats like "9000.5" but accept "9000" or "9000.0".
        as_float = float(cleaned)
    except ValueError as exc:
        raise SalaryParseError(
            f"Row {source_row_number}: salary {raw!r} is not an integer."
        ) from exc
    if not as_float.is_integer():
        raise SalaryParseError(
            f"Row {source_row_number}: salary {raw!r} is not an integer."
        )
    salary_int = int(as_float)
    if salary_int < 0:
        raise SalaryParseError(
            f"Row {source_row_number}: salary {salary_int} is negative."
        )
    return salary_int


def _optional_text(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, float) and raw != raw:  # NaN  # noqa: PLR0124
        return None
    text = str(raw).strip()
    if text == "" or text.lower() == "nan":
        return None
    return text


def parse_dk_salary_rows(df: pd.DataFrame) -> list[ParsedSalaryRow]:
    """Parse a validated DK UFC Classic salary DataFrame into typed records.

    Input contract: ``df`` should already have passed ``validate_dk_salary_csv``
    (required columns present, non-empty). This function does not write to any
    database, does not perform fuzzy matching, and does not infer unsupported
    fields. Unknown columns are ignored.

    Row-level rules:
      - ``Name`` (fighter name) must be non-empty after trim.
      - ``Salary`` must parse to a non-negative integer (``$`` and ``,`` are
        tolerated as formatting, consistent with the existing validation flow
        which accepts them at the header layer).
      - Duplicate fighter names within one parsed file raise ``SalaryParseError``.
      - ``Position`` (roster position) and ``Game Info`` are preserved when
        present; missing/blank values become ``None`` rather than empty strings.

    Returns rows in the original file order. ``source_row_number`` is 1-based
    over data rows (header row excluded), matching how a spreadsheet user would
    refer to row N in the CSV body.
    """
    if "Name" not in df.columns or "Salary" not in df.columns:
        raise SalaryParseError(
            "DataFrame is missing required columns 'Name' and/or 'Salary'. "
            "Run validate_dk_salary_csv before parse_dk_salary_rows."
        )

    parsed: list[ParsedSalaryRow] = []
    seen_names: dict[str, int] = {}

    for offset, (_idx, row) in enumerate(df.iterrows(), start=1):
        name_raw = row.get("Name")
        name = _optional_text(name_raw)
        if name is None:
            raise SalaryParseError(
                f"Row {offset}: fighter name is missing or blank."
            )

        salary = _coerce_salary(row.get("Salary"), offset)

        if name in seen_names:
            raise SalaryParseError(
                f"Row {offset}: duplicate fighter name {name!r} "
                f"(also appears at row {seen_names[name]})."
            )
        seen_names[name] = offset

        parsed.append(
            ParsedSalaryRow(
                fighter_name=name,
                salary=salary,
                roster_position=_optional_text(row.get("Position"))
                if "Position" in df.columns
                else None,
                game_info=_optional_text(row.get("Game Info"))
                if "Game Info" in df.columns
                else None,
                source_row_number=offset,
            )
        )

    return parsed
