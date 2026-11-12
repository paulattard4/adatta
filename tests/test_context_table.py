"""The context table as a stored snapshot (Phase 2 F3): content-addressed storage, the join
into the backtest frame, and scoring targets at the T - 1 h and T - 48 h horizons."""

from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest
from test_backtest import LEAGUES, config, matches
from test_context import _rules, _setup
from test_covariates import CovFitter, _run, with_context

from adatta.adapters.storage import LocalStore
from adatta.features import context_table as ct
from adatta.model import backtest as bt
from adatta.model import covariates as cv

H1, H48 = timedelta(hours=1), timedelta(hours=48)
CTX_COLS = ["match_id", "ctx_as_of", "temperature_c", "wind_speed_kmh", "precipitation_mm"]


def snapshot(seed: int = 0, horizon: timedelta = H1) -> pl.DataFrame:
    df = with_context(matches(), seed).with_columns(ctx_as_of=pl.col("kickoff") - horizon)
    return df.select(CTX_COLS)


def test_snapshots_are_named_by_their_content(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    df = snapshot()
    d = ct.save(store, df, H1, "abc")
    assert d == ct.digest(df.sample(fraction=1.0, shuffle=True, seed=1))  # row order free
    assert ct.digest(snapshot(seed=1)) != d
    got, d2 = ct.load(store, H1)  # latest
    assert d2 == d and got.equals(df.sort("match_id"))
    assert ct.load(store, H1, d)[1] == d
    with pytest.raises(FileNotFoundError):
        ct.load(store, H48)
    store.put(ct.key(H1, d), _parquet(snapshot(seed=2)))  # content swapped
    with pytest.raises(ValueError, match="does not match"):
        ct.load(store, H1, d)


def _parquet(df: pl.DataFrame) -> bytes:
    import io

    buf = io.BytesIO()
    df.write_parquet(buf)
    return buf.getvalue()


def test_attach_keeps_the_frame_and_leaves_missing_rows_null() -> None:
    df = matches()
    ctx = snapshot().filter(pl.col("match_id") != 7)
    out = ct.attach(df, ctx)
    assert out.height == df.height and out.select(df.columns).equals(df)
    assert out.filter(pl.col("match_id") == 7)["ctx_as_of"][0] is None
    with pytest.raises(ValueError, match="after kickoff"):  # a missing row is refused
        cv.check_as_of(out)


def test_build_joins_context_and_style(tmp_path: Path) -> None:
    s, store, ids = _setup(tmp_path)
    df = ct.build(s, store, [ids["target"]], _rules(), H1)
    assert "as_of" not in df.columns
    row = df.row(0, named=True)
    kickoff = ids["target"].kickoff_time
    assert row["ctx_as_of"] == kickoff.replace(tzinfo=row["ctx_as_of"].tzinfo) - H1
    assert {"home_tempo_rel", "away_style_matches", "temperature_c"} <= set(df.columns)
    assert row["home_tempo_rel"] is None  # no statistics in this world: null, not 0


def test_backtest_scores_targets_at_either_horizon(tmp_path: Path) -> None:
    spec = bt.RunSpec(every_days=28, groups=("weather",), context="d1")
    df = ct.attach(matches(), snapshot())
    p1 = _run(df, spec, CovFitter(beta=-0.1), tmp_path / "a")
    hot = snapshot(seed=5, horizon=H48)  # a different (earlier) forecast for every target
    p48 = bt.run(df, LEAGUES, "england", config(), [2019], spec,
                 LocalStore(tmp_path / "b"), CovFitter(beta=-0.1), log=lambda _: None,
                 limit=3, target_context=hot, target_horizon=H48)  # fmt: skip
    assert p1["match_id"].equals(p48["match_id"])
    assert (p1["exp_total"] != p48["exp_total"]).any()
    assert set(p48["ctx_horizon_h"]) == {48.0} and set(p1["ctx_horizon_h"]) == {1.0}
    with pytest.raises(ValueError, match="after kickoff"):  # 1 h context is too late at 48 h
        bt.run(df, LEAGUES, "england", config(), [2019], spec,
               LocalStore(tmp_path / "c"), CovFitter(), log=lambda _: None, limit=1,
               target_context=snapshot(), target_horizon=H48)  # fmt: skip


def test_the_snapshot_digest_keys_the_fitted_draws() -> None:
    a = bt.RunSpec(groups=("rest",), context="aaaa")
    assert bt.config_hash(a) != bt.config_hash(bt.RunSpec(groups=("rest",), context="bbbb"))
    assert bt.config_hash(bt.RunSpec()) == bt.config_hash(bt.RunSpec(context=""))
