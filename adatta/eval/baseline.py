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
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import SOURCE_ID
from adatta.db import models
from adatta.eval import harness

SEED = 0  # the fit is deterministic (L-BFGS from a fixed start); logged for the manifest


def _git_sha() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True
        )
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


def _file_hash(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return "unknown"


def sync_bundles(session: Session, cfg: harness.EvalConfig) -> int:
    """Mirror config bundles into competition_bundle(_member). Returns members added."""
    C, B, BM = models.Competition, models.CompetitionBundle, models.CompetitionBundleMember
    added = 0
    for name, leagues in cfg.bundles.items():
        bundle = session.scalar(select(B).where(B.name == name))
        if bundle is None:
            bundle = B(name=name)
            session.add(bundle)
            session.flush()
        for pos, lid in enumerate(leagues):
            comp = session.scalar(
                select(C).where(C.source_id == SOURCE_ID, C.source_ref == str(lid))
            )
            if comp is None:
                continue  # not ingested yet; synced on a later run
            member = session.get(BM, (bundle.bundle_id, comp.competition_id))
            if member is None:
                session.add(BM(bundle_id=bundle.bundle_id, competition_id=comp.competition_id,
                               position=pos))  # fmt: skip
                added += 1
            else:
                member.position = pos
    session.flush()
    return added


def record_run(
    session: Session, manifest: dict[str, Any], report_path: str, scores: dict[str, Any]
) -> models.ModelRun:
    """Register a baseline run in model_run (experiment tracking, §14.8)."""
    row = models.ModelRun(
        model_name="dixon_coles_static", component="phase1a", status="ok",
        created_at=datetime.strptime(manifest["created_at"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC),
        window=manifest.get("window_scored", ""), git_commit=manifest["code_sha"],
        config_hash=manifest.get("config_hash", "unknown"),
        lockfile_hash=manifest.get("lockfile_hash", "unknown"),
        data_snapshot_hash=manifest["data_snapshot"]["sha256"], seed=manifest["seed"],
        report_path=report_path, backtest_scores=scores, manifest=manifest,
    )  # fmt: skip
    session.add(row)
    session.flush()
    return row


def headline_scores(summary: pl.DataFrame) -> dict[str, Any]:
    """Warm-match overall metrics per model, for model_run.backtest_scores."""
    s = summary.filter(pl.col("segment_type") == "all")
    out: dict[str, Any] = {}
    for r in s.iter_rows(named=True):
        out.setdefault(r["model"], {})[r["metric"]] = r["value"]
        out[r["model"]]["n"] = r["n"]
    return out


def run(session: Session, cfg: harness.EvalConfig, out_root: Path) -> Path:
    started = time.time()
    # Taken before any output exists; untracked files (e.g. earlier reports) don't count as
    # dirty, only changes to tracked code and config do.
    code_sha = _git_sha()
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
        "code_sha": code_sha,
        "seed": SEED,
        "config_hash": _file_hash("config/evaluation.yml"),
        "lockfile_hash": _file_hash("uv.lock"),
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
    sync_bundles(session, cfg)
    record_run(session, json.loads(json.dumps(manifest, default=str)), str(out),
               headline_scores(summary))  # fmt: skip
    session.commit()
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
