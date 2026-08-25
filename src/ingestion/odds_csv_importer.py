"""Odds CSV importer.

STATUS: v0 skeleton. Not validated against a real Odds API / Google Sheets
CSV export yet. Do not claim complete until tested against a real file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS: tuple[str, ...] = (
    "fighter",
    "moneyline",
    "source",
    "timestamp",
)

PREFERRED_OPTIONAL_COLUMNS: tuple[str, ...] = (
    "opponent",
    "bookmaker",
)


@dataclass
class OddsCsvValidationResult:
    is_valid: bool
    missing_columns: list[str] = field(default_factory=list)
    detected_columns: list[str] = field(default_factory=list)
    row_count: int = 0
    warning_messages: list[str] = field(default_factory=list)
    error_message: str | None = None


def load_odds_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    return df


def parse_moneyline(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = int(value)
        return n if n != 0 else None
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("+"):
        s = s[1:]
    try:
        n = int(s)
    except ValueError:
        try:
            n = int(float(s))
        except ValueError:
            return None
    if n == 0:
        return None
    return n


def validate_odds_csv(path: str | Path) -> OddsCsvValidationResult:
    """Validate that a CSV at ``path`` looks like a usable v0 odds CSV.

    v0 policy: minimum required columns are fighter, moneyline, source,
    timestamp. Extra columns are allowed and reported. Preferred optional
    columns (opponent, bookmaker) are reported as warnings if missing.
    """
    p = Path(path)
    if not p.exists():
        return OddsCsvValidationResult(
            is_valid=False,
            error_message=f"File not found: {p}",
        )

    try:
        df = pd.read_csv(p)
    except pd.errors.EmptyDataError:
        return OddsCsvValidationResult(
            is_valid=False,
            error_message=(
                "CSV is empty. Expected an odds CSV with at least columns: "
                + ", ".join(REQUIRED_COLUMNS)
            ),
        )
    except Exception as exc:
        return OddsCsvValidationResult(
            is_valid=False,
            error_message=f"Could not parse CSV: {exc}",
        )

    detected = [c.strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in detected]
    row_count = int(len(df))
    warnings: list[str] = []

    if missing:
        return OddsCsvValidationResult(
            is_valid=False,
            missing_columns=missing,
            detected_columns=detected,
            row_count=row_count,
            error_message=(
                "Odds CSV is missing required column(s): "
                f"{', '.join(missing)}. Required: {', '.join(REQUIRED_COLUMNS)}."
            ),
        )

    if row_count == 0:
        return OddsCsvValidationResult(
            is_valid=False,
            detected_columns=detected,
            row_count=0,
            error_message="CSV header is valid but contains zero data rows.",
        )

    for opt in PREFERRED_OPTIONAL_COLUMNS:
        if opt not in detected:
            warnings.append(
                f"Preferred optional column '{opt}' not present."
            )

    bad_rows: list[int] = []
    for idx, value in enumerate(df["moneyline"].tolist()):
        if parse_moneyline(value) is None:
            bad_rows.append(idx)

    if bad_rows:
        preview = ", ".join(str(i) for i in bad_rows[:5])
        return OddsCsvValidationResult(
            is_valid=False,
            detected_columns=detected,
            row_count=row_count,
            warning_messages=warnings,
            error_message=(
                f"Invalid or missing moneyline value(s) in {len(bad_rows)} "
                f"row(s) (0-indexed: {preview}"
                f"{'...' if len(bad_rows) > 5 else ''})."
            ),
        )

    extras = [
        c for c in detected
        if c not in REQUIRED_COLUMNS and c not in PREFERRED_OPTIONAL_COLUMNS
    ]
    if extras:
        warnings.append(
            "Extra column(s) present (will be ignored by v0 importer): "
            + ", ".join(extras)
        )

    return OddsCsvValidationResult(
        is_valid=True,
        missing_columns=[],
        detected_columns=detected,
        row_count=row_count,
        warning_messages=warnings,
    )
