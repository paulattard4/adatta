"""League-season rules for importance and VAR (spec §6.2; register 2.20, 2.21).

config/league_rules.yml holds hand-entered pre-season zone rules, dated point deductions and
VAR start seasons. `check_against_standings` compares them with the stored final /standings
tables: every team's points gap (3*W + D - points) must equal its net deductions, and every
season must have a rule with the table's number of teams.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, model_validator

ZONES = (
    "champion", "ucl", "europe", "promotion", "promotion_playoff", "relegation_playoff",
    "relegation",
)  # fmt: skip


class SeasonRule(BaseModel):
    years: list[int]
    teams: int
    champion: tuple[int, int] | None = None
    ucl: tuple[int, int] | None = None
    europe: tuple[int, int] | None = None
    promotion: tuple[int, int] | None = None
    promotion_playoff: tuple[int, int] | None = None
    relegation_playoff: tuple[int, int] | None = None
    relegation: tuple[int, int] | None = None

    @model_validator(mode="after")
    def _ranges(self) -> SeasonRule:
        for name in ZONES:
            r = getattr(self, name)
            if r is not None and not 1 <= r[0] <= r[1] <= self.teams:
                raise ValueError(f"{name} {r} outside 1..{self.teams}")
        return self

    def zones(self) -> dict[str, tuple[int, int]]:
        return {n: getattr(self, n) for n in ZONES if getattr(self, n) is not None}


class LeagueRules(BaseModel):
    league_id: int
    var_from: int | None
    seasons: list[SeasonRule]

    def has_var(self, year: int) -> bool:
        return self.var_from is not None and year >= self.var_from


class Deduction(BaseModel):
    league_id: int
    season: int
    team_ref: str
    team: str
    points: int  # positive = taken, negative = restored
    effective: date | None
    date_confidence: str
    note: str = ""

    @model_validator(mode="after")
    def _confidence(self) -> Deduction:
        if self.date_confidence not in ("high", "approximate", "unknown"):
            raise ValueError(f"date_confidence {self.date_confidence!r}")
        if (self.effective is None) != (self.date_confidence == "unknown"):
            raise ValueError("effective is null exactly when date_confidence is unknown")
        return self


class Rules(BaseModel):
    leagues: list[LeagueRules]
    deductions: list[Deduction]

    @model_validator(mode="after")
    def _unique(self) -> Rules:
        seen: set[tuple[int, int]] = set()
        for lg in self.leagues:
            for s in lg.seasons:
                for y in s.years:
                    if (lg.league_id, y) in seen:
                        raise ValueError(f"two rules for league {lg.league_id} season {y}")
                    seen.add((lg.league_id, y))
        return self

    def season(self, league_id: int, year: int) -> SeasonRule | None:
        for lg in self.leagues:
            if lg.league_id == league_id:
                return next((s for s in lg.seasons if year in s.years), None)
        return None

    def league(self, league_id: int) -> LeagueRules | None:
        return next((lg for lg in self.leagues if lg.league_id == league_id), None)

    def deductions_as_of(
        self, league_id: int, year: int, as_of: date, season_over: bool = False
    ) -> dict[str, int]:
        """{team_ref: points deducted so far} for a table as of `as_of`. A deduction with an
        unknown date counts only once the season is over (season_over=True), so a backtest
        never sees it early."""
        out: dict[str, int] = defaultdict(int)
        for d in self.deductions:
            if (d.league_id, d.season) != (league_id, year):
                continue
            if (d.effective is None and season_over) or (
                d.effective is not None and d.effective <= as_of
            ):
                out[d.team_ref] += d.points
        return dict(out)

    def net_deductions(self, league_id: int, year: int) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for d in self.deductions:
            if (d.league_id, d.season) == (league_id, year):
                out[d.team_ref] += d.points
        return dict(out)


def load_rules(path: Path | str = "config/league_rules.yml") -> Rules:
    return Rules(**yaml.safe_load(Path(path).read_text()))


def check_against_standings(
    rules: Rules, standings: dict[tuple[int, int], list[dict[str, Any]]]
) -> list[str]:
    """Problems found comparing the rules with final tables {(league_id, year): rows}, where
    rows are the provider's standings entries. Empty when everything agrees."""
    problems: list[str] = []
    for (league_id, year), rows in sorted(standings.items()):
        rule = rules.season(league_id, year)
        if rule is None:
            problems.append(f"{league_id} {year}: no zone rule")
        elif rule.teams != len(rows):
            problems.append(f"{league_id} {year}: rule has {rule.teams} teams, table {len(rows)}")
        net = rules.net_deductions(league_id, year)
        for r in rows:
            ref = str(r["team"]["id"])
            gap = 3 * r["all"]["win"] + r["all"]["draw"] - r["points"]
            if gap != net.get(ref, 0):
                problems.append(
                    f"{league_id} {year} {r['team']['name']}: table gap {gap}, "
                    f"deductions {net.get(ref, 0)}"
                )
        refs = {str(r["team"]["id"]) for r in rows}
        problems += [
            f"{league_id} {year}: deduction for team {ref} not in the table"
            for ref in net
            if ref not in refs
        ]
    return problems
