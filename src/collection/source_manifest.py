"""Public-source fight-week collector — source manifest parser/validator.

STATUS: v0 foundation slice. This module parses and validates the public-source
registry (``data/uploads/sources/UFC_DATA.json``) into an in-memory registry.
It is the catalogue layer only. See
``docs/FIGHT_WEEK_SOURCE_COLLECTOR_DESIGN.md``.

Hard boundaries (this slice stays tiny on purpose):
  - **No web requests.** Reads a local JSON file or an in-memory list, nothing
    else. It does not fetch, scrape, log in, or call any API.
  - **No DB writes.** Returns plain dataclasses; persistence is out of scope.
  - **No Streamlit / pandas dependency.** Pure stdlib, so the registry can be
    parsed from a script, a test, or a later service without the UI stack.

It turns the manifest into a validated, normalized, de-duplicated registry plus
a structured report (counts + warnings + errors) for human review. It does NOT
know how to retrieve any source; that is deferred to later, separately designed
slices (design §11).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class SourceManifestError(ValueError):
    """Raised when the manifest file itself cannot be loaded into a list.

    File-level failures (missing file, unreadable file, invalid JSON, or a
    top-level value that is not a JSON array) raise this. Per-record problems
    (missing fields, unknown enum values, duplicates) are *not* raised — they
    are collected into the returned :class:`SourceManifestResult` so the caller
    can review every issue at once instead of failing on the first bad entry.
    """


# Required fields every source entry must carry. Missing or blank -> error.
REQUIRED_FIELDS: tuple[str, ...] = (
    "sport",
    "category",
    "name",
    "type",
    "url",
    "frequency",
)

# Canonical category names. Matching is case-insensitive on the trimmed value;
# an unknown category is preserved verbatim and flagged as a warning (the field
# is present, just unrecognized — safe to keep, worth surfacing).
CANONICAL_CATEGORIES: tuple[str, ...] = (
    "Official",
    "Analytics",
    "Betting",
    "News",
    "Community",
    "Insiders",
    "Tool",
)
_CATEGORY_LOOKUP: dict[str, str] = {c.lower(): c for c in CANONICAL_CATEGORIES}

# Canonical source types. "X" (the social network) normalizes to "x".
CANONICAL_TYPES: tuple[str, ...] = (
    "website",
    "x",
)

# Canonical fetch frequencies. "auto" = eligible for a future *safe* automated
# fetcher; "manual" = human-in-the-loop only. This slice fetches nothing, so the
# value is forward-looking metadata for later slices and the review UI.
CANONICAL_FREQUENCIES: tuple[str, ...] = (
    "auto",
    "manual",
)


@dataclass(frozen=True)
class SourceRecord:
    """One validated, normalized entry from the source manifest.

    ``name`` and ``url`` are preserved as authored (whitespace-trimmed only) so
    the registry round-trips back to the file. ``category``, ``type`` and
    ``frequency`` are normalized to canonical form. ``source_index`` is the
    1-based position of the entry in the manifest list, for traceability.
    """

    sport: str
    category: str
    name: str
    type: str
    url: str
    frequency: str
    source_index: int


@dataclass
class SourceManifestResult:
    """Structured outcome of parsing a source manifest.

    ``records`` holds only entries that passed required-field validation, with
    exact duplicates removed (first occurrence wins). ``warnings`` covers
    recoverable issues (unknown enum value, dropped duplicate, empty manifest);
    ``errors`` covers entries that were excluded (missing/blank required field,
    non-object entry).
    """

    records: list[SourceRecord] = field(default_factory=list)
    counts_by_category: dict[str, int] = field(default_factory=dict)
    counts_by_frequency: dict[str, int] = field(default_factory=dict)
    counts_by_type: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total_input: int = 0
    valid_count: int = 0
    duplicate_count: int = 0
    error_count: int = 0


def _decode_manifest_list(text: str, origin: str) -> list:
    """Decode manifest JSON ``text`` into a list, or raise SourceManifestError.

    ``origin`` is a human-readable label used only in error messages (a file
    path for the file loader, or e.g. ``"upload"`` for an in-memory string).
    Network-free and does no file I/O — it only parses the text it is given.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceManifestError(
            f"Source manifest {origin} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise SourceManifestError(
            f"Source manifest {origin} must contain a JSON array of sources, "
            f"got {type(data).__name__}."
        )
    return data


