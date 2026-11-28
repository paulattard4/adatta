"""D68: warm-started P refits (docs/PHASE1B_TASKS.md "8. Faster refits").

Each weekly refit starts from the previous refit of the same job: every chain from a
different stored posterior draw (no jitter), NumPyro's adaptation from the stored inverse
mass diagonal and step size, and TUNE warm-up iterations instead of 2,000. Draws, model,
priors and target_accept are P's. A refit starts cold (as today) at the first refit of a
job, after a gap of more than MAX_GAP_WEEKS weeks, or when the source refit failed a gate.

The model is `team_strength.build`, unchanged; this file is not one of the files hashed
for P (`backtest.HASHED_FILES`), so P's cached refits keep their hash. It joins the hash of
warm specs only (`backtest.WARM_FILE`).

CLI (CI, model group; one backtest-type run at a time), the dev check of the acceptance:
  run       --bundle B --season S --part i/n --upload RUN
            a contiguous block of a dev bundle-season's weekly refits: W (warm) fitted, P
            read from its cached refits (fitted only if missing), both scored, and the
            per-refit agreement of the two posteriors
  assemble  --fetch RUN --bundles ... --seasons ... --parts N
            the acceptance (adatta.eval.warm); metrics only to stdout, scored rows to R2
Holdout seasons are refused. The repo is public: logs carry metrics, never data.
"""

from __future__ import annotations

import argparse
import io
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.adapters.storage import RawStore, make_store, try_get
from adatta.config import get_settings

TUNE = 250  # warm-up iterations of a warm refit (D68; a cold refit keeps P's 2,000)
MAX_GAP_WEEKS = 4  # a longer gap since the source refit (summer, a suspension) starts cold
VAR_DRAWS = 1000  # draws behind each stored posterior variance (the mass matrix)
SUFFIX = ".warm.npz"  # the warm state, stored next to a refit's cached draws
PREFIX = "warm"
# the strength walks of team_strength.build: name -> its initial-level scale
WALKS = {"strength": "sigma_strength", "attack": "sigma_attack", "defence": "sigma_defence"}
# posterior means compared between the warm and the cold refit (the cached prediction set)
COMPARED = ("attack_last", "defence_last", "mu_league", "h_league", "h_team", "sigma_home",
            "rho", "kappa", "sigma_walk", "xg_shape", "xg_bias")  # fmt: skip

Arr = NDArray[np.float64]


@dataclass
class State:
    """What the next refit starts from."""

    start: datetime  # the source refit's first weekly step (FitData.start)
    season_start: NDArray[np.bool_]  # its per-step season starts (the walk's reset)
    coords: dict[str, list[Any]]  # its model coordinates, except "step"
    draws: dict[str, Arr]  # free variable -> (chains, *shape), constrained
    var: dict[str, Arr]  # value variable -> per-element posterior variance, unconstrained
    step_size: float
    passed: bool = True  # the source refit passed the gates


# ----------------------------------------------------------------------------- the plan


def blocks[T](plan: Sequence[T], k: int, n: int) -> list[T]:
    """Part k of n as a contiguous block (warm chains need consecutive refits; the cold
    runs take every n-th refit instead). Blocks differ in length by at most one."""
    if not 0 <= k < n:
        raise ValueError(f"part {k}/{n}: need 0 <= k < n")
    edges = np.linspace(0, len(plan), n + 1).round().astype(int)
    return list(plan[edges[k] : edges[k + 1]])


def gap_weeks(state: State | None, start: datetime) -> int | None:
    """Weeks since the source refit, or None where the refit starts cold."""
    if state is None or not state.passed:
        return None
    gap = (start - state.start).days // 7
    return gap if 0 <= gap <= MAX_GAP_WEEKS else None


# ----------------------------------------------------------------------- state storage


