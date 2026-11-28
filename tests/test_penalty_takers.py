"""P3b-5 (D72): penalty takers in the goalscorer forecast. The inputs read only matches
final by the as-of time, M' is M when no team scores penalties, and the usual taker's
P(at least one goal) rises when his team wins penalties."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from adatta.eval import penalty_takers as pt

K0 = datetime(2021, 8, 2, 15, tzinfo=UTC)


def _app(weeks: int, pens: dict[int, int], late_pen: bool = False) -> pl.DataFrame:
    """Team 1 (players 101-111) plays weekly in league 39; player 109 takes `pens[week]`
    penalties (scored). `late_pen` adds one for player 105 in the target week itself."""
    rows = []
    for w in range(weeks):
        k = K0 + timedelta(days=7 * w)
        for i in range(1, 12):
            pid = 100 + i
            pen = pens.get(w, 0) if pid == 109 else 0
            if late_pen and w == weeks - 1 and pid == 105:
                pen = 1
            goals = pen + (1 if i == 10 else 0)
            rows.append({"league_ref": "39", "match_id": w + 1, "team_id": 1, "player_id": pid,
                         "kickoff": k, "pos": "F" if i >= 9 else "M", "goals": goals,
                         "pen_scored": pen, "pen_missed": 0, "minutes": 90})  # fmt: skip
    return pl.DataFrame(rows)


def _table(match_id: int, kickoff: datetime) -> pl.DataFrame:
    rows = [{"match_id": match_id, "team_id": 1, "player_id": 100 + i, "kickoff": kickoff,
             "league_ref": "39", "season": 2021, "line": "F" if i >= 9 else "M", "started": True,
             "in_squad": True, "in_pool": True, "e_lk": 1.0, "e24_start": 1.0, "e24_bench": 0.0,
             "q_goals_start": 0.3 if i >= 9 else 0.1, "q_goals_sub": 0.2, "r0_goals": 0.2,
             "r1_goals": 0.2, "y_goals": 1 if i == 9 else 0, "y_pen": 1 if i == 9 else 0,
             "o": 0.03, "alpha": 0.7, "u18": False} for i in range(1, 12)]  # fmt: skip
    return pl.DataFrame(rows)


def test_inputs_read_only_matches_final_by_the_as_of() -> None:
    weeks = 30
    app = _app(weeks, {5: 1, 12: 1, 20: 1}, late_pen=True)
    target = _table(weeks, K0 + timedelta(days=7 * (weeks - 1)))
    t = pt.columns(target, app)
    att = dict(zip(t["player_id"], t["pen_att"], strict=True))
    assert att[109] == 3 and att[105] == 0  # the target match's own penalty is not read
    pi = float(t["pi"][0])
    assert 0 < pi < 3 / 29 + 1e-9  # shrunk towards the league's (here the same team)
    assert float(t.filter(pl.col("player_id") == 109)["line_pen"][0]) > 0


def test_m_prime_is_m_without_penalties_and_lifts_the_taker() -> None:
    weeks = 30
    target = _table(weeks, K0 + timedelta(days=7 * (weeks - 1)))
    pmf = [0.25, 0.35, 0.25, 0.1, 0.05]
    pmfs = pl.DataFrame({"match_id": [weeks], "team_id": [1], "pmf": [pmf]})
    none = pt.columns(target, _app(weeks, {}))
    s0 = pt.score_goals(none, pmfs, "lk")
    m, mp = (s0.filter(pl.col("model") == x).sort("player_id") for x in ("M", "M_pen"))
    assert np.allclose(m["p1"].to_numpy(), mp["p1"].to_numpy())  # pi = 0, line_pen = 0
    taker = pt.columns(target, _app(weeks, {w: 1 for w in range(0, 28, 2)}))
    s1 = pt.score_goals(taker, pmfs, "lk")
    m, mp = (s1.filter(pl.col("model") == x).sort("player_id") for x in ("M", "M_pen"))
    gain = dict(zip(mp["player_id"], mp["p1"].to_numpy() - m["p1"].to_numpy(), strict=True))
    assert gain[109] > 0.05 and gain[111] < 0  # the taker up, another forward down
    assert mp["mean"].sum() == pytest.approx(m["mean"].sum(), rel=1e-6)  # the team total kept
