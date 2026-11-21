"""P3-i0 (docs/PHASE3_TASKS.md, P3-i): stints from a payload, the defensive rating in closed
form, the dev check's scores, and the training rows matched to P's fit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest

from adatta.adapters import api_football_stats as st
from adatta.eval import defence as ed
from adatta.features import appearances as ap
from adatta.features import stints as sn
from adatta.model import defence as md

K = datetime(2022, 9, 3, 15, tzinfo=UTC)


def _lineup(team: int, first: int) -> dict[str, Any]:
    return {"team": {"id": team},
            "startXI": [{"player": {"id": first + i}} for i in range(11)],
            "substitutes": [{"player": {"id": first + 11 + i}} for i in range(3)]}  # fmt: skip


def _ev(kind: str, team: int, minute: int, player: int, assist: int | None = None,
        detail: str = "", extra: int | None = None) -> dict[str, Any]:  # fmt: skip
    return {"type": kind, "detail": detail, "team": {"id": team}, "player": {"id": player},
            "assist": {"id": assist}, "time": {"elapsed": minute, "extra": extra}}  # fmt: skip


def _fx(events: list[dict[str, Any]], ft: tuple[int, int]) -> dict[str, Any]:
    return {"fixture": {"id": 1, "status": {"short": "FT"}},
            "teams": {"home": {"id": 10}, "away": {"id": 20}},
            "score": {"fulltime": {"home": ft[0], "away": ft[1]}},
            "lineups": [_lineup(10, 100), _lineup(20, 200)], "events": events}  # fmt: skip


EVENTS = [
    _ev("Goal", 10, 12, 105, detail="Normal Goal"),  # home scores: away concedes
    _ev("subst", 20, 46, 203, 211),  # away: 203 off, 211 on
    _ev("Goal", 10, 46, 201, detail="Own Goal"),  # credited to home: away concedes
    _ev("Card", 10, 70, 104, detail="Red Card"),  # home down to 10
    _ev("Goal", 20, 90, 205, detail="Normal Goal", extra=4),  # 90+4: home concedes
    _ev("Goal", 20, 88, 206, detail="Missed Penalty"),
]


def test_stints_cut_at_own_changes_and_credit_goals_to_the_other_side() -> None:
    rows, c = st.stints(_fx(EVENTS, (2, 1)))
    home = [r for r in rows if r["team_ref"] == "10"]
    away = [r for r in rows if r["team_ref"] == "20"]
    assert [(r["start"], r["end"], r["conceded"]) for r in home] == [(0, 70, 0), (70, 90, 1)]
    assert len(home[1]["players"]) == 10 and "104" not in home[1]["players"]
    # the own goal is listed after the substitution in the same minute: the new stint
    assert [(r["start"], r["end"], r["conceded"]) for r in away] == [(0, 46, 1), (46, 90, 1)]
    assert "211" in away[1]["players"] and "203" not in away[1]["players"]
    assert c == {"unclear_sub": 0, "no_lineup": 0}
    # extra time is left out; an unfollowable substitution drops only that team
    more = [*EVENTS, _ev("Goal", 10, 105, 105, detail="Normal Goal"),
            _ev("subst", 10, 60, 150, 151)]  # fmt: skip
    rows, c = st.stints(_fx(more, (2, 1)))
    assert {r["team_ref"] for r in rows} == {"20"} and c["unclear_sub"] == 1
    assert sum(r["conceded"] for r in rows) == 2


def test_rows_drop_team_matches_that_disagree_with_the_90_minute_score() -> None:
    t = ap.Target("k", 7, "1", K, K, 1, 2)
    ids = {str(p): p for p in [*range(100, 114), *range(200, 214)]}
    rows, c = sn.rows_for(_fx(EVENTS, (2, 1)), t, ids, "39", 2022, K)
    assert c["team_matches"] == 2 and {r["team_id"] for r in rows} == {1, 2}
    assert next(r for r in rows if r["team_id"] == 2)["opp_id"] == 1
    rows, c = sn.rows_for(_fx(EVENTS, (3, 1)), t, ids, "39", 2022, K)  # away conceded 2, not 3
    assert c["goals_mismatch"] == 1 and {r["team_id"] for r in rows} == {1}


def _stints(n: int, rng: np.random.Generator) -> pl.DataFrame:
    """n matches of team 1 (home) vs 2; player 5 of team 1 plays half the matches and the
    team concedes twice as often with him on the pitch."""
    rows = []
    for m in range(n):
        with5 = m % 2 == 0
        players = [1, 2, 3, 4, 5] if with5 else [1, 2, 3, 4, 6]
        rows.append({"match_id": m, "team_id": 1, "season": 2022,
                     "kickoff": K + timedelta(days=m), "start": 0, "end": 90,
                     "conceded": int(rng.poisson(2.0 if with5 else 1.0)),
                     "players": players})  # fmt: skip
    return pl.DataFrame(rows)


def test_rating_is_the_closed_form_posterior_mean_with_k_by_marginal_likelihood() -> None:
    rng = np.random.default_rng(1)
    s = _stints(400, rng)
    lam = pl.DataFrame({"match_id": range(400), "home_id": 1, "away_id": 2,
                        "lam_home": 1.5, "lam_away": 1.0})  # fmt: skip
    expo = ed.exposure(s, lam)
    p5 = expo.filter(pl.col("player_id") == 5).row(0, named=True)
    assert p5["E"] == pytest.approx(200.0)  # 200 full matches at the opponent's 1.0
    line = pl.DataFrame({"player_id": [1, 2, 3, 4, 5, 6],
                         "line": ["G", "D", "D", "D", "D", "D"]})  # fmt: skip
    r = ed.rate(expo, line, K)
    lt = dict(r.log_theta.select("player_id", "log_theta").iter_rows())
    k = r.k["D"]
    assert lt[5] == pytest.approx(np.log((k + p5["G"]) / (k + p5["E"])))
    assert lt[5] > 0.3 and abs(lt[6]) < 0.15  # twice the goals with 5 on; as expected with 6
    d = expo.filter(pl.col("player_id") != 1)  # the D line
    best = ed.LOG_K[np.argmax(ed.marginal(d["G"].to_numpy(), d["E"].to_numpy()))]
    assert k == pytest.approx(np.exp(best)) and "D" not in r.at_bound
    # no differences between players: k runs to the grid's edge and θ to 1
    flat = pl.DataFrame({"player_id": [1, 2, 3], "G": [100.0, 100.0, 100.0],
                         "E": [100.0, 100.0, 100.0]})  # fmt: skip
    r2 = ed.rate(flat, line, K)
    assert {"G", "D"} <= set(r2.at_bound)
    assert r2.log_theta["log_theta"].abs().max() == pytest.approx(0.0)
    assert r2.fallback == 0


def test_lines_are_as_of_the_cutoff() -> None:
    app = pl.DataFrame({"player_id": [1, 1, 1, 2], "pos": ["D", "M", "M", "G"],
                        "kickoff": [K, K, K + timedelta(days=30), K]})  # fmt: skip
    got = dict(ed.lines(app, K + timedelta(days=2)).iter_rows())
    assert got == {1: "D", 2: "G"}  # a tie goes to the earlier line; the later M is not final
    assert dict(ed.lines(app, K + timedelta(days=40)).iter_rows())[1] == "M"


def test_score_r1_moves_r0_by_the_minute_weighted_lineup_and_the_gate_reads_below_zero() -> None:
    s = pl.DataFrame({"match_id": [1, 1, 1], "team_id": [1, 1, 2], "season": 2022,
                      "kickoff": K, "start": [0, 45, 0], "end": [45, 90, 90],
                      "conceded": [0, 1, 2], "players": [[5, 7], [6, 7], [8]]})  # fmt: skip
    eg = pl.DataFrame({"match_id": [1], "home_id": [1], "away_id": [2], "eg_home": [1.4],
                       "eg_away": [0.8]})  # fmt: skip
    lt = pl.DataFrame({"player_id": [5, 6], "log_theta": [0.2, -0.2]})
    got = ed.score(s, eg, lt).sort("team_id")
    home, away = got.row(0, named=True), got.row(1, named=True)
    assert home["mu0"] == pytest.approx(0.8) and away["mu0"] == pytest.approx(1.4)
    want = 0.8 * (0.5 * np.exp(0.1) + 0.5 * np.exp(-0.1))  # 7 unrated: θ = 1
    assert home["mu1"] == pytest.approx(want) and home["y"] == 1
    assert away["mu1"] == pytest.approx(1.4)  # nobody rated
    assert home["rated"] == pytest.approx(0.5)
    v = ed.verdict(pl.DataFrame({"match_id": [1, 1, 2, 2], "ll0": [1.0, 1.2, 0.9, 1.1],
                                 "ll1": [0.9, 1.1, 0.85, 1.0]}))  # fmt: skip
    assert v["passed"] and v["rel"] < 0


def test_training_rows_are_matched_to_the_fit_within_kickoff_ties() -> None:
    d = pl.DataFrame({"match_id": [1, 2, 3], "home": [10, 30, 50], "away": [20, 40, 60],
                      "kickoff": [K, K + timedelta(hours=2), K + timedelta(hours=2)]})  # fmt: skip
    got = md.align(d, np.array([10, 50, 30]), np.array([20, 60, 40]))
    assert got["match_id"].to_list() == [1, 3, 2]
    with pytest.raises(ValueError):
        md.align(d, np.array([30, 10, 50]), np.array([40, 20, 60]))  # across kickoffs
