"""Phase 1b walk-forward backtest (PHASE1B_TASKS task 4; spec §5.3, §14.1–14.4).

The 1b model is refitted on the same refit plan as the Phase 1a harness
(`harness.refit_plan`), so both models predict exactly the same (match, as_of) pairs. Each
refit's prediction draws are cached under (config hash, bundle, as_of) in the store (R2 on
CI, data/cache locally), so a rerun or a resumed CI job only fits what is missing.

    # one bundle x season (a CI matrix job); fitting needs the model group (Docker, D13)
    scripts/model.sh uv run --frozen --group model python -m adatta.model.backtest run \\
        --bundle england --season 2019 --out reports/phase1b/backtest/parts
    # combine parts, score 1a on the same grid, compare (no model group needed)
    uv run python -m adatta.model.backtest assemble --parts reports/phase1b/backtest/parts

Output is metrics only (CI logs are public).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.adapters.storage import LocalStore, RawStore, make_store, try_get
from adatta.eval import harness, metrics
from adatta.model import dixon_coles as dc
from adatta.model import team_strength as ts

Draws = dict[str, NDArray[np.float64]]
Diag = dict[str, float]
Fitter = Callable[[ts.FitData, int], tuple[Draws, Diag]]
CACHE_PREFIX = "backtest"
HASHED_FILES = ("adatta/model/team_strength.py", "config/evaluation.yml", "uv.lock")


@dataclass(frozen=True)
class RunSpec:
    """Everything that determines a refit's draws (plus the cadence of the grid)."""

    structure: str = "strength_open"
    prior_scale: float = 1.0
    draws: int = 2000  # 1,000 left 160 of 266 dev refits over R-hat 1.01 (2026-11-06)
    chains: int = 4
    tune: int | None = None
    target_accept: float = 0.95
    seed: int = 20261105
    every_days: int = 28
    max_pred_draws: int = 400
    shifts: bool = False  # D16 promotion / relegation shift
    form: bool = False  # AR(1) short-term form (§5.2)

    @property
    def model_name(self) -> str:
        k = "" if self.prior_scale == 1.0 else f"_k{self.prior_scale:g}"
        tags = ("_d16" if self.shifts else "") + ("_ar1" if self.form else "")
        return f"phase1b_{self.structure}{tags}{k}"


def _mean(s: pl.Series) -> float:
    return float(s.cast(pl.Float64).to_numpy().mean()) if len(s) else float("nan")


def _file_hash(path: str) -> str:
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "missing"


def config_hash(spec: RunSpec) -> str:
    """Changes whenever the model code, sampler settings, evaluation config or locked
    libraries change, so stale cached draws are never reused."""
    blob = json.dumps({"spec": asdict(spec), "files": {f: _file_hash(f) for f in HASHED_FILES}},
                      sort_keys=True)  # fmt: skip
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def cache_key(h: str, bundle: str, as_of: datetime) -> str:
    return f"{CACHE_PREFIX}/{h}/{bundle}/{as_of:%Y%m%d}.npz"


def encode(draws: Draws, diag: Diag) -> bytes:
    buf = io.BytesIO()
    arrays = {f"draw__{k}": v for k, v in draws.items()}
    arrays |= {f"diag__{k}": np.array(v, dtype=np.float64) for k, v in diag.items()}
    np.savez_compressed(buf, **arrays)  # type: ignore[arg-type]
    return buf.getvalue()


def decode(data: bytes) -> tuple[Draws, Diag]:
    with np.load(io.BytesIO(data)) as z:
        draws = {k[6:]: z[k] for k in z.files if k.startswith("draw__")}
        diag = {k[6:]: float(z[k]) for k in z.files if k.startswith("diag__")}
    return draws, diag


