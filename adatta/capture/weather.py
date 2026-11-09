"""Weather-forecast capture for upcoming matches (spec §6.2, §12.2; feasibility register 2.12).

As-issued forecasts can't be fetched after the fact before about Feb 2024, and only for a few
days back after that, so they are captured live, like odds. Each tick:

  overrides  config/venue_overrides.yml (manual review results) -> venue, status reviewed
  geocode    venues of upcoming matches not yet geocoded -> Open-Meteo city search
  forecast   matches whose lead time has crossed into a new milestone bucket since their last
             snapshot (or whose kickoff moved) -> one forecast request per venue, one
             weather_snapshot row per match

Milestones bound the rows per match (at most one per bucket); lead_hours on each row keeps the
exact lead. Only `auto` and `reviewed` coordinates are used: ambiguous and unresolved venues
wait for review (`adatta venues-review`) and get no forecast rather than a guessed one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_capture as afc
from adatta.adapters import normalize
from adatta.adapters.open_meteo import (
    DEFAULT_TIMEZONE,
    SOURCE_ID,
    OpenMeteoClient,
    city_query,
    match_weather,
)
from adatta.db import models

HORIZON = timedelta(days=15)  # inside the 16-day forecast, so the match hours are covered
MILESTONES_H = (360.0, 168.0, 72.0, 24.0, 6.0, 2.0)
USABLE = ("auto", "reviewed")
MAX_GEOCODES = 40  # venues per tick; the rest are picked up by later ticks
MAX_FORECASTS = 150  # forecast requests per tick


class VenueOverride(BaseModel):
    name: str
    city: str
    latitude: float
    longitude: float
    elevation_m: float | None = None
    timezone: str | None = None  # default: the competition country's zone
    note: str = ""


def load_overrides(path: Path | str = "config/venue_overrides.yml") -> list[VenueOverride]:
    p = Path(path)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text()) or {}
    return [VenueOverride(**row) for row in raw.get("venues") or []]


def bucket(lead_hours: float) -> float | None:
    """The smallest milestone at or above the lead time; None beyond the horizon."""
    fits = [m for m in MILESTONES_H if lead_hours <= m]
    return min(fits) if fits else None


def apply_overrides(session: Session, overrides: list[VenueOverride], now: datetime) -> int:
    V = models.Venue
    n = 0
    for o in overrides:
        for v in session.scalars(select(V).where(V.name == o.name, V.city == o.city)):
            tz = o.timezone or DEFAULT_TIMEZONE.get(v.country)
            new = (o.latitude, o.longitude, o.elevation_m, tz)
            old = (v.latitude, v.longitude, v.elevation_m, v.timezone)
            if v.geocode_status == "reviewed" and old == new:
                continue
            v.latitude, v.longitude, v.elevation_m, v.timezone = new
            v.geocode_status, v.geocode_source = "reviewed", "config/venue_overrides.yml"
            v.geocode_match, v.geocoded_at = (o.note or "manual review")[:255], now
            n += 1
    session.flush()
    return n


def _stored_query(v: models.Venue) -> str | None:
    """The city query a geocoded venue was looked up with (the head of geocode_match)."""
    if not v.geocode_match:
        return None
    return v.geocode_match.split(" -> ")[0].split(": no populated place")[0]


def geocode_venues(
    session: Session, om: OpenMeteoClient, venue_ids: list[int], now: datetime,
    limit: int = MAX_GEOCODES, backlog: bool = False,
) -> Counter[str]:  # fmt: skip
    """Geocode pending venues by city; record the outcome either way (never retried blindly:
    ambiguous and unresolved venues need a reviewed override). A placed venue with no
    elevation gets it from the elevation API.

    With backlog (the one-off `venues-geocode` command, never the tick), also: venues placed
    automatically before timezones were stored get theirs from the same search (same place
    only), and ambiguous or unresolved venues are retried when their city query has changed
    since (a new alias or the bracketed-city rule)."""
    V = models.Venue
    counts: Counter[str] = Counter()
    wanted = V.geocode_status == "pending"
    if backlog:
        wanted = (
            wanted
            | ((V.geocode_status == "auto") & V.timezone.is_(None))
            | V.geocode_status.in_(["ambiguous", "unresolved"])
        )
    todo = [
        v
        for part in normalize.chunks(venue_ids)
        for v in session.scalars(select(V).where(V.venue_id.in_(part), wanted))
        if v.geocode_status not in ("ambiguous", "unresolved")
        or _stored_query(v) != city_query(v.city or v.name)
    ]
    for v in todo[:limit]:
        g = om.geocode(session, v.city or v.name, v.country)
        if v.geocode_status == "auto":
            same = g.place is not None and (g.place.latitude, g.place.longitude) == (
                v.latitude, v.longitude,
            )  # fmt: skip
            if same and g.place is not None:
                v.timezone = g.place.timezone
            counts["timezone_filled" if same else "timezone_mismatch"] += 1
            session.commit()
            continue
        counts[f"retried_{v.geocode_status}"] += v.geocode_status != "pending"
        v.geocode_status, v.geocode_source = g.status, SOURCE_ID
        v.geocode_match, v.geocoded_at = g.describe()[:255], om.last_response_at or now
        if g.place is not None:
            v.latitude, v.longitude = g.place.latitude, g.place.longitude
            v.elevation_m = g.place.elevation_m
            v.timezone = g.place.timezone or DEFAULT_TIMEZONE.get(v.country)
        counts[g.status] += 1
        session.commit()
    counts["deferred"] += max(0, len(todo) - limit)
    counts["borrowed"] += borrow_by_name(session, venue_ids, now)
    counts["elevation_filled"] += fill_elevations(session, om, venue_ids)
    return +counts


def borrow_by_name(session: Session, venue_ids: list[int], now: datetime) -> int:
    """An ambiguous or unresolved venue takes the coordinates of a placed venue of the same
    (normalised) name in the same country, when every such venue is at one place: the
    provider lists one ground with a blank or misspelt city ('Estadio de Mestalla | Valdivia')
    as well as correctly."""
    V = models.Venue
    stuck = [
        v
        for part in normalize.chunks(venue_ids)
        for v in session.scalars(
            select(V).where(V.venue_id.in_(part), V.geocode_status.in_(["ambiguous", "unresolved"]))
        )
    ]
    if not stuck:
        return 0
    places: dict[tuple[str, str], set[tuple[float, float, float | None, str | None]]] = {}
    for p in session.scalars(
        select(V).where(
            V.geocode_status.in_(USABLE),
            V.latitude.is_not(None),
            V.country.in_(sorted({v.country for v in stuck})),
        )
    ):
        assert p.latitude is not None and p.longitude is not None
        where = (round(p.latitude, 3), round(p.longitude, 3), p.elevation_m, p.timezone)
        places.setdefault((p.country, normalize.name_key(p.name)), set()).add(where)
    n = 0
    for v in stuck:
        found = places.get((v.country, normalize.name_key(v.name)), set())
        if len({(lat, lon) for lat, lon, _, _ in found}) != 1:
            continue
        lat, lon, elev, tz = sorted(found, key=lambda w: (w[2] is None, w[3] is None))[0]
        v.latitude, v.longitude, v.elevation_m, v.timezone = lat, lon, elev, tz
        v.geocode_status, v.geocode_source, v.geocoded_at = "auto", "venue_name", now
        v.geocode_match = f"same name as a placed venue at {lat}, {lon}"
        n += 1
    session.commit()
    return n


def fill_elevations(session: Session, om: OpenMeteoClient, venue_ids: list[int]) -> int:
    V = models.Venue
    n = 0
    for part in normalize.chunks(venue_ids):
        for v in session.scalars(
            select(V).where(
                V.venue_id.in_(part),
                V.geocode_status.in_(USABLE),
                V.elevation_m.is_(None),
                V.latitude.is_not(None),
                V.longitude.is_not(None),
            )
        ):
            assert v.latitude is not None and v.longitude is not None
            v.elevation_m = om.elevation(session, v.latitude, v.longitude)
            n += v.elevation_m is not None
            session.commit()
    return n


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def capture_weather(
    session: Session, om: OpenMeteoClient, now: datetime,
    overrides: list[VenueOverride] | None = None, max_forecasts: int = MAX_FORECASTS,
    clock: Callable[[], datetime] | None = None,
) -> Counter[str]:  # fmt: skip
    M, V, W = models.Match, models.Venue, models.WeatherSnapshot
    counts: Counter[str] = Counter()
    counts["overrides"] = apply_overrides(
        session, load_overrides() if overrides is None else overrides, now
    )
    upcoming = [
        m
        for m in session.scalars(
            select(M).where(
                M.kickoff_time > now, M.kickoff_time <= now + HORIZON, M.venue_id.is_not(None)
            )
        )
        if m.status not in afc.NO_MATCH and m.status != "TBD"
    ]
    venue_ids = sorted({m.venue_id for m in upcoming if m.venue_id is not None})
    counts.update(geocode_venues(session, om, venue_ids, now))
    venues = {
        v.venue_id: v
        for part in normalize.chunks(venue_ids)
        for v in session.scalars(select(V).where(V.venue_id.in_(part)))
    }
    latest: dict[int, tuple[float, datetime]] = {}
    ids = [m.match_id for m in upcoming]
    for part in normalize.chunks(ids):
        newest = (
            select(W.match_id, func.max(W.observed_at).label("at"))
            .where(W.match_id.in_(part))
            .group_by(W.match_id)
            .subquery()
        )
        for row in session.scalars(
            select(W).join(
                newest, (W.match_id == newest.c.match_id) & (W.observed_at == newest.c.at)
            )
        ):
            latest[row.match_id] = (row.lead_hours, _utc(row.kickoff_time))
    due: dict[int, list[tuple[int, datetime]]] = {}
    for m in upcoming:
        v = venues.get(m.venue_id or -1)
        if v is None or v.geocode_status not in USABLE or v.latitude is None:
            counts["no_coordinates"] += 1
            continue
        kickoff = _utc(m.kickoff_time)
        b = bucket((kickoff - now).total_seconds() / 3600)
        last = latest.get(m.match_id)
        if last is not None and last[1] == kickoff and bucket(last[0]) == b:
            continue
        due.setdefault(v.venue_id, []).append((m.match_id, kickoff))
    for n, (venue_id, matches) in enumerate(sorted(due.items())):
        if n >= max_forecasts:
            counts["forecast_deferred"] += len(due) - n
            break
        v = venues[venue_id]
        assert v.latitude is not None and v.longitude is not None
        payload: dict[str, Any] = om.forecast(session, v.latitude, v.longitude)
        observed = om.last_response_at or (clock() if clock else now)
        for match_id, kickoff in matches:
            if observed >= kickoff:
                continue
            w = match_weather(payload, kickoff)
            if w is None:
                counts["beyond_forecast"] += 1
                continue
            session.add(
                W(
                    match_id=match_id, venue_id=venue_id, source_id=SOURCE_ID,
                    observed_at=observed, kickoff_time=kickoff,
                    lead_hours=round((kickoff - observed).total_seconds() / 3600, 3),
                    latitude=v.latitude, longitude=v.longitude, temperature_c=w.temperature_c,
                    precipitation_mm=w.precipitation_mm, wind_speed_kmh=w.wind_speed_kmh,
                    hourly=w.hourly, storage_key=om.last_storage_key or "",
                )
            )  # fmt: skip
            counts["snapshots"] += 1
        session.commit()  # a later failure must not lose this venue's forecasts
    counts["requests"] = om.requests_made
    return +counts
