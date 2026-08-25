"""AppTest coverage for the read-only Captain (Showdown) builder (slice C5).

Drives the **Captain** branch of the ``app/pages/00_build.py`` contest router
(rendered by ``app.captain_build.render_captain_section``) via
``streamlit.testing.v1.AppTest`` against an isolated temp SQLite DB, pinning
``docs/CAPTAIN_MODE_DESIGN.md`` §4 (the C5 MVP note), §5, §6, §7, §10 and the
§3 additive rules / ``docs/DEVELOPMENT_NOTES.md`` §11:

- The Captain branch renders the upload control + (after a parse) the moneyline,
  5-round, review-ack, and Build controls; the Classic two-step builder body is
  short-circuited (``st.stop`` fires first).
- **Build is DISABLED** until the review is acknowledged AND every rostered
  fighter has a moneyline; enabling both then building renders lineups +
  deterministic reasoning.
- No lineup output appears before the acknowledgement (the gate's spirit).
- Page load and the Classic→Captain switch write **NOTHING** to the DB
  (the Captain path opens no connection at all).
- A known synthetic slate (the C4 integration slate, fed as moneylines that
  de-vig back to its win probabilities) pins the top lineup: CPT **Alex
  Pereira**, **$49,500**, **~294.3** pts.

All fixtures are **synthetic** (invented salaries shared with the C4 unit test);
no real DK Captain CSV is read or committed (``docs/DEVELOPMENT_NOTES.md`` §7).
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.captain.finish_signal import (
    FinishOddsBout,
    MethodOfVictoryOdds,
    compute_finish_signals,
)
from src.db.connection import get_connection
from src.db.repositories import SlateRepository
from src.db.schema import apply_schema

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = REPO_ROOT / "app" / "pages" / "00_build.py"

_CONTEST_FORMAT_KEY = "builder_contest_format"
_UPLOAD_KEY = "captain_salary_upload"
_METHOD_KEY = "captain_method"
_STACK_MODE_KEY = "captain_stack_mode"
_ACK_KEY = "captain_review_ack"
_BUILD_BTN_KEY = "captain_build_btn"
_BUILD_BTN_LABEL = "Build Captain lineups"
_CAPTAIN_PIN_KEY = "captain_pin"  # captain-leverage view (slice C11b)
_HEURISTIC_METHOD_NAME = "heuristic"
_FINISH_AWARE_METHOD_NAME = "finish_aware"

# Consensus odds paste path (slice C8).
_BFO_PASTE_KEY = "captain_bfo_paste"
_MULTIBOOK_PASTE_KEY = "captain_multibook_paste"
_COMPUTE_KEY = "captain_compute_consensus_btn"


# ---------------------------------------------------------------------------
# Known synthetic Captain slate (mirrors tests/test_captain_build_method.py).
#
# (name, base_salary, scheduled_rounds) + the bout pairings. The per-fighter
# moneylines are chosen so the no-vig de-vig reproduces the C4 win probabilities
# closely enough that the optimizer's top lineup is the known answer (CPT Alex
# Pereira, $49,500, ~294.3). captain_salary in the CSV is round(1.5 * base).
# ---------------------------------------------------------------------------

# (name, base_salary, scheduled_rounds, american_moneyline)
_SLATE = {
    "Ilia Topuria": (9600, 5, -378),
    "Justin Gaethje": (5400, 5, 378),
    "Mauricio Ruffy": (10000, 3, -410),
    "Michael Chandler": (5000, 3, 410),
    "Sean O'Malley": (9200, 3, -359),
    "Aiemann Zahabi": (5800, 3, 359),
    "Josh Hokit": (9000, 3, -339),
    "Derrick Lewis": (6000, 3, 339),
    "Bo Nickal": (8800, 3, -260),
    "Kyle Daukaus": (6200, 3, 260),
    "Ciryl Gane": (7600, 5, -100),
    "Alex Pereira": (7400, 5, -100),
    "Diego Lopes": (8400, 3, -129),
    "Steve Garcia": (6600, 3, 129),
}

# Each tuple is one bout (the two fighters share a Game Info string). The 5-round
# bouts are Topuria/Gaethje and Gane/Pereira (the rest default to 3).
_BOUTS = [
    ("Ilia Topuria", "Justin Gaethje"),
    ("Mauricio Ruffy", "Michael Chandler"),
    ("Sean O'Malley", "Aiemann Zahabi"),
    ("Josh Hokit", "Derrick Lewis"),
    ("Bo Nickal", "Kyle Daukaus"),
    ("Ciryl Gane", "Alex Pereira"),
    ("Diego Lopes", "Steve Garcia"),
]
_FIVE_ROUND_BOUTS = {("Ilia Topuria", "Justin Gaethje"), ("Ciryl Gane", "Alex Pereira")}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _ml_key(name: str) -> str:
    return f"captain_ml_{_slug(name)}"


def _five_round_key(a: str, b: str) -> str:
    first, second = sorted((a, b))
    return f"captain_5r_{_slug(first)}__{_slug(second)}"


# Method-of-victory odds widget keys (this slice). Per-fighter for the tier-0
# tree; per-bout (sorted, mirroring the parser) for the two fallback markets.
def _mov_ko_key(name: str) -> str:
    return f"captain_mov_ko_{_slug(name)}"


def _mov_sub_key(name: str) -> str:
    return f"captain_mov_sub_{_slug(name)}"


def _mov_dec_key(name: str) -> str:
    return f"captain_mov_dec_{_slug(name)}"


def _bout_key(prefix: str, a: str, b: str) -> str:
    first, second = sorted((a, b))
    return f"captain_{prefix}_{_slug(first)}__{_slug(second)}"


def _captain_csv_bytes() -> bytes:
    """A synthetic DK Captain salary CSV: each fighter as a CPT (1.5×) + F row,
    bout opponents sharing a byte-identical Game Info string."""
    header = "Name,Roster Position,Salary,Game Info,ID,TeamAbbrev,Position"
    lines = [header]
    next_id = 1000
    for idx, (a, b) in enumerate(_BOUTS, start=1):
        game_info = f"{a}@{b} 06/14/2026 0{idx}:00PM ET"
        for fighter in (a, b):
            base = _SLATE[fighter][0]
            cpt = round(1.5 * base)
            # CPT (1.5×) row
            lines.append(
                f'"{fighter}",CPT,{cpt},"{game_info}",{next_id},UFC,'
            )
            next_id += 1
            # F (base) row
            lines.append(
                f'"{fighter}",F,{base},"{game_info}",{next_id},UFC,'
            )
            next_id += 1
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Fixtures + drivers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "captain_build.sqlite3"
    monkeypatch.setenv("DK_LAB_DB_PATH", str(db_path))
    monkeypatch.setattr("src.db.connection.DB_PATH", db_path)
    return db_path


def _open_captain() -> AppTest:
    """Open the Build page and switch the contest router to Captain."""
    at = AppTest.from_file(str(PAGE_PATH), default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    at.radio(key=_CONTEST_FORMAT_KEY).set_value("Captain")
    at = at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _uploader(at: AppTest):
    return next(u for u in at.file_uploader if u.key == _UPLOAD_KEY)


def _build_btn(at: AppTest):
    matched = [b for b in at.button if b.key == _BUILD_BTN_KEY]
    assert len(matched) == 1, [b.key for b in at.button]
    return matched[0]


def _method_select(at: AppTest):
    matched = [s for s in at.selectbox if s.key == _METHOD_KEY]
    assert len(matched) == 1, [s.key for s in at.selectbox]
    return matched[0]


def _text_blob(at: AppTest) -> str:
    parts: list[str] = []
    parts.extend(m.value for m in at.markdown)
    parts.extend(c.value for c in at.caption)
    parts.extend(i.value for i in at.info)
    parts.extend(w.value for w in at.warning)
    return " ".join(parts)


def _upload_slate(at: AppTest) -> AppTest:
    _uploader(at).upload("dk_captain.csv", _captain_csv_bytes(), "text/csv")
    return at.run()


def _set_all_moneylines(at: AppTest) -> None:
    for name, (_base, _rounds, ml) in _SLATE.items():
        at.number_input(key=_ml_key(name)).set_value(ml)


def _set_five_round_flags(at: AppTest) -> None:
    for a, b in _FIVE_ROUND_BOUTS:
        at.checkbox(key=_five_round_key(a, b)).set_value(True)


def _set_cash(at: AppTest) -> None:
    """Select the cash stack mode (the UI defaults to GPP, slice C11a §14.3).

    The cash-optimum fixture (CPT Alex Pereira, $49,500, 294.3) rosters both
    sides of the Gane/Pereira bout, which GPP (the default) excludes — so the
    tests pinning that fixture explicitly choose cash.
    """
    at.radio(key=_STACK_MODE_KEY).set_value("Cash")


def _set_manual_moneylines_except(at: AppTest, skip: set[str]) -> None:
    """Set every fighter's manual moneyline except the named ones (left at 0)."""
    for name, (_base, _rounds, ml) in _SLATE.items():
        if name in skip:
            continue
        at.number_input(key=_ml_key(name)).set_value(ml)


