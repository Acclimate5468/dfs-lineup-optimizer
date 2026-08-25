"""Projection v1 read-side aggregation (Phase B).

Phase B of ``docs/PROJECTION_V1_DESIGN.md`` §8: a thin read-only
aggregator that, given a ``slate_id``, returns the per-fighter input
bundle (salary, fight group, opponent signals, scheduled rounds, odds
win probability) needed by :func:`compute_projection_v1` (Phase A).

Pure read end to end:

- No INSERT / UPDATE / DELETE on any table.
- No odds recompute.
- No override mutation.
- No projection persistence (open question §10.1 — Phase B stays
  computed-on-read).
- Win probability is sourced from ``odds_match_results.effective_status``
  via the shared projection-eligibility predicate (Phase D.5.2 of
  ``ODDS_PERSISTENCE_DESIGN.md`` §16.9 / ``PROJECTION_V1_DESIGN.md`` §11):
  ``auto_match``, ``review_accepted`` (accept_match), and ``force_pair``
  feed projections; ``review_required`` / ``unmatched`` /
  ``review_rejected`` do not. ``match_status`` stays matcher/audit-only
  (§16.8). Because one predicate now governs both the Odds resolved view
  and the Build pool, the two cannot diverge (§16.9).

This module composes existing repositories (``FighterRepository``,
``FightGroupRepository``, ``OddsMatchResultRepository``,
``OddsRowRepository``); it does not alter their query semantics.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.db.repositories import (
    FightGroupRecord,
    FightGroupRepository,
    FighterRepository,
    OddsMatchResultRepository,
    OddsRowRepository,
)
from src.ingestion.effective_status_resolver import (
    is_projection_eligible_effective_status,
)
from src.projections.projection_service import ProjectionInputs
from src.utils.text_cleaning import normalize_name

ACTIVE_FIGHTER_STATUS = "active"


@dataclass(frozen=True)
class ProjectionInputBundle:
    """Per-fighter aggregated inputs for Projection v1.

    Wraps :class:`ProjectionInputs` with diagnostic context — the
    fighter's persisted display name and any per-fighter aggregation
    notes — so a later UI / Phase C consumer can render *why* an input
    is missing without re-querying. ``inputs`` feeds
    :func:`compute_projection_v1` directly.
    """

    fighter_name: str
    inputs: ProjectionInputs
    notes: tuple[str, ...] = ()


def aggregate_projection_inputs(
    conn: sqlite3.Connection, slate_id: int
) -> list[ProjectionInputBundle]:
    """Aggregate Projection v1 input bundles for every active fighter on
    ``slate_id`` from persisted state.

    Inputs assembled per fighter (design §2):

    - ``salary`` — from the ``fighters`` row.
    - ``scheduled_rounds`` — from the ``fight_groups`` row that contains
      the fighter, if any. Not silently defaulted to 3 when absent
      (design §5).
    - ``has_fight_group`` / ``has_opponent`` — structural flags set
      ``True`` when a slate fight group references the fighter and
      carries a non-empty opponent name.
    - ``implied_win_probability`` — the raw ``odds_rows.implied_probability``
      value for the ``odds_match_results`` row whose ``effective_status``
      is projection-eligible (``auto_match`` / ``review_accepted`` /
      ``force_pair``; §16.9) and whose ``fighter_id`` equals the fighter's
      id. ``review_required`` / ``unmatched`` / ``review_rejected`` rows do
      not contribute. For ``accept_match`` / ``force_pair`` bindings the
      ``fighter_id`` was written by D.5.1's apply pass (§16.5), so the
      previously-``NULL`` join is now populated. No no-vig two-way pairing
      is performed here — that refinement is left to a Phase C / Phase E
      pass so Phase B stays a thin read.

    Missing-input policy (design §5):

    - Fighters whose ``fighters.status`` is not ``'active'`` are
      excluded. Design §5's ``"fighter_status"`` tag is reserved for a
      future gating slice; until then this read layer simply omits
      inactive fighters so they cannot surface as ghost rows in
      downstream UI / projections.
    - Missing fight group → ``has_fight_group=False``,
      ``has_opponent=False``, ``scheduled_rounds=None``. Phase A then
      classifies the row as ``non_projectable`` with tag
      ``"fight_group"``.
    - Missing odds → ``implied_win_probability=None``. Phase A then
      classifies the row as ``missing_inputs`` with tag
      ``"win_probability"``.
    - Multiple projection-eligible rows for one fighter → use the row
      with the lowest ``odds_row_id`` (deterministic) and append a
      diagnostic note. D.5.1's already-bound check makes this rare, but
      it stays as a defensive deterministic tiebreak.
    - Multiple fight groups referencing one fighter → use the group
      with the lowest ``id`` (deterministic) and append a diagnostic
      note.
    - Unknown ``slate_id`` → empty list. Mirrors
      ``FighterRepository.list_for_slate`` (no rows means no bundles)
      and avoids inventing a "no such slate" error type for what is
      semantically the same outcome.

    Out of scope (design §6, §8 Phase B):

    - DB writes of any kind.
    - Fuzzy opponent inference — only conservative name normalization
      (``src.utils.text_cleaning.normalize_name``) is used to tie
      ``fighters`` rows to ``fight_groups`` rows.
    - Changing odds matching semantics — win probability still comes from
      ``odds_rows.implied_probability`` via the matched/bound odds row;
      D.5.2 only widens *which* rows qualify (from ``auto_match`` to the
      projection-eligible ``effective_status`` set), it does not re-run
      the matcher or invent a probability.
    - Triggering ``recompute_and_replace_match_results`` or any other
      recompute path.
    """
    slate_id = int(slate_id)

    fighters = [
        f
        for f in FighterRepository(conn).list_for_slate(slate_id)
        if f.status == ACTIVE_FIGHTER_STATUS
    ]
    if not fighters:
        return []

    fight_groups = FightGroupRepository(conn).list_for_slate(slate_id)
    match_results = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    odds_rows = OddsRowRepository(conn).list_for_slate(slate_id)

    odds_by_id = {row.id: row for row in odds_rows}
    fight_group_by_fighter = _index_fight_groups_by_fighter(fight_groups)
    eligible_matches_by_fighter = _index_projection_eligible_by_fighter(
        match_results
    )

    bundles: list[ProjectionInputBundle] = []
    for fighter in fighters:
        notes: list[str] = []

        groups = fight_group_by_fighter.get(_fighter_key(fighter.name), [])
        if not groups:
            scheduled_rounds: int | None = None
            has_fight_group = False
            has_opponent = False
        else:
            group = groups[0]
            scheduled_rounds = int(group.scheduled_rounds)
            has_fight_group = True
            opponent_name = _other_name(group, fighter.name)
            has_opponent = bool((opponent_name or "").strip())
            if len(groups) > 1:
                notes.append(
                    "multiple fight groups reference fighter; "
                    f"using fight_group_id={group.id}"
                )

        implied: float | None = None
        matches = eligible_matches_by_fighter.get(int(fighter.id), [])
        if matches:
            chosen = matches[0]
            odds_row = odds_by_id.get(int(chosen.odds_row_id))
            if odds_row is None or odds_row.implied_probability is None:
                notes.append(
                    "projection-eligible odds row missing implied_probability "
                    f"(odds_row_id={chosen.odds_row_id})"
                )
            else:
                implied = float(odds_row.implied_probability)
            if len(matches) > 1:
                notes.append(
                    "multiple projection-eligible odds rows for fighter; "
                    f"using odds_row_id={chosen.odds_row_id}"
                )

        inputs = ProjectionInputs(
            fighter_id=int(fighter.id),
            slate_id=slate_id,
            salary=int(fighter.salary),
            implied_win_probability=implied,
            scheduled_rounds=scheduled_rounds,
            has_fight_group=has_fight_group,
            has_opponent=has_opponent,
        )
        bundles.append(
            ProjectionInputBundle(
                fighter_name=fighter.name,
                inputs=inputs,
                notes=tuple(notes),
            )
        )

    return bundles


def _fighter_key(name: str) -> str:
    """Conservative normalization for ``fighters`` ↔ ``fight_groups`` joins.

    Mirrors ``src.utils.text_cleaning.normalize_name`` — case-insensitive,
    NFKD-folded, whitespace-collapsed. Fuzzy / aggressive matching
    (nickname expansion, suffix stripping) is intentionally NOT used
    here: Phase B's join is "same fighter" only, not "probably the same
    fighter".
    """
    return normalize_name(name)


def _index_fight_groups_by_fighter(
    fight_groups: list[FightGroupRecord],
) -> dict[str, list[FightGroupRecord]]:
    index: dict[str, list[FightGroupRecord]] = {}
    for group in fight_groups:
        for name in (group.fighter_1_name, group.fighter_2_name):
            key = _fighter_key(name)
            if not key:
                continue
            index.setdefault(key, []).append(group)
    for key in index:
        index[key].sort(key=lambda g: int(g.id))
    return index


def _index_projection_eligible_by_fighter(match_results) -> dict[int, list]:
    """Index ``odds_match_results`` rows that feed projections, keyed by
    ``fighter_id``.

    Eligibility is decided by ``effective_status`` (the post-override view),
    not ``match_status`` (Phase D.5.2 / §16.9). A force-paired ``unmatched``
    row qualifies once its ``effective_status`` is ``force_pair`` and D.5.1's
    apply pass has written the bound ``fighter_id``; a rejected row
    (``effective_status='review_rejected'``) is excluded even though its
    ``match_status`` may still read ``auto_match``.
    """
    index: dict[int, list] = {}
    for result in match_results:
        if not is_projection_eligible_effective_status(result.effective_status):
            continue
        if result.fighter_id is None:
            continue
        index.setdefault(int(result.fighter_id), []).append(result)
    for fid in index:
        index[fid].sort(key=lambda r: int(r.odds_row_id))
    return index


def _other_name(group: FightGroupRecord, fighter_name: str) -> str | None:
    key = _fighter_key(fighter_name)
    if _fighter_key(group.fighter_1_name) == key:
        return group.fighter_2_name
    if _fighter_key(group.fighter_2_name) == key:
        return group.fighter_1_name
    return None
