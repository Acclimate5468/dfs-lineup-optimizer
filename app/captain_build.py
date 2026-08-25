"""App-side DK UFC **Captain (Showdown)** builder — self-contained, read-only
MVP (slice C5).

Realizes ``docs/CAPTAIN_MODE_DESIGN.md`` §4 (the **C5 MVP note**), §5, §6, §7,
§10, §14.2 (the MOV finish-aware method selector, **C10**), §14.3 (the GPP | cash
stack toggle, **C11a** — default GPP), §14.4 (the captain-leverage CPTproj view,
**C11b**), and the §3 additive
rules. This module renders
the **Captain** branch of the contest router in ``app/pages/00_build.py``; the
Classic two-step builder is left byte-for-byte unchanged and ``st.stop()``s
before this ever runs, so the two formats never share a code path (design §3).

What this wires (all pure modules already built in C2–C4 / C9–C10):

1. **Upload** a DK Captain salary CSV → :func:`src.captain.salary_csv.parse_captain_salary_rows`
   (C2) — collapses the CPT (1.5×) + F rows into one fighter and pairs bouts by
   byte-identical ``Game Info``; out (``O``) fighters are surfaced, never rostered.
2. **Consensus odds (slice C8)** — two read-only paste boxes (BestFightOdds
   all-books HTML and/or a DraftKings / multi-book grid) that **reuse** the
   validated Classic odds modules
   (:func:`src.ingestion.providers.bestfightodds.parse_bestfightodds_all_books`,
   :func:`src.ingestion.providers.multi_book_paste.parse_multi_book_paste`,
   :func:`src.ingestion.consensus_assembly.merge_sources` /
   :func:`~src.ingestion.consensus_assembly.assemble_fights`,
   :func:`src.projections.odds_consensus.compute_slate_consensus`) to compute
   de-vigged **median consensus** win probabilities, mapped to the Captain
   fighters by :func:`src.utils.text_cleaning.normalize_name`. Imported, never
   edited (design §3). No network fetch (paste only); nothing is saved.
3. **Manual moneyline** entry per fighter (one number input each) — the
   **fallback / override** for any fighter consensus did not price (design §13
   C8 step 4). **De-vig per bout** reuses
   :func:`src.projections.implied_probability.american_pair_to_no_vig`
   (no de-vig math is re-implemented here) → each fighter's no-vig win prob.
   Each fighter's build win prob resolves to **consensus if priced, else the
   manual moneyline**, and the chosen source is surfaced per fighter.
4. **5-round toggle** per bout (title / main events); default 3 rounds (§5).
5. **Review gate in spirit** (design §4 C5 MVP note): an explicit
   "I reviewed this slate" acknowledgement. The Build button is **disabled**
   until it is checked *and* every rostered fighter has a moneyline. The full
   ``evaluate_manual_review`` gate reads persisted slate data and is the **C6**
   persistence slice — not wired here.
6. **Build-method selector** (§7 / §14.2 / C10): **Heuristic** (the default) or
   the additive **MOV finish-aware** engine (``adjProj = baseProj + K·finish
   signal``), labeled **experimental** with a clear caveat (``K`` is an
   unvalidated knob). ``K`` is an editable number (default
   :data:`~src.captain.build_method.FINISH_BONUS_K_DEFAULT`). When Finish-aware is
   selected, an optional **method-of-victory odds** section (§14.1, via
   :func:`_render_mov_section`) lets the user price each bout's finish signal
   through :mod:`src.captain.finish_signal` (C9 — reused, never re-implemented);
   a bout left blank or malformed keeps its fighters on the base projection. The
   selected :class:`~src.captain.build_method.ProjectionMethod` drives the build.
7. **Build** → :func:`src.captain.build_method.get_method` → the selected method's
   ``project`` → :func:`src.captain.optimizer.optimize_captain_lineups` (1 CPT at
   1.5× / ``captain_salary`` + 5 fighters, ``$50,000`` cap, **no** same-fight
   exclusion, §6) → top-N lineups.
8. **Deterministic reasoning** per lineup (:func:`build_captain_reasoning`,
   pure): names the method, then cites win prob, the engine's base projection
   (Heuristic decomposes its components; the experimental engine states what its
   projection is and that it is unvalidated), the 1.5× captain leverage, and
   constraint satisfaction. It never invents a finish / KO / "lock" / predicted
   winner (design §10) — every number traces to a supplied input.

**Read-only end to end (design §4 C5 MVP note; ``docs/DEVELOPMENT_NOTES.md`` §11).** This module
opens **no** database connection, runs no optimizer on page load, and writes
nothing — to the DB or anywhere. All state lives in Streamlit widgets / session
for the duration of the render. Persistence + the real Manual Review gate are
the deferred **C6** slice.
"""

from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from src.captain.build_method import (
    FINISH_AWARE_METHOD_NAME,
    FINISH_BONUS_K_DEFAULT,
    FighterProjectionInput,
    FinishAwareMethod,
    available_methods,
    get_method,
    is_experimental,
    method_label,
    HEURISTIC_METHOD_NAME,
)
from src.captain.finish_signal import (
    FinishOddsBout,
    FinishSignalError,
    FinishSignalTier,
    MethodOfVictoryOdds,
    compute_finish_signals,
)
from src.captain.optimizer import (
    CAPTAIN_MULTIPLIER,
    LINEUP_SIZE,
    SALARY_CAP as CAPTAIN_SALARY_CAP,
    CaptainLineup,
    CaptainOptimizerStatus,
    CaptainRanking,
    StackMode,
    optimize_captain_lineups,
    rank_captains_by_cptproj,
)
from src.captain.salary_csv import (
    CaptainFighter,
    CaptainSalaryParseError,
    REQUIRED_COLUMNS as CAPTAIN_REQUIRED_COLUMNS,
    parse_captain_salary_rows,
)
from src.ingestion.consensus_assembly import assemble_fights, merge_sources
from src.ingestion.providers.bestfightodds import (
    BestFightOddsParseError,
    parse_bestfightodds_all_books,
)
from src.ingestion.providers.multi_book_paste import (
    MultiBookPasteParseError,
    parse_multi_book_paste,
)
from src.projections.default_projection import WIN_PROB_WEIGHT
from src.projections.implied_probability import american_pair_to_no_vig
from src.projections.odds_consensus import (
    MIN_BOOKS_DEFAULT,
    compute_slate_consensus,
)
from src.projections.value_bonus import five_round_bonus, value_gap_bonus
from src.utils.text_cleaning import normalize_name

# --- Widget keys (stable so AppTest can target them unambiguously) ----------
_UPLOAD_KEY = "captain_salary_upload"
_METHOD_KEY = "captain_method"
_K_KEY = "captain_finish_k"
_STACK_MODE_KEY = "captain_stack_mode"

# Stack-toggle option labels (design §14.3). The UI selector defaults to GPP (the
# tournament default), even though the optimizer PARAMETER defaults to cash.
_STACK_GPP_LABEL = "GPP"
_STACK_CASH_LABEL = "Cash"
_ACK_KEY = "captain_review_ack"
_BUILD_BTN_KEY = "captain_build_btn"

