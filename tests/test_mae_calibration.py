"""Tests for the Phase A′ calibration harness (design §12 / §12.1).

Covers the locked A′ decisions D1–D5: the combined hard gate (pooled MAE arm +
ddof=1 variance arm), the skip-unless-both-valid row policy, per-slate MAE as a
diagnostic (never gating), insufficient-data handling, and that the committed
synthetic sample loads. No real calibration data file is required — every test
drives synthetic CSVs (a tmp file or the committed sample).

Residuals are controlled indirectly: both engines are pure, so for a chosen
``(p_win, salary, scheduled_rounds)`` the test computes the real v0 / v2
projections and sets ``realized_dk_points`` to hit a target signed residual.
``residual_v2 - residual_v0`` is the fixed engine gap ``d = proj_v2 - proj_v0``;
holding inputs constant pins the variance ratio at 1.0 (isolating the MAE arm),
while two distinct input groups let the variance arm move independently.
"""

import csv
import math
from pathlib import Path

import pytest

from src.projections import mae_calibration as mc
from src.projections.default_projection import default_projection
from src.projections.finish_model import compute_finish_projection

# Two input groups with distinct, same-sign engine gaps d = proj_v2 - proj_v0
# (v2 > v0 in both of these ranges), used to drive the variance arm.
GROUP_A = (0.60, 8000, 3)
GROUP_B = (0.40, 7000, 3)


# --- helpers ------------------------------------------------------------------

def _proj_v0(p, salary, rounds):
    return default_projection(p, salary, rounds)


def _proj_v2(p, rounds):
    return compute_finish_projection(p, rounds).projected_dk_points


def _row_for_residual_v0(slate, fighter, inputs, residual_v0):
    """A CSV row dict whose v0 residual (proj_v0 - realized) equals ``residual_v0``."""
    p, salary, rounds = inputs
    realized = _proj_v0(p, salary, rounds) - residual_v0
    return {
        "slate": slate,
        "fighter": fighter,
        "implied_win_probability": p,
        "salary": salary,
        "scheduled_rounds": rounds,
        "realized_dk_points": realized,
    }


def _row_for_residual_v2(slate, fighter, inputs, residual_v2):
    """A CSV row dict whose v2 residual (proj_v2 - realized) equals ``residual_v2``."""
    p, salary, rounds = inputs
    realized = _proj_v2(p, rounds) - residual_v2
    return {
        "slate": slate,
        "fighter": fighter,
        "implied_win_probability": p,
        "salary": salary,
        "scheduled_rounds": rounds,
        "realized_dk_points": realized,
    }


def _gap(inputs):
    p, salary, rounds = inputs
    return _proj_v2(p, rounds) - _proj_v0(p, salary, rounds)


def _write_csv(path, rows, columns=None, header=None):
    columns = columns or list(mc.REQUIRED_COLUMNS)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header if header is not None else columns)
        for row in rows:
            writer.writerow([row.get(col, "") for col in columns])
    return path


def _report(tmp_path, rows, columns=None):
    path = _write_csv(tmp_path / "calib.csv", rows, columns=columns)
    return mc.run_calibration(path)


# --- D2′ combined gate: the four quadrants ------------------------------------

def test_gate_passes_when_v2_better_on_both_mae_and_variance(tmp_path):
    # v2 nearly perfect (small residuals), v0 far off => mae_v2 << mae_v0 and
    # var_v2 << var_v0. Two groups so the residuals genuinely vary (var > 0).
    eps = [1.0, -1.0, 1.0, -1.0]
    inputs = [GROUP_A, GROUP_A, GROUP_B, GROUP_B]
    rows = [
        _row_for_residual_v2("S", f"f{i}", inp, e)
        for i, (inp, e) in enumerate(zip(inputs, eps))
    ]
    report = _report(tmp_path, rows)

    assert report.status == mc.STATUS_OK_REPORT
    assert report.mae_v2_pooled < report.mae_v0_pooled
    assert report.mae_v2_le_v0 is True
    assert report.variance_ratio <= mc.VARIANCE_RATIO_MAX
    assert report.passes_variance_gate is True
    assert report.passes_gate is True


