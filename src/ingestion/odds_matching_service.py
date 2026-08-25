"""Pure odds-matching service (Phase C.2 of docs/ODDS_PERSISTENCE_DESIGN.md).

Reads persisted ``odds_rows`` and slate fighter records, runs the existing
``match_odds_to_dk`` pipeline, and returns one in-memory ``OddsMatchResultRecord``
per odds row. No DB writes — the write path is Phase C.3 and lives in a
future ``OddsMatchResultRepository``.

The record shape mirrors the eventual ``odds_match_results`` row (design §5.2):
slate / odds-row identifiers, the matcher's verdict, candidate context, and a
mirrored ``effective_status`` field that — in Phase C — always equals
``match_status`` (design §14.6). Phase D will be the first slice where the
two diverge.

Matching behavior is delegated to ``odds_matching.match_odds_to_dk``; this
module does not alter thresholds, normalization, or opponent-check rules.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass

from src.db.repositories import (
    FightGroupRecord,
    FightGroupRepository,
    FighterRecord,
    FighterRepository,
    ManualMatchOverrideRecord,
    ManualMatchOverrideRepository,
    OddsMatchResultRepository,
    OddsRowRecord,
    OddsRowRepository,
)
from src.ingestion.effective_status_resolver import (
    BINDING_OVERRIDE_TYPES,
    RESOLUTION_OVERRIDE_TYPES,
    resolve_match_binding,
)
from src.ingestion.name_matching import normalize_name_aggressive
from src.ingestion.odds_matching import (
    OddsRowInput,
    OpponentContext,
    match_odds_to_dk,
)
from src.utils.text_cleaning import normalize_name

ACTIVE_FIGHTER_STATUS = "active"


class EmptyDkRosterError(RuntimeError):
    """Raised when a slate has no active ``fighters`` rows to match against.

    Design §14.3 mode B: the persistence pass refuses to fabricate
    ``unmatched`` results before the salary CSV has been imported. The caller
    surfaces this to the user as "import the salary CSV before recomputing."
    """

    def __init__(self, slate_id: int) -> None:
        super().__init__(
            f"slate #{slate_id} has no active DK fighters — import the salary "
            "CSV before computing match results"
        )
        self.slate_id = slate_id


@dataclass(frozen=True)
class OddsMatchResultRecord:
    """In-memory shape of one persisted match result.

    Field names mirror the ``odds_match_results`` columns (design §5.2) so a
    later repository can write the record without renaming. ``candidates``
    and ``notes`` are kept as tuples here — JSON serialization is the
    repository's responsibility.
    """

    slate_id: int
    odds_row_id: int
    odds_row_key: str
    fighter_id: int | None
    match_status: str
    effective_status: str
    match_stage: str
    match_score: int
    preferred_candidate: str | None
    opponent_check: str
    candidates: tuple[str, ...]
    notes: tuple[str, ...]


def compute_match_results(
    *,
    slate_id: int,
    odds_rows: list[OddsRowRecord],
    fighters: list[FighterRecord],
    fight_groups: list[FightGroupRecord] | None = None,
) -> list[OddsMatchResultRecord]:
    """Compute match results for one slate purely in memory.

    Loads only what the caller already supplies — no DB access. Returns one
    record per input ``odds_row`` in the order given.

    Raises ``EmptyDkRosterError`` when ``fighters`` contains no rows with
    ``status='active'``: matching against an empty roster would emit a bag
    of ``unmatched`` rows that the persistence layer would have to throw
    away on the next run (design §14.3 mode B).
    """
    active_fighters = [f for f in fighters if f.status == ACTIVE_FIGHTER_STATUS]
    if not active_fighters:
        raise EmptyDkRosterError(slate_id)

    if not odds_rows:
        return []

    dk_names = [f.name for f in active_fighters]
    fighter_id_by_name = {f.name: f.id for f in active_fighters}
    opponents = _build_opponent_context(dk_names, fight_groups or [])

    matcher_inputs = [
        OddsRowInput(
            fighter=row.fighter_name_raw,
            opponent=row.opponent_name_raw,
            row_id=str(row.id),
        )
        for row in odds_rows
    ]
    matcher_results = match_odds_to_dk(
        dk_names, matcher_inputs, opponents=opponents
    )

    return [
        _to_record(slate_id, row, result, fighter_id_by_name)
        for row, result in zip(odds_rows, matcher_results)
    ]


def _to_record(
    slate_id: int,
    odds_row: OddsRowRecord,
    matcher_result,
    fighter_id_by_name: dict[str, int],
) -> OddsMatchResultRecord:
    fighter_id = (
        fighter_id_by_name.get(matcher_result.dk_fighter)
        if matcher_result.dk_fighter is not None
        else None
    )
    return OddsMatchResultRecord(
        slate_id=slate_id,
        odds_row_id=odds_row.id,
        odds_row_key=odds_row.odds_row_key,
        fighter_id=fighter_id,
        match_status=matcher_result.status,
        effective_status=matcher_result.status,
        match_stage=matcher_result.stage,
        match_score=matcher_result.score,
        preferred_candidate=matcher_result.preferred_candidate,
        opponent_check=matcher_result.opponent_check,
        candidates=tuple(matcher_result.candidates),
        notes=tuple(matcher_result.notes),
    )


def _build_opponent_context(
    dk_fighters: list[str],
    fight_groups: list[FightGroupRecord],
) -> dict[str, OpponentContext]:
    """Map DK fighter names → expected opponent from saved fight groups.

    Mirrors the ``_build_opponent_context`` helper currently embedded in
    ``app/pages/03_odds.py``. Phase C.5+ will collapse the two copies into
    this module's version; until then both produce identical results.
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


