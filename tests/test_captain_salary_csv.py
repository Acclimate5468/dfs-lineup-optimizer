"""Unit tests for the DK UFC Captain salary CSV parser (CAPTAIN_MODE_DESIGN §5).

Synthetic fixtures only — no real DK salary CSV is committed (docs/DEVELOPMENT_NOTES.md §7).
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.captain.salary_csv import (
    CaptainSalaryParseError,
    parse_captain_salary_rows,
)

# DK Captain column set. Each fighter contributes a CPT row (1.5x salary) and
# an F row (base salary); both rows share Name / ID / Game Info / TeamAbbrev.
_COLUMNS = [
    "Position",
    "Name + ID",
    "Name",
    "ID",
    "Roster Position",
    "Salary",
    "Game Info",
    "TeamAbbrev",
    "AvgPointsPerGame",
]


def _row(
    name: str,
    dk_id: str,
    roster_position: str,
    salary: int,
    game_info: str,
    team: str = "UFC",
    position: str = "CPT/F",
) -> dict[str, object]:
    return {
        "Position": position,
        "Name + ID": f"{name} ({dk_id})",
        "Name": name,
        "ID": dk_id,
        "Roster Position": roster_position,
        "Salary": salary,
        "Game Info": game_info,
        "TeamAbbrev": team,
        "AvgPointsPerGame": 0.0,
    }


def _fighter_rows(
    name: str,
    dk_id: str,
    base_salary: int,
    game_info: str,
    *,
    captain_salary: int | None = None,
    position: str = "CPT/F",
) -> list[dict[str, object]]:
    # Real DK Captain exports give a fighter's CPT and F rows DIFFERENT ids
    # (and different "Name + ID" strings) while sharing Name + Game Info. The
    # fixtures mirror that: collapse must key on Name, not ID.
    cpt_salary = captain_salary if captain_salary is not None else round(1.5 * base_salary)
    cpt_id = f"{dk_id}1"
    base_id = f"{dk_id}0"
    return [
        _row(name, cpt_id, "CPT", cpt_salary, game_info, position=position),
        _row(name, base_id, "F", base_salary, game_info, position=position),
    ]


def _df(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_COLUMNS)


def _clean_bout_df() -> pd.DataFrame:
    game = "FTR@CHL 06/14/2026 10:00PM ET"
    rows = (
        _fighter_rows("Challenger", "111", 8000, game)
        + _fighter_rows("Favorite", "222", 9000, game)
    )
    return _df(rows)


def test_cpt_and_f_collapse_to_one_fighter_with_correct_salaries():
    result = parse_captain_salary_rows(_clean_bout_df())

    assert len(result.fighters) == 2
    by_name = {f.name: f for f in result.fighters}

    chl = by_name["Challenger"]
    assert chl.base_salary == 8000
    assert chl.captain_salary == 12000  # 1.5 * 8000
    assert chl.captain_salary == round(1.5 * chl.base_salary)
    # CPT and F rows carry different ids; both are kept, neither keys identity.
    assert chl.captain_dk_id == "1111"
    assert chl.base_dk_id == "1110"
    assert chl.captain_dk_id != chl.base_dk_id
    assert chl.team == "UFC"
    assert chl.is_out is False

    fav = by_name["Favorite"]
    assert fav.base_salary == 9000
    assert fav.captain_salary == 13500  # 1.5 * 9000
    assert not result.warnings


def test_collapse_succeeds_when_cpt_and_f_ids_differ():
    # The real DK case: a fighter's two rows share Name + Game Info but have
    # distinct ids. Identity must collapse on Name, so this yields ONE fighter,
    # not two single-slot groups raising "missing CPT/F row".
    game = "FTR@CHL 06/14/2026 10:00PM ET"
    rows = [
        _row("Ruffy", "43247050", "CPT", 12000, game),
        _row("Ruffy", "43247036", "F", 8000, game),
    ]
    result = parse_captain_salary_rows(_df(rows))

    assert len(result.fighters) == 1
    fighter = result.fighters[0]
    assert fighter.name == "Ruffy"
    assert fighter.base_salary == 8000
    assert fighter.captain_salary == 12000
    assert fighter.captain_dk_id == "43247050"
    assert fighter.base_dk_id == "43247036"


def test_opponent_pairing_via_game_info():
    result = parse_captain_salary_rows(_clean_bout_df())

    assert len(result.bouts) == 1
    bout = result.bouts[0]
    assert bout.game_info == "FTR@CHL 06/14/2026 10:00PM ET"
    # Deterministically ordered by name regardless of input row order.
    assert (bout.fighter_1_name, bout.fighter_2_name) == ("Challenger", "Favorite")


def test_two_bouts_pair_independently():
    rows = (
        _fighter_rows("A One", "1", 7000, "GAME-A")
        + _fighter_rows("B Two", "2", 7400, "GAME-A")
        + _fighter_rows("C Three", "3", 8200, "GAME-B")
        + _fighter_rows("D Four", "4", 8800, "GAME-B")
    )
    result = parse_captain_salary_rows(_df(rows))

    assert len(result.fighters) == 4
    assert {b.game_info for b in result.bouts} == {"GAME-A", "GAME-B"}
    pairs = {(b.fighter_1_name, b.fighter_2_name) for b in result.bouts}
    assert pairs == {("A One", "B Two"), ("C Three", "D Four")}


def test_out_flag_fighter_is_recorded_but_not_paired():
    game = "OUT@OPP 06/14/2026 08:00PM ET"
    rows = (
        _fighter_rows("Sidelined", "9", 7600, game, position="O")
        + _fighter_rows("Opponent", "10", 8400, game)
    )
    result = parse_captain_salary_rows(_df(rows))

    by_name = {f.name: f for f in result.fighters}
    assert by_name["Sidelined"].is_out is True
    assert by_name["Opponent"].is_out is False
    # The out fighter is excluded from pairing, leaving an incomplete group.
    assert result.bouts == ()


def test_captain_salary_not_1_5x_surfaces_warning_not_error():
    game = "FTR@CHL 06/14/2026 10:00PM ET"
    rows = (
        _fighter_rows("Odd Salary", "1", 8000, game, captain_salary=11000)
        + _fighter_rows("Even Salary", "2", 9000, game)
    )
    result = parse_captain_salary_rows(_df(rows))

    assert len(result.warnings) == 1
    assert "Odd Salary" in result.warnings[0]
    # Still parsed, not dropped.
    assert {f.name for f in result.fighters} == {"Odd Salary", "Even Salary"}


def test_missing_cpt_row_raises():
    game = "FTR@CHL 06/14/2026 10:00PM ET"
    rows = [
        _row("Only F", "1", "F", 8000, game),
        *_fighter_rows("Complete", "2", 9000, game),
    ]
    with pytest.raises(CaptainSalaryParseError, match="missing CPT row"):
        parse_captain_salary_rows(_df(rows))


def test_missing_f_row_raises():
    game = "FTR@CHL 06/14/2026 10:00PM ET"
    rows = [
        _row("Only CPT", "1", "CPT", 12000, game),
        *_fighter_rows("Complete", "2", 9000, game),
    ]
    with pytest.raises(CaptainSalaryParseError, match="missing F row"):
        parse_captain_salary_rows(_df(rows))


def test_unknown_roster_position_raises():
    game = "FTR@CHL 06/14/2026 10:00PM ET"
    rows = [
        _row("Weird Slot", "1", "BENCH", 12000, game),
        _row("Weird Slot", "1", "F", 8000, game),
    ]
    with pytest.raises(CaptainSalaryParseError, match="unknown Roster Position"):
        parse_captain_salary_rows(_df(rows))


def test_missing_required_column_raises():
    df = _clean_bout_df().drop(columns=["Roster Position"])
    with pytest.raises(CaptainSalaryParseError, match="Missing required column"):
        parse_captain_salary_rows(df)


def test_duplicate_cpt_row_raises():
    game = "FTR@CHL 06/14/2026 10:00PM ET"
    rows = [
        _row("Dup", "1", "CPT", 12000, game),
        _row("Dup", "1", "CPT", 12000, game),
        _row("Dup", "1", "F", 8000, game),
    ]
    with pytest.raises(CaptainSalaryParseError, match="duplicate CPT row"):
        parse_captain_salary_rows(_df(rows))
