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
    p = stored(w, mid)
    assert (m.home_p.p, m.draw_p.p, m.away_p.p) == (p[("1X2", None, "home")],
                                                    p[("1X2", None, "draw")],
                                                    p[("1X2", None, "away")])  # fmt: skip
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
    assert client.get("/api/played", params={"competition_id": 999}).json()["matches"] == []