def test_gate_fails_on_mae_even_when_variance_ok(tmp_path):
    # Identical inputs => the engine gap d is constant => adding it shifts v2
    # residuals by a constant => variance ratio is exactly 1.0 (variance OK).
    # v0 nearly perfect, so v2 (off by ~d) loses the MAE arm.
    residuals_v0 = [-1.0, 1.0, -1.0, 1.0]
    rows = [
        _row_for_residual_v0("S", f"f{i}", GROUP_A, r)
        for i, r in enumerate(residuals_v0)
    ]
    report = _report(tmp_path, rows)

    assert report.mae_v2_pooled > report.mae_v0_pooled
    assert report.mae_v2_le_v0 is False
    assert math.isclose(report.variance_ratio, 1.0, abs_tol=1e-9)
    assert report.passes_variance_gate is True  # variance arm alone is fine...
    assert report.passes_gate is False  # ...but the combined gate fails on MAE.


def test_gate_fails_on_variance_even_when_mae_ok(tmp_path):
    # Two groups with distinct same-sign gaps. Pin every v0 residual to a constant
    # c = -(d_A + d_B)/2 (plus a tiny ± delta so var_v0 > 0 and the ratio is
    # finite). Then |v2 residual| = |d_A - d_B|/2 < |c| = mae_v0 (same-sign gaps),
    # so MAE passes, while the per-group gap split inflates var_v2 past tolerance.
    d_a, d_b = _gap(GROUP_A), _gap(GROUP_B)
    assert d_a * d_b > 0  # same sign — the MAE-OK construction requires it.
    c = -(d_a + d_b) / 2.0
    delta = 1.0
    specs = [
        (GROUP_A, c + delta),
        (GROUP_A, c - delta),
        (GROUP_B, c + delta),
        (GROUP_B, c - delta),
    ]
    rows = [
        _row_for_residual_v0("S", f"f{i}", inp, r)
        for i, (inp, r) in enumerate(specs)
    ]
    report = _report(tmp_path, rows)

    assert report.mae_v2_le_v0 is True  # MAE arm OK...
    assert report.variance_ratio > mc.VARIANCE_RATIO_MAX  # ...variance arm too high.
    assert math.isfinite(report.variance_ratio)  # finite, not the degenerate inf path.
    assert report.passes_variance_gate is False
    assert report.passes_gate is False


def test_gate_fails_on_both_arms(tmp_path):
    # v0 perfect on every row (residual 0) => var_v0 = 0, mae_v0 = 0. v2 is off by
    # the (varying) gap => mae_v2 > 0 (MAE fails) and var_v0 = 0 with var_v2 > 0
    # => ratio is inf (variance fails).
    inputs = [GROUP_A, GROUP_A, GROUP_B, GROUP_B]
    rows = [
        _row_for_residual_v0("S", f"f{i}", inp, 0.0)
        for i, inp in enumerate(inputs)
    ]
    report = _report(tmp_path, rows)

    assert report.mae_v2_le_v0 is False
    assert report.passes_variance_gate is False
    assert report.variance_ratio == math.inf
    assert report.passes_gate is False


# --- D2 MAE math + boundary ---------------------------------------------------

