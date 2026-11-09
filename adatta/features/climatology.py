"""Weather climatology for a venue and kickoff (spec §6.2; register 2.13).

The weather fallback for every match without an as-issued forecast (all of 2019 to Feb 2024,
and anything more than 16 days out): what the weather usually is at that place, time of
year and time of day. Built from ERA5 reanalysis (Open-Meteo archive) over a fixed baseline,
2016-2018, entirely before the modelled seasons (2019 on), so it uses only years before every
season it is applied to: no leakage. Realised weather on the day is never used.

Reanalysis is on a 0.25-degree grid whose cells can sit far above a coastal town (the cell
for Las Palmas is centred at 668 m). So each request is for the venue's own point (venue
coordinates rounded to 0.01 degree, which venues of one geocoded city share) with its
elevation, and Open-Meteo corrects temperature to that height. One request per point.

  match value = mean over the baseline years of the hours within +/- WINDOW_DAYS of the
  kickoff's calendar day and from the kickoff hour to two hours after it (UTC), matching the
  forecast summary in adatta/adapters/open_meteo.match_weather: temperature and wind as
  means, precipitation as the mean total over the two hours after kickoff.
"""

from __future__ import annotations

import gzip
import json
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters.open_meteo import DAYS_PER_CALL, OpenMeteoClient
from adatta.adapters.storage import RawStore
from adatta.db import models

BASELINE = (date(2016, 1, 1), date(2018, 12, 31))
BASELINE_LABEL = "era5_2016_2018"
WINDOW_DAYS = 15
USABLE = ("auto", "reviewed")
CALLS_PER_CELL = -(-((BASELINE[1] - BASELINE[0]).days + 1) // DAYS_PER_CALL)  # 79
HOURLY_CALL_LIMIT = 5000


def cell(lat: float, lon: float) -> tuple[float, float]:
    """The point a venue's climatology is fetched for."""
    return (round(lat, 2), round(lon, 2))


def needed_cells(
    session: Session, venue_ids: Iterable[int]
) -> dict[tuple[float, float], float | None]:
    """{point: elevation} for the usable venues given."""
    V = models.Venue
    out: dict[tuple[float, float], float | None] = {}
    for part in normalize.chunks(sorted(set(venue_ids))):
        for v in session.scalars(select(V).where(V.venue_id.in_(part))):
            if v.geocode_status in USABLE and v.latitude is not None and v.longitude is not None:
                key = cell(v.latitude, v.longitude)
                if out.get(key) is None:
                    out[key] = v.elevation_m
    return out


def fetched_cells(session: Session) -> dict[tuple[float, float], str]:
    """{cell: storage key} for baseline archive payloads already in the raw store."""
    R = models.RawPayload
    out: dict[tuple[float, float], str] = {}
    for params, key in session.execute(
        select(R.params, R.storage_key).where(R.endpoint == "/v1/archive")
    ):
        if params.get("start_date") == BASELINE[0].isoformat():
            out[(float(params["latitude"]), float(params["longitude"]))] = key
    return out


def fetch_baseline(
    session: Session, om: OpenMeteoClient, cells: dict[tuple[float, float], float | None],
    budget_calls: int, sleep: Callable[[float], None] = time.sleep,
    calls_per_hour: int = 4000,
) -> Counter[str]:  # fmt: skip
    """Fetch the baseline for cells not yet stored, until `budget_calls` (counted as the free
    tier counts them) would be exceeded. Paced under the hourly limit; stops cleanly on a
    rate-limit response so the next run resumes."""
    have = fetched_cells(session)
    todo = [c for c in sorted(cells) if c not in have]
    counts: Counter[str] = Counter(already=len(have), todo=len(todo))
    pause = 3600 * CALLS_PER_CELL / calls_per_hour
    for i, (lat, lon) in enumerate(todo):
        if counts["calls"] + CALLS_PER_CELL > budget_calls:
            counts["left_for_later"] = len(todo) - i
            break
        if i:
            sleep(pause)
        try:
            om.archive(
                session, lat, lon, BASELINE[0].isoformat(), BASELINE[1].isoformat(),
                elevation=cells[(lat, lon)],
            )  # fmt: skip
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                counts["rate_limited"] += 1
                counts["left_for_later"] = len(todo) - i
                break
            raise
        session.commit()
        counts["fetched"] += 1
        counts["calls"] += CALLS_PER_CELL
    return counts


@dataclass(frozen=True)
class CellClimate:
    """Hourly baseline series for one cell, indexed by (day of year, hour)."""

    by_slot: dict[tuple[int, int], list[tuple[float | None, float | None, float | None]]]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CellClimate:
        h = payload["hourly"]
        assert isinstance(h, dict)
        slots: dict[tuple[int, int], list[tuple[float | None, float | None, float | None]]] = {}
        for t, temp, rain, wind in zip(
            h["time"], h["temperature_2m"], h["precipitation"], h["wind_speed_10m"], strict=True
        ):
            at = datetime.fromisoformat(t)
            slots.setdefault((_doy(at.date()), at.hour), []).append((temp, rain, wind))
        return cls(slots)

    def at(self, kickoff: datetime) -> tuple[float | None, float | None, float | None, int]:
        """(temperature_c, precipitation_mm, wind_speed_kmh, n_years) for a kickoff."""
        k = kickoff.astimezone(UTC)
        temps: list[float] = []
        winds: list[float] = []
        rains: list[float] = []
        for d in range(-WINDOW_DAYS, WINDOW_DAYS + 1):
            for step in (0, 1, 2):
                t = k.replace(minute=0, second=0, microsecond=0) + timedelta(days=d, hours=step)
                for temp, rain, wind in self.by_slot.get((_doy(t.date()), t.hour), []):
                    if temp is not None:
                        temps.append(temp)
                    if wind is not None:
                        winds.append(wind)
                    if step and rain is not None:
                        rains.append(rain)
        n_days = 2 * WINDOW_DAYS + 1
        years = round(len(temps) / (3 * n_days)) if temps else 0
        return (
            round(sum(temps) / len(temps), 2) if temps else None,
            # two hourly values per match: their mean times two is the mean match total
            round(2 * sum(rains) / len(rains), 2) if rains else None,
            round(sum(winds) / len(winds), 2) if winds else None,
            years,
        )


def _doy(d: date) -> int:
    """Day of year on a 366-day calendar (29 Feb is its own day; 1 Mar is always 61)."""
    leap = d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)
    n = d.timetuple().tm_yday
    return n + 1 if not leap and n >= 60 else n


