"""P3-j step 2 (D65): the driver's row alignment, the cache hash, the redo merge and the
go-live rule. No PyMC (the model itself: tests/test_lineup_strength_model.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest
from test_backtest import LEAGUES, matches

from adatta.eval import lineup_j as ej
from adatta.features import lineup_signals as sig
from adatta.model import lineup_j as lj
from adatta.model import team_strength as ts


def test_training_signals_follow_prepares_row_order() -> None:
    df = matches()
    rng = np.random.default_rng(0)
    cols = {f"j_{s}_lk_{side}": rng.normal(size=df.height) for s in sig.STEP2_SIGNALS
            for side in ("home", "away")}  # fmt: skip
    df = lj.with_row_index(df.with_columns(**{k: pl.Series(v) for k, v in cols.items()}))
    sh, sa = sig.side_signals(df, "home"), sig.side_signals(df, "away")
    as_of = df["kickoff"][700] + timedelta(days=1)
    fd0 = ts.prepare(df, LEAGUES, as_of, 1095, 3.0)
    fd, s_home, s_away = lj.training_signals(fd0, sh, sa)
    assert fd.lu_home is None and fd.lu_away is None
    # the same rows prepare took, in its order (kickoff + 3 h <= as_of, sorted by kickoff)
    d = df.filter(pl.col("kickoff") <= as_of - timedelta(hours=3)).sort("kickoff")
    assert d.height == len(fd.hg) == s_home.shape[0]
    by_id = dict(zip(df["match_id"], range(df.height), strict=True))
    order = fd0.lu_home.astype(np.int64)  # type: ignore[union-attr]
    assert np.array_equal(df["hg"].to_numpy()[order], fd.hg)
    assert np.array_equal(s_home, sh[order]) and np.array_equal(s_away, sa[order])
    assert set(order) == {by_id[m] for m in d["match_id"]}


def test_js_cache_hash_moves_with_the_table_and_sampler_but_p_stays() -> None:
    a = lj.j_hash("p1", "t1", 0.95, 2000)
    assert a == lj.j_hash("p1", "t1", 0.95, 2000)
    assert len({a, lj.j_hash("p2", "t1", 0.95, 2000), lj.j_hash("p1", "t2", 0.95, 2000),
                lj.j_hash("p1", "t1", 0.99, 4000)}) == 4  # fmt: skip


def _pred(gain: float, n: int = 400, seed: int = 0, worse_league: int | None = None,
          ) -> pl.DataFrame:  # fmt: skip
    rng = np.random.default_rng(seed)
    rows: list[dict[str, Any]] = []
    for i in range(n):
        lg = 39 if i % 2 else 140
        base = {m: float(rng.uniform(0.8, 1.2)) for m in ("1X2:log_loss", "1X2:rps",
                "OU2.5:log_loss", "BTTS:log_loss", "total:crps")}  # fmt: skip
        for model in ("P", "J_lk", "J_24"):
            f = 1.0 if model == "P" else 1.0 - gain
            if model != "P" and worse_league == lg:
                f = 1.02
            rows.append({"match_id": i, "league_id": lg, "season": 2020, "model": model,
                         "cold_start": False, **{k: v * f for k, v in base.items()}})  # fmt: skip
    return pl.DataFrame(rows)


def _diags(j_ok: bool = True, j_s: float = 600.0) -> list[dict[str, Any]]:
    d = {"rhat_max": 1.0, "ess_bulk_min": 1000.0, "ess_tail_min": 1000.0, "divergences": 0.0}
    return [{"model": "P", "fit_s": 500.0, "gates_ok": True, "cached": True, **d},
            {"model": "J", "fit_s": j_s, "gates_ok": j_ok, "redone": False, "cached": False,
             "bundle": "england", "as_of": "2020-01-06", **d}]  # fmt: skip


def test_j_goes_live_only_if_every_part_of_the_rule_holds() -> None:
    assert ej.verdict(_pred(0.002), _diags(), True).passed
    assert not ej.verdict(_pred(-0.002), _diags(), True).passed  # worse on average
    assert not ej.verdict(_pred(0.002, worse_league=39), _diags(), True).passed  # a segment
    assert not ej.verdict(_pred(0.002), _diags(j_ok=False), True).passed  # gates
    assert not ej.verdict(_pred(0.002), _diags(j_s=800.0), True).passed  # 1.6x P's time
    assert not ej.verdict(_pred(0.002), _diags(), False).passed  # leakage
    text = ej.markdown(_pred(0.002), ej.verdict(_pred(0.002), _diags(), True), _diags(),
                       {"signals": ["s_miss"], "scale": {"s_miss": 0.1}}, "abc")  # fmt: skip
    assert "Go live (shadow): yes" in text and "detectable gain" in text


def test_power_scales_with_the_spread_of_the_differences() -> None:
    p = ej.power(_pred(0.002, n=2000))
    assert p["n_dev"] == 2000 and p["detectable"] > 0
    q = ej.power(_pred(0.002, n=2000), n_live=4 * ej.LIVE_N)
    assert q["detectable"] == pytest.approx(p["detectable"] / 2)


def test_a_redo_replaces_only_js_rows_of_the_redone_refits() -> None:
    t1, t2 = datetime(2020, 1, 6, tzinfo=UTC), datetime(2020, 1, 13, tzinfo=UTC)
    base = pl.DataFrame({"as_of": [t1, t1, t2, t2], "model": ["P", "J_lk", "P", "J_lk"],
                         "v": [1.0, 2.0, 3.0, 4.0]})  # fmt: skip
    over = pl.DataFrame({"as_of": [t2, t2], "model": ["P", "J_lk"], "v": [9.0, 8.0]})
    bd = [{"model": m, "as_of": t.isoformat(), "gates_ok": m == "P" or t == t1}
          for t in (t1, t2) for m in ("P", "J")]  # fmt: skip
    od = [{"model": m, "as_of": t2.isoformat(), "gates_ok": True} for m in ("P", "J")]
    rows, diags = lj.merge_redo(base, bd, over, od)
    assert sorted(rows["v"].to_list()) == [1.0, 2.0, 3.0, 8.0]
    assert all(d["gates_ok"] for d in diags) and len(diags) == 4
