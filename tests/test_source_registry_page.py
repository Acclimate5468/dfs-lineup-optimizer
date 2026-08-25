"""AppTest coverage for the Source Registry review page (S1).

Loads ``app/pages/10_source_registry.py`` via ``streamlit.testing.v1.AppTest``
and pins the read-only contract per
``docs/FIGHT_WEEK_SOURCE_COLLECTOR_DESIGN.md`` §11 S1 / §6 and ``docs/DEVELOPMENT_NOTES.md``
§11:

  - Missing manifest → instructions, no metrics / table.
  - Valid manifest → summary metrics, counts tables, and the source table.
  - Warnings (unknown value, duplicate) and errors (missing field) surface.
  - Malformed JSON / non-array top level → friendly error, no crash.
  - Empty manifest → empty-warning + "no valid sources" note.
  - Required safety copy is present.
  - The page exposes no write buttons and never mutates the manifest file.

All fixtures are **synthetic** and written to ``tmp_path``; the page's default
manifest path is redirected via ``DK_LAB_SOURCE_MANIFEST_PATH`` so these tests
never read the real ``data/uploads/sources/UFC_DATA.json`` (operator data,
design §12) and never write into the real ``data/`` directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE = REPO_ROOT / "app" / "pages" / "10_source_registry.py"
ENV_VAR = "DK_LAB_SOURCE_MANIFEST_PATH"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _source(**overrides) -> dict:
    """A minimal valid synthetic source entry, overridable per field."""
    base = {
        "sport": "UFC",
        "category": "Betting",
        "name": "Best Fight Odds",
        "type": "website",
        "url": "https://www.bestfightodds.com/",
        "frequency": "manual",
    }
    base.update(overrides)
    return base


@pytest.fixture
def manifest_path(tmp_path, monkeypatch):
    """Redirect the page's default manifest at a synthetic tmp file.

    The file does not exist until a test writes it, so the same fixture covers
    both the "missing manifest" and "present manifest" branches.
    """
    path = tmp_path / "UFC_DATA.json"
    monkeypatch.setenv(ENV_VAR, str(path))
    return path


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _open_page() -> AppTest:
    at = AppTest.from_file(str(PAGE), default_timeout=30)
    at.run()
    return at


def _metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


# ---------------------------------------------------------------------------
# Missing manifest
# ---------------------------------------------------------------------------


def test_missing_manifest_shows_instructions(manifest_path):
    at = _open_page()  # manifest_path not written -> absent

    assert not at.exception, [str(e.value) for e in at.exception]
    infos = [i.value for i in at.info]
    assert any("No source manifest found" in m for m in infos), infos
    assert any(str(manifest_path) in m for m in infos), infos
    # Stopped before rendering anything derived.
    assert len(at.metric) == 0
    assert len(at.dataframe) == 0


# ---------------------------------------------------------------------------
# Valid manifest
# ---------------------------------------------------------------------------


def test_valid_manifest_renders_metrics_counts_and_table(manifest_path):
    _write(
        manifest_path,
        [
            _source(
                name="UFC Stats",
                url="https://ufcstats.example/",
                category="Official",
                type="website",
                frequency="auto",
            ),
            _source(
                name="BFO",
                url="https://bfo.example/",
                category="Betting",
                type="website",
                frequency="manual",
            ),
            _source(
                name="Helwani",
                url="https://x.example/helwani",
                category="Insiders",
                type="X",
                frequency="manual",
            ),
        ],
    )
    at = _open_page()

    assert not at.exception, [str(e.value) for e in at.exception]
    assert [e.value for e in at.error] == []

    by_label = _metrics(at)
    assert by_label["Valid sources"] == "3"
    assert by_label["Warnings"] == "0"
    assert by_label["Errors"] == "0"
    assert by_label["Categories"] == "3"
    assert by_label["Source types"] == "2"
    assert by_label["Frequencies"] == "2"

    # 3 counts tables + 1 source table.
    assert len(at.dataframe) == 4
    source_df = at.dataframe[-1].value
    assert list(source_df.columns) == [
        "Category",
        "Name",
        "Type",
        "Frequency",
        "URL",
    ]
    assert set(source_df["Name"]) == {"UFC Stats", "BFO", "Helwani"}
    helwani = source_df.loc[source_df["Name"] == "Helwani"].iloc[0]
    assert helwani["Type"] == "x"  # normalized from "X"
    assert helwani["Category"] == "Insiders"

    assert any("No warnings or errors" in s.value for s in at.success)


def test_valid_manifest_counts_tables_content(manifest_path):
    _write(
        manifest_path,
        [
            _source(name="A", url="https://a.example/", category="Betting"),
            _source(name="B", url="https://b.example/", category="Betting"),
            _source(name="C", url="https://c.example/", category="Official"),
        ],
    )
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    category_df = at.dataframe[0].value
    assert list(category_df.columns) == ["Category", "Count"]
    by_cat = dict(zip(category_df["Category"], category_df["Count"]))
    assert by_cat == {"Betting": 2, "Official": 1}


# ---------------------------------------------------------------------------
# Warnings + errors
# ---------------------------------------------------------------------------


def test_warnings_and_errors_surface(manifest_path):
    _write(
        manifest_path,
        [
            _source(
                name="Podcast Co",
                url="https://pod.example/",
                category="Podcast",  # unknown category -> warning, kept
            ),
            _source(name="Dup", url="https://dup.example/"),
            _source(name="Dup", url="https://dup.example/"),  # duplicate -> warning
            {"sport": "UFC"},  # missing required fields -> error, excluded
        ],
    )
    at = _open_page()

    assert not at.exception, [str(e.value) for e in at.exception]
    by_label = _metrics(at)
    assert by_label["Valid sources"] == "2"
    assert by_label["Warnings"] == "2"
    assert by_label["Errors"] == "1"

    errors_text = " ".join(e.value for e in at.error)
    assert "error(s)" in errors_text
    warns_text = " ".join(w.value for w in at.warning)
    assert "warning(s)" in warns_text

    issue_md = " ".join(m.value for m in at.markdown)
    assert "unknown category" in issue_md
    assert "duplicate" in issue_md.lower()
    # No "all clear" success message when issues exist.
    assert not any("No warnings or errors" in s.value for s in at.success)


# ---------------------------------------------------------------------------
# Malformed / non-array input
# ---------------------------------------------------------------------------


def test_malformed_json_shows_error_without_crashing(manifest_path):
    manifest_path.write_text("{not valid json", encoding="utf-8")
    at = _open_page()

    assert not at.exception, [str(e.value) for e in at.exception]
    errors = [e.value for e in at.error]
    assert any(
        "Could not parse" in m and "not valid JSON" in m for m in errors
    ), errors
    assert len(at.metric) == 0  # stopped before summary


def test_non_array_manifest_shows_error(manifest_path):
    manifest_path.write_text(json.dumps({"sport": "UFC"}), encoding="utf-8")
    at = _open_page()

    assert not at.exception, [str(e.value) for e in at.exception]
    errors = [e.value for e in at.error]
    assert any("JSON array" in m for m in errors), errors
    assert len(at.metric) == 0


# ---------------------------------------------------------------------------
# Empty manifest
# ---------------------------------------------------------------------------


def test_empty_manifest_warns_and_shows_no_sources(manifest_path):
    _write(manifest_path, [])
    at = _open_page()

    assert not at.exception, [str(e.value) for e in at.exception]
    by_label = _metrics(at)
    assert by_label["Valid sources"] == "0"
    assert by_label["Warnings"] == "1"  # parser flags empty manifest
    infos = [i.value for i in at.info]
    assert any("No valid sources" in m for m in infos), infos


# ---------------------------------------------------------------------------
# Safety copy + read-only invariants
# ---------------------------------------------------------------------------


def test_safety_copy_present(manifest_path):
    _write(manifest_path, [_source()])
    at = _open_page()

    warns = " ".join(w.value for w in at.warning)
    assert (
        "Review only — no fetching, scraping, network calls, or DB writes."
        in warns
    )
    caps = " ".join(c.value for c in at.caption)
    assert (
        "Future collector slices will add approved public fetchers "
        "source-by-source." in caps
    )


def test_page_exposes_no_buttons(manifest_path):
    _write(manifest_path, [_source()])
    at = _open_page()
    assert list(at.button) == [], [b.label for b in at.button]


def test_file_uploader_widget_present(manifest_path):
    _write(manifest_path, [_source()])
    at = _open_page()
    assert len(at.file_uploader) == 1


def test_repeated_render_does_not_mutate_manifest_file(manifest_path):
    _write(manifest_path, [_source(name="A", url="https://a.example/")])
    before = manifest_path.read_bytes()

    _open_page()
    _open_page()

    after = manifest_path.read_bytes()
    assert before == after, "Page render must not mutate the manifest file"
