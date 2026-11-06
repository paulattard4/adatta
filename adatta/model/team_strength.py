"""Phase 1b production team model, v1: Layers 1–2 on 90' goals (spec §5.1–5.3, §8.1).

    attack[s, t]  = attack0[t] + cumsum_s(sd_s * z_attack[s, t]), centred over teams per step
    defence[s, t] likewise
    sd_s          = sigma_walk, or sigma_walk * sqrt(1 + reset_weeks) at a season's first step
                    (the soft reset of §5.3: the prior season's state carries over, inflated)
    log lam_home  = mu_league[l] + h_league[l] + sigma_home * h_team[home]
                    + attack[s, home] - defence[s, away]
    log lam_away  = mu_league[l] + attack[s, away] - defence[s, home]
    + Dixon-Coles tau(rho) on the four low scores

Priors are §5.1–5.2: mu_league ~ Normal(log 1.35, 0.3); sigma_attack, sigma_defence ~
HalfNormal(0.5); sigma_walk ~ HalfNormal(0.15); sigma_home ~ HalfNormal(0.3). The walk is
non-centred, with zero-sum innovations over teams (identification). Steps are calendar
weeks (refits are weekly, D11). Teams are shared across the bundle's leagues, so promoted
and relegated teams carry their state across (§10.3).

D15 (`structure="strength_open"`): each team is instead a strength walk s and an openness
walk o (how high-scoring its games are), with attack = (s + o) / sqrt 2 and defence =
(s - o) / sqrt 2, each with its own scale and walk sd. This is a rotation of the spec model:
with equal scales and walk sds it is exactly the independent-prior model, so the data decide
how hard openness is shrunk. Kept only if it passes the D15 acceptance test.

Only importable where the model dependency group is installed (Linux / Docker, D13).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

IntArr = NDArray[np.int64]
STRUCTURES = ("independent", "strength_open")
LOG_BASE_RATE = float(np.log(1.35))


@dataclass(frozen=True)
class FitData:
    home: IntArr
    away: IntArr
    league: IntArr
    step: IntArr
    hg: IntArr
    ag: IntArr
    season_start: NDArray[np.bool_]  # per step: first step of a new season
    team_ids: IntArr
    leagues: list[int]
    start: datetime
    n_steps: int


def prepare(df: pl.DataFrame, leagues: list[int], as_of: datetime, lookback_days: int,
            lag_hours: float = 3.0) -> FitData:  # fmt: skip
    """Training rows observable at `as_of` (kickoff + lag <= as_of, within the lookback),
    indexed by team, league and weekly step."""
    lo = as_of - timedelta(days=lookback_days)
    hi = as_of - timedelta(hours=lag_hours)
    d = df.filter((pl.col("kickoff") >= lo) & (pl.col("kickoff") <= hi)).sort("kickoff")
    if d.height == 0:
        raise ValueError("no training rows in the window")
    start = lo - timedelta(days=lo.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    days = np.array([(k - start).days for k in d["kickoff"].to_list()], dtype=np.int64)
    step = (days // 7).astype(np.int64)
    n_steps = int((as_of - start).days // 7) + 1
    teams = np.unique(np.concatenate([d["home"].to_numpy(), d["away"].to_numpy()]))
    tix = {int(t): i for i, t in enumerate(teams)}
    lix = {lid: i for i, lid in enumerate(leagues)}
    season_of_step = np.full(n_steps, -1)
    for s, season in zip(step, d["season"].to_numpy(), strict=True):
        season_of_step[s] = max(season_of_step[s], int(season))
    # carry the last seen season forward through empty weeks (summer, winter breaks)
    for s in range(1, n_steps):
        if season_of_step[s] < 0:
            season_of_step[s] = season_of_step[s - 1]
    season_start = np.zeros(n_steps, dtype=bool)
    season_start[1:] = season_of_step[1:] > season_of_step[:-1]
    return FitData(
        home=np.array([tix[int(t)] for t in d["home"]], dtype=np.int64),
        away=np.array([tix[int(t)] for t in d["away"]], dtype=np.int64),
        league=np.array([lix[int(x)] for x in d["league_id"]], dtype=np.int64),
        step=step, hg=d["hg"].to_numpy().astype(np.int64), ag=d["ag"].to_numpy().astype(np.int64),
        season_start=season_start, team_ids=teams.astype(np.int64), leagues=leagues,
        start=start, n_steps=n_steps,
    )  # fmt: skip


def build(fd: FitData, prior_scale: float = 1.0, structure: str = "independent") -> Any:
    """`prior_scale` multiplies every prior scale (the §14.5 prior-sensitivity check uses 0.5
    and 2); 1.0 is the production model. `structure` is "independent" (spec §5.1) or
    "strength_open" (D15)."""
    if structure not in STRUCTURES:
        raise ValueError(f"structure must be one of {STRUCTURES}")
    import pymc as pm
    import pytensor.tensor as pt

    coords = {"team": fd.team_ids.tolist(), "step": list(range(fd.n_steps)),
              "league": fd.leagues}  # fmt: skip
    k = prior_scale
    with pm.Model(coords=coords) as model:
        mu_league = pm.Normal("mu_league", LOG_BASE_RATE, 0.3 * k, dims="league")
        if structure == "independent":
            sigma_a = pm.HalfNormal("sigma_attack", 0.5 * k)
            sigma_b = pm.HalfNormal("sigma_defence", 0.5 * k)
            sigma_walk = pm.HalfNormal("sigma_walk", 0.15 * k)
            walk_b = sigma_walk
        else:
            sigma_a = pm.HalfNormal("sigma_strength", 0.5 * k)
            sigma_b = pm.HalfNormal("sigma_open", 0.5 * k)
            sigma_walk = pm.HalfNormal("sigma_walk", 0.15 * k)
            walk_b = pm.HalfNormal("sigma_walk_open", 0.15 * k)
        # Season reset as a multiple of the weekly step: with only 2–3 season boundaries in a
        # window, a free scale is nearly unidentified (R-hat 1.02, ESS ~270 in the first fits).
        # Prior: a reset worth ~16 weeks of drift (a transfer window), within about x2.7.
        reset_weeks = pm.LogNormal("reset_weeks", np.log(16.0), 0.5 * k)
        sigma_home = pm.HalfNormal("sigma_home", 0.3 * k)
        h_league = pm.Normal("h_league", 0.25, 0.2 * k, dims="league")
        h_team = pm.Normal("h_team", 0.0, 1.0, dims="team")
        rho = pm.Uniform("rho", -0.2, 0.2)

        reset = pt.where(pt.as_tensor(fd.season_start), pt.sqrt(1.0 + reset_weeks), 1.0)

        def walk(name: str, sigma0: Any, step_sd: Any, keep: bool = True) -> Any:
            # Zero-sum over teams at every step: identifies the level (it lives in mu_league)
            # without the redundant directions that re-centring after the fact leaves behind.
            z0 = pm.ZeroSumNormal(f"{name}_z0", 1.0, dims="team")
            z = pm.ZeroSumNormal(f"{name}_z", 1.0, dims=("step", "team"))
            path = z0[None, :] * sigma0 + pt.cumsum(z * (step_sd * reset)[:, None], axis=0)
            # Only the final step is stored (all prediction needs): the full path for every
            # draw is ~0.5 GB per fit and can be rebuilt from the innovations if needed.
            if keep:
                pm.Deterministic(f"{name}_last", path[-1], dims="team")
            return path

        if structure == "independent":
            attack = walk("attack", sigma_a, sigma_walk)
            defence = walk("defence", sigma_b, walk_b)
        else:
            strength = walk("strength", sigma_a, sigma_walk, keep=False)
            openness = walk("open", sigma_b, walk_b, keep=False)
            attack = (strength + openness) / np.sqrt(2.0)
            defence = (strength - openness) / np.sqrt(2.0)
            pm.Deterministic("attack_last", attack[-1], dims="team")
            pm.Deterministic("defence_last", defence[-1], dims="team")
        home = h_league[fd.league] + sigma_home * h_team[fd.home]
        log_lam = (mu_league[fd.league] + home + attack[fd.step, fd.home]
                   - defence[fd.step, fd.away])  # fmt: skip
        log_mu = mu_league[fd.league] + attack[fd.step, fd.away] - defence[fd.step, fd.home]
        lam, mu_ = pt.exp(log_lam), pt.exp(log_mu)
        pm.Poisson("home_goals", lam, observed=fd.hg)
        pm.Poisson("away_goals", mu_, observed=fd.ag)
        hg, ag = fd.hg, fd.ag
        tau = pt.switch(
            (hg == 0) & (ag == 0), 1 - lam * mu_ * rho,
            pt.switch((hg == 0) & (ag == 1), 1 + lam * rho,
                      pt.switch((hg == 1) & (ag == 0), 1 + mu_ * rho,
                                pt.switch((hg == 1) & (ag == 1), 1 - rho, 1.0))),
        )  # fmt: skip
        pm.Potential("dixon_coles", pt.sum(pt.log(pt.maximum(tau, 1e-10))))
    return model


def sample(fd: FitData, seed: int, *, prior_scale: float = 1.0, draws: int = 1000,
           chains: int = 4, target_accept: float = 0.95,
           structure: str = "independent") -> Any:  # fmt: skip
    """NUTS via NumPyro (D10), posterior predictive goals included. Returns InferenceData."""
    import pymc as pm

    with build(fd, prior_scale, structure):
        idata = pm.sample(
            draws=draws, tune=draws, chains=chains, nuts_sampler="numpyro", random_seed=seed,
            target_accept=target_accept, progressbar=False,
        )  # fmt: skip
        pm.sample_posterior_predictive(idata, extend_inferencedata=True, random_seed=seed,
                                       progressbar=False)  # fmt: skip
    return idata


def stacked(idata: Any, group: str = "posterior") -> Any:
    """A group of the inference data as an xarray Dataset with (chain, draw) stacked into
    `sample` (ArviZ 1.x returns DataTree nodes, which don't stack)."""
    node = idata[group]
    ds = node.to_dataset() if hasattr(node, "to_dataset") else node
    return ds.stack(sample=("chain", "draw"))


def rates(idata: Any, fd: FitData, home: IntArr, away: IntArr, league: IntArr,
          max_draws: int = 400) -> tuple[NDArray[np.float64], NDArray[np.float64]]:  # fmt: skip
    """Posterior draws of (lambda_home, lambda_away) for matches after the training window,
    from the final step's state. home/away index fd.team_ids; -1 = unseen team, which gets
    the average of its league's teams at that step (cold start, reported apart)."""
    post = stacked(idata)
    n = post.sizes["sample"]
    take = np.linspace(0, n - 1, min(max_draws, n)).astype(int)
    att = post["attack_last"].values[:, take]  # (team, draw)
    dfn = post["defence_last"].values[:, take]
    mu_l = post["mu_league"].values[:, take]
    h_l = post["h_league"].values[:, take]
    h_t = post["h_team"].values[:, take] * post["sigma_home"].values[take]
    recent = fd.step > fd.step.max() - 52  # teams seen in each league over the last year
    members = {
        lg: np.unique(np.concatenate([fd.home[recent & (fd.league == lg)],
                                      fd.away[recent & (fd.league == lg)]]))
        for lg in np.unique(league)
    }  # fmt: skip
    zeros = np.zeros(len(take))

    def team(t: int, lg: int) -> tuple[NDArray[np.float64], ...]:
        if t >= 0:
            return att[t], dfn[t], h_t[t]
        m = members[lg]
        return att[m].mean(0), dfn[m].mean(0), zeros

    out_h, out_a = [], []
    for hh, aa, lg in zip(home, away, league, strict=True):
        a_h, d_h, ht = team(int(hh), int(lg))
        a_a, d_a, _ = team(int(aa), int(lg))
        out_h.append(np.exp(mu_l[lg] + h_l[lg] + ht + a_h - d_a))
        out_a.append(np.exp(mu_l[lg] + a_a - d_h))
    return np.array(out_h), np.array(out_a)
