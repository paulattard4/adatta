"""Provider team ids that hold two clubs (docs/BEYOND_LEAGUES_PLAN.md, Reggiana / Reggina
split). API-Football files Reggina's matches up to 2019/20 under Reggiana's id; the clubs
are named in `config/team_splits.yml`, and this module knows none of them.

A regular-round league match of the shared id goes to the other club when its opponent is
in the other club's group: groups come from who plays whom in that competition-season
without the shared id, and the other club's group is the one whose teams the shared id
hosts in the other club's city. Any other match (play-off, cup) goes to the other club when
the shared id is at home in that city, or when the coach named for the shared side coached
only the other club's matches that season; a coach of only the shared club's matches keeps
it; anything else stays as filed and is listed (principle 8: no guess).

Moving a match rewrites its home/away team id and the team id of its per-match rows (every
table with `match_id` and `team_id`), nothing else. Re-ingesting fixtures never rewrites the
teams of a stored match, so the move holds; a rebuild from raw payloads runs
`adatta teams-split --write` again.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters.backfill import round_robin_groups
from adatta.db import models

SPLITS_PATH = Path("config/team_splits.yml")

GROUP, HOME_CITY, COACH = "group", "home city", "coach"


class SplitError(ValueError):
    pass


@dataclass(frozen=True)
class Split:
    provider: str
    team_ref: str
    other_ref: str
    last_season: int
    other_city: str


def load_splits(path: Path | str = SPLITS_PATH) -> list[Split]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return [
        Split(str(s["provider"]), str(s["team_ref"]), str(s["other_ref"]),
              int(s["last_season"]), str(s["other_city"]))
        for s in raw.get("splits") or []
    ]  # fmt: skip


@dataclass(frozen=True)
class Fx:
    """One match, as much as the split reads."""

    match_id: int
    competition_id: int
    season: int
    round: str
    home: int
    away: int
    venue: str = ""  # venue name and city; "" when unknown


@dataclass
class Plan:
    moves: dict[int, str] = field(default_factory=dict)  # match_id -> reason
    kept: dict[int, str] = field(default_factory=dict)  # resolved as the shared club
    unresolved: list[int] = field(default_factory=list)


def _league_round(f: Fx) -> bool:
    return normalize.is_regular_round(f.round) and not normalize.is_second_phase(f.round)


def plan_split(
    fixtures: Sequence[Fx], shared: int, other: int, city: str,
    coaches: Mapping[tuple[int, int], str],
) -> Plan:  # fmt: skip
    """Which of the shared id's matches are the other club's. `fixtures` holds every match of
    the competition-seasons involved (the groups need them); `coaches` maps (match_id,
    team_id) to the coach named for that side."""
    plan = Plan()

    def in_city(f: Fx) -> bool:
        return city.casefold() in f.venue.casefold()

    def opponent(f: Fx) -> int:
        return f.away if f.home == shared else f.home

    mine = [f for f in fixtures if shared in (f.home, f.away)]
    by_cs: dict[tuple[int, int], list[Fx]] = defaultdict(list)
    for f in fixtures:
        if _league_round(f):
            by_cs[(f.competition_id, f.season)].append(f)
    for (comp, season), fs in sorted(by_cs.items()):
        own = [f for f in fs if shared in (f.home, f.away)]
        if not own:
            continue
        groups = round_robin_groups((f.home, f.away) for f in fs if shared not in (f.home, f.away))
        hosted = {groups.get(opponent(f)) for f in own if f.home == shared and in_city(f)}
        hosted.discard(None)
        if len(hosted) > 1:
            raise SplitError(
                f"competition {comp} season {season}: the shared id hosts {len(hosted)} groups "
                f"in {city}"
            )
        g = next(iter(hosted), None)
        for f in own:
            to_other = g is not None and groups.get(opponent(f)) == g
            (plan.moves if to_other else plan.kept)[f.match_id] = GROUP

    rest = [f for f in mine if f.match_id not in plan.moves and f.match_id not in plan.kept]
    for f in rest:
        if f.home == shared and in_city(f):
            plan.moves[f.match_id] = HOME_CITY
    season_of = {f.match_id: f.season for f in fixtures}
    theirs: dict[int, set[str]] = defaultdict(set)
    ours: dict[int, set[str]] = defaultdict(set)
    for mid in plan.moves:
        if (c := coaches.get((mid, shared))) is not None:
            theirs[season_of[mid]].add(c)
    for f in fixtures:
        if other in (f.home, f.away) and (c := coaches.get((f.match_id, other))) is not None:
            theirs[f.season].add(c)
    for mid in plan.kept:
        if (c := coaches.get((mid, shared))) is not None:
            ours[season_of[mid]].add(c)
    for f in rest:
        if f.match_id in plan.moves:
            continue
        c = coaches.get((f.match_id, shared))
        if c is not None and c in theirs[f.season] and c not in ours[f.season]:
            plan.moves[f.match_id] = COACH
        elif c is not None and c in ours[f.season] and c not in theirs[f.season]:
            plan.kept[f.match_id] = COACH
        else:
            plan.unresolved.append(f.match_id)
    return plan


def moved(fixtures: Iterable[Fx], moves: Mapping[int, str], shared: int, other: int) -> list[Fx]:
    """The fixtures as they read after the moves."""

    def side(t: int, mid: int) -> int:
        return other if t == shared and mid in moves else t

    return [Fx(f.match_id, f.competition_id, f.season, f.round, side(f.home, f.match_id),
               side(f.away, f.match_id), f.venue) for f in fixtures]  # fmt: skip


def group_mismatch(fixtures: Sequence[Fx]) -> list[tuple[int, int, int, int, int]]:
    """(competition, season, team, regular matches, double round robin of its group) wherever
    a team's regular-round count is not exactly a double round robin of its group: a shared
    id merges two groups, so its group-mates fall short."""
    by_cs: dict[tuple[int, int], list[Fx]] = defaultdict(list)
    for f in fixtures:
        if _league_round(f):
            by_cs[(f.competition_id, f.season)].append(f)
    out = []
    for (comp, season), fs in sorted(by_cs.items()):
        groups = round_robin_groups((f.home, f.away) for f in fs)
        size = Counter(groups.values())
        played = Counter(t for f in fs for t in (f.home, f.away))
        for t, n in sorted(played.items()):
            full = 2 * (size[groups[t]] - 1)
            if n != full:
                out.append((comp, season, t, n, full))
    return out


def shared_coaches(
    fixtures: Sequence[Fx], a: int, b: int, coaches: Mapping[tuple[int, int], str]
) -> dict[int, set[str]]:
    """{season: coaches named for both clubs that season}; empty when the split is clean."""
    named: dict[tuple[int, int], set[str]] = defaultdict(set)
    for f in fixtures:
        for t in (a, b):
            if t in (f.home, f.away) and (c := coaches.get((f.match_id, t))) is not None:
                named[(f.season, t)].add(c)
    seasons = {s for s, _ in named}
    return {s: both for s in sorted(seasons) if (both := named[(s, a)] & named[(s, b)])}


# --- database -------------------------------------------------------------------------------


def team_id(session: Session, provider: str, ref: str) -> int:
    T = models.Team
    tid = session.scalar(select(T.team_id).where(T.source_id == provider, T.source_ref == ref))
    if tid is None:
        raise SplitError(f"no team {provider}:{ref}")
    return tid


def load_fixtures(session: Session, shared: int, other: int, last_season: int) -> list[Fx]:
    """Every match of the competition-seasons up to `last_season` in which either id plays."""
    M, S, V = models.Match, models.Season, models.Venue
    seasons = select(M.season_id).join(S, S.season_id == M.season_id).where(
        S.year <= last_season, or_(M.home_team_id.in_([shared, other]),
                                   M.away_team_id.in_([shared, other])))  # fmt: skip
    rows = session.execute(
        select(M.match_id, S.competition_id, S.year, M.round, M.home_team_id, M.away_team_id,
               V.name, V.city)
        .join(S, S.season_id == M.season_id)
        .outerjoin(V, V.venue_id == M.venue_id)
        .where(M.season_id.in_(seasons))
    ).all()  # fmt: skip
    return [Fx(r[0], r[1], r[2], r[3] or "", r[4], r[5], f"{r[6] or ''} {r[7] or ''}".strip())
            for r in rows]  # fmt: skip


def load_coaches(session: Session, match_ids: Iterable[int], teams: Iterable[int]
                 ) -> dict[tuple[int, int], str]:  # fmt: skip
    C = models.MatchCoach
    out: dict[tuple[int, int], str] = {}
    ids, ts = list(match_ids), list(teams)
    for part in normalize.chunks(ids):
        q = select(C.match_id, C.team_id, C.coach_ref).where(
            C.match_id.in_(part), C.team_id.in_(ts), C.superseded_at.is_(None))  # fmt: skip
        out.update(((m, t), c) for m, t, c in session.execute(q))
    return out


def per_match_tables() -> list[str]:
    """Tables whose rows belong to one side of one match."""
    return sorted(t.name for t in models.Base.metadata.sorted_tables
                  if {"match_id", "team_id"} <= set(t.c.keys()))  # fmt: skip


def apply(session: Session, moves: Iterable[int], shared: int, other: int) -> dict[str, int]:
    """Move the matches to the other club; returns rows changed per table."""
    ids = sorted(moves)
    M = models.Match
    counts = {"match": 0}
    for part in normalize.chunks(ids):
        for m in session.scalars(select(M).where(M.match_id.in_(part))):
            if shared not in (m.home_team_id, m.away_team_id):
                continue
            if m.home_team_id == shared:
                m.home_team_id = other
            if m.away_team_id == shared:
                m.away_team_id = other
            counts["match"] += 1
    session.flush()
    for name in per_match_tables():
        t = models.Base.metadata.tables[name]
        n = 0
        for part in normalize.chunks(ids):
            res = session.execute(update(t).where(t.c.match_id.in_(part), t.c.team_id == shared)
                                  .values(team_id=other))  # fmt: skip
            n += res.rowcount  # type: ignore[attr-defined]
        counts[name] = n
    return counts
