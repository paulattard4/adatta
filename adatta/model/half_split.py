"""P3b-1, the first-half share of goals (docs/PHASE3B_TASKS.md (b), (c); spec §8.2; D47).

For side j of match i with N 90' goals, the observed first-half goals x ~ Binomial(N, p):
- S0: logit p = beta_side, beta ~ Normal(logit 0.44, 0.5)
- S1: + alpha_league = sigma_league (z - mean z), sigma_league ~ HalfNormal(0.2)
- S2: + u_team = sigma_team w_team, sigma_team ~ HalfNormal(0.2) (the scoring team)
Fitted jointly over the ten bundle leagues by NUTS (NumPyro; the model group, D13), 2,000
draws, as of each 28-day dev Monday (D14) on matches final by then within `lookback_days`.
Each refit is gated (c): R-hat, ESS, divergences; the 0-0 half-time PPC; prior sensitivity
(scales x0.5, x2: every league's mean share moves < 0.005). The dispersion PPC (N = 1, 2,
3, 4, 5+) is reported, not gating (D48, as D20). Half-time scores never enter Layer 2.

CLI (the model container, scripts/model.sh; one backtest-type run at a time):
  run --variant S0|S1|S2 --upload RUN [--part i/n] [--only YYYY-MM-DD,...]
      every dev share refit of the variant: posterior and gates to the store
      (half/share/RUN/<variant>/<as_of>.npz, .json); metrics only to stdout
Holdout seasons are refused.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import SOURCE_ID
from adatta.eval import harness
from adatta.eval.selection import DEV, HoldoutRefused

Arr = NDArray[np.float64]
IntArr = NDArray[np.int64]
PREFIX = "half"
VARIANTS = ("S0", "S1", "S2")
LOGIT0 = math.log(0.44 / 0.56)
BETA_SD, SIGMA_SD = 0.5, 0.2
REFIT_DAYS = 28  # D14
SEED = 20261125
DRAWS, CHAINS = 500, 4  # 2,000 draws
TUNE = 1000  # warm-up steps (D48)
PRED_DRAWS = 200  # thinned draws kept for prediction and the PPC
PPC_BAND = (0.025, 0.975)  # the posterior predictive 95% band
N_GROUPS = (1, 2, 3, 4, 5)  # 5 = 5+
SENS_SCALES = (0.5, 2.0)
SENS_MAX = 0.005  # a league's mean share may move less than this under x0.5 / x2
PASS_SHARE = 0.97  # the chosen variant passes (c) at >= 97% of refits


def leagues(cfg: harness.EvalConfig) -> list[int]:
    return [lid for b in cfg.bundles.values() for lid in b]


# ----------------------------------------------------------------------------------- data


def load(session: Session, league_ids: list[int]) -> pl.DataFrame:
    """Phase 1a's matches (regular-season league, 90' score known) with the half-time score
    (`xh`, `xa`; null when not stored, never imputed) and whether the league-season has
    `has_half_split_stats` available (`ht_ok`, the scope of the forecasts, rule 8)."""
    from adatta.db import models

    df, _ = harness.load_matches(session, league_ids)
    R = models.MatchResult
    ht = session.execute(
        select(R.match_id, R.home_goals_ht, R.away_goals_ht)
        .where(R.match_id.in_(df["match_id"].to_list()), R.superseded_at.is_(None))
    ).all()  # fmt: skip
    htf = pl.DataFrame([{"match_id": int(a), "xh": b, "xa": c} for a, b, c in ht],
                       schema={"match_id": pl.Int64, "xh": pl.Int64, "xa": pl.Int64})  # fmt: skip
    K, C, S = models.CompetitionSourceCapability, models.Competition, models.Season
    cap = session.execute(
        select(C.source_ref, S.year).select_from(K)
        .join(C, C.competition_id == K.competition_id).join(S, S.season_id == K.season_id)
        .where(K.capability == "has_half_split_stats", K.valid_to.is_(None),
               K.status == "available", C.source_id == SOURCE_ID,
               C.source_ref.in_([str(i) for i in league_ids]))
    ).all()  # fmt: skip
    ok = pl.DataFrame([{"league_id": int(a), "season": int(b)} for a, b in cap],
                      schema={"league_id": pl.Int64, "season": pl.Int64})  # fmt: skip
    df = df.join(htf.unique("match_id"), on="match_id", how="left")
    df = df.join(ok.unique().with_columns(ht_ok=pl.lit(True)), on=["league_id", "season"],
                 how="left").with_columns(pl.col("ht_ok").fill_null(False))  # fmt: skip
    # a half-time score above full time is an error in the record, not a split
    bad = (pl.col("xh") > pl.col("hg")) | (pl.col("xa") > pl.col("ag"))
    df = df.with_columns(pl.when(bad).then(None).otherwise(pl.col("xh")).alias("xh"),
                         pl.when(bad).then(None).otherwise(pl.col("xa")).alias("xa"))  # fmt: skip
    return df.sort("kickoff", "match_id")


def share_plan(df: pl.DataFrame, cfg: harness.EvalConfig, seasons: Sequence[int]
               ) -> list[tuple[datetime, IntArr, IntArr]]:  # fmt: skip
    """The 28-day share refits over `seasons` (dev only): (as_of, targets, training rows)."""
    if set(seasons) - set(DEV):
        raise HoldoutRefused(f"seasons {sorted(set(seasons) - set(DEV))} are not dev seasons")
    return harness.refit_plan(df, cfg, list(seasons), REFIT_DAYS)


@dataclass
class Obs:
    """One row per side with N >= 1 of the training matches, plus the match-level arrays the
    0-0 PPC needs."""

    n: IntArr
    x: IntArr
    side: IntArr
    league: IntArr
    team: IntArr
    match: IntArr  # position of the side's match in the training matches
    league_ids: list[int]
    team_ids: list[int]
    n_matches: int


def observations(train: pl.DataFrame, league_ids: list[int]) -> Obs:
    t = train.filter(pl.col("xh").is_not_null() & pl.col("xa").is_not_null())
    lix = {lid: i for i, lid in enumerate(league_ids)}
    teams = sorted(set(t["home"].to_list()) | set(t["away"].to_list()))
    tix = {x: i for i, x in enumerate(teams)}
    k = np.arange(t.height)
    n = np.concatenate([t["hg"].to_numpy(), t["ag"].to_numpy()])
    x = np.concatenate([t["xh"].to_numpy(), t["xa"].to_numpy()])
    side = np.concatenate([np.zeros(t.height, np.int64), np.ones(t.height, np.int64)])
    lg = np.array([lix[int(v)] for v in t["league_id"]], dtype=np.int64)
    tm = np.array([tix[int(v)] for v in t["home"]] + [tix[int(v)] for v in t["away"]],
                  dtype=np.int64)  # fmt: skip
    keep = n > 0
    return Obs(n=n[keep].astype(np.int64), x=x[keep].astype(np.int64), side=side[keep],
               league=np.concatenate([lg, lg])[keep], team=tm[keep],
               match=np.concatenate([k, k])[keep], league_ids=league_ids, team_ids=teams,
               n_matches=t.height)  # fmt: skip


# ---------------------------------------------------------------------------------- model


def grouped(o: Obs) -> dict[str, IntArr]:
    """The sides summed by (side, league, team): every side in a group has the same share in
    every variant, and a sum of binomials with one p is binomial in the summed N, so the
    likelihood (and the posterior) is the per-side one up to a constant."""
    k = (o.side * len(o.league_ids) + o.league) * len(o.team_ids) + o.team
    u, inv = np.unique(k, return_inverse=True)
    return {
        "n": np.bincount(inv, weights=o.n).astype(np.int64),
        "x": np.bincount(inv, weights=o.x).astype(np.int64),
        "side": (u // (len(o.league_ids) * len(o.team_ids))).astype(np.int64),
        "league": ((u // len(o.team_ids)) % len(o.league_ids)).astype(np.int64),
        "team": (u % len(o.team_ids)).astype(np.int64),
    }


def build(o: Obs, variant: str, scale: float = 1.0) -> Any:
    import pymc as pm

    if variant not in VARIANTS:
        raise ValueError(variant)
    g = grouped(o)
    with pm.Model() as m:
        beta = pm.Normal("beta", LOGIT0, BETA_SD * scale, shape=2)
        eta = beta[g["side"]]
        if variant in ("S1", "S2"):
            s_l = pm.HalfNormal("sigma_league", SIGMA_SD * scale)
            z_l = pm.Normal("z_league", 0.0, 1.0, shape=len(o.league_ids))
            alpha = pm.Deterministic("alpha", s_l * (z_l - z_l.mean()))
            eta = eta + alpha[g["league"]]
        if variant == "S2":
            s_t = pm.HalfNormal("sigma_team", SIGMA_SD * scale)
            w = pm.Normal("w_team", 0.0, 1.0, shape=len(o.team_ids))
            eta = eta + pm.Deterministic("u", s_t * w)[g["team"]]
        pm.Binomial("x", n=g["n"], logit_p=eta, observed=g["x"])
    return m


def fit(o: Obs, variant: str, scale: float = 1.0, seed: int = SEED) -> Any:
    import os

    # the four chains in parallel on the CPU (read when JAX first starts)
    os.environ.setdefault("XLA_FLAGS", f"--xla_force_host_platform_device_count={CHAINS}")
    import pymc as pm

    with build(o, variant, scale):
        return pm.sample(draws=DRAWS, tune=TUNE, chains=CHAINS, nuts_sampler="numpyro",
                         random_seed=seed, target_accept=0.9, progressbar=False)  # fmt: skip


def posterior(idata: Any, o: Obs, n: int = PRED_DRAWS) -> dict[str, Arr]:
    """Thinned draws: beta (n, 2), alpha (n, leagues), u (n, teams); zeros where the
    variant has no such term."""
    post = idata["posterior"]
    post = post.to_dataset() if hasattr(post, "to_dataset") else post

    def flat(v: str) -> Arr:
        arr = np.asarray(post[v].values, dtype=np.float64)
        f = arr.reshape(-1, *arr.shape[2:])
        return f[:: max(len(f) // n, 1)][:n]

    beta = flat("beta")
    d = len(beta)
    return {
        "beta": beta,
        "alpha": flat("alpha") if "alpha" in post else np.zeros((d, len(o.league_ids))),
        "u": flat("u") if "u" in post else np.zeros((d, len(o.team_ids))),
    }


def expit(v: Arr) -> Arr:
    return np.asarray(1.0 / (1.0 + np.exp(-v)), dtype=np.float64)


def share(post: dict[str, Arr], league_ids: Sequence[int], team_ids: Sequence[int],
          side: int, league: int, team: int) -> Arr:  # fmt: skip
    """(draws,) first-half share of `team`'s goals as `side` (0 home, 1 away) in `league`.
    A team with no training match gets the pool mean (u = 0)."""
    eta = post["beta"][:, side] + post["alpha"][:, list(league_ids).index(league)]
    if team in team_ids:
        eta = eta + post["u"][:, list(team_ids).index(team)]
    return expit(eta)


def obs_share(post: dict[str, Arr], o: Obs) -> Arr:
    """(draws, sides) share of every observation."""
    eta = post["beta"][:, o.side] + post["alpha"][:, o.league] + post["u"][:, o.team]
    return expit(eta)


def league_means(post: dict[str, Arr]) -> Arr:
    """Each league's mean share at u = 0, averaged over the sides (the sensitivity check)."""
    p = expit(post["beta"][:, :, None] + post["alpha"][:, None, :])  # (d, side, league)
    return np.asarray(p.mean(axis=(0, 1)), dtype=np.float64)


