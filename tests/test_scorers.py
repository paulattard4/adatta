"""P3-e scorers: the player table (features.scorers) and the allocation, baselines and pass
rule (eval.scorers)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest
from scipy.stats import poisson

from adatta.eval import scorers as es
from adatta.features import scorers as fs

T0 = datetime(2019, 8, 3, 15, tzinfo=UTC)
LEAGUES = ("39", "40")
RATE = {"G": 0.01, "D": 0.05, "M": 0.10, "F": 0.40}  # goals per 90 at theta = 1
ARATE = {"G": 0.01, "D": 0.04, "M": 0.08, "F": 0.10}  # assists per 90


def _posts(rate: dict[str, float], n: int = 4) -> dict[datetime, dict[str, np.ndarray]]:
    return {datetime(2018, 7, 1, tzinfo=UTC): {
        "log_mu": np.tile(np.log([rate[x] for x in "GDMF"]), (n, 1)),
        "log_l": np.zeros((n, 1)), "b": np.zeros((n, 3)), "log_k": np.full((n, 4), np.log(5.0)),
    }}  # fmt: skip


POSTS = {"goals": _posts(RATE), "assists": _posts(ARATE)}
SQUAD = [(1, "G", True), *[(p, "D", True) for p in range(2, 6)],
         *[(p, "M", True) for p in range(6, 10)], (10, "F", True), (11, "F", True),
         (12, "F", False)]  # fmt: skip


def _app(n: int = 6) -> pl.DataFrame:
    """Team 1's matches a week apart: the starters play 90, the bench F (12) never plays.
    Player 10 scores once in every match (a penalty in the last), 11 assists it, and
    defender 2 scores an own goal in the first."""
    rows = []
    for m in range(n):
        for pid, pos, st in SQUAD:
            rows.append({
                "match_id": 100 + m, "team_id": 1, "player_id": pid, "league_ref": "39",
                "season": 2019, "kickoff": T0 + timedelta(days=7 * m), "started": st,
                "pos": pos, "minutes": 90 if st else 0, "goals": int(pid == 10),
                "own_goals": int(pid == 2 and m == 0), "assists": int(pid == 11),
                "pen_scored": int(pid == 10 and m == n - 1),
            })  # fmt: skip
    ints = {c: pl.Int32 for c in ("minutes", "goals", "own_goals", "assists", "pen_scored")}
    return pl.DataFrame(rows, schema_overrides=ints)


def _hist(app: pl.DataFrame) -> pl.DataFrame:
    """The counts table of the played rows, plus one unrelated forward in 2018 (so R0 has a
    window). Team 1's players have no history before T0."""
    played = app.filter(pl.col("minutes") > 0)
    t = played.select(
        "match_id", "team_id", "player_id", "league_ref", "season", "kickoff",
        sub=~pl.col("started"), e=pl.col("minutes").cast(pl.Float64) / 90, line="pos",
        new_club=pl.lit(False), ret=pl.lit(False), slot=pl.lit(None, pl.Utf8),
        y_goals="goals", y_assists="assists",
    )  # fmt: skip
    old = pl.DataFrame({
        "match_id": [1], "team_id": [99], "player_id": [9999], "league_ref": ["39"],
        "season": [2018], "kickoff": [T0 - timedelta(days=400)], "sub": [False], "e": [2.0],
        "line": ["F"], "new_club": [False], "ret": [False], "slot": [None],
        "y_goals": [1], "y_assists": [1],
    }, schema_overrides={"slot": pl.Utf8, "y_goals": pl.Int32, "y_assists": pl.Int32})  # fmt: skip
    return pl.concat([old, t], how="vertical_relaxed")


def _export(match: int) -> pl.DataFrame:
    return pl.DataFrame([
        {"match_id": match, "team_id": 1, "player_id": p, "pos": pos, "model": "model",
         "p_start": 0.9 if st else 0.1, "p_bench": 0.1 if st else 0.8,
         "emin_start": 80.0, "emin_bench": 20.0}
        for p, pos, st in SQUAD
    ])  # fmt: skip


def _build(app: pl.DataFrame, match: int = 105, **kw: object) -> pl.DataFrame:
    key = pl.DataFrame({"match_id": [match], "team_id": [1]})
    return fs.build(app, _hist(app), POSTS, LEAGUES, _export(match), key, **kw)  # type: ignore[arg-type]