def nuts_fitter(spec: RunSpec) -> Fitter:
    """The production fitter: NUTS via NumPyro, then the §14.5 convergence numbers."""

    def fit(fd: ts.FitData, seed: int) -> tuple[Draws, Diag]:
        from adatta.model import diagnostics as dg

        t0 = time.time()
        idata = ts.sample(fd, seed, prior_scale=spec.prior_scale, draws=spec.draws,
                          chains=spec.chains, target_accept=spec.target_accept,
                          structure=spec.structure, tune=spec.tune,
                          shifts=spec.shifts, form=spec.form)  # fmt: skip
        checks = dg.convergence(idata)
        conv = {c.name: c.value for c in checks}
        # parameter names only (metrics, no data): which parameter is slowest to mix
        print("    " + "; ".join(f"{c.name} {c.value:.3f} {c.detail}" for c in checks[:2]),
              flush=True)  # fmt: skip
        diag = {"rhat_max": conv["rhat"], "ess_bulk_min": conv["ess_bulk"],
                "ess_tail_min": conv["ess_tail"], "divergences": conv["divergences"],
                "fit_s": round(time.time() - t0)}  # fmt: skip
        return ts.slim(idata, spec.max_pred_draws), diag

    return fit


def score_targets(draws: Draws, fd: ts.FitData, targets: pl.DataFrame, max_goals: int
                  ) -> list[dict[str, Any]]:  # fmt: skip
    """Per target: markets from the posterior-mean score matrix, their scores, tail mass."""
    tix = {int(t): i for i, t in enumerate(fd.team_ids)}
    lix = {lid: i for i, lid in enumerate(fd.leagues)}
    home = np.array([tix.get(int(t), -1) for t in targets["home"]], dtype=np.int64)
    away = np.array([tix.get(int(t), -1) for t in targets["away"]], dtype=np.int64)
    league = np.array([lix[int(x)] for x in targets["league_id"]], dtype=np.int64)
    pending: list[NDArray[np.int64] | None] = [None, None]
    if "mover_home" in targets.columns and "delta" in draws:
        # a move whose season has no training match yet is applied at prediction (D16)
        for j, side in enumerate(("home", "away")):
            pending[j] = np.array([
                int(c) if (int(t), int(s)) not in fd.team_seasons else 0
                for t, s, c in zip(targets[side], targets["season"], targets[f"mover_{side}"],
                                   strict=True)
            ], dtype=np.int64)  # fmt: skip
    # weeks from the last training step to each match (AR(1) form fades as phi^h)
    horizon = np.array([(k - fd.start).days // 7 - (fd.n_steps - 1) for k in targets["kickoff"]],
                       dtype=np.int64)  # fmt: skip
    lam, mu_ = ts.rates_from_draws(draws, fd, home, away, league, pending[0], pending[1],
                                   horizon)  # fmt: skip
    rho = draws["rho"]
    out = []
    for k, (hg, ag) in enumerate(zip(targets["hg"], targets["ag"], strict=True)):
        mats = [dc.score_matrix(float(lam[k, d]), float(mu_[k, d]), float(rho[d]), max_goals)
                for d in range(lam.shape[1])]  # fmt: skip
        m = np.mean([x[0] for x in mats], axis=0)
        mk = metrics.markets(m)
        out.append({
            "tail_mass": float(np.mean([x[1] for x in mats])),
            "exp_total": float(np.mean(lam[k] + mu_[k])),
            "p_home": mk["1X2"][0], "p_draw": mk["1X2"][1], "p_away": mk["1X2"][2],
            "p_over25": mk["OU2.5"][0], "p_btts": mk["BTTS"][0],
            **metrics.score_markets(mk, int(hg), int(ag)),
        })  # fmt: skip
    return out


def run(df: pl.DataFrame, leagues: list[int], bundle: str, cfg: harness.EvalConfig,
        seasons: list[int], spec: RunSpec, store: RawStore, fitter: Fitter | None = None,
        log: Callable[[str], None] = print, limit: int | None = None
        ) -> pl.DataFrame:  # fmt: skip
    """Score `seasons` of one bundle on the shared refit plan. Holdout seasons raise
    HoldoutLocked (inside refit_plan). Returns one row per target match. `limit` keeps only
    the first N refits (smoke tests)."""
    fitter = fitter or nuts_fitter(spec)
    plan = harness.refit_plan(df, cfg, seasons, spec.every_days)[:limit]
    h = config_hash(spec)
    log(f"{bundle} seasons {seasons}: {len(plan)} refits, model {spec.model_name}, config {h}")
    lag = cfg.result_available_after_hours
    mh, ma = harness.mover_codes(df, leagues)  # leak-free: season s and s - 1 only
    df = df.with_columns(mover_home=pl.Series(mh), mover_away=pl.Series(ma))
    rows: list[dict[str, Any]] = []
    for i, (as_of, tgt, train) in enumerate(plan):
        fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, lag)
        # the 1b window is the plan's training set: same rows, all observable at as_of
        assert len(fd.hg) == len(train), "1b training window differs from the refit plan"
        key = cache_key(h, bundle, as_of)
        cached = try_get(store, key)
        if cached is not None:
            draws, diag = decode(cached)
        else:
            draws, diag = fitter(fd, spec.seed)
            store.put(key, encode(draws, diag))
        targets = df[tgt]
        counts = np.bincount(np.concatenate([fd.home, fd.away]), minlength=len(fd.team_ids))
        n_of = {int(t): int(counts[j]) for j, t in enumerate(fd.team_ids)}
        for r, s in zip(targets.iter_rows(named=True),
                        score_targets(draws, fd, targets, cfg.max_goals), strict=True):  # fmt: skip
            n_h, n_a = n_of.get(int(r["home"]), 0), n_of.get(int(r["away"]), 0)
            rows.append({
                "match_id": int(r["match_id"]), "league_id": int(r["league_id"]),
                "season": int(r["season"]), "kickoff": r["kickoff"], "as_of": as_of,
                "hg": int(r["hg"]), "ag": int(r["ag"]), "n_train": len(train),
                "home": int(r["home"]), "away": int(r["away"]),
                "mover_home": int(r["mover_home"]), "mover_away": int(r["mover_away"]),
                "n_prior_home": n_h, "n_prior_away": n_a,
                "cold_start": min(n_h, n_a) < cfg.min_warmup_matches,
                "model": spec.model_name, "bundle": bundle, "config_hash": h,
                "cached": cached is not None, **{f"refit_{k}": v for k, v in diag.items()}, **s,
            })  # fmt: skip
        log(f"  refit {i + 1}/{len(plan)} as of {as_of:%Y-%m-%d}: {len(tgt)} targets, "
            f"{'cached' if cached is not None else f'fitted in {diag.get("fit_s", 0):.0f} s'}, "
            f"R-hat {diag.get('rhat_max', float('nan')):.3f}, "
            f"ESS {diag.get('ess_bulk_min', float('nan')):.0f}, "
            f"div {diag.get('divergences', float('nan')):.0f}")  # fmt: skip
    return pl.DataFrame(rows)


