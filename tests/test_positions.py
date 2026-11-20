"""P3-g (docs/PHASE3_TASKS.md): slot codes of a formation, the as-of position fit, the
scaling of W, the slot-by-slot XI (always one keeper) and the dev evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from adatta.eval import positions as ps
from adatta.features.lineups import CODE_LINE

T0 = datetime(2022, 8, 6, 15, tzinfo=UTC)
F433 = "4-3-3"


def test_formation_slots_start_with_the_keeper() -> None:
    s = ps.slots_of("4-2-3-1")
    assert len(s) == 11 and s[0] == "GK" and s.count("GK") == 1
    assert s[1:5] == ["LB", "LCB", "RCB", "RB"] and s[-1] == "ST"
    assert ps.projected_formation("other", [0.5, 0.3, 0.2], ["other", "4-4-2", F433]) == "4-4-2"


def test_ipf_meets_both_margins_and_the_xi_has_one_keeper() -> None:
    rng = np.random.default_rng(0)
    slots = ps.slots_of(F433)
    slot_gk = np.array([c == "GK" for c in slots])
    n = 18
    is_gk = np.zeros(n, bool)
    is_gk[:2] = True
    p = np.concatenate([[0.6, 0.4], rng.uniform(0.2, 0.9, n - 2)])
    fits = rng.uniform(0.01, 1, (n, len(slots)))
    rows = ps.row_targets(p, is_gk, 1, len(slots))
    w = ps.ipf(ps.weights(p, fits, is_gk, slot_gk), rows)
    assert np.allclose(w.sum(axis=0), 1.0, atol=1e-8)
    assert np.allclose(w.sum(axis=1), rows, atol=1e-6)
    assert w[np.ix_(is_gk, ~slot_gk)].sum() == 0 and w[np.ix_(~is_gk, slot_gk)].sum() == 0
    pick = ps.assign(w)
    assert (pick >= 0).all() and len(set(pick)) == 11
    assert is_gk[pick[0]] and is_gk[pick].sum() == 1
    top = ps.b_top(p, is_gk)
    assert len(top) == 11 and sum(is_gk[i] for i in top) == 1


def test_fit_shrinks_a_players_history_towards_his_line() -> None:
    h = ps.History(["GK", "LB", "RB", "CB", "ST"])
    for _ in range(4):
        h.add(1, [(10, "D", "LB"), (11, "D", "RB"), (12, "D", "CB")])
    f10, new = h.fit(1, 10, "D"), h.fit(1, 99, "D")
    assert f10[h.ix["LB"]] > 0.5 and f10[h.ix["LB"]] < 1
    assert new[h.ix["LB"]] == pytest.approx(new[h.ix["RB"]])  # no history: the team's shares
    assert new[h.ix["ST"]] < 0.05


def _world(n: int = 6) -> ps.Inputs:
    """Team 1 plays 4-3-3 weekly with a fixed XI (players 1-11 in the slot order); player 12
    is a back-up keeper, 13 a reserve striker. P3-b's pool rows and P3-c's predictions exist
    for the last match only."""
    slots = ps.slots_of(F433)
    rows = []
    for m in range(n):
        for i, code in enumerate(slots):
            rows.append({"match_id": 100 + m, "team_id": 1, "player_id": i + 1,
                         "league_ref": "39", "season": 2022, "kickoff": T0 + timedelta(days=7 * m),
                         "pos": CODE_LINE[code], "slot": code, "formation": F433,
                         "positional": True})  # fmt: skip
    lineups = pl.DataFrame(rows)
    last = 100 + n - 1
    pool = pl.DataFrame({
        "match_id": [last] * 13, "team_id": [1] * 13, "player_id": list(range(1, 14)),
        "pos": [CODE_LINE[c] for c in slots] + ["G", "F"],
        "p_start": [0.9] * 11 + [0.1, 0.1],
    })  # fmt: skip
    vocab = ["4-4-2", F433, "other"]
    formations = pl.DataFrame({"match_id": [last], "team_id": [1], "top": [F433],
                               "p": [[0.2, 0.7, 0.1]]})  # fmt: skip
    return ps.Inputs(lineups, pool, formations, vocab)


def test_evaluation_on_a_settled_team() -> None:
    inp = _world()
    key = pl.DataFrame({"match_id": [105], "team_id": [1]})
    res, calib = ps.evaluate(inp, key)
    r = res.row(0, named=True)
    assert r["formation_hit"] and r["gk_in_xi"] == 1
    assert r["overlap_g"] == 11 and r["overlap_top"] == 11
    assert r["slot_g"] == pytest.approx(1.0) and r["slot_last"] == pytest.approx(1.0)
    assert calib.height > 0 and ps.ece(calib) < 0.5
    assert ps.leakage(inp, key, res)


def test_history_is_as_of_the_target() -> None:
    inp = _world()
    # swap the two full-backs in the target match itself: their history doesn't know yet
    lu = inp.lineups.with_columns(
        slot=pl.when((pl.col("match_id") == 105) & (pl.col("slot") == "LB")).then(pl.lit("RB"))
        .when((pl.col("match_id") == 105) & (pl.col("slot") == "RB")).then(pl.lit("LB"))
        .otherwise(pl.col("slot"))
    )  # fmt: skip
    res, _ = ps.evaluate(ps.Inputs(lu, inp.pool, inp.formations, inp.vocab),
                         pl.DataFrame({"match_id": [105], "team_id": [1]}))  # fmt: skip
    r = res.row(0, named=True)
    assert r["overlap_g"] == 11 and r["slot_g"] == pytest.approx(9 / 11)


# ------------------------------------------------------------------- restated: arrange B_top


def test_arrange_puts_the_keeper_in_goal_and_never_an_outfielder() -> None:
    slots = ps.slots_of(F433)
    slot_gk = np.array([c == "GK" for c in slots])
    rng = np.random.default_rng(1)
    fits = rng.uniform(0.01, 1, (11, 11))
    is_gk = np.zeros(11, bool)
    is_gk[3] = True
    out = ps.arrange(fits, is_gk, slot_gk)
    assert out[0] == 3 and sorted(out.tolist()) == list(range(11))
    none = ps.arrange(fits, np.zeros(11, bool), slot_gk)  # no keeper in the squad list
    assert none[0] == -1 and (none[1:] >= 0).all() and len(set(none[1:].tolist())) == 10


def test_place_last_keeps_last_codes_and_fills_the_rest_by_fit() -> None:
    slots = ps.slots_of(F433)
    is_gk = np.array([True] + [False] * 10)
    fits = np.full((11, 11), 0.01)
    for r in range(11):
        fits[r, r] = 0.9  # every player fits his own slot best
    codes: list[str | None] = [slots[0], slots[2], slots[1]] + [None] * 8
    out = ps.place_last(codes, fits, is_gk, slots)
    assert out[0] == 0 and out[2] == 1 and out[1] == 2  # swapped by last match
    assert [int(out[s]) for s in range(3, 11)] == list(range(3, 11))  # the rest by fit


def test_arranged_evaluation_on_a_settled_team() -> None:
    inp = _world()
    key = pl.DataFrame({"match_id": [105], "team_id": [1]})
    res, calib = ps.evaluate(inp, key)
    r = res.row(0, named=True)
    assert r["overlap_a"] == r["overlap_top"] == 11 and r["placed_in_top"]
    assert r["slot_a"] == pytest.approx(1.0) and r["slot_xl"] == pytest.approx(1.0)
    assert r["gk_empty"] == 0 and r["a_G"] == 1 and r["n_D"] == 4
    v = ps.verdict_arranged(res, calib, ps.leakage(inp, key, res))
    assert v.checks[1]["pass"] and v.checks[3]["pass"]  # the XI is B_top's; leakage
    assert "B_xi_last" in ps.markdown_arranged(res, v)


def test_arranged_history_is_as_of_the_target() -> None:
    inp = _world()
    lu = inp.lineups.with_columns(
        slot=pl.when((pl.col("match_id") == 105) & (pl.col("slot") == "LB")).then(pl.lit("RB"))
        .when((pl.col("match_id") == 105) & (pl.col("slot") == "RB")).then(pl.lit("LB"))
        .otherwise(pl.col("slot"))
    )  # fmt: skip
    res, _ = ps.evaluate(ps.Inputs(lu, inp.pool, inp.formations, inp.vocab),
                         pl.DataFrame({"match_id": [105], "team_id": [1]}))  # fmt: skip
    r = res.row(0, named=True)
    assert r["slot_a"] == pytest.approx(9 / 11) and r["slot_xl"] == pytest.approx(9 / 11)
