"""P3b-3: team cards with referee strictness (docs/PHASE3B_TASKS.md P3b-3, D66; spec §8.4).

For team j against opponent o in match i, with referee r:

    y_ij ~ NegBinomial(mu_ij, phi)
    log mu_ij = c[l, s] + h home_ij + a[j] + b[o] + s_ref[r]

c[l, s] = c_l + delta[l, s], delta ~ Normal(0, SEASON_SD) with SEASON_SD fixed (a stated
assumption, as D21's reset: 2-3 seasons per window don't identify a learned sd), a own card
tendency, b the cards a team draws from opponents, s_ref the referee (one per identity in
the country's pool). a, b and s_ref non-centred; sigma ~ HalfNormal(0.3 x scale),
c_l ~ Normal(log 2.2, 0.5), h ~ Normal(0, 0.2), phi ~ Gamma(2, 0.1). Variants,
nested: C0 (c, h), C1 (+ a, b), C2 (+ s_ref). Fitted per bundle on league matches with a
named referee, final by the as-of (kickoff + 3 h) within WINDOW_DAYS, every GRID_DAYS on
the dev Mondays.

Prediction: a team or referee without a training match gets 0 for a team and a pool draw
Normal(0, sigma_ref) per posterior draw for a referee; so does a match whose referee is not
known at the horizon (T-24h in POOL_24's leagues, or never named). A league-season without
training matches draws its level from Normal(c_l, SEASON_SD). The two teams' counts are
independent given the draw; the match total is their convolution within each draw.

CLI (CI, model group; one backtest-type run at a time):
  run      --bundle B --season S --upload RUN [--table DIGEST]     dev seasons only
  window   --bundle B --upload RUN [--table DIGEST]                window (i), 2026/27
  assemble --fetch RUN [--table DIGEST]                            the dev gate
  judge    --fetch RUN --dev RUN [--table DIGEST]                  window (i), read once
The repo is public: logs carry metrics only; per-match rows go to R2 (cards/...).
"""

from __future__ import annotations

import argparse
import io
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

Arr = NDArray[np.float64]
IntArr = NDArray[np.int64]

PREFIX = "cards"
VARIANTS = ("C0", "C1", "C2")
WINDOW_DAYS = 730
GRID_DAYS = 28  # D14's dev iteration grid
LAG = timedelta(hours=3)
DRAWS, CHAINS, TARGET_ACCEPT, PRED_DRAWS = 2000, 4, 0.95, 400
SEED = 20261127
MAX_TEAM = 20  # team counts 0-20; the match total 0-40
TOTAL_LINES = (3.5, 4.5, 5.5)
TEAM_LINES = (1.5, 2.5)
SIGMA_SD, C_MEAN, C_SD, H_SD, PHI_A, PHI_B = 0.3, float(np.log(2.2)), 0.5, 0.2, 2.0, 0.1
SEASON_SD = 0.1  # fixed: yearly card levels moved about +-5% (2016-2023); see the docstring
POOL_24 = frozenset({140, 141})  # La Liga, Segunda: referee from the pool at T-24h
HORIZONS = ("60", "24")
DEV = (2019, 2020, 2021, 2022, 2023)
HOLDOUT = (2024, 2025)
WINDOW_SEASON = 2026
WINDOW_END = datetime(2026, 11, 27, tzinfo=UTC)  # window (i): kickoff before (D66)
SENS_SCALES = (0.5, 2.0)


# ------------------------------------------------------------------------------ design


@dataclass(frozen=True)
class Index:
    leagues: tuple[int, ...]
    ls: tuple[tuple[int, int], ...]  # (league, season)
    teams: tuple[int, ...]
    refs: tuple[str, ...]


@dataclass(frozen=True)
class Design:
    y: IntArr
    home: Arr
    ls: IntArr
    ls_league: IntArr  # per league-season, its league
    team: IntArr
    opp: IntArr
    ref: IntArr  # -1: none
    match: IntArr  # per row, its match (0..n_matches-1)
    index: Index