def encode(s: State) -> bytes:
    meta = {"start": s.start.isoformat(), "coords": s.coords, "step_size": s.step_size,
            "passed": s.passed}  # fmt: skip
    arrays: dict[str, Any] = {f"d__{k}": v for k, v in s.draws.items()}
    arrays |= {f"v__{k}": v for k, v in s.var.items()}
    arrays["season_start"] = s.season_start
    arrays["meta"] = np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8)
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def decode(data: bytes) -> State:
    with np.load(io.BytesIO(data)) as z:
        meta = json.loads(z["meta"].tobytes().decode())
        return State(
            start=datetime.fromisoformat(meta["start"]),
            season_start=z["season_start"].astype(bool),
            coords=meta["coords"],
            draws={k[3:]: z[k] for k in z.files if k.startswith("d__")},
            var={k[3:]: z[k] for k in z.files if k.startswith("v__")},
            step_size=float(meta["step_size"]),
            passed=bool(meta["passed"]),
        )


def _coords(model: Any) -> dict[str, list[Any]]:
    return {d: [x.item() if isinstance(x, np.generic) else x for x in v]
            for d, v in model.coords.items() if d != "step"}  # fmt: skip


def extract(idata: Any, model: Any, start: datetime, season_start: NDArray[np.bool_],
            seed: int, var_draws: int = VAR_DRAWS) -> State:  # fmt: skip
    """The warm state of a finished fit: one draw per chain, each chain's at a different
    random position, and each value variable's posterior variance on the sampler's
    (unconstrained) scale, from `var_draws` evenly spaced draws put through the model's own
    transforms (keeping the unconstrained trace instead doubled the fit's time and memory)."""
    import pytensor.tensor as pt

    post = idata.posterior
    rng = np.random.default_rng(seed)
    pick = rng.integers(post.sizes["draw"], size=post.sizes["chain"])
    draws = {}
    for rv in model.free_RVs:
        a = np.asarray(post[rv.name].values, dtype=np.float64)
        draws[rv.name] = np.stack([a[c, pick[c]] for c in range(a.shape[0])])
    var = {}
    for v in model.value_vars:
        rv = model.values_to_rvs[v]
        a = np.asarray(post[rv.name].values, dtype=np.float64)
        a = a.reshape((-1, *a.shape[2:]))
        a = a[np.linspace(0, len(a) - 1, min(var_draws, len(a))).round().astype(int)]
        t = model.rvs_to_transforms.get(rv)
        if t is not None:
            a = np.asarray(t.forward(pt.as_tensor(a), *rv.owner.inputs).eval(),
                           dtype=np.float64)  # fmt: skip
        var[v.name] = a.var(axis=0)
    step = float(np.median(np.asarray(idata.sample_stats["step_size"].values)))
    return State(start, np.asarray(season_start, dtype=bool), _coords(model), draws, var, step)


# ----------------------------------------------------------------------------- mapping


def remap(a: Arr, dims: Sequence[str | None], old: dict[str, list[Any]],
          new: dict[str, list[Any]], off: int) -> Arr:  # fmt: skip
    """`a` (axes `dims`, the source's coordinates) on the new refit's coordinates: "step"
    shifted by `off` weeks, every other named axis matched by coordinate value (a team id, a
    league id). Positions with no source take the mean of the matched ones along that axis;
    an axis named None is kept as it is."""
    out = np.asarray(a, dtype=np.float64)
    for ax, dim in enumerate(dims):
        if dim is None:
            continue
        n_new = len(new[dim])
        if dim == "step":
            idx = np.arange(n_new) + off
        else:
            pos = {c: i for i, c in enumerate(old[dim])}
            idx = np.array([pos.get(c, -1) for c in new[dim]], dtype=np.int64)
        ok = (idx >= 0) & (idx < out.shape[ax])
        taken = np.take(out, np.where(ok, idx, 0), axis=ax)
        shape = [1] * out.ndim
        shape[ax] = n_new
        mask = ok.reshape(shape)
        if ok.any():
            fill = np.take(out, idx[ok], axis=ax).mean(axis=ax, keepdims=True)
        else:
            fill = np.zeros_like(np.take(out, [0], axis=ax))
        out = np.where(mask, taken, fill)
    return out


