"""Phase 1a baseline run: tune the half-life on the dev window, score every model, write the
report and a reproducibility manifest (CLAUDE.md; spec §14, §19.4)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy.orm import Session

from adatta.eval import harness

SEED = 0  # the fit is deterministic (L-BFGS from a fixed start); logged for the manifest


def _git_sha() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        return sha.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except OSError:
        return "unknown"


def _snapshot(frames: dict[str, pl.DataFrame]) -> dict[str, Any]:
    h = hashlib.sha256()
    n = 0
    for name in sorted(frames):
        df = frames[name].sort("match_id").select("match_id", "kickoff", "hg", "ag")
        h.update(name.encode())
        h.update(df.write_csv().encode())
        n += df.height
    return {"matches": n, "sha256": h.hexdigest()}


def run(session: Session, cfg: harness.EvalConfig, out_root: Path) -> Path:
    started = time.time()
    data: dict[str, pl.DataFrame] = {}
    unscorable: list[pl.DataFrame] = []
    for bundle, leagues in cfg.bundles.items():
        df, bad = harness.load_matches(session, leagues)
        data[bundle] = df
        unscorable.append(bad.with_columns(bundle=pl.lit(bundle)))
    bundle_of = {lid: b for b, ls in cfg.bundles.items() for lid in ls}
    # Everything below is computation: end the read transaction now, or a remote database
    # times the idle connection out (Neon's idle-in-transaction limit) mid-run.
    session.rollback()

    # 1. Tune the half-life on the dev window only (Dixon-Coles, warm matches, 1X2 log loss).
    # Selection uses all dev matches (warm and cold): the prior exists for the cold ones.
    tuning = []
    for hl in cfg.half_life_grid_days:
        for sd in cfg.team_prior_sd_grid:
            preds = [
                harness.walk_forward(data[b], ls, cfg, hl, cfg.dev_seasons, ("dixon_coles",), sd)
                for b, ls in cfg.bundles.items()
            ]
            p = pl.concat([x for x in preds if x.height])
            warm = p.filter(~pl.col("cold_start"))
            tuning.append({
                "half_life_days": hl, "prior_sd": sd, "n": p.height,
                "1X2:log_loss": p["1X2:log_loss"].mean(), "1X2:rps": p["1X2:rps"].mean(),
                "warm 1X2:log_loss": warm["1X2:log_loss"].mean(),
            })  # fmt: skip
    tune = pl.DataFrame(tuning, schema_overrides={"prior_sd": pl.Float64})
    top = tune.sort("1X2:log_loss").row(0, named=True)
    best, best_sd = float(top["half_life_days"]), top["prior_sd"]

    # 2. Final dev-window run, every model, chosen half-life.
    pred = pl.concat(
        [
            x.with_columns(bundle=pl.lit(b))
            for b, ls in cfg.bundles.items()
            if (
                x := harness.walk_forward(data[b], ls, cfg, best, cfg.dev_seasons, prior_sd=best_sd)
            ).height
        ]
    )
    summary = harness.summarise(pred, bundle_of)
    flags = harness.baseline_flags(summary)
    rel = harness.reliability(pred)
    paired = {
        f"{metric} dixon_coles - {base}": harness.paired(
            pred.filter(~pl.col("cold_start")), metric, "dixon_coles", base
        )
        for metric in ("1X2:log_loss", "1X2:rps")
        for base in ("league_poisson", "base_rate")
    }

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = out_root / stamp
    out.mkdir(parents=True, exist_ok=True)
    pred.write_parquet(out / "predictions.parquet")
    summary.write_csv(out / "metrics.csv")
    rel.write_csv(out / "reliability.csv")
    tune.write_csv(out / "tuning.csv")
    bad = pl.concat(unscorable)
    bad.write_csv(out / "unscorable.csv")
    manifest = {
        "run": "phase1a_baseline",
        "created_at": stamp,
        "code_sha": _git_sha(),
        "seed": SEED,
        "data_snapshot": _snapshot(data),
        "libraries": {lib: version(lib) for lib in ("numpy", "scipy", "polars", "sqlalchemy")},
        "config": cfg.model_dump(),
        "window_scored": "dev",
        "holdout_opened": False,
        "half_life_days": best,
        "team_prior_sd": best_sd,
        "runtime_seconds": round(time.time() - started, 1),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    (out / "summary.md").write_text(
        _summary_md(cfg, summary, tune, best, best_sd, paired, flags, pred, bad)
    )
    return out


def _summary_md(
    cfg: harness.EvalConfig, summary: pl.DataFrame, tune: pl.DataFrame, best: float,
    best_sd: float | None, paired: dict[str, dict[str, float]], flags: list[str],
    pred: pl.DataFrame, bad: pl.DataFrame,
) -> str:  # fmt: skip
    def table(seg_type: str) -> str:
        s = summary.filter(pl.col("segment_type") == seg_type).pivot(
            on="model", index=["seg", "metric", "n"], values="value"
        )
        cols = ["seg", "metric", "n", *harness.MODELS]
        lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for r in s.sort("seg", "metric").iter_rows(named=True):
            cells = (f"{r[c]:.4f}" if isinstance(r[c], float) else str(r[c]) for c in cols)
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)

    n_dc = pred.filter(pl.col("model") == "dixon_coles")
    cold = n_dc.filter(pl.col("cold_start")).height
    parts = [
        "# Phase 1a baseline — dev window",
        "",
        f"Dev seasons {cfg.dev_seasons}; locked holdout {cfg.holdout_seasons} **not opened**. "
        f"Weekly walk-forward, lookback {cfg.lookback_days} days, results usable "
        f"{cfg.result_available_after_hours:g}h after kickoff. Regular-season league matches.",
        "",
        f"Predictions: {n_dc.height} matches x {len(harness.MODELS)} models; cold start "
        f"(either team < {cfg.min_warmup_matches} prior matches): {cold}, reported separately. "
        f"Unscorable (90' score unknown): {bad.height}.",
        "",
        f"Chosen on dev (all matches, 1X2 log loss): half-life **{best:g} days**, team "
        f"prior sd **{best_sd if best_sd is not None else 'none'}**.",
        "",
        "## Tuning grid (Dixon-Coles, dev window)",
        "",
        "| half-life (days) | prior sd | n | 1X2 log loss | 1X2 RPS | warm 1X2 log loss |",
        "|---|---|---|---|---|---|",
        *[_tune_row(r) for r in tune.iter_rows(named=True)],
        "",
        "## Paired differences (warm matches; negative favours Dixon-Coles)",
        "",
        "| comparison | n | mean | 95% interval |",
        "|---|---|---|---|",
        *[
            f"| {k} | {v['n']:.0f} | {v['mean_diff']:+.4f} | [{v['lo']:+.4f}, {v['hi']:+.4f}] |"
            for k, v in paired.items()
        ],  # fmt: skip
        "",
        "## Baseline check (§14.4)",
        "",
        "\n".join(f"- FLAG: {f}" for f in flags)
        if flags
        else "Dixon-Coles beats both baselines on 1X2 log loss in every league.",
        "",
        "## Overall (warm matches)",
        "",
        table("all"),
        "",
        "## By bundle",
        "",
        table("bundle"),
        "",
        "## By league",
        "",
        table("league_id"),
        "",
        "## By season",
        "",
        table("season"),
        "",
        "## Cold start",
        "",
        table("cold_start"),
        "",
    ]
    return "\n".join(parts)


def _tune_row(r: dict[str, Any]) -> str:
    sd = "none" if r["prior_sd"] is None else f"{r['prior_sd']:g}"
    return (
        f"| {r['half_life_days']:g} | {sd} | {r['n']} | {r['1X2:log_loss']:.4f} "
        f"| {r['1X2:rps']:.4f} | {r['warm 1X2:log_loss']:.4f} |"
    )
