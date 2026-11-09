"""Venue geocoding and weather-forecast capture against a fake Open-Meteo."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters import open_meteo as om
from adatta.adapters.storage import LocalStore
from adatta.capture import weather
from adatta.db import models
from adatta.db.models import Base

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
K = datetime(2026, 11, 8, 15, 30, tzinfo=UTC)  # 7 days 6.5 h after T0


def place(
    name: str, cc: str, pop: int | None, lat: float = 50.0, code: str = "PPL"
) -> dict[str, Any]:
    return {"name": name, "country_code": cc, "population": pop, "latitude": lat,
            "longitude": 1.0, "elevation": 120.0, "feature_code": code}  # fmt: skip


def test_pick_place_prefers_a_clearly_larger_town_in_country() -> None:
    rome = [place("Roma", "IT", 87), place("Rome", "IT", 2_318_895, 41.9, "PPLC")]
    g = om.pick_place(rome, ("IT",), "Rome")
    assert g.status == "auto" and g.place is not None and g.place.latitude == 41.9
    lezama = [place("Lezama", "ES", 2261), place("Lezama", "ES", None, 43.27)]
    assert om.pick_place(lezama, ("ES",), "Lezama").status == "ambiguous"
    island = [place("Tenerife", "ES", 906_854, code="ISL"), place("Paris", "FR", 2_000_000)]
    assert om.pick_place(island, ("ES",), "Tenerife").status == "unresolved"
    cesena = [place("Cesena", "IT", 97_137), place("Cesenatico", "IT", 25_000),
              place("Cesena 3", "IT", None, code="PPLX")]  # fmt: skip
    assert om.pick_place(cesena, ("IT",), "Cesena").status == "auto"  # prefix match, district
    assert om.city_query("Barnsley, South Yorkshire") == "Barnsley"
    assert om.city_query("Napoli") == "Naples"


def hourly(start: datetime, hours: int) -> dict[str, Any]:
    times = [start + timedelta(hours=i) for i in range(hours)]
    return {"hourly": {
        "time": [t.strftime("%Y-%m-%dT%H:%M") for t in times],
        "temperature_2m": [float(i) for i in range(hours)],
        "precipitation": [1.0] * hours,
        "wind_speed_10m": [10.0] * hours,
    }}  # fmt: skip


def test_match_weather_summarises_the_match_hours() -> None:
    payload = hourly(datetime(2026, 11, 8, 0, tzinfo=UTC), 48)
    w = om.match_weather(payload, K)  # kickoff hour 15:00 -> index 15
    assert w is not None
    assert w.temperature_c == 16.0  # mean of hours 15, 16, 17
    assert w.precipitation_mm == 2.0  # hours ending 16:00 and 17:00
    assert w.wind_speed_kmh == 10.0 and len(w.hourly["time"]) == 5
    assert om.match_weather(payload, K + timedelta(days=2)) is None


def test_buckets() -> None:
    assert weather.bucket(400) is None and weather.bucket(200) == 360
    assert weather.bucket(30) == 72 and weather.bucket(1.5) == 2


class FakeMeteo:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.places: dict[str, list[dict[str, Any]]] = {
            "Leeds": [place("Leeds", "GB", 455_123, 53.8)],
            # two towns of one name (the harness league is English, so the country is GB)
            "Lezama": [place("Lezama", "GB", 2261), place("Lezama", "GB", None, 43.27)],
        }

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req.url.path)
        if req.url.path == "/v1/search":
            return httpx.Response(
                200, json={"results": self.places.get(req.url.params["name"], [])}
            )
        return httpx.Response(200, json=hourly(datetime(2026, 11, 1, 0, tzinfo=UTC), 16 * 24))


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.api = FakeMeteo()
        self.store = LocalStore(tmp_path)
        with Session(self.engine) as s:
            normalize.ensure_source(s)
            comp = normalize.ensure_competition(s, league_id=1, name="L", country="England",
                                                level="senior", tier="1")  # fmt: skip
            season = normalize.ensure_season(s, comp.competition_id, 2026)
            fixtures = [self.fixture(1, "Elland Road", "Leeds", K),
                        self.fixture(2, "Lezama", "Lezama", K)]  # fmt: skip
            normalize.ingest_fixtures(s, season, fixtures, "England")
            s.commit()

    @staticmethod
    def fixture(fid: int, venue: str, city: str, kickoff: datetime) -> dict[str, Any]:
        return {"fixture": {"id": fid, "date": kickoff.isoformat(), "status": {"short": "NS"},
                            "venue": {"id": None, "name": venue, "city": city}},
                "league": {"round": "Regular Season - 1"},
                "teams": {"home": {"id": 10 + fid, "name": "H"},
                          "away": {"id": 20 + fid, "name": "A"}},
                "score": {}}  # fmt: skip

    def tick(self, now: datetime, overrides: list[weather.VenueOverride] | None = None) -> Any:
        http = httpx.Client(transport=httpx.MockTransport(self.api.handler))
        client = om.OpenMeteoClient(self.store, http=http, clock=lambda: now)
        with Session(self.engine) as s:
            return weather.capture_weather(s, client, now, overrides=overrides or [])

    def snapshots(self) -> list[models.WeatherSnapshot]:
        with Session(self.engine) as s:
            return list(
                s.scalars(
                    select(models.WeatherSnapshot).order_by(models.WeatherSnapshot.snapshot_id)
                )
            )


def test_capture_geocodes_once_and_snapshots_per_milestone(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = h.tick(T0)
    assert first["auto"] == 1 and first["ambiguous"] == 1  # Lezama waits for review
    assert first["snapshots"] == 1 and first["no_coordinates"] == 1
    snap = h.snapshots()[0]
    assert snap.lead_hours == 174.5 and snap.temperature_c is not None and snap.storage_key
    assert h.tick(T0 + timedelta(hours=1)).get("snapshots", 0) == 0  # same bucket (360 h)
    assert h.api.calls.count("/v1/search") == 2  # geocoded once each, not every tick
    assert h.tick(T0 + timedelta(hours=7))["snapshots"] == 1  # 167.5 h: the 168 h bucket
    with Session(h.engine) as s:
        assert (s.scalar(select(func.count()).select_from(models.RawPayload)) or 0) >= 3
        venue = s.scalar(select(models.Venue).where(models.Venue.city == "Leeds"))
        assert venue is not None and venue.latitude == 53.8 and venue.elevation_m == 120.0


def test_override_unblocks_an_ambiguous_venue(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.tick(T0)
    fix = weather.VenueOverride(
        name="Lezama", city="Lezama", latitude=43.27, longitude=-2.83, elevation_m=93,
        note="Athletic training ground, Bizkaia",
    )  # fmt: skip
    out = h.tick(T0 + timedelta(hours=1), overrides=[fix])
    assert out["overrides"] == 1 and out["snapshots"] == 1
    assert h.snapshots()[-1].latitude == 43.27


def test_moved_kickoff_triggers_a_new_snapshot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.tick(T0)
    with Session(h.engine) as s:
        m = s.scalar(select(models.Match).where(models.Match.source_ref == "1"))
        assert m is not None
        m.kickoff_time = K + timedelta(hours=2)
        s.commit()
    assert h.tick(T0 + timedelta(hours=1))["snapshots"] == 1
    assert h.snapshots()[-1].kickoff_time.replace(tzinfo=UTC) == K + timedelta(hours=2)


def test_load_venues_links_matches_from_stored_fixture_lists(tmp_path: Path) -> None:
    from adatta.adapters.detail_store import load_venues
    from adatta.adapters.storage import encode_payload

    h = Harness(tmp_path)
    moved = Harness.fixture(1, "New Ground", "Leeds", K)
    blob, digest = encode_payload({"response": [moved]})
    h.store.put("k1", blob)
    with Session(h.engine) as s:
        s.add(models.RawPayload(source_id="api_football", endpoint="/fixtures",
                                params={"league": 1, "season": 2026}, storage_key="k1",
                                content_hash=digest))  # fmt: skip
        s.commit()
        counts = load_venues(s, h.store, {1: "England"}, None)
        assert counts["linked"] == 1 and "unknown_match" not in counts
        m = s.scalar(select(models.Match).where(models.Match.source_ref == "1"))
        v = s.get(models.Venue, m.venue_id if m else -1)
        assert v is not None and v.name == "New Ground" and v.geocode_status == "pending"
        assert load_venues(s, h.store, {1: "England"}, None).get("linked", 0) == 0  # idempotent
