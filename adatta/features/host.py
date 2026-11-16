"""Host role of national-team matches (D35 Stage 0; docs/BEYOND_LEAGUES_PLAN.md, the host
flag; spec §10.4 asks for host and travel, not a neutral binary).

A venue's own country comes from the provider's `/venues?country=` (stored on
`venue.provider_country`; `venue.country` is the competition's country, which is wrong for a
ground hosting another country's league: Cardiff City Stadium). A team's country is its name
without a youth, reserve or women's suffix, matched to the provider's `/countries` names
(`config/national_countries.yml` lists the names that differ).

  home     the venue is in the home side's country
  swapped  the venue is in the away side's country only (the away side hosts)
  neutral  both sides' countries are known and the venue is in neither
  unknown  no venue, no venue country, or a side without a country where it matters (a club
           in a national competition, a nation the provider does not list); never guessed

`home` and `swapped` need only the matching side's country. No requests here except in
`fetch_venues`, which goes through the client and its quota planner.
"""

from __future__ import annotations

import gzip
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

import polars as pl
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters.api_football import ProviderVenue, parse_venues
from adatta.adapters.open_meteo import Place
from adatta.adapters.storage import RawStore
from adatta.db import models

ROLES = ("home", "swapped", "neutral", "unknown")
ALIASES_PATH = Path("config/national_countries.yml")
SUFFIX = re.compile(r"\s+(U-?\d{2}|B|W)$")
NOT_A_COUNTRY = frozenset({"World"})


def load_aliases(path: Path | str = ALIASES_PATH) -> dict[str, str]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return {str(k): str(v) for k, v in (raw.get("aliases") or {}).items()}


def base_name(team_name: str) -> str:
    """The name without its youth, reserve or women's suffixes ("Canada W U17" -> "Canada")."""
    base, prev = team_name.strip(), ""
    while base != prev:
        prev, base = base, SUFFIX.sub("", base)
    return base


class Countries:
    """Team name -> provider country name, from the provider's names and the aliases."""

    def __init__(self, provider_names: Iterable[str], aliases: dict[str, str]) -> None:
        self.names = frozenset(n for n in provider_names if n not in NOT_A_COUNTRY)
        self.by_key = {normalize.name_key(n): n for n in self.names}
        self.aliases = aliases

    def of(self, team_name: str) -> str | None:
        base = base_name(team_name)
        if base in self.aliases:
            alias = self.aliases[base]
            return alias if alias in self.names else None
        return self.by_key.get(normalize.name_key(base))


def _latest_payloads(session: Session, endpoint: str) -> list[tuple[dict[str, Any], str]]:
    R = models.RawPayload
    rows = session.execute(
        select(R.params, R.storage_key).where(R.endpoint == endpoint).order_by(R.fetched_at)
    ).all()
    latest: dict[str, tuple[dict[str, Any], str]] = {}
    for params, key in rows:
        latest[json.dumps(params, sort_keys=True)] = (params, key)  # newest wins
    return list(latest.values())


def _read(store: RawStore, key: str) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(gzip.decompress(store.get(key)))
    return out


def stored_country_names(session: Session, store: RawStore) -> list[str]:
    """The provider's country names from the newest stored /countries payload ([] if none)."""
    got = _latest_payloads(session, "/countries")
    if not got:
        return []
    return [str(c["name"]) for c in _read(store, got[-1][1]).get("response", []) if c.get("name")]


def fetched_countries(session: Session) -> set[str]:
    return {str(p["country"]) for p, _ in _latest_payloads(session, "/venues") if "country" in p}


def national_matches(session: Session) -> list[tuple[models.Match, str, str, str]]:
    """(match, competition name, home team name, away team name) for every match of a
    national competition."""
    M, S, C, T = models.Match, models.Season, models.Competition, models.Team
    H, A = T.__table__.alias("h"), T.__table__.alias("a")
    rows = session.execute(
        select(M, C.name, H.c.name, A.c.name)
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .join(H, H.c.team_id == M.home_team_id)
        .join(A, A.c.team_id == M.away_team_id)
        .where(C.is_national)
    ).all()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def countries_needed(teams: Iterable[str], countries: Countries) -> list[str]:
    return sorted({c for t in teams if (c := countries.of(t)) is not None})


class VenueSource(Protocol):
    def venues(self, session: Session, country: str) -> list[ProviderVenue]: ...


def fetch_venues(session: Session, client: VenueSource, wanted: Iterable[str]) -> int:
    """/venues?country for each wanted country not fetched yet (one request each; resumable:
    a stop leaves the fetched ones stored). Returns the number fetched."""
    done = fetched_countries(session)
    n = 0
    for country in sorted(set(wanted) - done):
        client.venues(session, country)
        session.commit()
        n += 1
    return n


def stored_venues(session: Session, store: RawStore) -> list[ProviderVenue]:
    """Every venue in the newest stored /venues payload of each country (rebuilt from the raw
    payloads, so a resumed fetch applies everything fetched so far)."""
    out: list[ProviderVenue] = []
    for _, key in _latest_payloads(session, "/venues"):
        out.extend(parse_venues(_read(store, key)))
    return out


PAREN = re.compile(r"^(.*?)\s*\((.*)\)\s*$")


def variants(text: str) -> set[str]:
    """Normalised forms of a provider name or city: whole, before and inside a bracket
    ("Jassim Bin Hamad Stadium (Al-Sadd Stadium)", "ad-Dōha (Doha)")."""
    out = {normalize.name_key(text)}
    m = PAREN.match(text)
    if m:
        out |= {normalize.name_key(m.group(1)), normalize.name_key(m.group(2))}
    return {k for k in out if k}


