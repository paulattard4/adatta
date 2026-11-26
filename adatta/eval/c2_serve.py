"""C2 step 3 (D54): serving Phase 1a on the eight top flights.

(b) The Phase 1a bar on the eight, as for the five (PHASE0_TASKS): over the dev window,
warm matches, Phase 1a (results-only Dixon-Coles at `phase1a_selected`, fixed on the five
before the c2 holdout opened) beats league base rates and a league-only Poisson on 1X2 log
loss in each top flight, on the weekly plan, scored to 20 goals (D53). A league that does
not is flagged as a probable bug and not served.

Kept apart from `adatta/eval/c2.py`, whose digest is part of the C2 fit cache key.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from adatta.eval import c2, harness

EVERY_DAYS = 7  # the weekly plan of the c2 dev and holdout runs
BASES = ("league_poisson", "base_rate")


def baseline_predictions(df: pl.DataFrame, leagues: list[int], cfg: harness.EvalConfig,
                         seasons: list[int]) -> pl.DataFrame:  # fmt: skip
    """Phase 1a and both baselines on one C2 bundle's weekly plan (top-flight targets)."""
    sel = cfg.phase1a_selected
    c = cfg.model_copy(update={"refit_every_days": EVERY_DAYS})
    return harness.walk_forward(df, leagues, c, sel.half_life_days, seasons, harness.MODELS,
                                sel.team_prior_sd)  # fmt: skip


def baseline_bar(pred: pl.DataFrame, top_flights: list[int]) -> dict[str, Any]:
    """(b) per top flight: Phase 1a's warm 1X2 log loss against each baseline, the paired
    difference with its 95% interval, and pass (Phase 1a lower than both)."""
    warm = pred.filter(~pl.col("cold_start"))
    seen = {int(x) for x in warm["league_id"].unique().to_list()}
    if seen != set(top_flights):
        raise ValueError(f"top flights scored {sorted(seen)} != {sorted(top_flights)}")
    leagues: dict[int, dict[str, Any]] = {}
    for lid in top_flights:
        g = warm.filter(pl.col("league_id") == lid)
        mean = {m: float(g.filter(pl.col("model") == m)["1X2:log_loss"].mean())  # type: ignore[arg-type]
                for m in ("dixon_coles", *BASES)}  # fmt: skip
        diffs = {b: harness.paired(g, "1X2:log_loss", "dixon_coles", b) for b in BASES}
        leagues[lid] = {
            "n": int(diffs["base_rate"]["n"]),
            "log_loss": mean,
            "paired": diffs,
            "pass": all(mean["dixon_coles"] < mean[b] for b in BASES),
        }
    return {"leagues": leagues, "passed": sorted(k for k, v in leagues.items() if v["pass"]),
            "failed": sorted(k for k, v in leagues.items() if not v["pass"])}  # fmt: skip


def lines(bar: dict[str, Any]) -> list[str]:
    out = ["| top flight | n | Phase 1a | league Poisson | base rate | 1a - Poisson [95%] "
           "| 1a - base rate [95%] | pass |", "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for lid, r in bar["leagues"].items():
        ll, p = r["log_loss"], r["paired"]
        iv = {b: f"{p[b]['mean_diff']:+.4f} [{p[b]['lo']:+.4f}, {p[b]['hi']:+.4f}]" for b in BASES}
        out.append(f"| {lid} | {r['n']} | {ll['dixon_coles']:.4f} | {ll['league_poisson']:.4f} "
                   f"| {ll['base_rate']:.4f} | {iv['league_poisson']} | {iv['base_rate']} "
                   f"| {'yes' if r['pass'] else '**no**'} |")  # fmt: skip
    return out


def main() -> None:
    from adatta.model import backtest as bt

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("baseline-bar", help="(b): Phase 1a vs the two baselines on dev")
    b.add_argument("--out", default="reports/c2/baseline_bar")
    args = ap.parse_args()
    if args.cmd == "baseline-bar":
        conf = c2.load_config()
        frames = []
        for name in conf.bundles:
            cfg = bt.scoring_config(harness.load_config(), name)
            leagues = c2.leagues_of(name, cfg.bundles)
            pred = baseline_predictions(bt._load(leagues), leagues, cfg, cfg.dev_seasons)
            frames.append(pred.with_columns(bundle=pl.lit(name)))
            print(f"{name}: {pred.filter(pl.col('model') == 'dixon_coles').height} predictions")
        pred = pl.concat(frames, how="diagonal_relaxed")
        bar = baseline_bar(pred, [ls[0] for ls in conf.bundles.values()])
        out = Path(args.out) / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out.mkdir(parents=True, exist_ok=True)
        pred.write_parquet(out / "predictions.parquet")  # match level: local only, not in git
        (out / "bar.json").write_text(json.dumps(bar, indent=2, default=str))
        print("\n".join(lines(bar)))
        print(f"passed {bar['passed']}; failed {bar['failed']}; written {out}")


if __name__ == "__main__":
    main()
