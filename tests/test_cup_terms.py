"""Stage A, A2: cup terms on the A1 draws (docs/BEYOND_LEAGUES_PLAN.md, A2 design)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl

from adatta.eval import cup_terms as ct
from adatta.eval import harness
from adatta.model import dixon_coles as dc

MAX_GOALS = 10
D = 40


def a1_frame(n: int, seed: int = 1, gamma: float = 0.0, theta: tuple[float, float] = (0, 0),
             neutral_every: int = 10, drop_home_on_neutral: bool = False,
             ) -> pl.DataFrame:  # fmt: skip
    """Synthetic A1 rows: draws around a tie-level rate, scores simulated from the truth with
    a known cup effect (gamma on non-neutral home rates, theta rotation, or no home term on
    neutral ties), stored probabilities as A1 computes them (no effect)."""
    rng = np.random.default_rng(seed)
    t0 = datetime(2019, 8, 5, tzinfo=UTC)  # a Monday
    rows = []
    for i in range(n):
        lam0, mu0 = rng.uniform(0.9, 2.2), rng.uniform(0.6, 1.6)
        lam = lam0 * np.exp(rng.normal(0, 0.05, D))
        mu = mu0 * np.exp(rng.normal(0, 0.05, D))
        rho, kappa, home = np.full(D, -0.05), np.full(D, 0.05), np.full(D, 0.25)
        neu = i % neutral_every == 0
        rnd = "Round of 16" if i % 3 else "Quarter-finals"
        th, ta = (1, 2) if i % 4 == 0 else (2, 1) if i % 4 == 1 else (1, 1)
        gh, ga = (2.0, 6.0) if i % 5 == 0 else (6.0, 2.0) if i % 5 == 1 else (6.0, 6.0)
        e = ct.early(rnd)
        s = (theta[0] * ((e and th < ta) - (e and ta < th))
             + theta[1] * ((gh <= ct.GAP_DAYS) - (ga <= ct.GAP_DAYS)))  # fmt: skip
        true_h = lam0 * np.exp(
            (0.0 if neu else gamma) + s - (0.25 if neu and drop_home_on_neutral else 0)
        )
        true_a = mu0 * np.exp(-s)
        hg, ag = int(rng.poisson(true_h)), int(rng.poisson(true_a))
        m = np.mean([dc.score_matrix(lam[d], mu[d], rho[d], MAX_GOALS, kappa[d])[0]
                     for d in range(D)], 0)  # fmt: skip
        rows.append({
            "match_id": i + 1, "cup_id": 45 if i % 2 else 81, "season": 2019,
            "kickoff": t0 + timedelta(days=3 * i, hours=19), "round": rnd, "neutral": neu,
            "tier_home": th, "tier_away": ta, "gap_home_days": gh, "gap_away_days": ga,
            "hg": hg, "ag": ag, "p_home": float(np.tril(m, -1).sum()),
            "p_draw": float(np.trace(m)), "p_away": float(np.triu(m, 1).sum()),
            "draws_lam": lam.tolist(), "draws_mu": mu.tolist(), "draws_rho": rho.tolist(),
            "draws_kappa": kappa.tolist(), "draws_home_term": home.tolist(),
        })  # fmt: skip
    return pl.DataFrame(rows)


def test_vectorised_matrices_equal_dixon_coles() -> None:
    lam, mu, rho, kappa = np.array([1.3, 0.4]), np.array([0.9, 2.1]), np.array([-0.1, 0.05]), \
        np.array([0.0, 0.2])  # fmt: skip
    m = ct.matrices(lam, mu, rho, kappa, MAX_GOALS)
    for d in range(2):
        ref, _ = dc.score_matrix(lam[d], mu[d], rho[d], MAX_GOALS, kappa[d])
        assert np.allclose(m[d], ref, atol=1e-14)


def test_a1_rescores_to_its_stored_probabilities() -> None:
    rep = ct.evaluate(a1_frame(40), {45: "FA Cup", 81: "DFB Pokal"}, MAX_GOALS, terms=(),
                      log=lambda _: None)  # fmt: skip
    assert rep["a1_rescore_max_diff"] < 1e-12


def test_walk_forward_uses_only_ties_available_by_monday() -> None:
    t = ct.from_a1(a1_frame(30))
    table = ct.loglik_table(ct.TERM["T1"], t, MAX_GOALS)
    params, fits = ct.walk_forward(ct.TERM["T1"], t, table)
    kick = t.frame["kickoff"].to_list()
    for f in fits:
        avail = sum(k + ct.LAG <= f["as_of"] for k in kick)
        assert f["n_train"] == avail
    assert fits[0]["n_train"] == 0 and abs(params[0, 0]) < 1e-12  # no history: the prior's mean


def test_a_real_home_effect_is_recovered_and_passes() -> None:
    pred = a1_frame(900, seed=3, gamma=-0.35)
    rep = ct.evaluate(pred, {45: "FA Cup", 81: "DFB Pokal"}, MAX_GOALS, terms=("T1",),
                      log=lambda _: None)  # fmt: skip
    r = rep["terms"]["T1"]
    mean, sd = r["fit"]["last"]["mean"][0], r["fit"]["last"]["sd"][0]
    assert mean < 0 and abs(mean - (-0.35)) < 3 * sd
    assert r["fit"]["max_edge_mass"] < ct.EDGE_MASS
    assert r["rel"]["1X2:log_loss"]["rel"] < 0 and r["improves"]


def test_rotation_and_neutral_terms_move_only_their_ties() -> None:
    pred = a1_frame(450, seed=5, theta=(-0.3, 0.0), drop_home_on_neutral=True)
    t = ct.from_a1(pred)
    assert set(np.flatnonzero(ct.active("T2", t))) == set(np.flatnonzero(t.frame["neutral"]))
    rep = ct.evaluate(pred, {45: "FA Cup", 81: "DFB Pokal"}, MAX_GOALS, terms=("T2", "T3"),
                      log=lambda _: None)  # fmt: skip
    assert rep["terms"]["T2"]["fit"] == {}  # no parameter to fit
    fit = rep["terms"]["T3"]["fit"]["last"]
    (t_tier, t_gap), (sd_tier, sd_gap) = fit["mean"], fit["sd"]
    assert t_tier < 0 and abs(t_tier - (-0.3)) < 3 * sd_tier and abs(t_gap) < 3 * sd_gap


def test_a_harmful_term_fails_the_gate() -> None:
    a1 = pl.DataFrame({"match_id": [1, 2, 3, 4], "cup_id": [45, 45, 81, 81], "hg": [1, 0, 2, 1],
                       "ag": [0, 0, 1, 1]})  # fmt: skip
    good = {m: [0.5, 0.6, 0.55, 0.7] for m in ct.D28 + ("score:log_loss",)}
    bad = {m: [v * 1.1 for v in good[m]] for m in good}
    probs = {"p_home": [0.5] * 4, "p_draw": [0.3] * 4, "p_away": [0.2] * 4}
    base = a1.with_columns(**{k: pl.Series(v) for k, v in (good | probs).items()})
    cand = a1.with_columns(**{k: pl.Series(v) for k, v in (bad | probs).items()})
    out = ct.compare(base, cand, {45: "FA Cup", 81: "DFB Pokal"})
    assert not out["passes"] and out["worst_segment"]["rel"] > ct.MAX_SEGMENT_WORSE


def test_early_rounds() -> None:
    assert ct.early("Round of 16") and ct.early("1st Round") and ct.early("3rd Round Replays")
    assert not ct.early("Quarter-finals") and not ct.early("Semi-finals")
    assert not ct.early("Final")
    assert harness.week_start(datetime(2019, 8, 7, tzinfo=UTC)) == datetime(2019, 8, 5, tzinfo=UTC)
