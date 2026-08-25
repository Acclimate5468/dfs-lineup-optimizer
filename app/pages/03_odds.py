"""Odds — CSV/manual odds inputs, optional persistence, and read-only views.

Page layout (top to bottom):
    Zone 1 — Inputs & Session Preview (browser session only, no writes)
    Zone 2 — Save to Slate (explicit writes to ``odds_rows``)
    Zone 3 — Persisted Slate Data (read-only views + explicit Recompute)
"""

from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.prototype_mode import lock_to_build_page  # noqa: E402

from datetime import datetime, timezone  # noqa: E402

import pandas as pd  # noqa: E402

from src.db.connection import get_connection  # noqa: E402
from src.db.migrations import bootstrap_database  # noqa: E402
from src.collection.odds_news_snapshot import (  # noqa: E402
    SnapshotFormatError,
    validate_snapshot_text,
)
from src.db.repositories import (  # noqa: E402
    FightGroupRecord,
    FightGroupRepository,
    FighterRecord,
    FighterRepository,
    ManualMatchOverrideRecord,
    ManualMatchOverrideRepository,
    OddsMatchResultRepository,
    OddsRowRecord,
    OddsRowRepository,
    SlateRepository,
)
from src.ingestion.manual_odds import save_manual_odds_entries  # noqa: E402
from src.ingestion.name_matching import normalize_name_aggressive  # noqa: E402
from src.ingestion.odds_csv_importer import (  # noqa: E402
    load_odds_csv,
    parse_moneyline,
    validate_odds_csv,
)
from src.ingestion.odds_csv_save import save_csv_odds_rows  # noqa: E402
from src.ingestion.odds_match_filters import (  # noqa: E402
    assignable_match_results,
    format_assignable_label,
    format_rejectable_label,
    rejectable_match_results,
)
from src.ingestion.odds_matching import (  # noqa: E402
    OddsRowInput,
    OpponentContext,
    match_odds_to_dk,
)
from src.ingestion.odds_matching_service import (  # noqa: E402
    EmptyDkRosterError,
    OddsMatchResultRecord,
    record_assign_match_override,
    record_reject_match_override,
    recompute_and_replace_match_results,
)
from src.ingestion.snapshot_odds_save import (  # noqa: E402
    save_snapshot_odds_to_slate,
)
from src.projections.implied_probability import (  # noqa: E402
    american_pair_to_no_vig,
    american_to_implied_probability,
)
from src.utils.text_cleaning import normalize_name  # noqa: E402


def _build_opponent_context(
    dk_fighters: list[str],
    fight_groups: list[FightGroupRecord],
) -> dict[str, OpponentContext]:
    """Map DK fighter names → expected opponent from saved fight groups.

    DK names are mapped to fight-group fighter names using the same
    conservative + aggressive normalization the matcher uses, so accent /
    case / Jr.-style variants still wire up. Keys are the original DK
    strings because ``match_odds_to_dk`` looks up opponents by the exact
    DK string it resolved to.
    """
    if not dk_fighters or not fight_groups:
        return {}

    dk_by_cons: dict[str, str] = {}
    dk_by_agg: dict[str, str] = {}
    for dk in dk_fighters:
        c = normalize_name(dk)
        if c:
            dk_by_cons.setdefault(c, dk)
        a = normalize_name_aggressive(dk)
        if a:
            dk_by_agg.setdefault(a, dk)

    out: dict[str, OpponentContext] = {}
    for g in fight_groups:
        confirmed = g.status == "confirmed"
        for primary, other in (
            (g.fighter_1_name, g.fighter_2_name),
            (g.fighter_2_name, g.fighter_1_name),
        ):
            dk_match: str | None = None
            c = normalize_name(primary)
            if c and c in dk_by_cons:
                dk_match = dk_by_cons[c]
            else:
                a = normalize_name_aggressive(primary)
                if a and a in dk_by_agg:
                    dk_match = dk_by_agg[a]
            if dk_match and dk_match not in out:
                out[dk_match] = OpponentContext(
                    expected_opponent=other, confirmed=confirmed
                )
    return out


def _persisted_match_result_display_rows(
    records: list[OddsMatchResultRecord],
) -> list[dict]:
    """Project ``OddsMatchResultRecord``s into a flat dict per row for the
    read-only persisted-match-results table. Tuples (``candidates``,
    ``notes``) render as comma-joined strings — empty tuples and ``None``
    fields render as empty strings so pandas keeps friendly column dtypes.
    """
    out: list[dict] = []
    for rec in records:
        out.append(
            {
                "odds_row_id": rec.odds_row_id,
                "odds_row_key": rec.odds_row_key,
                "fighter_id": ("" if rec.fighter_id is None else rec.fighter_id),
                "match_status": rec.match_status,
                "effective_status": rec.effective_status,
                "match_stage": rec.match_stage,
                "match_score": rec.match_score,
                "preferred_candidate": rec.preferred_candidate or "",
                "opponent_check": rec.opponent_check,
                "candidates": ", ".join(rec.candidates),
                "notes": ", ".join(rec.notes),
            }
        )
    return out


def _active_override_display_rows(
    records: list[ManualMatchOverrideRecord],
) -> list[dict]:
    """Project active ``ManualMatchOverrideRecord``s into a flat dict per row
    for the read-only Active Overrides panel. ``None`` fields render as
    empty strings to keep pandas column dtypes friendly.
    """
    out: list[dict] = []
    for rec in records:
        out.append(
            {
                "override_type": rec.override_type,
                "odds_row_key": rec.odds_row_key or "",
                "fighter_id": ("" if rec.fighter_id is None else rec.fighter_id),
                "payload_json": rec.payload_json or "",
                "reason": rec.reason or "",
                "created_at": rec.created_at,
            }
        )
    return out


def _persisted_odds_display_rows(records: list[OddsRowRecord]) -> list[dict]:
    """Project ``OddsRowRecord``s into a flat dict per row for the read-only
    persisted-odds table. Implied probability is shown as a percentage so it
    matches the formatting used elsewhere on the page; missing optional fields
    render as empty strings to keep the column dtypes friendly to pandas.
    """
    out: list[dict] = []
    for rec in records:
        if rec.implied_probability is None:
            prob_pct = ""
        else:
            prob_pct = f"{rec.implied_probability * 100:.1f}%"
        out.append(
            {
                "id": rec.id,
                "fighter": rec.fighter_name_raw,
                "opponent": rec.opponent_name_raw or "",
                "american_odds": rec.american_odds,
                "implied_probability": prob_pct,
                "source": rec.source,
                "bookmaker": rec.bookmaker or "",
                "captured_at": rec.captured_at,
                "import_batch_id": rec.import_batch_id or "",
            }
        )
    return out


def _snapshot_entry_display_rows(entries: list) -> list[dict]:
    """Project validated ``SnapshotEntry`` objects into flat dicts for the
    read-only Zone 1e snapshot preview table.

    The implied-probability column shows the **app-derived** value (computed
    from the raw American moneyline), never the snapshot's own advisory
    ``implied_probability`` — making the "raw is canonical, derived is
    advisory" rule from ``docs/ODDS_NEWS_SNAPSHOT_DESIGN.md`` §2/§5 visible.
    Missing optional fields render as empty strings to keep pandas column
    dtypes friendly.
    """
    out: list[dict] = []
    for e in entries:
        if e.derived_implied_probability is None:
            implied_pct = ""
        else:
            implied_pct = f"{e.derived_implied_probability * 100:.1f}%"
        out.append(
            {
                "fighter": e.fighter_name,
                "opponent": e.opponent_name,
                "kind": e.entry_kind,
                "moneyline": ("" if e.moneyline is None else e.moneyline),
                "implied (app-derived)": implied_pct,
                "book/source": e.book or e.source_name or "",
                "movement": e.line_movement or "",
                "news_flags": ", ".join(e.news_flags),
                "status": e.status or "",
                "confidence": (
                    "" if e.confidence is None else f"{e.confidence:.2f}"
                ),
                "freshness": "stale" if e.is_stale else "ok",
            }
        )
    return out


def _summarize_match_review(
    records: list[OddsMatchResultRecord],
) -> dict[str, int]:
    """Count persisted match results by ``effective_status`` for the Zone 3
    review-by-exception banner.

    Buckets (mutually exclusive on ``effective_status``):
      - ``auto_match``     → cleanly matched, no review needed.
      - ``review_required`` / ``unmatched`` → still need attention.
      - ``review_rejected`` → handled (an explicit reject override applied).

    ``needs_action`` rolls up ``review_required`` + ``unmatched`` — the rows
    that keep the slate from being odds-review-complete.
    """
    counts = {
        "total": 0,
        "auto_match": 0,
        "review_required": 0,
        "unmatched": 0,
        "review_rejected": 0,
    }
    for rec in records:
        counts["total"] += 1
        if rec.effective_status in counts:
            counts[rec.effective_status] += 1
    counts["needs_action"] = counts["review_required"] + counts["unmatched"]
    return counts