def test_pooled_mae_and_delta_match_independent_recomputation(tmp_path):
    specs = [
        (GROUP_A, -5.0),
        (GROUP_A, 3.0),
        (GROUP_B, -2.0),
        (GROUP_B, 8.0),
    ]
    rows = [
        _row_for_residual_v0("S", f"f{i}", inp, r)
        for i, (inp, r) in enumerate(specs)
    ]
    report = _report(tmp_path, rows)

    # Recompute residuals independently from the raw rows.
    res_v0, res_v2 = [], []
    for row in rows:
        p, salary, rounds = (
            row["implied_win_probability"],
            row["salary"],
            row["scheduled_rounds"],
        )
        realized = row["realized_dk_points"]
        res_v0.append(_proj_v0(p, salary, rounds) - realized)
        res_v2.append(_proj_v2(p, rounds) - realized)
    exp_mae_v0 = sum(abs(x) for x in res_v0) / len(res_v0)
    exp_mae_v2 = sum(abs(x) for x in res_v2) / len(res_v2)

    assert math.isclose(report.mae_v0_pooled, exp_mae_v0, abs_tol=1e-9)
    assert math.isclose(report.mae_v2_pooled, exp_mae_v2, abs_tol=1e-9)
    assert math.isclose(report.mae_delta, exp_mae_v2 - exp_mae_v0, abs_tol=1e-9)
    assert report.passes_mae_gate == report.mae_v2_le_v0  # alias property.


def test_mae_arm_uses_le_so_a_tie_passes(tmp_path):
    # realized = proj_v0 on every row => v0 residual 0 => v0 mae 0; pin the v2
    # residual to 0 too (realized must equal both, so pick inputs where v0 == v2).
    # Instead, force an exact tie: choose realized so |res_v0| == |res_v2| via the
    # symmetric residual res_v0 = -d/2 (=> res_v2 = +d/2, equal magnitudes).
    d = _gap(GROUP_A)
    rows = [
        _row_for_residual_v0("S", "f0", GROUP_A, -d / 2.0),
        _row_for_residual_v0("S", "f1", GROUP_A, -d / 2.0),
    ]
    report = _report(tmp_path, rows)
    assert math.isclose(report.mae_v0_pooled, report.mae_v2_pooled, abs_tol=1e-9)
    assert report.mae_v2_le_v0 is True  # exact tie passes the <= MAE arm.


def test_variance_gate_boundary_is_inclusive():
    # The D1 variance gate is variance_ratio <= 1.05 (inclusive). Probe the pure
    # threshold helper directly so the boundary is pinned exactly.
    assert mc.VARIANCE_RATIO_MAX == 1.05
    assert mc._passes_variance(1.05) is True
    assert mc._passes_variance(1.05 + 1e-9) is False
    assert mc._passes_variance(math.inf) is False
    assert mc._passes_variance(None) is False


def test_variance_ratio_is_variance_based_not_mean_absolute_deviation():
    # The arm must use VARIANCE (squared deviations), not a MAD-style spread:
    # doubling the spread quadruples the variance => ratio 4.0 (a MAD ratio would
    # be 2.0). This catches a variance->abs-deviation regression.
    assert math.isclose(mc._variance_ratio([-1.0, 1.0], [-2.0, 2.0]), 4.0, abs_tol=1e-12)
    # v0 zero-variance, v2 zero-variance => no inflation => 1.0.
    assert mc._variance_ratio([3.0, 3.0], [7.0, 7.0]) == 1.0
    # v0 zero-variance, v2 has spread => inf (gate fails).
    assert mc._variance_ratio([3.0, 3.0], [3.0, 5.0]) == math.inf


def test_variance_ratio_is_ddof_invariant_at_equal_n():
    # Design §12.1 D1 names sample variance (ddof=1), but numerator and
    # denominator always share the same n, so the ratio is ddof-INVARIANT (the
    # n/(n-1) factor cancels). This pins that documented invariance: a population
    # variance (ddof=0) would yield the same ratio. The module keeps ddof=1 to
    # honor the locked text verbatim; no test can (or should) claim a ddof=1
    # ratio differs from ddof=0 here, because mathematically it does not.
    import statistics

    res_v0 = [0.0, 0.0, 3.0]
    res_v2 = [0.0, 0.0, 6.0]
    sample_ratio = statistics.variance(res_v2) / statistics.variance(res_v0)
    pop_ratio = statistics.pvariance(res_v2) / statistics.pvariance(res_v0)
    assert math.isclose(sample_ratio, pop_ratio, abs_tol=1e-12)
    assert math.isclose(mc._variance_ratio(res_v0, res_v2), sample_ratio, abs_tol=1e-12)


