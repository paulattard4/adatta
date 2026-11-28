"""D68 warm refits with the model: the mapped starting point keeps each source path on the
weeks both refits share, and a warm refit follows a cold one (a failed source starts cold).
Needs PyMC (model group, Linux / Docker, D13); skipped elsewhere."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

pytest.importorskip("pymc")

import pymc as pm  # noqa: E402
import pytensor.tensor as pt  # noqa: E402
from test_backtest import LEAGUES, matches  # noqa: E402

from adatta.model import backtest as bt  # noqa: E402
from adatta.model import team_strength as ts  # noqa: E402
from adatta.model import warm as wm  # noqa: E402


def _fds(weeks: int = 1) -> tuple[ts.FitData, ts.FitData]:
    df = matches()
    rng = np.random.default_rng(5)
    df = df.with_columns(xg_home=pl.Series(rng.gamma(4, 0.35, df.height)),
                         xg_away=pl.Series(rng.gamma(4, 0.3, df.height)))  # fmt: skip
    t0 = df["kickoff"][900].replace(hour=0) + timedelta(days=1)
    t0 -= timedelta(days=t0.weekday())  # a Monday, as the weekly grid
    prep = [ts.prepare(df, LEAGUES, t, 1095, 3.0) for t in (t0, t0 + timedelta(weeks=weeks))]
    return prep[0], prep[1]


def _path(p: dict[str, np.ndarray], season_start: np.ndarray) -> np.ndarray:
    reset = np.where(season_start, np.sqrt(1.0 + ts.RESET_WEEKS), 1.0)
    innov = p["strength_z"] * (p["sigma_walk"] * reset)[:, None]
    path = p["strength_z0"][None, :] * p["sigma_strength"] + np.cumsum(innov, axis=0)
    return np.asarray(path, dtype=np.float64)


@pytest.mark.parametrize("weeks", [1, 2])
def test_the_mapped_start_keeps_each_path_on_the_shared_weeks(weeks: int) -> None:
    fd0, fd1 = _fds(weeks)
    m0 = ts.build(fd0, structure="strength_open", xg=True)
    m1 = ts.build(fd1, structure="strength_open", xg=True)
    names = [rv.name for rv in m0.free_RVs]
    drawn = pm.draw(m0.free_RVs, draws=4, random_seed=1)
    var = {v.name: np.full(np.shape(m0.initial_point()[v.name]), 0.5) for v in m0.value_vars}
    s = wm.State(fd0.start, fd0.season_start, wm._coords(m0),
                 {n: np.asarray(d, dtype=np.float64) for n, d in zip(names, drawn, strict=True)},
                 var, 0.2)  # fmt: skip
    off = wm.gap_weeks(s, fd1.start)
    assert off == weeks
    starts = wm.inits(s, m1, off)
    assert len(starts) == 4 and set(starts[0]) == set(names)
    shared = min(fd1.n_steps, fd0.n_steps - off)
    for c, p in enumerate(starts):
        old = _path({k: v[c] for k, v in s.draws.items()}, fd0.season_start)
        new = _path(p, fd1.season_start)
        assert np.allclose(new[:shared], old[off : off + shared], atol=1e-9)
        assert abs(p["strength_z0"].sum()) < 1e-9 and np.abs(p["strength_z"].sum(1)).max() < 1e-9
    # every chain starts at a finite density, and the mass matrix fits the parameters
    logp = m1.compile_logp()
    for p in starts:
        point = m1.initial_point()
        for rv in m1.free_RVs:
            v = m1.rvs_to_values[rv]
            t = m1.rvs_to_transforms.get(rv)
            point[v.name] = (t.forward(pt.as_tensor(p[rv.name]), *rv.owner.inputs).eval()
                             if t is not None else p[rv.name])  # fmt: skip
        assert np.isfinite(logp(point))
    im = wm.inverse_mass(s, m1, off)
    assert im.size == sum(np.size(x) for x in m1.initial_point().values()) and (im > 0).all()


def test_a_warm_refit_follows_a_cold_one_and_a_failed_source_starts_cold() -> None:
    fd0, fd1 = _fds()
    spec = replace(bt.RunSpec(), draws=300, chains=2, warm=True)
    chain = wm.Chain(spec)
    _, d0 = chain.fit(fd0, 7)
    assert d0["warm_weeks"] == -1 and d0["tune"] == 300
    chain.state = wm.decode(chain.state_bytes())  # as read back from the store
    chain.state.passed = True
    w, d1 = chain.fit(fd1, 7)
    assert d1["warm_weeks"] == 1 and d1["tune"] == wm.TUNE
    assert d1["divergences"] == 0 and d1["rhat_max"] < 1.05
    assert chain.state is not None
    chain.state.passed = False
    c, d2 = chain.fit(fd1, 8)
    assert d2["warm_weeks"] == -1 and d2["tune"] == 300
    agree = wm.agreement(w, c)  # warm and cold refits of the same week agree
    assert agree["z_max"] < 0.5, agree


def test_stored_variances_are_on_the_samplers_scale() -> None:
    """The state's variances (constrained draws through the model's transforms) equal those
    of the unconstrained trace PyMC keeps on request."""
    with pm.Model(coords={"team": [1, 2, 3, 4]}) as m:
        pm.ZeroSumNormal("z", 1.0, dims="team")
        pm.HalfNormal("s", 1.0)
        pm.Uniform("r", -0.2, 0.2)
        idata = pm.sample(draws=200, tune=200, chains=2, nuts_sampler="numpyro",
                          random_seed=3, progressbar=False,
                          nuts={"keep_untransformed": True})  # fmt: skip
    s = wm.extract(idata, m, datetime(2022, 1, 3, tzinfo=UTC), np.zeros(3, dtype=bool), 1,
                   var_draws=10_000)  # fmt: skip
    for v in m.value_vars:
        a = np.asarray(idata.posterior[v.name].values)
        assert np.allclose(s.var[v.name], a.reshape((-1, *a.shape[2:])).var(axis=0), rtol=1e-6)
    assert set(s.draws) == {"z", "s", "r"} and s.draws["z"].shape == (2, 4)
