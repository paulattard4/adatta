"""P3-i0: the defensive player rating's dev check (docs/PHASE3_TASKS.md, P3-i;
`adatta.eval.defence`). No sampling: P's draws come from the P3-d dev run's cache, which
must hold every dev refit (a miss stops the run rather than refitting P).

CLI (CI; one backtest-type run at a time):
  lambdas  --bundle B --upload RUN
           per weekly dev refit of P: its in-sample rates for every training match and its
           expected goals for its targets (defence/lambdas/RUN/<bundle>.parquet)
  assemble --fetch RUN
           the ratings on the 28-day grid and the gate; metrics only to stdout, the
           scored team-matches to R2 (defence/reports/RUN/...)
Holdout seasons are never loaded. The repo is public: logs carry metrics, never data.
"""

from __future__ import annotations

import argparse
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from adatta.adapters.storage import RawStore, make_store, try_get
from adatta.config import get_settings
from adatta.eval import defence as ed
from adatta.eval.selection import DEV, WARMUP

PREFIX = "defence"
REFIT_DAYS = 28  # P3-d0's grid (D14)
HISTORY = (2015, *WARMUP)  # stint seasons before dev that P's training windows reach


def _f(x: Any) -> float:
    """A Polars scalar aggregate as a float (NaN when empty)."""
    return float("nan") if x is None else float(x)


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def training_rows(df: pl.DataFrame, as_of: datetime, lookback_days: int,
                  lag_hours: float) -> pl.DataFrame:  # fmt: skip
    """The rows `team_strength.prepare` trains on, in its order (the same filter and sort)."""
    lo = as_of - timedelta(days=lookback_days)
    hi = as_of - timedelta(hours=lag_hours)
    return df.filter(pl.col("kickoff") <= hi).filter(pl.col("kickoff") >= lo).sort("kickoff")


def align(d: pl.DataFrame, home: np.ndarray, away: np.ndarray) -> pl.DataFrame:
    """`d` reordered to the fit's (home, away) sequence. Rows can differ in order only
    within one kickoff time; anything else is an error."""
    if d.height != len(home):
        raise ValueError(f"{d.height} rows vs {len(home)} in the fit")
    h, a = d["home"].to_numpy(), d["away"].to_numpy()
    if (h == home).all() and (a == away).all():
        return d
    pos = {(int(x), int(y), k): i for i, (x, y, k) in
           enumerate(zip(h, a, d["kickoff"].to_list(), strict=True))}  # fmt: skip
    ks = d["kickoff"].to_list()
    order = []
    for i, (x, y) in enumerate(zip(home, away, strict=True)):
        j = pos.get((int(x), int(y), ks[i]))
        if j is None:
            raise ValueError(f"fit row {i} has no match in the frame at its kickoff")
        order.append(j)
    if len(set(order)) != len(order):
        raise ValueError("the fit's rows don't map one to one onto the frame")
    return d[order]