def design(train: pl.DataFrame) -> Design:
    """The training rows (cards table rows) as indices."""
    leagues = tuple(sorted({int(x) for x in train["league_id"]}))
    ls = tuple(sorted({(int(a), int(b)) for a, b in zip(train["league_id"], train["season"],
                                                         strict=True)}))  # fmt: skip
    teams = tuple(sorted({int(x) for x in train["team_id"]} | {int(x) for x in train["opp_id"]}))
    refs = tuple(sorted({str(x) for x in train["referee"].drop_nulls()}))
    li, lsi = {x: i for i, x in enumerate(leagues)}, {x: i for i, x in enumerate(ls)}
    ti, ri = {x: i for i, x in enumerate(teams)}, {x: i for i, x in enumerate(refs)}
    mi: dict[int, int] = {}
    match = np.array([mi.setdefault(int(m), len(mi)) for m in train["match_id"]], dtype=np.int64)
    return Design(
        y=train["cards"].to_numpy().astype(np.int64),
        home=train["is_home"].to_numpy().astype(np.float64),
        ls=np.array([lsi[(int(a), int(b))] for a, b in zip(train["league_id"], train["season"],
                                                           strict=True)], dtype=np.int64),
        ls_league=np.array([li[a] for a, _ in ls], dtype=np.int64),
        team=np.array([ti[int(x)] for x in train["team_id"]], dtype=np.int64),
        opp=np.array([ti[int(x)] for x in train["opp_id"]], dtype=np.int64),
        ref=np.array([ri.get(str(x), -1) if x is not None else -1 for x in train["referee"]],
                     dtype=np.int64),
        match=match, index=Index(leagues, ls, teams, refs),
    )  # fmt: skip


# ------------------------------------------------------------------------------- model


def build(d: Design, variant: str, prior_scale: float = 1.0) -> Any:
    import pymc as pm
    import pytensor.tensor as pt

    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}")
    k = prior_scale
    ix = d.index
    with pm.Model() as model:
        c_l = pm.Normal("c_league", C_MEAN, C_SD, shape=len(ix.leagues))
        # fixed sd (CI runs 46014739829 and the next: a learned sigma_season over 2-3
        # seasons diverged, non-centred 3 and centred 72)
        delta = pm.Normal("delta_ls", 0.0, SEASON_SD, shape=len(ix.ls))
        c_ls = pm.Deterministic("c_ls", c_l[d.ls_league] + delta)
        h = pm.Normal("h", 0.0, H_SD)
        eta = c_ls[d.ls] + h * d.home
        if variant in ("C1", "C2"):
            s_a = pm.HalfNormal("sigma_a", SIGMA_SD * k)
            s_b = pm.HalfNormal("sigma_b", SIGMA_SD * k)
            z_a = pm.Normal("z_a", 0.0, 1.0, shape=len(ix.teams))
            z_b = pm.Normal("z_b", 0.0, 1.0, shape=len(ix.teams))
            eta = eta + s_a * z_a[d.team] + s_b * z_b[d.opp]
        if variant == "C2":
            if (d.ref < 0).any():
                raise ValueError("C2 trains on rows with a named referee only")
            s_r = pm.HalfNormal("sigma_ref", SIGMA_SD * k)
            z_r = pm.Normal("z_ref", 0.0, 1.0, shape=len(ix.refs))
            eta = eta + s_r * z_r[d.ref]
        phi = pm.Gamma("phi", PHI_A, PHI_B)
        pm.NegativeBinomial("y", mu=pt.exp(eta), alpha=phi, observed=d.y)
    return model


def post_draws(idata: Any, variant: str, n: int = PRED_DRAWS) -> dict[str, Arr]:
    """The thinned posterior on the natural scale: c_league (L, D), c_ls (LS, D), h, phi
    (D); C1+: a, b (T, D), sigma_a, sigma_b; C2: r (R, D), sigma_ref."""
    post = idata["posterior"]
    flat = {v: np.asarray(post[v].values).reshape(-1, *post[v].shape[2:])
            for v in post.data_vars}  # fmt: skip
    take = np.linspace(0, len(flat["h"]) - 1, min(n, len(flat["h"]))).astype(np.int64)
    f = {k: v[take] for k, v in flat.items()}
    out: dict[str, Arr] = {"c_league": f["c_league"].T,
                           "c_ls": f["c_ls"].T, "h": f["h"], "phi": f["phi"]}  # fmt: skip
    if variant in ("C1", "C2"):
        out |= {"a": (f["z_a"] * f["sigma_a"][:, None]).T,
                "b": (f["z_b"] * f["sigma_b"][:, None]).T,
                "sigma_a": f["sigma_a"], "sigma_b": f["sigma_b"]}  # fmt: skip
    if variant == "C2":
        out |= {"r": (f["z_ref"] * f["sigma_ref"][:, None]).T, "sigma_ref": f["sigma_ref"]}
    return out


