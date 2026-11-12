"""The Phase 2 per-group report (docs/PHASE2_TASKS.md acceptance, items 1-5) on a synthetic
backtest: a context-group challenger against the 1b reference on the same grid."""

from pathlib import Path

import numpy as np
import polars as pl
import pytest
from test_backtest import FakeFitter, matches
from test_covariates import CovFitter, _run, with_context

from adatta.eval import phase2
from adatta.model import backtest as bt
from adatta.model import covariates as cv


def predictions(tmp: Path, beta: float) -> pl.DataFrame:
    df = with_context(matches())
    ref = _run(df, bt.RunSpec(every_days=28), FakeFitter(), tmp / "ref")
    ch = _run(df, bt.RunSpec(every_days=28, groups=("rest",)), CovFitter(beta=beta), tmp / "ch")
    return pl.concat([ref, ch], how="diagonal_relaxed")


def test_rows_carry_active_flags_and_coefficients(tmp_path: Path) -> None:
    pred = predictions(tmp_path, -0.1)
    ch = pred.filter(pl.col("model").str.ends_with("_ctx-rest"))
    assert ch["active__rest"].dtype == pl.Boolean and 0 < ch["active__rest"].sum() < ch.height
    assert np.allclose(ch["coef__rest__short_rest__mean"].to_numpy(), -0.1)
    assert np.allclose(ch["coef__rest__long_break__sd"].to_numpy(), 0.0)
    # a match is active exactly when either side is on short rest or back from a long break
    df = with_context(matches())
    days = df.select("match_id", "home_days_since_last", "away_days_since_last")
    j = ch.join(days, on="match_id")
    expect = pl.any_horizontal(
        [(pl.col(c) < 4) | (pl.col(c) >= 14) for c in ("home_days_since_last",
                                                       "away_days_since_last")]
    ).fill_null(False)  # fmt: skip
    assert j.select((pl.col("active__rest") == expect).all()).item()


def test_report_applies_every_item(tmp_path: Path) -> None:
    pred = predictions(tmp_path, -0.1)
    rep = phase2.group_report(pred, "phase1b_strength_open_xg_ctx-rest", "phase1b_strength_open_xg")
    assert rep["same_grid"] and rep["metric"] == "1X2:log_loss" and rep["active_matches"] > 0
    items = {c["item"] for c in rep["checks"]}
    assert items == {1, 2, 3, 4}
    assert sum(c["name"].startswith("bundle ") for c in rep["checks"]) == 1  # one bundle here
    assert rep["verdict"] in ("pass", "fail")
    assert rep["optimistic"] is not None  # rest: D25/D26 leagues apart
    sense = {(s["bundle"], s["coefficient"]): s for s in rep["sense"]}
    s = sense[("england", "short_rest")]
    assert s["mean"] == pytest.approx(-0.1) and s["sign_ok"] is True and s["sd_ratio"] < 1e-9
    assert sense[("england", "long_break")]["expected_sign"] is None
    text = "\n".join(phase2.markdown(rep))
    assert "Phase 2 group rest" in text and "match_id" not in text  # metrics only


def test_a_harmless_zero_effect_fails_on_accuracy_only(tmp_path: Path) -> None:
    pred = predictions(tmp_path, 0.0)  # the challenger predicts exactly as the reference
    rep = phase2.group_report(pred, "phase1b_strength_open_xg_ctx-rest", "phase1b_strength_open_xg")
    failed = {c["item"] for c in rep["checks"] if not c["passed"]}
    assert failed == {2} and rep["verdict"] == "fail"


def test_one_group_per_challenger() -> None:
    with pytest.raises(ValueError, match="one group"):
        phase2.group_report(pl.DataFrame(), "m_ctx-rest-weather", "m")


def test_ece_is_zero_when_calibrated() -> None:
    p = np.repeat(np.linspace(0.05, 0.95, 10), 100)
    hit = np.concatenate([np.r_[np.ones(round(100 * q)), np.zeros(100 - round(100 * q))]
                          for q in np.linspace(0.05, 0.95, 10)])  # fmt: skip
    assert phase2.ece(p, hit) == pytest.approx(0.0, abs=1e-9)
    assert phase2.ece(np.full(100, 0.9), np.zeros(100)) == pytest.approx(0.9)


def test_input_columns_are_the_groups_inputs() -> None:
    assert cv.input_columns(["G1", "G7"]) == [
        "match_id", "ctx_as_of", "home_days_since_last", "away_days_since_last",
        "home_tempo_rel", "away_tempo_rel",
    ]  # fmt: skip