def lambdas(store: RawStore, bundle: str) -> pl.DataFrame:
    """Per weekly dev refit of P (cached draws only): kind "train" rows with its in-sample
    rates per training match, kind "target" rows with its expected goals per target (the
    posterior-mean score matrix's marginals)."""
    from adatta.eval import harness
    from adatta.model import backtest as bt
    from adatta.model import team_strength as ts

    cfg = harness.load_config()
    P = bt.production_spec(cfg)
    leagues = cfg.bundles[bundle]
    df = bt._load(leagues, xg=True)
    plan = harness.refit_plan(df, cfg, list(DEV), P.every_days, None, P.grid)
    mh, ma = harness.mover_codes(df, leagues)
    df = df.with_columns(mover_home=pl.Series(mh), mover_away=pl.Series(ma))
    lag = cfg.result_available_after_hours
    h = bt.fit_hash(P)
    goals = np.arange(cfg.max_goals + 1, dtype=np.float64)
    frames: list[pl.DataFrame] = []
    print(f"{bundle}: {len(plan)} P refits, hash {h}", flush=True)
    for as_of, tgt, _ in plan:
        raw = try_get(store, bt.cache_key(h, bundle, as_of))
        if raw is None:
            raise SystemExit(f"{bundle} {as_of:%Y-%m-%d}: P is not cached under {h}")
        draws, _ = bt.decode(raw)
        if "train_lam_home" not in draws:
            raise SystemExit(f"{bundle} {as_of:%Y-%m-%d}: no training rates in the cache")
        fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, lag)
        d = align(training_rows(df, as_of, cfg.lookback_days, lag),
                  fd.team_ids[fd.home], fd.team_ids[fd.away])  # fmt: skip
        if not ((d["hg"].to_numpy() == fd.hg).all() and (d["ag"].to_numpy() == fd.ag).all()):
            raise ValueError(f"{bundle} {as_of:%Y-%m-%d}: goals differ from the fit's rows")
        frames.append(pl.DataFrame({
            "kind": "train", "p_as_of": as_of, "match_id": d["match_id"],
            "home_id": d["home"], "away_id": d["away"],
            "lam_home": np.asarray(draws["train_lam_home"], dtype=np.float64).reshape(-1),
            "lam_away": np.asarray(draws["train_lam_away"], dtype=np.float64).reshape(-1),
        }))  # fmt: skip
        targets = df[tgt]
        scored = bt.score_targets(draws, fd, targets, cfg.max_goals, pmf=True)
        frames.append(pl.DataFrame({
            "kind": "target", "p_as_of": as_of, "match_id": targets["match_id"],
            "home_id": targets["home"], "away_id": targets["away"],
            "lam_home": [float(goals @ np.asarray(s["pmf_home"])) for s in scored],
            "lam_away": [float(goals @ np.asarray(s["pmf_away"])) for s in scored],
        }))  # fmt: skip
    out = pl.concat(frames, how="vertical_relaxed")
    print(f"{bundle}: {out.filter(pl.col('kind') == 'train').height} training rates, "
          f"{out.filter(pl.col('kind') == 'target').height} targets", flush=True)  # fmt: skip
    return out


def assemble(store: RawStore, run: str) -> tuple[str, dict[str, Any], pl.DataFrame]:
    """Ratings on the 28-day grid over the dev targets, scored against R0."""
    from adatta.eval import harness
    from adatta.features import appearances as ap
    from adatta.features import stints as sn

    cfg = harness.load_config()
    lam_parts = []
    for b in cfg.bundles:
        raw = try_get(store, f"{PREFIX}/lambdas/{run}/{b}.parquet")
        if raw is None:
            raise SystemExit(f"missing lambdas for {b}")
        lam_parts.append(pl.read_parquet(io.BytesIO(raw)).with_columns(bundle=pl.lit(b)))
    lam = pl.concat(lam_parts)
    groups = [(str(lg), yr) for ls in cfg.bundles.values() for lg in ls
              for yr in (*HISTORY, *DEV)]  # fmt: skip
    stints = sn.load(store, groups)
    app = ap.load(store, groups).select("player_id", "kickoff", "pos")
    tg = lam.filter(pl.col("kind") == "target").rename(
        {"lam_home": "eg_home", "lam_away": "eg_away"})  # fmt: skip
    train = lam.filter(pl.col("kind") == "train")
    tk = stints.filter(pl.col("match_id").is_in(tg["match_id"])).group_by(
        "match_id").agg(pl.col("kickoff").first())  # fmt: skip
    asof = [k - timedelta(days=1) for k in tk["kickoff"].to_list()]
    times = harness.as_of_times(min(asof), max(asof), REFIT_DAYS)
    p_times = {b: sorted(set(train.filter(pl.col("bundle") == b)["p_as_of"].to_list()))
               for b in cfg.bundles}  # fmt: skip
    k_rows: list[dict[str, Any]] = []
    scored: list[pl.DataFrame] = []
    for i, t in enumerate(times):
        nxt = times[i + 1] if i + 1 < len(times) else datetime.max.replace(tzinfo=UTC)
        # targets whose latest rating refit at or before kickoff - 24 h is t
        ids = tk.filter((pl.col("kickoff") - timedelta(days=1) >= t)
                        & (pl.col("kickoff") - timedelta(days=1) < nxt))["match_id"]  # fmt: skip
        if ids.is_empty():
            continue
        tr = []
        for b, ts_ in p_times.items():
            got = [x for x in ts_ if x <= t]
            if got:
                tr.append(train.filter((pl.col("bundle") == b) & (pl.col("p_as_of") == got[-1])))
        if not tr:
            continue
        tl = pl.concat(tr)
        expo = ed.exposure(stints.filter(pl.col("match_id").is_in(tl["match_id"])), tl)
        r = ed.rate(expo, ed.lines(app, t), t)
        k_rows.append({"as_of": t, **{f"k_{x}": r.k[x] for x in ed.LINES},
                       "at_bound": ",".join(r.at_bound), "players": r.log_theta.height,
                       "fallback": r.fallback,
                       "sd_log_theta": _f(r.log_theta["log_theta"].std())})  # fmt: skip
        eg = tg.filter(pl.col("match_id").is_in(ids)).unique("match_id")
        st = stints.filter(pl.col("match_id").is_in(ids))
        scored.append(ed.score(st, eg, r.log_theta).with_columns(rating_as_of=pl.lit(t)))
    s = pl.concat(scored)
    v = ed.verdict(s)
    ks = pl.DataFrame(k_rows)
    return markdown(s, v, ks), v, s