# --------------------------------------------------------------------------- assembly


def phase1a_on_grid(df: pl.DataFrame, leagues: list[int], cfg: harness.EvalConfig,
                    seasons: list[int], every_days: int) -> pl.DataFrame:  # fmt: skip
    """The Phase 1a benchmark (dev-tuned half-life and prior sd) on the same refit plan."""
    sel = cfg.phase1a_selected
    c = cfg.model_copy(update={"refit_every_days": every_days})
    return harness.walk_forward(df, leagues, c, sel.half_life_days, seasons, ("dixon_coles",),
                                sel.team_prior_sd)  # fmt: skip


def compare(pred: pl.DataFrame, challenger: str, base: str = "dixon_coles",
            horizons: bool = False) -> pl.DataFrame:  # fmt: skip
    """Paired challenger vs base per metric, overall (warm) and per bundle and league, plus
    the cold-start segment. rel = relative change (negative = challenger better)."""
    out = []
    warm, cold = pred.filter(~pl.col("cold_start")), pred.filter(pl.col("cold_start"))
    segs: list[tuple[str, str, pl.DataFrame]] = [("all", "all", warm), ("cold_start", "all", cold)]
    for col in ("bundle", "league_id"):
        for (v,), g in warm.group_by([col]):
            segs.append((col, str(v), g))
    if horizons and "horizon_week" in warm.columns:  # weeks after the refit (AR(1) form)
        for (v,), g in warm.group_by(["horizon_week"]):
            segs.append(("horizon", str(v), g))
    if "mover_home" in warm.columns:  # D16 segments: a promoted / relegated team plays
        for code, name in ((1, "promoted"), (2, "relegated")):
            g = warm.filter((pl.col("mover_home") == code) | (pl.col("mover_away") == code))
            segs.append(("mover", name, g))
    for seg_type, seg, g in segs:
        if g.filter(pl.col("model") == challenger).height == 0:
            continue
        for m in harness.METRICS:
            p = harness.paired(g, m, challenger, base)
            b = _mean(g.filter(pl.col("model") == base)[m])
            out.append(
                {
                    "segment_type": seg_type,
                    "seg": seg,
                    "metric": m,
                    "n": int(p["n"]),
                    "base": b,
                    "challenger": b + p["mean_diff"],
                    "rel": p["mean_diff"] / b,
                    "rel_lo": p["lo"] / b,
                    "rel_hi": p["hi"] / b,
                }
            )
    return pl.DataFrame(out).sort("segment_type", "seg", "metric")


