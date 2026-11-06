"""Phase 1a benchmark: static, non-hierarchical Dixon-Coles (spec §19.4, §14.4).

A point-estimate (maximum weighted likelihood) model on normal-time goals only. It is the
permanent comparison floor and the one model exempt from "everything is a posterior" (§1.2).

    log lambda_home = mu + mu_L[league] + home_L[league] + attack[home] - defence[away]
    log lambda_away = mu + mu_L[league]                  + attack[away] - defence[home]

Teams are shared across a bundle's leagues, so promoted and relegated teams carry their
strength across (§19.1); mu_L of the first league (the top flight) is the reference, 0.
The Dixon-Coles tau corrects the four low scores with one rho per fit. Matches are weighted
by exp(-ln2 * age / half_life). Sum-to-zero penalties on attack and defence fix the
location. An L2 penalty ridge * (attack^2 + defence^2), i.e. a fixed Normal(0, sd) prior with
ridge = 1 / (2 sd^2), stops a team with one or two results (e.g. two 0-0 draws after
promotion) from getting an unbounded rating; sd is tuned on the dev window. It is a fixed
regulariser, not a learned hierarchy, so the model stays non-hierarchical.
An optional extra penalty open_ridge * (attack - defence)^2 shrinks each team's openness
(how high-scoring its games are) separately from its strength (D15 probe; 0 = off).
D16 probe: in a team's first season after changing tier its strength moves by a learned
shift, delta_up (promoted) or delta_down (relegated), split evenly between attack and
defence; with no movers flagged the shifts stay at 0 and the fit is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import minimize
from scipy.stats import poisson

Arr = NDArray[np.float64]
IntArr = NDArray[np.int64]

RHO_BOUND = 0.3
SUM_PENALTY = 10.0  # weight on (sum attack)^2 and (sum defence)^2
RIDGE = 1e-3  # default L2 on attack/defence: numerical stability only (no prior)
TAU_FLOOR = 1e-10


@dataclass(frozen=True)
class MatchData:
    home: IntArr  # team index
    away: IntArr
    league: IntArr  # 0 = reference (top flight)
    hg: IntArr  # normal-time goals
    ag: IntArr
    weight: Arr
    n_teams: int
    n_leagues: int
    ridge: float = RIDGE
    open_ridge: float = 0.0  # L2 on openness = attack - defence (D15); 0 = off
    # D16: per match, 0 = no tier change, 1 = team's first season after promotion,
    # 2 = after relegation. None = off.
    mover_home: IntArr | None = None
    mover_away: IntArr | None = None
    shift_ridge: float = 12.5  # Normal(0, 0.2) prior on delta_up / delta_down

    @property
    def shifts(self) -> bool:
        return self.mover_home is not None and self.mover_away is not None


def ridge_for(prior_sd: float | None) -> float:
    """L2 weight equivalent to a Normal(0, prior_sd) prior on attack and defence."""
    return RIDGE if prior_sd is None else 1.0 / (2.0 * prior_sd**2)


@dataclass(frozen=True)
class Params:
    mu: float
    mu_league: Arr  # [n_leagues], index 0 is 0
    home_league: Arr  # [n_leagues]
    attack: Arr  # [n_teams]
    defence: Arr  # [n_teams]
    rho: float
    shift: Arr = field(default_factory=lambda: np.zeros(2))  # D16 (delta_up, delta_down)

    def offset(self, mover_home: IntArr | None, mover_away: IntArr | None) -> Arr | float:
        """D16 log-rate offset for the home side (the away side gets its negative)."""
        if mover_home is None or mover_away is None:
            return 0.0
        s = np.concatenate([[0.0], self.shift])
        return np.asarray((s[mover_home] - s[mover_away]) / 2.0)

    def rates(self, home: IntArr, away: IntArr, league: IntArr,
              mover_home: IntArr | None = None, mover_away: IntArr | None = None
              ) -> tuple[Arr, Arr]:  # fmt: skip
        base = self.mu + self.mu_league[league]
        off = self.offset(mover_home, mover_away)
        lam = np.exp(base + self.home_league[league] + self.attack[home] - self.defence[away]
                     + off)  # fmt: skip
        mu_ = np.exp(base + self.attack[away] - self.defence[home] - off)
        return lam, mu_


def _layout(n_teams: int, n_leagues: int, shifts: bool = False) -> dict[str, slice]:
    """Parameter vector layout; the two D16 shifts exist only when movers are flagged, so
    the benchmark's optimisation (and its predictions) are exactly as before otherwise."""
    i = 0
    out = {}
    for name, size in (
        ("mu", 1),
        ("mu_league", n_leagues - 1),
        ("home_league", n_leagues),
        ("attack", n_teams),
        ("defence", n_teams),
        ("shift", 2 if shifts else 0),
        ("rho", 1),
    ):
        out[name] = slice(i, i + size)
        i += size
    return out


