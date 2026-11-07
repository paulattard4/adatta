"""xG auxiliary likelihood (§5.2). Needs PyMC (model group, Linux / Docker, D13); skipped
elsewhere."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

pytest.importorskip("pymc")


def fit_data(xg: bool) -> object:
    from adatta.model import team_strength as ts

    n = 6
    return ts.FitData(
        home=np.array([0, 1, 2, 0, 1, 2]), away=np.array([1, 2, 0, 2, 0, 1]),
        league=np.zeros(n, dtype=np.int64), step=np.array([0, 0, 0, 1, 1, 1]),
        hg=np.array([1, 0, 2, 1, 1, 0]), ag=np.array([0, 1, 1, 2, 0, 0]),
        season_start=np.zeros(2, dtype=bool), team_ids=np.array([10, 11, 12]), leagues=[39],
        start=datetime(2023, 1, 2, tzinfo=UTC), n_steps=2,
        xg_home=np.array([1.2, np.nan, 0.0, 0.9, 1.1, 1.4]) if xg else None,
        xg_away=np.array([0.7, 1.0, 0.3, np.nan, 0.8, 0.6]) if xg else None,
    )  # fmt: skip


def test_xg_uses_matches_with_both_sides_and_floors_zero() -> None:
    from adatta.model import team_strength as ts

    model = ts.build(fit_data(True), structure="strength_open", xg=True)  # type: ignore[arg-type]
    observed = {v.name: v for v in model.observed_RVs}
    home = model.rvs_to_values[observed["home_xg"]].eval()
    assert len(home) == 4 and home.min() == ts.XG_FLOOR
    assert np.isfinite(model.compile_logp()(model.initial_point()))


def test_no_xg_is_the_goals_model() -> None:
    from adatta.model import team_strength as ts

    goals = ts.build(fit_data(False), structure="strength_open")  # type: ignore[arg-type]
    gated = ts.build(fit_data(False), structure="strength_open", xg=True)  # type: ignore[arg-type]
    assert {v.name for v in gated.free_RVs} == {v.name for v in goals.free_RVs}
    assert {v.name for v in gated.observed_RVs} == {v.name for v in goals.observed_RVs}
