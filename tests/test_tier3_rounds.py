"""Tier-3 cleanup (docs/BEYOND_LEAGUES_PLAN.md): one regular-round rule, duplicate fixtures
kept once, a group key from who plays whom, and play-off ids that are not round robins."""

from datetime import UTC, datetime, timedelta
from itertools import permutations
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from adatta.adapters import backfill, normalize
from adatta.config import Competition
from adatta.db import models
from adatta.db.models import Base
from adatta.eval import harness

T0 = datetime(2024, 9, 1, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ("label", "regular", "second"),
    [
        ("Regular Season - 12", True, False),
        ("Girone B - 3", True, False),
        ("Group 1 - 36", True, False),
        ("Group 4 - 2nd Phase - 2", True, True),
        ("Group A - 3", False, False),  # UEFA's lettered group stage
        ("Group Stage - 1", False, False),
        ("Play-offs - Final", False, False),
        ("Relegation Round - 1", False, False),
        ("", False, False),
        (None, False, False),
    ],
)
def test_regular_round_rule(label: str | None, regular: bool, second: bool) -> None:
    assert normalize.is_regular_round(label) is regular
    assert normalize.is_second_phase(label) is second


def test_groups_are_the_components_of_who_plays_whom() -> None:
    a, b = range(1, 5), range(11, 14)
    pairs = list(permutations(a, 2)) + list(permutations(b, 2))
    groups = backfill.round_robin_groups(pairs)
    assert {groups[t] for t in a} == {1} and {groups[t] for t in b} == {11}


def _m(mid: int, home: int, away: int, kick: datetime) -> Any:
    return SimpleNamespace(match_id=mid, home_team_id=home, away_team_id=away, kickoff_time=kick)


def test_duplicate_fixtures_keep_the_lowest_id() -> None:
    ms = [_m(7, 1, 2, T0), _m(5, 1, 2, T0), _m(6, 2, 1, T0), _m(8, 1, 2, T0 + timedelta(days=9))]
    assert [m.match_id for m in backfill.duplicate_fixtures(ms)] == [7]


def _world() -> tuple[Session, int]:
    """One league season under id 900: two groups of three (a split league under one id),
    one fixture listed twice, and one play-off match."""
    s = Session(create_engine("sqlite://"))
    Base.metadata.create_all(s.get_bind())
    normalize.ensure_source(s)
    comp = normalize.ensure_competition(s, league_id=900, name="L", country="X",
                                        level="senior", tier="3")  # fmt: skip
    season = normalize.ensure_season(s, comp.competition_id, 2024)
    teams = {i: normalize.ensure_team(s, str(i), f"T{i}").team_id for i in (1, 2, 3, 11, 12, 13)}
    mid = 0

    def add(home: int, away: int, kick: datetime, rnd: str) -> None:
        nonlocal mid
        mid += 1
        m = models.Match(source_id=normalize.SOURCE_ID, source_ref=str(mid),
                         season_id=season.season_id, home_team_id=teams[home],
                         away_team_id=teams[away], kickoff_time=kick, status="FT",
                         round=rnd)  # fmt: skip
        s.add(m)
        s.flush()
        s.add(models.MatchResult(match_id=m.match_id, source_id=normalize.SOURCE_ID,
                                 home_goals_nt=1, away_goals_nt=0, observed_at=kick))  # fmt: skip

    day = 0
    for grp in ((1, 2, 3), (11, 12, 13)):
        for h, a in permutations(grp, 2):
            day += 1
            add(h, a, T0 + timedelta(days=day), f"Girone {'A' if grp[0] == 1 else 'B'} - {day}")
    add(1, 2, T0 + timedelta(days=1), "Girone A - 1")  # the provider's second listing
    add(1, 11, T0 + timedelta(days=60), "Play-offs - Final")
    s.commit()
    return s, mid


def test_load_matches_reads_group_rounds_and_keeps_a_duplicate_once() -> None:
    s, n = _world()
    df, unscorable = harness.load_matches(s, [900])
    assert df.height == 12 and unscorable.height == 0  # 2 x 3*2 round robin, no play-off
    assert df["match_id"].n_unique() == 12 and 13 not in df["match_id"].to_list()
    assert n == 14


def test_report_counts_groups_flags_the_duplicate_and_skips_knockout_ids() -> None:
    s, _ = _world()
    league = Competition(name="L", country="X", league_id=900, level="senior", tier=3,
                         priority=6, seasons=[2024])  # fmt: skip
    (row,) = backfill.completeness(s, [league])
    assert row["groups"] == 2 and row["regular_expected"] == row["regular_listed"] == 12
    assert row["duplicates"] == 1 and "duplicate fixtures 1" in row["flags"]
    assert "regular season" not in row["flags"] and "round labels" not in row["flags"]
    knockout = league.model_copy(update={"knockout": True})
    (ko,) = backfill.completeness(s, [knockout])
    assert ko["regular_expected"] is None and "round labels" not in ko["flags"]


def test_capture_reads_tier3_without_duplicate_ids() -> None:
    from adatta.cli import CAPTURE_FILES
    from adatta.config import load_competitions

    comps = [c for path in CAPTURE_FILES for c in load_competitions(path)]
    ids = [c.league_id for c in comps]
    assert len(ids) == len(set(ids))
    assert {41, 80, 63, 138, 942, 943, 435, 436} <= set(ids)


def test_capture_reads_the_c2_top_flights_not_their_second_tiers() -> None:
    from adatta.cli import CAPTURE_FILES
    from adatta.config import load_competitions

    ids = {c.league_id for path in CAPTURE_FILES for c in load_competitions(path)}
    assert {94, 88, 144, 218, 179, 203, 345, 119} <= ids  # D49 top flights, captured live
    assert not {95, 89, 145, 219, 180, 204, 346, 120} & ids  # second tiers: history only
