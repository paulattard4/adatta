"""P3-d0 hyperparameter fit (adatta/model/rates.py) on a synthetic table: it samples, it
recovers the rate and the spread between players, the slot variant builds, and the PPC
gives quantiles. PyMC + NumPyro: runs in the CI model job (D13)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

pytest.importorskip("pymc")

from adatta.eval import rates as ev  # noqa: E402
from adatta.model import rates as mr  # noqa: E402

T0 = datetime(2019, 8, 3, 15, tzinfo=UTC)
LEAGUES = ("39", "40")
K, RATE, SUB = 3.0, 0.5, 0.4  # true spread, per-90 rate, substitute multiplier


def table(n_players: int = 300, weeks: int = 30, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    theta = rng.gamma(K, 1 / K, n_players)
    rows = []
    for w in range(weeks):
        for p in range(n_players):
            sub = (w + p) % 4 == 0
            e = 0.3 if sub else 1.0
            mu = RATE * theta[p] * e * (SUB if sub else 1.0)
            rows.append({"match_id": w * 1000 + p // 14, "team_id": p // 14, "player_id": p,
                         "league_ref": LEAGUES[(p // 14) % 2], "season": 2019,
                         "kickoff": T0 + timedelta(weeks=w), "sub": sub, "e": e,
                         "line": "F" if p % 2 else "M", "new_club": False, "ret": False,
                         "slot": "ST" if p % 2 else "CM",
                         "y_shots_on": int(rng.poisson(mu))})  # fmt: skip
    return pl.DataFrame(rows, schema_overrides={"kickoff": pl.Datetime("us", "UTC")})


def test_the_fit_recovers_rate_spread_and_substitute_term() -> None:
    d = ev.Design(LEAGUES)
    t = d.frame(table())
    tr = mr.train_arrays(t, "shots_on")
    assert len(tr.Y) == 300 and tr.Ycell.sum() == t["y_shots_on"].sum()
    idata = mr.fit(tr, d, draws=300)
    post = mr.posterior(idata, n=40)
    assert np.exp(post["log_k"][:, 2:].mean(axis=0)) == pytest.approx([K, K], rel=0.35)
    assert np.exp(post["log_mu"][:, 3].mean()) == pytest.approx(RATE, rel=0.15)
    assert np.exp(post["b"][:, 0].mean()) == pytest.approx(SUB, rel=0.2)
    assert abs(post["log_l"].mean()) < 0.1
    q = mr.ppc(post, tr, d, np.random.default_rng(0))
    assert q and all(0.0 <= v <= 1.0 for v in q.values())
    block = t.filter(pl.col("kickoff") == T0 + timedelta(weeks=29))
    b = ev.block_arrays(t, block, "shots_on")
    p = ev.predict(post, d, b, "shots_on")
    assert np.all((p.p0 > 0) & (p.p0 < 1)) and np.isfinite(p.log_loss).all()
    ph = mr.phis(post, d, tr, "shots_on", [2, 3])
    assert all(v > 5 for v in ph.values())  # Poisson within a player: a large NB size


def test_the_slot_variant_samples() -> None:
    d = ev.Design(LEAGUES, ("CM", "ST"))
    t = d.frame(table(n_players=60, weeks=10))
    tr = mr.train_arrays(t, "shots_on")
    post = mr.posterior(mr.fit(tr, d, draws=100), n=10)
    assert post["slot"].shape == (10, 2)
