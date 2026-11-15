"""Opening the locked holdout (PHASE1B_TASKS holdout tooling acceptance test): opened once,
production spec and weekly cadence only, and the pre-registered gate (D18, D28). Synthetic
data only; nothing here reads the real holdout."""

from __future__ import annotations

import json
from dataclasses import replace
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


def test_a_split_holdout_run_assembles_like_the_whole_run(tmp_path: Path) -> None:
    """Holdout jobs split in two (M5 prerequisite): same predictions, verdict and metrics."""
    df = matches((2022, 2023, 2024))
    cfg = config().model_copy(update={"bundles": {"england": LEAGUES}, "holdout_seasons": [2024]})
    store, key = LocalStore(tmp_path / "store"), harness.HoldoutKey("1")
    spec = bt.production_spec(cfg)
    name = f"{spec.model_name}_england_2024"

    def tag(p: pl.DataFrame) -> pl.DataFrame:
        return p.with_columns(refit_every_days=pl.lit(7), plan_seasons=pl.lit("2024"))

    whole = bt.run(df, LEAGUES, "england", cfg, [2024], spec, store, FakeFitter(), log=quiet,
                   holdout=key)  # fmt: skip
    pieces = [bt.run(df, LEAGUES, "england", cfg, [2024], spec, store, FakeFitter(), log=quiet,
                     holdout=key, part=(k, 2)) for k in range(2)]  # fmt: skip
    a, b = tmp_path / "whole", tmp_path / "split"
    a.mkdir()
    b.mkdir()
    tag(whole).write_parquet(a / f"{name}.parquet")
    for k, p in enumerate(pieces):
        tag(p).write_parquet(b / f"{name}{bt.part_suffix((k, 2))}.parquet")
    one = bt.assemble(a, tmp_path / "out_a", cfg, lambda _: df, holdout=key)
    two = bt.assemble(b, tmp_path / "out_b", cfg, lambda _: df, holdout=key)
    ga, gb = (json.loads((o / "gate.json").read_text()) for o in (one, two))
    assert ga["verdict"] == gb["verdict"] and len(ga["checks"]) == len(gb["checks"])
    for ca, cb in zip(ga["checks"], gb["checks"], strict=True):  # same up to summation order
        assert (ca["name"], ca["passed"]) == (cb["name"], cb["passed"])
        assert ca["value"] == pytest.approx(cb["value"], rel=1e-9, abs=1e-15)
    joined = pl.concat(pieces).drop("part", "cached").sort("match_id", "as_of")
    assert joined.equals(whole.drop("part", "cached").sort("match_id", "as_of"))
    (b / f"{name}{bt.part_suffix((1, 2))}.parquet").unlink()
    with pytest.raises(SystemExit, match="incomplete"):
        bt.assemble(b, tmp_path / "out_c", cfg, lambda _: df, holdout=key)


# ------------------------------------------------------------------ Phase 2 opening (D30)


def promo_cfg(groups: list[str] | None = None) -> harness.EvalConfig:
    cfg = config().model_copy(update={"bundles": {"england": LEAGUES}, "holdout_seasons": [2024]})
    promo = harness.Phase2Promotion(groups=groups if groups is not None else ["style"],
                                    context="abc123")  # fmt: skip
    return cfg.model_copy(update={"promotion": promo})


