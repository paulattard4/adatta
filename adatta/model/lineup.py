"""P3-d: lineup offset in the likelihood vs serving-only (docs/PHASE3_TASKS.md P3-d; §7.5).

CLI (CI, model group; one backtest-type run at a time):
  quality  --upload RUN [--part i/n] [--draws 2000]
           the P3-d0 goals hyperparameters at the warm-up refit and every dev refit
           (posterior draws to R2: lineup/quality/RUN/<time>.npz)
  table    --quality RUN --export REPORT [--rates latest]
           the offset table (features.lineup_offset) and its leakage check, to R2
  run      --bundle B --season S --part i/n --table DIGEST --upload RUN
           P, L and S on the weekly dev grid (lineup/parts/RUN/...)
  assemble --fetch RUN --bundles ... --seasons ... --parts N --table DIGEST
           the pass rule (e); metrics only to stdout, scored rows to R2
Holdout seasons are refused. The repo is public: logs carry metrics, never data.
"""

from __future__ import annotations

import argparse
import io
import json
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import numpy as np
import polars as pl

from adatta.adapters.storage import RawStore, make_store, try_get
from adatta.config import get_settings
from adatta.eval import lineup as el
from adatta.eval import rates as er
from adatta.eval.selection import DEV, WARMUP, HoldoutRefused
from adatta.features import appearances as ap
from adatta.features import lineup_offset as lo
from adatta.features import rates as fr
from adatta.features.referees import RESULT_LAG

PREFIX = "lineup"
QUALITY_DRAWS = 2000  # as P3-d0 amendment 2
LEAKAGE_N = 100
SEED = 20261118


# ------------------------------------------------------------------------------ quality


def quality_times(rows: pl.DataFrame) -> list[datetime]:
    """The warm-up refit and P3-d0's dev refit times (28-day grid over dev as-of times)."""
    from adatta.eval import harness
    from adatta.model import rates as mr

    dev = rows.filter(pl.col("season").is_in(DEV))
    times = harness.as_of_times(dev["as_of"].min(), dev["as_of"].max(), mr.REFIT_DAYS)  # type: ignore[arg-type]
    return sorted({mr.WARMUP_REFIT, *times})


def _stamp(t: datetime) -> str:
    return f"{t:%Y%m%dT%H%M}"


def quality_key(run: str, t: datetime) -> str:
    return f"{PREFIX}/quality/{run}/{_stamp(t)}.npz"


def _npz(post: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, **{k: np.asarray(v) for k, v in post.items()})  # type: ignore[arg-type]
    return buf.getvalue()


def load_posts(store: RawStore, run: str,
               times: Sequence[datetime]) -> dict[datetime, dict[str, Any]]:  # fmt: skip
    out = {}
    for t in times:
        with np.load(io.BytesIO(store.get(quality_key(run, t)))) as z:
            out[t] = {k: z[k] for k in z.files}
    return out


def run_quality(store: RawStore, run: str, part: tuple[int, int], draws: int,
                rates_ref: str) -> None:  # fmt: skip
    from adatta.model import rates as mr

    table, _, digest = fr.load(store, rates_ref)
    leagues = tuple(er.leagues())
    d = er.Design(leagues)
    rows = mr._frame(table, d)
    times = quality_times(rows)
    diags = []
    for t in times[part[0] :: part[1]]:
        _, post, diag = mr.one_fit(mr._window(rows, t), d, lo.STAT, draws)
        keep = ("log_mu", "log_l", "b", "log_k")
        store.put(quality_key(run, t), _npz({k: post[k] for k in keep}))
        diags.append({**diag, "refit": t.isoformat()})
        print(f"quality refit {t:%Y-%m-%d}: fit {diag['fit_s']:.0f} s, rhat {diag['rhat']:.3f}, "
              f"gates {'ok' if diag['gates_ok'] else 'FAIL'}", flush=True)  # fmt: skip
    store.put(
        f"{PREFIX}/quality/{run}/diags_{part[0]}of{part[1]}.json",
        json.dumps({"rates": digest, "diags": diags}, default=str).encode(),
    )


# ------------------------------------------------------------------------------- table


def load_inputs(store: RawStore, rates_ref: str, export_ref: str,
                ) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, str]:  # fmt: skip
    leagues = er.leagues()
    groups = [(lg, y) for lg in leagues for y in range(WARMUP[0], DEV[-1] + 1)]
    app = ap.load(store, groups)
    hist, _, digest = fr.load(store, rates_ref)
    data = store.get(f"selection/reports/{export_ref}/scored.parquet")
    export = pl.read_parquet(io.BytesIO(data))
    need = {"p_bench", "emin_start", "emin_bench"}
    if not need <= set(export.columns):
        raise SystemExit(f"selection report {export_ref} has no P3-d export columns")
    return app, hist, export, digest


