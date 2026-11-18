"""P3-d0 player rates, Layer 5 (docs/PHASE3_TASKS.md, spec §7.4): the hyperparameter fit,
sampled with NumPyro (D10); the model dependency group, CI only (D13).

Per statistic, Poisson within a player and θ_i ~ Gamma(k, k) integrated out in closed form
(the Poisson-Gamma marginal of a player's per-appearance counts):
    log p = Σ_m y_m log λ_m + Σ_i [lgamma(k+Y_i) - lgamma(k) + k log k - (k+Y_i) log(k+Λ_i)]
(+ const), with λ_m = e_m exp(L[cell_m]) and Λ_i = Σ_m λ_m. Only the hyperparameters are
sampled: log_mu[line], log_l[league] (the first league the reference), b (substitute, new
club, return), log_k[line], and in the slot variant (d) a slot term pooled towards 0.
Goalkeepers have no player term except for yellow cards (their likelihood is -Λ_i).
Appearances in a cell share L, so the data enter as sums: Y per cell, E per player x cell.

CLI (CI): `warmup --upload RUN` fixes the dispersion family and R1's c on warm-up;
`run --season S --upload RUN --warmup W` scores one dev season's 28-day refits;
`assemble --fetch RUN` applies the pass rule and prints metrics only (the repo is public).
"""

from __future__ import annotations

import io
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.eval import rates as ev
from adatta.eval.selection import DEV, HoldoutRefused
from adatta.features import rates as fr
from adatta.features.referees import RESULT_LAG

Arr = NDArray[np.float64]
IntArr = NDArray[np.int64]
PREFIX = "rates"
REFIT_DAYS = 28  # D14
TRAIN_DAYS = 730  # as P3-b amendment 7 and P3-c
SEED = 20261118
DRAWS = 1000
CHAINS = 4
PRED_DRAWS = 40
PPC_BAND = (0.05, 0.95)  # a warning only (as P3-b amendment 8)
WARMUP_REFIT = datetime(2018, 7, 1, tzinfo=UTC)  # fit on 2016-2017 ...
WARMUP_SCORE = 2018  # ... score season 2018
SLOT_SEASONS = (2022, 2023)  # positional league-seasons (P3-c amendment 1)


@dataclass(frozen=True)
class Train:
    cells: IntArr  # used cell ids
    Ycell: Arr
    pair_p: IntArr
    pair_c: IntArr  # index into cells
    pair_E: Arr
    Y: Arr  # per player
    kline: IntArr
    fixed: NDArray[np.bool_]
    row_p: IntArr  # per appearance, for the PPC and the dispersion fit
    row_c: IntArr
    row_e: Arr
    row_y: Arr
    row_li: IntArr


def train_arrays(train: pl.DataFrame, stat: str) -> Train:
    """`train` carries the design columns (Design.frame)."""
    y = f"y_{stat}"
    t = train.filter(pl.col(y).is_not_null())
    players = t["player_id"].unique().sort()
    t = t.with_columns(p=pl.col("player_id").replace_strict(
        {v: i for i, v in enumerate(players.to_list())}, return_dtype=pl.Int64))  # fmt: skip
    cells = np.unique(t["cell"].to_numpy()).astype(np.int64)
    t = t.with_columns(c=pl.Series(np.searchsorted(cells, t["cell"].to_numpy())))
    pairs = t.group_by("p", "c").agg(pl.col("e").sum()).sort("p", "c")
    yc = t.group_by("c").agg(pl.col(y).sum()).sort("c")
    Ycell = np.zeros(len(cells))
    Ycell[yc["c"].to_numpy()] = yc[y].to_numpy()
    per = t.group_by("p").agg(pl.col(y).sum().alias("Y")).sort("p")
    kl = (t.group_by("p", "li").agg(pl.len().alias("n"))
          .sort("p", "n", "li", descending=[False, True, False])
          .group_by("p", maintain_order=True).agg(pl.col("li").first()).sort("p"))  # fmt: skip
    kline = kl["li"].to_numpy().astype(np.int64)
    return Train(
        cells, Ycell, pairs["p"].to_numpy().astype(np.int64),
        pairs["c"].to_numpy().astype(np.int64), pairs["e"].to_numpy().astype(float),
        per["Y"].to_numpy().astype(float), kline, ev.fixed(stat, kline),
        t["p"].to_numpy().astype(np.int64), t["c"].to_numpy().astype(np.int64),
        t["e"].to_numpy().astype(float), t[y].to_numpy().astype(float),
        t["li"].to_numpy().astype(np.int64),
    )  # fmt: skip