def mover_bias(pred: pl.DataFrame) -> pl.DataFrame:
    """D16: actual minus expected points per team-match, by model, status (promoted /
    relegated into this tier this season, or other) and phase (team's matches 1-10, 11+),
    warm matches, with a 95% interval half-width."""
    warm = pred.filter(~pl.col("cold_start"))
    sides = []
    for side, w in (("home", "p_home"), ("away", "p_away")):
        won = (pl.col("hg") > pl.col("ag")) if side == "home" else (pl.col("ag") > pl.col("hg"))
        sides.append(warm.select(
            "model", "season", "kickoff", team=pl.col(side), code=pl.col(f"mover_{side}"),
            gap=pl.when(won).then(3).when(pl.col("hg") == pl.col("ag")).then(1).otherwise(0)
            - 3 * pl.col(w) - pl.col("p_draw"),
        ))  # fmt: skip
    tm = pl.concat(sides).with_columns(
        n=pl.col("kickoff").rank("ordinal").over("model", "team", "season"),
        status=pl.col("code").replace_strict({0: "other", 1: "promoted", 2: "relegated"},
                                             return_dtype=pl.Utf8),
    )  # fmt: skip
    tm = tm.with_columns(phase=pl.when(pl.col("n") <= 10).then(pl.lit("1-10"))
                         .otherwise(pl.lit("11+")))  # fmt: skip
    return (tm.group_by("model", "status", "phase")
            .agg(pl.len().alias("n"), pl.col("gap").mean().alias("gap"),
                 (1.96 * pl.col("gap").std() / pl.len().sqrt()).alias("ci95"))
            .sort("status", "phase", "model"))  # fmt: skip


def predictive_sensitivity(pred: pl.DataFrame) -> list[dict[str, Any]]:
    """Dev 1X2 log-loss change of each x0.5 / x2 prior-scale run vs its x1 run (§14.5:
    < 0.2% relative), on the matches both scored."""
    out = []
    names = set(pred["model"].unique())
    for n in sorted(names):
        for k in ("0.5", "2"):
            if n.endswith(f"_k{k}") and n[: -len(f"_k{k}")] in names:
                base = n[: -len(f"_k{k}")]
                p = harness.paired(pred.filter(~pl.col("cold_start")), "1X2:log_loss", n, base)
                b = _mean(pred.filter((pl.col("model") == base) & ~pl.col("cold_start"))
                          ["1X2:log_loss"])  # fmt: skip
                rel = abs(p["mean_diff"]) / b
                out.append({"model": base, "scale": k, "n": int(p["n"]), "rel": rel,
                            "passed": rel < 0.002})  # fmt: skip
    return out