def leakage_check(app: pl.DataFrame, hist: pl.DataFrame, export: pl.DataFrame,
                  posts: dict[datetime, dict[str, Any]], table: pl.DataFrame,
                  n: int = LEAKAGE_N) -> dict[str, Any]:  # fmt: skip
    """For n sampled dev team-matches: rebuild from appearances final by the as-of time
    (kickoff - 24 h - RESULT_LAG); matches from then up to the kickoff (the match itself
    included) keep their announced roles with their minutes removed. The counts table is
    cut the same way, and P3-b rows are those of team-matches up to the kickoff (each made
    at its own T-24h). A_bar, delta_lk and delta_24 must not change."""
    leagues = tuple(er.leagues())
    dev = table.filter(pl.col("season").is_in(DEV) & pl.col("ok_lk"))
    sample = dev.sample(min(n, dev.height), seed=SEED)
    changed = 0
    for r in sample.iter_rows(named=True):
        cut = r["kickoff"] - fr.HORIZON - RESULT_LAG
        late = (pl.col("kickoff") > cut) & (pl.col("kickoff") <= r["kickoff"])
        a = pl.concat([app.filter(pl.col("kickoff") <= cut),
                       app.filter(late).with_columns(minutes=pl.lit(None, pl.Int32))])  # fmt: skip
        h = hist.filter(pl.col("kickoff") <= cut)
        # P3-b rows of team-matches up to this kickoff (each made at its own T-24h)
        ex = export.join(
            app.filter(pl.col("kickoff") <= r["kickoff"]).select("match_id", "team_id").unique(),
            on=["match_id", "team_id"],
        )
        key = pl.DataFrame({"match_id": [r["match_id"]], "team_id": [r["team_id"]]})
        re_ = lo.build(a, h, posts, leagues, ex, only=key)
        if re_.is_empty():
            changed += 1
            continue
        x = re_.row(0, named=True)
        same = all(abs((x[c] or 0.0) - (r[c] or 0.0)) < 1e-9 for c in ("a_bar", "delta_lk",
                                                                         "delta_24"))  # fmt: skip
        changed += 0 if same else 1
    return {"leakage_sampled": sample.height, "leakage_changed": changed,
            "leakage_ok": changed == 0}  # fmt: skip


def build_table(store: RawStore, quality_run: str, export_ref: str, rates_ref: str) -> None:
    app, hist, export, rates_digest = load_inputs(store, rates_ref, export_ref)
    leagues = tuple(er.leagues())
    from adatta.model import rates as mr

    times = quality_times(mr._frame(hist, er.Design(leagues)))
    posts = load_posts(store, quality_run, times)
    t0 = time.monotonic()
    table = lo.build(app, hist, posts, leagues, export)
    print(f"offset table: {table.height} team-matches in {time.monotonic() - t0:.0f} s", flush=True)
    leak = leakage_check(app, hist, export, posts, table)
    print(f"leakage: {leak['leakage_changed']} of {leak['leakage_sampled']} changed", flush=True)
    cov = el.coverage(table, DEV)
    meta = {"rates": rates_digest, "quality_run": quality_run, "export": export_ref,
            "coverage": cov, **leak, "built_at": datetime.now(UTC).isoformat()}  # fmt: skip
    h = lo.save(store, table, meta)
    print(f"stored offset table {h}")
    for c in cov:
        print(f"  {c['league_ref']}: ok real {c['ok_real']:.3f}, lk {c['ok_lk']:.3f}, "
              f"t24 {c['ok_24']:.3f}, lk fallback players {c['fallback']:.2f}")  # fmt: skip
    for v in ("real", "lk", "24"):
        x = table.filter(pl.col("season").is_in(DEV) & pl.col(f"ok_{v}"))[f"delta_{v}"]
        print(f"  delta_{v} on dev: sd {x.std():.3f}, 5%-95% "
              f"{x.quantile(0.05):.3f} to {x.quantile(0.95):.3f}")  # fmt: skip


# --------------------------------------------------------------------------------- run


def gates_ok(diag: dict[str, float]) -> bool:
    """§14.5 convergence gates (model.diagnostics thresholds) on a refit's numbers."""
    from adatta.model import diagnostics as dg

    return bool(diag["rhat_max"] <= dg.RHAT_MAX and diag["ess_bulk_min"] >= dg.ESS_MIN
                and diag["ess_tail_min"] >= dg.ESS_MIN
                and diag["divergences"] <= dg.DIVERGENCES_MAX)  # fmt: skip


