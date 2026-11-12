"""Phase 2 context covariates (M1, M2): the coding of each group, the as-of contract,
prediction-time scoring with the target's own context row, and the 1b model unchanged
with no groups. Pure numpy: runs everywhere (the PyMC side is tests/test_covariate_model.py)."""

from datetime import timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from test_backtest import LEAGUES, FakeFitter, config, matches

from adatta.adapters.storage import LocalStore
from adatta.model import backtest as bt
from adatta.model import covariates as cv
from adatta.model import team_strength as ts

H = timedelta(hours=1)


def with_context(df: pl.DataFrame, seed: int = 0) -> pl.DataFrame:
    """Synthetic context for every row, observed at kickoff - 1 h."""
    rng = np.random.default_rng(seed)
    n = df.height
    nullable = rng.random(n) < 0.1  # some nulls in every group

    def holes(x: np.ndarray) -> pl.Series:
        vals = [None if k else float(v) for k, v in zip(nullable, x, strict=True)]
        return pl.Series(vals, dtype=pl.Float64)

    def num(x: np.ndarray) -> pl.Series:
        return pl.Series(np.asarray(x, dtype=float))

    labels = ["live", "dead_rubber", "must_win"]
    home_imp = [None if k else str(v) for k, v in zip(nullable, rng.choice(labels, n), strict=True)]
    return df.with_columns(
        ctx_as_of=pl.col("kickoff") - H,
        home_days_since_last=holes(rng.integers(2, 20, n)),
        away_days_since_last=num(rng.integers(2, 20, n)),
        home_travel_km=num(np.zeros(n)),
        away_travel_km=num(rng.uniform(0, 900, n)),
        home_tz_shift_h=num(np.zeros(n)),
        away_tz_shift_h=num(rng.integers(-1, 2, n)),
        home_altitude_diff_m=num(np.zeros(n)),
        away_altitude_diff_m=num(rng.uniform(-500, 800, n)),
        temperature_c=num(rng.uniform(-5, 35, n)),
        wind_speed_kmh=num(rng.uniform(0, 45, n)),
        precipitation_mm=num(rng.exponential(1, n)),
        referee_fouls_pm=num(rng.uniform(18, 30, n)),
        referee_matches=pl.Series(rng.integers(0, 60, n)),
        pool_fouls_pm=num(np.full(n, 24.0)),
        referee_basis=pl.Series(rng.choice(["snapshot", "d26", "pool_prior"], n)),
        home_importance=pl.Series(home_imp, dtype=pl.Utf8),
        away_importance=pl.Series(rng.choice(labels, n)),
        kickoff_hour_local=pl.Series(rng.integers(12, 22, n)),
        home_tempo_rel=holes(rng.normal(0, 0.06, n)),
        away_tempo_rel=num(rng.normal(0, 0.06, n)),
    )


class CovFitter(FakeFitter):
    """The fake posterior plus coefficient draws for each group in the fit."""

    def __init__(self, beta: float = 0.1) -> None:
        super().__init__()
        self.beta = beta

    def __call__(self, fd: ts.FitData, seed: int) -> tuple[bt.Draws, bt.Diag]:
        draws, diag = super().__call__(fd, seed)
        d = draws["mu_league"].shape[1]
        for b in fd.cov:
            draws[cv.beta_name(b.group)] = np.full((len(b.group.columns), d), self.beta)
        return draws, diag


def frame(**cols: list[object]) -> pl.DataFrame:
    return pl.DataFrame(cols)


