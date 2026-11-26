"""C2 step 2 data scope (docs/BEYOND_LEAGUES_PLAN.md "C2 step 2 acceptance" (a)-(c), D52):
round classes, the cross-tier and tie rules, top-flight targets, second tiers observable
only after the summer load, the five bundles untouched, the holdout keys kept apart; (g)
the c2 opening and its pre-registered per-top-flight gate. Synthetic data only."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from adatta.adapters.storage import LocalStore
from adatta.db import models
from adatta.eval import c2, harness
from adatta.eval import holdout as ho
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


def c2_matches(seasons: tuple[int, ...] = (2017, 2018, 2019)) -> pl.DataFrame:
    df = matches(seasons).with_columns(
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
    assert p["config_hash"][0] == bt.bundle_hash(bt.config_hash(spec), "portugal",
                                                 bt.C2_MAX_GOALS)  # fmt: skip


def test_c2_scores_to_20_goals_on_the_same_fits_and_the_five_keep_15(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """D53: the cap is prediction-time only. A rescore at another cap reuses every cached
    fit, its parts carry a different hash, and the truncated tail only shrinks; the five
    are scored with evaluation.yml as it is."""
    df, cfg = c2_matches(), config()
    assert harness.load_config().max_goals == 15 and bt.C2_MAX_GOALS == 20
    assert bt.scoring_config(cfg, "england") is cfg
    assert bt.scoring_config(cfg, "portugal").max_goals == 20
    spec, store, fake = bt.RunSpec(every_days=28), LocalStore(tmp_path), FakeFitter()
    p20 = bt.run(df, C2L, "portugal", cfg, [2019], spec, store, fake, log=lambda _: None)
    fitted = fake.calls
    monkeypatch.setattr(bt, "C2_MAX_GOALS", 15)
    p15 = bt.run(df, C2L, "portugal", cfg, [2019], spec, store, fake, log=lambda _: None)
    assert fake.calls == fitted and p15["cached"].all()  # no refits: same fit cache key
    assert p15["config_hash"][0] != p20["config_hash"][0]
    assert (p20["tail_mass"] <= p15["tail_mass"]).all()
    assert np.abs(p20["p_home"].to_numpy() - p15["p_home"].to_numpy()).max() < 1e-6
    # Phase 1a on the C2 bundle is scored to the same cap as 1b
    a20 = bt.phase1a_on_grid(df, C2L, bt.scoring_config(cfg, "portugal"), [2019], 28)
    a15 = bt.phase1a_on_grid(df, C2L, cfg, [2019], 28)
    assert (a20.sort("match_id")["tail_mass"] <= a15.sort("match_id")["tail_mass"]).all()


def test_bundle_hash_leaves_the_five_and_moves_with_the_c2_scope(tmp_path: Path) -> None:
    h = bt.config_hash(bt.RunSpec())
    assert bt.bundle_hash(h, "england") == h
    assert bt.bundle_hash(h, "england", 20) == h  # the five: the cap never enters
    assert bt.bundle_hash(h, "portugal") != h
    assert bt.bundle_hash(h, "portugal", 20) != bt.bundle_hash(h, "portugal")
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


# --------------------------------------------------------------- (g) the c2 holdout opening

PROD = "phase1b_strength_open_xg"
TOP = [94, 88, 144, 218, 179, 203, 345, 119]


def test_c2_gate_is_d18_with_d28_segments_over_the_eight_top_flights() -> None:
    g, base = ho.load_c2_gate(), harness.load_config().phase1b_gate
    assert g.model_dump(exclude={"segment_fail", "top_flights"}) == base.model_dump()
    assert g.segment_fail == "league_alone"
    assert sorted(g.top_flights) == sorted(b[0] for b in c2.load_config().bundles.values())
    assert sorted(g.top_flights) == sorted(TOP)


def test_c2_retest_gate_is_the_c2_gate_over_the_next_two_seasons() -> None:
    """D54 (h): the 1b retest is pre-registered with the c2 holdout's rule unchanged."""
    assert ho.load_c2_gate(ho.C2_RETEST_GATE_FILE) == ho.load_c2_gate()
    window = yaml.safe_load(Path(ho.C2_RETEST_GATE_FILE).read_text())["window"]
    assert window == {"seasons": [2026, 2027], "opens_after": "2028-06-30"}
    assert not set(window["seasons"]) & set(harness.load_config().holdout_seasons)


def c2_cmp(leagues: list[int], seg: dict[int, float] | None = None,
           pooled: tuple[float, float] = (-0.004, -0.001)) -> pl.DataFrame:  # fmt: skip
    """A compare() table: overall 1X2 rows, and every D28 segment per league at -0.2% except
    `seg` (league -> its total:crps rel)."""
    rows: list[dict[str, Any]] = [
        {"segment_type": "all", "seg": "all", "metric": m, "rel": pooled[0], "rel_hi": pooled[1]}
        for m in ("1X2:log_loss", "1X2:rps")
    ]
    for lg in leagues:
        for m in ho.load_c2_gate().segment_metrics:
            rel = (seg or {}).get(lg, -0.002) if m == "total:crps" else -0.002
            rows.append({"segment_type": "league_id", "seg": str(lg), "metric": m, "rel": rel,
                         "rel_hi": 0.01})  # fmt: skip
    return pl.DataFrame(rows)


