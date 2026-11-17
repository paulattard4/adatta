"""P3-c (c), (d): the formation table (as-of features, coach pooling, tier), its leakage
check, the baselines, metrics, role assignment and the verdict (adatta/features/
formation.py, adatta/eval/formation.py). The NumPyro model runs in the CI model job."""

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from adatta.eval import formation as ev
from adatta.features import formation as fe

T0 = datetime(2018, 8, 4, 15, tzinfo=UTC)
VOCAB = ["4-4-2", "4-3-3", "3-5-2", fe.OTHER]


def world(extra: list[dict[str, object]] | None = None) -> fe.Inputs:
    """Team 1 plays 4-4-2 under coach a for 6 weeks, then coach b (who played 3-5-2 at
    team 3) takes over; target match 99 is in week 8 against team 2."""
    fm, co = [], []
    for w in range(8):
        f, c = ("4-4-2", "a") if w < 6 else ("3-5-2", "b")
        fm.append({"match_id": w, "team_id": 1, "kickoff": T0 + timedelta(weeks=w), "formation": f})
        co.append({"match_id": w, "team_id": 1, "coach_ref": c})
        fm.append({"match_id": 50 + w, "team_id": 3, "kickoff": T0 - timedelta(weeks=20 - w),
                   "formation": "3-5-2" if w < 4 else "4-2-2-2"})  # fmt: skip
        co.append({"match_id": 50 + w, "team_id": 3, "coach_ref": "b"})
    ko = T0 + timedelta(weeks=8)
    fm += [{"match_id": 99, "team_id": 1, "kickoff": ko, "formation": "3-5-2"},
           {"match_id": 99, "team_id": 2, "kickoff": ko, "formation": "4-3-3"}]  # fmt: skip
    fm += extra or []
    schema = {"kickoff": pl.Datetime("us", "UTC")}
    ctx = pl.DataFrame({"match_id": [99], "home_importance": ["must_win"],
                        "away_importance": ["live"]})  # fmt: skip
    tier = pl.DataFrame({"match_id": [99], "as_of": [ko - timedelta(days=3)],
                         "p_home": [0.6], "p_away": [0.2]},
                        schema_overrides={"as_of": pl.Datetime("us", "UTC")})  # fmt: skip
    targets = pl.DataFrame({"match_id": [99], "league_ref": ["39"], "season": [2018],
                            "kickoff": [ko], "home_team_id": [1], "away_team_id": [2]},
                           schema_overrides=schema)  # fmt: skip
    return fe.Inputs(pl.DataFrame(fm, schema_overrides=schema), pl.DataFrame(co), ctx, tier,
                     targets)  # fmt: skip


def test_features_read_the_team_the_coach_and_the_tier_as_of() -> None:
    t = fe.build(world(), VOCAB)
    home = t.filter(pl.col("team_id") == 1).row(0, named=True)
    assert home["y"] == 2 and home["home"] == 1.0 and home["must_win"] == 1.0
    assert home["tier"] == pytest.approx(0.4) and home["tier_known"] == 1.0
    assert home["last_2"] == 1.0 and home["tenure"] == 2.0 and home["new_coach_5"] == 1.0
    # team shares: 6 x 4-4-2 then 2 x 3-5-2, recency-weighted
    w = fe.TEAM_DECAY ** np.arange(8)[::-1]
    assert home["ts_2"] == pytest.approx(w[-2:].sum() / w.sum())
    # coach b: 4 x 3-5-2 and 4 x other at team 3, then 2 x 3-5-2 at team 1
    assert home["coach_n"] == 10.0 and home["cm_2"] > home["ts_2"]
    away = t.filter(pl.col("team_id") == 2).row(0, named=True)
    assert away["tier"] == pytest.approx(-0.4) and away["log_team_n"] == 0.0
    assert sum(away[f"ts_{i}"] for i in range(4)) == pytest.approx(1.0)  # no history: past shares


def test_a_late_tier_prediction_is_not_used() -> None:
    inp = world()
    late = inp.tier.with_columns(as_of=pl.col("as_of") + timedelta(days=2, hours=23))
    t = fe.build(fe.Inputs(inp.formations, inp.coaches, inp.context, late, inp.targets), VOCAB)
    assert (t["tier"] == 0).all() and (t["tier_known"] == 0).all()


def test_formations_after_the_cutoff_change_nothing() -> None:
    late = T0 + timedelta(weeks=8) - timedelta(hours=20)  # inside the 24 h before kickoff
    extra = [{"match_id": 77, "team_id": 1, "kickoff": late, "formation": "4-3-3"}]
    a = fe.build(world(), VOCAB)
    b = fe.build(world(extra), VOCAB)
    assert a.equals(b)
    inp = world(extra)
    assert ev.leakage_check(inp, fe.build(inp, VOCAB), VOCAB)["leakage_ok"]


