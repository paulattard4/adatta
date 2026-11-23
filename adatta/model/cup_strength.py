"""Stage A, A3: domestic cup ties as training data for the league model
(docs/BEYOND_LEAGUES_PLAN.md, "A3 acceptance").

A3 is the production model P (`team_strength.build`, strength_open with xG) built on the same
league rows, with the bundle's cup ties added to the goals likelihood. Nothing in P changes:
this module rebuilds the strength path from P's own random variables and adds the cup rows
as extra observations, so `team_strength.py` (part of every fit hash) is untouched and P's
cached refits stay valid.

For a cup row (home side's league l, at weekly step s):

    log lam_home = mu_league[l] + home + attack[s, home] - defence[s, away]
    log lam_away = mu_league[l] + attack[s, away] - defence[s, home]
    home         = h_league[l] + sigma_home * h_team[home] + delta_cup   (not neutral)
                 = 0                                                     (neutral ground)
    + Dixon-Coles tau(rho[l]) and the draw inflation kappa[l], as league rows

delta_cup ~ Normal(0, 0.25), one per bundle (the prior of A2's T1). Cup rows enter the goals
likelihood only, never the xG one, at weight 1.0. A cup row whose side has no league match in
the training window stays out (the side has no strength to attach it to).

The rows are built here without PyMC; `add_cups` and `sample` need the model group (D13).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.model import dixon_coles as dc
from adatta.model import team_strength as ts

PRIOR_SD = 0.25  # delta_cup, as A2's T1 (fixed in the acceptance)


@dataclass(frozen=True)
class CupRows:
    """Cup ties in a refit's training window, indexed like its FitData."""

    match_id: NDArray[np.int64]
    home: NDArray[np.int64]
    away: NDArray[np.int64]
    league: NDArray[np.int64]
    step: NDArray[np.int64]
    hg: NDArray[np.int64]
    ag: NDArray[np.int64]
    neutral: NDArray[np.bool_]

    @property
    def n(self) -> int:
        return len(self.hg)


def cup_rows(fd: ts.FitData, ties: pl.DataFrame, as_of: datetime, lookback_days: int,
             lag_hours: float, holdout_from: int) -> tuple[CupRows, int]:  # fmt: skip
    """The ties (cup_backtest.build_targets rows: sides already swapped for a `swapped`
    venue, league_id the home side's league) observable at `as_of`: kickoff + lag <= as_of,
    within the lookback, exactly as `ts.prepare` takes league rows. Returns the rows and how
    many observable ties were left out because a side has no league row in the window.
    A tie of a holdout season (or later) never enters: it raises, since the caller filters
    those out first."""
    lo = as_of - timedelta(days=lookback_days)
    hi = as_of - timedelta(hours=lag_hours)
    t = ties.filter((pl.col("kickoff") >= lo) & (pl.col("kickoff") <= hi)).sort(
        "kickoff", "match_id"
    )
    if t.height and int(t["season"].max()) >= holdout_from:  # type: ignore[arg-type]
        raise ValueError("a holdout-season cup tie reached a training window")
    tix = {int(x): i for i, x in enumerate(fd.team_ids)}
    lix = {lid: i for i, lid in enumerate(fd.leagues)}
    keep = [int(h) in tix and int(a) in tix for h, a in zip(t["home"], t["away"], strict=True)]
    dropped = len(keep) - sum(keep)
    t = t.filter(pl.Series(keep, dtype=pl.Boolean)) if t.height else t
    days = np.array([(k - fd.start).days for k in t["kickoff"].to_list()], dtype=np.int64)
    step = days // 7
    if len(step) and (step.min() < 0 or step.max() >= fd.n_steps):
        raise ValueError("a cup row falls outside the refit's weekly steps")
    rows = CupRows(
        match_id=t["match_id"].to_numpy().astype(np.int64),
        home=np.array([tix[int(x)] for x in t["home"]], dtype=np.int64),
        away=np.array([tix[int(x)] for x in t["away"]], dtype=np.int64),
        league=np.array([lix[int(x)] for x in t["league_id"]], dtype=np.int64),
        step=step.astype(np.int64),
        hg=t["hg"].to_numpy().astype(np.int64),
        ag=t["ag"].to_numpy().astype(np.int64),
        neutral=(t["role"] == "neutral").to_numpy().astype(bool),
    )  # fmt: skip
    return rows, dropped


