"""Home-venue spells, travel distance, timezone shift and altitude differential."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.db import models
from adatta.db.models import Base
from adatta.features import travel

T0 = datetime(2023, 8, 12, 14, 0, tzinfo=UTC)


def week(n: int) -> datetime:
    return T0 + timedelta(weeks=n)


def test_home_spells_ignore_temporary_venues_and_follow_moves() -> None:
    fixtures = [(week(i), 1) for i in range(5)] + [(week(5), 9)]  # one game elsewhere
    fixtures += [(week(i), 1) for i in range(6, 9)] + [(week(i), 2) for i in range(9, 13)]
    spells = travel.home_spells(fixtures)
    assert [(s.venue_id, s.n_home_matches) for s in spells] == [(1, 9), (2, 4)]
    assert spells[0].valid_from == week(0) and spells[0].valid_to == week(9)
    assert spells[1].valid_to is None
    # A team that returns to its old ground gets a third spell.
    back = travel.home_spells(fixtures + [(week(i), 1) for i in range(13, 16)])
    assert [s.venue_id for s in back] == [1, 2, 1]
    # No run of three anywhere: the most used venue, for the whole history.
    short = travel.home_spells([(week(0), 4), (week(1), 5), (week(2), 4)])
    assert [(s.venue_id, s.valid_to) for s in short] == [(4, None)]


def test_distance_and_timezone_helpers() -> None:
    london_manchester = travel.haversine_km(51.5072, -0.1276, 53.4808, -2.2426)
    assert 255 < london_manchester < 265
    summer = datetime(2024, 7, 1, 12, tzinfo=UTC)
    shift = travel.utc_offset_h("Atlantic/Canary", summer) - travel.utc_offset_h(
        "Europe/Madrid", summer
    )
    assert shift == -1.0


def _venue(s: Session, name: str, lat: float | None, lon: float | None, elev: float | None,
           tz: str | None = "Europe/Madrid", status: str = "auto") -> int:  # fmt: skip
    v = models.Venue(source_id=normalize.SOURCE_ID, name=name, city=name, country="Spain",
                     latitude=lat, longitude=lon, elevation_m=elev, timezone=tz,
                     geocode_status=status, observed_at=T0)  # fmt: skip
    s.add(v)
    s.flush()
    return v.venue_id


def _setup() -> tuple[Session, dict[str, int], int]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    normalize.ensure_source(s)
    comp = normalize.ensure_competition(s, league_id=140, name="La Liga", country="Spain",
                                        level="senior", tier="1")  # fmt: skip
    season = normalize.ensure_season(s, comp.competition_id, 2023)
    teams = {n: models.Team(source_id=normalize.SOURCE_ID, source_ref=n, name=n)
             for n in ("madrid", "laspalmas", "bilbao")}  # fmt: skip
    s.add_all(teams.values())
    s.flush()
    venues = {
        "madrid": _venue(s, "Madrid", 40.4165, -3.7026, 657.0),
        "laspalmas": _venue(s, "Las Palmas", 28.0997, -15.4134, 10.0, tz="Atlantic/Canary"),
        "bilbao": _venue(s, "Bilbao", 43.2627, -2.9253, 19.0),
        "unplaced": _venue(s, "Lezama", None, None, None, status="ambiguous"),
    }
    n = 0
    for home in ("madrid", "laspalmas", "bilbao"):
        away = teams["bilbao" if home != "bilbao" else "madrid"].team_id
        for i in range(4):
            n += 1
            s.add(
                models.Match(
                    source_id=normalize.SOURCE_ID, source_ref=str(n),
                    season_id=season.season_id, home_team_id=teams[home].team_id,
                    away_team_id=away, kickoff_time=week(i), status="FT",
                    venue_id=venues[home],
                )
            )  # fmt: skip
    s.commit()
    ids = {k: t.team_id for k, t in teams.items()} | {f"v_{k}": v for k, v in venues.items()}
    return s, ids, comp.competition_id


def _match(s: Session, ref: str, home: int, away: int, venue: int | None) -> models.Match:
    season_id = s.scalars(select(models.Season.season_id)).one()
    m = models.Match(source_id=normalize.SOURCE_ID, source_ref=ref, season_id=season_id,
                     home_team_id=home, away_team_id=away, kickoff_time=week(10), status="NS",
                     venue_id=venue)  # fmt: skip
    s.add(m)
    s.flush()
    return m


def test_build_home_venues_is_idempotent_and_supersedes_on_change() -> None:
    s, ids, comp = _setup()
    first = travel.build_home_venues(s, [comp])
    assert first["teams_new"] == 3 and first["spells"] == 3
    assert travel.build_home_venues(s, [comp])["unchanged"] == 3
    # Madrid move to another city for the rest of the season.
    new_ground = _venue(s, "Valencia", 39.4699, -0.3763, 15.0)
    for i in range(20, 24):
        _match(s, f"m{i}", ids["madrid"], ids["bilbao"], new_ground).kickoff_time = week(i)
    s.commit()
    again = travel.build_home_venues(s, [comp])
    assert again["teams_changed"] == 1 and again["moved"] == 1
    H = models.TeamHomeVenue
    current = s.scalars(select(H).where(H.team_id == ids["madrid"], H.superseded_at.is_(None)))
    assert [h.venue_id for h in current] == [ids["v_madrid"], new_ground]
    assert s.scalars(select(H).where(H.superseded_at.is_not(None))).one().team_id == ids["madrid"]


def test_travel_features() -> None:
    s, ids, comp = _setup()
    travel.build_home_venues(s, [comp])
    at_canaries = _match(s, "x1", ids["laspalmas"], ids["madrid"], ids["v_laspalmas"])
    no_venue = _match(s, "x2", ids["bilbao"], ids["madrid"], None)
    unplaced = _match(s, "x3", ids["bilbao"], ids["madrid"], ids["v_unplaced"])
    f = travel.travel_features(s, [at_canaries, no_venue, unplaced]).rows_by_key(
        "match_id", named=True, unique=True
    )
    row = f[at_canaries.match_id]
    assert row["home_travel_km"] == 0.0 and 1700 < row["away_travel_km"] < 1800
    assert row["away_tz_shift_h"] == -1.0 and row["home_tz_shift_h"] == 0.0
    assert row["away_altitude_diff_m"] == -647.0  # Madrid side comes down to sea level
    fallback = f[no_venue.match_id]
    assert fallback["venue_source"] == "home_base" and fallback["venue_id"] == ids["v_bilbao"]
    assert 300 < fallback["away_travel_km"] < 330
    blank = f[unplaced.match_id]  # ambiguous coordinates: no terms, nothing imputed
    assert blank["venue_source"] == "fixture" and blank["away_travel_km"] is None
    assert blank["away_altitude_diff_m"] is None and blank["venue_elevation_m"] is None


def test_domestic_list_claims_a_venue_first_seen_in_europe() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        normalize.ensure_source(s)
        v = normalize.venues_by_key(s, {("San Mamés", "Bilbao"): "1460"}, "Europe")
        assert v[("San Mamés", "Bilbao")].country == "Europe"
        again = normalize.venues_by_key(s, {("San Mamés", "Bilbao"): None}, "Spain")
        assert again[("San Mamés", "Bilbao")].country == "Spain"
        assert again[("San Mamés", "Bilbao")].source_ref == "1460"


def test_renamed_and_respelt_grounds_are_one_site() -> None:
    s, ids, comp = _setup()
    renamed = _venue(s, "Estadio Madrid Sponsor Arena", 40.42, -3.70, 650.0)  # 1 km away
    blank_city = models.Venue(
        source_id=normalize.SOURCE_ID,
        name="Madrid",
        city="",
        country="Spain",
        geocode_status="unresolved",
        observed_at=T0,
    )
    s.add(blank_city)
    s.flush()
    for i, v in enumerate([renamed] * 4 + [blank_city.venue_id] * 4):
        _match(s, f"r{i}", ids["madrid"], ids["bilbao"], v).kickoff_time = week(20 + i)
    s.commit()
    travel.build_home_venues(s, [comp])
    H = models.TeamHomeVenue
    spells = s.scalars(select(H).where(H.team_id == ids["madrid"])).all()
    assert len(spells) == 1 and spells[0].n_home_matches == 12
    assert spells[0].venue_id in (ids["v_madrid"], renamed)  # a placed venue, not the blank one
