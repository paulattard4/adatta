"""Phase 1b production team model, v1: Layers 1–2 on 90' goals (spec §5.1–5.3, §8.1).

    attack[s, t]  = attack0[t] + cumsum_s(sd_s * z_attack[s, t]), centred over teams per step
    defence[s, t] likewise
    sd_s          = sigma_walk, or sigma_walk * sqrt(1 + 16) at a season's first step (D21)
                    (the soft reset of §5.3: the prior season's state carries over, inflated)
    log lam_home  = mu_league[l] + h_league[l] + sigma_home * h_team[home]
                    + attack[s, home] - defence[s, away]
    log lam_away  = mu_league[l] + attack[s, away] - defence[s, home]
    + Dixon-Coles tau(rho[l]) on the four low scores, one rho per league (D21)

Priors are §5.1–5.2: mu_league ~ Normal(log 1.35, 0.3); sigma_attack, sigma_defence ~
HalfNormal(0.5); sigma_walk ~ HalfNormal(0.15); sigma_home ~ HalfNormal(0.3). The walk is
non-centred, with zero-sum innovations over teams (identification). Steps are calendar
weeks (refits are weekly, D11). Teams are shared across the bundle's leagues, so promoted
and relegated teams carry their state across (§10.3).

D15 (`structure="strength_open"`): each team is instead a strength walk s and a static
openness o (how high-scoring its games are), with attack = (s + o) / sqrt 2 and defence =
(s - o) / sqrt 2. Strength has its own scale and weekly walk; openness has its own scale and
does not move within the window. A first version with an openness walk too learnt a walk sd
of ~0.005 (vs 0.028 for strength) and mixed poorly at that near-zero scale (R-hat 1.015), so
the walk was dropped. Kept only if it passes the D15 acceptance test.

D16 (`shifts=True`, strength_open only): a team's strength moves by a learned delta_up when
it is promoted and delta_down when relegated, from its first match in the new tier on (the
walk carries the shift forward). Shifts are centred over teams at every step, so the league
levels keep their meaning. A move whose new season has not started inside the training
window is applied at prediction time instead. Prior Normal(0, 0.2).

AR(1) form (`form=True`, strength_open only, §5.2): a per-team weekly zero-sum AR(1),
m_t = phi m_{t-1} + sigma_form e_t from its stationary sd, added to strength (form is about
results, not openness). logit(phi) ~ Normal(logit 0.8, 0.75); sigma_form ~ HalfNormal(0.1).
attack_last / defence_last exclude form; a match h weeks after the last step adds
phi^h m_last / sqrt 2 to each. Kept only if it passes its acceptance test.

xG auxiliary signal (`xg=True`, §5.2): where a training match has xG for both sides (the
has_xg gate, `harness.load_xg`), each side adds xG ~ Gamma(mean = lambda exp(xg_bias[league]),
shape = xg_shape), on the same rate as its goals. xg_shape ~ LogNormal(log 4, 0.5); xg_bias ~
Normal(0, 0.1). Prediction is unchanged, and a window with no xG is the goals model. Passed
its acceptance test on 2026-11-07 and is on in the production RunSpec; `build` and `sample`
keep it off unless asked.

Manager-change widening (`manager=True`, strength_open, §5.2 regime changes, D12): for the
MGR_WINDOW_WEEKS steps from a coach change (from lineups, training matches only), the team's
strength innovation sd is multiplied by sqrt(1 + change_weeks / MGR_WINDOW_WEEKS);
innovations are re-centred over teams so the walk stays zero-sum. change_weeks ~
LogNormal(log 8, 0.75). Prediction reads the last state, so it is unchanged. Kept only if it
passes its acceptance test.

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
XG_FLOOR = 0.01  # a Gamma has no mass at 0
MGR_WINDOW_WEEKS = 6  # steps a coach change widens the walk for (fixed before testing)
RESET_WEEKS = 16.0  # season-start drift, in weeks of walk (fixed, D21)


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
    # D16: cumulative count of a team's promotions / relegations up to each step (step, team)
    moved_up: NDArray[np.float64] | None = None
    moved_down: NDArray[np.float64] | None = None
    # (team id, season) pairs with at least one training match (moves not yet applied)
    team_seasons: frozenset[tuple[int, int]] = frozenset()
    # xG per training match, NaN where absent (None: not loaded)
    xg_home: NDArray[np.float64] | None = None
    xg_away: NDArray[np.float64] | None = None
    # (step, team): 1 inside a coach-change window, else 0 (None: coaches not loaded)
    mgr: NDArray[np.float64] | None = None

    @property
    def xg_rows(self) -> IntArr:
        """Training matches with xG for both sides."""
        if self.xg_home is None or self.xg_away is None:
            return np.zeros(0, dtype=np.int64)
        return np.flatnonzero(np.isfinite(self.xg_home) & np.isfinite(self.xg_away))


def prepare(df: pl.DataFrame, leagues: list[int], as_of: datetime, lookback_days: int,
            lag_hours: float = 3.0) -> FitData:  # fmt: skip
    """Training rows observable at `as_of` (kickoff + lag <= as_of, within the lookback),
    indexed by team, league and weekly step."""
    lo = as_of - timedelta(days=lookback_days)
    hi = as_of - timedelta(hours=lag_hours)
    # D16 status from everything observable at as_of (the season before the window counts)
    from adatta.eval.harness import mover_codes

    hist = df.filter(pl.col("kickoff") <= hi)
    mh, ma = mover_codes(hist, leagues)
    hist = hist.with_columns(mh=pl.Series(mh), ma=pl.Series(ma))
    d = hist.filter(pl.col("kickoff") >= lo).sort("kickoff")
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
    # D16 moves inside the window: a team's first step in a season after a tier change
    moved = {1: np.zeros((n_steps, len(teams))), 2: np.zeros((n_steps, len(teams)))}
    first: dict[tuple[int, int], tuple[int, int]] = {}  # (team, season) -> (step, code)
    for s, season, h, a, ch, ca in zip(step, d["season"], d["home"], d["away"], d["mh"],
                                       d["ma"], strict=True):  # fmt: skip
        for team, c in ((int(h), int(ch)), (int(a), int(ca))):
            if c and (team, int(season)) not in first:
                first[(team, int(season))] = (int(s), c)
    for (team, _), (s0, c) in first.items():
        moved[c][s0:, tix[team]] += 1.0
    seasons_seen = frozenset(
        (int(t), int(se)) for col in ("home", "away")
        for t, se in zip(d[col], d["season"], strict=True)
    )  # fmt: skip
    xg = {c: d[c].cast(pl.Float64).fill_null(np.nan).to_numpy() if c in d.columns else None
          for c in ("xg_home", "xg_away")}  # fmt: skip
    mgr = None
    if "coach_home" in hist.columns:  # D12: changes from every observable lineup up to as_of
        mgr = np.zeros((n_steps, len(teams)))
        for team, kick in coach_changes(hist):
            if team not in tix:
                continue
            s0 = (kick - start).days // 7
            lo_s, hi_s = max(s0, 0), min(s0 + MGR_WINDOW_WEEKS, n_steps)
            if lo_s < hi_s:
                mgr[lo_s:hi_s, tix[team]] = 1.0
    return FitData(
        home=np.array([tix[int(t)] for t in d["home"]], dtype=np.int64),
        away=np.array([tix[int(t)] for t in d["away"]], dtype=np.int64),
        league=np.array([lix[int(x)] for x in d["league_id"]], dtype=np.int64),
        step=step, hg=d["hg"].to_numpy().astype(np.int64), ag=d["ag"].to_numpy().astype(np.int64),
        season_start=season_start, team_ids=teams.astype(np.int64), leagues=leagues,
        start=start, n_steps=n_steps, moved_up=moved[1], moved_down=moved[2],
        team_seasons=seasons_seen, xg_home=xg["xg_home"], xg_away=xg["xg_away"], mgr=mgr,
    )  # fmt: skip


def coach_changes(hist: pl.DataFrame) -> list[tuple[int, datetime]]:
    """(team id, kickoff) of every match whose lineup coach differs from the team's previous
    known coach, in kickoff order. A missing coach carries the previous one; a team's first
    known coach is not a change. `hist` must hold only matches observable at the as-of."""
    sides = pl.concat([
        hist.select("kickoff", team=pl.col(t), coach=pl.col(c))
        for t, c in (("home", "coach_home"), ("away", "coach_away"))
    ]).drop_nulls("coach").sort("team", "kickoff")  # fmt: skip
    sides = sides.with_columns(prev=pl.col("coach").shift(1).over("team"))
    ch = sides.filter(pl.col("prev").is_not_null() & (pl.col("coach") != pl.col("prev")))
    return [(int(t), k) for t, k in zip(ch["team"], ch["kickoff"], strict=True)]


def build(fd: FitData, prior_scale: float = 1.0, structure: str = "independent",
          shifts: bool = False, form: bool = False, xg: bool = False,
          manager: bool = False) -> Any:  # fmt: skip
    """`prior_scale` multiplies every prior scale (the §14.5 prior-sensitivity check uses 0.5
    and 2); 1.0 is the production model. `structure` is "independent" (spec §5.1) or
    "strength_open" (D15)."""
    if structure not in STRUCTURES:
        raise ValueError(f"structure must be one of {STRUCTURES}")
    if shifts and structure != "strength_open":
        raise ValueError("D16 shifts need structure='strength_open' (they move strength)")
    if form and structure != "strength_open":
        raise ValueError("AR(1) form needs structure='strength_open' (it moves strength)")
    if xg and structure != "strength_open":
        raise ValueError("the xG signal needs structure='strength_open'")
    if manager and structure != "strength_open":
        raise ValueError("manager-change widening needs structure='strength_open'")
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
        else:
            sigma_a = pm.HalfNormal("sigma_strength", 0.5 * k)
            sigma_b = pm.HalfNormal("sigma_open", 0.5 * k)
            sigma_walk = pm.HalfNormal("sigma_walk", 0.15 * k)
        # Season reset as a multiple of the weekly step, fixed (D21): with 2–3 season
        # boundaries in a window the data do not identify it (a LogNormal(log 16, 0.5) stayed on
        # its prior and failed the §14.5 prior-sensitivity check), so it is an assumption: a
        # reset worth 16 weeks of drift (a transfer window).
        reset_weeks = RESET_WEEKS
        sigma_home = pm.HalfNormal("sigma_home", 0.3 * k)
        h_league = pm.Normal("h_league", 0.25, 0.2 * k, dims="league")
        h_team = pm.Normal("h_team", 0.0, 1.0, dims="team")
        # one rho per league (D21): second tiers draw more than top flights
        rho = pm.Uniform("rho", -0.2, 0.2, dims="league")

        reset = pt.where(pt.as_tensor(fd.season_start), pt.sqrt(1.0 + reset_weeks), 1.0)

        def walk(name: str, sigma0: Any, step_sd: Any, keep: bool = True,
                 team_scale: Any = None) -> Any:  # fmt: skip
            # Zero-sum over teams at every step: identifies the level (it lives in mu_league)
            # without the redundant directions that re-centring after the fact leaves behind.
            z0 = pm.ZeroSumNormal(f"{name}_z0", 1.0, dims="team")
            z = pm.ZeroSumNormal(f"{name}_z", 1.0, dims=("step", "team"))
            innov = z * (step_sd * reset)[:, None]
            if team_scale is not None:  # per-team widening, re-centred to stay zero-sum
                innov = innov * team_scale
                innov = innov - innov.mean(axis=1, keepdims=True)
            path = z0[None, :] * sigma0 + pt.cumsum(innov, axis=0)
            # Only the final step is stored (all prediction needs): the full path for every
            # draw is ~0.5 GB per fit and can be rebuilt from the innovations if needed.
            if keep:
                pm.Deterministic(f"{name}_last", path[-1], dims="team")
            return path

        if structure == "independent":
            attack = walk("attack", sigma_a, sigma_walk)
            defence = walk("defence", sigma_b, sigma_walk)
        else:
            team_scale = None
            if manager and fd.mgr is not None and fd.mgr.any():
                change_weeks = pm.LogNormal("change_weeks", np.log(8.0), 0.75 * k)
                team_scale = pt.sqrt(1.0 + change_weeks * fd.mgr / MGR_WINDOW_WEEKS)
            strength = walk("strength", sigma_a, sigma_walk, keep=False, team_scale=team_scale)
            if shifts:
                assert fd.moved_up is not None and fd.moved_down is not None
                delta = pm.Normal("delta", 0.0, 0.2 * k, shape=2)  # (up, down)
                off = fd.moved_up * delta[0] + fd.moved_down * delta[1]
                strength = strength + off - off.mean(axis=1, keepdims=True)
            # static: one zero-sum value per team, the same at every step
            openness = (pm.ZeroSumNormal("open_z0", 1.0, dims="team") * sigma_b)[None, :]
            attack = (strength + openness) / np.sqrt(2.0)
            defence = (strength - openness) / np.sqrt(2.0)
            pm.Deterministic("attack_last", attack[-1], dims="team")
            pm.Deterministic("defence_last", defence[-1], dims="team")
            if form:
                m = ar1_form(fd.n_steps, k)
                pm.Deterministic("form_last", m[-1], dims="team")
                attack = attack + m / np.sqrt(2.0)
                defence = defence + m / np.sqrt(2.0)
        home = h_league[fd.league] + sigma_home * h_team[fd.home]
        log_lam = (mu_league[fd.league] + home + attack[fd.step, fd.home]
                   - defence[fd.step, fd.away])  # fmt: skip
        log_mu = mu_league[fd.league] + attack[fd.step, fd.away] - defence[fd.step, fd.home]
        lam, mu_ = pt.exp(log_lam), pt.exp(log_mu)
        pm.Poisson("home_goals", lam, observed=fd.hg)
        pm.Poisson("away_goals", mu_, observed=fd.ag)
        if xg:
            xg_likelihood(fd, lam, mu_, k)
        hg, ag = fd.hg, fd.ag
        rho = rho[fd.league]
        tau = pt.switch(
            (hg == 0) & (ag == 0), 1 - lam * mu_ * rho,
            pt.switch((hg == 0) & (ag == 1), 1 + lam * rho,
                      pt.switch((hg == 1) & (ag == 0), 1 + mu_ * rho,
                                pt.switch((hg == 1) & (ag == 1), 1 - rho, 1.0))),
        )  # fmt: skip
        pm.Potential("dixon_coles", pt.sum(pt.log(pt.maximum(tau, 1e-10))))
    return model


def xg_likelihood(fd: FitData, lam: Any, mu_: Any, k: float = 1.0) -> None:
    """Gamma xG on the goal rates, for the matches with xG; adds nothing if there are none.
    Call inside a model with a "league" dimension."""
    import pymc as pm
    import pytensor.tensor as pt

    rows = fd.xg_rows
    if len(rows) == 0:
        return
    assert fd.xg_home is not None and fd.xg_away is not None
    shape = pm.LogNormal("xg_shape", np.log(4.0), 0.5 * k)
    bias = pt.exp(pm.Normal("xg_bias", 0.0, 0.1 * k, dims="league")[fd.league[rows]])
    pm.Gamma("home_xg", alpha=shape, beta=shape / (lam[rows] * bias),
             observed=np.maximum(fd.xg_home[rows], XG_FLOOR))  # fmt: skip
    pm.Gamma("away_xg", alpha=shape, beta=shape / (mu_[rows] * bias),
             observed=np.maximum(fd.xg_away[rows], XG_FLOOR))  # fmt: skip


def ar1_form(n_steps: int, k: float = 1.0) -> Any:
    """Zero-sum AR(1) form, (step, team), non-centred: m = P @ (sigma_form * e) with
    P[t, j] = phi^(t - j) for j <= t, the first innovation scaled to the stationary sd.
    Call inside a model with a "team" dimension."""
    import pymc as pm
    import pytensor.tensor as pt

    logit_phi = pm.Normal("logit_phi", np.log(0.8 / 0.2), 0.75 * k)
    phi = pm.Deterministic("phi", pm.math.sigmoid(logit_phi))
    sigma_form = pm.HalfNormal("sigma_form", 0.1 * k)
    e = pm.ZeroSumNormal("form_z", 1.0, dims=("step", "team"))
    t = np.arange(n_steps)
    lag = t[:, None] - t[None, :]
    decay = pt.switch(pt.as_tensor(lag >= 0), phi ** np.maximum(lag, 0), 0.0)
    first = np.zeros(n_steps)
    first[0] = 1.0
    scale = sigma_form * (1.0 + first * (1.0 / pt.sqrt(1.0 - phi**2) - 1.0))
    return pt.dot(decay, e * scale[:, None])


def sample(fd: FitData, seed: int, *, prior_scale: float = 1.0, draws: int = 1000,
           chains: int = 4, target_accept: float = 0.95,
           structure: str = "independent", tune: int | None = None,
           shifts: bool = False, form: bool = False, xg: bool = False,
           manager: bool = False) -> Any:  # fmt: skip
    """NUTS via NumPyro (D10), posterior predictive goals included. Returns InferenceData.
    `tune` defaults to `draws`; a longer warm-up improves adaptation without a larger trace."""
    import pymc as pm

    with build(fd, prior_scale, structure, shifts, form, xg, manager):
        idata = pm.sample(
            draws=draws, tune=draws if tune is None else tune, chains=chains,
            nuts_sampler="numpyro", random_seed=seed, target_accept=target_accept,
            progressbar=False,
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


SLIM_VARS = ("attack_last", "defence_last", "mu_league", "h_league", "h_team", "sigma_home",
             "rho")  # fmt: skip


def slim(idata: Any, max_draws: int = 400) -> dict[str, NDArray[np.float64]]:
    """The posterior draws prediction needs, thinned to at most `max_draws` (sample axis last).
    A few MB per fit instead of the ~0.7 GB full trace; used for the backtest cache."""
    post = stacked(idata)
    n = post.sizes["sample"]
    take = np.linspace(0, n - 1, min(max_draws, n)).astype(int)
    # optional: delta (D16), form_last and phi (AR(1) form), xg_shape and xg_bias (xG)
    extra = ("delta", "form_last", "phi", "xg_shape", "xg_bias", "change_weeks")
    names = [v for v in (*SLIM_VARS, *extra) if v in post.data_vars]
    return {v: np.asarray(post[v].values[..., take], dtype=np.float64) for v in names}


def rates_from_draws(d: dict[str, NDArray[np.float64]], fd: FitData, home: IntArr,
                     away: IntArr, league: IntArr, pending_home: IntArr | None = None,
                     pending_away: IntArr | None = None, horizon: IntArr | None = None,
                     ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:  # fmt: skip
    """Posterior draws of (lambda_home, lambda_away) for matches after the training window,
    from the final step's state. home/away index fd.team_ids; -1 = unseen team, which gets
    the average of its league's teams seen in the last 52 weeks (cold start, reported apart).
    pending_home/away (D16): 1 / 2 = the team was promoted / relegated into this match's
    tier and that season has no training match yet, so its shift is applied here.
    horizon (AR(1) form): weeks from the last training step to each match (default 0); form
    adds phi^h m_last / sqrt 2 to attack and defence. Unseen teams have no form.
    Pure numpy: works on the Mac and on cached draws."""
    att, dfn = d["attack_last"], d["defence_last"]  # (team, draw)
    mu_l, h_l = d["mu_league"], d["h_league"]  # (league, draw)
    h_t = d["h_team"] * d["sigma_home"]
    recent = fd.step > fd.step.max() - 52
    members = {
        lg: np.unique(np.concatenate([fd.home[recent & (fd.league == lg)],
                                      fd.away[recent & (fd.league == lg)]]))
        for lg in np.unique(league)
    }  # fmt: skip
    zeros = np.zeros(att.shape[1])

    def team(t: int, lg: int) -> tuple[NDArray[np.float64], ...]:
        if t >= 0:
            return att[t], dfn[t], h_t[t]
        m = members[lg]
        return att[m].mean(0), dfn[m].mean(0), zeros

    n = len(home)
    ph = np.zeros(n, np.int64) if pending_home is None else pending_home
    pa = np.zeros(n, np.int64) if pending_away is None else pending_away
    if "delta" in d:  # a strength shift moves attack and defence by delta / sqrt 2 each
        sh = np.vstack([zeros, d["delta"][0], d["delta"][1]]) / np.sqrt(2.0)
    else:
        sh = np.vstack([zeros, zeros, zeros])
    hz = np.zeros(n, np.int64) if horizon is None else np.maximum(horizon, 0)

    def form(t: int, h: int) -> NDArray[np.float64]:
        if "form_last" not in d or t < 0:
            return zeros
        return np.asarray(d["phi"] ** h * d["form_last"][t] / np.sqrt(2.0))

    out_h, out_a = [], []
    for hh, aa, lg, ch, ca, h in zip(home, away, league, ph, pa, hz, strict=True):
        a_h, d_h, ht = team(int(hh), int(lg))
        a_a, d_a, _ = team(int(aa), int(lg))
        f_h, f_a = form(int(hh), int(h)), form(int(aa), int(h))
        a_h, d_h = a_h + sh[ch] + f_h, d_h + sh[ch] + f_h
        a_a, d_a = a_a + sh[ca] + f_a, d_a + sh[ca] + f_a
        out_h.append(np.exp(mu_l[lg] + h_l[lg] + ht + a_h - d_a))
        out_a.append(np.exp(mu_l[lg] + a_a - d_h))
    return np.array(out_h), np.array(out_a)


def rates(idata: Any, fd: FitData, home: IntArr, away: IntArr, league: IntArr,
          max_draws: int = 400) -> tuple[NDArray[np.float64], NDArray[np.float64]]:  # fmt: skip
    """`rates_from_draws` on a full trace, thinned to `max_draws`."""
    return rates_from_draws(slim(idata, max_draws), fd, home, away, league)