# Captain-leverage view (design §14.4, slice C11b). The pin selectbox persists the
# chosen captain across reruns; the built flag keeps the build (and the leverage
# view) rendered after the first Build click so pivoting the pin rebuilds in place
# (a fresh click would otherwise be the only run that renders the build).
_CAPTAIN_PIN_KEY = "captain_pin"
_BUILT_FLAG_KEY = "captain_built"

# Consensus odds paste path (slice C8). Two paste boxes + a read-only compute
# button; the flag persists the "compute" decision across reruns so the blend
# survives the Streamlit rerun a button click triggers.
_BFO_PASTE_KEY = "captain_bfo_paste"
_MULTIBOOK_PASTE_KEY = "captain_multibook_paste"
_COMPUTE_KEY = "captain_compute_consensus_btn"
_CONSENSUS_FLAG_KEY = "captain_consensus_computed"

# Build button label — surfaced for tests and so the Classic Build button
# (``Build research lineups``) is never confused with the Captain one.
BUILD_BTN_LABEL = "Build Captain lineups"

# How many lineups the optimizer returns / the page renders.
_TOP_N = 5


def _slug(name: str) -> str:
    """A stable, collision-resistant widget-key slug for a fighter / bout."""
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def _ml_key(name: str) -> str:
    return f"captain_ml_{_slug(name)}"


def _five_round_key(fighter_1_name: str, fighter_2_name: str) -> str:
    return f"captain_5r_{_slug(fighter_1_name)}__{_slug(fighter_2_name)}"


# Method-of-victory (MOV) odds widget keys (this slice). Per-fighter for the
# tier-0 method tree (KO/TKO, Submission, Decision); per-bout for the two §14.1
# fallback markets (go-the-distance No/Yes, round-total Under/Over).
def _mov_ko_key(name: str) -> str:
    return f"captain_mov_ko_{_slug(name)}"


def _mov_sub_key(name: str) -> str:
    return f"captain_mov_sub_{_slug(name)}"


def _mov_dec_key(name: str) -> str:
    return f"captain_mov_dec_{_slug(name)}"


def _bout_key(prefix: str, fighter_1_name: str, fighter_2_name: str) -> str:
    return f"captain_{prefix}_{_slug(fighter_1_name)}__{_slug(fighter_2_name)}"


# Human labels for the §14.1 ladder tier surfaced per bout (how it was priced).
_TIER_LABELS = {
    FinishSignalTier.METHOD_OF_VICTORY: "Method-of-victory",
    FinishSignalTier.DISTANCE: "Distance",
    FinishSignalTier.ROUND_TOTAL: "Round-total",
}


# ---------------------------------------------------------------------------
# Consensus odds (slice C8 — design §13 C8). Pure: parse two paste blocks,
# REUSE the validated Classic odds modules to compute de-vigged median consensus
# win probabilities per fighter, and resolve each Captain fighter's build
# win_prob (consensus preferred, manual moneyline as fallback). No Streamlit, no
# DB, no network — every reused module is imported, never edited (design §3).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsensusFighterPrice:
    """One fighter's consensus win probability + its book-confidence signals.

    Carries the no-vig median consensus win prob and the ``odds_consensus``
    confidence signals (``book_count`` / ``dispersion`` / ``low_confidence``)
    so the UI can surface them. ``fighter_name`` is the raw name as it appeared
    in the pasted odds; ``normalized`` is the ``normalize_name`` key used to map
    it to a Captain fighter.
    """

    fighter_name: str
    normalized: str
    win_prob: float
    book_count: int
    dispersion: float | None
    low_confidence: bool


@dataclass(frozen=True)
class CaptainConsensusResult:
    """Outcome of :func:`compute_captain_consensus` (pure).

    ``by_normalized`` maps each priced fighter's ``normalize_name`` key to its
    :class:`ConsensusFighterPrice`. ``unpaired`` are the raw names the assembly
    could not pair into a fight (surfaced, never silently dropped — design §9).
    ``parse_warnings`` collects each source's parse error / skip so a partial or
    failed paste is reported, not swallowed. ``fights_considered`` is the number
    of fights the blend evaluated.
    """

    by_normalized: dict[str, ConsensusFighterPrice]
    unpaired: list[str]
    parse_warnings: list[str]
    fights_considered: int


def compute_captain_consensus(
    *,
    bestfightodds_text: str = "",
    multibook_text: str = "",
    min_books: int = MIN_BOOKS_DEFAULT,
) -> CaptainConsensusResult:
    """Blend pasted BestFightOdds / multi-book odds into per-fighter consensus.

    Reuses the validated Classic odds pipeline end to end (design §13 C8) — the
    two parsers, the :func:`merge_sources` / :func:`assemble_fights` assembly,
    and :func:`compute_slate_consensus` — importing each and editing none. Each
    source is optional; a source that fails to parse is recorded in
    ``parse_warnings`` (never raised) so the other source still contributes.

    A fighter is "priced" when its fight had at least one two-sided book, even if
    that is below ``min_books`` (the result is still emitted, only flagged
    ``low_confidence`` — the same posture as the Classic blend). Fighters whose
    fight had no two-sided price are simply absent from ``by_normalized``.
    """
    parse_warnings: list[str] = []

    bfo_rows = ()
    if bestfightodds_text and bestfightodds_text.strip():
        try:
            bfo_rows = parse_bestfightodds_all_books(bestfightodds_text)
        except BestFightOddsParseError as exc:
            parse_warnings.append(f"BestFightOdds paste could not be parsed: {exc}")

    paste_rows = ()
    if multibook_text and multibook_text.strip():
        try:
            paste_result = parse_multi_book_paste(multibook_text)
            paste_rows = paste_result.rows
            parse_warnings.extend(paste_result.warnings)
        except MultiBookPasteParseError as exc:
            parse_warnings.append(
                f"DraftKings / multi-book paste could not be parsed: {exc}"
            )

    merged = merge_sources(bestfightodds_rows=bfo_rows, paste_rows=paste_rows)
    assembly = assemble_fights(merged)
    results = compute_slate_consensus(assembly.fights, min_books=min_books)

    by_normalized: dict[str, ConsensusFighterPrice] = {}
    for res in results:
        for raw_name, prob in (
            (res.fighter_a, res.prob_a),
            (res.fighter_b, res.prob_b),
        ):
            if prob is None:
                continue
            norm = normalize_name(raw_name)
            if not norm:
                continue
            by_normalized[norm] = ConsensusFighterPrice(
                fighter_name=raw_name,
                normalized=norm,
                win_prob=prob,
                book_count=res.book_count,
                dispersion=res.dispersion,
                low_confidence=res.low_confidence,
            )

    return CaptainConsensusResult(
        by_normalized=by_normalized,
        unpaired=list(assembly.unpaired),
        parse_warnings=parse_warnings,
        fights_considered=len(results),
    )


