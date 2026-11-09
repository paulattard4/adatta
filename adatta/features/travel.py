"""Home-venue spells and the venue terms of spec §6.2 (Layer 3b): travel burden and altitude.

Travel is measured from the team's home base to the match venue (not previous venue to next,
since squads reconvene at home between fixtures), so it is zero at home. Altitude is the
differential: match venue elevation minus the team's home elevation. Coordinates are
city-level (Open-Meteo geocoding), so distances are good to a few km and a ground share or a
move within one city reads as zero.

Only `auto` and `reviewed` venue coordinates are used. A term is null when either end has none
(principle 8: degrade, never impute). The one stated fallback: a match with no listed venue is
taken to be at the home team's base (docs/FEASIBILITY_REGISTER.md 2.8), with
venue_source = "home_base".

Temporal note: venues come from the provider's published fixture lists, so the base valid at a
kickoff is known before it. Historical lists hold the final venue only (register 2.8), so a
late venue change is seen as if it were known from the start.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_capture as afc
from adatta.adapters import normalize
from adatta.db import models

MIN_RUN = 3  # consecutive home fixtures at one site that make it the home base
SAME_SITE_KM = 15.0  # a team's venues this close together are one site (renames, spellings)
USABLE = ("auto", "reviewed")
EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def utc_offset_h(tz: str, at: datetime) -> float:
    offset = at.astimezone(ZoneInfo(tz)).utcoffset()
    return 0.0 if offset is None else offset.total_seconds() / 3600


@dataclass(frozen=True)
class Spell:
    venue_id: int
    valid_from: datetime
    valid_to: datetime | None
    n_home_matches: int


def home_spells(fixtures: Sequence[tuple[datetime, int]], min_run: int = MIN_RUN) -> list[Spell]:
    """Spells of a team's home base from its home fixtures [(kickoff, venue_id)] in kickoff
    order. A run of at least `min_run` consecutive fixtures at one venue sets the base; shorter
    runs are temporary venues and count towards the base they interrupt. A team with no such
    run gets its most used venue for its whole history."""
    if not fixtures:
        return []
    runs: list[tuple[int, datetime, int]] = []  # (venue_id, first kickoff, length)
    for kickoff, venue in fixtures:
        if runs and runs[-1][0] == venue:
            runs[-1] = (venue, runs[-1][1], runs[-1][2] + 1)
        else:
            runs.append((venue, kickoff, 1))
    bases = [r for r in runs if r[2] >= min_run]
    if not bases:
        top = Counter(v for _, v in fixtures).most_common(1)[0][0]
        return [Spell(top, fixtures[0][0], None, len(fixtures))]
    spells: list[Spell] = []
    for venue, start, _ in bases:
        if spells and spells[-1].venue_id == venue:
            continue  # the same base resumes after a temporary venue
        spells.append(Spell(venue, fixtures[0][0] if not spells else start, None, 0))
    # close each spell where the next starts, and count the fixtures inside it
    out: list[Spell] = []
    for i, s in enumerate(spells):
        end = spells[i + 1].valid_from if i + 1 < len(spells) else None
        n = sum(1 for k, _ in fixtures if k >= s.valid_from and (end is None or k < end))
        out.append(Spell(s.venue_id, s.valid_from, end, n))
    return out


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _usable(v: models.Venue) -> bool:
    return v.geocode_status in USABLE and v.latitude is not None and v.longitude is not None


def same_sites(venues: Sequence[models.Venue]) -> dict[int, int]:
    """{venue_id: site id} for one team's home venues. The provider lists one ground under
    several names and spellings (sponsor renames, 'Venezia'/'Venice', a blank city), so
    venues are one site when placed within SAME_SITE_KM of each other, or when they share a
    normalised name or city."""
    parent = {v.venue_id: v.venue_id for v in venues}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    seen: dict[str, int] = {}
    for v in venues:
        for k in (
            f"n:{normalize.name_key(v.name)}",
            f"c:{normalize.name_key(v.city)}" if normalize.name_key(v.city) else "",
        ):
            if k:
                union(v.venue_id, seen.setdefault(k, v.venue_id))
    placed = [v for v in venues if _usable(v)]
    for i, a in enumerate(placed):
        for b in placed[i + 1 :]:
            assert a.latitude is not None and a.longitude is not None
            assert b.latitude is not None and b.longitude is not None
            if haversine_km(a.latitude, a.longitude, b.latitude, b.longitude) <= SAME_SITE_KM:
                union(a.venue_id, b.venue_id)
    return {v: find(v) for v in parent}


def build_home_venues(
    session: Session, competition_ids: Iterable[int], now: datetime | None = None
) -> Counter[str]:
    """(Re)build team_home_venue from the home league fixtures of the given competitions.
    A team whose spells are unchanged is left alone; a changed team's current rows are
    superseded and the new spells added."""
    now = now or datetime.now(UTC)
    M, S, H = models.Match, models.Season, models.TeamHomeVenue
    rows = session.execute(
        select(M.home_team_id, M.kickoff_time, M.venue_id, M.status)
        .join(S, S.season_id == M.season_id)
        .where(S.competition_id.in_(list(competition_ids)), M.venue_id.is_not(None))
        .order_by(M.home_team_id, M.kickoff_time)
    ).all()
    by_team: dict[int, list[tuple[datetime, int]]] = {}
    for team, kickoff, venue, status in rows:
        if status not in afc.NO_MATCH and venue is not None:
            by_team.setdefault(team, []).append((_utc(kickoff), venue))
    current: dict[int, list[models.TeamHomeVenue]] = {}
    for h in session.scalars(select(H).where(H.superseded_at.is_(None)).order_by(H.valid_from)):
        current.setdefault(h.team_id, []).append(h)
    ids = sorted({v for fixtures in by_team.values() for _, v in fixtures})
    venues = {
        v.venue_id: v
        for part in normalize.chunks(ids)
        for v in session.scalars(select(models.Venue).where(models.Venue.venue_id.in_(part)))
    }
    counts: Counter[str] = Counter()
    for team, fixtures in by_team.items():
        site = same_sites([venues[v] for v in sorted({v for _, v in fixtures})])
        spells = [
            _representative(s, fixtures, venues)
            for s in home_spells([(k, site[v]) for k, v in fixtures])
        ]
        old = [
            Spell(h.venue_id, _utc(h.valid_from),
                  None if h.valid_to is None else _utc(h.valid_to), h.n_home_matches)
            for h in current.get(team, [])
        ]  # fmt: skip
        if old == spells:
            counts["unchanged"] += 1
            continue
        for h in current.get(team, []):
            h.superseded_at = now
        for s in spells:
            session.add(
                H(team_id=team, venue_id=s.venue_id, source_id=normalize.SOURCE_ID,
                  n_home_matches=s.n_home_matches, observed_at=now, valid_from=s.valid_from,
                  valid_to=s.valid_to)
            )  # fmt: skip
        counts["teams_changed" if old else "teams_new"] += 1
        counts["spells"] += len(spells)
        counts["moved"] += len(spells) > 1
    session.commit()
    return counts


def _representative(
    spell: Spell, fixtures: Sequence[tuple[datetime, int]], venues: dict[int, models.Venue]
) -> Spell:
    """The spell's stored venue: its most used placed venue (else its most used venue)."""
    inside = Counter(
        v for k, v in fixtures
        if k >= spell.valid_from and (spell.valid_to is None or k < spell.valid_to)
    )  # fmt: skip
    placed = [v for v, _ in inside.most_common() if _usable(venues[v])]
    best = placed[0] if placed else inside.most_common(1)[0][0]
    return Spell(best, spell.valid_from, spell.valid_to, spell.n_home_matches)