def run_part(df: pl.DataFrame, leagues: list[int], bundle: str, season: int,
             store: RawStore, table_digest: str, part: tuple[int, int],
             on_refit: Any = None) -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """P, L and S on one dev bundle-season's weekly refits (every part[1]-th from part[0])."""
    from adatta.eval import harness
    from adatta.model import backtest as bt
    from adatta.model import team_strength as ts

    if season not in DEV:
        raise HoldoutRefused(f"season {season} is not a dev season")
    cfg = harness.load_config()
    P = bt.production_spec(cfg)
    # the training version is part of L's cache key (lineup_offset.py is not hashed)
    L = replace(P, lineup=True, lineup_table=f"{table_digest}:{lo.TRAIN}")
    plan = harness.refit_plan(df, cfg, [season], P.every_days, None, P.grid)
    plan = plan[part[0] :: part[1]]
    mh, ma = harness.mover_codes(df, leagues)
    df = df.with_columns(mover_home=pl.Series(mh), mover_away=pl.Series(ma))
    lag = cfg.result_available_after_hours
    rows: list[dict[str, Any]] = []
    diags: list[dict[str, Any]] = []
    print(f"{bundle} {season} part {part[0]}/{part[1]}: {len(plan)} refits, "
          f"P {bt.config_hash(P)}, L {bt.config_hash(L)}", flush=True)  # fmt: skip
    for as_of, tgt, train in plan:
        fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, lag)
        assert len(fd.hg) == len(train)
        fits = {}
        for name, spec in (("P", P), ("L", L)):
            key = bt.cache_key(bt.fit_hash(spec), bundle, as_of)
            cached = try_get(store, key)
            got = bt.decode(cached) if cached is not None else None
            if got is not None and name == "P" and "train_lam_home" not in got[0]:
                got = None  # cached by a run that kept no training means: refit
            hit = got is not None
            if got is None:
                got = bt.nuts_fitter(spec, train_means=name == "P")(fd, spec.seed)
                store.put(key, bt.encode(*got))
            fits[name] = (got[0], got[1], hit)
        dP, diagP, cP = fits["P"]
        dL, diagL, cL = fits["L"]
        assert fd.lu_home is not None and fd.lu_away is not None
        hg, ag = fd.hg.astype(np.float64), fd.ag.astype(np.float64)
        beta_s = el.serving_beta(dP["train_lam_home"], dP["train_lam_away"], hg, ag,
                                 fd.lu_home, fd.lu_away)  # fmt: skip
        beta_l = np.asarray(dL["beta_lineup"], dtype=np.float64).reshape(-1)
        targets = df[tgt]
        counts = np.bincount(np.concatenate([fd.home, fd.away]), minlength=len(fd.team_ids))
        n_of = {int(t): int(counts[j]) for j, t in enumerate(fd.team_ids)}
        n_dp = int(np.asarray(dP["mu_league"]).shape[-1])

        def score(draws: dict[str, Any], off: Any, fd: Any = fd,
                  targets: pl.DataFrame = targets) -> list[dict[str, Any]]:  # fmt: skip
            return bt.score_targets(draws, fd, targets, cfg.max_goals, extra_offset=off)

        variants: list[tuple[str, str, list[dict[str, Any]]]] = [("P", "", score(dP, None))]
        for h in el.HORIZONS:
            dh = targets[f"d{h}_home"].to_numpy()
            da = targets[f"d{h}_away"].to_numpy()
            s_off = (np.outer(dh, np.full(n_dp, beta_s)), np.outer(da, np.full(n_dp, beta_s)))
            l_off = (np.outer(dh, beta_l), np.outer(da, beta_l))
            variants.append((f"S_{h}", h, score(dP, s_off)))
            variants.append((f"L_{h}", h, score(dL, l_off)))
        for model, h, scored in variants:
            for r, s in zip(targets.iter_rows(named=True), scored, strict=True):
                n_h, n_a = n_of.get(int(r["home"]), 0), n_of.get(int(r["away"]), 0)
                rows.append({
                    "match_id": int(r["match_id"]), "league_id": int(r["league_id"]),
                    "season": int(r["season"]), "kickoff": r["kickoff"], "as_of": as_of,
                    "hg": int(r["hg"]), "ag": int(r["ag"]), "home": int(r["home"]),
                    "away": int(r["away"]), "bundle": bundle, "model": model, "horizon": h,
                    "delta_home": float(r[f"d{h}_home"]) if h else 0.0,
                    "delta_away": float(r[f"d{h}_away"]) if h else 0.0,
                    "cold_start": min(n_h, n_a) < cfg.min_warmup_matches, **s,
                })  # fmt: skip
        for name, dd, dg, c in (("P", dP, diagP, cP), ("L", dL, diagL, cL)):
            sw = float(np.mean(dd["sigma_walk"])) if "sigma_walk" in dd else float("nan")
            g = {
                "model": name,
                "bundle": bundle,
                "season": season,
                "as_of": as_of.isoformat(),
                "cached": bool(c),
                **dg,
                "gates_ok": gates_ok(dg),
                "sigma_walk": sw,
            }
            if name == "L":
                g["beta_lineup"] = float(beta_l.mean())
                g["beta_lineup_sd"] = float(beta_l.std())
            else:
                g["beta_serving"] = beta_s
            diags.append(g)
        print(f"  refit {as_of:%Y-%m-%d}: {len(targets)} targets; P {diagP.get('fit_s', 0):.0f} s "
              f"rhat {diagP['rhat_max']:.3f}; L {diagL.get('fit_s', 0):.0f} s rhat "
              f"{diagL['rhat_max']:.3f}, beta {beta_l.mean():.3f}; beta_S {beta_s:.3f}",
              flush=True)  # fmt: skip
        if on_refit is not None:
            on_refit(pl.DataFrame(rows), diags)
    return pl.DataFrame(rows), diags


