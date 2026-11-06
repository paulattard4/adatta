"""§14.5 gate checks on synthetic inputs (the ArviZ-based convergence check runs in the
model container only)."""

import numpy as np

from adatta.model import diagnostics as dg


def test_ppc_passes_for_matching_data_and_fails_for_overdispersed() -> None:
    rng = np.random.default_rng(0)
    obs_h, obs_a = rng.poisson(1.5, 400), rng.poisson(1.1, 400)
    sim_h, sim_a = rng.poisson(1.5, (500, 400)), rng.poisson(1.1, (500, 400))
    assert all(c.passed for c in dg.ppc(obs_h, obs_a, sim_h, sim_a, "x"))
    # observed goals far more dispersed than the Poisson model predicts
    wild_h = rng.negative_binomial(1, 1 / 2.5, 400)
    checks = {c.name: c for c in dg.ppc(wild_h, obs_a, sim_h, sim_a, "x")}
    assert not checks["ppc x total_dispersion"].passed


def test_identifiability_flags_a_confounded_level() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=1000)
    assert dg.identifiability(x, rng.normal(size=1000), "a").passed
    assert not dg.identifiability(x, x + 0.01 * rng.normal(size=1000), "b").passed


def test_sensitivity_and_truncation_and_reproducibility() -> None:
    base, sd = {"a": 1.0, "b": 0.0}, {"a": 0.1, "b": 0.1}
    assert dg.sensitivity(base, sd, {"a": 1.05, "b": 0.02}, "x2").passed
    assert not dg.sensitivity(base, sd, {"a": 1.3, "b": 0.0}, "x2").passed
    assert dg.predictive_sensitivity(1.000, 1.001, "x2").passed
    assert not dg.predictive_sensitivity(1.000, 1.01, "x2").passed
    assert dg.truncation(np.full(1000, 1e-6)).passed
    assert not dg.truncation(np.r_[np.full(990, 1e-6), np.full(10, 1e-3)]).passed
    a = np.arange(6.0)
    assert dg.reproducible(a, a.copy()).passed and not dg.reproducible(a, a + 1e-12).passed


def test_proxy_predictive_check_is_non_blocking() -> None:
    c = dg.predictive_sensitivity(1.000, 1.01, "x2 (proxy)", blocking=False)
    assert not c.passed and not c.blocking and not c.as_dict()["blocking"]
