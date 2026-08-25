"""Phase A′ calibration harness — pure, read-only MAE of v0 vs v2 (the §12 gate).

Realizes ``docs/PROJECTION_V2_METHOD_AWARE_DESIGN.md`` §12 / §12.1 (the *locked*
Phase A′ calibration decisions D1–D5). It compares the v0 default formula
(``default_projection``) against the Tier-0 finish-aware v2 model
(``compute_finish_projection``) on **realized** DK points read from a local CSV,
and reports the hard promotion gate.

This module is the **gate** that decides whether v2 deserves to be carried
further. A **failing** gate blocks promotion; a **passing** gate is a necessary
precondition only — it does **not** promote v2 or change the ``docs/DEVELOPMENT_NOTES.md`` §4 v0
formula. v0 remains the default engine and promotion stays a separate, explicit
user decision (design §12).

PURITY / SCOPE (design §12.1, ``docs/DEVELOPMENT_NOTES.md`` §1):
  - No DB, no Streamlit, no service / optimizer / projection-service wiring.
  - The only I/O is reading the input CSV in :func:`load_calibration_csv`. The
    comparator (:func:`compare_engines`) and the :class:`CalibrationReport` are
    pure data — no printing, no side effects, no promotion.

THE LOCKED GATE (design §12.1):
  - **D2′ combined hard gate.** A′ passes iff
    ``pooled_mae_v2 <= pooled_mae_v0`` (D2) **AND** ``variance_ratio <= 1.05``
    (D1). Both arms are objective and binding.
  - **D1 variance arm.** Per-row residual = ``projection - realized_dk_points``
    (signed). ``variance_ratio = Var(residual_v2) / Var(residual_v0)`` using
    **sample variance (ddof = 1)** over the pooled residuals.
  - **D2 MAE arm.** Pooled MAE = mean of ``|residual|`` over all included rows.
    The MAE arm is a hard gate — there is no subjective MAE allowance. Per-slate
    MAE is reported as a **diagnostic only** and never gates.
  - **D3 missing-row policy (skip-unless-both-valid).** A row is included only
    when ``implied_win_probability ∈ [0, 1]``, ``salary`` is numeric,
    ``scheduled_rounds ∈ {3, 5}``, and ``realized_dk_points`` is numeric. Any row
    failing the predicate is skipped, **counted, and reported with a reason** —
    never silently dropped. Both engines are scored on the same included set.

INPUT (design §12.1 D4): a CSV with required header
``slate, fighter, implied_win_probability, salary, scheduled_rounds,
realized_dk_points``. Optional ``p_fight_finishes`` / ``finish_share`` columns may
appear but are **ignored at Tier 0** (the model uses league defaults).
  - **Real calibration data:** ``data/calibration/realized_points.csv`` — must
    remain **gitignored / local**, never committed (``docs/DEVELOPMENT_NOTES.md`` §7). See
    :data:`REAL_CALIBRATION_CSV_PATH`.
  - **Committed synthetic template:** ``data/calibration/realized_points.sample.csv``
    (synthetic rows only — no real results). See :data:`SAMPLE_CALIBRATION_CSV_PATH`.
"""

from __future__ import annotations

import csv
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from src.projections.default_projection import default_projection
from src.projections.finish_model import (
    STATUS_OK,
    VALID_SCHEDULED_ROUNDS,
    compute_finish_projection,
)

# --- Locked gate constants (design §12.1) -------------------------------------
# D1: v2 may not inflate pooled error variance by more than 5%.
VARIANCE_RATIO_MAX = 1.05
# Sample variance (ddof=1) is undefined for fewer than two data points.
MIN_ROWS_FOR_VARIANCE = 2

# --- CSV contract (design §12.1 D4) -------------------------------------------
REQUIRED_COLUMNS = (
    "slate",
    "fighter",
    "implied_win_probability",
    "salary",
    "scheduled_rounds",
    "realized_dk_points",
)
# Documentation-only path constants. The real CSV is GITIGNORED and never
# committed (docs/DEVELOPMENT_NOTES.md §7); only the synthetic sample is checked in.
REAL_CALIBRATION_CSV_PATH = "data/calibration/realized_points.csv"
SAMPLE_CALIBRATION_CSV_PATH = "data/calibration/realized_points.sample.csv"

# --- Report status codes ------------------------------------------------------
STATUS_OK_REPORT = "ok"
STATUS_INSUFFICIENT = "insufficient_data"