# --- D2 per-slate diagnostic (reported, never gating) -------------------------

def test_per_slate_mae_is_reported_but_does_not_gate(tmp_path):
    # Slate "Clean": v2 nearly perfect, v0 far => v2 much better there, dominating
    # the pooled MAE. Slate "Split": v2 slightly worse than v0 (res_v0 = -d/2+0.5
    # => |res_v2| = |d/2+0.5| > |d/2-0.5| ... constructed so v2 loses in-slate).
    d_a = _gap(GROUP_A)
    clean = [
        _row_for_residual_v2("Clean", "c0", GROUP_A, 0.5),
        _row_for_residual_v2("Clean", "c1", GROUP_A, -0.5),
        _row_for_residual_v2("Clean", "c2", GROUP_B, 0.5),
        _row_for_residual_v2("Clean", "c3", GROUP_B, -0.5),
    ]
    split = [
        _row_for_residual_v0("Split", "s0", GROUP_A, -d_a / 2.0 + 0.5),
        _row_for_residual_v0("Split", "s1", GROUP_A, -d_a / 2.0 + 0.5),
    ]
    report = _report(tmp_path, clean + split)

    # Per-slate structure is reported for every slate, with correct counts.
    assert set(report.per_slate) == {"Clean", "Split"}
    assert report.per_slate["Clean"].n == 4
    assert report.per_slate["Split"].n == 2

    # Pooled v2 wins (the gate's lens), yet at least one slate has v2 worse —
    # proving per-slate MAE is diagnostic only.
    assert report.mae_v2_le_v0 is True
    assert any(s.mae_v2 > s.mae_v0 for s in report.per_slate.values())
    # The combined gate is built ONLY from the pooled arms — no per-slate term.
    assert report.passes_gate == (report.mae_v2_le_v0 and report.passes_variance_gate)


def test_per_slate_values_match_independent_recomputation(tmp_path):
    rows = [
        _row_for_residual_v0("X", "x0", GROUP_A, -4.0),
        _row_for_residual_v0("X", "x1", GROUP_A, 6.0),
        _row_for_residual_v0("Y", "y0", GROUP_B, 2.0),
    ]
    report = _report(tmp_path, rows)

    def expected_slate(slate_rows):
        a0 = [abs(_proj_v0(*inp) - rl) for inp, rl in slate_rows]
        a2 = [abs(_proj_v2(inp[0], inp[2]) - rl) for inp, rl in slate_rows]
        return sum(a0) / len(a0), sum(a2) / len(a2)

    x_rows = [(GROUP_A, rows[0]["realized_dk_points"]), (GROUP_A, rows[1]["realized_dk_points"])]
    y_rows = [(GROUP_B, rows[2]["realized_dk_points"])]
    ex0, ex2 = expected_slate(x_rows)
    ey0, ey2 = expected_slate(y_rows)
    assert math.isclose(report.per_slate["X"].mae_v0, ex0, abs_tol=1e-9)
    assert math.isclose(report.per_slate["X"].mae_v2, ex2, abs_tol=1e-9)
    assert math.isclose(report.per_slate["Y"].mae_v0, ey0, abs_tol=1e-9)
    assert math.isclose(report.per_slate["Y"].mae_v2, ey2, abs_tol=1e-9)


# --- D3 skip-unless-both-valid policy -----------------------------------------

