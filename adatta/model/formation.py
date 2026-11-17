"""P3-c (c) formation model (docs/PHASE3_TASKS.md, spec §7.3): a conditional multinomial
logit over the formation vocabulary, sampled with NumPyro (D10); the model dependency group,
CI only (D13). Covariates only, no per-team random effects (the P3-b lesson, amendment 7).

Utility of formation k for a team-match:
    a_k + b_team log(team share_k) + b_coach log(coach mix_k) + b_last last_k
        + tier g_k + home h_k + must_win m_k + dead_rubber r_k
with the first class as the reference for the per-class terms.

CLI (CI): `run --bundle B --season S --upload RUN` fits the 28-day refits of one dev
season of one bundle (each on the 730 days before it) and stores the scored rows in R2;
`assemble --fetch RUN` applies the pass rule and prints metrics only (the repo is public).
"""

from __future__ import annotations

import io
import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.eval import formation as ev
from adatta.eval.selection import DEV, WARMUP, HoldoutRefused
from adatta.features import formation as fe
from adatta.features.referees import RESULT_LAG

Arr = NDArray[np.float64]
PREFIX = "formation"
REFIT_DAYS = 28  # D14
TRAIN_DAYS = 730  # as P3-b amendment 7
SEED = 20261117
DRAWS = 500
CHAINS = 4
PRED_DRAWS = 200
EPS = 1e-4  # floor inside the log shares
PPC_BAND = (0.05, 0.95)  # a warning only (as P3-b amendment 8)
PER_CLASS = ["tier", "home", "must_win", "dead_rubber"]


def design(rows: pl.DataFrame, k: int) -> dict[str, Arr]:
    return {
        "ts": np.log(ev.matrix(rows, "ts", k) + EPS),
        "cm": np.log(ev.matrix(rows, "cm", k) + EPS),
        "last": ev.matrix(rows, "last", k),
        "z": rows.select(PER_CLASS).to_numpy().astype(float),
    }


def build_model(train: pl.DataFrame, k: int) -> Any:
    import pymc as pm
    import pytensor.tensor as pt

    x = design(train, k)
    y = train["y"].to_numpy()
    with pm.Model() as m:
        a = pm.Normal("a", 0.0, 2.0, shape=k - 1)
        b = pm.Normal("b", 0.0, 1.0, shape=3)
        g = pm.Normal("g", 0.0, 1.0, shape=(len(PER_CLASS), k - 1))
        G = pt.concatenate([pt.zeros((len(PER_CLASS), 1)), g], axis=1)  # (z, k)
        per = pt.concatenate([pt.zeros(1), a])[None, :] + pt.dot(x["z"], G)  # (n, k)
        u = per + b[0] * x["ts"] + b[1] * x["cm"] + b[2] * x["last"]
        pm.Categorical("y", logit_p=u, observed=y)
    return m


def fit(train: pl.DataFrame, k: int, draws: int = DRAWS, seed: int = SEED) -> Any:
    import pymc as pm

    with build_model(train, k):
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


def probs(post: dict[str, Arr], rows: pl.DataFrame, k: int) -> Arr:
    """(draws, n, k) softmax probabilities."""
    x = design(rows, k)
    d = len(post["a"])
    a = np.concatenate([np.zeros((d, 1)), post["a"]], axis=1)  # (d, k)
    g = np.concatenate([np.zeros((d, len(PER_CLASS), 1)), post["g"]], axis=2)  # (d, z, k)
    b = post["b"]
    u = (a[:, None, :] + np.einsum("nz,dzk->dnk", x["z"], g)
         + b[:, 0, None, None] * x["ts"][None] + b[:, 1, None, None] * x["cm"][None]
         + b[:, 2, None, None] * x["last"][None])  # fmt: skip
    u -= u.max(axis=2, keepdims=True)
    e = np.exp(u)
    return np.asarray(e / e.sum(axis=2, keepdims=True), dtype=np.float64)


def ppc_top(post: dict[str, Arr], train: pl.DataFrame, k: int,
            rng: np.random.Generator) -> float:  # fmt: skip
    """Quantile of the observed count of the most common class in its posterior predictive."""
    p = probs(post, train, k)
    y = train["y"].to_numpy()
    top = int(np.bincount(y, minlength=k).argmax())
    obs = float((y == top).sum())
    sims = [float((rng.random(len(y)) < p[i, :, top]).sum()) for i in range(len(p))]
    return float((np.array(sims) < obs).mean())


# ---------------------------------------------------------------------------- walk-forward


