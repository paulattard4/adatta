"""P3-k: team strength from penalty-free xG (docs/PHASE3_TASKS.md P3-k, D71).

Q is P exactly, fitted on training rows whose xG has PEN_XG taken off per penalty the side
took (scored plus missed, from the stored appearances). A match where either side's count
is missing keeps no xG row (rule 8). Goals, the model and its priors are P's; only the xG
data differ, so Q has its own cache key and P's cached weekly dev refits stay valid.

On the dev seasons whose refits see xG (DEV_XG), on P's weekly grid: P read from its
cached refits (fitted only if missing, as P3-j step 2), Q fitted, both scored. A Q refit
failing the inference gates is redone once in the same job at target_accept 0.99 and
4,000 draws. The rule is P3-j step 2's (`eval.lineup_j.verdict` with Q as the
challenger): Q's 1X2 log loss and RPS point estimates beat P's, no league x market segment
worse than 1.0%, every Q refit passes the gates, Q's median refit within 1.5x P's. Dev
decides only whether Q is judged live (D59, season end).

CLI (CI, model group; one backtest-type run at a time, never on a Monday):
  run      --bundle B --season S --part i/n --upload RUN
  assemble --fetch RUN --bundles ... --seasons ... --parts N
The repo is public: logs carry metrics only; scored rows go to R2 (penalty_xg/...).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import numpy as np
import polars as pl

from adatta.adapters.storage import RawStore, make_store, try_get
from adatta.config import get_settings
from adatta.eval import lineup as el
from adatta.eval import lineup_j as ej
from adatta.eval.selection import DEV, HoldoutRefused

PREFIX = "penalty_xg"
PEN_XG = 0.76  # D71 (a): the usual public value, fixed before any score
DEV_XG = (2022, 2023)  # the dev seasons whose refits see xG (team xG is stored from 2022)
MODEL_FILE = "adatta/model/penalty_xg.py"
REDO_TARGET_ACCEPT, REDO_DRAWS = 0.99, 4000


def penalty_counts(app: pl.DataFrame) -> pl.DataFrame:
    """(match_id, team_id, taken): penalties each side took, scored plus missed, over the
    team-matches with a statistics block (any player's provider minutes)."""
    stats = app.group_by("match_id", "team_id").agg(
        taken=(pl.col("pen_scored").fill_null(0) + pl.col("pen_missed").fill_null(0)).sum(),
        has=pl.col("minutes_provider").is_not_null().any(),
    )
    return stats.filter(pl.col("has")).select("match_id", "team_id",
                                              pl.col("taken").cast(pl.Int64))  # fmt: skip


def penalty_free(df: pl.DataFrame, counts: pl.DataFrame) -> pl.DataFrame:
    """df with xg_home / xg_away less PEN_XG per penalty taken (at least 0.01). A match
    with xG but a side's count missing loses both xG values (rule 8: nothing imputed)."""
    out = df
    for side in ("home", "away"):
        out = out.join(counts.rename({"team_id": side, "taken": f"pen_{side}"}),
                       on=["match_id", side], how="left")  # fmt: skip
    known = pl.col("pen_home").is_not_null() & pl.col("pen_away").is_not_null()
    adj = [pl.when(known).then((pl.col(f"xg_{s}") - PEN_XG * pl.col(f"pen_{s}")).clip(0.01))
           .alias(f"xg_{s}") for s in ("home", "away")]  # fmt: skip
    return out.with_columns(adj).drop("pen_home", "pen_away")


def q_hash(p_hash: str) -> str:
    """Q's cache hash: P's fit hash, this file, the penalty value."""
    from adatta.model import backtest as bt

    blob = json.dumps({"p": p_hash, "file": bt._file_hash(MODEL_FILE), "pen_xg": PEN_XG},
                      sort_keys=True)  # fmt: skip
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def run_part(df: pl.DataFrame, dq: pl.DataFrame, leagues: list[int], bundle: str, season: int,
             store: RawStore, part: tuple[int, int], on_refit: Any = None,
             ) -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """P and Q on one dev bundle-season's weekly refits (every part[1]-th from part[0])."""
    from adatta.eval import harness
    from adatta.model import backtest as bt
    from adatta.model import team_strength as ts

    if season not in DEV or season not in DEV_XG:
        raise HoldoutRefused(f"season {season} is not a dev season with xG")
    cfg = harness.load_config()
    P = bt.production_spec(cfg)
    p_hash = bt.fit_hash(P)
    qh = {k: q_hash(p_hash) + k for k in ("", "r")}  # first try, redo
    plan = harness.refit_plan(df, cfg, [season], P.every_days, None, P.grid)[part[0] :: part[1]]
    lag = cfg.result_available_after_hours
    rows: list[dict[str, Any]] = []
    diags: list[dict[str, Any]] = []
    print(f"{bundle} {season} part {part[0]}/{part[1]}: {len(plan)} refits, P {p_hash}, "
          f"Q {qh['']}", flush=True)  # fmt: skip
    for as_of, tgt, _train in plan:
        fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, lag)
        fq = ts.prepare(dq, leagues, as_of, cfg.lookback_days, lag)
        assert np.array_equal(fd.hg, fq.hg) and np.array_equal(fd.home, fq.home)
        raw = try_get(store, bt.cache_key(p_hash, bundle, as_of))
        p_cached = raw is not None
        if raw is not None:
            dP, diagP = bt.decode(raw)
        else:
            dP, diagP = bt.nuts_fitter(P, train_means=True)(fd, P.seed)
            store.put(bt.cache_key(p_hash, bundle, as_of), bt.encode(dP, diagP))
        redone = False
        for k, spec in (("", P), ("r", replace(P, target_accept=REDO_TARGET_ACCEPT,
                                                draws=REDO_DRAWS))):  # fmt: skip
            key = bt.cache_key(qh[k], bundle, as_of)
            raw = try_get(store, key)
            q_cached = raw is not None
            if raw is not None:
                dQ, diagQ = bt.decode(raw)
            else:
                dQ, diagQ = bt.nuts_fitter(spec)(fq, P.seed)
                store.put(key, bt.encode(dQ, diagQ))
            redone = k == "r"
            if _gates_ok(diagQ):
                break
        targets = df[tgt]
        counts = np.bincount(np.concatenate([fd.home, fd.away]), minlength=len(fd.team_ids))
        n_of = {int(t): int(counts[j]) for j, t in enumerate(fd.team_ids)}
        for model, draws, fd_ in (("P", dP, fd), ("Q", dQ, fq)):
            scored = bt.score_targets(draws, fd_, targets, cfg.max_goals)
            for r, s in zip(targets.iter_rows(named=True), scored, strict=True):
                rows.append({"match_id": int(r["match_id"]), "league_id": int(r["league_id"]),
                             "season": int(r["season"]), "kickoff": r["kickoff"],
                             "as_of": as_of, "hg": int(r["hg"]), "ag": int(r["ag"]),
                             "bundle": bundle, "model": model, "horizon": "",
                             "cold_start": min(n_of.get(int(r["home"]), 0),
                                               n_of.get(int(r["away"]), 0))
                             < cfg.min_warmup_matches, **s})  # fmt: skip
        for name, dg, cached in (("P", diagP, p_cached), ("Q", diagQ, q_cached)):
            diags.append({"model": name, "bundle": bundle, "season": season,
                          "as_of": as_of.isoformat(), "cached": bool(cached), **dg,
                          "gates_ok": _gates_ok(dg), "redone": redone and name == "Q"})  # fmt: skip
        p_txt = "cached" if p_cached else f"{diagP.get('fit_s', 0):.0f} s"
        print(f"  refit {as_of:%Y-%m-%d}: {len(targets)} targets; P {p_txt}; Q "
              f"{diagQ.get('fit_s', 0):.0f} s rhat {diagQ['rhat_max']:.3f} div "
              f"{diagQ['divergences']:.0f}{' (redone)' if redone else ''}", flush=True)  # fmt: skip
        if on_refit is not None:
            on_refit(pl.DataFrame(rows), diags)
    return pl.DataFrame(rows), diags


