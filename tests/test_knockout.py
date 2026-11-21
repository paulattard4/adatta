"""Stage B (docs/BEYOND_LEAGUES_PLAN.md): format rules, tie paths, who advanced, extra time
and P(advance)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from adatta.eval import cup_terms
from adatta.eval import knockout as ek
from adatta.features import cup_ties, knockout

K = datetime(2022, 1, 8, 15, tzinfo=UTC)
RULES = knockout.load_rules()


def _p(mid: int, kind: str, home: int, away: int, status: str, nt: tuple[int, int],
       et: tuple[int | None, int | None] = (None, None),
       pens: tuple[int | None, int | None] = (None, None),
       days: int = 0) -> knockout.Played:  # fmt: skip
    return knockout.Played(mid, kind, home, away, K + timedelta(days=days), status, nt, et, pens)


def test_rules_by_cup_season_and_round() -> None:
    fa = knockout.rule_for(RULES, 45, 2022, "3rd Round")
    assert fa is not None and fa.replay and fa.extra_time
    assert not knockout.rule_for(RULES, 45, 2022, "5th Round").replay  # type: ignore[union-attr]
    assert not knockout.rule_for(RULES, 45, 2026, "1/128-finals").replay  # type: ignore[union-attr]
    q = knockout.rule_for(RULES, 45, 2020, "2nd Round Qualifying")
    assert q is not None and not q.replay and not q.extra_time  # 2020/21: straight to penalties
    sf = knockout.rule_for(RULES, 143, 2019, "Semi-finals")
    assert sf is not None and sf.legs == 2 and sf.away_goals == knockout.ALSO_ET
    assert knockout.rule_for(RULES, 143, 2023, "Semi-finals").away_goals == knockout.NONE  # type: ignore[union-attr]
    assert not knockout.rule_for(RULES, 66, 2021, "Round of 64").extra_time  # type: ignore[union-attr]
    assert knockout.rule_for(RULES, 66, 2021, "Final").extra_time  # type: ignore[union-attr]
    assert knockout.rule_for(RULES, 81, 2022, "2nd Round") == knockout.Rule()


def test_check_paths_against_rules() -> None:
    et = knockout.Rule()
    t = knockout.Tie(
        81, 2022, "2nd Round", 1, 2, [_p(1, "single", 1, 2, "PEN", (1, 1), (0, 0), (4, 3))]
    )
    assert knockout.check(t, et) is None
    t = knockout.Tie(81, 2022, "2nd Round", 1, 2, [_p(1, "single", 1, 2, "FT", (1, 1))])
    assert knockout.check(t, et) == "level, ended at 90' where extra time applies"
    rep = knockout.Rule(replay=True)
    t = knockout.Tie(45, 2022, "3rd Round", 1, 2, [_p(1, "single", 1, 2, "FT", (0, 0)),
                                                    _p(2, "replay", 2, 1, "AET", (1, 1), (0, 1),
                                                       days=10)])  # fmt: skip
    assert knockout.check(t, rep) is None
    assert (
        knockout.check(knockout.Tie(45, 2022, "3rd Round", 1, 2, t.matches[:1]), rep)
        == "level, no replay"
    )
    two = knockout.Rule(legs=2, away_goals=knockout.AFTER_90)
    legs = [_p(1, "leg1", 1, 2, "FT", (1, 0)), _p(2, "leg2", 2, 1, "FT", (2, 1), days=7)]
    assert (
        knockout.check(knockout.Tie(143, 2019, "Semi-finals", 1, 2, legs), two) is None
    )  # 2-2, A away 1
    legs2 = [legs[0], _p(2, "leg2", 2, 1, "FT", (1, 0), days=7)]  # 1-1, away goals level
    assert knockout.check(knockout.Tie(143, 2019, "Semi-finals", 1, 2, legs2), two) == \
        "level aggregate, leg 2 ended at 90'"  # fmt: skip


def test_who_advanced() -> None:
    t = knockout.Tie(
        81, 2022, "Final", 1, 2, [_p(1, "single", 1, 2, "PEN", (1, 1), (0, 0), (3, 4))]
    )
    assert knockout.advanced(t, set()) == (2, "shootout")
    assert knockout.advanced(t, {1, 9}) == (1, "later")  # a later round wins over the record
    legs = [_p(1, "leg1", 1, 2, "FT", (2, 0)), _p(2, "leg2", 2, 1, "AET", (2, 0), (1, 0), days=7)]
    assert knockout.advanced(knockout.Tie(143, 2022, "Semi-finals", 1, 2, legs), set()) == (
        2,
        "result",
    )


def test_extra_time_and_single_tie_probabilities() -> None:
    lam, mu = np.array([1.5, 1.5]), np.array([1.0, 1.0])
    h, lev, a = ek.et_probs(lam, mu, 1.0)
    assert np.allclose(h + lev + a, 1) and (h > a).all()
    h0, lev0, _ = ek.et_probs(lam, mu, 1e-9)
    assert np.allclose(lev0, 1)  # kappa -> 0: no extra-time goals
    z = np.zeros(2)
    p, base = ek.single(lam, mu, z, z, knockout.Rule(), 1.0, 0.5, z, neutral=False)
    m = cup_terms.matrices(lam, mu, z, z, ek.MAX_GOALS)
    ph, pd = np.tril(m, -1).sum(axis=(-2, -1)), np.trace(m, axis1=-2, axis2=-1)
    assert np.allclose(base, ph + 0.5 * pd)
    assert np.allclose(p, ph + pd * (h + lev * 0.5))
    p_ne, _ = ek.single(lam, mu, z, z, knockout.Rule(extra_time=False), 1.0, 0.7, z, False)
    assert np.allclose(p_ne, ph + pd * 0.7)
    # equal sides, neutral replay with a coin-flip shootout: a fair tie
    eq = np.array([1.2])
    p_eq, _ = ek.single(eq, eq, np.zeros(1), np.zeros(1), knockout.Rule(replay=True), 1.0, 0.5,
                        np.zeros(1), neutral=True)  # fmt: skip
    assert p_eq[0] == pytest.approx(0.5, abs=1e-9)


def test_two_legs_uses_leg_one_and_away_goals() -> None:
    lam2, mu2, z = np.array([1.3]), np.array([1.1]), np.zeros(1)
    none = knockout.Rule(legs=2)
    p_up, _ = ek.two_legs(3, 0, lam2, mu2, z, z, none, 1.0, 0.5)
    p_even, base = ek.two_legs(0, 0, lam2, mu2, z, z, none, 1.0, 0.5)
    assert p_up[0] > 0.95 and 0.2 < p_even[0] < 0.6 and 0 < base[0] < 1
    # away goals: A won 1-0 at home, so B needs to win by two in leg 2 unless A scores
    ag = knockout.Rule(legs=2, away_goals=knockout.ALSO_ET)
    p_ag, _ = ek.two_legs(1, 0, lam2, mu2, z, z, ag, 1.0, 0.5)
    p_no, _ = ek.two_legs(1, 0, lam2, mu2, z, z, none, 1.0, 0.5)
    assert p_ag[0] != pytest.approx(p_no[0])
    assert ek.ece(np.array([0.2, 0.8]), np.array([0.0, 1.0])) == pytest.approx(0.2)


def test_walk_forward_uses_only_earlier_results() -> None:
    grid = np.array([-1.0, 0.0, 1.0])
    ll = np.array([[0.0, 0.0, 5.0], [5.0, 0.0, 0.0]])
    times = [K, K + timedelta(days=30)]
    fits = ek.walk(times, ll, [K, K + timedelta(days=31), K + timedelta(days=60)], grid, 1.0)
    assert fits[0][0] == pytest.approx(0.0)  # nothing final before the first: the prior
    assert fits[1][0] > 0.5  # only the first observation is final by then
    assert ek.fit_at([(K, 0.3), (K + timedelta(days=30), -0.2)], K + timedelta(days=10)) == 0.3
    assert cup_ties.base_round("3rd Round Replays") == "3rd Round"


def test_stretch_and_walk_forward_recalibration() -> None:
    import polars as pl

    assert ek.stretch(np.array([0.5]), 2.0)[0] == pytest.approx(0.5)
    assert ek.stretch(np.array([0.7]), 2.0)[0] > 0.7 > ek.stretch(np.array([0.7]), 0.5)[0]
    # forecasts too close to 0.5: favourites always go through; s grows over the walk
    n = 120
    rows = [{"kickoff": K + timedelta(days=7 * i), "end": K + timedelta(days=7 * i),
             "p": 0.6 if i % 2 else 0.4, "p_lo": 0.5, "p_hi": 0.7, "y": float(i % 2)}
            for i in range(n)]  # fmt: skip
    out, fits = ek.recalibrate(pl.DataFrame(rows))
    s = out["s"].to_numpy()
    prior = np.exp(ek.posterior_mean(np.zeros(len(ek.LOG_S)), ek.LOG_S, ek.S_PRIOR_SD)[0])
    assert s[0] == pytest.approx(prior) and prior == pytest.approx(1.025, abs=0.002)
    assert s[-1] > s[10] > prior  # the prior first (the grid is not symmetric), then up
    assert out["p"][-1] > 0.6
