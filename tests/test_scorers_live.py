"""P3-e (f) live verdict: season guards, the live refit grids, the lineup-snapshot squad,
P as served, the anytime-scorer parser and the name matching."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from test_backtest import LEAGUES
from test_scorers import _app
from test_serve import AS_OF, K, World

from adatta.adapters import api_football_capture as afc
from adatta.adapters.normalize import SOURCE_ID
from adatta.db import models
from adatta.eval import selection as ev
from adatta.features import scorers as fs
from adatta.model import lineup as ml
from adatta.model import scorers_live as sl


def test_live_builds_may_load_the_holdout_as_history_only() -> None:
    ev.check_max_season(2023, live=False)
    ev.check_max_season(2026, live=True)
    for season, live in ((2024, False), (2026, False), (2024, True), (2025, True)):
        with pytest.raises(ev.HoldoutRefused):
            ev.check_max_season(season, live)


def test_live_quality_grid_continues_the_dev_grid() -> None:
    start = datetime(2019, 7, 1, tzinfo=UTC)
    rows = pl.DataFrame({
        "season": [2019, 2023, 2024, 2026, 2026],
        "as_of": [start, start + timedelta(days=1700), start + timedelta(days=1900),
                  datetime(2026, 8, 20, tzinfo=UTC), datetime(2026, 11, 15, tzinfo=UTC)],
    })  # fmt: skip
    dev = ml.quality_times(rows)
    live = ml.quality_times(rows, live=True)
    step = timedelta(days=28)
    assert all((b - a) == step for a, b in zip(live, live[1:], strict=False))
    assert (live[0] - dev[1]) % step == timedelta(0)  # the same grid, continued
    assert live[0] <= datetime(2026, 8, 20, tzinfo=UTC) < live[0] + step
    assert live[-1] <= datetime(2026, 11, 15, tzinfo=UTC)


def test_snapshot_squad_is_the_announced_lineup() -> None:
    app = _app()  # team 1, matches 100-105; player 12 never comes on
    squad = pl.DataFrame({"match_id": [105] * 3, "team_id": [1] * 3,
                          "player_id": [10, 12, 77], "started": [True, False, False],
                          "pos": ["F", "F", None]})  # fmt: skip
    tg = pl.DataFrame({"match_id": [105], "team_id": [1], "kickoff": [app["kickoff"].max()],
                       "league_ref": ["39"], "season": [2019]},
                      schema_overrides={"season": pl.Int32})  # fmt: skip
    sq, tm = fs.snapshot_squad(app, squad, tg)
    got = {r["player_id"]: r for r in sq.iter_rows(named=True)}
    assert got[10]["played"] and got[10]["line"] == "F" and got[10]["started"]
    assert not got[12]["played"] and not got[12]["started"]
    assert got[77]["line"] is None  # no announced position: the pool's line, else dropped
    assert tm.columns == ["match_id", "team_id", "kickoff", "league_ref", "season"]


def test_scorer_odds_parser_reads_only_the_anytime_market() -> None:
    payload = {"response": [{"bookmakers": [
        {"name": "A", "bets": [
            {"name": "Anytime Goal Scorer", "values": [{"value": "Harry Kane", "odd": "2.10"},
                                                       {"value": "X", "odd": "bad"}]},
            {"name": "First Goal Scorer", "values": [{"value": "Harry Kane", "odd": "5.0"}]},
        ]},
        {"name": "B", "bets": [
            {"name": "Anytime Goal Scorer", "values": [{"value": "Harry Kane", "odd": "2.20"}]},
        ]},
    ]}]}  # fmt: skip
    got = afc.parse_scorer_odds(payload)
    assert [(p.bookmaker, p.selection, p.decimal_odds) for p in got] == [
        ("A", "Harry Kane", 2.10), ("B", "Harry Kane", 2.20)]  # fmt: skip


def test_names_match_through_accents_initials_and_never_ambiguously() -> None:
    assert fs.name_keys("Ángel Cano") == ("angel cano", "a cano", "cano")
    players = [(1, ["A. Cano", "Ángel Cano"]), (2, ["G. Miller"]), (3, ["Luis Garcia"]),
               (4, ["Pablo Garcia"])]  # fmt: skip
    got = fs.match_names(["Angel Cano", "Gavin Miller", "Garcia", "Nobody"], players)
    assert got == {"Angel Cano": 1, "Gavin Miller": 2}  # "Garcia" is two players


# ------------------------------------------------------------------------- DB pieces


def _lineup(w: World, mid: int, team: int, at: datetime) -> None:
    xi = [{"player_ref": str(100 * team + i), "name": f"P{i}", "number": i, "pos": "M",
           "grid": None} for i in range(11)]  # fmt: skip
    w.s.add(models.LineupSnapshot(match_id=mid, team_id=team, source_id=SOURCE_ID,
                                  observed_at=at, starters=xi, substitutes=[]))  # fmt: skip


def _finish(w: World, mid: int) -> None:
    m = w.s.get(models.Match, mid)
    assert m is not None
    m.status = "FT"
    w.s.add(
        models.MatchResult(
            match_id=mid,
            source_id=SOURCE_ID,
            home_goals_nt=1,
            away_goals_nt=0,
            observed_at=m.kickoff_time + timedelta(hours=3),
        )
    )


def _season_2026(w: World) -> None:
    for se in w.s.query(models.Season).all():
        se.year = 2026


def test_targets_need_both_lineups_before_kickoff_and_a_result(tmp_path: Path) -> None:
    w = World(tmp_path)
    _season_2026(w)
    ok, late, unplayed = w.fixture(K), w.fixture(K, home=3, away=4), w.fixture(K, home=5, away=6)
    for mid, (h, a) in ((ok, (1, 2)), (late, (3, 4)), (unplayed, (5, 6))):
        _lineup(w, mid, h, K - timedelta(minutes=60))
        _lineup(w, mid, a, K + timedelta(minutes=1) if mid == late else K - timedelta(minutes=50))
    _finish(w, ok)
    _finish(w, late)
    w.s.commit()
    t = sl.live_targets(w.s, [str(x) for x in LEAGUES], 2026)
    assert sorted(set(t["match_id"])) == [ok]
    assert t["lineup_seen"].unique().to_list() == [K - timedelta(minutes=60)]


def test_p_as_served_reproduces_the_forecast_before_the_lineup(tmp_path: Path) -> None:
    w = World(tmp_path)
    _season_2026(w)
    w.refit(AS_OF)
    mid = w.fixture(K)
    w.tick(K - timedelta(days=10))  # initial
    w.tick(K - timedelta(hours=21))  # T-24h
    seen = K - timedelta(minutes=55)
    w.tick(K - timedelta(minutes=45))  # T-60m: after the lineup, so not used
    _lineup(w, mid, 1, seen)
    _lineup(w, mid, 2, seen)
    _finish(w, mid)
    w.s.commit()
    tg = sl.live_targets(w.s, [str(x) for x in LEAGUES], 2026)
    pm, stats = sl.live_pmfs(w.s, w.store, tg, 10, {lg: "england" for lg in LEAGUES})
    assert stats["incoherent"] == 0 and stats["max_diff"] < 1e-9
    assert pm.height == 2 and set(pm["milestone"]) == {"T-24h"}
    for p in pm["pmf"].to_list():
        assert np.isclose(sum(p), 1.0)


def test_auc_ignores_a_margin_that_keeps_the_order() -> None:
    from adatta.edge import scorers as ed

    rng = np.random.default_rng(3)
    p = rng.uniform(0.01, 0.4, 2000)
    hit = rng.uniform(size=2000) < p
    assert ed.auc(p, hit) == pytest.approx(ed.auc(1.5 * p + 0.02, hit))  # order kept
    assert ed.auc(np.array([0.9, 0.1, 0.5]), np.array([True, False, False])) == 1.0
    match = np.repeat(np.arange(100), 20)
    d, lo, hi = ed.auc_diff(p, 3 * p, hit, match, n=200)
    assert d == pytest.approx(0.0) and lo <= 0.0 <= hi
    d, lo, hi = ed.auc_diff(p, rng.uniform(size=2000), hit, match, n=200)
    assert d > 0 and lo > 0  # a random ranking is worse
