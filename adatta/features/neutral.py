"""Venue role per match: is the listed home team at its own ground? (D35 Stage 0; the open
item "neutral venue flag" in docs/SOURCE_FEASIBILITY.md; docs/BEYOND_LEAGUES_PLAN.md.)

API-Football has no neutral field. Coordinates are city-level, so distance can't tell a
neutral ground from a home one in the same city (Arsenal at Wembley reads as zero km). The
role therefore rests on venue identity: a team's *grounds* at a kickoff are the venues of its
league home fixtures in the home spell valid then (adatta.features.travel), keeping the
spell's base and any venue used at least MIN_RUN times in it (sponsor renames, respellings),
matched by venue id or normalised name.

  home     the venue is one of the home team's grounds
  swapped  the venue is one of the away team's grounds (the tie was played at the other club)
  neutral  both teams have grounds, the venue is neither's and isn't in either's city
  review   the venue is in the away team's city, or in a city both teams share, but is
           neither's ground; decided by a rule or an override
  (a venue in the home team's city only, not its known ground, is home by city: a
  temporary home or an unlisted name. A rule can still make it neutral: Arsenal at Wembley)
  unknown  no venue listed, or a team with no league home fixtures (a lower-tier, foreign or
           national side), so nothing to compare with

Precedence: a per-match override (config/neutral_venues.yml) wins; then exact ground
identity (home, swapped), so a final at a finalist's own ground is home, as it plays; then the
competition-round rules (finals, semi-finals at a national stadium) make the rest neutral;
then the derived role. `neutral` on the match is role == "neutral".

National teams have no league grounds, so their matches come out unknown here; the
international host flag (spec §10.4: host and travel, not a neutral binary) is Stage D.

Temporal note: a venue is published before kickoff. Grounds are built from league fixture
lists including later ones in the same spell, which only adds names of the same ground.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import polars as pl
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_capture as afc
from adatta.adapters import normalize
from adatta.db import models
from adatta.features.travel import (
    MIN_RUN,
    SAME_SITE_KM,
    USABLE,
    _utc,
    haversine_km,
    home_spells,
)

ROLES = ("home", "swapped", "neutral", "review", "unknown")
RULES_PATH = Path("config/neutral_venues.yml")


@dataclass(frozen=True)
class Rule:
    league_ids: frozenset[int]
    round: re.Pattern[str]
    note: str


@dataclass(frozen=True)
class Rules:
    rules: tuple[Rule, ...]
    overrides: dict[str, tuple[str, str]]  # fixture source_ref -> (role, note)

    def rule_for(self, league_id: int | None, round_: str | None) -> Rule | None:
        if league_id is None or not round_:
            return None
        return next(
            (r for r in self.rules if league_id in r.league_ids and r.round.fullmatch(round_)),
            None,
        )


def load_rules(path: Path | str = RULES_PATH) -> Rules:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    rules = tuple(
        Rule(frozenset(int(x) for x in r["league_ids"]), re.compile(r["round"]), r.get("note", ""))
        for r in raw.get("rules") or []
    )
    overrides: dict[str, tuple[str, str]] = {}
    for o in raw.get("overrides") or []:
        if o["role"] not in ROLES[:3]:
            raise ValueError(f"override role must be home, swapped or neutral: {o}")
        overrides[str(o["fixture"])] = (o["role"], o.get("note", ""))
    return Rules(rules, overrides)


@dataclass(frozen=True)
class Spell:
    valid_from: datetime
    valid_to: datetime | None
    venues: frozenset[int]  # venue ids of the ground
    names: frozenset[str]  # their normalised names
    base: int  # the spell's base venue


def team_grounds(
    session: Session, league_ids: Iterable[int], teams: Iterable[int]
) -> dict[int, list[Spell]]:
    """Per team, its grounds over time from its league home fixtures. Spells are runs of one
    venue (travel.home_spells on venue ids, not on merged sites: the travel base merges a
    team's venues in one city, which would keep Spurs' Wembley years as a ground for good).
    A spell's ground is its venue plus any venue used at least MIN_RUN times within it
    (alternating names of one ground)."""
    M, S, V = models.Match, models.Season, models.Venue
    teams = sorted(set(teams))
    fixtures: dict[int, list[tuple[datetime, int]]] = {}
    for part in normalize.chunks(teams):
        for team, kickoff, venue, status in session.execute(
            select(M.home_team_id, M.kickoff_time, M.venue_id, M.status)
            .join(S, S.season_id == M.season_id)
            .where(S.competition_id.in_(list(league_ids)), M.home_team_id.in_(part),
                   M.venue_id.is_not(None))
        ):  # fmt: skip
            if status not in afc.NO_MATCH and venue is not None:
                fixtures.setdefault(team, []).append((_utc(kickoff), venue))
    ids = sorted({v for fx in fixtures.values() for _, v in fx})
    names = {
        v.venue_id: normalize.name_key(v.name)
        for part in normalize.chunks(ids)
        for v in session.scalars(select(V).where(V.venue_id.in_(part)))
    }
    out: dict[int, list[Spell]] = {}
    for team, fx in fixtures.items():
        fx.sort()
        for h in home_spells(fx):
            hi = h.valid_to
            used = Counter(v for k, v in fx if k >= h.valid_from and (hi is None or k < hi))
            ground = {h.venue_id} | {v for v, n in used.items() if n >= MIN_RUN}
            out.setdefault(team, []).append(
                Spell(h.valid_from, h.valid_to, frozenset(ground),
                      frozenset(names[v] for v in ground if names.get(v)), h.venue_id)
            )  # fmt: skip
    return out


def spell_at(spells: list[Spell], at: datetime) -> Spell | None:
    """The spell valid at `at`; before the first one, the first; after the last, the last."""
    if not spells:
        return None
    for s in spells:
        if s.valid_from <= at and (s.valid_to is None or at < s.valid_to):
            return s
    return spells[0] if at < spells[0].valid_from else spells[-1]


PREFIX_MIN = 10  # shortest ground name matched as a prefix ("St Mary's Stadium, Southampton")


def _is_ground(spell: Spell | None, venue: models.Venue) -> bool:
    """The venue is one of the spell's grounds: same id, same normalised name, or a known
    name of at least PREFIX_MIN characters that starts the venue's name (the provider
    sometimes appends the town: "Allianz Arena (Munchen)", "Old Trafford, Manchester")."""
    if spell is None:
        return False
    key = normalize.name_key(venue.name)
    return (
        venue.venue_id in spell.venues
        or key in spell.names
        or any(len(n) >= PREFIX_MIN and key.startswith(n) for n in spell.names)
    )


def _same_city(base: models.Venue | None, venue: models.Venue) -> bool:
    if base is None:
        return False
    city = normalize.name_key(venue.city)
    if city and city == normalize.name_key(base.city):
        return True
    placed = all(
        v.geocode_status in USABLE and v.latitude is not None and v.longitude is not None
        for v in (base, venue)
    )
    if not placed:
        return False
    assert base.latitude is not None and base.longitude is not None
    assert venue.latitude is not None and venue.longitude is not None
    return haversine_km(base.latitude, base.longitude, venue.latitude, venue.longitude) <= (
        SAME_SITE_KM
    )


def derive(
    venue: models.Venue | None,
    home: Spell | None,
    away: Spell | None,
    bases: dict[int, models.Venue],
) -> str:
    """The role from the venue alone (no rules or overrides). Besides ROLES, two derived
    values: home_city, another ground in the home team's city only (a temporary home, or a
    name the league fixtures never used), taken as home unless a rule says neutral; and
    away_city, a ground in the away team's city only, sent to review."""
    if venue is None:
        return "unknown"
    if _is_ground(home, venue):
        return "home"
    if _is_ground(away, venue):
        return "swapped"
    in_home = home is not None and _same_city(bases.get(home.base), venue)
    in_away = away is not None and _same_city(bases.get(away.base), venue)
    if in_home and in_away:
        return "review"  # a third ground in a shared city
    if in_home:
        return "home_city"
    if in_away:
        return "away_city"
    if home is None or away is None:
        return "unknown"
    return "neutral"