def run_part(table: pl.DataFrame, vocab: list[str], leagues: list[str], season: int,
             draws: int = DRAWS, part: tuple[int, int] = (0, 1),
             on_refit: Callable[[pl.DataFrame, list[dict[str, Any]]], None] | None = None,
             ) -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """Score one dev season of one bundle: baselines from warm-up, the model refitted every
    28 days on the TRAIN_DAYS of team-matches final by the refit time."""
    from adatta.eval import harness
    from adatta.model import diagnostics as dg

    k = len(vocab)
    if season not in DEV:
        raise HoldoutRefused(f"season {season} is not a dev season")
    rows = table.filter(pl.col("league_ref").is_in(leagues)).with_columns(
        as_of=pl.col("kickoff") - fe.HORIZON, final_at=pl.col("kickoff") + RESULT_LAG
    )
    base = ev.fit_baselines(rows.filter(pl.col("season").is_in(WARMUP)), k)
    dev = rows.filter(pl.col("season").is_in(DEV))
    times = harness.as_of_times(dev["as_of"].min(), dev["as_of"].max(), REFIT_DAYS)  # type: ignore[arg-type]
    target = rows.filter(pl.col("season") == season)
    blocks = []
    for i, t in enumerate(times):
        nxt = times[i + 1] if i + 1 < len(times) else datetime.max.replace(tzinfo=UTC)
        block = target.filter((pl.col("as_of") >= t) & (pl.col("as_of") < nxt))
        if not block.is_empty():
            blocks.append((t, block))
    out, diags = [], []
    for t, block in blocks[part[0] :: part[1]]:
        train = rows.filter((pl.col("final_at") <= t)
                            & (pl.col("kickoff") > t - timedelta(days=TRAIN_DAYS)))  # fmt: skip
        t0 = time.monotonic()
        idata = fit(train, k, draws)
        fit_s = time.monotonic() - t0
        checks = dg.convergence(idata)
        post = posterior(idata)
        q = ppc_top(post, train, k, np.random.default_rng(SEED))
        p_model = probs(post, block, k).mean(axis=0)
        parts = [ev.score(block, p_model, vocab).with_columns(model=pl.lit("model"))]
        for name, p in ev.baseline_probs(base, block, k).items():
            parts.append(ev.score(block, p, vocab).with_columns(model=pl.lit(name)))
        out.append(pl.concat(parts))
        diag: dict[str, Any] = {
            "refit": t.isoformat(), "train_rows": len(train), "rows": len(block),
            "fit_s": fit_s, **{c.name: c.value for c in checks},
            "worst": {c.name: c.detail for c in checks if c.detail},
            "gates_ok": all(c.passed for c in checks if c.blocking),
            "ppc_q": q, "ppc_ok": PPC_BAND[0] <= q <= PPC_BAND[1],
        }  # fmt: skip
        diags.append(diag)
        g = diag
        print(f"refit {t:%Y-%m-%d}: train {len(train)} rows, fit {fit_s:.0f} s; "
              f"rhat {g['rhat']:.3f}, ess {min(g['ess_bulk'], g['ess_tail']):.0f}, "
              f"div {g['divergences']:.0f}, ppc q {q:.2f}; "
              f"gates {'ok' if g['gates_ok'] else 'FAIL'}", flush=True)  # fmt: skip
        if on_refit is not None:
            on_refit(pl.concat(out), diags)
    return (pl.concat(out) if out else pl.DataFrame()), diags


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def strong_baseline(table: pl.DataFrame, k: int) -> str:
    """The stronger baseline on warm-up (in-sample log loss)."""
    warm = table.filter(pl.col("season").is_in(WARMUP))
    base = ev.fit_baselines(warm, k)
    y = warm["y"].to_numpy()
    ll = {n: float(-np.log(p[np.arange(len(y)), y] + 1e-9).mean())
          for n, p in ev.baseline_probs(base, warm, k).items()}  # fmt: skip
    return "F2" if ll["F2"] <= ll["F1"] else "F1"


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.eval import harness

    p = argparse.ArgumentParser(prog="adatta.model.formation")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--season", type=int, required=True)
    r.add_argument("--draws", type=int, default=DRAWS)
    r.add_argument("--upload", required=True)
    r.add_argument("--table", default="latest")
    r.add_argument("--part", default="0/1")
    a = sub.add_parser("assemble")
    a.add_argument("--fetch", required=True)
    a.add_argument("--bundles", required=True)
    a.add_argument("--seasons", required=True)
    a.add_argument("--table", default="latest")
    a.add_argument("--parts", type=int, default=1)
    args = p.parse_args(argv)
    store = make_store(get_settings())
    table, meta, digest = ev.load_table(store, args.table)
    vocab = meta["vocab"]
    k = len(vocab)
    cfg = harness.load_config()
    if args.cmd == "run":
        leagues = [str(x) for x in cfg.bundles[args.bundle]]
        i, n = (int(x) for x in args.part.split("/"))
        key = f"{PREFIX}/parts/{args.upload}/{args.bundle}_{args.season}_{i}of{n}"

        def save(scored: pl.DataFrame, diags: list[dict[str, Any]]) -> None:
            store.put(f"{key}.parquet", _parquet(scored))
            store.put(f"{key}.json", json.dumps({"table": digest, "diags": diags}).encode())

        scored, diags = run_part(table, vocab, leagues, args.season, args.draws, (i, n),
                                 on_refit=save)  # fmt: skip
        save(scored, diags)
        ok = sum(d["gates_ok"] for d in diags)
        print(f"{args.bundle} {args.season}: {len(diags)} refits, {ok} pass the gates; "
              f"median fit {np.median([d['fit_s'] for d in diags]):.0f} s")  # fmt: skip
        return
    parts, diags = [], []
    for b in args.bundles.split(","):
        for s in args.seasons.split(","):
            for i in range(args.parts):
                key = f"{PREFIX}/parts/{args.fetch}/{b}_{s}_{i}of{args.parts}"
                parts.append(pl.read_parquet(io.BytesIO(store.get(f"{key}.parquet"))))
                diags += json.loads(store.get(f"{key}.json"))["diags"]
    scored = pl.concat(parts)
    v = ev.verdict(scored, meta, strong_baseline(table, k), all(d["gates_ok"] for d in diags),
                   vocab)  # fmt: skip
    print(ev.markdown(scored, v, diags, meta, digest))
    store.put(f"{PREFIX}/reports/{args.fetch}/scored.parquet", _parquet(scored))


if __name__ == "__main__":
    main()
