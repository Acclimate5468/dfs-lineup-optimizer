"""DK salary import service.

Slice C of ``docs/SALARY_PERSISTENCE_DESIGN.md`` §9: compose the existing
DataFrame structural validator, the typed-record parser, and
``FighterRepository.upsert_for_slate`` into a single transactional
service entry point. UI wiring (Slice D) and AppTest coverage (Slice E)
are explicitly out of scope here.

The service does not introduce new salary semantics: it reuses
``validate_dk_salary_dataframe`` and ``parse_dk_salary_rows`` verbatim,
and delegates every fighter row write to
``FighterRepository.upsert_for_slate`` so any future change to upsert
behavior (idempotence, ID preservation, inactive-marking) flows through
one canonical write path.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pandas as pd

from src.db.repositories import FighterRepository, FighterUpsertResult
from src.ingestion.dk_salary_importer import (
    SalaryCsvValidationResult,
    SalaryParseError,
    parse_dk_salary_rows,
    validate_dk_salary_dataframe,
)

IMPORTED = "imported"
VALIDATION_FAILED = "validation_failed"
PARSE_FAILED = "parse_failed"


@dataclass(frozen=True)
class SalaryImportResult:
    """Outcome of one ``import_dk_salary_dataframe`` call.

    ``status`` is one of:

    - ``"imported"``: structural validation, row-level parsing, and the
      slate-scoped upsert all succeeded. ``upsert`` carries the
      :class:`FighterUpsertResult` returned by the repository.
    - ``"validation_failed"``: ``validate_dk_salary_dataframe`` rejected
      the input (missing required column, zero data rows). No DB read
      or write occurred. ``upsert`` is ``None`` and ``error_message``
      mirrors ``validation.error_message``.
    - ``"parse_failed"``: structural validation passed but
      ``parse_dk_salary_rows`` raised ``SalaryParseError`` (blank
      fighter name, non-integer or negative salary, duplicate name).
      No DB write occurred. ``upsert`` is ``None`` and
      ``error_message`` carries the parser's message.

    ``parsed_row_count`` is the count of typed ``ParsedSalaryRow``
    records produced by the parser. On non-``"imported"`` outcomes it
    is 0.
    """

    status: str
    parsed_row_count: int
    validation: SalaryCsvValidationResult
    upsert: FighterUpsertResult | None
    error_message: str | None


def import_dk_salary_dataframe(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    df: pd.DataFrame,
) -> SalaryImportResult:
    """Validate, parse, and persist DK UFC Classic salary rows for one slate.

    Pipeline (Slice C of ``docs/SALARY_PERSISTENCE_DESIGN.md`` §9):

      1. ``validate_dk_salary_dataframe`` — structural column / row-count
         check on the in-memory DataFrame. Failure returns a result with
         ``status='validation_failed'``; the DB is neither read nor
         written.
      2. ``parse_dk_salary_rows`` — typed-record parsing with row-level
         rules (non-empty name, integer non-negative salary, no
         duplicates within the file). ``SalaryParseError`` is captured
         and returned as ``status='parse_failed'``; still no DB write.
      3. ``FighterRepository.upsert_for_slate`` — the single write path.
         The repository already wraps its INSERT / UPDATE / deactivate
         pass in ``with self.conn:`` (design §5), so the whole upsert is
         atomic: a mid-pass failure rolls back the entire import and
         leaves prior persisted state for the slate intact.

    Transaction story:

    - Validation and parsing happen before any DB write, so a failure
      in either stage cannot leave fighter rows half-changed.
    - The service does **not** open its own ``with conn:`` block. The
      repository owns the single write transaction, which matches the
      surrounding service pattern of "one composed write = one
      transaction" and avoids nested-``with`` surprises on the sqlite3
      connection.
    - Repository-level ``ValueError`` (unknown ``slate_id``, duplicate
      names slipping past the parser, empty ``parsed_rows``, wrong row
      type) propagates unchanged. These represent programmer / wiring
      errors, not data-shape feedback, so they surface as exceptions
      rather than result-object branches.

    Out of scope (design §8, ``docs/DEVELOPMENT_NOTES.md`` §10): the service does not
    recompute ``odds_match_results``, does not rewrite
    ``manual_match_overrides``, does not infer fight groups from
    ``Game Info``, and is not wired into any Streamlit page — UI wiring
    is Slice D.
    """
    validation = validate_dk_salary_dataframe(df)
    if not validation.is_valid:
        return SalaryImportResult(
            status=VALIDATION_FAILED,
            parsed_row_count=0,
            validation=validation,
            upsert=None,
            error_message=validation.error_message,
        )

    try:
        parsed_rows = parse_dk_salary_rows(df)
    except SalaryParseError as exc:
        return SalaryImportResult(
            status=PARSE_FAILED,
            parsed_row_count=0,
            validation=validation,
            upsert=None,
            error_message=str(exc),
        )

    upsert_result = FighterRepository(conn).upsert_for_slate(
        slate_id=slate_id,
        parsed_rows=parsed_rows,
    )

    return SalaryImportResult(
        status=IMPORTED,
        parsed_row_count=len(parsed_rows),
        validation=validation,
        upsert=upsert_result,
        error_message=None,
    )
