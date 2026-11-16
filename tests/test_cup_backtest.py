"""Stage A, A1: the 1b model applied unchanged to cup ties, with a fake sampler: targets from
ties, the week's Monday refit shared with the league grid, the holdout refused, metrics only."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from test_backtest import LEAGUES, FakeFitter, config, matches

from adatta.adapters.storage import LocalStore
from adatta.eval import harness
from adatta.model import backtest as bt
from adatta.model import cup_backtest as cb


def ties() -> pl.DataFrame:
    """Five 2019 ties: top v second tier, a swapped venue, a neutral final, one with a side
    from outside the bundle (dropped), and a second leg."""
    wed = datetime(2019, 10, 2, 18, 45, tzinfo=UTC)  # a Wednesday
    rows = [
        (1, 1000, 1010, 2, 1, "home", "single", wed),
        (2, 1001, 1002, 0, 0, "swapped", "single", wed),
        (3, 1003, 1011, 1, 3, "neutral", "single", wed + timedelta(days=60)),
        (4, 1004, 99999, 4, 0, "home", "single", wed),
        (5, 1012, 1013, 1, 1, "home", "leg2", wed + timedelta(days=7)),
    ]
    return pl.DataFrame(
        [{"match_id": m, "cup_id": 45, "season": 2019, "kickoff": k, "round": "Round",
          "home": h, "away": a, "hg": hg, "ag": ag, "role": role, "kind": kind}
         for m, h, a, hg, ag, role, kind, k in rows],
        schema=cb.TIE_SCHEMA,
    )  # fmt: skip


def spec() -> bt.RunSpec:
    return replace(bt.production_spec(config()), xg=False)


def test_targets_keep_bundle_ties_and_swap_a_swapped_venue() -> None:
    t = cb.build_targets(ties(), matches(), LEAGUES)
    assert set(t["match_id"]) == {1, 2, 3, 5}  # 4 has a side outside the bundle
    by = {r["match_id"]: r for r in t.iter_rows(named=True)}
    assert (by[1]["league_id"], by[1]["pairing"]) == (39, "1v2")
    assert (by[2]["home"], by[2]["away"], by[2]["hg"], by[2]["ag"]) == (1002, 1001, 0, 0)
    assert (by[5]["league_id"], by[5]["pairing"]) == (40, "2v2")
    assert (by[1]["tier_home"], by[1]["tier_away"]) == (1, 2)
    # synthetic leagues play every 7 days from a Wednesday 15:00 plus a few hours
    assert all(0 < by[1][f"gap_{s}_days"] < 8 for s in ("home", "away"))


def test_scores_from_the_weeks_monday_refit_shared_with_the_league(tmp_path: Path) -> None:
    cfg, df, store, fake = config(), matches(), LocalStore(tmp_path), FakeFitter()
    t = cb.build_targets(ties(), df, LEAGUES)
    pred = cb.score_season(df, LEAGUES, "england", cfg, 2019, spec(), store, t, fake,
                           log=lambda _: None)  # fmt: skip
    assert pred.height == 4 and fake.calls == 3  # three distinct weeks
    for r in pred.iter_rows(named=True):
        assert r["as_of"] == harness.week_start(r["kickoff"]) <= r["kickoff"]
        assert abs(r["p_home"] + r["p_draw"] + r["p_away"] - 1) < 1e-9
        assert r["score:log_loss"] > 0
    # the league backtest's refit of the same Monday is the same cache entry
    league = bt.run(df, LEAGUES, "england", cfg, [2019], spec(), store, fake, log=lambda _: None)
    weeks = set(pred["as_of"].to_list())
    assert weeks <= set(league["as_of"].to_list())
    assert league.filter(pl.col("as_of").is_in(list(weeks)))["cached"].all()
    again = cb.score_season(df, LEAGUES, "england", cfg, 2019, spec(), store, t, fake,
                            log=lambda _: None)  # fmt: skip
    assert again["cached"].all()
    assert again["1X2:log_loss"].to_list() == pred["1X2:log_loss"].to_list()


def test_holdout_other_seasons_and_changed_models_are_refused(tmp_path: Path) -> None:
    cfg, df, store = config(), matches(), LocalStore(tmp_path)
    t = cb.build_targets(ties(), df, LEAGUES)
    with pytest.raises(harness.HoldoutLocked):
        cb.score_season(df, LEAGUES, "england", cfg, cfg.holdout_seasons[0], spec(), store, t,
                        FakeFitter(), log=lambda _: None)  # fmt: skip
    with pytest.raises(ValueError):
        cb.score_season(df, LEAGUES, "england", cfg, 2017, spec(), store, t, FakeFitter(),
                        log=lambda _: None)  # fmt: skip
    with pytest.raises(ValueError):
        cb.score_season(df, LEAGUES, "england", cfg, 2019, replace(spec(), groups=("style",)),
                        store, t, FakeFitter(), log=lambda _: None)  # fmt: skip


def test_summary_and_markdown_carry_metrics_only(tmp_path: Path) -> None:
    cfg, df = config(), matches()
    t = cb.build_targets(ties(), df, LEAGUES)
    pred = cb.score_season(df, LEAGUES, "england", cfg, 2019, spec(), LocalStore(tmp_path), t,
                           FakeFitter(), log=lambda _: None)  # fmt: skip
    rep = cb.summarise(pred, {45: "FA Cup"})
    assert rep["overall"]["n"] == 4 and rep["refits"]["n"] == 3
    assert set(rep["segments"]["pairing"]) == {"1v1", "1v2", "2v2"} & set(pred["pairing"])
    assert rep["segments"]["neutral"]["True"]["n"] == 1
    md = cb.markdown(rep)
    assert "FA Cup" in md and "1001" not in md and "1002" not in md  # no team ids
    assert md.startswith("# Stage A, A1")


def test_shipped_cup_formats_name_each_bundles_cup() -> None:
    expected = {"england": [45], "spain": [143], "italy": [137], "germany": [81], "france": [66]}
    assert {b: cb.cups_of(b) for b in expected} == expected


def test_rows_keep_each_draws_rates_and_home_term(tmp_path: Path) -> None:
    cfg, df = config(), matches()
    t = cb.build_targets(ties(), df, LEAGUES)
    pred = cb.score_season(df, LEAGUES, "england", cfg, 2019, spec(), LocalStore(tmp_path), t,
                           FakeFitter(), log=lambda _: None)  # fmt: skip
    r = pred.row(0, named=True)
    assert len(r["draws_lam"]) == len(r["draws_mu"]) == len(r["draws_rho"]) == 20
    assert r["draws_home_term"] == [0.25] * 20  # FakeFitter: h_league 0.25, h_team 0
    assert abs(sum(r["draws_lam"]) / 20 + sum(r["draws_mu"]) / 20 - r["exp_total"]) < 1e-9
