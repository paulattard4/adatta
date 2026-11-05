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


def build(fd: FitData) -> Any:
    import pymc as pm
    import pytensor.tensor as pt

    coords = {"team": fd.team_ids.tolist(), "step": list(range(fd.n_steps)),
              "league": fd.leagues}  # fmt: skip
    with pm.Model(coords=coords) as model:
        mu_league = pm.Normal("mu_league", LOG_BASE_RATE, 0.3, dims="league")
        sigma_attack = pm.HalfNormal("sigma_attack", 0.5)
        sigma_defence = pm.HalfNormal("sigma_defence", 0.5)
        sigma_walk = pm.HalfNormal("sigma_walk", 0.15)
        # Season reset as a multiple of the weekly step: with only 2–3 season boundaries in a
        # window, a free scale is nearly unidentified (R-hat 1.02, ESS ~270 in the first fits).
        # Prior: a reset worth ~16 weeks of drift (a transfer window), within about x2.7.
        reset_weeks = pm.LogNormal("reset_weeks", np.log(16.0), 0.5)
        sigma_home = pm.HalfNormal("sigma_home", 0.3)
        h_league = pm.Normal("h_league", 0.25, 0.2, dims="league")
        h_team = pm.Normal("h_team", 0.0, 1.0, dims="team")
        rho = pm.Uniform("rho", -0.2, 0.2)

        sd = pt.where(
            pt.as_tensor(fd.season_start), sigma_walk * pt.sqrt(1.0 + reset_weeks), sigma_walk
        )

        def walk(name: str, sigma0: Any) -> Any:
            # Zero-sum over teams at every step: identifies the level (it lives in mu_league)
            # without the redundant directions that re-centring after the fact leaves behind.
            z0 = pm.ZeroSumNormal(f"{name}_z0", 1.0, dims="team")
            z = pm.ZeroSumNormal(f"{name}_z", 1.0, dims=("step", "team"))
            return pm.Deterministic(
                name,
                z0[None, :] * sigma0 + pt.cumsum(z * sd[:, None], axis=0),
                dims=("step", "team"),
            )

        attack = walk("attack", sigma_attack)
        defence = walk("defence", sigma_defence)
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