def _split_match_results_by_review(
    records: list[OddsMatchResultRecord],
) -> tuple[list[OddsMatchResultRecord], list[OddsMatchResultRecord]]:
    """Partition match results for the review-by-exception table.

    Returns ``(needs_review, clean)`` where ``clean`` is every row whose
    ``effective_status`` is ``auto_match`` and ``needs_review`` is everything
    else (``review_required`` / ``unmatched`` / ``review_rejected``). Input
    order is preserved within each list.
    """
    needs_review = [r for r in records if r.effective_status != "auto_match"]
    clean = [r for r in records if r.effective_status == "auto_match"]
    return needs_review, clean


def _slate_label(slate) -> str:
    """Consistent dropdown label for a slate row across all three zones."""
    return (
        f"#{slate.id} — {slate.event_name}"
        + (f" ({slate.event_date})" if slate.event_date else "")
    )


st.set_page_config(page_title="Odds — DK Lineup Lab", layout="wide")
# Advanced setup page: still reachable directly even in prototype mode (nav
# stays hidden; a Back-to-Build control is added). The Build gate no longer
# jumps here — odds are resolved inline on Build (Step 2 paste + name-match
# fixer); this page remains for CSV / manual entry / reject overrides.
lock_to_build_page("Odds", allow_in_prototype=True)
st.title("Odds")

st.info(
    "**Workflow.** Zone 1 — validate odds CSVs and try matching against "
    "pasted DK names in a session-only sandbox (no writes). Zone 2 — save "
    "validated CSV rows and/or manual odds entries to a selected slate's "
    "`odds_rows` table (explicit writes). Zone 3 — view persisted "
    "`odds_rows` and `odds_match_results` for a selected slate, and "
    "explicitly recompute match results via a button. **Recompute is "
    "button-only — never automatic on page load.** Persisted match results "
    "do NOT yet feed projections or the optimizer."
)

st.warning(
    "Odds CSV validation has NOT yet been tested against a real The Odds API "
    "or Google Sheets export. Treat validation results as provisional."
)


# =============================================================================
# Zone 1 — Inputs & Session Preview (browser session only, no DB writes)
# =============================================================================
st.header("1. Inputs & Session Preview")
st.caption(
    "Everything in this zone lives only in your browser session — nothing "
    "here is persisted to the database. Use it to validate an odds CSV, type "
    "manual odds, and preview no-vig probabilities and DK-fighter matching "
    "before persisting anything in Zone 2."
)

# --- 1a. Upload Odds CSV (validate only) -------------------------------------
st.subheader("1a. Upload Odds CSV (validate only)")
st.caption(
    "Validates the CSV structure. Validation is session-only — once it "
    "passes, the validated rows become available to Zone 2's CSV save "
    "action."
)

uploaded = st.file_uploader(
    "Odds CSV",
    type=["csv"],
    accept_multiple_files=False,
    help="CSV with at minimum: fighter, moneyline, source, timestamp.",
    key="odds_csv_uploader",
)

# Removing the upload clears any prior validated state so Zone 2 can't act on
# a stale CSV. New uploads / failed validations also clear it (below).
if uploaded is None:
    st.session_state.pop("odds_csv_validated_df", None)
    st.session_state.pop("odds_csv_validated_row_count", None)
    st.session_state.pop("odds_csv_validated_filename", None)

if uploaded is not None:
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(uploaded.getvalue())
        tmp_path = Path(tmp.name)

    uploaded_df: pd.DataFrame | None = None
    try:
        result = validate_odds_csv(tmp_path)
        if result.is_valid:
            try:
                uploaded_df = load_odds_csv(tmp_path)
            except Exception as exc:  # noqa: BLE001
                # Validation passed but the second read failed — surface
                # the error and disable the save path for this upload.
                st.error(f"Could not re-read CSV for save: {exc}")
                uploaded_df = None
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    if result.is_valid:
        st.success(f"Valid odds CSV — {result.row_count} rows detected.")
        if uploaded_df is not None:
            st.session_state.odds_csv_validated_df = uploaded_df
            st.session_state.odds_csv_validated_row_count = int(len(uploaded_df))
            st.session_state.odds_csv_validated_filename = uploaded.name
        else:
            st.session_state.pop("odds_csv_validated_df", None)
            st.session_state.pop("odds_csv_validated_row_count", None)
            st.session_state.pop("odds_csv_validated_filename", None)
    else:
        st.error(result.error_message or "CSV failed validation.")
        st.session_state.pop("odds_csv_validated_df", None)
        st.session_state.pop("odds_csv_validated_row_count", None)
        st.session_state.pop("odds_csv_validated_filename", None)

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Detected columns**")
        if result.detected_columns:
            st.write(result.detected_columns)
        else:
            st.caption("None detected.")
    with col2:
        st.markdown("**Missing required columns**")
        if result.missing_columns:
            st.write(result.missing_columns)
        else:
            st.caption("None.")

    st.metric("Row count", result.row_count)

    if result.warning_messages:
        st.markdown("**Warnings**")
        for msg in result.warning_messages:
            st.warning(msg)

# --- 1b. Manual Odds Entry ---------------------------------------------------
st.subheader("1b. Manual Odds Entry")
st.caption(
    "Enter a single fighter's moneyline by hand. Raw implied win probability "
    "is computed from the moneyline (no vig removal). Not persisted to the "
    "database and not matched to DK fighters — entries live only in this "
    "browser session. Use Zone 2 below to persist these manual entries to a "
    "slate."
)

if "manual_odds_entries" not in st.session_state:
    st.session_state.manual_odds_entries = []

with st.form("manual_odds_form", clear_on_submit=True):
    fighter = st.text_input("Fighter name")
    moneyline_raw = st.text_input(
        "Moneyline",
        help="American odds, e.g. -150 or +220.",
    )
    opponent = st.text_input(
        "Opponent (optional)",
        help=(
            "Opponent name as it appears on the odds source. Optional — when "
            "provided AND a slate is selected in the Odds Matching Preview "
            "below, this is compared to the saved fight group to populate "
            "opponent_check."
        ),
    )
    source = st.text_input("Source", value="manual")
    default_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    timestamp = st.text_input("Timestamp", value=default_ts)
    submitted = st.form_submit_button("Add entry")

if submitted:
    errors: list[str] = []
    fighter_clean = fighter.strip()
    if not fighter_clean:
        errors.append("Fighter name is required.")
    parsed_ml = parse_moneyline(moneyline_raw)
    if parsed_ml is None:
        errors.append("Moneyline must be a non-zero integer (e.g. -150, +220).")
    source_clean = source.strip() or "manual"
    timestamp_clean = timestamp.strip() or default_ts

    if errors:
        for e in errors:
            st.error(e)
    else:
        implied_prob = american_to_implied_probability(parsed_ml)
        opponent_clean = opponent.strip()
        st.session_state.manual_odds_entries.append(
            {
                "fighter": fighter_clean,
                "opponent": opponent_clean,
                "moneyline": parsed_ml,
                "implied_probability": implied_prob,
                "implied_probability_pct": f"{implied_prob * 100:.1f}%",
                "source": source_clean,
                "timestamp": timestamp_clean,
            }
        )
        st.success(f"Added manual odds for {fighter_clean}.")

if st.session_state.manual_odds_entries:
    st.dataframe(
        pd.DataFrame(st.session_state.manual_odds_entries),
        use_container_width=True,
    )
    if st.button("Clear manual entries"):
        st.session_state.manual_odds_entries = []
else:
    st.caption("No manual entries yet.")

# --- 1c. No-Vig Implied Probability — preview only ---------------------------
st.subheader("1c. No-Vig Implied Probability — preview only")
st.caption(
    "When both sides of a matchup are present in the manual entries above "
    "(fighter A lists fighter B as opponent and vice versa, matched by "
    "normalized name), the raw implied probabilities are normalized to remove "
    "the bookmaker vig. Preview only — NOT persisted, NOT fed into "
    "projections, and NOT fed into the optimizer."
)

_manual_entries = st.session_state.manual_odds_entries
if not _manual_entries:
    st.caption("Add manual entries above to populate the no-vig preview.")
