"""P3-d lineup offset in the team model (L). Needs PyMC (model group, Linux / Docker, D13);
skipped elsewhere."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import polars as pl
import pytest

pytest.importorskip("pymc")

from test_backtest import LEAGUES, matches  # noqa: E402

from adatta.model import team_strength as ts  # noqa: E402


def _with_offsets(df: pl.DataFrame, rng: np.random.Generator) -> pl.DataFrame:
    n = df.height
    return df.with_columns(lu_home=pl.Series(rng.normal(0, 0.15, n)),
                           lu_away=pl.Series(rng.normal(0, 0.15, n)))  # fmt: skip


def test_lineup_off_adds_nothing_and_on_adds_beta() -> None:
    df = _with_offsets(matches(), np.random.default_rng(0))
    fd = ts.prepare(df, LEAGUES, df["kickoff"][900] + timedelta(days=1), 1095, 3.0)
    plain = ts.build(fd, structure="strength_open", xg=True)
    names = {v.name for v in plain.free_RVs}
    assert "beta_lineup" not in names
    lu = ts.build(fd, structure="strength_open", xg=True, lineup=True)
    assert {v.name for v in lu.free_RVs} - names == {"beta_lineup"}
    assert np.isfinite(lu.compile_logp()(lu.initial_point()))
    with pytest.raises(ValueError):
        ts.build(ts.prepare(matches(), LEAGUES, df["kickoff"][900], 1095, 3.0),
                 structure="strength_open", lineup=True)  # fmt: skip


def test_a_known_offset_is_recovered() -> None:
    """Goals simulated with log rate + 0.8 x offset: beta_lineup is clearly positive and
    near 0.8. The weekly strength walk can absorb part of an offset that varies match by
    match (first CI run: 90% interval up to 0.75), so the check is not an interval
    containing 0.8."""
    rng = np.random.default_rng(11)
    df = _with_offsets(matches(), rng)
    lam = 1.5 * np.exp(0.8 * df["lu_home"].to_numpy())
    mu_ = 1.1 * np.exp(0.8 * df["lu_away"].to_numpy())
    df = df.with_columns(hg=pl.Series(rng.poisson(lam)), ag=pl.Series(rng.poisson(mu_)))
    fd = ts.prepare(df, LEAGUES, df["kickoff"][-1] + timedelta(days=1), 1095, 3.0)
    idata = ts.sample(fd, seed=3, draws=300, chains=2, structure="strength_open", lineup=True)
    beta = ts.slim(idata)["beta_lineup"]
    lo = float(np.quantile(beta, 0.05))
    assert lo > 0.2 and 0.4 < float(beta.mean()) < 1.2
