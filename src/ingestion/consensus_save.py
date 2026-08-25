"""Persist a multi-book consensus blend into a slate (Slice 5).

Realizes the persistence half of ``docs/ODDS_CONSENSUS_DESIGN.md`` §5.4 / §6 /
§7 / §9: from the two parsers' per-fighter book lines, write the per-(fighter,
book) provenance into ``odds_book_lines`` and one synthesized
``source="consensus"`` row per fighter into the *existing* ``odds_rows``, then
chain the existing match recompute so the consensus flows through the unchanged
matcher → ``effective_status`` → projection path.

Boundaries (design §3 / §7 / §10; ``docs/DEVELOPMENT_NOTES.md`` §10):

  - **Projection-neutral.** The consensus row is an ordinary ``odds_rows`` insert
    (``source = bookmaker = "consensus"``); ``american_odds`` is the fair line
    and ``implied_probability`` carries the exact consensus ``prob`` (§6). No
    ``odds_rows`` / ``odds_match_results`` schema change; no ``effective_status``
    promotion into optimizer / alerts / exports.
  - **Last save wins.** ``odds_book_lines`` is cleared/rewritten per slate, and
    the ``source="consensus"`` ``odds_rows`` are atomically delete-then-inserted
    (``OddsRowRepository.replace_for_slate_source``), so a re-blend replaces the
    prior consensus without duplication regardless of whether the value changed.
  - **Low confidence stays out of the projection.** A fight with fewer than
    ``min_books`` priced books keeps its provenance but writes **no** consensus
    ``odds_rows`` row; it is returned for the UI (Slice 6) to surface (§9).
  - **No UI / no fetch.** This module is pure-to-DB; the BFO-url / paste preview
    and the Save button are Slice 6.

Transaction note: ``odds_book_lines`` and the ``odds_rows`` consensus set each
land atomically (their own ``with conn:`` replace); the recompute owns its own
transaction afterward, as every other save path does. A retry after a partial
failure is safe (the replaces are idempotent).
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

from src.db.repositories import (
    OddsBookLineRecord,
    OddsBookLineRepository,
    OddsRowRecord,
    OddsRowRepository,
)
from src.ingestion.consensus_assembly import (
    assemble_fights,
    merge_sources,
)
from src.ingestion.odds_matching_service import (
    EmptyDkRosterError,
    RecomputeSummary,
    recompute_and_replace_match_results,
)
from src.projections.odds_consensus import (
    MIN_BOOKS_DEFAULT,
    ConsensusResult,
    compute_slate_consensus,
)

# §5.4 / §6: the synthesized row's source + bookmaker. Distinct from the
# per-book provenance origin tokens ('bestfightodds' | 'paste').
SOURCE_CONSENSUS = "consensus"
BOOKMAKER_CONSENSUS = "consensus"

_BATCH_ID_HASH_LEN = 12


@dataclass(frozen=True)
class ConsensusSaveResult:
    """Outcome of one :func:`save_consensus_to_slate` call.

    ``book_line_rows`` are the persisted provenance rows; ``consensus_rows`` are
    the synthesized ``source="consensus"`` ``odds_rows`` (one per fighter of each
    blended fight that met ``min_books``). ``low_confidence`` are the blended
    fights below ``min_books`` whose consensus row was deliberately *not* written
    (§9). ``unpaired_fighters`` had no resolvable opponent. ``recompute`` is the
    chained ``RecomputeSummary`` or ``None`` with ``recompute_error`` set when the
    slate had no active DK fighters yet (the rows are still saved). ``source_url``
    is echoed provenance, never persisted (``odds_rows`` has no URL column).
    """

    import_batch_id: str
    book_line_rows: list[OddsBookLineRecord] = field(default_factory=list)
    consensus_rows: list[OddsRowRecord] = field(default_factory=list)
    low_confidence: list[ConsensusResult] = field(default_factory=list)
    unpaired_fighters: list[str] = field(default_factory=list)
    fights_considered: int = 0
    recompute: RecomputeSummary | None = None
    recompute_error: str | None = None
    source_url: str | None = None

    @property
    def book_line_count(self) -> int:
        return len(self.book_line_rows)

    @property
    def consensus_count(self) -> int:
        return len(self.consensus_rows)

    @property
    def low_confidence_count(self) -> int:
        return len(self.low_confidence)


def _compute_batch_id(captured_at: str) -> str:
    """Deterministic batch id grouping one consensus save (mirrors the paste path).

    ``captured_at`` is fixed when the preview is produced, so the identical
    previewed blend yields the identical id.
    """
    digest = hashlib.sha1(
        f"{SOURCE_CONSENSUS}|{captured_at or ''}".encode("utf-8")
    ).hexdigest()
    return f"consensus-{digest[:_BATCH_ID_HASH_LEN]}"


def save_consensus_to_slate(
    conn: sqlite3.Connection,
    *,
    slate_id: int,
    captured_at: str,
    bestfightodds_rows=None,
    paste_rows=None,
    min_books: int = MIN_BOOKS_DEFAULT,
    source_url: str | None = None,
) -> ConsensusSaveResult:
    """Blend the supplied per-book lines and persist the consensus into a slate.

    ``bestfightodds_rows`` / ``paste_rows`` are the ``AllBooksFighterRow`` /
    ``MultiBookPasteRow`` lists from the two parsers (either or both). The merged
    per-book lines are written to ``odds_book_lines`` (provenance), paired into
    fights, blended (§2), and the per-fighter fair lines are written as
    ``source="consensus"`` ``odds_rows`` before the match recompute is chained.
    """
    slate_id = int(slate_id)
    import_batch_id = _compute_batch_id(captured_at)

    merged = merge_sources(
        bestfightodds_rows=bestfightodds_rows or (),
        paste_rows=paste_rows or (),
    )

    # --- provenance: clear-and-rewrite odds_book_lines for the slate (§5.4) ---
    provenance_payload = [
        {
            "fighter_name_raw": fighter.fighter_name,
            "opponent_name_raw": fighter.opponent,
            "book": entry.book,
            "american_odds": entry.american_odds,
            "source": entry.source,
            "captured_at": captured_at,
            "import_batch_id": import_batch_id,
        }
        for fighter in merged
        for entry in fighter.lines
    ]
    book_line_rows = OddsBookLineRepository(conn).replace_for_slate(
        slate_id, provenance_payload
    )

    # --- blend the paired fights (§2) ---
    assembly = assemble_fights(merged)
    results = compute_slate_consensus(assembly.fights, min_books=min_books)

    # --- consensus odds_rows: skip low-confidence, atomic replace the rest (§7/§9) ---
    consensus_payload: list[dict] = []
    low_confidence: list[ConsensusResult] = []
    for res in results:
        if res.low_confidence or res.prob_a is None or res.prob_b is None:
            low_confidence.append(res)
            continue
        consensus_payload.append(
            {
                "fighter_name_raw": res.fighter_a,
                "american_odds": res.fair_american_a,
                "captured_at": captured_at,
                "bookmaker": BOOKMAKER_CONSENSUS,
                "opponent_name_raw": res.fighter_b,
                "import_batch_id": import_batch_id,
                "implied_probability": res.prob_a,
            }
        )
        consensus_payload.append(
            {
                "fighter_name_raw": res.fighter_b,
                "american_odds": res.fair_american_b,
                "captured_at": captured_at,
                "bookmaker": BOOKMAKER_CONSENSUS,
                "opponent_name_raw": res.fighter_a,
                "import_batch_id": import_batch_id,
                "implied_probability": res.prob_b,
            }
        )
    consensus_rows = OddsRowRepository(conn).replace_for_slate_source(
        slate_id, SOURCE_CONSENSUS, consensus_payload
    )

    # --- recompute match results (own transaction; reapplies overrides) ---
    recompute: RecomputeSummary | None = None
    recompute_error: str | None = None
    try:
        recompute = recompute_and_replace_match_results(conn, slate_id)
    except EmptyDkRosterError as exc:
        recompute_error = str(exc)

    return ConsensusSaveResult(
        import_batch_id=import_batch_id,
        book_line_rows=book_line_rows,
        consensus_rows=consensus_rows,
        low_confidence=low_confidence,
        unpaired_fighters=assembly.unpaired,
        fights_considered=len(results),
        recompute=recompute,
        recompute_error=recompute_error,
        source_url=(source_url or None),
    )
