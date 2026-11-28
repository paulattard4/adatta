"""P3b-3 (D66): the cards table, prediction, scoring, leakage and the gate. No PyMC (the
model: tests/test_cards_model.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest
from scipy.stats import poisson

from adatta.eval import cards as ec
from adatta.features import cards as fc
from adatta.model import cards as mc

K0 = datetime(2019, 8, 5, 15, tzinfo=UTC)


def _table(weeks: int = 60, seed: int = 0, season_of: Any = None) -> pl.DataFrame:
    """Four teams in league 39 (bundle england), two matches a week, referees R0-R3."""
    rng = np.random.default_rng(seed)
    m, st, ref = [], [], {}
    mid = 0
    for w in range(weeks):
        k = K0 + timedelta(days=7 * w)
        for h, a in ((1, 2), (3, 4)) if w % 2 else ((2, 3), (4, 1)):
            mid += 1
            season = season_of(k) if season_of else 2019 + (w >= 40)
            m.append({"match_id": mid, "league_id": 39, "season": season, "kickoff": k,
                      "home": h, "away": a})  # fmt: skip
            for t in (h, a):
                st.append({"match_id": mid, "team_id": t, "yellow": int(rng.poisson(2.0)),
                           "red": int(rng.random() < 0.05)})  # fmt: skip
            ref[mid] = f"England|R{mid % 4}"
    matches = pl.DataFrame(m, schema_overrides={"kickoff": pl.Datetime("us", "UTC")})
    stats = pl.DataFrame(st)
    ok = {(39, 2019), (39, 2020), (39, 2021)}
    return fc.rows(matches, stats, ref, {39: "england"}, ok)


def test_rows_need_both_teams_and_cards_data() -> None:
    t = _table(4)
    assert t.height == 16 and t["is_home"].sum() == 8
    assert (t["cards"] >= 0).all() and t["referee"].null_count() == 0
    m = pl.DataFrame({"match_id": [1, 2], "league_id": [39, 39], "season": [2019, 2018],
                      "kickoff": [K0, K0], "home": [1, 1], "away": [2, 2]},
                     schema_overrides={"kickoff": pl.Datetime("us", "UTC")})  # fmt: skip
    st = pl.DataFrame({"match_id": [1, 2, 2], "team_id": [1, 1, 2], "yellow": [1, 2, None],
                       "red": [0, None, 0]})  # fmt: skip
    got = fc.rows(m, st, {1: None, 2: "x"}, {39: "england"}, {(39, 2019), (39, 2018)})
    assert got["match_id"].to_list() == [2, 2]  # match 1 has one team's statistics
    assert got.filter(pl.col("team_id") == 2)["cards"].item() == 0
    assert fc.rows(m, st, {}, {39: "england"}, {(39, 2019)}).is_empty()  # 2018: no cards data


def test_training_is_final_by_the_as_of_named_and_within_the_window() -> None:
    t = _table(140, season_of=lambda k: 2019 + (k - K0).days // 365)
    t = t.with_columns(referee=pl.when(pl.col("match_id") == 3).then(None)
                       .otherwise(pl.col("referee")))  # fmt: skip
    for as_of, tgt, train in mc.plan(t, "england", 2021):
        assert (train["kickoff"] + mc.LAG <= as_of).all()
        assert (train["kickoff"] > as_of - timedelta(days=mc.WINDOW_DAYS)).all()
        assert train["referee"].null_count() == 0 and (tgt["season"] == 2021).all()
    # later matches change no refit's training rows (leakage)
    short = {a: tr["match_id"].to_list() for a, _, tr in mc.plan(t, "england", 2020)}
    longer = _table(160, season_of=lambda k: 2019 + (k - K0).days // 365).with_columns(
        referee=pl.when(pl.col("match_id") == 3).then(None).otherwise(pl.col("referee"))
    )
    for a, _, tr in mc.plan(longer, "england", 2020):
        assert tr["match_id"].to_list() == short[a]


def test_holdout_and_window_seasons_are_refused_for_dev_runs() -> None:
    for s in (2024, 2025, 2026):
        with pytest.raises(ValueError):
            mc.run_part(_table(2), "england", s)


def test_team_and_total_distributions() -> None:
    k = np.arange(mc.MAX_TEAM + 1)
    p = mc.cmp_pmf(np.log(np.array([[2.0, 2.5]])), np.ones((1, 2)))
    assert p.shape == (1, 2, mc.MAX_TEAM + 1)
    assert np.allclose(p[0, 0], poisson.pmf(k, 2.0))  # nu = 1 is Poisson
    under = mc.cmp_pmf(np.array([np.log(2.2)]), np.array([1.5]), mc.K_Z)[0]
    m = float(under @ np.arange(mc.K_Z + 1))
    assert (under @ np.arange(mc.K_Z + 1) ** 2 - m * m) / m < 0.8  # nu > 1: under-dispersed
    assert 1 - 1e-6 < p.sum(axis=2).max() <= 1.0
    ph, pa = p[0], mc.cmp_pmf(np.log(np.array([[1.5, 1.8]])), np.ones((1, 2)))[0]
    tot = mc.total_pmf(ph, pa)  # B_clim's independent teams
    brute = np.array([[sum(ph[d, i] * pa[d, k - i] for i in range(21) if 0 <= k - i <= 20)
                       for k in range(41)] for d in range(2)])  # fmt: skip
    assert np.allclose(tot, brute)
    s = mc.score(tot.mean(axis=0), ph.mean(axis=0), pa.mean(axis=0), 2, 1)
    assert 0 < s["p_over_3.5"] < s["p_over_4.5"] + 1 and s["p_over_4.5"] < s["p_over_3.5"]
    assert s["tail"] < 1e-6 and s["rps_total"] > 0
    point = np.zeros(41)
    point[3] = 1.0
    assert mc.score(point, point[:21], point[:21], 2, 1)["rps_total"] == 0.0


def test_the_match_term_links_the_two_teams() -> None:
    """D69 (b): the shared match term, integrated out by quadrature. Without it the teams
    are independent; with it they are positively linked, the joint matches a simulation,
    and the total is the joint's anti-diagonals."""
    eh, ea, nu = np.log(np.full(2, 2.0)), np.log(np.full(2, 1.8)), np.full(2, 1.3)
    joint, ph, pa = mc.match_pmfs(eh, ea, nu, nu, np.array([0.0, 0.35]))
    assert joint.shape == (2, mc.MAX_TEAM + 1, mc.MAX_TEAM + 1)
    mc.coherent(joint, ph, pa)
    assert np.allclose(joint[0], np.outer(ph[0], pa[0]))
    k = np.arange(mc.MAX_TEAM + 1)
    cov = float(k @ joint[1] @ k - (ph[1] @ k) * (pa[1] @ k))
    assert cov > 0.3
    rng = np.random.default_rng(0)
    n = 200_000
    e = rng.normal(0.0, 0.35, n)

    def draw(eta: np.ndarray) -> np.ndarray:
        cdf = np.cumsum(mc.cmp_pmf(eta, np.full(n, 1.3), mc.K_Z), axis=-1)
        return np.asarray((cdf < rng.random(n)[:, None]).sum(axis=-1))

    yh, ya = draw(np.log(2.0) + e), draw(np.log(1.8) + e)
    emp = np.zeros((21, 21))
    np.add.at(emp, (np.minimum(yh, 20), np.minimum(ya, 20)), 1.0)
    assert np.abs(joint[1] - emp / n).max() < 0.003
    tot = mc.joint_total(joint)
    assert tot[1, 3] == pytest.approx(sum(joint[1, i, 3 - i] for i in range(4)))
    with pytest.raises(AssertionError):  # a margin above its team's distribution
        mc.coherent(joint, ph * 0.9, pa)