def test_player_table_minutes_rates_outcomes_and_shares() -> None:
    t = _build(_app()).sort("player_id")
    assert t.height == len(SQUAD) and t["in_squad"].all() and t["in_pool"].all()
    r10, r12 = t.row(9, named=True), t.row(11, named=True)
    assert r10["e_lk"] == pytest.approx(80 / 90) and r12["e_lk"] == pytest.approx(20 / 90)
    assert r10["e24_start"] == pytest.approx(0.9 * 80 / 90)
    assert r12["e24_bench"] == pytest.approx(0.8 * 20 / 90)
    # 12 never played: quality is the line rate, R1 is R0
    assert r12["q_goals_start"] == pytest.approx(RATE["F"])
    assert r12["q_assists_start"] == pytest.approx(ARATE["F"])
    assert r12["r0_goals"] == pytest.approx(0.5)  # the 2018 forward: 1 goal in 2.0 x 90
    assert r12["r1_goals"] == pytest.approx(r12["r0_goals"])
    # 10 scored in each of matches 100-104: Gamma(k + 5, k + 5 x 0.4), k = 5
    assert r10["q_goals_start"] == pytest.approx(RATE["F"] * 10 / 7)
    assert r10["r1_goals"] == pytest.approx((5 + fs.C["goals"] * 0.5) / (5 + fs.C["goals"]))
    # outcomes: the scorer, a bench player who never came on (0), a penalty
    assert (r10["y_goals"], r10["y_pen"]) == (1, 1)
    assert r12["y_goals"] == 0 and r12["y_assists"] == 0
    # league shares over matches 100-104: 5 player goals, 1 own goal, 5 assists
    assert r10["o"] == pytest.approx(1 / 6) and r10["alpha"] == pytest.approx(1.0)


def test_player_table_ignores_the_match_and_later_rows() -> None:
    app = _app(8)
    base = _build(app).sort("player_id")
    own = pl.col("match_id") >= 105
    cut = pl.concat([
        app.filter(~own),
        app.filter(pl.col("match_id") == 105).with_columns(
            [pl.lit(None, pl.Int32).alias(c) for c in ("minutes", "goals", "own_goals",
                                                         "assists", "pen_scored")]),
    ])  # fmt: skip
    re_ = _build(cut).sort("player_id")
    for c in ("e_lk", "e24_start", "q_goals_start", "q_assists_sub", "r0_goals", "r1_assists",
              "o", "alpha"):  # fmt: skip
        assert re_[c].to_list() == pytest.approx(base[c].to_list()), c


def test_under_18_flag() -> None:
    born = [T0 - timedelta(days=365 * 17), T0 - timedelta(days=365 * 25)]
    birth = pl.DataFrame({"player_id": [12, 10], "birth_date": born},
                         schema_overrides={"birth_date": pl.Datetime("us", "UTC")})  # fmt: skip
    t = _build(_app(), birth=birth).sort("player_id")
    assert t.filter(pl.col("u18"))["player_id"].to_list() == [12]


# ----------------------------------------------------------------------------- eval


def test_thinning_is_binomial_given_n_and_poisson_for_poisson_n() -> None:
    k = np.arange(40)
    lam, p = 1.6, np.array([0.0, 0.1, 0.35, 1.0])
    pmf = np.tile(poisson.pmf(k, lam), (len(p), 1))
    got = es.thinned(pmf, p)
    for i, pi in enumerate(p):
        want = poisson.pmf(np.arange(3), lam * pi)
        assert got[i, :3] == pytest.approx(want, abs=1e-10)
    assert got.sum(axis=1) == pytest.approx(np.ones(len(p)))
    point = np.zeros((1, 6))
    point[0, 3] = 1.0
    assert es.thinned(point, np.array([0.2]))[0, 0] == pytest.approx(0.8**3)


