"""Team style as of a prediction time (Phase 2 F1): the window, the minimum, the league
normalisation, and the no-leakage acceptance test."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.db import models
from adatta.db.models import Base
from adatta.features import style
from adatta.features.referees import RESULT_LAG
from adatta.features.style import MIN_MATCHES, WINDOW, Played, StyleHistory

T0 = datetime(2023, 8, 1, 15, 0, tzinfo=UTC)
LEAGUE, LOWER = 1, 2
TOL = 1e-4  # values are rounded to 4-5 decimals


def played(team: int, ko: datetime, sf: float = 12, sa: float = 8, comp: int = LEAGUE,
           poss: float | None = 50.0) -> Played:  # fmt: skip
    return Played(team, comp, ko, sf, sa, sf / 3, sa / 3, poss)


def weekly(team: int, n: int, start: datetime = T0, **kw: object) -> list[Played]:
    return [played(team, start + timedelta(weeks=w), **kw) for w in range(n)]  # type: ignore[arg-type]


def test_values_are_window_means() -> None:
    rows = weekly(7, 6, sf=10, sa=6, poss=None)
    rows[0] = played(7, T0, sf=16, sa=12, poss=60.0)
    v = StyleHistory(rows).team(7, T0 + timedelta(weeks=6))
    assert v["matches"] == 6
    assert v["shots_for_pm"] == pytest.approx((16 + 5 * 10) / 6, abs=TOL)
    assert v["shots_against_pm"] == pytest.approx((12 + 5 * 6) / 6, abs=TOL)
    assert v["sot_for_pm"] == pytest.approx((16 + 5 * 10) / 18, abs=TOL)
    assert v["possession"] == 60.0  # mean over the matches that report it


def test_fewer_than_the_minimum_is_null_never_imputed() -> None:
    h = StyleHistory(weekly(7, MIN_MATCHES - 1))
    v = h.team(7, T0 + timedelta(weeks=MIN_MATCHES))
    assert v["matches"] == MIN_MATCHES - 1
    assert all(v[k] is None for k in style.VALUES)
    assert (
        StyleHistory(weekly(7, MIN_MATCHES)).team(7, T0 + timedelta(weeks=9))["tempo_rel"]
        is not None
    )
    unseen = h.team(99, T0 + timedelta(weeks=9))
    assert unseen["matches"] == 0 and unseen["tempo_rel"] is None


def test_window_drops_matches_older_than_a_year() -> None:
    old = weekly(7, 10, start=T0 - WINDOW - timedelta(weeks=12), sf=30)
    recent = weekly(7, 5, sf=10)
    v = StyleHistory(old + recent).team(7, T0 + timedelta(weeks=5))
    assert v["matches"] == 5 and v["shots_for_pm"] == 10


def test_no_match_after_as_of_minus_lag_reaches_the_value() -> None:
    """Leakage acceptance test: later matches and their statistics change nothing."""
    as_of = T0 + timedelta(weeks=8)
    base = weekly(7, 8) + weekly(8, 8, sf=8, sa=12)
    ref = StyleHistory(base).team(7, as_of)
    late = [
        played(7, as_of - RESULT_LAG + timedelta(minutes=1), sf=40, sa=40),  # not final yet
        played(7, as_of + timedelta(days=3), sf=40, sa=40),
        played(8, as_of + timedelta(days=1), sf=90, sa=0),  # moves the league mean later
    ]
    assert StyleHistory(base + late).team(7, as_of) == ref
    # a match exactly at the cutoff is final and counts
    edge = played(7, as_of - RESULT_LAG, sf=40, sa=40)
    n_ref = ref["matches"]
    assert isinstance(n_ref, int)
    assert StyleHistory([*base, edge]).team(7, as_of)["matches"] == n_ref + 1


def test_tempo_is_relative_to_each_match_league() -> None:
    # League: sides 7 and 8 (tempo 30 and 20 per match: mean 25); lower league: 9 at 10.
    top = weekly(7, 6, sf=18, sa=12) + weekly(8, 6, sf=8, sa=12)
    lower = weekly(9, 6, sf=5, sa=5, comp=LOWER) + weekly(10, 6, sf=10, sa=10, comp=LOWER)
    rows = top + lower
    h = StyleHistory(rows)
    as_of = T0 + timedelta(weeks=7)
    assert h.team(7, as_of)["tempo_rel"] == pytest.approx(30 / 25 - 1, abs=TOL)
    assert h.team(8, as_of)["tempo_rel"] == pytest.approx(20 / 25 - 1, abs=TOL)
    assert h.team(9, as_of)["tempo_rel"] == pytest.approx(10 / 15 - 1, abs=TOL)
    # a promoted side: half its window in the lower league, measured against that league
    promoted = weekly(11, 4, sf=10, sa=10, comp=LOWER) + weekly(
        11, 4, start=T0 + timedelta(weeks=4), sf=15, sa=10)  # fmt: skip
    h2 = StyleHistory(rows + promoted)
    v = h2.team(11, T0 + timedelta(weeks=9))
    lower_mean = (10 * 6 + 20 * 6 + 20 * 4) / 16  # sides 9, 10, 11 in the lower league
    league_mean = (30 * 6 + 20 * 6 + 25 * 4) / 16
    assert v["tempo_rel"] == pytest.approx(
        (4 * 20 / lower_mean + 4 * 25 / league_mean) / 8 - 1, abs=TOL
    )


def test_load_keeps_finished_league_matches_with_both_sides() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    normalize.ensure_source(s)
    league = normalize.ensure_competition(s, league_id=140, name="La Liga", country="Spain",
                                          level="senior", tier="1")  # fmt: skip
    cup = normalize.ensure_competition(s, league_id=143, name="Copa del Rey", country="Spain",
                                       level="senior", tier="cup")  # fmt: skip
    ls = normalize.ensure_season(s, league.competition_id, 2023)
    cs = normalize.ensure_season(s, cup.competition_id, 2023)
    teams = [models.Team(source_id=normalize.SOURCE_ID, source_ref=str(i), name=f"T{i}")
             for i in range(2)]  # fmt: skip
    s.add_all(teams)
    s.flush()
    a, b = (t.team_id for t in teams)
    n = 0

    def match(season: int, status: str = "FT", sides: tuple[int, ...] = (a, b),
              shots: int = 10, superseded: bool = False) -> None:  # fmt: skip
        nonlocal n
        n += 1
        m = models.Match(source_id=normalize.SOURCE_ID, source_ref=f"m{n}", season_id=season,
                         home_team_id=a, away_team_id=b, kickoff_time=T0 + timedelta(days=n),
                         status=status)  # fmt: skip
        s.add(m)
        s.flush()
        for t in sides:
            s.add(models.TeamMatchStat(
                match_id=m.match_id, team_id=t, source_id=normalize.SOURCE_ID,
                storage_key="k", shots_total=shots, shots_on_target=3, possession=50.0,
                stats={}, observed_at=T0, ingested_at=T0,
                superseded_at=T0 if superseded else None))  # fmt: skip

    match(ls.season_id)  # kept
    match(cs.season_id)  # cup
    match(ls.season_id, status="NS")  # not played
    match(ls.season_id, sides=(a,))  # one side only
    match(ls.season_id, superseded=True)  # only a superseded version
    s.commit()
    h = StyleHistory.load(s)
    v = h.team(a, T0 + timedelta(days=30))
    assert v["matches"] == 1