def add_cups(model: Any, fd: ts.FitData, cr: CupRows, prior_sd: float = PRIOR_SD) -> Any:
    """Add delta_cup and the cup rows' likelihood to P's model (built by `ts.build` with
    structure strength_open and no shifts, form, manager widening, lineup or covariates).
    Returns the rebuilt (step, team) attack path, so a test can check it against P's."""
    import pymc as pm
    import pytensor.tensor as pt

    v = model.named_vars
    for name in ("strength_z0", "strength_z", "open_z0", "sigma_strength", "sigma_open",
                 "sigma_walk"):  # fmt: skip
        if name not in v:
            raise ValueError(f"A3 adds cups to P (strength_open) only: {name} is missing")
    for name in ("delta", "form_z", "change_weeks", "beta_lineup"):
        if name in v:
            raise ValueError(f"A3 adds cups to P only, not to a model with {name}")
    with model:
        reset = pt.where(pt.as_tensor(fd.season_start), pt.sqrt(1.0 + ts.RESET_WEEKS), 1.0)
        innov = v["strength_z"] * (v["sigma_walk"] * reset)[:, None]
        strength = v["strength_z0"][None, :] * v["sigma_strength"] + pt.cumsum(innov, axis=0)
        openness = (v["open_z0"] * v["sigma_open"])[None, :]
        attack = (strength + openness) / np.sqrt(2.0)
        defence = (strength - openness) / np.sqrt(2.0)
        if cr.n == 0:
            return attack
        delta = pm.Normal("delta_cup", 0.0, prior_sd)
        lg = cr.league
        home_term = v["h_league"][lg] + v["sigma_home"] * v["h_team"][cr.home] + delta
        home = pt.switch(pt.as_tensor(cr.neutral), 0.0, home_term)
        log_lam = (v["mu_league"][lg] + home + attack[cr.step, cr.home]
                   - defence[cr.step, cr.away])  # fmt: skip
        log_mu = v["mu_league"][lg] + attack[cr.step, cr.away] - defence[cr.step, cr.home]
        lam, mu_ = pt.exp(log_lam), pt.exp(log_mu)
        pm.Poisson("cup_home_goals", lam, observed=cr.hg)
        pm.Poisson("cup_away_goals", mu_, observed=cr.ag)
        hg, ag = cr.hg, cr.ag
        rho = v["rho"][lg]
        tau = pt.switch(
            (hg == 0) & (ag == 0), 1 - lam * mu_ * rho,
            pt.switch((hg == 0) & (ag == 1), 1 + lam * rho,
                      pt.switch((hg == 1) & (ag == 0), 1 + mu_ * rho,
                                pt.switch((hg == 1) & (ag == 1), 1 - rho, 1.0))),
        )  # fmt: skip
        pm.Potential("cup_dixon_coles", pt.sum(pt.log(pt.maximum(tau, 1e-10))))
        kap = v["kappa"][lg]
        p_draw = dc.draw_mass(lam, mu_, rho, pt)
        pm.Potential("cup_draw_inflation",
                     pt.sum(kap * (hg == ag) - pt.log1p(pt.expm1(kap) * p_draw)))  # fmt: skip
    return attack


def build(fd: ts.FitData, cr: CupRows, prior_sd: float = PRIOR_SD, xg: bool = True) -> Any:
    """P's model (strength_open, xG as in production) with the cup rows added."""
    model = ts.build(fd, structure="strength_open", xg=xg)
    add_cups(model, fd, cr, prior_sd)
    return model


def sample(fd: ts.FitData, cr: CupRows, seed: int, *, draws: int, chains: int,
           target_accept: float, tune: int | None = None, xg: bool = True,
           prior_sd: float = PRIOR_SD) -> Any:  # fmt: skip
    """NUTS via NumPyro with the same settings as `ts.sample` (posterior predictive
    included, so the timing compares like with like)."""
    import pymc as pm

    with build(fd, cr, prior_sd, xg):
        idata = pm.sample(
            draws=draws, tune=draws if tune is None else tune, chains=chains,
            nuts_sampler="numpyro", random_seed=seed, target_accept=target_accept,
            progressbar=False,
        )  # fmt: skip
        pm.sample_posterior_predictive(idata, extend_inferencedata=True, random_seed=seed,
                                       progressbar=False)  # fmt: skip
    return idata