def assemble(parts_dir: Path, out_root: Path, cfg: harness.EvalConfig,
             load: Callable[[list[int]], pl.DataFrame], reference: str | None = None
             ) -> Path:  # fmt: skip
    """Combine 1b parts, score 1a on the same grid, write the comparison report.
    `reference`: a 1b model among the parts (e.g. from an earlier run) that every other 1b
    model is also compared against, with segments by weeks after the refit; it is not
    recorded again."""
    parts = sorted(parts_dir.glob("*.parquet"))
    if not parts:
        raise SystemExit(f"no parts in {parts_dir}")
    b1 = pl.concat([pl.read_parquet(p) for p in parts], how="diagonal_relaxed")
    every = {int(x) for x in b1["refit_every_days"].unique()} if "refit_every_days" in b1.columns \
        else set()  # fmt: skip
    if len(every) > 1:
        raise SystemExit(f"parts mix refit cadences {sorted(every)}")
    cadence = every.pop() if every else 28
    frames, grid_ok = [b1], True
    for bundle, g in b1.group_by("bundle"):
        leagues = cfg.bundles[str(bundle[0])]
        # 1a must use the same refit plans as the parts: a plan's 28-day grid starts at its
        # first target, so a part planned for one season has a different phase from a plan
        # over several. Each part records its plan's seasons (older parts: one per season).
        if "plan_seasons" in g.columns:
            plans = sorted(set(g["plan_seasons"].to_list()))
        else:
            plans = [str(int(s)) for s in sorted(g["season"].unique())]
        df_b = load(leagues)
        a = pl.concat([
            phase1a_on_grid(df_b, leagues, cfg, [int(x) for x in plan.split("-")], cadence)
            for plan in plans
        ], how="diagonal_relaxed").with_columns(bundle=pl.lit(str(bundle[0])))  # fmt: skip
        for name, gm in g.group_by("model"):
            mine = set(zip(gm["match_id"], gm["as_of"], strict=True))
            theirs = set(zip(a["match_id"], a["as_of"], strict=True))
            if mine != theirs:
                grid_ok = False
                print(f"grid mismatch {bundle[0]} {name[0]}: {len(mine ^ theirs)} pairs differ")
        frames.append(a)
    pred = pl.concat(frames, how="diagonal_relaxed").with_columns(
        horizon_week=(pl.col("kickoff") - pl.col("as_of")).dt.total_days() // 7
    )
    if reference is not None and reference not in set(pred["model"].unique()):
        raise SystemExit(f"reference model {reference} is not among the parts")
    challengers = sorted(m for m in pred["model"].unique() if m != "dixon_coles")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = out_root / stamp
    out.mkdir(parents=True, exist_ok=True)
    pred.write_parquet(out / "predictions.parquet")
    comps = {c: compare(pred, c) for c in challengers}
    for c, t in comps.items():
        t.write_csv(out / f"compare_{c}.csv")
    vs = {c: compare(pred, c, reference, horizons=True) for c in challengers
          if reference is not None and c != reference}  # fmt: skip
    for c, t in vs.items():
        t.write_csv(out / f"vs_{c}__{reference}.csv")
    sens = predictive_sensitivity(pred)
    refits = (pred.filter(pl.col("model") != "dixon_coles")
              .unique(["model", "bundle", "as_of"]))  # fmt: skip
    diag = (
        refits.group_by("model")
        .agg(
            pl.len().alias("refits"),
            (pl.col("refit_rhat_max") > 1.01).sum().alias("rhat_over"),
            (pl.col("refit_ess_bulk_min") < 400).sum().alias("ess_under"),
            (pl.col("refit_divergences") > 0).sum().alias("with_divergences"),
            pl.col("refit_rhat_max").max().alias("worst_rhat"),
        )
        .sort("model")
    )
    lines = [f"# Phase 1b dev backtest — {stamp}", "",
             f"Refit every {cadence} days on the shared plan; grid identical to 1a: "
             f"**{'yes' if grid_ok else 'NO'}**. Matches with either team < "
             f"{cfg.min_warmup_matches} prior matches are cold start, reported apart.", "",
             "## Refit diagnostics", "", "| model | refits | R-hat > 1.01 | ESS < 400 | "
             "with divergences | worst R-hat |", "|---|---|---|---|---|---|"]  # fmt: skip
    lines += [f"| {r['model']} | {r['refits']} | {r['rhat_over']} | {r['ess_under']} | "
              f"{r['with_divergences']} | {r['worst_rhat']:.3f} |"
              for r in diag.iter_rows(named=True)]  # fmt: skip
    for c, t in comps.items():
        lines += ["", f"## {c} vs Phase 1a (relative change; negative = 1b better)", "",
                  "| segment | metric | n | 1a | 1b | rel | 95% interval |",
                  "|---|---|---|---|---|---|---|"]  # fmt: skip
        for r in t.filter(pl.col("segment_type").is_in(["all", "bundle", "mover", "cold_start"])
                          ).iter_rows(named=True):  # fmt: skip
            lines.append(
                f"| {r['segment_type']} {r['seg']} | {r['metric']} | {r['n']} | "
                f"{r['base']:.4f} | {r['challenger']:.4f} | {100 * r['rel']:+.2f}% | "
                f"[{100 * r['rel_lo']:+.2f}%, {100 * r['rel_hi']:+.2f}%] |"
            )
    for c, t in vs.items():
        lines += ["", f"## {c} vs {reference} (relative change; negative = {c} better)", "",
                  "| segment | metric | n | reference | challenger | rel | 95% interval |",
                  "|---|---|---|---|---|---|---|"]  # fmt: skip
        shown = ["all", "bundle", "horizon", "mover", "cold_start"]
        for r in t.filter(pl.col("segment_type").is_in(shown)).iter_rows(named=True):
            lines.append(
                f"| {r['segment_type']} {r['seg']} | {r['metric']} | {r['n']} | "
                f"{r['base']:.4f} | {r['challenger']:.4f} | {100 * r['rel']:+.2f}% | "
                f"[{100 * r['rel_lo']:+.2f}%, {100 * r['rel_hi']:+.2f}%] |"
            )
    if "mover_home" in pred.columns and pred["mover_home"].null_count() == 0:
        mb = mover_bias(pred)
        mb.write_csv(out / "mover_bias.csv")
        lines += [
            "",
            "## Promoted / relegated teams: actual − expected points per team-match",
            "",
            "| status | matches | model | n | gap | 95% ± |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {r['status']} | {r['phase']} | {r['model']} | {r['n']} | "
            f"{r['gap']:+.3f} | {r['ci95']:.3f} |"
            for r in mb.iter_rows(named=True)
        ]
    if sens:
        lines += [
            "",
            "## Dev predictive prior sensitivity (§14.5: < 0.2%)",
            "",
            "| model | scale | n | 1X2 log-loss change | pass |",
            "|---|---|---|---|---|",
        ]
        lines += [f"| {s['model']} | x{s['scale']} | {s['n']} | {100 * s['rel']:.3f}% | "
                  f"{'yes' if s['passed'] else '**no**'} |" for s in sens]  # fmt: skip
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    manifest = {
        "run": "phase1b_dev_backtest", "created_at": stamp, "parts": [p.name for p in parts],
        "config_hashes": sorted(b1["config_hash"].unique().to_list()),
        "evaluation_config_hash": _file_hash("config/evaluation.yml"),
        "lockfile_hash": _file_hash("uv.lock"), "refit_every_days": cadence,
        "grid_identical_to_1a": grid_ok, "predictive_sensitivity": sens,
        "reference": reference,
    }  # fmt: skip
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    print("\n".join(lines))
    return out


