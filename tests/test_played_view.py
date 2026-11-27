"""The played view (docs/DASHBOARD_TASKS.md): finished matches against the forecast
published before kickoff, scored like the backtest."""

from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
import test_serve
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_dashboard_api import ctx
from test_serve import AS_OF, K, World

from adatta.adapters.normalize import SOURCE_ID
from adatta.api import views
from adatta.api.app import create_app
from adatta.db import models
from adatta.eval import metrics

NOW = AS_OF + timedelta(days=2)
AFTER = K + timedelta(hours=3)


def played_world(tmp_path: Path, result: tuple[int, int] = (2, 1)) -> tuple[World, int]:
    """One fixture forecast before kickoff, then finished with a result."""
    w = World(tmp_path)
    w.refit(AS_OF)
    mid = w.fixture(K)
    w.tick(NOW)
    m = w.s.get(models.Match, mid)
    assert m is not None
    m.status = "FT"
    w.s.add(models.MatchResult(match_id=mid, source_id=SOURCE_ID, home_goals_nt=result[0],
                               away_goals_nt=result[1], observed_at=AFTER))  # fmt: skip
    w.s.commit()
    return w, mid


def stored(w: World, mid: int) -> dict[tuple[str, float | None, str], float]:
    MP = models.MarketProjection
    return {(r.market_type, r.line, r.selection): r.probability
            for r in w.s.scalars(select(MP).where(MP.match_id == mid))}  # fmt: skip


def test_a_finished_match_shows_its_pre_kickoff_forecast_and_scores(tmp_path: Path) -> None:
    w, mid = played_world(tmp_path, (2, 1))
    v = views.played(w.s, ctx(w), AFTER)
    assert [m.match_id for m in v.matches] == [mid]
    m = v.matches[0]
    assert (m.home_goals, m.away_goals, m.outcome, m.milestone) == (2, 1, "home", "initial")
    assert m.team_level == "senior"  # the page shows fair odds for senior only
    p = stored(w, mid)
    assert (m.home_p.p, m.draw_p.p, m.away_p.p) == (p[("1X2", None, "home")],
                                                    p[("1X2", None, "draw")],
                                                    p[("1X2", None, "away")])  # fmt: skip
    assert m.home_p.lo is not None and m.home_p.hi is not None
    assert m.home_p.lo <= m.home_p.p <= m.home_p.hi
    x = np.array([m.home_p.p, m.draw_p.p, m.away_p.p])
    assert m.scores.log_loss_1x2 == pytest.approx(-np.log(x[0]))
    assert m.scores.rps_1x2 == pytest.approx(((x[0] - 1) ** 2 + (x[0] + x[1] - 1) ** 2) / 2)
    assert m.scores.brier_1x2 == pytest.approx(metrics.brier(x, 0))
    over = p[("OU", 2.5, "over")]
    assert m.scores.log_loss_ou25 == pytest.approx(-np.log(over))  # 3 goals: over
    assert m.scores.log_loss_btts == pytest.approx(-np.log(p[("BTTS", None, "yes")]))
    assert [s.segment for s in v.summary] == ["all", m.competition]
    assert v.summary[0].n == 1 and v.uniform_log_loss_1x2 == pytest.approx(np.log(3))
    assert v.forecasts_since is not None


def test_a_projection_published_after_kickoff_is_never_used(tmp_path: Path) -> None:
    w, mid = played_world(tmp_path)
    before = views.played(w.s, ctx(w), AFTER).matches[0]
    # a later run (after kickoff) with a far better-looking "close" forecast
    run = models.ProjectionRun(
        generated_at=K + timedelta(minutes=5),
        as_of=K,
        git_commit="x",
        lockfile_hash="x",
        config_hash="x",
        component_versions={},
        training_snapshot_hash="x",
        override_set_hash="x",
        seed=1,
    )
    w.s.add(run)
    w.s.flush()
    for sel, p in (("home", 0.98), ("draw", 0.01), ("away", 0.01)):
        w.s.add(models.MarketProjection(projection_run_id=run.projection_run_id, match_id=mid,
                                        milestone="close", market_type="1X2", line=None,
                                        selection=sel, probability=p, interval_lo=p,
                                        interval_hi=p))  # fmt: skip
    w.s.commit()
    after = views.played(w.s, ctx(w), AFTER).matches[0]
    assert after.milestone == "initial" and after.home_p == before.home_p


