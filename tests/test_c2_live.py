"""C2 step 3 (D54): the live Phase 1a refit is the model the backtest scored ((a), (c))."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from test_c2 import C2L, c2_matches, config

from adatta.adapters.storage import LocalStore
from adatta.eval import c2, harness
from adatta.model import c2_live


def _one_week(df: pl.DataFrame, week: datetime) -> pl.DataFrame:
    """Targets only in the week from `week`, so walk_forward makes that one refit, cold."""
    end = week.timestamp() + 7 * 86400
    k = pl.col("kickoff").dt.epoch("s")
    return df.with_columns(target=pl.col("target") & (k >= week.timestamp()) & (k < end))


def _weeks(df: pl.DataFrame, cfg: harness.EvalConfig, n: int) -> list[datetime]:
    plan = harness.refit_plan(df, cfg, [2019], 7)
    return [plan[i][0] for i in np.linspace(0, len(plan) - 1, n).astype(int)]


def test_live_refit_reproduces_walk_forwards_phase1a_to_1e9() -> None:
    """(a): same training rows, same fit, same predictions as one walk_forward refit."""
    df, cfg = c2_matches(), config().model_copy(update={"max_goals": 20})
    sel = cfg.phase1a_selected
    for week in _weeks(df, cfg, 4):
        ref = harness.walk_forward(_one_week(df, week), C2L, cfg, sel.half_life_days, [2019],
                                   ("dixon_coles",), sel.team_prior_sd)  # fmt: skip
        assert ref.height and ref["as_of"].unique().to_list() == [week]
        fit = c2_live.fit_as_of(df, C2L, cfg, week, "portugal")
        assert fit.n_train == int(ref["n_train"][0])
        for r in ref.iter_rows(named=True):
            m, tail = fit.matrix(r["home"], r["away"], r["league_id"], cfg.max_goals)
            i, j = np.indices(m.shape)
            got = (m[i > j].sum(), np.trace(m), m[i < j].sum())
            assert np.allclose(got, (r["p_home"], r["p_draw"], r["p_away"]), rtol=0, atol=1e-9)
            assert tail == pytest.approx(r["tail_mass"], abs=1e-12)
            assert (fit.n_prior(r["home"]), fit.n_prior(r["away"])) == (
                r["n_prior_home"], r["n_prior_away"])  # fmt: skip


def test_the_warm_started_grid_agrees_to_the_optimiser_tolerance() -> None:
    """(a), against the multi-week walk_forward: each refit there starts from the previous
    week's optimum, so it stops at a slightly different point within L-BFGS's gtol (1e-6).
    Measured on the eight bundles' dev weeks: at most 2.3e-4 (raised with the owner)."""
    df, cfg = c2_matches(), config().model_copy(update={"max_goals": 20})
    sel = cfg.phase1a_selected
    grid = harness.walk_forward(df, C2L, cfg, sel.half_life_days, [2019], ("dixon_coles",),
                                sel.team_prior_sd)  # fmt: skip
    worst = 0.0
    for week in _weeks(df, cfg, 4):
        fit = c2_live.fit_as_of(df, C2L, cfg, week)
        for r in grid.filter(pl.col("as_of") == week).iter_rows(named=True):
            m, _ = fit.matrix(r["home"], r["away"], r["league_id"], cfg.max_goals)
            i, j = np.indices(m.shape)
            worst = max(worst, abs(float(m[i > j].sum()) - r["p_home"]))
    assert 0 < worst < 5e-4


def test_no_training_row_after_the_refit_time_or_before_its_summer_load() -> None:
    df, cfg = c2_matches(), config()
    week = _weeks(df, cfg, 2)[1]
    fit = c2_live.fit_as_of(df, C2L, cfg, week)
    lag = cfg.result_available_after_hours * 3600
    ok = df.filter((pl.col("kickoff").dt.epoch("s") + lag <= week.timestamp())
                   & (pl.col("available_at") <= week))  # fmt: skip
    assert fit.n_train == ok.filter(
        pl.col("kickoff").dt.epoch("s") >= week.timestamp() - cfg.lookback_days * 86400
    ).height  # fmt: skip
    with pytest.raises(ValueError, match="no training match"):
        c2_live.fit_as_of(df, C2L, cfg, datetime(2000, 1, 3, tzinfo=UTC))


def test_a_stored_fit_predicts_what_the_fit_did(tmp_path: Path) -> None:
    df, cfg = c2_matches(), config().model_copy(update={"max_goals": 20})
    week = _weeks(df, cfg, 2)[1]
    fit = c2_live.fit_as_of(df, C2L, cfg, week, "portugal")
    store = LocalStore(tmp_path)
    store.put("k.npz", c2_live.encode(fit, {"model": c2_live.MODEL}))
    back, meta = c2_live.decode(store.get("k.npz"))
    assert meta["bundle"] == "portugal" and back.as_of == week
    h, a = int(fit.team_ids[0]), int(fit.team_ids[1])
    for team in (h, 999_999):  # a team with no history gets its league's average
        m1, t1 = fit.matrix(team, a, 94, 20)
        m2, t2 = back.matrix(team, a, 94, 20)
        assert np.array_equal(m1, m2) and t1 == t2
    assert c2_live.gate_failures(back) == []


def test_c2_scope_and_code_move_the_config_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = c2_live.scoring_config()
    assert cfg.max_goals == 20
    h = c2_live.config_hash(cfg)
    other = cfg.model_copy(update={"max_goals": 15})
    assert c2_live.config_hash(other) != h
    monkeypatch.setattr(c2, "digest", lambda: "changed")
    assert c2_live.config_hash(cfg) != h