def _multi_book_grid(rows: list[tuple[str, list[int]]], books: list[str]) -> str:
    """A SYNTHETIC tab-delimited multi-book paste grid (docs/DEVELOPMENT_NOTES.md §7).

    Header row of book names + one row per fighter of signed American lines.
    """
    lines = ["Matchup\t" + "\t".join(books)]
    for name, mls in rows:
        lines.append(name + "\t" + "\t".join(f"{m:+d}" for m in mls))
    return "\n".join(lines)


def _compute_btn(at: AppTest):
    matched = [b for b in at.button if b.key == _COMPUTE_KEY]
    assert len(matched) == 1, [b.key for b in at.button]
    return matched[0]


def _compute_consensus(at: AppTest, *, multibook: str = "", bfo: str = "") -> AppTest:
    """Paste the given blocks and click the read-only Compute consensus button."""
    if bfo:
        at.text_area(key=_BFO_PASTE_KEY).set_value(bfo)
    if multibook:
        at.text_area(key=_MULTIBOOK_PASTE_KEY).set_value(multibook)
    at = at.run()
    return _compute_btn(at).click().run()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_captain_branch_renders_upload_and_short_circuits_classic(isolated_db):
    """Captain shows the read-only builder heading + uploader; the Classic
    two-step body is short-circuited by ``st.stop`` (design §3 / §4)."""
    at = _open_captain()

    blob = _text_blob(at)
    assert "Captain Mode (Showdown)" in blob
    assert any(u.key == _UPLOAD_KEY for u in at.file_uploader)
    # Classic body did not render.
    assert "DraftKings salary" not in blob
    assert "Odds checker" not in blob
    assert all(b.label != "Build research lineups" for b in at.button)