def test_unfinished_or_unforecast_matches_are_left_out(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.refit(AS_OF)
    w.fixture(K)  # forecast but not played
    w.tick(NOW)
    old = w.fixture(AS_OF - timedelta(days=30), status="FT")  # played, never forecast
    w.s.add(models.MatchResult(match_id=old, source_id=SOURCE_ID, home_goals_nt=0,
                               away_goals_nt=0, observed_at=AS_OF))  # fmt: skip
    w.s.commit()
    v = views.played(w.s, ctx(w), AFTER)
    assert v.matches == [] and v.summary == []


def test_played_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        test_serve, "create_engine",
        lambda url: create_engine(url, poolclass=StaticPool,
                                  connect_args={"check_same_thread": False}),
    )  # fmt: skip
    w, mid = played_world(tmp_path)

    def sessions() -> Session:
        return Session(w.engine)

    client = TestClient(create_app(sessions, ctx(w), clock=lambda: AFTER))
    r = client.get("/api/played")
    assert r.status_code == 200
    body = r.json()
    assert body["matches"][0]["match_id"] == mid and body["matches"][0]["home_p"]["lo"] is not None
    # One played match: a mean but no interval, sent as null rather than NaN.
    one = body["summary"][0]["scores"][0]
    assert body["summary"][0]["tier"] is None and body["summary"][1]["tier"] == "1"
    assert body["matches"][0]["competition_tier"] == "1"
    assert one["n"] == 1 and one["mean"] > 0 and one["lo"] is None and one["hi"] is None
    assert client.get("/api/played", params={"competition_id": 999}).json()["matches"] == []


def test_match_page_carries_the_90_minute_result_of_a_finished_match(tmp_path: Path) -> None:
    w, mid = played_world(tmp_path, (0, 1))
    r = views.match_view(w.s, ctx(w), mid, AFTER).header.result
    assert r is not None and (r.home_goals, r.away_goals) == (0, 1)
    # a corrected result supersedes the first; only the current one is shown
    first = w.s.scalars(select(models.MatchResult).where(models.MatchResult.match_id == mid)).one()
    fixed = AFTER + timedelta(hours=1)
    first.superseded_at = fixed
    w.s.add(models.MatchResult(match_id=mid, source_id=SOURCE_ID, home_goals_nt=1,
                               away_goals_nt=1, observed_at=fixed))  # fmt: skip
    w.s.commit()
    r = views.match_view(w.s, ctx(w), mid, AFTER).header.result
    assert r is not None and (r.home_goals, r.away_goals) == (1, 1)
    m = w.s.get(models.Match, mid)
    assert m is not None
    m.status = "2H"  # not finished: no result shown, whatever the table holds
    w.s.commit()
    assert views.match_view(w.s, ctx(w), mid, AFTER).header.result is None