def _dims(model: Any, name: str, ndim: int) -> list[str | None]:
    d = list(model.named_vars_to_dims.get(name, ()))
    return [None] * (ndim - len(d)) + d if len(d) < ndim else d[:ndim]


def _zerosum_axes(model: Any, rv: Any, ndim: int) -> tuple[int, ...]:
    from pymc.distributions.transforms import ZeroSumTransform

    t = model.rvs_to_transforms.get(rv)
    if not isinstance(t, ZeroSumTransform):
        return ()
    return tuple(int(a) % ndim for a in t.zerosum_axes)


def _old_coords(s: State) -> dict[str, list[Any]]:
    return {**s.coords, "step": list(range(len(s.season_start)))}


def inits(s: State, model: Any, off: int) -> list[dict[str, Arr]]:
    """Per-chain starting points (constrained) on the new refit. Each walk's initial level
    absorbs the innovations of the `off` dropped weeks, so the starting path at each kept
    week is the source draw's path at that week; zero-sum variables are re-centred."""
    from adatta.model import team_strength as ts

    old, new = _old_coords(s), {**_coords(model), "step": list(model.coords.get("step", []))}
    reset = np.where(s.season_start, np.sqrt(1.0 + ts.RESET_WEEKS), 1.0)
    chains = next(iter(s.draws.values())).shape[0]
    out = []
    for c in range(chains):
        d = {k: v[c] for k, v in s.draws.items()}
        for walk, s0 in WALKS.items():
            z0, z = f"{walk}_z0", f"{walk}_z"
            if off > 0 and {z0, z, s0, "sigma_walk"} <= set(d):
                innov = d[z] * (d["sigma_walk"] * reset)[:, None]
                d[z0] = d[z0] + innov[:off].sum(axis=0) / d[s0]
        point = {}
        for rv in model.free_RVs:
            if rv.name not in d:
                continue
            v = d[rv.name]
            x = remap(v, _dims(model, rv.name, v.ndim), old, new, off)
            for ax in _zerosum_axes(model, rv, x.ndim):
                x = x - x.mean(axis=ax, keepdims=True)
            point[rv.name] = x
        out.append(point)
    return out


def inverse_mass(s: State, model: Any, off: int) -> Arr:
    """The diagonal inverse mass matrix for NumPyro, flattened in `model.value_vars` order
    (the order PyMC passes the parameters). A zero-sum axis has one element fewer than its
    coordinates and no per-team meaning, so it takes its mean; a variable the source lacks
    gets 1 (NumPyro's default)."""
    old, new = _old_coords(s), {**_coords(model), "step": list(model.coords.get("step", []))}
    ip = model.initial_point()
    parts = []
    for v in model.value_vars:
        shape = np.shape(ip[v.name])
        var = s.var.get(v.name)
        if var is None:
            parts.append(np.ones(shape).ravel())
            continue
        rv = model.values_to_rvs[v]
        dims = _dims(model, rv.name, len(shape))
        for ax in _zerosum_axes(model, rv, len(shape)):
            var = np.repeat(var.mean(axis=ax, keepdims=True), shape[ax], axis=ax)
            dims[ax] = None
        x = remap(var, dims, old, new, off) if var.ndim == len(shape) else var
        if np.shape(x) != shape:
            x = np.full(shape, float(np.mean(var)))
        parts.append(np.maximum(x, 1e-8).ravel())
    return np.concatenate(parts) if parts else np.zeros(0)


# ----------------------------------------------------------------------------- sampling


