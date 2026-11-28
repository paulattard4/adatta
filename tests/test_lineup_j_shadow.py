"""P3-j step 3 (D65): J's live shadow. Pairs from refits of the same as-of time (comparator
(a)), the offset applied once from pre-kickoff XIs, rows written once per match x horizon x
model, rule 8 for missing signals, live refits following P's, and the verdict read once.
The API never reads the shadow table."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
import pytest
from sqlalchemy import select
from test_backtest import LEAGUES
from test_serve import AS_OF, PROD, World, lively_draws

from adatta.db import models
from adatta.eval import lineup_j_live as ej
from adatta.features import lineup_signals as sig
from adatta.model import team_strength as ts
from adatta.sim import lineup_j_shadow as ljs
from adatta.sim import serve

K = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)  # a fixture of the week after AS_OF
SCALE = {"s_miss": 0.1131, "s_rat": 0.0322}
MEANS = {"G": 6.7, "D": 6.7, "M": 6.8, "F": 6.8, "": 6.75}


def _fd(as_of: datetime) -> ts.FitData:
    idx = np.array([0, 1, 2, 3, 4, 5])
    return ts.FitData(
        home=idx[[0, 2, 4, 1, 3, 5]], away=idx[[1, 3, 5, 0, 2, 4]],
        league=np.array([0, 0, 0, 1, 1, 1]), step=np.array([0, 0, 0, 1, 1, 1]),
        hg=np.zeros(6, np.int64), ag=np.zeros(6, np.int64), season_start=np.zeros(2, bool),
        team_ids=np.arange(1, 7), leagues=LEAGUES, start=as_of - timedelta(days=7), n_steps=2,
    )  # fmt: skip


def j_refit(w: World, as_of: datetime, p_run: int, beta: tuple[float, float] = (-0.5, 0.4),
            status: str = "ok") -> int:  # fmt: skip
    """J's refit: P's draws (the same fake posterior) plus beta per signal."""
    fd = _fd(as_of)
    draws = lively_draws(fd)
    d = draws["rho"].shape[1]
    draws[ljs.BETA] = np.vstack([np.full(d, beta[0]), np.full(d, beta[1])])
    key = f"serving/lineup_j/h/england/{as_of:%Y%m%dT%H%M}.npz"
    w.store.put(key, serve.encode_refit(draws, fd, {"bundle": "england"}))
    r = models.ModelRun(model_name=ljs.MODEL_NAME, component=ljs.COMPONENT, status=status,
                        created_at=as_of, as_of=as_of, window=ljs.WINDOW, git_commit="x",
                        config_hash="h", lockfile_hash="x", data_snapshot_hash="d", seed=1,
                        trace_path=key, manifest={"bundle": "england", "p_run": p_run,
                                                  "diag": {"fit_s": 110.0}})  # fmt: skip
    w.s.add(r)
    w.s.flush()
    return r.model_run_id


def players(w: World) -> None:
    for t in (1, 2):
        for i in range(1, 15):
            pid = t * 100 + i
            w.s.add(models.Player(player_id=pid, source_id="api_football",
                                  source_ref=str(pid), name=f"P{pid}"))  # fmt: skip
    w.s.flush()


def basis(w: World, as_of: datetime) -> None:
    """Each team's usual XI is players t*100 + 1..11 (share 1), 12-14 rarely play."""
    u = pl.DataFrame([(t, t * 100 + i, 1.0 if i <= 11 else 0.1, i <= 11, 10)
                      for t in (1, 2) for i in range(1, 15)],
                     schema={"team_id": pl.Int64, "player_id": pl.Int64, "share": pl.Float64,
                             "usual": pl.Boolean, "n_prev": pl.UInt32}, orient="row")  # fmt: skip
    p = pl.DataFrame([(t * 100 + i, 6.8 if i <= 11 else 6.2, 10.0, "M")
                      for t in (1, 2) for i in range(1, 15)],
                     schema={"player_id": pl.Int64, "r_mean": pl.Float64, "r_n": pl.Float64,
                             "h_line": pl.Utf8}, orient="row")  # fmt: skip
    ljs.save_basis(w.store, u, p, {"as_of": as_of.isoformat(), "table": sig.J_TABLE,
                                   "scale": SCALE, "line_means": MEANS})  # fmt: skip


