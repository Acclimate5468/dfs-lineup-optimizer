"""Pluggable projection / build **method** interface for Captain mode.

Realizes `docs/CAPTAIN_MODE_DESIGN.md` §7 (pluggable build methods) — the
projection step is an *interface*, not a hardwired formula, so the current
**Heuristic** engine and a future **Monte Carlo** engine can coexist and be
selected per build. Two rules from the design are load-bearing here:

  1. **Nothing is deleted.** The Heuristic is permanent. A new method (a
     finish-adjusted heuristic, Monte Carlo, …) is added by *registering* an
     additional implementation via :func:`register_method` — never by editing
     or removing this one (§7, and `docs/DEVELOPMENT_NOTES.md` §3 additive rule).
  2. **The optimizer and this interface are stable.** A future method plugs in
     behind the same input/output contract — per-fighter
     :class:`FighterProjectionInput` in, a list of
     :class:`~src.captain.optimizer.CaptainCandidate` out — so registering a
     second engine requires **no change** to the optimizer or to this module
     (design §7).

This module is additive and lives beside Classic per §3: it **reuses** the
Classic projection math by *importing* it (the sanctioned reuse) and does not
duplicate, reimplement, or edit it. No I/O: no DB, no Streamlit, no network, no
file writes. Deterministic.

Two engines are registered (design §7, §14):

  - :class:`HeuristicMethod` — the **default**, ``win_prob×70 + value_gap_bonus
    + five_round_bonus`` (`docs/DEVELOPMENT_NOTES.md` §4). Permanent, never removed.
  - :class:`FinishAwareMethod` — the **MOV finish-aware** method (slice **C10**,
    design §14.2): ``adjProj = default_projection(...) + K * finish_signal``,
    where ``finish_signal`` is the per-fighter de-vigged probability of *winning
    inside the distance* from :mod:`src.captain.finish_signal` (C9) and ``K`` is a
    single editable, **UNVALIDATED** knob (default :data:`FINISH_BONUS_K_DEFAULT`).
    It **reuses** :func:`~src.projections.default_projection.default_projection`
    unchanged, so ``K == 0`` (or a ``None`` finish signal) reproduces the
    Heuristic exactly. It is **EXPERIMENTAL** — ``K`` has never been graded
    against realized DK points — so it is **never the default**. This method
    **supersedes and retires the C7 finish-aware v2 method** (design §14.6):
    v2's league-average ``finish_share = win_prob`` could not tell a finisher
    from a decision machine. :mod:`src.projections.finish_model` is kept
    untouched as the scaffold for the future Tier-2 method (§14.7) and is **not**
    used here.

Each method carries light UI metadata — a ``display_label`` and an
``experimental`` flag (queried via :func:`method_label` / :func:`is_experimental`
with safe defaults) — so the UI can label / caveat the engine. This is additive:
the :class:`ProjectionMethod` Protocol stays a minimal ``name`` + ``project``
contract, so a future engine that omits the metadata still satisfies it.

STATUS: two engines registered (Heuristic default, MOV finish-aware experimental).
Tested in isolation / integration against the optimizer and exercised in the UI
(C5 + the method selector).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.captain.optimizer import CaptainCandidate
from src.projections.default_projection import default_projection

# Canonical registry key for the Heuristic engine. Method names are matched
# case-insensitively (trimmed + lowercased) so a UI selector value never has to
# match casing exactly; this is the normalized form they collapse to.
HEURISTIC_METHOD_NAME = "heuristic"

# Canonical registry key for the MOV finish-aware engine (slice C10, the
# Finish-aware slot — it superseded the retired C7 v2 method). Experimental.
FINISH_AWARE_METHOD_NAME = "finish_aware"

# Default finish-bonus coefficient K (design §14.2) — a single editable,
# UNVALIDATED knob. ``adjProj = baseProj + K * finish_signal``; ``K == 0``
# reproduces the Heuristic exactly (the regression anchor).
FINISH_BONUS_K_DEFAULT = 20.0

# Scheduled-round values DK UFC fights take (§5): a normal bout is 3 rounds, a
# title / main event is 5. The input contract is explicit about this set.
_VALID_SCHEDULED_ROUNDS = frozenset({3, 5})


class UnknownProjectionMethodError(ValueError):
    """Raised by :func:`get_method` when no method is registered under a name.

    Carries the offending name and the list of available methods so a caller
    (or a UI selector) can surface a clear, actionable error rather than a bare
    ``KeyError``.
    """


class DuplicateProjectionMethodError(ValueError):
    """Raised by :func:`register_method` when a name is already registered.

    Registration is additive (§7) but **not** silently overwriting: re-using a
    live name is a programming error, so it is rejected rather than clobbering
    an existing engine (which could include the permanent Heuristic).
    """


@dataclass(frozen=True)
class FighterProjectionInput:
    """Per-fighter input bundle handed to a projection method (§7 input contract).

    One record per *eligible* fighter — the caller has already collapsed the
    CPT/F salary rows (C2) and excluded any "out" fighters, so a method projects
    exactly the pool it is given and applies no exclusion of its own.

    Fields:
      - ``name`` — the fighter identity (the DK ``Name``), carried through to
        the resulting :class:`~src.captain.optimizer.CaptainCandidate`.
      - ``base_salary`` / ``captain_salary`` — the two DK Captain salaries; both
        pass through to the candidate untouched. The 1.5× Captain multiplier is
        the **optimizer's** job, not the method's (design §6 / §7).
      - ``win_prob`` — the fighter's **already de-vigged** win probability in
        ``[0, 1]`` (the method does not de-vig; that is the odds pipeline). It
        stays the **moneyline** de-vig (never method-implied, design §14.2).
      - ``scheduled_rounds`` — 3 (normal) or 5 (title / main event), feeding the
        Classic five-round bonus.
      - ``finish_signal`` — *optional* per-fighter de-vigged probability of
        *winning inside the distance* in ``[0, 1]``, produced by
        :mod:`src.captain.finish_signal` (C9 — its
        :attr:`~src.captain.finish_signal.FighterFinishSignal.finish_signal`).
        Consumed only by the MOV :class:`FinishAwareMethod` (design §14.2);
        ``None`` (the default — no method-of-victory odds) means **no finish
        bonus**, so the finish-aware projection degrades to the base projection.
    """

    name: str
    base_salary: int
    captain_salary: int
    win_prob: float
    scheduled_rounds: int
    finish_signal: float | None = None

    def __post_init__(self) -> None:
        if not self.name or not str(self.name).strip():
            raise ValueError("FighterProjectionInput.name must be non-empty.")
        p = float(self.win_prob)
        if not 0.0 <= p <= 1.0:
            raise ValueError(
                f"FighterProjectionInput.win_prob for {self.name!r} must be in "
                f"[0, 1]; got {self.win_prob!r}."
            )
        if int(self.scheduled_rounds) not in _VALID_SCHEDULED_ROUNDS:
            raise ValueError(
                f"FighterProjectionInput.scheduled_rounds for {self.name!r} "
                f"must be 3 or 5; got {self.scheduled_rounds!r}."
            )
        if int(self.base_salary) < 0 or int(self.captain_salary) < 0:
            raise ValueError(
                f"FighterProjectionInput salaries for {self.name!r} must be "
                f"non-negative."
            )
        if self.finish_signal is not None:
            try:
                signal = float(self.finish_signal)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"FighterProjectionInput.finish_signal for {self.name!r} "
                    f"must be numeric or None; got {self.finish_signal!r}."
                ) from exc
            if not 0.0 <= signal <= 1.0:
                raise ValueError(
                    f"FighterProjectionInput.finish_signal for {self.name!r} "
                    f"must be a probability in [0, 1]; got {self.finish_signal!r}."
                )


@runtime_checkable
class ProjectionMethod(Protocol):
    """The pluggable build-method contract (design §7).

    A method has a stable ``name`` (its registry key) and a pure
    :meth:`project` that maps the per-fighter input bundles to optimizer-ready
    candidates. It is intentionally a structural :class:`~typing.Protocol`, not
    a base class: a future engine (Monte Carlo, a finish-adjusted heuristic)
    satisfies the contract by *shape* and registers via :func:`register_method`,
    so adding one needs no edit to this interface or to the optimizer (§7).
    """

    name: str

    def project(
        self, fighter_inputs: Sequence[FighterProjectionInput]
    ) -> list[CaptainCandidate]:
        """Map fighter inputs to optimizer candidates (base projection only)."""
        ...


class HeuristicMethod:
    """The default Heuristic engine — the v0 method, and a permanent one (§7).

    Each fighter's projection is produced by **reusing the Classic heuristic**
    :func:`~src.projections.default_projection.default_projection`
    (``win_prob × 70 + value_gap_bonus + five_round_bonus``) — imported, never
    re-implemented (`docs/DEVELOPMENT_NOTES.md` §3 / §4). The value-gap bonus keys on the
    fighter's **base** salary; the resulting projection is the *base* points,
    leaving the 1.5× Captain multiplier to the optimizer (design §6 / §7).
    """

    name = HEURISTIC_METHOD_NAME
    # UI metadata (queried via method_label / is_experimental). The Heuristic is
    # the validated v0 default, so it is NOT experimental.
    display_label = "Heuristic (default)"
    experimental = False

    def project(
        self, fighter_inputs: Sequence[FighterProjectionInput]
    ) -> list[CaptainCandidate]:
        """Project each fighter via the Classic heuristic → candidates (§7).

        Order-preserving and pure: one :class:`CaptainCandidate` per input, in
        input order, with both salaries passed through unchanged.
        """
        candidates: list[CaptainCandidate] = []
        for fighter in fighter_inputs:
            projection = default_projection(
                fighter.win_prob,
                fighter.base_salary,
                fighter.scheduled_rounds,
            )
            # EXTENSION POINT — finish equity (DEFERRED, design §7). A finish-
            # adjusted projection is NOT added here: it arrives later as its own
            # *additional* registered method (a finish-aware heuristic or a
            # Monte Carlo engine), leaving this Heuristic and the optimizer
            # untouched. Do not bolt a finish term onto this method.
            candidates.append(
                CaptainCandidate(
                    name=fighter.name,
                    base_salary=fighter.base_salary,
                    captain_salary=fighter.captain_salary,
                    projection=projection,
                )
            )
        return candidates


class FinishAwareMethod:
    """The MOV finish-aware method — the second selectable engine (§14.2, C10).

    Each fighter's projection is the **Heuristic base** plus a method-of-victory
    **finish bonus** (design §14.2)::

        adjProj(f) = default_projection(win_prob, base_salary, rounds)
                     + K * finish_signal(f)

    :func:`~src.projections.default_projection.default_projection` is **reused
    unchanged** (imported, `docs/DEVELOPMENT_NOTES.md` §3 / §4), so the v0 base — and the §14.5
    anchor — are preserved; ``win_prob`` stays the moneyline de-vig (never
    method-implied, §14.2). ``finish_signal`` is the per-fighter de-vigged
    probability of *winning inside the distance* produced by
    :mod:`src.captain.finish_signal` (C9), carried on
    :class:`FighterProjectionInput`. When it is ``None`` (no method-of-victory
    odds yet) the bonus is ``0`` and ``adjProj == baseProj`` — graceful, and the
    exact-Heuristic anchor at ``K == 0`` (design §14.2).

    ``K`` (default :data:`FINISH_BONUS_K_DEFAULT`) is a single **editable,
    UNVALIDATED** knob (§14.2): it has never been graded against realized DK
    points, so this method is **EXPERIMENTAL** and **never the default**.

    This **supersedes and retires the C7 finish-aware v2 method** (design §14.6):
    v2 used a league-average ``finish_share = win_prob`` that treated a finisher
    and a decision machine identically, whereas the MOV signal prices a real
    per-fighter finish. :mod:`src.projections.finish_model` is kept untouched as
    the scaffold for the future Tier-2 market-implied method (§14.7); this method
    does **not** use it.

    Input handling mirrors :class:`HeuristicMethod`: one candidate per input, in
    input order, both salaries passed through; the 1.5× Captain multiplier is the
    optimizer's job (design §6 / §7).
    """

    name = FINISH_AWARE_METHOD_NAME
    display_label = "Finish-aware (MOV)"
    experimental = True

    def __init__(self, finish_bonus_k: float = FINISH_BONUS_K_DEFAULT) -> None:
        self.finish_bonus_k = float(finish_bonus_k)

    def project(
        self, fighter_inputs: Sequence[FighterProjectionInput]
    ) -> list[CaptainCandidate]:
        """Project each fighter via ``baseProj + K·finish_signal`` → candidates.

        Order-preserving and pure: one :class:`CaptainCandidate` per input, in
        input order, both salaries passed through unchanged. A ``None`` finish
        signal contributes no bonus (``adjProj == baseProj``), so a slate with no
        method-of-victory odds reproduces the Heuristic exactly (design §14.2).
        """
        candidates: list[CaptainCandidate] = []
        for fighter in fighter_inputs:
            base = default_projection(
                fighter.win_prob,
                fighter.base_salary,
                fighter.scheduled_rounds,
            )
            adj = base + self.finish_bonus_k * (fighter.finish_signal or 0.0)
            candidates.append(
                CaptainCandidate(
                    name=fighter.name,
                    base_salary=fighter.base_salary,
                    captain_salary=fighter.captain_salary,
                    projection=adj,
                )
            )
        return candidates


# --- Method registry / selector (§7) ---------------------------------------
#
# The registry is the seam that lets a future method be selected by name and
# registered without touching this interface or the optimizer. It is seeded with
# the permanent Heuristic; additional engines call register_method().

_METHODS: dict[str, ProjectionMethod] = {}


def _normalize(name: str) -> str:
    return str(name).strip().lower()


def register_method(method: ProjectionMethod) -> None:
    """Register an additional projection method under its ``name`` (§7).

    This is the additive seam for future engines (Monte Carlo, a finish-aware
    heuristic). Registration never overwrites an existing name — re-using a live
    key raises :class:`DuplicateProjectionMethodError` rather than clobbering an
    engine (the Heuristic must stay reachable, §7).
    """
    key = _normalize(method.name)
    if not key:
        raise ValueError("Projection method name must be non-empty.")
    if key in _METHODS:
        raise DuplicateProjectionMethodError(
            f"A projection method named {key!r} is already registered."
        )
    _METHODS[key] = method


def get_method(name: str) -> ProjectionMethod:
    """Return the registered method for ``name`` (case-insensitive); else raise.

    Raises :class:`UnknownProjectionMethodError` with the available names when
    nothing is registered under ``name`` — a clean, actionable failure for the
    UI selector.
    """
    key = _normalize(name)
    try:
        return _METHODS[key]
    except KeyError:
        raise UnknownProjectionMethodError(
            f"Unknown projection method {name!r}. "
            f"Available: {', '.join(available_methods()) or '(none)'}."
        ) from None


def available_methods() -> tuple[str, ...]:
    """Return the registered method names, sorted, for selectors / messages."""
    return tuple(sorted(_METHODS))


def method_label(name: str) -> str:
    """Human-readable label for a registered method (its ``name`` as fallback).

    A method may carry an optional ``display_label`` for the UI; engines that
    omit it (a minimal future method) fall back to their registry ``name`` so
    the selector always has something to show.
    """
    method = get_method(name)
    return str(getattr(method, "display_label", None) or method.name)


def is_experimental(name: str) -> bool:
    """Whether a registered method is flagged **experimental / unvalidated**.

    Defaults to ``False`` for any engine that does not declare the flag, so the
    UI never mislabels a method as experimental by accident; the finish-aware v2
    engine sets it ``True`` (design §7).
    """
    return bool(getattr(get_method(name), "experimental", False))


# Seed the registry with the permanent default first, then the additive
# experimental MOV finish-aware engine (slice C10 — it occupies the Finish-aware
# slot vacated by the retired C7 v2 method, §14.6). The registered instance uses
# the default K; the UI builds a fresh instance for an edited K. The Heuristic is
# always present and is the default (§7); future methods are added beside them via
# register_method(). Order of registration does not affect the default — the UI
# selects the Heuristic explicitly.
register_method(HeuristicMethod())
register_method(FinishAwareMethod())