def test_phase2_opening_has_its_own_marker_after_the_1b_opening(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    ch = PROD + "_ctx-style"
    with pytest.raises(harness.HoldoutLocked, match="1b opening comes first"):
        ho.claim(store, "2", ch, "abc", "phase2", [ch, PROD])
    ho.claim(store, "1", PROD, "abc")
    first = (tmp_path / ho.MARKER).read_bytes()
    marker = ho.claim(store, "2", ch, "def", "phase2", [ch, PROD])
    assert marker["models"] == sorted([ch, PROD]) and marker["opening"] == "phase2"
    assert (tmp_path / ho.MARKER).read_bytes() == first  # the 1b marker is never touched
    assert ho.claim(store, "2", ch, "def", "phase2")["run_id"] == "2"  # a rerun of its job
    with pytest.raises(harness.HoldoutLocked, match="made once"):
        ho.claim(store, "3", ch, "def", "phase2", [ch, PROD])
    for m in (ch, PROD):
        assert ho.unlock(store, "2", m, "phase2") == harness.HoldoutKey("2", "phase2")
    with pytest.raises(harness.HoldoutLocked):
        ho.unlock(store, "2", PROD + "_ctx-weather", "phase2")
    with pytest.raises(harness.HoldoutLocked):
        ho.unlock(store, "2", PROD)  # the 1b opening belongs to run 1
    assert ho.unlock(store, "1", PROD) == harness.HoldoutKey("1")


def test_phase2_holdout_scores_only_the_pinned_specs(tmp_path: Path) -> None:
    from test_covariates import CovFitter, with_context

    cfg, df = promo_cfg(), with_context(matches((2022, 2023, 2024)))
    assert bt.promotion_spec(config()) is None  # empty in the shipped config
    promo = bt.promotion_spec(cfg)
    assert promo is not None and promo.groups == ("style",) and promo.every_days == 7
    store, key2 = LocalStore(tmp_path), harness.HoldoutKey("2", "phase2")
    for spec in (bt.production_spec(cfg), replace(promo, context="digest-of-columns")):
        out = bt.run(df, LEAGUES, "england", cfg, [2024], spec, store, CovFitter(),
                     log=quiet, holdout=key2)  # fmt: skip
        assert out.height > 0
    wrong = replace(promo, groups=("weather",))
    with pytest.raises(harness.HoldoutLocked, match="phase2 opening does not score"):
        bt.run(df, LEAGUES, "england", cfg, [2024], wrong, store, CovFitter(), log=quiet,
               holdout=key2)  # fmt: skip
    with pytest.raises(harness.HoldoutLocked, match="phase1b opening does not score"):
        bt.run(df, LEAGUES, "england", cfg, [2024], promo, store, CovFitter(), log=quiet,
               holdout=harness.HoldoutKey("1"))  # fmt: skip


def test_phase2_holdout_report_end_to_end(tmp_path: Path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from test_covariates import CovFitter, with_context

    from adatta.db import models

    cfg, df = promo_cfg(), with_context(matches((2022, 2023, 2024)))
    promo = bt.promotion_spec(cfg)
    assert promo is not None
    store, key = LocalStore(tmp_path / "store"), harness.HoldoutKey("2", "phase2")
    parts = tmp_path / "parts"
    parts.mkdir()
    for spec in (bt.production_spec(cfg), promo):
        pred = bt.run(df, LEAGUES, "england", cfg, [2024], spec, store, CovFitter(beta=0.05),
                      log=quiet, holdout=key)  # fmt: skip
        pred.with_columns(refit_every_days=pl.lit(7), plan_seasons=pl.lit("2024")).write_parquet(
            parts / f"{spec.model_name}_england_2024.parquet"
        )
    with pytest.raises(harness.HoldoutLocked):  # no other reference on the phase2 opening
        bt.assemble(parts, tmp_path / "out", cfg, lambda _: df, PROD, key)
    out = bt.assemble(parts, tmp_path / "out", cfg, lambda _: df, holdout=key)
    gate = json.loads((out / "gate.json").read_text())
    manifest = json.loads((out / "manifest.json").read_text())
    assert gate["model"] == promo.model_name and gate["reference"] == PROD
    assert gate["verdict"] in ("champion", "rejected") and gate["opening"] == "phase2"
    assert (gate["verdict"] == "champion") == bool(gate["routes"])
    assert {c["route"] for c in gate["checks"]} == {"A 1X2", "B total goals"}
    assert manifest["run"] == "phase2_holdout" and manifest["reference"] == PROD
    summary = (out / "summary.md").read_text()
    assert "Phase 2 promotion (D30, D36)" in summary and "combined-model gate" in summary

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        assert bt.record(out, s) == 1  # the challenger; the rescored reference is not recorded
        row = s.query(models.ModelRun).one()
        assert (row.model_name, row.component, row.window, row.status) == (
            promo.model_name,
            "phase2",
            "holdout",
            gate["verdict"],
        )

    (parts / f"{promo.model_name}_england_2024.parquet").unlink()
    with pytest.raises(harness.HoldoutLocked, match="holds exactly"):
        bt.assemble(parts, tmp_path / "out2", cfg, lambda _: df, holdout=key)
