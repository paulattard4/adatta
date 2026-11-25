"""P3b-1 dev run: the half split scored on the dev seasons (docs/PHASE3B_TASKS.md (d), (e);
amended by D48). No API requests, no Layer 2 refits, odds never read.

  matrices --bundle B --season S --upload RUN
      the production model's posterior-mean 90' matrix of every dev target on the weekly
      grid, from the fit cache of run 43421580602's spec (a missing fit is an error, never a
      refit): half/matrix/RUN/<bundle>_<season>.parquet. Numpy only.
  assemble --matrices RUN --shares RUN [--upload RUN]
      the share variant chosen by the conditional score, the half markets, the baselines and
      the gate; metrics only to stdout, rows and the verdict to half/v1/RUN/.

The repo is public: logs carry metrics, never match rows. Holdout seasons are refused.
"""

from __future__ import annotations

import argparse
import io
import json
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.adapters.storage import RawStore, try_get
from adatta.eval import harness
from adatta.eval.selection import DEV, HoldoutRefused
from adatta.model import half_split as hm
from adatta.sim import half_split as hs

Arr = NDArray[np.float64]
PREFIX = "half"
FT_RUN = "43421580602"  # the production weekly dev run whose fit cache is rescored
CLIM_PRIOR = 0.5  # B_clim: Jeffreys pseudo-count per outcome
ECE_MARGIN = 0.005  # check 4: ECE no more than this above its noise floor
SEGMENT_MAX = 0.010  # check 5 (D18 rule)
TOP_FIFTH = 0.8  # amendment 3: the top fifth by expected total goals
G = 16  # max_goals 15


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def matrix_key(run: str, bundle: str, season: int) -> str:
    return f"{PREFIX}/matrix/{run}/{bundle}_{season}.parquet"


# ------------------------------------------------------------------------------ matrices


def run_matrices(df: pl.DataFrame, leagues: list[int], bundle: str, season: int,
                 store: RawStore, ft_run: str = FT_RUN) -> pl.DataFrame:  # fmt: skip
    """The posterior-mean 90' matrix of each dev target of one bundle-season, from run
    `ft_run`'s own cached weekly fits (its stored config hash), exactly as its 1X2 was
    scored: every target's 1X2 must equal the run's stored one to 1e-9."""
    from adatta.model import backtest as bt
    from adatta.model import team_strength as ts

    if season not in DEV:
        raise HoldoutRefused(f"season {season} is not a dev season")
    cfg = harness.load_config()
    P = bt.production_spec(cfg)
    got = bt.fetch_part(store, f"{ft_run}/{P.model_name}_{bundle}_{season}.parquet")
    if not got:
        raise SystemExit(f"run {ft_run} has no part for {bundle} {season}")
    ref = pl.concat([pl.read_parquet(io.BytesIO(b)) for _, b in got], how="diagonal_relaxed")
    hashes = ref["config_hash"].unique().to_list()
    if len(hashes) != 1:
        raise SystemExit(f"run {ft_run} {bundle} {season}: config hashes {hashes}")
    h = str(hashes[0])
    want = {(int(a), b): (float(x), float(y)) for a, b, x, y in
            ref.select("match_id", "as_of", "p_home", "p_draw").iter_rows()}  # fmt: skip
    plan = harness.refit_plan(df, cfg, [season], P.every_days, None, P.grid)
    mh, ma = harness.mover_codes(df, leagues)
    df = df.with_columns(mover_home=pl.Series(mh), mover_away=pl.Series(ma))
    rows: list[dict[str, Any]] = []
    for as_of, tgt, _ in plan:
        cached = try_get(store, bt.cache_key(h, bundle, as_of))
        if cached is None:
            raise SystemExit(f"fit {bundle} {as_of:%Y-%m-%d} of {h} is not cached (no refits)")
        draws, _ = bt.decode(cached)
        fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, cfg.result_available_after_hours)
        targets = df[tgt]
        scored = bt.score_targets(draws, fd, targets, cfg.max_goals, matrix=True)
        for r, s in zip(targets.iter_rows(named=True), scored, strict=True):
            w = want.get((int(r["match_id"]), as_of))
            if w is None or max(abs(w[0] - s["p_home"]), abs(w[1] - s["p_draw"])) > 1e-9:
                raise SystemExit(f"{bundle} {season}: target {r['match_id']} as of "
                                 f"{as_of:%Y-%m-%d} differs from run {ft_run}")  # fmt: skip
            rows.append({"match_id": int(r["match_id"]), "league_id": int(r["league_id"]),
                         "season": int(r["season"]), "kickoff": r["kickoff"], "as_of": as_of,
                         "home": int(r["home"]), "away": int(r["away"]), "bundle": bundle,
                         "hg": int(r["hg"]), "ag": int(r["ag"]), "p_home": s["p_home"],
                         "p_draw": s["p_draw"], "p_away": s["p_away"],
                         "exp_total": s["exp_total"], "matrix": s["matrix"]})  # fmt: skip
    return pl.DataFrame(rows)


