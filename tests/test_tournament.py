"""Stage D v2: the tournament simulator's rules engine, simulation and leakage
(adatta/sim/tournament.py, adatta/eval/tournament.py). No database, no fits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from adatta.eval import tournament as ev
from adatta.sim import tournament as tm

RNG = np.random.default_rng(0)


def g(h: int, a: int, x: int, y: int) -> tuple[int, int, int, int]:
    return (h, a, x, y)


# ------------------------------------------------------------------------ rules engine


def test_points_then_head_to_head_with_reapplication() -> None:
    # 1, 2, 3 all on 6 points; head-to-head among them: 1 beat 2, 2 beat 3, 3 beat 1, all
    # 1-0, so h2h is level; overall goal difference decides (1 beat 4 by most)
    games = [g(1, 2, 1, 0), g(2, 3, 1, 0), g(3, 1, 1, 0), g(1, 4, 5, 0), g(2, 4, 3, 0),
             g(3, 4, 1, 0)]  # fmt: skip
    tab = tm.rank_group([1, 2, 3, 4], games, ["h2h", "gd", "gf", "random"], RNG)
    assert tab.order == [1, 2, 3, 4] and not tab.random
    # 1 and 4 level on 6 points: 4 beat 1; 2 and 3 level on 3: 3 beat 2, although 2 has
    # the better goal difference overall
    games = [g(1, 2, 2, 0), g(1, 3, 2, 0), g(2, 3, 0, 1), g(2, 4, 9, 0), g(3, 4, 0, 3),
             g(1, 4, 0, 5)]  # fmt: skip
    tab = tm.rank_group([1, 2, 3, 4], games, ["h2h", "gd", "gf", "random"], RNG)
    assert tab.order == [4, 1, 3, 2]


def test_overall_first_differs_from_head_to_head_first() -> None:
    # 2 and 3 level on 4 points; 2 has the better goal difference, 3 won their meeting
    games = [g(2, 3, 0, 1), g(2, 4, 5, 0), g(1, 2, 0, 0), g(1, 3, 1, 0), g(3, 4, 0, 0),
             g(1, 4, 1, 1)]  # fmt: skip
    st = tm._stats([1, 2, 3, 4], games)
    assert st[2][0] == st[3][0] == 4 and st[2][1] > st[3][1]
    overall = tm.rank_group([1, 2, 3, 4], games, ["gd", "gf", "h2h_pts", "h2h_gd", "h2h_gf",
                                                  "random"], RNG)  # fmt: skip
    h2h = tm.rank_group([1, 2, 3, 4], games, ["h2h", "gd", "gf", "random"], RNG)
    assert overall.order.index(2) < overall.order.index(3)
    assert h2h.order.index(3) < h2h.order.index(2)


def test_what_the_data_cant_decide_is_random_and_flagged() -> None:
    games = [g(1, 2, 1, 1), g(3, 4, 1, 1), g(1, 3, 0, 0), g(2, 4, 0, 0), g(1, 4, 2, 2),
             g(2, 3, 2, 2)]  # fmt: skip
    seen = set()
    for seed in range(30):
        tab = tm.rank_group([1, 2, 3, 4], games, ["h2h", "gd", "gf", "random"],
                            np.random.default_rng(seed))  # fmt: skip
        assert tab.random and sorted(tab.order) == [1, 2, 3, 4]
        seen.add(tuple(tab.order))
    assert len(seen) > 1


def test_best_thirds_and_their_slots() -> None:
    eds = tm.load_formats()
    euro = eds["euro2020"]
    rows = [("A", [4, 0, 4, 1]), ("B", [3, -2, 1, 1]), ("C", [3, -1, 4, 1]), ("D", [4, 1, 3, 1]),
            ("E", [3, -5, 2, 1]), ("F", [4, 1, 7, 1])]  # fmt: skip
    order, rand = tm.rank_thirds(rows, euro.thirds.rank if euro.thirds else (), RNG)
    assert order[:4] == ["F", "D", "A", "C"] and not rand
    places = {x: [10 * i + 1, 10 * i + 2, 10 * i + 3, 10 * i + 4]
              for i, x in enumerate("ABCDEF")}  # fmt: skip
    pairs = tm.first_round(euro, places, order)
    # ACDF: 1B v 3F, 1C v 3D, 1E v 3C, 1F v 3A (the Euro 2020 row, as published)
    assert pairs["R16_1"] == (11, 53) and pairs["R16_7"] == (21, 33)
    assert pairs["R16_5"] == (41, 23) and pairs["R16_3"] == (51, 3)


def test_every_edition_validates_and_a_bad_table_is_refused() -> None:
    eds = tm.load_formats()
    assert len(eds) == 17 and len(eds["wc2026"].thirds.table) == 495  # type: ignore[union-attr]
    ed = eds["asian2019"]
    assert ed.thirds is not None
    table = dict(ed.thirds.table)
    table["ABCD"] = "ABCD"  # 1A can't meet the third of group A
    bad = tm.Edition(**{**ed.__dict__, "thirds": tm.Thirds(4, ed.thirds.rank, ed.thirds.columns,
                                                            table)})  # fmt: skip
    with pytest.raises(ValueError, match="not allowed"):
        tm.validate(bad)


# -------------------------------------------------------------------------- simulation


def fake_post(teams: list[int], k: int = 40) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(1)
    n = len(teams)
    return {
        "c": np.full(k, 0.1), "g": np.zeros((k, 2)), "h": np.full(k, 0.3), "h_u": np.full(k, 0.2),
        "phi": np.full(k, 50.0), "rho": np.full(k, -0.03), "teams": np.array(teams),
        "s_last": np.linspace(-1, 1, n)[None, :] + 0.05 * rng.standard_normal((k, n)),
        "o_last": np.zeros((k, n)), "sigma_w": np.full(k, 0.04), "sigma_u": np.full(k, 1.0),
        "sigma_o": np.full(k, 0.2), "conf": np.zeros((k, 5)),
    }  # fmt: skip


def mini() -> tm.Edition:
    """Two groups of four, the top two to semi-finals, then a final."""
    return tm.Edition(
        "mini", "Mini", 0, 2020, "2020-06-01", "2020-06-30", "dev", "", {
            "A": ("a1", "a2", "a3", "a4"), "B": ("b1", "b2", "b3", "b4")},
        ("h2h", "gd", "gf", "random"), None,
        (tm.Round("SF", "stated", True, {"SF_1": ("1A", "2B"), "SF_2": ("1B", "2A")}),
         tm.Round("F", "stated", True, {"F": ("W:SF_1", "W:SF_2")})))  # fmt: skip


def mini_state(scores: dict[tuple[int, int], tuple[int, int]] | None = None) -> tm.State:
    groups = {"A": [1, 2, 3, 4], "B": [5, 6, 7, 8]}
    fixtures = {
        x: [tm.Fixture(h, a, 100, "neutral", (scores or {}).get((h, a)))
            for i, h in enumerate(ts) for a in ts[i + 1:]]
        for x, ts in groups.items()
    }  # fmt: skip
    slots = {s: tm.Slot(100, None) for s in ("SF_1", "SF_2", "F")}
    return tm.State(groups, fixtures, slots, {t: None for t in range(1, 9)})


def test_simulation_is_coherent_and_matches_the_model() -> None:
    ed = mini()
    tm.validate(ed)
    teams = list(range(1, 9))
    post = fake_post(teams)
    rt = tm.rates(post, teams, {t: "UEFA" for t in teams}, 100, 60, [100], "finals",
                  np.random.default_rng(2))  # fmt: skip
    st = mini_state()
    res = tm.simulate(ed, st, rt, 4000, 7, track=[(1, 2)])
    assert np.allclose(res.position.sum(axis=1), 1)
    assert abs(res.advance.sum() - 4) < 1e-9 and abs(res.win.sum() - 1) < 1e-9
    assert abs(res.reach["F"].sum() - 2) < 1e-9
    assert res.win[teams.index(8)] > res.win[teams.index(1)]  # the strongest side wins most
    p = ev.closed_1x2(rt, 1, 2, 100, "neutral")
    se = np.sqrt(p * (1 - p) / 4000)
    assert (np.abs(res.match_1x2[(1, 2)] - p) <= 4 * se).all()


def test_a_fixed_result_stays_fixed_and_a_tie_matches_its_closed_form() -> None:
    ed = mini()
    teams = list(range(1, 9))
    rt = tm.rates(fake_post(teams), teams, {t: "UEFA" for t in teams}, 100, 60, [100], "finals",
                  np.random.default_rng(2))  # fmt: skip
    # team 1 has won all three: it tops group A in every simulation
    st = mini_state({(1, 2): (3, 0), (1, 3): (3, 0), (1, 4): (3, 0)})
    res = tm.simulate(ed, st, rt, 2000, 7)
    assert res.position[0, 0] == 1.0
    p = ev.Played(1, 1, 8, datetime(2020, 6, 1, tzinfo=UTC), "FT", (0, 0), None, None, "SF",
                  None, "neutral")  # fmt: skip
    for et in (True, False):
        closed, base = ev.tie_probs(rt, p, 100, et)
        label, z = ev.tie_coherence(rt, p, 100, et, closed, 20000)
        assert label == "tie 1" and abs(z) <= ev.fw_threshold(1)
        assert (closed == base) == (not et)


def test_check_2_is_family_wise() -> None:
    # Bonferroni at 1%: one comparison needs |z| <= 2.58; ~900 need about 4.4
    assert abs(ev.fw_threshold(1) - 2.5758) < 1e-3
    assert 4.3 < ev.fw_threshold(906) < 4.5
    sims = 4000
    z = ev._z([0.31, 0.30], [0.30, 0.30], sims)
    assert abs(z[0] - 0.01 / np.sqrt(0.21 / sims)) < 1e-9 and z[1] == 0.0
    assert ev._z([0.01], [0.0], sims) == [np.inf]  # off a certain outcome: always flagged


def test_winner_rule() -> None:
    k = datetime(2020, 6, 1, tzinfo=UTC)
    assert ev.winner(ev.Played(1, 1, 2, k, "PEN", (1, 1), (0, 0), (4, 5), "QF", None, "")) == 2
    assert ev.winner(ev.Played(1, 1, 2, k, "AET", (1, 1), (1, 0), None, "QF", None, "")) == 1
    assert ev.winner(ev.Played(1, 1, 2, k, "WO", None, None, None, "F", None, "")) is None
    # a shoot-out without a stored 90' score (Copa América 2019's quarter-finals)
    assert ev.winner(ev.Played(1, 1, 2, k, "PEN", None, None, (4, 3), "QF", None, "")) == 1


# ------------------------------------------------------------------------------ leakage


def test_a_state_uses_only_what_is_final_three_hours_before() -> None:
    ed = mini()
    t0 = datetime(2020, 6, 1, tzinfo=UTC)
    groups = {"A": [1, 2, 3, 4], "B": [5, 6, 7, 8]}
    gm = []
    n = 0
    for ts in groups.values():
        for i, h in enumerate(ts):
            for a in ts[i + 1 :]:
                gm.append(ev.Played(n, h, a, t0 + timedelta(days=n), "FT", (1, 0), None, None,
                                    "group", None, "neutral"))  # fmt: skip
                n += 1
    d = ev.EdData(ed, groups, gm, [], {t: "UEFA" for t in range(1, 9)},
                  {t: None for t in range(1, 9)}, {t: str(t) for t in range(1, 9)})  # fmt: skip
    rp = ev.Replay([], [], {}, [])
    t = t0 + timedelta(days=3, hours=2)  # match 3 kicked off 2 h before: not final yet
    st = ev.state_at(d, rp, t)
    fixed = sum(f.score is not None for fx in st.fixtures.values() for f in fx)
    assert fixed == 3 and not st.done_groups
    assert ev.leakage(d, rp, t) == []
