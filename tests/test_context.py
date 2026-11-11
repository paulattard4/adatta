"""The match context table: as-of rules and the no-leakage acceptance test."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters.storage import LocalStore, encode_payload
from adatta.db import models
from adatta.db.models import Base
from adatta.features import context, travel
from adatta.features.league_rules import Deduction, LeagueRules, Rules, SeasonRule

T0 = datetime(2023, 8, 12, 14, 0, tzinfo=UTC)
KO = T0 + timedelta(weeks=5)  # the target match
HORIZON = timedelta(hours=1)
AS_OF = KO - HORIZON


def _rules(deductions: list[Deduction] | None = None) -> Rules:
    rule = SeasonRule(years=[2023], teams=4, champion=(1, 1), relegation=(4, 4))
    return Rules(
        leagues=[LeagueRules(league_id=140, var_from=2018, seasons=[rule])],
        deductions=deductions or [],
    )


def _setup(tmp_path: Path) -> tuple[Session, LocalStore, dict[str, Any]]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    normalize.ensure_source(s)
    league = normalize.ensure_competition(s, league_id=140, name="La Liga", country="Spain",
                                          level="senior", tier="1")  # fmt: skip
    ucl = normalize.ensure_competition(s, league_id=2, name="UEFA Champions League",
                                       country="Europe", level="senior", tier="cup")  # fmt: skip
    season = normalize.ensure_season(s, league.competition_id, 2023)
    ucl_season = normalize.ensure_season(s, ucl.competition_id, 2023)
    venue = models.Venue(source_id=normalize.SOURCE_ID, name="Bilbao", city="Bilbao",
                         country="Spain", latitude=43.26, longitude=-2.93, elevation_m=19.0,
                         timezone="Europe/Madrid", geocode_status="auto",
                         observed_at=T0)  # fmt: skip
    teams = [models.Team(source_id=normalize.SOURCE_ID, source_ref=str(i), name=f"T{i}")
             for i in range(4)]  # fmt: skip
    s.add_all([venue, *teams])
    s.flush()
    t = [x.team_id for x in teams]
    ids: dict[str, Any] = {"teams": t, "season": season.season_id}

    def match(ref: str, h: int, a: int, ko: datetime, status: str = "FT",
              goals: tuple[int, int] | None = (1, 0), season_id: int | None = None,
              rnd: str = "Regular Season - 1") -> models.Match:  # fmt: skip
        m = models.Match(source_id=normalize.SOURCE_ID, source_ref=ref,
                         season_id=season_id or season.season_id, home_team_id=h,
                         away_team_id=a, kickoff_time=ko, status=status, round=rnd,
                         venue_id=venue.venue_id)  # fmt: skip
        s.add(m)
        s.flush()
        if goals is not None:
            s.add(models.MatchResult(match_id=m.match_id, source_id=normalize.SOURCE_ID,
                                     home_goals_nt=goals[0], away_goals_nt=goals[1],
                                     observed_at=ko + timedelta(hours=2)))  # fmt: skip
        return m

    ids["match"] = match
    for w in range(5):  # four weeks of a round robin before the target
        ko = T0 + timedelta(weeks=w)
        match(f"r{w}a", t[0], t[2], ko)
        match(f"r{w}b", t[1], t[3], ko)
    match("ucl", t[0], t[3], KO - timedelta(days=3), season_id=ucl_season.season_id,
          rnd="Group Stage - 1")  # fmt: skip
    ids["target"] = match("target", t[0], t[1], KO, status="NS", goals=None)
    match("next", t[1], t[2], KO + timedelta(days=4), status="NS", goals=None)
    s.commit()
    travel.build_home_venues(s, [league.competition_id])
    return s, LocalStore(tmp_path), ids


def _row(s: Session, store: LocalStore, ids: dict[str, Any], rules: Rules,
         horizon: timedelta = HORIZON) -> dict[str, Any]:  # fmt: skip
    df = context.context_features(s, store, [ids["target"]], rules, horizon)
    return df.row(0, named=True)


def test_context_columns_follow_their_as_of_rules(tmp_path: Path) -> None:
    s, store, ids = _setup(tmp_path)
    row = _row(s, store, ids, _rules())
    assert row["as_of"] == AS_OF
    assert row["home_days_since_last"] == 3.0  # the Champions League game counts
    assert row["away_days_since_last"] == 7.0
    assert row["away_days_to_next"] == 4.0 and row["away_next_kind"] == "league"
    assert row["home_days_to_next"] is None
    assert row["weather_source"] is None  # no forecast, climatology not downloaded
    assert row["referee"] is None and row["referee_basis"] == "pool_prior"
    assert row["has_var"] is True and row["kickoff_hour_local"] == 16  # CEST
    assert row["home_importance"] == "live" and row["venue_source"] == "fixture"


def test_no_fact_observed_after_as_of_reaches_the_row(tmp_path: Path) -> None:
    """The acceptance test: later facts leave the row unchanged; earlier ones are used."""
    s, store, ids = _setup(tmp_path)
    t, target = ids["teams"], ids["target"]
    # Capture was watching before as_of; no referee named yet.
    s.add(models.FixtureSnapshot(match_id=target.match_id, source_id=normalize.SOURCE_ID,
                                 observed_at=AS_OF - timedelta(days=3), kickoff_time=KO,
                                 status="NS"))  # fmt: skip
    s.commit()
    before = _row(s, store, ids, _rules())
    assert before["referee_basis"] == "pool_prior"
    # A result that kicked off after as_of - RESULT_LAG: not final by as_of.
    ids["match"]("late", t[2], t[3], AS_OF - timedelta(hours=2), goals=(5, 0))
    # A deduction effective on as_of's own day (only the day before counts).
    late_deduction = Deduction(league_id=140, season=2023, team_ref="1", team="T1", points=20,
                               effective=AS_OF.date(), date_confidence="high")  # fmt: skip
    # A forecast and a referee first seen a minute after as_of.
    after = AS_OF + timedelta(minutes=1)
    s.add(_forecast(target, after, 30.0))
    s.add(models.FixtureSnapshot(match_id=target.match_id, source_id=normalize.SOURCE_ID,
                                 observed_at=after, kickoff_time=KO, status="NS",
                                 referee="Late Referee"))  # fmt: skip
    s.commit()
    later = _row(s, store, ids, _rules([late_deduction]))
    assert later == before
    # The same kinds of fact observed in time are used.
    s.add(_forecast(target, AS_OF - timedelta(hours=5), 12.5))
    s.add(_forecast(target, AS_OF - timedelta(hours=1), None))  # blank: past its range
    s.add(models.FixtureSnapshot(match_id=target.match_id, source_id=normalize.SOURCE_ID,
                                 observed_at=AS_OF - timedelta(days=2), kickoff_time=KO,
                                 status="NS", referee="Early Referee"))  # fmt: skip
    s.commit()
    early_deduction = late_deduction.model_copy(update={"effective": date(2023, 9, 1)})
    used = _row(s, store, ids, _rules([early_deduction]))
    assert used["weather_source"] == "forecast" and used["temperature_c"] == 12.5
    assert used["forecast_lead_hours"] == 6.0
    assert (used["referee"], used["referee_basis"]) == ("Early Referee", "snapshot")
    assert used["away_rank"] == 4  # 15 points less 20: bottom


def test_season_list_referee_is_known_only_from_d26_lead(tmp_path: Path) -> None:
    s, store, ids = _setup(tmp_path)
    payload = {"response": [{"fixture": {"id": "target", "referee": "List Referee"}}]}
    blob, digest = encode_payload(payload)
    store.put("fixtures/140-2023.json.gz", blob)
    s.add(models.RawPayload(source_id=normalize.SOURCE_ID, endpoint="/fixtures",
                            params={"league": 140, "season": 2023},
                            storage_key="fixtures/140-2023.json.gz",
                            content_hash=digest))  # fmt: skip
    s.commit()
    near = _row(s, store, ids, _rules(), timedelta(hours=1))
    assert (near["referee"], near["referee_basis"]) == ("List Referee", "d26")
    far = _row(s, store, ids, _rules(), timedelta(hours=48))
    assert (far["referee"], far["referee_basis"]) == (None, "pool_prior")


def _forecast(
    match: models.Match, observed: datetime, temp: float | None
) -> models.WeatherSnapshot:
    return models.WeatherSnapshot(
        match_id=match.match_id, venue_id=match.venue_id, source_id="open_meteo",
        observed_at=observed, kickoff_time=KO, lead_hours=(KO - observed).total_seconds() / 3600,
        latitude=43.26, longitude=-2.93, temperature_c=temp, precipitation_mm=0.0,
        wind_speed_kmh=10.0, hourly={}, storage_key="w",
    )  # fmt: skip