@dataclass(frozen=True)
class ResolvedWinProb:
    """One fighter's resolved build win probability and where it came from."""

    name: str
    win_prob: float
    source: str  # "consensus" | "manual"


@dataclass(frozen=True)
class WinProbResolution:
    """Per-fighter win-prob resolution (design §13 C8 step 4).

    ``resolved`` maps fighter name -> :class:`ResolvedWinProb` (consensus
    preferred, manual moneyline as fallback). ``uncovered`` lists eligible
    fighters priced by neither source (they cannot be built and gate the Build
    button). ``errors`` records per-bout de-vig failures, surfaced not silent.
    """

    resolved: dict[str, ResolvedWinProb]
    uncovered: list[str]
    errors: list[str]


def resolve_win_probs(
    eligible: list[CaptainFighter],
    bouts,
    moneyline_by_name: dict[str, int | None],
    consensus_by_norm: dict[str, ConsensusFighterPrice],
) -> WinProbResolution:
    """Resolve each eligible fighter's build win prob (consensus, else manual).

    Consensus (fighter-level, already de-vigged by the blend) takes precedence
    when a fighter's ``normalize_name`` key is priced. Otherwise the fighter
    falls back to the **existing C5 manual path**: de-vig the fighter's bout pair
    via :func:`american_pair_to_no_vig` (both sides' moneylines required). A
    fighter priced by neither source is reported in ``uncovered`` — never
    silently dropped.
    """
    # Manual de-vig per bout — the unchanged C5 path (both moneylines required).
    manual: dict[str, float] = {}
    errors: list[str] = []
    for bout in bouts:
        ml_1 = moneyline_by_name.get(bout.fighter_1_name)
        ml_2 = moneyline_by_name.get(bout.fighter_2_name)
        if ml_1 is None or ml_2 is None:
            continue
        try:
            p1, p2 = american_pair_to_no_vig(ml_1, ml_2)
        except ValueError as exc:
            errors.append(
                f"Could not de-vig {bout.fighter_1_name} vs "
                f"{bout.fighter_2_name}: {exc}"
            )
            continue
        manual[bout.fighter_1_name] = p1
        manual[bout.fighter_2_name] = p2

    resolved: dict[str, ResolvedWinProb] = {}
    uncovered: list[str] = []
    for fighter in eligible:
        price = consensus_by_norm.get(normalize_name(fighter.name))
        if price is not None:
            resolved[fighter.name] = ResolvedWinProb(
                name=fighter.name, win_prob=price.win_prob, source="consensus"
            )
        elif fighter.name in manual:
            resolved[fighter.name] = ResolvedWinProb(
                name=fighter.name, win_prob=manual[fighter.name], source="manual"
            )
        else:
            uncovered.append(fighter.name)
    return WinProbResolution(resolved=resolved, uncovered=uncovered, errors=errors)


# ---------------------------------------------------------------------------
# Pure deterministic reasoning (design §10). No Streamlit, no I/O — every line
# is a string built from supplied facts; it never asserts a fight outcome.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FighterFacts:
    """The already-computed facts the reasoning cites for one fighter."""

    name: str
    win_prob: float
    scheduled_rounds: int
    base_salary: int
    captain_salary: int
    # The projection the selected method produced for this fighter (the actual
    # number the optimizer ranked, before the 1.5× captain multiplier). For the
    # MOV finish-aware method this is the *adjusted* projection (base + K×signal).
    # Carried so the reasoning cites what the engine returned rather than
    # re-deriving a method-specific formula.
    projection: float
    # The MOV finish signal priced for this fighter (None when no method-of-victory
    # odds were entered for its bout) and the finish-bonus K in force, so the
    # finish-aware reasoning can decompose adjProj = base + K×signal accurately.
    finish_signal: float | None = None
    finish_bonus_k: float = 0.0


def _pct(p: float) -> str:
    return f"{float(p) * 100:.0f}%"


def _money(value: int | float) -> str:
    return f"${int(round(value)):,}"


def build_captain_reasoning(
    lineup: CaptainLineup,
    facts_by_name: dict[str, _FighterFacts],
    *,
    rank: int,
    method_name: str,
) -> list[str]:
    """Return deterministic, fact-bounded reasoning lines for one lineup (§10).

    Names the method that produced the projections, cites the captain's win
    probability and base projection, the 1.5× captain leverage, and constraint
    satisfaction (6 fighters, under the $50k cap, no same-fight exclusion). The
    base-projection line is method-aware: the **Heuristic** decomposes its
    ``win_prob×70 + value + five-round`` components (and cites the flex value
    drivers); the **experimental** MOV finish-aware engine instead states what
    its projection is (base + K × finish signal) and flags it unvalidated —
    decomposing adjProj = base + K×signal when method-of-victory odds were
    entered, else stating the bonus is 0. Either way every number comes
    from ``facts_by_name`` — the engine's own output plus the odds-derived win
    probability — and it invents no finish / KO / "lock" / predicted winner.
    """
    cap = facts_by_name[lineup.captain_name]
    is_heuristic = method_name == HEURISTIC_METHOD_NAME
    experimental = is_experimental(method_name)
    label = method_label(method_name)
    # The actual projection the engine returned (what the optimizer ranked) — the
    # base for the Heuristic, the adjusted projection when a MOV finish bonus was
    # applied. Name it accordingly so the leverage line is never misleading.
    projection = cap.projection
    captain_pts = CAPTAIN_MULTIPLIER * projection
    method_suffix = " (experimental, unvalidated)" if experimental else ""
    proj_noun = (
        "adjusted projection"
        if (not is_heuristic and cap.finish_signal is not None)
        else "base projection"
    )

    lines = [
        (
            f"Lineup {rank}: 1 Captain + 5 Fighters, {_money(lineup.salary)} "
            f"total salary, {lineup.points:.1f} projected points — built with "
            f"the {label} method{method_suffix}."
        ),
        (
            f"Captain {lineup.captain_name}: {captain_pts:.1f} pts — {proj_noun} "
            f"{projection:.1f} × {CAPTAIN_MULTIPLIER} leverage, "
            f"costing {_money(cap.captain_salary)} (the CPT / 1.5× salary row)."
        ),
        (
            f"{lineup.captain_name} carries a {_pct(cap.win_prob)} implied win "
            "probability (no-vig moneyline) — a stored number, not an outcome."
        ),
    ]

    if is_heuristic:
        win_component = cap.win_prob * WIN_PROB_WEIGHT
        vg = value_gap_bonus(cap.base_salary, cap.win_prob)
        fr = five_round_bonus(cap.scheduled_rounds)
        lines.append(
            f"{lineup.captain_name} base projection {projection:.1f} = "
            f"{_pct(cap.win_prob)}×70 ({win_component:.1f}) "
            f"{vg:+.0f} value bonus {fr:+.0f} five-round bonus."
        )
        # Value drivers among the five flex fighters (cited only when positive).
        for flex_name in lineup.flex_names:
            f = facts_by_name[flex_name]
            flex_vg = value_gap_bonus(f.base_salary, f.win_prob)
            if flex_vg > 0:
                lines.append(
                    f"{f.name} ({_money(f.base_salary)} at {_pct(f.win_prob)} "
                    f"implied) clears a +{flex_vg:.0f} value-gap tier."
                )
    else:
        # MOV finish-aware engine (design §14.2): state what the adjusted
        # projection IS — the Heuristic base plus K × finish signal — without
        # re-deriving the Heuristic formula or claiming any result. The signal is
        # the de-vigged probability of winning inside the distance, priced from the
        # entered method-of-victory odds; with none entered the bonus is 0 and
        # adjProj == base. Every number traces to an input.
        if cap.finish_signal is not None:
            bonus = cap.finish_bonus_k * cap.finish_signal
            base = projection - bonus
            lines.append(
                f"{lineup.captain_name} adjusted projection {projection:.1f} = "
                f"{label}: base projection {base:.1f} + K={cap.finish_bonus_k:g} × "
                f"{_pct(cap.finish_signal)} finish signal ({bonus:+.1f}). The "
                "finish signal is the de-vigged probability of winning inside the "
                "distance, from the entered method-of-victory odds — experimental, "
                "K is an unvalidated knob, and this is not an assumed result."
            )
        else:
            lines.append(
                f"{lineup.captain_name} base projection {projection:.1f} = "
                f"{label}: the Heuristic base plus K × finish signal (the de-vigged "
                "probability of winning inside the distance, from method-of-victory "
                "odds). Experimental — K is an unvalidated knob; with no "
                "method-of-victory odds entered for this bout the finish bonus is "
                f"0, so this equals the {_pct(cap.win_prob)}-win-probability base "
                "projection, not an assumed result."
            )

    lines.append(
        f"Constraints satisfied: {LINEUP_SIZE} fighters (1 CPT + 5), "
        f"{_money(lineup.salary)} ≤ {_money(CAPTAIN_SALARY_CAP)} cap. No "
        "same-fight exclusion is applied — both fighters of a bout may be "
        "rostered (unlike Classic)."
    )
    return lines


