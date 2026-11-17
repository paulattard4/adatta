"""P3-c formation model (adatta/model/formation.py) on a synthetic table: it samples, its
probabilities are normalised and follow the team's shares, and the PPC is a quantile.
PyMC + NumPyro: runs in the CI model job (D13)."""

import numpy as np
import polars as pl
import pytest

pytest.importorskip("pymc")

from adatta.model import formation as mf  # noqa: E402


def table(n: int = 400, k: int = 4, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        habit = i % k
        y = habit if rng.random() < 0.7 else int(rng.integers(0, k))
        ts = 0.6 * np.eye(k)[habit] + 0.4 / k
        rows.append({"match_id": i, "team_id": i % 7, "league_ref": "39", "season": 2019,
                     "y": y, "tier": float(rng.normal(0, 0.3)), "home": float(i % 2),
                     "must_win": 0.0, "dead_rubber": 0.0, "new_coach_5": 0.0,
                     **{f"ts_{j}": ts[j] for j in range(k)},
                     **{f"cm_{j}": ts[j] for j in range(k)},
                     **{f"last_{j}": float(j == habit) for j in range(k)}})  # fmt: skip
    return pl.DataFrame(rows)


def test_the_model_samples_and_follows_the_habit() -> None:
    t = table()
    idata = mf.fit(t, 4, draws=100)
    post = mf.posterior(idata, n=20)
    p = mf.probs(post, t, 4).mean(axis=0)
    assert np.allclose(p.sum(axis=1), 1.0)
    habit = np.arange(len(t)) % 4
    assert (p.argmax(axis=1) == habit).mean() > 0.9
    q = mf.ppc_top(post, t, 4, np.random.default_rng(0))
    assert 0.0 <= q <= 1.0
