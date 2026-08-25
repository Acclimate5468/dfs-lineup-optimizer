"""Two-step builder — Step 1 salary + Step 2 odds wiring (slices B3 / B4).

Realizes ``docs/TWO_STEP_BUILDER_PRODUCTION_DESIGN.md`` §5 (Step 1 — salary
upload / slate setup) and §6 (Step 2 — odds + news), building on the B2
shell (`app/pages/00_build.py`). The page is still a friendlier
*re-presentation* of services that already exist; it derives no new business
rule (design §1.1 / §2). The gate verdict (blocked / warning / ready /
not-started) is read straight off
:func:`src.slate.home_dashboard.builder_gate_view`, which itself only reads
each Manual Review check's own ``status`` / ``category`` — never
re-classifying a check, so the builder can never drift from Manual Review /
Optimizer / Export (design §13 risk #1).

What B3 wires (design §5):

- **Step 1 is interactive.** Upload a DK UFC Classic salary CSV → structural
  validation (``validate_dk_salary_dataframe``) → explicit **Create slate
  from this CSV** (``SlateRepository.create``) → explicit **Import salaries**
  (``import_dk_salary_dataframe``). Both writes are button-gated and go
  through the existing repository/service layer (§5.1 / §5.2 / §5.6;
  ``docs/DEVELOPMENT_NOTES.md`` §11); no new salary logic is added.
- **Game Info pairing stays suggest-only** (§5.3): after an import the page
  reports ``group_fighters_by_game_info`` counts and points the user to the
  Fight Card Review (Fight Groups) page for the explicit Apply. The builder
  creates no fight groups and never infers scheduled rounds (§5.3 / §5.4).
- **The "importer is NOT complete" non-claim is carried verbatim** (§5.1;
  ``docs/DEVELOPMENT_NOTES.md`` §8 / §10 Slice F): salary-derived state is provisional until
  the real-file smoke test is documented.

What B4 wires (design §6):

- **Step 2 odds status** is read-only data for the active slate: odds-row
  counts by source (manual / CSV / snapshot), persisted match-result coverage
  (the same ``effective_status`` buckets the Odds page Zone 3 shows —
  matched / need-review / rejected), and the active manual-override count.
  These are plain repository reads; the gate verdict is still owned by
  ``builder_gate_view`` (no rule re-derived — §1.1 / §6.4).
- **DraftKings copied-board paste is the recommended odds path** (§6.2 /
  §6.3): paste the visible DraftKings board → the pure ``parse_draftkings_paste``
  parser previews the normalized moneylines (offline; reads only the pasted
  string) → an explicit **Save** writes them into the *active* slate through
  the existing ``odds_rows`` → recompute path. An optional **BestFightOdds**
  fetch previews public event moneylines (preview-only; never saved). News
  flags, props, and line movement stay preview-only and are never persisted
  (§6.6).
- **Odds CSV / manual entry, the review-by-exception table, and reject-match
  overrides stay on the 03 Odds page** (their multi-step, session-coupled
  single write path lives there — §6.1 / §6.4). The builder surfaces status
  and links there rather than duplicating those flows.

What B5 wires (design §7 / §12):

- **The Manual Review gate becomes actionable.** When the gate is
  structurally clean (``builder_gate_view.ready_to_mark`` — every Blocking
  check except the reviewer ack passes) an explicit **Mark slate reviewed**
  control is shown; it reuses the Manual Review page's single write path
  (``SlateRepository.set_manual_review_reviewed``) verbatim, never fires on
  page load, and never auto-acknowledges (§7.3 / §7.4). The session-only
  late-news / weigh-in checkbox is carried over unchanged.
- **Build is wired to the gated optimizer.** The **Build lineups** button is
  enabled only when ``builder_gate_view.ready_to_build`` (==
  ``readiness.summary.ready``) is True. On an explicit click the handler
  re-evaluates the gate fresh and aborts before the solver if the slate is no
  longer ready, then calls the read-only ``run_optimizer`` (which itself
  re-reads the gate and raises ``ManualReviewGateError`` as defense in depth
  — §7.5 / §7.6). The resulting in-memory lineups render read-only; no lineup
  is persisted, no DK upload CSV is produced, and no contest is entered
  (§7.7). The builder never calls the pool builder or solver directly.
- **Deterministic per-lineup reasoning is wired (B6).** After a successful
  Build, each lineup carries a compact "Why this lineup?" expander whose lines
  come from the read-only ``assemble_reasoning_context`` (design §8.1) feeding
  the pure ``build_lineup_reasoning`` generator (design §8). The assembler
  composes the read-only ``build_run_log`` bundle with ``project_slate`` /
  ``aggregate_projection_inputs`` and the gate readout — no new math, no
  writes, no projection recompute (§7.6 / §7.7). Every line is fact-bounded to
  the closed §8.2 list; the generator never asserts a fight outcome (§8.3).

What this slice deliberately leaves to later slices:

- **Run-log download buttons and the B7 cutover are not wired here.** The
  optional ``format_*`` download surface (design §11.1 B6) and promoting the
  builder into ``app/streamlit_app.py`` remain their own slices.

Read-only invariants (design §11.3; ``docs/DEVELOPMENT_NOTES.md`` §11): page load and slate
switch perform no INSERT/UPDATE/DELETE. The only writes are the explicit
Step 1 Create / Import buttons, the Step 2 DraftKings paste Save button, and
the Build section's **Mark slate reviewed** button; Build itself
(``run_optimizer``) is read-only end to end. The only session write is
``active_slate_id`` (the shared key the Command Center owns; §5.2). File
upload, validation, the Game Info readout, the odds-status reads, the
BestFightOdds preview, and lineup rendering all write nothing.

This file is a new page (``00_`` sorts it to the top of the sidebar); the
existing home and all nine detail pages are untouched. The cutover that
promotes the builder to ``app/streamlit_app.py`` is slice B7 (design §3).
"""

from __future__ import annotations

import html
import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config.constants import SALARY_CAP  # noqa: E402
from src.db.connection import get_connection  # noqa: E402
from src.db.migrations import bootstrap_database  # noqa: E402
from src.db.repositories import (  # noqa: E402
    FightGroupRepository,
    FighterRepository,
    ManualMatchOverrideRepository,
    OddsMatchResultRepository,
    OddsRowRepository,
    SlateRecord,
    SlateRepository,
)
from src.ingestion.dk_salary_import_service import (  # noqa: E402
    IMPORTED,
    PARSE_FAILED,
    VALIDATION_FAILED,
    import_dk_salary_dataframe,
)
from src.ingestion.dk_salary_importer import (  # noqa: E402
    REQUIRED_COLUMNS,
    validate_dk_salary_dataframe,
)
from src.ingestion.providers import bestfightodds_fetch  # noqa: E402
from src.ingestion.providers.bestfightodds import (  # noqa: E402
    BestFightOddsParseError,
)
from src.ingestion.providers.bestfightodds_fetch import (  # noqa: E402
    BestFightOddsFetchError,
)
from src.ingestion.providers.multi_book_paste import (  # noqa: E402
    MultiBookPasteParseError,
    parse_multi_book_paste,
)
from src.ingestion import draftkings_paste_save  # noqa: E402
from src.ingestion import consensus_save  # noqa: E402
from src.ingestion.consensus_assembly import (  # noqa: E402
    assemble_fights,
    merge_sources,
)
from src.projections.odds_consensus import (  # noqa: E402
    compute_slate_consensus,
)
from src.ingestion.effective_status_resolver import (  # noqa: E402
    is_projection_eligible_effective_status,
)
from src.ingestion.manual_odds import (  # noqa: E402
    save_manual_odds_and_recompute,
)
from src.ingestion.providers.draftkings_paste import (  # noqa: E402
    DraftKingsPasteParseError,
    parse_draftkings_paste,
)
from src.ingestion.name_matching import suggest_best_fighter  # noqa: E402
from src.ingestion.odds_match_filters import (  # noqa: E402
    assignable_match_results,
)
from src.ingestion.odds_matching_service import (  # noqa: E402
    record_assign_match_override,
)
from src.ingestion.snapshot_odds_save import is_snapshot_source  # noqa: E402
from src.optimizer.lineup_solver import (  # noqa: E402
    STATUS_INFEASIBLE_CONSTRAINTS,
    STATUS_INFEASIBLE_POOL_TOO_SMALL,
    STATUS_OK,
    STATUS_OK_PARTIAL,
    SolveResult,
)
from src.optimizer.optimizer_service import (  # noqa: E402
    ManualReviewGateError,
    run_optimizer,
)
from src.exports.export_service import build_run_log  # noqa: E402
from src.exports.lineup_reasoning import (  # noqa: E402
    ReasoningItem,
    build_lineup_reasoning,
)
from src.exports.reasoning_service import (  # noqa: E402
    assemble_reasoning_context,
)
from src.slate import home_dashboard as hd  # noqa: E402
from src.slate import manual_review as mr  # noqa: E402
from src.slate.fight_group_apply_service import (  # noqa: E402
    GameInfoApplyResult,
    apply_game_info_pairings,
    compute_apply_context,
)
from src.slate.fight_grouping import (  # noqa: E402
    detect_main_event_pair,
    group_fighters_by_game_info,
)
from src.slate.manual_review_service import (  # noqa: E402
    ReviewReadiness,
    evaluate_manual_review,
)
from src.utils.text_cleaning import normalize_name  # noqa: E402

from app.captain_build import render_captain_section  # noqa: E402

st.set_page_config(
    page_title="Build — DK Lineup Lab",
    page_icon=":fire:",
    layout="centered",
    initial_sidebar_state="collapsed",
)

_ACTIVE_FIGHTER_STATUS = "active"

# --- Contest-format router (Captain Mode design §2 / §3, slice C1) ----------
# A thin, ADDITIVE selector chooses the contest format at the top of the Build
# workflow. ``CONTEST_CLASSIC`` is the default and falls through to the existing
# two-step builder below, BYTE-FOR-BYTE UNCHANGED; ``CONTEST_CAPTAIN`` renders
# the read-only Captain builder (``app.captain_build.render_captain_section``,
# slice C5) and ``st.stop()``s before any Classic code runs. The choice lives in
# ``st.session_state`` under the selector's own widget key
# (``_CONTEST_FORMAT_KEY``). The Captain section wires the additive C2–C4 modules
# in ``src/captain/`` (parse → de-vig → method → optimize) and is itself read-only
# (no DB connection, no writes). This selector is the only Classic-page edit: it
# adds no business rule, opens no DB connection, and writes nothing.
CONTEST_CLASSIC = "Classic"
CONTEST_CAPTAIN = "Captain"
_CONTEST_FORMAT_KEY = "builder_contest_format"

# Builder-gate verdict → the prototype's CSS state token. Presentation
# only; the verdict itself comes from ``builder_gate_view`` (design §4).
_VERDICT_CLASS: dict[str, str] = {
    hd.GATE_BLOCKED: "block",
    hd.GATE_WARNING: "warn",
    hd.GATE_READY: "ready",
    hd.GATE_NOT_STARTED: "idle",
}

# Checklist row status (already derived + tested in home_dashboard) → the
# prototype's chip CSS token. Keeps the chips in lock-step with the gate.
_CHIP_CLASS: dict[str, str] = {
    hd.ROW_PASS: "ok",
    hd.ROW_WARN: "warn",
    hd.ROW_BLOCK: "block",
    hd.ROW_NOT_STARTED: "idle",
}

# Session key holding a one-shot Step 1 feedback payload, written by a
# Create / Import handler immediately before ``st.rerun`` and rendered (then
# cleared) on the next run so the refreshed cards and the success banner show
# together. Failures are surfaced inline without a rerun (so the upload state
# is preserved for a retry).
_STEP1_FEEDBACK_KEY = "builder_step1_feedback"

# Step 1 DK Game Info apply (slice 1b). The active slate is stashed when the
# button renders so the ``on_click`` handler (which runs before the body on the
# apply rerun) knows which slate to act on; the apply result is stashed for the
# body to render once. The handler defers entirely to the shared
# ``apply_game_info_pairings`` service — Build adds no new pairing/rounds logic
# (design FIGHT_GROUP_APPLY_SERVICE_DESIGN §5). No DB connection is stored.
_GI_APPLY_SLATE_KEY = "builder_game_info_apply_slate"
_GI_APPLY_RESULT_KEY = "builder_game_info_apply_result"

# One-shot ``(kind, message)`` feedback for the inline odds name-match fixer
# (the unresolved-row resolver added under the Step 2 status). A successful
# per-row Assign stashes its "Assigned X to Y." line here and reruns so the
# status above refreshes and the resolved row drops off; the message is shown
# on the next run even when that was the last unresolved row.
_ODDS_FIX_FEEDBACK_KEY = "builder_odds_fix_feedback"

# --- BestFightOdds live fetch → preview (Odds Acquisition v0 Phase 2) -------
# Phase 2 is preview-only: an explicit, user-triggered GET of a public
# BestFightOdds page, parsed by the pure Phase 1 parser, rendered for review.
# Nothing is fetched on page load and nothing is saved (no DB write, no
# recompute, no override change — that is Phase 3). The preview rows and any
# fetch/parse error live only in session state (keyed below) so they survive a
# rerun within the session; they vanish on a hard refresh, which is acceptable
# for Phase 2 (design §3 / §1.9 / §1.11 / §1.12).
_BFO_URL_KEY = "builder_bfo_url"
_BFO_FETCH_BTN_KEY = "builder_bfo_fetch_btn"
_BFO_PREVIEW_KEY = "builder_bfo_preview"
_BFO_ERROR_KEY = "builder_bfo_error"

# --- DraftKings copied-board paste → preview + save (Odds Acquisition v0
# Phases 4 + 3A) ---
# The user views the public DraftKings UFC odds board in their own browser,
# copies the visible text, and pastes it here. An explicit click parses that
# text with the pure ``parse_draftkings_paste`` parser and renders the
# normalized moneylines for review. Parsing is offline (the parser reads only
# the pasted string — no network at all), nothing is parsed on page load, and a
# parse alone writes nothing. Phase 3A adds an explicit, separate **Save parsed
# DraftKings odds to slate** action that persists the previewed rows through the
# existing ``odds_rows`` → recompute path (``draftkings_paste_save``); it fires
# only on a button click, never on parse or page load. An optional DraftKings
# source URL is carried only as provenance — it is never fetched and (no
# ``odds_rows`` URL column) never persisted. The preview rows / provenance /
# warnings and any parse error live only in session state (keyed below) so they
# survive a rerun within the session; they vanish on a hard refresh, which is
# acceptable (design §2 / §3 / §1.9 / §1.11 / §1.12; ``docs/DEVELOPMENT_NOTES.md`` §3 / §11).
_DK_PASTE_TEXT_KEY = "builder_dk_paste_text"
_DK_PASTE_URL_KEY = "builder_dk_paste_url"
_DK_PASTE_BTN_KEY = "builder_dk_paste_btn"
_DK_PASTE_SAVE_BTN_KEY = "builder_dk_paste_save_btn"
_DK_PASTE_PREVIEW_KEY = "builder_dk_paste_preview"
_DK_PASTE_ERROR_KEY = "builder_dk_paste_error"
# One-shot feedback for a DraftKings paste Save (persist + recompute), stashed
# so the page can rerun and refresh the Step 2 odds-status card / gate above in
# the same interaction instead of a run behind. Mirrors the manual-odds pattern.
_DK_PASTE_SAVE_FEEDBACK_KEY = "builder_dk_paste_save_feedback"

# --- Multi-book consensus → preview + save (ODDS_CONSENSUS_DESIGN §5.5 / §8) ---
# Enter a BestFightOdds event URL and/or paste a multi-book odds grid, click
# Preview consensus to fetch (if a URL) + parse + blend (median of de-vigged
# books) into a read-only per-fighter table, then explicitly Save consensus to
# slate. The single BestFightOdds GET fires only inside the Preview click; the
# parse is offline. Preview writes nothing; Save persists one
# ``source="consensus"`` odds row per fighter (+ per-book provenance) through the
# existing matcher → recompute (``consensus_save.save_consensus_to_slate``). The
# preview payload (parser rows + display table) and any error live in session
# state so they survive a rerun; SAVE_FEEDBACK is one-shot (set, then rerun +
# pop) so the Step 2 odds-status card refreshes in the same interaction.
_CONSENSUS_URL_KEY = "builder_consensus_url"
_CONSENSUS_PASTE_KEY = "builder_consensus_paste"
_CONSENSUS_PREVIEW_BTN_KEY = "builder_consensus_preview_btn"
_CONSENSUS_SAVE_BTN_KEY = "builder_consensus_save_btn"
_CONSENSUS_PREVIEW_KEY = "builder_consensus_preview"
_CONSENSUS_ERROR_KEY = "builder_consensus_error"
_CONSENSUS_SAVE_FEEDBACK_KEY = "builder_consensus_save_feedback"