@dataclass(frozen=True)
class RecomputeSummary:
    """Small summary returned by ``recompute_and_replace_match_results``.

    ``total`` is the row count just persisted. ``status_counts`` maps each
    ``match_status`` value (``auto_match`` / ``review_required`` /
    ``unmatched``) to its count. Statuses with zero rows are omitted.
    ``apply`` is the ``ApplyOverridesSummary`` from the override apply pass
    that runs inside the same transaction as the replace (Phase D.4.3.b);
    on a fresh slate with no active overrides, ``apply.rows_updated`` is 0.
    """

    slate_id: int
    total: int
    status_counts: dict[str, int]
    apply: ApplyOverridesSummary


def recompute_and_replace_match_results(
    conn: sqlite3.Connection, slate_id: int
) -> RecomputeSummary:
    """Phase C.4 driver + Phase D.4.3.b override-apply composition.

    Reads persisted ``odds_rows`` and slate fighters (plus any fight groups
    for opponent context), runs ``compute_match_results``, and persists the
    result via ``OddsMatchResultRepository._replace_for_slate_unlocked``
    followed by ``_apply_overrides_unlocked`` — both inside a single
    ``with conn:`` block so a failure during the apply pass rolls back
    the replace as well.

    Behavior matches design §14.1–§14.3 + §15.6:

    - When ``odds_rows`` is empty for the slate, the unlocked replace is
      still called with an empty list; this clears any previously persisted
      match results for the slate (per the §11 reset-behavior table) and
      the returned ``total`` is 0. The apply pass then runs against the now
      empty result set — any active reject overrides surface in
      ``apply.stale_override_ids``.
    - When the slate has no active fighters, ``EmptyDkRosterError`` is
      raised *before* the transaction opens — prior persisted results for
      the slate survive intact (§13.12 lean: refuse rather than wipe).
    - Other slates' persisted results are never touched — both the unlocked
      replace and the apply pass are slate-scoped.

    The apply pass writes ``effective_status`` only. ``match_status`` is
    set by the replace and is never modified afterwards.

    Returns a ``RecomputeSummary`` with the persisted row count, a
    per-status breakdown, and the apply summary.
    """
    slate_id = int(slate_id)
    odds_rows = OddsRowRepository(conn).list_for_slate(slate_id)
    fighters = FighterRepository(conn).list_for_slate(slate_id)
    fight_groups = FightGroupRepository(conn).list_for_slate(slate_id)

    records = compute_match_results(
        slate_id=slate_id,
        odds_rows=odds_rows,
        fighters=fighters,
        fight_groups=fight_groups,
    )

    with conn:
        OddsMatchResultRepository(conn)._replace_for_slate_unlocked(
            slate_id, records
        )
        apply_summary = _apply_overrides_unlocked(conn, slate_id)

    counts = Counter(r.match_status for r in records)
    return RecomputeSummary(
        slate_id=slate_id,
        total=len(records),
        status_counts=dict(counts),
        apply=apply_summary,
    )


@dataclass(frozen=True)
class ApplyOverridesSummary:
    """Outcome of one ``apply_effective_status_overrides_for_slate`` run.

    ``rows_updated`` counts ``odds_match_results`` rows whose
    ``effective_status`` *or* bound ``fighter_id`` actually changed
    (D.5.1 added the ``fighter_id`` half — accept/force bindings, §16.5).
    ``stale_override_ids`` lists active *resolution-set* overrides that
    cannot be applied (design §15.4 / §16.12): a ``reject_match`` /
    ``accept_match`` / ``force_pair`` whose ``odds_row_key`` has no
    matching result row, or an ``accept_match`` / ``force_pair`` whose
    bound fighter is inactive. The order matches
    ``ManualMatchOverrideRepository.list_active_for_slate`` (created_at
    ASC, id ASC).
    """

    slate_id: int
    rows_updated: int
    stale_override_ids: list[int]


