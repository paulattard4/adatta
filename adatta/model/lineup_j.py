"""P3-j step 2: J (P + the lineup signals in the likelihood) against P on the weekly dev
walk-forward (docs/PHASE3_TASKS.md P3-j (c), D65).

CLI (CI, model group; one backtest-type run at a time):
  run      --bundle B --season S --part i/n --table DIGEST --upload RUN [--only DATES]
           J fitted per weekly refit (P read from its cached refits, fitted only if
           missing) and both scored: P, J_lk (announced XI, T-60m), J_24 (P3-b's projected
           XI, T-24h). With --only, just those refits, J redone once at target_accept
           REDO_TARGET_ACCEPT and REDO_DRAWS draws.
  failing  --fetch RUN --bundles ... --seasons ... --parts N
           the J refits failing the gates, as the JSON list a redo run takes
  assemble --fetch RUN --bundles ... --seasons ... --parts N --table DIGEST [--override R]
           step 2's rule; metrics only to stdout, scored rows to R2
The signals table is built beforehand (`python -m adatta.features.lineup_signals --step2`).
Holdout seasons are refused. The repo is public: logs carry metrics, never data.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.adapters.storage import RawStore, make_store, try_get
from adatta.config import get_settings
from adatta.eval import lineup_j as ej
from adatta.eval.selection import DEV, HoldoutRefused
from adatta.features import lineup_signals as sig

PREFIX = "lineup_j"
MODEL_FILE = "adatta/model/lineup_strength.py"
REDO_TARGET_ACCEPT, REDO_DRAWS = 0.99, 4000  # one redo of a failing J refit (as A3)
HORIZONS = ("lk", "24")

Arr = NDArray[np.float64]


def j_hash(p_hash: str, table: str, target_accept: float, draws: int) -> str:
    """J's cache hash: P's fit hash (P's code, settings, config, libraries), the signals
    table, J's model file and sampler settings, the prior and the signals."""
    from adatta.model import backtest as bt
    from adatta.model import lineup_strength as lsm

    blob = json.dumps({"p": p_hash, "table": table, "file": bt._file_hash(MODEL_FILE),
                       "target_accept": target_accept, "draws": draws,
                       "prior_sd": lsm.PRIOR_SD, "signals": list(sig.STEP2_SIGNALS)},
                      sort_keys=True)  # fmt: skip
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def gates_ok(diag: dict[str, float]) -> bool:
    from adatta.model import diagnostics as dg

    return bool(diag["rhat_max"] <= dg.RHAT_MAX and diag["ess_bulk_min"] >= dg.ESS_MIN
                and diag["ess_tail_min"] >= dg.ESS_MIN
                and diag["divergences"] <= dg.DIVERGENCES_MAX)  # fmt: skip


def fit_j(fd: Any, s_home: Arr, s_away: Arr, seed: int, *, draws: int, chains: int,
          target_accept: float, tune: int | None, max_pred_draws: int,
          ) -> tuple[dict[str, Arr], dict[str, float]]:  # fmt: skip
    """J's draws (P's prediction set plus beta_signals) and the §14.5 numbers."""
    from adatta.model import backtest as bt
    from adatta.model import diagnostics as dg
    from adatta.model import lineup_strength as lsm

    t0 = time.time()
    idata = lsm.sample(fd, s_home, s_away, seed, draws=draws, chains=chains,
                       target_accept=target_accept, tune=tune)  # fmt: skip
    checks = dg.convergence(idata)
    conv = {c.name: c.value for c in checks}
    print("    " + "; ".join(f"{c.name} {c.value:.3f} {c.detail}" for c in checks[:2]),
          flush=True)  # fmt: skip
    diag = {"rhat_max": conv["rhat"], "ess_bulk_min": conv["ess_bulk"],
            "ess_tail_min": conv["ess_tail"], "divergences": conv["divergences"],
            "fit_s": round(time.time() - t0)}  # fmt: skip
    out = lsm.slim(idata, max_pred_draws)
    del idata
    bt._release_memory()
    return out, diag


def with_row_index(df: pl.DataFrame) -> pl.DataFrame:
    """Each row's position in lu_home / lu_away, so `team_strength.prepare` (which keeps
    those columns, in its training rows' order) reports which rows it took. Neither P nor
    J reads them as an offset (P has lineup off; J's fd has them cleared)."""
    pos = pl.int_range(pl.len(), dtype=pl.Int64).cast(pl.Float64)
    return df.with_columns(lu_home=pos, lu_away=pos)


def training_signals(fd: Any, sh_all: Arr, sa_all: Arr) -> tuple[Any, Arr, Arr]:
    """fd with the row positions cleared, and the training rows' signals in fd's order."""
    assert fd.lu_home is not None
    idx = fd.lu_home.astype(np.int64)
    if not np.array_equal(idx, fd.lu_away.astype(np.int64)):
        raise ValueError("row positions differ between sides")
    return replace(fd, lu_home=None, lu_away=None), sh_all[idx], sa_all[idx]


def run_part(df: pl.DataFrame, leagues: list[int], bundle: str, season: int,
             store: RawStore, table_digest: str, part: tuple[int, int],
             on_refit: Any = None, only: set[str] | None = None,
             ) -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """P and J on one dev bundle-season's weekly refits (every part[1]-th from part[0])."""
    from adatta.eval import harness
    from adatta.model import backtest as bt
    from adatta.model import lineup_strength as lsm
    from adatta.model import team_strength as ts

    if season not in DEV:
        raise HoldoutRefused(f"season {season} is not a dev season")
    cfg = harness.load_config()
    P = bt.production_spec(cfg)
    p_hash = bt.fit_hash(P)
    ta, nd = (REDO_TARGET_ACCEPT, REDO_DRAWS) if only is not None else (P.target_accept,
                                                                         P.draws)  # fmt: skip
    jh = j_hash(p_hash, table_digest, ta, nd)
    plan = harness.refit_plan(df, cfg, [season], P.every_days, None, P.grid)
    plan = plan[part[0] :: part[1]]
    if only is not None:
        plan = [x for x in plan if f"{x[0]:%Y-%m-%d}" in only]
        missing = only - {f"{x[0]:%Y-%m-%d}" for x in plan}
        if missing:
            raise ValueError(f"refits not in this part: {sorted(missing)}")
    mh, ma = harness.mover_codes(df, leagues)
    df = with_row_index(df.with_columns(mover_home=pl.Series(mh), mover_away=pl.Series(ma)))
    sh_all, sa_all = sig.side_signals(df, "home"), sig.side_signals(df, "away")
    lag = cfg.result_available_after_hours
    rows: list[dict[str, Any]] = []
    diags: list[dict[str, Any]] = []
    print(f"{bundle} {season} part {part[0]}/{part[1]}: {len(plan)} refits, P {p_hash}, "
          f"J {jh}", flush=True)  # fmt: skip
    for as_of, tgt, train in plan:
        fd0 = ts.prepare(df, leagues, as_of, cfg.lookback_days, lag)
        assert len(fd0.hg) == len(train)
        fd, s_home, s_away = training_signals(fd0, sh_all, sa_all)
        key_p = bt.cache_key(p_hash, bundle, as_of)
        raw = try_get(store, key_p)
        p_cached = raw is not None
        if raw is not None:
            dP, diagP = bt.decode(raw)
        else:
            dP, diagP = bt.nuts_fitter(P, train_means=True)(fd, P.seed)
            store.put(key_p, bt.encode(dP, diagP))
        key_j = bt.cache_key(jh, bundle, as_of)
        raw = try_get(store, key_j)
        j_cached = raw is not None
        if raw is not None:
            dJ, diagJ = bt.decode(raw)
        else:
            dJ, diagJ = fit_j(fd, s_home, s_away, P.seed, draws=nd, chains=P.chains,
                              target_accept=ta, tune=P.tune,
                              max_pred_draws=P.max_pred_draws)  # fmt: skip
            store.put(key_j, bt.encode(dJ, diagJ))
        beta = np.asarray(dJ[lsm.BETA], dtype=np.float64).reshape(len(sig.STEP2_SIGNALS), -1)
        targets = df[tgt]
        counts = np.bincount(np.concatenate([fd.home, fd.away]), minlength=len(fd.team_ids))
        n_of = {int(t): int(counts[j]) for j, t in enumerate(fd.team_ids)}
        variants: list[tuple[str, str, list[dict[str, Any]]]] = [
            ("P", "", bt.score_targets(dP, fd, targets, cfg.max_goals))
        ]
        for h in HORIZONS:
            off = (sig.side_signals(targets, "home", h) @ beta,
                   sig.side_signals(targets, "away", h) @ beta)  # fmt: skip
            variants.append((f"J_{h}", h, bt.score_targets(dJ, fd, targets, cfg.max_goals,
                                                           extra_offset=off)))  # fmt: skip
        for model, h, scored in variants:
            for r, s in zip(targets.iter_rows(named=True), scored, strict=True):
                n_h, n_a = n_of.get(int(r["home"]), 0), n_of.get(int(r["away"]), 0)
                hz = h or "lk"
                rows.append({
                    "match_id": int(r["match_id"]), "league_id": int(r["league_id"]),
                    "season": int(r["season"]), "kickoff": r["kickoff"], "as_of": as_of,
                    "hg": int(r["hg"]), "ag": int(r["ag"]), "home": int(r["home"]),
                    "away": int(r["away"]), "bundle": bundle, "model": model, "horizon": h,
                    **{f"{x}_{side}": float(r[f"j_{x}_{hz}_{side}"]) if h else 0.0
                       for x in sig.STEP2_SIGNALS for side in ("home", "away")},
                    "cold_start": min(n_h, n_a) < cfg.min_warmup_matches, **s,
                })  # fmt: skip
        for name, dg_, c in (("P", diagP, p_cached), ("J", diagJ, j_cached)):
            g = {"model": name, "bundle": bundle, "season": season,
                 "as_of": as_of.isoformat(), "cached": bool(c), **dg_,
                 "gates_ok": gates_ok(dg_)}  # fmt: skip
            if name == "J":
                g["redone"] = only is not None
                for k, s_ in enumerate(sig.STEP2_SIGNALS):
                    g[f"beta_{s_}"] = float(beta[k].mean())
                    g[f"beta_{s_}_sd"] = float(beta[k].std())
            diags.append(g)
        p_txt = "cached" if p_cached else f"{diagP.get('fit_s', 0):.0f} s"
        print(f"  refit {as_of:%Y-%m-%d}: {len(targets)} targets; P {p_txt}; J "
              f"{diagJ.get('fit_s', 0):.0f} s rhat {diagJ['rhat_max']:.3f} div "
              f"{diagJ['divergences']:.0f}, beta "
              + " ".join(f"{b:+.3f}" for b in beta.mean(axis=1)), flush=True)  # fmt: skip
        if on_refit is not None:
            on_refit(pl.DataFrame(rows), diags)
    return pl.DataFrame(rows), diags


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def _part_keys(run: str, bundles: Sequence[str], seasons: Sequence[int],
               parts: int) -> list[tuple[str, int, int, str]]:  # fmt: skip
    return [(b, s, i, f"{PREFIX}/parts/{run}/{b}_{s}_{i}of{parts}")
            for b in bundles for s in seasons for i in range(parts)]  # fmt: skip


def failing(store: RawStore, run: str, bundles: Sequence[str], seasons: Sequence[int],
            parts: int) -> list[dict[str, Any]]:  # fmt: skip
    """The J refits of a run that fail the gates, one entry per part."""
    out = []
    for b, s, i, key in _part_keys(run, bundles, seasons, parts):
        raw = try_get(store, f"{key}.json")
        if raw is None:
            raise SystemExit(f"missing part {key}")
        bad = sorted(g["as_of"][:10] for g in json.loads(raw)["diags"]
                     if g["model"] == "J" and not g["gates_ok"])  # fmt: skip
        if bad:
            out.append({"bundle": b, "season": s, "part": f"{i}/{parts}", "only": ",".join(bad)})
    return out


def merge_redo(base: pl.DataFrame, base_diags: list[dict[str, Any]], over: pl.DataFrame,
               over_diags: list[dict[str, Any]],
               ) -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """The redo's J rows and diagnostics replace the base run's for the redone refits."""
    redone = {g["as_of"] for g in over_diags if g["model"] == "J"}
    if not redone <= {g["as_of"] for g in base_diags if g["model"] == "J"}:
        raise ValueError("a redone refit is not in the base part")
    times = [datetime.fromisoformat(t) for t in redone]
    is_j = pl.col("model").str.starts_with("J_")
    kept = base.filter(~(is_j & pl.col("as_of").is_in(times)))
    new = over.filter(is_j & pl.col("as_of").is_in(times)).select(base.columns)
    diags = [g for g in base_diags if not (g["model"] == "J" and g["as_of"] in redone)]
    diags += [g for g in over_diags if g["model"] == "J"]
    return pl.concat([kept, new]), diags


def assemble(store: RawStore, run: str, bundles: Sequence[str], seasons: Sequence[int],
             parts: int, table_ref: str, override: str = "") -> str:  # fmt: skip
    _, meta, digest = sig.load_step2(store, table_ref)
    preds, diags, missing = [], [], []
    redone = 0
    for b, s, i, key in _part_keys(run, bundles, seasons, parts):
        data = try_get(store, f"{key}.parquet")
        if data is None:
            missing.append(key)
            continue
        rows = pl.read_parquet(io.BytesIO(data))
        dg = json.loads(store.get(f"{key}.json"))["diags"]
        okey = f"{PREFIX}/parts/{override}/{b}_{s}_{i}of{parts}"
        over = try_get(store, f"{okey}.json") if override else None
        if over is not None:
            o_dg = json.loads(over)["diags"]
            o_rows = pl.read_parquet(io.BytesIO(store.get(f"{okey}.parquet")))
            rows, dg = merge_redo(rows, dg, o_rows, o_dg)
            redone += sum(g["model"] == "J" for g in o_dg)
        preds.append(rows)
        diags += dg
    if missing:
        raise SystemExit(f"missing parts: {len(missing)} (first {missing[0]})")
    pred = pl.concat(preds, how="diagonal")
    v = ej.verdict(pred, diags, bool(meta.get("leakage", {}).get("ok")))
    out = ej.markdown(pred, v, diags, meta, digest)
    if override:
        out += (f"\nJ refits redone by run {override} (target_accept {REDO_TARGET_ACCEPT}, "
                f"{REDO_DRAWS} draws): {redone}.\n")  # fmt: skip
    run = override or run
    store.put(f"{PREFIX}/reports/{run}/pred.parquet", _parquet(pred))
    store.put(f"{PREFIX}/reports/{run}/verdict.json",
              json.dumps({"passed": v.passed, "checks": v.checks},
                         default=str).encode())  # fmt: skip
    return out


def main(argv: Sequence[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m adatta.model.lineup_j")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--season", type=int, required=True)
    r.add_argument("--part", default="0/1")
    r.add_argument("--table", default="latest")
    r.add_argument("--upload", required=True)
    r.add_argument("--only", default="", help="comma-separated refit dates (one redo)")
    f = sub.add_parser("failing")
    a = sub.add_parser("assemble")
    for q in (f, a):
        q.add_argument("--fetch", required=True)
        q.add_argument("--bundles", required=True)
        q.add_argument("--seasons", required=True)
        q.add_argument("--parts", type=int, default=1)
    a.add_argument("--table", default="latest")
    a.add_argument("--override", default="", help="a redo run whose J refits replace the base's")
    args = p.parse_args(argv)
    store = make_store(get_settings())
    if args.cmd == "run":
        from adatta.eval import harness
        from adatta.model import backtest as bt

        cfg = harness.load_config()
        leagues = cfg.bundles[args.bundle]
        table, meta, digest = sig.load_step2(store, args.table)
        df = sig.attach(bt._load(leagues, xg=True), table, meta["scale"])
        i, n = (int(x) for x in args.part.split("/"))
        key = f"{PREFIX}/parts/{args.upload}/{args.bundle}_{args.season}_{i}of{n}"

        def save(rows: pl.DataFrame, diags: list[dict[str, Any]]) -> None:
            store.put(f"{key}.parquet", _parquet(rows))
            store.put(f"{key}.json", json.dumps({"table": digest, "diags": diags},
                                                default=str).encode())  # fmt: skip

        only = set(args.only.split(",")) if args.only else None
        rows, diags = run_part(df, leagues, args.bundle, args.season, store, digest, (i, n),
                               save, only)  # fmt: skip
        save(rows, diags)
        ok = sum(d["gates_ok"] for d in diags if d["model"] == "J")
        print(f"{args.bundle} {args.season}: {len(diags) // 2} refits, J passing the gates {ok}")
    elif args.cmd == "failing":
        seasons = [int(s) for s in args.seasons.split(",")]
        print(json.dumps(failing(store, args.fetch, args.bundles.split(","), seasons,
                                 args.parts)))  # fmt: skip
    else:
        seasons = [int(s) for s in args.seasons.split(",")]
        print(assemble(store, args.fetch, args.bundles.split(","), seasons, args.parts,
                       args.table, args.override))  # fmt: skip


if __name__ == "__main__":
    main()