def test_upload_parses_slate_and_shows_controls(isolated_db):
    """After upload the parsed slate + per-bout moneyline / 5-round controls and
    the disabled Build button render (design §5)."""
    at = _open_captain()
    at = _upload_slate(at)
    assert not at.exception, [str(e.value) for e in at.exception]

    # 14 fighters → 14 moneyline inputs; 7 bouts → 7 five-round checkboxes
    # plus the review ack checkbox.
    ml_keys = [n.key for n in at.number_input if n.key.startswith("captain_ml_")]
    assert len(ml_keys) == 14
    assert _ml_key("Alex Pereira") in ml_keys
    five_keys = [c.key for c in at.checkbox if c.key.startswith("captain_5r_")]
    assert len(five_keys) == 7
    assert any(c.key == _ACK_KEY for c in at.checkbox)

    # Parsed-slate readout names the bouts.
    blob = _text_blob(at)
    assert "Alex Pereira vs Ciryl Gane" in blob
    # Build is present but disabled (no moneylines, no ack yet).
    assert _build_btn(at).disabled is True


def test_build_disabled_until_ack_and_all_moneylines(isolated_db):
    """The gate's spirit (design §4 C5 MVP): Build stays disabled until the
    review is acked AND every rostered fighter has a moneyline."""
    at = _open_captain()
    at = _upload_slate(at)

    # Moneylines present, but no acknowledgement → still disabled.
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    at = at.run()
    assert _build_btn(at).disabled is True
    assert "Acknowledge your review" in _text_blob(at)

    # Acknowledge but clear one moneyline (0 = not entered) → still disabled.
    at.checkbox(key=_ACK_KEY).set_value(True)
    at.number_input(key=_ml_key("Alex Pereira")).set_value(0)
    at = at.run()
    assert _build_btn(at).disabled is True
    assert "still missing" in _text_blob(at)

    # Restore the moneyline with the ack on → enabled.
    at.number_input(key=_ml_key("Alex Pereira")).set_value(-100)
    at = at.run()
    assert _build_btn(at).disabled is False


def test_no_lineups_before_acknowledgement(isolated_db):
    """No lineup output appears before the acknowledgement, even with every
    moneyline present (the gate is never bypassed)."""
    at = _open_captain()
    at = _upload_slate(at)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    at = at.run()  # no ack

    blob = _text_blob(at)
    assert "Top" not in blob or "Captain lineups" not in blob
    assert "Why this lineup?" not in blob
    assert not any("CPT) ·" in m.value for m in at.markdown)


