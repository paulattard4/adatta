"""AR(1) form (§5.2): the closed-form matrix equals the recursion. Needs PyMC (model group,
Linux / Docker, D13); skipped elsewhere."""

from __future__ import annotations

import numpy as np
import pytest

pm = pytest.importorskip("pymc")


def test_ar1_form_matches_recursion() -> None:
    from adatta.model import team_strength as ts

    n_steps, teams = 6, ["a", "b", "c"]
    with pm.Model(coords={"step": list(range(n_steps)), "team": teams}):
        m = ts.ar1_form(n_steps)
        fn = pm.compile([pm.modelcontext(None)["logit_phi"],
                         pm.modelcontext(None)["sigma_form"],
                         pm.modelcontext(None)["form_z"]], m)  # fmt: skip
    rng = np.random.default_rng(0)
    e = rng.normal(size=(n_steps, 3))
    e -= e.mean(axis=1, keepdims=True)
    phi, sigma = 0.7, 0.2
    got = fn(np.log(phi / (1 - phi)), sigma, e)
    want = np.zeros_like(e)
    want[0] = sigma / np.sqrt(1 - phi**2) * e[0]
    for t in range(1, n_steps):
        want[t] = phi * want[t - 1] + sigma * e[t]
    assert np.allclose(got, want)
    assert np.allclose(got.sum(axis=1), 0.0)  # zero-sum over teams at every step