# ------------------------------------------------------------------------------- shares


class Shares:
    """The share refits of one run: for each variant and date, the posterior and its gates."""

    def __init__(self, store: RawStore, run: str, dates: list[datetime]):
        self.dates = sorted(dates)
        self.post: dict[tuple[str, datetime], tuple[dict[str, Arr], list[int], list[int]]] = {}
        self.gates: dict[tuple[str, datetime], dict[str, Any]] = {}
        for v in hm.VARIANTS:
            for d in self.dates:
                raw = try_get(store, hm.key(run, v, d, "npz"))
                g = try_get(store, hm.key(run, v, d, "json"))
                if raw is None or g is None:
                    raise SystemExit(f"share refit {v} {d:%Y-%m-%d} of run {run} is missing")
                self.post[(v, d)] = hm.decode(raw)
                self.gates[(v, d)] = json.loads(g)

    def date_for(self, as_of: datetime) -> datetime:
        """The latest share refit at or before a forecast's as-of."""
        ok = [d for d in self.dates if d <= as_of]
        if not ok:
            raise ValueError(f"no share refit at or before {as_of}")
        return ok[-1]

    def served(self, variant: str, d: datetime) -> str | None:
        """(c): a refit failing the gates falls back to the next simpler passing variant."""
        order = hm.VARIANTS[: hm.VARIANTS.index(variant) + 1][::-1]
        for v in order:
            if self.gates[(v, d)]["passes"]:
                return v
        return None

    def draws(self, variant: str, d: datetime, league: int, home: int, away: int
              ) -> tuple[Arr, Arr]:  # fmt: skip
        post, lids, tids = self.post[(variant, d)]
        return (
            hm.share(post, lids, tids, 0, league, home),
            hm.share(post, lids, tids, 1, league, away),
        )


# ----------------------------------------------------------------------------- baselines


def climatology(train: pl.DataFrame, league: int) -> tuple[Arr, Arr]:
    """B_clim: the league's half-time 1X2 and HT/FT frequencies over the share refit's
    training matches (Jeffreys pseudo-counts)."""
    t = train.filter((pl.col("league_id") == league) & pl.col("xh").is_not_null()
                     & pl.col("xa").is_not_null())  # fmt: skip
    ht = np.full(3, CLIM_PRIOR)
    htft = np.full(9, CLIM_PRIOR)
    for xh, xa, hg, ag in t.select("xh", "xa", "hg", "ag").iter_rows():
        ht[hs.ht_index(xh, xa)] += 1
        htft[hs.htft_index((xh, xa), (hg, ag))] += 1
    return ht / ht.sum(), htft / htft.sum()


# ------------------------------------------------------------------------------ scoring


def _ll(p: Arr, k: int) -> float:
    return float(-np.log(max(float(p[k]), hs.EPS)))


def _rps(p: Arr, k: int) -> float:
    y = np.zeros(3)
    y[k] = 1.0
    cp, cy = np.cumsum(p)[:-1], np.cumsum(y)[:-1]
    return float(((cp - cy) ** 2).sum() / 2)