# ---------------------------------------------------------------------------------- gates


def _pearson(x: Any, n: Any, p: Arr) -> float:
    return float(((x - n * p) ** 2 / np.maximum(n * p * (1 - p), 1e-12)).sum())


def ppc(post: dict[str, Arr], o: Obs, rng: np.random.Generator) -> dict[str, float]:
    """Quantile of each observed statistic in its posterior predictive:
    - `disp_N`: the Pearson dispersion of x given N (N = 1..4, 5 = 5+), at the posterior-mean
      share, replicated from each draw's share;
    - `ht00`: matches 0-0 at half time among those with a goal."""
    p = obs_share(post, o)  # (d, k)
    pbar = p.mean(axis=0)
    sims = rng.binomial(o.n[None, :], p)  # (d, k)
    out = {}
    for g in N_GROUPS:
        s = (o.n == g) if g < N_GROUPS[-1] else (o.n >= g)
        if s.sum() < 2:
            continue
        obs = _pearson(o.x[s], o.n[s], pbar[s])
        rep = np.array([_pearson(sims[i, s], o.n[s], pbar[s]) for i in range(len(p))])
        out[f"disp_{g}"] = float((rep < obs).mean())
    goals = np.bincount(o.match, minlength=o.n_matches) > 0  # matches with a goal
    first = np.bincount(o.match, weights=o.x, minlength=o.n_matches)
    obs00 = float(((first == 0) & goals).sum())
    rep_first = np.stack([np.bincount(o.match, weights=sims[i], minlength=o.n_matches)
                          for i in range(len(p))])  # fmt: skip
    rep00 = ((rep_first == 0) & goals[None, :]).sum(axis=1)
    out["ht00"] = float((rep00 < obs00).mean())
    return out


