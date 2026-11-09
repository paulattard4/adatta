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
from adatta.adapters.open_meteo import SOURCE_ID, OpenMeteoClient, match_weather
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
            new = (o.latitude, o.longitude, o.elevation_m)
            if v.geocode_status == "reviewed" and (v.latitude, v.longitude, v.elevation_m) == new:
                continue
            v.latitude, v.longitude, v.elevation_m = new
            v.geocode_status, v.geocode_source = "reviewed", "config/venue_overrides.yml"
            v.geocode_match, v.geocoded_at = (o.note or "manual review")[:255], now
            n += 1
    session.flush()
    return n


def geocode_venues(
    session: Session, om: OpenMeteoClient, venue_ids: list[int], now: datetime,
    limit: int = MAX_GEOCODES,
) -> Counter[str]:  # fmt: skip
    """Geocode pending venues by city; record the outcome either way (never retried blindly:
    ambiguous and unresolved venues need a reviewed override)."""
    V = models.Venue
    counts: Counter[str] = Counter()
    pending = [
        v
        for part in normalize.chunks(venue_ids)
        for v in session.scalars(
            select(V).where(V.venue_id.in_(part), V.geocode_status == "pending")
        )
    ]
    for v in pending[:limit]:
        g = om.geocode(session, v.city or v.name, v.country)
        v.geocode_status, v.geocode_source = g.status, SOURCE_ID
        v.geocode_match, v.geocoded_at = g.describe()[:255], om.last_response_at or now
        if g.place is not None:
            v.latitude, v.longitude = g.place.latitude, g.place.longitude
            v.elevation_m = g.place.elevation_m
        counts[g.status] += 1
        session.commit()
    counts["deferred"] += max(0, len(pending) - limit)
    return counts


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