def c_ls(post: dict[str, Arr], d: Design) -> Arr:
    return post["c_ls"]


def fit(d: Design, variant: str, prior_scale: float = 1.0, seed: int = SEED,
        draws: int = DRAWS, chains: int = CHAINS,
        ) -> tuple[dict[str, Arr], dict[str, Any]]:  # fmt: skip
    import pymc as pm

    from adatta.model import diagnostics as dg

    t0 = time.time()
    with build(d, variant, prior_scale):
        idata = pm.sample(draws=draws, tune=draws, chains=chains, nuts_sampler="numpyro",
                          random_seed=seed, target_accept=TARGET_ACCEPT,
                          progressbar=False)  # fmt: skip
    conv = {c.name: c.value for c in dg.convergence(idata)}
    post = post_draws(idata, variant)
    diag: dict[str, Any] = {"rhat": conv["rhat"], "ess_bulk": conv["ess_bulk"],
                            "ess_tail": conv["ess_tail"], "divergences": conv["divergences"],
                            "fit_s": round(time.time() - t0)}  # fmt: skip
    diag["gates_ok"] = bool(diag["rhat"] <= dg.RHAT_MAX and diag["ess_bulk"] >= dg.ESS_MIN
                            and diag["ess_tail"] >= dg.ESS_MIN
                            and diag["divergences"] <= dg.DIVERGENCES_MAX)  # fmt: skip
    return post, diag


# ---------------------------------------------------------------------- PPC and checks


def train_mu(post: dict[str, Arr], d: Design) -> Arr:
    """(rows, D) fitted means of the training rows."""
    eta = c_ls(post, d)[d.ls] + post["h"][None, :] * d.home[:, None]
    if "a" in post:
        eta = eta + post["a"][d.team] + post["b"][d.opp]
    if "r" in post:
        eta = eta + post["r"][d.ref]
    return np.asarray(np.exp(eta))


def nb_sample(mu: Arr, phi: Arr, rng: np.random.Generator) -> Arr:
    """Negative binomial draws (mean mu, dispersion phi per column) via Gamma-Poisson."""
    lam = rng.gamma(phi[None, :], mu / phi[None, :])
    return rng.poisson(lam).astype(np.float64)


def ppc(post: dict[str, Arr], d: Design, seed: int = SEED, n: int = 200) -> dict[str, Any]:
    """Dispersion (variance / mean) of team cards and of match totals, and the correlation
    of the two teams' counts, observed against the posterior predictive. The dispersions
    pass inside its 95% band; the correlation is reported."""
    rng = np.random.default_rng(seed)
    cols = np.linspace(0, len(post["h"]) - 1, min(n, len(post["h"]))).astype(np.int64)
    mu = train_mu(post, d)[:, cols]
    rep = nb_sample(mu, post["phi"][cols], rng)
    y = d.y.astype(np.float64)
    nm = int(d.match.max()) + 1
    two = np.bincount(d.match, minlength=nm) == 2

    def totals(v: NDArray[Any]) -> NDArray[Any]:
        out = np.zeros((nm,) + v.shape[1:])
        np.add.at(out, d.match, v)
        return np.asarray(out[two])

    def disp(v: NDArray[Any]) -> NDArray[Any]:
        return v.var(axis=0) / v.mean(axis=0)

    home = d.home > 0.5
    both = np.zeros(nm, dtype=bool)
    both[d.match[home]] = True
    has_away = np.zeros(nm, dtype=bool)
    has_away[d.match[~home]] = True
    both &= has_away
    yh = np.zeros(nm)
    ya = np.zeros(nm)
    yh[d.match[home]], ya[d.match[~home]] = y[home], y[~home]
    rh = np.zeros((nm, rep.shape[1]))
    ra = np.zeros((nm, rep.shape[1]))
    rh[d.match[home]], ra[d.match[~home]] = rep[home], rep[~home]

    def corr(a: NDArray[Any], b: NDArray[Any]) -> float:
        return float(np.corrcoef(a, b)[0, 1])

    out: dict[str, Any] = {}
    for name, obs, sims in (("team", disp(y), disp(rep)),
                            ("total", disp(totals(y)), disp(totals(rep)))):  # fmt: skip
        q = float((sims <= obs).mean())
        out[f"disp_{name}"] = {"obs": float(obs), "q": q, "pass": 0.025 <= q <= 0.975}
    rc = [corr(rh[both, j], ra[both, j]) for j in range(rep.shape[1])]
    out["corr"] = {"obs": corr(yh[both], ya[both]), "rep_mean": float(np.mean(rc)),
                   "q": float(np.mean(np.array(rc) <= corr(yh[both], ya[both])))}  # fmt: skip
    out["pass"] = out["disp_team"]["pass"] and out["disp_total"]["pass"]
    return out