def load_cells(
    session: Session, store: RawStore, cells: Iterable[tuple[float, float]]
) -> dict[tuple[float, float], CellClimate]:
    have = fetched_cells(session)
    return {
        c: CellClimate.from_payload(json.loads(gzip.decompress(store.get(have[c]))))
        for c in set(cells)
        if c in have
    }


CLIMATE_SCHEMA = {
    "match_id": pl.Int64,
    "venue_id": pl.Int64,
    "clim_temperature_c": pl.Float64,
    "clim_precipitation_mm": pl.Float64,
    "clim_wind_speed_kmh": pl.Float64,
    "clim_years": pl.Int64,
}


def match_climatology(
    session: Session, matches: Sequence[models.Match], venue_of: dict[int, int | None],
    climates: dict[tuple[float, float], CellClimate],
) -> pl.DataFrame:  # fmt: skip
    """One row per match: the baseline climatology at its venue (`venue_of`: match_id ->
    venue_id, e.g. travel_features' venue_id, which falls back to the home base). Null where
    the venue is unplaced or its cell is not fetched yet."""
    V = models.Venue
    ids = sorted({v for v in venue_of.values() if v is not None})
    venues = {
        v.venue_id: v
        for part in normalize.chunks(ids)
        for v in session.scalars(select(V).where(V.venue_id.in_(part)))
    }
    rows: list[dict[str, object]] = []
    for m in matches:
        vid = venue_of.get(m.match_id)
        v = venues.get(vid) if vid is not None else None
        values: tuple[float | None, float | None, float | None, int | None] = (
            None, None, None, None,
        )  # fmt: skip
        if v is not None and v.geocode_status in USABLE and v.latitude is not None:
            assert v.longitude is not None
            climate = climates.get(cell(v.latitude, v.longitude))
            if climate is not None:
                ko = m.kickoff_time if m.kickoff_time.tzinfo else m.kickoff_time.replace(tzinfo=UTC)
                values = climate.at(ko)
        rows.append(
            {"match_id": m.match_id, "venue_id": vid, "clim_temperature_c": values[0],
             "clim_precipitation_mm": values[1], "clim_wind_speed_kmh": values[2],
             "clim_years": values[3]}
        )  # fmt: skip
    return pl.DataFrame(rows, schema=CLIMATE_SCHEMA)