def markdown(s: pl.DataFrame, v: dict[str, Any], ks: pl.DataFrame) -> str:
    tm, bm = v["team_match"], v["by_match"]
    lines = [
        "# P3-i0 defensive player rating, dev check",
        "",
        f"Team-matches scored: {tm['n']:,} over {s['match_id'].n_unique():,} matches; "
        f"rating refits: {ks.height}.",
        f"R0 Poisson log loss {v['ll0']:.5f}. R1 - R0 per team-match {tm['mean']:+.6f} "
        f"[{tm['lo']:+.6f}, {tm['hi']:+.6f}], relative {v['rel']:+.3%} "
        f"[{v['rel_lo']:+.3%}, {v['rel_hi']:+.3%}].",
        f"Clustered by match (reported): [{bm['lo']:+.6f}, {bm['hi']:+.6f}].",
        f"**Gate: {'PASS' if v['passed'] else 'FAIL'}** (interval excluding 0 below).",
        "",
        "| season | n | R1 - R0 | 95% interval |",
        "|---|---|---|---|",
    ]
    for (yr,), g in sorted(s.group_by("season")):
        p = ed.paired((g["ll1"] - g["ll0"]).to_numpy())
        lines.append(f"| {yr} | {p['n']:,} | {p['mean']:+.6f} | [{p['lo']:+.6f}, {p['hi']:+.6f}] |")
    lines += [
        "",
        f"Minute share of on-pitch players rated: mean {_f(s['rated'].mean()):.3f}. "
        f"Lineup shift (minute-weighted mean log θ): sd {_f(s['shift'].std()):.4f}, "
        f"5-95% [{_f(s['shift'].quantile(0.05)):+.4f}, {_f(s['shift'].quantile(0.95)):+.4f}].",
        "",
        "| line | k median | k range | refits at the grid edge |",
        "|---|---|---|---|",
    ]
    for x in ed.LINES:
        c = ks[f"k_{x}"]
        edge = int(ks["at_bound"].str.contains(x).sum())
        lines.append(
            f"| {x} | {_f(c.median()):.1f} | {_f(c.min()):.1f}-{_f(c.max()):.1f} | {edge} |"
        )
    lines.append(f"\nPlayers without a position (rated as {ed.FALLBACK_LINE}): "
                 f"max {_f(ks['fallback'].max()):.0f} per refit.")  # fmt: skip
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> None:
    ap_ = argparse.ArgumentParser(prog="adatta.model.defence")
    sub = ap_.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("lambdas")
    a.add_argument("--bundle", required=True)
    a.add_argument("--upload", required=True)
    b = sub.add_parser("assemble")
    b.add_argument("--fetch", required=True)
    args = ap_.parse_args(argv)
    store = make_store(get_settings())
    if args.cmd == "lambdas":
        out = lambdas(store, args.bundle)
        store.put(f"{PREFIX}/lambdas/{args.upload}/{args.bundle}.parquet", _parquet(out))
        return
    text, v, s = assemble(store, args.fetch)
    store.put(f"{PREFIX}/reports/{args.fetch}/scored.parquet", _parquet(s))
    store.put(f"{PREFIX}/reports/{args.fetch}/verdict.json",
              json.dumps(v, default=str).encode())  # fmt: skip
    print(text)


if __name__ == "__main__":
    main()
