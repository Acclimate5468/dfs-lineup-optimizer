"""AppTest coverage for the Export / Run Log v1 Slice C.3 page.

Loads ``app/pages/08_export_run_log.py`` via
``streamlit.testing.v1.AppTest`` against an isolated temp SQLite DB
and pins ``docs/EXPORT_RUN_LOG_V1_DESIGN.md`` §1.1 / §2 / §5 / §7
plus ``docs/DEVELOPMENT_NOTES.md`` §11:

- Empty DB → "No slates yet" info; no slate selector, no Build button.
- Persistent research-only banner covers the §1.1 / §2 non-goal list
  (internal research only, no DK upload, no contest entry, no file
  writes, no DB persistence).
- No export build on page load (the bundle / preview / downloads only
  appear after the explicit Build click).
- Gate-not-ready slate: blocked banner + disabled Build button + a
  no-op click leaves persisted state untouched.
- Ready slate: clicking Build renders the preview tables and surfaces
  three ``st.download_button`` widgets (CSV / JSON / Markdown) with
  ``optimizer_run_`` filenames and non-DK MIME types.
- ``n_lineups`` control is bounded to ``[1, 5]`` (design §7).
- Page load AND Build click do NOT mutate persisted state (docs/DEVELOPMENT_NOTES.md
  §11 — read-only end to end).
- No download button label, filename, or MIME type contains the
  literal substring ``DraftKings`` (design §1.1 / §11 risk #1).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import get_connection
from src.db.repositories import (
    FightGroupRepository,
    OddsRowRepository,
    SlateRepository,
)
from src.db.schema import apply_schema
from src.ingestion.odds_matching_service import (
    recompute_and_replace_match_results,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = REPO_ROOT / "app" / "pages" / "08_export_run_log.py"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "export_run_log_page.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _open_page() -> AppTest:
    at = AppTest.from_file(str(PAGE_PATH), default_timeout=60)
    at.run()
    return at


def _insert_fighter(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    name: str,
    salary: int = 8000,
) -> int:
    cur = conn.execute(
        "INSERT INTO fighters (slate_id, name, salary, status) "
        "VALUES (?, ?, ?, 'active')",
        (int(slate_id), name, int(salary)),
    )
    conn.commit()
    return int(cur.lastrowid)


def _save_odds_row(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    fighter_name_raw: str,
    american_odds: int,
    captured_at: str,
) -> None:
    OddsRowRepository(conn).create(
        slate_id=slate_id,
        fighter_name_raw=fighter_name_raw,
        american_odds=american_odds,
        source="manual",
        captured_at=captured_at,
    )


def _seed_not_ready_slate(name: str = "UFC NotReady") -> int:
    """Slate with no salary import / no fighters — Blocking checks fail
    and ``summary.ready`` is False."""
    conn = get_connection()
    try:
        apply_schema(conn)
        return SlateRepository(conn).create(event_name=name).id
    finally:
        conn.close()


READY_FIGHTS: tuple[tuple[str, str], ...] = (
    ("Aldo", "Vera"),
    ("Holloway", "Topuria"),
    ("Pereira", "Hill"),
    ("Adesanya", "Strickland"),
    ("Volkanovski", "Makhachev"),
    ("Oliveira", "Gaethje"),
)


def _seed_ready_slate(name: str = "UFC Ready Export") -> int:
    conn = get_connection()
    try:
        apply_schema(conn)
        sid = SlateRepository(conn).create(
            event_name=name,
            event_date="2026-05-31",
            salary_csv_status="validated",
            salary_row_count=2 * len(READY_FIGHTS),
        ).id
        for fav, dog in READY_FIGHTS:
            _insert_fighter(conn, slate_id=sid, name=fav, salary=8500)
            _insert_fighter(conn, slate_id=sid, name=dog, salary=7500)
        for fav, dog in READY_FIGHTS:
            fg = FightGroupRepository(conn).create(
                slate_id=sid,
                fighter_1_name=fav,
                fighter_2_name=dog,
                scheduled_rounds=3,
            )
            FightGroupRepository(conn).update_status(fg.id, "confirmed")
        for i, (fav, dog) in enumerate(READY_FIGHTS):
            _save_odds_row(
                conn,
                slate_id=sid,
                fighter_name_raw=fav,
                american_odds=-160,
                captured_at=f"2026-05-20T00:00:{2 * i:02d}Z",
            )
            _save_odds_row(
                conn,
                slate_id=sid,
                fighter_name_raw=dog,
                american_odds=+140,
                captured_at=f"2026-05-20T00:00:{2 * i + 1:02d}Z",
            )
        recompute_and_replace_match_results(conn, sid)
        SlateRepository(conn).set_manual_review_reviewed(sid)
        return sid
    finally:
        conn.close()


_SNAPSHOT_TABLES = (
    "slates",
    "fighters",
    "fight_groups",
    "odds_rows",
    "odds_match_results",
    "manual_match_overrides",
)


def _db_snapshot() -> dict[str, list[tuple]]:
    conn = get_connection()
    try:
        apply_schema(conn)
        snap: dict[str, list[tuple]] = {}
        for table in _SNAPSHOT_TABLES:
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            snap[table] = [tuple(r) for r in rows]
        return snap
    finally:
        conn.close()


def _button(at: AppTest, key: str):
    matched = [b for b in at.button if b.key == key]
    assert len(matched) == 1, (
        f"Expected exactly one button with key {key!r}; "
        f"saw button keys: {[b.key for b in at.button]}"
    )
    return matched[0]


def _number_input(at: AppTest, key: str):
    matched = [n for n in at.number_input if n.key == key]
    assert len(matched) == 1, (
        f"Expected exactly one number_input with key {key!r}; "
        f"saw number_input keys: {[n.key for n in at.number_input]}"
    )
    return matched[0]


# Streamlit's ``AppTest`` does not expose a typed ``.download_button``
# accessor; the elements come back from ``at.get("download_button")``
# as :class:`UnknownElement` instances. Identify them by the label
# strings set in ``app/pages/08_export_run_log.py``.
DOWNLOAD_LABELS: dict[str, str] = {
    "csv": "Download internal CSV summary (tidy, one row per fighter)",
    "wide_csv": "Download per-lineup CSV (wide, one row per lineup)",
    "json": "Download internal JSON summary",
    "md": "Download Markdown run log",
}


def _download_buttons(at: AppTest) -> list:
    return list(at.get("download_button"))


def _download_button_by_label(at: AppTest, label: str):
    matched = [
        d for d in _download_buttons(at) if d.proto.label == label
    ]
    assert len(matched) == 1, (
        f"Expected exactly one download_button labelled {label!r}; "
        f"saw labels: "
        f"{[d.proto.label for d in _download_buttons(at)]}"
    )
    return matched[0]


# ---------------------------------------------------------------------------
# Empty state + banner
# ---------------------------------------------------------------------------


def test_empty_db_shows_no_slates_info_and_no_build_button(isolated_db):
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    infos = [i.value for i in at.info]
    assert any("No slates yet" in s for s in infos), infos

    assert [b.key for b in at.button] == []
    assert [s.key for s in at.selectbox] == []


def test_banner_covers_v1_contract_fragments(isolated_db):
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    warnings = " ".join(w.value for w in at.warning)
    for fragment in (
        "Internal research export only",
        "NOT a DraftKings upload",
        "does NOT enter DraftKings",
        "writes no files",
        "does NOT persist",
        "Manual Review Gate",
    ):
        assert fragment in warnings, (
            f"Expected banner to mention {fragment!r}; got: {warnings!r}"
        )


# ---------------------------------------------------------------------------
# Slate selector + gate readout
# ---------------------------------------------------------------------------


def test_slate_selector_renders_when_slates_exist(isolated_db):
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception

    selectbox_keys = [s.key for s in at.selectbox]
    assert "export_slate_id" in selectbox_keys


def test_gate_readout_caption_renders_counts(isolated_db):
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception

    captions = " ".join(c.value for c in at.caption)
    assert "Blocking:" in captions
    assert "Warning:" in captions
    assert "Informational:" in captions
    assert "Ready:" in captions


# ---------------------------------------------------------------------------
# Page load does NOT build the export
# ---------------------------------------------------------------------------


def test_page_load_does_not_build_export_on_ready_slate(isolated_db):
    _seed_ready_slate()
    at = _open_page()
    assert not at.exception, [str(e.value) for e in at.exception]

    # No preview dataframes on page load.
    assert len(at.dataframe) == 0

    # No solver-status markdown was rendered.
    markdown_blob = " ".join(m.value for m in at.markdown)
    assert "Solver status:" not in markdown_blob

    # No download buttons on page load.
    assert _download_buttons(at) == [], _download_buttons(at)


# ---------------------------------------------------------------------------
# Not-ready gate: blocked banner + disabled button + no-op click
# ---------------------------------------------------------------------------


def test_not_ready_slate_renders_blocked_banner_and_disables_button(
    isolated_db,
):
    _seed_not_ready_slate()
    at = _open_page()
    assert not at.exception

    warnings = " ".join(w.value for w in at.warning)
    assert "Manual Review Gate is not green" in warnings, warnings

    btn = _button(at, "export_build_btn")
    assert btn.disabled is True


def test_not_ready_slate_click_does_not_crash_or_mutate(isolated_db):
    _seed_not_ready_slate()
    before = _db_snapshot()

    at = _open_page()
    assert not at.exception

    btn = _button(at, "export_build_btn")
    assert btn.disabled is True

    btn.click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    warnings = " ".join(w.value for w in at.warning)
    assert "Manual Review Gate is not green" in warnings

    after = _db_snapshot()
    assert before == after


# ---------------------------------------------------------------------------
# Ready slate: build click → preview + downloads
# ---------------------------------------------------------------------------


def test_ready_slate_build_click_renders_preview_and_downloads(isolated_db):
    _seed_ready_slate()
    at = _open_page()
    assert not at.exception

    btn = _button(at, "export_build_btn")
    assert btn.disabled is False

    btn.click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    markdown_blob = " ".join(m.value for m in at.markdown)
    assert "Solver status:" in markdown_blob
    assert "`ok`" in markdown_blob, markdown_blob

    # One preview dataframe per generated lineup.
    assert len(at.dataframe) >= 1

    labels = {d.proto.label for d in _download_buttons(at)}
    assert DOWNLOAD_LABELS["csv"] in labels, labels
    assert DOWNLOAD_LABELS["wide_csv"] in labels, labels
    assert DOWNLOAD_LABELS["json"] in labels, labels
    assert DOWNLOAD_LABELS["md"] in labels, labels
    # Tidy + wide + json + md = four research-only downloads.
    assert len(_download_buttons(at)) == 4, labels


def test_download_buttons_carry_internal_extensions_and_safe_labels(
    isolated_db,
):
    """Each download button's mock-media URL must end with its
    research-only extension (.csv / .json / .md), and no label or URL
    may carry the substring ``DraftKings`` (design §1.1 / §11 risk #1
    / #5)."""
    _seed_ready_slate()
    at = _open_page()
    _button(at, "export_build_btn").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    csv_btn = _download_button_by_label(at, DOWNLOAD_LABELS["csv"])
    wide_csv_btn = _download_button_by_label(at, DOWNLOAD_LABELS["wide_csv"])
    json_btn = _download_button_by_label(at, DOWNLOAD_LABELS["json"])
    md_btn = _download_button_by_label(at, DOWNLOAD_LABELS["md"])

    # NOTE: the AppTest mock-media URL extension is derived from the
    # download button's MIME type, not its ``file_name``. Both CSV
    # buttons share ``text/csv`` so both URLs end ``.csv``; the
    # ``_wide`` filename suffix lives in ``file_name`` (asserted at the
    # formatter/wiring level), not in this mock URL.
    for btn, suffix in (
        (csv_btn, ".csv"),
        (wide_csv_btn, ".csv"),
        (json_btn, ".json"),
        (md_btn, ".md"),
    ):
        proto = btn.proto
        assert proto.url, f"download button {proto.label!r} has empty URL"
        assert proto.url.endswith(suffix), (
            f"download button {proto.label!r} URL {proto.url!r} does "
            f"not end with {suffix!r}"
        )
        assert "DraftKings" not in proto.label
        assert "DraftKings" not in proto.url


def test_download_buttons_are_enabled_after_build(isolated_db):
    """The post-click download_buttons must not carry
    ``proto.disabled``; the C.2 formatter bytes are ready to download."""
    _seed_ready_slate()
    at = _open_page()
    _button(at, "export_build_btn").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    for label in DOWNLOAD_LABELS.values():
        proto = _download_button_by_label(at, label).proto
        assert proto.disabled is False, (
            f"download button {label!r} is unexpectedly disabled"
        )


# ---------------------------------------------------------------------------
# n_lineups control bounds
# ---------------------------------------------------------------------------


def test_n_lineups_input_is_bounded_one_to_five(isolated_db):
    _seed_ready_slate()
    at = _open_page()
    assert not at.exception

    ni = _number_input(at, "export_n_lineups")
    assert ni.min == 1
    assert ni.max == 5
    assert ni.step == 1
    assert ni.value == 1


# ---------------------------------------------------------------------------
# Read-only invariants (docs/DEVELOPMENT_NOTES.md §11)
# ---------------------------------------------------------------------------


def test_page_load_does_not_mutate_db(isolated_db):
    _seed_ready_slate()
    before = _db_snapshot()
    at = _open_page()
    assert not at.exception
    after = _db_snapshot()
    assert before == after


def test_build_click_does_not_mutate_db(isolated_db):
    _seed_ready_slate()
    before = _db_snapshot()

    at = _open_page()
    _button(at, "export_build_btn").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    after = _db_snapshot()
    assert before == after, (
        "build_run_log is read-only end to end; the Build click must "
        "not write to any table (design §5 Option A / docs/DEVELOPMENT_NOTES.md §11)."
    )


# ---------------------------------------------------------------------------
# No DK-upload-compatible language anywhere in the rendered UI
# ---------------------------------------------------------------------------


def test_ui_does_not_use_dk_upload_schema_or_contest_language(isolated_db):
    """The page must not surface DK-upload column names, contest-entry
    language, or DK-login language — design §1.1 / §11 risk #1 / #5.
    "DraftKings" itself may appear in the research-only warning to
    flag the file as not-an-upload."""
    _seed_ready_slate()
    at = _open_page()
    _button(at, "export_build_btn").click()
    at.run()
    assert not at.exception

    blob_parts: list[str] = []
    blob_parts.extend(m.value for m in at.markdown)
    blob_parts.extend(c.value for c in at.caption)
    blob_parts.extend(w.value for w in at.warning)
    blob_parts.extend(s.value for s in at.subheader)
    blob_parts.extend(d.proto.label for d in _download_buttons(at))
    blob = " ".join(blob_parts)

    for forbidden in (
        "Entry ID",
        "Contest ID",
        "Contest Name",
        "Lineup Name",
        "Upload to DraftKings",
        "Submit to DraftKings",
        "DK upload file",
    ):
        assert forbidden not in blob, (
            f"UI must not surface {forbidden!r} (design §1.1 / §11)"
        )