def build_model(tr: Train, d: ev.Design) -> Any:
    import pymc as pm
    import pytensor.tensor as pt

    x = d.decode(tr.cells)
    ng = len(d.leagues)
    with pm.Model() as m:
        log_mu = pm.Normal("log_mu", -1.5, 2.0, shape=len(ev.LINES))
        log_l = pm.Normal("log_l", 0.0, 0.5, shape=ng - 1)
        b = pm.Normal("b", 0.0, 0.5, shape=3)
        log_k = pm.Normal("log_k", 1.0, 1.5, shape=len(ev.LINES))
        lg = pt.concatenate([pt.zeros(1), log_l])
        L = (log_mu[x["li"]] + lg[x["gi"]] + b[0] * x["sub"] + b[1] * x["new"]
             + b[2] * x["ret"])  # fmt: skip
        if d.slots:
            tau = pm.HalfNormal("tau", 0.3)
            z = pm.Normal("z", 0.0, 1.0, shape=len(d.slots))
            slot = pm.Deterministic("slot", tau * z)
            L = L + pt.concatenate([pt.zeros(1), slot])[x["si"]]
        lam = tr.pair_E * pt.exp(L[tr.pair_c])
        Lam = pt.inc_subtensor(pt.zeros(len(tr.Y))[tr.pair_p], lam)
        k = pt.exp(log_k)[tr.kline]
        th = (pt.gammaln(k + tr.Y) - pt.gammaln(k) + k * pt.log(k)
              - (k + tr.Y) * pt.log(k + Lam))  # fmt: skip
        pm.Potential("ll", pt.sum(tr.Ycell * L) + pt.sum(pt.where(tr.fixed, -Lam, th)))
    return m


def fit(tr: Train, d: ev.Design, draws: int = DRAWS, seed: int = SEED) -> Any:
    import pymc as pm

    with build_model(tr, d):
        return pm.sample(draws=draws, tune=draws, chains=CHAINS, nuts_sampler="numpyro",
                         random_seed=seed, target_accept=0.9, progressbar=False)  # fmt: skip