# ---------------------------------------------------------------------------
# Render (the only impure surface — Streamlit widgets; still no DB / network).
# ---------------------------------------------------------------------------


def render_captain_section() -> None:
    """Render the read-only Captain builder (design §4 C5 MVP note).

    Called from the Captain branch of the ``00_build.py`` contest router, which
    ``st.stop()``s immediately after so no Classic code runs. Writes nothing.
    """
    st.markdown(
        '<div class="tsb-card tsb-idle">'
        '<div class="tsb-step">Captain · Showdown</div>'
        "<h2>Captain Mode (Showdown)</h2>"
        '<div class="tsb-desc">Upload a DraftKings UFC <b>Captain</b> salary '
        "CSV, enter the moneylines, flag the 5-round bouts, acknowledge your "
        "review, and build research lineups. This is a self-contained, "
        "<b>read-only</b> builder — nothing is saved.</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    uploaded = st.file_uploader(
        "DK UFC Captain (Showdown) salary CSV",
        type=["csv"],
        accept_multiple_files=False,
        help=(
            "Official DraftKings UFC Captain export — each fighter listed twice "
            "(a CPT 1.5× row and an F base row)."
        ),
        key=_UPLOAD_KEY,
    )
    if uploaded is None:
        st.info(
            "Upload a DraftKings UFC Captain (Showdown) salary CSV to begin. "
            "Expected columns: " + ", ".join(CAPTAIN_REQUIRED_COLUMNS) + "."
        )
        return

    try:
        df = pd.read_csv(io.BytesIO(uploaded.getvalue()))
        df.columns = [c.strip() for c in df.columns]
    except pd.errors.EmptyDataError:
        st.error(
            "CSV is empty. Expected a DK UFC Captain salary export with "
            "columns: " + ", ".join(CAPTAIN_REQUIRED_COLUMNS) + "."
        )
        return
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read CSV: {exc}")
        return

    try:
        parsed = parse_captain_salary_rows(df)
    except CaptainSalaryParseError as exc:
        st.error(f"Could not parse Captain salary CSV: {exc}")
        return

    for warning in parsed.warnings:
        st.warning(warning)

    fighters_by_name = {f.name: f for f in parsed.fighters}
    bout_names: set[str] = set()
    for bout in parsed.bouts:
        bout_names.add(bout.fighter_1_name)
        bout_names.add(bout.fighter_2_name)

    eligible = [f for f in parsed.fighters if f.name in bout_names]
    out_fighters = [f for f in parsed.fighters if f.is_out]
    unpaired = [
        f
        for f in parsed.fighters
        if not f.is_out and f.name not in bout_names
    ]

    _render_slate_summary(parsed.fighters, parsed.bouts, out_fighters, unpaired)

    if len(eligible) < LINEUP_SIZE:
        st.warning(
            f"Need at least {LINEUP_SIZE} fighters in paired bouts to build a "
            f"Captain lineup; this slate has {len(eligible)}."
        )
        return

    # Build method selector (design §7 / slice C7). The Heuristic is the v0
    # DEFAULT; the finish-aware v2 engine is an additive, EXPERIMENTAL second
    # method. Future engines (Monte Carlo) register additively and appear here
    # automatically. Order non-experimental engines first (so the default leads),
    # then experimental ones; the default is still selected explicitly below.
    method_names = sorted(
        available_methods(), key=lambda n: (is_experimental(n), n)
    )
    default_index = (
        method_names.index(HEURISTIC_METHOD_NAME)
        if HEURISTIC_METHOD_NAME in method_names
        else 0
    )
    method_name = st.selectbox(
        "Build method",
        options=method_names,
        index=default_index,
        key=_METHOD_KEY,
        format_func=method_label,
        help=(
            "Heuristic is the v0 default (win_prob×70 + value bonus + five-round "
            "bonus). Finish-aware (MOV) is experimental. Engines register "
            "additively; nothing is removed."
        ),
    )
    # K is the MOV finish-bonus knob (design §14.2); only the finish-aware engine
    # reads it. Default to the registered K so the Heuristic build is unaffected.
    finish_bonus_k = float(FINISH_BONUS_K_DEFAULT)
    if is_experimental(method_name):
        # Clear caveat (design §14.2): K is an unvalidated knob, and with no
        # method-of-victory odds input yet the finish bonus is inert.
        st.warning(
            f"**{method_label(method_name)} is experimental.** K is an "
            "unvalidated knob (default 20): adjProj = base projection + K × "
            "finish signal (the de-vigged probability of winning inside the "
            "distance). Enter method-of-victory odds below to activate the finish "
            "bonus — a bout left blank keeps its fighters on the base projection."
        )
        finish_bonus_k = float(
            st.number_input(
                "Finish bonus K",
                min_value=0.0,
                value=float(FINISH_BONUS_K_DEFAULT),
                step=1.0,
                key=_K_KEY,
                help=(
                    "Coefficient on the finish signal (adjProj = base + K × "
                    "finish signal). Unvalidated; default 20. Inert until "
                    "method-of-victory odds are entered (a later slice)."
                ),
            )
        )

    # Stack toggle (design §14.3, slice C11a). The UI selector defaults to GPP
    # (the tournament default) — reject any lineup with both fighters of a bout —
    # even though the optimizer parameter defaults to cash. Cash allows same-fight
    # pairs (the original C3 behavior). The bout pairings come from the same parsed
    # slate the MOV-odds section uses; nothing here writes or fetches.
    stack_choice = st.radio(
        "Stack mode",
        options=[_STACK_GPP_LABEL, _STACK_CASH_LABEL],
        index=0,
        key=_STACK_MODE_KEY,
        horizontal=True,
        help=(
            "GPP (tournament): reject any lineup rostering both fighters of a "
            "bout (no same-fight pairs). Cash: allow same-fight pairs (the "
            "current behavior)."
        ),
    )
    stack_mode = (
        StackMode.GPP if stack_choice == _STACK_GPP_LABEL else StackMode.CASH
    )

    # Consensus odds paste path (design §13 C8). Two read-only paste boxes feed
    # the validated Classic odds modules to compute de-vigged median consensus
    # win probabilities; consensus is preferred and the manual moneylines below
    # are the fallback for any fighter consensus did not price. Nothing is
    # fetched or saved (no network, no DB) — paste only.
    consensus_by_norm = _render_consensus_section(eligible)

    st.markdown("#### Moneylines &amp; rounds", unsafe_allow_html=True)
    st.caption(
        "Enter each fighter's moneyline (American odds) and flag any 5-round "
        "title / main-event bout. Win probabilities are de-vigged per bout. "
        "Consensus odds (above), when computed, take precedence over a fighter's "
        "manual moneyline — the manual entry is the fallback."
    )

    moneyline_by_name: dict[str, int | None] = {}
    rounds_by_name: dict[str, int] = {}
    for bout in parsed.bouts:
        five = st.checkbox(
            f"{bout.fighter_1_name} vs {bout.fighter_2_name} — "
            "5-round bout (title / main event)",
            key=_five_round_key(bout.fighter_1_name, bout.fighter_2_name),
        )
        rounds = 5 if five else 3
        col1, col2 = st.columns(2)
        for column, fighter_name in (
            (col1, bout.fighter_1_name),
            (col2, bout.fighter_2_name),
        ):
            with column:
                raw = st.number_input(
                    f"{fighter_name} moneyline",
                    value=0,
                    step=5,
                    format="%d",
                    key=_ml_key(fighter_name),
                    help=(
                        "A non-zero American price, e.g. -150 (favorite) or "
                        "+180 (underdog). Leave 0 if unknown."
                    ),
                )
            moneyline_by_name[fighter_name] = (
                int(raw) if raw not in (None, 0) else None
            )
            rounds_by_name[fighter_name] = rounds

    # Resolve every eligible fighter's win prob (consensus preferred, manual
    # fallback — design §13 C8 step 4). A fighter priced by neither source is
    # "uncovered" and gates the Build button; a de-vig failure is surfaced.
    resolution = resolve_win_probs(
        eligible, parsed.bouts, moneyline_by_name, consensus_by_norm
    )
    for err in resolution.errors:
        st.error(err)
    missing = resolution.uncovered
    all_present = not missing and not resolution.errors

    # Method-of-victory odds input (design §14.1) — only for the Finish-aware
    # (MOV) method, so the Heuristic path stays byte-for-byte unchanged. Prices
    # each bout's finish signal via the C9 module (reused, never re-implemented);
    # a blank / malformed bout leaves its fighters on the base projection (§14.2).
    finish_signal_by_name: dict[str, float] = {}
    if method_name == FINISH_AWARE_METHOD_NAME:
        finish_signal_by_name = _render_mov_section(parsed.bouts, resolution)

    # Review gate IN SPIRIT (design §4 C5 MVP note): explicit acknowledgement,
    # no build until it is checked AND every rostered fighter has a resolved
    # win prob (from consensus or a manual moneyline).
    ack = st.checkbox(
        "I reviewed this slate — moneylines, 5-round bouts, and scratches "
        "are correct.",
        key=_ACK_KEY,
    )

    can_build = ack and all_present
    if not all_present:
        st.caption(
            "Enter a moneyline (or paste consensus odds) for every rostered "
            f"fighter to enable Build ({len(missing)} still missing)."
        )
    elif not ack:
        st.caption("Acknowledge your review above to enable Build.")

    clicked = st.button(
        BUILD_BTN_LABEL,
        key=_BUILD_BTN_KEY,
        type="primary",
        disabled=not can_build,
    )
    # Latch the build once it is triggered so it (and the captain-leverage view,
    # slice C11b) stays rendered across the reruns a pivot triggers — without the
    # latch only the single run of the click would render the build. The gate is
    # still enforced: can_build must hold to render, and an un-acked / incomplete
    # slate never builds.
    if clicked and can_build:
        st.session_state[_BUILT_FLAG_KEY] = True
    if not can_build or not st.session_state.get(_BUILT_FLAG_KEY):
        # Defense in depth: never build without the acknowledgement + a full
        # set of resolved win probs, even if a stale click slips through.
        return

    _render_build(
        parsed,
        eligible,
        resolution,
        rounds_by_name,
        method_name,
        finish_bonus_k,
        finish_signal_by_name,
        stack_mode,
    )


