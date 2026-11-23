"""Stage A, A3 (cups as training data), with fake samplers: P's hash and cache untouched,
cup rows only from what is observable at the refit (no leakage, no holdout season), the
home term on neutral and swapped ties, the pairing with P's cached fits, the redo merge and
the gate."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from test_backtest import LEAGUES, FakeFitter, config, matches

from adatta.adapters.storage import LocalStore
from adatta.eval import harness
from adatta.model import backtest as bt
from adatta.model import cup_backtest as cb
from adatta.model import cup_strength as cs
from adatta.model import cup_train as ct
from adatta.model import team_strength as ts


def ties() -> pl.DataFrame:
    """Bundle ties over 2017-2019: midweek, a neutral final, a swapped venue, and one
    between second-tier sides."""
    rows: list[dict[str, Any]] = []
    mid = 0
    for season in (2017, 2018, 2019):
        wed = datetime(season, 9, 13, 19, 45, tzinfo=UTC)
        for k in range(8):
            mid += 1
            role = "neutral" if k == 7 else "swapped" if k == 3 else "home"
            rows.append({"match_id": 90000 + mid, "cup_id": 45, "season": season,
                         "kickoff": wed + timedelta(days=21 * k), "round": "Round",
                         "home": 1000 + k, "away": 1010 + k, "hg": k % 3, "ag": (k + 1) % 2,
                         "role": role, "kind": "single"})  # fmt: skip
    t = pl.DataFrame(rows, schema=cb.TIE_SCHEMA)
    return cb.build_targets(t, matches(), LEAGUES)


def fd_at(as_of: datetime) -> ts.FitData:
    return ts.prepare(matches(), LEAGUES, as_of, 1095, 3.0)


def test_p_hash_is_unchanged_and_a3_has_its_own() -> None:
    cfg = config()
    P = bt.production_spec(cfg)
    A = ct.a3_spec(cfg, "abc")
    assert bt.config_hash(P) == bt.config_hash(replace(P, cups=False, cup_table="",
                                                       cup_prior_sd=0.25))  # fmt: skip
    hashes = {bt.fit_hash(P), bt.fit_hash(A), bt.fit_hash(ct.a3_spec(cfg, "abd")),
              bt.fit_hash(ct.a3_spec(cfg, "abc", 0.1))}  # fmt: skip
    assert len(hashes) == 4
    assert A.model_name == "phase1b_strength_open_xg_cup"


def test_cup_rows_are_only_what_is_observable_at_the_refit() -> None:
    t = ties()
    as_of = datetime(2019, 10, 7, tzinfo=UTC)  # the 2019 ties of 13 Sep and 4 Oct are in
    fd = fd_at(as_of)
    cr, dropped = cs.cup_rows(fd, t, as_of, 1095, 3.0, 2024)
    ids = set(cr.match_id.tolist())
    seen = t.filter((pl.col("kickoff") + timedelta(hours=3) <= as_of)
                    & (pl.col("kickoff") >= as_of - timedelta(days=1095)))  # fmt: skip
    assert ids == set(seen["match_id"]) and dropped == 0
    assert all(k + timedelta(hours=3) <= as_of
               for k in t.filter(pl.col("match_id").is_in(list(ids)))["kickoff"])  # fmt: skip
    # a tie kicking off 2 h before the refit is not yet observable (3 h lag)
    late = t.head(1).with_columns(
        kickoff=pl.lit(as_of - timedelta(hours=2)), match_id=pl.lit(1, dtype=pl.Int64)
    )
    cr2, _ = cs.cup_rows(fd, pl.concat([t, late]), as_of, 1095, 3.0, 2024)
    assert 1 not in cr2.match_id.tolist()
    # indexed like the league rows, inside the refit's steps
    assert cr.step.min() >= 0 and cr.step.max() < fd.n_steps
    assert set(cr.league.tolist()) <= {0, 1}
    # the neutral final has no home term, the others do
    by = dict(zip(cr.match_id.tolist(), cr.neutral.tolist(), strict=True))
    assert by[90008] and not by[90001]


def test_a_holdout_season_tie_never_reaches_a_refit() -> None:
    t = ties()
    as_of = datetime(2019, 10, 7, tzinfo=UTC)
    with pytest.raises(ValueError, match="holdout"):
        cs.cup_rows(fd_at(as_of), t, as_of, 1095, 3.0, 2018)
    cfg = config()
    late = t.with_columns(season=pl.lit(cfg.holdout_seasons[0], dtype=pl.Int64))
    assert ct.training_ties(pl.concat([t, late]), cfg).height == t.height


def test_a_side_without_league_rows_stays_out() -> None:
    t = ties().with_columns(away=pl.when(pl.col("match_id") == 90001).then(77777)
                            .otherwise(pl.col("away")))  # fmt: skip
    as_of = datetime(2019, 10, 7, tzinfo=UTC)
    cr, dropped = cs.cup_rows(fd_at(as_of), t, as_of, 1095, 3.0, 2024)
    assert dropped == 1 and 90001 not in cr.match_id.tolist()


def test_cup_offsets_take_the_home_term_off_neutral_ties_and_add_delta() -> None:
    as_of = datetime(2019, 12, 2, tzinfo=UTC)
    fd = fd_at(as_of)
    draws, _ = FakeFitter()(fd, 1)
    draws["delta_cup"] = np.full(20, -0.1)
    grp = ties().filter(pl.col("season") == 2019)
    off_h, off_a = ct.cup_offsets(draws, fd, grp)
    home = np.asarray(cb.home_terms(draws, fd, grp))
    neutral = (grp["role"] == "neutral").to_numpy()
    assert np.allclose(off_h[neutral], -home[neutral])
    assert np.allclose(off_h[~neutral], -0.1) and np.allclose(off_a, 0.0)
    # with the offset, a neutral tie's rates carry no home term at all
    lam, _ = ts.rates_from_draws(draws, fd, np.array([0]), np.array([1]), np.array([0]),
                                 offset=(-np.asarray(cb.home_terms(
                                     draws, fd, pl.DataFrame({"home": [int(fd.team_ids[0])],
                                                              "league_id": [39]})
                                 )), np.zeros((1, 20))))  # fmt: skip
    plain = np.exp(draws["mu_league"][0] + draws["attack_last"][0] - draws["defence_last"][1])
    assert np.allclose(lam[0], plain)


class A3Fake:
    """A3's fake fitter: P's fake draws plus delta_cup, recording the cup rows it saw."""

    def __init__(self) -> None:
        self.seen: list[cs.CupRows] = []

    def __call__(self, cr: cs.CupRows) -> bt.Fitter:
        def fit(fd: ts.FitData, seed: int) -> tuple[bt.Draws, bt.Diag]:
            self.seen.append(cr)
            d, diag = FakeFitter()(fd, seed)
            d["delta_cup"] = np.full(20, 0.03)
            return d, {**diag, "ess_tail_min": 900.0, "fit_s": 12.0}

        return fit


def p_fake(fd: ts.FitData, seed: int) -> tuple[bt.Draws, bt.Diag]:
    d, diag = FakeFitter()(fd, seed)
    return d, {**diag, "ess_tail_min": 900.0, "fit_s": 10.0}


def _seed_p_cache(store: LocalStore, cfg: harness.EvalConfig, df: pl.DataFrame) -> None:
    P = bt.production_spec(cfg)
    for as_of, _, _ in harness.refit_plan(df, cfg, [2019], 7, None, "weekly"):
        fd = ts.prepare(df, LEAGUES, as_of, cfg.lookback_days, cfg.result_available_after_hours)
        store.put(bt.cache_key(bt.fit_hash(P), "england", as_of), bt.encode(*p_fake(fd, 1)))


def test_run_pairs_p_and_a3_on_the_same_refits(tmp_path: Path) -> None:
    cfg, df, store = config(), matches(), LocalStore(tmp_path)
    t = ties()
    # P is not refitted on the grid: a missing cached fit stops the job
    with pytest.raises(SystemExit, match="not cached"):
        ct.run_part(df, LEAGUES, "england", 2019, store, t, (0, 1),
                    fitters=(p_fake, A3Fake()), cfg=cfg)  # fmt: skip
    _seed_p_cache(store, cfg, df)
    fake = A3Fake()
    lg, cu, diags = ct.run_part(df, LEAGUES, "england", 2019, store, t, (0, 1),
                                fitters=(p_fake, fake), cfg=cfg)  # fmt: skip
    plan = harness.refit_plan(df, cfg, [2019], 7, None, "weekly")
    weeks = {harness.week_start(k) for k in t.filter(pl.col("season") == 2019)["kickoff"]}
    times = ct.refit_times(plan, weeks)
    assert len(diags) == 2 * len(times)
    # the same league targets, scored by both from the same refit
    p_rows = lg.filter(pl.col("model") == "P").select("match_id", "as_of").sort("match_id")
    a_rows = lg.filter(pl.col("model") == "A3").select("match_id", "as_of").sort("match_id")
    assert p_rows.equals(a_rows) and p_rows.height == sum(len(x[1]) for x in plan)
    # every 2019 tie scored by both, from its Monday's refit, after it
    assert cu.height == 2 * t.filter(pl.col("season") == 2019).height
    assert (cu["kickoff"] >= cu["as_of"]).all()
    # no cup row a fit saw was played after its refit's as_of minus the lag
    kick = dict(zip(t["match_id"].to_list(), t["kickoff"].to_list(), strict=True))
    for (as_of, _), cr in zip(times, fake.seen, strict=True):
        assert all(kick[int(m)] + timedelta(hours=3) <= as_of for m in cr.match_id)
    a3 = [g for g in diags if g["model"] == "A3"]
    assert all(g["gates_ok"] and g["cup_rows"] > 0 for g in a3)
    # rerun: everything from the cache
    fake2 = A3Fake()
    ct.run_part(df, LEAGUES, "england", 2019, store, t, (0, 1), fitters=(p_fake, fake2),
                cfg=cfg)  # fmt: skip
    assert fake2.seen == []


def test_pairing_check_stops_on_a_different_training_set() -> None:
    fd = fd_at(datetime(2019, 12, 2, tzinfo=UTC))
    draws, _ = p_fake(fd, 1)
    ct.check_pairing(draws, fd)
    draws["train_lam_home"] = np.full(len(fd.hg) - 1, 1.5)
    draws["train_lam_away"] = np.full(len(fd.hg) - 1, 1.1)
    with pytest.raises(SystemExit, match="training rows"):
        ct.check_pairing(draws, fd)
    draws["attack_last"] = draws["attack_last"][:-1]
    with pytest.raises(SystemExit, match="teams"):
        ct.check_pairing(draws, fd)


def test_merge_redo_replaces_only_a3_refits() -> None:
    t0, t1 = datetime(2019, 9, 2, tzinfo=UTC), datetime(2019, 9, 9, tzinfo=UTC)
    rows = pl.DataFrame({"match_id": [1, 1, 2, 2], "as_of": [t0, t0, t1, t1],
                         "model": ["P", "A3", "P", "A3"], "x": [1.0, 2.0, 3.0, 4.0]})  # fmt: skip
    diags = [{"model": m, "as_of": a.isoformat(), "gates_ok": m == "P" or a == t0}
             for a in (t0, t1) for m in ("P", "A3")]  # fmt: skip
    over_rows = pl.DataFrame({"match_id": [2, 2], "as_of": [t1, t1], "model": ["P", "A3"],
                              "x": [3.0, 40.0]})  # fmt: skip
    over_diags = [{"model": "A3", "as_of": t1.isoformat(), "gates_ok": True}]
    lg, cu, dg = ct.merge_redo((rows, rows, diags), (over_rows, over_rows, over_diags))
    assert sorted(lg["x"].to_list()) == [1.0, 2.0, 3.0, 40.0]
    assert all(g["gates_ok"] for g in dg) and len(dg) == 4


def _scored(n: int, a3_better: float, seed: int = 0) -> tuple[pl.DataFrame, pl.DataFrame]:
    rng = np.random.default_rng(seed)
    hg, ag = rng.poisson(1.4, n), rng.poisson(1.1, n)
    rows = []
    for model, shift in (("P", 0.0), ("A3", a3_better)):
        for i in range(n):
            ll = 1.0 + 0.3 * rng.random() - shift
            rows.append({"match_id": i, "model": model, "cup_id": 45 + i % 2, "hg": int(hg[i]),
                         "ag": int(ag[i]), "league_id": 39 + i % 2, "bundle": "england",
                         "cold_start": False, "neutral": i % 20 == 0,
                         "pairing": "1v2", "p_home": 0.45, "p_draw": 0.27, "p_away": 0.28,
                         "exp_total": 2.5, "total_var": 2.6, "1X2:log_loss": ll,
                         "1X2:rps": ll / 5, "1X2:brier": ll / 2, "OU2.5:log_loss": 0.69,
                         "OU2.5:brier": 0.25, "BTTS:log_loss": 0.69, "BTTS:brier": 0.25,
                         "total:crps": 0.9, "score:log_loss": 2.9})  # fmt: skip
    df = pl.DataFrame(rows)
    return df, df


def _diags(ratio: float, fail: bool = False) -> list[dict[str, Any]]:
    out = []
    for k in range(4):
        a = f"2019-09-{2 + 7 * k:02d}T00:00:00+00:00"
        out.append({"model": "P", "bundle": "england", "as_of": a, "fit_s": 100.0,
                    "gates_ok": True, "tier_gap": -0.3})  # fmt: skip
        out.append({"model": "A3", "bundle": "england", "as_of": a, "fit_s": 100.0 * ratio,
                    "gates_ok": not (fail and k == 0), "tier_gap": -0.28, "delta_cup": 0.02,
                    "delta_cup_sd": 0.03, "cup_dropped": 0, "redone": False})  # fmt: skip
    return out


def test_gate_passes_only_with_every_check() -> None:
    cfg, names = config(), {45: "FA Cup", 46: "Copa"}
    lg, cu = _scored(600, 0.05)
    v = ct.verdict(lg, cu, _diags(1.2), cfg, names)
    assert v["passed"], v["checks"]
    assert not ct.verdict(lg, cu, _diags(1.6), cfg, names)["checks"]["4_timing"]
    assert not ct.verdict(lg, cu, _diags(1.0, fail=True), cfg, names)["checks"]["3_sampler_gates"]
    lg_worse, _ = _scored(600, -0.05)  # leagues clearly worse
    v2 = ct.verdict(lg_worse, cu, _diags(1.0), cfg, names)
    assert not v2["checks"]["2_leagues_no_worse"] and v2["checks"]["1_cups_improve"]
    _, cu_flat = _scored(600, 0.0)
    assert not ct.verdict(lg, cu_flat, _diags(1.0), cfg, names)["checks"]["1_cups_improve"]
    text = ct.markdown(v, {})
    assert "PASSES" in text and "match_id" not in text