def snapshot(w: World, mid: int, team: int, seen: datetime, out: tuple[int, ...] = ()) -> None:
    """Team `team`'s announced XI: the usual one with `out` replaced by 12, 13, 14."""
    xi = [team * 100 + i for i in range(1, 12)]
    for k, i in enumerate(out):
        xi[xi.index(team * 100 + i)] = team * 100 + 12 + k
    w.s.add(models.LineupSnapshot(match_id=mid, team_id=team, source_id="api_football",
                                  observed_at=seen, starters=[{"player_ref": str(x), "pos": "M"}
                                                              for x in xi],
                                  substitutes=[]))  # fmt: skip
    w.s.flush()


def world(tmp_path: Path, j: bool = True) -> tuple[World, int, int]:
    w = World(tmp_path)
    players(w)
    p_run = w.refit(AS_OF)
    if j:
        j_refit(w, AS_OF, p_run)
    basis(w, K - timedelta(hours=10))
    mid = w.fixture(K)
    return w, mid, p_run


def tick(w: World, now: datetime) -> ljs.Report:
    rep = ljs.run(w.s, w.store, {"england": LEAGUES}, PROD, 10, now)
    w.s.commit()
    return rep


def rows(w: World, mid: int) -> dict[tuple[str, str], models.LineupJShadow]:
    S = models.LineupJShadow
    return {(r.horizon, r.model): r for r in w.s.scalars(select(S).where(S.match_id == mid))}


def test_lk_pairs_j_and_p_from_the_same_refit_once_both_xis_are_seen(tmp_path: Path) -> None:
    w, mid, p_run = world(tmp_path)
    snapshot(w, mid, 1, K - timedelta(minutes=62), out=(3, 7, 9))
    assert tick(w, K - timedelta(minutes=60)).written == {}  # one side only, not last call
    snapshot(w, mid, 2, K - timedelta(minutes=55))
    rep = tick(w, K - timedelta(minutes=50))
    assert rep.written == {"lk": 1}
    got = rows(w, mid)
    j, p = got[("lk", "J")], got[("lk", "P")]
    assert p.model_run_id == p_run and j.refit_as_of == p.refit_as_of
    assert j.lineup_seen_at is not None
    home = j.signals["home"]
    assert home["s_miss"] == pytest.approx(1 - 8.3 / 11) and not home["projected"]
    assert j.signals["away"]["s_miss"] == pytest.approx(0.0)
    assert home["s_rat"] < 0  # three weaker players in
    assert j.p_home < p.p_home  # beta_miss < 0: a weakened home XI scores less
    # P's row is the served model's projection from the same refit
    p_row = w.s.get(models.ModelRun, p_run)
    assert p_row is not None and p_row.trace_path is not None
    draws, fd, _ = serve.decode_refit(w.store.get(p_row.trace_path))
    fx = serve.upcoming(w.s, {lg: "england" for lg in LEAGUES}, K - timedelta(hours=1))
    m = serve.fixture_matrices(draws, fd, fx, 10)[0].mean(axis=0)
    assert np.allclose(np.asarray(p.matrix).reshape(m.shape), m)
    assert sum(j.matrix) == pytest.approx(1.0)
    assert tick(w, K - timedelta(minutes=35)).written == {}  # once per match x horizon


def test_a_lineup_seen_after_kickoff_is_never_read_and_missing_signals_are_zero(
        tmp_path: Path) -> None:  # fmt: skip
    w, mid, _ = world(tmp_path)
    snapshot(w, mid, 1, K + timedelta(minutes=5), out=(3, 7, 9))  # after kickoff
    rep = tick(w, K - timedelta(minutes=15))  # last call: no XI, no projection
    assert rep.written == {"lk": 1}
    got = rows(w, mid)
    sides = got[("lk", "J")].signals
    assert sides["home"]["projected"] and sides["home"]["x_s_miss"] == 0.0
    assert got[("lk", "J")].lineup_seen_at is None
    assert np.allclose(got[("lk", "J")].matrix, got[("lk", "P")].matrix)  # offset 0: J is P


