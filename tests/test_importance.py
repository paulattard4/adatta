"""Importance labels from an as-of table, and the climatology summary."""

from datetime import UTC, date, datetime, timedelta

from adatta.features import climatology, importance
from adatta.features.league_rules import Deduction, Rules, SeasonRule

RULE = SeasonRule(years=[2020], teams=4, champion=(1, 1), relegation=(4, 4))


def test_stakes_of_rules() -> None:
    assert importance.stakes_of(RULE) == {"title": 1, "survival": 3}
    bundesliga = SeasonRule(years=[2020], teams=18, champion=(1, 1), ucl=(1, 4), europe=(1, 6),
                            relegation_playoff=(16, 16), relegation=(17, 18))  # fmt: skip
    assert importance.stakes_of(bundesliga) == {
        "title": 1, "ucl": 4, "europe": 6, "survival": 15, "avoid_direct_relegation": 16,
    }  # fmt: skip


def test_standing_counts_points_and_deductions() -> None:
    t = importance.standing([1, 2, 3], [(1, 2, 2, 0), (2, 3, 1, 1)], {3: 3})
    assert t.points == {1: 3, 2: 1, 3: -2} and t.played == {1: 1, 2: 2, 3: 1}
    assert t.rank(1) == 1 and t.rank(3) == 3


def test_classify_dead_rubber_must_win_and_live() -> None:
    stakes = importance.stakes_of(RULE)
    # 4 teams, 6 games each, 5 played: one round to go.
    t = importance.Standing(points={1: 15, 2: 13, 3: 4, 4: 2}, played=dict.fromkeys(range(1, 5), 5),
                            goal_diff=dict.fromkeys(range(1, 5), 0),
                            goals_for=dict.fromkeys(range(1, 5), 0))  # fmt: skip
    assert importance.classify(1, t, 6, stakes) == ("live", ["title"])  # a draw is enough
    assert importance.classify(2, t, 6, stakes) == ("must_win", ["title"])  # draw ends it
    assert importance.classify(3, t, 6, stakes) == ("live", ["survival"])
    assert importance.classify(4, t, 6, stakes) == ("must_win", ["survival"])
    t.points[4] = 0  # now 4 points adrift with 3 left: relegated
    assert importance.classify(3, t, 6, stakes) == ("dead_rubber", [])
    assert importance.classify(4, t, 6, stakes) == ("dead_rubber", [])
    early = importance.Standing(points={1: 3, 2: 1, 3: 1, 4: 0},
                                played=dict.fromkeys(range(1, 5), 1),
                                goal_diff=dict.fromkeys(range(1, 5), 0),
                                goals_for=dict.fromkeys(range(1, 5), 0))  # fmt: skip
    assert importance.classify(4, early, 6, stakes) == ("live", ["title", "survival"])


def test_awarded_results_count_as_dated_changes() -> None:
    rules = Rules(
        leagues=[], deductions=[],
        awarded_results=[Deduction(league_id=1, season=2020, team_ref="9", team="X", points=2,
                                   effective=date(2021, 1, 9), date_confidence="approximate")],
    )  # fmt: skip
    assert rules.deductions_as_of(1, 2020, date(2021, 1, 8)) == {}
    assert rules.deductions_as_of(1, 2020, date(2021, 1, 9)) == {"9": -2}  # a gain
    assert rules.net_deductions(1, 2020) == {}  # not part of the W/D/L gap check


def test_climatology_window_and_match_hours() -> None:
    start = datetime(2016, 1, 1, tzinfo=UTC)
    hours = [start + timedelta(hours=i) for i in range(3 * 366 * 24)]
    hours = [h for h in hours if h.year <= 2018]
    payload = {"hourly": {
        "time": [h.strftime("%Y-%m-%dT%H:%M") for h in hours],
        "temperature_2m": [float(h.hour) for h in hours],  # temperature = hour of day
        "precipitation": [0.5] * len(hours),
        "wind_speed_10m": [10.0] * len(hours),
    }}  # fmt: skip
    cc = climatology.CellClimate.from_payload(payload)
    temp, rain, wind, years = cc.at(datetime(2023, 3, 1, 15, 30, tzinfo=UTC))
    assert temp == 16.0  # mean of hours 15, 16, 17
    assert rain == 1.0 and wind == 10.0 and years == 3  # two match hours of 0.5 mm
    late = cc.at(datetime(2023, 12, 31, 23, 0, tzinfo=UTC))  # crosses midnight and the year
    assert late[0] is not None and abs(late[0] - (23 + 0 + 1) / 3) < 0.01
    assert climatology.cell(51.48153, -0.19098) == (51.48, -0.19)