def _render_consensus_section(
    eligible: list[CaptainFighter],
) -> dict[str, ConsensusFighterPrice]:
    """Render the C8 consensus paste boxes + read-only preview (design §13 C8).

    Two paste boxes (BestFightOdds and DraftKings / multi-book) feed
    :func:`compute_captain_consensus`, which reuses the validated Classic odds
    modules. A read-only **Compute consensus odds** button triggers the blend;
    the result is previewed per fighter (consensus %, book count, low confidence,
    dispersion) and unmatched / unpaired fighters are surfaced, never silently
    dropped. Returns the ``normalize_name`` -> :class:`ConsensusFighterPrice` map
    used by the win-prob resolution. Writes nothing — no network, no DB.
    """
    st.markdown("#### Consensus odds (optional)", unsafe_allow_html=True)
    st.caption(
        "Paste a BestFightOdds block and/or a DraftKings / multi-book grid, then "
        "Compute consensus odds to get de-vigged median consensus win "
        "probabilities per fighter. Consensus is preferred; the manual "
        "moneylines below are the fallback for any fighter it did not price. "
        "Read-only — nothing is fetched or saved."
    )
    bfo_text = st.text_area(
        "Paste BestFightOdds (all-books event table HTML)",
        key=_BFO_PASTE_KEY,
        help=(
            "The copied BestFightOdds event odds table (all book columns). The "
            "validated all-books parser reads every book; nothing is fetched."
        ),
    )
    multibook_text = st.text_area(
        "Paste DraftKings / other books (tab-separated grid)",
        key=_MULTIBOOK_PASTE_KEY,
        help=(
            "A copied multi-book odds grid: a header row of book names and one "
            "row per fighter of American lines (a single DraftKings column is "
            "fine)."
        ),
    )
    if st.button("Compute consensus odds", key=_COMPUTE_KEY):
        st.session_state[_CONSENSUS_FLAG_KEY] = True

    has_paste = bool(bfo_text.strip() or multibook_text.strip())
    if not st.session_state.get(_CONSENSUS_FLAG_KEY) or not has_paste:
        if st.session_state.get(_CONSENSUS_FLAG_KEY) and not has_paste:
            st.caption("Paste at least one odds block above, then recompute.")
        return {}

    consensus = compute_captain_consensus(
        bestfightodds_text=bfo_text, multibook_text=multibook_text
    )
    _render_consensus_preview(consensus, eligible)
    return consensus.by_normalized


