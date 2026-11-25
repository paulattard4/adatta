"""C2 step 2 data scope (docs/BEYOND_LEAGUES_PLAN.md "C2 step 2 acceptance" (a)-(c), D52):
round classes, the cross-tier and tie rules, top-flight targets, second tiers observable
only after the summer load, the five bundles untouched, the holdout keys kept apart."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
import yaml

from adatta.adapters.storage import LocalStore
from adatta.eval import c2, harness
from adatta.model import backtest as bt
from adatta.model import team_strength as ts

C2L = [94, 95]


def matches(seasons: tuple[int, ...] = (2017, 2018, 2019)) -> pl.DataFrame:
    """Two linked tiers, ten sides each, as tests/test_backtest.py."""
    rng = np.random.default_rng(0)
    rows, mid = [], 0
    for season in seasons:
        start = datetime(season, 8, 7, 15, tzinfo=UTC)
        for week in range(36):
            for lg, base in ((39, 0), (40, 10)):
                teams = rng.permutation(np.arange(base, base + 10))
                for i in range(0, 10, 2):
                    mid += 1
                    rows.append({
                        "match_id": mid, "league_id": lg, "season": season,
                        "kickoff": start + timedelta(days=7 * week, hours=i),
                        "home": int(teams[i]) + 1000, "away": int(teams[i + 1]) + 1000,
                        "round": "Regular Season - 1",
                        "hg": int(rng.poisson(1.5)), "ag": int(rng.poisson(1.1)),
                    })  # fmt: skip
    return pl.DataFrame(rows)


class FakeFitter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, fd: ts.FitData, seed: int) -> tuple[bt.Draws, bt.Diag]:
        self.calls += 1
        n, d = len(fd.team_ids), 20
        rng = np.random.default_rng(seed)
        draws = {
            "attack_last": rng.normal(0, 0.1, (n, d)), "defence_last": rng.normal(0, 0.1, (n, d)),
            "mu_league": np.full((2, d), 0.2), "h_league": np.full((2, d), 0.25),
            "h_team": np.zeros((n, d)), "sigma_home": np.full(d, 0.05), "rho": np.zeros((2, d)),
            "kappa": np.zeros((2, d)),
        }  # fmt: skip
        return draws, {"rhat_max": 1.001, "ess_bulk_min": 900.0, "divergences": 0.0}


def config() -> harness.EvalConfig:
    return harness.load_config().model_copy(update={"max_goals": 8})


def c2cfg() -> c2.C2Config:
    return c2.C2Config(
        second_tier_available="07-01",
        bundles={"portugal": C2L},
        rounds={1: {"Regular Season": "regular", "Championship Round": "phase",
                    "Relegation Round": "phase", "Final": "excluded"}},
    )  # fmt: skip


def row(
    mid: int, home: int, away: int, rnd: str, lg: int = 1, season: int = 2020
) -> dict[str, Any]:
    return {"match_id": mid, "league_id": lg, "season": season,
            "kickoff": datetime(2021, 1, 1, tzinfo=UTC) + timedelta(days=mid),
            "home": home, "away": away, "round": rnd, "hg": 1, "ag": 0}  # fmt: skip


def frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema={
        "match_id": pl.Int64, "league_id": pl.Int64, "season": pl.Int64,
        "kickoff": pl.Datetime("us", "UTC"), "home": pl.Int64, "away": pl.Int64,
        "round": pl.Utf8, "hg": pl.Int64, "ag": pl.Int64,
    })  # fmt: skip


def scoped_rows() -> pl.DataFrame:
    rows, mid = [], 0
    teams = list(range(1, 9))
    for h in teams:  # regular season: 8 sides, double round robin
        for a in teams:
            if h != a:
                mid += 1
                rows.append(row(mid, h, a, f"Regular Season - {mid % 30 + 1}"))
    for h in (1, 2, 3, 4):  # a championship group of four
        for a in (1, 2, 3, 4):
            if h != a:
                mid += 1
                rows.append(row(mid, h, a, "Championship Round - 1"))
    for h, a in ((7, 99), (99, 7)):  # a play-off against an outside (lower-tier) side
        mid += 1
        rows.append(row(mid, h, a, "Relegation Round - 1"))
    for h, a in ((5, 6), (6, 5)):  # a two-side tie under a group label
        mid += 1
        rows.append(row(mid, h, a, "Relegation Round - 2"))
    mid += 1
    rows.append(row(mid, 1, 2, "Final"))
    return frame(rows)


def test_family_strips_the_round_number_only() -> None:
    assert c2.family("Regular Season - 12") == "Regular Season"
    assert c2.family("Europa League Play-offs - Final") == "Europa League Play-offs - Final"
    assert c2.family("1st Phase - 3") == "1st Phase"


def test_scope_keeps_regular_and_phase_drops_cross_tier_ties_and_knockouts() -> None:
    kept, fates = c2.scope(scoped_rows(), c2cfg())
    counts = dict(fates.group_by("fate").len().rows())
    assert counts == {"regular": 56, "phase": 12, "cross-tier": 2, "tie": 2, "excluded": 1}
    assert kept.height == 68
    assert kept["phase"].sum() == 12
    assert 99 not in set(kept["home"]) | set(kept["away"])


def test_an_unclassed_round_label_fails_loading() -> None:
    df = frame([row(1, 1, 2, "Regular Season - 1"), row(2, 2, 1, "Mystery Round - 1")])
    with pytest.raises(c2.UnclassedRound, match="Mystery Round"):
        c2.scope(df, c2cfg())


def test_annotate_targets_top_flight_and_delays_second_tiers_to_the_summer_load() -> None:
    k = datetime(2021, 3, 6, 15, tzinfo=UTC)
    df = frame([row(1, 1, 2, "Regular Season - 1", lg=94), row(2, 3, 4, "Regular Season - 1",
                                                                lg=95)])  # fmt: skip
    df = df.with_columns(kickoff=pl.lit(k).cast(pl.Datetime("us", "UTC")))
    out = c2.annotate(df, C2L, c2cfg(), 3.0)
    assert out["target"].to_list() == [True, False]
    assert out["available_at"].to_list() == [k + timedelta(hours=3),
                                             datetime(2021, 7, 1, tzinfo=UTC)]  # fmt: skip
    assert c2.observable(out, datetime(2021, 6, 30, tzinfo=UTC)).height == 1
    assert c2.observable(out, datetime(2021, 7, 1, tzinfo=UTC)).height == 2
    assert c2.observable(df, k) is df  # the five: no column, nothing filtered


def c2_matches() -> pl.DataFrame:
    df = matches((2017, 2018, 2019)).with_columns(
        league_id=pl.col("league_id").replace_strict({39: 94, 40: 95})
    )
    return c2.annotate(df, C2L, c2cfg(), config().result_available_after_hours)


def test_plan_targets_top_flight_and_trains_on_second_tiers_only_once_loaded() -> None:
    df, cfg = c2_matches(), config()
    plan = harness.refit_plan(df, cfg, [2019], 7)
    assert plan
    lg, season = df["league_id"].to_numpy(), df["season"].to_numpy()
    for as_of, tgt, train in plan:
        assert (lg[tgt] == 94).all()
        second = train[lg[train] == 95]
        assert all(datetime(int(s) + 1, 7, 1, tzinfo=UTC) <= as_of for s in season[second])
        assert not (season[second] == 2019).any()  # the current season's second tier: never
    # without the columns the plan is the five bundles' plan, unchanged
    plain = df.drop("target", "available_at")
    old = harness.refit_plan(plain, cfg, [2019], 7)
    assert sum(len(t) for _, t, _ in old) > sum(len(t) for _, t, _ in plan)


def test_c2_run_fits_the_plans_rows_and_shares_phase1a_grid(tmp_path: Path) -> None:
    df, cfg = c2_matches(), config()
    spec = bt.RunSpec(every_days=28)
    fake = FakeFitter()
    # run() asserts prepare's window (after `observable`) equals the plan's training rows
    p = bt.run(df, C2L, "portugal", cfg, [2019], spec, LocalStore(tmp_path), fake,
               log=lambda _: None)  # fmt: skip
    assert fake.calls > 5 and set(p["league_id"].unique()) == {94}
    a = bt.phase1a_on_grid(df, C2L, cfg, [2019], 28)
    assert set(zip(p["match_id"], p["as_of"], strict=True)) == set(
        zip(a["match_id"], a["as_of"], strict=True)
    )
    assert p["config_hash"][0] == bt.bundle_hash(bt.config_hash(spec), "portugal")


def test_bundle_hash_leaves_the_five_and_moves_with_the_c2_scope(tmp_path: Path) -> None:
    h = bt.config_hash(bt.RunSpec())
    assert bt.bundle_hash(h, "england") == h
    assert bt.bundle_hash(h, "portugal") != h
    f = tmp_path / "c2.yml"
    f.write_text("a: 1\n")
    d1 = c2.digest((str(f),))
    f.write_text("a: 2\n")
    assert c2.digest((str(f),)) != d1


def test_holdout_keys_stay_apart(tmp_path: Path) -> None:
    df, cfg = c2_matches(), config()
    key = harness.HoldoutKey(run_id="1", opening="phase1b")
    with pytest.raises(harness.HoldoutLocked, match="not scored by the phase1b"):
        bt.run(df, C2L, "portugal", cfg, [2024], bt.production_spec(cfg), LocalStore(tmp_path),
               FakeFitter(), log=lambda _: None, holdout=key)  # fmt: skip
    five = matches()
    with pytest.raises(harness.HoldoutLocked, match="not scored by the c2"):
        bt.run(five, [39, 40], "england", cfg, [2024], bt.production_spec(cfg),
               LocalStore(tmp_path), FakeFitter(), log=lambda _: None,
               holdout=harness.HoldoutKey(run_id="1", opening="c2"))  # fmt: skip
    with pytest.raises(harness.HoldoutLocked):  # and no key at all: locked as ever
        bt.run(df, C2L, "portugal", cfg, [2024], bt.RunSpec(), LocalStore(tmp_path),
               FakeFitter(), log=lambda _: None)  # fmt: skip


def test_dev_verdict_fails_a_league_worse_than_the_segment_limit() -> None:
    gate = config().phase1b_gate
    rows = [
        {"segment_type": "league_id", "seg": lg, "metric": m, "rel": rel}
        for lg, rel in (("94", 0.002), ("88", 0.012))
        for m in gate.segment_metrics
    ]
    rows.append({"segment_type": "all", "seg": "all", "metric": "1X2:log_loss", "rel": 0.05})
    v = c2.dev_verdict(pl.DataFrame(rows), gate)
    assert v["verdict"] == "fail"
    assert v["failed"] == sorted(f"league 88 {m}" for m in gate.segment_metrics)
    ok = c2.dev_verdict(pl.DataFrame([r for r in rows if r["seg"] != "88"]), gate)
    assert ok["verdict"] == "pass"  # the overall gain is not a dev check (D52)


def test_config_covers_the_sixteen_loaded_leagues() -> None:
    cfg = c2.load_config()
    loaded = [c["league_id"] for f in ("config/c2_competitions.yml", "config/c2_history.yml")
              for c in yaml.safe_load(Path(f).read_text())["competitions"]]  # fmt: skip
    assert sorted(lid for ls in cfg.bundles.values() for lid in ls) == sorted(loaded)
    assert set(cfg.rounds) == set(loaded)
    tops = {ls[0] for ls in cfg.bundles.values()}
    assert tops == {c["league_id"] for c in yaml.safe_load(
        Path("config/c2_competitions.yml").read_text())["competitions"]}  # fmt: skip
    assert not set(cfg.bundles) & set(harness.load_config().bundles)
    for ls in cfg.bundles.values():
        assert c2.bundle_for(ls, cfg) is not None
    assert c2.bundle_for([39, 40], cfg) is None