# Inline single-fighter manual moneyline entry (Step 2). One-shot feedback for
# a successful Save (persist + recompute) is stashed and the page reruns so the
# odds status / gate above refresh; widget keys carry the fighter + moneyline.
_MANUAL_ODDS_FIGHTER_KEY = "builder_manual_odds_fighter"
_MANUAL_ODDS_ML_KEY = "builder_manual_odds_moneyline"
_MANUAL_ODDS_SAVE_BTN_KEY = "builder_manual_odds_save_btn"
_MANUAL_ODDS_FEEDBACK_KEY = "builder_manual_odds_feedback"

# Same one-shot pattern for the Build section's Mark-reviewed write: the
# successful ``set_manual_review_reviewed`` call stashes its outcome and
# reruns so the gate panel / chips / Build enablement above refresh to the
# now-reviewed state (a failed write is surfaced inline without a rerun).
_MARK_REVIEWED_FEEDBACK_KEY = "builder_mark_reviewed_feedback"

# Session-only acknowledgement that the user has reviewed the scheduled rounds
# (3 vs 5) for the card. When ticked it dismisses the §5.3 scheduled-rounds
# Warning for a fully-confirmed slate, so a 5-round main event stops nagging.
# Read at the top of the run (set on the prior run by the checkbox below) and
# passed into ``evaluate_manual_review``.
_SCHEDULED_ROUNDS_ACK_KEY = "builder_scheduled_rounds_ack"

# Button key for the gate's direct Fight Groups jump (the only navigation off
# the Build surface in the prototype). Odds are resolved inline on Build, so
# there is no jump to the 03 Odds page. ``st.switch_page``-s on click only.
_FIX_FIGHT_GROUPS_BTN_KEY = "builder_goto_fight_groups_btn"

# Button key for the *standing* Fight Groups link in the Step 1 card. Unlike the
# gate's blocking ``_FIX_FIGHT_GROUPS_BTN_KEY`` jump (shown only while fight-group
# coverage blocks), this one renders at every gate state so Fight Card Review is
# reachable from Build even on a green slate, where the (collapsed) sidebar would
# otherwise be the only route. ``st.switch_page``-s on click only.
_FIGHT_GROUPS_NAV_BTN_KEY = "builder_fight_groups_nav_btn"

# The slate selector's own widget key — deliberately distinct from the shared
# ``active_slate_id`` session value so Create / Import can update the active
# slate after the selector has been instantiated this run (Streamlit forbids
# mutating a widget-owned key post-instantiation). The selection is synced into
# ``active_slate_id`` each run.
_SLATE_SELECTOR_KEY = "builder_active_slate_selector"

# One-shot hand-off written by Create just before its ``st.rerun`` and consumed
# at the top of the next run — *before* the selector widget renders — so the
# new slate becomes active and the selector shows it without a post-widget
# write (the bug this hotfix repairs).
_PENDING_ACTIVE_SLATE_KEY = "builder_pending_active_slate"

# --- Local slate cleanup ("start fresh") session keys ----------------------
# All destructive cleanup is explicit and routed through the repository layer;
# nothing here fires on page load (docs/DEVELOPMENT_NOTES.md §11). The delete / reset handlers
# stash a one-shot reset flag + feedback and rerun; the flag is consumed at the
# very top of the next run — *before* any widget renders — to clear the active
# slate, the selector widget state, and the armed confirmation widgets, so the
# page re-resolves the active slate from scratch with no stale id and no
# pre-armed delete (mutating those widget keys post-instantiation is illegal).
_DELETE_CONFIRM_KEY = "builder_delete_confirm"
_RESET_ALL_TEXT_KEY = "builder_reset_all_text"
_POST_DELETE_RESET_KEY = "builder_post_delete_reset"
_CLEANUP_FEEDBACK_KEY = "builder_cleanup_feedback"


# ---------------------------------------------------------------------------
# Two-step-builder stylesheet. Ported from the DK palette in
# ``docs/ui_prototypes/two_step_builder.html``. Two layers:
#
#   1. Scoped ``tsb-`` classes for the bespoke card / gate / chip markup.
#   2. A *page-local* theme that restyles Streamlit's own chrome (app
#      background, content column, buttons, file uploaders, inputs,
#      bordered containers) so the production builder reads like the
#      prototype rather than a default Streamlit page.
#
# The whole block is injected via ``st.markdown`` only while ``00_build`` is
# rendering, so it never leaks onto the Command Center or the detail pages
# (each multipage script run re-renders its own DOM — docs/DEVELOPMENT_NOTES.md scope: this
# slice touches only ``00_build.py``). Injected once per render.
# ---------------------------------------------------------------------------

_CSS = """
<style>
:root {
  --tsb-bg:#0a0e0f; --tsb-panel:#141a1d; --tsb-line:#2a3439;
  --tsb-txt:#f4f7f5; --tsb-dim:#9fb0a8; --tsb-faint:#6f8079;
  --tsb-accent:#53d337; --tsb-accent-2:#36a31f; --tsb-orange:#f46c21;
  --tsb-ready:#53d337; --tsb-ready-bg:rgba(83,211,55,.14);
  --tsb-warn:#f5a623;  --tsb-warn-bg:rgba(245,166,35,.15);
  --tsb-block:#ff5247; --tsb-block-bg:rgba(255,82,71,.14);
  --tsb-idle:#7a8194;  --tsb-idle-bg:rgba(120,130,160,.12);
}

/* ---- Page chrome: dark DK canvas, compact centered column ------------- */
.stApp {
  background:
    radial-gradient(900px 500px at 85% -10%, #103018 0%, transparent 60%),
    var(--tsb-bg);
  color:var(--tsb-txt);
}
[data-testid="stHeader"] { background:transparent; }

/* ---- No sidebar: the builder is a prototype canvas, not a nav shell.
   Hide Streamlit's multipage nav + the whole sidebar (and the collapsed
   "open" control / collapse button) so the page matches
   ``two_step_builder.html``, which has no sidebar at all.
   ``initial_sidebar_state="collapsed"`` only sets the *initial* state and
   leaves the nav + expand control reachable; this removes them. Scoped to
   this page's render only (each multipage script run owns its own DOM). - */
[data-testid="stSidebar"],
[data-testid="stSidebarNav"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="stSidebarCollapseButton"],
[data-testid="stExpandSidebarButton"],
[data-testid="collapsedControl"] {
  display:none !important;
}

[data-testid="stMainBlockContainer"], .block-container {
  max-width:960px; margin:0 auto;
  padding-top:2rem; padding-bottom:5rem;
}
[data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp label,
.stApp [data-testid="stWidgetLabel"] * { color:var(--tsb-txt); }
[data-testid="stCaptionContainer"], .stApp small { color:var(--tsb-dim); }
.stApp hr, [data-testid="stDivider"] hr { border-color:var(--tsb-line); }

/* ---- Stacked step cards: orange-outlined dark panels ------------------ */
[data-testid="stVerticalBlockBorderWrapper"] {
  background:var(--tsb-panel); border:1px solid var(--tsb-orange);
  border-radius:14px; margin-top:14px;
}

/* ---- Action buttons: DK signature green; Build = gradient primary ----- */
[data-testid="stBaseButton-secondary"], .stButton > button {
  background:var(--tsb-accent); color:#07230b; border:none;
  font-weight:700; border-radius:10px;
  box-shadow:0 6px 16px rgba(83,211,55,.28);
}
[data-testid="stBaseButton-secondary"]:hover, .stButton > button:hover {
  background:#46c42f; color:#07230b;
}
[data-testid="stBaseButton-secondary"]:disabled, .stButton > button:disabled {
  background:#2a3439; color:#8aa093; box-shadow:none; opacity:.55;
}
[data-testid="stBaseButton-primary"] {
  background:linear-gradient(135deg,var(--tsb-accent),var(--tsb-accent-2));
  color:#07230b; border:none; font-weight:800; border-radius:12px;
  box-shadow:0 8px 22px rgba(83,211,55,.35);
}
[data-testid="stBaseButton-primary"]:disabled {
  background:#2a3439; color:#8aa093; box-shadow:none; opacity:.55;
}

/* ---- File uploaders: dashed dark drop zones --------------------------- */
[data-testid="stFileUploaderDropzone"] {
  background:#0f140f; border:1.5px dashed var(--tsb-line);
  border-radius:12px;
}
[data-testid="stFileUploaderDropzoneInstructions"],
[data-testid="stFileUploaderDropzoneInstructions"] * { color:var(--tsb-faint); }

/* ---- Form inputs: dark fields to match the canvas --------------------- */
.stTextInput input, .stNumberInput input, .stDateInput input,
[data-baseweb="input"], [data-baseweb="select"] > div,
[data-baseweb="base-input"] {
  background:#0f140f; color:var(--tsb-txt);
  border-color:var(--tsb-line);
}

.tsb-card {
  background:transparent; border:none; padding:2px 0 4px;
  color:var(--tsb-txt);
}
.tsb-step { font-size:11px; letter-spacing:1px; text-transform:uppercase;
  color:var(--tsb-faint); font-weight:700; }
.tsb-card h2 { font-size:17px; margin:5px 0 3px; }
.tsb-desc { color:var(--tsb-dim); font-size:12.5px; margin-bottom:14px; }
.tsb-okrow { display:flex; align-items:center; gap:9px; font-weight:700;
  font-size:14px; }
.tsb-okrow .tsb-dot { width:9px; height:9px; border-radius:50%;
  background:currentColor; flex:none; }
.tsb-okrow.tsb-ready { color:var(--tsb-ready); }
.tsb-okrow.tsb-warn  { color:var(--tsb-warn); }
.tsb-okrow.tsb-block { color:var(--tsb-block); }
.tsb-okrow.tsb-idle  { color:var(--tsb-idle); }
.tsb-stat { display:flex; gap:22px; margin-top:14px; }
.tsb-stat .tsb-v { font-size:22px; font-weight:800; }
.tsb-stat .tsb-l { font-size:11px; color:var(--tsb-faint);
  text-transform:uppercase; letter-spacing:.5px; }
.tsb-note { margin-top:12px; font-size:12.5px; color:var(--tsb-dim); }

/* ---- Header: DK badge + wordmark -------------------------------------- */
.tsb-head { display:flex; align-items:center; gap:12px; margin:0 0 4px; }
.tsb-logo { width:34px; height:34px; border-radius:9px;
  background:linear-gradient(135deg,var(--tsb-accent),var(--tsb-accent-2));
  display:grid; place-items:center; color:#fff; font-weight:800;
  font-size:15px; }
.tsb-h1 { font-size:22px; margin:0; font-weight:800; letter-spacing:-.3px;
  color:var(--tsb-txt); }
.tsb-sub { color:var(--tsb-dim); font-size:13.5px; margin:3px 0 18px; }

.tsb-gate-panel { border:1px solid var(--tsb-line); border-left-width:4px;
  border-radius:12px; padding:16px 18px; background:#10161a; }
.tsb-gate-panel.tsb-block { border-left-color:var(--tsb-block); }
.tsb-gate-panel.tsb-warn  { border-left-color:var(--tsb-warn); }
.tsb-gate-panel.tsb-ready { border-left-color:var(--tsb-ready); }
.tsb-gate-panel.tsb-idle  { border-left-color:var(--tsb-idle); }
.tsb-gate-top { display:flex; align-items:center; justify-content:space-between;
  gap:12px; margin-bottom:12px; }
.tsb-gate-title { display:flex; align-items:center; gap:8px; font-size:14.5px;
  font-weight:800; letter-spacing:.2px; }
.tsb-verdict { display:inline-flex; align-items:center; gap:8px; font-size:12px;
  font-weight:800; text-transform:uppercase; letter-spacing:.6px;
  padding:6px 13px; border-radius:999px; border:1px solid currentColor; }
.tsb-verdict .tsb-vd { width:9px; height:9px; border-radius:50%;
  background:currentColor; }
.tsb-verdict.tsb-ready { color:var(--tsb-ready); background:var(--tsb-ready-bg); }
.tsb-verdict.tsb-warn  { color:var(--tsb-warn);  background:var(--tsb-warn-bg); }
.tsb-verdict.tsb-block { color:var(--tsb-block); background:var(--tsb-block-bg); }
.tsb-verdict.tsb-idle  { color:var(--tsb-idle);  background:var(--tsb-idle-bg); }
.tsb-gate-summary { color:var(--tsb-dim); font-size:13px; margin-bottom:12px; }
.tsb-checks { display:flex; flex-wrap:wrap; gap:8px; }
.tsb-gc { display:inline-flex; align-items:center; gap:6px; font-size:12px;
  font-weight:600; padding:5px 11px; border-radius:8px;
  border:1px solid var(--tsb-line); }
.tsb-gc.tsb-ok    { color:var(--tsb-ready); background:var(--tsb-ready-bg);
  border-color:rgba(83,211,55,.3); }
.tsb-gc.tsb-warn  { color:var(--tsb-warn);  background:var(--tsb-warn-bg);
  border-color:rgba(245,166,35,.3); }
.tsb-gc.tsb-block { color:var(--tsb-block); background:var(--tsb-block-bg);
  border-color:rgba(255,82,71,.35); }
.tsb-gc.tsb-idle  { color:var(--tsb-idle);  background:var(--tsb-idle-bg); }
.tsb-chipmsg { color:var(--tsb-dim); font-size:12.5px; margin:10px 0 0;
  padding-left:2px; }
.tsb-chipmsg li { margin:3px 0; }

/* ---- Build bar status line (prototype ``.buildbar .status``) ---------- */
.tsb-buildstatus { color:var(--tsb-dim); font-size:13.5px; margin:2px 0 14px; }
.tsb-buildstatus b { color:var(--tsb-txt); font-weight:800; }
/* ---- Short, non-dominant in-card / footer note ------------------------ */
.tsb-shortnote { color:var(--tsb-faint); font-size:12px; margin:8px 0 0; }
</style>
"""


# ---------------------------------------------------------------------------
# Small pure render helpers (view only — each reads a check's own status /
# category; no verdict is re-derived — design §1.1 / §4).
# ---------------------------------------------------------------------------


def _slate_label(slate: SlateRecord) -> str:
    return (
        f"#{slate.id} — {slate.event_name}"
        + (f" ({slate.event_date})" if slate.event_date else "")
    )


def _domain_status(readiness: ReviewReadiness, *codes: str) -> str:
    """Most severe per-check status across ``codes`` → a card CSS token.

    Mirrors the (already-tested) severity rule the home dashboard's
    ``_signal_status`` uses: block beats warn beats pass; ``idle`` when no
    governing check is present. Reads only each check's own ``status`` /
    ``category`` — re-derives no rule (design §1.1).
    """
    by_code = {c.code: c for c in readiness.checks}
    present = [by_code[c] for c in codes if c in by_code]
    if not present:
        return "idle"
    if any(
        c.category == mr.CATEGORY_BLOCKING and c.status == mr.STATUS_FAIL
        for c in present
    ):
        return "block"
    if any(
        c.category == mr.CATEGORY_WARNING and c.status == mr.STATUS_FAIL
        for c in present
    ):
        return "warn"
    return "ready"


def _governing_message(readiness: ReviewReadiness, *codes: str) -> str:
    """Surface the most severe governing check's own message (verbatim,
    truncated). The gate, not the builder, owns this copy (design §4)."""
    by_code = {c.code: c for c in readiness.checks}
    present = [by_code[c] for c in codes if c in by_code]
    for want_cat in (mr.CATEGORY_BLOCKING, mr.CATEGORY_WARNING):
        for c in present:
            if c.category == want_cat and c.status == mr.STATUS_FAIL:
                return _truncate(c.message)
    for c in present:
        if c.status == mr.STATUS_PASS:
            return _truncate(c.message)
    return "Not started — complete the earlier steps first."


