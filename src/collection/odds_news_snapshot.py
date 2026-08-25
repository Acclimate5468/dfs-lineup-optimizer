"""Odds / news snapshot parser + validator (pure, no I/O beyond given bytes).

Implements ``docs/ODDS_NEWS_SNAPSHOT_DESIGN.md`` §4 (the S3 slice). This is the
pure parse → validate layer for the one normalized fight-week odds/news
snapshot format (§3). It is modelled on the sibling
``src/collection/source_manifest.py`` and on the existing
``dk_salary_importer`` / ``odds_csv_importer`` validate→load split.

Hard boundaries (kept deliberately narrow, per design §1.2 and docs/DEVELOPMENT_NOTES.md §3):
  - **No network / scraping / API.** It only consumes the bytes/str it is
    given (or reads one local file via :func:`load_snapshot`). It never fetches
    a URL, even those stored in the snapshot.
  - **No DB writes, no schema, no migration.** Returns plain dataclasses;
    persistence is a later slice (design §8, S5).
  - **No projection / optimizer change.** Props (`itd_odds` / `goes_distance` /
    `decision_odds`) are validated and carried but inert (design §6).
  - **Raw is canonical; derived is advisory.** The American ``moneyline`` is
    the source of truth; implied win probability is **derived here** via the
    shared :func:`american_to_implied_probability`. A snapshot-supplied
    ``implied_probability`` is only cross-checked (mismatch → warning), never
    trusted over the app's own derivation (design §2 #1, §4).
  - **No fuzzy matching to a slate.** Name normalization is used only to flag
    duplicate fighters / one-sided bouts *within* the snapshot. Binding to a DK
    slate stays a later UI step (design §5).

Two tiers of failure, mirroring the manifest parser:
  - Document-level problems raise :class:`SnapshotFormatError` from
    :func:`parse_snapshot` — unparseable JSON, a non-object top level, or a
    missing/unsupported ``schema_version`` / ``snapshot_kind`` (the format
    gate, design §4).
  - Content-level problems are *collected* by :func:`validate_snapshot` into a
    :class:`SnapshotValidationReport` (``errors`` reject, ``warnings`` keep) so
    a reviewer sees every issue at once instead of failing on the first.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.collection.source_manifest import CANONICAL_CATEGORIES, normalize_category
from src.ingestion.name_matching import normalize_name_aggressive
from src.projections.implied_probability import american_to_implied_probability


class SnapshotFormatError(ValueError):
    """Raised when a snapshot document cannot be interpreted at all.

    Document-level failures only: invalid JSON, a non-object top level, or a
    missing/unsupported ``schema_version`` / ``snapshot_kind``. Per-field
    problems (missing identity, bad moneyline, unknown enums, staleness) are
    *not* raised — they are collected into a :class:`SnapshotValidationReport`
    so the caller can review every issue at once.
    """


# --- Format gate -----------------------------------------------------------

SUPPORTED_SCHEMA_VERSION = 1
KNOWN_SNAPSHOT_KINDS: tuple[str, ...] = ("odds_news",)

# --- Enumerations (design §3) ---------------------------------------------

COLLECTION_METHODS: tuple[str, ...] = ("manual", "file_upload", "fetcher")
LINE_MOVEMENTS: tuple[str, ...] = ("toward", "away", "flat", "unknown")
NEWS_FLAGS: tuple[str, ...] = (
    "injury",
    "replacement",
    "withdrawal",
    "short_notice",
    "weight_miss",
    "reschedule",
    "other",
)
ENTRY_STATUSES: tuple[str, ...] = (
    "ok",
    "needs_review",
    "conflict",
    "unmatched",
    "stale",
)
ENTRY_KINDS: tuple[str, ...] = ("odds", "news_only")

# --- Tunables (design §4 / §3.6) ------------------------------------------

# Advisory implied-probability tolerance: source vs app-derived disagreement
# beyond this is surfaced as a warning (design §4: "e.g. > 0.03").
IMPLIED_PROB_TOLERANCE = 0.03

# Staleness defaults applied when ``staleness_policy`` is absent (design §3.6).
DEFAULT_WARN_AFTER_HOURS = 12.0
DEFAULT_BLOCK_AFTER_EVENT_START = True

# Defensive free-text cap (design §4 "length-capped … defensive, not semantic").
MAX_FREE_TEXT_LEN = 2000

# Control characters stripped from all text at parse (C0, DEL, C1). Tab / LF /
# CR are intentionally excluded here and instead collapsed to a single space by
# ``_WHITESPACE_RUN`` in free-text fields.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_WHITESPACE_RUN = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Dataclasses (design §4 public surface)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedSnapshot:
    """Output of :func:`parse_snapshot`: a format-gated, decoded snapshot.

    Holds the validated ``schema_version`` / ``snapshot_kind`` plus the full
    decoded top-level object (``data``). All deeper field validation happens in
    :func:`validate_snapshot`; this stage only proves the document is JSON, an
    object, and of a supported kind/version.
    """

    schema_version: int
    snapshot_kind: str
    data: dict


@dataclass(frozen=True)
class SourceChecked:
    """One ``sources_checked[]`` provenance row (design §3.4)."""

    name: str
    url: str | None = None
    category: str | None = None
    checked_at: str | None = None


@dataclass(frozen=True)
class GoesDistance:
    """``goes_distance`` prop: American odds for the fight going the distance."""

    yes: int | None = None
    no: int | None = None


@dataclass(frozen=True)
class SnapshotEnvelope:
    """Validated envelope (design §3.1–§3.4, §3.6).

    ``collected_at_dt`` / ``event_date_value`` are the parsed forms, present
    only when the raw fields validated. ``warn_after_hours`` /
    ``block_after_event_start`` are the *resolved* policy (file value or app
    default).
    """

    event_name: str | None = None
    event_date: str | None = None
    event_date_value: date | None = None
    dk_game_info_hint: str | None = None
    event_id: str | None = None
    collected_at: str | None = None
    collected_at_dt: datetime | None = None
    collected_by_method: str | None = None
    collected_by_agent: str | None = None
    collected_by_tool_version: str | None = None
    sources_checked: tuple[SourceChecked, ...] = ()
    warn_after_hours: float = DEFAULT_WARN_AFTER_HOURS
    block_after_event_start: bool = DEFAULT_BLOCK_AFTER_EVENT_START
    notes: str | None = None


@dataclass(frozen=True)
class SnapshotEntry:
    """One validated per-fighter-side entry (design §3.5).

    ``moneyline`` is the canonical raw line; ``derived_implied_probability`` is
    the app's own derivation from it (None for a ``news_only`` entry with no
    moneyline). ``implied_probability`` / ``no_vig_probability`` are the
    snapshot's *advisory* values, kept for cross-checking only.
    """

    entry_index: int
    entry_kind: str
    fighter_name: str
    opponent_name: str
    dk_name_hint: str | None = None
    moneyline: int | None = None
    derived_implied_probability: float | None = None
    implied_probability: float | None = None
    no_vig_probability: float | None = None
    book: str | None = None
    line_open: int | None = None
    line_current: int | None = None
    line_movement: str | None = None
    itd_odds: int | None = None
    decision_odds: int | None = None
    goes_distance: GoesDistance | None = None
    news_flags: tuple[str, ...] = ()
    news_note: str | None = None
    news_source: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    collected_at: str | None = None
    collected_at_dt: datetime | None = None
    confidence: float | None = None
    status: str | None = None
    is_stale: bool = False


@dataclass
class SnapshotSummary:
    """Aggregate counts for a parse result (design §4 ``summary``)."""

    total_entries: int = 0
    ok_entries: int = 0
    rejected_entries: int = 0
    odds_entries: int = 0
    news_only_entries: int = 0
    stale_entries: int = 0
    error_count: int = 0
    warning_count: int = 0


@dataclass
class SnapshotValidationReport:
    """Structured outcome of validating a parsed snapshot (design §4).

    ``entries_ok`` holds entries that passed (they may still carry warnings);
    an entry with any hard error is excluded and its messages land in
    ``errors``. ``warnings`` are kept-but-surfaced findings. ``is_valid`` is
    True exactly when there are no errors.
    """

    envelope: SnapshotEnvelope | None = None
    entries_ok: list[SnapshotEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary: SnapshotSummary = field(default_factory=SnapshotSummary)

    @property
    def is_valid(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Small coercion / validation helpers
# ---------------------------------------------------------------------------


def _required_text(value: object) -> str | None:
    """Trimmed, control-stripped string for required identity fields.

    Returns ``None`` if the value is not a usable string (so a stray number or
    bool is treated as missing). Case, punctuation and internal spacing are
    preserved — identity names round-trip verbatim apart from control chars
    (design §4 "raw names preserved verbatim").
    """
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    return cleaned or None


def _sanitize_text(value: object, *, max_len: int = MAX_FREE_TEXT_LEN) -> str | None:
    """Defensive clean for free-text fields: strip control chars, collapse
    whitespace, length-cap (design §4 / §7 #6). Non-strings → ``None``.
    """
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_CHARS.sub(" ", value)
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned).strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def _validate_probability(value: object) -> tuple[float | None, str | None]:
    """Validate an advisory probability. Returns (value, error_reason)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "must be a number in [0, 1]"
    f = float(value)
    if f < 0.0 or f > 1.0:
        return None, "must be within [0, 1]"
    return f, None


def _is_int_odds(value: object) -> bool:
    """True for a genuine int (excludes bool, which is an int subclass)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _optional_int_odds(
    value: object, field_name: str, label: str, warnings: list[str]
) -> int | None:
    """Coerce an optional American-odds int. Invalid → warn + drop (not a hard
    error: optional odds aren't on the design §4 reject list).
    """
    if value is None:
        return None
    if not _is_int_odds(value):
        warnings.append(
            f"{label}: {field_name} should be an integer (American odds); "
            f"got {value!r}; ignored."
        )
        return None
    if value == 0:
        warnings.append(f"{label}: {field_name} cannot be 0; ignored.")
        return None
    return value


def _parse_utc_timestamp(value: object) -> tuple[datetime | None, str | None]:
    """Parse an ISO-8601 **UTC** timestamp. Returns (datetime, error_reason).

    Accepts a trailing ``Z`` or an explicit ``+00:00`` offset; rejects naive
    timestamps and any non-UTC offset (design §3.1).
    """
    if not isinstance(value, str):
        return None, "must be an ISO-8601 UTC string"
    text = value.strip()
    if not text:
        return None, "must be an ISO-8601 UTC string"
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None, "is not a valid ISO-8601 timestamp"
    if dt.tzinfo is None:
        return None, "must include a UTC offset (got a naive timestamp)"
    if dt.utcoffset() != timedelta(0):
        return None, "must be UTC (offset +00:00 / 'Z')"
    return dt, None


def _parse_event_date(value: object) -> tuple[date | None, str | None]:
    """Parse a ``YYYY-MM-DD`` event date. Returns (date, error_reason)."""
    if not isinstance(value, str):
        return None, "must be a YYYY-MM-DD string"
    try:
        return date.fromisoformat(value.strip()), None
    except ValueError:
        return None, "must be a valid YYYY-MM-DD date"


def _derive_line_movement(line_open: int, line_current: int) -> str:
    """Derive movement direction for *this* fighter from open vs current line.

    "toward" = implied probability rose (money came in on this fighter);
    "away" = it fell; "flat" = unchanged within a tiny epsilon (design §3.5).
    """
    try:
        delta = american_to_implied_probability(
            line_current
        ) - american_to_implied_probability(line_open)
    except ValueError:
        return "unknown"
    if delta > 0.001:
        return "toward"
    if delta < -0.001:
        return "away"
    return "flat"


def _normalize_now(now: datetime | None) -> datetime:
    """Resolve the staleness reference time to an aware UTC datetime."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# parse_snapshot — the format gate (raises)
# ---------------------------------------------------------------------------


def parse_snapshot(raw: bytes | str) -> ParsedSnapshot:
    """Decode + format-gate a snapshot. Raises :class:`SnapshotFormatError`.

    Network-free, does no file I/O — it only parses the bytes/str it is given.
    Use :func:`load_snapshot` to read a file first. Raises for: non-UTF-8
    bytes, invalid JSON, a non-object top level, or a missing/unsupported
    ``schema_version`` / ``snapshot_kind`` (design §4). Deeper validation is
    :func:`validate_snapshot`'s job.
    """
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SnapshotFormatError(
                f"Snapshot bytes are not valid UTF-8: {exc}"
            ) from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise SnapshotFormatError(
            f"Snapshot input must be str or bytes, got {type(raw).__name__}."
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnapshotFormatError(f"Snapshot is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise SnapshotFormatError(
            f"Snapshot top level must be a JSON object, got {type(data).__name__}."
        )

    raw_version = data.get("schema_version")
    if not _is_int_odds(raw_version) or raw_version != SUPPORTED_SCHEMA_VERSION:
        raise SnapshotFormatError(
            f"Unsupported or missing schema_version {raw_version!r}; "
            f"expected {SUPPORTED_SCHEMA_VERSION}."
        )

    raw_kind = data.get("snapshot_kind")
    kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else None
    if kind not in KNOWN_SNAPSHOT_KINDS:
        raise SnapshotFormatError(
            f"Unsupported or missing snapshot_kind {raw_kind!r}; "
            f"expected one of {KNOWN_SNAPSHOT_KINDS}."
        )

    return ParsedSnapshot(schema_version=raw_version, snapshot_kind=kind, data=data)


def load_snapshot(path: str | Path) -> ParsedSnapshot:
    """Read a snapshot JSON file and format-gate it.

    Raises :class:`SnapshotFormatError` when the file is missing, unreadable,
    or fails the :func:`parse_snapshot` gate. Makes no network call.
    """
    p = Path(path)
    if not p.exists():
        raise SnapshotFormatError(f"Snapshot file not found: {p}")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotFormatError(f"Could not read snapshot {p}: {exc}") from exc
    return parse_snapshot(raw)


# ---------------------------------------------------------------------------
# Envelope validation
# ---------------------------------------------------------------------------


def _validate_sources_checked(
    raw: object, report: SnapshotValidationReport
) -> tuple[SourceChecked, ...]:
    """Validate ``sources_checked[]``. Problems warn (kept/skipped), never
    reject the snapshot — provenance metadata is not on the §4 reject list.
    """
    out: list[SourceChecked] = []
    if raw is None:
        report.warnings.append(
            "Envelope 'sources_checked' is missing; no sources recorded."
        )
        return ()
    if not isinstance(raw, list):
        report.warnings.append(
            "Envelope 'sources_checked' is not a JSON array; ignored."
        )
        return ()
    if not raw:
        report.warnings.append(
            "Envelope 'sources_checked' is empty; no sources recorded."
        )
        return ()

    for offset, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            report.warnings.append(
                f"sources_checked #{offset}: expected a JSON object; skipped."
            )
            continue
        name = _required_text(item.get("name"))
        if name is None:
            report.warnings.append(
                f"sources_checked #{offset}: missing 'name'; skipped."
            )
            continue
        category = None
        raw_category = item.get("category")
        if raw_category is not None:
            category = normalize_category(raw_category)
            if category not in CANONICAL_CATEGORIES:
                report.warnings.append(
                    f"sources_checked #{offset} ({name!r}): unknown category "
                    f"{category!r}; kept."
                )
        checked_at = None
        raw_checked = item.get("checked_at")
        if raw_checked is not None:
            dt, err = _parse_utc_timestamp(raw_checked)
            if err:
                report.warnings.append(
                    f"sources_checked #{offset} ({name!r}): checked_at {err}; ignored."
                )
            else:
                checked_at = raw_checked.strip()
        out.append(
            SourceChecked(
                name=name,
                url=_sanitize_text(item.get("url")),
                category=category,
                checked_at=checked_at,
            )
        )
    return tuple(out)


def _resolve_staleness_policy(
    raw: object, report: SnapshotValidationReport
) -> tuple[float, bool]:
    """Resolve ``staleness_policy`` to (warn_after_hours, block_after_event_start).

    Invalid values warn and fall back to the app default (not the §4 reject
    list).
    """
    warn_hours = DEFAULT_WARN_AFTER_HOURS
    block_after = DEFAULT_BLOCK_AFTER_EVENT_START
    if raw is None:
        return warn_hours, block_after
    if not isinstance(raw, dict):
        report.warnings.append(
            "Envelope 'staleness_policy' is not a JSON object; using defaults."
        )
        return warn_hours, block_after

    if "warn_after_hours" in raw:
        value = raw.get("warn_after_hours")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            report.warnings.append(
                f"staleness_policy.warn_after_hours {value!r} is invalid; "
                f"using default {DEFAULT_WARN_AFTER_HOURS}."
            )
        else:
            warn_hours = float(value)

    if "block_after_event_start" in raw:
        value = raw.get("block_after_event_start")
        if isinstance(value, bool):
            block_after = value
        else:
            report.warnings.append(
                f"staleness_policy.block_after_event_start {value!r} is invalid; "
                f"using default {DEFAULT_BLOCK_AFTER_EVENT_START}."
            )
    return warn_hours, block_after


def _validate_envelope(
    data: dict, report: SnapshotValidationReport
) -> SnapshotEnvelope:
    """Validate the envelope into a :class:`SnapshotEnvelope`, collecting
    missing/invalid required fields as errors (design §3.1–§3.4, §4).
    """
    # --- event identity ---
    event = data.get("event")
    event_name = event_date_str = dk_hint = event_id = None
    event_date_value = None
    if not isinstance(event, dict):
        report.errors.append("Envelope 'event' is missing or not a JSON object.")
    else:
        event_name = _required_text(event.get("name"))
        if event_name is None:
            report.errors.append("Envelope 'event.name' is required.")
        raw_date = event.get("date")
        if _required_text(raw_date) is None:
            report.errors.append("Envelope 'event.date' is required.")
        else:
            event_date_value, err = _parse_event_date(raw_date)
            if err:
                report.errors.append(f"Envelope 'event.date' {err}.")
            else:
                event_date_str = raw_date.strip()
        dk_hint = _sanitize_text(event.get("dk_game_info_hint"))
        event_id = _sanitize_text(event.get("event_id"))

    # --- snapshot-level collected_at ---
    collected_at_str = None
    collected_at_dt = None
    raw_collected = data.get("collected_at")
    if _required_text(raw_collected) is None and not isinstance(
        raw_collected, (int, float)
    ):
        report.errors.append("Envelope 'collected_at' is required.")
    else:
        collected_at_dt, err = _parse_utc_timestamp(raw_collected)
        if err:
            report.errors.append(f"Envelope 'collected_at' {err}.")
        else:
            collected_at_str = raw_collected.strip()

    # --- provenance ---
    method = agent = tool_version = None
    collected_by = data.get("collected_by")
    if not isinstance(collected_by, dict):
        report.errors.append(
            "Envelope 'collected_by' is missing or not a JSON object."
        )
    else:
        raw_method = collected_by.get("method")
        if _required_text(raw_method) is None:
            report.errors.append("Envelope 'collected_by.method' is required.")
        elif not isinstance(raw_method, str):
            report.errors.append("Envelope 'collected_by.method' must be a string.")
        else:
            candidate = raw_method.strip().lower()
            if candidate not in COLLECTION_METHODS:
                report.errors.append(
                    f"Envelope 'collected_by.method' {raw_method!r} is not one of "
                    f"{COLLECTION_METHODS}."
                )
            else:
                method = candidate
        agent = _sanitize_text(collected_by.get("agent"))
        tool_version = _sanitize_text(collected_by.get("tool_version"))

    sources = _validate_sources_checked(data.get("sources_checked"), report)
    warn_hours, block_after = _resolve_staleness_policy(
        data.get("staleness_policy"), report
    )

    # --- whole-snapshot "taken after event" check (design §3.6) ---
    # Only a date is carried, so we flag the unambiguous case: the snapshot was
    # captured on a calendar day strictly after the event date.
    if (
        block_after
        and collected_at_dt is not None
        and event_date_value is not None
        and collected_at_dt.date() > event_date_value
    ):
        report.warnings.append(
            "Snapshot 'collected_at' is after the event date; odds/news may be "
            "post-event (stale)."
        )

    return SnapshotEnvelope(
        event_name=event_name,
        event_date=event_date_str,
        event_date_value=event_date_value,
        dk_game_info_hint=dk_hint,
        event_id=event_id,
        collected_at=collected_at_str,
        collected_at_dt=collected_at_dt,
        collected_by_method=method,
        collected_by_agent=agent,
        collected_by_tool_version=tool_version,
        sources_checked=sources,
        warn_after_hours=warn_hours,
        block_after_event_start=block_after,
        notes=_sanitize_text(data.get("notes")),
    )


# ---------------------------------------------------------------------------
# Entry validation
# ---------------------------------------------------------------------------


def _validate_entry(
    raw: object, index: int, envelope: SnapshotEnvelope, now: datetime
) -> tuple[SnapshotEntry | None, list[str], list[str]]:
    """Validate one entry. Returns (entry|None, errors, warnings).

    An entry with any hard error returns ``None`` (rejected); otherwise the
    built :class:`SnapshotEntry` is returned. ``errors`` / ``warnings`` are the
    per-entry findings the caller folds into the report.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(raw, dict):
        errors.append(
            f"Entry #{index}: expected a JSON object, got {type(raw).__name__}."
        )
        return None, errors, warnings

    label = f"Entry #{index}"

    # --- entry_kind (default 'odds') ---
    kind = "odds"
    raw_kind = raw.get("entry_kind")
    if raw_kind is not None:
        if not isinstance(raw_kind, str):
            errors.append(f"{label}: entry_kind must be a string.")
        else:
            candidate = raw_kind.strip().lower()
            if candidate and candidate not in ENTRY_KINDS:
                errors.append(
                    f"{label}: unknown entry_kind {raw_kind!r}; "
                    f"expected one of {ENTRY_KINDS}."
                )
            elif candidate:
                kind = candidate
    is_news_only = kind == "news_only"

    # --- identity (always required) ---
    fighter_name = _required_text(raw.get("fighter_name"))
    if fighter_name is None:
        errors.append(f"{label}: 'fighter_name' is required.")
    else:
        label = f"Entry #{index} ({fighter_name!r})"
    opponent_name = _required_text(raw.get("opponent_name"))
    if opponent_name is None:
        errors.append(f"{label}: 'opponent_name' is required.")
    dk_name_hint = _sanitize_text(raw.get("dk_name_hint"))

    # --- moneyline (canonical; required unless news_only) ---
    moneyline = None
    raw_ml = raw.get("moneyline")
    if raw_ml is None:
        if not is_news_only:
            errors.append(f"{label}: 'moneyline' is required for an odds entry.")
    elif not _is_int_odds(raw_ml):
        errors.append(
            f"{label}: 'moneyline' must be an integer (American odds), got {raw_ml!r}."
        )
    elif raw_ml == 0:
        errors.append(f"{label}: 'moneyline' cannot be 0.")
    else:
        moneyline = raw_ml

    derived_ip = (
        american_to_implied_probability(moneyline) if moneyline is not None else None
    )

    # --- advisory implied_probability (cross-check only) ---
    implied_probability = None
    if raw.get("implied_probability") is not None:
        value, err = _validate_probability(raw.get("implied_probability"))
        if err:
            errors.append(f"{label}: 'implied_probability' {err}.")
        else:
            implied_probability = value
            if derived_ip is not None and abs(value - derived_ip) > IMPLIED_PROB_TOLERANCE:
                warnings.append(
                    f"{label}: source implied_probability {value:.3f} differs from "
                    f"app-derived {derived_ip:.3f} by more than "
                    f"{IMPLIED_PROB_TOLERANCE:.2f}; app value used."
                )

    no_vig_probability = None
    if raw.get("no_vig_probability") is not None:
        value, err = _validate_probability(raw.get("no_vig_probability"))
        if err:
            errors.append(f"{label}: 'no_vig_probability' {err}.")
        else:
            no_vig_probability = value

    confidence = None
    if raw.get("confidence") is not None:
        value, err = _validate_probability(raw.get("confidence"))
        if err:
            errors.append(f"{label}: 'confidence' {err}.")
        else:
            confidence = value

    # --- line open / current / movement ---
    line_open = _optional_int_odds(raw.get("line_open"), "line_open", label, warnings)
    line_current = _optional_int_odds(
        raw.get("line_current"), "line_current", label, warnings
    )
    if line_current is None:
        line_current = moneyline  # §3.5: defaults to moneyline

    line_movement = None
    raw_movement = raw.get("line_movement")
    if raw_movement is not None:
        if not isinstance(raw_movement, str):
            errors.append(f"{label}: line_movement must be a string.")
        else:
            candidate = raw_movement.strip().lower()
            if candidate and candidate not in LINE_MOVEMENTS:
                errors.append(
                    f"{label}: unknown line_movement {raw_movement!r}; "
                    f"expected one of {LINE_MOVEMENTS}."
                )
            elif candidate:
                line_movement = candidate
    if line_movement is None and line_open is not None and line_current is not None:
        line_movement = _derive_line_movement(line_open, line_current)

    # --- prop markets (carried, inert) ---
    itd_odds = _optional_int_odds(raw.get("itd_odds"), "itd_odds", label, warnings)
    decision_odds = _optional_int_odds(
        raw.get("decision_odds"), "decision_odds", label, warnings
    )
    goes_distance = _validate_goes_distance(raw.get("goes_distance"), label, warnings)

    # --- news ---
    news_flags: tuple[str, ...] = ()
    raw_flags = raw.get("news_flags")
    if raw_flags is not None:
        if not isinstance(raw_flags, list):
            errors.append(f"{label}: news_flags must be a list.")
        else:
            collected: list[str] = []
            for flag in raw_flags:
                if not isinstance(flag, str):
                    errors.append(
                        f"{label}: news_flags entries must be strings, got {flag!r}."
                    )
                    continue
                value = flag.strip().lower()
                if not value:
                    continue
                if value not in NEWS_FLAGS:
                    errors.append(
                        f"{label}: unknown news flag {flag!r}; "
                        f"expected one of {NEWS_FLAGS}."
                    )
                    continue
                collected.append(value)
            news_flags = tuple(collected)
    news_note = _sanitize_text(raw.get("news_note"))
    news_source = _sanitize_text(raw.get("news_source"))
    if is_news_only and news_flags and news_note is None:
        warnings.append(f"{label}: news_only entry has news_flags but no news_note.")

    # --- status ---
    status = None
    raw_status = raw.get("status")
    if raw_status is not None:
        if not isinstance(raw_status, str):
            errors.append(f"{label}: status must be a string.")
        else:
            candidate = raw_status.strip().lower()
            if candidate and candidate not in ENTRY_STATUSES:
                errors.append(
                    f"{label}: unknown status {raw_status!r}; "
                    f"expected one of {ENTRY_STATUSES}."
                )
            elif candidate:
                status = candidate

    # --- per-entry provenance / collected_at override ---
    source_name = _sanitize_text(raw.get("source_name"))
    source_url = _sanitize_text(raw.get("source_url"))
    entry_collected_at = None
    entry_collected_dt = None
    raw_entry_collected = raw.get("collected_at")
    if raw_entry_collected is not None:
        dt, err = _parse_utc_timestamp(raw_entry_collected)
        if err:
            errors.append(f"{label}: entry 'collected_at' {err}.")
        else:
            entry_collected_at = raw_entry_collected.strip()
            entry_collected_dt = dt

    # --- entry-level staleness (only for an entry with its own collected_at;
    #     entries inheriting the envelope time are covered by the snapshot-level
    #     check, so we don't double-warn) ---
    is_stale = False
    if entry_collected_dt is not None:
        age_hours = (now - entry_collected_dt).total_seconds() / 3600.0
        if age_hours > envelope.warn_after_hours:
            is_stale = True
            warnings.append(
                f"{label}: entry collected_at is {age_hours:.1f}h old "
                f"(warn threshold {envelope.warn_after_hours:.0f}h)."
            )

    if errors:
        return None, errors, warnings

    entry = SnapshotEntry(
        entry_index=index,
        entry_kind=kind,
        fighter_name=fighter_name,
        opponent_name=opponent_name,
        dk_name_hint=dk_name_hint,
        moneyline=moneyline,
        derived_implied_probability=derived_ip,
        implied_probability=implied_probability,
        no_vig_probability=no_vig_probability,
        book=_sanitize_text(raw.get("book")),
        line_open=line_open,
        line_current=line_current,
        line_movement=line_movement,
        itd_odds=itd_odds,
        decision_odds=decision_odds,
        goes_distance=goes_distance,
        news_flags=news_flags,
        news_note=news_note,
        news_source=news_source,
        source_name=source_name,
        source_url=source_url,
        collected_at=entry_collected_at,
        collected_at_dt=entry_collected_dt,
        confidence=confidence,
        status=status,
        is_stale=is_stale,
    )
    return entry, errors, warnings


def _validate_goes_distance(
    value: object, label: str, warnings: list[str]
) -> GoesDistance | None:
    """Validate the optional ``goes_distance`` prop object. Malformed → warn +
    drop (props are inert, not on the §4 reject list).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        warnings.append(
            f"{label}: goes_distance should be an object with 'yes'/'no' "
            f"American odds; ignored."
        )
        return None
    yes = _optional_int_odds(value.get("yes"), "goes_distance.yes", label, warnings)
    no = _optional_int_odds(value.get("no"), "goes_distance.no", label, warnings)
    if yes is None and no is None:
        return None
    return GoesDistance(yes=yes, no=no)


def _check_bouts_and_duplicates(
    entries: list[SnapshotEntry], report: SnapshotValidationReport
) -> None:
    """Flag duplicate fighters and one-sided/contradictory bouts (design §4).

    Uses the existing aggressive odds normalizer so spelling variants collapse;
    raw names are untouched. A proper two-sided bout has both ``(A, B)`` and
    ``(B, A)`` normalized (fighter, opponent) pairs present.
    """
    seen: dict[str, int] = {}
    pairs: set[tuple[str, str]] = set()
    for entry in entries:
        fighter = normalize_name_aggressive(entry.fighter_name)
        opponent = normalize_name_aggressive(entry.opponent_name)
        if fighter in seen:
            report.warnings.append(
                f"Duplicate fighter across entries: {entry.fighter_name!r} "
                f"(entry #{entry.entry_index}) also appears at entry #{seen[fighter]}."
            )
        else:
            seen[fighter] = entry.entry_index
        pairs.add((fighter, opponent))

    for entry in entries:
        fighter = normalize_name_aggressive(entry.fighter_name)
        opponent = normalize_name_aggressive(entry.opponent_name)
        if (opponent, fighter) not in pairs:
            report.warnings.append(
                f"One-sided or contradictory bout: entry #{entry.entry_index} "
                f"({entry.fighter_name!r} vs {entry.opponent_name!r}) has no "
                f"matching opposite-side entry."
            )


# ---------------------------------------------------------------------------
# validate_snapshot — the content checker (collects)
# ---------------------------------------------------------------------------


def validate_snapshot(
    parsed: ParsedSnapshot, *, now: datetime | None = None
) -> SnapshotValidationReport:
    """Validate a parsed snapshot into a :class:`SnapshotValidationReport`.

    Pure and deterministic: ``now`` (the staleness reference) defaults to the
    current UTC time but can be injected for reproducible tests. Collects
    per-field ``errors`` (reject) and ``warnings`` (keep) plus envelope-level
    findings; never raises on content (design §4).
    """
    now = _normalize_now(now)
    report = SnapshotValidationReport()

    envelope = _validate_envelope(parsed.data, report)
    report.envelope = envelope

    raw_entries = parsed.data.get("entries")
    if not isinstance(raw_entries, list):
        report.errors.append("Envelope 'entries' is missing or not a JSON array.")
        raw_entries = []
    elif not raw_entries:
        report.warnings.append("Snapshot has no entries.")

    entries_ok: list[SnapshotEntry] = []
    for offset, raw_entry in enumerate(raw_entries, start=1):
        entry, entry_errors, entry_warnings = _validate_entry(
            raw_entry, offset, envelope, now
        )
        report.errors.extend(entry_errors)
        report.warnings.extend(entry_warnings)
        if entry is not None:
            entries_ok.append(entry)
    report.entries_ok = entries_ok

    _check_bouts_and_duplicates(entries_ok, report)

    # Snapshot-level staleness (design §4: snapshot collected_at older than
    # warn_after_hours).
    if envelope.collected_at_dt is not None:
        age_hours = (now - envelope.collected_at_dt).total_seconds() / 3600.0
        if age_hours > envelope.warn_after_hours:
            report.warnings.append(
                f"Snapshot collected_at is {age_hours:.1f}h old "
                f"(warn threshold {envelope.warn_after_hours:.0f}h)."
            )

    report.summary = SnapshotSummary(
        total_entries=len(raw_entries),
        ok_entries=len(entries_ok),
        rejected_entries=len(raw_entries) - len(entries_ok),
        odds_entries=sum(1 for e in entries_ok if e.moneyline is not None),
        news_only_entries=sum(1 for e in entries_ok if e.entry_kind == "news_only"),
        stale_entries=sum(1 for e in entries_ok if e.is_stale),
        error_count=len(report.errors),
        warning_count=len(report.warnings),
    )
    return report


def validate_snapshot_text(
    raw: bytes | str, *, now: datetime | None = None
) -> SnapshotValidationReport:
    """Convenience: :func:`parse_snapshot` then :func:`validate_snapshot`.

    Raises :class:`SnapshotFormatError` only for document-level problems;
    content issues are reported inside the result.
    """
    return validate_snapshot(parse_snapshot(raw), now=now)


def validate_snapshot_file(
    path: str | Path, *, now: datetime | None = None
) -> SnapshotValidationReport:
    """Convenience: :func:`load_snapshot` then :func:`validate_snapshot`."""
    return validate_snapshot(load_snapshot(path), now=now)


# ---------------------------------------------------------------------------
# summarize + CLI
# ---------------------------------------------------------------------------


def summarize(report: SnapshotValidationReport) -> str:
    """Render a short text summary of a validation result (counts + issues).

    A local debugging aid only — never used to write files. Makes no network
    call.
    """
    summary = report.summary
    lines: list[str] = []
    if report.envelope is not None and report.envelope.event_name:
        lines.append(
            f"Event: {report.envelope.event_name} "
            f"({report.envelope.event_date or '?'})."
        )
    lines.append(
        f"Entries: {summary.ok_entries} ok / {summary.total_entries} total "
        f"({summary.rejected_entries} rejected; {summary.odds_entries} odds, "
        f"{summary.news_only_entries} news-only, {summary.stale_entries} stale)."
    )
    lines.append(
        f"Issues: {summary.error_count} error(s), {summary.warning_count} warning(s)."
    )
    lines.append(f"Valid: {report.is_valid}.")
    if report.warnings:
        lines.append(f"Warnings ({len(report.warnings)}):")
        lines.extend(f"  - {w}" for w in report.warnings)
    if report.errors:
        lines.append(f"Errors ({len(report.errors)}):")
        lines.extend(f"  - {e}" for e in report.errors)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Network-free CLI: validate a snapshot path and print a summary.

    Usage: ``python -m src.collection.odds_news_snapshot <path-to-snapshot.json>``.
    Returns 0 on a valid snapshot, 1 when content errors were found, and 2 on a
    document-level (format-gate) failure.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Parse and validate an odds/news snapshot (no network, no DB). "
            "Prints a counts + issues summary."
        )
    )
    parser.add_argument("path", help="Path to the snapshot JSON file.")
    args = parser.parse_args(argv)
    try:
        report = validate_snapshot_file(args.path)
    except SnapshotFormatError as exc:
        print(f"error: {exc}")
        return 2
    print(summarize(report))
    return 0 if report.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