def _render_consensus_preview(
    consensus: CaptainConsensusResult,
    eligible: list[CaptainFighter],
) -> None:
    """Surface the blended consensus per fighter + every dropped/unmatched name."""
    for warning in consensus.parse_warnings:
        st.warning(warning)

    eligible_by_norm = {normalize_name(f.name): f.name for f in eligible}

    matched_rows: list[str] = []
    matched_norms: set[str] = set()
    for norm, price in consensus.by_normalized.items():
        slate_name = eligible_by_norm.get(norm)
        if slate_name is None:
            continue
        matched_norms.add(norm)
        conf = "low confidence" if price.low_confidence else "ok"
        dispersion = (
            f"{price.dispersion:.3f}" if price.dispersion is not None else "n/a"
        )
        matched_rows.append(
            "<li><b>"
            f"{html.escape(slate_name)}</b> — {_pct(price.win_prob)} consensus "
            f"win prob · {price.book_count} book(s) · {conf} · dispersion "
            f"{dispersion}</li>"
        )

    if matched_rows:
        st.markdown(
            '<div class="tsb-card">'
            '<div class="tsb-step">Consensus win probabilities</div>'
            f'<ul class="tsb-chipmsg">{"".join(matched_rows)}</ul>'
            "</div>",
            unsafe_allow_html=True,
        )
    else:
        st.info(
            "No pasted consensus fighter mapped to a slate fighter. The build "
            "will fall back to the manual moneylines below."
        )

    unmatched = [
        price.fighter_name
        for norm, price in consensus.by_normalized.items()
        if norm not in eligible_by_norm
    ]
    if unmatched:
        st.warning(
            "Consensus priced these fighters but they did not match any slate "
            "fighter (ignored — confirm spelling): " + ", ".join(unmatched) + "."
        )
    if consensus.unpaired:
        st.warning(
            "Could not pair these fighters in the pasted odds, so no consensus "
            "was computed for them: " + ", ".join(consensus.unpaired) + "."
        )

    fallback = [
        f.name
        for f in eligible
        if normalize_name(f.name) not in matched_norms
    ]
    if fallback:
        st.caption(
            "Falling back to the manual moneyline for: "
            + ", ".join(fallback)
            + "."
        )


def _render_slate_summary(
    fighters,
    bouts,
    out_fighters: list[CaptainFighter],
    unpaired: list[CaptainFighter],
) -> None:
    """Read-only fighters + bouts readout (design §5). Writes nothing."""
    bout_rows = "".join(
        "<li>"
        f"{html.escape(b.fighter_1_name)} vs {html.escape(b.fighter_2_name)}"
        "</li>"
        for b in bouts
    )
    st.markdown(
        '<div class="tsb-card">'
        '<div class="tsb-step">Parsed slate</div>'
        f'<div class="tsb-desc">{len(fighters)} fighter(s), {len(bouts)} '
        "bout(s) detected from the Captain CSV.</div>"
        f'<ul class="tsb-chipmsg">{bout_rows}</ul>'
        "</div>",
        unsafe_allow_html=True,
    )
    if out_fighters:
        st.warning(
            "Flagged out (excluded from lineups): "
            + ", ".join(f.name for f in out_fighters)
            + "."
        )
    if unpaired:
        st.warning(
            "Unpaired — no opponent found by Game Info, so not rosterable: "
            + ", ".join(f.name for f in unpaired)
            + ". Confirm the CSV pairs these fighters."
        )


def _odds_input(container, label: str, key: str) -> int | None:
    """One American-odds number input; 0 (the default) reads as "not entered".

    Mirrors the manual-moneyline input convention (a non-zero signed American
    price, else ``None``) so a blank field never fabricates a price.
    """
    raw = container.number_input(
        label,
        value=0,
        step=5,
        format="%d",
        key=key,
        help="American odds, e.g. -225 or +500. Leave 0 if unknown.",
    )
    return int(raw) if raw not in (None, 0) else None


def _render_fighter_mov(container, name: str) -> MethodOfVictoryOdds | None:
    """Render one fighter's KO/TKO + Submission + Decision inputs (tier-0 tree).

    Returns a :class:`MethodOfVictoryOdds` when *any* of the three is entered
    (missing fields stay ``None`` so a partial tree surfaces as a typed
    :class:`FinishSignalError` from C9 at compute time, never a fabricated
    price), or ``None`` when the fighter's tree is left entirely blank.
    """
    container.markdown(f"_{html.escape(name)}_")
    ko = _odds_input(container, f"{name} — KO/TKO", _mov_ko_key(name))
    sub = _odds_input(container, f"{name} — Submission", _mov_sub_key(name))
    dec = _odds_input(container, f"{name} — Decision", _mov_dec_key(name))
    if ko is None and sub is None and dec is None:
        return None
    return MethodOfVictoryOdds(ko_tko=ko, submission=sub, decision=dec)