def gates_ok(diag: dict[str, float]) -> bool:
    from adatta.model import diagnostics as dg

    return bool(diag["rhat_max"] <= dg.RHAT_MAX and diag["ess_bulk_min"] >= dg.ESS_MIN
                and diag["ess_tail_min"] >= dg.ESS_MIN
                and diag["divergences"] <= dg.DIVERGENCES_MAX)  # fmt: skip


class Chain:
    """The fitter of a warm spec: remembers the last refit's state (fitted here, or read
    with `load` next to a cached refit) and starts the next refit from it."""

    def __init__(self, spec: Any, train_means: bool = False) -> None:
        self.spec = spec
        self.train_means = train_means
        self.state: State | None = None

    def load(self, raw: bytes | None) -> None:
        self.state = decode(raw) if raw is not None else None

    def state_bytes(self) -> bytes:
        if self.state is None:
            raise ValueError("no refit fitted yet")
        return encode(self.state)

    def fit(self, fd: Any, seed: int) -> tuple[dict[str, Arr], dict[str, float]]:
        import jax.numpy as jnp
        import pymc as pm

        from adatta.model import backtest as bt
        from adatta.model import diagnostics as dg
        from adatta.model import team_strength as ts

        sp = self.spec
        t0 = time.time()
        gap = gap_weeks(self.state, fd.start)
        with ts.build(fd, sp.prior_scale, sp.structure, sp.shifts, sp.form, sp.xg,
                      sp.manager, sp.lineup) as model:  # fmt: skip
            nuts: dict[str, Any] = {}
            start = None
            if gap is not None and self.state is not None:
                start = inits(self.state, model, gap)
                nuts |= {"jitter": False, "step_size": self.state.step_size,
                         "inverse_mass_matrix": jnp.asarray(inverse_mass(self.state, model,
                                                                         gap))}  # fmt: skip
                tune = TUNE
            else:
                tune = sp.draws if sp.tune is None else sp.tune
            idata = pm.sample(draws=sp.draws, tune=tune, chains=sp.chains,
                              nuts_sampler="numpyro", random_seed=seed,
                              target_accept=sp.target_accept, progressbar=False,
                              initvals=start, nuts=nuts)  # fmt: skip
            if self.train_means:
                pm.sample_posterior_predictive(idata, extend_inferencedata=True,
                                               random_seed=seed, progressbar=False)  # fmt: skip
            new = extract(idata, model, fd.start, fd.season_start, seed)
        checks = dg.convergence(idata)
        conv = {c.name: c.value for c in checks}
        print("    " + "; ".join(f"{c.name} {c.value:.3f} {c.detail}" for c in checks[:2]),
              flush=True)  # fmt: skip
        diag = {"rhat_max": conv["rhat"], "ess_bulk_min": conv["ess_bulk"],
                "ess_tail_min": conv["ess_tail"], "divergences": conv["divergences"],
                "fit_s": round(time.time() - t0), "tune": float(tune),
                "warm_weeks": float(-1 if gap is None else gap)}  # fmt: skip
        new.passed = gates_ok(diag)
        self.state = new
        out = ts.slim(idata, sp.max_pred_draws)
        if self.train_means:
            pp = ts.stacked(idata, "posterior_predictive")
            for side, var in (("home", "home_goals"), ("away", "away_goals")):
                out[f"train_lam_{side}"] = np.asarray(pp[var].mean("sample").values,
                                                      dtype=np.float64)  # fmt: skip
        del idata
        bt._release_memory()
        return out, diag


# ------------------------------------------------------------------------ the dev check