def _ci(d: Arr) -> list[float]:
    m = float(d.mean())
    se = float(d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 1 else float("nan")
    return [m, m - 1.96 * se, m + 1.96 * se]


def level_2h(m: Arr, ph: float, pa: float) -> tuple[float, float]:
    """(P(level at half time), E[second-half goals and level at half time]) at the mean
    shares: the simplification check's implied rate is the ratio."""
    bh, ba = hs.thinning(np.array([ph]), G)[0], hs.thinning(np.array([pa]), G)[0]
    j = np.einsum("ha,hx,ay->xyha", m, bh, ba)
    x = np.arange(G)
    lvl = j[x, x]  # (x, h, a)
    second = (x[None, :, None] + x[None, None, :]) - 2 * x[:, None, None]
    return float(lvl.sum()), float((lvl * second).sum())


def score(mx: pl.DataFrame, df: pl.DataFrame, sh: Shares, cfg: harness.EvalConfig
          ) -> tuple[pl.DataFrame, dict[str, Any]]:  # fmt: skip
    """Per target: the conditional score of every variant (as served, with fallbacks), the
    chosen variant's half markets and B_clim. Returns (rows, meta)."""
    ht = df.select("match_id", "xh", "xa", "ht_ok")
    t = mx.join(ht, on="match_id", how="left")
    n_all = t.height
    t = t.filter(pl.col("ht_ok") & pl.col("xh").is_not_null() & pl.col("xa").is_not_null())
    plan = hm.share_plan(df, cfg, DEV)
    train_of = {d: df[tr] for d, _, tr in plan}
    clim: dict[tuple[datetime, int], tuple[Arr, Arr]] = {}
    rows = []
    t0 = time.monotonic()
    for r in t.iter_rows(named=True):
        d = sh.date_for(r["as_of"])
        out: dict[str, Any] = {k: r[k] for k in ("match_id", "league_id", "season", "bundle",
                                                 "hg", "ag", "xh", "xa", "exp_total")}  # fmt: skip
        out["share_date"] = d
        for v in hm.VARIANTS:
            used = sh.served(v, d)
            out[f"used_{v}"] = used
            if used is None:
                out[f"cond_{v}"] = None
                continue
            ph, pa = sh.draws(used, d, r["league_id"], r["home"], r["away"])
            out[f"cond_{v}"] = hs.conditional((r["xh"], r["xa"]), (r["hg"], r["ag"]), ph, pa)
            out[f"ph_{v}"], out[f"pa_{v}"] = float(ph.mean()), float(pa.mean())
        key = (d, int(r["league_id"]))
        if key not in clim:
            clim[key] = climatology(train_of[d], key[1])
        out["clim_ht"], out["clim_htft"] = clim[key][0].tolist(), clim[key][1].tolist()
        out["ft_1x2"] = [r["p_home"], r["p_draw"], r["p_away"]]
        rows.append(out)
    meta = {"targets": n_all, "scored": len(rows), "out_of_scope": n_all - len(rows),
            "seconds": time.monotonic() - t0}  # fmt: skip
    return pl.DataFrame(rows, infer_schema_length=None), meta


def choose(rows: pl.DataFrame) -> tuple[str, dict[str, Any]]:
    """Check 2: S1 replaces S0 only if the paired 95% interval of the mean conditional-score
    difference is below 0; S2 replaces the chosen one likewise."""
    chosen = "S0"
    steps = {}
    for v in ("S1", "S2"):
        ok = rows.filter(pl.col(f"cond_{v}").is_not_null()
                         & pl.col(f"cond_{chosen}").is_not_null())  # fmt: skip
        base = ok[f"cond_{chosen}"].to_numpy()
        d = ok[f"cond_{v}"].to_numpy() - base
        ci = _ci(d)
        steps[f"{v}_vs_{chosen}"] = {"n": len(d), "diff": ci,
                                     "rel": ci[0] / float(base.mean())}  # fmt: skip
        if ci[2] < 0:
            chosen = v
    return chosen, steps


def markets(rows: pl.DataFrame, mx: pl.DataFrame, sh: Shares, variant: str) -> pl.DataFrame:
    """The chosen variant's half markets per target, with check 1's coherence and the
    simplification check's terms."""
    mats = dict(zip(mx["match_id"].to_list(), mx["matrix"].to_list(), strict=True))
    teams = {r[0]: (r[1], r[2]) for r in mx.select("match_id", "home", "away").iter_rows()}
    out = []
    for r in rows.iter_rows(named=True):
        used = r[f"used_{variant}"]
        if used is None:
            continue
        m = np.asarray(mats[r["match_id"]], dtype=np.float64).reshape(G, G)
        home, away = teams[r["match_id"]]
        ph, pa = sh.draws(used, r["share_date"], r["league_id"], home, away)
        mk = hs.markets(m, ph, pa)
        bad = hs.coherence(mk, np.asarray(r["ft_1x2"]))
        lvl, second = level_2h(m, float(ph.mean()), float(pa.mean()))
        out.append({"match_id": r["match_id"], "ht_1x2": mk["ht_1x2"].tolist(),
                    "ht_ft": mk["ht_ft"].tolist(), "ht_ou": mk["ht_ou"].tolist(),
                    "second_1x2": mk["second_1x2"].tolist(),
                    "ht_cs": float(mk["ht_matrix"][r["xh"], r["xa"]]),
                    "coherence": "; ".join(bad), "p_level": lvl,
                    "second_if_level": second})  # fmt: skip
    return pl.DataFrame(out)


# ---------------------------------------------------------------------------------- gate


def gate(rows: pl.DataFrame, mk: pl.DataFrame, sh: Shares, chosen: str,
         steps: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    from adatta.eval import knockout as kb

    t = rows.join(mk, on="match_id", how="inner")
    y_ht = np.array([hs.ht_index(a, b) for a, b in t.select("xh", "xa").iter_rows()])
    y_htft = np.array([hs.htft_index((a, b), (c, d))
                       for a, b, c, d in t.select("xh", "xa", "hg", "ag").iter_rows()])  # fmt: skip
    p_ht = np.array(t["ht_1x2"].to_list())
    p_htft = np.array(t["ht_ft"].to_list())
    b_ht = np.array(t["clim_ht"].to_list())
    b_htft = np.array(t["clim_htft"].to_list())
    n = len(t)
    ll = np.array([_ll(p_ht[i], y_ht[i]) for i in range(n)])
    ll_b = np.array([_ll(b_ht[i], y_ht[i]) for i in range(n)])
    rps = np.array([_rps(p_ht[i], y_ht[i]) for i in range(n)])
    rps_b = np.array([_rps(b_ht[i], y_ht[i]) for i in range(n)])
    lj = np.array([_ll(p_htft[i], y_htft[i]) for i in range(n)])
    lj_b = np.array([_ll(b_htft[i], y_htft[i]) for i in range(n)])
    out: dict[str, Any] = {"chosen": chosen, "selection": steps, **meta, "n": n}

    # 1. coherence, exact
    incoherent = int((t["coherence"] != "").sum())
    out["c1"] = {"incoherent": incoherent, "passes": incoherent == 0}

    # 2. the chosen variant passes (c) at >= 97% of refits
    dates = sh.dates
    passing = sum(sh.gates[(chosen, d)]["passes"] for d in dates)
    fallback = {v: sum(1 for d in dates if sh.served(chosen, d) == v) for v in hm.VARIANTS}
    fails: dict[str, int] = {}
    for d in dates:
        for f in sh.gates[(chosen, d)]["failed"]:
            fails[f] = fails.get(f, 0) + 1
    out["c2"] = {"refits": len(dates), "passing": passing, "share": passing / len(dates),
                 "served_by": fallback, "failed_by": fails,
                 "passes": passing / len(dates) >= hm.PASS_SHARE}  # fmt: skip

    # 3. better than climatology
    c3: dict[str, Any] = {
          "ht_logloss": [float(ll.mean()), float(ll_b.mean())], "ht_logloss_diff": _ci(ll - ll_b),
          "ht_rps": [float(rps.mean()), float(rps_b.mean())], "ht_rps_diff": _ci(rps - rps_b),
          "htft_logloss": [float(lj.mean()), float(lj_b.mean())],
          "htft_logloss_diff": _ci(lj - lj_b)}  # fmt: skip
    c3["passes"] = all(c3[k][2] < 0 for k in ("ht_logloss_diff", "ht_rps_diff",
                                               "htft_logloss_diff"))  # fmt: skip
    out["c3"] = c3

    # 4. calibration: ECE no more than 0.005 above its noise floor
    ece = {}
    ou = np.array(t["ht_ou"].to_list())
    ysum = (t["xh"] + t["xa"]).to_numpy()
    series = {"ht_home": (p_ht[:, 0], y_ht == 0), "ht_draw": (p_ht[:, 1], y_ht == 1),
              "ht_away": (p_ht[:, 2], y_ht == 2), "ht_over_1.5": (ou[:, 1], ysum >= 2)}  # fmt: skip
    for k, (p, y) in series.items():
        e, null = kb.ece(p, y.astype(float)), kb.ece_null(p)
        ece[k] = {"ece": e, "noise_floor": null, "passes": e <= null + ECE_MARGIN}
    out["c4"] = {"ece": ece, "passes": all(v["passes"] for v in ece.values())}

    # 5. segments
    seg: dict[str, Any] = {}
    worst = 0.0
    for lid in sorted(set(t["league_id"].to_list())):
        s = (t["league_id"] == lid).to_numpy()
        rel = float(ll[s].mean() / ll_b[s].mean() - 1)
        seg[str(lid)] = {"n": int(s.sum()), "ht_logloss_rel_vs_clim": rel}
        worst = max(worst, rel)
        if chosen != "S0":
            c, c0 = t[f"cond_{chosen}"].to_numpy()[s], t["cond_S0"].to_numpy()[s]
            r2 = float(c.mean() / c0.mean() - 1)
            seg[str(lid)]["cond_rel_vs_S0"] = r2
            worst = max(worst, r2)
    out["c5"] = {"leagues": seg, "worst": worst, "passes": worst <= SEGMENT_MAX}

    # reported, not gated
    rep: dict[str, Any] = {}
    rep["conditional"] = {v: float(rows[f"cond_{v}"].drop_nulls().mean())  # type: ignore[arg-type]
                          for v in hm.VARIANTS}  # fmt: skip
    rep["htft_ece"] = {hs.HTFT[k]: kb.ece(p_htft[:, k], (y_htft == k).astype(float))
                       for k in range(9)}  # fmt: skip
    top = t["exp_total"].to_numpy() >= np.quantile(t["exp_total"].to_numpy(), TOP_FIFTH)
    rep["top_fifth_ece"] = {
        "n": int(top.sum()),
        "ht_over_0.5": kb.ece(ou[top, 0], (ysum[top] >= 1).astype(float)),
        "ht_over_1.5": kb.ece(ou[top, 1], (ysum[top] >= 2).astype(float)),
        "noise_floor_0.5": kb.ece_null(ou[top, 0]), "noise_floor_1.5": kb.ece_null(ou[top, 1]),
    }  # fmt: skip
    level = (t["xh"] == t["xa"]).to_numpy()
    second = ((t["hg"] + t["ag"]) - (t["xh"] + t["xa"])).to_numpy()
    rep["second_half_if_level"] = {
        "observed": float(second[level].mean()),
        "implied": float(t["second_if_level"].to_numpy().sum() / t["p_level"].to_numpy().sum()),
        "n_level": int(level.sum()),
    }
    disp: dict[str, list[float]] = {}
    for d in dates:
        for k, q in sh.gates[(chosen, d)]["ppc"].items():
            disp.setdefault(k, []).append(q)
    rep["ppc_quantiles"] = {k: {"min": min(v), "median": float(np.median(v)), "max": max(v),
                                "in_band": sum(hm.PPC_BAND[0] <= q <= hm.PPC_BAND[1] for q in v)}
                            for k, v in disp.items()}  # fmt: skip
    last = dates[-1]
    post, lids, tids = sh.post[(chosen, last)]
    shares = hm.expit(post["beta"][:, :, None] + post["alpha"][:, None, :]).mean(axis=0)
    rep["share_last_refit"] = {"as_of": last.isoformat(),
                               "home": dict(zip(map(str, lids), shares[0].round(4).tolist(),
                                                strict=True)),
                               "away": dict(zip(map(str, lids), shares[1].round(4).tolist(),
                                                strict=True)),
                               "team_u_sd": float(post["u"].mean(axis=0).std())}  # fmt: skip
    out["reported"] = rep
    out["passes"] = all(out[c]["passes"] for c in ("c1", "c2", "c3", "c4", "c5"))
    return out


def markdown(v: dict[str, Any]) -> str:
    c3, c4, c5 = v["c3"], v["c4"], v["c5"]
    f = "{:+.4f} [{:+.4f}, {:+.4f}]".format
    lines = [
        f"## P3b-1 dev: {'PASSES' if v['passes'] else 'FAILS'} (chosen {v['chosen']})", "",
        f"- targets {v['targets']}, scored {v['scored']} (out of scope {v['out_of_scope']})",
        f"- 1 coherence: {v['c1']['incoherent']} incoherent",
        *[f"- 2 {k}: n {s['n']}, {f(*s['diff'])} ({s['rel']:+.2%})"
          for k, s in v["selection"].items()],
        f"- 2 gates: {v['c2']['passing']}/{v['c2']['refits']} refits pass; served by "
        f"{v['c2']['served_by']}; failed {v['c2']['failed_by']}",
        f"- 3 HT 1X2 log loss {c3['ht_logloss'][0]:.4f} vs clim {c3['ht_logloss'][1]:.4f}: "
        f"{f(*c3['ht_logloss_diff'])}; RPS {f(*c3['ht_rps_diff'])}; HT/FT log loss "
        f"{c3['htft_logloss'][0]:.4f} vs {c3['htft_logloss'][1]:.4f}: "
        f"{f(*c3['htft_logloss_diff'])}",
        "- 4 ECE: " + "; ".join(f"{k} {x['ece']:.4f} (floor {x['noise_floor']:.4f})"
                                for k, x in c4["ece"].items()),
        f"- 5 worst league {c5['worst']:+.2%}",
    ]  # fmt: skip
    r = v["reported"]
    lines.append(f"- reported: conditional {r['conditional']}; 2H if level observed "
                 f"{r['second_half_if_level']['observed']:.3f} vs implied "
                 f"{r['second_half_if_level']['implied']:.3f}; top fifth "
                 f"{r['top_fifth_ece']}")  # fmt: skip
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session
    from adatta.model import backtest as bt

    ap = argparse.ArgumentParser(prog="python -m adatta.eval.half_split")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("matrices")
    m.add_argument("--bundle", required=True)
    m.add_argument("--season", type=int, required=True)
    m.add_argument("--upload", required=True)
    a = sub.add_parser("assemble")
    a.add_argument("--matrices", required=True)
    a.add_argument("--shares", required=True)
    a.add_argument("--upload", default="")
    args = ap.parse_args(argv)
    cfg = harness.load_config()
    store = make_store(get_settings())
    if args.cmd == "matrices":
        leagues = cfg.bundles[args.bundle]
        t0 = time.monotonic()
        rows = run_matrices(bt._load(leagues, xg=True), leagues, args.bundle, args.season, store)
        store.put(matrix_key(args.upload, args.bundle, args.season), _parquet(rows))
        print(json.dumps({"bundle": args.bundle, "season": args.season, "targets": rows.height,
                          "seconds": round(time.monotonic() - t0)}))  # fmt: skip
        return
    with get_session() as s:
        df = hm.load(s, hm.leagues(cfg))
        s.rollback()
    parts = []
    for b in cfg.bundles:
        for season in DEV:
            raw = try_get(store, matrix_key(args.matrices, b, season))
            if raw is None:
                raise SystemExit(f"matrix part {b} {season} of run {args.matrices} is missing")
            parts.append(pl.read_parquet(io.BytesIO(raw)))
    mx = pl.concat(parts)
    sh = Shares(store, args.shares, [d for d, _, _ in hm.share_plan(df, cfg, DEV)])
    rows, meta = score(mx, df, sh, cfg)
    chosen, steps = choose(rows)
    mk = markets(rows, mx, sh, chosen)
    v = gate(rows, mk, sh, chosen, steps, meta)
    if args.upload:
        store.put(f"{PREFIX}/v1/{args.upload}/rows.parquet",
                  _parquet(rows.join(mk, on="match_id", how="left")))  # fmt: skip
        store.put(f"{PREFIX}/v1/{args.upload}/verdict.json", json.dumps(v, default=str).encode())
    print(markdown(v))


if __name__ == "__main__":
    main()
