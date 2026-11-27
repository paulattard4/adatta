"""C1 v2 (D62): the UEFA draw term's fit, with PyMC (the model group; CI's model job)."""

import numpy as np
import polars as pl
import pytest

from adatta.model import continental as ct


def test_the_draw_term_is_recovered_from_simulated_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1 v2's fit recovers a known low-score dependence per phase (Dixon-Coles likelihood)."""
    pytest.importorskip("pymc")
    from scipy.stats import poisson

    from adatta.eval import cup_terms

    monkeypatch.setattr(ct, "DRAWS", 500)
    monkeypatch.setattr(ct, "CHAINS", 2)
    rng = np.random.default_rng(3)
    n = 4000
    true = {"group_stage": 0.0, "league_phase": 0.12, "qualifying": -0.05, "knockout": -0.1}
    phases = rng.choice(ct.PHASES, n)
    bundles = list(ct.BUNDLE_ORDER)
    bh = rng.choice(bundles, n)
    ba = np.array([rng.choice([b for b in bundles if b != x]) for x in bh])
    lam, mu = np.full(n, np.exp(0.2 + 0.25)), np.full(n, np.exp(0.2))
    hg, ag = np.zeros(n, dtype=int), np.zeros(n, dtype=int)
    k = np.arange(11)
    for i in range(n):
        m = cup_terms.matrices(lam[i], mu[i], np.array(true[phases[i]]), np.array(0.0), 10)
        cell = rng.choice(m.size, p=m.reshape(-1) / m.sum())
        hg[i], ag[i] = divmod(int(cell), 11)
    assert poisson.pmf(k, 1.0).sum() > 0.99  # the 10-goal cap loses nothing here
    train = pl.DataFrame({"bundle_home": bh, "bundle_away": ba, "neutral": [False] * n,
                          "att_home": [0.0] * n, "def_home": [0.0] * n, "att_away": [0.0] * n,
                          "def_away": [0.0] * n, "hg": hg, "ag": ag, "phase": phases,
                          "rho_home": [[0.0]] * n})  # fmt: skip
    post, diag = ct.fit_q(train, True, rho_u=True)
    got = dict(zip(ct.PHASES, post["rho_u"].mean(axis=0), strict=True))
    for ph, v in true.items():
        assert abs(got[ph] - v) < 0.06, (ph, got[ph], v)
    assert set(diag["rho_u"]) == set(ct.PHASES)
