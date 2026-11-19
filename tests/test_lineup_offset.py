"""P3-d lineup offset: table construction, serving-only beta, verdict and the 1b wiring."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest

from adatta.eval import lineup as el
from adatta.features import lineup_offset as lo

T0 = datetime(2019, 8, 3, 15, tzinfo=UTC)
LEAGUES = ("39", "40")
RATE = {"G": 0.01, "D": 0.05, "M": 0.10, "F": 0.40}  # per 90 at theta = 1, league 39


def _posts(n: int = 4) -> dict[datetime, dict[str, np.ndarray]]:
    """Hyperparameters with known rates; one refit before everything."""
    log_mu = np.tile(np.log([RATE[x] for x in "GDMF"]), (n, 1))
    return {datetime(2018, 7, 1, tzinfo=UTC): {
        "log_mu": log_mu, "log_l": np.zeros((n, 1)), "b": np.zeros((n, 3)),
        "log_k": np.full((n, 4), np.log(5.0)),
    }}  # fmt: skip


def _hist() -> pl.DataFrame:
    """A counts table holding one unrelated player only: everyone else has no history, so
    theta-hat = 1 and quality = the line rate."""
    return pl.DataFrame({
        "match_id": [1], "team_id": [99], "player_id": [9999], "league_ref": ["39"],
        "season": [2018], "kickoff": [T0 - timedelta(days=400)], "sub": [False], "e": [1.0],
        "line": ["F"], "new_club": [False], "ret": [False], "slot": [None],
        "y_goals": [1],
    }, schema_overrides={"slot": pl.Utf8, "y_goals": pl.Int32})  # fmt: skip


def _app(n_matches: int = 6, team: int = 1, fw_minutes: list[int] | None = None) -> pl.DataFrame:
    """Team 1's league matches a week apart: a GK, 4 D, 4 M and 2 F start; F2 (player 11)
    plays the minutes in `fw_minutes` (90 each by default), a bench F (player 12) never plays."""
    fw = fw_minutes or [90] * n_matches
    rows = []
    for m in range(n_matches):
        k = T0 + timedelta(days=7 * m)
        squad = [(1, "G", True, 90)] + [(p, "D", True, 90) for p in range(2, 6)]
        squad += [(p, "M", True, 90) for p in range(6, 10)] + [(10, "F", True, 90)]
        squad += [(11, "F", fw[m] > 0, fw[m]), (12, "F", False, 0)]
        for pid, pos, st, mins in squad:
            rows.append({"match_id": 100 + m, "team_id": team, "player_id": pid,
                         "league_ref": "39", "season": 2019, "kickoff": k, "started": st,
                         "pos": pos, "minutes": mins})  # fmt: skip
    return pl.DataFrame(rows, schema_overrides={"minutes": pl.Int32})


def test_previous_takes_final_matches_only() -> None:
    sq = lo.squads(_app(14))
    prev = lo.previous(lo.team_matches(sq))
    last = prev.filter(pl.col("match_id") == 113)
    assert last.height == lo.PREV_N
    assert set(last["prev_id"]) == set(range(103, 113))
    assert prev.filter(pl.col("match_id") == 100).is_empty()


def test_delta_real_is_log_ratio_to_typical_lineup() -> None:
    # F2 plays 90 in five matches, then 0 in the sixth (one forward fewer)
    app = _app(6, fw_minutes=[90, 90, 90, 90, 90, 0])
    t = lo.build(app, _hist(), _posts(), LEAGUES).sort("kickoff")
    a_full = 4 * RATE["D"] + 4 * RATE["M"] + 2 * RATE["F"]
    a_short = a_full - RATE["F"]
    last = t.row(5, named=True)
    assert last["n_prev"] == 5
    assert last["a_real"] == pytest.approx(a_short)
    assert last["a_bar"] == pytest.approx(a_full)
    assert last["delta_real"] == pytest.approx(np.log(a_short / a_full))
    # fewer than MIN_PREV previous matches: no baseline, delta 0 and flagged
    first = t.row(0, named=True)
    assert not first["ok_real"] and first["delta_real"] == 0.0
    # goalkeepers are not in A: quality of G never enters
    assert t.row(3, named=True)["delta_real"] == pytest.approx(0.0)


def test_lineups_known_uses_role_minutes_and_ignores_realised() -> None:
    app = _app(6)
    export = pl.DataFrame([
        {"match_id": 105, "team_id": 1, "player_id": p, "pos": pos, "model": "model",
         "p_start": 1.0 if st else 0.0, "p_bench": 0.0 if st else 1.0,
         "emin_start": 80.0, "emin_bench": 20.0}
        for p, pos, st in [(1, "G", True), *[(p, "D", True) for p in range(2, 6)],
                           *[(p, "M", True) for p in range(6, 10)], (10, "F", True),
                           (11, "F", True), (12, "F", False)]
    ])  # fmt: skip
    t = lo.build(app, _hist(), _posts(), LEAGUES, export).sort("kickoff")
    last = t.row(5, named=True)
    a_lk = (80 * (4 * RATE["D"] + 4 * RATE["M"] + 2 * RATE["F"]) + 20 * RATE["F"]) / 90
    assert last["a_lk"] == pytest.approx(a_lk)
    assert last["a_24"] == pytest.approx(a_lk)
    assert last["n_lk_fallback"] == 0
    # the match's own minutes never reach lk: rebuild with them removed
    own = pl.col("match_id") == 105
    blank = app.filter(own).with_columns(minutes=pl.lit(None, pl.Int32))
    app2 = pl.concat([app.filter(~own), blank])
    t2 = lo.build(app2, _hist(), _posts(), LEAGUES, export).sort("kickoff")
    assert t2.row(5, named=True)["delta_lk"] == pytest.approx(last["delta_lk"])


def test_future_rows_do_not_change_the_offset() -> None:
    app = _app(12)
    key = pl.DataFrame({"match_id": [107], "team_id": [1]})
    full = lo.build(app, _hist(), _posts(), LEAGUES, only=key)
    cut = app.filter(pl.col("kickoff") <= T0 + timedelta(days=7 * 7))
    part = lo.build(cut, _hist(), _posts(), LEAGUES, only=key)
    for c in ("a_bar", "a_real", "delta_real"):
        assert part[c][0] == pytest.approx(full[c][0])


def test_attach_fills_zero() -> None:
    df = pl.DataFrame({"match_id": [100, 200], "home": [1, 2], "away": [2, 1]})
    table = pl.DataFrame({"match_id": [100], "team_id": [1], "delta_real": [0.1],
                          "delta_lk": [0.2], "delta_24": [0.3]})  # fmt: skip
    out = lo.attach(df, table, ("lk", "24")).sort("match_id")
    # training rows read the lineups-known version, never the realised one (amendment 8)
    assert out["lu_home"].to_list() == [0.2, 0.0]
    assert out["dlk_home"].to_list() == [0.2, 0.0]
    assert out["d24_away"].to_list() == [0.0, 0.0]


def test_serving_beta_recovers_the_coefficient() -> None:
    rng = np.random.default_rng(0)
    n = 20_000
    lam_h, lam_a = rng.uniform(0.8, 2.0, n), rng.uniform(0.6, 1.6, n)
    dh, da = rng.normal(0, 0.15, n), rng.normal(0, 0.15, n)
    hg, ag = rng.poisson(lam_h * np.exp(0.8 * dh)), rng.poisson(lam_a * np.exp(0.8 * da))
    b = el.serving_beta(lam_h, lam_a, hg.astype(float), ag.astype(float), dh, da)
    assert b == pytest.approx(0.8, abs=0.08)
    assert el.serving_beta(lam_h, lam_a, hg.astype(float), ag.astype(float),
                           np.zeros(n), np.zeros(n)) == 0.0  # fmt: skip


def _pred(gain: float) -> pl.DataFrame:
    rng = np.random.default_rng(1)
    rows = []
    for i in range(400):
        base = {
            "match_id": i,
            "league_id": 39 if i % 2 else 40,
            "season": 2019,
            "cold_start": False,
            "hg": 1,
            "ag": 1,
            "delta_home": 0.1,
            "delta_away": 0.0,
        }
        p = rng.uniform(0.9, 1.1)
        for model, k in (("P", 0.0), ("S_lk", 0.3), ("L_lk", 1.0), ("S_24", 0.1), ("L_24", 0.2)):
            v = p * (1 - gain * k) + rng.normal(0, 0.001)
            rows.append({**base, "model": model, "1X2:log_loss": v, "1X2:rps": v / 5,
                         "OU2.5:log_loss": p, "BTTS:log_loss": p, "total:crps": p})  # fmt: skip
    return pl.DataFrame(rows)


def _diags(ok: bool = True) -> list[dict[str, object]]:
    return [{"model": "L", "gates_ok": ok, "fit_s": 100.0, "cached": False},
            {"model": "P", "gates_ok": True, "fit_s": 90.0, "cached": False}]  # fmt: skip


def test_verdict_pass_and_fail() -> None:
    v = el.verdict(_pred(0.02), _diags(), True)
    assert v.passed, [c for c in v.checks if not c["passed"]]
    assert not el.verdict(_pred(0.02), _diags(ok=False), True).passed
    assert not el.verdict(_pred(-0.02), _diags(), True).passed
    assert not el.verdict(_pred(0.02), _diags(), False).passed


def test_runspec_lineup_tag_and_production_unchanged() -> None:
    from adatta.eval import harness
    from adatta.model import backtest as bt

    p = bt.production_spec(harness.load_config())
    assert p.model_name == "phase1b_strength_open_xg"
    lu = replace(p, lineup=True, lineup_table="abc")
    assert lu.model_name == "phase1b_strength_open_xg_lu"
    assert bt.config_hash(lu) != bt.config_hash(p)
    assert bt.config_hash(replace(p, lineup_table="")) == bt.config_hash(p)


def test_prepare_reads_lineup_columns() -> None:
    from adatta.model import team_strength as ts

    k = [T0 + timedelta(days=7 * i) for i in range(4)]
    df = pl.DataFrame({"match_id": [1, 2, 3, 4], "home": [1, 2, 1, 2], "away": [2, 1, 2, 1],
                       "kickoff": k, "league_id": [39] * 4, "season": [2019] * 4,
                       "hg": [1, 0, 2, 1], "ag": [0, 0, 1, 1],
                       "lu_home": [0.1, None, 0.0, -0.2],
                       "lu_away": [0.0, 0.3, None, 0.0]})  # fmt: skip
    fd = ts.prepare(df, [39], T0 + timedelta(days=30), 365)
    assert fd.lu_home is not None and fd.lu_away is not None
    assert fd.lu_home.tolist() == [0.1, 0.0, 0.0, -0.2]
    assert fd.lu_away.tolist() == [0.0, 0.3, 0.0, 0.0]


def test_newcomer_fallback_uses_earlier_matches_only() -> None:
    k1, k2 = T0, T0 + timedelta(days=7)
    p3b = pl.DataFrame({"kickoff": [k1, k1, k2], "line": ["F", "F", "F"],
                        "emin_start": [70.0, 80.0, 10.0],
                        "emin_bench": [20.0, 10.0, 0.0]})  # fmt: skip
    rows = pl.DataFrame({"kickoff": [k1, k2, k2 + timedelta(days=1)], "line": ["F", "F", "F"],
                         "started": [True, True, False]})  # fmt: skip
    fb = lo.role_fallback(p3b, rows).to_list()
    assert fb[0] is None  # nothing earlier
    assert fb[1] == pytest.approx(75.0)  # same-time rows excluded
    assert fb[2] == pytest.approx(10.0)


def _part(
    t0: datetime, n: int, ok: list[bool], tag: str
) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """A stored part: n refits a week apart, rows for P, S_lk and L_lk, L gates as given."""
    times = [t0 + timedelta(days=7 * k) for k in range(n)]
    rows = pl.DataFrame([{"match_id": k, "as_of": t, "model": m, "1X2:log_loss": 1.0, "src": tag}
                         for k, t in enumerate(times) for m in ("P", "S_lk", "L_lk")],
                        schema_overrides={"as_of": pl.Datetime("us", "UTC")})  # fmt: skip
    diags = [{"model": m, "as_of": t.isoformat(), "gates_ok": m == "P" or ok[k],
              "fit_s": 100.0, "cached": False, "src": tag}
             for k, t in enumerate(times) for m in ("P", "L")]  # fmt: skip
    return rows, diags


def test_redo_merge_replaces_only_the_redone_l_refits() -> None:
    from adatta.model import lineup as ml

    base, bd = _part(T0, 3, [True, False, True], "base")
    t1 = T0 + timedelta(days=7)
    over, od = _part(t1, 1, [True], "redo")
    for g in od:
        g["redone"] = g["model"] == "L"
    rows, diags = ml.merge_redo(base, bd, over, od)
    assert rows.height == base.height
    src = dict(zip(rows["model"] + "@" + rows["match_id"].cast(str), rows["src"], strict=True))
    # only L at the redone refit comes from the redo; P and S stay from the base run
    assert src["L_lk@0"] == "redo" and src["P@0"] == "base"
    assert sum(v == "redo" for v in src.values()) == 1
    assert sum(g["src"] == "redo" for g in diags) == 1 and len(diags) == len(bd)
    assert all(g["gates_ok"] for g in diags if g["model"] == "L")
    with pytest.raises(ValueError):
        ml.merge_redo(base, bd, *_part(T0 + timedelta(days=70), 1, [True], "x"))


def test_failing_lists_l_refits_per_part(tmp_path: object) -> None:
    import json as js

    from adatta.adapters.storage import LocalStore
    from adatta.model import lineup as ml

    store = LocalStore(str(tmp_path))
    for i, ok in enumerate(([True, True], [False, True])):
        _, d = _part(T0, 2, ok, "base")
        store.put(f"lineup/parts/R/england_2019_{i}of2.json", js.dumps({"diags": d}).encode())
    assert ml.failing(store, "R", ["england"], [2019], 2) == [
        {"bundle": "england", "season": 2019, "part": "1/2", "only": f"{T0:%Y-%m-%d}"}
    ]


def test_redone_refits_are_not_timed() -> None:
    d = _diags()
    d.append({"model": "L", "gates_ok": True, "fit_s": 10_000.0, "cached": False,
              "redone": True})  # fmt: skip
    v = el.verdict(_pred(0.02), d, True)
    timing = next(c for c in v.checks if "median refit" in c["check"])
    assert timing["value"] == pytest.approx(100.0 / 90.0)
