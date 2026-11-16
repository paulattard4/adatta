"""P3-b (docs/PHASE3_TASKS.md): the selection table (pool, covariates, outcome, no leakage),
coherent team probabilities, the baselines, the minutes pmf and the metrics. The JAX model is
exercised on CI (model group)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from adatta.eval import selection as ev
from adatta.features import selection as sel

T0 = datetime(2019, 8, 3, 15, tzinfo=UTC)
TEAM, OTHER, OPP = 1, 2, 3


def app_rows(match: int, team: int, ko: datetime, starters: list[int], bench: list[int],
             minutes: dict[int, float] | None = None, sent: set[int] | None = None,
             pos: str = "M") -> list[dict[str, object]]:  # fmt: skip
    minutes = minutes or {}
    out = []
    for p in starters + bench:
        st = p in starters
        out.append({"match_id": match, "team_id": team, "player_id": p, "kickoff": ko,
                    "started": st, "minutes_nt": minutes.get(p, 90.0 if st else 0.0),
                    "sent_off": p in (sent or set()), "pos": "G" if p == 1 else pos})  # fmt: skip
    return out


XI = list(range(1, 12))


def world(extra: list[dict[str, object]] | None = None) -> sel.Inputs:
    rows: list[dict[str, object]] = []
    for w in range(6):  # six weekly matches before the target
        bench = [12, 13] if w < 4 else [12]
        starters = XI if w != 5 else [*XI[:10], 13]  # 11 dropped, 13 starts last match
        rows += app_rows(100 + w, TEAM, T0 + timedelta(weeks=w), starters, bench,
                         sent={5} if w == 5 else None)  # fmt: skip
    rows += app_rows(100, TEAM, T0, [], [20])  # 20 on the bench in the first match only
    rows += app_rows(300, OTHER, T0 + timedelta(weeks=2), [20], [])  # then plays elsewhere
    target_ko = T0 + timedelta(weeks=6)
    rows += app_rows(200, TEAM, target_ko, [*XI[:4], *XI[5:], 13], [12, 30])  # 5 out, 30 new
    rows += app_rows(200, OPP, target_ko, list(range(41, 52)), [])
    rows += extra or []
    app = pl.DataFrame(rows, schema_overrides={"kickoff": pl.Datetime("us", "UTC"),
                                               "minutes_nt": pl.Float64})  # fmt: skip
    coaches = pl.DataFrame({"match_id": [100, 101, 102, 103, 104, 105],
                            "team_id": [TEAM] * 6, "coach_ref": ["a"] * 5 + ["b"]})  # fmt: skip
    ctx = pl.DataFrame({
        "match_id": [200], "home_days_since_last": [7.0], "away_days_since_last": [7.0],
        "home_days_to_next": [3.0], "away_days_to_next": [7.0], "home_next_kind": ["cup"],
        "away_next_kind": ["league"], "home_importance": ["must_win"],
        "away_importance": ["live"],
    })  # fmt: skip
    targets = pl.DataFrame({"match_id": [200], "league_ref": ["39"], "season": [2019],
                            "kickoff": [target_ko], "home_team_id": [TEAM],
                            "away_team_id": [OPP]},
                           schema_overrides={"kickoff": pl.Datetime("us", "UTC")})  # fmt: skip
    return sel.Inputs(app, coaches, ctx, targets)


def team_rows(players: pl.DataFrame) -> dict[int, dict[str, object]]:
    return {
        r["player_id"]: r for r in players.filter(pl.col("team_id") == TEAM).iter_rows(named=True)
    }


def test_pool_outcome_and_covariates() -> None:
    players, teams = sel.build(world())
    r = team_rows(players)
    assert set(r) == set(range(1, 14))  # 20 left for another club; 30 is new
    assert r[5]["y"] == sel.OUT and r[13]["y"] == sel.START and r[12]["y"] == sel.BENCH
    assert r[13]["last_start"] == 1 and r[11]["last_start"] == 0 and r[11]["last_bench"] == 0
    assert r[11]["out_streak"] == 1 and r[1]["out_streak"] == 0
    assert r[5]["sent_off_last"] == 1
    assert r[1]["coach_changed"] == 0  # started under b in the last match
    assert r[11]["coach_changed"] == 1  # last start under a, the team's last coach is b
    assert r[1]["pos"] == "G" and r[2]["pos"] == "M"
    assert r[1]["next_cup"] == 1 and r[1]["must_win"] == 1 and r[1]["rest_days"] == 7.0
    w = sel.EW_DECAY ** np.arange(6)
    assert r[13]["ew_start"] == pytest.approx(w[0] / w.sum())
    t = teams.filter(pl.col("team_id") == TEAM).row(0, named=True)
    assert (t["new_start"], t["new_bench"], t["pool"], t["starters"]) == (0, 1, 13, 11)


def test_a_return_after_a_gap_is_measured() -> None:
    players, _ = sel.build(world())
    r = team_rows(players)
    # 13: bench, bench, bench, bench, out, start -> returned after a gap of 1
    assert r[13]["returned_gap"] == pytest.approx(np.log1p(1))
    assert r[2]["returned_gap"] == 0


def test_appearances_after_the_cutoff_change_nothing() -> None:
    late = T0 + timedelta(weeks=6) - timedelta(hours=10)  # inside the 24 h before kickoff
    extra = app_rows(400, TEAM, late, [*XI[:10], 12], [13]) + app_rows(401, OTHER, late, [2], [])
    cols = ["player_id", *sel.FEATURES, "pos", "y"]
    a, _ = sel.build(world())
    b, _ = sel.build(world(extra))
    pick = (pl.col("team_id") == TEAM) & (pl.col("match_id") == 200)
    assert (
        a.filter(pick)
        .select(cols)
        .sort("player_id")
        .equals(b.filter(pick).select(cols).sort("player_id"))
    )


def test_waterfill_and_coherent_team_totals() -> None:
    p = ev.waterfill(np.array([5.0, 1.0, 1.0, 1.0]), 2.0, np.ones(4))
    assert p.sum() == pytest.approx(2.0) and p[0] == 1.0
    raw = np.tile([0.6, 0.2, 0.2], (20, 1))
    raw[:3, 0] = 0.9
    gk = np.zeros(20, bool)
    gk[[0, 1]] = True
    c = ev.coherent(raw, gk, 11.0, 7.0)
    assert c[:, 0].sum() == pytest.approx(11.0, abs=1e-4)
    assert c[gk, 0].sum() == pytest.approx(1.0, abs=1e-4)
    assert c[:, 1].sum() == pytest.approx(7.0, abs=1e-4)
    assert np.allclose(c.sum(axis=1), 1.0) and (c > 0).all()


def test_baselines_are_warm_up_only_and_normalised() -> None:
    players, teams = sel.build(world())
    warm = players.with_columns(season=pl.lit(2017))
    b = ev.fit_baselines(warm)
    assert 0.0 <= b.lam <= 1.0
    with pytest.raises(ValueError):
        ev.fit_baselines(players)  # 2019 is a dev season
    raw = ev.baseline_raw(b, warm)
    for r in raw.values():
        assert r.shape == (len(warm), 3)
    p = ev.coherent_all(warm, raw["B2"], teams, {"39": (0.0, 0.0)})
    t = warm.with_row_index("i").filter(pl.col("team_id") == TEAM)["i"].to_numpy()
    assert p[t, 0].sum() == pytest.approx(11.0, abs=1e-4)
    mins = ev.baseline_minutes(b, warm)
    assert np.allclose(mins.sum(axis=1), 1.0)


def test_minutes_pmf_and_crps() -> None:
    p = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.2, 0.5, 0.3]])
    sb = ev.beta_grid(np.array([0.7] * 3), np.array([5.0] * 3))
    pmf = ev.minutes_pmf(p, np.array([1.0, 0.5, 0.5]), sb, np.array([0.5] * 3), sb)
    assert np.allclose(pmf.sum(axis=1), 1.0)
    assert pmf[0, 90] == pytest.approx(1.0) and pmf[1, 0] == pytest.approx(1.0)
    rows = pl.DataFrame({"match_id": [1, 1, 1], "team_id": [1, 1, 1], "player_id": [1, 2, 3],
                         "league_ref": ["39"] * 3, "season": [2019] * 3, "pos": ["M"] * 3,
                         "y": [sel.START, sel.OUT, sel.BENCH], "minutes_nt": [90.0, 0.0, 20.0],
                         "log_club_matches": [3.0] * 3, "returned_gap": [0.0] * 3})  # fmt: skip
    s = ev.score(rows, np.clip(p, 1e-6, 1), pmf)
    assert s["crps"][0] == pytest.approx(0.0) and s["crps"][1] == pytest.approx(0.0)
    assert s["crps"][2] > 0 and s["log_loss"][0] == pytest.approx(0.0, abs=1e-5)


def test_verdict_needs_every_check() -> None:
    rng = np.random.default_rng(0)
    rows = []
    for m in range(200):
        for pl_id in range(15):
            start = pl_id < 11
            for model, good in (("model", 0.9), ("B1", 0.7), ("B2", 0.8)):
                p = good if start else 1 - good
                rows.append({"model": model, "match_id": m, "team_id": 1, "player_id": pl_id,
                             "league_ref": "39" if m % 2 else "40", "p_start": p * 11 / 11,
                             "start": start, "log_loss": -np.log(good) + rng.normal(0, 0.01),
                             "crps": (1 - good) * 10})  # fmt: skip
    scored = pl.DataFrame(rows)
    v = ev.verdict(scored, "B2", inference_ok=True, leakage_ok=True)
    names = {c["check"]: c["pass"] for c in v.checks}
    assert names["log loss vs B1"] and names["log loss vs B2"] and names["minutes CRPS vs B2"]
    assert not names["ECE overall"]  # 0.9 / 0.1 against outcomes of 1 / 0
    assert not v.passed
    assert not ev.verdict(scored, "B2", inference_ok=False, leakage_ok=True).checks[-2]["pass"]