def apply_effective_status_overrides_for_slate(
    conn: sqlite3.Connection, slate_id: int
) -> ApplyOverridesSummary:
    """Apply active manual overrides to persisted ``effective_status``.

    Phase D.4.2 of ``docs/ODDS_PERSISTENCE_DESIGN.md`` §15.6 / §15.9.

    Composition:

    - ``ManualMatchOverrideRepository.list_active_for_slate`` already
      filters ``superseded_at IS NULL``.
    - ``OddsMatchResultRepository.list_for_slate`` provides the row set.
    - ``resolve_match_binding`` (Phase D.4.1 / D.5.1, pure) decides the
      new ``(effective_status, fighter_id)`` per row.

    Updates ``odds_match_results.effective_status`` and — for
    ``accept_match`` / ``force_pair`` bindings only — ``fighter_id``
    (§16.5). Never touches ``match_status``, ``match_stage``,
    ``match_score``, ``computed_at``, the raw ``odds_rows``, or any other
    column. Never inserts or deletes rows. Slate-scoped: other slates are
    not read or written.

    Self-healing: with no applicable override, the resolver returns the
    row's ``match_status`` and the matcher's ``fighter_id``; a row whose
    binding was set by a now-superseded override is reset on the next
    call. A binding to a since-deactivated fighter is treated as stale
    (§16.12): the binding is not written and the override id is reported
    in ``stale_override_ids``. Idempotent — a second call with no
    intervening change writes zero rows.

    Direct callers (no surrounding write) hit this entry point.
    ``recompute_and_replace_match_results`` and
    ``record_reject_match_override`` compose ``_apply_overrides_unlocked``
    inside their own ``with conn:`` block so the apply rolls back together
    with the preceding write on failure.
    """
    slate_id = int(slate_id)
    with conn:
        return _apply_overrides_unlocked(conn, slate_id)


def _apply_overrides_unlocked(
    conn: sqlite3.Connection, slate_id: int
) -> ApplyOverridesSummary:
    """Worker for ``apply_effective_status_overrides_for_slate``.

    Performs the read + UPDATE sequence without managing the transaction
    so Phase D.4.3 composers (``recompute_and_replace_match_results``,
    ``record_reject_match_override``) can invoke it from inside an
    externally-provided ``with conn:`` block alongside other writes.
    """
    slate_id = int(slate_id)
    active_overrides = ManualMatchOverrideRepository(
        conn
    ).list_active_for_slate(slate_id)
    results = OddsMatchResultRepository(conn).list_for_slate(slate_id)
    result_keys = {r.odds_row_key for r in results}
    active_fighter_ids = {
        f.id
        for f in FighterRepository(conn).list_for_slate(slate_id)
        if f.status == ACTIVE_FIGHTER_STATUS
    }

    # Partition active overrides into the set the resolver may act on and
    # the stale set (§15.4 / §16.12). An override is stale when it cannot
    # be applied: an orphaned resolution override (key has no result row),
    # or a binding to an inactive fighter. The inactive-fighter binding is
    # also withheld from the resolver so its binding is never written; an
    # orphaned override is harmless to pass (no result row matches it) and
    # is left in to keep reject semantics byte-identical to D.4.
    stale_override_ids: list[int] = []
    resolver_overrides = []
    for ov in active_overrides:
        is_binding = ov.override_type in BINDING_OVERRIDE_TYPES
        orphan = (
            ov.override_type in RESOLUTION_OVERRIDE_TYPES
            and ov.odds_row_key is not None
            and ov.odds_row_key not in result_keys
        )
        inactive_fighter = (
            is_binding
            and ov.fighter_id is not None
            and ov.fighter_id not in active_fighter_ids
        )
        if orphan or inactive_fighter:
            stale_override_ids.append(ov.id)
        if inactive_fighter:
            continue
        resolver_overrides.append(ov)

    rows_updated = 0
    for r in results:
        binding = resolve_match_binding(r, resolver_overrides)
        new_effective = binding.effective_status
        if (
            new_effective
            not in OddsMatchResultRepository.ALLOWED_EFFECTIVE_STATUSES
        ):
            raise RuntimeError(
                "resolve_match_binding returned unexpected effective_status "
                f"{new_effective!r} for slate #{slate_id} "
                f"odds_row_id={r.odds_row_id}; expected one of "
                f"{sorted(OddsMatchResultRepository.ALLOWED_EFFECTIVE_STATUSES)}"
            )
        new_fighter_id = binding.fighter_id
        if new_effective == r.effective_status and new_fighter_id == r.fighter_id:
            continue
        conn.execute(
            "UPDATE odds_match_results SET effective_status = ?, "
            "fighter_id = ? WHERE slate_id = ? AND odds_row_id = ?",
            (new_effective, new_fighter_id, slate_id, int(r.odds_row_id)),
        )
        rows_updated += 1

    return ApplyOverridesSummary(
        slate_id=slate_id,
        rows_updated=rows_updated,
        stale_override_ids=stale_override_ids,
    )


