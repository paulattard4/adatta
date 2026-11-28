"""D68 warm refits: the block split, the cold fallbacks, the state round trip, the mapping
onto the next refit's coordinates, P's hash, and the acceptance verdict. No PyMC here (the
sampler and the walk mapping are in test_warm_model.py)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from test_backtest import LEAGUES, FakeFitter, config, matches

from adatta.adapters.storage import LocalStore
from adatta.eval import harness
from adatta.eval import warm as ew
from adatta.model import backtest as bt
from adatta.model import warm as wm

T0 = datetime(2022, 11, 28, tzinfo=UTC)


def _state(passed: bool = True) -> wm.State:
    rng = np.random.default_rng(3)
    return wm.State(start=T0, season_start=np.array([False, True, False]),
                    coords={"team": [7, 8, 9], "league": [39]},
                    draws={"strength_z": rng.normal(size=(4, 3, 3)), "sigma_walk": rng.random(4)},
                    var={"sigma_walk_log__": np.array(0.2)}, step_size=0.31,
                    passed=passed)  # fmt: skip


def test_blocks_are_contiguous_and_cover_the_plan_once() -> None:
    plan = list(range(45))
    parts = [wm.blocks(plan, k, 4) for k in range(4)]
    assert sum(parts, []) == plan
    assert {len(p) for p in parts} <= {11, 12}
    assert wm.blocks(plan, 0, 1) == plan
    with pytest.raises(ValueError, match="part"):
        wm.blocks(plan, 4, 4)


def test_cold_start_unless_a_passing_source_within_four_weeks() -> None:
    s = _state()
    assert wm.gap_weeks(None, T0) is None
    assert wm.gap_weeks(_state(passed=False), T0 + timedelta(days=7)) is None
    assert [wm.gap_weeks(s, T0 + timedelta(days=7 * k)) for k in range(6)] == [0, 1, 2, 3, 4,
                                                                                None]  # fmt: skip
    assert wm.gap_weeks(s, T0 - timedelta(days=7)) is None


def test_state_round_trip() -> None:
    s = _state()
    t = wm.decode(wm.encode(s))
    assert (t.start, t.coords, t.step_size, t.passed) == (s.start, s.coords, s.step_size, True)
    assert np.array_equal(t.season_start, s.season_start)
    for k in s.draws:
        assert np.array_equal(t.draws[k], s.draws[k])
    assert np.array_equal(t.var["sigma_walk_log__"], s.var["sigma_walk_log__"])


def test_remap_shifts_steps_and_matches_teams_by_id() -> None:
    a = np.arange(12, dtype=float).reshape(4, 3)  # (step, team)
    old = {"team": [7, 8, 9], "step": list(range(4))}
    new = {"team": [9, 7, 5], "step": list(range(4))}  # 8 left, 5 is new
    out = wm.remap(a, ["step", "team"], old, new, off=1)
    # step s takes the source's step s + 1; the last step has no source: the mean of the rest
    assert out[:3, 0].tolist() == a[1:, 2].tolist() and out[:3, 1].tolist() == a[1:, 0].tolist()
    assert out[:3, 2].tolist() == ((a[1:, 2] + a[1:, 0]) / 2).tolist()  # new team: the mean
    assert np.allclose(out[3], out[:3].mean(axis=0))
    assert wm.remap(a, [None, "team"], old, old, off=0).tolist() == a.tolist()


def test_p_keeps_its_hash_and_a_warm_spec_has_its_own() -> None:
    p = bt.production_spec(config())
    w = replace(p, warm=True)
    assert bt.fit_hash(p) == bt.fit_hash(replace(p, warm=False))
    assert bt.fit_hash(w) != bt.fit_hash(p)
    assert w.model_name == p.model_name + "_warm"
    assert w not in bt.holdout_specs(config(), harness.HoldoutKey(run_id="t"))


def test_a_warm_run_splits_into_contiguous_blocks(tmp_path: Path) -> None:
    """Parts of a warm spec are consecutive refits; together they are the whole plan."""
    df, cfg = matches(), config()
    spec, store = bt.RunSpec(every_days=7, warm=True), LocalStore(tmp_path / "c")
    whole = bt.run(df, LEAGUES, "england", cfg, [2019], spec, store, FakeFitter(),
                   log=lambda _: None)  # fmt: skip
    times = sorted(set(whole["as_of"].to_list()))
    pieces = [sorted(set(bt.run(df, LEAGUES, "england", cfg, [2019], spec, store, FakeFitter(),
                                log=lambda _: None, part=(k, 3))["as_of"].to_list()))
              for k in range(3)]  # fmt: skip
    assert sum(pieces, []) == times
    assert all(p == times[times.index(p[0]) : times.index(p[0]) + len(p)] for p in pieces)


def _diags(z: float, fit_w: float, ok_w: bool = True, zmc: float = 2.5,
           sq: float = 1.0) -> list[dict[str, Any]]:  # fmt: skip
    out = []
    for i in range(20):
        out.append({"model": "P", "gates_ok": True, "fit_s": 450.0})
        out.append({"model": "W", "gates_ok": ok_w or i > 0, "fit_s": fit_w,
                    "warm_weeks": -1.0 if i == 0 else 1.0, "z_max": z,
                    "z_mc_max": zmc if i == 5 else 2.5, "z_mc_sq_sum": 110.0 * sq,
                    "n_params": 110.0})  # fmt: skip
    return out


def _pred(shift: float) -> pl.DataFrame:
    rng = np.random.default_rng(1)
    ll = rng.uniform(0.6, 1.4, 2000)
    rps = rng.uniform(0.1, 0.3, 2000)
    return pl.concat([
        pl.DataFrame({"match_id": range(2000), "model": "P", "1X2:log_loss": ll, "1X2:rps": rps}),
        pl.DataFrame({"match_id": range(2000), "model": "W", "1X2:log_loss": ll * (1 + shift),
                      "1X2:rps": rps * (1 + shift)}),
    ])  # fmt: skip


def test_verdict_passes_an_equal_faster_run_and_fails_each_check() -> None:
    v = ew.verdict(_pred(0.0002), _diags(0.04, 230.0))
    assert v["passed"] and v["warm"] == 19 and v["cold"] == 1 and v["z"]["replaced_form_pass"]
    assert "PASS" in ew.markdown(v)
    assert ew.verdict(_pred(0.0), _diags(0.25, 230.0))["passed"]  # raw sd: reported only
    assert not ew.verdict(_pred(0.0), _diags(0.04, 230.0, zmc=5.5))["passed"]  # 1a, one refit
    assert not ew.verdict(_pred(0.0), _diags(0.04, 230.0, sq=1.6))["passed"]  # 1a, pooled
    assert not ew.verdict(_pred(0.002), _diags(0.04, 230.0))["passed"]  # 1b
    assert not ew.verdict(_pred(0.0), _diags(0.04, 230.0, ok_w=False))["passed"]  # 2
    assert not ew.verdict(_pred(0.0), _diags(0.04, 400.0))["passed"]  # 3
    with pytest.raises(ValueError, match="refits"):
        ew.verdict(_pred(0.0), [g for g in _diags(0.04, 230.0) if g["model"] == "W"])