def apply_countries(session: Session, venues: Sequence[ProviderVenue]) -> dict[str, int]:
    """Set venue.provider_country: by provider id first; a venue stored without an id by its
    normalised name and city (each also before and inside a bracket) when exactly one
    provider country lists that pair."""
    V = models.Venue
    by_ref = {v.ref: v.country for v in venues}
    by_name: dict[tuple[str, str], set[str]] = defaultdict(set)
    for v in venues:
        for n in variants(v.name):
            for c in variants(v.city) or {""}:
                by_name[(n, c)].add(v.country)
    counts: Counter[str] = Counter()
    for row in session.scalars(select(V).where(V.source_id == normalize.SOURCE_ID)):
        country: str | None = None
        if row.source_ref is not None and row.source_ref in by_ref:
            country, how = by_ref[row.source_ref], "by_id"
        else:
            hit = {c for n in variants(row.name) for ci in variants(row.city) or {""}
                   for c in by_name.get((n, ci), set())}  # fmt: skip
            if len(hit) == 1:
                country, how = next(iter(hit)), "by_name"
            elif len(hit) > 1:
                counts["ambiguous_name"] += 1
        if country is not None:
            counts[how] += 1
            if row.provider_country != country:
                row.provider_country = country
                counts["changed"] += 1
    session.flush()
    return dict(counts)


CLEAR_MARGIN = 10.0  # the most populous place wins over a rival country this many times smaller
CITY_MIN = 50_000  # ... or, when no rival's population is known, if it is at least this large


def place_country(places: Sequence[Place], query: str, countries: Countries) -> str | None:
    """The provider country of a city from its populated places: places of exactly the
    queried name if any, else all; one country among them, or the most populous at least
    CLEAR_MARGIN times the largest place in any other country. A UK place takes its region
    (England, Scotland, Wales, Northern Ireland). A place whose country has no provider name
    still counts as a rival."""

    def country(p: Place) -> str | None:
        name = p.admin1 if p.country_code == "GB" else p.country
        return countries.of(name or "")

    exact = [p for p in places if normalize.name_key(p.name) == normalize.name_key(query)]
    cands = sorted(exact or places, key=lambda p: p.population or 0, reverse=True)
    if not cands:
        return None
    best = country(cands[0])
    rivals = [p for p in cands[1:] if country(p) != best or best is None]
    if best is None or not rivals:
        return best
    known = [p.population for p in rivals if p.population is not None]
    pop = cands[0].population or 0
    clear = pop >= CLEAR_MARGIN * max(max(known), 1) if known else pop >= CITY_MIN
    return best if clear else None


class PlaceSource(Protocol):
    def places(self, session: Session, city: str) -> tuple[str, list[Place]]: ...


def geocode_fallback(
    session: Session, om: PlaceSource, rows: Sequence[tuple[models.Match, str, str, str]],
    countries: Countries,
) -> dict[str, int]:  # fmt: skip
    """venue.geo_country for venues of national matches that /venues does not place and that
    have no geo_country yet (resumable): one place search per city, no API-Football quota."""
    V = models.Venue
    ids = sorted({m.venue_id for m, *_ in rows if m.venue_id is not None})
    counts: Counter[str] = Counter()
    for part in normalize.chunks(ids):
        todo = session.scalars(select(V).where(
            V.venue_id.in_(part), V.provider_country.is_(None), V.geo_country.is_(None)
        )).all()  # fmt: skip
        for v in todo:
            text = v.city or v.name
            if not text.strip():
                counts["no_city"] += 1
                continue
            query, places = om.places(session, text)
            got = place_country(places, query, countries)
            counts["placed" if got else "not_placed"] += 1
            v.geo_country = got
        session.commit()
    return dict(counts)


def role(venue_country: str | None, home: str | None, away: str | None) -> str:
    if venue_country is None:
        return "unknown"
    if home is not None and venue_country == home:
        return "home"
    if away is not None and venue_country == away:
        return "swapped"
    if home is not None and away is not None:
        return "neutral"
    return "unknown"


def host_roles(
    session: Session, rows: Sequence[tuple[models.Match, str, str, str]], countries: Countries
) -> pl.DataFrame:
    """match_id, competition, role and basis (why unknown) per national match. The venue's
    country is the provider's, else the geocoded one."""
    V = models.Venue
    ids = sorted({m.venue_id for m, *_ in rows if m.venue_id is not None})
    vc: dict[int, str | None] = {}
    for part in normalize.chunks(ids):
        q = select(V.venue_id, V.provider_country, V.geo_country).where(V.venue_id.in_(part))
        vc.update((int(i), pc or gc) for i, pc, gc in session.execute(q))
    out: dict[str, list[Any]] = {"match_id": [], "competition": [], "status": [], "role": [],
                                 "basis": []}  # fmt: skip
    for m, comp, home, away in rows:
        c = vc.get(m.venue_id) if m.venue_id is not None else None
        h, a = countries.of(home), countries.of(away)
        r = role(c, h, a)
        basis = ("no_venue" if m.venue_id is None else "no_venue_country" if c is None
                 else "no_team_country" if r == "unknown" else "derived")  # fmt: skip
        for k, v in (("match_id", m.match_id), ("competition", comp), ("status", m.status),
                     ("role", r), ("basis", basis)):  # fmt: skip
            out[k].append(v)
    return pl.DataFrame(out)


def write_flags(rows: Sequence[tuple[models.Match, str, str, str]], roles: pl.DataFrame) -> int:
    """match.neutral = (role == neutral) on national matches; returns how many changed."""
    flag = dict(zip(roles["match_id"].to_list(), (roles["role"] == "neutral").to_list(),
                    strict=True))  # fmt: skip
    changed = 0
    for m, *_ in rows:
        want = bool(flag.get(m.match_id, False))
        if bool(m.neutral) != want:
            m.neutral = want
            changed += 1
    return changed