def _gates_ok(diag: dict[str, float]) -> bool:
    from adatta.model import diagnostics as dg

    return bool(diag["rhat_max"] <= dg.RHAT_MAX and diag["ess_bulk_min"] >= dg.ESS_MIN
                and diag.get("ess_tail_min", dg.ESS_MIN) >= dg.ESS_MIN
                and diag["divergences"] <= dg.DIVERGENCES_MAX)  # fmt: skip


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def _key(run: str, b: str, s: int, i: int, n: int) -> str:
    return f"{PREFIX}/parts/{run}/{b}_{s}_{i}of{n}"


def assemble(store: RawStore, run: str, bundles: Sequence[str], seasons: Sequence[int],
             parts: int) -> str:  # fmt: skip
    preds, diags, missing = [], [], []
    for b in bundles:
        for s in seasons:
            for i in range(parts):
                key = _key(run, b, s, i, parts)
                data = try_get(store, f"{key}.parquet")
                if data is None:
                    missing.append(key)
                    continue
                preds.append(pl.read_parquet(io.BytesIO(data)))
                diags += json.loads(store.get(f"{key}.json"))["diags"]
    if missing:
        raise SystemExit(f"missing parts: {len(missing)} (first {missing[0]})")
    pred = pl.concat(preds, how="diagonal")
    v = ej.verdict(pred, diags, None, ch="Q", fitted="Q")
    store.put(f"{PREFIX}/reports/{run}/pred.parquet", _parquet(pred))
    verdict = json.dumps({"passed": v.passed, "checks": v.checks}, default=str)
    store.put(f"{PREFIX}/reports/{run}/verdict.json", verdict.encode())
    return markdown(pred, v, diags)