def _load_frame(leagues: list[int], table: pl.DataFrame) -> pl.DataFrame:
    from adatta.model import backtest as bt

    df = bt._load(leagues, xg=True)
    return lo.attach(df, table, el.HORIZONS)


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


# ----------------------------------------------------------------------------- assemble


def assemble(store: RawStore, run: str, bundles: Sequence[str], seasons: Sequence[int],
             parts: int, table_ref: str) -> str:  # fmt: skip
    table, meta, digest = lo.load(store, table_ref)
    preds, diags, missing = [], [], []
    for b in bundles:
        for s in seasons:
            for i in range(parts):
                key = f"{PREFIX}/parts/{run}/{b}_{s}_{i}of{parts}"
                data = try_get(store, f"{key}.parquet")
                if data is None:
                    missing.append(key)
                    continue
                preds.append(pl.read_parquet(io.BytesIO(data)))
                diags += json.loads(store.get(f"{key}.json"))["diags"]
    if missing:
        raise SystemExit(f"missing parts: {len(missing)} (first {missing[0]})")
    pred = pl.concat(preds, how="diagonal")
    v = el.verdict(pred, diags, bool(meta.get("leakage_ok")))
    out = el.markdown(pred, v, diags, meta, digest)
    store.put(f"{PREFIX}/reports/{run}/pred.parquet", _parquet(pred))
    store.put(
        f"{PREFIX}/reports/{run}/verdict.json",
        json.dumps({"passed": v.passed, "checks": v.checks}, default=str).encode(),
    )
    return out


# --------------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m adatta.model.lineup")
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("quality")
    q.add_argument("--upload", required=True)
    q.add_argument("--part", default="0/1")
    q.add_argument("--draws", type=int, default=QUALITY_DRAWS)
    q.add_argument("--rates", default="latest")
    t = sub.add_parser("table")
    t.add_argument("--quality", required=True)
    t.add_argument("--export", required=True, help="selection report id with the P3-d export")
    t.add_argument("--rates", default="latest")
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--season", type=int, required=True)
    r.add_argument("--part", default="0/1")
    r.add_argument("--table", default="latest")
    r.add_argument("--upload", required=True)
    a = sub.add_parser("assemble")
    a.add_argument("--fetch", required=True)
    a.add_argument("--bundles", required=True)
    a.add_argument("--seasons", required=True)
    a.add_argument("--parts", type=int, default=1)
    a.add_argument("--table", default="latest")
    args = p.parse_args(argv)
    store = make_store(get_settings())
    if args.cmd == "quality":
        i, n = (int(x) for x in args.part.split("/"))
        run_quality(store, args.upload, (i, n), args.draws, args.rates)
    elif args.cmd == "table":
        build_table(store, args.quality, args.export, args.rates)
    elif args.cmd == "run":
        from adatta.eval import harness

        cfg = harness.load_config()
        leagues = cfg.bundles[args.bundle]
        table, _, digest = lo.load(store, args.table)
        df = _load_frame(leagues, table)
        i, n = (int(x) for x in args.part.split("/"))
        key = f"{PREFIX}/parts/{args.upload}/{args.bundle}_{args.season}_{i}of{n}"

        def save(rows: pl.DataFrame, diags: list[dict[str, Any]]) -> None:
            store.put(f"{key}.parquet", _parquet(rows))
            store.put(f"{key}.json", json.dumps({"table": digest, "diags": diags},
                                                default=str).encode())  # fmt: skip

        rows, diags = run_part(df, leagues, args.bundle, args.season, store, digest, (i, n), save)
        save(rows, diags)
        ok = sum(d["gates_ok"] for d in diags if d["model"] == "L")
        print(f"{args.bundle} {args.season}: {len(diags) // 2} refits, L passing the gates {ok}")
    else:
        seasons = [int(s) for s in args.seasons.split(",")]
        print(assemble(store, args.fetch, args.bundles.split(","), seasons, args.parts,
                       args.table))  # fmt: skip


if __name__ == "__main__":
    main()