@dataclass(frozen=True)
class RejectMatchOverrideResult:
    """Outcome of ``record_reject_match_override``.

    ``override`` is the freshly-inserted ``manual_match_overrides`` row
    (``superseded_at`` is ``None``). ``apply`` is the
    ``ApplyOverridesSummary`` from the override apply pass that runs in
    the same transaction as the insert; if the inserted override targets
    an ``odds_row_key`` with no persisted match result row,
    ``apply.stale_override_ids`` contains the new override's id
    (design §15.4).
    """

    override: ManualMatchOverrideRecord
    apply: ApplyOverridesSummary


def record_reject_match_override(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    odds_row_key: str,
    fighter_id: int | None = None,
    reason: str | None = None,
) -> RejectMatchOverrideResult:
    """Insert a ``reject_match`` override and apply effective_status in one
    transaction (Phase D.4.3.b of ``docs/ODDS_PERSISTENCE_DESIGN.md`` §15.6).

    Wraps ``ManualMatchOverrideRepository._add_override_unlocked`` and
    ``_apply_overrides_unlocked`` in a single ``with conn:`` block so a
    failure during the apply pass rolls back the override insert too —
    the override row never becomes durable without the corresponding
    ``effective_status`` write.

    The override insert reuses ``add_override``'s validation surface
    (slate / odds_row_key / fighter_id existence, payload rejection,
    reason normalization, supersession of any prior active reject on the
    same ``(slate_id, odds_row_key)``); validation failures raise
    ``ValueError`` before any DB write, so a bad input cannot silently
    wipe the prior active reject. ``add_override``'s public method is
    unchanged.

    Slate-scoped: every read and write is keyed on ``slate_id``; another
    slate's overrides or persisted match results are never touched.

    Not called by the Odds page yet — that wiring is Phase D.4.3.c.
    """
    slate_id = int(slate_id)
    repo = ManualMatchOverrideRepository(conn)

    with conn:
        new_id = repo._add_override_unlocked(
            slate_id=slate_id,
            override_type="reject_match",
            odds_row_key=odds_row_key,
            fighter_id=fighter_id,
            reason=reason,
        )
        row = conn.execute(
            "SELECT id, slate_id, odds_row_key, fighter_id, override_type, "
            "payload_json, reason, created_at, superseded_at "
            "FROM manual_match_overrides WHERE id = ?",
            (new_id,),
        ).fetchone()
        override = ManualMatchOverrideRecord(
            id=int(row[0]),
            slate_id=int(row[1]),
            odds_row_key=row[2],
            fighter_id=(int(row[3]) if row[3] is not None else None),
            override_type=row[4],
            payload_json=row[5],
            reason=row[6],
            created_at=row[7],
            superseded_at=row[8],
        )
        apply_summary = _apply_overrides_unlocked(conn, slate_id)

    return RejectMatchOverrideResult(
        override=override,
        apply=apply_summary,
    )


@dataclass(frozen=True)
class AssignMatchOverrideResult:
    """Outcome of ``record_assign_match_override`` (Phase D.5.1, §16.10).

    ``override`` is the freshly-inserted ``manual_match_overrides`` row
    (``superseded_at`` is ``None``). ``override_type`` is the derived
    type (``accept_match`` or ``force_pair``) — also available on
    ``override.override_type``, surfaced here so a UI can label the
    action without re-deriving it. ``apply`` is the
    ``ApplyOverridesSummary`` from the apply pass run in the same
    transaction; for a row that exists and a still-active bound fighter
    it reflects the ``effective_status`` / ``fighter_id`` write.
    """

    override: ManualMatchOverrideRecord
    override_type: str
    apply: ApplyOverridesSummary


