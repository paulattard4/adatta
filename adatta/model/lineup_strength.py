"""P3-j step 2 (docs/PHASE3_TASKS.md P3-j (c), D65): the production model P with one lineup
offset in the likelihood, built from the signals that passed step 1.

J is P (`team_strength.build`, strength_open with xG, nothing else) with, for each training
match,

    log lambda_home += Delta_home,   log lambda_away += Delta_away,
    Delta_side = sum_k beta_k x S_k(side),   beta_k ~ Normal(0, PRIOR_SD) on scaled signals,

entering every term P builds on the rates (Poisson goals, Dixon-Coles tau, the draw
inflation and the xG Gamma), exactly as P3-d's offset did, and exactly once (principle 7).
S_k is a side's signal at T-60m from its announced XI, divided by its warm-up sd (0 where
absent: rule 8, the term then falls back to P).

The model is written out here, not added to `team_strength.py`, so P's file (part of every
fit hash) is untouched and P's cached weekly dev refits stay valid. A test checks that its
log density equals P3-d's model (`lineup=True`) with the offset Delta and beta_lineup = 1,
up to the two priors, so the copy cannot drift from P.

`build` and `sample` need the model group (D13).
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

from adatta.model import dixon_coles as dc
from adatta.model import team_strength as ts

PRIOR_SD = 0.5  # beta_k on scaled signals (D65 (c))
BETA = "beta_signals"


def build(fd: ts.FitData, s_home: NDArray[np.float64], s_away: NDArray[np.float64],
          prior_scale: float = 1.0, prior_sd: float = PRIOR_SD) -> Any:  # fmt: skip
    """P's model with the lineup offset; s_home / s_away are (training rows, K), in fd's
    row order."""
    import pymc as pm
    import pytensor.tensor as pt

    n = len(fd.hg)
    if s_home.shape != s_away.shape or s_home.ndim != 2 or s_home.shape[0] != n:
        raise ValueError("signals must be (training rows, K) for both sides")
    coords = {"team": fd.team_ids.tolist(), "step": list(range(fd.n_steps)),
              "league": fd.leagues, "signal": list(range(s_home.shape[1]))}  # fmt: skip
    k = prior_scale
    with pm.Model(coords=coords) as model:
        # P's priors (team_strength.build, structure strength_open), unchanged
        mu_league = pm.Normal("mu_league", ts.LOG_BASE_RATE, 0.3 * k, dims="league")
        sigma_a = pm.HalfNormal("sigma_strength", 0.5 * k)
        sigma_b = pm.HalfNormal("sigma_open", 0.5 * k)
        sigma_walk = pm.HalfNormal("sigma_walk", 0.15 * k)
        sigma_home = pm.HalfNormal("sigma_home", 0.3 * k)
        h_league = pm.Normal("h_league", 0.25, 0.2 * k, dims="league")
        h_team = pm.Normal("h_team", 0.0, 1.0, dims="team")
        rho = pm.Uniform("rho", -0.2, 0.2, dims="league")
        kappa = pm.Normal("kappa", 0.0, 0.5 * k, dims="league")
        reset = pt.where(pt.as_tensor(fd.season_start), pt.sqrt(1.0 + ts.RESET_WEEKS), 1.0)
        z0 = pm.ZeroSumNormal("strength_z0", 1.0, dims="team")
        z = pm.ZeroSumNormal("strength_z", 1.0, dims=("step", "team"))
        strength = z0[None, :] * sigma_a + pt.cumsum(z * (sigma_walk * reset)[:, None], axis=0)
        openness = (pm.ZeroSumNormal("open_z0", 1.0, dims="team") * sigma_b)[None, :]
        attack = (strength + openness) / np.sqrt(2.0)
        defence = (strength - openness) / np.sqrt(2.0)
        pm.Deterministic("attack_last", attack[-1], dims="team")
        pm.Deterministic("defence_last", defence[-1], dims="team")
        home = h_league[fd.league] + sigma_home * h_team[fd.home]
        log_lam = (mu_league[fd.league] + home + attack[fd.step, fd.home]
                   - defence[fd.step, fd.away])  # fmt: skip
        log_mu = mu_league[fd.league] + attack[fd.step, fd.away] - defence[fd.step, fd.home]
        # P3-j: the lineup offset, once, before every term that reads the rates
        beta = pm.Normal(BETA, 0.0, prior_sd * k, dims="signal")
        log_lam = log_lam + pt.dot(pt.as_tensor(s_home), beta)
        log_mu = log_mu + pt.dot(pt.as_tensor(s_away), beta)
        lam, mu_ = pt.exp(log_lam), pt.exp(log_mu)
        pm.Poisson("home_goals", lam, observed=fd.hg)
        pm.Poisson("away_goals", mu_, observed=fd.ag)
        ts.xg_likelihood(fd, lam, mu_, k)
        hg, ag = fd.hg, fd.ag
        r = rho[fd.league]
        tau = pt.switch(
            (hg == 0) & (ag == 0), 1 - lam * mu_ * r,
            pt.switch((hg == 0) & (ag == 1), 1 + lam * r,
                      pt.switch((hg == 1) & (ag == 0), 1 + mu_ * r,
                                pt.switch((hg == 1) & (ag == 1), 1 - r, 1.0))),
        )  # fmt: skip
        pm.Potential("dixon_coles", pt.sum(pt.log(pt.maximum(tau, 1e-10))))
        kap = kappa[fd.league]
        p_draw = dc.draw_mass(lam, mu_, r, pt)
        pm.Potential("draw_inflation",
                     pt.sum(kap * (hg == ag) - pt.log1p(pt.expm1(kap) * p_draw)))  # fmt: skip
    return model


def sample(fd: ts.FitData, s_home: NDArray[np.float64], s_away: NDArray[np.float64],
           seed: int, *, draws: int, chains: int, target_accept: float,
           tune: int | None = None) -> Any:  # fmt: skip
    """NUTS via NumPyro with P's settings. No posterior predictive: J's scoring needs only
    the draws (P's cached fits carry their own)."""
    import pymc as pm

    with build(fd, s_home, s_away):
        return pm.sample(draws=draws, tune=draws if tune is None else tune, chains=chains,
                         nuts_sampler="numpyro", random_seed=seed,
                         target_accept=target_accept, progressbar=False)  # fmt: skip


def slim(idata: Any, max_draws: int) -> dict[str, NDArray[np.float64]]:
    """P's prediction draws (`team_strength.slim`) plus beta_signals (K, draws), thinned on
    the same indices."""
    out = ts.slim(idata, max_draws)
    post = ts.stacked(idata)
    n = post.sizes["sample"]
    take = np.linspace(0, n - 1, min(max_draws, n)).astype(int)
    out[BETA] = np.asarray(post[BETA].values[..., take], dtype=np.float64)
    return out
