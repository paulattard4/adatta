"""Results-triggered refits (D33; docs/PHASE1B_TASKS.md section 7b, acceptance items 1-2):
one trigger rule for production and the backtest, no leakage, Monday refits unchanged,
results fetched every tick and the refit dispatched."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from test_backtest import LEAGUES, FakeFitter, config, matches
from test_capture import Harness
from test_capture import K as CK
from test_serve import AS_OF, World

from adatta.adapters.storage import LocalStore
from adatta.db import models
from adatta.eval import harness
from adatta.model import backtest as bt
from adatta.model import live
from adatta.sim import serve

SAT0 = AS_OF - timedelta(days=2)  # Saturday before the test week (Monday AS_OF)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """Day 0 = Monday AS_OF."""
    return AS_OF + timedelta(days=day, hours=hour, minutes=minute)


def fx(*rows: tuple[datetime, int, int]) -> pl.DataFrame:
    return pl.DataFrame(
        [{"kickoff": k, "home": h, "away": a} for k, h, a in rows],
        schema={"kickoff": pl.Datetime("us", "UTC"), "home": pl.Int64, "away": pl.Int64},
    )


# --------------------------------------------------------------------------- the rule


def test_weekends_alone_trigger_nothing() -> None:
    rows = [(at(5 + 7 * w, 15), 1 + 2 * i, 2 + 2 * i) for w in range(-1, 3) for i in range(3)]
    assert harness.refit_triggers(fx(*rows), 3.0) == []


def test_a_midweek_block_gets_one_refit_before_the_weekend() -> None:
    f = fx(
        (SAT0, 1, 2), (SAT0, 3, 4),
        (at(1, 19, 45), 1, 3),  # Tuesday
        (at(2, 20), 2, 4),  # Wednesday: its teams last played before Monday
        (at(5, 15), 1, 4), (at(5, 15), 2, 3),  # Saturday
    )  # fmt: skip
    assert harness.refit_triggers(f, 3.0) == [at(2, 23)]


def test_monday_night_and_late_sunday_results_are_caught() -> None:
    f = fx((at(0, 20), 1, 2), (at(5, 15), 1, 3))  # Monday night, then Saturday
    assert harness.refit_triggers(f, 3.0) == [at(0, 23)]
    # Sunday 21:30 ends after the Monday refit's cut-off; the team plays again on Wednesday
    f = fx((at(-1, 21, 30), 1, 2), (at(2, 19), 1, 3), (at(5, 15), 1, 2))
    assert harness.refit_triggers(f, 3.0) == [at(0, 0, 30), at(2, 22)]


def test_a_refit_waits_only_while_the_next_t24h_forecast_allows() -> None:
    # Monday-night result, other teams on Tuesday, Wednesday and Friday, the team on
    # Saturday 15:00 (T-24h Friday 15:00): refit after Wednesday, not Friday night
    f = fx(
        (at(0, 20), 1, 2), (at(1, 19), 3, 4), (at(2, 19), 5, 6), (at(4, 20), 7, 8),
        (at(5, 15), 1, 3),
    )  # fmt: skip
    assert harness.refit_triggers(f, 3.0) == [at(2, 22)]


def test_the_rule_reads_kickoffs_only() -> None:
    f = fx((at(1, 19), 1, 2), (at(4, 20), 1, 3), (at(5, 15), 2, 3))
    with_scores = f.with_columns(hg=pl.lit(7), ag=pl.lit(0))
    assert harness.refit_triggers(f, 3.0) == harness.refit_triggers(with_scores, 3.0)
    # a match kicking off before the trigger time (here 01:00 the next day) can't use it
    f = fx((at(1, 22), 1, 2), (at(2, 1), 1, 3))
    assert harness.refit_triggers(f, 3.0) == []


# --------------------------------------------------------------------------- the backtest


def with_midweek(df: pl.DataFrame, weeks: range) -> pl.DataFrame:
    """Add a Wednesday and a Saturday round for four teams in the given weeks of 2019."""
    rows, mid = [], int(df["match_id"].max())  # type: ignore[arg-type]
    start = datetime(2019, 8, 7, tzinfo=UTC)  # a Wednesday
    for w in weeks:
        for day, hour, pairs in ((0, 19, ((1000, 1001), (1002, 1003))),
                                 (3, 15, ((1000, 1002), (1001, 1003)))):  # fmt: skip
            for h, a in pairs:
                mid += 1
                rows.append({"match_id": mid, "league_id": 39, "season": 2019,
                             "kickoff": start + timedelta(days=7 * w + day, hours=hour),
                             "home": h, "away": a, "round": "Regular Season - 2",
                             "hg": 1, "ag": 1})  # fmt: skip
    return pl.concat([df, pl.DataFrame(rows, schema=df.schema)]).sort("kickoff", "match_id")


def test_without_midweek_matches_the_results_grid_is_the_weekly_grid() -> None:
    df, cfg = matches(), config()
    weekly = harness.refit_plan(df, cfg, [2019], 7)
    results = harness.refit_plan(df, cfg, [2019], 7, grid="results")
    assert [a for a, _, _ in weekly] == [a for a, _, _ in results]
    assert all(np.array_equal(w[1], r[1]) and np.array_equal(w[2], r[2])
               for w, r in zip(weekly, results, strict=True))  # fmt: skip


def test_results_grid_predicts_each_match_once_without_leakage() -> None:
    df, cfg = with_midweek(matches(), range(2, 6)), config()
    kick = df["kickoff"].to_list()
    lag = timedelta(hours=cfg.result_available_after_hours)
    weekly = harness.refit_plan(df, cfg, [2019], 7)
    plan = harness.refit_plan(df, cfg, [2019], 7, grid="results")
    triggers = [a for a, _, _ in plan if a != harness.week_start(a)]
    assert len(triggers) >= 4
    targets = np.concatenate([t for _, t, _ in plan])
    assert len(targets) == len(set(targets.tolist()))
    assert set(targets.tolist()) == set(np.concatenate([t for _, t, _ in weekly]).tolist())
    times = [a for a, _, _ in plan]
    for i, (as_of, tgt, train) in enumerate(plan):
        assert all(kick[j] + lag <= as_of for j in train)
        end = times[i + 1] if i + 1 < len(times) else as_of + timedelta(days=7)
        assert all(as_of <= kick[j] < end for j in tgt)
    # a trigger's training includes that Wednesday's matches
    first = triggers[0]
    _, _, train = next(p for p in plan if p[0] == first)
    assert any(first - lag - timedelta(hours=1) < kick[j] <= first - lag for j in train)


def test_holdout_stays_weekly() -> None:
    df = matches((2022, 2023, 2024))
    with pytest.raises(harness.HoldoutLocked):
        harness.refit_plan(df, config(), [2024], 7, harness.HoldoutKey("x"), grid="results")


def test_weekly_hash_and_keys_are_unchanged() -> None:
    spec = bt.production_spec(config())
    fields = asdict(spec)
    for k in ("groups", "context", "grid", "widen"):  # the pre-D33 formula
        del fields[k]
    blob = json.dumps({"spec": fields, "files": {f: bt._file_hash(f) for f in bt.HASHED_FILES}},
                      sort_keys=True)  # fmt: skip
    assert bt.config_hash(spec) == hashlib.sha256(blob.encode()).hexdigest()[:16]
    rd = replace(spec, grid="results")
    assert rd.model_name == spec.model_name + "_rd"
    assert bt.config_hash(rd) != bt.config_hash(spec) and bt.fit_hash(rd) == bt.config_hash(spec)
    assert bt.cache_key("h", "england", AS_OF).endswith("/england/20261109.npz")
    assert serve.refit_key("h", "england", AS_OF) == "serving/h/england/20261109.npz"
    assert serve.refit_key("h", "england", at(2, 23)).endswith("/20261111T230000.npz")


def test_results_run_shares_the_monday_fits_and_reports(tmp_path: Path) -> None:
    df, cfg = with_midweek(matches(), range(2, 6)), config()
    store, parts = LocalStore(tmp_path / "c"), tmp_path / "parts"
    parts.mkdir()
    weekly_spec = bt.RunSpec(every_days=7)
    rd_spec = replace(weekly_spec, grid="results")
    fake = FakeFitter()
    pw = bt.run(df, LEAGUES, "england", cfg, [2019], weekly_spec, store, fake, log=lambda _: None)
    n_weekly = fake.calls
    pr = bt.run(df, LEAGUES, "england", cfg, [2019], rd_spec, store, fake, log=lambda _: None)
    n_trig = pr.filter(pl.col("as_of").dt.hour() != 0)["as_of"].n_unique()
    assert n_trig >= 4 and fake.calls - n_weekly == n_trig  # only the triggers are fitted
    assert set(pr["model"]) == {"phase1b_strength_open_xg_rd"}
    for p, name in ((pw, "w"), (pr, "r")):
        p.with_columns(refit_every_days=pl.lit(7)).write_parquet(parts / f"{name}.parquet")
    out = bt.assemble(parts, tmp_path / "out", cfg, lambda _: df, "phase1b_strength_open_xg")
    rep = json.loads((out / "cadence_phase1b_strength_open_xg_rd.json").read_text())
    assert rep["trigger_refits"] == n_trig and rep["affected_matches"] > 0
    assert rep["unchanged_max_diff"] == 0.0  # same Monday draws, same probabilities
    assert rep["verdict"] in ("pass", "fail")
    summary = (out / "summary.md").read_text()
    assert "Results-triggered refits" in summary and "match_id" not in summary


# --------------------------------------------------------------------------- live


def test_live_refit_times_and_what_is_due(tmp_path: Path) -> None:
    w = World(tmp_path)
    cfg = config().model_copy(update={"bundles": {"england": LEAGUES}})
    w.fixture(SAT0, home=1, away=2, status="FT")
    w.fixture(at(2, 19), home=1, away=3, status="FT")  # Wednesday
    w.fixture(at(3, 19), home=5, away=6, status="PST")  # postponed: not a match day
    w.fixture(at(5, 15), home=1, away=4)  # Saturday
    w.s.commit()
    assert live.refit_times(w.s, cfg, "england", at(2, 21, 59)) == [AS_OF]
    assert live.refit_times(w.s, cfg, "england", at(2, 22)) == [AS_OF, at(2, 22)]
    assert live.due(w.s, cfg, "phase1b_strength_open_xg", at(3, 9)) == [
        ("england", AS_OF), ("england", at(2, 22))
    ]  # fmt: skip
    w.refit(AS_OF)
    w.s.commit()
    assert live.due(w.s, cfg, "phase1b_strength_open_xg", at(3, 9)) == [("england", at(2, 22))]
    # switched off (RESULTS_REFITS unset): the Monday refit only, as before D33
    assert live.due(w.s, cfg, "phase1b_strength_open_xg", at(3, 9), triggers=False) == []
    w.refit(at(2, 22), status="failed")  # a failed refit is not retried
    w.s.commit()
    assert live.due(w.s, cfg, "phase1b_strength_open_xg", at(3, 9)) == []


def test_trigger_refits_are_off_until_switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from adatta import cli

    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.delenv("RESULTS_REFITS", raising=False)
    assert not live.triggers_enabled() and cli._serve_dispatcher() is None
    monkeypatch.setenv("RESULTS_REFITS", "on")
    assert live.triggers_enabled() and cli._serve_dispatcher() is not None


# --------------------------------------------------------------------------- capture


def test_results_are_fetched_every_tick_once_the_match_is_over(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    final = {"on": False}
    base = h.api.fixture

    def fixture(fid: int, kickoff: datetime) -> dict[str, Any]:
        f = base(fid, kickoff)
        if final["on"]:
            f["fixture"]["status"] = {"short": "FT"}
            f["goals"] = {"home": 2, "away": 1}
            f["score"] = {"halftime": {"home": 1, "away": 0}, "fulltime": {"home": 2, "away": 1}}
        return f

    h.api.fixture = fixture  # type: ignore[method-assign]
    h.tick(CK - timedelta(days=2))  # calendar: the match row exists
    n = len(h.api.calls)
    h.tick(CK + timedelta(hours=1))  # too early to ask
    assert not any("ids" in c for c in h.api.calls[n:])
    h.tick(CK + timedelta(hours=2, minutes=5))  # asked, not final yet
    assert h.count(models.MatchResult) == 0
    final["on"] = True
    r = h.tick(CK + timedelta(hours=2, minutes=20))
    assert r.actions["results_final"] == 1 and r.ok
    assert h.count(models.MatchResult) == 1 and h.count(models.MatchDetail) == 1
    n = len(h.api.calls)
    h.tick(CK + timedelta(hours=2, minutes=35))  # final: not asked again
    assert not any("ids" in c for c in h.api.calls[n:])


def test_a_due_refit_is_dispatched_at_most_hourly(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    sent: list[int] = []
    due = {"items": ["england 20261111T220000"]}

    def tick(now: datetime) -> None:
        h.tick(now, refits_due=lambda _: list(due["items"]), dispatch=lambda: sent.append(1))

    t = CK - timedelta(days=3)
    tick(t)
    tick(t + timedelta(minutes=15))
    assert len(sent) == 1
    tick(t + timedelta(minutes=61))
    assert len(sent) == 2
    due["items"] = []
    tick(t + timedelta(hours=3))
    assert len(sent) == 2
