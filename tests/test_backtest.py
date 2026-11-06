"""Phase 1b backtest runner (task 4 acceptance test), with a fake sampler: same grid as 1a,
no leakage, holdout refused, resumable from cache."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from adatta.adapters.storage import LocalStore
from adatta.eval import harness
from adatta.model import backtest as bt
from adatta.model import team_strength as ts

LEAGUES = [39, 40]


def matches(seasons: tuple[int, ...] = (2017, 2018, 2019)) -> pl.DataFrame:
    rng = np.random.default_rng(0)
    rows, mid = [], 0
    for season in seasons:
        start = datetime(season, 8, 7, 15, tzinfo=UTC)
        for week in range(36):
            for lg, base in ((39, 0), (40, 10)):
                teams = rng.permutation(np.arange(base, base + 10))
                for i in range(0, 10, 2):
                    mid += 1
                    rows.append({
                        "match_id": mid, "league_id": lg, "season": season,
                        "kickoff": start + timedelta(days=7 * week, hours=i),
                        "home": int(teams[i]) + 1000, "away": int(teams[i + 1]) + 1000,
                        "round": "Regular Season - 1",
                        "hg": int(rng.poisson(1.5)), "ag": int(rng.poisson(1.1)),
                    })  # fmt: skip
    return pl.DataFrame(rows)


class FakeFitter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, fd: ts.FitData, seed: int) -> tuple[bt.Draws, bt.Diag]:
        self.calls += 1
        n, d = len(fd.team_ids), 20
        rng = np.random.default_rng(seed)
        draws = {
            "attack_last": rng.normal(0, 0.1, (n, d)), "defence_last": rng.normal(0, 0.1, (n, d)),
            "mu_league": np.full((2, d), 0.2), "h_league": np.full((2, d), 0.25),
            "h_team": np.zeros((n, d)), "sigma_home": np.full(d, 0.05), "rho": np.zeros(d),
        }  # fmt: skip
        return draws, {"rhat_max": 1.001, "ess_bulk_min": 900.0, "divergences": 0.0}


def config() -> harness.EvalConfig:
    return harness.load_config().model_copy(update={"max_goals": 8})


def test_same_grid_as_phase1a_and_resumable_from_cache(tmp_path: Path) -> None:
    df, cfg = matches(), config()
    spec = bt.RunSpec(every_days=28)
    store, fake = LocalStore(tmp_path), FakeFitter()
    p1 = bt.run(df, LEAGUES, "england", cfg, [2019], spec, store, fake, log=lambda _: None)
    n_refits = fake.calls
    assert n_refits > 5 and p1.height > 0

    a = bt.phase1a_on_grid(df, LEAGUES, cfg, [2019], 28)
    assert set(zip(p1["match_id"], p1["as_of"], strict=True)) == set(
        zip(a["match_id"], a["as_of"], strict=True)
    )
    assert (p1["kickoff"] >= p1["as_of"]).all()
    for c in ("1X2:log_loss", "OU2.5:log_loss", "total:crps", "refit_rhat_max"):
        assert p1[c].is_finite().all()

    p2 = bt.run(df, LEAGUES, "england", cfg, [2019], spec, store, fake, log=lambda _: None)
    assert fake.calls == n_refits  # warm cache: zero fits
    assert p2["cached"].all()
    assert np.allclose(p1["p_home"].to_numpy(), p2["p_home"].to_numpy())


def test_refit_plan_has_no_leakage() -> None:
    df, cfg = matches(), config()
    kick = df["kickoff"].to_list()
    lag = timedelta(hours=cfg.result_available_after_hours)
    for as_of, tgt, train in harness.refit_plan(df, cfg, [2019], 28):
        assert all(kick[i] + lag <= as_of for i in train)
        assert all(as_of <= kick[i] < as_of + timedelta(days=28) for i in tgt)


def test_holdout_is_refused(tmp_path: Path) -> None:
    df = matches((2022, 2023, 2024))
    with pytest.raises(harness.HoldoutLocked):
        bt.run(df, LEAGUES, "england", config(), [2024], bt.RunSpec(), LocalStore(tmp_path),
               FakeFitter(), log=lambda _: None)  # fmt: skip


def test_cache_encoding_and_config_hash() -> None:
    draws, diag = FakeFitter()(ts.prepare(matches(), LEAGUES,
                                          datetime(2019, 9, 2, tzinfo=UTC), 1095), 1)  # fmt: skip
    d2, g2 = bt.decode(bt.encode(draws, diag))
    assert g2 == diag and all(np.array_equal(draws[k], d2[k]) for k in draws)
    assert bt.config_hash(bt.RunSpec()) != bt.config_hash(bt.RunSpec(prior_scale=2.0))
    assert bt.RunSpec(prior_scale=0.5).model_name == "phase1b_strength_open_k0.5"


def test_assemble_compares_on_the_same_grid(tmp_path: Path) -> None:
    df, cfg = matches(), config()
    parts = tmp_path / "parts"
    parts.mkdir()
    for k in (1.0, 2.0):
        spec = bt.RunSpec(every_days=28, prior_scale=k)
        p = bt.run(df, LEAGUES, "england", cfg, [2019], spec, LocalStore(tmp_path / "c"),
                   FakeFitter(), log=lambda _: None)  # fmt: skip
        p.with_columns(refit_every_days=pl.lit(28)).write_parquet(parts / f"{k}.parquet")
    out = bt.assemble(parts, tmp_path / "out", cfg, lambda _: df)
    summary = (out / "summary.md").read_text()
    assert "grid identical to 1a: **yes**" in summary
    assert "phase1b_strength_open vs Phase 1a" in summary
    assert "Dev predictive prior sensitivity" in summary
    assert "Promoted / relegated teams" in summary
    assert (out / "compare_phase1b_strength_open.csv").exists()


def test_record_writes_one_model_run_per_challenger(tmp_path: Path) -> None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from adatta.db import models

    df, cfg = matches(), config()
    parts = tmp_path / "parts"
    parts.mkdir()
    p = bt.run(df, LEAGUES, "england", cfg, [2019], bt.RunSpec(), LocalStore(tmp_path / "c"),
               FakeFitter(), log=lambda _: None)  # fmt: skip
    p.with_columns(refit_every_days=pl.lit(28)).write_parquet(parts / "a.parquet")
    out = bt.assemble(parts, tmp_path / "out", cfg, lambda _: df)
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        assert bt.record(out, s) == 1
        row = s.scalars(select(models.ModelRun)).one()
        assert row.model_name == "phase1b_strength_open" and row.window == "dev"
        assert "1X2:log_loss" in row.backtest_scores


def test_prepare_records_moves_and_pending_shift_applies() -> None:
    df = matches((2018, 2019))
    # swap team 1000 (tier 1) and 1010 (tier 2) for 2019: one promoted, one relegated
    swap = {1000: 1010, 1010: 1000}
    df = df.with_columns(
        [pl.when(pl.col("season") == 2019).then(pl.col(c).replace(swap)).otherwise(pl.col(c))
         .alias(c) for c in ("home", "away")]
    )  # fmt: skip
    fd = ts.prepare(df, LEAGUES, datetime(2019, 10, 7, tzinfo=UTC), 1095)
    assert fd.moved_up is not None and fd.moved_down is not None
    up, down = list(fd.team_ids).index(1010), list(fd.team_ids).index(1000)
    first_2019 = int((datetime(2019, 8, 7, tzinfo=UTC) - fd.start).days // 7)
    assert fd.moved_up[first_2019 - 1, up] == 0 and fd.moved_up[first_2019, up] == 1
    assert fd.moved_up[-1].sum() == 1 and fd.moved_down[-1, down] == 1
    # before the new season, the move is pending and applied at prediction time
    early = ts.prepare(df, LEAGUES, datetime(2019, 8, 5, tzinfo=UTC), 1095)
    assert early.moved_up is not None and early.moved_up.sum() == 0
    assert (1010, 2019) not in early.team_seasons
    draws, _ = FakeFitter()(early, 1)
    draws["delta"] = np.vstack([np.full(20, -0.4), np.full(20, 0.4)])
    i = list(early.team_ids).index(1010)
    args = (draws, early, np.array([i]), np.array([0]), np.array([0]))
    base_h, base_a = ts.rates_from_draws(*args)
    sh_h, sh_a = ts.rates_from_draws(*args, np.array([1]), np.array([0]))
    assert np.allclose(sh_h / base_h, np.exp(-0.4 / np.sqrt(2)))
    assert np.allclose(sh_a / base_a, np.exp(0.4 / np.sqrt(2)))


def test_assemble_matches_per_season_plans(tmp_path: Path) -> None:
    """Parts planned one season at a time must be compared with 1a planned the same way."""
    df, cfg = matches((2017, 2018, 2019, 2020)), config()
    # start 2020 two weeks later, so a joint 28-day grid is out of phase with its own
    df = df.with_columns(
        kickoff=pl.when(pl.col("season") == 2020)
        .then(pl.col("kickoff") + timedelta(days=14))
        .otherwise(pl.col("kickoff"))
    )
    parts = tmp_path / "parts"
    parts.mkdir()
    for season in (2019, 2020):
        p = bt.run(df, LEAGUES, "england", cfg, [season], bt.RunSpec(every_days=28),
                   LocalStore(tmp_path / "c"), FakeFitter(), log=lambda _: None)  # fmt: skip
        p.with_columns(refit_every_days=pl.lit(28), plan_seasons=pl.lit(str(season))
                       ).write_parquet(parts / f"{season}.parquet")  # fmt: skip
    joint = {a for a, _, _ in harness.refit_plan(df, cfg, [2019, 2020], 28)}
    single = {a for s in (2019, 2020) for a, _, _ in harness.refit_plan(df, cfg, [s], 28)}
    assert joint != single  # the phases really differ, so the test is meaningful
    out = bt.assemble(parts, tmp_path / "out", cfg, lambda _: df)
    assert "grid identical to 1a: **yes**" in (out / "summary.md").read_text()