def test_known_fixture_build_pins_top_lineup(isolated_db):
    """A known synthetic slate builds the expected top lineup: CPT Alex Pereira,
    $49,500, ~294.3 pts, with deterministic reasoning (design §6 / §7 / §10)."""
    at = _open_captain()
    at = _upload_slate(at)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    _set_cash(at)  # the 294.3 fixture rosters both Gane & Pereira -> cash, not GPP
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    assert _build_btn(at).disabled is False

    at = _build_btn(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "Top 5 Captain lineups" in blob
    # The pinned top lineup.
    assert "Alex Pereira (CPT) · $49,500 · 294.3 pts" in blob
    # Captain-leverage reasoning is present and fact-backed (no invented winner).
    assert any("Why this lineup?" in e.label for e in at.expander)

    # The deterministic reasoning bullets (rendered as a markdown list).
    reasoning = "\n".join(
        m.value for m in at.markdown if "base projection" in m.value
    )
    assert reasoning, "expected captain reasoning markdown"
    assert "× 1.5 leverage" in reasoning
    assert "implied win probability" in reasoning
    assert "No same-fight exclusion is applied" in reasoning
    # The reasoning never asserts a finish / KO / predicted winner.
    lowered = reasoning.lower()
    for bad in ("knockout", "finish wins", "will win", "predicted winner", "guaranteed"):
        assert bad not in lowered


def test_captain_path_writes_nothing_to_db(isolated_db):
    """The whole Captain flow (load → switch → upload → build) persists nothing:
    the Captain path opens no DB connection (design §4 C5 MVP; ``docs/DEVELOPMENT_NOTES.md`` §11)."""
    at = _open_captain()
    at = _upload_slate(at)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    at = _build_btn(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # No slate (or any other row) was written by the Captain builder.
    conn = get_connection()
    try:
        apply_schema(conn)
        assert SlateRepository(conn).list_all() == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Build-method selector (design §7, slice C7)
# ---------------------------------------------------------------------------


def test_method_selector_renders_and_defaults_to_heuristic(isolated_db):
    """After upload the Build-method selector renders and defaults to Heuristic;
    both engines are options and no experimental caveat shows yet (design §7)."""
    at = _open_captain()
    at = _upload_slate(at)
    assert not at.exception, [str(e.value) for e in at.exception]

    select = _method_select(at)
    # The underlying selected value is the Heuristic registry key (the default);
    # the visible options are the human labels (format_func is applied).
    assert select.value == _HEURISTIC_METHOD_NAME
    assert any("Heuristic" in o for o in select.options)
    assert any("Finish-aware" in o for o in select.options)
    # Default (Heuristic) is not experimental, so no caveat banner / K knob shows.
    assert "unvalidated knob" not in _text_blob(at)
    assert not any(n.key == "captain_finish_k" for n in at.number_input)


def test_finish_aware_selection_shows_experimental_caveat_and_k(isolated_db):
    """Selecting Finish-aware (MOV) surfaces the §14.2 caveat (experimental; K is
    an unvalidated knob; enter method-of-victory odds to activate the bonus) and
    exposes the editable K number (default 20)."""
    at = _open_captain()
    at = _upload_slate(at)
    _method_select(at).set_value(_FINISH_AWARE_METHOD_NAME)
    at = at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "experimental" in blob.lower()
    assert "unvalidated knob" in blob
    assert "method-of-victory odds" in blob
    # The editable K knob renders and defaults to 20.
    k_input = next(n for n in at.number_input if n.key == "captain_finish_k")
    assert k_input.value == pytest.approx(20.0)


def test_finish_aware_build_equals_base_and_caveats_reasoning(isolated_db):
    """A Finish-aware (MOV) build runs end to end: with no MOV odds input yet the
    finish signal is None, so adjProj == base — the same top lineup the Heuristic
    builds (CPT Alex Pereira, $49,500, 294.3). The method is named in the output
    and the reasoning flags it experimental without inventing a result (§14.2)."""
    at = _open_captain()
    at = _upload_slate(at)
    _method_select(at).set_value(_FINISH_AWARE_METHOD_NAME)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    _set_cash(at)  # pin the cash 294.3 fixture (GPP would exclude it)
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    assert _build_btn(at).disabled is False

    at = _build_btn(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "Top 5 Captain lineups" in blob
    # No MOV odds yet -> finish bonus is 0 -> the base projection -> the same top
    # lineup and point total as the Heuristic default (design §14.2).
    assert "Alex Pereira (CPT) · $49,500 · 294.3 pts" in blob
    # The build caption / reasoning name the experimental method.
    assert "Finish-aware" in blob
    assert "experimental" in blob.lower()

    # Reasoning is fact-backed and invents no finish / KO / predicted winner.
    reasoning = "\n".join(
        m.value for m in at.markdown if "base projection" in m.value
    )
    assert reasoning, "expected captain reasoning markdown"
    assert "× 1.5 leverage" in reasoning
    assert "implied win probability" in reasoning
    assert "Finish-aware" in reasoning
    lowered = reasoning.lower()
    for bad in ("knockout", "finish wins", "will win", "predicted winner", "guaranteed"):
        assert bad not in lowered


def test_finish_aware_build_equals_heuristic_with_no_mov_odds(isolated_db):
    """With no MOV odds entered, the MOV finish-aware build reports the SAME point
    total as the Heuristic default for the same lineup — proof the finish bonus is
    inert (finish_signal=None) and the base projection is used (design §14.2)."""
    # Heuristic (default) build — cash, to pin the both-sides 294.3 fixture.
    at = _open_captain()
    at = _upload_slate(at)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    _set_cash(at)
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    at = _build_btn(at).click().run()
    heur_pts = _captain_lineup_points(at, "Alex Pereira", 49_500)
    assert heur_pts == pytest.approx(294.3, abs=0.05)

    # Finish-aware (MOV) build of the same slate, K left at its default 20.
    at = _open_captain()
    at = _upload_slate(at)
    _method_select(at).set_value(_FINISH_AWARE_METHOD_NAME)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    _set_cash(at)
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    at = _build_btn(at).click().run()
    fa_pts = _captain_lineup_points(at, "Alex Pereira", 49_500)
    assert fa_pts == pytest.approx(heur_pts)  # no MOV odds -> equals the base


def _captain_lineup_points(at: AppTest, captain: str, salary: int) -> float:
    """Pull the rendered point total from the ``CPT · $salary · NNN.N pts`` row."""
    needle = f"{captain} (CPT) · ${salary:,} · "
    for m in at.markdown:
        if needle in m.value:
            tail = m.value.split(needle, 1)[1]
            match = re.search(r"([0-9]+(?:\.[0-9]+)?) pts", tail)
            assert match, m.value
            return float(match.group(1))
    raise AssertionError(f"no lineup row for {captain} at ${salary:,}")


# ---------------------------------------------------------------------------
# Stack toggle (design §14.3, slice C11a)
# ---------------------------------------------------------------------------


def _stack_radio(at: AppTest):
    matched = [r for r in at.radio if r.key == _STACK_MODE_KEY]
    assert len(matched) == 1, [r.key for r in at.radio]
    return matched[0]


def test_stack_toggle_renders_and_defaults_to_gpp(isolated_db):
    """After upload the GPP | cash stack toggle renders and defaults to GPP, the
    tournament default surfaced at the UI (design §14.3)."""
    at = _open_captain()
    at = _upload_slate(at)
    assert not at.exception, [str(e.value) for e in at.exception]

    radio = _stack_radio(at)
    assert radio.value == "GPP"
    assert "GPP" in radio.options
    assert "Cash" in radio.options


def test_gpp_default_build_excludes_same_fight_pair(isolated_db):
    """A default (GPP) build respects the stack mode: the cash-optimum lineup
    (CPT Alex Pereira, $49,500, 294.3 — rostering BOTH Gane & Pereira) is NOT
    produced, the GPP mode is surfaced, and a lineup still builds (design §14.3)."""
    at = _open_captain()
    at = _upload_slate(at)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    # Default is GPP (no explicit cash selection).
    assert _stack_radio(at).value == "GPP"
    assert _build_btn(at).disabled is False

    at = _build_btn(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "Top 5 Captain lineups" in blob
    # The build surfaces which mode built it.
    assert "GPP — no same-fight pairs" in blob
    # The cash optimum rosters both sides of the Gane/Pereira bout, so GPP must
    # not produce it.
    assert "Alex Pereira (CPT) · $49,500 · 294.3 pts" not in blob


def test_cash_selection_surfaced_in_build(isolated_db):
    """Selecting cash is surfaced in the build caption (design §14.3)."""
    at = _open_captain()
    at = _upload_slate(at)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    _set_cash(at)
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    at = _build_btn(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "Cash — same-fight pairs allowed" in blob
    # Cash allows the both-sides optimum.
    assert "Alex Pereira (CPT) · $49,500 · 294.3 pts" in blob


# ---------------------------------------------------------------------------
# Consensus odds paste path (design §13 C8)
# ---------------------------------------------------------------------------


def test_consensus_paste_boxes_and_compute_button_render(isolated_db):
    """After upload the two consensus paste boxes + the read-only Compute button
    render beside the manual moneylines (design §13 C8)."""
    at = _open_captain()
    at = _upload_slate(at)
    assert not at.exception, [str(e.value) for e in at.exception]

    area_keys = {a.key for a in at.text_area}
    assert _BFO_PASTE_KEY in area_keys
    assert _MULTIBOOK_PASTE_KEY in area_keys
    assert any(b.key == _COMPUTE_KEY for b in at.button)
    # The manual moneylines + Build button still render (manual is the fallback).
    assert any(n.key.startswith("captain_ml_") for n in at.number_input)
    assert any(b.key == _BUILD_BTN_KEY for b in at.button)


def test_compute_consensus_is_read_only_and_surfaces_low_confidence_unpaired(
    isolated_db,
):
    """Compute parses a synthetic single-book grid (low confidence) plus a lone
    unpaired fighter; both are surfaced and NOTHING is written to the DB."""
    grid = _multi_book_grid(
        [
            ("Bo Nickal", [-150]),
            ("Kyle Daukaus", [130]),
            ("Phantom Challenger", [200]),
        ],
        ["DraftKings"],
    )
    at = _open_captain()
    at = _upload_slate(at)
    at = _compute_consensus(at, multibook=grid)
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    # Matched slate fighters appear with their consensus % and the low-confidence
    # flag (only one book priced the bout).
    assert "Bo Nickal" in blob
    assert "low confidence" in blob
    # The lone fighter is reported unpaired, never silently dropped.
    assert "Phantom Challenger" in blob
    assert "Could not pair" in blob

    # Read-only: the Captain consensus path persisted nothing.
    conn = get_connection()
    try:
        apply_schema(conn)
        assert SlateRepository(conn).list_all() == []
    finally:
        conn.close()


def test_consensus_used_with_manual_fallback_and_sources_shown(isolated_db):
    """A two-book consensus prices one bout; the rest fall back to manual. The
    build runs, names both sources, and the per-fighter source readout shows the
    consensus-priced fighters used consensus even though a manual line exists
    (consensus precedence — design §13 C8 step 4)."""
    grid = _multi_book_grid(
        [("Bo Nickal", [-150, -155]), ("Kyle Daukaus", [130, 135])],
        ["DraftKings", "BetMGM"],
    )
    at = _open_captain()
    at = _upload_slate(at)
    at = _compute_consensus(at, multibook=grid)
    # Manual moneylines for EVERYONE (including the consensus-priced bout) — so
    # consensus precedence is the only reason Bo/Kyle resolve to consensus.
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    assert _build_btn(at).disabled is False

    at = _build_btn(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "Top 5 Captain lineups" in blob
    # The build caption names both win-prob sources.
    assert "Win-probability source: 2 consensus, 12 manual" in blob
    # The per-fighter source readout: consensus-priced fighters used consensus.
    sources = "\n".join(m.value for m in at.markdown if ": consensus" in m.value or ": manual" in m.value)
    assert "Bo Nickal: consensus" in sources
    assert "Kyle Daukaus: consensus" in sources
    assert "Alex Pereira: manual" in sources


def test_consensus_coverage_enables_build_without_manual_for_that_bout(isolated_db):
    """A bout priced by consensus needs no manual moneyline: with the other
    fighters' manual lines set and that bout left at 0, the gate still clears."""
    grid = _multi_book_grid(
        [("Bo Nickal", [-150, -155]), ("Kyle Daukaus", [130, 135])],
        ["DraftKings", "BetMGM"],
    )
    at = _open_captain()
    at = _upload_slate(at)
    at = _compute_consensus(at, multibook=grid)
    # Manual lines for everyone EXCEPT the consensus-priced bout.
    _set_manual_moneylines_except(at, skip={"Bo Nickal", "Kyle Daukaus"})
    _set_five_round_flags(at)
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    assert _build_btn(at).disabled is False  # consensus covered Bo/Kyle

    at = _build_btn(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "Top 5 Captain lineups" in _text_blob(at)


def test_unmatched_consensus_fighter_is_reported(isolated_db):
    """A consensus fighter whose name matches no slate fighter is reported as
    ignored (surfaced, never silently merged)."""
    grid = _multi_book_grid(
        [("Totally Unknown", [-150, -155]), ("Nobody Here", [130, 135])],
        ["DraftKings", "BetMGM"],
    )
    at = _open_captain()
    at = _upload_slate(at)
    at = _compute_consensus(at, multibook=grid)
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "did not match any slate fighter" in blob
    assert "Totally Unknown" in blob


# ---------------------------------------------------------------------------
# Method-of-victory (MOV) odds input — activates the C10 finish bonus
# (design §14.1 input + §14.2 bonus)
# ---------------------------------------------------------------------------

# The known Topuria-like MOV tree from tests/test_captain_finish_signal.py: a
# fighter holding (KO/TKO -225, Submission +500, Decision +1200) against
# (+500, +3500, +1600) prices to a finish signal of ~0.7223 (design §14.5).
_PEREIRA_MOV = (-225, 500, 1200)
_GANE_MOV = (500, 3500, 1600)


def _pereira_finish_signal() -> float:
    """The exact C9 finish signal for the Topuria-like tree given to Pereira."""
    bout = FinishOddsBout(
        fighter_a="Alex Pereira",  # the parser sorts Gane/Pereira -> Pereira is A
        fighter_b="Ciryl Gane",
        win_prob_a=0.5,
        win_prob_b=0.5,
        mov_a=MethodOfVictoryOdds(*_PEREIRA_MOV),
        mov_b=MethodOfVictoryOdds(*_GANE_MOV),
    )
    return compute_finish_signals(bout).fighter_a.finish_signal


def _set_mov_tree(at: AppTest, name: str, ko: int, sub: int, dec: int) -> None:
    at.number_input(key=_mov_ko_key(name)).set_value(ko)
    at.number_input(key=_mov_sub_key(name)).set_value(sub)
    at.number_input(key=_mov_dec_key(name)).set_value(dec)


def _captain_projection(at: AppTest, captain: str) -> float:
    """Pull the captain's (base or adjusted) projection from its reasoning line.

    The line reads ``<captain>: NN.N pts — base/adjusted projection PP.P × 1.5
    leverage`` — isolating the captain's own projection (no flex contamination,
    unlike the lineup total)."""
    pat = re.compile(
        rf"{re.escape(captain)}: [0-9.]+ pts — (?:adjusted|base) projection "
        r"([0-9]+(?:\.[0-9]+)?) "
    )
    for m in at.markdown:
        match = pat.search(m.value)
        if match:
            return float(match.group(1))
    raise AssertionError(f"no captain projection reasoning line for {captain}")


def _open_finish_aware(isolated_db) -> AppTest:
    """Open Captain, upload the slate, select Finish-aware, and render it so the
    MOV-odds inputs exist on the returned AppTest."""
    at = _open_captain()
    at = _upload_slate(at)
    _method_select(at).set_value(_FINISH_AWARE_METHOD_NAME)
    return at.run()


def _build_finish_aware(isolated_db, set_mov=None, *, stack: str = "Cash") -> AppTest:
    """Run a full Finish-aware build (moneylines + 5R + optional MOV odds + ack).

    Defaults to the cash stack mode so the captain-projection / lineup-total
    assertions keep pinning the CPT Alex Pereira fixture (GPP, the UI default,
    would exclude that both-sides lineup — slice C11a §14.3).
    """
    at = _open_finish_aware(isolated_db)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    at.radio(key=_STACK_MODE_KEY).set_value(stack)
    if set_mov is not None:
        set_mov(at)
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    assert _build_btn(at).disabled is False
    at = _build_btn(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_complete_mov_tree_activates_finish_bonus(isolated_db):
    """A complete MOV tree activates the bonus: the captain's Finish-aware
    projection rises by exactly K × finish_signal vs the no-MOV base, matching
    compute_finish_signals (~0.7223 -> +~14.4 at K=20; design §14.1 / §14.2)."""
    signal = _pereira_finish_signal()
    assert signal == pytest.approx(0.722334, abs=1e-4)

    base_proj = _captain_projection(_build_finish_aware(isolated_db), "Alex Pereira")

    def _mov(at: AppTest) -> None:
        _set_mov_tree(at, "Alex Pereira", *_PEREIRA_MOV)
        _set_mov_tree(at, "Ciryl Gane", *_GANE_MOV)

    adj_at = _build_finish_aware(isolated_db, _mov)
    adj_proj = _captain_projection(adj_at, "Alex Pereira")

    # adjProj = base + K × finish_signal (K default 20) -> ~+14.4.
    assert adj_proj - base_proj == pytest.approx(20.0 * signal, abs=0.1)
    # The captain reasoning decomposes the bonus and cites the priced signal,
    # matching compute_finish_signals (no invented finish / winner).
    reasoning = _text_blob(adj_at)
    assert "Method-of-victory" in reasoning  # per-bout tier badge
    assert "K=20" in reasoning
    assert "72% finish signal" in reasoning
    assert "+14.4" in reasoning


def test_tier_badges_render_for_each_market(isolated_db):
    """The §14.1 ladder tier is surfaced per bout: Method-of-victory for a full
    tree, Distance and Round-total for the two fallback markets."""
    at = _open_finish_aware(isolated_db)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    # Bout 1 — full MOV tree -> Method-of-victory.
    _set_mov_tree(at, "Alex Pereira", *_PEREIRA_MOV)
    _set_mov_tree(at, "Ciryl Gane", *_GANE_MOV)
    # Bout 2 — go-the-distance fallback only -> Distance.
    at.number_input(key=_bout_key("distno", "Bo Nickal", "Kyle Daukaus")).set_value(-150)
    at.number_input(key=_bout_key("distyes", "Bo Nickal", "Kyle Daukaus")).set_value(120)
    # Bout 3 — round-total fallback only -> Round-total.
    at.number_input(key=_bout_key("rtunder", "Josh Hokit", "Derrick Lewis")).set_value(-130)
    at.number_input(key=_bout_key("rtover", "Josh Hokit", "Derrick Lewis")).set_value(110)
    at = at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert "Method-of-victory" in blob
    assert "Distance" in blob
    assert "Round-total" in blob


def test_no_mov_odds_finish_aware_equals_base_and_heuristic_unchanged(isolated_db):
    """A bout with no MOV odds keeps its fighters on the base projection: the
    Finish-aware build (signals all None) reproduces the Heuristic total exactly,
    and the Heuristic stays the default with no MOV widgets on its path (§14.2)."""
    # Fresh load defaults to Heuristic, and the Heuristic path renders NO MOV
    # inputs (byte-for-byte unchanged).
    fresh = _open_captain()
    fresh = _upload_slate(fresh)
    assert _method_select(fresh).value == _HEURISTIC_METHOD_NAME
    assert not any(n.key.startswith("captain_mov_") for n in fresh.number_input)

    # Selecting Finish-aware renders the per-fighter MOV inputs (14 × 3 = 42).
    fa = _open_finish_aware(isolated_db)
    mov_keys = [n.key for n in fa.number_input if n.key.startswith("captain_mov_")]
    assert len(mov_keys) == 42
    assert _mov_ko_key("Alex Pereira") in mov_keys

    # Heuristic build total (cash, to pin the both-sides $49,500 fixture).
    at = _open_captain()
    at = _upload_slate(at)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    _set_cash(at)
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    at = _build_btn(at).click().run()
    heur_total = _captain_lineup_points(at, "Alex Pereira", 49_500)

    # Finish-aware build with NO MOV odds entered -> identical total.
    fa_total = _captain_lineup_points(_build_finish_aware(isolated_db), "Alex Pereira", 49_500)
    assert fa_total == pytest.approx(heur_total)


def test_partial_mov_odds_show_per_bout_error_and_fall_back_to_base(isolated_db):
    """Malformed / partial MOV odds (an incomplete tree) surface a clear per-bout
    error without crashing; those fighters fall back to the base projection, so
    the captain total matches the no-MOV build (graceful, design §14.1)."""
    base_total = _captain_lineup_points(
        _build_finish_aware(isolated_db), "Alex Pereira", 49_500
    )

    def _partial(at: AppTest) -> None:
        # Both fighters get only a KO/TKO price -> the MOV tree is incomplete, so
        # C9 raises FinishSignalError (missing Submission odds).
        at.number_input(key=_mov_ko_key("Alex Pereira")).set_value(-225)
        at.number_input(key=_mov_ko_key("Ciryl Gane")).set_value(500)

    at = _build_finish_aware(isolated_db, _partial)
    blob = _text_blob(at)
    assert "could not price the finish signal" in blob

    # Those fighters fell back to base -> the captain total is unchanged.
    adj_total = _captain_lineup_points(at, "Alex Pereira", 49_500)
    assert adj_total == pytest.approx(base_total)


# ---------------------------------------------------------------------------
# Captain-leverage view (design §14.4, slice C11b)
# ---------------------------------------------------------------------------


def _pin_select(at: AppTest):
    matched = [s for s in at.selectbox if s.key == _CAPTAIN_PIN_KEY]
    assert len(matched) == 1, [s.key for s in at.selectbox]
    return matched[0]


def _ranking_card_order(at: AppTest) -> list[str]:
    """The captain names, in CPTproj order, from the rendered ranking card."""
    for m in at.markdown:
        if "CPTproj ranking" in m.value:
            return [html.unescape(n) for n in re.findall(r"<b>(.*?)</b>", m.value)]
    raise AssertionError("no CPTproj ranking card rendered")


def _build_gpp(isolated_db) -> AppTest:
    """A default (GPP) Heuristic build so the leverage view renders below it."""
    at = _open_captain()
    at = _upload_slate(at)
    _set_all_moneylines(at)
    _set_five_round_flags(at)
    at.checkbox(key=_ACK_KEY).set_value(True)
    at = at.run()
    at = _build_btn(at).click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_leverage_view_renders_ranked_list_and_defaults_to_top(isolated_db):
    """After a build the captain-leverage view renders: a CPTproj-ranked captain
    list and a pin selectbox that defaults to the top of that ranking (§14.4)."""
    at = _build_gpp(isolated_db)

    blob = _text_blob(at)
    assert "Captain leverage" in blob
    assert "CPTproj" in blob

    # The ranked card lists all 14 captains; the pin selectbox offers the same
    # captains in the same order and defaults (index 0) to the ranking top.
    order = _ranking_card_order(at)
    assert len(order) == 14
    select = _pin_select(at)
    assert list(select.options) == order
    assert select.index == 0
    assert select.value == order[0]

    # The default pinned build renders that top captain's lineup below the card.
    assert f"Pinned Captain: {order[0]}" in _text_blob(at)
    cpt_needle = f"{html.escape(order[0])} (CPT) ·"
    assert any(cpt_needle in m.value for m in at.markdown)


def test_leverage_view_pivot_rebuilds_with_pinned_captain(isolated_db):
    """Picking a different captain in the pin selectbox rebuilds with that fighter
    pinned as Captain (the leverage pivot survives the rerun — §14.4)."""
    at = _build_gpp(isolated_db)
    order = _ranking_card_order(at)

    # Pivot to the second-ranked captain (distinct from the default top).
    target = order[1]
    _pin_select(at).set_value(target)
    at = at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    blob = _text_blob(at)
    assert f"Pinned Captain: {target}" in blob
    # A lineup card captained by the pinned fighter is rendered.
    cpt_needle = f"{html.escape(target)} (CPT) ·"
    assert any(cpt_needle in m.value for m in at.markdown)
    # The free-EV build above is still present (the leverage view is additive).
    assert "Top 5 Captain lineups" in blob


def test_leverage_view_is_distinct_from_free_ev_in_gpp(isolated_db):
    """The leverage view is labeled as the ceiling pick, distinct from the free-EV
    build above it (design §14.4 — GPP is where they disagree)."""
    at = _build_gpp(isolated_db)
    blob = _text_blob(at)
    # Both surfaces are present and the leverage caption frames the trade-off.
    assert "Top 5 Captain lineups" in blob  # free-EV build
    assert "rank by ceiling" in blob.lower()
    assert "trades ev for ceiling" in blob.lower()


def test_captain_path_writes_nothing_with_mov_odds(isolated_db):
    """Even with MOV odds entered and a Finish-aware build, the Captain path opens
    no DB connection and persists nothing (design §4 C5 MVP; ``docs/DEVELOPMENT_NOTES.md`` §11)."""

    def _mov(at: AppTest) -> None:
        _set_mov_tree(at, "Alex Pereira", *_PEREIRA_MOV)
        _set_mov_tree(at, "Ciryl Gane", *_GANE_MOV)

    at = _build_finish_aware(isolated_db, _mov)
    assert "Top 5 Captain lineups" in _text_blob(at)

    conn = get_connection()
    try:
        apply_schema(conn)
        assert SlateRepository(conn).list_all() == []
    finally:
        conn.close()
