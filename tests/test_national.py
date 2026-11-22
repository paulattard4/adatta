"""Stage D v0 (docs/BEYOND_LEAGUES_PLAN.md, "D v0 acceptance"): competition rules, the
confederation assignment, the eloratings.net Elo as of each Monday, the refit grid, the
negative binomial score matrix and the walk carried to the match month."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from adatta.adapters.storage import LocalStore
from adatta.eval import holdout as ho
from adatta.eval.harness import HoldoutKey, HoldoutLocked
from adatta.model import continental as ct
from adatta.model import national as nt

K = datetime(2019, 3, 21, 19, tzinfo=UTC)  # a Thursday


def _tg(rows: list[tuple[int, int, int, int, str, str, int]]) -> pl.DataFrame:
    """(home, away, hg, ag, league id, role, days after K)."""
    out = []
    for i, (h, a, hg, ag, lg, role, d) in enumerate(rows):
        k = K + timedelta(days=d)
        out.append({"match_id": i, "league_id": lg, "type": nt.comp_type(lg),
                    "comp_conf": nt.COMP_CONF.get(lg, "world"), "kickoff": k,
                    "monday": ct.monday(k), "home": h, "away": a, "conf_home": "UEFA",
                    "conf_away": "UEFA", "role": role, "hg": hg, "ag": ag})  # fmt: skip
    return pl.DataFrame(out, schema=nt.TARGET_SCHEMA)


def test_competition_rules() -> None:
    assert nt.comp_type("10") == "friendly" and nt.comp_type("1") == "finals"
    assert nt.comp_type("5") == "qualifier" and nt.comp_type("32") == "qualifier"
    assert [nt.elo_k(x) for x in ("1", "4", "32", "10")] == [60, 50, 40, 20]
    assert [nt.goal_mult(m) for m in (0, -1, 2, 3, -5)] == [1, 1, 1.5, 14 / 8, 16 / 8]
    assert nt.host_sides("swapped") == (0, 1, 0) and nt.host_sides("unknown") == (0, 0, 1)
    assert "9" not in nt.MEMBER_COMPS and "22" not in nt.MEMBER_COMPS  # guests at Copa, Gold Cup


def test_confederations_from_member_competitions_only() -> None:
    got = nt.assign_confederations([(1, "32"), (1, "10"), (2, "9"), (2, "34"), (3, "9")])
    assert got == {1: "UEFA", 2: "CONMEBOL"}  # 3 only played Copa America: no confederation
    with pytest.raises(ValueError):
        nt.assign_confederations([(4, "32"), (4, "30")])


def test_elo_is_as_of_the_monday_with_host_and_goal_margin() -> None:
    tg = _tg([(1, 2, 3, 0, "32", "home", 0), (1, 2, 0, 0, "10", "neutral", 5),
              (2, 1, 1, 1, "32", "swapped", 30)])  # fmt: skip
    elo = nt.elo_at(tg, [(m, r) for m, r in zip(tg["match_id"], tg["monday"], strict=True)])
    assert elo[0] == pytest.approx(nt.ELO_HOME)  # nothing final before match 0's Monday
    we = 1 / (10 ** (-nt.ELO_HOME / 400) + 1)
    d = 40 * (14 / 8) * (1 - we)  # qualifier, margin 3
    assert elo[1] == pytest.approx(2 * d)  # Monday after match 0; neutral: no host term
    # match 2 lists 2 at home but 1 hosts it: the 100 goes to the listed away side
    assert elo[2] < -2 * d + 1e-9


def test_refit_grid_and_training_cut() -> None:
    tg = _tg([(1, 2, 1, 0, "32", "home", 0), (2, 1, 0, 2, "32", "home", 5),
              (1, 3, 2, 2, "10", "home", 40)])  # fmt: skip
    times = nt.refit_times(tg, K - timedelta(days=10), K + timedelta(days=100))
    assert times == sorted(set(tg["monday"].to_list())) and len(times) == 3
    tr = nt.training(tg, times[1])  # the Tuesday match sees the Thursday result
    assert tr["match_id"].to_list() == [0]


def test_score_matrix_normalised_heavier_tail_and_tau() -> None:
    m = nt.score_matrix(np.array([1.4]), np.array([1.1]), np.array([5.0]), np.array([0.0]))
    assert m.shape == (1, 21, 21) and m.sum() == pytest.approx(1.0)
    pois = nt.score_matrix(np.array([1.4]), np.array([1.1]), np.array([1e6]), np.array([0.0]))
    tail = np.add.outer(np.arange(21), np.arange(21)) >= 6
    assert m[0][tail].sum() > pois[0][tail].sum()  # negative binomial: more big scores
    r = nt.score_matrix(np.array([1.4]), np.array([1.1]), np.array([5.0]), np.array([-0.1]))
    assert r[0, 0, 0] > m[0, 0, 0] and r[0, 1, 1] > m[0, 1, 1]  # rho < 0: more 0-0 and 1-1


def test_predict_carries_the_walk_and_cold_sides() -> None:
    k = 400
    post = {"c": np.full(k, 0.25), "g": np.zeros((k, 2)), "h": np.full(k, 0.3),
            "h_u": np.full(k, 0.2), "conf": np.zeros((k, 5)), "sigma_w": np.full(k, 0.05),
            "sigma_u": np.full(k, 0.5), "sigma_o": np.full(k, 0.05), "phi": np.full(k, 20.0),
            "rho": np.zeros(k), "s_last": np.tile([0.5, -0.5], (k, 1)),
            "o_last": np.zeros((k, 2)), "teams": np.array([1.0, 2.0]),
            "team_conf": np.zeros(2)}  # fmt: skip
    tg = _tg([(1, 2, 0, 0, "32", "neutral", 0), (2, 1, 0, 0, "32", "neutral", 0),
              (1, 2, 0, 0, "32", "home", 0), (1, 9, 0, 0, "32", "neutral", 0)])  # fmt: skip
    p, xg = nt.predict(post, tg, ct.monday(K), 60)
    assert np.allclose(p.sum(axis=1), 1)
    assert p[0, 0] == pytest.approx(p[1, 2], abs=0.02)  # neutral: listed order doesn't matter
    assert p[2, 0] > p[0, 0] and xg[2, 0] > xg[0, 0]  # host advantage
    assert 0 < p[3, 0] < 1  # 9 is unseen: its confederation's level with the spreads
    far = nt.predict(post, tg.with_columns(kickoff=pl.col("kickoff") + timedelta(days=720)),
                     ct.monday(K), 60)[0]  # fmt: skip
    assert far[0, 0] < p[0, 0]  # two years on, the stronger side's edge is less certain


def test_walk_collapsed_onto_played_months_equals_the_monthly_walk() -> None:
    rng = np.random.default_rng(1)
    rows = []
    for _ in range(120):
        h, a = (int(x) for x in rng.choice(9, 2, replace=False))
        rows.append((h, a, 1, 0, "32", "home", int(rng.integers(0, 700))))
    tg = _tg(rows)
    t = ct.monday(K + timedelta(days=900))
    d = nt.design(tg, t, 0.5)
    assert len(d.node_team) < len(d.teams) * d.n_months  # months off are marginalised out
    steps = rng.standard_normal((len(d.teams), d.n_months - 1))
    full = np.cumsum(np.c_[np.zeros(len(d.teams)), steps], 1)
    first = np.r_[True, d.node_team[1:] != d.node_team[:-1]]
    node_month = np.zeros(len(d.node_team), dtype=np.int64)
    for j in range(len(d.node_team)):
        node_month[j] = (0 if first[j] else node_month[j - 1]) + int(d.node_gap[j])
    at = full[d.node_team, node_month]
    w = nt.walk(at - np.where(first, 0.0, np.r_[0.0, at[:-1]]), d.node_team)
    assert np.allclose(w[d.hn], full[d.hi, d.month]) and np.allclose(w[d.an], full[d.ai, d.month])
    assert np.allclose(w[d.last_node], full[:, -1])  # each team's refit month
    assert np.allclose(nt.walk(np.stack([w * 0 + 1, w * 0 + 2]), d.node_team)[1],
                       2 * nt.walk(w * 0 + 1, d.node_team))  # (draws, nodes) as well  # fmt: skip


class _Store:
    def __init__(self) -> None:
        self.d: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self.d[key] = data

    def get(self, key: str) -> bytes:
        if key not in self.d:
            raise FileNotFoundError(key)
        return self.d[key]


def test_a_sampler_failure_is_refitted_once_at_twice_the_draws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    outcomes = iter([["rhat"], ["rhat"], [], ["identified conf OFC"]])

    def fake_fit(*_: object, draws: int = nt.DRAWS, **__: object) -> nt.Post:
        calls.append(draws)
        f = next(outcomes)
        return {"c": np.zeros(2)}, {"failed": f, "rhat": 1.02 if f else 1.0, "draws": draws}

    monkeypatch.setattr(nt, "fit", fake_fit)
    tg = _tg([(1, 2, 1, 0, "32", "home", 0)])
    st = _Store()
    t1, t2 = ct.monday(K) + timedelta(days=7), ct.monday(K) + timedelta(days=14)
    _, d = nt.cached_fit(st, tg, t1, 0.5)  # fails, rerun fails too: it stands, no third fit
    assert calls == [nt.DRAWS, nt.RERUN_DRAWS] and d["first_attempt"]["failed"] == ["rhat"]
    assert nt.cached_fit(st, tg, t1, 0.5)[1]["failed"] == ["rhat"] and len(calls) == 2
    _, d = nt.cached_fit(st, tg, t2, 0.5)  # passes first time: no rerun
    assert calls[2:] == [nt.DRAWS] and "first_attempt" not in d
    # a cached failure from before the rule is rerun when next read, the original kept
    key = nt._cache_key(t2, 0.5, 1.0)
    st.put(key + ".json", json.dumps({"failed": ["rhat"]}).encode())
    _, d = nt.cached_fit(st, tg, t2, 0.5)  # its rerun fails a non-sampler check: no rerun
    assert calls[3:] == [nt.RERUN_DRAWS] and key.removesuffix(".npz") + "_first.npz" in st.d
    assert d["failed"] == ["identified conf OFC"]


def test_v1_priors_as_amended() -> None:
    # D v1 amendment: only the confederation and openness widths changed from v0
    assert nt.VERSION == "v1" and (nt.CONF_SD, nt.SIGMA_O_SD) == (3.0, 0.3)
    assert nt._cache_key(K, 0.5, 1.0).startswith(f"{nt.PREFIX}/refits/v1")  # no v0 fit reused


def test_holdout_targets_need_the_international_opening(tmp_path: Path) -> None:
    nt.check_upto(nt.HOLDOUT_FROM, None)  # dev needs no key
    for key in (None, HoldoutKey("1"), HoldoutKey("1", "cups")):
        with pytest.raises(HoldoutLocked):
            nt.targets(None, LocalStore(tmp_path), nt.LIVE_FROM, key)
    nt.check_upto(nt.LIVE_FROM, HoldoutKey("1", nt.OPENING))
    with pytest.raises(ValueError, match="live period"):
        nt.check_upto(nt.LIVE_FROM + timedelta(days=1), HoldoutKey("1", nt.OPENING))


def test_international_opening_claimed_once_for_this_version(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    ho.claim(store, "1", "phase1b_strength_open_xg", "abc")  # the 1b opening exists
    m = ho.claim(store, "7", nt.model_name(), "def", nt.OPENING)
    assert m["model"] == "national_v1" and (tmp_path / ho.MARKERS["international"]).exists()
    with pytest.raises(HoldoutLocked, match="made once"):
        ho.claim(store, "8", nt.model_name(), "def", nt.OPENING)
    with pytest.raises(HoldoutLocked):
        ho.unlock(store, "8", nt.model_name(), nt.OPENING)
    assert ho.unlock(store, "7", nt.model_name(), nt.OPENING) == HoldoutKey("7", nt.OPENING)


def test_holdout_promotion_is_checks_1_3_4_and_dev_is_every_check() -> None:
    checks = {"1 v1 beats B_elo": True, "3 segments": True, "4 1X2 ECE": True,
              "5 inference gates on every refit": False, "6 leakage": True}  # fmt: skip
    assert nt.passed(checks, holdout=True) and not nt.passed(checks, holdout=False)
    assert not nt.passed({**checks, "4 1X2 ECE": False}, holdout=True)
    assert nt.HOLDOUT == (nt.HOLDOUT_FROM, nt.LIVE_FROM) and nt.DEV[1] == nt.HOLDOUT_FROM