# --- Skip-reason codes (design §12.1 D3) --------------------------------------
# A skipped row is tagged with the FIRST failing condition in the fixed
# precedence order below, so the per-row reasons partition the skipped set and
# ``skip_reason_counts`` sums exactly to ``n_skipped``.
SKIP_WIN_PROB = "win_probability_invalid"
SKIP_SALARY = "salary_invalid"
SKIP_ROUNDS = "scheduled_rounds_invalid"
SKIP_REALIZED = "realized_points_invalid"


# --- Typed rows ---------------------------------------------------------------

@dataclass(frozen=True)
class CalibrationInputRow:
    """A validated CSV row that passes the D3 inclusion predicate (both engines
    computable + realized points present)."""

    slate: str
    fighter: str
    implied_win_probability: float
    salary: float
    scheduled_rounds: int
    realized_dk_points: float


@dataclass(frozen=True)
class SkippedRow:
    """A CSV row excluded from the comparison, with its reason — design §12.1 D3
    requires skips to be counted and reasoned, never silently dropped."""

    line_number: int  # 1-based CSV line (the header is line 1).
    slate: str
    fighter: str
    reason: str  # one of the SKIP_* codes above.
    detail: str = ""  # human-readable specifics (the offending field/value).


@dataclass(frozen=True)
class LoadResult:
    """Output of :func:`load_calibration_csv`: the included rows + skip records."""

    rows: tuple[CalibrationInputRow, ...]
    skipped: tuple[SkippedRow, ...]
    total_rows: int  # data rows seen (header + blank lines excluded).


@dataclass(frozen=True)
class SlateMae:
    """Per-slate MAE diagnostic (design §12.1 D2 — diagnostic only, never gates)."""

    mae_v0: float
    mae_v2: float
    n: int


@dataclass(frozen=True)
class CalibrationReport:
    """The Phase A′ calibration result (design §12.1 output contract).

    Data only — no printing, no I/O, no promotion side effect. When
    ``status == "insufficient_data"`` the gate cannot be evaluated honestly and
    ``passes_gate`` is ``False`` (the harness never invents confidence).
    """

    status: str  # STATUS_OK_REPORT | STATUS_INSUFFICIENT
    # --- MAE arm (D2) ---
    mae_v0_pooled: float | None
    mae_v2_pooled: float | None
    mae_delta: float | None  # v2 - v0 (negative => v2 better). Reported pass OR fail.
    mae_v2_le_v0: bool  # the D2 MAE-gate arm (a.k.a. ``passes_mae_gate``).
    # --- Variance arm (D1) ---
    variance_ratio: float | None  # Var(res_v2)/Var(res_v0), ddof=1. ``inf`` if v0 has
    #                               zero residual variance but v2 does not.
    passes_variance_gate: bool
    # --- Combined hard gate (D2′) ---
    passes_gate: bool  # mae_v2_le_v0 AND passes_variance_gate (False if insufficient).
    # --- Diagnostics / accounting ---
    per_slate: dict[str, SlateMae]
    total_rows: int
    n_included: int
    n_skipped: int
    skip_reason_counts: dict[str, int]
    skipped: tuple[SkippedRow, ...]

    @property
    def passes_mae_gate(self) -> bool:
        """Alias for :attr:`mae_v2_le_v0` — the design §12.1 D2 MAE-gate arm."""
        return self.mae_v2_le_v0


# --- CSV loading / validation (design §12.1 D4) -------------------------------

def load_calibration_csv(csv_path: str | Path) -> LoadResult:
    """Parse and validate a calibration CSV into included rows + skip records.

    Enforces the D3 net inclusion predicate (the apples-to-apples condition under
    which BOTH engines yield a valid projection): ``implied_win_probability ∈
    [0, 1]``, ``salary`` numeric, ``scheduled_rounds ∈ {3, 5}``,
    ``realized_dk_points`` numeric. Optional finish-signal columns are tolerated
    and ignored (Tier 0). Blank lines are skipped and not counted.

    Raises ``ValueError`` if a required column is absent from the header (a
    structural file error, not a per-row skip).
    """
    path = Path(csv_path)
    rows: list[CalibrationInputRow] = []
    skipped: list[SkippedRow] = []
    total = 0
    # utf-8-sig transparently strips a leading BOM from spreadsheet exports
    # (Excel / Sheets) so the first column name still matches; a no-op otherwise.
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [col for col in REQUIRED_COLUMNS if col not in fieldnames]
        if missing:
            raise ValueError(
                "calibration CSV missing required column(s): " + ", ".join(missing)
            )
        for raw in reader:
            if _is_blank(raw):
                continue
            total += 1
            parsed, skip = _parse_row(raw, reader.line_num)
            if skip is not None:
                skipped.append(skip)
            else:
                rows.append(parsed)  # type: ignore[arg-type]
    return LoadResult(rows=tuple(rows), skipped=tuple(skipped), total_rows=total)


