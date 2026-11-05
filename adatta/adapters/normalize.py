"""Turn API-Football fixture payloads into normalised rows. Idempotent upserts by source_ref."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
        goals, ht = fx["goals"], fx["score"]["halftime"]
        new = (ht["home"], ht["away"], goals["home"], goals["away"])
        cur = session.scalar(
            select(models.MatchResult).where(
                models.MatchResult.match_id == match.match_id,
                models.MatchResult.superseded_at.is_(None),
            )
        )
        if cur is not None:
            old = (cur.home_goals_ht, cur.away_goals_ht, cur.home_goals_ft, cur.away_goals_ft)
            if old == new:
                continue
            cur.superseded_at = now
            cur.valid_to = now
        session.add(
            models.MatchResult(
                match_id=match.match_id,
                source_id=SOURCE_ID,
                observed_at=now,
                home_goals_ht=new[0],
                away_goals_ht=new[1],
                home_goals_ft=new[2],
                away_goals_ft=new[3],
            )
        )
        n += 1
    session.flush()
    return n
