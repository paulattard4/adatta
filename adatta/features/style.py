"""Team style as of a prediction time (spec §6.1, Layer 3a; register 2.1-2.4; Phase 2 F1).

Per side of a match, the side's own league matches in the WINDOW before as_of:
  {side}_style_matches   league matches with statistics for both sides in the window
  {side}_shots_for_pm    shots per match, for and against; shots on target likewise
  {side}_shots_against_pm
  {side}_sot_for_pm, {side}_sot_against_pm
  {side}_possession      mean ball possession, percent (over the matches that report it)
  {side}_tempo_rel       league-normalised tempo: the mean over the window's matches of
                         (shots for + shots against) / the mean of the same quantity over
                         every match of that match's league in the same window, minus 1.
                         Each match is normalised by its own league, so a promoted side's
                         earlier season is measured against the league it was played in.

As-of rule: a match counts when it kicked off at or before as_of - RESULT_LAG and in the
WINDOW before that (the same "final by as_of" rule as results, referee history and
importance). Statistics are post-match facts usable from kickoff + result lag, like the
result (models.TeamMatchStat); the current, non-superseded row is used.

Only league matches count: domestic cups have patchy statistics (register 2.1: 8-100%),
and the league-normalisation needs a league. Cup and continental matches are skipped.

Nulls: every value except {side}_style_matches is null when the side has fewer than
MIN_MATCHES matches in the window (cold start, or a side new to the loaded leagues);
possession is null when none of the window's matches report it. Never imputed or
zero-filled: the model drops the style term for that match (register 2.1, principle 8).

Acceptance test (docs/PHASE2_TASKS.md F1, stated 2026-11-11, before building):
  leakage   a match after as_of - RESULT_LAG, or its statistics, changes nothing
            (tests/test_style.py);
  coverage  >= 99% of 2019-2025 bundle league matches whose side has at least MIN_MATCHES
            prior matches in the window get values; null otherwise.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.db import models
from adatta.features.referees import RESULT_LAG

WINDOW = timedelta(days=365)
MIN_MATCHES = 5
DEFAULT_HORIZON = timedelta(hours=1)
NOT_LEAGUE_COUNTRIES = frozenset({"Europe", "World"})  # continental (as context.CONTINENTAL)
VALUES = ("shots_for_pm", "shots_against_pm", "sot_for_pm", "sot_against_pm", "possession",
          "tempo_rel")  # fmt: skip

# columns of a series: shots for, against, on target for, against, tempo, possession, has it
_SF, _SA, _OF, _OA, _TEMPO, _POSS, _POSS_N = range(7)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Played:
    """One side of one finished league match that has statistics for both sides."""

    team_id: int
    competition_id: int
    kickoff: datetime
    shots_for: float
    shots_against: float
    sot_for: float
    sot_against: float
    possession: float | None


class _Series:
    """Rows in kickoff order with prefix sums, for window sums by bisection."""

    def __init__(self, rows: list[Played]) -> None:
        rows = sorted(rows, key=lambda r: r.kickoff)
        self.kickoffs = [r.kickoff for r in rows]
        vals = np.array(
            [[r.shots_for, r.shots_against, r.sot_for, r.sot_against,
              r.shots_for + r.shots_against,
              r.possession if r.possession is not None else 0.0,
              1.0 if r.possession is not None else 0.0] for r in rows],
            dtype=float,
        ).reshape(len(rows), 7)  # fmt: skip
        self.cum = np.vstack([np.zeros((1, 7)), np.cumsum(vals, axis=0)])

    def window(self, start: datetime, end: datetime) -> tuple[int, np.ndarray]:
        """(rows, column sums) with start < kickoff <= end."""
        hi = bisect_right(self.kickoffs, end)
        lo = bisect_right(self.kickoffs, start)
        n = max(hi - lo, 0)
        return n, (self.cum[hi] - self.cum[lo]) if n else np.zeros(7)


class StyleHistory:
    """As-of style per team from finished league matches (see the module docstring)."""

    def __init__(self, rows: Iterable[Played]) -> None:
        by_team: dict[int, list[Played]] = defaultdict(list)
        by_team_comp: dict[tuple[int, int], list[Played]] = defaultdict(list)
        by_comp: dict[int, list[Played]] = defaultdict(list)
        for r in rows:
            by_team[r.team_id].append(r)
            by_team_comp[(r.team_id, r.competition_id)].append(r)
            by_comp[r.competition_id].append(r)
        self._team = {k: _Series(v) for k, v in by_team.items()}
        self._team_comp = {k: _Series(v) for k, v in by_team_comp.items()}
        self._comp = {k: _Series(v) for k, v in by_comp.items()}
        self._comps_of: dict[int, list[int]] = defaultdict(list)
        for team, comp in by_team_comp:
            self._comps_of[team].append(comp)

    def team(self, team_id: int, as_of: datetime) -> dict[str, float | int | None]:
        """The side's style values as of as_of (keys: matches and VALUES)."""
        end = _utc(as_of) - RESULT_LAG
        start = end - WINDOW
        series = self._team.get(team_id)
        n, s = series.window(start, end) if series is not None else (0, np.zeros(7))
        out: dict[str, float | int | None] = {"matches": n} | dict.fromkeys(VALUES)
        if n < MIN_MATCHES:
            return out
        out["shots_for_pm"] = round(float(s[_SF]) / n, 4)
        out["shots_against_pm"] = round(float(s[_SA]) / n, 4)
        out["sot_for_pm"] = round(float(s[_OF]) / n, 4)
        out["sot_against_pm"] = round(float(s[_OA]) / n, 4)
        if s[_POSS_N] > 0:
            out["possession"] = round(float(s[_POSS] / s[_POSS_N]), 3)
        ratio = 0.0
        for comp in self._comps_of[team_id]:
            nc, sc = self._team_comp[(team_id, comp)].window(start, end)
            if nc == 0:
                continue
            nl, sl = self._comp[comp].window(start, end)  # nl >= nc > 0
            league_mean = float(sl[_TEMPO]) / nl
            if league_mean <= 0:
                return out | {"tempo_rel": None}
            ratio += float(sc[_TEMPO]) / league_mean
        out["tempo_rel"] = round(ratio / n - 1.0, 5)
        return out

    @classmethod
    def load(cls, session: Session) -> StyleHistory:
        """Every finished league match whose two sides both have current shot statistics."""
        M, S, C, T = models.Match, models.Season, models.Competition, models.TeamMatchStat
        sides: dict[int, dict[int, tuple[float, float, float | None]]] = defaultdict(dict)
        meta: dict[int, tuple[int, int, int, datetime]] = {}
        for mid, home, away, ko, comp_id, country, tier, team, shots, sot, poss in session.execute(
            select(M.match_id, M.home_team_id, M.away_team_id, M.kickoff_time,
                   C.competition_id, C.country, C.tier, T.team_id, T.shots_total,
                   T.shots_on_target, T.possession)
            .join(S, S.season_id == M.season_id)
            .join(C, C.competition_id == S.competition_id)
            .join(T, T.match_id == M.match_id)
            .where(M.status.in_(normalize.FINISHED), T.superseded_at.is_(None))
        ):  # fmt: skip
            if tier == "cup" or country in NOT_LEAGUE_COUNTRIES:
                continue
            if shots is None or sot is None or team not in (home, away):
                continue
            meta[mid] = (comp_id, home, away, _utc(ko))
            sides[mid][team] = (float(shots), float(sot), poss)
        rows: list[Played] = []
        for mid, (comp_id, home, away, ko) in meta.items():
            got = sides[mid]
            if home not in got or away not in got:
                continue
            for me, opp in ((home, away), (away, home)):
                (sf, of, poss), (sa, oa, _) = got[me], got[opp]
                rows.append(Played(me, comp_id, ko, sf, sa, of, oa, poss))
        return cls(rows)


def style_features(
    history: StyleHistory, matches: Sequence[models.Match], horizon: timedelta = DEFAULT_HORIZON
) -> pl.DataFrame:
    """One row per match: both sides' style as of kickoff - horizon."""
    rows = []
    for m in matches:
        as_of = _utc(m.kickoff_time) - horizon
        row: dict[str, object] = {"match_id": m.match_id, "as_of": as_of}
        for side, team in (("home", m.home_team_id), ("away", m.away_team_id)):
            vals = history.team(team, as_of)
            row[f"{side}_style_matches"] = vals["matches"]
            row |= {f"{side}_{k}": vals[k] for k in VALUES}
        rows.append(row)
    schema: dict[str, pl.DataType | type[pl.DataType]] = {
        "match_id": pl.Int64, "as_of": pl.Datetime("us", "UTC"),
    }  # fmt: skip
    for side in ("home", "away"):
        schema[f"{side}_style_matches"] = pl.Int64
        schema |= {f"{side}_{k}": pl.Float64 for k in VALUES}
    return pl.DataFrame(rows, schema=schema)