def _derive_assign_override_type(
    conn: sqlite3.Connection, *, slate_id: int, odds_row_key, fighter_id
) -> str:
    """Derive ``accept_match`` vs ``force_pair`` for an assign (§16.10).

    ``accept_match`` only when the matcher left the row ``review_required``
    **and** the chosen fighter is the one the matcher already proposed (the
    user is confirming it). The matcher's proposal for a review_required
    row is captured two ways: a fuzzy 88–94 single candidate lands in the
    result row's ``fighter_id`` (§16.2's headline accept case — the
    matcher does *not* fill ``preferred_candidate`` there), while an
    opponent-disambiguated ambiguous row names it in ``preferred_candidate``
    (``fighter_id`` is NULL). Confirming *either* proposal is an
    ``accept_match``; every other case — an ``unmatched`` row, or a
    ``review_required`` row resolved to a fighter the matcher did not
    propose — is ``force_pair`` (§16.10). The accept/force split is
    audit-fidelity only: downstream treats them identically (§16.9).

    Best-effort: a malformed key / fighter falls back to ``force_pair`` and
    lets ``_add_override_unlocked`` raise the authoritative ``ValueError``.
    """
    if (
        not isinstance(odds_row_key, str)
        or not odds_row_key.strip()
        or fighter_id is None
    ):
        return "force_pair"
    key = odds_row_key.strip()
    result_row = conn.execute(
        "SELECT match_status, fighter_id, preferred_candidate "
        "FROM odds_match_results WHERE slate_id = ? AND odds_row_key = ?",
        (int(slate_id), key),
    ).fetchone()
    if result_row is None:
        return "force_pair"
    match_status, matched_fighter_id, preferred_candidate = result_row
    if match_status != "review_required":
        return "force_pair"
    chosen = int(fighter_id)
    if matched_fighter_id is not None and int(matched_fighter_id) == chosen:
        return "accept_match"
    if preferred_candidate is not None:
        name_row = conn.execute(
            "SELECT name FROM fighters WHERE id = ? AND slate_id = ?",
            (chosen, int(slate_id)),
        ).fetchone()
        if name_row is not None and name_row[0] == preferred_candidate:
            return "accept_match"
    return "force_pair"


def record_assign_match_override(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    odds_row_key: str,
    fighter_id: int,
    reason: str | None = None,
) -> AssignMatchOverrideResult:
    """Bind an odds row to a DK fighter and apply it in one transaction.

    Phase D.5.1 of ``docs/ODDS_PERSISTENCE_DESIGN.md`` §16.10. Mirrors
    ``record_reject_match_override``'s composition: derive the override
    type (§16.10), insert it via
    ``ManualMatchOverrideRepository._add_override_unlocked`` (which
    validates §16.11 and supersedes any active resolution override on the
    key, §16.4), then run ``_apply_overrides_unlocked`` — all inside a
    single ``with conn:`` block so a failure during the apply rolls the
    insert (and supersession) back.

    Validation failures (inactive / wrong-slate / already-bound fighter,
    missing odds row) raise ``ValueError`` before any DB write, so a bad
    input cannot wipe a prior active binding.

    Slate-scoped: every read and write is keyed on ``slate_id``.

    Sequencing note (§16.14): D.5.1 writes the binding and ``fighter_id``,
    but projections still read ``match_status == 'auto_match'`` until
    D.5.2 — so this service does **not** yet un-exclude a bound fighter
    from Build. Do not present it as the assignment fix on its own.

    Not called by the Odds page yet — that wiring is Phase D.5.3.
    """
    slate_id = int(slate_id)
    repo = ManualMatchOverrideRepository(conn)

    with conn:
        override_type = _derive_assign_override_type(
            conn,
            slate_id=slate_id,
            odds_row_key=odds_row_key,
            fighter_id=fighter_id,
        )
        new_id = repo._add_override_unlocked(
            slate_id=slate_id,
            override_type=override_type,
            odds_row_key=odds_row_key,
            fighter_id=fighter_id,
            reason=reason,
        )
        row = conn.execute(
            "SELECT id, slate_id, odds_row_key, fighter_id, override_type, "
            "payload_json, reason, created_at, superseded_at "
            "FROM manual_match_overrides WHERE id = ?",
            (new_id,),
        ).fetchone()
        override = ManualMatchOverrideRecord(
            id=int(row[0]),
            slate_id=int(row[1]),
            odds_row_key=row[2],
            fighter_id=(int(row[3]) if row[3] is not None else None),
            override_type=row[4],
            payload_json=row[5],
            reason=row[6],
            created_at=row[7],
            superseded_at=row[8],
        )
        apply_summary = _apply_overrides_unlocked(conn, slate_id)

    return AssignMatchOverrideResult(
        override=override,
        override_type=override_type,
        apply=apply_summary,
    )