def sensitivity(base: dict[str, Arr], alt: dict[str, dict[str, Arr]]) -> dict[str, Any]:
    """The largest move of a referee's posterior mean under the scaled priors, in posterior
    sds at scale 1 (the check: < 1 sd, diagnostics.SENS_SHIFT_SD)."""
    from adatta.model import diagnostics as dg

    m, sd = base["r"].mean(axis=1), np.maximum(base["r"].std(axis=1), 1e-12)
    worst = {s: float(np.max(np.abs(p["r"].mean(axis=1) - m) / sd)) for s, p in alt.items()}
    return {"worst": worst, "pass": all(v < dg.SENS_SHIFT_SD for v in worst.values())}


# --------------------------------------------------------------------------- prediction


def target_mu(post: dict[str, Arr], ix: Index, t: pl.DataFrame, pool: NDArray[np.bool_],
              seed: int = SEED) -> Arr:  # fmt: skip
    """(rows, D) means for target rows (cards table rows). `pool`: rows whose referee is
    drawn from the pool (unknown at the horizon or never named)."""
    rng = np.random.default_rng(seed)
    nd = len(post["h"])
    li, lsi = {x: i for i, x in enumerate(ix.leagues)}, {x: i for i, x in enumerate(ix.ls)}
    ti, ri = {x: i for i, x in enumerate(ix.teams)}, {x: i for i, x in enumerate(ix.refs)}
    cls = post["c_ls"]
    eta = np.zeros((t.height, nd))
    for k, r in enumerate(t.iter_rows(named=True)):
        key = (int(r["league_id"]), int(r["season"]))
        if key in lsi:
            lvl = cls[lsi[key]]
        else:  # a league-season with no training match
            lvl = post["c_league"][li[key[0]]] + SEASON_SD * rng.standard_normal(nd)
        e = lvl + post["h"] * float(r["is_home"])
        if "a" in post:
            j, o = ti.get(int(r["team_id"])), ti.get(int(r["opp_id"]))
            e = e + (post["a"][j] if j is not None else 0.0) + (post["b"][o] if o is not None
                                                                 else 0.0)  # fmt: skip
        if "r" in post:
            ref = r["referee"]
            if not pool[k] and ref is not None and str(ref) in ri:
                e = e + post["r"][ri[str(ref)]]
            else:
                e = e + post["sigma_ref"] * rng.standard_normal(nd)
        eta[k] = e
    return np.asarray(np.exp(eta))


def team_pmf(mu: Arr, phi: Arr) -> Arr:
    """(rows, D, MAX_TEAM + 1) negative binomial probabilities (tail beyond the cap kept
    out, never renormalised)."""
    from scipy.stats import nbinom

    p = phi[None, :] / (phi[None, :] + mu)
    k = np.arange(MAX_TEAM + 1)
    return np.asarray(nbinom.pmf(k[None, None, :], phi[None, :, None], p[:, :, None]))


