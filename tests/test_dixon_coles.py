"""Phase 1a model correctness: gradient, parameter recovery, score matrix."""

from typing import Any

import numpy as np
from scipy.optimize import approx_fprime

from adatta.eval import metrics
from adatta.model import dixon_coles as dc


def simulate(
    seed: int = 3, n_teams: int = 16, rounds: int = 6
) -> tuple[dc.MatchData, dict[str, Any]]:
    rng = np.random.default_rng(seed)
    attack = rng.normal(0, 0.3, n_teams)
    attack -= attack.mean()
    defence = rng.normal(0, 0.3, n_teams)
    defence -= defence.mean()
    league = (np.arange(n_teams) >= n_teams // 2).astype(np.int64)  # two leagues of teams
    truth: dict[str, Any] = {
        "mu": 0.15, "home": np.array([0.3, 0.2]), "mu_league": np.array([0.0, -0.1]),
        "attack": attack, "defence": defence,
    }  # fmt: skip
    pairs = [
        (i, j, int(league[i]))
        for _ in range(rounds)
        for i in range(n_teams)
        for j in range(n_teams)
        if i != j and league[i] == league[j]
    ]
    # a few cross-league matches tie the leagues together (as promotion does over seasons)
    pairs += [(i, n_teams - 1 - i, 0) for i in range(n_teams // 2)]
    home, away, lgs = (np.array(c, dtype=np.int64) for c in zip(*pairs, strict=True))
    base = truth["mu"] + truth["mu_league"][lgs]
    lam = np.exp(base + truth["home"][lgs] + attack[home] - defence[away])
    mu_ = np.exp(base + attack[away] - defence[home])
    data = dc.MatchData(
        home=home, away=away, league=lgs, hg=np.asarray(rng.poisson(lam), dtype=np.int64),
        ag=np.asarray(rng.poisson(mu_), dtype=np.int64),
        weight=np.ones(len(home)), n_teams=n_teams, n_leagues=2,
    )  # fmt: skip
    return data, truth


def test_gradient_matches_finite_differences() -> None:
    d, _ = simulate(rounds=1)
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.1, dc._layout(d.n_teams, d.n_leagues)["rho"].stop)
    x[-1] = 0.05
    # make the four low scores frequent enough that tau terms are exercised
    _, g = dc.neg_loglik(x, d)

    def f(v: dc.Arr) -> float:
        return dc.neg_loglik(v, d)[0]

    num = approx_fprime(x, f, 1e-6)
    assert np.allclose(g, num, rtol=1e-4, atol=1e-3)


def test_fit_recovers_simulated_parameters() -> None:
    d, truth = simulate()
    p, _ = dc.fit(d)
    assert np.corrcoef(p.attack, truth["attack"])[0, 1] > 0.9
    assert np.corrcoef(p.defence, truth["defence"])[0, 1] > 0.9
    assert np.allclose(p.home_league, truth["home"], atol=0.1)
    assert abs(p.rho) < dc.RHO_BOUND


def test_score_matrix_is_a_distribution_and_markets_are_consistent() -> None:
    m, tail = dc.score_matrix(1.6, 1.1, -0.08, 10)
    assert abs(m.sum() - 1) < 1e-12 and 0 <= tail < 1e-4 and (m >= 0).all()
    mk = metrics.markets(m)
    assert abs(mk["1X2"].sum() - 1) < 1e-12 and abs(mk["total"].sum() - 1) < 1e-12
    assert mk["1X2"][0] > mk["1X2"][2]  # stronger home rate


def test_scoring_rules_on_known_values() -> None:
    p = np.array([0.5, 0.3, 0.2])
    assert np.isclose(metrics.brier(p, 0), 0.25 + 0.09 + 0.04)
    assert np.isclose(metrics.log_loss(p, 2), -np.log(0.2))
    # RPS: cumulative (0.5, 0.8) vs (0, 0) for an away win -> (0.25 + 0.64) / 2
    assert np.isclose(metrics.rps(p, 2), 0.445)
    assert metrics.rps(np.array([1.0, 0, 0]), 0) == 0
    assert np.isclose(metrics.crps_discrete(np.array([0, 1.0, 0]), 1), 0)


def test_team_prior_bounds_a_team_with_one_goalless_result() -> None:
    d, _ = simulate()
    # a newcomer (index n_teams) whose only result is a 0-0
    d2 = dc.MatchData(
        home=np.append(d.home, d.n_teams), away=np.append(d.away, 0), league=np.append(d.league, 0),
        hg=np.append(d.hg, 0), ag=np.append(d.ag, 0), weight=np.append(d.weight, 1.0),
        n_teams=d.n_teams + 1, n_leagues=2,
    )  # fmt: skip
    loose, _ = dc.fit(d2)
    shrunk, _ = dc.fit(dc.MatchData(**{**d2.__dict__, "ridge": dc.ridge_for(0.35)}))
    assert abs(loose.attack[-1]) > 2  # unbounded without a prior: near-certain 0 goals
    assert abs(shrunk.attack[-1]) < 0.5


def test_openness_penalty_gradient_and_shrinkage() -> None:
    d, _ = simulate(rounds=2)
    d = dc.MatchData(**{**d.__dict__, "open_ridge": 5.0})
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.1, dc._layout(d.n_teams, d.n_leagues)["rho"].stop)
    _, g = dc.neg_loglik(x, d)

    def f(v: dc.Arr) -> float:
        return dc.neg_loglik(v, d)[0]

    num = approx_fprime(x, f, 1e-6)
    assert np.allclose(g, num, rtol=1e-4, atol=1e-3)
    free, _ = dc.fit(dc.MatchData(**{**d.__dict__, "open_ridge": 0.0}))
    shrunk, _ = dc.fit(d)
    assert np.std(shrunk.attack - shrunk.defence) < np.std(free.attack - free.defence)