def _is_blank(raw: dict[str, str | None]) -> bool:
    return all(v is None or str(v).strip() == "" for v in raw.values())


def _parse_row(
    raw: dict[str, str | None], line_number: int
) -> tuple[CalibrationInputRow | None, SkippedRow | None]:
    slate = (raw.get("slate") or "").strip()
    fighter = (raw.get("fighter") or "").strip()

    p = _parse_float(raw.get("implied_win_probability"))
    if p is None or not 0.0 <= p <= 1.0:
        return None, SkippedRow(
            line_number, slate, fighter, SKIP_WIN_PROB,
            _detail("implied_win_probability", raw.get("implied_win_probability")),
        )

    salary = _parse_float(raw.get("salary"))
    if salary is None:
        return None, SkippedRow(
            line_number, slate, fighter, SKIP_SALARY,
            _detail("salary", raw.get("salary")),
        )

    rounds = _parse_rounds(raw.get("scheduled_rounds"))
    if rounds is None:
        return None, SkippedRow(
            line_number, slate, fighter, SKIP_ROUNDS,
            _detail("scheduled_rounds", raw.get("scheduled_rounds")),
        )

    realized = _parse_float(raw.get("realized_dk_points"))
    if realized is None:
        return None, SkippedRow(
            line_number, slate, fighter, SKIP_REALIZED,
            _detail("realized_dk_points", raw.get("realized_dk_points")),
        )

    return (
        CalibrationInputRow(slate, fighter, p, salary, rounds, realized),
        None,
    )


def _parse_float(value: str | None) -> float | None:
    """Parse a CSV cell to a *finite* float, or ``None`` if missing / non-numeric.

    ``float()`` would happily accept ``"nan"`` / ``"inf"`` / ``"-inf"``; a
    non-finite value is treated as non-numeric (``None``) so the D3 predicate
    skips + counts it (design §12.1 D3) rather than letting NaN/inf silently
    poison the pooled MAE / variance, and so ``_parse_rounds`` never reaches
    ``int(float("nan"))`` (which would raise and abort the whole load).
    """
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_rounds(value: str | None) -> int | None:
    parsed = _parse_float(value)
    if parsed is None or parsed != int(parsed):
        return None
    rounds = int(parsed)
    return rounds if rounds in VALID_SCHEDULED_ROUNDS else None


def _detail(field: str, value: str | None) -> str:
    return f"{field}={value!r}"


# --- Comparator (design §12.1 D5 + output contract) ---------------------------

