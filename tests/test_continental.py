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


# ------------------------------------------------------------- the UEFA holdout (D58)


def test_holdout_seasons_need_the_uefa_opening() -> None:
    from adatta.eval import harness

    assert ct.seasons() == (ct.SEASONS, ct.DEV)
    ho = tuple(harness.load_config().holdout_seasons)
    assert ct.seasons(harness.HoldoutKey("7", "uefa")) == (ct.SEASONS + ho, ho)
    for other in ("phase1b", "phase2", "cups", "c2", "international"):
        with pytest.raises(harness.HoldoutLocked):
            ct.seasons(harness.HoldoutKey("7", other))
        with pytest.raises(harness.HoldoutLocked):  # before any query
            ct.targets(None, {}, harness.HoldoutKey("7", other))
    assert not set(ct.SEASONS) & set(ho)  # dev runs never load a holdout season


def test_the_uefa_gate_file_matches_the_scoring_code(tmp_path: Any) -> None:
    import yaml

    g = ct.load_gate()
    assert g["checks"] == [1, 2, 3, 4] and g["max_goals"] == ct.MAX_GOALS
    assert g["grid_days"] == ct.REFIT_DAYS
    from pathlib import Path

    bad = yaml.safe_load(Path(ct.GATE_FILE).read_text())
    bad["uefa_gate"]["max_competition_rel_worse"] = 0.02
    path = tmp_path / "gate.yml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValueError, match="does not match"):
        ct.load_gate(str(path))


def test_the_holdout_is_judged_on_checks_1_to_4_alone() -> None:
    checks = {"1 C1 beats B_own": True, "2 C1 beats B_elo": True, "3 no competition": True,
              "4 ECE": True, "5 inference gates": False, "6 leakage": False}  # fmt: skip
    assert ct.passed(checks, holdout=True) and not ct.passed(checks, holdout=False)
    assert not ct.passed({**checks, "3 no competition": False}, holdout=True)
    with pytest.raises(ValueError, match="not all reported"):
        ct.passed({k: v for k, v in checks.items() if not k.startswith("4")}, holdout=True)


def test_the_uefa_opening_is_made_once_after_phase1b(tmp_path: Any) -> None:
    from adatta.adapters.storage import LocalStore
    from adatta.eval import harness
    from adatta.eval import holdout as ho

    store = LocalStore(tmp_path)
    with pytest.raises(harness.HoldoutLocked, match="comes first"):
        ho.claim(store, "9", ct.MODEL_NAME, "abc", ct.OPENING)
    ho.claim(store, "1", "phase1b_strength_open_xg", "abc")
    assert ho.claim(store, "9", ct.MODEL_NAME, "abc", ct.OPENING)["opening"] == "uefa"
    assert ho.claim(store, "9", ct.MODEL_NAME, "abc", ct.OPENING)["run_id"] == "9"  # a rerun
    with pytest.raises(harness.HoldoutLocked):
        ho.claim(store, "10", ct.MODEL_NAME, "abc", ct.OPENING)
    assert ho.unlock(store, "9", ct.MODEL_NAME, ct.OPENING) == harness.HoldoutKey("9", "uefa")
    with pytest.raises(harness.HoldoutLocked):
        ho.unlock(store, "10", ct.MODEL_NAME, ct.OPENING)
    with pytest.raises(harness.HoldoutLocked):  # the C2 opening's marker is not this one
        ho.unlock(store, "9", ct.MODEL_NAME, "c2")


def test_leakage_samples_only_the_scored_seasons() -> None:
    hist = _hist([(1, 2, 1, 0, "39", 0), (3, 4, 0, 0, "39", 2)])
    mon = K + timedelta(days=12)
    tg = pl.DataFrame({"match_id": [100, 101], "season": [2023, 2024], "cross": [True, True],
                       "monday": [mon, mon], "kickoff": [mon + timedelta(days=1)] * 2,
                       "home": [1, 3], "away": [2, 4]})  # fmt: skip
    elo = ct.elo_at(hist, [(100, mon, 1, 2), (101, mon, 3, 4)])
    leak = ct.leakage(tg, hist, elo, set(), scored=(2024,))
    assert leak == {"n": 1, "changed": 0, "ok": True}


# ---------------------------------------------- pre-registrations of 2026-11-27 (D61, D62)


def test_the_uefa_live_gate_keeps_the_holdout_checks() -> None:
    """D62: C1 v2 is judged on 2026/27 by the UEFA holdout's checks 1-4, unchanged."""
    from pathlib import Path

    import yaml

    live = yaml.safe_load(Path("config/uefa_live_gate.yml").read_text())["uefa_live_gate"]
    held = ct.load_gate()
    for k in ("grid_days", "max_goals", "paired_interval", "max_competition_rel_worse",
              "ece_sims", "ece_quantile", "checks"):  # fmt: skip
        assert live[k] == held[k], k
    assert live["seasons"] == [2026] and live["opens_after"] == "2027-06-30"
    assert live["rho_phases"] == ["group_stage", "league_phase", "qualifying", "knockout"]


def test_the_c2_league_gate_is_per_league_with_d59_routes() -> None:
    """D61: each C2 top flight alone, two looks at 97.5%, D59's routes and margin."""
    from pathlib import Path

    import yaml

    from adatta.eval import c2

    g = yaml.safe_load(Path("config/c2_league_gate.yml").read_text())["c2_league_gate"]
    tops = sorted(ls[0] for ls in c2.load_config().bundles.values())
    assert sorted(g["top_flights"]) == tops and g["routes"] == ["1", "2", "2T"]
    assert g["margin"] == 0.001 and g["max_segment_rel_worse"] == 0.01 and g["max_goals"] == 20
    assert [lk["seasons"] for lk in g["looks"]] == [[2026], [2026, 2027]]
    assert all(lk["interval"] == 0.975 for lk in g["looks"])
    old = yaml.safe_load(Path("config/c2_retest_gate.yml").read_text())["c2_gate"]
    assert g["segment_metrics"] == old["segment_metrics"]  # the same segments as D54 (h)
