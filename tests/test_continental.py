"""Stage C, C1 (docs/BEYOND_LEAGUES_PLAN.md): side strengths from a refit, the Elo baseline
as of each Monday, the ordered logit, prediction and the scores."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import numpy as np
import polars as pl
import pytest

from adatta.model import continental as ct

K = datetime(2020, 10, 21, 20, tzinfo=UTC)


def _hist(rows: list[tuple[int, int, int, int, str | None, int]]) -> pl.DataFrame:
    """(home, away, hg, ag, league, days after K)."""
    return pl.DataFrame(
        [{"match_id": i, "kickoff": K + timedelta(days=d), "home": h, "away": a, "league": lg,
          "hg": hg, "ag": ag} for i, (h, a, hg, ag, lg, d) in enumerate(rows)],
        schema={"match_id": pl.Int64, "kickoff": pl.Datetime("us", "UTC"), "home": pl.Int64,
                "away": pl.Int64, "league": pl.Utf8, "hg": pl.Int64, "ag": pl.Int64},
    )  # fmt: skip


def test_elo_is_as_of_the_monday_with_home_advantage_and_league_starts() -> None:
    hist = _hist([(1, 2, 2, 0, "39", 0), (3, 4, 0, 0, "39", 1), (1, 3, 1, 0, "39", 30)])
    mon = ct.monday(K + timedelta(days=7))
    got = ct.elo_at(hist, [(100, mon, 1, 2), (101, mon, 2, 1)], neutral={101})
    eh = 1 / (1 + 10 ** (-ct.ELO_HOME / 400))
    up = ct.ELO_K * (1 - eh)
    assert got[100] == pytest.approx(2 * up + ct.ELO_HOME)
    assert got[101] == pytest.approx(-2 * up)  # neutral: no home advantage
    # the match 30 days later is not final by that Monday; a new club starts at its league's mean
    late = ct.elo_at(_hist([(1, 2, 2, 0, "39", 0), (5, 1, 0, 0, "39", 2)]),
                     [(102, ct.monday(K + timedelta(days=7)), 5, 2)])  # fmt: skip
    assert late[102] != pytest.approx(ct.ELO_HOME)  # 5 started at the league mean, not 1,500


def test_ordered_logit_recovers_its_parameters() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(0, 1.5, 6000)
    p = ct.logit_probs(x, 0.8, -0.5, 0.6)
    y = np.array([rng.choice(3, p=q) for q in p])
    b, c1, c2 = ct.ordered_logit(x, y)
    assert b == pytest.approx(0.8, abs=0.08) and c1 == pytest.approx(-0.5, abs=0.08)
    assert c2 == pytest.approx(0.6, abs=0.08)


def test_side_strength_reads_the_refits_last_step_and_cold_teams() -> None:
    fd = SimpleNamespace(team_ids=np.array([10, 11, 12]), leagues=[39, 40],
                         step=np.array([0, 60]), league=np.array([0, 0]),
                         home=np.array([0, 1]), away=np.array([1, 0]))  # fmt: skip
    draws = {"attack_last": np.array([[0.1, 0.3], [0.5, 0.7], [0.0, 0.0]]),
             "defence_last": np.zeros((3, 2)), "rho": np.array([[-0.1, -0.1], [0.0, 0.0]]),
             "kappa": np.array([[0.05, 0.05], [0.0, 0.0]])}  # fmt: skip
    idx = np.array([0, 1])
    s = ct.side_strength(draws, fd, 10, 39, idx)
    assert s["att_mean"] == pytest.approx(0.2) and not s["cold"] and s["rho"] == [-0.1, -0.1]
    cold = ct.side_strength(draws, fd, 99, 39, idx)
    assert cold["cold"] and cold["att_mean"] == pytest.approx(0.4)  # league 39's recent teams


def test_predict_uses_q_and_neutral_ground() -> None:
    row = {"bundle_home": "england", "bundle_away": "france", "neutral": False,
           "att_draws_home": [0.0], "def_draws_home": [0.0], "att_draws_away": [0.0],
           "def_draws_away": [0.0], "rho_home": [0.0], "kappa_home": [0.0]}  # fmt: skip
    tg = pl.DataFrame([row, {**row, "neutral": True}])
    post = {"c": np.array([0.2]), "h": np.array([0.3]), "Q": np.array([[0, 0, 0, 0, -0.3]])}
    p = ct.predict(post, tg)
    assert np.allclose(p.sum(axis=1), 1) and p[0, 0] > p[1, 0] > p[1, 2]  # home, then Q
    flat = {"c": np.array([0.2]), "h": np.array([0.0]), "Q": np.zeros((1, 5))}
    q = ct.predict(flat, tg)
    assert q[0, 0] == pytest.approx(q[0, 2])  # equal sides, no home, no Q: symmetric


def test_scores_and_paired_differences() -> None:
    y = np.array([0, 1, 2])
    p = np.array([[0.6, 0.3, 0.1], [0.2, 0.5, 0.3], [0.1, 0.2, 0.7]])
    assert ct.log_loss(p, y) == pytest.approx(-np.log([0.6, 0.5, 0.7]))
    assert ct.rps(np.array([[1.0, 0, 0]]), np.array([0]))[0] == 0
    assert ct.rps(np.array([[0.0, 0, 1.0]]), np.array([0]))[0] == pytest.approx(1.0)
    d = ct.paired(np.array([1.0, 1.1, 0.9]), np.array([1.2, 1.2, 1.2]))
    assert d["mean"] < 0 and d["hi"] < 0
    assert ct.outcome(np.array([2, 1, 0]), np.array([1, 1, 3])).tolist() == [0, 1, 2]


# ------------------------------------------------------------------ C2 step 4 (D56)


def test_the_rerun_covers_the_five_then_the_eight_c2_bundles() -> None:
    from adatta.eval import c2, harness

    five, eight = list(harness.load_config().bundles), list(c2.load_config().bundles)
    assert list(ct.BUNDLE_ORDER) == five + eight and ct.BUNDLE_ORDER[0] == "england"
    assert list(ct.bundles()) == list(ct.BUNDLE_ORDER)
    assert ct.MAX_GOALS == 20


def _c2_fit() -> Any:
    from test_c2_projection import a_fit

    return a_fit()


def test_a_c2_side_is_phase1a_as_served_constant_across_draws() -> None:
    fit = _c2_fit()
    s = ct.c2_side(fit, 3, 94)
    assert not s["cold"] and s["att_mean"] == pytest.approx(float(fit.params.attack[2]))
    assert len(s["att"]) == ct.SIDE_DRAWS and len(set(s["att"])) == 1
    assert set(s["rho"]) == {fit.params.rho} and set(s["kappa"]) == {0.0}
    cold = ct.c2_side(fit, 999, 94)
    assert cold["cold"] and cold["att_mean"] == float(fit.league_att[0])


def test_c2_strengths_come_from_the_live_fit_at_the_monday() -> None:
    from test_c2 import C2L, c2_matches

    from adatta.model import c2_live

    df = c2_matches()
    mon = datetime(2019, 11, 4, tzinfo=UTC)
    sides = pl.DataFrame({"match_id": [1, 2], "monday": [mon, mon], "team": [1001, 1002],
                          "league": [94, 94], "side": ["home", "away"]})  # fmt: skip
    rows = ct.c2_strengths(df, C2L, "portugal", sides, [mon], log=lambda _: None)
    fit = c2_live.fit_as_of(df, C2L, c2_live.scoring_config(), mon, "portugal")
    assert rows.height == 2 and rows["gates_ok"].all() and not rows["cached"].any()
    assert rows["att_mean"][0] == pytest.approx(ct.c2_side(fit, 1001, 94)["att_mean"], abs=0)


def test_predict_and_tails_reach_the_c2_bundles_at_20_goals() -> None:
    row = {"bundle_home": "denmark", "bundle_away": "england", "neutral": False,
           "att_draws_home": [0.0], "def_draws_home": [0.0], "att_draws_away": [0.0],
           "def_draws_away": [0.0], "rho_home": [0.0], "kappa_home": [0.0]}  # fmt: skip
    tg = pl.DataFrame([row])
    q = np.zeros((1, len(ct.BUNDLE_ORDER)))
    q[0, ct.BUNDLE_ORDER.index("denmark")] = -0.4
    post = {"c": np.array([0.2]), "h": np.array([0.0]), "Q": q}
    p = ct.predict(post, tg)
    assert np.allclose(p.sum(axis=1), 1) and p[0, 2] > p[0, 0]  # the weaker league at home
    assert ct.tails(post, tg)[0] < 1e-12
    big = {"c": np.array([2.6]), "h": np.array([0.0]), "Q": np.zeros((1, len(ct.BUNDLE_ORDER)))}
    assert ct.tails(big, tg)[0] > ct.TAIL_MAX  # rates of about 13 goals a side
