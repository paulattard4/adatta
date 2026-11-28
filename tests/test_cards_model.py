"""P3b-3 (D66): the cards model's variants and recovery of a referee effect. Needs PyMC
(model group, Linux / Docker, D13); skipped elsewhere."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

pytest.importorskip("pymc")

from adatta.model import cards as mc  # noqa: E402


def _data(seed: int = 0, n_weeks: int = 120) -> tuple[pl.DataFrame, np.ndarray]:
    """Eight teams, six referees with effects -0.4..+0.4, team cards Poisson(mu g) with one
    lognormal term g per match, sd 0.3 (D69)."""
    rng = np.random.default_rng(seed)
    refs = np.linspace(-0.4, 0.4, 6)
    rows = []
    mid = 0
    k0 = datetime(2019, 8, 5, 15, tzinfo=UTC)
    for w in range(n_weeks):
        teams = rng.permutation(8) + 1
        for i in range(0, 8, 2):
            mid += 1
            r = int(rng.integers(6))
            g = float(np.exp(rng.normal(0.0, 0.3)))
            for t, o, home in ((teams[i], teams[i + 1], True), (teams[i + 1], teams[i], False)):
                mu = 2.0 * np.exp(refs[r] + (0.1 if home else 0.0) * 0)
                y = rng.poisson(mu * g)  # D69: the match's shared term
                rows.append({"match_id": mid, "team_id": int(t), "opp_id": int(o),
                             "is_home": home, "league_id": 39, "season": 2019 + w // 52,
                             "kickoff": k0 + timedelta(days=7 * w), "bundle": "england",
                             "cards": int(y), "referee": f"England|R{r}"})  # fmt: skip
    return pl.DataFrame(rows), refs


def test_each_variant_adds_its_terms() -> None:
    t, _ = _data(n_weeks=10)
    d = mc.design(t)
    names = {v: {x.name for x in mc.build(d, v).free_RVs} for v in mc.VARIANTS}
    assert {"log_nu", "sigma_match", "z_match"} <= names["C0"]
    assert names["C1"] - names["C0"] == {"sigma_a", "sigma_b", "z_a", "z_b"}
    assert names["C2"] - names["C1"] == {"sigma_ref", "z_ref"}
    for v in mc.VARIANTS:
        m = mc.build(d, v)
        assert np.isfinite(m.compile_logp()(m.initial_point()))
    with pytest.raises(ValueError):
        mc.build(mc.design(t.with_columns(referee=pl.lit(None, pl.Utf8))), "C2")


def test_the_likelihood_is_cmp_with_a_shared_match_term() -> None:
    """D69 (b): the model's log likelihood is the CMP log pmf of every row, its log rate
    nu (eta + e_match), summed (the normaliser over 0..K_Z)."""
    from scipy.special import gammaln, logsumexp

    t, _ = _data(n_weeks=6)
    d = mc.design(t)
    m = mc.build(d, "C0")
    pt0 = m.initial_point()
    rng = np.random.default_rng(2)
    pt0["z_match"] = rng.standard_normal(pt0["z_match"].shape)
    pt0["sigma_match_log__"] = np.log(0.3)
    pt0["log_nu"] = np.array([np.log(1.4)])
    pt0["c_league"] = np.array([np.log(2.1)])
    got = float(m.compile_logp(vars=m.potentials)(pt0))
    eta = np.log(2.1) + pt0["h"] * d.home + pt0["delta_ls"][d.ls] + 0.3 * pt0["z_match"][d.match]
    loglam = 1.4 * eta
    kk = np.arange(mc.K_Z + 1)
    log_z = logsumexp(loglam[:, None] * kk - 1.4 * gammaln(kk + 1), axis=1)
    want = float(np.sum(d.y * loglam - 1.4 * gammaln(d.y + 1) - log_z))
    assert got == pytest.approx(want, rel=1e-9)


def test_referee_effects_are_recovered() -> None:
    t, refs = _data()
    d = mc.design(t)
    post, diag = mc.fit(d, "C2", draws=500, chains=2)
    r = post["r"].mean(axis=1)
    order = [d.index.refs.index(f"England|R{i}") for i in range(6)]
    assert np.corrcoef(r[order], refs)[0, 1] > 0.9
    assert 0.15 < float(post["sigma_ref"].mean()) < 0.6
    assert diag["divergences"] == 0
    assert mc.ppc(post, d)["pass"]
