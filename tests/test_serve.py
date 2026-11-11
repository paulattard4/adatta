"""Serving (PHASE1B_TASKS section 7): projections from stored draws equal the backtest's,
carry 90% intervals, follow the milestone schedule once and never late, use only a passing
refit from before `now`, and run without the model dependency group."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session
from test_backtest import LEAGUES, FakeFitter, config, matches

from adatta.adapters.storage import LocalStore
from adatta.db import models
from adatta.db.models import Base
from adatta.model import backtest as bt
from adatta.model import dixon_coles as dc
from adatta.model import live
from adatta.model import team_strength as ts
from adatta.sim import serve

PROD = "phase1b_strength_open_xg"
AS_OF = datetime(2026, 11, 9, tzinfo=UTC)  # a Monday
K = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)  # kickoff of the test fixture


def lively_draws(fd: ts.FitData, seed: int = 1) -> bt.Draws:
    """Fake posterior with non-zero rho and kappa per league, so every term is exercised."""
    draws, _ = FakeFitter()(fd, seed)
    rng = np.random.default_rng(seed)
    d = draws["rho"].shape[1]
    draws["rho"] = rng.uniform(-0.15, 0.05, (2, d))
    draws["kappa"] = rng.normal(0.1, 0.05, (2, d))
    draws["h_team"] = rng.normal(0, 1, draws["h_team"].shape)
    return draws


def fit_data() -> tuple[pl.DataFrame, ts.FitData]:
    df = matches()
    as_of = datetime(2019, 1, 7, tzinfo=UTC)
    return df, ts.prepare(df, LEAGUES, as_of, 1095, 3.0)


def test_score_matrices_match_the_scalar_function() -> None:
    rng = np.random.default_rng(0)
    lam, mu_ = rng.uniform(0.3, 3.0, 50), rng.uniform(0.3, 3.0, 50)
    rho, kappa = rng.uniform(-0.2, 0.2, 50), rng.normal(0, 0.3, 50)
    got = serve.score_matrices(lam, mu_, rho, kappa, 15)
    for i in range(50):
        want, _ = dc.score_matrix(lam[i], mu_[i], rho[i], 15, kappa[i])
        np.testing.assert_allclose(got[i], want, rtol=0, atol=1e-12)


def test_served_probabilities_equal_the_backtest(tmp_path: Path) -> None:
    df, fd = fit_data()
    draws = lively_draws(fd)
    later = df.filter(pl.col("kickoff") > datetime(2019, 1, 7, tzinfo=UTC)).head(12)
    # one unseen team (cold start: the league average)
    later = later.with_columns(pl.when(pl.int_range(pl.len()) == 0).then(999)
                               .otherwise(pl.col("home")).alias("home"))  # fmt: skip
    want = bt.score_targets(draws, fd, later, 15)
    # through the stored file: serving has only the draws and the refit index
    d2, fd2, meta = serve.decode_refit(serve.encode_refit(draws, fd, {"bundle": "england"}))
    assert meta["bundle"] == "england"
    fixtures = [serve.Fixture(r["match_id"], r["kickoff"], r["league_id"], r["home"], r["away"])
                for r in later.iter_rows(named=True)]  # fmt: skip
    got = serve.project(d2, fd2, fixtures, 15)
    for w, g in zip(want, got, strict=True):
        assert abs(g[("1X2", None, "home")][0] - w["p_home"]) < 1e-9
        assert abs(g[("1X2", None, "draw")][0] - w["p_draw"]) < 1e-9
        assert abs(g[("1X2", None, "away")][0] - w["p_away"]) < 1e-9
        assert abs(g[("OU", 2.5, "over")][0] - w["p_over25"]) < 1e-9
        assert abs(g[("BTTS", None, "yes")][0] - w["p_btts"]) < 1e-9


def test_intervals_contain_the_mean_and_markets_sum_to_one() -> None:
    _, fd = fit_data()
    draws = lively_draws(fd)
    fx = [serve.Fixture(1, datetime(2019, 1, 12, 15, tzinfo=UTC), 39, int(fd.team_ids[0]),
                        int(fd.team_ids[1]))]  # fmt: skip
    (p,) = serve.project(draws, fd, fx, 15)
    assert len(p) == 3 + 2 + 2 * len(serve.OU_LINES)
    for mean, lo, hi in p.values():
        assert 0.0 <= lo <= mean <= hi <= 1.0
        assert hi > lo  # the posterior is not a point
    groups: dict[tuple[str, float | None], float] = {}
    for (market, line, _), (mean, _, _) in p.items():
        groups[(market, line)] = groups.get((market, line), 0.0) + mean
    assert all(abs(v - 1.0) < 1e-12 for v in groups.values())


def test_serving_runs_without_the_model_group() -> None:
    code = (
        "import sys\n"
        "sys.path.insert(0, 'tests')\n"
        "from test_serve import fit_data, lively_draws\n"
        "from adatta.sim import serve\n"
        "_, fd = fit_data()\n"
        "fx = [serve.Fixture(1, fd.start, 39, int(fd.team_ids[0]), int(fd.team_ids[1]))]\n"
        "serve.project(lively_draws(fd), fd, fx, 15)\n"
        "bad = [m for m in ('pymc', 'pytensor', 'jax', 'numpyro', 'arviz') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-c", code], check=True, cwd=root)


# --------------------------------------------------------------------------- database


class World:
    """One bundle (leagues 39, 40) with a few teams, fixtures and stored refits."""

    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.store = LocalStore(tmp_path / "store")
        self.s = Session(self.engine)
        s = self.s
        s.add(models.Source(source_id="api_football"))
        self.season: dict[int, int] = {}
        for lg in LEAGUES:
            c = models.Competition(source_id="api_football", source_ref=str(lg), name=f"L{lg}",
                                   country="X")  # fmt: skip
            s.add(c)
            s.flush()
            se = models.Season(competition_id=c.competition_id, year=2026)
            s.add(se)
            s.flush()
            self.season[lg] = se.season_id
        for t in range(1, 7):
            s.add(models.Team(team_id=t, source_id="api_football", source_ref=str(t), name=f"T{t}"))
        s.add(models.ModelRun(model_name=PROD, component="phase1b", status="champion",
                              created_at=AS_OF, window="holdout", git_commit="x", config_hash="x",
                              lockfile_hash="x", data_snapshot_hash="x", seed=1))  # fmt: skip
        s.flush()
        self.n = 0

    def fixture(self, kickoff: datetime, league: int = 39, home: int = 1, away: int = 2,
                status: str = "NS", rnd: str = "Regular Season - 12") -> int:  # fmt: skip
        self.n += 1
        m = models.Match(source_id="api_football", source_ref=str(1000 + self.n),
                         season_id=self.season[league], home_team_id=home, away_team_id=away,
                         kickoff_time=kickoff, status=status, round=rnd)  # fmt: skip
        self.s.add(m)
        self.s.flush()
        return m.match_id

    def refit(self, as_of: datetime, status: str = "ok", bundle: str = "england") -> int:
        idx = np.array([0, 1, 2, 3, 4, 5])
        fd = ts.FitData(
            home=idx[[0, 2, 4, 1, 3, 5]], away=idx[[1, 3, 5, 0, 2, 4]],
            league=np.array([0, 0, 0, 1, 1, 1]), step=np.array([0, 0, 0, 1, 1, 1]),
            hg=np.zeros(6, np.int64), ag=np.zeros(6, np.int64), season_start=np.zeros(2, bool),
            team_ids=np.arange(1, 7), leagues=LEAGUES, start=as_of - timedelta(days=7),
            n_steps=2,
        )  # fmt: skip
        key = serve.refit_key("cfg", bundle, as_of)
        self.store.put(key, serve.encode_refit(lively_draws(fd), fd, {"bundle": bundle}))
        r = models.ModelRun(model_name=PROD, component="phase1b", status=status,
                            created_at=as_of, as_of=as_of, window="live", git_commit="x",
                            config_hash="cfg", lockfile_hash="x", data_snapshot_hash="d",
                            seed=1, trace_path=key, manifest={"bundle": bundle})  # fmt: skip
        self.s.add(r)
        self.s.flush()
        return r.model_run_id

    def tick(self, now: datetime) -> serve.ProjectReport:
        rep = serve.run_projection(self.s, self.store, {"england": LEAGUES}, PROD, 10,
                                   {"git_commit": "abc", "lockfile_hash": "l", "seed": 1},
                                   now=now)  # fmt: skip
        self.s.commit()
        return rep

    def milestones(self, match_id: int) -> list[str]:
        MP = models.MarketProjection
        rows = self.s.execute(
            select(MP.milestone, func.count()).where(MP.match_id == match_id)
            .group_by(MP.milestone)
        ).all()  # fmt: skip
        n_sel = 3 + 2 + 2 * len(serve.OU_LINES)
        assert all(c == n_sel for _, c in rows), rows  # each milestone written exactly once
        return sorted(m for m, _ in rows)


def test_milestones_once_in_their_windows(tmp_path: Path) -> None:
    w = World(tmp_path)
    run_id = w.refit(AS_OF)
    m = w.fixture(K)
    skipped = [
        w.fixture(K, status="PST"),
        w.fixture(K, rnd="Relegation Round"),
        w.fixture(K + timedelta(days=20)),
    ]  # postponed, not regular season, too far
    timeline = [
        (K - timedelta(days=10), ["initial"]),
        (K - timedelta(days=10) + timedelta(minutes=15), ["initial"]),
        (K - timedelta(days=6, hours=12), ["T-7d", "initial"]),
        (K - timedelta(days=6), ["T-7d", "initial"]),  # T-7d window has closed
        (K - timedelta(hours=21), ["T-24h", "T-7d", "initial"]),
        (K - timedelta(hours=20, minutes=50), ["T-24h", "T-7d", "initial"]),
        (K - timedelta(minutes=45), ["T-24h", "T-60m", "T-7d", "initial"]),
        (K - timedelta(minutes=5), ["T-24h", "T-60m", "T-7d", "close", "initial"]),
        (K + timedelta(minutes=1), ["T-24h", "T-60m", "T-7d", "close", "initial"]),
    ]
    for now, want in timeline:
        w.tick(now)
        assert w.milestones(m) == sorted(want), now
    assert all(w.milestones(x) == [] for x in skipped)
    pr = w.s.scalars(select(models.ProjectionRun)).first()
    assert pr is not None and pr.component_versions["england"]["model_run_id"] == run_id
    assert pr.git_commit == "abc" and pr.override_set_hash == serve.NO_OVERRIDES


def test_late_fixture_gets_initial_and_the_open_window_only(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.refit(AS_OF)
    m = w.fixture(K)
    w.tick(K - timedelta(hours=22))  # first seen inside the T-24h window
    assert w.milestones(m) == ["T-24h", "initial"]


def test_uses_the_last_passing_refit_from_before_now(tmp_path: Path) -> None:
    w = World(tmp_path)
    old = w.refit(AS_OF)
    w.refit(AS_OF + timedelta(days=7), status="failed")
    w.refit(AS_OF + timedelta(days=14))  # after `now`: not observable yet
    w.fixture(K)
    now = K - timedelta(days=5)
    rep = w.tick(now)
    pr = w.s.get(models.ProjectionRun, rep.projection_run_id)
    assert pr is not None and pr.component_versions["england"]["model_run_id"] == old
    assert any("failed" in p for p in rep.problems)
    stale = w.tick(AS_OF + timedelta(days=10))  # nothing due, so no check either
    assert stale.problems == []
    w.fixture(AS_OF + timedelta(days=11))
    assert any("older than" in p for p in w.tick(AS_OF + timedelta(days=10)).problems)


def test_no_refit_no_projection(tmp_path: Path) -> None:
    w = World(tmp_path)
    m = w.fixture(K)
    rep = w.tick(K - timedelta(days=3))
    assert rep.no_refit == 1 and rep.projection_run_id is None and w.milestones(m) == []


# --------------------------------------------------------------------------- live refit


def test_week_start_and_gates() -> None:
    assert live.week_start(datetime(2026, 11, 11, 13, tzinfo=UTC)) == AS_OF
    assert live.week_start(AS_OF) == AS_OF
    ok = {"rhat_max": 1.004, "ess_bulk_min": 900.0, "ess_tail_min": 800.0, "divergences": 0.0}
    assert live.gate_failures(ok) == []
    assert len(live.gate_failures(ok | {"divergences": 2.0, "ess_tail_min": 300.0})) == 2
    assert live.gate_failures({k: v for k, v in ok.items() if k != "rhat_max"})  # missing


def test_live_refit_records_resumes_and_waits_for_results(tmp_path: Path) -> None:
    w = World(tmp_path)
    df, cfg = matches(), config()
    as_of = live.week_start(datetime(2019, 1, 10, tzinfo=UTC))
    fake = FakeFitter()

    def fitter(fd: ts.FitData, seed: int) -> tuple[bt.Draws, bt.Diag]:
        draws, diag = fake(fd, seed)
        return draws, diag | {"ess_tail_min": 900.0}

    row = live.refit(w.s, w.store, cfg, "england", as_of, df, fitter, "abc")
    assert row.status == "ok" and row.trace_path is not None and fake.calls == 1
    assert row.manifest["bundle"] == "england" and row.manifest["champion_run"] is not None
    _, fd, meta = serve.decode_refit(w.store.get(row.trace_path))
    assert meta["as_of"] == as_of.isoformat() and fd.n_steps > 1
    assert live.refit(w.s, w.store, cfg, "england", as_of, df, fitter).model_run_id == \
        row.model_run_id and fake.calls == 1  # fmt: skip
    w.s.delete(row)
    w.s.flush()  # recorded row lost: the stored draws are reused, not refitted
    again = live.refit(w.s, w.store, cfg, "england", as_of, df, fitter)
    assert fake.calls == 1 and again.status == "ok"

    w.fixture(as_of - timedelta(days=1), status="NS")  # Sunday's result not in yet
    with pytest.raises(live.NotReady):
        live.refit(w.s, w.store, cfg, "england", as_of + timedelta(days=7), df, fitter)


def test_failed_gates_are_recorded_not_served(tmp_path: Path) -> None:
    w = World(tmp_path)
    df, cfg = matches(), config()
    as_of = live.week_start(datetime(2019, 1, 10, tzinfo=UTC))
    fake = FakeFitter()
    row = live.refit(w.s, w.store, cfg, "england", as_of, df, fake)  # no tail ESS reported
    assert row.status == "failed" and row.manifest["gate_failures"]
    runs, problems = serve.latest_refits(w.s, as_of + timedelta(days=1), PROD)
    assert "england" not in runs and problems


def test_only_the_champion_is_refitted(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.s.execute(delete(models.ModelRun))
    with pytest.raises(SystemExit, match="champion"):
        live.refit(w.s, w.store, config(), "england", AS_OF, matches(), FakeFitter())


def test_serving_problems_alert_once_a_day(tmp_path: Path) -> None:
    from adatta.capture.jobs import _alert_once

    w = World(tmp_path)
    msg = ["serving england: latest passing refit is as of 2026-11-09, older than 9 days"]
    assert _alert_once(w.s, "serve_alert", msg, K) == msg
    assert _alert_once(w.s, "serve_alert", msg, K + timedelta(hours=1)) == []
    assert _alert_once(w.s, "serve_alert", msg, K + timedelta(hours=21)) == msg