def _base_at(spells: list[models.TeamHomeVenue], at: datetime) -> int | None:
    """The team's base at `at`; before its first listed home fixture, its first base."""
    if not spells:
        return None
    for s in spells:
        if _utc(s.valid_from) <= at and (s.valid_to is None or at < _utc(s.valid_to)):
            return s.venue_id
    return spells[0].venue_id if at < _utc(spells[0].valid_from) else spells[-1].venue_id


def _terms(
    side: str, base: models.Venue | None, here: models.Venue | None, at: datetime
) -> dict[str, float | None]:
    km = tz = alt = None
    if base is not None and here is not None:
        assert base.latitude is not None and base.longitude is not None
        assert here.latitude is not None and here.longitude is not None
        km = round(haversine_km(base.latitude, base.longitude, here.latitude, here.longitude), 1)
        if base.timezone and here.timezone:
            tz = utc_offset_h(here.timezone, at) - utc_offset_h(base.timezone, at)
        if base.elevation_m is not None and here.elevation_m is not None:
            alt = round(here.elevation_m - base.elevation_m, 1)
    return {f"{side}_travel_km": km, f"{side}_tz_shift_h": tz, f"{side}_altitude_diff_m": alt}


FEATURE_SCHEMA = {
    "match_id": pl.Int64,
    "venue_id": pl.Int64,
    "venue_source": pl.Utf8,
    "home_base_venue_id": pl.Int64,
    "away_base_venue_id": pl.Int64,
    "venue_elevation_m": pl.Float64,
    "home_travel_km": pl.Float64,
    "away_travel_km": pl.Float64,
    "home_tz_shift_h": pl.Float64,
    "away_tz_shift_h": pl.Float64,
    "home_altitude_diff_m": pl.Float64,
    "away_altitude_diff_m": pl.Float64,
}