def test_t24_reads_p3bs_projected_xi_inside_its_window(tmp_path: Path) -> None:
    w, mid, _ = world(tmp_path)
    run_at = K - timedelta(hours=30)
    basis(w, run_at)  # a basis from after the tick would never be read
    for t in (1, 2):
        for i in range(1, 15):
            p = 0.9 if i <= 11 else 0.2
            if t == 1 and i in (2, 4):  # P3-b expects two of the usual out
                p = 0.1
            w.s.add(models.LineupProjection(
                match_id=mid, team_id=t, player_id=t * 100 + i, generated_at=run_at,
                as_of=run_at, refit_key="k", refit_as_of=AS_OF, pos="M", p_start=p,
                p_bench=0.0, p_out=1 - p, emin_start=80.0, emin_bench=10.0, emin=70.0))  # fmt: skip
    w.s.flush()
    assert tick(w, K - timedelta(hours=26)).written == {}  # before the T-24h window
    assert tick(w, K - timedelta(hours=23)).written == {"24": 1}
    got = rows(w, mid)
    assert got[("24", "J")].signals["home"]["s_miss"] == pytest.approx(1 - 9.2 / 11)
    assert got[("24", "J")].lineup_seen_at is None


def test_no_pair_or_a_stale_basis_writes_nothing(tmp_path: Path) -> None:
    w, mid, _ = world(tmp_path, j=False)
    snapshot(w, mid, 1, K - timedelta(minutes=62))
    snapshot(w, mid, 2, K - timedelta(minutes=61))
    rep = tick(w, K - timedelta(minutes=50))
    assert rep.no_pair == 1 and not rows(w, mid)
    w2, mid2, p_run = world(tmp_path / "b")
    basis(w2, K - timedelta(days=3))
    snapshot(w2, mid2, 1, K - timedelta(minutes=62))
    snapshot(w2, mid2, 2, K - timedelta(minutes=61))
    rep = tick(w2, K - timedelta(minutes=50))
    assert rep.problems and "stale" in rep.problems[0] and not rows(w2, mid2)


def test_j_follows_ps_passing_refits_latest_first_then_every_newer_one(tmp_path: Path) -> None:
    from adatta.model import lineup_j_live as jl

    w = World(tmp_path)
    w.refit(AS_OF - timedelta(days=7))
    p2 = w.refit(AS_OF)
    w.refit(AS_OF + timedelta(days=2), status="failed")
    assert [r.model_run_id for r in jl.todo(w.s, PROD, "england")] == [p2]
    j_refit(w, AS_OF, p2)
    p4 = w.refit(AS_OF + timedelta(days=3))
    p5 = w.refit(AS_OF + timedelta(days=7))
    assert [r.model_run_id for r in jl.todo(w.s, PROD, "england")] == [p4, p5]
    j_refit(w, AS_OF + timedelta(days=7), p5, status="failed")
    assert jl.todo(w.s, PROD, "england") == []  # a failed J refit is not retried


Fit = tuple[dict[str, Any], dict[str, float]]


def test_a_failing_j_refit_is_redone_once_then_recorded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from test_backtest import config

    from adatta.model import lineup_j_live as jl

    w = World(tmp_path)
    p_run = w.refit(AS_OF)
    fd = _fd(AS_OF)
    monkeypatch.setattr(jl, "training", lambda *a, **k: (fd, np.zeros((6, 2)), np.zeros((6, 2))))
    calls: list[float] = []

    def fit(fd_: Any, sh: Any, sa: Any, seed: int, **kw: Any) -> Fit:
        calls.append(kw["target_accept"])
        draws = lively_draws(fd_)
        draws[ljs.BETA] = np.zeros((2, draws["rho"].shape[1]))
        bad = len(calls) == 1
        return draws, {"rhat_max": 1.02 if bad else 1.001, "ess_bulk_min": 900.0,
                       "ess_tail_min": 900.0, "divergences": 0.0, "fit_s": 100.0}  # fmt: skip

    meta2 = {"scale": SCALE, "line_means": MEANS}
    p_row = w.s.get(models.ModelRun, p_run)
    assert p_row is not None
    row = jl.refit(w.s, w.store, config(), "england", p_row, pl.DataFrame(), pl.DataFrame(),
                   meta2, fitter=fit)  # fmt: skip
    assert len(calls) == 2 and calls[1] == 0.99
    assert row.status == "ok" and row.manifest["redone"] and row.manifest["p_run"] == p_run
    assert row.as_of is not None and serve._utc(row.as_of) == AS_OF


def test_the_api_never_reads_the_shadow_table() -> None:
    root = Path(__file__).resolve().parents[1] / "adatta" / "api"
    for f in root.rglob("*.py"):
        src = f.read_text()
        assert "lineup_j_shadow" not in src and "LineupJShadow" not in src, f
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.ImportFrom) and n.module:
                assert "lineup_j" not in n.module, f


def _matrix(ph: float, pd_: float) -> list[float]:
    m = np.zeros((11, 11))
    m[1, 0], m[0, 0], m[0, 1] = ph, pd_, 1 - ph - pd_
    return [float(x) for x in m.ravel()]


