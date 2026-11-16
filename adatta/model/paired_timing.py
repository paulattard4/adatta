"""Paired refit timing (docs/PHASE2_TASKS.md, M4b (d')): item 4's feasibility rule with both
specs fitted on the same runner, so runner-to-runner speed cancels.

One job per bundle-season fits, for each of that season's fixed dates (positions
floor(n/6), floor(n/2), floor(5n/6) of its n weekly refits), the 1b production spec and the
challenger (production spec plus context groups), fresh: no refit cache is read or written.
The order alternates by date (reference first on even positions). Each fit is timed as
`refit_fit_s` is, by the production fitter. `report` combines the jobs' JSON files: per
bundle, median challenger time / median reference time over the same dates, limit 1.25
(phase2.FEASIBLE_RATIO); a bundle with a missing season or date is "not measured".

Output is metrics only (dates, fit times, convergence numbers): the repo is public.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from adatta.eval import harness
from adatta.eval.phase2 import FEASIBLE_RATIO
from adatta.model import covariates as cv
from adatta.model import team_strength as ts
from adatta.model.backtest import Diag, Draws, RunSpec, nuts_fitter, production_spec

POSITIONS = ((1, 6), (1, 2), (5, 6))  # fractions of a season's refits
DEV_SEASONS = (2019, 2020, 2021, 2022, 2023)

Fit = Callable[[RunSpec, ts.FitData], tuple[Draws, Diag]]


def sample_positions(n: int) -> list[int]:
    """The fixed refit positions of a season with n refits (distinct, in order)."""
    return sorted({min(n - 1, n * a // b) for a, b in POSITIONS}) if n else []


def _fit(spec: RunSpec, fd: ts.FitData) -> tuple[Draws, Diag]:
    return nuts_fitter(spec)(fd, spec.seed)


def time_pairs(df: pl.DataFrame, leagues: list[int], bundle: str, season: int,
               cfg: harness.EvalConfig, reference: RunSpec, challenger: RunSpec,
               fit: Fit = _fit, log: Callable[[str], None] = print,
               ) -> list[dict[str, Any]]:  # fmt: skip
    """Fit both specs, fresh, on this process's machine at each fixed date of the season."""
    if reference.every_days != challenger.every_days:
        raise ValueError("both specs must share the refit grid")
    plan = harness.refit_plan(df, cfg, [season], reference.every_days)
    lag = cfg.result_available_after_hours
    rows: list[dict[str, Any]] = []
    for i, pos in enumerate(sample_positions(len(plan))):
        as_of: datetime = plan[pos][0]
        order = (reference, challenger) if i % 2 == 0 else (challenger, reference)
        row: dict[str, Any] = {"bundle": bundle, "season": season, "position": pos,
                               "n_refits": len(plan), "as_of": as_of.isoformat(),
                               "first": "reference" if i % 2 == 0 else "challenger"}  # fmt: skip
        for spec in order:
            role = "reference" if spec is reference else "challenger"
            fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, lag, spec.groups)
            _, diag = fit(spec, fd)
            row |= {f"{role}_{k}": v for k, v in diag.items()}
        log(f"{bundle} {season} refit {pos + 1}/{len(plan)} as of {as_of:%Y-%m-%d}: "
            f"reference {row['reference_fit_s']:.0f} s, challenger "
            f"{row['challenger_fit_s']:.0f} s ({row['first']} first)")  # fmt: skip
        rows.append(row)
    return rows


def verdict(rows: Sequence[dict[str, Any]], bundles: Sequence[str],
            seasons: Sequence[int] = DEV_SEASONS, limit: float = FEASIBLE_RATIO,
            ) -> dict[str, Any]:  # fmt: skip
    """Per bundle: ratio of median fit times over the paired dates; every bundle must pass."""
    df = pl.DataFrame(list(rows)) if rows else pl.DataFrame()
    out: list[dict[str, Any]] = []
    for b in bundles:
        g = df.filter(pl.col("bundle") == b) if df.height else df
        complete = bool(g.height) and set(g["season"].to_list()) == set(seasons) and all(
            r["n_refits"] > 0 and len(sample_positions(r["n_refits"])) ==
            g.filter(pl.col("season") == r["season"]).height
            for r in g.unique("season").iter_rows(named=True)
        )  # fmt: skip
        if not complete:
            out.append({"bundle": b, "dates": g.height, "challenger_s": None,
                        "reference_s": None, "ratio": None, "paired_median": None,
                        "passed": False})  # fmt: skip
            continue
        c = float(g["challenger_fit_s"].median())  # type: ignore[arg-type]
        r_ = float(g["reference_fit_s"].median())  # type: ignore[arg-type]
        paired = float((g["challenger_fit_s"] / g["reference_fit_s"]).median())  # type: ignore[arg-type]
        ratio = c / r_ if r_ else None
        out.append({"bundle": b, "dates": g.height, "challenger_s": c, "reference_s": r_,
                    "ratio": ratio, "paired_median": paired,
                    "passed": ratio is not None and ratio <= limit})  # fmt: skip
    measured = [r for r in out if r["ratio"] is not None]
    v = ("pass" if out and all(r["passed"] for r in out)
         else "not measured" if len(measured) < len(out) else "fail")  # fmt: skip
    return {"limit": limit, "bundles": out, "verdict": v}


def markdown(rep: dict[str, Any]) -> str:
    def num(x: float | None, f: str) -> str:
        return "—" if x is None else format(x, f)

    lines = [f"## Paired refit timing (M4b d'): **{rep['verdict']}**", "",
             f"Both specs fitted on the same runner per date; limit {rep['limit']:.2f} x the "
             "reference's median.", "",
             "| bundle | dates | challenger s | reference s | ratio | median paired ratio | pass |",
             "|---|---|---|---|---|---|---|"]  # fmt: skip
    for r in rep["bundles"]:
        lines.append(f"| {r['bundle']} | {r['dates']} | {num(r['challenger_s'], '.0f')} | "
                     f"{num(r['reference_s'], '.0f')} | {num(r['ratio'], '.2f')} | "
                     f"{num(r['paired_median'], '.2f')} | "
                     f"{'yes' if r['passed'] else '**no**'} |")  # fmt: skip
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--season", type=int, required=True, choices=DEV_SEASONS)
    r.add_argument("--group", action="append", required=True, help="challenger groups")
    r.add_argument("--context", required=True, help="context snapshot digest")
    r.add_argument("--cache", default="data/cache")
    r.add_argument("--out", default="reports/phase2/paired_timing")
    p = sub.add_parser("report")
    p.add_argument("files", nargs="+")
    args = ap.parse_args()
    cfg = harness.load_config()
    if args.cmd == "report":
        rows = [x for f in args.files for x in json.loads(Path(f).read_text())]
        rep = verdict(rows, sorted(cfg.bundles))
        print(markdown(rep))
        return

    from adatta.features import context_table
    from adatta.model.backtest import _load, _store

    reference = production_spec(cfg)
    challenger = replace(reference, groups=cv.resolve(args.group))
    leagues = cfg.bundles[args.bundle]
    df = _load(leagues, reference.xg, reference.manager)
    ctx, snapshot = context_table.load(_store(args.cache), cv.HEADLINE_HORIZON, args.context)
    df = context_table.attach(df, ctx)
    print(f"context snapshot {snapshot}; reference {reference.model_name}, "
          f"challenger {challenger.model_name}")  # fmt: skip
    rows = time_pairs(df, leagues, args.bundle, args.season, cfg, reference, challenger)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.bundle}_{args.season}.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