def travel_features(session: Session, matches: Sequence[models.Match]) -> pl.DataFrame:
    """One row per match: venue, each team's home base, travel distance, timezone shift
    (venue UTC offset minus base UTC offset at kickoff, hours) and altitude differential."""
    H, V = models.TeamHomeVenue, models.Venue
    teams = sorted({t for m in matches for t in (m.home_team_id, m.away_team_id)})
    spells: dict[int, list[models.TeamHomeVenue]] = {}
    for part in normalize.chunks(teams):
        for h in session.scalars(
            select(H).where(H.team_id.in_(part), H.superseded_at.is_(None)).order_by(H.valid_from)
        ):
            spells.setdefault(h.team_id, []).append(h)
    rows: list[dict[str, object]] = []
    bases: list[tuple[int | None, int | None]] = []
    for m in matches:
        at = _utc(m.kickoff_time)
        bases.append(
            (_base_at(spells.get(m.home_team_id, []), at),
             _base_at(spells.get(m.away_team_id, []), at))
        )  # fmt: skip
    ids = sorted(
        {m.venue_id for m in matches if m.venue_id is not None}
        | {v for pair in bases for v in pair if v is not None}
    )
    venues = {
        v.venue_id: v
        for part in normalize.chunks(ids)
        for v in session.scalars(select(V).where(V.venue_id.in_(part)))
        if v.geocode_status in USABLE and v.latitude is not None and v.longitude is not None
    }
    for m, (home_base, away_base) in zip(matches, bases, strict=True):
        at = _utc(m.kickoff_time)
        venue_id, source = (m.venue_id, "fixture") if m.venue_id else (home_base, "home_base")
        here = venues.get(venue_id) if venue_id is not None else None
        row: dict[str, object] = {
            "match_id": m.match_id, "venue_id": venue_id,
            "venue_source": source if venue_id is not None else None,
            "home_base_venue_id": home_base, "away_base_venue_id": away_base,
            "venue_elevation_m": here.elevation_m if here else None,
        }  # fmt: skip
        for side, base_id in (("home", home_base), ("away", away_base)):
            base = venues.get(base_id) if base_id is not None else None
            row.update(_terms(side, base, here, at))
        rows.append(row)
    return pl.DataFrame(rows, schema=FEATURE_SCHEMA)


def relevant_venues(
    session: Session, league_ids: Iterable[int], cup_ids: Iterable[int]
) -> dict[int, int]:
    """{venue_id: matches} for venues of bundle league fixtures, plus cup fixtures in which a
    team from those leagues plays that season (a cup tie between two lower-league clubs is
    not modelled). Most used first."""
    M, S = models.Match, models.Season
    league_ids, cup_ids = list(league_ids), list(cup_ids)
    league_teams: set[tuple[int, int]] = set()
    for home, away, year in session.execute(
        select(M.home_team_id, M.away_team_id, S.year)
        .join(S, S.season_id == M.season_id)
        .where(S.competition_id.in_(league_ids))
    ):
        league_teams |= {(home, year), (away, year)}
    counts: Counter[int] = Counter()
    for venue, home, away, year, comp in session.execute(
        select(M.venue_id, M.home_team_id, M.away_team_id, S.year, S.competition_id)
        .join(S, S.season_id == M.season_id)
        .where(S.competition_id.in_(league_ids + cup_ids), M.venue_id.is_not(None))
    ):
        bundle = comp in league_ids or (home, year) in league_teams or (away, year) in league_teams
        if bundle and venue is not None:
            counts[venue] += 1
    return dict(counts.most_common())
