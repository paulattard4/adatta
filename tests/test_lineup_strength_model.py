"""P3-j step 2 (D65): J is P with the lineup offset in the likelihood. Needs PyMC (model
group, Linux / Docker, D13); skipped elsewhere."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import numpy as np
import polars as pl
import pytest

pytest.importorskip("pymc")

import pymc as pm  # noqa: E402
from test_backtest import LEAGUES, matches  # noqa: E402

from adatta.model import lineup_strength as lsm  # noqa: E402
from adatta.model import team_strength as ts  # noqa: E402


def _fd() -> tuple[ts.FitData, np.ndarray, np.ndarray]:
    df = matches()
    rng = np.random.default_rng(5)
    xg = df.with_columns(xg_home=pl.Series(rng.gamma(4, 0.35, df.height)),
                         xg_away=pl.Series(rng.gamma(4, 0.3, df.height)))  # fmt: skip
    fd = ts.prepare(xg, LEAGUES, df["kickoff"][900] + timedelta(days=1), 1095, 3.0)
    n = len(fd.hg)
    return fd, rng.normal(size=(n, 2)), rng.normal(size=(n, 2))


def _normal(x: np.ndarray, mu: float, sd: float) -> float:
    return float(np.sum(-0.5 * ((x - mu) / sd) ** 2 - np.log(sd * np.sqrt(2 * np.pi))))


def test_j_is_p3ds_model_with_the_offset_up_to_the_priors() -> None:
    """At the same point, J's log density equals P3-d's lineup model (whose offset term is
    P's rates plus beta_lineup x lu) with lu = S beta and beta_lineup = 1, once each
    model's own coefficient prior is taken out. So J's copy of P cannot drift from P."""
    fd, sh, sa = _fd()
    beta = np.array([0.3, -0.2])
    j = lsm.build(fd, sh, sa)
    lu = ts.build(replace(fd, lu_home=sh @ beta, lu_away=sa @ beta), structure="strength_open",
                  xg=True, lineup=True)  # fmt: skip
    rng = np.random.default_rng(1)
    pj = j.initial_point()
    for k, v in pj.items():
        pj[k] = v + rng.normal(0, 0.1, np.shape(v))
    pj[lsm.BETA] = beta
    pl_ = {k: pj[k] for k in lu.initial_point() if k in pj}
    pl_["beta_lineup"] = np.array(1.0)
    assert set(lu.initial_point()) - set(pl_) == set()
    a = j.compile_logp()(pj) - _normal(beta, 0.0, lsm.PRIOR_SD)
    b = lu.compile_logp()(pl_) - _normal(np.array(1.0), 1.0, 0.5)
    assert a == pytest.approx(b, rel=1e-9, abs=1e-6)


def test_with_no_signals_j_is_p_plus_the_prior() -> None:
    fd, sh, _ = _fd()
    z = np.zeros_like(sh)
    j, p = lsm.build(fd, z, z), ts.build(fd, structure="strength_open", xg=True)
    pt_ = j.initial_point()
    pt_[lsm.BETA] = np.array([0.4, -0.1])
    pp = {k: pt_[k] for k in p.initial_point()}
    diff = j.compile_logp()(pt_) - p.compile_logp()(pp)
    assert diff == pytest.approx(_normal(np.array([0.4, -0.1]), 0.0, lsm.PRIOR_SD), abs=1e-6)
    assert {v.name for v in j.free_RVs} - {v.name for v in p.free_RVs} == {lsm.BETA}


def test_known_signal_effects_are_recovered() -> None:
    rng = np.random.default_rng(11)
    df = matches()
    sh, sa = rng.normal(size=(df.height, 2)), rng.normal(size=(df.height, 2))
    beta = np.array([-0.25, 0.2])
    df = df.with_columns(hg=pl.Series(rng.poisson(1.5 * np.exp(sh @ beta))),
                         ag=pl.Series(rng.poisson(1.1 * np.exp(sa @ beta))),
                         lu_home=pl.Series(np.arange(df.height, dtype=np.float64)),
                         lu_away=pl.Series(np.arange(df.height, dtype=np.float64)))  # fmt: skip
    fd = ts.prepare(df, LEAGUES, df["kickoff"][-1] + timedelta(days=1), 1095, 3.0)
    idx = fd.lu_home.astype(np.int64)  # type: ignore[union-attr]
    fd = replace(fd, lu_home=None, lu_away=None)
    with lsm.build(fd, sh[idx], sa[idx]):
        idata = pm.sample(draws=300, tune=300, chains=2, nuts_sampler="numpyro",
                          random_seed=3, target_accept=0.95, progressbar=False)  # fmt: skip
    b = lsm.slim(idata, 200)[lsm.BETA]
    assert b.shape == (2, 200)
    lo, hi = np.quantile(b, 0.025, axis=1), np.quantile(b, 0.975, axis=1)
    assert hi[0] < 0 < lo[1]
    assert np.all(np.abs(b.mean(axis=1) - beta) < 0.1)