def table(n: int = 300, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        y = int(rng.integers(0, 4))
        last = np.eye(4)[y if rng.random() < 0.6 else int(rng.integers(0, 4))]
        ts = 0.5 * last + 0.5 * np.full(4, 0.25)
        rows.append({"match_id": i, "team_id": 1, "league_ref": "39" if i % 2 else "40",
                     "season": 2016 + i % 8, "y": y, "tier": 0.0, "new_coach_5": 0.0,
                     **{f"ts_{j}": ts[j] for j in range(4)},
                     **{f"cm_{j}": ts[j] for j in range(4)},
                     **{f"last_{j}": last[j] for j in range(4)}})  # fmt: skip
    return pl.DataFrame(rows)


def test_baselines_are_warm_up_only_and_normalised() -> None:
    t = table()
    b = ev.fit_baselines(t.filter(pl.col("season") <= 2018), 4)
    assert 0 < b.w_last < 1 and 0 < b.w_ew < 1
    with pytest.raises(ValueError):
        ev.fit_baselines(t, 4)
    for p in ev.baseline_probs(b, t, 4).values():
        assert np.allclose(p.sum(axis=1), 1.0)


def test_metrics_and_family() -> None:
    t = table(8)
    p = np.tile([0.7, 0.1, 0.1, 0.1], (8, 1))
    s = ev.score(t, p, VOCAB)
    fam = ev.family(VOCAB)
    assert fam.tolist() == [4, 4, 3, 0]
    row0 = s.row(0, named=True)
    y0 = t["y"][0]
    assert row0["log_loss"] == pytest.approx(-np.log(p[0, y0]))
    want_fam = 0.8 if y0 in (0, 1) else 0.1
    assert row0["fam_log_loss"] == pytest.approx(-np.log(want_fam))
    assert 0 <= ev.ece(s) <= 1


def lineup(match: int, team: int, ko: datetime, swap: bool) -> list[dict[str, object]]:
    """4-4-2; players 2 (LB) and 5 (RB). `swap` puts 5 at left-back that match."""
    slots = ["GK", "LB", "LCB", "RCB", "RB", "LM", "CM", "CM", "RM", "ST", "ST"]
    pos = ["G", "D", "D", "D", "D", "M", "M", "M", "M", "F", "F"]
    players = list(range(1, 12))
    if swap:
        players[1], players[4] = players[4], players[1]
    return [{"match_id": match, "team_id": team, "player_id": p, "league_ref": "39",
             "season": 2022, "kickoff": ko, "pos": ps, "slot": s, "positional": True}
            for p, ps, s in zip(players, pos, slots, strict=True)]  # fmt: skip


def test_role_assignment_uses_the_players_own_history() -> None:
    T1 = datetime(2022, 8, 6, 15, tzinfo=UTC)
    rows = []
    for w in range(6):
        rows += lineup(w, 1, T1 + timedelta(weeks=w), swap=False)
    rows += lineup(6, 1, T1 + timedelta(weeks=6), swap=False)
    lineups = pl.DataFrame(rows, schema_overrides={"kickoff": pl.Datetime("us", "UTC")})
    targets = pl.DataFrame({"match_id": [6], "team_id": [1]})
    r = ev.role_assignment(lineups, targets).row(0, named=True)
    assert r["acc_model"] == 1.0  # history settles who is left and who is right
    assert r["acc_R1"] <= 1.0
    s = ev.role_summary(ev.role_assignment(lineups, targets))
    assert s["team_matches"] == 1.0 and s["acc_model"] == 1.0


def test_verdict_needs_every_check() -> None:
    rng = np.random.default_rng(0)
    rows = []
    for m in range(400):
        for model, good in (("model", 0.6), ("F1", 0.4), ("F2", 0.5)):
            rows.append({"model": model, "match_id": m, "team_id": 1,
                         "league_ref": "39" if m % 2 else "40",
                         "log_loss": -np.log(good) + rng.normal(0, 0.01),
                         "fam_log_loss": -np.log(good + 0.2), "p_top": 0.6,
                         "top_correct": m % 5 < 3, "tier": 0.0,
                         "new_coach_5": 0.0})  # fmt: skip
    scored = pl.DataFrame(rows)
    meta = {"checks_a": [{"passed": True}], "checks_b": {"passed": True}, "leakage_ok": True,
            "role": {"diff": 0.1, "lo": 0.05, "hi": 0.15, "team_matches": 10.0,
                     "acc_model": 0.8, "acc_R1": 0.7, "sparse_players": 3.0,
                     "sparse_acc_model": 0.5, "sparse_acc_R1": 0.4}, "vocab": VOCAB}  # fmt: skip
    v = ev.verdict(scored, meta, "F2", inference_ok=True, vocab=VOCAB)
    assert v.passed, [c for c in v.checks if not c["pass"]]
    bad = ev.verdict(scored, {**meta, "checks_b": {"passed": False}}, "F2", True, VOCAB)
    assert not bad.passed
    text = ev.markdown(scored, v, [{"fit_s": 30.0, "gates_ok": True, "ppc_q": 0.5}], meta, "d")
    assert "**Verdict: pass**" in text and "Role assignment" in text
