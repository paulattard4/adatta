"""Walk-forward harness: holdout lock, no leakage, baselines, summaries."""

from datetime import UTC, datetime, timedelta
from itertools import permutations

import numpy as np
import polars as pl
import pytest

from adatta.eval import harness

CFG = harness.load_config()


def league_df(seasons: range, seed: int = 1) -> pl.DataFrame:
    """Two-league bundle: 8 teams each, double round robin per season, weekly matchdays."""
    rng = np.random.default_rng(seed)
    strength = {t: rng.normal(0, 0.35) for t in range(1, 17)}
    rows, mid = [], 1
    for season in seasons:
        start = datetime(2000 + season - 2000, 8, 10, 15, tzinfo=UTC)
        for lg, teams in ((39, range(1, 9)), (40, range(9, 17))):
            for k, (h, a) in enumerate(permutations(teams, 2)):
                lam = np.exp(0.3 + strength[h] - strength[a])
                mu_ = np.exp(0.05 + strength[a] - strength[h])
                rows.append({
                    "match_id": mid, "league_id": lg, "season": season,
                    "kickoff": start + timedelta(days=7 * (k // 4)), "home": h, "away": a,
                    "round": "Regular Season - 1", "hg": int(rng.poisson(lam)),
                    "ag": int(rng.poisson(mu_)),
                })  # fmt: skip
                mid += 1
    return pl.DataFrame(rows).with_columns(pl.col("kickoff").dt.cast_time_unit("us"))


def test_holdout_seasons_are_refused() -> None:
    df = league_df(range(2022, 2025))
    with pytest.raises(harness.HoldoutLocked):
        harness.walk_forward(df, [39, 40], CFG, 365, seasons=[2023, 2024])


def test_future_results_never_change_predictions() -> None:
    df = league_df(range(2017, 2020))
    before = harness.walk_forward(df, [39, 40], CFG, 365, seasons=[2019])
    # plant absurd results dated after every target match: no prediction may move
    future = df.filter(pl.col("season") == 2019).with_columns(
        pl.col("kickoff") + pl.duration(days=400), pl.col("match_id") + 100_000,
        pl.lit(9, pl.Int64).alias("hg"), pl.lit(0, pl.Int64).alias("ag"),
        pl.lit(2020, pl.Int64).alias("season"),
    )  # fmt: skip
    after = harness.walk_forward(pl.concat([df, future]), [39, 40], CFG, 365, seasons=[2019])
    cols = ["match_id", "model", "p_home", "p_draw", "p_away", "p_over25", "p_btts"]
    assert before.select(cols).equals(after.select(cols))


def test_every_target_scored_by_every_model_and_dc_beats_base_rate() -> None:
    df = league_df(range(2016, 2020))
    pred = harness.walk_forward(df, [39, 40], CFG, 365, seasons=[2019])
    n_targets = df.filter(pl.col("season") == 2019).height
    assert pred.group_by("model").len()["len"].to_list() == [n_targets] * 3
    assert (pred["as_of"] <= pred["kickoff"]).all()
    assert (pred["n_prior_home"] > 0).all()
    s = harness.summarise(pred, {39: "england", 40: "england"})
    ll = s.filter((pl.col("seg") == "all") & (pl.col("metric") == "1X2:log_loss"))
    by = dict(zip(ll["model"].to_list(), ll["value"].to_list(), strict=True))
    assert by["dixon_coles"] < by["base_rate"]
    assert by["dixon_coles"] < by["league_poisson"]
    diff = harness.paired(pred, "1X2:log_loss", "dixon_coles", "base_rate")
    assert diff["hi"] < 0
    assert harness.reliability(pred).height > 0


def test_config_windows_are_disjoint_and_ordered() -> None:
    w, d, h = CFG.warmup_seasons, CFG.dev_seasons, CFG.holdout_seasons
    assert max(w) < min(d) and max(d) < min(h) and max(h) < CFG.live_from_season


def test_unseen_team_gets_its_league_average_not_the_bundle_average() -> None:
    # team 99 replaces team 16 in league 40 for 2019: no history until its first result
    df = league_df(range(2017, 2020)).with_columns(
        home=pl.when((pl.col("season") == 2019) & (pl.col("home") == 16)).then(99)
        .otherwise(pl.col("home")),
        away=pl.when((pl.col("season") == 2019) & (pl.col("away") == 16)).then(99)
        .otherwise(pl.col("away")),
    )  # fmt: skip
    pred = harness.walk_forward(df, [39, 40], CFG, 365, seasons=[2019], models_=("dixon_coles",))
    new = pred.filter((pl.col("n_prior_home") == 0) | (pl.col("n_prior_away") == 0))
    assert new.height > 0 and new["cold_start"].all()
    assert new.select(pl.col("p_home", "p_draw", "p_away").is_nan().any()).row(0) == (
        False, False, False,
    )  # fmt: skip