def test_missing_realized_points_is_skipped_and_counted(tmp_path):
    good = _row_for_residual_v0("S", "good0", GROUP_A, 1.0)
    good2 = _row_for_residual_v0("S", "good1", GROUP_B, -1.0)
    missing = dict(good)
    missing["fighter"] = "no_result"
    missing["realized_dk_points"] = ""  # blank realized => skip.
    report = _report(tmp_path, [good, missing, good2])

    assert report.n_included == 2
    assert report.n_skipped == 1
    assert report.total_rows == 3
    assert report.skip_reason_counts == {mc.SKIP_REALIZED: 1}
    skipped = report.skipped[0]
    assert skipped.reason == mc.SKIP_REALIZED
    assert skipped.fighter == "no_result"
    assert "realized_dk_points" in skipped.detail


def test_rows_with_either_engine_invalid_are_skipped_and_counted(tmp_path):
    good = _row_for_residual_v0("S", "good", GROUP_A, 0.0)
    bad_prob = dict(good, fighter="bad_prob", implied_win_probability=1.5)  # out of [0,1]
    blank_prob = dict(good, fighter="blank_prob", implied_win_probability="")
    bad_salary = dict(good, fighter="bad_salary", salary="not-a-number")
    bad_rounds = dict(good, fighter="bad_rounds", scheduled_rounds=4)  # v2 rejects 4
    rows = [good, bad_prob, blank_prob, bad_salary, bad_rounds]
    report = _report(tmp_path, rows)

    assert report.n_included == 1
    assert report.n_skipped == 4
    assert report.total_rows == 5
    assert report.skip_reason_counts == {
        mc.SKIP_WIN_PROB: 2,  # out-of-range and blank both fail the win-prob arm.
        mc.SKIP_SALARY: 1,
        mc.SKIP_ROUNDS: 1,
    }
    by_fighter = {s.fighter: s.reason for s in report.skipped}
    assert by_fighter["bad_prob"] == mc.SKIP_WIN_PROB
    assert by_fighter["blank_prob"] == mc.SKIP_WIN_PROB
    assert by_fighter["bad_salary"] == mc.SKIP_SALARY
    assert by_fighter["bad_rounds"] == mc.SKIP_ROUNDS


def test_skip_precedence_is_win_prob_then_salary_then_rounds_then_realized(tmp_path):
    # A row failing multiple conditions reports the FIRST in the fixed precedence.
    multi = {
        "slate": "S",
        "fighter": "multi",
        "implied_win_probability": "",  # fails first...
        "salary": "bad",  # ...even though salary is also bad.
        "scheduled_rounds": 4,
        "realized_dk_points": "",
    }
    report = _report(tmp_path, [multi])
    assert report.skipped[0].reason == mc.SKIP_WIN_PROB


def test_skip_counts_partition_the_skipped_set(tmp_path):
    rows = [
        dict(_row_for_residual_v0("S", "a", GROUP_A, 0.0), implied_win_probability=2.0),
        dict(_row_for_residual_v0("S", "b", GROUP_A, 0.0), salary=""),
        _row_for_residual_v0("S", "c", GROUP_A, 0.0),
    ]
    report = _report(tmp_path, rows)
    assert report.n_included + report.n_skipped == report.total_rows
    assert sum(report.skip_reason_counts.values()) == report.n_skipped


def test_blank_lines_are_not_counted_as_rows(tmp_path):
    path = tmp_path / "with_blank.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        handle.write(",".join(mc.REQUIRED_COLUMNS) + "\n")
        handle.write("S,good,0.6,8000,3,55.0\n")
        handle.write("\n")  # blank line — ignored, not a skipped row.
        handle.write("S,good2,0.4,7000,3,40.0\n")
    report = mc.run_calibration(path)
    assert report.total_rows == 2
    assert report.n_included == 2
    assert report.n_skipped == 0