def _truncate(text: str, cap: int = 160) -> str:
    text = " ".join((text or "").split())
    if len(text) <= cap:
        return text
    return text[: cap - 1].rstrip() + "…"


def _empty_readiness() -> ReviewReadiness:
    """The vacuous readiness used for the empty-DB call-to-action (mirrors
    ``app/streamlit_app.py`` §3.2; nothing to evaluate yet)."""
    return ReviewReadiness(
        slate_id=0,
        manual_review_status=None,
        manual_review_completed_at=None,
        checks=(),
        summary=mr.summarize([]),
    )


# ---------------------------------------------------------------------------
# Render blocks.
# ---------------------------------------------------------------------------


def _render_salary_card(
    readiness: ReviewReadiness,
    *,
    active_fighter_count: int,
    fight_group_count: int,
) -> None:
    """Step 1 — DraftKings salary status card (read-only; design §5.5)."""
    status = _domain_status(readiness, mr.CHECK_SALARY_IMPORTED)
    imported = status == "ready"
    headline = (
        "DK salaries imported" if imported else "No salaries imported yet"
    )
    st.markdown(
        f'<div class="tsb-card tsb-{status}">'
        '<div class="tsb-step">Step 1</div>'
        "<h2>DraftKings salary</h2>"
        '<div class="tsb-desc">Import the DKSalaries CSV for the slate.</div>'
        f'<div class="tsb-okrow tsb-{status}"><span class="tsb-dot"></span>'
        f"{html.escape(headline)}</div>"
        '<div class="tsb-stat">'
        f'<div><div class="tsb-v">{active_fighter_count}</div>'
        '<div class="tsb-l">Fighters</div></div>'
        f'<div><div class="tsb-v">{fight_group_count}</div>'
        '<div class="tsb-l">Fights</div></div>'
        f'<div><div class="tsb-v">${SALARY_CAP // 1000}k</div>'
        '<div class="tsb-l">Cap</div></div>'
        "</div>"
        '<div class="tsb-note">Fight pairings are <b>suggested</b> from the '
        "DK Game Info column — confirm them on Fight Card Review (02 Fight "
        "Groups), and set the 5-round main event / title bout there. "
        "Upload &amp; import salaries below.</div>"
        "</div>",
        unsafe_allow_html=True,
    )


def _apply_game_info_callback() -> None:
    """on_click handler for Build Step 1 "Apply Suggested DK Pairings".

    The only Build Step 1 fight-group write (design
    FIGHT_GROUP_APPLY_SERVICE_DESIGN §5.2). Runs at the start of the apply
    rerun, before the body re-renders, so the freshly created groups and the
    refreshed Step 1 fight count / gate appear on the same run with no explicit
    ``st.rerun`` (docs/DEVELOPMENT_NOTES.md §11). It defers entirely to the shared
    ``apply_game_info_pairings`` service with ``include_grouped=False`` and
    ``auto_set_main_event=True`` — Build never confirms a group and never infers
    a new rounds rule (§5.3 / §5.4); the service recomputes the suggestions from
    the persisted roster, so a re-call under unchanged state creates nothing.
    The slate id is read from session state (stashed by the body when the button
    rendered). Opens its own short-lived connection, like the other Build write
    callbacks."""
    slate_id = st.session_state.get(_GI_APPLY_SLATE_KEY)
    if slate_id is None:
        return
    conn = get_connection()
    bootstrap_database(conn)
    try:
        st.session_state[_GI_APPLY_RESULT_KEY] = apply_game_info_pairings(
            conn,
            int(slate_id),
            include_grouped=False,
            auto_set_main_event=True,
        )
    finally:
        conn.close()


def _render_game_info_apply_result(result: GameInfoApplyResult) -> None:
    """Render the one-shot Build Step 1 apply outcome (design §5.2 / §5.3).

    Created groups are ``unconfirmed`` — Build never confirms. The auto-detected
    main event (latest Game Info start) is created at 5 rounds and named; with no
    clear main event every group is 3 rounds and the user is told to set the
    5-round bout on Fight Card Review. Skipped lines name already-grouped
    fighters left untouched. Pure display — writes nothing."""
    outcome = result.outcome
    created = outcome.created
    skip_lines = list(outcome.skipped_grouped) + [
        f"{pair} — pair already saved" for pair in outcome.skipped_exists
    ]
    if created:
        st.success(
            f"Applied {len(created)} new fight group(s) from DK Game Info: "
            + "; ".join(f"{a} vs {b}" for a, b in created)
            + ". Groups are unconfirmed — confirm them on Fight Card Review "
            "(02 Fight Groups)."
        )
        if outcome.five_round:
            st.info(
                "Auto-detected main event (latest Game Info start): "
                f"**{outcome.five_round}** — set to **5 rounds**. If a "
                "different bout is the 5-round fight, change it on Fight Card "
                "Review."
            )
        else:
            st.warning(
                "No main event auto-detected (Game Info start times missing or "
                "ambiguous) — all new groups were created at 3 rounds. Set the "
                "5-round main event / title bout manually on Fight Card Review "
                "(02 Fight Groups)."
            )
        if skip_lines:
            st.warning(
                f"Skipped {len(skip_lines)} already-grouped pairing(s): "
                + "; ".join(skip_lines)
                + ". Existing groups were left unchanged (no overwrite, no "
                "delete)."
            )
    elif result.eligible:
        st.info(
            f"No new fight groups created — all {result.eligible} suggested "
            "pairing(s) name fighters that are already grouped, so they were "
            "skipped and existing groups were left unchanged. Manage them on "
            "Fight Card Review (02 Fight Groups)."
        )
    else:
        st.info("No suggested DK pairings to apply.")
    if outcome.errors:
        st.error("Could not save some pairings: " + "; ".join(outcome.errors))


def _render_suggested_pairings(conn, roster, *, active_slate_id: int | None) -> None:
    """Step 1 — DK Game Info suggested pairings + explicit Apply (slices 1a/1b).

    Reuses ``group_fighters_by_game_info`` + ``detect_main_event_pair``
    (``src/slate/fight_grouping.py``) verbatim to show the detected fights and
    the latest-starting bout as the auto-detected 5-round main event (read-only).
    Slice 1b adds one explicit forward action: an **Apply Suggested DK Pairings**
    button (design §5) that, on click, creates the unconfirmed fight groups via
    the shared ``apply_game_info_pairings`` service. The button renders only when
    an active slate exists and ≥1 suggested pair is ready to apply (not already
    grouped / not already saved); when every suggestion is already grouped it is
    replaced by a compact note. Rendering writes nothing — the apply is the
    button's ``on_click`` only. Fight Card Review (02 Fight Groups) remains the
    advanced / manual correction surface (§5.4)."""
    # One-shot apply result (stashed by the on_click callback that ran before
    # this body). Render once, scoped to the active slate.
    _result = st.session_state.pop(_GI_APPLY_RESULT_KEY, None)
    if (
        _result is not None
        and active_slate_id is not None
        and _result.slate_id == active_slate_id
    ):
        _render_game_info_apply_result(_result)

    grouping = group_fighters_by_game_info(roster)
    pairs = grouping.suggested_pairs
    if not pairs:
        st.markdown(
            '<div class="tsb-note">No DK Game Info pairings detected yet — '
            "import salaries whose Game Info column is populated, then confirm "
            "fights on Fight Card Review (02 Fight Groups).</div>",
            unsafe_allow_html=True,
        )
        return

    main_event = detect_main_event_pair(pairs)

    rows_html = []
    for pair in pairs:
        is_main = (
            main_event is not None and pair.game_info == main_event.game_info
        )
        tag = (
            ' <span class="tsb-gc tsb-warn">Main event · 5 rounds</span>'
            if is_main
            else ""
        )
        rows_html.append(
            "<li>"
            f"{html.escape(pair.fighter_1_name)} vs "
            f"{html.escape(pair.fighter_2_name)}{tag}</li>"
        )

    if main_event is not None:
        main_line = (
            "Auto-detected main event (latest start → 5 rounds): "
            f"<b>{html.escape(main_event.fighter_1_name)} vs "
            f"{html.escape(main_event.fighter_2_name)}</b>."
        )
    else:
        main_line = (
            "No main event auto-detected — start times missing or ambiguous; "
            "set the 5-round bout manually on Fight Card Review."
        )

    st.markdown(
        '<div class="tsb-card">'
        '<div class="tsb-step">Suggested fights</div>'
        f'<div class="tsb-desc">{len(pairs)} DK Game Info pairing(s) detected '
        "— apply them below, then confirm and correct on Fight Card Review "
        "(02 Fight Groups).</div>"
        f'<ul class="tsb-chipmsg">{"".join(rows_html)}</ul>'
        f'<div class="tsb-note">{main_line}</div>'
        "</div>",
        unsafe_allow_html=True,
    )

    # Apply affordance (design §5.1). Without an active slate there is nothing
    # to write into. "Ready to apply" mirrors exactly what the service will
    # create: a suggested pair whose fighters are not already grouped and whose
    # exact pair is not already saved — a pure read of the persisted state, so
    # no write happens on render.
    if active_slate_id is None:
        return
    _roster_norms, grouped_norms, existing_pairs = compute_apply_context(
        FightGroupRepository(conn), int(active_slate_id), roster
    )
    ready = [
        p
        for p in pairs
        if normalize_name(p.fighter_1_name) not in grouped_norms
        and normalize_name(p.fighter_2_name) not in grouped_norms
        and frozenset(
            (normalize_name(p.fighter_1_name), normalize_name(p.fighter_2_name))
        )
        not in existing_pairs
    ]
    if not ready:
        st.caption(
            "All DK Game Info pairings are already applied. Confirm or correct "
            "fights on Fight Card Review (02 Fight Groups)."
        )
        return

    # Stash the slate so the on_click handler (which runs before the next body)
    # acts on the right slate.
    st.session_state[_GI_APPLY_SLATE_KEY] = int(active_slate_id)
    st.button(
        "Apply Suggested DK Pairings",
        key="builder_apply_game_info_btn",
        on_click=_apply_game_info_callback,
        help=(
            f"Create {len(ready)} unconfirmed fight group(s) from the ready DK "
            "Game Info pairing(s). The latest-starting bout is set to 5 rounds; "
            "already-grouped fighters are skipped. Confirm and correct on Fight "
            "Card Review (02 Fight Groups)."
        ),
    )


def _render_odds_card(readiness: ReviewReadiness) -> None:
    """Step 2 — Odds + news status card (read-only; design §6.4)."""
    status = _domain_status(
        readiness,
        mr.CHECK_ODDS_UNMATCHED_ACTIVE,
        mr.CHECK_ODDS_COVERAGE_PARTIAL,
        mr.CHECK_ODDS_MATCH_REVIEW,
    )
    message = _governing_message(
        readiness,
        mr.CHECK_ODDS_UNMATCHED_ACTIVE,
        mr.CHECK_ODDS_COVERAGE_PARTIAL,
        mr.CHECK_ODDS_MATCH_REVIEW,
    )
    headline = {
        "ready": "Odds matched",
        "warn": "Odds checked — review warnings",
        "block": "Odds checked — blocking issues",
        "idle": "Odds not checked yet",
    }[status]
    st.markdown(
        f'<div class="tsb-card tsb-{status}">'
        '<div class="tsb-step">Step 2</div>'
        "<h2>Odds checker</h2>"
        '<div class="tsb-desc">Import current moneylines &amp; news for the '
        "same slate.</div>"
        f'<div class="tsb-okrow tsb-{status}"><span class="tsb-dot"></span>'
        f"{html.escape(headline)}</div>"
        f'<div class="tsb-note">{html.escape(message)}</div>'
        '<div class="tsb-note">News flags, line movement and snapshot age '
        "are <b>preview-only</b> and never saved. Import / review odds and "
        "save snapshot moneylines on 03 Odds.</div>"
        "</div>",
        unsafe_allow_html=True,
    )


def _render_gate_panel(view: hd.BuilderGateView) -> None:
    """The folded Build gate panel — verdict, chips, and next action, all
    read straight off ``builder_gate_view`` (display-only in B3; design
    §4 / §7.2). Marking reviewed and Build stay on their owning detail
    pages until slice B5 wires them here."""
    vclass = _VERDICT_CLASS.get(view.verdict, "idle")

    chips_html = "".join(
        f'<span class="tsb-gc tsb-{_CHIP_CLASS.get(chip.status, "idle")}">'
        f"{chip.icon} {html.escape(chip.label)}</span>"
        for chip in view.chips
    )

    st.markdown(
        f'<div class="tsb-gate-panel tsb-{vclass}">'
        '<div class="tsb-gate-top">'
        '<div class="tsb-gate-title">🔒 Manual Review gate</div>'
        f'<div class="tsb-verdict tsb-{vclass}"><span class="tsb-vd"></span>'
        f"{html.escape(view.title)}</div>"
        "</div>"
        f'<div class="tsb-gate-summary">{html.escape(view.summary)}</div>'
        f'<div class="tsb-checks">{chips_html}</div>'
        "</div>",
        unsafe_allow_html=True,
    )


def _fight_groups_action_needed(view: hd.BuilderGateView) -> bool:
    """True when the Build gate is blocked by fight-group coverage — the page
    then offers a direct **Fix fight groups** jump to 02 Fight Groups (the
    coverage check's own page). Reads the gate chip's own status; re-derives no
    rule (design §4)."""
    return any(
        chip.code == mr.CHECK_FIGHT_GROUP_COVERAGE and chip.status == hd.ROW_BLOCK
        for chip in view.chips
    )


# ---------------------------------------------------------------------------
# Step 1 write section — upload / validate / Create / Import (design §5).
#
# Two explicit, button-gated writes, both through the existing
# repository/service layer (§5.6 / docs/DEVELOPMENT_NOTES.md §11):
#   1. Create slate from this CSV → SlateRepository.create
#   2. Import salaries           → import_dk_salary_dataframe
# Page load, file upload, validation, and the Game Info readout write
# nothing. No salary logic is added — this reuses the Slate Setup services
# verbatim (§5.1) and the Game Info readout stays suggest-only (§5.3).
# ---------------------------------------------------------------------------


def _step1_feedback_payload(result, roster, target_id: int) -> dict:
    """Build the one-shot feedback payload for an import outcome.

    On success it carries the parsed/inserted/updated/unchanged/deactivated
    summary plus the suggest-only Game Info readout (counts only — this
    action creates no fight groups; design §5.3). On a structural /
    parse failure it carries an error message; no fighters were persisted.
    """
    if result.status == IMPORTED and result.upsert is not None:
        ups = result.upsert
        summary = (
            f"Imported salaries into slate #{target_id}: "
            f"parsed {result.parsed_row_count}, "
            f"inserted {ups.inserted}, "
            f"updated {ups.updated}, "
            f"unchanged {ups.unchanged}, "
            f"deactivated {ups.deactivated}."
        )
        active = [f for f in roster if f.status == _ACTIVE_FIGHTER_STATUS]
        grouping = group_fighters_by_game_info(roster)
        captured = len(active) - grouping.uncovered_count
        if not active:
            gi_kind = "info"
            gi_msg = "No active fighters persisted — no Game Info to report."
        elif captured == 0:
            gi_kind = "warn"
            gi_msg = (
                f"Game Info captured: 0 of {len(active)} active fighters. "
                "This CSV carried no usable Game Info, so no DK pairings can "
                "be suggested — pair fighters manually on the Fight Groups "
                "page."
            )
        else:
            gi_kind = "info"
            gi_msg = (
                f"Game Info captured: {captured} of {len(active)} active "
                f"fighters. Suggested DK pairings available: "
                f"{grouping.suggested_count} — review and apply on the Fight "
                "Groups page. Remember to set the 5-round main event / title "
                "bout manually after applying."
            )
        return {
            "kind": "imported",
            "summary": summary,
            "gi_kind": gi_kind,
            "gi_msg": gi_msg,
        }
    if result.status == VALIDATION_FAILED:
        return {
            "kind": "import_error",
            "msg": (
                "Salary CSV structural validation failed during import; no "
                "fighters were persisted. Details: "
                + (result.error_message or "(no detail)")
            ),
        }
    if result.status == PARSE_FAILED:
        return {
            "kind": "import_error",
            "msg": (
                "Salary CSV row-level parsing failed after structural "
                "validation; no fighters were persisted. Details: "
                + (result.error_message or "(no detail)")
            ),
        }
    return {
        "kind": "import_error",
        "msg": f"Unexpected salary import status: {result.status!r}",
    }


