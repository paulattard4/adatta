"""Manager-change widening (§5.2, D12). Needs PyMC (model group, Linux / Docker, D13);
skipped elsewhere."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

pytest.importorskip("pymc")


def fit_data(mgr: np.ndarray | None) -> object:
    from adatta.model import team_strength as ts

    return ts.FitData(
        home=np.array([0, 1, 2, 0, 1, 2]), away=np.array([1, 2, 0, 2, 0, 1]),
        league=np.zeros(6, dtype=np.int64), step=np.array([0, 0, 0, 1, 1, 1]),
        hg=np.array([1, 0, 2, 1, 1, 0]), ag=np.array([0, 1, 1, 2, 0, 0]),
        season_start=np.zeros(2, dtype=bool), team_ids=np.array([10, 11, 12]), leagues=[39],
        start=datetime(2023, 1, 2, tzinfo=UTC), n_steps=2, mgr=mgr,
    )  # fmt: skip


def test_widening_adds_change_weeks_and_keeps_the_walk_zero_sum() -> None:
    import pymc as pm

    from adatta.model import team_strength as ts

    mgr = np.array([[0.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    model = ts.build(fit_data(mgr), structure="strength_open", manager=True)  # type: ignore[arg-type]
    assert "change_weeks" in {v.name for v in model.free_RVs}
    assert np.isfinite(model.compile_logp()(model.initial_point()))
    draw = pm.draw(model["attack_last"] + model["defence_last"], random_seed=1)
    assert abs(float(np.sum(draw))) < 1e-8  # strength is zero-sum over teams


def test_no_changes_is_the_unwidened_model() -> None:
    from adatta.model import team_strength as ts

    plain = ts.build(fit_data(None), structure="strength_open")  # type: ignore[arg-type]
    flat = ts.build(fit_data(np.zeros((2, 3))), structure="strength_open",  # type: ignore[arg-type]
                    manager=True)  # fmt: skip
    assert {v.name for v in flat.free_RVs} == {v.name for v in plain.free_RVs}