def test_optional_finish_columns_are_tolerated_and_ignored(tmp_path):
    columns = list(mc.REQUIRED_COLUMNS) + ["p_fight_finishes", "finish_share"]
    rows = [
        dict(_row_for_residual_v0("S", "a", GROUP_A, 1.0), p_fight_finishes=0.9, finish_share=0.9),
        dict(_row_for_residual_v0("S", "b", GROUP_B, -1.0), p_fight_finishes=0.1, finish_share=0.1),
    ]
    report = _report(tmp_path, rows, columns=columns)
    # Tier 0 ignores the optional columns: the result matches the no-extra-column run.
    plain = _report(tmp_path, [
        _row_for_residual_v0("S", "a", GROUP_A, 1.0),
        _row_for_residual_v0("S", "b", GROUP_B, -1.0),
    ])
    assert report.n_included == 2
    assert math.isclose(report.mae_v2_pooled, plain.mae_v2_pooled, abs_tol=1e-9)


def test_missing_required_column_raises(tmp_path):
    path = tmp_path / "bad_header.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        handle.write("slate,fighter,implied_win_probability,salary,scheduled_rounds\n")
        handle.write("S,a,0.6,8000,3\n")
    with pytest.raises(ValueError, match="realized_dk_points"):
        mc.load_calibration_csv(path)


# --- Non-finite cells are non-numeric: skipped + counted, never poison the gate -

@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_non_finite_realized_is_skipped_and_pooled_mae_stays_finite(tmp_path, bad):
    # float("nan"/"inf") would parse, so a non-finite realized must be rejected as
    # non-numeric (design §12.1 D3) — otherwise NaN/inf poisons the pooled stats.
    clean1 = _row_for_residual_v0("S", "c1", GROUP_A, -3.0)
    clean2 = _row_for_residual_v0("S", "c2", GROUP_B, 4.0)
    poison = dict(_row_for_residual_v0("S", "poison", GROUP_A, 0.0), realized_dk_points=bad)
    report = _report(tmp_path, [clean1, poison, clean2])

    assert report.n_included == 2
    assert report.skip_reason_counts == {mc.SKIP_REALIZED: 1}
    assert report.skipped[0].fighter == "poison"
    assert math.isfinite(report.mae_v0_pooled)
    assert math.isfinite(report.mae_v2_pooled)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_non_finite_salary_is_skipped(tmp_path, bad):
    good = _row_for_residual_v0("S", "good", GROUP_A, 1.0)
    good2 = _row_for_residual_v0("S", "good2", GROUP_B, -1.0)
    bad_row = dict(_row_for_residual_v0("S", "bad_salary", GROUP_A, 0.0), salary=bad)
    report = _report(tmp_path, [good, bad_row, good2])
    assert report.n_included == 2
    assert report.skip_reason_counts == {mc.SKIP_SALARY: 1}


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_non_finite_scheduled_rounds_is_skipped_not_a_crash(tmp_path, bad):
    # int(float("nan")) raises ValueError and int(float("inf")) raises
    # OverflowError; a single bad cell must NOT abort the whole load.
    good = _row_for_residual_v0("S", "good", GROUP_A, 1.0)
    good2 = _row_for_residual_v0("S", "good2", GROUP_B, -1.0)
    bad_row = dict(_row_for_residual_v0("S", "bad_rounds", GROUP_A, 0.0), scheduled_rounds=bad)
    report = _report(tmp_path, [good, bad_row, good2])  # must not raise.
    assert report.n_included == 2
    assert report.skip_reason_counts == {mc.SKIP_ROUNDS: 1}


@pytest.mark.parametrize("bad", ["nan", "inf"])
def test_non_finite_win_probability_is_skipped(tmp_path, bad):
    good = _row_for_residual_v0("S", "good", GROUP_A, 1.0)
    good2 = _row_for_residual_v0("S", "good2", GROUP_B, -1.0)
    bad_row = dict(_row_for_residual_v0("S", "bad_prob", GROUP_A, 0.0), implied_win_probability=bad)
    report = _report(tmp_path, [good, bad_row, good2])
    assert report.n_included == 2
    assert report.skip_reason_counts == {mc.SKIP_WIN_PROB: 1}


