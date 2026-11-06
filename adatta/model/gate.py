"""Run the §14.5 inference gate on one Phase 1b fit and write a report.

    scripts/model.sh uv run --frozen --group model python -m adatta.model.gate \\
        --bundle england --as-of 2023-08-07

Fits the production model plus x0.5 / x2 prior-scale refits (sensitivity) and two short
same-seed fits (reproducibility). Traces go to data/traces/ (git-ignored); the report goes
to reports/phase1b/gate/. Output is metrics only (logs may be public on CI).
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from adatta.db.session import get_session
from adatta.eval import metrics
from adatta.eval.baseline import _file_hash, _git_sha
from adatta.eval.harness import load_config, load_matches
from adatta.model import diagnostics as dg
from adatta.model import dixon_coles as dc
from adatta.model import team_strength as ts

SEED = 20261105
HEADLINE = ("mu_league", "h_league", "sigma_attack", "sigma_defence", "sigma_strength",
            "sigma_open", "sigma_walk", "reset_weeks", "sigma_home",
            "rho")  # fmt: skip


def _flat(idata: Any, name: str) -> dict[str, tuple[float, float]]:
    x = ts.stacked(idata)[name].values
    x = x.reshape(-1, x.shape[-1])
    return {f"{name}[{i}]" if len(x) > 1 else name: (float(r.mean()), float(r.std()))
            for i, r in enumerate(x)}  # fmt: skip


def headline(idata: Any) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    names = set(ts.stacked(idata).data_vars)
    for name in HEADLINE:
        if name in names:
            out.update(_flat(idata, name))
    return out


def predict(idata: Any, fd: ts.FitData, targets: pl.DataFrame, max_goals: int) -> tuple[float, Any]:
    """Mean 1X2 log loss over target matches and each prediction's truncated tail mass."""
    tix = {int(t): i for i, t in enumerate(fd.team_ids)}
    lix = {lid: i for i, lid in enumerate(fd.leagues)}
    home = np.array([tix.get(int(t), -1) for t in targets["home"]])
    away = np.array([tix.get(int(t), -1) for t in targets["away"]])
    league = np.array([lix[int(x)] for x in targets["league_id"]])
    lam, mu_ = ts.rates(idata, fd, home, away, league)
    rho = ts.stacked(idata)["rho"].values
    rho = rho[np.linspace(0, len(rho) - 1, lam.shape[1]).astype(int)]
    losses, tails = [], []
    for k, (hg, ag) in enumerate(zip(targets["hg"], targets["ag"], strict=True)):
        mats = [dc.score_matrix(float(lam[k, d]), float(mu_[k, d]), float(rho[d]), max_goals)
                for d in range(lam.shape[1])]  # fmt: skip
        m = np.mean([x[0] for x in mats], axis=0)
        tails.append(float(np.mean([x[1] for x in mats])))  # tail of the averaged matrix
        losses.append(metrics.score_all(m, int(hg), int(ag))["1X2:log_loss"])
    return float(np.mean(losses)), np.array(tails)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default="england")
    ap.add_argument("--as-of", default="2023-08-07")
    ap.add_argument("--draws", type=int, default=1000)
    ap.add_argument("--chains", type=int, default=4)
    ap.add_argument("--tune", type=int, default=None, help="warm-up steps (default: draws)")
    ap.add_argument("--structure", default="independent", choices=ts.STRUCTURES)
    ap.add_argument("--reuse", action="store_true", help="load saved fits if present (debug)")
    args = ap.parse_args()

    started = time.time()
    code_sha = _git_sha()
    cfg = load_config()
    leagues = cfg.bundles[args.bundle]
    as_of = datetime.fromisoformat(args.as_of).replace(tzinfo=UTC)
    with get_session() as s:
        df, _ = load_matches(s, leagues)
        s.rollback()
    fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, cfg.result_available_after_hours)
    targets = df.filter(
        (pl.col("kickoff") >= as_of) & (pl.col("kickoff") < as_of + timedelta(days=28))
    )
    print(f"{args.bundle} as of {as_of:%Y-%m-%d}: {len(fd.hg)} matches, "
          f"{len(fd.team_ids)} teams, {fd.n_steps} steps; {targets.height} targets")  # fmt: skip

    checks: list[dg.Check] = []
    timings: dict[str, float] = {}
    ll: dict[float, float] = {}
    heads: dict[float, dict[str, tuple[float, float]]] = {}
    traces = Path("data/traces")
    traces.mkdir(parents=True, exist_ok=True)
    tag = "" if args.structure == "independent" else f"_{args.structure}"
    tune = args.draws if args.tune is None else args.tune
    tag += "" if tune == args.draws else f"_t{tune}"
    trace_path = traces / f"{args.bundle}_{as_of:%Y%m%d}{tag}_k1_{args.chains}x{args.draws}.nc"
    # One fit at a time: sample (or reload), save, extract what the checks need, release.
    for k in (1.0, 0.5, 2.0):
        path = traces / f"{args.bundle}_{as_of:%Y%m%d}{tag}_k{k:g}_{args.chains}x{args.draws}.nc"
        t0 = time.time()
        if args.reuse and path.exists():
            import arviz as az

            idata = az.from_netcdf(path)
        else:
            idata = ts.sample(
                fd,
                SEED,
                prior_scale=k,
                draws=args.draws,
                chains=args.chains,
                structure=args.structure,
                tune=tune,
            )
            idata.to_netcdf(path)
        timings[f"fit_scale_{k:g}"] = round(time.time() - t0)
        print(f"fit prior_scale={k:g}: {timings[f'fit_scale_{k:g}']} s", flush=True)
        heads[k] = headline(idata)
        ll[k], tails = predict(idata, fd, targets, cfg.max_goals)
        if k == 1.0:
            checks += dg.convergence(idata)
            checks.append(dg.truncation(tails))
            pp, post = ts.stacked(idata, "posterior_predictive"), ts.stacked(idata)
            sim_h, sim_a = pp["home_goals"].values.T, pp["away_goals"].values.T
            att, mu_l = post["attack_last"].values, post["mu_league"].values
            for li, lid in enumerate(leagues):
                sel = fd.league == li
                checks += dg.ppc(fd.hg[sel], fd.ag[sel], sim_h[:, sel], sim_a[:, sel], str(lid))
                recent = sel & (fd.step > fd.step.max() - 52)
                members = np.unique(np.concatenate([fd.home[recent], fd.away[recent]]))
                checks.append(dg.identifiability(mu_l[li], att[members].mean(0), str(lid)))
            del pp, post, sim_h, sim_a
        del idata

    hb = heads[1.0]
    for k in (0.5, 2.0):
        base_mean = {n: v[0] for n, v in hb.items()}
        base_sd = {n: v[1] for n, v in hb.items()}
        alt_mean = {n: v[0] for n, v in heads[k].items()}
        checks.append(dg.sensitivity(base_mean, base_sd, alt_mean, f"x{k:g}"))
        # Proxy only: 4 weeks of targets after one as-of date. The real check is dev 1X2 log
        # loss on the harness (decided 2026-11-06), so this one warns but doesn't block.
        checks.append(dg.predictive_sensitivity(ll[1.0], ll[k], f"x{k:g} (4-week proxy)",
                                                blocking=False))  # fmt: skip

    r1 = ts.sample(fd, SEED, draws=100, chains=2, structure=args.structure)
    r2 = ts.sample(fd, SEED, draws=100, chains=2, structure=args.structure)
    checks.append(dg.reproducible(ts.stacked(r1)["sigma_walk"].values,
                                  ts.stacked(r2)["sigma_walk"].values))  # fmt: skip

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path("reports/phase1b/gate") / f"{stamp}_{args.bundle}_{as_of:%Y%m%d}{tag}"
    out.mkdir(parents=True, exist_ok=True)
    passed = all(c.passed for c in checks if c.blocking)
    gating = [c for c in checks if c.blocking]
    report = {
        "bundle": args.bundle, "structure": args.structure, "as_of": as_of.isoformat(),
        "passed": passed,
        "code_sha": code_sha, "config_hash": _file_hash("config/evaluation.yml"),
        "lockfile_hash": _file_hash("uv.lock"), "seed": SEED, "draws": args.draws,
        "chains": args.chains, "tune": tune, "trace_path": str(trace_path), "timings_s": timings,
        "runtime_s": round(time.time() - started), "n_matches": len(fd.hg),
        "n_targets": targets.height, "logloss_1x2_by_prior_scale": ll,
        "headline": {n: {"mean": v[0], "sd": v[1]} for n, v in hb.items()},
        "checks": [c.as_dict() for c in checks],
    }  # fmt: skip
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    lines = [f"# Phase 1b inference gate — {args.bundle} as of {as_of:%Y-%m-%d} "
             f"({args.structure})", "",
             f"**{'PASS' if passed else 'FAIL'}** ({sum(c.passed for c in gating)}/"
             f"{len(gating)} gating checks; warnings don't block). Code {code_sha}, seed {SEED}, "
             f"{args.chains} x {args.draws} draws, {tune} tuning.", "",
             "| check | pass | value | detail |", "|---|---|---|---|"]  # fmt: skip

    def mark(c: dg.Check) -> str:
        return "yes" if c.passed else ("**no**" if c.blocking else "warn")

    lines += [f"| {c.name} | {mark(c)} | {c.value:.4g} | {c.detail} |" for c in checks]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