def test_each_group_is_coded_as_the_plan_states() -> None:
    df = frame(
        home_days_since_last=[3.0, 14.0, None], away_days_since_last=[7.0, 2.0, 30.0],
        home_travel_km=[0.0, 0.0, None], away_travel_km=[100.0, 900.0, 0.0],
        home_tz_shift_h=[0.0, 0.0, 0.0], away_tz_shift_h=[-1.0, 0.0, 2.0],
        home_altitude_diff_m=[0.0, -300.0, 0.0], away_altitude_diff_m=[500.0, -200.0, None],
        temperature_c=[27.0, 0.0, None], wind_speed_kmh=[30.0, 10.0, None],
        precipitation_mm=[0.0, np.e - 1, None],
        home_importance=["dead_rubber", "live", None], away_importance=["must_win", None, "live"],
        kickoff_hour_local=[12, 16, 21],
        home_tempo_rel=[0.1, None, 0.0], away_tempo_rel=[0.05, 0.2, -0.1],
    )  # fmt: skip
    blocks = {b.group.name: b for b in cv.design(df, ["G1", "G2", "G3", "G5", "G6", "G7"])}
    rest = blocks["rest"]
    np.testing.assert_array_equal(rest.x_home, [[1, 0], [0, 1], [0, 0]])  # null -> 0
    np.testing.assert_array_equal(rest.x_away, [[0, 0], [1, 0], [0, 1]])
    tr = blocks["travel"].x_away
    np.testing.assert_allclose(tr[:, 0], [np.log(2), np.log(10), 0.0])
    np.testing.assert_allclose(tr[:, 1], [1.0, 0.0, 2.0])
    np.testing.assert_allclose(tr[:, 2], [0.5, 0.0, 0.0])  # climbing only; null -> 0
    w = blocks["weather"]
    np.testing.assert_allclose(w.x_home, np.array([[1, 0, 1, 0], [0, 1, 0, 1], [0, 0, 0, 0.0]]))
    assert w.x_home is w.x_away or np.array_equal(w.x_home, w.x_away)  # acts on both rates
    imp = blocks["importance"]
    np.testing.assert_array_equal(imp.x_home, [[1, 0], [0, 0], [0, 0]])
    np.testing.assert_array_equal(imp.x_away, [[0, 1], [0, 0], [0, 0]])
    np.testing.assert_array_equal(blocks["kickoff"].x_home, [[1, 0], [0, 0], [0, 1]])
    np.testing.assert_allclose(blocks["style"].x_home[:, 0], [0.15, 0.0, -0.1])  # null -> 0
    assert [b.group.name for b in cv.design(df, ["style", "G1"])] == ["rest", "style"]
    with pytest.raises(ValueError, match="unknown"):
        cv.resolve(["G9"])


def test_referee_pace_is_shrunk_to_the_pool_and_off_without_a_referee() -> None:
    df = frame(referee_fouls_pm=[30.0, 30.0, 30.0, None], referee_matches=[20, 0, 20, None],
               pool_fouls_pm=[24.0, 24.0, 24.0, 24.0],
               referee_basis=["snapshot", "snapshot", "pool_prior", "pool_prior"])  # fmt: skip
    x = cv.design(df, ["G4"])[0].x_home[:, 0]
    np.testing.assert_allclose(x, [(20 * 30 + 20 * 24) / 40 / 24 - 1, 0.0, 0.0, 0.0])
    assert cv.GROUPS["referee"].sd == 0.1 and cv.GROUPS["kickoff"].prior == "horseshoe"


def test_offsets_are_the_design_times_the_coefficient_draws() -> None:
    df = frame(home_days_since_last=[3.0, 20.0], away_days_since_last=[5.0, 3.0],
               home_tempo_rel=[0.1, 0.0], away_tempo_rel=[0.1, 0.2])  # fmt: skip
    blocks = cv.design(df, ["rest", "style"])
    draws = {"beta_rest": np.array([[-0.1, -0.2], [0.05, 0.0]]),
             "beta_style": np.array([[0.5, 1.0]])}  # fmt: skip
    oh, oa = cv.offsets(draws, blocks)
    np.testing.assert_allclose(oh, [[-0.1 + 0.1, -0.2 + 0.2], [0.05 + 0.1, 0.0 + 0.2]])
    np.testing.assert_allclose(oa, [[0.1, 0.2], [-0.1 + 0.1, -0.2 + 0.2]])


def test_context_observed_after_the_prediction_time_is_refused() -> None:
    df = with_context(matches())
    cv.check_as_of(df)
    late = df.with_columns(ctx_as_of=pl.when(pl.col("match_id") == 5)
                           .then(pl.col("kickoff")).otherwise(pl.col("ctx_as_of")))  # fmt: skip
    with pytest.raises(ValueError, match="after kickoff"):
        cv.check_as_of(late)
    with pytest.raises(ValueError, match="ctx_as_of"):
        cv.check_as_of(matches())
    as_of = df["kickoff"][900] + timedelta(days=1)
    with pytest.raises(ValueError, match="after kickoff"):  # a training row's context
        ts.prepare(late.filter(pl.col("match_id") >= 5), LEAGUES, as_of, 1095, 3.0, ["rest"])


def test_training_design_uses_only_rows_final_by_the_refit() -> None:
    df = with_context(matches())
    as_of = df["kickoff"][900] + timedelta(days=1)
    fd = ts.prepare(df, LEAGUES, as_of, 1095, 3.0, ["rest", "weather"])
    assert [b.group.name for b in fd.cov] == ["rest", "weather"]
    assert all(b.x_home.shape[0] == len(fd.hg) for b in fd.cov)
    # context of matches not yet final at as_of changes nothing in the fit
    later = pl.col("kickoff") + timedelta(hours=3) > as_of
    moved = df.with_columns(temperature_c=pl.when(later).then(40.0).otherwise("temperature_c"),
                            home_days_since_last=pl.when(later).then(1.0)
                            .otherwise("home_days_since_last"))  # fmt: skip
    fd2 = ts.prepare(moved, LEAGUES, as_of, 1095, 3.0, ["rest", "weather"])
    for a, b in zip(fd.cov, fd2.cov, strict=True):
        np.testing.assert_array_equal(a.x_home, b.x_home)
        np.testing.assert_array_equal(a.x_away, b.x_away)
    assert ts.prepare(df, LEAGUES, as_of, 1095, 3.0).cov == ()


