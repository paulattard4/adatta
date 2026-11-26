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
