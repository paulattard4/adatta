"""Opening the locked holdout (PHASE1B_TASKS holdout tooling acceptance test): opened once,
production spec and weekly cadence only, and the pre-registered gate (D18, D28). Synthetic
data only; nothing here reads the real holdout."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
from test_backtest import LEAGUES, FakeFitter, config, matches

from adatta.adapters.storage import LocalStore
from adatta.eval import harness
from adatta.eval import holdout as ho
from adatta.model import backtest as bt

PROD = "phase1b_strength_open_xg"


def quiet(_: str) -> None:
    pass


def test_claimed_once_per_run(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    with pytest.raises(harness.HoldoutLocked):
        ho.unlock(store, "1", PROD)  # nothing claimed yet
    ho.claim(store, "1", PROD, "abc")
    assert ho.claim(store, "1", PROD, "abc")["run_id"] == "1"  # a rerun of the same run
    with pytest.raises(harness.HoldoutLocked):
        ho.claim(store, "2", PROD, "abc")
    with pytest.raises(harness.HoldoutLocked):
        ho.unlock(store, "2", PROD)
    with pytest.raises(harness.HoldoutLocked):
        ho.unlock(store, "1", "phase1b_strength_open_xg_k2")
    assert ho.unlock(store, "1", PROD) == harness.HoldoutKey("1")


def test_plan_needs_key_holdout_seasons_and_weekly_cadence() -> None:
    df, cfg, key = matches((2022, 2023, 2024)), config(), harness.HoldoutKey("1")
    with pytest.raises(harness.HoldoutLocked):
        harness.refit_plan(df, cfg, [2024], 7)
    with pytest.raises(harness.HoldoutLocked):
        harness.refit_plan(df, cfg, [2023, 2024], 7, key)  # dev mixed in
    with pytest.raises(harness.HoldoutLocked):
        harness.refit_plan(df, cfg, [2023], 7, key)  # a key is for the holdout only
    with pytest.raises(harness.HoldoutLocked):
        harness.refit_plan(df, cfg, [2024], 28, key)
    plan = harness.refit_plan(df, cfg, [2024], 7, key)
    assert plan and all(df["season"][int(i)] == 2024 for _, tgt, _ in plan for i in tgt)


def test_run_scores_the_production_spec_only(tmp_path: Path) -> None:
    df, cfg, key = matches((2022, 2023, 2024)), config(), harness.HoldoutKey("1")
    store = LocalStore(tmp_path)
    for spec in (bt.RunSpec(every_days=7, prior_scale=2.0), bt.RunSpec(every_days=7, form=True)):
        with pytest.raises(harness.HoldoutLocked):
            bt.run(df, LEAGUES, "england", cfg, [2024], spec, store, FakeFitter(),
                   log=quiet, holdout=key)  # fmt: skip
    with pytest.raises(harness.HoldoutLocked):
        bt.run(df, LEAGUES, "england", cfg, [2024], bt.production_spec(cfg), store,
               FakeFitter(), log=quiet, limit=2, holdout=key)  # fmt: skip
    assert bt.production_spec(cfg).model_name == PROD


def cmp_table(overall: dict[str, tuple[float, float]], seg_rel: float) -> pl.DataFrame:
    rows = [{"segment_type": "all", "seg": "all", "metric": m, "rel": r, "rel_hi": hi}
            for m, (r, hi) in overall.items()]  # fmt: skip
    rows += [{"segment_type": "league_id", "seg": lg, "metric": m, "rel": -0.002, "rel_hi": 0.01}
             for lg in ("39", "40") for m in config().phase1b_gate.segment_metrics]  # fmt: skip
    rows[-1]["rel"] = seg_rel  # one league x market segment
    rows.append({"segment_type": "league_id", "seg": "39", "metric": "OU2.5:brier",
                 "rel": 0.05, "rel_hi": 0.08})  # reported, not gated (D28)  # fmt: skip
    return pl.DataFrame(rows)


def test_verdict_applies_the_preregistered_gate() -> None:
    gate = config().phase1b_gate
    good = {"1X2:log_loss": (-0.004, -0.001), "1X2:rps": (-0.007, -0.002)}
    status, checks = ho.verdict(cmp_table(good, 0.0099), gate)
    assert status == "champion" and all(c["passed"] for c in checks)
    status, checks = ho.verdict(cmp_table(good, 0.0101), gate)
    assert status == "rejected"
    assert [c["name"] for c in checks if not c["passed"]] == ["league 40 total:crps"]
    wide = good | {"1X2:rps": (-0.007, 0.0005)}  # interval includes 0
    assert ho.verdict(cmp_table(wide, 0.0), gate)[0] == "rejected"
    worse = good | {"1X2:log_loss": (0.001, 0.003)}
    assert ho.verdict(cmp_table(worse, 0.0), gate)[0] == "rejected"
    with pytest.raises(ValueError):
        ho.verdict(cmp_table(good, 0.0).filter(pl.col("metric") != "BTTS:log_loss"), gate)


def test_holdout_report_end_to_end(tmp_path: Path) -> None:
    df = matches((2022, 2023, 2024))
    cfg = config().model_copy(update={"bundles": {"england": LEAGUES}, "holdout_seasons": [2024]})
    store, key = LocalStore(tmp_path / "store"), harness.HoldoutKey("1")
    spec = bt.production_spec(cfg)
    pred = bt.run(df, LEAGUES, "england", cfg, [2024], spec, store, FakeFitter(), log=quiet,
                  holdout=key)  # fmt: skip
    parts = tmp_path / "parts"
    parts.mkdir()
    pred.with_columns(refit_every_days=pl.lit(7), plan_seasons=pl.lit("2024")).write_parquet(
        parts / f"{spec.model_name}_england_2024.parquet"
    )
    with pytest.raises(harness.HoldoutLocked):  # assembling the holdout needs the key
        bt.assemble(parts, tmp_path / "out", cfg, lambda _: df)
    out = bt.assemble(parts, tmp_path / "out", cfg, lambda _: df, holdout=key)
    gate = json.loads((out / "gate.json").read_text())
    manifest = json.loads((out / "manifest.json").read_text())
    assert gate["verdict"] in ("champion", "rejected") and manifest["holdout_run"] == "1"
    assert manifest["verdict"] == gate["verdict"] and (out / "reliability.csv").exists()
    assert "Pre-registered gate" in (out / "summary.md").read_text()

    two = cfg.model_copy(update={"bundles": {"england": LEAGUES, "spain": [140, 141]}})
    with pytest.raises(SystemExit, match="incomplete"):
        bt.assemble(parts, tmp_path / "out", two, lambda _: df, holdout=key)
