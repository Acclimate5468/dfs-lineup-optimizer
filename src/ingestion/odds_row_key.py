"""Deterministic ``odds_row_key`` helpers.

The canonical key for a raw odds row must survive re-import of the same CSV
so duplicate-detection at the (slate, key) level is stable. See
``docs/ODDS_MATCHING_DESIGN.md`` §6.2 for the wire format.

Two schemes:

- CSV-derived rows use a truncated SHA-1 of
  ``normalize_name(fighter) | bookmaker | source | captured_at``.
- ``ManualOddsEntry`` rows use the literal ``manual:<normalized>:<timestamp>``
  form. Manual entries usually have no bookmaker, so a separate scheme keeps
  the key human-readable and avoids hashing an almost-empty input.
"""

from __future__ import annotations

import hashlib

from src.utils.text_cleaning import normalize_name

_CSV_HASH_LEN = 16


def compute_odds_row_key(
    *,
    fighter_name: str,
    bookmaker: str | None,
    source: str,
    captured_at: str,
) -> str:
    """CSV-style key: truncated SHA-1 of the four-field tuple."""
    raw = "|".join(
        [
            normalize_name(fighter_name),
            (bookmaker or "").strip(),
            (source or "").strip(),
            (captured_at or "").strip(),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:_CSV_HASH_LEN]


def compute_manual_odds_row_key(
    *,
    fighter_name: str,
    captured_at: str,
) -> str:
    """Manual-entry key: ``manual:<normalized_name>:<timestamp>``."""
    return f"manual:{normalize_name(fighter_name)}:{(captured_at or '').strip()}"