def gates(idata: Any, post: dict[str, Arr], sens: dict[float, dict[str, Arr]], o: Obs,
          rng: np.random.Generator) -> dict[str, Any]:  # fmt: skip
    from adatta.model import diagnostics as dg

    conv = [c.as_dict() for c in dg.convergence(idata)]
    q = ppc(post, o, rng)
    base = league_means(post)
    moves = {str(k): float(np.abs(league_means(v) - base).max()) for k, v in sens.items()}
    ok_conv = all(c["passed"] for c in conv)
    # D48: the dispersion PPC is reported, not gating (as D20); the 0-0 PPC gates
    ok_ppc = PPC_BAND[0] <= q["ht00"] <= PPC_BAND[1]
    ok_sens = all(v < SENS_MAX for v in moves.values())
    ident: dict[str, float] = {}
    if np.abs(post["u"]).sum() > 0:
        u_sd = post["u"].std(axis=1).mean()
        ident["sigma_team_post_mean_sd_of_u"] = float(u_sd)
        ident["sigma_team_prior_mean"] = SIGMA_SD * math.sqrt(2 / math.pi)
    return {
        "convergence": conv, "ppc": q,
        "dispersion_in_band": {k: PPC_BAND[0] <= v <= PPC_BAND[1] for k, v in q.items()
                               if k.startswith("disp_")},
        "sensitivity_max_move": moves, "identifiability": ident,
        "passes": ok_conv and ok_ppc and ok_sens,
        "failed": [n for n, ok in (("convergence", ok_conv), ("ppc", ok_ppc),
                                   ("sensitivity", ok_sens)) if not ok],
        "n_obs": len(o.n), "n_matches": o.n_matches, "n_teams": len(o.team_ids),
    }  # fmt: skip