SCHEMA = {
    "match_id": pl.Int64,
    "derived": pl.Utf8,
    "role": pl.Utf8,
    "basis": pl.Utf8,
    "neutral": pl.Boolean,
}


def venue_roles(
    session: Session,
    matches: Sequence[models.Match],
    league_ids: Iterable[int],
    rules: Rules,
    league_of: dict[int, int],
) -> pl.DataFrame:
    """One row per match: the derived role, the final role and its basis (override, ground,
    rule or derived). `league_of` maps season_id to the provider's league id, for the rules."""
    teams = {t for m in matches for t in (m.home_team_id, m.away_team_id)}
    grounds = team_grounds(session, league_ids, teams)
    ids = sorted({m.venue_id for m in matches if m.venue_id is not None}
                 | {s.base for ss in grounds.values() for s in ss})  # fmt: skip
    venues = {
        v.venue_id: v
        for part in normalize.chunks(ids)
        for v in session.scalars(select(models.Venue).where(models.Venue.venue_id.in_(part)))
    }
    rows: list[dict[str, object]] = []
    for m in matches:
        at = _utc(m.kickoff_time)
        home = spell_at(grounds.get(m.home_team_id, []), at)
        away = spell_at(grounds.get(m.away_team_id, []), at)
        venue = venues.get(m.venue_id) if m.venue_id is not None else None
        derived = derive(venue, home, away, venues)
        role, basis = derived, "derived"
        if derived in ("home", "swapped"):
            basis = "ground"
        elif rules.rule_for(league_of.get(m.season_id), m.round) is not None:
            role, basis = "neutral", "rule"
        elif derived == "home_city":
            role, basis = "home", "city"
        elif derived == "away_city":
            role = "review"
        if m.source_ref in rules.overrides:
            role, basis = rules.overrides[m.source_ref][0], "override"
        rows.append({"match_id": m.match_id, "derived": derived, "role": role, "basis": basis,
                     "neutral": role == "neutral"})  # fmt: skip
    return pl.DataFrame(rows, schema=SCHEMA)


def write_flags(session: Session, matches: Sequence[models.Match], roles: pl.DataFrame) -> int:
    """Set match.neutral from the roles; returns how many changed. Idempotent."""
    flag = dict(zip(roles["match_id"].to_list(), roles["neutral"].to_list(), strict=True))
    changed = 0
    for m in matches:
        want = bool(flag.get(m.match_id, False))
        if bool(m.neutral) != want:
            m.neutral = want
            changed += 1
    session.flush()
    return changed


def stage(round_: str | None) -> str:
    """A coarse round type for reports: league, group, qualifying, knockout, semi, final."""
    r = (round_ or "").casefold()
    if r == "final":
        return "final"
    if r.startswith("semi"):
        return "semi"
    if "regular season" in r:
        return "league"
    if r.startswith(("group", "league stage")):
        return "group"
    if "qualif" in r or "preliminary" in r or "play-off" in r or "playoff" in r:
        return "qualifying"
    return "knockout" if r else "none"