def _render_step1_feedback(fb: dict) -> None:
    """Render a one-shot Step 1 feedback payload (success / Game Info /
    error). Pure display — never writes."""
    kind = fb.get("kind")
    if kind == "created":
        st.success(
            f"Saved slate #{fb['slate_id']}: {fb['event_name']} — now the "
            "active slate. Click Import salaries below to persist its "
            "fighters."
        )
    elif kind == "imported":
        st.success(fb["summary"])
        if fb["gi_kind"] == "warn":
            st.warning(fb["gi_msg"])
        else:
            st.info(fb["gi_msg"])
    elif kind == "import_error":
        st.error(fb["msg"])


def _render_step1_upload(conn, *, active_slate_id: int | None) -> None:
    """Render the Step 1 upload / Create / Import controls (design §5).

    ``active_slate_id`` is the slate the active-slate selector resolved
    (``None`` only on an empty DB). Create makes a *new* slate and promotes
    it to active; Import persists the uploaded CSV's fighters into the
    active slate. Both are explicit button writes; everything else is
    read-only.
    """
    st.caption(
        "Upload the DK UFC Classic CSV, create a slate, then import fighters "
        "— nothing is written until you click Create slate or Import salaries."
    )

    # One-shot feedback from a prior Create / Import click (set just before
    # the rerun that refreshed the cards above). Rendered then cleared.
    fb = st.session_state.pop(_STEP1_FEEDBACK_KEY, None)
    if fb is not None:
        _render_step1_feedback(fb)

    name_col, date_col = st.columns(2)
    with name_col:
        event_name = st.text_input(
            "Event name",
            key="builder_event_name",
            placeholder="e.g. UFC 999",
        )
    with date_col:
        event_date_val = st.date_input(
            "Event date (optional)", value=None, key="builder_event_date"
        )

    uploaded = st.file_uploader(
        "DK UFC Classic salary CSV",
        type=["csv"],
        accept_multiple_files=False,
        help="Official DraftKings UFC Classic salary export.",
        key="builder_salary_upload",
    )

    salary_df: pd.DataFrame | None = None
    validation_result = None
    load_error: str | None = None
    if uploaded is not None:
        try:
            salary_df = pd.read_csv(io.BytesIO(uploaded.getvalue()))
            salary_df.columns = [c.strip() for c in salary_df.columns]
        except pd.errors.EmptyDataError:
            salary_df = None
            load_error = (
                "CSV is empty. Expected an official DK UFC Classic salary "
                "export with columns: " + ", ".join(REQUIRED_COLUMNS)
            )
        except Exception as exc:  # noqa: BLE001
            salary_df = None
            load_error = f"Could not parse CSV: {exc}"

        if salary_df is not None:
            validation_result = validate_dk_salary_dataframe(salary_df)

        if load_error is not None:
            st.error(load_error)
        elif validation_result is not None and validation_result.is_valid:
            st.success(
                f"Valid DK UFC Classic salary CSV — "
                f"{validation_result.row_count} rows detected."
            )
        elif validation_result is not None:
            st.error(
                validation_result.error_message or "CSV failed validation."
            )
            if validation_result.missing_columns:
                st.caption(
                    "Missing required columns: "
                    + ", ".join(validation_result.missing_columns)
                )

    csv_valid = validation_result is not None and validation_result.is_valid

    create_col, import_col = st.columns(2)

    # --- Create slate from this CSV (a new slate; becomes active) ---------
    with create_col:
        can_create = bool(event_name and event_name.strip()) and csv_valid
        if validation_result is None and load_error is None:
            st.caption("Upload and validate a salary CSV to create a slate.")
        elif not csv_valid:
            st.caption("Salary CSV must pass validation to create a slate.")
        elif not (event_name and event_name.strip()):
            st.caption("Enter an event name to create a slate.")

        if st.button(
            "Create slate from this CSV",
            key="builder_create_slate_btn",
            disabled=not can_create,
        ):
            try:
                record = SlateRepository(conn).create(
                    event_name=event_name.strip(),
                    event_date=(
                        event_date_val.isoformat() if event_date_val else None
                    ),
                    salary_csv_status="validated",
                    salary_row_count=validation_result.row_count,
                )
            except Exception as exc:  # noqa: BLE001
                st.error(f"Failed to save slate: {exc}")
            else:
                # Hand the new slate off as the pending active slate; it is
                # applied at the top of the next run before the selector renders
                # (writing ``active_slate_id`` / the selector key here would hit
                # the post-instantiation guard, since both already rendered).
                st.session_state[_PENDING_ACTIVE_SLATE_KEY] = record.id
                st.session_state["last_created_slate_id"] = record.id
                st.session_state[_STEP1_FEEDBACK_KEY] = {
                    "kind": "created",
                    "slate_id": record.id,
                    "event_name": record.event_name,
                }
                st.rerun()

    # --- Import salaries into the active slate ----------------------------
    with import_col:
        can_import = active_slate_id is not None and csv_valid
        if active_slate_id is None:
            st.caption("Create a slate above before importing salaries.")
        elif not csv_valid:
            st.caption("Upload a validated salary CSV to import salaries.")

        import_label = (
            f"Import salaries into slate #{active_slate_id}"
            if active_slate_id is not None
            else "Import salaries into slate"
        )
        if st.button(
            import_label,
            key="builder_import_salaries_btn",
            disabled=not can_import,
        ):
            target_id = int(active_slate_id)
            try:
                result = import_dk_salary_dataframe(
                    conn, slate_id=target_id, df=salary_df
                )
                # Read the roster back on the same connection so the Game
                # Info readout reflects exactly what was just persisted
                # (design §5; counts only — creates no fight groups).
                roster = (
                    FighterRepository(conn).list_for_slate(target_id)
                    if result.status == IMPORTED
                    else []
                )
            except ValueError as exc:
                st.error(f"Salary import failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Salary import failed: {exc}")
            else:
                payload = _step1_feedback_payload(result, roster, target_id)
                if result.status == IMPORTED:
                    # Stash + rerun so the cards above refresh with the new
                    # counts and the success banner renders together.
                    st.session_state[_STEP1_FEEDBACK_KEY] = payload
                    st.rerun()
                else:
                    # Surface the failure inline; keep the upload state so the
                    # user can correct and retry without re-uploading.
                    _render_step1_feedback(payload)


# ---------------------------------------------------------------------------
# Step 2 — odds status + DraftKings paste / BestFightOdds fetch (design §6).
#
# The builder surfaces the *common path* — read-only odds status for the
# active slate, the recommended DraftKings copied-board paste → save, and an
# optional BestFightOdds preview fetch — and links to the 03 Odds page for the
# CSV / manual entry, the review-by-exception table, and reject-match
# overrides (those are coupled, multi-step session flows whose single write
# path stays on the Odds page — design §6.1 / §6.4).
# ---------------------------------------------------------------------------


def _odds_status(conn, slate_id: int) -> dict:
    """Read-only odds status counts for the active slate (data reads, not
    gate logic — the verdict still comes from ``builder_gate_view``).

    The match buckets mirror the Odds page Zone 3 review summary
    (``effective_status``). ``matched`` counts every projection-eligible row
    (``auto_match`` / ``review_accepted`` / ``force_pair``) — the same predicate
    the gate and projections use — so an inline Assign is reflected here, not
    just the raw matcher's ``auto_match``. ``review_required`` / ``unmatched``
    roll up to ``needs_action``; ``review_rejected`` is terminal (neither).
    """
    odds_rows = OddsRowRepository(conn).list_for_slate(slate_id)
    by_source = {"manual": 0, "csv": 0, "snapshot": 0, "other": 0}
    for r in odds_rows:
        src = r.source or ""
        if src == "manual":
            by_source["manual"] += 1
        elif src == "csv":
            by_source["csv"] += 1
        elif is_snapshot_source(src):
            by_source["snapshot"] += 1
        else:
            by_source["other"] += 1

    match = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    buckets = {
        "total": len(match),
        "auto_match": 0,
        "review_accepted": 0,
        "force_pair": 0,
        "review_required": 0,
        "unmatched": 0,
        "review_rejected": 0,
    }
    matched = 0
    for rec in match:
        if rec.effective_status in buckets:
            buckets[rec.effective_status] += 1
        if is_projection_eligible_effective_status(rec.effective_status):
            matched += 1

    overrides = ManualMatchOverrideRepository(conn).list_active_for_slate(
        slate_id
    )
    return {
        "odds_rows_total": len(odds_rows),
        "by_source": by_source,
        "match": buckets,
        "matched": matched,
        "needs_action": buckets["review_required"] + buckets["unmatched"],
        "override_count": len(overrides),
    }


def _render_feedback_items(items: list[tuple[str, str]]) -> None:
    """Render a list of ``(kind, message)`` feedback items. Pure display."""
    for kind, msg in items:
        if kind == "success":
            st.success(msg)
        elif kind == "info":
            st.info(msg)
        elif kind == "warning":
            st.warning(msg)
        elif kind == "error":
            st.error(msg)


# ---------------------------------------------------------------------------
# BestFightOdds live fetch → preview (Phase 2; design §3 / §1.9 / §1.11 / §1.12).
#
# An explicit, user-triggered preview path: paste a public BestFightOdds event
# URL, click Fetch, and the page GETs that page *once* (via
# ``bestfightodds_fetch.fetch_bestfightodds_preview`` — the sole network I/O in
# the acquisition path) and renders the normalized DraftKings moneylines for
# review. This is **preview only**: no DB write, no match recompute, no
# override change, and the Manual Review gate is untouched (saving is Phase 3).
# Nothing is fetched on page load — the GET lives strictly inside the button
# handler. The preview / error live in session state so they survive a rerun.
# ---------------------------------------------------------------------------


def _render_bfo_preview(preview: dict) -> None:
    """Render a stored BestFightOdds preview payload (rows + provenance).

    Pure display — reads ``st.session_state`` only and writes nothing. The
    table shows fighter, American moneyline, source, and book; the caption
    carries the source URL + fetched-at and restates that this is preview-only,
    unsaved data (design §1.9 — a fetch never touches SQLite)."""
    rows = preview.get("rows", [])
    st.success(
        f"Fetched {len(rows)} DraftKings moneyline(s) from BestFightOdds — "
        "preview only, nothing saved."
    )
    df = pd.DataFrame(
        [
            {
                "Fighter": r["fighter_name"],
                "Moneyline": r["american_moneyline"],
                "Source": r["source"],
                "Book": r["book"],
            }
            for r in rows
        ],
        columns=["Fighter", "Moneyline", "Source", "Book"],
    )
    st.dataframe(
        df, hide_index=True, width="stretch", key="builder_bfo_preview_df"
    )
    st.caption(
        f"Source: {preview.get('source_url', '')} · fetched "
        f"{preview.get('fetched_at', '')} — preview only. Not saved to the "
        "slate, no matches recomputed, the Manual Review gate is unchanged. "
        "Saving acquired odds is a later phase."
    )


def _render_bestfightodds_fetch() -> None:
    """Step 2 — explicit BestFightOdds live fetch → preview (Phase 2).

    A collapsed control: a URL field and a Fetch button. The GET runs only
    inside the button handler — never on page load — and only for a public
    ``bestfightodds.com`` URL (the fetch helper validates the host). A fetch or
    parse failure is surfaced as a user-facing error; on success the normalized
    rows render in a read-only preview table. Nothing here writes to the DB or
    recomputes matches (design §1.9 / §1.11; ``docs/DEVELOPMENT_NOTES.md`` §3 / §11)."""
    with st.expander(
        "Fetch from BestFightOdds (preview only — not saved yet)",
        expanded=False,
    ):
        st.caption(
            "Optional / advanced. Phase 2 preview: on an explicit click the app "
            "fetches the public "
            "BestFightOdds event page once and previews its DraftKings "
            "moneylines. Nothing is fetched on page load and nothing is saved "
            "— previewing does not write to the database, recompute matches, "
            "or change the Manual Review gate. Saving acquired odds is a later "
            "phase; for now paste the DraftKings board above or use 03 Odds."
        )
        url = st.text_input(
            "BestFightOdds event URL",
            key=_BFO_URL_KEY,
            placeholder="https://www.bestfightodds.com/events/...",
            help=(
                "A public BestFightOdds event page (host must be "
                "bestfightodds.com). The DraftKings column is read; other "
                "books are ignored."
            ),
        )

        if st.button(
            "Fetch & preview BestFightOdds odds",
            key=_BFO_FETCH_BTN_KEY,
            disabled=not (url and url.strip()),
        ):
            # Clear any prior preview / error so the button reflects this click
            # only. The GET happens here and nowhere else (no page-load fetch).
            st.session_state.pop(_BFO_PREVIEW_KEY, None)
            st.session_state.pop(_BFO_ERROR_KEY, None)
            try:
                result = bestfightodds_fetch.fetch_bestfightodds_preview(
                    url.strip()
                )
            except BestFightOddsParseError as exc:
                st.session_state[_BFO_ERROR_KEY] = (
                    "Fetched the BestFightOdds page, but could not parse any "
                    f"DraftKings moneylines from it: {exc}"
                )
            except BestFightOddsFetchError as exc:
                st.session_state[_BFO_ERROR_KEY] = (
                    f"Could not fetch BestFightOdds: {exc}"
                )
            except Exception as exc:  # noqa: BLE001 — never crash the page
                st.session_state[_BFO_ERROR_KEY] = (
                    f"Unexpected error fetching BestFightOdds: {exc}"
                )
            else:
                # Store plain dicts (not dataclasses) so the preview is a simple
                # session payload; nothing is persisted to the DB.
                st.session_state[_BFO_PREVIEW_KEY] = {
                    "source_url": result.source_url,
                    "fetched_at": result.fetched_at,
                    "rows": [
                        {
                            "fighter_name": r.fighter_name,
                            "american_moneyline": r.american_moneyline,
                            "source": r.source,
                            "book": r.book,
                        }
                        for r in result.rows
                    ],
                }

        # Render the stored error / preview (this run's click set it above, and
        # it persists across later reruns within the session).
        error = st.session_state.get(_BFO_ERROR_KEY)
        if error:
            st.error(error)
        preview = st.session_state.get(_BFO_PREVIEW_KEY)
        if preview:
            _render_bfo_preview(preview)


# ---------------------------------------------------------------------------
# DraftKings copied-board paste → preview (Phase 4; design §3 Phase 4).
#
# An explicit, user-triggered, offline preview path: paste the text copied from
# the public DraftKings UFC odds board, click Parse, and the page runs the pure
# ``parse_draftkings_paste`` parser on that string and renders the normalized
# DraftKings moneylines for review. This is **preview only**: no DB write, no
# match recompute, no override change, the Manual Review gate is untouched, and
# there is no network I/O at all (the parser reads only the pasted string).
# Nothing is parsed on page load — the parse lives strictly inside the button
# handler. The preview / error live in session state so they survive a rerun.
# ---------------------------------------------------------------------------


def _is_draftkings_url(url: str) -> bool:
    """True for a ``draftkings.com`` (incl. ``sportsbook.draftkings.com``) URL.

    Used only to surface a *non-blocking* hint when the optional provenance URL
    does not look like DraftKings; the URL is never fetched and never blocks the
    parse or the save (design §1.9 / §1.11 — paste is offline)."""
    try:
        host = urlparse(url.strip()).netloc.lower()
    except (ValueError, AttributeError):
        return False
    host = host.split("@")[-1].split(":")[0]
    return host == "draftkings.com" or host.endswith(".draftkings.com")


