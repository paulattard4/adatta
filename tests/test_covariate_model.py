"""Phase 2 context terms in the team model (M2). Needs PyMC (model group, Linux / Docker,
D13); skipped elsewhere."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import numpy as np
import polars as pl
import pytest

pytest.importorskip("pymc")

from test_backtest import LEAGUES, matches  # noqa: E402
from test_covariates import with_context  # noqa: E402

from adatta.model import covariates as cv  # noqa: E402
from adatta.model import team_strength as ts  # noqa: E402


def fit_data(groups: tuple[str, ...]) -> ts.FitData:
    df = with_context(matches())
    as_of = df["kickoff"][900] + timedelta(days=1)
    return ts.prepare(df, LEAGUES, as_of, 1095, 3.0, groups)


def test_no_groups_adds_nothing_to_the_model() -> None:
    plain = ts.build(fit_data(()), structure="strength_open", xg=True)
    names = {v.name for v in plain.free_RVs}
    assert not {n for n in names if n.startswith("beta_")}
    ctx = ts.build(fit_data(("rest", "kickoff")), structure="strength_open", xg=True)
    added = {v.name for v in ctx.free_RVs} - names
    assert added == {"beta_rest", "beta_kickoff_tau", "beta_kickoff_lambda", "beta_kickoff_c2",
                     "beta_kickoff_z"}  # fmt: skip
    assert np.isfinite(ctx.compile_logp()(ctx.initial_point()))


def test_a_known_effect_is_recovered() -> None:
    """Goals simulated with heat lowering both rates by 0.3 per unit: the weather group's
    heat coefficient is recovered within its 90% interval."""
    rng = np.random.default_rng(7)
    df = with_context(matches())
    heat = cv.design(df, ["weather"])[0].x_home[:, 0]
    lam = 1.5 * np.exp(-0.3 * heat)
    mu_ = 1.1 * np.exp(-0.3 * heat)
    df = df.with_columns(hg=pl.Series(rng.poisson(lam)), ag=pl.Series(rng.poisson(mu_)))
    fd = ts.prepare(df, LEAGUES, df["kickoff"][-1] + timedelta(days=1), 1095, 3.0, ["weather"])
    idata = ts.sample(fd, seed=3, draws=300, chains=2, structure="strength_open")
    beta = ts.slim(idata)["beta_weather"]  # (columns, draws)
    lo, hi = np.quantile(beta[0], [0.05, 0.95])
    assert lo < -0.3 < hi and hi < 0


def test_sampling_without_groups_ignores_the_new_code_path() -> None:
    fd = fit_data(())
    with_empty = replace(fd, cov=())
    a = ts.slim(ts.sample(fd, seed=5, draws=50, chains=1, structure="strength_open"))
    b = ts.slim(ts.sample(with_empty, seed=5, draws=50, chains=1, structure="strength_open"))
    assert a.keys() == b.keys()
    for k in a:
        np.testing.assert_array_equal(a[k], b[k])
