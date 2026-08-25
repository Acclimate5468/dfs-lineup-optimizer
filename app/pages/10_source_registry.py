"""Source Registry (review surface) — read-only S1 page.

Implements ``docs/FIGHT_WEEK_SOURCE_COLLECTOR_DESIGN.md`` §11 S1 and the
"registry review" step of §8: a read-only Streamlit surface for inspecting the
public-source registry (``data/uploads/sources/UFC_DATA.json``) before any
collector / fetcher slice is built.

Hard contract (design §6 safety boundaries, §11 S1; ``docs/DEVELOPMENT_NOTES.md`` §11):

- **Review only.** The page never fetches, scrapes, makes a network call, or
  writes to the database. It only parses a local JSON file (or an uploaded
  one) with the existing :mod:`src.collection.source_manifest` parser and
  renders counts + warnings + errors + the source list.
- **No persistence.** Nothing is written to SQLite; the page does not even open
  a DB connection. Re-rendering is side-effect free.
- **No source-specific fetchers.** This is the catalogue review surface only.
  Future slices (S2+) add approved public fetchers, each with its own design.
- The uploaded manifest is operator data (``docs/DEVELOPMENT_NOTES.md`` §7 / design §12): it is
  parsed in memory and never written back to disk or committed by this page.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.prototype_mode import lock_to_build_page  # noqa: E402

from src.collection.source_manifest import (  # noqa: E402
    SourceManifestError,
    SourceManifestResult,
    SourceRecord,
    parse_source_manifest,
    parse_source_manifest_text,
)

# Default manifest location, overridable via an env var (mirrors the
# DK_LAB_DB_PATH seam used elsewhere) so tests can point at a synthetic file
# instead of reading the real operator manifest or writing into the real
# data/ directory.
DEFAULT_MANIFEST_PATH = Path(
    os.environ.get(
        "DK_LAB_SOURCE_MANIFEST_PATH",
        str(REPO_ROOT / "data" / "uploads" / "sources" / "UFC_DATA.json"),
    )
)

st.set_page_config(page_title="Source Registry — DK Lineup Lab", layout="wide")
lock_to_build_page("Source Registry")
st.title("Source Registry (review)")

st.warning("Review only — no fetching, scraping, network calls, or DB writes.")
st.caption(
    "This page verifies the source registry. Future collector slices will add "
    "approved public fetchers source-by-source."
)


def _counts_df(counts: dict[str, int], label: str) -> pd.DataFrame:
    """Frequency table for a counts dict, sorted by count desc then label."""
    return pd.DataFrame(
        sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])),
        columns=[label, "Count"],
    )


def _sources_df(records: list[SourceRecord]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Category": r.category,
                "Name": r.name,
                "Type": r.type,
                "Frequency": r.frequency,
                "URL": r.url,
            }
            for r in records
        ],
        columns=["Category", "Name", "Type", "Frequency", "URL"],
    )


# ---------------------------------------------------------------------------
# Load the manifest (uploaded file takes precedence over the default path).
# Read-only on page load: with no upload, the default file is parsed as-is.
# ---------------------------------------------------------------------------
st.subheader("Load a source manifest")

uploaded = st.file_uploader(
    "Upload a source manifest JSON (optional)",
    type=["json"],
    help=(
        "Optional. An uploaded manifest is parsed in memory and shown below; "
        "it is never written to disk or committed."
    ),
)

result: SourceManifestResult | None = None
parse_error: str | None = None
source_label = ""

if uploaded is not None:
    source_label = f'uploaded file "{uploaded.name}"'
    try:
        text = uploaded.getvalue().decode("utf-8", errors="replace")
        result = parse_source_manifest_text(text)
    except SourceManifestError as exc:
        parse_error = str(exc)
elif DEFAULT_MANIFEST_PATH.exists():
    source_label = f"`{DEFAULT_MANIFEST_PATH}`"
    try:
        result = parse_source_manifest(DEFAULT_MANIFEST_PATH)
    except SourceManifestError as exc:
        parse_error = str(exc)
else:
    st.info(
        "No source manifest found. Place one at "
        f"`{DEFAULT_MANIFEST_PATH}` (it stays local and untracked), or upload "
        "a JSON manifest above. This page only reads the file — it never "
        "fetches, scrapes, or writes to the database."
    )
    st.stop()

if parse_error is not None:
    st.error(f"Could not parse {source_label}: {parse_error}")
    st.stop()

# Guaranteed non-None by the branches above (every path either set `result`,
# set `parse_error`, or stopped); keeps type-checkers and readers honest.
if result is None:
    st.stop()

st.caption(f"Showing {source_label}.")

# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------
st.subheader("Summary")
metric_cols = st.columns(6)
metric_cols[0].metric("Valid sources", result.valid_count)
metric_cols[1].metric("Warnings", len(result.warnings))
metric_cols[2].metric("Errors", len(result.errors))
metric_cols[3].metric("Categories", len(result.counts_by_category))
metric_cols[4].metric("Source types", len(result.counts_by_type))
metric_cols[5].metric("Frequencies", len(result.counts_by_frequency))

# ---------------------------------------------------------------------------
# Counts by category / type / frequency
# ---------------------------------------------------------------------------
st.subheader("Source counts")
count_cols = st.columns(3)
with count_cols[0]:
    st.caption("By category")
    st.dataframe(
        _counts_df(result.counts_by_category, "Category"),
        hide_index=True,
        width="stretch",
    )
with count_cols[1]:
    st.caption("By type")
    st.dataframe(
        _counts_df(result.counts_by_type, "Type"),
        hide_index=True,
        width="stretch",
    )
with count_cols[2]:
    st.caption("By frequency")
    st.dataframe(
        _counts_df(result.counts_by_frequency, "Frequency"),
        hide_index=True,
        width="stretch",
    )

# ---------------------------------------------------------------------------
# Validation issues (errors exclude an entry; warnings keep it)
# ---------------------------------------------------------------------------
st.subheader("Validation issues")
if result.errors:
    st.error(f"{len(result.errors)} error(s) — these entries were excluded:")
    st.markdown("\n".join(f"- {e}" for e in result.errors))
if result.warnings:
    st.warning(f"{len(result.warnings)} warning(s) — entries kept, please review:")
    st.markdown("\n".join(f"- {w}" for w in result.warnings))
if not result.errors and not result.warnings:
    st.success("No warnings or errors. Every entry passed validation.")

# ---------------------------------------------------------------------------
# Source table
# ---------------------------------------------------------------------------
st.subheader("Sources")
if result.records:
    st.dataframe(
        _sources_df(result.records),
        hide_index=True,
        width="stretch",
    )
else:
    st.info("No valid sources to display.")
