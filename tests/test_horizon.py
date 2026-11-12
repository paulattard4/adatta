"""Strength drift ahead of the last refit step (D34; docs/PHASE1B_TASKS.md section 7c,
acceptance item 1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from test_backtest import LEAGUES, FakeFitter, config, matches
from test_serve import fit_data, lively_draws

from adatta.adapters.storage import LocalStore
from adatta.model import backtest as bt
from adatta.model import horizon as hz
from adatta.model import team_strength as ts
from adatta.sim import serve

WALK = hz.Walk(seed=20261105)


def with_sigma(draws: bt.Draws, sw: float = 0.05) -> bt.Draws:
    d = dict(draws)
    d["sigma_walk"] = np.full(d["rho"].shape[-1], sw)
    return d


class WalkFitter(FakeFitter):
    def __call__(self, fd: ts.FitData, seed: int) -> tuple[bt.Draws, bt.Diag]:
        draws, diag = super().__call__(fd, seed)
        return with_sigma(draws), diag


def test_weeks_ahead_and_new_season() -> None:
    _, fd = fit_data()  # trained through the 2018 season
    fresh = hz.new_season(fd, np.array([2018, 2019]))
    assert fresh.tolist() == [False, True]
    w = hz.weeks_ahead(np.array([0, 2]), fresh)
    assert w.tolist() == [0.0, 2.0 + ts.RESET_WEEKS]
    assert hz.weeks_ahead(np.array([-1]), np.array([False])).tolist() == [0.0]


def test_offsets_are_zero_at_w0_deterministic_and_grow_with_w() -> None:
    _, fd = fit_data()
    d = with_sigma(lively_draws(fd))
    h, a = np.array([0, 0, 0, -1]), np.array([1, 1, 1, -1])
    oh, oa = hz.offsets(d, fd, h, a, np.array([0.0, 1.0, 9.0, 4.0]), WALK)
    assert not oh[0].any() and not oa[0].any()  # w = 0: nothing
    assert not oh[3].any()  # cold start on both sides: nothing
    assert np.allclose(oh[1:3], -oa[1:3])  # strength moves both rates in opposite ways
    assert np.isclose(oh[2].std() / oh[1].std(), 3.0)  # sd grows with sqrt(w)
    again = hz.offsets(d, fd, h, a, np.array([0.0, 1.0, 9.0, 4.0]), WALK)
    assert np.array_equal(oh, again[0])
    other = hz.offsets(d, fd, h, a, np.array([0.0, 1.0, 9.0, 4.0]), hz.Walk(seed=1))
    assert not np.array_equal(oh[1], other[0][1])
    # independent structure: attack and defence innovations are separate
    ih, ia = hz.offsets(d, fd, h, a, np.array([0.0, 4.0, 4.0, 0.0]),
                        hz.Walk(seed=1, structure="independent"))  # fmt: skip
    assert not np.allclose(ih[1], -ia[1]) and np.isclose(ih[1].std(), ia[1].std(), rtol=0.3)


def test_w0_targets_score_bit_identically(tmp_path: Path) -> None:
    df, fd = fit_data()
    d = with_sigma(lively_draws(fd))
    same_week = df.filter(
        (pl.col("kickoff") >= fd.start + timedelta(weeks=fd.n_steps - 1))
        & (pl.col("kickoff") < fd.start + timedelta(weeks=fd.n_steps))
        & (pl.col("season") == 2018)
    )
    assert same_week.height > 0
    plain = bt.score_targets(d, fd, same_week, 10)
    widened = bt.score_targets(d, fd, same_week, 10, walk=WALK)
    assert plain == widened


def test_serving_equals_the_backtest_with_widening() -> None:
    df, fd = fit_data()
    d = with_sigma(lively_draws(fd))
    later = df.filter(pl.col("kickoff") > fd.start + timedelta(weeks=fd.n_steps + 1)).head(8)
    want = bt.score_targets(d, fd, later, 15, walk=WALK)
    plain = bt.score_targets(d, fd, later, 15)
    fixtures = [
        serve.Fixture(r["match_id"], r["kickoff"], r["league_id"], r["home"], r["away"],
                      r["season"])
        for r in later.iter_rows(named=True)
    ]  # fmt: skip
    got = serve.project(d, fd, fixtures, 15, WALK)
    for w, p, g in zip(want, plain, got, strict=True):
        assert abs(g[("1X2", None, "home")][0] - w["p_home"]) < 1e-9
        assert abs(g[("OU", 2.5, "over")][0] - w["p_over25"]) < 1e-9
        assert w["p_home"] != p["p_home"]  # ahead of the refit: widened
    # draws stored before D34 have no sigma_walk: served as before
    old = serve.project(lively_draws(fd), fd, fixtures, 15, WALK)
    for p, g in zip(plain, old, strict=True):
        assert abs(g[("1X2", None, "home")][0] - p["p_home"]) < 1e-9


def test_widened_run_reports_against_the_plain_model(tmp_path: Path) -> None:
    df, cfg = matches(), config()
    parts = tmp_path / "parts"
    parts.mkdir()
    store = LocalStore(tmp_path / "c")
    for widen in (False, True):
        spec = bt.RunSpec(every_days=28, widen=widen)
        p = bt.run(df, LEAGUES, "england", cfg, [2019], spec, store, WalkFitter(),
                   log=lambda _: None)  # fmt: skip
        p.with_columns(refit_every_days=pl.lit(28)).write_parquet(parts / f"{widen}.parquet")
    assert bt.RunSpec(widen=True).model_name == "phase1b_strength_open_xg_hz"
    assert bt.fit_hash(bt.RunSpec(widen=True)) == bt.config_hash(bt.RunSpec())  # shared fits
    wide = pl.read_parquet(parts / "True.parquet")
    assert wide["walk_weeks"].max() >= 1 and (wide["walk_weeks"] == 0).any()  # type: ignore[operator]
    out = bt.assemble(parts, tmp_path / "out", cfg, lambda _: df, "phase1b_strength_open_xg")
    rep = json.loads((out / "horizon_phase1b_strength_open_xg_hz.json").read_text())
    unchanged = next(c for c in rep["checks"] if c["name"].startswith("unchanged"))
    assert unchanged["value"] == 0.0 and unchanged["passed"]
    assert rep["widened_matches"] > 0 and rep["verdict"] in ("pass", "fail")
    assert "Strength drift ahead of the refit" in (out / "summary.md").read_text()


def test_live_fixtures_carry_their_season() -> None:
    f = serve.Fixture(1, datetime(2026, 11, 21, tzinfo=UTC), 39, 1, 2, 2026)
    assert f.season == 2026
