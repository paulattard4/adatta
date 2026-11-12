"""League-season rules: file validity, as-of deductions, and the check against final tables."""

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from adatta.features.league_rules import (
    Deduction,
    Rules,
    SeasonRule,
    TableIssue,
    check_against_standings,
    check_results_against_standings,
    load_rules,
)


def test_rules_file_covers_every_bundle_league_season() -> None:
    from adatta.config import load_competitions

    rules = load_rules()
    for comp in load_competitions("config/competitions.yml"):
        if comp.tier == "cup":
            continue
        for year in range(2016, 2027):  # dev training windows reach back to 2016/17
            assert rules.season(comp.league_id, year) is not None, (comp.league_id, year)
    pl = rules.league(39)
    assert pl is not None and pl.has_var(2019) and not pl.has_var(2018)
    ligue1_2022 = rules.season(61, 2022)
    assert ligue1_2022 is not None and ligue1_2022.zones()["relegation"] == (17, 20)


def test_deductions_as_of_follow_dates_and_hold_back_unknown_ones() -> None:
    rules = load_rules()
    everton = "45"
    assert everton not in rules.deductions_as_of(39, 2023, date(2023, 11, 16))
    assert rules.deductions_as_of(39, 2023, date(2023, 11, 17))[everton] == 10
    assert rules.deductions_as_of(39, 2023, date(2024, 3, 1))[everton] == 6
    assert rules.deductions_as_of(39, 2023, date(2024, 5, 20))[everton] == 8
    juve = "496"
    assert rules.deductions_as_of(135, 2022, date(2023, 5, 1))[juve] == 0  # set aside
    wigan = "61"  # 2019/20, date unknown: only once the season is over
    assert wigan not in rules.deductions_as_of(40, 2019, date(2020, 7, 22))
    assert rules.deductions_as_of(40, 2019, date(2020, 7, 22), season_over=True)[wigan] == 12


def test_validation_rejects_bad_rows() -> None:
    with pytest.raises(ValidationError):
        SeasonRule(years=[2019], teams=20, relegation=(18, 21))
    with pytest.raises(ValidationError):
        Deduction(league_id=1, season=2019, team_ref="1", team="X", points=1,
                  effective=None, date_confidence="high")  # fmt: skip
    rule = {"years": [2019], "teams": 2}
    with pytest.raises(ValidationError):
        Rules(leagues=[{"league_id": 1, "var_from": None, "seasons": [rule, rule]}],
              deductions=[])  # fmt: skip


def row(team: int, win: int, draw: int, points: int) -> dict[str, Any]:
    return {"team": {"id": team, "name": f"T{team}"}, "points": points,
            "all": {"win": win, "draw": draw}}  # fmt: skip


def test_check_against_standings_finds_unexplained_gaps() -> None:
    rules = Rules(
        leagues=[{"league_id": 1, "var_from": None, "seasons": [{"years": [2020], "teams": 2}]}],
        deductions=[Deduction(league_id=1, season=2020, team_ref="7", team="T7", points=3,
                              effective=date(2020, 9, 1), date_confidence="high")],
    )  # fmt: skip
    ok = {(1, 2020): [row(7, 10, 2, 29), row(8, 5, 5, 20)]}
    assert check_against_standings(rules, ok) == []
    bad = {(1, 2020): [row(7, 10, 2, 32), row(8, 5, 5, 19)], (1, 2021): [row(7, 1, 1, 4)]}
    problems = check_against_standings(rules, bad)
    assert any("T7: table gap 0, deductions 3" in p for p in problems)
    assert any("T8: table gap 1" in p for p in problems)
    assert any("2021: no zone rule" in p for p in problems)


def test_known_table_faults_are_accounted_for() -> None:
    rules = Rules(
        leagues=[{"league_id": 1, "var_from": None, "seasons": [{"years": [2017], "teams": 2}]}],
        deductions=[],
        table_issues=[TableIssue(league_id=1, season=2017, kind="team_ref", table_ref="99",
                                 team_ref="8")],
    )  # fmt: skip
    table = {(1, 2017): [row(7, 1, 0, 3), row(99, 0, 0, 0)]}
    results = {(1, 2017): [("7", "8", 2, 0)]}
    assert check_results_against_standings(rules, table, results) == []  # 99 read as 8
    half = {(1, 2017): [row(7, 1, 0, 3), row(8, 0, 0, 0)]}
    full = {(1, 2017): [("7", "8", 2, 0), ("8", "7", 1, 0)]}
    assert check_results_against_standings(rules, half, full)  # a snapshot disagrees
    partial = rules.model_copy(
        update={"table_issues": [TableIssue(league_id=1, season=2017, kind="partial")]}
    )
    assert check_results_against_standings(partial, half, full) == []
    three = {(1, 2017): [row(7, 1, 0, 3), row(8, 0, 0, 0), row(9, 0, 0, 0)]}
    assert any("2 teams, table 3" in p for p in check_against_standings(partial, three))
