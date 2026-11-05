"""Turn API-Football fixture payloads into normalised rows. Idempotent upserts by source_ref."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
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


def ensure_team(session: Session, ref: str, name: str, country: str = "") -> models.Team:
    return _team(session, {"id": ref, "name": name}, country)


def ensure_player(session: Session, ref: str, name: str) -> models.Player:
    player = session.scalar(
        select(models.Player).where(
            models.Player.source_id == SOURCE_ID, models.Player.source_ref == ref
        )
    )
    if player is None:
        player = models.Player(source_id=SOURCE_ID, source_ref=ref, name=name)
        session.add(player)
        session.flush()
    return player


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


def chunks(xs: list[Any], n: int = 500) -> Iterator[list[Any]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def teams_by_ref(
    session: Session, teams: dict[str, str], country: str = ""
) -> dict[str, models.Team]:
    """Bulk get-or-create teams from {source_ref: name}: one SELECT per 500 refs, one flush."""
    T = models.Team
    out: dict[str, models.Team] = {}
    for part in chunks(sorted(teams)):
        out.update(
            (t.source_ref, t)
            for t in session.scalars(
                select(T).where(T.source_id == SOURCE_ID, T.source_ref.in_(part))
            )
        )
    new = [
        T(source_id=SOURCE_ID, source_ref=ref, name=name, country=country)
        for ref, name in teams.items()
        if ref not in out
    ]
    if new:
        session.add_all(new)
        session.flush()
        out.update((t.source_ref, t) for t in new)
    return out


def players_by_ref(session: Session, players: dict[str, str]) -> dict[str, models.Player]:
    """Bulk get-or-create players from {source_ref: name}."""
    P = models.Player
    out: dict[str, models.Player] = {}
    for part in chunks(sorted(players)):
        out.update(
            (p.source_ref, p)
            for p in session.scalars(
                select(P).where(P.source_id == SOURCE_ID, P.source_ref.in_(part))
            )
        )
    new = [P(source_id=SOURCE_ID, source_ref=r, name=n) for r, n in players.items() if r not in out]
    if new:
        session.add_all(new)
        session.flush()
        out.update((p.source_ref, p) for p in new)
    return out


def matches_by_ref(session: Session, refs: Iterable[str]) -> dict[str, models.Match]:
    M = models.Match
    out: dict[str, models.Match] = {}
    for part in chunks(sorted(set(refs))):
        out.update(
            (m.source_ref, m)
            for m in session.scalars(
                select(M).where(M.source_id == SOURCE_ID, M.source_ref.in_(part))
            )
        )
    return out


def ingest_fixtures(
    session: Session, season: models.Season, fixtures: list[dict[str, Any]], country: str
) -> int:
    """Upsert matches; add a new MatchResult version only when the score changed.

    Lookups are batched (a handful of queries per call, not per fixture): against a remote
    database, per-row round trips made a 380-fixture season take minutes.
    """
    now = datetime.now(UTC)
    teams = teams_by_ref(
        session,
        {str(fx["teams"][side]["id"]): fx["teams"][side]["name"] for fx in fixtures
         for side in ("home", "away")},
        country,
    )  # fmt: skip
    matches = matches_by_ref(session, (str(fx["fixture"]["id"]) for fx in fixtures))
    for fx in fixtures:
        ref = str(fx["fixture"]["id"])
        kickoff = datetime.fromisoformat(fx["fixture"]["date"])
        match = matches.get(ref)
        if match is None:
            match = models.Match(
                source_id=SOURCE_ID,
                source_ref=ref,
                season_id=season.season_id,
                home_team_id=teams[str(fx["teams"]["home"]["id"])].team_id,
                away_team_id=teams[str(fx["teams"]["away"]["id"])].team_id,
                kickoff_time=kickoff,
            )
            session.add(match)
            matches[ref] = match
        match.status = fx["fixture"]["status"]["short"]
        match.kickoff_time = kickoff
        match.round = (fx.get("league") or {}).get("round") or match.round
    session.flush()
    finished = [fx for fx in fixtures if fx["fixture"]["status"]["short"] in FINISHED]
    R = models.MatchResult
    ids = [matches[str(fx["fixture"]["id"])].match_id for fx in finished]
    current: dict[int, models.MatchResult] = {}
    for part in chunks(ids):
        current.update(
            (r.match_id, r)
            for r in session.scalars(
                select(R).where(R.match_id.in_(part), R.superseded_at.is_(None))
            )
        )
    n = 0
    for fx in finished:
        match = matches[str(fx["fixture"]["id"])]
        new = split_score(fx)
        cur = current.get(match.match_id)
        if cur is not None:
            if _score_of(cur) == new:
                continue
            cur.superseded_at = now
            cur.valid_to = now
        session.add(
            R(match_id=match.match_id, source_id=SOURCE_ID, observed_at=now, **new._asdict())
        )
        n += 1
    session.flush()
    return n


def ingest_team_stats(
    session: Session, match_id: int, rows: list[dict[str, Any]], storage_key: str,
    observed_at: datetime, new_match: bool = False,
    teams: dict[str, models.Team] | None = None,
) -> int:  # fmt: skip
    """Insert team_match_stat rows for one match; supersede a current row whose values
    differ, skip identical ones. Returns rows added. `new_match=True` (caller guarantees the
    match has no rows yet) skips the lookup and `teams` (pre-resolved refs) the team lookup, for
    bulk rebuilds."""
    if not rows:
        return 0
    T = models.TeamMatchStat
    if teams is None or any(r["team_ref"] not in teams for r in rows):
        teams = teams_by_ref(session, {r["team_ref"]: r["team_ref"] for r in rows})
    if new_match:
        current: dict[int, models.TeamMatchStat] = {}
    else:
        current = {
            t.team_id: t
            for t in session.scalars(
                select(T).where(T.match_id == match_id, T.superseded_at.is_(None))
            )
        }
    added = 0
    for r in rows:
        team_id = teams[r["team_ref"]].team_id
        values = {k: v for k, v in r.items() if k != "team_ref"}
        cur = current.get(team_id)
        if cur is not None:
            if cur.stats == values["stats"]:
                continue
            cur.superseded_at = observed_at
            cur.valid_to = observed_at
        session.add(
            T(match_id=match_id, team_id=team_id, source_id=SOURCE_ID, storage_key=storage_key,
              observed_at=observed_at, valid_from=observed_at, **values)
        )  # fmt: skip
        added += 1
    return added
