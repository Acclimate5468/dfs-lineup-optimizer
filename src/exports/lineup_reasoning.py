"""Deterministic lineup reasoning generator (pure).

Realizes ``docs/TWO_STEP_BUILDER_PRODUCTION_DESIGN.md`` §8 — the only
genuinely new pure logic in the two-step builder port. Given an explicit
:class:`ReasoningContext` of already-computed / already-stored facts, it
produces a deterministic, structured :class:`LineupReasoningResult`
explaining each lineup in plain language.

Purity / safety contract (design §8.1 / §8.3):

- No DB, no Streamlit, no network, no file I/O — only stdlib + dataclasses.
- Side-effect-free and deterministic: identical input → identical ordered
  output.
- Every emitted claim is traceable to a value supplied in the context.
  Optional facts (implied probability, value-gap bonus, five-round bonus,
  fight-group ids, totals, salary cap) are cited **only** when the caller
  supplies them; nothing is inferred, predicted, or back-filled.
- The input model carries **no** prop / news / line-movement fields
  (``itd_odds`` / ``decision_odds`` / ``goes_distance`` / news flags are
  preview-only and unpersisted in v0 — design §6.6 / §8.3). A finish /
  ITD / "lock" / "safe favorite" claim is therefore structurally
  impossible: there is no field through which such data could enter the
  generator, and the templates below never assert a fight outcome — they
  state the stored implied probability and the projection-formula
  contribution, never a predicted result (design §8.3).

The read-only assembler that builds a :class:`ReasoningContext` from the
optimizer / export bundle and ``project_slate`` lives elsewhere and is a
later slice (design §8.1, B6); this module is the pure core it calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Reasoning item kinds (closed set — design §8.1).
# ---------------------------------------------------------------------------

KIND_LINEUP_SUMMARY = "lineup_summary"
KIND_PROJECTION_DRIVER = "projection_driver"
KIND_VALUE_DRIVER = "value_driver"
KIND_FIVE_ROUND_CONTEXT = "five_round_context"
KIND_CONSTRAINT_CHECK = "constraint_check"
KIND_EXCLUSION_OR_WARNING = "exclusion_or_warning"

ALLOWED_REASONING_KINDS: frozenset[str] = frozenset(
    {
        KIND_LINEUP_SUMMARY,
        KIND_PROJECTION_DRIVER,
        KIND_VALUE_DRIVER,
        KIND_FIVE_ROUND_CONTEXT,
        KIND_CONSTRAINT_CHECK,
        KIND_EXCLUSION_OR_WARNING,
    }
)


# ---------------------------------------------------------------------------
# Input value objects (all optional facts default to ``None`` so they are
# cited only when the caller explicitly supplies them).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FighterReasoningInput:
    """One fighter's already-computed facts (design §8.1 facts table).

    - ``name`` / ``salary`` / ``projection``: always required (the
      persisted salary and the formula's projected DK points).
    - ``implied_win_probability``: the no-vig moneyline-derived
      probability, when matched odds exist. ``None`` when no moneyline is
      matched — the generator then makes no probability claim about this
      fighter.
    - ``value_gap_bonus``: the +8 / +5 / +3 / 0 value-gap contribution
      (``src.projections.value_bonus.value_gap_bonus``), supplied by the
      caller. Cited only when supplied and positive.
    - ``five_round_bonus``: the +7 / 0 five-round contribution
      (``src.projections.value_bonus.five_round_bonus``). Cited only when
      supplied and positive.
    - ``scheduled_rounds``: the fight's scheduled rounds, never inferred.
    - ``fight_group_id``: the fighter's fight group — used only to state
      the no-same-fight-pair constraint when every fighter supplies one.
    - ``projection_status``: the Projection v1 status string carried
      through verbatim (informational; not asserted as an outcome).
    """

    name: str
    salary: int
    projection: float
    implied_win_probability: float | None = None
    value_gap_bonus: float | None = None
    five_round_bonus: float | None = None
    scheduled_rounds: int | None = None
    fight_group_id: int | None = None
    projection_status: str | None = None


@dataclass(frozen=True)
class LineupReasoningInput:
    """One lineup's fighters plus the optimizer's own totals.

    ``total_salary`` / ``total_projection`` are the solver's per-lineup
    totals carried through verbatim (design §8.1). They are optional so a
    caller can omit them; the under-cap constraint line is emitted only
    when both ``total_salary`` (here) and ``salary_cap`` (on the context)
    are supplied.
    """

    lineup_index: int
    fighters: tuple[FighterReasoningInput, ...]
    total_salary: int | None = None
    total_projection: float | None = None


@dataclass(frozen=True)
class ExcludedFighterNote:
    """A pool-exclusion diagnostic carried through verbatim (design §8.1).

    ``reason`` is the stored / derived reason string (e.g. the export
    diagnostics' ``ExcludedFighterEntry.reason`` or a Projection v1
    status) — not a generated claim.
    """

    name: str
    reason: str


@dataclass(frozen=True)
class WarningNote:
    """An active gate Warning / Blocking flag carried through verbatim.

    ``code`` is the check / alert identifier; ``message`` is that check's
    own message (the gate, not the generator, owns the wording).
    """

    code: str
    message: str


@dataclass(frozen=True)
class ReasoningContext:
    """Explicit, pure input for :func:`build_lineup_reasoning` (design §8.1).

    - ``lineups``: the lineups to explain, in the order to render them.
      Empty when the optimizer produced none (gate-blocked / infeasible) —
      the generator then emits a diagnostics-only explanation, no crash.
    - ``salary_cap``: the DK cap, supplied so the under-cap constraint can
      be stated; ``None`` suppresses that line.
    - ``roster_size``: the expected roster size (6 for DK UFC Classic).
      When supplied and a lineup matches it, the summary notes a full
      roster; otherwise only the raw count is stated.
    - ``excluded``: fighters left out of the pool, with stored reasons.
    - ``warnings``: active gate Warning / Blocking flags to surface.
    """

    lineups: tuple[LineupReasoningInput, ...] = ()
    salary_cap: int | None = None
    roster_size: int | None = None
    excluded: tuple[ExcludedFighterNote, ...] = ()
    warnings: tuple[WarningNote, ...] = ()


# ---------------------------------------------------------------------------
# Output value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReasoningItem:
    """One structured reasoning line (design §8.1).

    - ``kind``: one of :data:`ALLOWED_REASONING_KINDS`.
    - ``text``: the rendered sentence. Fact-bounded — every value in it
      comes from the context.
    - ``fighter_names``: the fighters the line is about (empty for
      lineup-level / context-level lines).
    - ``lineup_index``: the lineup this line belongs to, or ``None`` for
      context-level lines (exclusions / warnings / the empty-result note).
    """

    kind: str
    text: str
    fighter_names: tuple[str, ...] = field(default_factory=tuple)
    lineup_index: int | None = None


@dataclass(frozen=True)
class LineupReasoningResult:
    """The deterministic, ordered reasoning for a whole context (design §8.1)."""

    items: tuple[ReasoningItem, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Formatting helpers (display-only; no rounding policy beyond presentation).
# ---------------------------------------------------------------------------


def _money(value: int | float) -> str:
    return f"${int(round(value)):,}"


def _points(value: float) -> str:
    return f"{float(value):.1f}"


def _pct(probability: float) -> str:
    return f"{float(probability) * 100:.0f}%"


def _bonus(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.0f}"


# ---------------------------------------------------------------------------
# Per-lineup item builders.
# ---------------------------------------------------------------------------


def _lineup_summary_item(
    lineup: LineupReasoningInput, *, roster_size: int | None
) -> ReasoningItem:
    count = len(lineup.fighters)
    if roster_size is not None and count == roster_size:
        head = f"Lineup {lineup.lineup_index}: a full {roster_size}-fighter roster"
    else:
        head = f"Lineup {lineup.lineup_index}: {count} fighter(s)"
    parts = [head]
    if lineup.total_salary is not None:
        parts.append(f"{_money(lineup.total_salary)} total salary")
    if lineup.total_projection is not None:
        parts.append(f"{_points(lineup.total_projection)} projected points")
    return ReasoningItem(
        kind=KIND_LINEUP_SUMMARY,
        text=", ".join(parts) + ".",
        fighter_names=tuple(f.name for f in lineup.fighters),
        lineup_index=lineup.lineup_index,
    )


def _projection_driver_items(
    lineup: LineupReasoningInput,
) -> list[ReasoningItem]:
    items: list[ReasoningItem] = []
    if not lineup.fighters:
        return items

    # Top projection — the largest single contributor to the lineup's
    # total. Deterministic: highest projection, then name ascending.
    top = sorted(lineup.fighters, key=lambda f: (-float(f.projection), f.name))[0]
    items.append(
        ReasoningItem(
            kind=KIND_PROJECTION_DRIVER,
            text=(
                f"{top.name} is the top projection in Lineup "
                f"{lineup.lineup_index} ({_points(top.projection)} pts) — the "
                "lineup is anchored on projected points, not a predicted result."
            ),
            fighter_names=(top.name,),
            lineup_index=lineup.lineup_index,
        )
    )

    # Highest implied win probability — stated only when at least one
    # fighter carries a matched-odds probability (design §8.2 / §8.3).
    with_prob = [
        f for f in lineup.fighters if f.implied_win_probability is not None
    ]
    if with_prob:
        top_prob = sorted(
            with_prob,
            key=lambda f: (-float(f.implied_win_probability), f.name),
        )[0]
        items.append(
            ReasoningItem(
                kind=KIND_PROJECTION_DRIVER,
                text=(
                    f"{top_prob.name} carries the highest implied win "
                    f"probability in Lineup {lineup.lineup_index} "
                    f"({_pct(top_prob.implied_win_probability)}, from the "
                    "no-vig moneyline) — a stored number, not an outcome."
                ),
                fighter_names=(top_prob.name,),
                lineup_index=lineup.lineup_index,
            )
        )
    return items


def _value_driver_items(lineup: LineupReasoningInput) -> list[ReasoningItem]:
    items: list[ReasoningItem] = []
    for f in lineup.fighters:
        if f.value_gap_bonus is None or f.value_gap_bonus <= 0:
            continue
        prob_clause = ""
        if f.implied_win_probability is not None:
            prob_clause = (
                f" at {_pct(f.implied_win_probability)} implied win probability"
            )
        items.append(
            ReasoningItem(
                kind=KIND_VALUE_DRIVER,
                text=(
                    f"{f.name} ({_money(f.salary)}{prob_clause}) clears a "
                    f"{_bonus(f.value_gap_bonus)} value-gap tier, adding "
                    f"{_points(f.value_gap_bonus)} points beyond the base "
                    "projection."
                ),
                fighter_names=(f.name,),
                lineup_index=lineup.lineup_index,
            )
        )
    return items


def _five_round_items(lineup: LineupReasoningInput) -> list[ReasoningItem]:
    items: list[ReasoningItem] = []
    for f in lineup.fighters:
        if f.five_round_bonus is None or f.five_round_bonus <= 0:
            continue
        rounds_clause = ""
        if f.scheduled_rounds is not None:
            rounds_clause = f" (scheduled for {int(f.scheduled_rounds)} rounds)"
        items.append(
            ReasoningItem(
                kind=KIND_FIVE_ROUND_CONTEXT,
                text=(
                    f"{f.name}'s bout{rounds_clause} carries the "
                    f"{_bonus(f.five_round_bonus)} five-round bonus — more "
                    "scheduled rounds, more scoring opportunity."
                ),
                fighter_names=(f.name,),
                lineup_index=lineup.lineup_index,
            )
        )
    return items


def _constraint_items(
    lineup: LineupReasoningInput, *, salary_cap: int | None
) -> list[ReasoningItem]:
    items: list[ReasoningItem] = []

    # No-same-fight-pair: stated only when every fighter supplies a fight
    # group id (a supplied fact), so the claim is never invented.
    group_ids = [f.fight_group_id for f in lineup.fighters]
    if lineup.fighters and all(gid is not None for gid in group_ids):
        distinct = len(set(group_ids))
        count = len(group_ids)
        if distinct == count:
            text = (
                f"No two fighters in Lineup {lineup.lineup_index} come from the "
                f"same fight ({distinct} distinct fights) — the "
                "no-same-fight-pair constraint is satisfied."
            )
        else:
            text = (
                f"Lineup {lineup.lineup_index} draws {count} fighters from only "
                f"{distinct} distinct fights — the no-same-fight-pair "
                "constraint is not satisfied."
            )
        items.append(
            ReasoningItem(
                kind=KIND_CONSTRAINT_CHECK,
                text=text,
                fighter_names=tuple(f.name for f in lineup.fighters),
                lineup_index=lineup.lineup_index,
            )
        )

    # Under-cap: stated only when both the lineup total and the cap are
    # supplied (a supplied fact).
    if lineup.total_salary is not None and salary_cap is not None:
        within = lineup.total_salary <= salary_cap
        relation = "within" if within else "over"
        items.append(
            ReasoningItem(
                kind=KIND_CONSTRAINT_CHECK,
                text=(
                    f"Lineup {lineup.lineup_index} totals "
                    f"{_money(lineup.total_salary)} — {relation} the "
                    f"{_money(salary_cap)} salary cap."
                ),
                lineup_index=lineup.lineup_index,
            )
        )
    return items


def _exclusion_items(context: ReasoningContext) -> list[ReasoningItem]:
    items: list[ReasoningItem] = []
    for note in context.excluded:
        items.append(
            ReasoningItem(
                kind=KIND_EXCLUSION_OR_WARNING,
                text=f"Left out of the pool: {note.name} — {note.reason}.",
                fighter_names=(note.name,),
            )
        )
    return items


def _warning_items(context: ReasoningContext) -> list[ReasoningItem]:
    items: list[ReasoningItem] = []
    for note in context.warnings:
        items.append(
            ReasoningItem(
                kind=KIND_EXCLUSION_OR_WARNING,
                text=f"Review flag ({note.code}): {note.message}",
            )
        )
    return items


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def build_lineup_reasoning(context: ReasoningContext) -> LineupReasoningResult:
    """Return deterministic, fact-bounded reasoning for ``context``.

    Item order (stable for a given input):

    1. Per lineup, in ``context.lineups`` order:
       lineup summary → projection driver(s) → value driver(s) →
       five-round context → constraint check(s).
    2. Context-level exclusions, in ``context.excluded`` order.
    3. Context-level review flags, in ``context.warnings`` order.

    When ``context.lineups`` is empty (the optimizer returned none —
    gate-blocked / infeasible), a single diagnostics-only summary item is
    emitted in place of the per-lineup section, followed by the same
    exclusions / warnings. No exception is raised.
    """
    items: list[ReasoningItem] = []

    if not context.lineups:
        items.append(
            ReasoningItem(
                kind=KIND_LINEUP_SUMMARY,
                text=(
                    "No lineups were generated for this slate — see the "
                    "exclusions and review flags below."
                ),
            )
        )
    else:
        for lineup in context.lineups:
            items.append(
                _lineup_summary_item(lineup, roster_size=context.roster_size)
            )
            items.extend(_projection_driver_items(lineup))
            items.extend(_value_driver_items(lineup))
            items.extend(_five_round_items(lineup))
            items.extend(
                _constraint_items(lineup, salary_cap=context.salary_cap)
            )

    items.extend(_exclusion_items(context))
    items.extend(_warning_items(context))

    return LineupReasoningResult(items=tuple(items))