def record(out: Path, session: Any, seed: int = RunSpec.seed) -> int:
    """One model_run row (§14.8) per 1b model in an assembled report; status "ok" (a dev
    backtest is evidence, not a promotion)."""
    from adatta.db import models
    from adatta.eval.baseline import _git_sha

    manifest = json.loads((out / "manifest.json").read_text())
    pred = pl.read_parquet(out / "predictions.parquet")
    snap = pred.select("match_id", "hg", "ag").unique().sort("match_id")
    snap_hash = hashlib.sha256(snap.write_csv().encode()).hexdigest()
    hashes = ",".join(manifest["config_hashes"])
    if len(hashes) > 64:
        hashes = hashlib.sha256(hashes.encode()).hexdigest()
    n = 0
    s = session
    ref = manifest.get("reference")
    for f in sorted(out.glob("compare_*.csv")):
        if ref is not None and f.stem == f"compare_{ref}":
            continue  # recorded by its own run
        c = pl.read_csv(f).filter(pl.col("segment_type") == "all")
        keys = ("n", "base", "challenger", "rel", "rel_lo", "rel_hi")
        scores = {r["metric"]: {k: r[k] for k in keys} for r in c.iter_rows(named=True)}
        created = datetime.strptime(manifest["created_at"], "%Y%m%dT%H%M%SZ")
        s.add(models.ModelRun(
            model_name=f.stem.removeprefix("compare_"), component="phase1b", status="ok",
            created_at=created.replace(tzinfo=UTC), window="dev", git_commit=_git_sha(),
            config_hash=hashes, lockfile_hash=manifest["lockfile_hash"],
            data_snapshot_hash=snap_hash, seed=seed, report_path=str(out),
            backtest_scores=scores, manifest=manifest,
        ))  # fmt: skip
        n += 1
    s.flush()
    return n


# --------------------------------------------------------------------------- CLI


def _store(local_root: str) -> RawStore:
    from adatta.config import Settings

    s = Settings()
    return make_store(s) if s.storage_backend == "r2" else LocalStore(local_root)