def agreement(w: dict[str, Arr], p: dict[str, Arr]) -> dict[str, float]:
    """The warm refit's posterior means against the cold refit's, over the cached draws
    (sample axis last). z: |difference| / the cold sd (the acceptance as approved); z_mc:
    |difference| / its Monte Carlo standard error, sd / sqrt(draws) on each side (the
    cached draws are thinned, so nearly independent)."""
    zs, zm = [], []
    for k in COMPARED:
        if k not in w or k not in p:
            continue
        a, b = np.asarray(w[k], dtype=np.float64), np.asarray(p[k], dtype=np.float64)
        d = np.abs(a.mean(axis=-1) - b.mean(axis=-1))
        sd_a, sd_b = a.std(axis=-1), b.std(axis=-1)
        zs.append(np.ravel(d / np.maximum(sd_b, 1e-12)))
        se = np.sqrt(sd_a**2 / a.shape[-1] + sd_b**2 / b.shape[-1])
        zm.append(np.ravel(d / np.maximum(se, 1e-12)))
    z, m = np.concatenate(zs), np.concatenate(zm)
    return {"z_max": float(z.max()), "z_mc_max": float(m.max()),
            "z_mc_sq_sum": float(np.sum(m**2)), "n_params": float(len(m))}  # fmt: skip


def run_part(df: pl.DataFrame, leagues: list[int], bundle: str, season: int,
             store: RawStore, part: tuple[int, int],
             on_refit: Callable[[pl.DataFrame, list[dict[str, Any]]], None] | None = None,
             ) -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """W and P on a contiguous block of one dev bundle-season's weekly refits."""
    from adatta.eval import harness
    from adatta.eval.selection import DEV, HoldoutRefused
    from adatta.model import backtest as bt
    from adatta.model import team_strength as ts

    if season not in DEV:
        raise HoldoutRefused(f"season {season} is not a dev season")
    cfg = harness.load_config()
    P = bt.production_spec(cfg)
    W = replace(P, warm=True)
    p_hash, w_hash = bt.fit_hash(P), bt.fit_hash(W)
    plan = blocks(harness.refit_plan(df, cfg, [season], P.every_days, None, P.grid), *part)
    mh, ma = harness.mover_codes(df, leagues)
    df = df.with_columns(mover_home=pl.Series(mh), mover_away=pl.Series(ma))
    lag = cfg.result_available_after_hours
    chain = Chain(W)
    rows: list[dict[str, Any]] = []
    diags: list[dict[str, Any]] = []
    print(f"{bundle} {season} block {part[0]}/{part[1]}: {len(plan)} refits, P {p_hash}, "
          f"W {w_hash}", flush=True)  # fmt: skip
    for as_of, tgt, train in plan:
        fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, lag)
        assert len(fd.hg) == len(train)
        key_p = bt.cache_key(p_hash, bundle, as_of)
        raw = try_get(store, key_p)
        p_cached = raw is not None
        if raw is not None:
            dP, diagP = bt.decode(raw)
        else:
            dP, diagP = bt.nuts_fitter(P, train_means=True)(fd, P.seed)
            store.put(key_p, bt.encode(dP, diagP))
        key_w = bt.cache_key(w_hash, bundle, as_of)
        raw = try_get(store, key_w)
        w_cached = raw is not None
        if raw is not None:
            dW, diagW = bt.decode(raw)
            chain.load(try_get(store, key_w + SUFFIX))
        else:
            dW, diagW = chain.fit(fd, W.seed)
            store.put(key_w, bt.encode(dW, diagW))
            store.put(key_w + SUFFIX, chain.state_bytes())
        agree = agreement(dW, dP)
        targets = df[tgt]
        counts = np.bincount(np.concatenate([fd.home, fd.away]), minlength=len(fd.team_ids))
        n_of = {int(t): int(counts[j]) for j, t in enumerate(fd.team_ids)}
        for model, draws in (("P", dP), ("W", dW)):
            for r, s in zip(
                targets.iter_rows(named=True),
                bt.score_targets(draws, fd, targets, cfg.max_goals),
                strict=True,
            ):
                n_h, n_a = n_of.get(int(r["home"]), 0), n_of.get(int(r["away"]), 0)
                rows.append({
                    "match_id": int(r["match_id"]), "league_id": int(r["league_id"]),
                    "season": int(r["season"]), "kickoff": r["kickoff"], "as_of": as_of,
                    "hg": int(r["hg"]), "ag": int(r["ag"]), "bundle": bundle, "model": model,
                    "cold_start": min(n_h, n_a) < cfg.min_warmup_matches, **s,
                })  # fmt: skip
        for name, dg_, c in (("P", diagP, p_cached), ("W", diagW, w_cached)):
            g = {"model": name, "bundle": bundle, "season": season, "as_of": as_of.isoformat(),
                 "cached": bool(c), **dg_, "gates_ok": gates_ok(dg_)}  # fmt: skip
            if name == "W":
                g |= agree
            diags.append(g)
        print(f"  refit {as_of:%Y-%m-%d}: {len(targets)} targets; P "
              f"{'cached' if p_cached else 'fitted'} {diagP.get('fit_s', 0):.0f} s; W "
              f"{'cached' if w_cached else 'fitted'} {diagW.get('fit_s', 0):.0f} s, weeks "
              f"{diagW.get('warm_weeks', -1):.0f}, rhat {diagW['rhat_max']:.3f}, div "
              f"{diagW['divergences']:.0f}, z {agree['z_max']:.3f}, z_mc "
              f"{agree['z_mc_max']:.2f}", flush=True)  # fmt: skip
        if on_refit is not None:
            on_refit(pl.DataFrame(rows), diags)
    return pl.DataFrame(rows), diags


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def _part_keys(run: str, bundles: Sequence[str], seasons: Sequence[int],
               parts: int) -> list[str]:  # fmt: skip
    return [f"{PREFIX}/parts/{run}/{b}_{s}_{i}of{parts}"
            for b in bundles for s in seasons for i in range(parts)]  # fmt: skip