def total_pmf(ph: Arr, pa: Arr) -> Arr:
    """(D, 2 MAX_TEAM + 1) totals within each draw (independent teams), from (D, K) pmfs."""
    n = ph.shape[1]
    out = np.zeros((ph.shape[0], 2 * n - 1))
    for i in range(n):
        out[:, i : i + n] += ph[:, i : i + 1] * pa
    return out


def over(p: Arr, line: float) -> float:
    return float(p[int(np.floor(line)) + 1 :].sum())


def _ll(q: float, hit: bool) -> float:
    q = min(max(q, 1e-12), 1 - 1e-12)
    return float(-np.log(q if hit else 1 - q))


def score(total: Arr, th: Arr, ta: Arr, yh: int, ya: int) -> dict[str, float]:
    """The match's scores from its total and team distributions (each summing to 1 minus
    its cap's tail): discrete CRPS of the total over 0-40 (`rps_total`), its log score,
    O/U log losses and probabilities."""
    y = yh + ya
    cdf = np.cumsum(total)
    out = {"rps_total": float(np.sum((cdf - (np.arange(len(total)) >= y)) ** 2)),
           "logs_total": float(-np.log(max(total[y] if y < len(total) else 0.0, 1e-12))),
           "tail": float(1.0 - total.sum())}  # fmt: skip
    for line in TOTAL_LINES:
        q = over(total, line)
        out[f"p_over_{line}"] = q
        out[f"ll_over_{line}"] = _ll(q, y > line)
    for line in TEAM_LINES:
        out[f"ll_team_{line}"] = 0.5 * (_ll(over(th, line), yh > line)
                                        + _ll(over(ta, line), ya > line))  # fmt: skip
    return out


def climatology(train: pl.DataFrame) -> dict[tuple[int, bool], Arr]:
    """B_clim: per (league, home) the training window's team-card frequencies, 0.5 pseudo
    per value 0..MAX_TEAM."""
    out = {}
    for (lg, hm), g in train.group_by(["league_id", "is_home"]):
        c = np.bincount(np.clip(g["cards"].to_numpy(), 0, MAX_TEAM), minlength=MAX_TEAM + 1)
        p = c + 0.5
        out[(int(lg), bool(hm))] = p / p.sum()
    return out


# ---------------------------------------------------------------------------- the run


def plan(table: pl.DataFrame, bundle: str, season: int, end: datetime | None = None,
         ) -> list[tuple[datetime, pl.DataFrame, pl.DataFrame]]:  # fmt: skip
    """(as_of, target rows, training rows) on the 28-day Mondays of one bundle-season.
    Training: the bundle's rows with a named referee, final by the as-of, within
    WINDOW_DAYS."""
    from adatta.eval import harness

    b = table.filter(pl.col("bundle") == bundle)
    tg = b.filter(pl.col("season") == season)
    if end is not None:
        tg = tg.filter(pl.col("kickoff") < end)
    if tg.is_empty():
        return []
    times = harness.as_of_times(tg["kickoff"].min(), tg["kickoff"].max(), GRID_DAYS)  # type: ignore[arg-type]
    out = []
    for i, t in enumerate(times):
        nxt = times[i + 1] if i + 1 < len(times) else datetime.max.replace(tzinfo=UTC)
        tgt = tg.filter((pl.col("kickoff") >= t) & (pl.col("kickoff") < nxt))
        train = b.filter((pl.col("kickoff") + LAG <= t)
                         & (pl.col("kickoff") > t - timedelta(days=WINDOW_DAYS))
                         & pl.col("referee").is_not_null())  # fmt: skip
        if tgt.height and train.height:
            assert (train["kickoff"] + LAG <= t).all()
            out.append((t, tgt, train))
    return out