def markdown(pred: pl.DataFrame, v: el.Verdict, diags: list[dict[str, Any]]) -> str:
    """Metrics only (the repo is public)."""
    lines = [f"# P3-k: Q (penalty-free xG, {PEN_XG} per penalty) vs P on dev "
             f"{'-'.join(map(str, DEV_XG))}", "",
             f"**Judge live: {'yes' if v.passed else 'no'}**", "",
             "| check | value | 95% interval | pass |", "|---|---|---|---|"]  # fmt: skip
    for c in v.checks:
        pct = any(x in c["check"] for x in ("log_loss", "rps", "crps"))
        iv = f"[{el._f(c['lo'], True)}, {el._f(c['hi'], True)}]" if "lo" in c else ""
        lines.append(f"| {c['check']} | {el._f(c['value'], pct)} | {iv} | "
                     f"{'yes' if c['passed'] else 'NO'} |")  # fmt: skip
    for by in ("league_id", "season"):
        lines += ["", f"## Q vs P by {by}", ""]
        lines += el.segment_table(pred, by, "Q", "P")
    pw = ej.power(pred, ch="Q")
    lines += ["", "## Power for the live window (D59)", "",
              f"Per-match Q - P log loss sd {pw['sd_diff']:.4f} over {int(pw['n_dev'])} dev "
              f"matches: at {int(pw['n_live'])} live matches the detectable gain is "
              f"{pw['detectable']:.2%} (80% power, two-sided 5%)."]  # fmt: skip
    lines += ["", "## Fits", ""]
    for m in ("P", "Q"):
        g = [d for d in diags if d["model"] == m]
        if g:
            lines.append(
                f"- {m}: {len(g)} refits ({sum(bool(d.get('cached')) for d in g)} cached, "
                f"{sum(bool(d.get('redone')) for d in g)} redone), "
                f"{sum(not d['gates_ok'] for d in g)} failing the gates; worst R-hat "
                f"{max(d['rhat_max'] for d in g):.3f}; median fit "
                f"{float(np.median([d.get('fit_s', np.nan) for d in g])):.0f} s"
            )  # fmt: skip
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m adatta.model.penalty_xg")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--season", type=int, required=True)
    r.add_argument("--part", default="0/1")
    r.add_argument("--upload", required=True)
    a = sub.add_parser("assemble")
    a.add_argument("--fetch", required=True)
    a.add_argument("--bundles", required=True)
    a.add_argument("--seasons", required=True)
    a.add_argument("--parts", type=int, default=1)
    args = p.parse_args(argv)
    store = make_store(get_settings())
    if args.cmd == "run":
        from adatta.eval import harness
        from adatta.features import appearances as ap
        from adatta.model import backtest as bt

        cfg = harness.load_config()
        leagues = cfg.bundles[args.bundle]
        P = bt.production_spec(cfg)
        df = bt._load(leagues, P.xg, P.manager)
        years = range(args.season - 3, args.season + 1)  # the lookback's seasons
        app = ap.load(store, [(str(lg), y) for lg in leagues for y in years])
        dq = penalty_free(df, penalty_counts(app))
        i, n = (int(x) for x in args.part.split("/"))
        key = _key(args.upload, args.bundle, args.season, i, n)

        def save(rows: pl.DataFrame, diags: list[dict[str, Any]]) -> None:
            store.put(f"{key}.parquet", _parquet(rows))
            store.put(f"{key}.json", json.dumps({"pen_xg": PEN_XG, "diags": diags},
                                                default=str).encode())  # fmt: skip

        rows, diags = run_part(df, dq, leagues, args.bundle, args.season, store, (i, n), save)
        save(rows, diags)
        ok = sum(d["gates_ok"] for d in diags if d["model"] == "Q")
        print(f"{args.bundle} {args.season}: {len(diags) // 2} refits, Q passing the gates {ok}")
    else:
        print(assemble(store, args.fetch, args.bundles.split(","),
                       [int(s) for s in args.seasons.split(",")], args.parts))  # fmt: skip


if __name__ == "__main__":
    main()