def assemble(store: RawStore, run: str, bundles: Sequence[str], seasons: Sequence[int],
             parts: int) -> str:  # fmt: skip
    from adatta.eval import warm as ew

    preds, diags, missing = [], [], []
    for key in _part_keys(run, bundles, seasons, parts):
        data = try_get(store, f"{key}.parquet")
        if data is None:
            missing.append(key)
            continue
        preds.append(pl.read_parquet(io.BytesIO(data)))
        diags += json.loads(store.get(f"{key}.json"))["diags"]
    if missing:
        raise SystemExit(f"missing parts: {len(missing)} (first {missing[0]})")
    pred = pl.concat(preds, how="diagonal")
    v = ew.verdict(pred, diags)
    store.put(f"{PREFIX}/reports/{run}/pred.parquet", _parquet(pred))
    store.put(f"{PREFIX}/reports/{run}/verdict.json",
              json.dumps(v, default=str).encode())  # fmt: skip
    return ew.markdown(v)


def main(argv: Sequence[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m adatta.model.warm")
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
        from adatta.model import backtest as bt

        cfg = harness.load_config()
        leagues = cfg.bundles[args.bundle]
        df = bt._load(leagues, xg=True)
        i, n = (int(x) for x in args.part.split("/"))
        key = f"{PREFIX}/parts/{args.upload}/{args.bundle}_{args.season}_{i}of{n}"

        def save(rows: pl.DataFrame, diags: list[dict[str, Any]]) -> None:
            store.put(f"{key}.parquet", _parquet(rows))
            store.put(f"{key}.json", json.dumps({"diags": diags}, default=str).encode())

        rows, diags = run_part(df, leagues, args.bundle, args.season, store, (i, n), save)
        save(rows, diags)
        ok = sum(d["gates_ok"] for d in diags if d["model"] == "W")
        print(f"{args.bundle} {args.season}: {len(diags) // 2} refits, W passing the gates {ok}")
    else:
        seasons = [int(s) for s in args.seasons.split(",")]
        print(assemble(store, args.fetch, args.bundles.split(","), seasons, args.parts))


if __name__ == "__main__":
    main()