def load_source_manifest(path: str | Path) -> list:
    """Load a source manifest JSON file into a Python list.

    Raises :class:`SourceManifestError` when the file is missing, unreadable, is
    not valid JSON, or does not contain a top-level JSON array. Performs no
    validation of individual entries and makes no network or DB calls.
    """
    p = Path(path)
    if not p.exists():
        raise SourceManifestError(f"Source manifest not found: {p}")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceManifestError(
            f"Could not read source manifest {p}: {exc}"
        ) from exc
    return _decode_manifest_list(raw, str(p))


def _clean_text(value: object) -> str | None:
    """Return a trimmed string, or ``None`` if the value is not a usable string.

    Booleans and numbers are rejected (``None``) because the manifest's required
    fields are all text; this keeps a stray ``true`` or ``0`` from masquerading
    as a valid value.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None
    text = str(value).strip()
    return text or None


def normalize_category(value: object) -> str:
    """Normalize a category to canonical form, case-insensitively.

    Known categories map to their canonical spelling; unknown categories are
    returned trimmed and unchanged (the caller flags them as a warning).
    """
    text = _clean_text(value) or ""
    return _CATEGORY_LOOKUP.get(text.lower(), text)


def normalize_type(value: object) -> str:
    """Normalize a source type (lowercased, trimmed). ``"X"`` -> ``"x"``."""
    return (_clean_text(value) or "").lower()


def normalize_frequency(value: object) -> str:
    """Normalize a fetch frequency (lowercased, trimmed)."""
    return (_clean_text(value) or "").lower()


def _count_by(
    records: list[SourceRecord], key: Callable[[SourceRecord], str]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        bucket = key(record)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def parse_sources(data: list) -> SourceManifestResult:
    """Validate, normalize and de-duplicate an in-memory list of source dicts.

    Pure function — no file I/O, no network, no DB. Use this directly with an
    already-loaded list (e.g. in tests); use :func:`parse_source_manifest` to
    load from a path first.

    Behavior (design §4):
      - Non-object entries and entries missing/blank in any required field are
        recorded as ``errors`` and excluded from ``records``.
      - Exact duplicates (same ``name`` and ``url``) are dropped with a warning;
        the first occurrence wins.
      - Unknown ``category`` / ``type`` / ``frequency`` values are kept (after
        best-effort normalization) and recorded as warnings.
      - ``source_index`` is the 1-based position in the input list.
    """
    result = SourceManifestResult(total_input=len(data))

    seen: dict[tuple[str, str], int] = {}
    for offset, entry in enumerate(data, start=1):
        if not isinstance(entry, dict):
            result.errors.append(
                f"Source #{offset}: expected a JSON object, got "
                f"{type(entry).__name__}; skipped."
            )
            continue

        missing = [f for f in REQUIRED_FIELDS if _clean_text(entry.get(f)) is None]
        if missing:
            result.errors.append(
                f"Source #{offset}: missing/blank required field(s): "
                f"{', '.join(missing)}; skipped."
            )
            continue

        # Safe to assume non-None: required-field check above passed.
        sport = _clean_text(entry.get("sport")) or ""
        name = _clean_text(entry.get("name")) or ""
        url = _clean_text(entry.get("url")) or ""
        category = normalize_category(entry.get("category"))
        source_type = normalize_type(entry.get("type"))
        frequency = normalize_frequency(entry.get("frequency"))

        # Exact-duplicate detection on (name, url). Near-duplicates that differ
        # only by scheme/host (http vs https, www, trailing slash) are
        # intentionally NOT merged here (design §4 #3).
        dedupe_key = (name, url)
        if dedupe_key in seen:
            result.duplicate_count += 1
            result.warnings.append(
                f"Source #{offset} ({name!r}) is an exact duplicate of "
                f"source #{seen[dedupe_key]}; dropped."
            )
            continue
        seen[dedupe_key] = offset

        if category not in CANONICAL_CATEGORIES:
            result.warnings.append(
                f"Source #{offset} ({name!r}): unknown category "
                f"{category!r}; kept as-is."
            )
        if source_type not in CANONICAL_TYPES:
            result.warnings.append(
                f"Source #{offset} ({name!r}): unknown type "
                f"{source_type!r}; kept as-is."
            )
        if frequency not in CANONICAL_FREQUENCIES:
            result.warnings.append(
                f"Source #{offset} ({name!r}): unknown frequency "
                f"{frequency!r}; kept as-is."
            )

        result.records.append(
            SourceRecord(
                sport=sport,
                category=category,
                name=name,
                type=source_type,
                url=url,
                frequency=frequency,
                source_index=offset,
            )
        )

    if result.total_input == 0:
        result.warnings.append("Source manifest is empty (zero entries).")

    result.valid_count = len(result.records)
    result.error_count = len(result.errors)
    result.counts_by_category = _count_by(result.records, lambda r: r.category)
    result.counts_by_frequency = _count_by(result.records, lambda r: r.frequency)
    result.counts_by_type = _count_by(result.records, lambda r: r.type)
    return result


def parse_source_manifest(path: str | Path) -> SourceManifestResult:
    """Load a manifest file and parse it into a :class:`SourceManifestResult`.

    Convenience wrapper over :func:`load_source_manifest` + :func:`parse_sources`.
    Raises :class:`SourceManifestError` only for file-level problems; per-record
    issues are reported inside the result.
    """
    return parse_sources(load_source_manifest(path))


def parse_source_manifest_text(text: str) -> SourceManifestResult:
    """Parse manifest JSON ``text`` into a :class:`SourceManifestResult`.

    For callers that already hold the manifest as a string (e.g. a Streamlit
    file upload) rather than a path on disk. Network-free and does no file I/O.
    Raises :class:`SourceManifestError` only for document-level problems
    (invalid JSON, non-array top level); per-record issues are reported inside
    the result, exactly as with :func:`parse_source_manifest`.
    """
    return parse_sources(_decode_manifest_list(text, "upload"))


def _fmt_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def summarize(result: SourceManifestResult) -> str:
    """Render a short, data-safe text summary of a parse result.

    Lists aggregate counts and any warnings/errors. Makes no network call. The
    source registry is a list of public site URLs, but this summary still keeps
    to counts + issues rather than dumping the full registry.
    """
    lines = [
        f"Sources: {result.valid_count} valid / {result.total_input} total "
        f"({result.duplicate_count} duplicate, {result.error_count} error).",
        "By category: " + (_fmt_counts(result.counts_by_category) or "(none)"),
        "By frequency: " + (_fmt_counts(result.counts_by_frequency) or "(none)"),
        "By type: " + (_fmt_counts(result.counts_by_type) or "(none)"),
    ]
    if result.warnings:
        lines.append(f"Warnings ({len(result.warnings)}):")
        lines.extend(f"  - {w}" for w in result.warnings)
    if result.errors:
        lines.append(f"Errors ({len(result.errors)}):")
        lines.extend(f"  - {e}" for e in result.errors)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Network-free CLI: parse a manifest path and print a counts summary.

    Usage: ``python -m src.collection.source_manifest <path-to-manifest.json>``.
    Returns 0 on a successful parse (even with per-record warnings/errors), and
    2 on a file-level failure.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Parse and validate a public-source fight-week manifest "
            "(no network, no DB). Prints a counts + warnings summary."
        )
    )
    parser.add_argument(
        "path",
        help=(
            "Path to the source manifest JSON file "
            "(e.g. data/uploads/sources/UFC_DATA.json)."
        ),
    )
    args = parser.parse_args(argv)
    try:
        result = parse_source_manifest(args.path)
    except SourceManifestError as exc:
        print(f"error: {exc}")
        return 2
    print(summarize(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
