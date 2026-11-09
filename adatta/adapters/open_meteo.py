"""Open-Meteo adapter: geocoding (venue coordinates, elevation) and weather forecasts.

The ONLY module that knows Open-Meteo's response shape. Free, keyless, non-commercial API
(D24: personal use). Every response is kept in the raw store (principle 4) and indexed in
raw_payload with source_id "open_meteo".
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import retry, stop_after_attempt, wait_exponential

from adatta.adapters.normalize import name_key
from adatta.adapters.storage import RawStore, encode_payload, storage_key
from adatta.db import models

SOURCE_ID = "open_meteo"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
# The free tier counts a request as one call per 14 days of data per location (fewer than 10
# variables), and allows 10,000 calls a day and 5,000 an hour.
DAYS_PER_CALL = 14
FORECAST_DAYS = 16
HOURLY = ("temperature_2m", "precipitation", "wind_speed_10m")

# Countries a club in each competition country can play at home in (Monaco in Ligue 1,
# Andorra in Segunda, San Marino in Serie B/C, Welsh clubs in the English pyramid).
COUNTRY_CODES: dict[str, tuple[str, ...]] = {
    "England": ("GB",),
    "Spain": ("ES", "AD"),
    "Italy": ("IT", "SM"),
    "Germany": ("DE",),
    "France": ("FR", "MC"),
}
# Zone for a reviewed venue with no geocoder result (all bundle grounds outside the Canary
# Islands; a Canary venue's override must name Atlantic/Canary).
DEFAULT_TIMEZONE: dict[str, str] = {
    "England": "Europe/London",
    "Spain": "Europe/Madrid",
    "Italy": "Europe/Rome",
    "Germany": "Europe/Berlin",
    "France": "Europe/Paris",
}
# The geocoder indexes English and official names; the provider uses local ones.
CITY_ALIASES: dict[str, str] = {
    "Caerdydd": "Cardiff",
    "Iruñea": "Pamplona",
    "La Coruña": "A Coruña",
    "Napoli": "Naples",
    "Roma": "Rome",
    "Milano": "Milan",
    "Torino": "Turin",
    "Firenze": "Florence",
    "Genova": "Genoa",
    "Venezia": "Venice",
    "Bozen": "Bolzano",
    "Mantova": "Mantua",
    "Padova": "Padua",
    "München": "Munich",
    "Köln": "Cologne",
    "Nürnberg": "Nuremberg",
    "Tenerife": "Santa Cruz de Tenerife",
    "Donostia-San Sebastián": "Donostia",
    "Saint Etienne": "Saint-Étienne",
    "Newcastle": "Newcastle upon Tyne",
    "Dunkerque": "Dunkirk",
    "Palma de Mallorca": "Palma",
    "San Sebastian": "Donostia",
    "Amorebieta-Etxano": "Amorebieta",
    "Azcoitia": "Azkoitia",
    "Paris - Saint-Ouen": "Saint-Ouen",
    "Castellón de la Plana": "Castellón",
    "Saragossa": "Zaragoza",
    "Villarreal": "Vila-real",
    "Freiburg": "Freiburg im Breisgau",
    "La Coruna": "A Coruña",
    "Wuerzburg": "Würzburg",
    "Hamburg - St. Pauli": "Hamburg",
}
CLEAR_MARGIN = 10.0  # chosen place must be this many times more populous than the runner-up
CITY_MIN = 50_000  # ... or, when the runner-up's population is unknown, at least this large


@dataclass(frozen=True)
class Place:
    name: str
    country_code: str
    latitude: float
    longitude: float
    elevation_m: float | None
    population: int | None
    feature_code: str
    timezone: str | None = None  # IANA name, as the geocoder gives it


@dataclass(frozen=True)
class Geocode:
    status: str  # auto | ambiguous | unresolved
    place: Place | None
    query: str
    n_candidates: int

    def describe(self) -> str:
        if self.place is None:
            return f"{self.query}: no populated place in country"
        p = self.place
        return (
            f"{self.query} -> {p.name}, {p.country_code} ({p.feature_code}, pop "
            f"{p.population}); {self.n_candidates} candidate(s)"
        )


def city_query(city: str) -> str:
    """'Barnsley, South Yorkshire' -> 'Barnsley'; local names -> the geocoder's names. A ground
    listed with no city often carries it in brackets: 'Wembley Stadium (London)' -> 'London'."""
    bracket = re.search(r"\(([^()]+)\)\s*$", city)
    head = (bracket.group(1) if bracket else city).split(",")[0].strip()
    return CITY_ALIASES.get(head, head)


def _place(r: dict[str, Any]) -> Place:
    elev = r.get("elevation")
    return Place(
        name=str(r.get("name")),
        country_code=str(r.get("country_code")),
        latitude=float(r["latitude"]),
        longitude=float(r["longitude"]),
        elevation_m=None if elev is None or float(elev) <= -9000 else float(elev),
        population=r.get("population"),
        feature_code=str(r.get("feature_code") or ""),
        timezone=r.get("timezone"),
    )


def pick_place(results: list[dict[str, Any]], countries: tuple[str, ...], query: str) -> Geocode:
    """Choose the most populous populated place (feature code PPL*, not a city district PPLX)
    in the allowed countries.

    `auto` only when it is the sole candidate or clearly larger than the next one; otherwise
    `ambiguous` (two towns of one name, as with Lezama), kept for manual review. When the best
    place has exactly the queried name, only places of that same name count as rivals (the
    search also returns prefix matches, e.g. Cesenatico for Cesena)."""
    cands = [
        _place(r)
        for r in results
        if str(r.get("feature_code") or "").startswith("PPL")
        and r.get("feature_code") != "PPLX"
        and (not countries or r.get("country_code") in countries)
    ]
    if not cands:
        return Geocode("unresolved", None, query, 0)
    cands.sort(key=lambda p: p.population or 0, reverse=True)
    best = cands[0]
    if name_key(best.name) == name_key(query):
        cands = [p for p in cands if name_key(p.name) == name_key(query)]
    if len(cands) == 1:
        return Geocode("auto", best, query, 1)
    pop, runner = best.population or 0, cands[1].population
    # A runner-up of unknown size is usually a hamlet, but only a city is safely larger.
    clear = pop >= CITY_MIN if runner is None else pop >= CLEAR_MARGIN * max(runner, 1)
    return Geocode("auto" if clear else "ambiguous", best, query, len(cands))


def _hour(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class MatchWeather:
    temperature_c: float | None
    precipitation_mm: float | None
    wind_speed_kmh: float | None
    hourly: dict[str, Any]


def match_weather(payload: dict[str, Any], kickoff: datetime) -> MatchWeather | None:
    """Summarise a forecast over a match: temperature and wind are the mean of the hourly
    values at kickoff hour and the two hours after it; precipitation (each value is the sum
    over the preceding hour) is the sum of the two hours ending at kickoff hour + 2 h.
    None when the forecast does not reach the kickoff."""
    h = payload.get("hourly") or {}
    times = [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in h.get("time") or []]
    index = {t: i for i, t in enumerate(times)}
    k = _hour(kickoff)
    mid = [index.get(k + timedelta(hours=d)) for d in (0, 1, 2)]
    if any(i is None for i in mid):
        return None

    def vals(var: str, idx: list[int | None]) -> list[float]:
        series = h.get(var) or []
        return [float(series[i]) for i in idx if i is not None and series[i] is not None]

    def mean(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 2) if xs else None

    rain = vals("precipitation", mid[1:])
    window = [index.get(k + timedelta(hours=d)) for d in range(-1, 4)]
    hourly = {
        "time": [times[i].isoformat() for i in window if i is not None],
        **{var: [(h.get(var) or [])[i] for i in window if i is not None] for var in HOURLY},
    }
    return MatchWeather(
        temperature_c=mean(vals("temperature_2m", mid)),
        precipitation_mm=round(sum(rain), 2) if rain else None,
        wind_speed_kmh=mean(vals("wind_speed_10m", mid)),
        hourly=hourly,
    )


def ensure_source(session: Session) -> None:
    if session.get(models.Source, SOURCE_ID) is None:
        session.add(models.Source(source_id=SOURCE_ID, description="Open-Meteo (free API)"))
        session.flush()


class OpenMeteoClient:
    def __init__(
        self,
        store: RawStore,
        http: httpx.Client | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._clock = clock
        self._http = http or httpx.Client(timeout=30)
        self.requests_made = 0
        self.last_storage_key: str | None = None
        self.last_response_at: datetime | None = None
        self._geocodes: dict[tuple[str, str], Geocode] = {}  # one search per city per run

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        resp = self._http.get(url, params=params)
        resp.raise_for_status()
        return resp

    def fetch(self, session: Session, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET, keep the raw response (object store + raw_payload row), return parsed JSON."""
        self.requests_made += 1
        resp = self._get(url, params)
        self.last_response_at = self._clock()
        payload: dict[str, Any] = resp.json()
        blob, digest = encode_payload(payload)
        endpoint = httpx.URL(url).path
        key = storage_key(SOURCE_ID, endpoint, self.last_response_at.strftime("%Y-%m-%d"), digest)
        self.last_storage_key = key
        R = models.RawPayload
        if session.scalar(select(R.payload_id).where(R.storage_key == key)) is None:
            ensure_source(session)
            self._store.put(key, blob)
            session.add(R(source_id=SOURCE_ID, endpoint=endpoint, params=params,
                          storage_key=key, content_hash=digest))  # fmt: skip
            session.flush()
        return payload

    def geocode(self, session: Session, city: str, country: str) -> Geocode:
        query = city_query(city)
        cached = self._geocodes.get((query, country))
        if cached is not None:
            return cached
        countries = COUNTRY_CODES.get(country, ())
        params: dict[str, Any] = {"name": query, "count": 10, "language": "en", "format": "json"}
        results: list[dict[str, Any]] = []
        for code in countries or (None,):
            if code is not None:
                params = {**params, "countryCode": code}
            results += self.fetch(session, GEOCODE_URL, params).get("results") or []
        self._geocodes[(query, country)] = pick_place(results, countries, query)
        return self._geocodes[(query, country)]

    def elevation(self, session: Session, latitude: float, longitude: float) -> float | None:
        """Ground elevation (90 m DEM) where the geocoder gives none."""
        params = {"latitude": round(latitude, 4), "longitude": round(longitude, 4)}
        values = self.fetch(session, ELEVATION_URL, params).get("elevation") or []
        return float(values[0]) if values and values[0] is not None else None

    def archive(
        self, session: Session, latitude: float, longitude: float, start: str, end: str,
        elevation: float | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """ERA5 reanalysis, hourly, UTC, for one point, temperature corrected to `elevation`
        (default: the 90 m terrain height there). Climatology only: realised weather is never
        a pre-match feature."""
        params: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start,
            "end_date": end,
            "hourly": ",".join(HOURLY),
            "models": "era5",
            "timezone": "UTC",
        }
        if elevation is not None:
            params["elevation"] = elevation
        return self.fetch(session, ARCHIVE_URL, params)

    def forecast(self, session: Session, latitude: float, longitude: float) -> dict[str, Any]:
        params = {
            "latitude": round(latitude, 4),
            "longitude": round(longitude, 4),
            "hourly": ",".join(HOURLY),
            "forecast_days": FORECAST_DAYS,
            "timezone": "UTC",
        }
        return self.fetch(session, FORECAST_URL, params)