def test_no_result_before_the_match_or_without_a_result_row(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.refit(AS_OF)
    mid = w.fixture(K)
    w.tick(NOW)
    assert views.match_view(w.s, ctx(w), mid, NOW).header.result is None
    m = w.s.get(models.Match, mid)
    assert m is not None
    m.status = "FT"  # finished, result not ingested yet
    w.s.commit()
    assert views.match_view(w.s, ctx(w), mid, AFTER).header.result is None


# ---------------------------------------------- the market benchmark (owner, 2026-11-27)


def _odds(w: World, mid: int, book: str, market: str, prices: dict[str, float],
          at: timedelta, line: float | None = None) -> None:  # fmt: skip
    for sel, odds in prices.items():
        w.s.add(models.OddsSnapshot(match_id=mid, source_id=SOURCE_ID, bookmaker=book,
                                    market=market, line=line, selection=sel, decimal_odds=odds,
                                    snapshot_kind="pre", observed_at=K + at))  # fmt: skip
    w.s.commit()


def test_the_market_is_the_last_pre_kickoff_price_de_margined_over_bookmakers(
    tmp_path: Path,
) -> None:
    w, mid = played_world(tmp_path, (2, 1))
    hour = timedelta(hours=1)
    _odds(w, mid, "A", "1X2", {"Home": 3.0, "Draw": 3.0, "Away": 3.0}, -3 * hour)  # replaced
    _odds(w, mid, "A", "1X2", {"Home": 1.8, "Draw": 3.6, "Away": 4.5}, -hour)
    _odds(w, mid, "A", "1X2", {"Home": 9.0, "Draw": 9.0, "Away": 1.1}, hour)  # after kickoff
    _odds(w, mid, "B", "1X2", {"Home": 2.0, "Draw": 3.4, "Away": 3.8}, -2 * hour)
    _odds(w, mid, "C", "1X2", {"Home": 1.5, "Draw": 4.0}, -hour)  # no away price: not used
    _odds(w, mid, "A", "OU", {"Over": 1.9, "Under": 1.9}, -hour, line=2.5)
    _odds(w, mid, "A", "OU", {"Over": 1.2, "Under": 4.0}, -hour, line=1.5)  # another line
    _odds(w, mid, "B", "BTTS", {"Yes": 1.7, "No": 2.1}, -hour)
    m = views.played(w.s, ctx(w), AFTER).matches[0]
    assert m.market is not None and m.market.bookmakers == 2

    def fair(prices: list[float]) -> np.ndarray:
        inv = 1 / np.asarray(prices)
        return np.asarray(inv / inv.sum())

    want = (fair([1.8, 3.6, 4.5]) + fair([2.0, 3.4, 3.8])) / 2
    got = np.array([m.market.home, m.market.draw, m.market.away])
    assert got == pytest.approx(want, abs=1e-12) and got.sum() == pytest.approx(1)
    assert m.market.over25 == pytest.approx(0.5)
    assert m.market.btts == pytest.approx(fair([1.7, 2.1])[0])
    assert m.market.scores.log_loss_1x2 == pytest.approx(metrics.log_loss(want, 0))
    assert m.market.scores.log_loss_ou25 == pytest.approx(np.log(2))  # 3 goals: over
    summary = views.played(w.s, ctx(w), AFTER).summary[0]
    vs = {x.metric: x for x in summary.vs_market}
    assert vs["log_loss_1x2"].mean == pytest.approx(
        m.scores.log_loss_1x2 - m.market.scores.log_loss_1x2
    )
    assert {x.metric for x in summary.market_scores} == set(views.PLAYED_METRICS)


def test_no_pre_kickoff_price_means_no_market(tmp_path: Path) -> None:
    w, mid = played_world(tmp_path)
    _odds(w, mid, "A", "1X2", {"Home": 1.8, "Draw": 3.6, "Away": 4.5}, timedelta(minutes=5))
    out = views.played(w.s, ctx(w), AFTER)
    assert out.matches[0].market is None
    assert out.summary[0].vs_market == [] and out.summary[0].market_scores == []


def test_closing_line_value_measures_the_move_towards_our_first_forecast(tmp_path: Path) -> None:
    """Owner, 2026-11-27: opening market at or after our first forecast, closing market at or
    before kickoff, per bookmaker with a later closing price; d = ours - open, m = close -
    open."""
    w, mid = played_world(tmp_path, (2, 1))
    first = views._pre_kickoff_forecasts(w.s, {mid: K}, first=True)[mid]
    p = {q.selection: q.probability for q in first[2] if q.market_type == "1X2"}
    ours = np.array([p["home"], p["draw"], p["away"]])
    gen = first[1]
    mins = timedelta(minutes=1)
    # A: an earlier price (before our forecast) is ignored; opens after it, then closes
    _odds(w, mid, "A", "1X2", {"Home": 9.0, "Draw": 9.0, "Away": 1.1}, gen - K - mins)
    _odds(w, mid, "A", "1X2", {"Home": 3.0, "Draw": 3.4, "Away": 2.4}, gen - K + mins)
    _odds(w, mid, "A", "1X2", {"Home": 2.6, "Draw": 3.4, "Away": 2.8}, -timedelta(hours=1))
    # B: one price only, so no movement to measure: not counted
    _odds(w, mid, "B", "1X2", {"Home": 2.0, "Draw": 3.4, "Away": 3.8}, -timedelta(hours=2))

    def fair(xs: list[float]) -> np.ndarray:
        inv = 1 / np.asarray(xs)
        return np.asarray(inv / inv.sum())

    q0, q1 = fair([3.0, 3.4, 2.4]), fair([2.6, 3.4, 2.8])
    out = views.played(w.s, ctx(w), AFTER)
    clv = out.matches[0].clv
    assert clv is not None and clv.bookmakers == 1
    assert clv.moved == pytest.approx((ours - q0) @ (q1 - q0))
    assert clv.gap == pytest.approx((ours - q0) @ (ours - q0))
    s = out.summary[0].clv
    assert s is not None and s.n == 1 and s.closed == pytest.approx(clv.moved / clv.gap)
    assert s.toward == (1.0 if clv.moved > 0 else 0.0)


def test_calibration_bins_and_the_running_gap_to_the_market(tmp_path: Path) -> None:
    w, mid = played_world(tmp_path, (0, 0))
    _odds(w, mid, "A", "1X2", {"Home": 2.0, "Draw": 3.4, "Away": 3.8}, -timedelta(hours=1))
    out = views.played(w.s, ctx(w), AFTER)
    m = out.matches[0]
    assert sum(b.n for b in out.reliability) == 3  # three outcomes of one match
    hit = [b for b in out.reliability if b.lo <= m.draw_p.p <= b.hi]
    assert any(b.observed > 0 for b in hit)
    assert m.market is not None
    assert out.trend == []  # one match: under TREND_MIN, so no running gap yet
    mp = pytest.MonkeyPatch()
    mp.setattr(views, "TREND_MIN", 1)
    try:
        [t] = views.played(w.s, ctx(w), AFTER).trend
    finally:
        mp.undo()
    assert t.n == 1
    assert t.diff == pytest.approx(m.scores.log_loss_1x2 - m.market.scores.log_loss_1x2)


def test_upcoming_forecasts_show_the_latest_market_before_now(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.refit(AS_OF)
    mid = w.fixture(K)
    w.tick(NOW)
    _odds(w, mid, "A", "1X2", {"Home": 2.0, "Draw": 3.4, "Away": 3.8}, NOW - K - timedelta(hours=1))
    _odds(w, mid, "A", "1X2", {"Home": 9.0, "Draw": 9.0, "Away": 1.1}, NOW - K + timedelta(hours=1))
    fl = views.fixture_list(w.s, ctx(w), NOW, end=K + timedelta(days=1))
    row = next(f for g in fl.competitions for f in g.fixtures if f.match_id == mid)
    assert row.market is not None and row.market.home > row.market.away  # the earlier price
    assert views.match_view(w.s, ctx(w), mid, NOW).market == row.market
    assert views.match_view(w.s, ctx(w), mid, AFTER).market is None  # not once it has started
