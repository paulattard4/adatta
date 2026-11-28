"""Improvement 3: the quick signal check. A signal that moves goals is found, noise is not,
and values known after kickoff are refused. (On real dev data it reproduced P3-j step 1's
verdicts in about a minute: S_miss worth a full test, S_apm unlikely.)"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import polars as pl
from test_backtest import LEAGUES, config, matches

from adatta.eval import quick_check as qc


def _signal(
    df: pl.DataFrame, values: dict[tuple[int, int], float], late: bool = False
) -> pl.DataFrame:
    rows = [
        (m, t, v, k + (timedelta(hours=1) if late else -timedelta(hours=1)))
        for (m, t), v in values.items()
        for k in df.filter(pl.col("match_id") == m)["kickoff"]
    ]
    names = ("match_id", "team_id", "value", "known_at")
    types = (pl.Int64, pl.Int64, pl.Float64, pl.Datetime("us", "UTC"))
    return pl.DataFrame(rows, schema=list(zip(names, types, strict=True)), orient="row")


def test_a_signal_that_moves_goals_is_found_and_noise_is_not() -> None:
    df, cfg = matches(), config()
    rng = np.random.default_rng(0)
    # rebuild goals so a pre-match signal x shifts each side's rate by exp(0.4 x)
    x = rng.normal(0, 1, (df.height, 2))
    hg = rng.poisson(1.4 * np.exp(0.4 * x[:, 0]))
    ag = rng.poisson(1.1 * np.exp(0.4 * x[:, 1]))
    df = df.with_columns(hg=pl.Series(hg), ag=pl.Series(ag))
    real = {
        (int(m), int(h)): float(a)
        for m, h, a in zip(df["match_id"], df["home"], x[:, 0], strict=True)
    }
    real |= {
        (int(m), int(w)): float(b)
        for m, w, b in zip(df["match_id"], df["away"], x[:, 1], strict=True)
    }
    noise = {k: float(rng.normal()) for k in real}
    seasons = [int(s) for s in sorted(df["season"].unique())[1:]]
    for values, want in ((real, "worth"), (noise, "unlikely")):
        d, late = qc.signal_columns(df, _signal(df, values))
        assert late == 0
        t = qc.run_bundle(d, list(LEAGUES), cfg, seasons)
        s = qc.summary(t, 1)
        assert s["verdict"].startswith(want), s
    d, late = qc.signal_columns(df, _signal(df, real, late=True))
    assert late == len(real) and (d["x_home"] == 0).all()  # known after kickoff: refused