def _post(nd: int = 400, refs: int = 4, teams: int = 4) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(1)
    return {"c_league": np.full((1, nd), np.log(2.0)), "c_ls": np.full((2, nd), np.log(2.0)),
            "h": np.zeros(nd), "nu": np.ones((1, nd)), "sigma_match": np.full(nd, 0.2),
            "a": np.zeros((teams, nd)), "b": np.zeros((teams, nd)), "sigma_a": np.full(nd, 0.1),
            "sigma_b": np.full(nd, 0.1),
            "r": np.vstack([np.full(nd, 0.3), np.zeros((refs - 1, nd))]),
            "sigma_ref": np.full(nd, 0.3 + 0 * rng.random(nd))}  # fmt: skip


def test_a_known_referee_moves_the_mean_and_the_pool_is_a_draw() -> None:
    ix = mc.Index((39,), ((39, 2019), (39, 2020)), (1, 2, 3, 4),
                  ("England|R0", "England|R1", "England|R2", "England|R3"))  # fmt: skip
    t = pl.DataFrame({"league_id": [39, 39, 39], "season": [2019, 2019, 2021],
                      "team_id": [1, 9, 1], "opp_id": [2, 2, 2], "is_home": [True] * 3,
                      "referee": ["England|R0", "England|R0", None]})  # fmt: skip
    mu = mc.target_mu(_post(), ix, t, np.array([False, True, False]))
    assert mu[0].mean() == pytest.approx(2.0 * np.exp(0.3))  # R0's +0.3, an unseen team: 0
    assert mu[1].std() > 0.1  # T-24h pool: a draw of sigma_ref per posterior draw
    assert mu[2].std() > 0.05  # no referee and a new league-season: both drawn


def test_every_target_gets_climatology_and_each_variant_at_both_horizons() -> None:
    t = _table(8)
    clim = mc.climatology(t)
    assert clim[(39, True)].sum() == pytest.approx(1.0)
    ix = mc.Index((39,), ((39, 2019),), (1, 2, 3, 4),
                  ("England|R0", "England|R1", "England|R2", "England|R3"))  # fmt: skip
    rows = mc.score_refit({"C2": _post()}, ix, t, clim, K0)
    n = t.height // 2
    assert len(rows) == n * (1 + len(mc.HORIZONS))
    assert {r["model"] for r in rows} == {"B_clim", "C2"}