def _dk_paste_save_feedback_items(
    result: "draftkings_paste_save.DraftKingsPasteSaveResult", slate_id: int
) -> list[tuple[str, str]]:
    """Project a paste-save result into ``(kind, message)`` feedback items,
    mirroring the snapshot save surfacing (saved / existing / per-row failures /
    recompute) plus the source-URL non-persistence note."""
    items: list[tuple[str, str]] = []
    if result.saved_count:
        items.append((
            "success",
            f"Saved {result.saved_count} DraftKings moneyline(s) to slate "
            f"#{slate_id} (source `{result.source_label}`, book DraftKings, "
            f"batch `{result.import_batch_id}`).",
        ))
    if result.existing_count:
        items.append((
            "info",
            f"{result.existing_count} row(s) already existed for this slate "
            "— idempotent, not duplicated.",
        ))
    if result.failure_count:
        items.append((
            "warning",
            f"{result.failure_count} row(s) failed validation and were not "
            "saved.",
        ))
        for label, err_msg in result.failures:
            items.append(("error", f"{label}: {err_msg}"))
    if result.recompute is not None:
        rc = result.recompute
        items.append((
            "success",
            f"Recomputed match results: {rc.total} row(s) — {rc.status_counts}.",
        ))
    elif result.recompute_error:
        items.append((
            "warning",
            "DraftKings odds saved, but match results were not recomputed: "
            f"{result.recompute_error}",
        ))
    if not (result.saved_count or result.existing_count):
        items.append(("info", "Nothing new to save."))
    if result.source_url:
        items.append((
            "info",
            f"Recorded DraftKings source URL for provenance: "
            f"{result.source_url} — not persisted (odds_rows has no URL "
            "column).",
        ))
    return items


def _render_dk_paste_preview(preview: dict) -> None:
    """Render a stored DraftKings paste preview payload (rows + warnings).

    Pure display — reads ``st.session_state`` only and writes nothing. The
    table shows fighter, opponent, American moneyline, source, and book; any
    skipped-block warnings are surfaced above it, and the caption restates that
    this is preview-only, unsaved data (design §1.9 — a paste never touches
    SQLite or the network)."""
    rows = preview.get("rows", [])
    warnings = preview.get("warnings", [])
    st.success(
        f"Parsed {len(rows)} DraftKings moneyline(s) from the pasted board — "
        "preview only, nothing saved."
    )
    if warnings:
        st.warning(
            f"{len(warnings)} fight block(s) were present but skipped as "
            "incomplete (no readable moneylines):\n\n"
            + "\n".join(f"- {w}" for w in warnings)
        )
    df = pd.DataFrame(
        [
            {
                "Fighter": r["fighter_name"],
                "Opponent": r.get("opponent"),
                "Moneyline": r["american_moneyline"],
                "Source": r["source"],
                "Book": r["book"],
            }
            for r in rows
        ],
        columns=["Fighter", "Opponent", "Moneyline", "Source", "Book"],
    )
    st.dataframe(
        df, hide_index=True, width="stretch", key="builder_dk_paste_preview_df"
    )
    source_url = preview.get("source_url")
    provenance = (
        f"Source: {source_url} (recorded for provenance, not fetched and not "
        "persisted). · "
        if source_url
        else ""
    )
    st.caption(
        f"{provenance}Preview only until you click **Save parsed DraftKings "
        "odds to slate** below. Total Rounds (over/under) prices are ignored; "
        "opponent is preserved on save."
    )


