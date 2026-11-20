"""P3-b selection and minutes model (docs/PHASE3_TASKS.md): a multinomial logit (start /
bench / out) with league intercepts and position offsets (the first position is the
reference), and a minutes model (full 90 given a start, coming on given the bench,
Beta-scaled minutes otherwise). PyMC, sampled with NumPyro (D10); the model dependency
group, CI only (D13).

Amendment 7 (after the first timing pilot): the five likelihoods share no parameter, so
each is sampled on its own (the joint posterior is their product); no per-player effects
(the player's own history enters through the covariates); and each refit trains on the
TRAIN_DAYS before it.

CLI (CI): `run --bundle B --season S --upload RUN` fits the 28-day refits covering one
dev season of one bundle and stores the scored rows in the private raw store; `assemble
--fetch RUN` applies the pass rule and prints metrics only (the repo is public).
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

from adatta.eval import selection as ev
from adatta.features import selection as sel
from adatta.features.referees import RESULT_LAG

Arr = NDArray[np.float64]
PREFIX = "selection"
REFIT_DAYS = 28  # D14
PPC_BAND = (0.05, 0.95)  # central 90%; a warning only (amendment 8)
TRAIN_DAYS = 730  # amendment 7: each refit trains on the two years before it
SEED = 20261116
DRAWS = 500
CHAINS = 4
PRED_DRAWS = 200  # thinned draws used for the coherent probabilities
PMF_DRAWS = 50  # of those, for the minutes pmf
MINUTES_X = ["ew_start", "last_minutes", "log_days_since", "returned_gap", "rest_days",
             "days_to_next", "next_cup", "next_continental"]  # fmt: skip


@dataclass(frozen=True)
class Design:
    mean: dict[str, float]
    sd: dict[str, float]
    leagues: list[str]


def design(train: pl.DataFrame) -> Design:
    mean, sd = {}, {}
    for f in sel.FEATURES:
        x = train[f].to_numpy().astype(float)
        mean[f] = float(np.nanmean(x))
        s = float(np.nanstd(x))
        sd[f] = s if s > 1e-9 else 1.0
    leagues = sorted(train["league_ref"].unique().to_list())
    return Design(mean, sd, leagues)


def matrix(d: Design, rows: pl.DataFrame, cols: Sequence[str] = sel.FEATURES) -> Arr:
    """Standardised covariates; a missing value is the training mean (0 after scaling)."""
    out = np.empty((len(rows), len(cols)))
    for j, f in enumerate(cols):
        x = (rows[f].to_numpy().astype(float) - d.mean[f]) / d.sd[f]
        out[:, j] = np.nan_to_num(x, nan=0.0)
    return out


def _pos(rows: pl.DataFrame) -> NDArray[np.int64]:
    names = (*sel.POS, "U")
    return np.array([names.index(p) for p in rows["pos"].to_list()])


def _leagues(d: Design, rows: pl.DataFrame) -> NDArray[np.int64]:
    return np.array([d.leagues.index(x) for x in rows["league_ref"].to_list()])


SUBMODELS = ("select", "full", "on", "start_m", "on_m")


def build_models(d: Design, train: pl.DataFrame) -> dict[str, Any]:
    """The five independent likelihoods, one PyMC model each."""
    import pymc as pm
    import pytensor.tensor as pt

    X = matrix(d, train)
    Xm = matrix(d, train, MINUTES_X)
    lg = _leagues(d, train)
    pos = _pos(train)
    y = train["y"].to_numpy()
    cls = np.select([y == sel.START, y == sel.BENCH], [0, 1], 2)  # start, bench, out
    mins = train["minutes_nt"].to_numpy()
    n_pos, n_lg = len(sel.POS) + 1, len(d.leagues)
    models: dict[str, Any] = {}
    with pm.Model() as m:
        a = pm.Normal("a", 0.0, 2.0, shape=(n_lg, 2))
        b = pm.Normal("b", 0.0, 1.0, shape=(X.shape[1], 2))
        pos_raw = pm.Normal("pos_raw", 0.0, 1.0, shape=(n_pos - 1, 2))
        pos_eff = pt.concatenate([pt.zeros((1, 2)), pos_raw], axis=0)
        eta = a[lg] + pt.dot(X, b) + pos_eff[pos]
        logits = pt.concatenate([eta, pt.zeros((len(y), 1))], axis=1)
        pm.Categorical("y", logit_p=logits, observed=cls)
    models["select"] = m
    # minutes: full 90 given a start; coming on given the bench; Beta-scaled minutes
    st, bn = y == sel.START, y == sel.BENCH
    for name, mask, obs, c in (("full", st, (mins >= 90), "c0"), ("on", bn, (mins > 0), "e0")):
        coef = "g" if name == "full" else "k"
        with pm.Model() as m:
            c0 = pm.Normal(c, 0.0, 2.0, shape=n_pos)
            g = pm.Normal(coef, 0.0, 1.0, shape=Xm.shape[1])
            pm.Bernoulli(name, logit_p=c0[pos[mask]] + pt.dot(Xm[mask], g),
                         observed=obs[mask].astype(int))  # fmt: skip
        models[name] = m
    part = st & (mins < 90) & (mins > 0)
    on = bn & (mins > 0)
    for name, mask in (("start_m", part), ("on_m", on)):
        with pm.Model() as m:
            d0 = pm.Normal(f"{name}_d0", 0.0, 2.0, shape=n_pos)
            h = pm.Normal(f"{name}_h", 0.0, 1.0, shape=Xm.shape[1])
            phi = pm.LogNormal(f"{name}_phi", np.log(5.0), 1.0, shape=n_pos)
            mu = pm.math.sigmoid(d0[pos[mask]] + pt.dot(Xm[mask], h))
            frac = np.clip(mins[mask] / 90.0, 0.005, 0.995)
            pm.Beta(name, alpha=mu * phi[pos[mask]], beta=(1 - mu) * phi[pos[mask]],
                    observed=frac)  # fmt: skip
        models[name] = m
    return models


def fit(d: Design, train: pl.DataFrame, draws: int = DRAWS, seed: int = SEED,
        timings: dict[str, float] | None = None) -> dict[str, Any]:  # fmt: skip
    """Sample each sub-model; `timings` (if given) receives seconds per sub-model."""
    import pymc as pm

    out = {}
    for k, (name, model) in enumerate(build_models(d, train).items()):
        t0 = time.monotonic()
        with model:
            out[name] = pm.sample(draws=draws, tune=draws, chains=CHAINS,
                                  nuts_sampler="numpyro", random_seed=seed + k,
                                  target_accept=0.9, progressbar=False)  # fmt: skip
        if timings is not None:
            timings[name] = time.monotonic() - t0
    return out


def posterior(fits: dict[str, Any], n: int = PRED_DRAWS) -> dict[str, Arr]:
    """Thinned draws of every sub-model's parameters. The sub-models are independent, so
    pairing their i-th draws is a draw from the joint posterior."""
    out = {}
    for idata in fits.values():
        post = idata["posterior"]
        post = post.to_dataset() if hasattr(post, "to_dataset") else post
        for v in post.data_vars:
            arr = np.asarray(post[v].values)
            flat = arr.reshape(-1, *arr.shape[2:])
            step = max(len(flat) // n, 1)
            out[str(v)] = flat[::step][:n]
    return out


def convergence(fits: dict[str, Any]) -> list[Any]:
    """The inference gates over all sub-models: worst R-hat and ESS, total divergences."""
    from adatta.model import diagnostics as dg

    per = {name: {c.name: c for c in dg.convergence(idata)} for name, idata in fits.items()}
    out = []
    for check, worst in (("rhat", max), ("ess_bulk", min), ("ess_tail", min)):
        name = worst(per, key=lambda m: per[m][check].value)
        c = per[name][check]
        out.append(dg.Check(check, c.passed, c.value, f"{name}: {c.detail}".rstrip(": ")))
    div = sum(per[m]["divergences"].value for m in per)
    out.append(dg.Check("divergences", all(per[m]["divergences"].passed for m in per), div))
    return out


def raw_probs(d: Design, post: dict[str, Arr], rows: pl.DataFrame) -> Arr:
    """(draws, n, 3) softmax probabilities (start, bench, out)."""
    X = matrix(d, rows)
    lg = _leagues(d, rows)
    pos = _pos(rows)
    pr = post["pos_raw"]
    pos_eff = np.concatenate([np.zeros((len(pr), 1, 2)), pr], axis=1)
    eta = (post["a"][:, lg, :] + np.einsum("nf,dfk->dnk", X, post["b"])
           + pos_eff[:, pos, :])  # fmt: skip
    full = np.concatenate([eta, np.zeros((*eta.shape[:2], 1))], axis=2)
    full -= full.max(axis=2, keepdims=True)
    e = np.exp(full)
    return np.asarray(e / e.sum(axis=2, keepdims=True), dtype=np.float64)


def _sig(x: Arr) -> Arr:
    return np.asarray(1.0 / (1.0 + np.exp(-x)), dtype=np.float64)


def minutes_params(d: Design, post: dict[str, Arr], rows: pl.DataFrame) -> dict[str, Arr]:
    """Per draw and row: q_full, q_on, and the two Beta means and precisions."""
    Xm = matrix(d, rows, MINUTES_X)
    pos = _pos(rows)
    return {
        "q_full": _sig(post["c0"][:, pos] + (post["g"] @ Xm.T)),
        "q_on": _sig(post["e0"][:, pos] + (post["k"] @ Xm.T)),
        "start_mu": _sig(post["start_m_d0"][:, pos] + (post["start_m_h"] @ Xm.T)),
        "start_phi": post["start_m_phi"][:, pos],
        "on_mu": _sig(post["on_m_d0"][:, pos] + (post["on_m_h"] @ Xm.T)),
        "on_phi": post["on_m_phi"][:, pos],
    }  # fmt: skip


def predict(d: Design, post: dict[str, Arr], rows: pl.DataFrame, teams: pl.DataFrame,
            nu: dict[str, tuple[float, float]]) -> tuple[Arr, Arr]:  # fmt: skip
    """Coherent three-way probabilities (mean over draws of the per-draw coherent ones) and
    the minutes pmf (mean over PMF_DRAWS draws)."""
    raw = raw_probs(d, post, rows)
    coh = np.stack([ev.coherent_all(rows, raw[i], teams, nu) for i in range(len(raw))])
    mp = minutes_params(d, post, rows)
    pmf = np.zeros((len(rows), 91))
    n = min(PMF_DRAWS, len(raw))
    for i in range(n):
        sb = ev.beta_grid(mp["start_mu"][i], mp["start_phi"][i])
        ob = ev.beta_grid(mp["on_mu"][i], mp["on_phi"][i])
        pmf += ev.minutes_pmf(coh[i], mp["q_full"][i], sb, mp["q_on"][i], ob)
    return coh.mean(axis=0), pmf / n


def role_minutes(d: Design, post: dict[str, Arr], rows: pl.DataFrame) -> tuple[Arr, Arr]:
    """Expected normal-time minutes given the role, mean over PMF_DRAWS draws (exported
    for P3-d's lineup offset): a starter plays 90 with q_full, else the start Beta; a bench
    player comes on with q_on and then plays the on Beta. Same grid as `predict`."""
    mp = minutes_params(d, post, rows)
    n = min(PMF_DRAWS, len(mp["q_full"]))
    e_start, e_bench = np.zeros(len(rows)), np.zeros(len(rows))
    for i in range(n):
        sb = ev.beta_grid(mp["start_mu"][i], mp["start_phi"][i]) @ ev.MINUTES
        ob = ev.beta_grid(mp["on_mu"][i], mp["on_phi"][i]) @ ev.MINUTES
        e_start += mp["q_full"][i] * 90.0 + (1.0 - mp["q_full"][i]) * sb
        e_bench += mp["q_on"][i] * ob
    return e_start / n, e_bench / n


def ppc_starts(d: Design, post: dict[str, Arr], train: pl.DataFrame,
               rng: np.random.Generator) -> dict[str, float]:  # fmt: skip
    """The training team-matches' mean pool starts and bench places, observed against the
    posterior predictive distribution of that mean (central 90% band)."""
    raw = raw_probs(d, post, train)
    key = train["match_id"].to_numpy() * 100_000 + train["team_id"].to_numpy()
    _, grp = np.unique(key, return_inverse=True)
    y = train["y"].to_numpy()
    out: dict[str, float] = {}
    for name, k, code in (("starts", 0, sel.START), ("bench", 1, sel.BENCH)):
        obs = np.bincount(grp, weights=(y == code).astype(float)).mean()
        sims = []
        for i in range(len(raw)):
            draw = rng.random(len(y)) < raw[i, :, k]
            sims.append(np.bincount(grp, weights=draw.astype(float)).mean())
        q = float((np.array(sims) < obs).mean())
        out[f"ppc_{name}_obs"], out[f"ppc_{name}_q"] = float(obs), q
    return out


# ---------------------------------------------------------------------------- walk-forward


def refit_times(rows: pl.DataFrame) -> list[datetime]:
    from adatta.eval import harness

    first = rows["as_of"].min()
    last = rows["as_of"].max()
    return harness.as_of_times(first, last, REFIT_DAYS)  # type: ignore[arg-type]


def with_times(players: pl.DataFrame, teams: pl.DataFrame) -> pl.DataFrame:
    t = teams.select("match_id", "team_id",
                     pl.from_epoch("as_of_us", time_unit="us").dt.replace_time_zone("UTC")
                     .alias("as_of"))  # fmt: skip
    return players.join(t, on=["match_id", "team_id"])


def run_part(players: pl.DataFrame, teams: pl.DataFrame, leagues: list[str], season: int,
             draws: int = DRAWS, part: tuple[int, int] = (0, 1),
             on_refit: Callable[[pl.DataFrame, list[dict[str, Any]]], None] | None = None,
             only: set[str] | None = None, live: bool = False,
             ) -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """Score one dev season (or with `live`, one live season: P3-e (f)) of one bundle: baselines
    from warm-up, the model refitted every 28 days on the TRAIN_DAYS of rows final by the refit
    time. `part` (i, n) takes every n-th refit block of the season from the i-th, so n jobs
    share a season. `on_refit` is called after each refit with everything scored so far (so a
    cut-off job keeps it). `only` (refit dates, YYYY-MM-DD) keeps just those refits of the part
    (amendment 9)."""
    if live and season < ev.LIVE_FROM:
        raise ev.HoldoutRefused(f"season {season} is not a live season")
    if not live and season not in ev.DEV:
        raise ev.HoldoutRefused(f"season {season} is not a dev season")
    rows = with_times(players.filter(pl.col("league_ref").is_in(leagues)), teams)
    kick = rows["as_of"] + sel.HORIZON  # the match's kickoff
    rows = rows.with_columns(final_at=kick + RESULT_LAG)
    warm = rows.filter(pl.col("season").is_in(ev.WARMUP))
    base = ev.fit_baselines(warm.drop("as_of", "final_at"))
    nu_warm = ev.newcomer_rates(teams.filter(pl.col("season").is_in(ev.WARMUP)))
    target = rows.filter(pl.col("season") == season)
    out, diags = [], []
    # the dev grid; a live run continues it through the live season (same 28-day steps)
    graded = pl.col("season").is_in(ev.DEV) | (pl.col("season") >= ev.LIVE_FROM if live
                                               else pl.lit(False))  # fmt: skip
    times = refit_times(rows.filter(graded))
    blocks = []
    for i, t in enumerate(times):
        nxt = times[i + 1] if i + 1 < len(times) else datetime.max.replace(tzinfo=UTC)
        block = target.filter((pl.col("as_of") >= t) & (pl.col("as_of") < nxt))
        if not block.is_empty():
            blocks.append((t, block))
    picked = blocks[part[0] :: part[1]]
    if only is not None:
        picked = [(t, b) for t, b in picked if f"{t:%Y-%m-%d}" in only]
        missing = only - {f"{t:%Y-%m-%d}" for t, _ in picked}
        if missing:
            raise ValueError(f"refits not in this part: {sorted(missing)}")
    for t, block in picked:
        train = rows.filter((pl.col("final_at") <= t)
                            & (pl.col("as_of") > t - timedelta(days=TRAIN_DAYS)))  # fmt: skip
        tm_train = teams.join(train.select("match_id", "team_id").unique(),
                              on=["match_id", "team_id"])  # fmt: skip
        d = design(train)
        timings: dict[str, float] = {}
        fits = fit(d, train, draws, timings=timings)
        fit_s = sum(timings.values())
        checks = convergence(fits)
        post = posterior(fits)
        ppc = ppc_starts(d, post, train.sample(min(len(train), 50_000), seed=SEED),
                         np.random.default_rng(SEED))  # fmt: skip
        nu = ev.newcomer_rates(tm_train)
        tm = teams.join(block.select("match_id", "team_id").unique(), on=["match_id", "team_id"])
        p_model, pmf_model = predict(d, post, block, tm, nu)
        raws = ev.baseline_raw(base, block)
        mins_b = ev.baseline_minutes(base, block)
        e_start, e_bench = role_minutes(d, post, block)
        # P3-d export: bench probability and minutes given the role, model rows only
        parts = [ev.score(block, p_model, pmf_model).with_columns(
            model=pl.lit("model"), p_bench=p_model[:, 1], emin_start=e_start,
            emin_bench=e_bench)]  # fmt: skip
        for name, r in raws.items():
            p = ev.coherent_all(block, r, tm, nu_warm)
            parts.append(ev.score(block, p, mins_b).with_columns(model=pl.lit(name)))
        out.append(pl.concat(parts, how="diagonal"))
        diags.append({
            "refit": t.isoformat(), "train_rows": len(train), "rows": len(block),
            "fit_s": fit_s, **{f"fit_s_{k}": v for k, v in timings.items()},
            **{c.name: c.value for c in checks},
            "worst": {c.name: c.detail for c in checks if c.detail},
            # amendment 8: R-hat, ESS and divergences gate every refit; the starts and bench
            # PPC is a reported warning (the served probabilities are coherent by
            # construction, and ECE on dev outcomes checks calibration)
            "gates_ok": all(c.passed for c in checks if c.blocking),
            "ppc_ok": all(PPC_BAND[0] <= ppc[f"ppc_{k}_q"] <= PPC_BAND[1]
                          for k in ("starts", "bench")),
            **ppc,
        })  # fmt: skip
        g = diags[-1]
        print(f"refit {t:%Y-%m-%d}: train {len(train)} rows, fit {fit_s:.0f} s "
              f"({', '.join(f'{k} {v:.0f}' for k, v in timings.items())}); "
              f"rhat {g['rhat']:.3f}, ess {min(g['ess_bulk'], g['ess_tail']):.0f}, "
              f"div {g['divergences']:.0f}, ppc q {g['ppc_starts_q']:.2f}/{g['ppc_bench_q']:.2f}; "
              f"gates {'ok' if g['gates_ok'] else 'FAIL'}"
              f"{'' if g['ppc_ok'] else ' (PPC warning)'}", flush=True)  # fmt: skip
        if on_refit is not None:
            on_refit(pl.concat(out), diags)
    return (pl.concat(out) if out else pl.DataFrame()), diags


def serve_refit(store: Any, session: Any, bundle: str, leagues: list[str], now: datetime,
                draws: int = DRAWS, force: bool = False) -> dict[str, Any]:  # fmt: skip
    """The serving refit of one bundle (docs/DASHBOARD_TASKS.md, projected lineups): P3-b
    as on dev, fitted at the latest time of the dev 28-day grid at or before `now` on the
    TRAIN_DAYS of rows final by then; stored and made the bundle's latest only if it passes
    the gates. Skipped unless the latest passing refit is REFIT_DAYS old (or `force`)."""
    from adatta.sim import lineups as sl

    if not force and not sl.due(store, bundle, now):
        return {"bundle": bundle, "skipped": "not due"}
    season = now.year if now.month >= 7 else now.year - 1
    inp = ev.load_inputs(session, store, leagues, max(season, ev.LIVE_FROM), live=True)
    session.rollback()  # end the read transaction: the fit takes far past Neon's idle timeout
    players, teams = sel.build(inp)
    rows = with_times(players.filter(pl.col("league_ref").is_in(leagues)), teams)
    rows = rows.with_columns(final_at=rows["as_of"] + sel.HORIZON + RESULT_LAG)
    first = rows.filter(pl.col("season").is_in(ev.DEV))["as_of"].min()
    t = refit_times(pl.DataFrame({"as_of": [first, now]}))[-1]
    train = rows.filter((pl.col("final_at") <= t)
                        & (pl.col("as_of") > t - timedelta(days=TRAIN_DAYS)))  # fmt: skip
    tm_train = teams.join(train.select("match_id", "team_id").unique(), on=["match_id", "team_id"])
    d = design(train)
    timings: dict[str, float] = {}
    fits = fit(d, train, draws, timings=timings)
    checks = convergence(fits)
    ok = all(c.passed for c in checks if c.blocking)
    meta = {"bundle": bundle, "as_of": t.isoformat(), "train_rows": len(train), "gates_ok": ok,
            "fit_s": sum(timings.values()), **{c.name: c.value for c in checks}}  # fmt: skip
    key = sl.refit_key(bundle, t)
    store.put(key, sl.encode(posterior(fits), d, ev.newcomer_rates(tm_train), meta))
    if ok:
        store.put(sl.latest_key(bundle), json.dumps({"key": key, "as_of": t.isoformat()}).encode())
    return meta


def merge_override(base: pl.DataFrame, base_diags: list[dict[str, Any]], over: pl.DataFrame,
                   over_diags: list[dict[str, Any]],
                   ) -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """One part with some refits redone (amendment 9): the rerun's team-matches and refit
    diagnostics replace the base run's; everything else is kept as it was."""
    redone = {g["refit"] for g in over_diags}
    if not redone <= {g["refit"] for g in base_diags}:
        raise ValueError("an override refit is not in the base part")
    keys = over.select("match_id", "team_id").unique()
    kept = base.join(keys, on=["match_id", "team_id"], how="anti")
    diags = [g for g in base_diags if g["refit"] not in redone] + list(over_diags)
    return pl.concat([kept, over.select(base.columns)]), sorted(diags, key=lambda g: g["refit"])


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def strong_baseline(players: pl.DataFrame, teams: pl.DataFrame) -> str:
    """The stronger baseline on warm-up (in-sample three-way log loss)."""
    warm = players.filter(pl.col("season").is_in(ev.WARMUP))
    base = ev.fit_baselines(warm)
    nu = ev.newcomer_rates(teams.filter(pl.col("season").is_in(ev.WARMUP)))
    tm = teams.filter(pl.col("season").is_in(ev.WARMUP))
    y = warm["y"].to_numpy()
    col = np.select([y == sel.START, y == sel.BENCH], [0, 1], 2)
    ll = {}
    for name, r in ev.baseline_raw(base, warm).items():
        p = ev.coherent_all(warm, r, tm, nu)
        ll[name] = float(-np.log(p[np.arange(len(p)), col]).mean())
    return "B2" if ll["B2"] <= ll["B1"] else "B1"


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    from adatta.adapters.storage import make_store, try_get
    from adatta.config import get_settings
    from adatta.eval import harness

    p = argparse.ArgumentParser(prog="adatta.model.selection")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--season", type=int, required=True)
    r.add_argument("--draws", type=int, default=DRAWS)
    r.add_argument("--upload", required=True)
    r.add_argument("--table", default="latest")
    r.add_argument("--part", default="0/1", help="i/n: every n-th refit block from the i-th")
    r.add_argument("--only", default="", help="comma-separated refit dates (a rerun)")
    r.add_argument("--live", action="store_true", help="P3-e (f): a live season")
    sr = sub.add_parser("serve-refit", help="the serving refit of one bundle (projected lineups)")
    sr.add_argument("--bundle", required=True)
    sr.add_argument("--force", action="store_true", help="refit even if not due")
    a = sub.add_parser("assemble")
    a.add_argument("--fetch", required=True)
    a.add_argument("--bundles", required=True)
    a.add_argument("--seasons", required=True)
    a.add_argument("--table", default="latest")
    a.add_argument("--parts", type=int, default=1)
    a.add_argument("--override", default="", help="a rerun whose refits replace the base's")
    args = p.parse_args(argv)
    store = make_store(get_settings())
    cfg = harness.load_config()
    if args.cmd == "serve-refit":
        from adatta.db.session import get_session

        with get_session() as session:
            meta = serve_refit(store, session, args.bundle,
                               [str(x) for x in cfg.bundles[args.bundle]], datetime.now(UTC),
                               force=args.force)  # fmt: skip
        print(json.dumps(meta, default=str))
        if meta.get("gates_ok") is False:
            raise SystemExit(f"serving refit {args.bundle} failed the gates: not served")
        return
    players, teams, digest = ev.load_table(store, args.table)
    if args.cmd == "run":
        leagues = [str(x) for x in cfg.bundles[args.bundle]]
        i, n = (int(x) for x in args.part.split("/"))
        key = f"{PREFIX}/parts/{args.upload}/{args.bundle}_{args.season}_{i}of{n}"

        def save(scored: pl.DataFrame, diags: list[dict[str, Any]]) -> None:
            store.put(f"{key}.parquet", _parquet(scored))
            store.put(f"{key}.json", json.dumps({"table": digest, "diags": diags}).encode())

        only = set(args.only.split(",")) if args.only else None
        scored, diags = run_part(players, teams, leagues, args.season, args.draws, (i, n),
                                 on_refit=save, only=only, live=args.live)  # fmt: skip
        save(scored, diags)
        ok = sum(d["gates_ok"] for d in diags)
        print(f"{args.bundle} {args.season}: {len(diags)} refits, {ok} pass the gates; "
              f"median fit {np.median([d['fit_s'] for d in diags]):.0f} s")  # fmt: skip
        return
    parts, diags, redone = [], [], 0
    for b in args.bundles.split(","):
        for s in args.seasons.split(","):
            for i in range(args.parts):
                name = f"{b}_{s}_{i}of{args.parts}"
                key = f"{PREFIX}/parts/{args.fetch}/{name}"
                part_rows = pl.read_parquet(io.BytesIO(store.get(f"{key}.parquet")))
                part_diags = json.loads(store.get(f"{key}.json"))["diags"]
                over = try_get(store, f"{PREFIX}/parts/{args.override}/{name}.json")
                if args.override and over is not None:
                    o_rows = pl.read_parquet(
                        io.BytesIO(store.get(f"{PREFIX}/parts/{args.override}/{name}.parquet"))
                    )
                    o_diags = json.loads(over)["diags"]
                    part_rows, part_diags = merge_override(part_rows, part_diags, o_rows, o_diags)
                    redone += len(o_diags)
                parts.append(part_rows)
                diags += part_diags
    scored = pl.concat(parts)
    meta = json.loads(store.get(f"{sel.PREFIX}/{digest}/meta.json"))
    v = ev.verdict(scored, strong_baseline(players, teams),
                   all(d["gates_ok"] for d in diags), bool(meta.get("leakage_ok")))  # fmt: skip
    print(ev.markdown(scored, v, diags, digest))
    if args.override:
        print(f"\nRefits redone by run {args.override} (amendment 9): {redone}.")
    report = args.override or args.fetch
    store.put(f"{PREFIX}/reports/{report}/scored.parquet", _parquet(scored))


if __name__ == "__main__":
    main()