def test_c2_verdict_rejects_a_failing_top_flight_alone_and_pools_the_rest() -> None:
    gate, asked = ho.load_c2_gate(), []

    def pooled_ok(lids: list[int]) -> pl.DataFrame:
        asked.append(sorted(lids))
        return c2_cmp(lids)

    v = ho.c2_verdict(c2_cmp(TOP), pooled_ok, gate)
    assert v["verdict"] == "champion" and v["pooled_over"] == sorted(TOP)
    assert all(x["status"] == "champion" for x in v["leagues"].values())

    v = ho.c2_verdict(c2_cmp(TOP, {218: 0.0101}), pooled_ok, gate)
    assert asked[-1] == sorted(lg for lg in TOP if lg != 218)  # pooled again without it
    assert v["leagues"][218] == {"status": "rejected", "reasons": ["league 218 total:crps"]}
    champions = [lg for lg, x in v["leagues"].items() if x["status"] == "champion"]
    assert champions == [lg for lg in TOP if lg != 218]
    assert v["verdict"] == "champion" and v["failed"] == ["league 218 total:crps"]

    # the pooled gain over the rest has an interval reaching zero: all eight rejected
    v = ho.c2_verdict(c2_cmp(TOP, {218: 0.0101}),
                      lambda lids: c2_cmp(lids, pooled=(-0.004, 0.0002)), gate)  # fmt: skip
    assert v["verdict"] == "rejected"
    assert v["leagues"][94]["reasons"] == [
        "pooled overall 1X2:log_loss interval upper bound",
        "pooled overall 1X2:rps interval upper bound",
    ]
    assert v["leagues"][218]["reasons"] == ["league 218 total:crps"]

    # every top flight fails a segment: nothing is pooled, all rejected
    v = ho.c2_verdict(c2_cmp(TOP, dict.fromkeys(TOP, 0.02)), pooled_ok, gate)
    assert v["verdict"] == "rejected" and v["pooled_over"] == []
    with pytest.raises(ValueError, match="top flights"):  # a top flight missing: no verdict
        ho.c2_verdict(c2_cmp(TOP[:-1]), pooled_ok, gate)


def test_the_c2_opening_is_a_marker_of_its_own_after_phase1b(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    with pytest.raises(harness.HoldoutLocked, match="comes first"):
        ho.claim(store, "9", PROD, "abc", "c2")
    ho.claim(store, "1", PROD, "abc")  # the five's opening, by another run
    assert ho.claim(store, "9", PROD, "abc", "c2")["opening"] == "c2"
    assert ho.claim(store, "9", PROD, "abc", "c2")["run_id"] == "9"  # a rerun of the same run
    with pytest.raises(harness.HoldoutLocked):
        ho.claim(store, "10", PROD, "abc", "c2")  # made once
    assert ho.unlock(store, "9", PROD, "c2") == harness.HoldoutKey("9", "c2")
    with pytest.raises(harness.HoldoutLocked):
        ho.unlock(store, "9", PROD)  # the phase1b opening is run 1's
    with pytest.raises(harness.HoldoutLocked):
        ho.unlock(store, "9", "phase1b_strength_open_xg_k2", "c2")  # production spec only
    assert ho.unlock(store, "1", PROD) == harness.HoldoutKey("1")
    cfg = harness.load_config()
    assert bt.holdout_bundles(cfg, harness.HoldoutKey("9", "c2")) == list(c2.load_config().bundles)
    assert bt.holdout_bundles(cfg, harness.HoldoutKey("1")) == list(cfg.bundles)


def test_c2_holdout_report_end_to_end(tmp_path: Path, monkeypatch: Any) -> None:
    """One C2 bundle as the whole C2 set: run, assemble with the c2 key, the per-top-flight
    gate, and one model_run row per top flight under its own name (serving's champion
    lookup by model name never sees it)."""
    small = c2cfg()
    gate = ho.load_c2_gate().model_copy(update={"top_flights": [94]})
    monkeypatch.setattr(c2, "load_config", lambda path=c2.FILE: small)
    monkeypatch.setattr(ho, "load_c2_gate", lambda path=ho.C2_GATE_FILE: gate)
    df = c2_matches((2022, 2023, 2024))
    cfg = config().model_copy(update={"holdout_seasons": [2024]})
    key, spec = harness.HoldoutKey("9", "c2"), bt.production_spec(config())
    pred = bt.run(df, C2L, "portugal", cfg, [2024], spec, LocalStore(tmp_path / "store"),
                  FakeFitter(), log=lambda _: None, holdout=key)  # fmt: skip
    assert set(pred["league_id"].unique()) == {94}
    parts = tmp_path / "parts"
    parts.mkdir()
    pred.with_columns(refit_every_days=pl.lit(7), plan_seasons=pl.lit("2024")).write_parquet(
        parts / f"{spec.model_name}_portugal_2024.parquet"
    )
    with pytest.raises(SystemExit, match="incomplete"):  # the five's key wants the five
        bt.assemble(parts, tmp_path / "out", cfg, lambda _: df, holdout=harness.HoldoutKey("1"))
    out = bt.assemble(parts, tmp_path / "out", cfg, lambda _: df, holdout=key)
    g = json.loads((out / "gate.json").read_text())
    manifest = json.loads((out / "manifest.json").read_text())
    assert g["opening"] == "c2" and set(g["leagues"]) == {"94"}
    assert manifest["run"] == "c2_holdout" and manifest["c2_leagues"] == g["leagues"]
    assert manifest["verdict"] == g["verdict"]
    assert "Pre-registered C2 gate" in (out / "summary.md").read_text()
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        assert bt.record(out, s) == 1
        r = s.scalars(select(models.ModelRun)).one()
        assert r.model_name == "phase1b_strength_open_xg_c2_94" and r.component == "c2"
        assert r.status == g["leagues"]["94"]["status"] and r.window == "holdout"
        assert r.manifest["league_id"] == 94 and "1X2:log_loss" in r.backtest_scores