def unpack(x: Arr, n_teams: int, n_leagues: int, shifts: bool = False) -> Params:
    s = _layout(n_teams, n_leagues, shifts)
    return Params(
        mu=float(x[s["mu"]][0]),
        mu_league=np.concatenate([[0.0], x[s["mu_league"]]]),
        home_league=x[s["home_league"]].copy(),
        attack=x[s["attack"]].copy(),
        defence=x[s["defence"]].copy(),
        rho=float(x[s["rho"]][0]),
        shift=x[s["shift"]].copy() if shifts else np.zeros(2),
    )


def _initial(d: MatchData) -> Arr:
    s = _layout(d.n_teams, d.n_leagues, d.shifts)
    x = np.zeros(s["rho"].stop)
    w = d.weight / d.weight.sum()
    x[s["mu"]] = np.log(max(float((w * d.ag).sum()), 0.1))
    x[s["home_league"]] = np.log(max(float((w * d.hg).sum()), 0.1)) - x[s["mu"]]
    return x


def _tau_terms(hg: IntArr, ag: IntArr, lam: Arr, mu_: Arr, rho: float) -> tuple[Arr, ...]:
    """log tau and its partial derivatives w.r.t. lambda, mu and rho, per match."""
    tau = np.ones_like(lam)
    dl = np.zeros_like(lam)
    dm = np.zeros_like(lam)
    dr = np.zeros_like(lam)
    m00 = (hg == 0) & (ag == 0)
    m01 = (hg == 0) & (ag == 1)
    m10 = (hg == 1) & (ag == 0)
    m11 = (hg == 1) & (ag == 1)
    tau[m00] = 1 - lam[m00] * mu_[m00] * rho
    tau[m01] = 1 + lam[m01] * rho
    tau[m10] = 1 + mu_[m10] * rho
    tau[m11] = 1 - rho
    tau = np.maximum(tau, TAU_FLOOR)
    dl[m00], dm[m00], dr[m00] = -mu_[m00] * rho, -lam[m00] * rho, -lam[m00] * mu_[m00]
    dl[m01], dr[m01] = rho, lam[m01]
    dm[m10], dr[m10] = rho, mu_[m10]
    dr[m11] = -1.0
    return np.log(tau), dl / tau, dm / tau, dr / tau


