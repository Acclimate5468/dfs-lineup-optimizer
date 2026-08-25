"""DraftKings UFC **Captain (Showdown)** salary CSV parser.

Pure logic for `docs/CAPTAIN_MODE_DESIGN.md` §5 (Captain data contract). This
module is additive and lives beside Classic per §3 — it does not import, edit,
or depend on any Classic salary path. No I/O beyond reading a DataFrame already
in hand: no DB, no Streamlit, no network, no file writes. Deterministic.

Captain salary contract (§5): a DK Captain CSV lists **each fighter twice** —

  - a ``Roster Position = CPT`` row whose ``Salary`` is **1.5×** the base, and
  - a ``Roster Position = F`` row at the base salary.

This parser collapses the two rows into **one** :class:`CaptainFighter` record
carrying both salaries, and reconstructs bouts by pairing the two fighters that
share a byte-identical ``Game Info`` string — the same pairing convention the
Classic grouping path uses (no ``@``-alias parsing, no fuzzy matching). A
fighter flagged out (``Position == "O"``) is still collapsed into a record but
is excluded from bout pairing (§5).

Scheduled rounds are deliberately **not** inferred here — they are not in the
file and are a later UI toggle (§5). Persistence is a separate, gated slice.

STATUS: tested in isolation against synthetic fixtures only. Not yet validated
against a real official DK UFC Captain salary CSV (docs/DEVELOPMENT_NOTES.md §8).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Columns this parser reads. DK Captain exports share the Classic column set
# plus the per-row ``Roster Position`` (CPT/F) discriminator. Real-file
# verification of these exact header names is still pending (docs/DEVELOPMENT_NOTES.md §8).
REQUIRED_COLUMNS: tuple[str, ...] = (
    "Name",
    "Roster Position",
    "Salary",
    "Game Info",
)

# Roster Position tokens (case-insensitive, trimmed) that mark each row's slot.
_CAPTAIN_TOKENS = frozenset({"CPT", "CAPTAIN"})
_FLEX_TOKENS = frozenset({"F", "FLEX", "UTIL"})

# Out / scratched indicator carried in the ``Position`` column (§5).
_OUT_TOKEN = "O"


class CaptainSalaryParseError(ValueError):
    """Raised when a DK Captain salary DataFrame cannot be parsed.

    Carries an explicit message describing the offending fighter/row so callers
    can surface a clear error rather than silently dropping or mis-pairing data.
    """


@dataclass(frozen=True)
class CaptainFighter:
    """One fighter, collapsed from its CPT + F rows (§5).

    ``name`` is the verbatim DK ``Name`` and is the identity used to collapse a
    fighter's two rows — in real DK Captain exports the CPT and F rows carry
    **different** DK ids, so each slot's id is kept separately
    (``captain_dk_id`` from the CPT row, ``base_dk_id`` from the F row).
    ``base_salary`` comes from the ``F`` row, ``captain_salary`` from the
    ``CPT`` row (the 1.5× row). ``is_out`` fighters are reported but excluded
    from bout pairing.
    """

    name: str
    base_salary: int
    captain_salary: int
    base_dk_id: str | None
    captain_dk_id: str | None
    game_info: str | None
    team: str | None
    is_out: bool


@dataclass(frozen=True)
class CaptainBout:
    """Two fighters that share a byte-identical ``Game Info`` string.

    Names are the canonical DK ``Name`` values, ordered deterministically so the
    parse is stable regardless of input row order.
    """

    game_info: str
    fighter_1_name: str
    fighter_2_name: str


@dataclass(frozen=True)
class ParsedCaptainSalaries:
    """Deterministic output of :func:`parse_captain_salary_rows`.

    ``fighters`` are sorted by name; ``bouts`` by ``(game_info, names)``.
    ``warnings`` carry non-fatal advisories (e.g. a captain salary that is not
    exactly 1.5× the base) — surfaced, never raised (§5).
    """

    fighters: tuple[CaptainFighter, ...]
    bouts: tuple[CaptainBout, ...]
    warnings: tuple[str, ...]


def _text_or_none(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, float) and raw != raw:  # NaN  # noqa: PLR0124
        return None
    text = str(raw).strip()
    if text == "" or text.lower() == "nan":
        return None
    return text


def _coerce_salary(raw: object, name: str, slot: str) -> int:
    """Parse a DK salary cell into a non-negative int.

    Tolerates ``$`` and ``,`` formatting, consistent with the Classic importer;
    rejects blanks, non-numerics, fractional and negative values.
    """
    text = _text_or_none(raw)
    if text is None:
        raise CaptainSalaryParseError(
            f"Fighter {name!r} ({slot} row): salary is missing."
        )
    cleaned = text.replace("$", "").replace(",", "").strip()
    try:
        as_float = float(cleaned)
    except ValueError as exc:
        raise CaptainSalaryParseError(
            f"Fighter {name!r} ({slot} row): salary {text!r} is not a number."
        ) from exc
    if not as_float.is_integer():
        raise CaptainSalaryParseError(
            f"Fighter {name!r} ({slot} row): salary {text!r} is not an integer."
        )
    salary_int = int(as_float)
    if salary_int < 0:
        raise CaptainSalaryParseError(
            f"Fighter {name!r} ({slot} row): salary {salary_int} is negative."
        )
    return salary_int


def _classify_slot(roster_position: str | None, name: str) -> str:
    """Map a ``Roster Position`` cell to ``"CPT"`` or ``"F"``; else raise."""
    token = (roster_position or "").strip().upper()
    if token in _CAPTAIN_TOKENS:
        return "CPT"
    if token in _FLEX_TOKENS:
        return "F"
    raise CaptainSalaryParseError(
        f"Fighter {name!r}: unknown Roster Position {roster_position!r}. "
        f"Expected one of CPT / F."
    )


def parse_captain_salary_rows(df: pd.DataFrame) -> ParsedCaptainSalaries:
    """Parse a DK UFC Captain salary DataFrame into fighters + bouts (§5).

    Input contract: ``df`` carries the DK Captain columns (see
    :data:`REQUIRED_COLUMNS`) with each fighter present as exactly one ``CPT``
    row and one ``F`` row. This function performs no I/O and infers no
    scheduled-rounds field.

    Raises :class:`CaptainSalaryParseError` on malformed input: a missing
    required column, an unknown ``Roster Position``, a fighter missing its CPT
    or F row, or a duplicated CPT/F row.

    Returns a fully deterministic :class:`ParsedCaptainSalaries` — fighters
    sorted by name, bouts by ``(game_info, names)`` — regardless of input order.
    """
    detected = [str(c).strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in detected]
    if missing:
        raise CaptainSalaryParseError(
            "DataFrame does not look like a DK UFC Captain salary export. "
            f"Missing required column(s): {', '.join(missing)}. "
            f"Expected: {', '.join(REQUIRED_COLUMNS)}."
        )

    has_id = "ID" in detected
    has_team = "TeamAbbrev" in detected
    has_position = "Position" in detected

    # Collapse rows into per-fighter groups keyed on the verbatim ``Name``.
    # In real DK Captain exports a fighter's CPT and F rows carry the same Name
    # / Game Info / Team but *different* DK ids, so identity must key on Name,
    # never on ID; each slot's id is captured separately.
    @dataclass
    class _Acc:
        name: str
        game_info: str | None
        team: str | None
        is_out: bool
        cpt_salary: int | None = None
        base_salary: int | None = None
        cpt_dk_id: str | None = None
        base_dk_id: str | None = None

    groups: dict[str, _Acc] = {}

    for offset, (_idx, row) in enumerate(df.iterrows(), start=1):
        name = _text_or_none(row.get("Name"))
        if name is None:
            raise CaptainSalaryParseError(
                f"Row {offset}: fighter name is missing or blank."
            )

        slot = _classify_slot(_text_or_none(row.get("Roster Position")), name)
        salary = _coerce_salary(row.get("Salary"), name, slot)

        dk_id = _text_or_none(row.get("ID")) if has_id else None
        game_info = _text_or_none(row.get("Game Info"))
        team = _text_or_none(row.get("TeamAbbrev")) if has_team else None
        is_out = False
        if has_position:
            position = _text_or_none(row.get("Position"))
            is_out = position is not None and position.upper() == _OUT_TOKEN

        acc = groups.get(name)
        if acc is None:
            acc = _Acc(
                name=name,
                game_info=game_info,
                team=team,
                is_out=is_out,
            )
            groups[name] = acc
        else:
            # An out flag on either row marks the fighter out.
            acc.is_out = acc.is_out or is_out

        if slot == "CPT":
            if acc.cpt_salary is not None:
                raise CaptainSalaryParseError(
                    f"Fighter {name!r}: duplicate CPT row."
                )
            acc.cpt_salary = salary
            acc.cpt_dk_id = dk_id
        else:
            if acc.base_salary is not None:
                raise CaptainSalaryParseError(
                    f"Fighter {name!r}: duplicate F row."
                )
            acc.base_salary = salary
            acc.base_dk_id = dk_id

    warnings: list[str] = []
    fighters: list[CaptainFighter] = []

    for acc in groups.values():
        if acc.cpt_salary is None:
            raise CaptainSalaryParseError(
                f"Fighter {acc.name!r}: missing CPT row "
                f"(found only the F / base row)."
            )
        if acc.base_salary is None:
            raise CaptainSalaryParseError(
                f"Fighter {acc.name!r}: missing F row "
                f"(found only the CPT row)."
            )

        expected_cpt = round(1.5 * acc.base_salary)
        if acc.cpt_salary != expected_cpt:
            warnings.append(
                f"Fighter {acc.name!r}: captain_salary {acc.cpt_salary} is not "
                f"1.5x base_salary {acc.base_salary} (expected {expected_cpt})."
            )

        fighters.append(
            CaptainFighter(
                name=acc.name,
                base_salary=acc.base_salary,
                captain_salary=acc.cpt_salary,
                base_dk_id=acc.base_dk_id,
                captain_dk_id=acc.cpt_dk_id,
                game_info=acc.game_info,
                team=acc.team,
                is_out=acc.is_out,
            )
        )

    fighters.sort(key=lambda f: f.name)
    bouts = _pair_bouts(fighters)

    return ParsedCaptainSalaries(
        fighters=tuple(fighters),
        bouts=bouts,
        warnings=tuple(warnings),
    )


def _pair_bouts(fighters: list[CaptainFighter]) -> tuple[CaptainBout, ...]:
    """Pair active (non-out) fighters that share a byte-identical Game Info.

    Mirrors the Classic grouping convention (§5, `fight_grouping`): only an
    exact-2 group forms a bout. Out fighters, blank Game Info, lone fighters,
    and >2-fighter collisions form no bout — they are simply not paired here,
    not an error (rounds/exclusions are a later concern).
    """
    by_game_info: dict[str, list[str]] = {}
    for fighter in fighters:
        if fighter.is_out or fighter.game_info is None:
            continue
        by_game_info.setdefault(fighter.game_info, []).append(fighter.name)

    bouts: list[CaptainBout] = []
    for game_info, names in by_game_info.items():
        if len(names) != 2:
            continue
        first, second = sorted(names)
        bouts.append(
            CaptainBout(
                game_info=game_info,
                fighter_1_name=first,
                fighter_2_name=second,
            )
        )

    bouts.sort(key=lambda b: (b.game_info, b.fighter_1_name, b.fighter_2_name))
    return tuple(bouts)
