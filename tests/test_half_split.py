"""P3b-1, the half split (docs/PHASE3B_TASKS.md): thinning, the half markets and their
coherence (check 1), the conditional score, the grouped likelihood, the share fallbacks and
leakage (check 6). Numpy only; the NUTS fits run in the model container."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest

from adatta.eval import half_split as eh
from adatta.eval import harness
from adatta.eval.selection import HoldoutRefused
from adatta.model import dixon_coles as dc
from adatta.model import half_split as hm
from adatta.sim import half_split as hs
from adatta.sim import serve

G = 16


def ft(lam: float = 1.6, mu: float = 1.1, rho: float = -0.06, kappa: float = 0.12) -> np.ndarray:
    m, _ = dc.score_matrix(lam, mu, rho, G - 1, kappa)
    return m


def brute_joint(m: np.ndarray, ph: float, pa: float) -> np.ndarray:
    j = np.zeros((G, G, G, G))  # x, y, h, a
    for h in range(G):
        for a in range(G):
            for x in range(h + 1):
                for y in range(a + 1):
                    j[x, y, h, a] = (m[h, a] * math.comb(h, x) * ph**x * (1 - ph) ** (h - x)
                                     * math.comb(a, y) * pa**y * (1 - pa) ** (a - y))  # fmt: skip
    return j


def test_thinning_is_the_binomial_pmf() -> None:
    b = hs.thinning(np.array([0.3, 0.6]), G)
    assert np.allclose(b.sum(axis=2), 1.0)
    assert abs(b[0, 4, 2] - math.comb(4, 2) * 0.3**2 * 0.7**2) < 1e-14
    assert b[1, 3, 5] == 0.0  # no more first-half goals than full-time goals
    with pytest.raises(ValueError):
        hs.thinning(np.array([1.0]), G)


def res(u: Any, v: Any) -> Any:
    return np.where(u > v, 0, np.where(u == v, 1, 2))


def test_half_markets_are_the_split_of_the_full_time_matrix() -> None:
    m, ph, pa = ft(), 0.44, 0.43
    mk = hs.markets(m, np.array([ph]), np.array([pa]))
    j = brute_joint(m, ph, pa)
    x, y, h, a = np.indices(j.shape)
    want_ht = [j[res(x, y) == k].sum() for k in range(3)]
    want_2h = [j[res(h - x, a - y) == k].sum() for k in range(3)]
    want_htft = [j[(res(x, y) == i) & (res(h, a) == f)].sum() for i in range(3) for f in range(3)]
    assert np.allclose(mk["ht_1x2"], want_ht, atol=1e-12)
    assert np.allclose(mk["second_1x2"], want_2h, atol=1e-12)
    assert np.allclose(mk["ht_ft"], want_htft, atol=1e-12)
    assert abs(mk["ht_ou"][0] - (1 - j[(x + y) == 0].sum())) < 1e-12
    # every half score sums back to its full-time score: no mass with x > h or y > a
    assert j[(x > h) | (y > a)].sum() == 0.0
    assert np.allclose(j.sum(axis=(0, 1)), m, atol=1e-14)  # the FT matrix is kept, rho too


def test_coherence_holds_and_catches_a_broken_market() -> None:
    m = ft()
    rng = np.random.default_rng(3)
    mk = hs.markets(m, rng.uniform(0.4, 0.47, 50), rng.uniform(0.4, 0.47, 50))
    assert hs.coherence(mk, mk["ft_1x2"]) == []
    bad = dict(mk, ht_ft=mk["ht_ft"] * 1.01)
    assert hs.coherence(bad, mk["ft_1x2"])


def test_conditional_score_integrates_the_share_jointly() -> None:
    ph, pa = np.array([0.4, 0.5]), np.array([0.45, 0.35])
    want = np.mean([math.comb(3, 1) * p**1 * (1 - p) ** 2 * math.comb(2, 2) * q**2
                    for p, q in zip(ph, pa, strict=True)])  # fmt: skip
    assert abs(hs.conditional((1, 2), (3, 2), ph, pa) + math.log(want)) < 1e-12


def obs(seed: int = 1) -> hm.Obs:
    rng = np.random.default_rng(seed)
    k = 400
    n = rng.integers(1, 5, k)
    return hm.Obs(n=n, x=rng.binomial(n, 0.44), side=rng.integers(0, 2, k),
                  league=rng.integers(0, 3, k), team=rng.integers(0, 12, k),
                  match=np.arange(k) // 2, league_ids=[39, 40, 61],
                  team_ids=list(range(100, 112)), n_matches=k // 2)  # fmt: skip


def test_grouped_likelihood_equals_the_per_side_one_up_to_a_constant() -> None:
    o = obs()
    g = hm.grouped(o)
    assert g["n"].sum() == o.n.sum() and g["x"].sum() == o.x.sum()

    def ll_sides(p: np.ndarray) -> float:
        q = p[o.side, o.league, o.team]
        return float((o.x * np.log(q) + (o.n - o.x) * np.log1p(-q)).sum())

    def ll_groups(p: np.ndarray) -> float:
        q = p[g["side"], g["league"], g["team"]]
        return float((g["x"] * np.log(q) + (g["n"] - g["x"]) * np.log1p(-q)).sum())

    rng = np.random.default_rng(2)
    p1, p2 = rng.uniform(0.3, 0.6, (2, 3, 12)), rng.uniform(0.3, 0.6, (2, 3, 12))
    # the binomial coefficients are the constant; the p-dependent part is identical
    assert abs((ll_sides(p1) - ll_groups(p1)) - (ll_sides(p2) - ll_groups(p2))) < 1e-9
    assert abs(ll_sides(p1) - ll_groups(p1)) < 1e-9


def test_share_of_an_unseen_team_is_the_pool_mean() -> None:
    post = {"beta": np.array([[0.0, -0.1]]), "alpha": np.array([[0.1, -0.1]]),
            "u": np.array([[0.3]])}  # fmt: skip
    seen = hm.share(post, [39, 40], [7], 0, 39, 7)
    unseen = hm.share(post, [39, 40], [7], 1, 40, 999)
    assert abs(seen[0] - hm.expit(np.array(0.4))) < 1e-12
    assert abs(unseen[0] - hm.expit(np.array(-0.2))) < 1e-12


def test_ppc_reports_dispersion_and_the_0_0_check() -> None:
    o = obs()
    d = 50
    post = {"beta": np.full((d, 2), math.log(0.44 / 0.56)), "alpha": np.zeros((d, 3)),
            "u": np.zeros((d, 12))}  # fmt: skip
    q = hm.ppc(post, o, np.random.default_rng(0))
    assert {"disp_1", "disp_2", "disp_3", "disp_4", "ht00"} <= set(q)
    assert all(0.0 <= v <= 1.0 for v in q.values())


# ------------------------------------------------------------------- plan, leakage, fallback


def frame(extra: bool = False) -> pl.DataFrame:
    rng = np.random.default_rng(5)
    t0 = datetime(2018, 8, 4, 15, tzinfo=UTC)
    rows = []
    for i in range(600):
        k = t0 + timedelta(days=int(i // 4) * 3)
        hg, ag = int(rng.poisson(1.5)), int(rng.poisson(1.1))
        rows.append({"match_id": i + 1, "league_id": [39, 40][i % 2],
                     "season": 2018 if k < datetime(2019, 7, 1, tzinfo=UTC) else 2019,
                     "kickoff": k, "home": 1 + i % 10, "away": 11 + i % 10, "hg": hg, "ag": ag,
                     "xh": int(rng.binomial(hg, 0.44)), "xa": int(rng.binomial(ag, 0.44)),
                     "ht_ok": True})  # fmt: skip
    df = pl.DataFrame(rows)
    if extra:  # a match added long after: changes no earlier refit
        df = pl.concat(
            [
                df,
                df.tail(1).with_columns(
                    match_id=pl.lit(10_000, pl.Int64), kickoff=pl.col("kickoff") + timedelta(300)
                ),
            ]
        )
    return df.sort("kickoff", "match_id")


def test_share_plan_is_dev_only_and_leak_free() -> None:
    cfg = harness.load_config()
    df = frame()
    with pytest.raises(HoldoutRefused):
        hm.share_plan(df, cfg, [2024])
    plan = hm.share_plan(df, cfg, [2019])
    assert plan
    lag = timedelta(hours=cfg.result_available_after_hours)
    for as_of, _, train in plan:
        assert (df[train]["kickoff"] + lag <= as_of).all()
    later = hm.share_plan(frame(extra=True), cfg, [2019])
    for (a, _, tr), (b, _, tr2) in zip(plan, later, strict=False):
        assert a == b and np.array_equal(tr, tr2)
        o1, o2 = hm.observations(df[tr], [39, 40]), hm.observations(frame(True)[tr2], [39, 40])
        assert np.array_equal(o1.x, o2.x) and np.array_equal(o1.n, o2.n)
        assert np.allclose(eh.climatology(df[tr], 39)[1], eh.climatology(frame(True)[tr2], 39)[1])


def test_climatology_counts_the_training_matches() -> None:
    df = pl.DataFrame({"league_id": [39, 39, 39, 40], "xh": [1, 0, 0, 2], "xa": [0, 0, 1, 0],
                       "hg": [2, 1, 1, 2], "ag": [0, 1, 1, 0]})  # fmt: skip
    ht, htft = eh.climatology(df, 39)
    assert np.allclose(ht, np.array([1.5, 1.5, 1.5]) / 4.5)
    want = np.full(9, 0.5)
    want[hs.htft_index((1, 0), (2, 0))] += 1
    want[hs.htft_index((0, 0), (1, 1))] += 1
    want[hs.htft_index((0, 1), (1, 1))] += 1
    assert np.allclose(htft, want / want.sum())


def test_a_failing_refit_falls_back_to_the_next_simpler_variant() -> None:
    sh = object.__new__(eh.Shares)
    d1, d2 = datetime(2019, 7, 22, tzinfo=UTC), datetime(2019, 8, 19, tzinfo=UTC)
    sh.dates = [d1, d2]
    sh.gates = {("S0", d1): {"passes": True}, ("S1", d1): {"passes": False},
                ("S2", d1): {"passes": False}, ("S0", d2): {"passes": False},
                ("S1", d2): {"passes": False}, ("S2", d2): {"passes": False}}  # fmt: skip
    assert sh.served("S2", d1) == "S0" and sh.served("S0", d1) == "S0"
    assert sh.served("S2", d2) is None
    assert sh.date_for(d2 + timedelta(days=3)) == d2
    with pytest.raises(ValueError):
        sh.date_for(d1 - timedelta(days=1))


def test_vectorised_matrices_equal_the_per_draw_loop() -> None:
    rng = np.random.default_rng(8)
    lam, mu = rng.uniform(0.5, 2.5, 30), rng.uniform(0.4, 2.0, 30)
    rho, kappa = rng.uniform(-0.15, 0.1, 30), rng.uniform(0, 0.3, 30)
    v = serve.score_matrices(lam, mu, rho, kappa, G - 1)
    for d in range(30):
        m, _ = dc.score_matrix(lam[d], mu[d], rho[d], G - 1, kappa[d])
        assert np.abs(v[d] - m).max() < 1e-12


def test_simplification_check_terms() -> None:
    m, ph, pa = ft(), 0.44, 0.43
    lvl, second = eh.level_2h(m, ph, pa)
    j = brute_joint(m, ph, pa)
    x, y, h, a = np.indices(j.shape)
    s = x == y
    assert abs(lvl - j[s].sum()) < 1e-12
    assert abs(second - (j * (h + a - x - y))[s].sum()) < 1e-10


def test_refits_are_regated_with_both_ppcs_reported_only() -> None:
    conv = [{"name": "rhat", "passed": True}, {"name": "divergences", "passed": True}]
    stored = {"convergence": conv, "sensitivity_max_move": {"0.5": 0.001, "2.0": 0.002},
              "ppc": {"disp_3": 0.0, "ht00": 0.0}, "passes": False, "failed": ["ppc"]}  # fmt: skip
    g = hm.regate(stored)
    assert g["passes"] and g["failed"] == []
    assert g["passes_as_run"] is False and g["failed_as_run"] == ["ppc"]
    assert hm.regate(g)["passes_as_run"] is False  # re-gating twice keeps the verdict as run
    bad = hm.regate({**stored, "sensitivity_max_move": {"0.5": 0.01}})
    assert not bad["passes"] and bad["failed"] == ["sensitivity"]
    conv_bad = [{"name": "rhat", "passed": False}]
    assert hm.regate({**stored, "convergence": conv_bad})["failed"] == ["convergence"]