def _table() -> tuple[pl.DataFrame, pl.DataFrame]:
    rows = []
    for m in range(40):
        for team in (1, 2):
            for p in range(14):
                started = p < 11
                line = "G" if p == 0 else ("D" if p < 5 else ("M" if p < 9 else "F"))
                rows.append({
                    "match_id": m, "team_id": team, "player_id": 100 * team + p,
                    "league_ref": "39" if m % 2 else "40", "season": 2019, "line": line,
                    "started": started, "in_squad": True, "in_pool": p < 13, "u18": False,
                    "e_lk": (85.0 if started else 15.0) / 90 if p != 13 else 0.0,
                    "e24_start": 0.8 * 85 / 90, "e24_bench": 0.2 * 15 / 90,
                    "q_goals_start": RATE[line], "q_goals_sub": RATE[line] * 1.3,
                    "q_assists_start": ARATE[line], "q_assists_sub": ARATE[line],
                    "r0_goals": 0.1, "r1_goals": 0.1 + 0.01 * (p % 3),
                    "r0_assists": 0.07, "r1_assists": 0.07,
                    "y_goals": int((m + p) % 17 == 0), "y_assists": int((m + p) % 23 == 0),
                    "y_pen": 0, "o": 0.03, "alpha": 0.7,
                })  # fmt: skip
    t = pl.DataFrame(rows, schema_overrides={"y_goals": pl.Int32, "y_assists": pl.Int32})
    k = np.arange(11)
    pm = [{"match_id": m, "team_id": team, "pmf": list(poisson.pmf(k, 1.2 + 0.3 * team)
                                                       / poisson.pmf(k, 1.2 + 0.3 * team).sum())}
          for m in range(40) for team in (1, 2)]  # fmt: skip
    return t, pl.DataFrame(pm)


def test_allocation_sums_to_the_team_rate() -> None:
    t, pmfs = _table()
    gaps = es.consistency(t, pmfs)
    assert gaps["goals"] < 1e-12 and gaps["assists"] < 1e-12
    s = es.score(t, pmfs, "lk")
    m = s.filter((pl.col("model") == "M") & (pl.col("stat") == "goals"))
    per = m.group_by("match_id", "team_id").agg(pl.col("mean").sum(), pl.col("team_en").first())
    assert per["mean"].to_numpy() == pytest.approx(0.97 * per["team_en"].to_numpy())
    b0 = s.filter((pl.col("model") == "B0") & (pl.col("stat") == "goals")).sort("player_id")
    assert b0.filter(pl.col("player_id") == 110)["mean"][0] == pytest.approx(85 / 90 * 0.40)
    # the zero-weight row is floored the same in every model
    z = s.filter((pl.col("player_id") == 113) & (pl.col("stat") == "goals"))
    assert z["zero_w"].all() and z["log_loss"].n_unique() <= 2


def test_t24_population_is_the_pool() -> None:
    t, pmfs = _table()
    s = es.score(t, pmfs, "24")
    assert 113 not in s["player_id"].to_list() and 213 not in s["player_id"].to_list()


def _scored(m_better: bool, seed: int = 1, n: int = 20_000) -> pl.DataFrame:
    """Binary outcomes with true probability p. The baselines predict a blur of p; M
    predicts p itself (m_better) or an overconfident 1.8 p."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.02, 0.4, n)
    y = (rng.uniform(size=n) < p).astype(int)
    lg = np.where(np.arange(n) % 2 == 0, "39", "40")
    out = []
    for stat in es.STATS:
        for model in es.MODELS:
            q = 0.5 * p + 0.5 * p.mean()
            if model == "M":
                q = p if m_better else np.clip(1.8 * p, 1e-4, 0.99)
            ll = -np.where(y >= 1, np.log(q), np.log(1 - q))
            out.append(pl.DataFrame({
                "match_id": np.arange(n), "team_id": 1, "player_id": np.arange(n),
                "league_ref": lg, "model": model, "stat": stat, "horizon": "lk", "y": y,
                "p1": q, "mean": q, "log_loss": ll, "count_log_loss": ll,
            }))  # fmt: skip
    return pl.concat(out)


def test_verdict_passes_a_better_model_and_fails_a_worse_one() -> None:
    gaps = {"goals": 0.0, "assists": 0.0}
    good = es.verdict(_scored(True), gaps, leakage_ok=True)
    assert good.passed == {"goals": True, "assists": True}, [
        c for c in good.checks if not c["pass"]
    ]
    bad = es.verdict(_scored(False), gaps, leakage_ok=True)
    assert bad.passed == {"goals": False, "assists": False}
    leak = es.verdict(_scored(True), gaps, leakage_ok=False)
    assert not any(leak.passed.values())
