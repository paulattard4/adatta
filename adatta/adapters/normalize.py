"""Turn API-Football fixture payloads into normalised rows. Idempotent upserts by source_ref."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.db import models

SOURCE_ID = "api_football"
FINISHED = {"FT", "AET", "PEN"}


def ensure_source(session: Session) -> None:
    if session.get(models.Source, SOURCE_ID) is None:
        session.add(models.Source(source_id=SOURCE_ID, description="API-Football v3"))
        session.flush()


def ensure_competition(
    session: Session, *, league_id: int, name: str, country: str, level: str, tier: str
) -> models.Competition:
    comp = session.scalar(
        select(models.Competition).where(
            models.Competition.source_id == SOURCE_ID,
            models.Competition.source_ref == str(league_id),
        )
    )
    if comp is None:
        comp = models.Competition(
            source_id=SOURCE_ID,
            source_ref=str(league_id),
            name=name,
            country=country,
            level=level,
            tier=tier,
        )
        session.add(comp)
        session.flush()
    return comp


def ensure_season(session: Session, competition_id: int, year: int) -> models.Season:
    season = session.scalar(
        select(models.Season).where(
            models.Season.competition_id == competition_id, models.Season.year == year
        )
    )
    if season is None:
        season = models.Season(competition_id=competition_id, year=year)
        session.add(season)
        session.flush()
    return season


def _team(session: Session, t: dict[str, Any], country: str) -> models.Team:
    ref = str(t["id"])
    team = session.scalar(
        select(models.Team).where(models.Team.source_id == SOURCE_ID, models.Team.source_ref == ref)
    )
    if team is None:
        team = models.Team(source_id=SOURCE_ID, source_ref=ref, name=t["name"], country=country)
        session.add(team)
        session.flush()
    return team


class ScoreSplit(NamedTuple):
    home_goals_ht: int | None
    away_goals_ht: int | None
    home_goals_nt: int | None
    away_goals_nt: int | None
    home_goals_et: int | None
    away_goals_et: int | None
    home_pens: int | None
    away_pens: int | None


def _score_of(r: models.MatchResult) -> ScoreSplit:
    return ScoreSplit(
        r.home_goals_ht,
        r.away_goals_ht,
        r.home_goals_nt,
        r.away_goals_nt,
        r.home_goals_et,
        r.away_goals_et,
        r.home_pens,
        r.away_pens,
    )


def _side(score: dict[str, Any], key: str, side: str) -> int | None:
    part = score.get(key) or {}
    v = part.get(side)
    return int(v) if v is not None else None


def _normal_time(goals: int | None, ft: int | None, et: int | None) -> int | None:
    """90' goals. `goals` is the provider's total (incl. ET); `score.fulltime` should be the
    90' score [verify in audit]. Accept fulltime only if it reconciles with goals and ET;
    otherwise return None rather than guess (principle 8: no imputed fiction)."""
    et0 = et or 0
    if ft is not None and goals is not None:
        if ft + et0 == goals:
            return ft
        if ft == goals and et is not None:  # provider reported fulltime cumulatively
            return goals - et0
        return None
    if ft is not None:
        return ft
    if goals is not None and et is not None:
        return goals - et
    return None


def split_score(fx: dict[str, Any]) -> ScoreSplit:
    """Separate normal-time, extra-time (ET-only) and shootout scores from a fixture payload."""
    status = fx["fixture"]["status"]["short"]
    score = fx.get("score") or {}
    goals = fx.get("goals") or {}
    went_et = status in {"AET", "PEN"}
    out: list[int | None] = []
    for side in ("home", "away"):
        g = goals.get(side)
        g = int(g) if g is not None else None
        et = _side(score, "extratime", side) if went_et else None
        if went_et and et is None and g is not None:
            nt = None  # cannot separate ET from the total
        elif went_et:
            nt = _normal_time(g, _side(score, "fulltime", side), et)
        else:  # no extra time played: total goals are the 90' score
            ft = _side(score, "fulltime", side)
            if g is None:
                nt = ft
            elif ft is None or ft == g:
                nt = g
            else:
                nt = None  # total and 90' disagree with no ET: don't guess
        out += [_side(score, "halftime", side), nt, et]
    h_ht, h_nt, h_et, a_ht, a_nt, a_et = out
    pens_h = _side(score, "penalty", "home") if status == "PEN" else None
    pens_a = _side(score, "penalty", "away") if status == "PEN" else None
    return ScoreSplit(h_ht, a_ht, h_nt, a_nt, h_et, a_et, pens_h, pens_a)


def ingest_fixtures(
    session: Session, season: models.Season, fixtures: list[dict[str, Any]], country: str
) -> int:
    """Upsert matches; add a new MatchResult version only when the score changed."""
    now = datetime.now(UTC)
    n = 0
    for fx in fixtures:
        ref = str(fx["fixture"]["id"])
        home = _team(session, fx["teams"]["home"], country)
        away = _team(session, fx["teams"]["away"], country)
        kickoff = datetime.fromisoformat(fx["fixture"]["date"])
        status = fx["fixture"]["status"]["short"]
        match = session.scalar(
            select(models.Match).where(
                models.Match.source_id == SOURCE_ID, models.Match.source_ref == ref
            )
        )
        if match is None:
            match = models.Match(
                source_id=SOURCE_ID,
                source_ref=ref,
                season_id=season.season_id,
                home_team_id=home.team_id,
                away_team_id=away.team_id,
                kickoff_time=kickoff,
            )
            session.add(match)
        match.status = status
        match.kickoff_time = kickoff
        session.flush()
        if status not in FINISHED:
            continue
        new = split_score(fx)
        cur = session.scalar(
            select(models.MatchResult).where(
                models.MatchResult.match_id == match.match_id,
                models.MatchResult.superseded_at.is_(None),
            )
        )
        if cur is not None:
            if _score_of(cur) == new:
                continue
            cur.superseded_at = now
            cur.valid_to = now
        session.add(
            models.MatchResult(
                match_id=match.match_id, source_id=SOURCE_ID, observed_at=now, **new._asdict()
            )
        )
        n += 1
    session.flush()
    return n