def score_refit(posts: dict[str, dict[str, Arr]], ix: Index, tgt: pl.DataFrame,
                clim: dict[tuple[int, bool], Arr], as_of: datetime,
                ) -> list[dict[str, Any]]:  # fmt: skip
    """Every target match scored by B_clim and each variant at both horizons."""
    rows: list[dict[str, Any]] = []
    t = tgt.sort("match_id", "is_home", descending=[False, True])
    pairs = t.group_by("match_id", maintain_order=True).agg(pl.len()).filter(pl.col("len") == 2)
    t = t.join(pairs.select("match_id"), on="match_id").sort("match_id", "is_home",
                                                              descending=[False, True])  # fmt: skip
    if t.is_empty():
        return rows
    lg = t["league_id"].to_numpy()
    named = t["referee"].is_not_null().to_numpy()
    base = [{"match_id": int(t["match_id"][2 * i]), "league_id": int(lg[2 * i]),
             "season": int(t["season"][2 * i]), "kickoff": t["kickoff"][2 * i], "as_of": as_of,
             "y_home": int(t["cards"][2 * i]), "y_away": int(t["cards"][2 * i + 1])}
            for i in range(t.height // 2)]  # fmt: skip
    for b in base:
        ph, pa = clim[(b["league_id"], True)], clim[(b["league_id"], False)]
        tot = total_pmf(ph[None, :], pa[None, :])[0]
        rows.append({**b, "model": "B_clim", "horizon": "", **score(tot, ph, pa, b["y_home"],
                                                                     b["y_away"])})  # fmt: skip
    for v, post in posts.items():
        for hz in HORIZONS:
            pool = ~named | ((np.isin(lg, list(POOL_24))) if hz == "24" else False)
            mu = target_mu(post, ix, t, np.asarray(pool, dtype=bool))
            pmf = team_pmf(mu, post["phi"])
            for i, b in enumerate(base):
                tot = total_pmf(pmf[2 * i], pmf[2 * i + 1]).mean(axis=0)
                th, ta = pmf[2 * i].mean(axis=0), pmf[2 * i + 1].mean(axis=0)
                mass = float((pmf[2 * i].sum(axis=1) * pmf[2 * i + 1].sum(axis=1)).mean())
                if abs(tot.sum() - mass) > 1e-9:
                    raise AssertionError("total is not the convolution of the team counts")
                rows.append({**b, "model": v, "horizon": hz,
                             **score(tot, th, ta, b["y_home"], b["y_away"])})  # fmt: skip
    return rows


def run_part(table: pl.DataFrame, bundle: str, season: int, end: datetime | None = None,
             on_refit: Any = None) -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """B_clim, C0, C1, C2 (with C2's prior sensitivity) on one bundle-season."""
    if season in HOLDOUT or (season not in DEV and not (season == WINDOW_SEASON
                                                         and end is not None)):  # fmt: skip
        raise ValueError(f"season {season} is not a dev season (window (i) only via `window`)")
    rows: list[dict[str, Any]] = []
    diags: list[dict[str, Any]] = []
    for as_of, tgt, train in plan(table, bundle, season, end):
        d = design(train)
        posts: dict[str, dict[str, Arr]] = {}
        for v in VARIANTS:
            post, diag = fit(d, v)
            pp = ppc(post, d)
            diag |= {"ppc": pp, "ppc_ok": pp["pass"]}
            if v == "C2":
                alt = {str(s): fit(d, v, s)[0] for s in SENS_SCALES}
                sens = sensitivity(post, alt)
                diag |= {"sens": sens, "sens_ok": sens["pass"],
                         "sigma_ref": float(post["sigma_ref"].mean())}  # fmt: skip
            if v != "C0":
                diag |= {"sigma_a": float(post["sigma_a"].mean()),
                         "sigma_b": float(post["sigma_b"].mean())}  # fmt: skip
            diag |= {"phi": float(post["phi"].mean()), "variant": v, "bundle": bundle,
                     "season": season, "as_of": as_of.isoformat(), "n_train": len(d.y),
                     "n_refs": len(d.index.refs)}  # fmt: skip
            diag["ok"] = bool(diag["gates_ok"] and diag["ppc_ok"] and diag.get("sens_ok", True))
            posts[v] = post
            diags.append(diag)
        rows += score_refit(posts, d.index, tgt, climatology(train), as_of)
        print(f"  {as_of:%Y-%m-%d}: {len(d.y)} train rows, {tgt.height // 2} matches; "
              + "; ".join(f"{g['variant']} rhat {g['rhat']:.3f} div {g['divergences']:.0f} "
                          f"ppc {'ok' if g['ppc_ok'] else 'FAIL'}"
                          for g in diags[-len(VARIANTS):]), flush=True)  # fmt: skip
        if on_refit is not None:
            on_refit(pl.DataFrame(rows), diags)
    return pl.DataFrame(rows), diags


# --------------------------------------------------------------------------------- CLI


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def _collect(store: Any, run: str, keys: Sequence[str]) -> tuple[pl.DataFrame, list[Any]]:
    from adatta.adapters.storage import try_get

    preds, diags, missing = [], [], []
    for key in keys:
        data = try_get(store, f"{PREFIX}/parts/{run}/{key}.parquet")
        if data is None:
            missing.append(key)
            continue
        preds.append(pl.read_parquet(io.BytesIO(data)))
        diags += json.loads(store.get(f"{PREFIX}/parts/{run}/{key}.json"))["diags"]
    if missing:
        raise SystemExit(f"missing parts: {len(missing)} (first {missing[0]})")
    return pl.concat(preds, how="diagonal"), diags


def main(argv: Sequence[str] | None = None) -> None:
    from adatta.adapters.storage import make_store, try_get
    from adatta.config import get_settings
    from adatta.eval import cards as ec
    from adatta.eval import harness
    from adatta.features import cards as fc

    p = argparse.ArgumentParser(prog="python -m adatta.model.cards")
    p.add_argument("cmd", choices=("run", "window", "assemble", "judge"))
    p.add_argument("--bundle", default="")
    p.add_argument("--season", type=int, default=0)
    p.add_argument("--upload", default="")
    p.add_argument("--fetch", default="")
    p.add_argument("--dev", default="", help="judge: the dev run whose chosen variant is judged")
    p.add_argument("--table", default="latest")
    args = p.parse_args(argv)
    store = make_store(get_settings())
    table, _, digest = fc.load_table(store, args.table)
    bundles = list(harness.load_config().bundles)
    if args.cmd in ("run", "window"):
        season = args.season if args.cmd == "run" else WINDOW_SEASON
        end = WINDOW_END if args.cmd == "window" else None
        key = f"{PREFIX}/parts/{args.upload}/{args.bundle}_{season}"

        def save(rows: pl.DataFrame, diags: list[dict[str, Any]]) -> None:
            store.put(f"{key}.parquet", _parquet(rows))
            store.put(f"{key}.json", json.dumps({"table": digest, "diags": diags},
                                                default=str).encode())  # fmt: skip

        rows, diags = run_part(table, args.bundle, season, end, save)
        save(rows, diags)
        print(f"{args.bundle} {season}: {len(diags) // len(VARIANTS)} refits; passing (c): "
              + ", ".join(f"{v} {sum(g['ok'] for g in diags if g['variant'] == v)}"
                          for v in VARIANTS))  # fmt: skip
    elif args.cmd == "assemble":
        pred, diags = _collect(store, args.fetch, [f"{b}_{s}" for b in bundles for s in DEV])
        v = ec.gate(pred, diags)
        store.put(f"{PREFIX}/reports/{args.fetch}/pred.parquet", _parquet(pred))
        store.put(f"{PREFIX}/reports/{args.fetch}/gate.json",
                  json.dumps(v, default=str).encode())  # fmt: skip
        print(ec.markdown(v, digest))
    else:
        marker = f"{PREFIX}/window/judged.json"
        if try_get(store, marker) is not None:
            raise SystemExit("window (i) was judged already; it is read once")
        dev = json.loads(store.get(f"{PREFIX}/reports/{args.dev}/gate.json"))
        if not dev.get("passed"):
            raise SystemExit("the dev gate did not pass: window (i) is not read")
        pred, diags = _collect(store, args.fetch, [f"{b}_{WINDOW_SEASON}" for b in bundles])
        w = ec.window(pred, diags, dev["chosen"])
        store.put(marker, json.dumps({"fetch": args.fetch, "dev": args.dev, **w},
                                     default=str).encode())  # fmt: skip
        print(ec.window_markdown(w, digest))


if __name__ == "__main__":
    main()
