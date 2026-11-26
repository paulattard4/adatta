"""C2 step 3 (D54): the Phase 1a bar on the eight top flights."""

from __future__ import annotations

import polars as pl
import pytest

from adatta.eval import c2_serve


def _pred(scores: dict[int, dict[str, float]]) -> pl.DataFrame:
    """Two warm matches per league and model, one cold row that must be ignored."""
    rows = []
    for lid, by_model in scores.items():
        for m, ll in by_model.items():
            for k, d in enumerate((-0.01, 0.01)):
                rows.append({"match_id": lid * 10 + k, "league_id": lid, "model": m,
                             "cold_start": False, "1X2:log_loss": ll + d})  # fmt: skip
            rows.append({"match_id": lid * 10 + 9, "league_id": lid, "model": m,
                         "cold_start": True, "1X2:log_loss": 9.0})  # fmt: skip
    return pl.DataFrame(rows)


def test_a_league_passes_only_below_both_baselines() -> None:
    pred = _pred({
        1: {"dixon_coles": 0.95, "league_poisson": 0.99, "base_rate": 1.02},
        2: {"dixon_coles": 1.00, "league_poisson": 0.99, "base_rate": 1.02},  # above Poisson
        3: {"dixon_coles": 1.02, "league_poisson": 1.03, "base_rate": 1.02},  # ties base rate
    })  # fmt: skip
    bar = c2_serve.baseline_bar(pred, [1, 2, 3])
    assert bar["passed"] == [1] and bar["failed"] == [2, 3]
    assert bar["leagues"][1]["n"] == 2  # warm only
    assert bar["leagues"][1]["log_loss"]["dixon_coles"] == pytest.approx(0.95)
    assert bar["leagues"][1]["paired"]["base_rate"]["mean_diff"] == pytest.approx(-0.07)
    assert len(c2_serve.lines(bar)) == 5


def test_a_missing_top_flight_gives_no_bar() -> None:
    pred = _pred({1: {"dixon_coles": 0.95, "league_poisson": 0.99, "base_rate": 1.02}})
    with pytest.raises(ValueError, match="top flights scored"):
        c2_serve.baseline_bar(pred, [1, 2])


def test_milestones_from_fourteen_days_ahead_include_every_timed_window() -> None:
    from datetime import UTC, datetime, timedelta

    k = datetime(2026, 12, 5, 15, tzinfo=UTC)
    assert c2_serve.milestones_ok(k, k - timedelta(days=14))
    assert not c2_serve.milestones_ok(k, k - timedelta(days=3))  # T-7d already past


def test_the_shadow_passes_only_with_every_check() -> None:
    rows = []
    for mid, (p1a, base) in enumerate(((0.9, 1.0), (1.0, 1.05))):
        for model, ll in (("dixon_coles", p1a), ("base_rate", base)):
            rows.append({"match_id": mid, "league_id": 94, "model": model, "as_of": 1,
                         "1X2:log_loss": ll, "cold_start": False, "tail": 1e-9,
                         "sum_err": 0.0, "milestones_ok": True, "p_home": 0.5, "p_draw": 0.3,
                         "p_away": 0.2, "hg": 1, "ag": 0})  # fmt: skip
    ok = c2_serve.shadow_verdict(pl.DataFrame(rows))
    assert ok["pass"] and ok["n"] == 2 and ok["pooled"]["mean_diff"] == pytest.approx(-0.075)
    worse = pl.DataFrame(rows).with_columns(
        tail=pl.when(pl.col("match_id") == 1).then(1e-3).otherwise(pl.col("tail"))
    )
    bad = c2_serve.shadow_verdict(worse)
    assert not bad["pass"] and not bad["checks"]["truncation"]
    assert "FAILS" in c2_serve.shadow_lines(bad)[-1]


def test_champions_are_the_leagues_passing_b_and_d() -> None:
    bar = {"leagues": {"94": {"pass": True}, "88": {"pass": False}}}
    assert c2_serve.servable(bar, {"pass": True}) == [94]
    assert c2_serve.servable(bar, {"pass": False}) == []
