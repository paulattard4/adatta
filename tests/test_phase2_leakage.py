"""Phase 2 combined model, item 3 (leakage), through the backtest harness end to end:
every context row the harness reads is observable at its prediction time (targets) or
its refit's as-of (training rows), and changing context observed after a prediction's
time leaves that prediction unchanged. The fake fitter's coefficients depend on the
training design, so context leaking into a fit would move the predictions too."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from test_backtest import LEAGUES, FakeFitter, config, matches
from test_covariates import with_context

from adatta.adapters.storage import LocalStore
from adatta.model import backtest as bt
from adatta.model import covariates as cv
from adatta.model import team_strength as ts

H = timedelta(hours=1)
GROUPS = ("style", "weather")  # G7, the group M4 carries, and one more with several terms


class DesignFitter(FakeFitter):
    """The fake posterior, with each coefficient set from its training design (column
    means), so the fitted draws change whenever any training row's context changes."""

    def __call__(self, fd: ts.FitData, seed: int) -> tuple[bt.Draws, bt.Diag]:
        draws, diag = super().__call__(fd, seed)
        d = draws["mu_league"].shape[1]
        for b in fd.cov:
            col = 0.1 + (b.x_home.mean(axis=0) + b.x_away.mean(axis=0)) / 2
            draws[cv.beta_name(b.group)] = np.repeat(col[:, None], d, axis=1)
        return draws, diag


def _run(df: pl.DataFrame, tmp: Path) -> pl.DataFrame:
    spec = bt.RunSpec(every_days=28, groups=GROUPS)
    return bt.run(df, LEAGUES, "england", config(), [2019], spec, LocalStore(tmp),
                  DesignFitter(), log=lambda _: None, limit=4)  # fmt: skip


def test_every_context_row_read_is_observable_at_its_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, datetime | None, pl.DataFrame]] = []
    refit: list[datetime] = []
    prepare, design = ts.prepare, cv.design

    def prepare_spy(df: pl.DataFrame, leagues: list[int], as_of: datetime,
                    *a: Any, **k: Any) -> ts.FitData:  # fmt: skip
        refit.append(as_of)
        try:
            return prepare(df, leagues, as_of, *a, **k)
        finally:
            refit.pop()

    def design_spy(df: pl.DataFrame, groups: Any) -> cv.Design:
        kind = "train" if refit else "target"
        seen.append((kind, refit[-1] if refit else None, df.select("kickoff", "ctx_as_of")))
        return design(df, groups)

    monkeypatch.setattr(ts, "prepare", prepare_spy)
    monkeypatch.setattr(cv, "design", design_spy)
    out = _run(with_context(matches()), tmp_path)

    train = [(a, f) for k, a, f in seen if k == "train"]
    targets = [f for k, _, f in seen if k == "target"]
    assert len(train) >= 3 and len(targets) == len(train) and out.height > 0
    lag = timedelta(hours=config().result_available_after_hours)
    for as_of, f in train:
        assert as_of is not None and f.height > 0
        assert (f["kickoff"] + lag <= as_of).all()  # final by the refit
        assert (f["ctx_as_of"] <= as_of).all()
    for f in targets:
        assert f.height > 0
        assert (f["ctx_as_of"] <= f["kickoff"] - H).all()  # observed by the prediction time


def test_context_changed_after_a_prediction_time_leaves_it_unchanged(tmp_path: Path) -> None:
    df = with_context(matches())
    base = _run(df, tmp_path / "a")
    times = base.select("match_id", "kickoff").sort("kickoff")
    cut = times["kickoff"][times.height // 2] - H  # predictions on both sides of it
    after = pl.col("ctx_as_of") > cut
    moved = df.with_columns(
        home_tempo_rel=pl.when(after).then(pl.col("home_tempo_rel") * 3 + 0.05)
        .otherwise("home_tempo_rel"),
        away_tempo_rel=pl.when(after).then(pl.col("away_tempo_rel") * 3 + 0.05)
        .otherwise("away_tempo_rel"),
        temperature_c=pl.when(after).then(pl.col("temperature_c") + 12.0)
        .otherwise("temperature_c"),
    )  # fmt: skip
    again = _run(moved, tmp_path / "b")
    key = ["match_id", "as_of"]
    both = base.select(*key, "kickoff", "exp_total", "p_home").join(
        again.select(*key, "exp_total", "p_home"), on=key, suffix="_moved"
    )
    assert both.height == base.height == again.height
    early = both.filter(pl.col("kickoff") - H <= cut)
    late = both.filter(pl.col("kickoff") - H > cut)
    assert early.height > 0 and late.height > 0
    for c in ("exp_total", "p_home"):
        assert early[c].to_list() == early[f"{c}_moved"].to_list()
    # the change is visible where it is allowed to be: the check has teeth
    assert (late["exp_total"] != late["exp_total_moved"]).any()