def test_rates_with_an_offset_scale_by_its_exponential() -> None:
    df = matches()
    as_of = df["kickoff"][900] + timedelta(days=1)
    fd = ts.prepare(df, LEAGUES, as_of, 1095, 3.0)
    draws, _ = FakeFitter()(fd, 1)
    idx = np.array([0, 3, 5])
    league = np.array([0, 0, 1])
    lam, mu_ = ts.rates_from_draws(draws, fd, idx, idx[::-1], league)
    zero = (np.zeros((3, 20)), np.zeros((3, 20)))
    lz, mz = ts.rates_from_draws(draws, fd, idx, idx[::-1], league, offset=zero)
    np.testing.assert_array_equal(lam, lz)
    np.testing.assert_array_equal(mu_, mz)
    off = (np.full((3, 20), 0.1), np.full((3, 20), -0.2))
    lo, mo = ts.rates_from_draws(draws, fd, idx, idx[::-1], league, offset=off)
    np.testing.assert_allclose(lo, lam * np.exp(0.1))
    np.testing.assert_allclose(mo, mu_ * np.exp(-0.2))


def _run(df: pl.DataFrame, spec: bt.RunSpec, fitter: FakeFitter, tmp: Path) -> pl.DataFrame:
    return bt.run(df, LEAGUES, "england", config(), [2019], spec, LocalStore(tmp), fitter,
                  log=lambda _: None, limit=3)  # fmt: skip


SCORES = ["1X2:log_loss", "1X2:rps", "OU2.5:log_loss", "p_home", "exp_total"]


def test_no_groups_scores_the_1b_model_unchanged(tmp_path: Path) -> None:
    plain = _run(matches(), bt.RunSpec(every_days=28), FakeFitter(), tmp_path / "a")
    ctx = _run(with_context(matches()), bt.RunSpec(every_days=28), FakeFitter(), tmp_path / "b")
    assert plain.select(SCORES).equals(ctx.select(SCORES))
    # with context terms whose coefficients are 0, the scores are the 1b scores too
    spec = bt.RunSpec(every_days=28, groups=("rest", "weather"))
    zero = _run(with_context(matches()), spec, CovFitter(beta=0.0), tmp_path / "c")
    np.testing.assert_allclose(zero.select(SCORES).to_numpy(), plain.select(SCORES).to_numpy(),
                               rtol=1e-12)  # fmt: skip
    assert zero["model"][0].endswith("_ctx-rest-weather")


def test_targets_are_scored_with_their_own_context_row(tmp_path: Path) -> None:
    """Prediction time (M1): the refit's draws with the target's context at kickoff - 1 h."""
    spec = bt.RunSpec(every_days=28, groups=("weather",))
    df = with_context(matches())
    base = _run(df, spec, CovFitter(beta=-0.1), tmp_path / "a")
    target = int(base["match_id"][0])
    # a hotter target lowers its own expected total, and no other prediction moves
    hot = df.with_columns(temperature_c=pl.when(pl.col("match_id") == target).then(37.0)
                          .otherwise("temperature_c"))  # fmt: skip
    moved = _run(hot, spec, CovFitter(beta=-0.1), tmp_path / "b")
    changed = base["exp_total"].to_numpy() != moved["exp_total"].to_numpy()
    assert changed.sum() == 1 and bool(changed[0])
    assert moved["exp_total"][0] < base["exp_total"][0]
    # a context row observed after the target's prediction time is refused
    late = df.with_columns(ctx_as_of=pl.when(pl.col("match_id") == target)
                           .then(pl.col("kickoff")).otherwise("ctx_as_of"))  # fmt: skip
    with pytest.raises(ValueError, match="after kickoff"):
        _run(late, spec, CovFitter(), tmp_path / "c")


def test_config_hash_of_the_1b_spec_ignores_the_new_field() -> None:
    spec = bt.RunSpec()
    assert bt.config_hash(spec) != bt.config_hash(bt.RunSpec(groups=("rest",)))
    import hashlib
    import json
    from dataclasses import asdict

    fields = asdict(spec)
    del fields["groups"], fields["context"]  # fields added in Phase 2, empty for 1b
    del fields["grid"]  # added for D33, "weekly" for 1b
    del fields["widen"]  # added for D34, off for 1b
    blob = json.dumps({"spec": fields, "files": {f: bt._file_hash(f) for f in bt.HASHED_FILES}},
                      sort_keys=True)  # fmt: skip
    assert bt.config_hash(spec) == hashlib.sha256(blob.encode()).hexdigest()[:16]