# ----------------------------------------------------------------------------------- store


def key(run: str, variant: str, as_of: datetime, ext: str) -> str:
    return f"{PREFIX}/share/{run}/{variant}/{as_of:%Y%m%d}.{ext}"


def encode(post: dict[str, Arr], o: Obs) -> bytes:
    buf = io.BytesIO()
    arrays: dict[str, Any] = {**post, "league_ids": np.array(o.league_ids, np.int64),
                              "team_ids": np.array(o.team_ids, np.int64)}  # fmt: skip
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def decode(data: bytes) -> tuple[dict[str, Arr], list[int], list[int]]:
    z = np.load(io.BytesIO(data))
    post = {k: np.asarray(z[k], dtype=np.float64) for k in ("beta", "alpha", "u")}
    return post, [int(v) for v in z["league_ids"]], [int(v) for v in z["team_ids"]]


def refit(df: pl.DataFrame, train: IntArr, as_of: datetime, variant: str, league_ids: list[int]
          ) -> tuple[dict[str, Arr], Obs, dict[str, Any]]:  # fmt: skip
    """One share refit with its gates."""
    rows = df[train]
    lag = harness.load_config().result_available_after_hours
    last = rows["kickoff"].max()
    assert isinstance(last, datetime)
    if (last.timestamp() + lag * 3600) > as_of.timestamp():  # never leak
        raise ValueError(f"a training match of the {as_of:%Y-%m-%d} share refit is not final")
    o = observations(rows, league_ids)
    t0 = time.monotonic()
    idata = fit(o, variant)
    post = posterior(idata, o)
    sens = {s: posterior(fit(o, variant, s), o) for s in SENS_SCALES}
    g = gates(idata, post, sens, o, np.random.default_rng(SEED))
    g["seconds"] = time.monotonic() - t0
    g["as_of"] = as_of.isoformat()
    g["variant"] = variant
    return post, o, g