def posterior(idata: Any, n: int = PRED_DRAWS) -> dict[str, Arr]:
    post = idata["posterior"]
    post = post.to_dataset() if hasattr(post, "to_dataset") else post
    out = {}
    for v in post.data_vars:
        arr = np.asarray(post[v].values)
        flat = arr.reshape(-1, *arr.shape[2:])
        step = max(len(flat) // n, 1)
        out[str(v)] = flat[::step][:n]
    return out


def ppc(post: dict[str, Arr], tr: Train, d: ev.Design, rng: np.random.Generator,
        ) -> dict[str, float]:  # fmt: skip
    """Per line: quantile of the observed zero share and variance-to-mean of per-appearance
    counts in their prior-predictive given the hyperparameters (θ drawn afresh)."""
    L = ev.log_rates(post, d, tr.cells)  # (D, cells)
    lam = tr.row_e[None, :] * np.exp(L[:, tr.row_c])
    k = np.exp(post["log_k"][:, tr.kline])  # (D, players)
    th = np.where(tr.fixed[None, :], 1.0, rng.gamma(k, 1.0 / k))
    sim = rng.poisson(lam * th[:, tr.row_p])
    out: dict[str, float] = {}
    for li, name in enumerate(ev.LINES):
        on = tr.row_li == li
        if on.sum() < 50:
            continue
        obs, s = tr.row_y[on], sim[:, on]
        out[f"{name}_zero"] = float(((s == 0).mean(axis=1) < (obs == 0).mean()).mean())
        vm = s.var(axis=1) / np.maximum(s.mean(axis=1), 1e-9)
        out[f"{name}_vm"] = float((vm < obs.var() / max(obs.mean(), 1e-9)).mean())
    return out


def phis(post: dict[str, Arr], d: ev.Design, tr: Train, stat: str,
         lines: Sequence[int]) -> dict[int, float]:  # fmt: skip
    """NB size per appearance line, by maximum likelihood with θ at its posterior mean."""
    if not lines:
        return {}
    lam, th = ev.theta_hat(post, d, tr.cells[tr.row_c], tr.row_e, tr.row_p, tr.Y, tr.kline, stat)
    return {li: ev.fit_phi(tr.row_y[tr.row_li == li], (lam * th)[tr.row_li == li]) for li in lines}


def one_fit(train: pl.DataFrame, d: ev.Design, stat: str, draws: int,
            ) -> tuple[Train, dict[str, Arr], dict[str, Any]]:  # fmt: skip
    from adatta.model import diagnostics as dg

    tr = train_arrays(train, stat)
    t0 = time.monotonic()
    idata = fit(tr, d, draws)
    fit_s = time.monotonic() - t0
    checks = dg.convergence(idata)
    post = posterior(idata)
    q = ppc(post, tr, d, np.random.default_rng(SEED))
    diag: dict[str, Any] = {
        "stat": stat, "variant": "slot" if d.slots else "line", "train_rows": len(tr.row_y),
        "players": len(tr.Y), "fit_s": fit_s, **{c.name: c.value for c in checks},
        "worst": {c.name: c.detail for c in checks if c.detail},
        "gates_ok": all(c.passed for c in checks if c.blocking),
        "ppc": q, "ppc_ok": all(PPC_BAND[0] <= v <= PPC_BAND[1] for v in q.values()),
        "b": post["b"].mean(axis=0).tolist(), "log_k": post["log_k"].mean(axis=0).tolist(),
    }  # fmt: skip
    return tr, post, diag


def _frame(table: pl.DataFrame, d: ev.Design) -> pl.DataFrame:
    return d.frame(table).with_columns(as_of=pl.col("kickoff") - fr.HORIZON,
                                       final_at=pl.col("kickoff") + RESULT_LAG)  # fmt: skip


def _window(rows: pl.DataFrame, t: datetime) -> pl.DataFrame:
    return rows.filter((pl.col("final_at") <= t)
                       & (pl.col("kickoff") > t - timedelta(days=TRAIN_DAYS)))  # fmt: skip


# ------------------------------------------------------------------------------ warm-up


def warmup(table: pl.DataFrame, leagues: Sequence[str], draws: int = DRAWS,
           stats: Sequence[str] = fr.STATS) -> dict[str, Any]:  # fmt: skip
    """The dispersion family per statistic x line and R1's c per statistic: fitted on
    2016-2017 (the 730 days before WARMUP_REFIT), scored on season 2018."""
    d = ev.Design(tuple(leagues))
    rows = _frame(table, d)
    train = _window(rows, WARMUP_REFIT)
    block = rows.filter(pl.col("season") == WARMUP_SCORE)
    rng = np.random.default_rng(SEED)
    pois, nb, fits, c_grid, phi_all = [], [], [], {}, {}
    for stat in stats:
        tr, post, diag = one_fit(train, d, stat, draws)
        fits.append({**diag, "refit": WARMUP_REFIT.isoformat()})
        b = ev.block_arrays(rows, block, stat)
        pois.append(ev.scored(b, stat, "pois", ev.predict(post, d, b, stat)))
        ph = phis(post, d, tr, stat, range(len(ev.LINES)))
        phi_all[stat] = {ev.LINES[k]: v for k, v in ph.items()}
        nb.append(ev.scored(b, stat, "nb", ev.predict(post, d, b, stat, ph, rng)))
        r0 = ev.r0_table(train, stat, len(leagues))
        c_grid[stat] = {str(c): float(ev.baselines(b, r0, c)["R1"].log_loss.mean())
                        for c in ev.C_GRID}  # fmt: skip
        print(f"warm-up {stat}: fit {diag['fit_s']:.0f} s, gates "
              f"{'ok' if diag['gates_ok'] else 'FAIL'}", flush=True)  # fmt: skip
    choice = ev.dispersion_choice(pl.concat(pois), pl.concat(nb))
    c = {s: float(min(g, key=lambda k: g[k])) for s, g in c_grid.items()}
    return {"dispersion": choice, "c": c, "c_grid": c_grid, "phi_warmup": phi_all,
            "fits": fits}  # fmt: skip


# --------------------------------------------------------------------------- walk-forward


def run_part(table: pl.DataFrame, leagues: Sequence[str], slots: Sequence[str], season: int,
             choices: dict[str, Any], draws: int = DRAWS, part: tuple[int, int] = (0, 1),
             stats: Sequence[str] = fr.STATS, slot_variant: bool = True,
             on_refit: Callable[[pl.DataFrame, list[dict[str, Any]]], None] | None = None,
             ) -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """Score one dev season: hyperparameters refitted every 28 days on the TRAIN_DAYS of
    appearances final by the refit time; each row's player history as of kickoff - 24 h."""
    from adatta.eval import harness

    if season not in DEV:
        raise HoldoutRefused(f"season {season} is not a dev season")
    d = ev.Design(tuple(leagues))
    ds = ev.Design(tuple(leagues), tuple(slots))
    rows = _frame(table, d)
    rows_s = _frame(table, ds)
    dev = rows.filter(pl.col("season").is_in(DEV))
    times = harness.as_of_times(dev["as_of"].min(), dev["as_of"].max(), REFIT_DAYS)  # type: ignore[arg-type]
    blocks = []
    for i, t in enumerate(times):
        nxt = times[i + 1] if i + 1 < len(times) else datetime.max.replace(tzinfo=UTC)
        on = (pl.col("season") == season) & (pl.col("as_of") >= t) & (pl.col("as_of") < nxt)
        if not rows.filter(on).is_empty():
            blocks.append((t, on))
    rng = np.random.default_rng(SEED)
    do_slot = slot_variant and bool(slots) and season in SLOT_SEASONS
    out: list[pl.DataFrame] = []
    diags: list[dict[str, Any]] = []
    for t, on in blocks[part[0] :: part[1]]:
        train, block = _window(rows, t), rows.filter(on)
        fits = []
        for stat in stats:
            tr, post, diag = one_fit(train, d, stat, draws)
            ph = phis(post, d, tr, stat, ev.phi_lines(choices["dispersion"], stat))
            b = ev.block_arrays(rows, block, stat)
            out.append(ev.scored(b, stat, "model", ev.predict(post, d, b, stat, ph, rng)))
            r0 = ev.r0_table(train, stat, len(leagues))
            for name, p in ev.baselines(b, r0, choices["c"][stat]).items():
                out.append(ev.scored(b, stat, name, p))
            fits.append({**diag, "phi": {ev.LINES[k]: v for k, v in ph.items()}})
            if do_slot:
                _, post_s, diag_s = one_fit(_window(rows_s, t), ds, stat, draws)
                blk = rows_s.filter(on & pl.col("slot").is_not_null())
                bs = ev.block_arrays(rows_s, blk, stat)
                out.append(ev.scored(bs, stat, "slot", ev.predict(post_s, ds, bs, stat, ph, rng)))
                fits.append(diag_s)
        diags.append({"refit": t.isoformat(), "rows": len(block), "fits": fits})
        g = fits
        print(f"refit {t:%Y-%m-%d}: {len(block)} rows; fits {len(g)}, gates fail "
              f"{sum(not f['gates_ok'] for f in g)}; worst rhat {max(f['rhat'] for f in g):.3f}; "
              f"fit {sum(f['fit_s'] for f in g):.0f} s", flush=True)  # fmt: skip
        for f in g:
            f["refit"] = t.isoformat()
        if on_refit is not None:
            on_refit(pl.concat(out), diags)
    return (pl.concat(out) if out else pl.DataFrame()), diags


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    from adatta.adapters.storage import make_store
    from adatta.config import get_settings

    p = argparse.ArgumentParser(prog="adatta.model.rates")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("warmup")
    w.add_argument("--upload", required=True)
    w.add_argument("--draws", type=int, default=DRAWS)
    w.add_argument("--table", default="latest")
    r = sub.add_parser("run")
    r.add_argument("--season", type=int, required=True)
    r.add_argument("--upload", required=True)
    r.add_argument("--warmup", required=True)
    r.add_argument("--draws", type=int, default=DRAWS)
    r.add_argument("--table", default="latest")
    r.add_argument("--part", default="0/1")
    r.add_argument("--stats", default=",".join(fr.STATS))
    a = sub.add_parser("assemble")
    a.add_argument("--fetch", required=True)
    a.add_argument("--warmup", required=True)
    a.add_argument("--seasons", required=True)
    a.add_argument("--parts", type=int, default=1)
    a.add_argument("--table", default="latest")
    args = p.parse_args(argv)
    store = make_store(get_settings())
    table, meta, digest = fr.load(store, args.table)
    leagues = meta["leagues"]
    if args.cmd == "warmup":
        choices = warmup(table, leagues, args.draws)
        choices["table"] = digest
        store.put(f"{PREFIX}/warmup/{args.upload}/choices.json", json.dumps(choices).encode())
        nb = [f"{r['stat']}/{r['line']}" for r in choices["dispersion"] if r["family"] == "nb"]
        print(json.dumps({"table": digest, "nb": nb, "c": choices["c"],
                          "gates_ok": all(f["gates_ok"] for f in choices["fits"])}))  # fmt: skip
        return
    choices = json.loads(store.get(f"{PREFIX}/warmup/{args.warmup}/choices.json"))
    if args.cmd == "run":
        i, n = (int(x) for x in args.part.split("/"))
        key = f"{PREFIX}/parts/{args.upload}/{args.season}_{i}of{n}"

        def save(scored: pl.DataFrame, diags: list[dict[str, Any]]) -> None:
            store.put(f"{key}.parquet", _parquet(scored))
            store.put(f"{key}.json", json.dumps({"table": digest, "diags": diags}).encode())

        scored, diags = run_part(table, leagues, meta["slots"], args.season, choices, args.draws,
                                 (i, n), args.stats.split(","), on_refit=save)  # fmt: skip
        save(scored, diags)
        fits = [f for dg_ in diags for f in dg_["fits"]]
        print(f"{args.season}: {len(diags)} refits, {len(fits)} fits, "
              f"{sum(f['gates_ok'] for f in fits)} pass the gates; median fit "
              f"{np.median([f['fit_s'] for f in fits]):.0f} s")  # fmt: skip
        return
    parts, diags = [], []
    for s in args.seasons.split(","):
        for i in range(args.parts):
            key = f"{PREFIX}/parts/{args.fetch}/{s}_{i}of{args.parts}"
            parts.append(pl.read_parquet(io.BytesIO(store.get(f"{key}.parquet"))))
            diags += json.loads(store.get(f"{key}.json"))["diags"]
    allrows = pl.concat(parts)
    main_ = allrows.filter(pl.col("model").is_in(["model", "R0", "R1"]))
    slot = pl.concat([
        allrows.filter(pl.col("model") == "slot"),
        main_.filter((pl.col("model") == "model") & pl.col("positional")
                     & pl.col("season").is_in(SLOT_SEASONS)),
    ])  # fmt: skip
    ok = all(f["gates_ok"] for dg_ in diags for f in dg_["fits"])
    v = ev.verdict(main_, meta, ok)
    print(ev.markdown(main_, slot, v, diags, meta, choices, digest))
    store.put(f"{PREFIX}/reports/{args.fetch}/scored.parquet", _parquet(allrows))


if __name__ == "__main__":
    main()