def _render_mov_section(bouts, resolution: WinProbResolution) -> dict[str, float]:
    """Render the optional per-bout MOV-odds inputs and price each finish signal.

    Realizes ``docs/CAPTAIN_MODE_DESIGN.md`` §14.1 input — only rendered when the
    **Finish-aware (MOV)** method is selected, so Classic and the Heuristic path
    are byte-for-byte unchanged. Per bout the user may enter, per fighter, the
    KO/TKO + Submission + Decision tree (tier 0), and optionally a bout-level
    go-the-distance (No / Yes) or round-total (Under / Over) fallback (tiers 1 / 2).

    For each bout a :class:`~src.captain.finish_signal.FinishOddsBout` is assembled
    from the two fighters, their **existing moneyline de-vig win probabilities**
    (``resolution`` — ``win_prob`` stays the moneyline de-vig, §14.2), and the
    entered odds, then priced by :func:`compute_finish_signals` (C9 — **all** the
    de-vig / ladder math is reused, never re-implemented here). The tier C9 chose
    is surfaced so the user sees *how* each bout was priced.

    Graceful (§14.1 / C10): a bout with no odds is skipped (its fighters keep
    ``finish_signal=None`` → ``adjProj == baseProj``); a bout whose odds are
    malformed or partial raises :class:`FinishSignalError`, which is caught and
    shown as a clear per-bout message, again leaving those fighters on the base
    projection. Returns ``name -> finish_signal`` for the fighters successfully
    priced; writes nothing (no DB, no network).
    """
    st.markdown("#### Method-of-victory odds (optional)", unsafe_allow_html=True)
    st.caption(
        "Finish-aware only. Enter each fighter's KO/TKO, Submission, and Decision "
        "American odds (the method-of-victory tree) to activate the finish bonus "
        "(adjProj = base projection + K × finish signal). No full tree? Give a "
        "bout-level go-the-distance (No / Yes) or round-total (Under / Over) line "
        "instead. Win probabilities stay your moneyline de-vig. A bout left blank "
        "keeps its fighters on the base projection. Read-only — nothing is saved."
    )

    finish_by_name: dict[str, float] = {}
    for bout in bouts:
        a, b = bout.fighter_1_name, bout.fighter_2_name
        st.markdown(f"**{html.escape(a)} vs {html.escape(b)}**")
        col_a, col_b = st.columns(2)
        mov_a = _render_fighter_mov(col_a, a)
        mov_b = _render_fighter_mov(col_b, b)

        with st.expander(f"Fallback markets — {a} vs {b} (optional)"):
            dno_col, dyes_col = st.columns(2)
            distance_no = _odds_input(
                dno_col, "Go-the-distance: No", _bout_key("distno", a, b)
            )
            distance_yes = _odds_input(
                dyes_col, "Go-the-distance: Yes", _bout_key("distyes", a, b)
            )
            under_col, over_col = st.columns(2)
            round_total_under = _odds_input(
                under_col, "Round-total: Under", _bout_key("rtunder", a, b)
            )
            round_total_over = _odds_input(
                over_col, "Round-total: Over", _bout_key("rtover", a, b)
            )

        has_any = any(
            v is not None
            for v in (
                mov_a,
                mov_b,
                distance_no,
                distance_yes,
                round_total_under,
                round_total_over,
            )
        )
        if not has_any:
            # No odds for this bout — its fighters keep finish_signal=None (base).
            continue

        win_a = resolution.resolved.get(a)
        win_b = resolution.resolved.get(b)
        if win_a is None or win_b is None:
            st.caption(
                f"{a} vs {b}: enter both moneylines (or paste consensus) to price "
                "the finish signal."
            )
            continue

        try:
            signals = compute_finish_signals(
                FinishOddsBout(
                    fighter_a=a,
                    fighter_b=b,
                    win_prob_a=win_a.win_prob,
                    win_prob_b=win_b.win_prob,
                    mov_a=mov_a,
                    mov_b=mov_b,
                    distance_no=distance_no,
                    distance_yes=distance_yes,
                    round_total_under=round_total_under,
                    round_total_over=round_total_over,
                )
            )
        except FinishSignalError as exc:
            st.warning(
                f"{a} vs {b}: could not price the finish signal — {exc} These "
                "fighters use the base projection."
            )
            continue

        finish_by_name[a] = signals.fighter_a.finish_signal
        finish_by_name[b] = signals.fighter_b.finish_signal
        st.caption(
            f"{a} vs {b}: priced from {_TIER_LABELS[signals.tier]} odds — "
            f"{a} finish signal {_pct(signals.fighter_a.finish_signal)}, "
            f"{b} {_pct(signals.fighter_b.finish_signal)}."
        )

    return finish_by_name


def _render_build(
    parsed,
    eligible: list[CaptainFighter],
    resolution: WinProbResolution,
    rounds_by_name: dict[str, int],
    method_name: str,
    finish_bonus_k: float,
    finish_signal_by_name: dict[str, float],
    stack_mode: StackMode,
) -> None:
    """Resolve → project (selected method) → optimize → render lineups (§6/§7/§10).

    Pure read path: takes the already-resolved per-fighter win probs (consensus
    preferred, manual fallback — design §13 C8 step 4), runs the selected method
    and the brute-force optimizer, and renders the top lineups with deterministic
    reasoning. No DB, no persistence.

    The MOV finish-aware method (design §14.2) is built with the editable ``K``;
    every other engine comes straight from the registry. ``finish_signal_by_name``
    carries the per-fighter finish signals priced from the method-of-victory odds
    (the C9 module, via :func:`_render_mov_section`); a fighter absent from that
    map (no / malformed MOV odds, or the Heuristic path) gets ``finish_signal=None``
    and the finish-aware projection degrades to the base projection (§14.2).

    ``stack_mode`` (design §14.3) is passed to the optimizer with the slate's bout
    pairings as ``same_fight_pairs``: in GPP no lineup rosters both fighters of a
    bout; in cash both may appear (the original behavior). The mode is surfaced.
    """
    inputs: list[FighterProjectionInput] = []
    input_meta: dict[str, tuple[float, int]] = {}
    source_by_name: dict[str, str] = {}
    for fighter in eligible:
        resolved = resolution.resolved.get(fighter.name)
        if resolved is None:
            # Uncovered: the gate prevents reaching here, but never silently
            # build a fighter with no win prob.
            continue
        rounds = rounds_by_name.get(fighter.name, 3)
        inputs.append(
            FighterProjectionInput(
                name=fighter.name,
                base_salary=fighter.base_salary,
                captain_salary=fighter.captain_salary,
                win_prob=resolved.win_prob,
                scheduled_rounds=rounds,
                # Finish signal priced from the entered MOV odds (None when the
                # bout had no / malformed odds): no signal -> no bonus -> the
                # finish-aware projection equals the base projection (§14.2).
                finish_signal=finish_signal_by_name.get(fighter.name),
            )
        )
        input_meta[fighter.name] = (resolved.win_prob, rounds)
        source_by_name[fighter.name] = resolved.source

    # The finish-aware engine carries the editable K; all others are registry
    # singletons. Construct a fresh instance so the registered default K is never
    # mutated (design §14.2).
    if method_name == FINISH_AWARE_METHOD_NAME:
        method = FinishAwareMethod(finish_bonus_k=finish_bonus_k)
    else:
        method = get_method(method_name)
    candidates = method.project(inputs)

    # Bout pairings drive the GPP same-fight exclusion (design §14.3) — the same
    # per-bout grouping the MOV-odds section uses; ignored in cash mode.
    same_fight_pairs = [
        (bout.fighter_1_name, bout.fighter_2_name) for bout in parsed.bouts
    ]
    result = optimize_captain_lineups(
        candidates,
        top_n=_TOP_N,
        stack_mode=stack_mode,
        same_fight_pairs=same_fight_pairs,
    )

    # Facts carry the engine's OWN base projection (so the reasoning cites the
    # number the optimizer ranked, not a re-derived method-specific formula).
    facts_by_name: dict[str, _FighterFacts] = {}
    for candidate in candidates:
        win_prob, rounds = input_meta[candidate.name]
        facts_by_name[candidate.name] = _FighterFacts(
            name=candidate.name,
            win_prob=win_prob,
            scheduled_rounds=rounds,
            base_salary=candidate.base_salary,
            captain_salary=candidate.captain_salary,
            projection=candidate.projection,
            finish_signal=finish_signal_by_name.get(candidate.name),
            finish_bonus_k=finish_bonus_k,
        )

    if result.status is not CaptainOptimizerStatus.OK:
        st.warning(result.message)
        return

    st.markdown(
        f"### Top {len(result.lineups)} Captain lineups",
        unsafe_allow_html=True,
    )
    n_consensus = sum(1 for s in source_by_name.values() if s == "consensus")
    n_manual = sum(1 for s in source_by_name.values() if s == "manual")
    stack_label = (
        "GPP — no same-fight pairs"
        if stack_mode is StackMode.GPP
        else "Cash — same-fight pairs allowed"
    )
    st.caption(
        f"Built with the {method_label(method_name)} method"
        + (" — experimental, unvalidated" if is_experimental(method_name) else "")
        + f". Stack mode: {stack_label}."
        + f" Win-probability source: {n_consensus} consensus, {n_manual} manual"
        " moneyline. Research lineups only — read-only, nothing saved."
    )
    # Per-fighter win-prob source (design §13 C8 step 4 — show which source each
    # fighter used), surfaced and never silently chosen.
    with st.expander("Win-probability sources"):
        source_lines = [
            f"- {html.escape(name)}: {source_by_name[name]}"
            for name in sorted(source_by_name)
        ]
        st.markdown("\n".join(source_lines))
    for rank, lineup in enumerate(result.lineups, start=1):
        _render_lineup(rank, lineup, facts_by_name, method_name)

    # Captain-leverage view (design §14.4, slice C11b): rank captains by CPTproj
    # and let the user pin one — additive, below the free-EV build above.
    _render_captain_leverage(
        candidates,
        facts_by_name,
        stack_mode=stack_mode,
        same_fight_pairs=same_fight_pairs,
        method_name=method_name,
    )


