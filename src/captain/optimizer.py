"""DraftKings UFC **Captain (Showdown)** lineup optimizer.

Pure brute-force solver for `docs/CAPTAIN_MODE_DESIGN.md` §6. This module is
additive and lives beside Classic per §3 — it does **not** import, edit, or
depend on any Classic optimizer / projection path, and it adds **no** PuLP
dependency (brute force only, the pool is ~14). No I/O: no DB, no Streamlit, no
network, no file writes. Deterministic.

The optimizer is **projection-source-agnostic** (§7): it takes a per-fighter
projected-points value as *input* and never computes projections, de-vigs odds,
or reads a salary CSV — those are the C2 parser and the C4 method interface. The
caller supplies the candidate pool with projections already attached and has
already excluded any "out" fighters.

DK Captain hard rules realized here (§6):

  - A lineup is **exactly 6 distinct fighters: 1 Captain + 5 Fighters**.
  - Salary cap is **$50,000**.
  - The Captain scores **1.5× points** and costs ``captain_salary`` (the 1.5×
    row); the other five score their base ``projection`` and cost
    ``base_salary``.
  - Same-fight handling is a **selectable stack mode** (§14.3). The default,
    :data:`StackMode.CASH`, applies **no same-fight exclusion** — a lineup *may*
    roster both fighters of a bout (the original C3 behavior, unchanged).
    :data:`StackMode.GPP` (the tournament default surfaced by the UI) **rejects**
    any lineup containing both fighters of a bout, given the slate's bout
    pairings as ``same_fight_pairs``.

STATUS: tested in isolation against synthetic fixtures only. This solver does
not gate on Manual Review; the gate (§4) is enforced by the caller/UI, not here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from itertools import combinations

# DK Captain (Showdown) ruleset constants (§6).
SALARY_CAP = 50_000
LINEUP_SIZE = 6
CAPTAIN_MULTIPLIER = 1.5


class CaptainOptimizerError(ValueError):
    """Raised on malformed *input* to the optimizer (a programming error).

    This is distinct from an *infeasible* slate (too few fighters, or nothing
    fits under the cap), which is reported as a typed status on the result
    rather than raised — see :class:`CaptainOptimizerStatus`.
    """


class CaptainOptimizerStatus(Enum):
    """Outcome of an optimize call.

    ``OK`` — at least one feasible lineup was found.
    ``NOT_ENOUGH_FIGHTERS`` — fewer than :data:`LINEUP_SIZE` candidates.
    ``NO_FEASIBLE_LINEUP`` — enough fighters, but no 6-fighter combination with
    any captain choice came in at or under :data:`SALARY_CAP`.
    """

    OK = "ok"
    NOT_ENOUGH_FIGHTERS = "not_enough_fighters"
    NO_FEASIBLE_LINEUP = "no_feasible_lineup"


class StackMode(Enum):
    """Same-fight stacking policy for a build (design §14.3).

    ``CASH`` — the **default** and the original C3 behavior: **no** same-fight
    exclusion, so a lineup may roster both fighters of a bout. ``same_fight_pairs``
    is ignored in this mode.

    ``GPP`` — the tournament policy (the UI's default selector value): **reject**
    any lineup whose six fighters contain *both* names of any bout in
    ``same_fight_pairs``. With no pairs supplied there is nothing to exclude, so
    ``GPP`` then behaves identically to ``CASH``.
    """

    CASH = "cash"
    GPP = "gpp"


def _coerce_stack_mode(stack_mode: "StackMode | str") -> "StackMode":
    """Accept a :class:`StackMode` or its string value; else raise.

    Lets a UI selector pass ``"gpp"`` / ``"cash"`` (case-insensitive, trimmed)
    without importing the enum, while a programming error (an unknown mode) is a
    clean :class:`CaptainOptimizerError` rather than a bare ``ValueError``.
    """
    if isinstance(stack_mode, StackMode):
        return stack_mode
    try:
        return StackMode(str(stack_mode).strip().lower())
    except ValueError as exc:
        valid = ", ".join(m.value for m in StackMode)
        raise CaptainOptimizerError(
            f"Unknown stack_mode {stack_mode!r}. Expected one of: {valid}."
        ) from exc


def _normalize_same_fight_pairs(
    same_fight_pairs: "Iterable[Iterable[str]] | None",
) -> frozenset[frozenset[str]]:
    """Normalize the bout pairings into a set of unordered two-name frozensets.

    Each pair must name **exactly two distinct** fighters; anything else is a
    malformed input and raises :class:`CaptainOptimizerError` (consistent with
    the optimizer's other input-validation failures). A name absent from the
    candidate pool is allowed — it simply never matches a lineup.
    """
    if not same_fight_pairs:
        return frozenset()
    normalized: set[frozenset[str]] = set()
    for pair in same_fight_pairs:
        names = tuple(str(n) for n in pair)
        if len(names) != 2 or names[0] == names[1]:
            raise CaptainOptimizerError(
                f"same_fight_pairs entries must name exactly two distinct "
                f"fighters; got {list(pair)!r}."
            )
        normalized.add(frozenset(names))
    return frozenset(normalized)


@dataclass(frozen=True)
class CaptainCandidate:
    """One eligible fighter the optimizer may roster (§6 / §7 input contract).

    ``projection`` is the **base** projected points (the Captain multiplier is
    applied by the optimizer, not baked in here). ``captain_salary`` is the
    cost when this fighter is the Captain (the DK 1.5× row); ``base_salary`` is
    the cost in any of the five Fighter slots. The optimizer treats these
    salaries as given and does not require ``captain_salary == 1.5 ×
    base_salary`` — that is the parser's concern (C2).
    """

    name: str
    base_salary: int
    captain_salary: int
    projection: float


@dataclass(frozen=True)
class CaptainLineup:
    """A valid 6-fighter Captain lineup (1 Captain + 5 Fighters).

    ``flex_names`` are the five non-captain fighters, sorted for determinism.
    ``salary`` is ``captain_salary(captain) + Σ base_salary(flex)`` and is
    guaranteed ``<= SALARY_CAP``. ``points`` is
    ``CAPTAIN_MULTIPLIER × projection(captain) + Σ projection(flex)``.
    """

    captain_name: str
    flex_names: tuple[str, ...]
    salary: int
    points: float

    @property
    def fighter_names(self) -> tuple[str, ...]:
        """All six rostered names: the captain followed by the sorted flex."""
        return (self.captain_name, *self.flex_names)


@dataclass(frozen=True)
class CaptainOptimizerResult:
    """Deterministic output of :func:`optimize_captain_lineups`.

    ``lineups`` are the top-N feasible lineups ordered by descending points,
    then ascending salary, then lineup names — empty whenever ``status`` is not
    ``OK``. ``message`` is a human-readable note for the infeasible cases.
    """

    status: CaptainOptimizerStatus
    lineups: tuple[CaptainLineup, ...]
    message: str


def _ordering_key(lineup: CaptainLineup) -> tuple[float, int, tuple[str, ...]]:
    """Deterministic sort key (§6): best points first, then cheapest, then names.

    Points descend (negated), salary ascends, and the full ordered name tuple
    (captain first, then sorted flex) breaks any remaining tie so the ranking
    is total and stable regardless of candidate input order.
    """
    return (-lineup.points, lineup.salary, lineup.fighter_names)


def optimize_captain_lineups(
    candidates: list[CaptainCandidate],
    top_n: int = 5,
    *,
    stack_mode: "StackMode | str" = StackMode.CASH,
    same_fight_pairs: "Iterable[Iterable[str]] | None" = None,
    captain: str | None = None,
) -> CaptainOptimizerResult:
    """Find the top ``top_n`` DK Captain lineups by projected points (§6 / §14.3).

    Brute force: every 6-fighter combination of ``candidates`` × each choice of
    Captain within it, keeping those at or under :data:`SALARY_CAP`, ranked by
    :func:`_ordering_key`.

    ``stack_mode`` selects the same-fight policy (§14.3). :data:`StackMode.CASH`
    (the **default**, the original C3 behavior) applies **no** same-fight
    exclusion — both fighters of a bout may appear together — and ignores
    ``same_fight_pairs``. :data:`StackMode.GPP` **rejects** any lineup whose six
    fighters contain both names of any pair in ``same_fight_pairs`` (the bout
    pairings). Called with neither argument, this is byte-for-byte the prior
    behavior.

    ``same_fight_pairs`` is an optional collection of unordered two-name pairs
    (the slate's bouts); it does **not** alter :class:`CaptainCandidate`. Names
    not in the pool are harmless.

    ``captain`` is an optional **captain pin** (design §14.4, the captain-leverage
    rule): when set, only lineups whose Captain is that fighter are considered —
    the rest of the slate still fills the five Fighter slots. ``None`` (the
    **default**) is the free-EV behavior, every candidate eligible to captain
    (byte-for-byte the C11a behavior). Use :func:`rank_captains_by_cptproj` to
    pick which captain to pin.

    Infeasibility is reported, never raised: fewer than six candidates yields
    ``NOT_ENOUGH_FIGHTERS`` and an empty lineup list; six or more with nothing
    under the cap (or, in GPP, nothing left after same-fight exclusion, or no
    lineup with the pinned ``captain``) yields ``NO_FEASIBLE_LINEUP``.

    Raises :class:`CaptainOptimizerError` only on malformed input: ``top_n < 1``,
    duplicate candidate names (identity would otherwise be ambiguous), an unknown
    ``stack_mode``, a malformed ``same_fight_pairs`` entry, or a ``captain`` pin
    naming a fighter absent from the pool.
    """
    if top_n < 1:
        raise CaptainOptimizerError(f"top_n must be >= 1, got {top_n}.")

    mode = _coerce_stack_mode(stack_mode)
    pairs = _normalize_same_fight_pairs(same_fight_pairs)
    exclude_same_fight = mode is StackMode.GPP and bool(pairs)

    names = [c.name for c in candidates]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise CaptainOptimizerError(
            f"Duplicate candidate name(s): {', '.join(duplicates)}. "
            f"Each fighter must appear at most once in the pool."
        )

    if captain is not None and captain not in names:
        raise CaptainOptimizerError(
            f"Pinned captain {captain!r} is not in the candidate pool."
        )

    if len(candidates) < LINEUP_SIZE:
        return CaptainOptimizerResult(
            status=CaptainOptimizerStatus.NOT_ENOUGH_FIGHTERS,
            lineups=(),
            message=(
                f"Need at least {LINEUP_SIZE} fighters to build a Captain "
                f"lineup; got {len(candidates)}."
            ),
        )

    # Dedup defensively on (captain, set-of-six): combinations are already
    # unique and names are unique, so this is belt-and-suspenders, but it keeps
    # the contract ("dedup identical captain + set") explicit and local.
    seen: set[tuple[str, tuple[str, ...]]] = set()
    lineups: list[CaptainLineup] = []

    for combo in combinations(candidates, LINEUP_SIZE):
        # GPP same-fight exclusion (§14.3): drop the whole 6-fighter set if it
        # rosters both fighters of any bout. This depends only on the set, not on
        # the captain choice, so it is checked once per combination. CASH (and GPP
        # with no pairs) never enters this branch — the original behavior.
        if exclude_same_fight:
            combo_names = frozenset(c.name for c in combo)
            if any(pair <= combo_names for pair in pairs):
                continue

        for captain_choice in combo:
            # Captain pin (§14.4): when a captain is pinned, only that fighter may
            # wear the C; the rest of the combo still fills the five Fighter slots.
            # None (the default) leaves every fighter eligible (free-EV, C11a).
            if captain is not None and captain_choice.name != captain:
                continue
            flex = [f for f in combo if f is not captain_choice]
            salary = captain_choice.captain_salary + sum(
                f.base_salary for f in flex
            )
            if salary > SALARY_CAP:
                continue

            flex_names = tuple(sorted(f.name for f in flex))
            key = (captain_choice.name, flex_names)
            if key in seen:
                continue
            seen.add(key)

            points = CAPTAIN_MULTIPLIER * captain_choice.projection + sum(
                f.projection for f in flex
            )
            lineups.append(
                CaptainLineup(
                    captain_name=captain_choice.name,
                    flex_names=flex_names,
                    salary=salary,
                    points=points,
                )
            )

    if not lineups:
        reason = f"No 6-fighter lineup fits under the ${SALARY_CAP:,} salary cap"
        if captain is not None:
            reason += f" with {captain} as Captain"
        if exclude_same_fight:
            reason += (
                " without rostering both fighters of a bout (GPP same-fight "
                "exclusion)"
            )
        return CaptainOptimizerResult(
            status=CaptainOptimizerStatus.NO_FEASIBLE_LINEUP,
            lineups=(),
            message=reason + ".",
        )

    lineups.sort(key=_ordering_key)
    return CaptainOptimizerResult(
        status=CaptainOptimizerStatus.OK,
        lineups=tuple(lineups[:top_n]),
        message=(
            f"Found {len(lineups)} feasible lineup(s); returning top "
            f"{min(top_n, len(lineups))}."
        ),
    )


@dataclass(frozen=True)
class CaptainRanking:
    """One captain's leverage-ranking entry (design §14.4).

    ``cptproj`` is ``CAPTAIN_MULTIPLIER × candidate.projection`` — the fighter's
    points *as Captain*, which is the leverage-ranking key. ``best_lineup`` is the
    highest-scoring feasible lineup with this fighter pinned as Captain under the
    build's ``stack_mode`` / ``same_fight_pairs`` (``None`` when no lineup with
    this captain fits the cap or survives the GPP same-fight exclusion). Entries
    are returned ordered by descending ``cptproj``.
    """

    captain_name: str
    cptproj: float
    best_lineup: CaptainLineup | None

    @property
    def best_total(self) -> float | None:
        """Best-lineup total points with this captain, or ``None`` if infeasible."""
        return None if self.best_lineup is None else self.best_lineup.points


def rank_captains_by_cptproj(
    candidates: list[CaptainCandidate],
    *,
    stack_mode: "StackMode | str" = StackMode.CASH,
    same_fight_pairs: "Iterable[Iterable[str]] | None" = None,
) -> tuple[CaptainRanking, ...]:
    """Rank every candidate as a Captain by ``CPTproj = 1.5 × projection`` (§14.4).

    The captain-leverage rule: pure EV in GPP captains the *cheapest* salary-
    efficient fighter to free cap (C11a's free-EV optimum), **not** the finisher.
    To surface the finish-favorite captain, rank captains by ``CPTproj`` (the
    captain's own 1.5× points), default to the top, and expose the ranked list so
    the UI can let the user pivot. This is **pure selection logic** — it neither
    changes :class:`CaptainCandidate` nor recomputes any projection; ``CPTproj``
    is simply 1.5× the projection already attached to the candidate.

    For each candidate this also reports its **best achievable lineup** with that
    fighter pinned as Captain under the given ``stack_mode`` / ``same_fight_pairs``
    (via :func:`optimize_captain_lineups` with ``captain=`` pinned), so the UI can
    show each captain's best-lineup total in the current stack mode. A captain
    with no feasible lineup carries ``best_lineup=None`` and still appears in the
    ranking — ``CPTproj`` is independent of feasibility.

    Entries are ordered by descending ``CPTproj``, then ascending captain name for
    a total, stable order. Duplicate names / malformed pairs / an unknown
    ``stack_mode`` raise the same :class:`CaptainOptimizerError` as
    :func:`optimize_captain_lineups` (the validation is delegated to it).
    """
    rankings: list[CaptainRanking] = []
    for candidate in candidates:
        result = optimize_captain_lineups(
            candidates,
            top_n=1,
            stack_mode=stack_mode,
            same_fight_pairs=same_fight_pairs,
            captain=candidate.name,
        )
        best = result.lineups[0] if result.lineups else None
        rankings.append(
            CaptainRanking(
                captain_name=candidate.name,
                cptproj=CAPTAIN_MULTIPLIER * candidate.projection,
                best_lineup=best,
            )
        )

    rankings.sort(key=lambda r: (-r.cptproj, r.captain_name))
    return tuple(rankings)
