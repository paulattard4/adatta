"""P3-b model (adatta/model/selection.py) on a small synthetic table: it samples, its
predictions are coherent per team-match, and an unseen player gets the pooled mean.
PyMC + NumPyro: runs in the CI model job (D13)."""

import numpy as np
import polars as pl
import pytest

from adatta.features import selection as sel

pytest.importorskip("pymc")

from adatta.model import selection as ms  # noqa: E402


def table(n_tm: int = 24, pool: int = 16, seed: int = 0) -> tuple[pl.DataFrame, pl.DataFrame]:
    rng = np.random.default_rng(seed)
    rows, tms = [], []
    for k in range(n_tm):
        team = 1 + k % 2
        ew = rng.random(pool)
        order = np.argsort(-ew + rng.normal(0, 0.2, pool))
        y = np.full(pool, sel.OUT)
        y[order[:11]] = sel.START
        y[order[11:14]] = sel.BENCH
        for i in range(pool):
            r = {f: 0.0 for f in sel.FEATURES}
            r.update(ew_start=ew[i], ew_squad=min(1.0, ew[i] + 0.2), last_start=float(ew[i] > 0.5))
            mins = (
                90.0
                if y[i] == sel.START and rng.random() < 0.7
                else (
                    float(rng.integers(30, 89))
                    if y[i] == sel.START
                    else float(rng.integers(1, 30))
                    if y[i] == sel.BENCH and rng.random() < 0.6
                    else 0.0
                )
            )
            rows.append({"match_id": k, "team_id": team, "player_id": team * 100 + i,
                         "league_ref": "39" if k % 3 else "40", "season": 2019,
                         "pos": "G" if i == 0 else "M", "y": int(y[i]), "minutes_nt": mins,
                         **r})  # fmt: skip
        tms.append(
            {
                "match_id": k,
                "team_id": team,
                "league_ref": "39" if k % 3 else "40",
                "season": 2019,
                "bench_expected": 3.0,
                "new_start": 0,
                "new_bench": 0,
            }
        )
    return pl.DataFrame(rows), pl.DataFrame(tms)


def test_the_model_samples_and_predicts_coherently() -> None:
    rows, teams = table()
    d = ms.design(rows)
    idata = ms.fit(d, rows, draws=100)
    post = ms.posterior(idata, n=20)
    unseen = rows.filter(pl.col("match_id") == 0).with_columns(player_id=pl.col("player_id") + 7)
    for block in (rows.filter(pl.col("match_id") < 4), unseen):
        tm = teams.join(block.select("match_id", "team_id").unique(), on=["match_id", "team_id"])
        p, pmf = ms.predict(d, post, block, tm, {"39": (0.0, 0.0), "40": (0.0, 0.0)})
        assert np.allclose(pmf.sum(axis=1), 1.0)
        for _, g in block.with_row_index("i").group_by("match_id", "team_id"):
            i = g["i"].to_numpy()
            assert p[i, 0].sum() == pytest.approx(11.0, abs=1e-3)
            assert p[i, 1].sum() == pytest.approx(3.0, abs=1e-3)
    ppc = ms.ppc_starts(d, post, rows, np.random.default_rng(0))
    assert 0.0 <= ppc["ppc_starts_q"] <= 1.0