else:
    _entries_by_norm: dict[str, dict] = {}
    for _entry in _manual_entries:
        _key = normalize_name(_entry["fighter"])
        if _key and _key not in _entries_by_norm:
            _entries_by_norm[_key] = _entry

    _seen_pairs: set[frozenset[str]] = set()
    _novig_rows: list[dict] = []
    _unpaired_rows: list[dict] = []

    for _entry in _manual_entries:
        _fighter = _entry["fighter"]
        _fighter_key = normalize_name(_fighter)
        _opponent = (_entry.get("opponent") or "").strip()
        if not _opponent:
            _unpaired_rows.append(
                {
                    "fighter": _fighter,
                    "moneyline": _entry["moneyline"],
                    "status": "no opponent specified",
                }
            )
            continue
        _opp_key = normalize_name(_opponent)
        _partner = _entries_by_norm.get(_opp_key)
        if _partner is None:
            _unpaired_rows.append(
                {
                    "fighter": _fighter,
                    "moneyline": _entry["moneyline"],
                    "status": (
                        f"opponent '{_opponent}' is not in manual entries"
                    ),
                }
            )
            continue
        _partner_opp = (_partner.get("opponent") or "").strip()
        if _partner_opp and normalize_name(_partner_opp) != _fighter_key:
            _unpaired_rows.append(
                {
                    "fighter": _fighter,
                    "moneyline": _entry["moneyline"],
                    "status": (
                        f"opponent mismatch: '{_partner['fighter']}' lists "
                        f"'{_partner_opp}' as their opponent"
                    ),
                }
            )
            continue
        _pair_key = frozenset({_fighter_key, _opp_key})
        if not _pair_key or len(_pair_key) < 2 or _pair_key in _seen_pairs:
            continue
        _seen_pairs.add(_pair_key)
        _p_a, _p_b = american_pair_to_no_vig(
            _entry["moneyline"], _partner["moneyline"]
        )
        _novig_rows.append(
            {
                "fighter A": _entry["fighter"],
                "moneyline A": _entry["moneyline"],
                "raw implied A": f"{_entry['implied_probability'] * 100:.1f}%",
                "no-vig A": f"{_p_a * 100:.1f}%",
                "fighter B": _partner["fighter"],
                "moneyline B": _partner["moneyline"],
                "raw implied B": (
                    f"{_partner['implied_probability'] * 100:.1f}%"
                ),
                "no-vig B": f"{_p_b * 100:.1f}%",
            }
        )

    if _novig_rows:
        st.dataframe(pd.DataFrame(_novig_rows), use_container_width=True)
    else:
        st.caption(
            "No complete two-sided matchups yet — add both sides of a fight "
            "with matching opponent names to see no-vig probabilities."
        )

    if _unpaired_rows:
        st.markdown("**Unpaired manual entries**")
        st.dataframe(pd.DataFrame(_unpaired_rows), use_container_width=True)

# --- 1d. Odds Matching Preview -----------------------------------------------
st.subheader("1d. Odds Matching Preview")
st.caption(
    "In-memory preview of how the session's manual odds entries would match "
    "against a DK fighter list. Nothing here is persisted to the database, "
    "and these matches do NOT feed into projections or the optimizer. Paste "
    "DK fighter names below — one per line — to try it."
)

_preview_slates: list = []
_preview_slate_err: str | None = None
_odds_preview_conn = None
try:
    _odds_preview_conn = get_connection()
    bootstrap_database(_odds_preview_conn)
    _preview_slates = SlateRepository(_odds_preview_conn).list_all()
except Exception as exc:  # noqa: BLE001
    _preview_slate_err = str(exc)
    _odds_preview_conn = None

selected_slate_id: int | None = None
fight_groups_for_slate: list[FightGroupRecord] = []

if _preview_slate_err:
    st.caption(
        f"Could not load saved slates for opponent context ({_preview_slate_err}). "
        "Preview will run without opponent context."
    )
elif _preview_slates:
    slate_options = {0: "— no slate (skip opponent context) —"}
    slate_options.update({s.id: _slate_label(s) for s in _preview_slates})
    slate_choice = st.selectbox(
        "Slate (optional — enriches preview with opponent context from saved fight groups)",
        options=list(slate_options.keys()),
        format_func=lambda sid: slate_options[sid],
        index=0,
        key="odds_match_preview_slate_id",
    )
    selected_slate_id = int(slate_choice) if slate_choice else None
    if selected_slate_id:
        try:
            fight_groups_for_slate = FightGroupRepository(
                _odds_preview_conn
            ).list_for_slate(selected_slate_id)
        except Exception as exc:  # noqa: BLE001
            fight_groups_for_slate = []
            st.caption(
                f"Could not load fight groups for slate ({exc}). "
                "Preview will run without opponent context."
            )
        if not fight_groups_for_slate:
            st.caption(
                "No fight groups saved for this slate yet — add pairings on "
                "the Fight Groups page to populate opponent_check."
            )
else:
    st.caption(
        "No slates saved yet. Create a slate on Build, Step 1 and add fight "
        "groups to enrich this preview with opponent context."
    )

if "odds_match_preview_dk_text" not in st.session_state:
    st.session_state.odds_match_preview_dk_text = ""

dk_names_text = st.text_area(
    "DK fighter names (one per line)",
    key="odds_match_preview_dk_text",
    height=180,
    help=(
        "Paste fighter names from the DK slate, one per line. Not persisted — "
        "lives only in this browser session."
    ),
)

dk_fighters = [line.strip() for line in dk_names_text.splitlines() if line.strip()]
odds_entries = st.session_state.manual_odds_entries

if not odds_entries:
    st.info(
        "Add at least one manual odds entry above to populate the preview."
    )
elif not dk_fighters:
    st.info("Paste one or more DK fighter names above to run the preview.")
else:
    opponents_map = _build_opponent_context(dk_fighters, fight_groups_for_slate)
    odds_rows = [
        OddsRowInput(
            fighter=entry["fighter"],
            opponent=(entry.get("opponent") or None),
            row_id=str(idx),
        )
        for idx, entry in enumerate(odds_entries)
    ]
    results = match_odds_to_dk(dk_fighters, odds_rows, opponents=opponents_map)

    preview_rows = [
        {
            "odds fighter": r.odds_fighter,
            "matched DK fighter": r.dk_fighter or "",
            "preferred_candidate": r.preferred_candidate or "",
            "status": r.status,
            "stage": r.stage,
            "score": r.score,
            "opponent_check": r.opponent_check,
            "notes": ", ".join(r.notes),
        }
        for r in results
    ]
    st.dataframe(
        pd.DataFrame(preview_rows),
        use_container_width=True,
    )
    if opponents_map:
        st.caption(
            f"Opponent context loaded for {len(opponents_map)} DK fighter(s) "
            f"from {len(fight_groups_for_slate)} fight group(s). Preview only "
            "— match results are NOT persisted and do NOT feed projections or "
            "the optimizer. Status/stage/score follow "
            "docs/ODDS_MATCHING_DESIGN.md."
        )
    else:
        st.caption(
            "Preview only — not saved. No opponent context available, so "
            "opponent_check will read 'not_applicable' or 'unknown'. "
            "Status/stage/score follow docs/ODDS_MATCHING_DESIGN.md."
        )


# --- 1e. Odds/News Snapshot Preview — read-only ------------------------------
st.subheader("Odds/news snapshot preview — read-only")
st.caption(
    "Upload a normalized odds/news snapshot JSON (the format defined in "
    "`docs/ODDS_NEWS_SNAPSHOT_DESIGN.md`) to validate it and preview its "
    "entries and app-derived implied probabilities. The snapshot is parsed "
    "in-memory from the bytes you upload — no network call is made, even for "
    "URLs stored inside it. The preview tables are read-only; once a snapshot "
    "validates, an explicit **Save snapshot odds to slate** action appears "
    "below that writes only the moneylines into `odds_rows`."
)

snapshot_uploaded = st.file_uploader(
    "Snapshot JSON",
    type=["json"],
    accept_multiple_files=False,
    key="odds_news_snapshot_uploader",
    help=(
        "A schema_version 1 'odds_news' snapshot. Validated and previewed in "
        "your browser session only — never written to the database."
    ),
)

if snapshot_uploaded is None:
    st.caption(
        "Upload a snapshot JSON above to preview it. Preview only — nothing "
        "is saved."
    )
