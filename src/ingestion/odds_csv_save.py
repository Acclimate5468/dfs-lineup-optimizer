"""CSV odds save helper.

Persists rows from a validated odds CSV upload into the ``odds_rows`` table
via ``OddsRowRepository.create_or_get`` so re-uploading the same CSV is
idempotent on ``(slate_id, odds_row_key)``. Scope is intentionally narrow:

- Write path only — no match results, no overrides, no projection wiring.
- ``source`` is recorded as ``csv:<csv_source>`` (or plain ``csv`` if the
  row's source cell is blank), keeping CSV-origin rows distinguishable from
  manual entries (``source = 'manual'``) per
  ``docs/ODDS_PERSISTENCE_DESIGN.md`` §5.1.
- Optional ``opponent`` / ``bookmaker`` columns are passed through when
  present and non-blank; the same row-key formula is used whether or not
  ``bookmaker`` is supplied (see ``compute_odds_row_key`` in
  :mod:`src.ingestion.odds_row_key`).
- Per-row validation errors are collected, not raised, so a single bad row
  does not abort the batch.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.db.repositories import OddsRowRecord, OddsRowRepository
from src.ingestion.odds_csv_importer import parse_moneyline
from src.ingestion.odds_row_key import compute_odds_row_key


@dataclass(frozen=True)
class CsvOddsSaveResult:
    saved: list[OddsRowRecord]
    already_existed: list[OddsRowRecord]
    failures: list[tuple[str, str]]

    @property
    def saved_count(self) -> int:
        return len(self.saved)

    @property
    def existing_count(self) -> int:
        return len(self.already_existed)

    @property
    def failure_count(self) -> int:
        return len(self.failures)


def _clean(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def save_csv_odds_rows(
    repo: OddsRowRepository,
    *,
    slate_id: int,
    df: pd.DataFrame,
    import_batch_id: str | None = None,
) -> CsvOddsSaveResult:
    """Persist each row of a validated odds CSV into ``odds_rows``.

    ``df`` is expected to satisfy the v0 required schema (``fighter``,
    ``moneyline``, ``source``, ``timestamp``) — typically the same DataFrame
    that backed a successful :func:`validate_odds_csv` call. Optional
    ``opponent`` and ``bookmaker`` columns are saved through when present
    and non-blank. Per-row errors land in ``failures`` and the batch
    continues.
    """
    saved: list[OddsRowRecord] = []
    existed: list[OddsRowRecord] = []
    failures: list[tuple[str, str]] = []

    columns = set(df.columns)
    has_opponent = "opponent" in columns
    has_bookmaker = "bookmaker" in columns

    for idx, row in df.iterrows():
        fighter = _clean(row.get("fighter"))
        captured_at = _clean(row.get("timestamp"))
        csv_source = _clean(row.get("source"))
        moneyline_raw = row.get("moneyline")
        bookmaker = _clean(row.get("bookmaker")) if has_bookmaker else ""
        opponent = _clean(row.get("opponent")) if has_opponent else ""

        label = fighter or f"row #{idx}"

        try:
            ml = parse_moneyline(moneyline_raw)
            if ml is None:
                raise ValueError(
                    f"moneyline {moneyline_raw!r} is not a valid non-zero integer"
                )

            source = f"csv:{csv_source}" if csv_source else "csv"
            bookmaker_value = bookmaker or None
            opponent_value = opponent or None

            key = compute_odds_row_key(
                fighter_name=fighter,
                bookmaker=bookmaker_value,
                source=source,
                captured_at=captured_at,
            )
            pre_existing = repo.get_by_key(
                slate_id=slate_id, odds_row_key=key
            )
            record = repo.create_or_get(
                slate_id=slate_id,
                fighter_name_raw=fighter,
                american_odds=int(ml),
                source=source,
                captured_at=captured_at,
                bookmaker=bookmaker_value,
                opponent_name_raw=opponent_value,
                import_batch_id=import_batch_id,
                odds_row_key=key,
            )
            if pre_existing is not None:
                existed.append(record)
            else:
                saved.append(record)
        except Exception as exc:  # noqa: BLE001
            failures.append((label, str(exc)))

    return CsvOddsSaveResult(
        saved=saved,
        already_existed=existed,
        failures=failures,
    )
