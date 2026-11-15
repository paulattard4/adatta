"""Host role of national-team matches (adatta/features/host.py; BEYOND_LEAGUES_PLAN host
flag acceptance (a)-(c)). Synthetic data, an in-memory database and a fake client."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters.api_football import ProviderVenue, parse_venues
from adatta.adapters.open_meteo import Place
from adatta.adapters.storage import LocalStore, encode_payload
from adatta.db import models
from adatta.db.models import Base
from adatta.features import host

T0 = datetime(2024, 3, 22, 19, 45, tzinfo=UTC)
PROVIDER = ["England", "Wales", "Scotland", "South-Korea", "Costa-Rica", "Brazil", "Qatar",
            "Russia", "USA", "France", "World"]  # fmt: skip
ALIASES = {"Korea Republic": "South-Korea", "Republic of Ireland": "Ireland"}


def test_team_country_strips_suffixes_and_uses_aliases() -> None:
    c = host.Countries(PROVIDER, ALIASES)
    assert c.of("England") == "England" and c.of("England U21") == "England"
    assert c.of("Wales U-19") == "Wales" and c.of("Brazil W") == "Brazil"
    assert c.of("Costa Rica") == "Costa-Rica"  # punctuation and spaces ignored
    assert c.of("Korea Republic") == "South-Korea"
    assert c.of("Republic of Ireland") is None  # alias to a name the provider lacks
    assert c.of("Los Angeles FC") is None and c.of("Bahamas") is None
    assert c.of("World") is None


def test_role_needs_only_the_matching_sides_country() -> None:
    assert host.role("England", "England", "Wales") == "home"
    assert host.role("Wales", "England", "Wales") == "swapped"
    assert host.role("Qatar", "England", "Wales") == "neutral"
    assert host.role("Brazil", "Brazil", None) == "home"  # away side unlisted: still home
    assert host.role("Brazil", None, "Brazil") == "swapped"
    assert host.role("Qatar", "England", None) == "unknown"  # could be the unlisted side's
    assert host.role(None, "England", "Wales") == "unknown"


def test_parse_venues_keeps_the_venues_own_country() -> None:
    payload = {"response": [
        {"id": 7, "name": "Cardiff City Stadium", "city": "Cardiff", "country": "Wales"},
        {"id": None, "name": "No id", "city": "X", "country": "Wales"},
        {"id": 8, "name": "No country", "city": "Y", "country": None},
    ]}  # fmt: skip
    assert parse_venues(payload) == [ProviderVenue("7", "Cardiff City Stadium", "Cardiff",
                                                   "Wales")]  # fmt: skip


class World:
    """One national competition, a club competition, venues with and without provider ids."""

    def __init__(self, tmp: Path) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.s = Session(engine)
        normalize.ensure_source(self.s)
        nat = normalize.ensure_competition(self.s, league_id=10, name="Friendlies",
                                           country="World", level="senior", tier="national",
                                           is_national=True)  # fmt: skip
        self.nat = normalize.ensure_season(self.s, nat.competition_id, 2024).season_id
        self.team: dict[str, int] = {}
        for n in ("England", "Wales", "Korea Republic", "Brazil", "Bahamas", "Los Angeles FC"):
            t = models.Team(source_id=normalize.SOURCE_ID, source_ref=n, name=n, country=n,
                            is_national=True)  # fmt: skip
            self.s.add(t)
            self.s.flush()
            self.team[n] = t.team_id
        self.venue: dict[str, int] = {}
        for key, ref, name, city, comp_country in (
            ("wembley", "489", "Wembley Stadium", "London", "England"),
            ("cardiff", "7", "Cardiff City Stadium", "Cardiff", "England"),  # Championship
            ("cardiff_alias", None, "Cardiff City Stadium.", "Cardiff", "World"),  # no id
            ("lusail", "900", "Lusail Stadium", "Lusail", "World"),
            ("nowhere", None, "Unlisted Ground", "Nassau", "World"),
        ):
            v = models.Venue(source_id=normalize.SOURCE_ID, source_ref=ref, name=name, city=city,
                             country=comp_country, observed_at=T0)  # fmt: skip
            self.s.add(v)
            self.s.flush()
            self.venue[key] = v.venue_id
        self.n = 0
        self.matches: dict[str, models.Match] = {}
        for key, home, away, venue in (
            ("eng_home", "England", "Wales", "wembley"),
            ("wal_home", "Wales", "England", "cardiff"),
            ("wal_home_noid", "Wales", "Korea Republic", "cardiff_alias"),
            ("listed_away_hosts", "England", "Wales", "cardiff"),
            ("neutral", "England", "Korea Republic", "lusail"),
            ("unlisted_side", "Brazil", "Bahamas", "lusail"),
            ("unlisted_home", "Brazil", "Bahamas", "nowhere"),
            ("club", "Los Angeles FC", "Korea Republic", "lusail"),
            ("no_venue", "England", "Wales", None),
        ):
            self.n += 1
            m = models.Match(source_id=normalize.SOURCE_ID, source_ref=str(self.n),
                             season_id=self.nat, home_team_id=self.team[home],
                             away_team_id=self.team[away], kickoff_time=T0, status="FT",
                             venue_id=None if venue is None else self.venue[venue],
                             neutral=key == "eng_home")  # a stale flag to correct  # fmt: skip
            self.s.add(m)
            self.s.flush()
            self.matches[key] = m
        self.s.commit()
        self.store = LocalStore(tmp)


class FakeClient:
    def __init__(self, s: Session, store: LocalStore,
                 listed: dict[str, list[dict[str, Any]]]) -> None:  # fmt: skip
        self.s, self.store, self.listed, self.calls = s, store, listed, []  # type: ignore[var-annotated]

    def _stored(self, endpoint: str, params: dict[str, Any], payload: dict[str, Any]) -> None:
        blob, digest = encode_payload(payload)
        key = f"api_football{endpoint}/{digest}.json.gz"
        self.store.put(key, blob)
        row = models.RawPayload(source_id=normalize.SOURCE_ID, endpoint=endpoint,
                                params=params, storage_key=key, content_hash=digest)  # fmt: skip
        self.s.add(row)
        self.s.flush()

    def countries(self, session: Session) -> list[str]:
        self._stored("/countries", {}, {"response": [{"name": n} for n in PROVIDER]})
        return PROVIDER

    def venues(self, session: Session, country: str) -> list[ProviderVenue]:
        self.calls.append(country)
        # the provider echoes the request's parameters, so payloads differ per country
        payload = {"parameters": {"country": country}, "response": self.listed.get(country, [])}
        self._stored("/venues", {"country": country}, payload)
        return parse_venues(payload)


LISTED = {
    "England": [{"id": 489, "name": "Wembley Stadium", "city": "London", "country": "England"}],
    "Wales": [{"id": 7, "name": "Cardiff City Stadium", "city": "Cardiff", "country": "Wales"}],
    "Qatar": [
        {"id": 900, "name": "Lusail Stadium", "city": "Lusail", "country": "Qatar"},
        {
            "id": 901,
            "name": "Jassim Bin Hamad Stadium (Al-Sadd Stadium)",
            "city": "ad-Dōha (Doha)",
            "country": "Qatar",
        },
    ],
}


def test_bracketed_provider_names_match_the_fixture_names(tmp_path: Path) -> None:
    w = World(tmp_path)
    v = models.Venue(source_id=normalize.SOURCE_ID, name="Jassim Bin Hamad Stadium",
                     city="Doha", country="World", observed_at=T0)  # fmt: skip
    w.s.add(v)
    w.s.flush()
    assert host.variants("ad-Dōha (Doha)") == {"addoha", "doha", "addohadoha"}
    host.apply_countries(w.s, parse_venues({"response": LISTED["Qatar"]}))
    assert v.provider_country == "Qatar"


def test_fetch_is_resumable_and_roles_follow_the_venues_own_country(tmp_path: Path) -> None:
    w = World(tmp_path)
    client = FakeClient(w.s, w.store, LISTED)
    countries = host.Countries(client.countries(w.s), ALIASES)
    rows = host.national_matches(w.s)
    wanted = host.countries_needed({t for _, _, h, a in rows for t in (h, a)}, countries)
    assert wanted == ["Brazil", "England", "South-Korea", "Wales"]  # clubs, unlisted: none
    assert host.fetch_venues(w.s, client, wanted[:2]) == 2  # stopped part way
    assert host.fetch_venues(w.s, client, wanted + ["Qatar"]) == 3  # the rest only
    assert client.calls == ["Brazil", "England", "Qatar", "South-Korea", "Wales"]
    assert host.fetch_venues(w.s, client, wanted) == 0

    stored = host.Countries(host.stored_country_names(w.s, w.store), ALIASES)
    applied = host.apply_countries(w.s, host.stored_venues(w.s, w.store))
    assert applied["by_id"] == 3 and applied["by_name"] == 1
    v = {k: w.s.get_one(models.Venue, i) for k, i in w.venue.items()}
    assert v["cardiff"].provider_country == "Wales" and v["cardiff"].country == "England"
    assert v["cardiff_alias"].provider_country == "Wales"  # matched by name and city
    assert v["nowhere"].provider_country is None

    roles = host.host_roles(w.s, rows, stored)
    got = dict(zip(roles["match_id"].to_list(), roles["role"].to_list(), strict=True))
    want = {"eng_home": "home", "wal_home": "home", "wal_home_noid": "home",
            "listed_away_hosts": "swapped", "neutral": "neutral", "unlisted_side": "unknown",
            "unlisted_home": "unknown", "club": "unknown", "no_venue": "unknown"}  # fmt: skip
    assert {k: got[m.match_id] for k, m in w.matches.items()} == want
    assert host.write_flags(rows, roles) == 2  # eng_home cleared, neutral set
    assert [k for k, m in w.matches.items() if m.neutral] == ["neutral"]
    assert host.write_flags(rows, roles) == 0


def place(name: str, code: str, country: str, pop: int | None, admin1: str | None = None) -> Place:
    return Place(name, code, 0.0, 0.0, None, pop, "PPLA", None, country, admin1)


def test_place_country_is_set_only_when_the_city_is_in_one_country() -> None:
    c = host.Countries(PROVIDER, {"United States": "USA"})
    cardiff = [place("Cardiff", "GB", "United Kingdom", 447_000, "Wales")]
    assert host.place_country(cardiff, "Cardiff", c) == "Wales"  # UK places by region
    arlington = [place("Arlington", "US", "United States", 398_000, "Texas"),
                 place("Arlington", "US", "United States", 236_000, "Virginia")]  # fmt: skip
    assert host.place_country(arlington, "Arlington", c) == "USA"  # two towns, one country
    paris = [
        place("Paris", "FR", "France", 2_100_000),
        place("Paris", "US", "United States", 25_000),
    ]
    assert host.place_country(paris, "Paris", c) == "France"  # clearly the larger
    newport = [place("Newport", "GB", "United Kingdom", 145_000, "Wales"),
               place("Newport", "US", "United States", 25_000, "Rhode Island")]  # fmt: skip
    assert host.place_country(newport, "Newport", c) is None  # under 10x: not placed
    nassau = [place("Nassau", "BS", "Bahamas", 274_000)]
    assert host.place_country(nassau, "Nassau", c) is None  # no provider name
    # prefix matches don't compete with an exact name
    sochi = [place("Sochi", "RU", "Russia", 343_000), place("Sochinskiy", "UA", "Ukraine", 900_000)]
    assert host.place_country(sochi, "Sochi", c) == "Russia"
    assert host.place_country([], "Atlantis", c) is None


class FakePlaces:
    def __init__(self, known: dict[str, list[Place]]) -> None:
        self.known, self.calls = known, []  # type: ignore[var-annotated]

    def places(self, session: Session, city: str) -> tuple[str, list[Place]]:
        self.calls.append(city)
        return city, self.known.get(city, [])


def test_geocoded_country_fills_in_where_the_listings_do_not(tmp_path: Path) -> None:
    w = World(tmp_path)
    client = FakeClient(w.s, w.store, LISTED)
    countries = host.Countries(client.countries(w.s), ALIASES)
    host.fetch_venues(w.s, client, ["England", "Qatar", "Wales"])
    host.apply_countries(w.s, host.stored_venues(w.s, w.store))
    moscow = models.Venue(source_id=normalize.SOURCE_ID, name="Luzhniki Stadium",
                          city="Moscow", country="World", observed_at=T0)  # fmt: skip
    w.s.add(moscow)
    w.s.flush()
    m = models.Match(source_id=normalize.SOURCE_ID, source_ref="99", season_id=w.nat,
                     home_team_id=w.team["England"], away_team_id=w.team["Korea Republic"],
                     kickoff_time=T0, status="FT", venue_id=moscow.venue_id)  # fmt: skip
    w.s.add(m)
    w.s.commit()
    rows = host.national_matches(w.s)
    om = FakePlaces({"Moscow": [place("Moscow", "RU", "Russia", 12_000_000)],
                     "London": [place("London", "GB", "United Kingdom", 8_900_000, "England")],
                     "Nassau": [place("Nassau", "BS", "Bahamas", 274_000)]})  # fmt: skip
    got = host.geocode_fallback(w.s, om, rows, countries)
    assert got == {"placed": 1, "not_placed": 1}  # Moscow; Nassau has no provider country
    assert sorted(om.calls) == ["Moscow", "Nassau"]  # venues /venues placed are not searched
    assert moscow.geo_country == "Russia" and moscow.provider_country is None
    wem = w.s.get_one(models.Venue, w.venue["wembley"])
    assert wem.provider_country == "England" and wem.geo_country is None
    assert host.geocode_fallback(w.s, om, rows, countries) == {"not_placed": 1}  # resumable
    roles = host.host_roles(w.s, rows, countries)
    assert (
        dict(zip(roles["match_id"].to_list(), roles["role"].to_list(), strict=True))[m.match_id]
        == "neutral"
    )