def compare_engines(load_result: LoadResult) -> CalibrationReport:
    """Run both engines over the included rows and return the gate report.

    Pure: consumes already-loaded rows (no I/O). Computes the signed residuals,
    pooled + per-slate MAE, the ddof=1 variance ratio, and the D2′ combined hard
    gate. With fewer than :data:`MIN_ROWS_FOR_VARIANCE` included rows the variance
    arm is undefined, so the report is ``status == "insufficient_data"`` and
    ``passes_gate`` is ``False`` (design §12.1: never invent confidence).
    """
    skipped = load_result.skipped
    n_skipped = len(skipped)
    skip_reason_counts = dict(Counter(s.reason for s in skipped))
    total_rows = load_result.total_rows

    residuals_v0: list[float] = []
    residuals_v2: list[float] = []
    abs_v0: list[float] = []
    abs_v2: list[float] = []
    per_slate_abs: dict[str, tuple[list[float], list[float]]] = {}

    for row in load_result.rows:
        proj_v0 = default_projection(
            row.implied_win_probability, row.salary, row.scheduled_rounds
        )
        v2 = compute_finish_projection(
            row.implied_win_probability, row.scheduled_rounds
        )
        # The D3 predicate guarantees v2 is 'ok'. If this ever fires, the loader
        # predicate and the engine have drifted apart — fail loud rather than
        # silently mis-score (design §12.1 D3: never silently drop).
        if v2.projection_status != STATUS_OK or v2.projected_dk_points is None:
            raise ValueError(
                f"included row unexpectedly failed the v2 engine: "
                f"{row.slate}/{row.fighter} (status={v2.projection_status})"
            )
        proj_v2 = v2.projected_dk_points

        r0 = proj_v0 - row.realized_dk_points
        r2 = proj_v2 - row.realized_dk_points
        residuals_v0.append(r0)
        residuals_v2.append(r2)
        abs_v0.append(abs(r0))
        abs_v2.append(abs(r2))
        slate_v0, slate_v2 = per_slate_abs.setdefault(row.slate, ([], []))
        slate_v0.append(abs(r0))
        slate_v2.append(abs(r2))

    n_included = len(load_result.rows)
    per_slate = {
        slate: SlateMae(
            mae_v0=statistics.mean(slate_v0),
            mae_v2=statistics.mean(slate_v2),
            n=len(slate_v0),
        )
        for slate, (slate_v0, slate_v2) in per_slate_abs.items()
    }

    if n_included == 0:
        return CalibrationReport(
            status=STATUS_INSUFFICIENT,
            mae_v0_pooled=None,
            mae_v2_pooled=None,
            mae_delta=None,
            mae_v2_le_v0=False,
            variance_ratio=None,
            passes_variance_gate=False,
            passes_gate=False,
            per_slate=per_slate,
            total_rows=total_rows,
            n_included=0,
            n_skipped=n_skipped,
            skip_reason_counts=skip_reason_counts,
            skipped=skipped,
        )

    mae_v0 = statistics.mean(abs_v0)
    mae_v2 = statistics.mean(abs_v2)
    mae_delta = mae_v2 - mae_v0
    mae_v2_le_v0 = mae_v2 <= mae_v0

    if n_included < MIN_ROWS_FOR_VARIANCE:
        # MAE is computable + reported, but the variance arm is undefined.
        return CalibrationReport(
            status=STATUS_INSUFFICIENT,
            mae_v0_pooled=mae_v0,
            mae_v2_pooled=mae_v2,
            mae_delta=mae_delta,
            mae_v2_le_v0=mae_v2_le_v0,
            variance_ratio=None,
            passes_variance_gate=False,
            passes_gate=False,
            per_slate=per_slate,
            total_rows=total_rows,
            n_included=n_included,
            n_skipped=n_skipped,
            skip_reason_counts=skip_reason_counts,
            skipped=skipped,
        )

    variance_ratio = _variance_ratio(residuals_v0, residuals_v2)
    passes_variance_gate = _passes_variance(variance_ratio)
    passes_gate = mae_v2_le_v0 and passes_variance_gate

    return CalibrationReport(
        status=STATUS_OK_REPORT,
        mae_v0_pooled=mae_v0,
        mae_v2_pooled=mae_v2,
        mae_delta=mae_delta,
        mae_v2_le_v0=mae_v2_le_v0,
        variance_ratio=variance_ratio,
        passes_variance_gate=passes_variance_gate,
        passes_gate=passes_gate,
        per_slate=per_slate,
        total_rows=total_rows,
        n_included=n_included,
        n_skipped=n_skipped,
        skip_reason_counts=skip_reason_counts,
        skipped=skipped,
    )


def _variance_ratio(residuals_v0: list[float], residuals_v2: list[float]) -> float:
    """``Var(res_v2) / Var(res_v0)`` using sample variance (ddof=1, design D1).

    Requires at least :data:`MIN_ROWS_FOR_VARIANCE` data points (the caller
    guards). When v0 has zero residual variance: returns ``1.0`` if v2 is also
    zero-variance (no inflation), else ``math.inf`` (v2 introduced variance where
    v0 had none — the variance gate then fails honestly).

    Note: numerator and denominator always share the same ``n`` (the two residual
    lists are appended in lockstep over the identical D3-included set), so the
    ddof ``n/(n-1)`` factor cancels and this RATIO is ddof-invariant. ``ddof=1``
    is kept to honor the design §12.1 D1 text verbatim; do not "simplify" it to
    ``pvariance`` — it would not change any gate result, but it would diverge from
    the locked spec.
    """
    var_v0 = statistics.variance(residuals_v0)  # ddof=1 sample variance.
    var_v2 = statistics.variance(residuals_v2)
    if var_v0 == 0.0:
        return 1.0 if var_v2 == 0.0 else math.inf
    return var_v2 / var_v0


def _passes_variance(variance_ratio: float | None) -> bool:
    """The D1 variance gate: ``variance_ratio <= 1.05`` (``inf`` / ``None`` fail)."""
    return variance_ratio is not None and variance_ratio <= VARIANCE_RATIO_MAX


# --- Convenience runner -------------------------------------------------------

def run_calibration(csv_path: str | Path) -> CalibrationReport:
    """Load ``csv_path`` and return its :class:`CalibrationReport` (load → compare)."""
    return compare_engines(load_calibration_csv(csv_path))