def _render_draftkings_paste(
    conn, *, active_slate_id: int | None, slate_has_no_odds: bool = False
) -> None:
    """Step 2 — DraftKings copied-board paste → preview + save (Phases 4 + 3A).

    This is the **recommended / default** odds-acquisition path: the
    DraftKings paste flow leads Step 2, and the expander opens by default when
    the active slate has no odds yet (``slate_has_no_odds``) so a first-run user
    lands on the working path. The BestFightOdds fetch control below is
    optional / advanced.

    A control with an optional DraftKings source-URL field (provenance
    only — never fetched, never persisted), a textarea, and a Parse button. The
    parse runs only inside its button handler — never on page load — and is
    fully offline (the pure parser reads only the pasted string; it opens no
    socket). On success the normalized rows render in a preview table.

    Phase 3A adds the **Save parsed DraftKings odds to slate** action: shown only
    when an active slate exists *and* a preview is present, it writes the
    previewed rows through the existing ``odds_rows`` → recompute path
    (``draftkings_paste_save``) on an explicit click only — never on parse or
    page load (design §2 / §3; ``docs/DEVELOPMENT_NOTES.md`` §3 / §11). Opening the expander by
    default is presentation only — it never parses or writes on load."""
    with st.expander(
        "Paste DraftKings odds board (recommended)",
        expanded=slate_has_no_odds,
    ):
        st.caption(
            "Open the public DraftKings UFC odds board in your own browser, copy "
            "the visible text, and paste it below. DraftKings blocks automated "
            "fetching — the app cannot pull the board from a URL, only your "
            "browser can load it — so copy/paste is the way in. On an explicit "
            "click the app parses the pasted text into normalized DraftKings "
            "moneylines and previews them — nothing is parsed on page load and a "
            "parse alone writes nothing. Total Rounds (over/under) prices are "
            "ignored."
        )
        pasted = st.text_area(
            "Copied DraftKings odds board text",
            key=_DK_PASTE_TEXT_KEY,
            height=170,
            placeholder=(
                "Paste the copied DraftKings UFC odds board text here…"
            ),
            help=(
                "Plain text copied from the public DraftKings UFC odds board. "
                "Each fight is paired on a 'vs' line; only DraftKings "
                "moneylines are read, and the Total Rounds prices are ignored."
            ),
        )
        url = st.text_input(
            "Where did this board come from? (note only — not fetched)",
            key=_DK_PASTE_URL_KEY,
            placeholder=(
                "https://sportsbook.draftkings.com/leagues/mma/ufc?category="
                "fights&subcategory=fight-lines"
            ),
            help=(
                "Optional note recording where you copied the board from. This "
                "is NOT fetched — DraftKings blocks automated requests, so the "
                "app never loads this URL — and it is NOT saved to the database "
                "(odds_rows has no URL column). It is only for your own "
                "traceability."
            ),
        )
        if url and url.strip() and not _is_draftkings_url(url):
            st.caption(
                "Heads up: that URL is not a draftkings.com address. It is only "
                "a note (never fetched), so parsing and saving still work."
            )

        if st.button(
            "Parse & preview DraftKings odds",
            key=_DK_PASTE_BTN_KEY,
            disabled=not (pasted and pasted.strip()),
        ):
            # Clear any prior preview / error so the button reflects this click
            # only. The parse happens here and nowhere else (no page-load parse,
            # no network — the parser reads only the pasted string).
            st.session_state.pop(_DK_PASTE_PREVIEW_KEY, None)
            st.session_state.pop(_DK_PASTE_ERROR_KEY, None)
            try:
                result = parse_draftkings_paste(pasted)
            except DraftKingsPasteParseError as exc:
                st.session_state[_DK_PASTE_ERROR_KEY] = (
                    "Could not parse any DraftKings moneylines from the pasted "
                    f"text: {exc}"
                )
            except Exception as exc:  # noqa: BLE001 — never crash the page
                st.session_state[_DK_PASTE_ERROR_KEY] = (
                    f"Unexpected error parsing the pasted DraftKings text: {exc}"
                )
            else:
                # Store plain dicts (not dataclasses) so the preview is a simple
                # session payload; nothing is persisted to the DB. ``collected_at``
                # is stamped once here so a later Save is idempotent (same
                # captured_at → same odds_row_key), and the optional source URL is
                # carried for provenance only (never fetched, never persisted).
                st.session_state[_DK_PASTE_PREVIEW_KEY] = {
                    "rows": [
                        {
                            "fighter_name": r.fighter_name,
                            "opponent": r.opponent,
                            "american_moneyline": r.american_moneyline,
                            "source": r.source,
                            "book": r.book,
                        }
                        for r in result.rows
                    ],
                    "warnings": list(result.warnings),
                    "source_url": (url.strip() or None) if url else None,
                    "collected_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                }

        # Render the stored error / preview (this run's click set it above, and
        # it persists across later reruns within the session).
        error = st.session_state.get(_DK_PASTE_ERROR_KEY)
        if error:
            st.error(error)
        preview = st.session_state.get(_DK_PASTE_PREVIEW_KEY)
        if preview:
            _render_dk_paste_preview(preview)
            _render_dk_paste_save(
                conn, preview=preview, active_slate_id=active_slate_id
            )


def _render_dk_paste_save(
    conn, *, preview: dict, active_slate_id: int | None
) -> None:
    """The explicit Phase 3A save for a DraftKings paste preview.

    The Save button is shown only when a preview exists (guaranteed by the
    caller) *and* an active slate exists; with no active slate a caption explains
    why. The save fires only on the click — it persists the previewed rows via
    ``draftkings_paste_save.save_draftkings_paste_rows`` and chains the existing
    recompute. The outcome (and any failure) is stashed and the page reruns, so
    the Step 2 odds-status card / gate above refresh to the just-saved counts in
    the same interaction rather than a run behind (mirrors the manual-odds Save;
    ``docs/DEVELOPMENT_NOTES.md`` §11). A failed save reruns too — only to surface the error —
    and writes nothing."""
    rows = preview.get("rows", [])
    if not rows:
        return

    if active_slate_id is None:
        st.caption(
            "Create or select a slate above to enable saving these DraftKings "
            "odds."
        )
        return

    if st.button(
        "Save parsed DraftKings odds to slate",
        key=_DK_PASTE_SAVE_BTN_KEY,
    ):
        captured_at = preview.get("collected_at") or datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds")
        try:
            result = draftkings_paste_save.save_draftkings_paste_rows(
                conn,
                slate_id=int(active_slate_id),
                rows=rows,
                captured_at=captured_at,
                source_url=preview.get("source_url"),
            )
        except Exception as exc:  # noqa: BLE001 — never crash the page
            st.session_state[_DK_PASTE_SAVE_FEEDBACK_KEY] = [
                ("error", f"Could not save DraftKings odds: {exc}")
            ]
        else:
            st.session_state[_DK_PASTE_SAVE_FEEDBACK_KEY] = (
                _dk_paste_save_feedback_items(result, int(active_slate_id))
            )
        st.rerun()

    fb = st.session_state.pop(_DK_PASTE_SAVE_FEEDBACK_KEY, None)
    if fb:
        _render_feedback_items(fb)


def _consensus_save_feedback_items(
    result: "consensus_save.ConsensusSaveResult", slate_id: int
) -> list[tuple[str, str]]:
    """Project a consensus save result into ``(kind, message)`` feedback items.

    Surfaces the saved consensus + provenance counts, the deliberately-unwritten
    low-confidence fights and unpaired fighters (design §9 — no silent drops),
    and the chained recompute outcome (mirrors ``_dk_paste_save_feedback_items``).
    """
    items: list[tuple[str, str]] = []
    blended = result.fights_considered - result.low_confidence_count
    if result.consensus_count:
        items.append((
            "success",
            f"Saved consensus odds for {result.consensus_count} fighter(s) "
            f"across {blended} blended fight(s) to slate #{slate_id} "
            f"(batch `{result.import_batch_id}`).",
        ))
    if result.book_line_count:
        items.append((
            "info",
            f"Recorded {result.book_line_count} per-book line(s) as provenance.",
        ))
    if result.low_confidence_count:
        labels = "; ".join(
            f"{r.fighter_a} vs {r.fighter_b}" for r in result.low_confidence
        )
        items.append((
            "warning",
            f"{result.low_confidence_count} fight(s) had fewer than the minimum "
            "books and were kept as provenance only (no consensus line written): "
            f"{labels}.",
        ))
    if result.unpaired_fighters:
        items.append((
            "warning",
            f"{len(result.unpaired_fighters)} fighter(s) had no matched opponent "
            f"and were excluded from the blend: "
            f"{', '.join(result.unpaired_fighters)}.",
        ))
    if result.consensus_count == 0 and not items:
        items.append((
            "info", "No consensus odds were written (no blended fights)."
        ))
    if result.recompute is not None:
        rc = result.recompute
        items.append((
            "success",
            f"Recomputed match results: {rc.total} row(s) — {rc.status_counts}.",
        ))
    elif result.recompute_error:
        items.append((
            "warning",
            "Consensus odds saved, but match results were not recomputed: "
            f"{result.recompute_error}",
        ))
    if result.source_url:
        items.append((
            "info",
            "Recorded BestFightOdds source URL for provenance: "
            f"{result.source_url} — not persisted (odds_rows has no URL column).",
        ))
    return items


def _compute_consensus_preview_payload(url: str, pasted: str) -> dict:
    """Fetch (if a URL) + parse + blend into a preview payload. Writes nothing.

    The single BestFightOdds GET happens here, only when ``url`` is supplied; the
    paste parse is offline. Returns a plain-dict payload carrying the parser rows
    (for the later Save), the read-only display table, the low-confidence /
    unpaired notices, and the ``collected_at`` stamp threaded into Save so a
    re-save is the idempotent last-write.

    Raises ``BestFightOddsFetchError`` / ``BestFightOddsParseError`` /
    ``MultiBookPasteParseError`` on bad input, or ``ValueError`` when neither
    source yields a book line.
    """
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    bfo_rows: list = []
    if url:
        fetched = bestfightodds_fetch.fetch_bestfightodds_all_books_preview(url)
        bfo_rows = list(fetched.rows)

    paste_rows: list = []
    warnings: list[str] = []
    if pasted.strip():
        result = parse_multi_book_paste(pasted, collected_at=collected_at)
        paste_rows = list(result.rows)
        warnings = list(result.warnings)

    if not bfo_rows and not paste_rows:
        raise ValueError(
            "No book lines found — enter a BestFightOdds URL or paste a "
            "multi-book grid with at least one fighter's odds."
        )

    merged = merge_sources(bestfightodds_rows=bfo_rows, paste_rows=paste_rows)
    assembly = assemble_fights(merged)
    results = compute_slate_consensus(assembly.fights)

    table: list[dict] = []
    low_confidence: list[str] = []
    for res in results:
        if res.low_confidence:
            low_confidence.append(f"{res.fighter_a} vs {res.fighter_b}")
        for fighter, opp, prob, fair in (
            (res.fighter_a, res.fighter_b, res.prob_a, res.fair_american_a),
            (res.fighter_b, res.fighter_a, res.prob_b, res.fair_american_b),
        ):
            table.append({
                "Fighter": fighter,
                "Opponent": opp,
                "# Books": res.book_count,
                "Median no-vig %": (
                    f"{prob * 100:.1f}%" if prob is not None else "—"
                ),
                "Fair line": (f"{fair:+d}" if fair is not None else "—"),
                "Dispersion": (
                    f"{res.dispersion * 100:.1f} pts"
                    if res.dispersion is not None
                    else "—"
                ),
                "Confidence": ("low — not saved" if res.low_confidence else "ok"),
            })

    return {
        "bfo_rows": bfo_rows,
        "paste_rows": paste_rows,
        "table": table,
        "low_confidence": low_confidence,
        "unpaired": list(assembly.unpaired),
        "warnings": warnings,
        "source_url": (url or None),
        "collected_at": collected_at,
    }


def _render_consensus_preview(preview: dict) -> None:
    """Read-only consensus preview: table + low-confidence / skip notices.

    Pure display — reads the stashed preview payload, writes nothing.
    """
    table = preview.get("table", [])
    if table:
        st.success(
            f"Blended consensus for {len(table)} fighter line(s) across "
            f"{len(table) // 2} fight(s)."
        )
    for warning in preview.get("warnings", []):
        st.warning(warning)
    low_confidence = preview.get("low_confidence", [])
    if low_confidence:
        st.warning(
            "Low-confidence fights (fewer than the minimum books) are kept as "
            "provenance but will NOT be written as consensus odds: "
            + "; ".join(low_confidence)
        )
    unpaired = preview.get("unpaired", [])
    if unpaired:
        st.warning(
            "Fighters with no matched opponent (excluded from the blend): "
            + ", ".join(unpaired)
        )
    if table:
        df = pd.DataFrame(
            table,
            columns=[
                "Fighter",
                "Opponent",
                "# Books",
                "Median no-vig %",
                "Fair line",
                "Dispersion",
                "Confidence",
            ],
        )
        st.dataframe(
            df,
            hide_index=True,
            width="stretch",
            key="builder_consensus_preview_df",
        )
    st.caption(
        "Consensus preview — nothing is saved until you click "
        "**Save consensus to slate**."
    )


def _render_consensus_save(
    conn, *, preview: dict, active_slate_id: int | None
) -> None:
    """Explicit **Save consensus to slate** — the only write in this path.

    Calls ``consensus_save.save_consensus_to_slate`` (which owns its own
    transactions and chains the recompute), stashes one-shot feedback, and reruns
    so the Step 2 odds-status line reflects the saved consensus in the same
    interaction (docs/DEVELOPMENT_NOTES.md §11). Threads the preview-time ``collected_at`` as
    ``captured_at`` so a re-save with the same preview is the idempotent
    last-write.
    """
    bfo_rows = preview.get("bfo_rows") or []
    paste_rows = preview.get("paste_rows") or []
    if not bfo_rows and not paste_rows:
        return
    if active_slate_id is None:
        st.caption("Select an active slate in Step 1 to save consensus odds.")
        return

    if st.button("Save consensus to slate", key=_CONSENSUS_SAVE_BTN_KEY):
        captured_at = preview.get("collected_at") or datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds")
        try:
            result = consensus_save.save_consensus_to_slate(
                conn,
                slate_id=int(active_slate_id),
                captured_at=captured_at,
                bestfightodds_rows=(bfo_rows or None),
                paste_rows=(paste_rows or None),
                source_url=preview.get("source_url"),
            )
        except Exception as exc:  # noqa: BLE001 — never crash the page
            st.session_state[_CONSENSUS_SAVE_FEEDBACK_KEY] = [
                ("error", f"Could not save consensus odds: {exc}")
            ]
        else:
            st.session_state[_CONSENSUS_SAVE_FEEDBACK_KEY] = (
                _consensus_save_feedback_items(result, int(active_slate_id))
            )
        st.rerun()

    fb = st.session_state.pop(_CONSENSUS_SAVE_FEEDBACK_KEY, None)
    if fb:
        _render_feedback_items(fb)


def _render_consensus_blend(conn, *, active_slate_id: int | None) -> None:
    """Multi-book consensus: blend many books into one win-prob per fighter.

    The consensus acquisition path of ODDS_CONSENSUS_DESIGN §5.5 / §8: enter a
    BestFightOdds event URL and/or paste a multi-book odds grid, click **Preview
    consensus** to fetch (if a URL) + parse + blend (median of the de-vigged
    books) into a read-only per-fighter table, then explicitly **Save consensus
    to slate**. The single BestFightOdds GET fires only inside the Preview click;
    nothing is fetched or written on page load. The save writes one
    ``source="consensus"`` odds row per fighter through the existing matcher →
    recompute, plus per-book provenance.
    """
    with st.expander("Blend multiple books → consensus odds", expanded=False):
        st.caption(
            "Sharper than one book: paste a multi-book odds grid and/or enter a "
            "BestFightOdds event URL, click Preview consensus to see the blended "
            "win probabilities (median of the de-vigged books), then Save. "
            "Nothing is fetched or saved until you click."
        )
        url = st.text_input(
            "BestFightOdds event URL (optional)",
            key=_CONSENSUS_URL_KEY,
            placeholder="https://www.bestfightodds.com/events/...",
        )
        st.caption(
            "Note: BestFightOdds static HTML may exclude DraftKings and BetMGM "
            "because those books load client-side. Use the DraftKings paste "
            "option if you want DK included in the consensus blend."
        )
        pasted = st.text_area(
            "Multi-book odds grid (optional) — paste a book-by-fighter table",
            key=_CONSENSUS_PASTE_KEY,
            height=160,
        )
        has_input = bool((url and url.strip()) or (pasted and pasted.strip()))
        if st.button(
            "Preview consensus",
            key=_CONSENSUS_PREVIEW_BTN_KEY,
            disabled=not has_input,
        ):
            st.session_state.pop(_CONSENSUS_PREVIEW_KEY, None)
            st.session_state.pop(_CONSENSUS_ERROR_KEY, None)
            try:
                payload = _compute_consensus_preview_payload(
                    (url.strip() if url else ""), (pasted or "")
                )
            except BestFightOddsFetchError as exc:
                st.session_state[_CONSENSUS_ERROR_KEY] = (
                    f"Could not fetch BestFightOdds: {exc}"
                )
            except BestFightOddsParseError as exc:
                st.session_state[_CONSENSUS_ERROR_KEY] = (
                    "Fetched the BestFightOdds page but could not parse its "
                    f"book columns: {exc}"
                )
            except MultiBookPasteParseError as exc:
                st.session_state[_CONSENSUS_ERROR_KEY] = (
                    f"Could not parse the pasted multi-book grid: {exc}"
                )
            except ValueError as exc:
                st.session_state[_CONSENSUS_ERROR_KEY] = str(exc)
            except Exception as exc:  # noqa: BLE001 — never crash the page
                st.session_state[_CONSENSUS_ERROR_KEY] = (
                    f"Unexpected error building the consensus preview: {exc}"
                )
            else:
                st.session_state[_CONSENSUS_PREVIEW_KEY] = payload

        error = st.session_state.get(_CONSENSUS_ERROR_KEY)
        if error:
            st.error(error)
        preview = st.session_state.get(_CONSENSUS_PREVIEW_KEY)
        if preview:
            _render_consensus_preview(preview)
            _render_consensus_save(
                conn, preview=preview, active_slate_id=active_slate_id
            )


def _render_manual_odds_entry(conn, *, active_slate_id: int | None) -> None:
    """Inline single-fighter manual moneyline entry (Step 2).

    Closes the one coverage gap the name-match fixer cannot — a fighter with
    *no* odds row at all (e.g. the DraftKings board omitted that fight). Saving
    writes one ``source="manual"`` row carrying the fighter's exact DK name and
    chains the existing recompute via ``save_manual_odds_and_recompute``, so the
    matcher auto-matches it and the fighter becomes covered. No parallel store,
    no schema. Button-only: page load only reads."""
    with st.expander("Add a fighter's moneyline by hand", expanded=False):
        st.caption(
            "For a fighter the DraftKings board missed. Pick the DK fighter, "
            "type their American moneyline (e.g. -150 or +180), and Save — the "
            "app records it and matches it to that fighter so they count toward "
            "projections and the gate. Nothing is saved until you click Save."
        )
        if active_slate_id is None:
            st.caption("Create or select a slate above first.")
            return
        try:
            fighters = [
                f
                for f in FighterRepository(conn).list_for_slate(
                    int(active_slate_id)
                )
                if f.status == _ACTIVE_FIGHTER_STATUS
            ]
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not load DK fighters ({exc}).")
            return
        if not fighters:
            st.caption(
                "Import the DK salary CSV in Step 1 before adding odds."
            )
            return

        fb = st.session_state.pop(_MANUAL_ODDS_FEEDBACK_KEY, None)
        if fb:
            _render_feedback_items(fb)

        sentinel = 0
        ids = [sentinel] + [f.id for f in fighters]
        labels = {sentinel: "— pick a fighter —"}
        name_by_id = {f.id: f.name for f in fighters}
        for f in fighters:
            labels[f.id] = f"{f.name} (${f.salary:,})"

        fighter_choice = st.selectbox(
            "DK fighter",
            options=ids,
            format_func=lambda fid: labels[fid],
            key=_MANUAL_ODDS_FIGHTER_KEY,
        )
        moneyline = st.number_input(
            "American moneyline",
            value=0,
            step=5,
            format="%d",
            key=_MANUAL_ODDS_ML_KEY,
            help=(
                "A non-zero American price, e.g. -150 (favorite) or "
                "+180 (underdog)."
            ),
        )

        if st.button("Save moneyline", key=_MANUAL_ODDS_SAVE_BTN_KEY):
            if fighter_choice == sentinel:
                st.warning("Pick the DK fighter this moneyline is for.")
                return
            ml = int(moneyline)
            if ml == 0:
                st.warning(
                    "Enter a non-zero American moneyline (e.g. -150 or +180)."
                )
                return
            who = name_by_id.get(int(fighter_choice), "the fighter")
            try:
                result = save_manual_odds_and_recompute(
                    conn,
                    slate_id=int(active_slate_id),
                    fighter_name=who,
                    american_odds=ml,
                    captured_at=datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — never crash the page
                st.error(f"Could not save the moneyline: {exc}")
                return
            if result.failure_count:
                _, reason = result.failures[0]
                st.error(f"Could not save the moneyline for {who}: {reason}")
                return
            items: list[tuple[str, str]] = []
            if result.saved_count:
                items.append(
                    ("success", f"Saved {who}'s moneyline ({ml:+d}) and matched it.")
                )
            else:
                items.append(
                    ("info", f"{who} already had this exact moneyline saved.")
                )
            if result.recompute_error:
                items.append(
                    (
                        "warning",
                        "Saved, but match recompute was skipped: "
                        f"{result.recompute_error}",
                    )
                )
            st.session_state[_MANUAL_ODDS_FEEDBACK_KEY] = items
            st.rerun()


# ---------------------------------------------------------------------------
# Step 2 — inline unresolved odds name-match fixer (UX repair).
#
# Surfaces the same D.5 Assign/Accept binding the 03 Odds "3f" panel owns, but
# inline on Build so a user never has to leave the one-page flow to resolve a
# sportsbook-vs-DK name mismatch (e.g. the odds say "Bruno Gustavo da Silva"
# while the DK salary lists "Bruno Silva"). It reuses the existing services
# verbatim — ``assignable_match_results`` to pick the rows and
# ``record_assign_match_override`` to bind one — and adds no new match
# semantics, no schema, no projection change (``docs/DEVELOPMENT_NOTES.md`` §11; the advanced
# odds-match workflow, reject / un-reject / reason notes, stays on 03 Odds).
#
# Writes are button-only: page load only *reads* the persisted match results.
# The override is written solely on an explicit per-row **Assign** click, which
# then reruns so the Step 2 status above refreshes and the resolved row drops
# off. A ``review_required`` row preselects the matcher's own proposal (one-click
# confirm); an ``unmatched`` row stays on a sentinel that forces an explicit
# pick — the page never guesses or auto-assigns a fighter.
# ---------------------------------------------------------------------------

# Plain-words rendering of a matcher status for the fixer headline (no internal
# jargon up top; the raw ``effective_status`` / accept-vs-force_pair mechanics
# live only inside the small "Technical details" expander below the rows).
_FIX_STATUS_PLAIN: dict[str, str] = {
    "unmatched": "No DK fighter matched yet",
    "review_required": "Possible match — needs your confirmation",
}

# Sentinel option for the per-row fighter dropdown; real fighter ids are
# positive, so 0 reliably means "no explicit pick yet".
_FIX_FIGHTER_SENTINEL = 0


def _render_one_odds_fix_row(
    conn,
    *,
    rec,
    slate_id: int,
    odds_row,
    fighter_ids: list[int],
    fighter_labels: dict[int, str],
    name_by_id: dict[int, str],
) -> None:
    """Render + wire one assignable odds row (sportsbook name → DK fighter).

    Plain-words row line, a fighter dropdown (name + salary), and an **Assign**
    button. The button is the only write: it calls ``record_assign_match_override``
    and, on success, stashes a one-shot "Assigned X to Y." line and reruns. An
    ``unmatched`` row must be given an explicit fighter pick first; a
    ``review_required`` row defaults to the matcher's proposed fighter so a
    single click confirms it (§16.10). Service ``ValueError``\\s (already-bound /
    inactive fighter, missing row) surface inline with no write.
    """
    sportsbook_raw = (
        odds_row.fighter_name_raw
        if odds_row is not None and odds_row.fighter_name_raw
        else rec.odds_row_key
    )
    parts = [f"<b>{html.escape(sportsbook_raw)}</b>"]
    if odds_row is not None and odds_row.opponent_name_raw:
        parts.append(f"vs {html.escape(odds_row.opponent_name_raw)}")
    if odds_row is not None:
        parts.append(f"@ {odds_row.american_odds:+d}")
    plain = _FIX_STATUS_PLAIN.get(
        rec.effective_status, rec.effective_status.replace("_", " ")
    )
    st.markdown(
        f'<div class="tsb-note">{" ".join(parts)} — {html.escape(plain)}</div>',
        unsafe_allow_html=True,
    )

    # Default selection: a review_required row preselects the matcher's proposal
    # (its bound fighter_id, or the named preferred candidate) for one-click
    # confirm. When there is no matcher proposal (an unmatched row, or a review
    # row with no candidate), fall back to a fuzzy *suggestion* of the closest DK
    # name so the user confirms one pick instead of guessing from a blank
    # dropdown — still a hint, never an auto-bind (the Assign click is the write).
    default_index = 0
    suggested_name: str | None = None
    if rec.match_status == "review_required":
        if rec.fighter_id is not None and rec.fighter_id in name_by_id:
            default_index = fighter_ids.index(rec.fighter_id)
        elif rec.preferred_candidate:
            for i, fid in enumerate(fighter_ids):
                if (
                    fid != _FIX_FIGHTER_SENTINEL
                    and name_by_id.get(fid) == rec.preferred_candidate
                ):
                    default_index = i
                    break

    if default_index == 0:
        suggested_name = suggest_best_fighter(
            sportsbook_raw,
            [name_by_id[fid] for fid in fighter_ids if fid != _FIX_FIGHTER_SENTINEL],
        )
        if suggested_name is not None:
            for i, fid in enumerate(fighter_ids):
                if name_by_id.get(fid) == suggested_name:
                    default_index = i
                    break

    if suggested_name is not None:
        st.caption(
            f"Suggested match: **{suggested_name}** (closest DK name) — "
            "confirm it is right, then Assign."
        )

    fighter_choice = st.selectbox(
        f"DK fighter for {sportsbook_raw}",
        options=fighter_ids,
        format_func=lambda fid: fighter_labels[fid],
        index=default_index,
        key=f"builder_odds_fix_fighter_{slate_id}_{rec.odds_row_id}",
        label_visibility="collapsed",
    )

    if st.button(
        "Assign",
        key=f"builder_odds_fix_assign_{slate_id}_{rec.odds_row_id}",
    ):
        if fighter_choice == _FIX_FIGHTER_SENTINEL:
            st.warning("Pick the DK fighter to assign this odds row to.")
            return
        try:
            record_assign_match_override(
                conn,
                slate_id=slate_id,
                odds_row_key=rec.odds_row_key,
                fighter_id=int(fighter_choice),
                reason="Assigned from Build (Step 2) odds name fixer",
            )
        except ValueError as exc:
            msg = str(exc)
            who = name_by_id.get(int(fighter_choice), "That fighter")
            if "already" in msg:
                st.error(
                    f"{who} is already assigned to another odds row on this "
                    "slate. Resolve that one first on 03 Odds, then retry."
                )
            elif "not active" in msg:
                st.error(
                    f"{who} is no longer active on this slate — re-import the "
                    "DK salary CSV in Step 1, then retry."
                )
            else:
                st.error(f"Assign failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Assign failed: {exc}")
        else:
            st.session_state[_ODDS_FIX_FEEDBACK_KEY] = [
                (
                    "success",
                    f"Assigned {sportsbook_raw} to "
                    f"{name_by_id.get(int(fighter_choice), 'the DK fighter')}.",
                )
            ]
            st.rerun()


def _render_odds_match_fixer(conn, *, slate_id: int) -> None:
    """Inline resolver for odds rows whose sportsbook name did not cleanly match
    a DK fighter — ``effective_status`` ``unmatched`` / ``review_required``.

    Reuses the D.5 ``assignable_match_results`` filter and the
    ``record_assign_match_override`` service the 03 Odds 3f panel owns; adds no
    new match semantics. Reads only on render. Renders nothing when every row is
    resolved (apart from a one-shot success line after the final assign), so a
    clean slate stays uncluttered.
    """
    try:
        match_records = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    except Exception:  # noqa: BLE001 - a status read must never break Build
        return
    assignable = assignable_match_results(match_records)

    # One-shot feedback from a prior Assign click — rendered then cleared. Popped
    # before the empty-check so the "Assigned X to Y." line still shows when that
    # assign resolved the last unresolved row (assignable now empty).
    fb = st.session_state.pop(_ODDS_FIX_FEEDBACK_KEY, None)

    if not assignable:
        if fb:
            _render_feedback_items(fb)
            st.markdown(
                '<div class="tsb-note">All sportsbook names are matched to DK '
                "fighters — nothing left to fix.</div>",
                unsafe_allow_html=True,
            )
        return

    st.markdown("**Fix odds name matches**")
    st.caption(
        "Some sportsbook names do not exactly match the DK salary names. "
        "Assign them below so projections and lineups can use those fighters."
    )
    if fb:
        _render_feedback_items(fb)

    try:
        fighters = [
            f
            for f in FighterRepository(conn).list_for_slate(slate_id)
            if f.status == _ACTIVE_FIGHTER_STATUS
        ]
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not load DK fighters to assign ({exc}).")
        return
    if not fighters:
        st.caption(
            "Import the DK salary CSV in Step 1 before assigning odds rows."
        )
        return

    odds_by_key = {
        r.odds_row_key: r
        for r in OddsRowRepository(conn).list_for_slate(slate_id)
    }
    name_by_id = {f.id: f.name for f in fighters}
    fighter_ids = [_FIX_FIGHTER_SENTINEL] + [f.id for f in fighters]
    fighter_labels = {_FIX_FIGHTER_SENTINEL: "— pick the DK fighter —"}
    for f in fighters:
        fighter_labels[f.id] = f"{f.name} (${f.salary:,})"

    for rec in assignable:
        _render_one_odds_fix_row(
            conn,
            rec=rec,
            slate_id=slate_id,
            odds_row=odds_by_key.get(rec.odds_row_key),
            fighter_ids=fighter_ids,
            fighter_labels=fighter_labels,
            name_by_id=name_by_id,
        )

    with st.expander("Technical details", expanded=False):
        st.caption(
            "Assigning records an `accept_match` (confirming the matcher's own "
            "proposal) or `force_pair` (a fighter the matcher missed) override "
            "and flips that row's `effective_status` + `fighter_id` in one "
            "transaction. The full advanced odds-match workflow — reject, "
            "un-reject, reason notes — lives on the **03 Odds** page."
        )


def _render_step2(conn, *, has_slates: bool, active_slate_id: int | None) -> None:
    """Step 2 — odds status + DraftKings paste save (design §6). Status is
    read-only; the DraftKings paste save is the Step 2 write and is
    button-gated. CSV / manual entry, the review-by-exception table, and
    reject-match overrides stay on the 03 Odds page (design §6.1 / §6.4)."""
    if not has_slates or active_slate_id is None:
        st.caption(
            "Create a slate in Step 1 first, then import current moneylines "
            "here (paste the DraftKings board) or on **03 Odds**."
        )
        return

    # Compact, single-line odds status (the same read-only counts as before —
    # no status "maze"; the card headline above already carries the verdict).
    status = _odds_status(conn, active_slate_id)
    slate_has_no_odds = status["odds_rows_total"] == 0
    st.markdown(
        f"**Odds loaded:** {status['odds_rows_total']} odds rows · "
        f"{status['matched']} matched · {status['needs_action']} need review"
    )

    # Inline unresolved odds name-match fixer — directly under the status line so
    # the "N need review" count is immediately actionable without leaving Build.
    # Renders only when persisted match results sit in unmatched /
    # review_required; button-only writes (reuses D.5 services verbatim).
    _render_odds_match_fixer(conn, slate_id=active_slate_id)

    # Short guidance line near the odds status: lead first-run users to the
    # working path (DraftKings paste) and frame the other two as optional. Copy
    # / direction only — it derives nothing and writes nothing.
    st.markdown(
        '<div class="tsb-note">Easiest way to add odds: paste the DraftKings '
        "board below. BestFightOdds fetch is optional.</div>",
        unsafe_allow_html=True,
    )

    # Recommended / default path first: the explicit DraftKings copied-board
    # paste → preview + save (Phases 4 + 3A). The parse is offline (the pure
    # parser reads only the pasted string); Phase 3A adds an explicit Save into
    # the active slate through the existing odds_rows → recompute path. The
    # expander opens by default when the slate has no odds so a first-run user
    # lands on the working path (presentation only — no parse / write on load).
    _render_draftkings_paste(
        conn,
        active_slate_id=active_slate_id,
        slate_has_no_odds=slate_has_no_odds,
    )

    # Multi-book consensus blend (ODDS_CONSENSUS_DESIGN §5.5 / §8): paste a
    # multi-book grid and/or enter a BestFightOdds URL → preview the blended
    # win probabilities → Save one source="consensus" row per fighter through
    # the existing matcher → recompute. Preview-only until Save; the single BFO
    # GET fires only inside the Preview click. Placed beside the DK paste.
    _render_consensus_blend(conn, active_slate_id=active_slate_id)

    # Inline single-fighter manual moneyline entry — closes the coverage gap the
    # name-match fixer cannot (a fighter with no odds row at all). Save persists
    # one manual row + recomputes so the fighter is matched and counts. Placed
    # under the recommended paste; button-only writes.
    _render_manual_odds_entry(conn, active_slate_id=active_slate_id)

    # Optional / advanced: explicit BestFightOdds live fetch → preview (Phase
    # 2). Preview-only and slate-independent (it never saves), placed last among
    # the acquisition controls (design §3; no tabs, no dashboard).
    _render_bestfightodds_fetch()

    st.markdown(
        '<div class="tsb-shortnote">Bulk odds CSV &amp; advanced match '
        "overrides live on <b>03 Odds</b>. News flags &amp; line movement are "
        "preview-only and never saved.</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Build section — gated Mark-reviewed write + gated optimizer run (design §7).
#
# Two layers of gating, both read off ``builder_gate_view`` (no rule
# re-derived — design §4 / §7.2):
#   - Mark slate reviewed: offered only when ``view.ready_to_mark`` is True
#     (every structural Blocking check passes); the lone write is
#     ``SlateRepository.set_manual_review_reviewed`` — the same call
#     ``06_manual_review.py`` makes — and it is never automatic (§7.4).
#   - Build lineups: enabled only when ``view.ready_to_build`` (==
#     ``summary.ready``). The click handler re-evaluates the gate fresh and
#     aborts before the solver if the slate is no longer ready, then calls the
#     read-only ``run_optimizer`` wrapped in ``ManualReviewGateError`` as
#     defense in depth (§7.5 / §7.6). The builder never touches the pool
#     builder or solver directly.
# ---------------------------------------------------------------------------


def _build_lineup_reasoning(
    conn, *, slate_id: int, n_lineups: int
) -> tuple[dict[int, list[ReasoningItem]], list[ReasoningItem], bool]:
    """Assemble deterministic per-lineup reasoning for a Build run (B6).

    Read-only end to end (design §7.6 / §7.7): re-reads the same slate via
    the gated ``build_run_log`` bundle and the read-only
    ``assemble_reasoning_context``, then runs the pure
    ``build_lineup_reasoning`` generator. Returns the reasoning items grouped
    by ``lineup_index`` plus the context-level items (excluded fighters /
    active gate flags), and a flag that is False only when assembly failed —
    so the caller can degrade to lineups-without-reasoning rather than crash
    the already-rendered tables. No writes, no projection recompute."""
    try:
        bundle = build_run_log(conn, slate_id=slate_id, n_lineups=n_lineups)
        context = assemble_reasoning_context(
            conn, slate_id=slate_id, bundle=bundle
        )
        reasoning = build_lineup_reasoning(context)
    except Exception:  # noqa: BLE001 — reasoning is best-effort; never crash Build output
        return {}, [], False

    by_lineup: dict[int, list[ReasoningItem]] = {}
    context_items: list[ReasoningItem] = []
    for item in reasoning.items:
        if item.lineup_index is None:
            context_items.append(item)
        else:
            by_lineup.setdefault(int(item.lineup_index), []).append(item)
    return by_lineup, context_items, True


def _render_solve_result(
    conn, result: SolveResult, *, slate_id: int, n_lineups: int
) -> None:
    """Render a :class:`SolveResult` read-only (design §7.6 / §7.7).

    Mirrors the 07 Optimizer page's rendering: a status line, an outcome
    banner, and one fighter-name/salary table per lineup, each followed by a
    compact "Why this lineup?" expander carrying the B6 deterministic
    reasoning (design §8 / §11.1 B6). Nothing is persisted — lineups live in
    memory only, no DK upload CSV, no contest entry (design §7.7;
    ``docs/DEVELOPMENT_NOTES.md`` §11)."""
    fighters = FighterRepository(conn).list_for_slate(slate_id)
    name_by_id = {int(f.id): f.name for f in fighters}
    salary_by_id = {int(f.id): int(f.salary) for f in fighters}

    st.markdown(f"**Solver status:** `{result.status}`")
    if result.status == STATUS_OK:
        st.success(
            f"Generated {len(result.lineups)} research lineup(s) — "
            "6 fighters each."
        )
    elif result.status == STATUS_OK_PARTIAL:
        st.warning(
            f"Partial solve: only {len(result.lineups)} feasible lineup(s). "
            f"{result.reason}"
        )
    elif result.status == STATUS_INFEASIBLE_POOL_TOO_SMALL:
        st.error(
            "Cannot build lineups — the optimizer pool is too small. "
            f"{result.reason}"
        )
        return
    elif result.status == STATUS_INFEASIBLE_CONSTRAINTS:
        st.error(
            "Cannot build lineups — no feasible lineup under the current "
            f"constraints. {result.reason}"
        )
        return
    else:
        st.error(
            f"Unexpected solver status `{result.status}`: {result.reason}"
        )
        return

    reasoning_by_lineup, reasoning_context_items, reasoning_ok = (
        _build_lineup_reasoning(conn, slate_id=slate_id, n_lineups=n_lineups)
    )

    total_lineups = len(result.lineups)
    for idx, lineup in enumerate(result.lineups, start=1):
        st.subheader(f"Lineup {idx} of {total_lineups}")
        rows = [
            {
                "Fighter": name_by_id.get(int(fid), f"#{int(fid)}"),
                "Salary": salary_by_id.get(int(fid), 0),
            }
            for fid in lineup.fighter_ids
        ]
        st.dataframe(
            pd.DataFrame(rows, columns=["Fighter", "Salary"]),
            hide_index=True,
            width="stretch",
            key=f"builder_lineup_df_{idx}",
        )
        st.caption(
            f"Total salary: ${lineup.total_salary:,} · "
            f"Total projection: {lineup.total_projection:.2f} DK pts"
        )
        lineup_items = reasoning_by_lineup.get(idx)
        if lineup_items:
            with st.expander(
                f"Why this lineup? (Lineup {idx})", expanded=False
            ):
                for item in lineup_items:
                    st.markdown(f"- {item.text}")

    st.caption(
        "These are research lineups, not guaranteed winning lineups."
    )

    # Context-level reasoning (excluded fighters + active gate flags) is shown
    # once, below the lineups (design §8.2). It carries the gate's own wording
    # and never asserts a fight outcome (§8.3).
    if reasoning_context_items:
        with st.expander(
            "Before you enter — review flags & excluded fighters",
            expanded=False,
        ):
            for item in reasoning_context_items:
                st.markdown(f"- {item.text}")
    elif not reasoning_ok:
        st.caption(
            "Per-lineup reasoning could not be assembled for this run."
        )


def _build_status_html(view: hd.BuilderGateView) -> str:
    """The prototype's ``#buildStatus`` line, keyed off the gate verdict
    (presentation only — the verdict still comes from ``builder_gate_view``).
    Mirrors ``two_step_builder.html`` ``refresh()``."""
    if view.verdict == hd.GATE_BLOCKED:
        msg = "<b>Blocked.</b> Resolve the issues in the gate before review."
    elif view.verdict == hd.GATE_WARNING:
        msg = (
            "Inputs loaded. <b>Review the slate</b> and mark it reviewed to "
            "unlock Build."
        )
    elif view.verdict == hd.GATE_READY:
        msg = "<b>Gate cleared.</b> Slate reviewed — build research lineups."
    else:  # GATE_NOT_STARTED
        msg = "Load both inputs to build."
    return f'<div class="tsb-buildstatus">{msg}</div>'


def _render_setup_jump_buttons(view: hd.BuilderGateView) -> None:
    """Render the gate's direct **Fix fight groups** jump (Build-gate
    actionability).

    Shows **Fix fight groups** (→ 02 Fight Groups) when fight-group coverage is
    blocking — read straight off the gate chip (no rule re-derived; design §4).
    Odds are resolved inline on Build (the Step 2 DraftKings paste + the
    name-match fixer), so there is no jump to the 03 Odds page. The button only
    ``st.switch_page``-s on an explicit click (never on load), so this renders
    no write and performs no navigation at page-load time."""
    if not _fight_groups_action_needed(view):
        return

    if st.button(
        "Fix fight groups",
        key=_FIX_FIGHT_GROUPS_BTN_KEY,
        help=(
            "Open 02 Fight Groups to pair active fighters and confirm "
            "scheduled rounds, then return to Build."
        ),
    ):
        st.switch_page(hd.PAGE_FIGHT_GROUPS.path)


def _render_fight_groups_nav() -> None:
    """Standing link to 02 Fight Groups, available from Build at any gate state.

    Distinct from the gate's blocking **Fix fight groups** jump
    (``_render_setup_jump_buttons``), which appears only while fight-group
    coverage blocks: this renders unconditionally so Fight Card Review stays one
    click away even on a fully-green slate — the case where the collapsed sidebar
    is otherwise the only route there. Button-only: ``st.switch_page`` fires on an
    explicit click, so this writes nothing and navigates nowhere on load."""
    if st.button(
        "Review fight card (02 Fight Groups)",
        key=_FIGHT_GROUPS_NAV_BTN_KEY,
        help=(
            "Open 02 Fight Groups to review pairings, confirm fights, and set "
            "the 5-round main event / title bout, then return to Build."
        ),
    ):
        st.switch_page(hd.PAGE_FIGHT_GROUPS.path)


def _render_build_section(
    conn, *, view: hd.BuilderGateView, active_slate_id: int | None
) -> None:
    """Render the full-width Build panel (design §7).

    Prototype order (``two_step_builder.html`` ``.buildbar``): a one-line
    status, the folded Manual Review gate panel, the gated Mark-reviewed
    control (shown only when ``view.ready_to_mark``), then the lineup-count
    input and the Build button (enabled only when ``view.ready_to_build``).
    All enablement reads ``builder_gate_view`` directly — no gate rule is
    re-derived here."""
    # Titled card header so the Build panel reads as the third stacked action
    # alongside the Step 1 / Step 2 input cards (parity with their
    # ``_render_*_card`` headers). Presentation only — no "Step 3" renumbering
    # (the design doc's "two-step builder" names only the two input steps); the
    # gate verdict / Build enablement below are unchanged.
    st.markdown(
        '<div class="tsb-card">'
        "<h2>Build research lineups</h2>"
        '<div class="tsb-desc">Generate up to 5 research lineups once the '
        "Manual Review gate is green.</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    # One-shot Mark-reviewed feedback from a prior click (set just before the
    # rerun that refreshed the gate above). Rendered, then cleared.
    fb = st.session_state.pop(_MARK_REVIEWED_FEEDBACK_KEY, None)
    if fb:
        _render_feedback_items(fb)

    # Status line, then the folded gate panel (verdict + chips) — the prototype
    # keeps the Manual Review gate as a visible safety checkpoint inside Build.
    st.markdown(_build_status_html(view), unsafe_allow_html=True)
    _render_gate_panel(view)

    # One concise "next required fix" — the gate's own single recommendation
    # (``recommend_next_action`` via ``view.next_action``), surfaced verbatim so
    # the user sees exactly the one thing blocking Build without scanning the
    # chip checklist. Shown whenever the slate is not yet ready; on a ready
    # slate the status line above already says "build lineups". No rule is
    # re-derived here (design §4 / §5).
    if view.verdict != hd.GATE_READY:
        na = view.next_action
        st.markdown(
            '<div class="tsb-buildstatus">Next required fix: '
            f"<b>{html.escape(na.why)}</b> "
            f"(see {html.escape(na.page.label)})</div>",
            unsafe_allow_html=True,
        )
        # Direct jump to 02 Fight Groups so the gate is actionable without the
        # (hidden) sidebar. Odds are resolved inline on Build (Step 2 paste +
        # name-match fixer), so the 03 Odds jump was removed.
        # ``st.switch_page`` fires only on an explicit click — never on load — so
        # this writes nothing and navigates nowhere at render time (docs/DEVELOPMENT_NOTES.md §11).
        # A native ``st.page_link`` isn't queryable in AppTest (Streamlit 1.57),
        # so a button + ``switch_page`` is used for a testable affordance.
        _render_setup_jump_buttons(view)

    # --- Mark slate reviewed (design §7.2 / §7.3 / §7.4) ------------------
    # Offered only when the slate is structurally clean (every Blocking check
    # except the reviewer ack passes). Never shown on a blocked slate, never
    # fired on page load, never automatic. Reuses the Manual Review page's
    # single write path verbatim.
    if view.ready_to_mark and active_slate_id is not None:
        st.caption(
            "Marking reviewed does NOT re-validate when the data changes "
            "later — re-review after any salary re-import, odds save, "
            "recompute, override, or fight-group edit."
        )
        # Session-only late-news / weigh-in acknowledgement, carried over from
        # the Manual Review page unchanged (design §7.3; not persisted in v1,
        # never feeds readiness).
        st.checkbox(
            "I have completed the off-app late-news / weigh-in checklist for "
            "this slate (session-only acknowledgement).",
            value=False,
            key="manual_review_late_news_ack",
        )
        # Session-only scheduled-rounds acknowledgement — dismisses the §5.3
        # Warning for a fully-confirmed card (read at the top of the next run).
        st.checkbox(
            "I have reviewed the scheduled rounds (3 vs 5) for this card — "
            "dismiss the rounds reminder.",
            value=False,
            key=_SCHEDULED_ROUNDS_ACK_KEY,
        )
        if st.button("Mark slate reviewed", key="builder_mark_reviewed_btn"):
            try:
                SlateRepository(conn).set_manual_review_reviewed(
                    int(active_slate_id)
                )
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not mark slate reviewed: {exc}")
            else:
                st.session_state[_MARK_REVIEWED_FEEDBACK_KEY] = [
                    (
                        "success",
                        f"Marked slate #{int(active_slate_id)} manually "
                        "reviewed — Build is now unlocked.",
                    )
                ]
                st.rerun()

    # --- Build lineups (design §7.1 / §7.5 / §7.6) -----------------------
    # Lineup-count input (left) + the primary Build button (right), mirroring
    # the prototype's build-bar row. ``n_lineups`` is instantiated before the
    # button so the click handler below can read it this run.
    bc_left, bc_right = st.columns([1, 1])
    with bc_left:
        n_lineups = st.number_input(
            "Research lineups (1–5)",
            min_value=1,
            max_value=5,
            value=1,
            step=1,
            key="builder_n_lineups",
        )
    with bc_right:
        # Spacer so the button baseline aligns with the input field (below its
        # label), matching the prototype's single status/button row.
        st.markdown("<div style='height:1.9rem'></div>", unsafe_allow_html=True)
        build_clicked = st.button(
            "Build research lineups",
            key="build_btn",
            type="primary",
            disabled=not view.ready_to_build,
            help=(
                "Enabled only when the Manual Review gate is green. Mark the "
                "slate reviewed above to unlock it; Build generates research "
                "lineups."
            ),
        )
    st.markdown(
        '<div class="tsb-shortnote">Each lineup is a full 6-fighter DK Classic '
        "roster. Build runs the gated optimizer — it does NOT enter DraftKings "
        "contests, export a DK CSV, or persist lineups.</div>",
        unsafe_allow_html=True,
    )
    if build_clicked:
        if active_slate_id is None:
            st.error("No active slate to build.")
            return
        # Defense in depth (design §7.5): re-evaluate the gate fresh inside the
        # handler and abort before the solver if the slate is no longer ready,
        # so a stale enabled button can never build an un-reviewed slate.
        fresh = evaluate_manual_review(conn, int(active_slate_id))
        if not fresh.summary.ready:
            st.error(
                "Slate is no longer ready to build — the Manual Review gate "
                "is not green. Re-check Manual Review and try again."
            )
            return
        try:
            result = run_optimizer(
                conn,
                slate_id=int(active_slate_id),
                n_lineups=int(n_lineups),
            )
        except ManualReviewGateError as exc:
            st.error(
                "Cannot build — Manual Review gate is not green for slate "
                f"#{exc.slate_id} "
                f"({exc.readiness.summary.blocking_count} Blocking check(s) "
                "failing). Resolve them on Manual Review and try again."
            )
            return
        _render_solve_result(
            conn,
            result,
            slate_id=int(active_slate_id),
            n_lineups=int(n_lineups),
        )


# ---------------------------------------------------------------------------
# Local slate cleanup ("start fresh").
#
# A compact, collapsed section that lets a single local user clear stale / test
# slates so the builder is usable again. Every destructive action is explicit
# and routes through the repository layer in a single transaction
# (``SlateRepository.delete`` / ``delete_all``); nothing fires on page load
# (docs/DEVELOPMENT_NOTES.md §11), the SQLite file is never removed from the running app, and
# code / ``.env`` / uploads / exports are never touched. There is no Undo — so
# the selected-slate delete is gated behind an explicit confirm checkbox and
# the full reset behind typing ``RESET``. After a successful delete the handler
# stashes a one-shot reset flag + feedback and reruns; the top-of-page handler
# then clears the active slate / selector / armed-confirm widget state so the
# active slate re-resolves safely (see ``_POST_DELETE_RESET_KEY``).
# ---------------------------------------------------------------------------


def _render_cleanup_section(conn, *, slates, active_slate_id: int | None) -> None:
    """Render the collapsed local-slate cleanup controls (delete selected /
    reset all). Read-only on load; the only writes are the two explicit,
    confirmation-gated button handlers, each a single repository transaction."""
    with st.expander("🧹 Local slate cleanup — start fresh", expanded=False):
        if not slates:
            st.caption(
                "No local slates to clean up yet — create one in Step 1 above."
            )
            return

        active = next(
            (s for s in slates if s.id == active_slate_id), None
        )
        st.caption(
            "Permanently delete a stale or test slate and everything tied to "
            "it (fighters, fight groups, odds, matches, overrides, lineups). "
            "This only removes rows from the local database — no files, "
            "uploads, or exports are touched, and there is no Undo."
        )

        # --- Delete the selected (active) slate ------------------------------
        if active is not None:
            st.markdown(f"**Active slate:** {_slate_label(active)}")
            confirm = st.checkbox(
                f"Yes, permanently delete slate #{active.id} and all of its "
                "data.",
                value=False,
                key=_DELETE_CONFIRM_KEY,
            )
            if st.button(
                f"Delete slate #{active.id}",
                key="builder_delete_slate_btn",
                disabled=not confirm,
            ):
                try:
                    SlateRepository(conn).delete(int(active.id))
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not delete slate #{active.id}: {exc}")
                else:
                    st.session_state[_CLEANUP_FEEDBACK_KEY] = (
                        "success",
                        f"Deleted slate #{active.id} ({active.event_name}) and "
                        "all of its data. Active slate reset.",
                    )
                    st.session_state[_POST_DELETE_RESET_KEY] = True
                    st.rerun()

        st.divider()

        # --- Reset ALL local slates (type RESET) -----------------------------
        st.markdown("**Reset all local slates**")
        st.caption(
            f"Delete every local slate ({len(slates)} total) and all dependent "
            "rows — meant for clearing old test data. Type RESET to confirm. "
            "This cannot be undone."
        )
        reset_text = st.text_input(
            "Type RESET to enable the full reset",
            key=_RESET_ALL_TEXT_KEY,
            placeholder="RESET",
        )
        if st.button(
            "Reset all local slates",
            key="builder_reset_all_btn",
            disabled=(reset_text or "").strip() != "RESET",
        ):
            try:
                removed = SlateRepository(conn).delete_all()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not reset local slates: {exc}")
            else:
                st.session_state[_CLEANUP_FEEDBACK_KEY] = (
                    "success",
                    f"Reset complete — deleted {removed} slate(s) and all "
                    "dependent rows. Create a slate in Step 1 to start fresh.",
                )
                st.session_state[_POST_DELETE_RESET_KEY] = True
                st.rerun()


# ---------------------------------------------------------------------------
# Page body.
# ---------------------------------------------------------------------------

st.markdown(_CSS, unsafe_allow_html=True)

# Prototype header: DK badge + wordmark, then the one-line pitch (ported
# from ``two_step_builder.html`` .head / .sub). The Streamlit sidebar /
# multipage nav is hidden on this page (see ``_CSS``) to match the
# sidebar-less, two-inputs-then-build prototype canvas.
st.markdown(
    '<div class="tsb-head"><div class="tsb-logo">DK</div>'
    '<h1 class="tsb-h1">Lineup Lab</h1></div>'
    '<div class="tsb-sub">Upload your DraftKings salaries, check the odds, '
    "and build research lineups.</div>",
    unsafe_allow_html=True,
)

# --- Contest-format router (Captain Mode design §2 / §3, slice C1) ----------
# A single selector at the top of the workflow chooses Classic | Captain
# (default Classic). The choice is held in ``st.session_state`` under the
# selector's widget key. Classic falls through to the existing two-step builder
# below, BYTE-FOR-BYTE UNCHANGED. Captain renders the read-only Captain builder
# (``render_captain_section``, slice C5) and ``st.stop()``s before any Classic
# code runs. The Captain section is read-only end to end (no DB connection); this
# is the only Classic-page edit the slice makes and it writes nothing.
contest_format = st.radio(
    "Contest format",
    options=(CONTEST_CLASSIC, CONTEST_CAPTAIN),
    horizontal=True,
    key=_CONTEST_FORMAT_KEY,
    help=(
        "Classic is the working two-step builder. Captain (Showdown) is an "
        "additive second format still in progress."
    ),
)
if contest_format == CONTEST_CAPTAIN:
    render_captain_section()
    st.stop()

# Standing non-claim carried verbatim from Slate Setup (design §5.1;
# docs/DEVELOPMENT_NOTES.md §8 / §10 Slice F), condensed to one non-dominant line: the salary
# importer is not yet validated against a real official DK UFC Classic CSV, so
# all salary-derived state shown here is provisional.
st.warning(
    "Importer is NOT complete — salary results are provisional until validated "
    "against a real DK UFC Classic CSV. Build outputs research lineups only."
)

conn = get_connection()
bootstrap_database(conn)

slates = SlateRepository(conn).list_all()
has_slates = bool(slates)

# One-shot post-delete / reset cleanup (set by a cleanup handler just before
# its ``st.rerun``). Consumed here — at the very top of the run, before any
# widget renders — so it is legal to clear the active-slate selection, the
# selector's own widget state, and the armed confirmation widgets. This lets
# the active slate re-resolve from scratch below with no stale id pointing at a
# just-deleted slate and no pre-armed delete confirm carrying over.
if st.session_state.pop(_POST_DELETE_RESET_KEY, None):
    for _stale_key in (
        "active_slate_id",
        _SLATE_SELECTOR_KEY,
        _DELETE_CONFIRM_KEY,
        _RESET_ALL_TEXT_KEY,
    ):
        st.session_state.pop(_stale_key, None)

# ---------------------------------------------------------------------------
# Active-slate selector. The shared, cross-page ``active_slate_id`` is kept as
# a *plain* session value (the key the Command Center owns; design §5.2) —
# never a widget key — so a Step 1 Create / Import action can update it after
# the selector has already been instantiated this run. The selector itself
# uses a distinct widget key (``_SLATE_SELECTOR_KEY``); its choice is synced
# back into ``active_slate_id`` below. A pending active-slate stashed by Create
# (``_PENDING_ACTIVE_SLATE_KEY``) is applied *before* the widget renders, which
# is the only legal point to seed the selector's own state. Stale ids fall back
# to the first slate. The DB is never touched on load; when the DB is empty
# there is no selector — the Step 1 Create control bootstraps the first slate.
# ---------------------------------------------------------------------------

if has_slates:
    slate_ids = [s.id for s in slates]

    # Apply a Create-stashed pending active slate before the selector widget is
    # instantiated (the only point at which seeding its key is allowed).
    pending = st.session_state.pop(_PENDING_ACTIVE_SLATE_KEY, None)
    if pending in slate_ids:
        st.session_state["active_slate_id"] = pending
        st.session_state[_SLATE_SELECTOR_KEY] = pending

    # Shared active slate (plain value); fall back to the first slate if unset
    # or stale.
    current = st.session_state.get("active_slate_id")
    if current not in slate_ids:
        current = slate_ids[0]

    # Keep the selector's own widget state valid before it renders.
    if st.session_state.get(_SLATE_SELECTOR_KEY) not in slate_ids:
        st.session_state[_SLATE_SELECTOR_KEY] = current

    slate_options = {s.id: _slate_label(s) for s in slates}
    slate_choice = st.selectbox(
        "Active slate",
        options=slate_ids,
        format_func=lambda sid: slate_options[sid],
        key=_SLATE_SELECTOR_KEY,
    )
    active_slate_id: int | None = int(slate_choice)
    # Sync the selection into the shared key — a plain-key write, always legal
    # (``active_slate_id`` is no longer a widget key).
    st.session_state["active_slate_id"] = active_slate_id
    # Make the active slate unambiguous (the selector value, restated).
    st.caption(f"Active slate: {slate_options[active_slate_id]}")
    readiness = evaluate_manual_review(
        conn,
        active_slate_id,
        scheduled_rounds_acknowledged=bool(
            st.session_state.get(_SCHEDULED_ROUNDS_ACK_KEY, False)
        ),
    )
    active_fighters = [
        f
        for f in FighterRepository(conn).list_for_slate(active_slate_id)
        if f.status == _ACTIVE_FIGHTER_STATUS
    ]
    fight_groups = FightGroupRepository(conn).list_for_slate(active_slate_id)
else:
    st.info(
        "No slates yet — upload a DK UFC Classic salary CSV below and click "
        "Create slate to begin."
    )
    active_slate_id = None
    readiness = _empty_readiness()
    active_fighters = []
    fight_groups = []

view = hd.builder_gate_view(readiness, has_slates=has_slates)

# One-shot cleanup feedback from a prior delete / reset (set just before the
# rerun that cleared the active slate). Rendered top-level so it is visible
# after the page re-resolves to a survivor slate (or the empty-DB call-to-action).
_cleanup_fb = st.session_state.pop(_CLEANUP_FEEDBACK_KEY, None)
if _cleanup_fb is not None:
    _render_feedback_items([_cleanup_fb])

# ---------------------------------------------------------------------------
# Prototype layout: the two input cards are stacked full-width (one above the
# other), then the full-width Build panel below them — the latest prototype
# canvas (no 2-up grid, no detail-page list). Each step's status header and
# its controls share one orange-outlined card (a bordered container styled by
# ``stVerticalBlockBorderWrapper`` in ``_CSS``).
# ---------------------------------------------------------------------------

# Step 1 card — DK salary status + upload / Create / Import controls.
with st.container(border=True):
    _render_salary_card(
        readiness,
        active_fighter_count=len(active_fighters),
        fight_group_count=len(fight_groups),
    )
    _render_suggested_pairings(conn, active_fighters, active_slate_id=active_slate_id)
    _render_step1_upload(conn, active_slate_id=active_slate_id)
    _render_fight_groups_nav()

# Step 2 card — odds status + DraftKings paste / BestFightOdds fetch.
with st.container(border=True):
    _render_odds_card(readiness)
    _render_step2(conn, has_slates=has_slates, active_slate_id=active_slate_id)

# Bottom build panel — status line + folded Manual Review gate + gated Build.
with st.container(border=True):
    _render_build_section(conn, view=view, active_slate_id=active_slate_id)

# Local slate cleanup — collapsed "start fresh" controls (explicit, gated,
# repository-layer writes only; nothing fires on load).
_render_cleanup_section(conn, slates=slates, active_slate_id=active_slate_id)