def neg_loglik(x: Arr, d: MatchData) -> tuple[float, Arr]:
    """Weighted negative log-likelihood (+ penalties) and its analytic gradient."""
    p = unpack(x, d.n_teams, d.n_leagues, d.shifts)
    lam, mu_ = p.rates(d.home, d.away, d.league, d.mover_home, d.mover_away)
    log_tau, dl, dm, dr = _tau_terms(d.hg, d.ag, lam, mu_, p.rho)
    w = d.weight
    ll = w * (log_tau + d.hg * np.log(lam) - lam + d.ag * np.log(mu_) - mu_)
    # d ll / d log(lambda) and d log(mu)
    g_lam = w * (d.hg - lam + lam * dl)
    g_mu = w * (d.ag - mu_ + mu_ * dm)

    s = _layout(d.n_teams, d.n_leagues, d.shifts)
    g = np.zeros_like(x)
    g[s["mu"]] = g_lam.sum() + g_mu.sum()
    by_league = np.bincount(d.league, g_lam + g_mu, d.n_leagues)
    g[s["mu_league"]] = by_league[1:]
    g[s["home_league"]] = np.bincount(d.league, g_lam, d.n_leagues)
    g[s["attack"]] = np.bincount(d.home, g_lam, d.n_teams) + np.bincount(d.away, g_mu, d.n_teams)
    g[s["defence"]] = -np.bincount(d.away, g_lam, d.n_teams) - np.bincount(d.home, g_mu, d.n_teams)
    g[s["rho"]] = (w * dr).sum()
    if d.mover_home is not None and d.mover_away is not None:  # d.shifts, narrowed for mypy
        g_off = (g_lam - g_mu) / 2.0  # d ll / d delta, via offset = (s_home - s_away) / 2
        for k in (1, 2):
            g[s["shift"].start + k - 1] = (
                g_off[d.mover_home == k].sum() - g_off[d.mover_away == k].sum()
            )

    total = float(ll.sum())
    a, dd = x[s["attack"]], x[s["defence"]]
    o = a - dd
    pen = (SUM_PENALTY * (a.sum() ** 2 + dd.sum() ** 2) + d.ridge * ((a**2).sum() + (dd**2).sum())
           + d.open_ridge * (o**2).sum())  # fmt: skip
    g_pen = np.zeros_like(x)
    g_pen[s["attack"]] = 2 * SUM_PENALTY * a.sum() + 2 * d.ridge * a + 2 * d.open_ridge * o
    g_pen[s["defence"]] = 2 * SUM_PENALTY * dd.sum() + 2 * d.ridge * dd - 2 * d.open_ridge * o
    sh = x[s["shift"]]
    pen += d.shift_ridge * float((sh**2).sum())
    g_pen[s["shift"]] = 2 * d.shift_ridge * sh
    return -total + pen, -g + g_pen


def fit(d: MatchData, init: Arr | None = None) -> tuple[Params, Arr]:
    """Maximum weighted likelihood. Returns params and the raw vector (to warm-start)."""
    x0 = (
        _initial(d)
        if init is None or init.shape[0] != _layout(d.n_teams, d.n_leagues, d.shifts)["rho"].stop
        else init
    )
    bounds = [(None, None)] * (x0.shape[0] - 1) + [(-RHO_BOUND, RHO_BOUND)]
    res = minimize(
        neg_loglik, x0, args=(d,), jac=True, method="L-BFGS-B", bounds=bounds,
        options={"maxiter": 2000, "gtol": 1e-6},
    )  # fmt: skip
    if not res.success and "ABNORMAL" not in str(res.message):
        raise RuntimeError(f"Dixon-Coles fit failed: {res.message}")
    return unpack(res.x, d.n_teams, d.n_leagues, d.shifts), res.x


def score_matrix(lam: float, mu_: float, rho: float, max_goals: int) -> tuple[Arr, float]:
    """P(home = i, away = j) for i, j <= max_goals, renormalised; returns (matrix, tail mass)."""
    k = np.arange(max_goals + 1)
    m = np.outer(poisson.pmf(k, lam), poisson.pmf(k, mu_))
    m[0, 0] *= 1 - lam * mu_ * rho
    m[0, 1] *= 1 + lam * rho
    m[1, 0] *= 1 + mu_ * rho
    m[1, 1] *= 1 - rho
    m = np.maximum(m, 0.0)
    tail = float(1.0 - m.sum())
    return m / m.sum(), tail


def independent_matrix(lam: float, mu_: float, max_goals: int) -> tuple[Arr, float]:
    return score_matrix(lam, mu_, 0.0, max_goals)
