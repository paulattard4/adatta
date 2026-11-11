"""Match importance (spec §6.2: categorical dead rubber / must-win / final, on log lambda and
on the rotation prior; register 2.20).

For each league match and each side, the league table as it stood before kickoff, from:
  - results of the season's matches that kicked off at least RESULT_LAG before it (so every
    result counted was final by then);
  - point deductions in force the day before (config/league_rules.yml; a deduction with an
    unknown date never counts mid-season, D27);
  - the season's zone rules (pre-season positions: title, Champions League, Europe,
    promotion, play-off, survival).

A team has a stake in a boundary (finish in the top b) while it can still finish on either
side of it. Bounds, not exact enumeration (others' head-to-heads are ignored), and both err
towards "still at stake":
  can reach top b   if fewer than b others already have more points than its maximum;
  has clinched it   if fewer than b others can still reach its current points.
  dead_rubber  no stake left in any boundary;
  must_win     a stake that a draw would end (its maximum after a draw falls short);
  live         otherwise.
"final" is for cup finals and play-off finals, which these league tables do not hold; the
column is never "final" here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.db import models
from adatta.features.league_rules import Rules, SeasonRule

RESULT_LAG = timedelta(hours=3)
REGULAR = "Regular Season"  # play-offs, play-outs and deciders share the league id


def stakes_of(rule: SeasonRule) -> dict[str, int]:
    """{stake: b}: the table positions (finish in the top b) worth playing for."""
    out: dict[str, int] = {}
    upper = {
        "title": rule.champion, "ucl": rule.ucl, "europe": rule.europe,
        "promotion": rule.promotion, "playoff": rule.promotion_playoff,
    }  # fmt: skip
    for name, zone in upper.items():
        if zone is not None:
            out[name] = zone[1]
    bottom = rule.relegation_playoff or rule.relegation
    if bottom is not None:
        out["survival"] = bottom[0] - 1
    if rule.relegation_playoff is not None and rule.relegation is not None:
        out["avoid_direct_relegation"] = rule.relegation[0] - 1
    return {k: b for k, b in out.items() if 1 <= b < rule.teams}


@dataclass(frozen=True)
class Standing:
    points: dict[int, int]
    played: dict[int, int]
    goal_diff: dict[int, int]
    goals_for: dict[int, int]

    def rank(self, team: int) -> int:
        def key(t: int) -> tuple[int, int, int]:
            return (-self.points[t], -self.goal_diff[t], -self.goals_for[t])

        return 1 + sum(1 for o in self.points if key(o) < key(team))


def standing(
    teams: Sequence[int], results: Sequence[tuple[int, int, int, int]],
    deductions: dict[int, int],
) -> Standing:  # fmt: skip
    """Table from results [(home, away, home goals, away goals)] minus deductions."""
    pts = {t: -deductions.get(t, 0) for t in teams}
    played = dict.fromkeys(teams, 0)
    gd = dict.fromkeys(teams, 0)
    gf = dict.fromkeys(teams, 0)
    for h, a, hg, ag in results:
        for t in (h, a):
            pts.setdefault(t, -deductions.get(t, 0))
            played.setdefault(t, 0)
            gd.setdefault(t, 0)
            gf.setdefault(t, 0)
        pts[h] += 3 if hg > ag else 1 if hg == ag else 0
        pts[a] += 3 if ag > hg else 1 if hg == ag else 0
        played[h] += 1
        played[a] += 1
        gd[h] += hg - ag
        gd[a] += ag - hg
        gf[h] += hg
        gf[a] += ag
    return Standing(pts, played, gd, gf)


def classify(
    team: int, table: Standing, games_each: int, stakes: dict[str, int]
) -> tuple[str, list[str]]:
    """(dead_rubber | must_win | live, the stakes still open) for `team` before its match."""
    maxima = {t: p + 3 * max(0, games_each - table.played[t]) for t, p in table.points.items()}
    others = [t for t in table.points if t != team]
    mine, best = table.points[team], maxima[team]
    open_: list[str] = []
    must = False
    for name, b in stakes.items():
        ahead_of_best = sum(1 for o in others if table.points[o] > best)
        can_reach = ahead_of_best <= b - 1
        clinched = sum(1 for o in others if maxima[o] >= mine) <= b - 1
        if can_reach and not clinched:
            open_.append(name)
            if sum(1 for o in others if table.points[o] > best - 2) > b - 1:
                must = True
    if not open_:
        return "dead_rubber", []
    return ("must_win" if must else "live"), open_


IMPORTANCE_SCHEMA = {
    "match_id": pl.Int64,
    "home_importance": pl.Utf8,
    "away_importance": pl.Utf8,
    "home_stakes": pl.Utf8,
    "away_stakes": pl.Utf8,
    "home_rank": pl.Int64,
    "away_rank": pl.Int64,
}


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def importance_features(
    session: Session, matches: Sequence[models.Match], rules: Rules,
    horizon: timedelta = timedelta(0),
) -> pl.DataFrame:  # fmt: skip
    """One row per regular-season league match, from the table as of kickoff - `horizon`.
    Null where the season has no zone rule, and for play-off and decider matches (their
    stakes are the tie itself, not the table)."""
    M, S, C, T, R = models.Match, models.Season, models.Competition, models.Team, models.MatchResult
    season_ids = sorted({m.season_id for m in matches})
    meta = {
        sid: (int(ref), year)
        for sid, ref, year in session.execute(
            select(S.season_id, C.source_ref, S.year)
            .join(C, C.competition_id == S.competition_id)
            .where(S.season_id.in_(season_ids))
        )
    }
    rows: list[dict[str, object]] = []
    for sid in season_ids:
        league_id, year = meta[sid]
        rule = rules.season(league_id, year)
        targets = [m for m in matches if m.season_id == sid]
        if rule is None or any(not (m.round or "").startswith(REGULAR) for m in targets):
            rows += [
                {"match_id": m.match_id} for m in targets
                if rule is None or not (m.round or "").startswith(REGULAR)
            ]  # fmt: skip
            targets = [
                m for m in targets if rule is not None and (m.round or "").startswith(REGULAR)
            ]
        if rule is None:
            rows += [{"match_id": m.match_id} for m in targets]
            continue
        played = session.execute(
            select(M.kickoff_time, M.home_team_id, M.away_team_id, R.home_goals_nt, R.away_goals_nt)
            .join(R, R.match_id == M.match_id)
            .where(
                M.season_id == sid,
                M.status.in_(normalize.FINISHED),
                R.superseded_at.is_(None),
                M.round.like(f"{REGULAR}%"),
            )
            .order_by(M.kickoff_time)
        ).all()
        season_teams = sorted(
            {t for m in session.scalars(select(M).where(M.season_id == sid))
             for t in (m.home_team_id, m.away_team_id)}
        )  # fmt: skip
        refs = dict(
            session.execute(
                select(T.team_id, T.source_ref).where(T.team_id.in_(season_teams))
            ).all()
        )
        stakes = stakes_of(rule)
        games_each = 2 * (rule.teams - 1)
        for m in sorted(targets, key=lambda x: x.kickoff_time):
            as_of = _utc(m.kickoff_time) - horizon
            before = [
                (h, a, hg, ag) for k, h, a, hg, ag in played
                if _utc(k) <= as_of - RESULT_LAG and hg is not None and ag is not None
            ]  # fmt: skip
            by_ref = rules.deductions_as_of(league_id, year, (as_of - timedelta(days=1)).date())
            ded: dict[int, int] = defaultdict(int)
            for t, ref in refs.items():
                ded[t] += by_ref.get(ref, 0)
            table = standing(season_teams, before, ded)
            row: dict[str, object] = {"match_id": m.match_id}
            for side, team in (("home", m.home_team_id), ("away", m.away_team_id)):
                label, open_ = classify(team, table, games_each, stakes)
                row[f"{side}_importance"] = label
                row[f"{side}_stakes"] = ",".join(open_)
                row[f"{side}_rank"] = table.rank(team)
            rows.append(row)
    return pl.DataFrame(rows, schema=IMPORTANCE_SCHEMA)
