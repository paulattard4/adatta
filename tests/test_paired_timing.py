"""Paired refit timing (docs/PHASE2_TASKS.md, M4b (d')): fixed dates, alternating order,
fresh fits on the same machine, and item 4's verdict per bundle."""

from dataclasses import replace
from typing import Any

import pytest
from test_backtest import LEAGUES, FakeFitter, config, matches
from test_covariates import with_context

from adatta.eval import harness
from adatta.model import backtest as bt
from adatta.model import paired_timing as pt
from adatta.model import team_strength as ts


@pytest.mark.parametrize(("n", "want"), [(0, []), (1, [0]), (2, [0, 1]), (6, [1, 3, 5]),
                                         (36, [6, 18, 30]), (37, [6, 18, 30])])  # fmt: skip
def test_fixed_positions(n: int, want: list[int]) -> None:
    assert pt.sample_positions(n) == want


def test_both_specs_fitted_fresh_at_each_date_in_alternating_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    df, cfg = with_context(matches()), config()
    ref = replace(bt.production_spec(cfg), every_days=28)
    chal = replace(ref, groups=("style",))
    calls: list[tuple[str, Any]] = []
    fake = FakeFitter()

    def fit(spec: bt.RunSpec, fd: ts.FitData) -> tuple[bt.Draws, bt.Diag]:
        calls.append(("challenger" if spec.groups else "reference", len(fd.hg)))
        assert bool(fd.cov) == bool(spec.groups)  # the challenger's design, not the 1b one
        _, diag = fake(fd, spec.seed)
        return {}, diag | {"fit_s": 200.0 if spec.groups else 100.0}

    def no_cache(*a: Any, **k: Any) -> None:
        raise AssertionError("the refit cache must not be used")

    monkeypatch.setattr(bt, "try_get", no_cache)
    rows = pt.time_pairs(df, LEAGUES, "england", 2019, cfg, ref, chal, fit, log=lambda _: None)
    plan = harness.refit_plan(df, cfg, [2019], 28)
    assert [r["position"] for r in rows] == pt.sample_positions(len(plan))
    want = [plan[p][0].isoformat() for p in pt.sample_positions(len(plan))]
    assert [r["as_of"] for r in rows] == want
    assert [r["first"] for r in rows] == ["reference", "challenger", "reference"]
    firsts = [calls[i][0] for i in range(0, len(calls), 2)]
    assert firsts == ["reference", "challenger", "reference"]
    assert all(calls[i][1] == calls[i + 1][1] for i in range(0, len(calls), 2))  # same date
    assert all(r["challenger_fit_s"] == 200.0 and r["reference_fit_s"] == 100.0 for r in rows)


def _rows(bundle: str, ratio: float, seasons: tuple[int, ...] = pt.DEV_SEASONS,
          n: int = 36) -> list[dict[str, Any]]:  # fmt: skip
    return [{"bundle": bundle, "season": s, "position": p, "n_refits": n,
             "reference_fit_s": 400.0 + p, "challenger_fit_s": (400.0 + p) * ratio}
            for s in seasons for p in pt.sample_positions(n)]  # fmt: skip


def test_verdict_passes_every_bundle_at_or_under_the_limit() -> None:
    rows = _rows("a", 1.25) + _rows("b", 0.9)
    rep = pt.verdict(rows, ["a", "b"])
    assert rep["verdict"] == "pass"
    assert [round(r["ratio"], 2) for r in rep["bundles"]] == [1.25, 0.9]
    assert "**pass**" in pt.markdown(rep)


def test_verdict_fails_one_bundle_over_the_limit() -> None:
    rep = pt.verdict(_rows("a", 1.0) + _rows("b", 1.3), ["a", "b"])
    assert rep["verdict"] == "fail"
    assert [r["passed"] for r in rep["bundles"]] == [True, False]


def test_a_missing_season_or_date_is_not_measured() -> None:
    short = _rows("a", 1.0, seasons=(2019, 2020, 2021, 2022))
    missing_date = _rows("b", 1.0)[1:]
    rep = pt.verdict(short + missing_date + _rows("c", 1.0), ["a", "b", "c", "d"])
    assert rep["verdict"] == "not measured"
    assert [r["passed"] for r in rep["bundles"]] == [False, False, True, False]
    assert [r["ratio"] for r in rep["bundles"]][:2] == [None, None]