def test_ppc_passes_on_data_from_the_model() -> None:
    """Data drawn from the posterior's own model pass the dispersion PPC most of the time
    (95% bands: about 1 in 10 seeds may fail one of the two)."""
    t = _table(200, seed=3)
    d = mc.design(t)
    post = {"c_league": np.full((1, 400), np.log(2.0)),
            "c_ls": np.full((len(d.index.ls), 400), np.log(2.0)), "h": np.zeros(400),
            "nu": np.ones((1, 400)), "sigma_match": np.full(400, 0.25)}  # fmt: skip
    passed = 0
    for seed in range(10):
        r = np.random.default_rng(100 + seed)  # D69: one match term per match, shared
        y = r.poisson(2.0 * np.exp(r.normal(0.0, 0.25, int(d.match.max()) + 1))[d.match])
        d2 = mc.Design(y=y.astype(np.int64), home=d.home, ls=d.ls, ls_league=d.ls_league,
                       team=d.team, opp=d.opp, ref=d.ref, match=d.match,
                       index=d.index)  # fmt: skip
        passed += mc.ppc(post, d2)["pass"]
    assert passed >= 8
    # D67: under-dispersed team counts don't fail it; only the total's dispersion gates
    under = np.random.default_rng(5).binomial(4, 0.5, len(d.y))
    d4 = mc.Design(y=under.astype(np.int64), home=d.home, ls=d.ls, ls_league=d.ls_league,
                   team=d.team, opp=d.opp, ref=d.ref, match=d.match, index=d.index)  # fmt: skip
    got = mc.ppc(post, d4)
    assert not got["disp_team"]["pass"] and got["pass"] == got["disp_total"]["pass"]
    over = np.random.default_rng(1).negative_binomial(2.0, 0.5, len(d.y))  # var / mean 3
    d3 = mc.Design(y=over.astype(np.int64), home=d.home, ls=d.ls, ls_league=d.ls_league,
                   team=d.team, opp=d.opp, ref=d.ref, match=d.match, index=d.index)  # fmt: skip
    assert not mc.ppc(post, d3)["pass"]


def _pred(gain: dict[str, float], n: int = 600) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    rng = np.random.default_rng(4)
    rows, diags = [], []
    asof = datetime(2020, 1, 6, tzinfo=UTC)
    for i in range(n):
        yh, ya = int(rng.poisson(2)), int(rng.poisson(2))
        base = float(rng.uniform(1.0, 2.0))
        for model, hz in (("B_clim", ""), *((v, h) for v in ec.VARIANTS for h in ("60", "24"))):
            g = gain.get(model, 0.0)
            p = float(rng.uniform(0.3, 0.7))
            rows.append({"match_id": i, "league_id": 39 if i % 2 else 140, "as_of": asof,
                         "model": model, "horizon": hz, "y_home": yh, "y_away": ya,
                         "rps_total": base * (1 - g), "logs_total": 2.0,
                         "ll_over_4.5": 0.69 * (1 - g), "ll_team_1.5": 0.6, "tail": 0.0,
                         **{f"p_over_{x}": p for x in ec.LINES},
                         **{f"p_team_{s}_{x}": p for x in ec.TEAM_LINES
                            for s in ("home", "away")}})  # fmt: skip
    for v in ec.VARIANTS:
        diags.append({"variant": v, "as_of": asof.isoformat(), "ok": True, "nu": 1.0,
                      "ppc": {"corr": {"obs": 0.1, "rep_mean": 0.0},
                              "disp_team": {"q": 0.01}}})  # fmt: skip
    return pl.DataFrame(rows), diags


def test_the_referee_enters_only_if_it_beats_team_only() -> None:
    pred, diags = _pred({"C0": 0.02, "C1": 0.04, "C2": 0.06})
    g = ec.gate(pred, diags)
    assert g["chosen"] == "C2" and g["c3"]["passes"]
    pred, diags = _pred({"C0": 0.02, "C1": 0.04, "C2": 0.04})
    assert ec.gate(pred, diags)["chosen"] == "C1"


def test_a_failing_refit_falls_back_to_the_simpler_variant() -> None:
    pred, diags = _pred({"C0": 0.02, "C1": 0.04, "C2": 0.06})
    diags = [{**g, "ok": g["variant"] != "C2"} for g in diags]
    eff = ec.effective(pred, diags, "C2")
    c1 = pred.filter((pl.col("model") == "C1") & (pl.col("horizon") == "60"))
    assert eff.sort("match_id")["rps_total"].to_list() == c1.sort("match_id")["rps_total"].to_list()
    assert ec.gate(pred, diags)["chosen"] == "C1"