def run(df: pl.DataFrame, cfg: harness.EvalConfig, variant: str, store: Any, upload: str,
        part: tuple[int, int] = (0, 1), only: Sequence[str] = ()
        ) -> list[dict[str, Any]]:  # fmt: skip
    """`only` (YYYY-MM-DD) reruns just those dates, e.g. after a killed job; the seed is
    fixed, so a rerun gives the refit an unbroken run would have."""
    plan = share_plan(df, cfg, DEV)
    mine = [p for i, p in enumerate(plan) if i % part[1] == part[0]]
    if only:
        mine = [p for p in mine if f"{p[0]:%Y-%m-%d}" in set(only)]
        if len(mine) != len(set(only)):
            raise SystemExit(f"--only {list(only)}: not all are share refit dates")
    out = []
    for as_of, _, train in mine:
        post, o, g = refit(df, train, as_of, variant, leagues(cfg))
        store.put(key(upload, variant, as_of, "npz"), encode(post, o))
        store.put(key(upload, variant, as_of, "json"), json.dumps(g).encode())
        out.append(g)
        print(f"{variant} {as_of:%Y-%m-%d}: {'pass' if g['passes'] else 'FAIL ' + str(g['failed'])}"
              f", {g['n_obs']} sides, {g['seconds']:.0f} s", flush=True)  # fmt: skip
    return out


def main(argv: Sequence[str] | None = None) -> None:
    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session

    ap = argparse.ArgumentParser(prog="python -m adatta.model.half_split")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--variant", required=True, choices=VARIANTS)
    r.add_argument("--upload", required=True)
    r.add_argument("--part", default="0/1")
    r.add_argument("--only", default="", help="comma-separated YYYY-MM-DD dates to rerun")
    args = ap.parse_args(argv)
    cfg = harness.load_config()
    with get_session() as s:
        df = load(s, leagues(cfg))
        s.rollback()
    i, n = (int(v) for v in args.part.split("/"))
    only = [x for x in args.only.split(",") if x]
    got = run(df, cfg, args.variant, make_store(get_settings()), args.upload, (i, n), only)
    print(json.dumps({"variant": args.variant, "refits": len(got),
                      "passing": sum(g["passes"] for g in got),
                      "at": datetime.now(UTC).isoformat()}))  # fmt: skip


if __name__ == "__main__":
    main()
