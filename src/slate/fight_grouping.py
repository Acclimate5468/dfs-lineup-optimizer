"""Group fighters into suggested fight pairs from the DK 'Game Info' string.

B3 of ``docs/DK_GAME_INFO_PAIRING_DESIGN.md`` (§3). A pure, Streamlit-free,
DB-free helper: it takes the slate roster as a plain iterable (each entry
exposing ``id``, ``name``, ``status``, and ``game_info``) and reads nothing,
writes nothing, and performs no file I/O.

Both rows of a bout carry the byte-identical DK ``Game Info`` string (§1.1),
so grouping the active roster by the *exact* stored value reconstructs the two
canonical DK ``Name`` values directly — no ``@``-alias parsing and no fuzzy
matching (§1.1, §3). The algorithm classifies each Game Info group by size:

  - exactly 2 active fighters -> a suggested pair (canonical names),
  - exactly 1                 -> incomplete (opponent missing / inactive),
  - more than 2               -> anomaly (a Game Info collision),

and reports active fighters with a blank/``NULL`` Game Info as uncovered. The
category of each non-pair bucket *is* the skip reason; a short human-readable
``reason`` is exposed for the preview surface. Group creation is the Fight
Groups page's explicit Apply (§4) — never this helper's job.

``detect_main_event_pair`` (below) is the one round-related inference: it reads
the Game Info start times of the suggested pairs and returns the latest-starting
bout as the main event, so the apply can auto-set it to 5 rounds. It still
writes nothing and only ever *suggests*; it returns ``None`` (no guess) when the
times are missing or ambiguous.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from src.utils.text_cleaning import normalize_name

# Mirrors the active-filter consumers (projection_input_service,
# odds_matching_service): only 'active' fighters are eligible for pairing,
# consistent with the Fight Groups Region A coverage join (§3 step 1).
ACTIVE_FIGHTER_STATUS = "active"


class RosterFighter(Protocol):
    """Structural type for the roster entries this helper consumes.

    ``FighterRecord`` (``src/db/repositories.py``) satisfies it. The helper
    depends only on these four attributes — never on the repository type — so
    it stays DB-free and trivially testable with a lightweight stand-in.
    """

    id: int
    name: str
    status: str
    game_info: str | None


@dataclass(frozen=True)
class SuggestedPair:
    """Two active fighters that share an exact Game Info string.

    ``fighter_1_name`` / ``fighter_2_name`` are the canonical DK ``Name``
    values (not the ``@``-aliases), ordered deterministically by normalized
    name so the preview is stable and an Apply is idempotent (§3 step 4).
    """

    game_info: str
    fighter_1_name: str
    fighter_2_name: str


@dataclass(frozen=True)
class IncompleteGroup:
    """A Game Info value carried by exactly one active fighter.

    The opponent is missing from the active roster — unimported, or marked
    inactive on a re-import (e.g. a scratched fighter). Never grouped (§3,
    §6.2).
    """

    game_info: str
    fighter_name: str

    @property
    def reason(self) -> str:
        return "only one active fighter for this Game Info"


@dataclass(frozen=True)
class AnomalyGroup:
    """A Game Info value shared by more than two active fighters.

    A collision that should not occur for a clean DK export. Never grouped and
    never sub-paired (§3, §6.3); the whole group is surfaced for inspection.
    """

    game_info: str
    fighter_names: tuple[str, ...]

    @property
    def reason(self) -> str:
        return (
            f"{len(self.fighter_names)} active fighters share this Game Info; "
            "expected exactly 2"
        )


@dataclass(frozen=True)
class UncoveredFighter:
    """An active fighter whose Game Info is ``NULL`` or blank.

    Reported so the user knows to pair them via the pasted-card builder or the
    manual selectboxes (§6.1). Never grouped.
    """

    name: str

    @property
    def reason(self) -> str:
        return "no Game Info captured"


@dataclass(frozen=True)
class GameInfoGroupingResult:
    """Structured, count-bearing output of :func:`group_fighters_by_game_info`.

    The four buckets are mutually exclusive over the active roster: an active
    fighter is in exactly one of a suggested pair, an incomplete group, an
    anomaly group, or the uncovered list. Inactive fighters appear in none.
    """

    suggested_pairs: tuple[SuggestedPair, ...]
    incomplete: tuple[IncompleteGroup, ...]
    anomalies: tuple[AnomalyGroup, ...]
    uncovered: tuple[UncoveredFighter, ...]

    @property
    def suggested_count(self) -> int:
        return len(self.suggested_pairs)

    @property
    def incomplete_count(self) -> int:
        return len(self.incomplete)

    @property
    def anomaly_count(self) -> int:
        return len(self.anomalies)

    @property
    def uncovered_count(self) -> int:
        return len(self.uncovered)


def _is_blank(game_info: str | None) -> bool:
    return game_info is None or not game_info.strip()


def group_fighters_by_game_info(
    fighters: Iterable[RosterFighter],
) -> GameInfoGroupingResult:
    """Group active fighters into suggested pairs by exact Game Info string.

    See the module docstring / design §3. Only ``status == 'active'`` fighters
    are considered; inactive fighters are ignored, so a withdrawn fighter
    correctly leaves its former opponent as *incomplete* (§6.4). Grouping keys
    on the exact stored ``game_info`` value with no normalization beyond the
    persist-time strip already applied at import (§3 step 3): two genuinely
    distinct bouts can never be merged, and a future export whose two sides do
    not share a byte-identical string degrades to *incomplete* rather than
    mis-pairing (§6.9).

    The result is fully deterministic regardless of input order: members of a
    group are ordered by ``(normalized name, id)`` and every output bucket is
    sorted by normalized name.
    """
    active = [f for f in fighters if f.status == ACTIVE_FIGHTER_STATUS]

    uncovered_fighters: list[RosterFighter] = []
    groups: dict[str, list[RosterFighter]] = {}
    for f in active:
        if _is_blank(f.game_info):
            uncovered_fighters.append(f)
            continue
        # Exact-string key; the stored value is already persist-time stripped.
        groups.setdefault(f.game_info, []).append(f)

    suggested: list[SuggestedPair] = []
    incomplete: list[IncompleteGroup] = []
    anomalies: list[AnomalyGroup] = []

    for game_info, members in groups.items():
        ordered = sorted(members, key=lambda m: (normalize_name(m.name), m.id))
        if len(ordered) == 2:
            suggested.append(
                SuggestedPair(
                    game_info=game_info,
                    fighter_1_name=ordered[0].name,
                    fighter_2_name=ordered[1].name,
                )
            )
        elif len(ordered) == 1:
            incomplete.append(
                IncompleteGroup(
                    game_info=game_info, fighter_name=ordered[0].name
                )
            )
        else:
            anomalies.append(
                AnomalyGroup(
                    game_info=game_info,
                    fighter_names=tuple(m.name for m in ordered),
                )
            )

    suggested.sort(
        key=lambda p: (
            normalize_name(p.fighter_1_name),
            normalize_name(p.fighter_2_name),
        )
    )
    incomplete.sort(key=lambda g: (normalize_name(g.fighter_name), g.game_info))
    anomalies.sort(key=lambda g: g.game_info)
    uncovered = sorted(
        (UncoveredFighter(name=f.name) for f in uncovered_fighters),
        key=lambda u: normalize_name(u.name),
    )

    return GameInfoGroupingResult(
        suggested_pairs=tuple(suggested),
        incomplete=tuple(incomplete),
        anomalies=tuple(anomalies),
        uncovered=tuple(uncovered),
    )


# ---------------------------------------------------------------------------
# Main-event detection from Game Info start times (5-round auto-set).
#
# Game Info carries the bout's scheduled start as "<date> <time> ET", e.g.
# "Costa@Schnell 06/06/2026 07:00PM ET". UFC cards run the main event last, so
# the latest start time on the card identifies the headliner — the bout that is
# (almost always) five rounds. This lets the Fight Groups apply auto-set that
# group to 5 rounds instead of making the user guess; a title fight below the
# main event is the rare exception the user overrides by hand. Pure: parses only
# the supplied strings, reads no DB, writes nothing.
# ---------------------------------------------------------------------------

_GAME_INFO_DATETIME_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2}\s*(?:AM|PM))",
    re.IGNORECASE,
)


def parse_game_info_start(game_info: str | None) -> datetime | None:
    """Parse the bout start datetime from a Game Info string, or ``None``.

    Tolerant by design: returns ``None`` when no ``MM/DD/YYYY HH:MM(AM|PM)``
    stamp is present, so the caller never guesses a time."""
    if not game_info:
        return None
    match = _GAME_INFO_DATETIME_RE.search(game_info)
    if match is None:
        return None
    stamp = f"{match.group(1)} {match.group(2).upper().replace(' ', '')}"
    try:
        return datetime.strptime(stamp, "%m/%d/%Y %I:%M%p")
    except ValueError:
        return None


def detect_main_event_pair(
    pairs: Iterable[SuggestedPair],
) -> SuggestedPair | None:
    """Return the suggested pair that is the card's main event, or ``None``.

    The main event is the bout with the *latest* parseable Game Info start time
    (UFC cards run the headliner last). Returns ``None`` when the signal is
    absent or ambiguous — fewer than two pairs carry a parseable start time, or
    two share the latest time — so a caller never auto-assigns 5 rounds on a
    guess. Pure."""
    timed: list[tuple[datetime, SuggestedPair]] = []
    for pair in pairs:
        started = parse_game_info_start(pair.game_info)
        if started is not None:
            timed.append((started, pair))
    if len(timed) < 2:
        return None
    latest = max(started for started, _ in timed)
    leaders = [pair for started, pair in timed if started == latest]
    if len(leaders) != 1:
        return None
    return leaders[0]