def test_utf8_bom_header_still_loads(tmp_path):
    # Excel / Google Sheets exports often prepend a UTF-8 BOM; the loader reads
    # utf-8-sig so the first column name still matches and the file is not
    # mis-reported as missing the 'slate' column.
    path = tmp_path / "bom.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        handle.write(",".join(mc.REQUIRED_COLUMNS) + "\n")
        handle.write("S,a,0.6,8000,3,55.0\n")
        handle.write("S,b,0.4,7000,3,40.0\n")
    report = mc.run_calibration(path)
    assert report.n_included == 2
    assert report.n_skipped == 0
    assert set(report.per_slate) == {"S"}


# --- Insufficient data handled safely -----------------------------------------

def test_zero_included_rows_is_insufficient_not_passing(tmp_path):
    only_bad = dict(_row_for_residual_v0("S", "a", GROUP_A, 0.0), realized_dk_points="")
    report = _report(tmp_path, [only_bad])
    assert report.status == mc.STATUS_INSUFFICIENT
    assert report.n_included == 0
    assert report.mae_v0_pooled is None
    assert report.mae_v2_pooled is None
    assert report.mae_delta is None
    assert report.variance_ratio is None
    assert report.passes_variance_gate is False
    assert report.passes_gate is False
    assert report.per_slate == {}


def test_single_row_reports_mae_but_is_insufficient_for_variance(tmp_path):
    rows = [_row_for_residual_v0("S", "a", GROUP_A, 3.0)]
    report = _report(tmp_path, rows)
    assert report.status == mc.STATUS_INSUFFICIENT
    assert report.n_included == 1
    # MAE is computable and reported for legibility...
    assert report.mae_v0_pooled is not None
    assert report.mae_v2_pooled is not None
    assert report.mae_delta is not None
    # ...but the variance arm is undefined, so the gate cannot pass.
    assert report.variance_ratio is None
    assert report.passes_variance_gate is False
    assert report.passes_gate is False


def test_empty_file_with_header_only_is_insufficient(tmp_path):
    path = tmp_path / "empty.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        handle.write(",".join(mc.REQUIRED_COLUMNS) + "\n")
    report = mc.run_calibration(path)
    assert report.status == mc.STATUS_INSUFFICIENT
    assert report.total_rows == 0
    assert report.n_included == 0
    assert report.passes_gate is False


# --- Committed synthetic sample + no real data dependency ---------------------

def _repo_root():
    return Path(__file__).resolve().parents[1]


def test_sample_template_csv_loads_and_runs():
    sample = _repo_root() / mc.SAMPLE_CALIBRATION_CSV_PATH
    assert sample.exists(), "the committed synthetic sample must exist"
    load = mc.load_calibration_csv(sample)
    assert load.skipped == ()
    assert len(load.rows) == 2  # header + two synthetic rows.
    report = mc.compare_engines(load)
    # Two rows is exactly MIN_ROWS_FOR_VARIANCE, so the gate is evaluable.
    assert report.status == mc.STATUS_OK_REPORT
    assert report.n_included == 2
    assert set(report.per_slate) == {"Sample Slate 1"}
    # Synthetic data — the gate outcome is meaningless, only that it is a bool.
    assert isinstance(report.passes_gate, bool)


def test_no_real_calibration_file_is_required():
    # The real CSV path is documented + gitignored; tests never read it. Assert the
    # contract (path constants) without touching any real local file.
    assert mc.REAL_CALIBRATION_CSV_PATH == "data/calibration/realized_points.csv"
    assert mc.SAMPLE_CALIBRATION_CSV_PATH == "data/calibration/realized_points.sample.csv"
    # The harness runs entirely off the committed sample — no real file needed.
    assert (_repo_root() / mc.SAMPLE_CALIBRATION_CSV_PATH).exists()