def _render_captain_leverage(
    candidates,
    facts_by_name: dict[str, _FighterFacts],
    *,
    stack_mode: StackMode,
    same_fight_pairs: list[tuple[str, str]],
    method_name: str,
) -> None:
    """Render the captain-leverage view (design §14.4, slice C11b).

    Ranks every candidate as Captain by ``CPTproj = 1.5 × adjProj`` (via
    :func:`src.captain.optimizer.rank_captains_by_cptproj` — pure selection logic,
    no projection is recomputed), shows the ranked list (captain · CPTproj · that
    captain's best-lineup total **in the current stack mode**), and lets the user
    pin a captain to rebuild. This is the **ceiling / leverage** view: in GPP pure
    EV captains the cheapest salary-efficient fighter (the free-EV build above), so
    to play the finish-favorite *as Captain* the user pins it here (§14.4). The
    pin selectbox **defaults to the top** CPTproj captain; picking another rebuilds
    with that fighter pinned. Distinct from the free-EV build; writes nothing.
    """
    rankings = rank_captains_by_cptproj(
        candidates,
        stack_mode=stack_mode,
        same_fight_pairs=same_fight_pairs,
    )
    if not rankings:
        return

    st.markdown(
        "### Captain leverage — rank by ceiling (CPTproj)",
        unsafe_allow_html=True,
    )
    st.caption(
        "Captains ranked by CPTproj = 1.5 × the (adjusted) projection — the "
        "fighter's own points as Captain. This is the leverage / ceiling view: in "
        "GPP pure EV captains the cheapest salary-efficient fighter (the free-EV "
        "build above), so to play the finish-favorite as Captain, pin it here. A "
        "win-probability floor does not reproduce this pick. Distinct from the "
        "free-EV build; read-only, nothing saved."
    )

    rank_rows = []
    for entry in rankings:
        total = (
            f"{entry.best_total:.1f} pts"
            if entry.best_total is not None
            else "no feasible lineup"
        )
        rank_rows.append(
            "<li><b>"
            f"{html.escape(entry.captain_name)}</b> — CPTproj "
            f"{entry.cptproj:.1f} · best lineup {total}</li>"
        )
    st.markdown(
        '<div class="tsb-card">'
        '<div class="tsb-step">CPTproj ranking</div>'
        f'<ul class="tsb-chipmsg">{"".join(rank_rows)}</ul>'
        "</div>",
        unsafe_allow_html=True,
    )

    pin_options = [entry.captain_name for entry in rankings]
    pinned = st.selectbox(
        "Pin a Captain (defaults to the top CPTproj)",
        options=pin_options,
        index=0,
        key=_CAPTAIN_PIN_KEY,
        help=(
            "Build the best lineup(s) with this fighter as Captain (the leverage "
            "pick). The list is ranked by CPTproj; the top is selected by default."
        ),
    )

    result = optimize_captain_lineups(
        candidates,
        top_n=_TOP_N,
        stack_mode=stack_mode,
        same_fight_pairs=same_fight_pairs,
        captain=pinned,
    )
    if result.status is not CaptainOptimizerStatus.OK:
        st.warning(result.message)
        return

    stack_label = (
        "GPP — no same-fight pairs"
        if stack_mode is StackMode.GPP
        else "Cash — same-fight pairs allowed"
    )
    st.caption(
        f"Pinned Captain: {pinned} (CPTproj leverage). Stack mode: {stack_label}. "
        "Trades EV for ceiling vs the free-EV build above."
    )
    for rank, lineup in enumerate(result.lineups, start=1):
        _render_lineup(rank, lineup, facts_by_name, method_name)


def _render_lineup(
    rank: int,
    lineup: CaptainLineup,
    facts_by_name: dict[str, _FighterFacts],
    method_name: str,
) -> None:
    """Render one lineup card + its deterministic "Why this lineup?" reasoning."""
    # Captain row + five flex rows (explicit so the 1.5× cost is clear).
    cap_facts = facts_by_name[lineup.captain_name]
    rows = [
        "<li><b>CPT</b> "
        f"{html.escape(lineup.captain_name)} — "
        f"{_money(cap_facts.captain_salary)} (1.5×)</li>"
    ]
    for flex_name in lineup.flex_names:
        rows.append(
            "<li>F "
            f"{html.escape(flex_name)} — "
            f"{_money(facts_by_name[flex_name].base_salary)}</li>"
        )
    st.markdown(
        '<div class="tsb-card">'
        f'<div class="tsb-step">Lineup {rank}</div>'
        f'<div class="tsb-okrow tsb-ready"><span class="tsb-dot"></span>'
        f"{html.escape(lineup.captain_name)} (CPT) · "
        f"{_money(lineup.salary)} · {lineup.points:.1f} pts</div>"
        f'<ul class="tsb-chipmsg">{"".join(rows)}</ul>'
        "</div>",
        unsafe_allow_html=True,
    )
    reasoning = build_captain_reasoning(
        lineup, facts_by_name, rank=rank, method_name=method_name
    )
    with st.expander(f"Why this lineup? (Lineup {rank})"):
        st.markdown("\n".join(f"- {html.escape(line)}" for line in reasoning))