else:
    _snapshot_report = None
    try:
        _snapshot_report = validate_snapshot_text(snapshot_uploaded.getvalue())
    except SnapshotFormatError as exc:
        st.error(
            f"Snapshot could not be parsed: {exc} "
            "(nothing was saved — preview only)."
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Unexpected error reading snapshot: {exc}")

    if _snapshot_report is not None:
        _summary = _snapshot_report.summary
        _env = _snapshot_report.envelope

        # --- Envelope identity / provenance ---
        if _env is not None and (_env.event_name or _env.event_date):
            st.markdown(
                f"**Event:** {_env.event_name or '—'}"
                + (f" · {_env.event_date}" if _env.event_date else "")
            )
        if _env is not None:
            st.caption(
                f"Collected at: {_env.collected_at or '—'} · "
                f"method: {_env.collected_by_method or '—'} · "
                f"sources checked: {len(_env.sources_checked)}"
            )

        # --- Summary metrics ---
        _news_flag_count = sum(
            len(e.news_flags) for e in _snapshot_report.entries_ok
        )
        _props_count = sum(
            1
            for e in _snapshot_report.entries_ok
            if e.itd_odds is not None
            or e.decision_odds is not None
            or e.goes_distance is not None
        )
        _movement_count = sum(
            1 for e in _snapshot_report.entries_ok if e.line_movement is not None
        )

        _m1, _m2, _m3, _m4 = st.columns(4)
        _m1.metric("Entries", _summary.total_entries)
        _m2.metric("Valid entries", _summary.ok_entries)
        _m3.metric("Errors", _summary.error_count)
        _m4.metric("Warnings", _summary.warning_count)
        _m5, _m6, _m7, _m8 = st.columns(4)
        _m5.metric(
            "Sources checked", len(_env.sources_checked) if _env else 0
        )
        _m6.metric("News flags", _news_flag_count)
        _m7.metric("Prop entries", _props_count)
        _m8.metric("Line moves", _movement_count)

        # --- Validity banner ---
        if _snapshot_report.is_valid:
            st.success(
                f"Snapshot is valid — {_summary.ok_entries} entr"
                f"{'y' if _summary.ok_entries == 1 else 'ies'} passed "
                "validation. Preview only — nothing is saved."
            )
        else:
            st.error(
                f"Snapshot has {_summary.error_count} error(s); "
                f"{_summary.rejected_entries} entr"
                f"{'y' if _summary.rejected_entries == 1 else 'ies'} rejected. "
                "Preview only — nothing is saved."
            )

        # --- Errors / warnings (shown clearly; never raised to the page) ---
        if _snapshot_report.errors:
            st.markdown(f"**Errors ({len(_snapshot_report.errors)})**")
            for _err in _snapshot_report.errors:
                st.error(_err)
        if _snapshot_report.warnings:
            st.markdown(f"**Warnings ({len(_snapshot_report.warnings)})**")
            for _warn in _snapshot_report.warnings:
                st.warning(_warn)

        # --- Valid entries table ---
        if _snapshot_report.entries_ok:
            st.markdown("**Valid entries**")
            st.dataframe(
                pd.DataFrame(
                    _snapshot_entry_display_rows(_snapshot_report.entries_ok)
                ),
                use_container_width=True,
            )
            st.caption(
                "`implied (app-derived)` is computed from the raw moneyline — "
                "the snapshot's own `implied_probability` is only cross-checked "
                "(mismatches surface as warnings), never trusted. Preview only "
                "— nothing is saved."
            )
        else:
            st.caption("No valid entries to preview.")

        # --- Optional preview-only slate name matching --------------------
        # Reuses the Zone 1d connection / slate list (same DB, same render);
        # matches snapshot fighter names against the slate's ACTIVE fighters.
        # Read-only — no persistence.
        if _snapshot_report.entries_ok:
            st.markdown("**Preview-only name matching against a slate**")
            st.caption(
                "Optionally match the snapshot's fighters against a saved "
                "slate's active fighters. This previews what a future "
                "save-and-match step would attempt — nothing is persisted, and "
                "these matches do NOT feed projections or the optimizer."
            )
            if _preview_slate_err or _odds_preview_conn is None:
                st.caption(
                    "Saved slates are unavailable, so the name-match preview is "
                    "skipped."
                )
            elif not _preview_slates:
                st.caption(
                    "No slates saved yet — create one on Build, Step 1 and import "
                    "its DK salaries to preview name matching."
                )
            else:
                _snap_slate_options = {0: "— no slate (skip name matching) —"}
                _snap_slate_options.update(
                    {s.id: _slate_label(s) for s in _preview_slates}
                )
                _snap_slate_choice = st.selectbox(
                    "Slate to match against (optional, preview only)",
                    options=list(_snap_slate_options.keys()),
                    format_func=lambda sid: _snap_slate_options[sid],
                    index=0,
                    key="odds_news_snapshot_slate_id",
                )
                _snap_slate_id = (
                    int(_snap_slate_choice) if _snap_slate_choice else None
                )
                if _snap_slate_id is not None:
                    try:
                        _active_fighters = [
                            f
                            for f in FighterRepository(
                                _odds_preview_conn
                            ).list_for_slate(_snap_slate_id)
                            if f.status == "active"
                        ]
                    except Exception as exc:  # noqa: BLE001
                        _active_fighters = []
                        st.caption(
                            f"Could not load fighters for slate "
                            f"#{_snap_slate_id} ({exc}); name-match preview "
                            "skipped."
                        )
                    _active_names = [f.name for f in _active_fighters]
                    if not _active_names:
                        st.caption(
                            f"Slate #{_snap_slate_id} has no active fighters "
                            "yet — import its DK salary CSV (Build, Step 1) to "
                            "enable name matching."
                        )
                    else:
                        _snap_odds_rows = [
                            OddsRowInput(
                                fighter=e.fighter_name,
                                opponent=(e.opponent_name or None),
                                row_id=str(e.entry_index),
                            )
                            for e in _snapshot_report.entries_ok
                        ]
                        _snap_results = match_odds_to_dk(
                            _active_names, _snap_odds_rows
                        )
                        _matched = [r for r in _snap_results if r.dk_fighter]
                        _unmatched = [
                            r for r in _snap_results if not r.dk_fighter
                        ]
                        _dk_counts: dict[str, int] = {}
                        for r in _matched:
                            _dk_counts[r.dk_fighter] = (
                                _dk_counts.get(r.dk_fighter, 0) + 1
                            )
                        _dupes = sorted(
                            name for name, c in _dk_counts.items() if c > 1
                        )
                        st.caption(
                            f"Matched {len(_matched)} / {len(_snap_results)} "
                            f"snapshot entr"
                            f"{'y' if len(_snap_results) == 1 else 'ies'} to "
                            f"active fighters on slate #{_snap_slate_id}; "
                            f"{len(_unmatched)} unmatched; "
                            f"{len(_dupes)} possible duplicate DK fighter(s). "
                            "Preview only — nothing is saved."
                        )
                        st.dataframe(
                            pd.DataFrame(
                                [
                                    {
                                        "snapshot fighter": r.odds_fighter,
                                        "matched DK fighter": r.dk_fighter or "",
                                        "status": r.status,
                                        "stage": r.stage,
                                        "score": r.score,
                                    }
                                    for r in _snap_results
                                ]
                            ),
                            use_container_width=True,
                        )
                        if _dupes:
                            st.warning(
                                "Possible duplicate match(es) — multiple "
                                "snapshot entries resolved to the same DK "
                                f"fighter: {', '.join(_dupes)}. Preview only — "
                                "nothing is saved."
                            )

            # --- S5a: explicit Save snapshot odds to slate ----------------
            # Append-only, moneyline-only write into ``odds_rows`` via the
            # snapshot save service, then a chained recompute. NOTHING is
            # written on page load or upload — only on the button click below
            # (docs/DEVELOPMENT_NOTES.md §11). News, props and line movement stay preview-only
            # (docs/ODDS_NEWS_SNAPSHOT_PERSISTENCE_DESIGN.md §9–§11, §14 S5a).
            st.divider()
            with st.container(border=True):
                st.markdown("**Save snapshot odds to selected slate**")
                st.caption(
                    "Only moneylines are saved. News, props, and line movement "
                    "remain preview-only in S5a."
                )
                _snap_moneyline_entries = [
                    e
                    for e in _snapshot_report.entries_ok
                    if e.moneyline is not None
                ]
                if _preview_slate_err or _odds_preview_conn is None:
                    st.caption(
                        "Saved slates are unavailable, so saving is disabled."
                    )
                elif not _preview_slates:
                    st.caption(
                        "No slates saved yet — create one on Build, Step 1 before "
                        "saving snapshot odds."
                    )
                elif not _snapshot_report.is_valid:
                    st.caption(
                        f"Snapshot has {len(_snapshot_report.errors)} hard "
                        "error(s) — fix them above before saving. "
                        "(Save disabled.)"
                    )
                elif not _snap_moneyline_entries:
                    st.caption(
                        "No valid moneyline entries to save (news-only snapshot)."
                    )
                else:
                    _snap_save_options = {
                        s.id: _slate_label(s) for s in _preview_slates
                    }
                    _snap_save_choice = st.selectbox(
                        "Slate to save snapshot odds into",
                        options=list(_snap_save_options.keys()),
                        format_func=lambda sid: _snap_save_options[sid],
                        key="odds_news_snapshot_save_slate_id",
                    )
                    _snap_save_slate_id = int(_snap_save_choice)
                    if _snapshot_report.warnings:
                        st.warning(
                            f"{len(_snapshot_report.warnings)} warning(s) above "
                            "do not block saving — review them before you save."
                        )
                    if st.button(
                        f"Save {len(_snap_moneyline_entries)} snapshot "
                        f"moneyline(s) to slate #{_snap_save_slate_id}",
                        key="odds_news_snapshot_save_btn",
                    ):
                        _snap_save_result = save_snapshot_odds_to_slate(
                            _odds_preview_conn,
                            slate_id=_snap_save_slate_id,
                            report=_snapshot_report,
                        )
                        if _snap_save_result.blocked:
                            st.error(_snap_save_result.blocked_reason)
                        else:
                            if _snap_save_result.saved_count:
                                st.success(
                                    f"Saved {_snap_save_result.saved_count} "
                                    "snapshot moneyline(s) to slate "
                                    f"#{_snap_save_slate_id} (source "
                                    f"`{_snap_save_result.source_label}`, batch "
                                    f"`{_snap_save_result.import_batch_id}`)."
                                )
                            if _snap_save_result.existing_count:
                                st.info(
                                    f"{_snap_save_result.existing_count} row(s) "
                                    "already existed for this slate — "
                                    "idempotent, not duplicated."
                                )
                            if _snap_save_result.skipped_count:
                                st.info(
                                    f"{_snap_save_result.skipped_count} entr"
                                    + (
                                        "y"
                                        if _snap_save_result.skipped_count == 1
                                        else "ies"
                                    )
                                    + " skipped (no moneyline / per-row failure)."
                                )
                            if _snap_save_result.recompute is not None:
                                _rc = _snap_save_result.recompute
                                st.success(
                                    "Recomputed match results: "
                                    f"{_rc.total} row(s) — {_rc.status_counts}."
                                )
                            elif _snap_save_result.recompute_error:
                                st.warning(
                                    "Snapshot odds saved, but match results were "
                                    "not recomputed: "
                                    f"{_snap_save_result.recompute_error}"
                                )
                            if not (
                                _snap_save_result.saved_count
                                or _snap_save_result.existing_count
                            ):
                                st.info("Nothing new to save.")


# =============================================================================
# Zone 2 — Save to Slate (Writes to SQLite)
# =============================================================================
st.divider()
st.header("2. Save to Slate — Writes to SQLite")
st.caption(
    "**Explicit write actions.** Persist the validated CSV from Zone 1 and/or "
    "the manual entries from Zone 1 into the local SQLite `odds_rows` table, "
    "scoped to a selected saved slate. Re-saving is safe — both saves are "
    "idempotent on (slate_id, odds_row_key). Saved rows do NOT yet feed "
    "projections, the optimizer, or odds-matching — use Zone 3's Recompute "
    "action to materialize match results from `odds_rows`."
)

_save_conn = None
_save_slates: list = []
_save_slate_err: str | None = None
try:
    _save_conn = get_connection()
    bootstrap_database(_save_conn)
    _save_slates = SlateRepository(_save_conn).list_all()
except Exception as exc:  # noqa: BLE001
    _save_slate_err = str(exc)
    _save_conn = None

_save_slate_id: int | None = None
if _save_slate_err:
    st.error(
        "Could not load saved slates for the save actions "
        f"({_save_slate_err})."
    )
elif not _save_slates:
    st.info(
        "No slates saved yet. Create a slate on Build, Step 1 before saving "
        "odds rows."
    )
else:
    _save_options = {s.id: _slate_label(s) for s in _save_slates}
    _save_choice = st.selectbox(
        "Slate to save into (applies to both save actions below)",
        options=list(_save_options.keys()),
        format_func=lambda sid: _save_options[sid],
        key="odds_save_slate_id",
    )
    _save_slate_id = int(_save_choice)

# --- 2a. Save Validated CSV Rows — explicit write action ---------------------
with st.container(border=True):
    st.markdown("**2a. Save Validated CSV Rows — Writes to `odds_rows`**")
    st.caption(
        "Persists the validated CSV rows from Zone 1's upload as immutable "
        "rows in `odds_rows`, scoped to the slate selected above."
    )
    _validated_df = st.session_state.get("odds_csv_validated_df")
    _validated_count = int(
        st.session_state.get("odds_csv_validated_row_count", 0)
    )
    _validated_name = st.session_state.get("odds_csv_validated_filename")

    if _validated_df is None:
        st.caption(
            "No validated CSV in this session. Upload a CSV in Zone 1 — "
            "once it validates, the save action becomes available here."
        )
    elif _save_slate_id is None:
        st.caption(
            f"Validated CSV `{_validated_name}` ({_validated_count} row(s)) "
            "ready — pick a slate above to enable saving."
        )
    else:
        if st.button(
            f"Save {_validated_count} validated CSV row(s) from "
            f"`{_validated_name}` to slate #{_save_slate_id}",
            key="csv_odds_save_btn",
            disabled=_validated_count == 0,
        ):
            _batch_id = uuid.uuid4().hex[:12]
            _csv_save_result = save_csv_odds_rows(
                OddsRowRepository(_save_conn),
                slate_id=_save_slate_id,
                df=_validated_df,
                import_batch_id=_batch_id,
            )
            if _csv_save_result.saved_count:
                st.success(
                    f"Saved {_csv_save_result.saved_count} new odds row(s) "
                    f"to slate #{_save_slate_id} (batch `{_batch_id}`)."
                )
            if _csv_save_result.existing_count:
                st.info(
                    f"{_csv_save_result.existing_count} row(s) already "
                    "existed for this slate — idempotent, not duplicated."
                )
            if _csv_save_result.failure_count:
                st.warning(
                    f"{_csv_save_result.failure_count} row(s) failed "
                    "validation and were not saved."
                )
                for fighter_label, err_msg in _csv_save_result.failures:
                    st.error(f"{fighter_label}: {err_msg}")
            if not (
                _csv_save_result.saved_count
                or _csv_save_result.existing_count
                or _csv_save_result.failure_count
            ):
                st.info("Nothing to save.")

# --- 2b. Save Manual Odds Entries — explicit write action --------------------
with st.container(border=True):
    st.markdown("**2b. Save Manual Odds Entries — Writes to `odds_rows`**")
    st.caption(
        "Persists the manual entries from Zone 1 into the local SQLite "
        "database as immutable rows in `odds_rows`, scoped to the slate "
        "selected above."
    )
    _save_entries = st.session_state.manual_odds_entries
    _entry_count = len(_save_entries)

    if _entry_count == 0:
        st.caption(
            "No manual entries in this session. Add at least one in Zone 1's "
            "Manual Odds Entry form."
        )
    elif _save_slate_id is None:
        st.caption(
            f"{_entry_count} manual entr{'y' if _entry_count == 1 else 'ies'} "
            "ready — pick a slate above to enable saving."
        )
    else:
        if st.button(
            f"Save {_entry_count} manual entr{'y' if _entry_count == 1 else 'ies'} "
            f"to slate #{_save_slate_id}",
            key="manual_odds_save_btn",
        ):
            result = save_manual_odds_entries(
                OddsRowRepository(_save_conn),
                slate_id=_save_slate_id,
                entries=list(_save_entries),
            )
            if result.saved_count:
                st.success(
                    f"Saved {result.saved_count} new odds row(s) to slate "
                    f"#{_save_slate_id}."
                )
            if result.existing_count:
                st.info(
                    f"{result.existing_count} entry(ies) already existed for "
                    "this slate — idempotent, not duplicated."
                )
            if result.failure_count:
                st.warning(
                    f"{result.failure_count} entry(ies) failed validation "
                    "and were not saved."
                )
                for fighter_label, err_msg in result.failures:
                    st.error(f"{fighter_label}: {err_msg}")
            if not (
                result.saved_count
                or result.existing_count
                or result.failure_count
            ):
                st.info("Nothing to save.")


# =============================================================================
# Zone 3 — Persisted Slate Data (Read-only views + Explicit Recompute)
# =============================================================================
st.divider()
st.header("3. Persisted Slate Data — Read + Explicit Recompute")
st.caption(
    "Read-only views of the local `odds_rows` and `odds_match_results` "
    "tables for a selected saved slate. Recompute is button-only — never "
    "automatic on page load. Persisted match results do NOT yet feed "
    "projections or the optimizer."
)

_pers_conn = None
_pers_slates: list = []
_pers_slate_err: str | None = None
try:
    _pers_conn = get_connection()
    bootstrap_database(_pers_conn)
    _pers_slates = SlateRepository(_pers_conn).list_all()
except Exception as exc:  # noqa: BLE001
    _pers_slate_err = str(exc)
    _pers_conn = None

_pers_slate_id: int | None = None
if _pers_slate_err:
    st.error(
        "Could not load saved slates for the persisted views "
        f"({_pers_slate_err})."
    )
elif not _pers_slates:
    st.info(
        "No slates saved yet. Create a slate on Build, Step 1 and use Zone 2 "
        "to save odds rows before viewing persisted data here."
    )
else:
    _pers_options = {s.id: _slate_label(s) for s in _pers_slates}
    _pers_choice = st.selectbox(
        "Slate to view persisted data for (applies to both views below)",
        options=list(_pers_options.keys()),
        format_func=lambda sid: _pers_options[sid],
        key="odds_persisted_slate_id",
    )
    _pers_slate_id = int(_pers_choice)

# Review-by-exception summary for the selected slate. Declared here so it
# renders at the TOP of Zone 3, but POPULATED after the 3c reject / 3d
# recompute handlers below so its counts reflect same-render writes
# (mirrors the 3b table placeholder pattern).
_zone3_summary_placeholder = st.container()

# Persisted odds rows are loaded ONCE per render and reused by both the
# Zone 3 summary banner and the 3a table below. Neither reject nor recompute
# mutates `odds_rows`, so a single load is safe.
_pers_records: list[OddsRowRecord] = []
_pers_load_err: str | None = None
if _pers_slate_id is not None:
    try:
        _pers_records = OddsRowRepository(_pers_conn).list_for_slate(
            _pers_slate_id
        )
    except Exception as exc:  # noqa: BLE001
        _pers_load_err = str(exc)

# --- 3a. Persisted Odds Rows — read-only -------------------------------------
st.subheader("3a. Persisted Odds Rows — read-only")
st.caption(
    "Stored raw inputs from CSV uploads and manual entries — they are NOT "
    "match results, manual overrides, or projections, and they do NOT yet "
    "feed projections or the optimizer."
)

if _pers_slate_id is None:
    st.caption("Pick a slate above to view persisted odds rows.")
elif _pers_load_err:
    st.error(
        f"Could not load persisted odds rows for slate "
        f"#{_pers_slate_id} ({_pers_load_err})."
    )
elif not _pers_records:
    st.info(
        f"No persisted odds rows for slate #{_pers_slate_id} yet. Use "
        "Zone 2's save actions to populate this view."
    )
else:
    st.dataframe(
        pd.DataFrame(_persisted_odds_display_rows(_pers_records)),
        use_container_width=True,
    )
    st.caption(
        f"{len(_pers_records)} persisted odds row(s) for slate "
        f"#{_pers_slate_id}. Read-only — no edit, delete, or re-load "
        "into manual entries."
    )

# --- 3b. Persisted Odds Match Results — read-only table ----------------------
st.subheader("3b. Persisted Odds Match Results — read-only")
st.caption(
    "Generated review data from the odds-matching service — NOT manual "
    "overrides, and does NOT yet feed projections or the optimizer. Use the "
    "Recompute action below the table to refresh — match results are NEVER "
    "recomputed automatically on page load. Active reject overrides created "
    "in 3c flip this table's `effective_status` to `review_rejected` for the "
    "affected row; `match_status` is never modified."
)

# Persisted match results are loaded ONCE per render for the selected slate;
# both the 3c reject UI and the 3b placeholder reuse this list. The 3d
# recompute success path reloads it so the placeholder reflects post-click
# state on the same render. Rejects (3c) never mutate `odds_match_results`,
# so they do not require a reload.
_pmr_records: list[OddsMatchResultRecord] = []
_pmr_load_err: str | None = None
if _pers_slate_id is not None:
    try:
        _pmr_records = OddsMatchResultRepository(_pers_conn).list_for_slate(
            _pers_slate_id
        )
    except Exception as exc:  # noqa: BLE001
        _pmr_load_err = str(exc)

# Reserve a placeholder for the table so it renders ABOVE the recompute
# container visually, but is populated AFTER the click handlers below have
# run in the same script execution. Without this, a same-render recompute
# click would show stale rows until the next interaction.
_pmr_table_placeholder = st.container()

# --- 3c. Reject a Match Result — explicit write, bordered container ---------
if _pers_slate_id is not None:
    with st.container(border=True):
        st.markdown(
            "**3c. Reject a Match Result — Writes to "
            "`manual_match_overrides`**"
        )
        st.caption(
            "Records an explicit `reject_match` override for the selected "
            "`review_required` match result, scoped to this slate and to "
            "the `(odds_row_key, fighter)` pair the matcher proposed. "
            "Re-running Recompute below will not silently revive the "
            "rejected pair — overrides survive re-imports "
            "(`docs/ODDS_PERSISTENCE_DESIGN.md` §7). The click also flips "
            "this row's `effective_status` to `review_rejected` in the "
            "same transaction; `match_status` is never modified. Does NOT "
            "affect projections, the optimizer, or any export. The new "
            "active override will appear in panel 3e on the next render."
        )

        if _pmr_load_err:
            st.caption(
                f"Could not load persisted match results "
                f"({_pmr_load_err}) — reject is unavailable."
            )
        else:
            _rejectable = rejectable_match_results(_pmr_records)
            if not _rejectable:
                st.caption(
                    "No `review_required` match results on this slate. "
                    "Reject is only available for review-tier matches in "
                    "this v0 slice (`auto_match` arrives in Phase D.4; "
                    "`unmatched` is never rejectable)."
                )
            else:
                _reject_options = {
                    r.odds_row_id: format_rejectable_label(r)
                    for r in _rejectable
                }
                _reject_choice = st.selectbox(
                    "Match result to reject",
                    options=list(_reject_options.keys()),
                    format_func=lambda rid: _reject_options[rid],
                    key=f"reject_match_result_choice_{_pers_slate_id}",
                )
                _reject_reason = st.text_input(
                    "Reason (optional, for your audit notes)",
                    max_chars=500,
                    key=f"reject_match_result_reason_{_pers_slate_id}",
                    help=(
                        "Free-text for your audit notes — never shown "
                        "outside this app. Optional; empty / whitespace "
                        "is stored as NULL."
                    ),
                )
                _selected_rec = next(
                    (
                        r
                        for r in _rejectable
                        if r.odds_row_id == _reject_choice
                    ),
                    None,
                )
                if _selected_rec is not None:
                    _key_display = _selected_rec.odds_row_key[:16]
                    if len(_selected_rec.odds_row_key) > 16:
                        _key_display += "…"
                    if st.button(
                        f"Reject match for `{_key_display}` on slate "
                        f"#{_pers_slate_id}",
                        key=(
                            "reject_match_btn_"
                            f"{_pers_slate_id}_{_reject_choice}"
                        ),
                    ):
                        try:
                            _reject_result = record_reject_match_override(
                                _pers_conn,
                                slate_id=_pers_slate_id,
                                odds_row_key=_selected_rec.odds_row_key,
                                fighter_id=_selected_rec.fighter_id,
                                reason=_reject_reason,
                            )
                            _reject_rec = _reject_result.override
                        except NotImplementedError:
                            st.error(
                                "Unsupported override type — internal "
                                "bug. Please report."
                            )
                        except ValueError as exc:
                            _msg = str(exc)
                            if "odds_row_key" in _msg:
                                st.error(
                                    "The selected match result is no "
                                    f"longer on slate #{_pers_slate_id}. "
                                    "Refresh the page and try again."
                                )
                            elif "fighter" in _msg:
                                st.error(
                                    "The fighter this match result "
                                    "pointed to no longer exists on this "
                                    "slate. Click Recompute (3d), then "
                                    "try again."
                                )
                            elif "slate" in _msg:
                                st.error(
                                    "Selected slate no longer exists. "
                                    "Refresh the page."
                                )
                            else:
                                st.error(f"Reject failed: {exc}")
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Reject failed: {exc}")
                        else:
                            # Reload so the 3b placeholder reflects the new
                            # effective_status on this same render.
                            try:
                                _pmr_records = OddsMatchResultRepository(
                                    _pers_conn
                                ).list_for_slate(_pers_slate_id)
                                _pmr_load_err = None
                            except Exception as exc:  # noqa: BLE001
                                _pmr_load_err = str(exc)
                            st.success(
                                f"Active reject override #{_reject_rec.id} "
                                f"recorded for slate #{_pers_slate_id}. "
                                "This row's `effective_status` is now "
                                "`review_rejected`; `match_status` is "
                                "unchanged. See panel 3e for the new "
                                "active override."
                            )

# --- 3d. Recompute action — explicit write, in a bordered container ----------
if _pers_slate_id is not None:
    with st.container(border=True):
        st.markdown(
            "**3d. Recompute Persisted Match Results — Writes to "
            "`odds_match_results`**"
        )
        st.caption(
            "Explicitly recompute and replace the persisted match results "
            "for the selected slate. Reads persisted odds rows, persisted "
            "fighters, and saved fight groups, then replaces this slate's "
            "`odds_match_results` in one transaction. Other slates are not "
            "touched. No projections, no optimizer, no manual overrides — "
            "and no automatic recompute. Click only when you want to "
            "refresh."
        )
        if st.button(
            f"Recompute match results for slate #{_pers_slate_id}",
            key=f"recompute_match_results_btn_{_pers_slate_id}",
        ):
            try:
                _recompute_summary = recompute_and_replace_match_results(
                    _pers_conn, _pers_slate_id
                )
            except EmptyDkRosterError:
                st.error(
                    f"Cannot recompute for slate #{_pers_slate_id}: this "
                    "slate has no active DK fighters. Import the salary "
                    "CSV (Build, Step 1) before recomputing. Prior persisted "
                    "match results for this slate were NOT cleared."
                )
            except Exception as exc:  # noqa: BLE001
                st.error(
                    f"Recompute failed for slate #{_pers_slate_id} ({exc})."
                )
            else:
                # Reload so the 3b placeholder reflects post-click state
                # on this same render.
                try:
                    _pmr_records = OddsMatchResultRepository(
                        _pers_conn
                    ).list_for_slate(_pers_slate_id)
                    _pmr_load_err = None
                except Exception as exc:  # noqa: BLE001
                    _pmr_load_err = str(exc)
                if _recompute_summary.total == 0:
                    st.warning(
                        f"Recompute complete for slate #{_pers_slate_id}: "
                        "0 match result(s) written. Any prior persisted "
                        "match results for this slate were cleared (no "
                        "odds rows to match against)."
                    )
                else:
                    st.success(
                        f"Recompute complete for slate #{_pers_slate_id}: "
                        f"{_recompute_summary.total} match result(s) "
                        "written."
                    )
                    st.write(
                        {
                            "total": _recompute_summary.total,
                            "status_counts": dict(
                                _recompute_summary.status_counts
                            ),
                        }
                    )

# --- 3f. Assign / Accept a Match — explicit write, bordered container --------
if _pers_slate_id is not None:
    with st.container(border=True):
        st.markdown(
            "**3f. Assign / Accept a Match — Writes to "
            "`manual_match_overrides`**"
        )
        st.caption(
            "Bind an `unmatched` or `review_required` odds row to an active "
            "DK fighter on this slate. **Use this when a sportsbook name "
            "differs from the DK salary name** (e.g. odds say "
            "`Bruno Gustavo da Silva` but the DK salary lists `Bruno Silva`). "
            "On click this records an `accept_match` (confirming the matcher's "
            "own proposal) or `force_pair` (a fighter the matcher missed) "
            "override, supersedes any active reject/binding on the same odds "
            "row (`docs/ODDS_PERSISTENCE_DESIGN.md` §16.4), and flips this "
            "row's `effective_status` + `fighter_id` in one transaction "
            "(§16.10). Assignment is button-only — never on page load — and "
            "never guesses the fighter for you."
        )

        if _pmr_load_err:
            st.caption(
                f"Could not load persisted match results "
                f"({_pmr_load_err}) — assign is unavailable."
            )
        else:
            _assignable = assignable_match_results(_pmr_records)
            if not _assignable:
                st.info(
                    "No assignable odds rows on this slate — every match "
                    "result is auto-matched or already handled. Assign is "
                    "only for `review_required` / `unmatched` rows; a "
                    "`review_rejected` row must be un-rejected first."
                )
            else:
                _assign_fighters: list[FighterRecord] = []
                _assign_fighter_err: str | None = None
                try:
                    _assign_fighters = [
                        f
                        for f in FighterRepository(_pers_conn).list_for_slate(
                            _pers_slate_id
                        )
                        if f.status == "active"
                    ]
                except Exception as exc:  # noqa: BLE001
                    _assign_fighter_err = str(exc)

                if _assign_fighter_err:
                    st.error(
                        f"Could not load active fighters for slate "
                        f"#{_pers_slate_id} ({_assign_fighter_err}) — assign "
                        "is unavailable."
                    )
                elif not _assign_fighters:
                    st.caption(
                        f"Slate #{_pers_slate_id} has no active fighters yet "
                        "— import the DK salary CSV (Build, Step 1) before "
                        "assigning odds rows."
                    )
                else:
                    # Reuse the Zone 3 odds-row load to enrich the row labels
                    # with the sportsbook fighter name / opponent / moneyline.
                    _odds_by_key = {r.odds_row_key: r for r in _pers_records}
                    _assign_options = {}
                    for _ar in _assignable:
                        _odds_row = _odds_by_key.get(_ar.odds_row_key)
                        _assign_options[_ar.odds_row_id] = format_assignable_label(
                            _ar,
                            odds_fighter_raw=(
                                _odds_row.fighter_name_raw
                                if _odds_row is not None
                                else None
                            ),
                            opponent_raw=(
                                _odds_row.opponent_name_raw
                                if _odds_row is not None
                                else None
                            ),
                            american_odds=(
                                _odds_row.american_odds
                                if _odds_row is not None
                                else None
                            ),
                        )
                    _assign_choice = st.selectbox(
                        "Odds row to assign",
                        options=list(_assign_options.keys()),
                        format_func=lambda rid: _assign_options[rid],
                        key=f"assign_match_row_choice_{_pers_slate_id}",
                        help=(
                            "Only `review_required` / `unmatched` rows appear "
                            "here. Each label shows the sportsbook name, "
                            "opponent, moneyline, and current status."
                        ),
                    )
                    _selected_assignable = next(
                        (
                            r
                            for r in _assignable
                            if r.odds_row_id == _assign_choice
                        ),
                        None,
                    )

                    # Sentinel 0 forces an explicit pick; fighter ids are
                    # positive. For a review_required row whose matcher
                    # proposed a candidate, default to that fighter (§16.10
                    # one-click accept) by name match.
                    _SENTINEL = 0
                    _name_by_id = {f.id: f.name for f in _assign_fighters}
                    _fighter_ids = [_SENTINEL] + [f.id for f in _assign_fighters]
                    _fighter_labels = {
                        _SENTINEL: "— select an active fighter —"
                    }
                    for f in _assign_fighters:
                        _fighter_labels[f.id] = f"{f.name} (${f.salary:,})"
                    _default_index = 0
                    if (
                        _selected_assignable is not None
                        and _selected_assignable.match_status == "review_required"
                        and _selected_assignable.preferred_candidate
                    ):
                        for _i, _fid in enumerate(_fighter_ids):
                            if (
                                _fid != _SENTINEL
                                and _name_by_id.get(_fid)
                                == _selected_assignable.preferred_candidate
                            ):
                                _default_index = _i
                                break
                    _fighter_choice = st.selectbox(
                        "Active fighter to bind",
                        options=_fighter_ids,
                        format_func=lambda fid: _fighter_labels[fid],
                        index=_default_index,
                        key=f"assign_match_fighter_choice_{_pers_slate_id}",
                        help=(
                            "Active DK fighters on this slate, labelled with "
                            "their salary. A `review_required` row defaults to "
                            "the matcher's proposed fighter; an `unmatched` "
                            "row forces an explicit pick."
                        ),
                    )
                    _assign_reason = st.text_input(
                        "Reason (optional, for your audit notes)",
                        max_chars=500,
                        key=f"assign_match_reason_{_pers_slate_id}",
                        help=(
                            "Free-text for your audit notes — never shown "
                            "outside this app. Optional; empty / whitespace "
                            "is stored as NULL."
                        ),
                    )

                    if _selected_assignable is not None:
                        if st.button(
                            "Assign odds row to fighter",
                            key=(
                                "assign_match_btn_"
                                f"{_pers_slate_id}_{_assign_choice}"
                            ),
                        ):
                            if _fighter_choice == _SENTINEL:
                                st.warning(
                                    "Pick an active fighter to assign this "
                                    "odds row to."
                                )
                            else:
                                try:
                                    _assign_result = (
                                        record_assign_match_override(
                                            _pers_conn,
                                            slate_id=_pers_slate_id,
                                            odds_row_key=(
                                                _selected_assignable.odds_row_key
                                            ),
                                            fighter_id=int(_fighter_choice),
                                            reason=_assign_reason,
                                        )
                                    )
                                except NotImplementedError:
                                    st.error(
                                        "Unsupported override type — internal "
                                        "bug. Please report."
                                    )
                                except ValueError as exc:
                                    _msg = str(exc)
                                    if "already" in _msg:
                                        st.error(
                                            "That fighter is already bound to "
                                            "another odds row on this slate. "
                                            "Reject the other binding first, "
                                            f"then retry. ({_msg})"
                                        )
                                    elif "not active" in _msg:
                                        st.error(
                                            "That fighter is no longer active "
                                            "on this slate. Recompute (3d), "
                                            f"then retry. ({_msg})"
                                        )
                                    elif "odds_row_key" in _msg:
                                        st.error(
                                            "The selected odds row is no "
                                            f"longer on slate #{_pers_slate_id}"
                                            ". Recompute (3d) and try again."
                                        )
                                    else:
                                        st.error(f"Assign failed: {exc}")
                                except Exception as exc:  # noqa: BLE001
                                    st.error(f"Assign failed: {exc}")
                                else:
                                    # Reload so the 3b placeholder reflects
                                    # the new effective_status / fighter_id
                                    # on this same render.
                                    try:
                                        _pmr_records = (
                                            OddsMatchResultRepository(
                                                _pers_conn
                                            ).list_for_slate(_pers_slate_id)
                                        )
                                        _pmr_load_err = None
                                    except Exception as exc:  # noqa: BLE001
                                        _pmr_load_err = str(exc)
                                    _bound_name = _name_by_id.get(
                                        int(_fighter_choice), ""
                                    )
                                    st.success(
                                        "Assigned odds row to "
                                        f"`{_bound_name}` on slate "
                                        f"#{_pers_slate_id} as "
                                        f"`{_assign_result.override_type}` "
                                        f"(override #{_assign_result.override.id}"
                                        "). This row's `effective_status` and "
                                        "`fighter_id` are updated in the table "
                                        "above; the active override appears in "
                                        "panel 3e on the next render."
                                    )

# Populate the 3b table placeholder AFTER 3c reject, 3d recompute, and 3f
# assign click handlers have run, so a same-render write is reflected here
# without needing st.rerun().
with _pmr_table_placeholder:
    if _pers_slate_id is None:
        st.caption("Pick a slate above to view persisted match results.")
    elif _pmr_load_err:
        st.error(
            f"Could not load persisted match results for slate "
            f"#{_pers_slate_id} ({_pmr_load_err})."
        )
    elif not _pmr_records:
        st.info(
            f"No persisted match results for slate #{_pers_slate_id} "
            "yet. Use the Recompute action below to populate this view "
            "from the slate's persisted odds rows and fighters."
        )
    else:
        # Review-by-exception: surface rows that still need attention first;
        # keep cleanly auto-matched rows collapsed below.
        _needs_review, _clean = _split_match_results_by_review(_pmr_records)
        if _needs_review:
            st.markdown(
                f"**Needs review — {len(_needs_review)} of "
                f"{len(_pmr_records)} match result(s)**"
            )
            st.caption(
                "Rows whose `effective_status` is not `auto_match` "
                "(`review_required`, `unmatched`, or `review_rejected`). "
                "Reject a bad `review_required` match in 3c, or add odds "
                "(Zone 2) and Recompute (3d) to resolve `unmatched` rows."
            )
            st.dataframe(
                pd.DataFrame(
                    _persisted_match_result_display_rows(_needs_review)
                ),
                use_container_width=True,
            )
        else:
            st.success(
                f"All {len(_pmr_records)} match result(s) are cleanly "
                "auto-matched — nothing needs review on this slate."
            )
        if _clean:
            with st.expander(
                f"Show {len(_clean)} auto-matched (clean) match result(s)",
                expanded=False,
            ):
                st.dataframe(
                    pd.DataFrame(
                        _persisted_match_result_display_rows(_clean)
                    ),
                    use_container_width=True,
                )
        st.caption(
            f"{len(_pmr_records)} persisted match result(s) for slate "
            f"#{_pers_slate_id}. Read-only — no edit, delete, or "
            "override from this page. Not a manual override and does "
            "NOT yet feed projections or the optimizer."
        )

# Populate the Zone 3 summary banner AFTER the 3c reject / 3d recompute
# handlers so its counts reflect any same-render write (mirrors the 3b
# table placeholder above).
with _zone3_summary_placeholder:
    if _pers_slate_id is None:
        st.caption(
            "Pick a slate below to see its odds-review status at a glance."
        )
    elif _pers_load_err or _pmr_load_err:
        st.caption(
            "Slate review summary unavailable — see the load error(s) below."
        )
    else:
        _summary = _summarize_match_review(_pmr_records)
        st.markdown(f"**Slate #{_pers_slate_id} — odds review status**")
        st.caption(
            f"Odds rows: {len(_pers_records)} · "
            f"Match results: {_summary['total']} · "
            f"Matched: {_summary['auto_match']} · "
            f"Needs review: {_summary['review_required']} · "
            f"Unmatched: {_summary['unmatched']} · "
            f"Rejected: {_summary['review_rejected']}"
        )
        if _summary["total"] == 0:
            if len(_pers_records) == 0:
                st.info(
                    "No persisted odds rows yet. Save odds in Zone 2, then "
                    "Recompute (3d) to generate match results."
                )
            else:
                st.info(
                    f"{len(_pers_records)} odds row(s) saved but no match "
                    "results yet. Click Recompute (3d) to match them "
                    "against this slate's fighters."
                )
        elif _summary["needs_action"] == 0:
            _handled = (
                f" ({_summary['review_rejected']} rejected/handled)"
                if _summary["review_rejected"]
                else ""
            )
            st.success(
                "✅ Odds review complete — every match result is "
                f"auto-matched{_handled}. Next: confirm fight groups, then "
                "run the Manual Review gate before building."
            )
        else:
            st.warning(
                f"⚠️ {_summary['needs_action']} match result(s) need review "
                f"(review_required: {_summary['review_required']}, "
                f"unmatched: {_summary['unmatched']}). Resolve them in the "
                "Needs-review table below — Reject a bad match in 3c, or add "
                "odds (Zone 2) and Recompute (3d)."
            )

# --- 3e. Active Manual Match Overrides — read-only ---------------------------
st.subheader("3e. Active Manual Match Overrides — read-only")
st.caption(
    "Active manual match overrides (`superseded_at IS NULL`) persisted for "
    "the selected slate. Rejects created in 3c land here on the next render. "
    "**Read-only panel** — no supersede / revoke actions are wired up yet. "
    "Active reject overrides flip the affected row's `effective_status` to "
    "`review_rejected` in 3b, but do NOT yet feed projections, the "
    "optimizer, or any export."
)

if _pers_slate_id is None:
    st.caption("Pick a slate above to view active manual match overrides.")
else:
    _overrides: list[ManualMatchOverrideRecord] = []
    _overrides_load_err: str | None = None
    try:
        _overrides = ManualMatchOverrideRepository(
            _pers_conn
        ).list_active_for_slate(_pers_slate_id)
    except Exception as exc:  # noqa: BLE001
        _overrides_load_err = str(exc)

    if _overrides_load_err:
        st.error(
            f"Could not load active manual match overrides for slate "
            f"#{_pers_slate_id} ({_overrides_load_err})."
        )
    elif not _overrides:
        st.info(
            f"No active manual match overrides for slate #{_pers_slate_id}. "
            "Override creation is not yet implemented — this panel will "
            "populate once the write path lands in a later phase."
        )
    else:
        st.dataframe(
            pd.DataFrame(_active_override_display_rows(_overrides)),
            use_container_width=True,
        )
        st.caption(
            f"{len(_overrides)} active override(s) for slate "
            f"#{_pers_slate_id}. Read-only — no override write actions yet, "
            "and these rows do NOT yet feed projections or the optimizer."
        )
