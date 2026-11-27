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
    """Eight teams, six referees with effects -0.4..+0.4, team cards NB(mu, 15)."""
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
            for t, o, home in ((teams[i], teams[i + 1], True), (teams[i + 1], teams[i], False)):
                mu = 2.0 * np.exp(refs[r] + (0.1 if home else 0.0) * 0)
                y = rng.negative_binomial(15, 15 / (15 + mu))
                rows.append({"match_id": mid, "team_id": int(t), "opp_id": int(o),
                             "is_home": home, "league_id": 39, "season": 2019 + w // 52,
                             "kickoff": k0 + timedelta(days=7 * w), "bundle": "england",
                             "cards": int(y), "referee": f"England|R{r}"})  # fmt: skip
    return pl.DataFrame(rows), refs


def test_each_variant_adds_its_terms() -> None:
    t, _ = _data(n_weeks=10)
    d = mc.design(t)
    names = {v: {x.name for x in mc.build(d, v).free_RVs} for v in mc.VARIANTS}
    assert names["C1"] - names["C0"] == {"sigma_a", "sigma_b", "z_a", "z_b"}
    assert names["C2"] - names["C1"] == {"sigma_ref", "z_ref"}
    for v in mc.VARIANTS:
        m = mc.build(d, v)
        assert np.isfinite(m.compile_logp()(m.initial_point()))
    with pytest.raises(ValueError):
        mc.build(mc.design(t.with_columns(referee=pl.lit(None, pl.Utf8))), "C2")


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
