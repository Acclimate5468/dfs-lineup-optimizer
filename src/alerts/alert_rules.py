"""Mismatch Alerts v1 — Phase A pure rule definitions.

One function per category in ``docs/MISMATCH_ALERTS_V1_DESIGN.md`` §3
(skipping §3.9, which is reserved and never fires in v1). Each rule is
pure-Python: no DB, no Streamlit, no service composition,
no ``effective_status`` parameter.

The shared :class:`Alert` value object is the v1 contract pinned in
design §9; consumers (future Phase B service, future Phase C page)
must not extend or reinterpret these fields without a paired design
update. The :func:`sort_alerts` helper realizes the §9 deterministic
ordering used by AppTest pinning in future Phase C.

Severity / scope rules:

- ``severity`` ∈ ``{"info", "warn"}`` (design §3 — no ``error`` in v1).
- ``scope`` ∈ ``{"fighter", "slate"}``. Only :func:`fight_group_issue`
  emits ``"slate"``; everything else is fighter-scoped.

The ``late_news_risk`` code is reserved here so the output-shape table
remains stable, but **no rule in this module ever emits it** (design
§3.9 / §15 risk #8). The Phase A test suite pins that contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ALERT_CODE_SALARY_INEFFICIENCY_HIGH = "salary_inefficiency_high"
ALERT_CODE_SALARY_INEFFICIENCY_LOW = "salary_inefficiency_low"
ALERT_CODE_ODDS_VS_SALARY_MISMATCH = "odds_vs_salary_mismatch"
ALERT_CODE_UNDERDOG_VALUE = "underdog_value"
ALERT_CODE_WEAK_EXPENSIVE_FAVORITE = "weak_expensive_favorite"
ALERT_CODE_FIVE_ROUND_EDGE = "five_round_edge"
ALERT_CODE_MISSING_INPUT = "missing_input"
ALERT_CODE_PROJECTION_NON_PROJECTABLE = "projection_non_projectable"
ALERT_CODE_FIGHT_GROUP_ISSUE = "fight_group_issue"
ALERT_CODE_LATE_NEWS_RISK = "late_news_risk"

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"

SCOPE_FIGHTER = "fighter"
SCOPE_SLATE = "slate"

PROJECTION_STATUS_OK = "ok"
PROJECTION_STATUS_MISSING_INPUTS = "missing_inputs"
PROJECTION_STATUS_NON_PROJECTABLE = "non_projectable"

# §3.1 salary inefficiency thresholds (per $1000).
SALARY_INEFF_HIGH_PPK = 5.0
SALARY_INEFF_LOW_PPK = 2.5
SALARY_INEFF_LOW_MIN_SALARY = 8500

# §3.4 weak expensive favorite thresholds.
WEAK_EXPENSIVE_MIN_SALARY = 9000
WEAK_EXPENSIVE_MAX_PWIN = 0.55  # strict <

# §3.5 five-round edge gate.
FIVE_ROUND_EDGE_MIN_PWIN = 0.55  # >=
FIVE_ROUND_SCHEDULED = 5


@dataclass(frozen=True)
class Alert:
    """v1 Mismatch Alert value object (design §9).

    ``fighter_id`` and ``fighter_name`` are required when
    ``scope == "fighter"`` and must be ``None`` when ``scope == "slate"``.
    ``tags`` carries projection-layer diagnostics through unchanged
    where applicable (e.g. ``missing_inputs`` tags for §3.6); empty
    tuple otherwise.
    """

    code: str
    severity: str
    scope: str
    fighter_id: int | None
    fighter_name: str | None
    message: str
    tags: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# §3.1 Salary inefficiency
# ---------------------------------------------------------------------------


def salary_inefficiency_high(
    fighter_id: int,
    fighter_name: str,
    salary: int,
    projected_dk_points: float,
    projection_status: str,
) -> Alert | None:
    """Design §3.1 high-efficiency value alert.

    Fires when ``projected_dk_points / (salary / 1000) >= 5.0`` for a
    fighter with ``projection_status == 'ok'``. Non-ok rows are never
    flagged here — §3.6 / §3.7 own those.
    """
    if projection_status != PROJECTION_STATUS_OK:
        return None
    if salary <= 0:
        return None
    points_per_k = float(projected_dk_points) / (float(salary) / 1000.0)
    if points_per_k < SALARY_INEFF_HIGH_PPK:
        return None
    return Alert(
        code=ALERT_CODE_SALARY_INEFFICIENCY_HIGH,
        severity=SEVERITY_INFO,
        scope=SCOPE_FIGHTER,
        fighter_id=fighter_id,
        fighter_name=fighter_name,
        message=(
            f"{fighter_name}: high salary efficiency "
            f"({points_per_k:.2f} pts/$1k at ${salary})."
        ),
        tags=(),
    )


def salary_inefficiency_low(
    fighter_id: int,
    fighter_name: str,
    salary: int,
    projected_dk_points: float,
    projection_status: str,
) -> Alert | None:
    """Design §3.1 low-efficiency pay-up alert.

    Fires when ``projected_dk_points / (salary / 1000) <= 2.5`` AND
    ``salary >= 8500`` — the cheap-low-projection case is intentionally
    not flagged here (it is chalk-avoid, not a mismatch alert).
    """
    if projection_status != PROJECTION_STATUS_OK:
        return None
    if salary < SALARY_INEFF_LOW_MIN_SALARY:
        return None
    if salary <= 0:
        return None
    points_per_k = float(projected_dk_points) / (float(salary) / 1000.0)
    if points_per_k > SALARY_INEFF_LOW_PPK:
        return None
    return Alert(
        code=ALERT_CODE_SALARY_INEFFICIENCY_LOW,
        severity=SEVERITY_INFO,
        scope=SCOPE_FIGHTER,
        fighter_id=fighter_id,
        fighter_name=fighter_name,
        message=(
            f"{fighter_name}: low salary efficiency for a pay-up "
            f"({points_per_k:.2f} pts/$1k at ${salary})."
        ),
        tags=(),
    )


# ---------------------------------------------------------------------------
# §3.2 Odds-vs-salary mismatch
# ---------------------------------------------------------------------------


def odds_vs_salary_mismatch(
    fighter_id: int,
    fighter_name: str,
    salary: int,
    implied_win_probability: float,
    projection_status: str,
) -> Alert | None:
    """Design §3.2 tier-mismatch alert.

    Coarse tier table; each tier defines the trigger inequality(ies).
    The table is duplicated from the design; any change requires a
    paired design + test update.
    """
    if projection_status != PROJECTION_STATUS_OK:
        return None
    p = float(implied_win_probability)
    s = int(salary)

    triggered = False
    if s >= 9500:
        triggered = p < 0.55
    elif s >= 9000:
        triggered = p < 0.50
    elif s >= 8000:
        triggered = p <= 0.42 or p >= 0.70
    elif s >= 7000:
        triggered = p >= 0.62
    else:
        triggered = p >= 0.55

    if not triggered:
        return None
    return Alert(
        code=ALERT_CODE_ODDS_VS_SALARY_MISMATCH,
        severity=SEVERITY_INFO,
        scope=SCOPE_FIGHTER,
        fighter_id=fighter_id,
        fighter_name=fighter_name,
        message=(
            f"{fighter_name}: salary ${s} and implied win prob "
            f"{p:.2f} disagree (tier mismatch)."
        ),
        tags=(),
    )


# ---------------------------------------------------------------------------
# §3.3 Underdog value
# ---------------------------------------------------------------------------


def underdog_value(
    fighter_id: int,
    fighter_name: str,
    salary: int,
    implied_win_probability: float,
    projection_status: str,
) -> Alert | None:
    """Design §3.3 / docs/DEVELOPMENT_NOTES.md §4 value-gap mirror.

    Thresholds intentionally mirror ``value_gap_bonus`` in
    ``src/projections/value_bonus.py``. They are duplicated here rather
    than imported because §4 of docs/DEVELOPMENT_NOTES.md treats the projection
    formula's coefficients as locked; if the bonus thresholds ever
    change, this rule and its tests change in the same slice.
    """
    if projection_status != PROJECTION_STATUS_OK:
        return None
    p = float(implied_win_probability)
    s = int(salary)
    if not (
        (s <= 7600 and p >= 0.45)
        or (s <= 8000 and p >= 0.48)
        or (s <= 8500 and p >= 0.55)
    ):
        return None
    return Alert(
        code=ALERT_CODE_UNDERDOG_VALUE,
        severity=SEVERITY_INFO,
        scope=SCOPE_FIGHTER,
        fighter_id=fighter_id,
        fighter_name=fighter_name,
        message=(
            f"{fighter_name}: underdog value "
            f"(salary ${s}, implied win prob {p:.2f})."
        ),
        tags=(),
    )


# ---------------------------------------------------------------------------
# §3.4 Weak expensive favorite
# ---------------------------------------------------------------------------


def weak_expensive_favorite(
    fighter_id: int,
    fighter_name: str,
    salary: int,
    implied_win_probability: float,
    projection_status: str,
) -> Alert | None:
    """Design §3.4 — paid-up without a dominant probability."""
    if projection_status != PROJECTION_STATUS_OK:
        return None
    if salary < WEAK_EXPENSIVE_MIN_SALARY:
        return None
    if implied_win_probability >= WEAK_EXPENSIVE_MAX_PWIN:
        return None
    return Alert(
        code=ALERT_CODE_WEAK_EXPENSIVE_FAVORITE,
        severity=SEVERITY_INFO,
        scope=SCOPE_FIGHTER,
        fighter_id=fighter_id,
        fighter_name=fighter_name,
        message=(
            f"{fighter_name}: expensive (${salary}) but weak implied "
            f"win prob ({float(implied_win_probability):.2f})."
        ),
        tags=(),
    )


# ---------------------------------------------------------------------------
# §3.5 Five-round / rounds edge
# ---------------------------------------------------------------------------


def five_round_edge(
    fighter_id: int,
    fighter_name: str,
    scheduled_rounds: int | None,
    implied_win_probability: float | None,
    projection_status: str,
) -> Alert | None:
    """Design §3.5 — main-event edge spot.

    Never fires when ``scheduled_rounds`` is missing (that case is
    owned by §3.6) or when ``projection_status != 'ok'``.
    """
    if projection_status != PROJECTION_STATUS_OK:
        return None
    if scheduled_rounds is None or int(scheduled_rounds) != FIVE_ROUND_SCHEDULED:
        return None
    if implied_win_probability is None:
        return None
    if implied_win_probability < FIVE_ROUND_EDGE_MIN_PWIN:
        return None
    return Alert(
        code=ALERT_CODE_FIVE_ROUND_EDGE,
        severity=SEVERITY_INFO,
        scope=SCOPE_FIGHTER,
        fighter_id=fighter_id,
        fighter_name=fighter_name,
        message=(
            f"{fighter_name}: 5-round edge "
            f"(implied win prob {float(implied_win_probability):.2f})."
        ),
        tags=(),
    )


# ---------------------------------------------------------------------------
# §3.6 Missing input
# ---------------------------------------------------------------------------


def missing_input(
    fighter_id: int,
    fighter_name: str,
    projection_status: str,
    missing_inputs: tuple[str, ...],
) -> Alert | None:
    """Design §3.6 — one alert per fighter with ``missing_inputs`` status.

    The fighter's ``missing_inputs`` tags pass through verbatim into
    ``Alert.tags`` so the UI can render them without re-querying the
    projection layer.
    """
    if projection_status != PROJECTION_STATUS_MISSING_INPUTS:
        return None
    tags = tuple(missing_inputs)
    if tags:
        tag_str = ", ".join(tags)
        message = f"{fighter_name}: projection missing inputs ({tag_str})."
    else:
        message = f"{fighter_name}: projection missing inputs."
    return Alert(
        code=ALERT_CODE_MISSING_INPUT,
        severity=SEVERITY_WARN,
        scope=SCOPE_FIGHTER,
        fighter_id=fighter_id,
        fighter_name=fighter_name,
        message=message,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# §3.7 Projection non-projectable
# ---------------------------------------------------------------------------


def projection_non_projectable(
    fighter_id: int,
    fighter_name: str,
    projection_status: str,
    missing_inputs: tuple[str, ...],
) -> Alert | None:
    """Design §3.7 — per-fighter consequence of a structural gap."""
    if projection_status != PROJECTION_STATUS_NON_PROJECTABLE:
        return None
    tags = tuple(missing_inputs)
    if tags:
        tag_str = ", ".join(tags)
        message = (
            f"{fighter_name}: not projectable ({tag_str})."
        )
    else:
        message = f"{fighter_name}: not projectable."
    return Alert(
        code=ALERT_CODE_PROJECTION_NON_PROJECTABLE,
        severity=SEVERITY_WARN,
        scope=SCOPE_FIGHTER,
        fighter_id=fighter_id,
        fighter_name=fighter_name,
        message=message,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# §3.8 Fight-group / opponent issue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FighterStructuralFlags:
    """Per-fighter structural-flag bundle for :func:`fight_group_issue`.

    Mirrors the ``has_fight_group`` / ``has_opponent`` flags surfaced
    by ``ProjectionInputs`` (design §3.8 inputs). Phase A keeps the
    rule layer pure by taking this directly rather than re-deriving it
    from a DB read.
    """

    fighter_name: str
    has_fight_group: bool
    has_opponent: bool


def fight_group_issue(
    fighters: list[FighterStructuralFlags] | tuple[FighterStructuralFlags, ...],
) -> Alert | None:
    """Design §3.8 — slate-scoped alert for missing fight-group context.

    Fires if any fighter in ``fighters`` lacks a fight group, or has a
    fight group with no resolved opponent. ``fighters`` is the active
    fighter set for the slate; inactive fighters are filtered upstream
    (Projection v1 Phase B).
    """
    affected = [
        f.fighter_name
        for f in fighters
        if (not f.has_fight_group) or (f.has_fight_group and not f.has_opponent)
    ]
    if not affected:
        return None
    affected_sorted = sorted(affected)
    names = ", ".join(affected_sorted)
    return Alert(
        code=ALERT_CODE_FIGHT_GROUP_ISSUE,
        severity=SEVERITY_WARN,
        scope=SCOPE_SLATE,
        fighter_id=None,
        fighter_name=None,
        message=(
            "Fight-group / opponent issue for: "
            f"{names}. Resolve on the Fight Groups page."
        ),
        tags=tuple(affected_sorted),
    )


# ---------------------------------------------------------------------------
# §9 deterministic ordering
# ---------------------------------------------------------------------------


_SEVERITY_RANK = {SEVERITY_WARN: 0, SEVERITY_INFO: 1}
_SCOPE_RANK = {SCOPE_SLATE: 0, SCOPE_FIGHTER: 1}


def sort_alerts(alerts: list[Alert] | tuple[Alert, ...]) -> list[Alert]:
    """Apply design §9 deterministic ordering.

    1. ``warn`` before ``info``
    2. within severity, ``slate`` before ``fighter``
    3. within scope, ``code`` ascending (lexicographic)
    4. within code, ``fighter_id`` ascending (``None`` first — slate
       alerts have ``fighter_id is None`` and already sort first via
       step 2, so this is a no-op for them).
    """

    def key(a: Alert) -> tuple[int, int, str, int]:
        return (
            _SEVERITY_RANK.get(a.severity, 99),
            _SCOPE_RANK.get(a.scope, 99),
            a.code,
            -1 if a.fighter_id is None else int(a.fighter_id),
        )

    return sorted(alerts, key=key)
