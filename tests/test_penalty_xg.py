"""P3-k (D71): Q is P on penalty-free xG. The adjustment, rule 8 for missing counts, Q's own
cache key beside P's cached refits, the one redo, and the rule with Q as challenger."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from test_backtest import LEAGUES, FakeFitter, matches

from adatta.adapters.storage import LocalStore
from adatta.eval import lineup_j as ej
from adatta.eval.selection import HoldoutRefused
from adatta.model import backtest as bt
from adatta.model import penalty_xg as pq


def _app(
    df: pl.DataFrame, pens: dict[tuple[int, int], int], missing: frozenset[int] = frozenset()
) -> pl.DataFrame:
    rows = []
    for r in df.iter_rows(named=True):
        for t in (r["home"], r["away"]):
            k = pens.get((r["match_id"], t), 0)
            rows.append({"match_id": r["match_id"], "team_id": t, "pen_scored": k, "pen_missed": 0,
                         "minutes_provider": None if r["match_id"] in missing else 90})  # fmt: skip
    return pl.DataFrame(rows, schema_overrides={"minutes_provider": pl.Int32})


def test_xg_loses_076_per_penalty_and_a_missing_count_drops_the_match() -> None:
    df = matches().head(6).with_columns(xg_home=pl.lit(1.5), xg_away=pl.lit(0.9))
    m0, m1, m2 = (int(x) for x in df["match_id"][:3])
    h0, a1 = int(df["home"][0]), int(df["away"][1])
    counts = pq.penalty_counts(_app(df, {(m0, h0): 2, (m1, a1): 1}, missing=frozenset({m2})))
    q = pq.penalty_free(df, counts)
    assert q["xg_home"][0] == pytest.approx(0.01)  # 1.5 - 1.52, floored
    assert q["xg_away"][1] == pytest.approx(0.9 - 0.76)
    assert q["xg_home"][1] == pytest.approx(1.5)
    assert q["xg_home"][2] is None and q["xg_away"][2] is None  # rule 8
    assert q.height == df.height and q["match_id"].to_list() == df["match_id"].to_list()


def test_q_is_fitted_beside_ps_cache_and_redone_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    df = matches().with_columns(xg_home=pl.lit(1.4), xg_away=pl.lit(1.1))
    dq = pq.penalty_free(df, pq.penalty_counts(_app(df, {})))
    monkeypatch.setattr(pq, "DEV_XG", (2019,))
    fake = FakeFitter()
    specs: list[Any] = []

    def nuts(spec: Any, train_means: bool = False) -> Any:
        def fit(fd: Any, seed: int) -> tuple[bt.Draws, bt.Diag]:
            specs.append((spec.target_accept, train_means))
            draws, diag = fake(fd, seed)
            bad = spec.target_accept < 0.99 and not train_means  # Q's first try fails
            return draws, diag | {"ess_tail_min": 900.0, "fit_s": 10.0,
                                  "rhat_max": 1.02 if bad else 1.001}  # fmt: skip

        return fit

    monkeypatch.setattr(bt, "nuts_fitter", nuts)
    store = LocalStore(tmp_path)
    rows, diags = pq.run_part(df, dq, list(LEAGUES), "england", 2019, store, (0, 8))
    q = [d for d in diags if d["model"] == "Q"]
    assert q and all(d["gates_ok"] and d["redone"] for d in q)
    assert {"P", "Q"} == set(rows["model"].unique())
    n_p = sum(1 for s in specs if s[1])
    rows2, diags2 = pq.run_part(df, dq, list(LEAGUES), "england", 2019, store, (0, 8))
    assert sum(1 for s in specs if s[1]) == n_p  # P and Q come from the cache the second time
    assert all(d["cached"] for d in diags2)
    with pytest.raises(HoldoutRefused):
        pq.run_part(df, dq, list(LEAGUES), "england", 2024, store, (0, 8))  # holdout refused
    v = ej.verdict(rows, diags, None, ch="Q", fitted="Q")
    assert not any("leakage" in c["check"] for c in v.checks)
    assert any(c["check"].startswith("Q vs P at T-60m, 1X2:log_loss") for c in v.checks)
    assert np.isfinite(v.checks[0]["value"])
