"""C2 step 3 (D54): serving Phase 1a on the eight top flights.

(d) Shadow on the live period to date: the serving code path (`c2_live.fit_as_of` each
Monday, `c2_serve.markets` per fixture) projects the top flights' 2026/27 matches played so
far. Each fixture gets its milestones, every market sums to 1, every matrix is inside the
truncation limit, and pooled 1X2 log loss beats league base rates (reliability reported).
Only Phase 1a is scored on this period; no 1b score is computed (D54 (h)). Metrics only.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
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


LIVE_SEASON = 2026  # the live period: 2026/27 (D54 (d)); 1b is never scored on it (h)
TICK = timedelta(minutes=15)  # the capture schedule


def milestones_ok(kickoff: datetime, first: datetime) -> bool:
    """Ticks every 15 minutes from `first` to kickoff write initial and every timed
    milestone (`serve.due_milestones`)."""
    from adatta.capture import snapshots
    from adatta.sim import serve

    done: set[str] = set()
    t = first
    while t < kickoff:
        done.update(serve.due_milestones(kickoff, t, done))
        t += TICK
    return done >= {"initial", *snapshots.TIMED}


def shadow_bundle(df: pl.DataFrame, leagues: list[int], cfg: harness.EvalConfig, bundle: str,
                  now: datetime, season: int = LIVE_SEASON) -> pl.DataFrame:  # fmt: skip
    """One row per finished top-flight match of `season` kicking off before `now`: the
    served 1X2 (Phase 1a, the serving code path), the league base rate from the same
    training rows, and the serving checks."""
    from adatta.eval import metrics
    from adatta.model import c2_live
    from adatta.sim import c2_serve as cs
    from adatta.sim import serve

    top = df.filter((pl.col("league_id") == leagues[0]) & (pl.col("season") == season)
                    & (pl.col("kickoff") < now))  # fmt: skip
    if top.is_empty():
        return pl.DataFrame()
    lag = timedelta(hours=cfg.result_available_after_hours)
    rows = []
    weeks = sorted({harness.week_start(t) for t in top["kickoff"].to_list()})
    for as_of in weeks:
        fit = c2_live.fit_as_of(df, leagues, cfg, as_of, bundle)
        seen = df.filter(
            (pl.col("league_id") == leagues[0])
            & (pl.col("kickoff") + lag <= as_of)
            & (pl.col("available_at") <= as_of)
            & (pl.col("kickoff") >= as_of - timedelta(days=cfg.lookback_days))
        )
        hg, ag = seen["hg"].to_numpy(), seen["ag"].to_numpy()
        res = np.array([(hg > ag).sum(), (hg == ag).sum(), (hg < ag).sum()], dtype=float)
        base = (res + 1) / (len(hg) + 3)  # harness._base_rate's smoothing
        week = top.filter((pl.col("kickoff") >= as_of)
                          & (pl.col("kickoff") < as_of + timedelta(days=7)))  # fmt: skip
        for r in week.iter_rows(named=True):
            f = serve.Fixture(r["match_id"], r["kickoff"], r["league_id"], r["home"], r["away"],
                              season)  # fmt: skip
            probs, tail = cs.markets(fit, f, cfg.max_goals)
            p = np.array([probs[("1X2", None, k)] for k in serve.RESULTS])
            outcome = 0 if r["hg"] > r["ag"] else 1 if r["hg"] == r["ag"] else 2
            # first projectable 14 days ahead, from the previous Monday's refit (weekly)
            first = r["kickoff"] - serve.HORIZON
            for model, q in (("dixon_coles", p), ("base_rate", base)):
                rows.append({
                    "bundle": bundle, "league_id": r["league_id"], "match_id": r["match_id"],
                    "as_of": as_of, "model": model, "hg": r["hg"], "ag": r["ag"],
                    "p_home": float(q[0]), "p_draw": float(q[1]), "p_away": float(q[2]),
                    "1X2:log_loss": metrics.log_loss(q, outcome),
                    "1X2:rps": metrics.rps(q, outcome),
                    "cold_start": min(fit.n_prior(r["home"]), fit.n_prior(r["away"]))
                    < cfg.min_warmup_matches,
                    "tail": tail, "sum_err": cs.sums_to_one(probs),
                    "milestones_ok": milestones_ok(r["kickoff"], first),
                })  # fmt: skip
    return pl.DataFrame(rows)


def shadow_verdict(pred: pl.DataFrame) -> dict[str, Any]:
    """(d): serving checks on every fixture; pooled 1X2 log loss of Phase 1a below the base
    rate's (all matches, warm reported); per league and reliability reported."""
    from adatta.model import c2_live

    one = pred.filter(pl.col("model") == "dixon_coles")
    pooled = harness.paired(pred, "1X2:log_loss", "dixon_coles", "base_rate")
    warm = harness.paired(pred.filter(~pl.col("cold_start")), "1X2:log_loss", "dixon_coles",
                          "base_rate")  # fmt: skip
    per = {int(lid): harness.paired(g, "1X2:log_loss", "dixon_coles", "base_rate")
           for (lid,), g in pred.group_by(["league_id"])}  # fmt: skip
    max_tail = float(one["tail"].to_numpy().max())
    max_sum = float(one["sum_err"].to_numpy().max())
    checks = {
        "milestones": bool(one["milestones_ok"].all()),
        "sums_to_one": max_sum < 1e-9,
        "truncation": max_tail < c2_live.TAIL_MAX,
        "beats_base_rate": pooled["mean_diff"] < 0,
    }
    rel = harness.reliability(pred.with_columns(cold_start=pl.lit(False)))
    return {"n": one.height, "weeks": one["as_of"].n_unique(), "pooled": pooled, "warm": warm,
            "per_league": per, "max_tail": max_tail,
            "max_sum_err": max_sum, "checks": checks,
            "pass": all(checks.values()),
            "reliability": rel.to_dicts()}  # fmt: skip