def test_scores_and_the_routes(tmp_path: Path) -> None:
    s = ej.scores(_matrix(0.5, 0.3), 1, 0)
    assert s["ll_1x2"] == pytest.approx(-np.log(0.5))
    assert s["rps"] == pytest.approx(((0.5 - 1) ** 2 + (0.8 - 1) ** 2) / 2)
    assert s["ll_btts"] == pytest.approx(-np.log(1.0 - 1e-12), abs=1e-9)  # no BTTS mass, no BTTS
    rng = np.random.default_rng(3)
    recs = []
    for i in range(2000):
        hg, ag = int(rng.poisson(1.4)), int(rng.poisson(1.1))
        base = 0.45 if hg > ag else 0.3 if hg == ag else 0.25
        for model, tilt in (("J", 0.02), ("P", 0.0)):
            ph = 0.45 + (tilt if hg > ag else -tilt / 2)
            pd_ = 0.28 + (tilt if hg == ag else -tilt / 2)
            recs.append({"match_id": i, "league_id": 39 if i % 2 else 40, "hg": hg, "ag": ag,
                         "horizon": "lk", "model": model, "model_run_id": 1,
                         **ej.scores(_matrix(ph, pd_), hg, ag)})  # fmt: skip
        assert base > 0
    gate = ej.load_gate()
    v = ej.verdict(pl.DataFrame(recs), gate, 1.1, 0)
    assert v["horizons"]["lk"]["pairs"] == 2000
    assert v["route"] == "1" and v["checks"]["segments"]
    v = ej.verdict(pl.DataFrame(recs), gate, 1.6, 0)
    assert not v["pass"] and not v["checks"]["runtime"]


def test_the_verdict_is_read_once_after_the_window(tmp_path: Path) -> None:
    w = World(tmp_path)
    gate = ej.load_gate()
    early = datetime(2027, 5, 1, tzinfo=UTC)
    with pytest.raises(SystemExit):
        ej.judge(w.s, w.store, list(LEAGUES), early, gate)
    w.store.put(ej.MARKER, b"{}")
    with pytest.raises(SystemExit):
        ej.judge(w.s, w.store, list(LEAGUES), datetime(2027, 7, 1, tzinfo=UTC), gate)


def test_live_training_signals_align_with_ps_training_rows() -> None:
    """The live refit's training: P's rows as of the refit (as live.refit prepares them),
    each side's step 2 signals in fd's order, 0 where a side has no baseline."""
    from test_backtest import config, matches

    from adatta.model import lineup_j_live as jl

    df, cfg = matches(), config()
    rng = np.random.default_rng(5)
    recs = []
    for r in df.iter_rows(named=True):
        for t in (r["home"], r["away"]):
            out = set(rng.choice(np.arange(1, 12), size=int(rng.integers(0, 3)), replace=False))
            for i in range(1, 15):
                on = (i <= 11 and i not in out) or (i >= 12 and i - 11 <= len(out))
                recs.append({"match_id": r["match_id"], "team_id": t, "player_id": t * 100 + i,
                             "kickoff": r["kickoff"], "started": on, "pos": "M",
                             "minutes": 90 if on else 0,
                             "rating": float(rng.uniform(6, 8)) if on else None})  # fmt: skip
    app = pl.DataFrame(recs, schema_overrides={"minutes": pl.Int32})
    as_of = datetime(2019, 1, 7, tzinfo=UTC)
    b = next(k for k, v in cfg.bundles.items() if set(v) == set(LEAGUES))
    cfg = cfg.model_copy(update={"bundles": {b: list(LEAGUES)}})
    gmt = ZoneInfo("GMT")  # how the database returns as_of (polars won't compare it to UTC)
    fd, sh, sa = jl.training(df, app, cfg, b, as_of.astimezone(gmt), SCALE, MEANS)
    ref = ts.prepare(df, list(LEAGUES), as_of, cfg.lookback_days, cfg.result_available_after_hours)
    assert np.array_equal(fd.hg, ref.hg) and np.array_equal(fd.home, ref.home)
    assert fd.lu_home is None and sh.shape == (len(fd.hg), 2) == sa.shape
    assert (sh[:, 0] > 0).mean() > 0.3 and np.isfinite(sh).all()  # S_miss scaled where built
    assert (sh[:5] == 0).all()  # the window's first matches: no 10-match baseline yet