def _load(leagues: list[int]) -> pl.DataFrame:
    from adatta.db.session import get_session

    with get_session() as s:
        df, _ = harness.load_matches(s, leagues)
        s.rollback()
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--season", type=int, action="append", required=True)
    r.add_argument("--structure", default="strength_open", choices=ts.STRUCTURES)
    r.add_argument("--prior-scale", type=float, default=1.0)
    r.add_argument("--shifts", action="store_true", help="D16 promotion/relegation shift")
    r.add_argument("--form", action="store_true", help="AR(1) short-term form (§5.2)")
    r.add_argument("--every-days", type=int, default=None, help="default: D14 dev cadence")
    r.add_argument("--draws", type=int, default=2000)
    r.add_argument("--chains", type=int, default=4)
    r.add_argument("--limit", type=int, default=None, help="first N refits only (smoke test)")
    r.add_argument("--out", default="reports/phase1b/backtest/parts")
    r.add_argument("--cache", default="data/cache", help="local cache root (if not R2)")
    r.add_argument("--upload", default=None, metavar="RUN_ID",
                   help="also put the part in the store under backtest/parts/RUN_ID/ (CI: "
                        "match-level data stays out of public artifacts)")  # fmt: skip
    a = sub.add_parser("assemble")
    a.add_argument("--parts", default="reports/phase1b/backtest/parts")
    a.add_argument("--out", default="reports/phase1b/backtest")
    a.add_argument(
        "--fetch",
        nargs="*",
        default=[],
        metavar="RUN_ID/NAME",
        help="first fetch these parts from the store's backtest/parts/ (several runs may be "
        "combined, e.g. x1 with x0.5 and x2, or strength_open with independent)",
    )
    a.add_argument("--cache", default="data/cache")
    a.add_argument("--record", action="store_true", help="write model_run rows to the DB")
    a.add_argument(
        "--vs",
        default=None,
        metavar="MODEL",
        help="also compare every other 1b model with this one (fetched with it)",
    )
    a.add_argument(
        "--upload-report",
        action="store_true",
        help="put the report in the store under backtest/reports/",
    )
    args = ap.parse_args()

    cfg = harness.load_config()
    if args.cmd == "assemble":
        parts = Path(args.parts)
        store = _store(args.cache)
        parts.mkdir(parents=True, exist_ok=True)
        for ref in args.fetch:
            data = try_get(store, f"{CACHE_PREFIX}/parts/{ref}")
            if data is None:  # its refit job failed: report what exists, say what is missing
                print(f"missing part {ref} (skipped)")
                continue
            (parts / ref.split("/")[-1]).write_bytes(data)
        out = assemble(parts, Path(args.out), cfg, _load, args.vs)
        if args.record:
            from adatta.db.session import get_session

            with get_session() as s:
                n = record(out, s)
                s.commit()
            print(f"recorded {n} model_run rows")
        if args.upload_report:
            for f in out.iterdir():
                store.put(f"{CACHE_PREFIX}/reports/{out.name}/{f.name}", f.read_bytes())
        return
    every = args.every_days or cfg.dev_iteration_refit_every_days
    spec = RunSpec(structure=args.structure, prior_scale=args.prior_scale, draws=args.draws,
                   chains=args.chains, every_days=every, shifts=args.shifts,
                   form=args.form)  # fmt: skip
    leagues = cfg.bundles[args.bundle]
    df = _load(leagues)
    t0 = time.time()
    pred = run(df, leagues, args.bundle, cfg, args.season, spec, _store(args.cache),
               limit=args.limit)  # fmt: skip
    pred = pred.with_columns(
        refit_every_days=pl.lit(every),
        plan_seasons=pl.lit("-".join(str(s) for s in sorted(args.season))),
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    seasons = "-".join(str(s) for s in args.season)
    path = out / f"{spec.model_name}_{args.bundle}_{seasons}.parquet"
    pred.write_parquet(path)
    if args.upload:
        _store(args.cache).put(f"{CACHE_PREFIX}/parts/{args.upload}/{path.name}",
                               path.read_bytes())  # fmt: skip
    warm = pred.filter(~pl.col("cold_start"))
    print(
        f"{len(pred)} predictions ({warm.height} warm) in {time.time() - t0:.0f} s; "
        f"warm 1X2 log loss {_mean(warm['1X2:log_loss']):.4f}, "
        f"OU2.5 log loss {_mean(warm['OU2.5:log_loss']):.4f} -> {path}"
    )


if __name__ == "__main__":
    main()