def shadow_lines(verdict: dict[str, Any]) -> list[str]:
    p, w = verdict["pooled"], verdict["warm"]
    out = [f"(d) shadow, live period 2026/27 to date: {verdict['n']} matches, "
           f"{verdict['weeks']} Monday refits",
           f"pooled 1X2 log loss, Phase 1a - base rate: {p['mean_diff']:+.4f} "
           f"[{p['lo']:+.4f}, {p['hi']:+.4f}] (n {int(p['n'])}); warm {w['mean_diff']:+.4f} "
           f"[{w['lo']:+.4f}, {w['hi']:+.4f}] (n {int(w['n'])})",
           f"max tail {verdict['max_tail']:.1e}; max |1 - sum| {verdict['max_sum_err']:.1e}",
           "| top flight | n | 1a - base rate [95%] |", "|---|---|---|"]  # fmt: skip
    for lid, d in sorted(verdict["per_league"].items()):
        out.append(f"| {lid} | {int(d['n'])} | {d['mean_diff']:+.4f} "
                   f"[{d['lo']:+.4f}, {d['hi']:+.4f}] |")  # fmt: skip
    out.append("checks: " + ", ".join(f"{k} {'pass' if v else 'FAIL'}"
                                      for k, v in verdict["checks"].items()))  # fmt: skip
    out.append(f"(d) {'PASSES' if verdict['pass'] else 'FAILS'}")
    return out


def servable(bar: dict[str, Any], verdict: dict[str, Any]) -> list[int]:
    """(g): the top flights passing (b), if (d) passes (it is pooled); else none."""
    if not verdict["pass"]:
        return []
    return sorted(int(k) for k, r in bar["leagues"].items() if r["pass"])


def main() -> None:
    from adatta.model import backtest as bt

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("baseline-bar", help="(b): Phase 1a vs the two baselines on dev")
    b.add_argument("--out", default="reports/c2/baseline_bar")
    sh = sub.add_parser("shadow", help="(d): the serving path on 2026/27 to date, metrics only")
    sh.add_argument("--out", default="reports/c2/shadow")
    rc = sub.add_parser("record", help="(g): champion rows for the leagues passing (b), (d)")
    rc.add_argument("--bar", required=True, help="baseline-bar's bar.json")
    rc.add_argument("--shadow", required=True, help="shadow's verdict.json")
    rc.add_argument("--shadow-run", required=True, help="the CI run of (d)")
    args = ap.parse_args()
    if args.cmd == "record":
        from adatta.db.session import get_session
        from adatta.eval.baseline import _git_sha
        from adatta.model import c2_live

        bar = json.loads(Path(args.bar).read_text())
        verdict = json.loads(Path(args.shadow).read_text())
        leagues = servable(bar, verdict)
        scores = {
            lg: {"bar": bar["leagues"][str(lg)], "shadow": verdict["per_league"].get(str(lg))}
            for lg in leagues
        }
        with get_session() as s:
            rows = c2_live.record_champions(
                s,
                scores,
                {
                    "run": "c2_phase1a_serving",
                    "shadow_run": args.shadow_run,
                    "shadow_pooled": verdict["pooled"],
                    "bar_file": args.bar,
                },
                git_commit=_git_sha(),
            )
            s.commit()
            print(f"recorded {[r.model_name for r in rows]}; servable {leagues}")
        return
    if args.cmd == "shadow":
        from adatta.model import c2_live

        conf, cfg = c2.load_config(), c2_live.scoring_config()
        now = datetime.now(UTC)
        frames = []
        for name, leagues in conf.bundles.items():
            frames.append(shadow_bundle(bt._load(leagues), leagues, cfg, name, now))
            print(f"{name}: {frames[-1].height // 2} matches")
        pred = pl.concat([f for f in frames if f.height])
        verdict = shadow_verdict(pred)
        out = Path(args.out) / now.strftime("%Y%m%dT%H%M%SZ")
        out.mkdir(parents=True, exist_ok=True)
        pred.write_parquet(out / "predictions.parquet")  # match level: local only, not in git
        (out / "verdict.json").write_text(json.dumps(verdict, indent=2, default=str))
        print("\n".join(shadow_lines(verdict)))
        print(f"written {out}")
        return
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
