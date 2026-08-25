"""Pure filters/formatters for persisted odds match results.

Used by the Odds page (Phase D.3 — first UI override write action) to
gate which match results are surfaced as candidates for the Reject UI.
Keeping the filter and label-formatting pure makes them unit-testable
without Streamlit.

Operates on the ``OddsMatchResultRecord`` dataclass produced by
``src.ingestion.odds_matching_service``. No DB access, no side effects.
"""

from __future__ import annotations

from src.ingestion.odds_matching_service import OddsMatchResultRecord

_REJECTABLE_STATUSES: frozenset[str] = frozenset({"review_required"})

# Rows the D.5.3 Assign/Accept UI may bind to a fighter (§16.10). Keyed on
# ``effective_status``, NOT ``match_status``: a row an active reject already
# flipped to ``review_rejected`` is intentionally excluded — it must be
# un-rejected first (assigning supersedes the reject via §16.4, but that
# recovery is surfaced through the reject panel, not here). ``auto_match`` is
# excluded because it is already bound, and ``review_accepted`` / ``force_pair``
# rows already carry a manual binding.
_ASSIGNABLE_EFFECTIVE_STATUSES: frozenset[str] = frozenset(
    {"review_required", "unmatched"}
)


def rejectable_match_results(
    records: list[OddsMatchResultRecord],
) -> list[OddsMatchResultRecord]:
    """Subset of ``records`` whose ``match_status`` is rejectable in v0.

    Phase D.3 ships ``review_required`` only. ``auto_match`` arrives in
    Phase D.4 once the review-only flow is proven. ``unmatched`` is
    never rejectable in v0 — there is no matcher-proposed fighter
    binding to reject against.

    Input order is preserved.
    """
    return [r for r in records if r.match_status in _REJECTABLE_STATUSES]


def format_rejectable_label(
    record: OddsMatchResultRecord, *, key_prefix_length: int = 16
) -> str:
    """Selectbox label for one rejectable match result.

    Shape: ``"<key prefix>… → <preferred or ∅> (score: <match_score>)"``.
    Keys at or below ``key_prefix_length`` are not truncated; longer
    keys are truncated with a single ``…`` suffix so the selectbox
    stays readable. Missing ``preferred_candidate`` renders as ``∅``.
    """
    key = record.odds_row_key
    if len(key) > key_prefix_length:
        key_display = key[:key_prefix_length] + "…"
    else:
        key_display = key
    candidate = record.preferred_candidate or "∅"
    return f"{key_display} → {candidate} (score: {record.match_score})"


def assignable_match_results(
    records: list[OddsMatchResultRecord],
) -> list[OddsMatchResultRecord]:
    """Subset of ``records`` the D.5.3 Assign/Accept UI may bind (§16.10).

    A row is assignable when its ``effective_status`` is ``review_required``
    or ``unmatched`` — the matcher could not cleanly resolve it. ``auto_match``
    rows are already bound; ``review_rejected`` rows must be un-rejected first;
    ``review_accepted`` / ``force_pair`` rows already carry a manual binding.

    Input order is preserved.
    """
    return [
        r for r in records
        if r.effective_status in _ASSIGNABLE_EFFECTIVE_STATUSES
    ]


def format_assignable_label(
    record: OddsMatchResultRecord,
    *,
    odds_fighter_raw: str | None = None,
    opponent_raw: str | None = None,
    american_odds: int | None = None,
    key_prefix_length: int = 16,
) -> str:
    """Selectbox label for one assignable odds row.

    Carries enough context to pick the right row without leaving the page
    (§16.10 / task brief): the sportsbook fighter's raw name, the raw
    opponent, the moneyline, the current ``effective_status``, and the
    matcher's proposed candidate when there is one.

    When ``odds_fighter_raw`` is not supplied (the odds row could not be
    joined), the truncated ``odds_row_key`` stands in so the label is never
    empty. Shape:

        ``"<fighter> vs <opponent> @ <±ml> — <effective_status>
           → proposes <candidate> (score: <match_score>)"``
    """
    if odds_fighter_raw and odds_fighter_raw.strip():
        head = odds_fighter_raw.strip()
    else:
        key = record.odds_row_key
        head = (
            key[:key_prefix_length] + "…"
            if len(key) > key_prefix_length
            else key
        )
    parts = [head]
    if opponent_raw and opponent_raw.strip():
        parts.append(f"vs {opponent_raw.strip()}")
    if american_odds is not None:
        parts.append(f"@ {american_odds:+d}")
    label = " ".join(parts)
    label += f" — {record.effective_status}"
    if record.preferred_candidate:
        label += f" → proposes {record.preferred_candidate}"
    label += f" (score: {record.match_score})"
    return label
