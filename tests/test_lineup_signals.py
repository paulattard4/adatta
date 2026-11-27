"""P3-j (D65) lineup signals: usual XI, the three signals as of T-60m, the ridge path and
the step 1 slope test, on small synthetic leagues."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest

from adatta.features import lineup_signals as ls

K0 = datetime(2020, 8, 3, 15, tzinfo=UTC)  # a Monday
BUNDLE = {39: "england"}


def _league(weeks: int, swap: dict[tuple[int, int], dict[int, int]] | None = None,
            rating: float = 7.0) -> tuple[pl.DataFrame, pl.DataFrame]:  # fmt: skip
    """Two teams (1, 2) meeting weekly, alternating home. Each has players t*100 + 1..14;
    1-11 start and play 90 minutes. `swap[(week, team)]` maps a starter to a bench player
    for that match."""
    swap = swap or {}
    m, a = [], []
    for w in range(weeks):
        home, away = (1, 2) if w % 2 == 0 else (2, 1)
        k = K0 + timedelta(days=7 * w)
        m.append({"match_id": w + 1, "kickoff": k, "season": 2020, "league_id": 39,
                  "home": home, "away": away, "hg": 1, "ag": 1})  # fmt: skip
        for t in (1, 2):
            xi = [t * 100 + i for i in range(1, 12)]
            for old, new in swap.get((w, t), {}).items():
                xi[xi.index(old)] = new
            for i in range(1, 15):
                pid = t * 100 + i
                on = pid in xi
                a.append({"match_id": w + 1, "team_id": t, "player_id": pid, "kickoff": k,
                          "started": on, "pos": "G" if i == 1 else "M",
                          "minutes": 90 if on else 0,
                          "rating": rating if on else None})  # fmt: skip
    mt = pl.DataFrame(m, schema={"match_id": pl.Int64, "kickoff": pl.Datetime("us", "UTC"),
                                 "season": pl.Int64, "league_id": pl.Int64, "home": pl.Int64,
                                 "away": pl.Int64, "hg": pl.Int64, "ag": pl.Int64})  # fmt: skip
    ap = pl.DataFrame(a, schema={"match_id": pl.Int64, "team_id": pl.Int64,
                                 "player_id": pl.Int64, "kickoff": pl.Datetime("us", "UTC"),
                                 "started": pl.Boolean, "pos": pl.Utf8, "minutes": pl.Int32,
                                 "rating": pl.Float64})  # fmt: skip
    return mt, ap


def _signals(mt: pl.DataFrame, ap: pl.DataFrame) -> pl.DataFrame:
    tm = ls.team_matches(mt, BUNDLE)
    sq = ls.squads(ap, mt["match_id"])
    u = ls.usual(ls.previous(tm), sq)
    return ls.s_miss(ls.starters(sq, tm), u)


def test_s_miss_is_zero_for_the_usual_xi_and_grows_with_changes() -> None:
    mt, ap = _league(6, {(5, 1): {102: 112, 103: 113}})
    s = _signals(mt, ap).sort("match_id", "team_id")
    last = s.filter(pl.col("match_id") == 6)
    assert last.filter(pl.col("team_id") == 2)["s_miss"].item() == pytest.approx(0.0)
    assert last.filter(pl.col("team_id") == 1)["s_miss"].item() == pytest.approx(2 / 11)
    # fewer than MIN_PREV previous matches: no baseline, null (rule 8)
    assert s.filter(pl.col("match_id") <= ls.MIN_PREV)["s_miss"].null_count() == 2 * ls.MIN_PREV


def test_signals_use_only_matches_final_by_the_as_of_time() -> None:
    mt, ap = _league(5, {(4, 1): {102: 112}})
    base = _signals(mt, ap).filter(pl.col("match_id") == 5)
    assert base.filter(pl.col("team_id") == 1)["s_miss"].item() == pytest.approx(1 / 11)
    # a match kicking off inside T-60m - RESULT_LAG, with 112 starting, changes nothing
    mt2, ap2 = _league(6, {(4, 1): {102: 112}, (5, 1): {102: 112}})
    late = mt2.with_columns(
        kickoff=pl.when(pl.col("match_id") == 6)
        .then(pl.col("kickoff").filter(pl.col("match_id") == 5).first() - timedelta(hours=1))
        .otherwise(pl.col("kickoff"))
    )
    ap2 = ap2.join(late.select("match_id", k2="kickoff"), on="match_id").with_columns(
        kickoff=pl.col("k2")).drop("k2")  # fmt: skip
    got = _signals(late, ap2).filter(pl.col("match_id") == 5)
    assert got.sort("team_id")["s_miss"].to_list() == base.sort("team_id")["s_miss"].to_list()


def test_ratings_are_shrunk_to_the_line_mean_as_of_the_cut() -> None:
    mt, ap = _league(4, rating=8.0)
    sq = ls.squads(ap, mt["match_id"])
    hist = ls.rating_history(sq)
    rows = pl.DataFrame({"match_id": [4, 1], "team_id": [1, 1], "player_id": [105, 105],
                         "kickoff": [K0 + timedelta(days=21), K0], "line": ["M", "M"]},
                        schema_overrides={"kickoff": pl.Datetime("us", "UTC")})  # fmt: skip
    r = ls.ratings_asof(rows, hist, {"M": 6.5, "": 6.6}).sort("match_id")["r"].to_list()
    assert r[0] == pytest.approx(6.5)  # nothing final before the first match
    assert r[1] == pytest.approx((3 * 8.0 + 5 * 6.5) / 8)  # three matches final by then


def test_ridge_path_runs_from_least_squares_to_zero() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=(400, 5))
    beta = np.array([1.0, -0.5, 0.0, 0.25, 2.0])
    y = 0.3 + x @ beta + rng.normal(scale=0.01, size=400)
    b, c = ls.ridge_path(x, y, [1e-8, 1e9])
    assert b[0] == pytest.approx(beta, abs=0.01) and c[0] == pytest.approx(0.3, abs=0.01)
    assert np.abs(b[1]).max() < 1e-4 and c[1] == pytest.approx(y.mean(), abs=1e-3)


def test_poisson_slope_recovers_a_known_effect() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(size=20_000)
    off = np.log(rng.uniform(0.8, 2.0, size=20_000))
    y = rng.poisson(np.exp(0.05 + 0.1 * x + off)).astype(np.float64)
    b, se = ls.poisson_slope(y, off, x)
    assert abs(b - 0.1) < 3 * se and se < 0.01


def test_the_check_needs_the_expected_sign_and_an_interval_off_zero() -> None:
    rng = np.random.default_rng(3)
    n = 6000
    s = {k: rng.normal(size=n) for k in ls.SIGNALS}
    lam = 1.4 * np.exp(-0.15 * s["s_miss"] - 0.15 * s["s_apm"])  # s_apm: wrong sign
    t = pl.DataFrame({"team_id": rng.integers(0, 40, n), "season": np.full(n, 2020),
                      "goals": rng.poisson(lam), **s})  # fmt: skip
    res: dict[str, Any] = ls.check(t)
    assert res["signals"]["s_miss"]["pass"]
    assert not res["signals"]["s_apm"]["pass"]  # significant, but the wrong sign
    assert "s_apm" not in res["passed"]


def test_build_gives_all_three_signals_once_history_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ls, "APM_MIN_TRAIN", 3)
    mt, ap = _league(12, {(11, 1): {102: 112, 103: 113}})
    xg = mt.select("match_id", xg_home=pl.lit(1.5), xg_away=pl.lit(1.0))
    t, meta = ls.build(ap, mt, xg, BUNDLE, [2020], K0)
    last = t.filter(pl.col("match_id") == 12).sort("team_id")
    assert last["s_miss"].to_list() == pytest.approx([2 / 11, 0.0])
    # no warm-up rows: every line's target is the default 6.7; ten 7.0s shrink to 6.9,
    # the two new starters have no history (6.7)
    assert last["s_rat"].to_list() == pytest.approx([-2 * 0.2 / 11, 0.0])
    assert last["s_apm"].null_count() == 0 and meta["cv_n"] > 0
