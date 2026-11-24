"""Stage D v2 serving: national knockout ties (adatta/sim/ties.py). No database, no fits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from adatta.eval import knockout as kb
from adatta.features import knockout
from adatta.model import national as nat
from adatta.sim import ties
from adatta.sim import tournament as tm

RULE = knockout.Rule(2, False, True, knockout.NONE)
K = 6  # draws


def rates(seed: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    lam, mu = rng.uniform(0.6, 2.2, K), rng.uniform(0.6, 2.2, K)
    return lam, mu, np.full(K, 30.0), np.full(K, -0.04)


def test_between_legs_is_stage_b_on_the_same_matrices() -> None:
    lam2, mu2, phi, rho = rates(1)
    m2 = nat.score_matrix(lam2, mu2, phi, rho)
    for x1, y1 in ((0, 0), (2, 1), (0, 3), (4, 4)):
        for et in (True, False):
            rule = knockout.Rule(2, False, et, knockout.NONE)
            p = ties.between_legs(m2, x1, y1, lam2, mu2, et)
            sb, base = kb.two_legs_m(m2, x1, y1, lam2, mu2, rule, tm.KAPPA_ET, tm.P_SHOOTOUT)
            assert np.abs(p.a - sb).max() < 1e-12 and np.abs(p.base - base).max() < 1e-12


def test_single_is_b3_closed_form_and_paths_add_up() -> None:
    lam, mu, phi, rho = rates(2)
    m = nat.score_matrix(lam, mu, phi, rho)
    ph, pd = np.tril(m, -1).sum(axis=(-2, -1)), np.trace(m, axis1=-2, axis2=-1)
    eh, ed, _ = kb.et_probs(lam, mu, tm.KAPPA_ET)
    for et, want in ((True, ph + pd * (eh + ed * 0.5)), (False, ph + pd * 0.5)):
        p = ties.single(m, lam, mu, et)
        assert np.abs(p.a - want).max() < 1e-12
        total = sum(p.path.values())
        assert np.abs(total - 1).max() < 1e-9
        b = sum(p.path[("b", h)] for h in ties.HOW)
        assert np.abs(p.a + b - 1).max() < 1e-9


def test_before_leg1_is_the_mixture_over_leg_1_results() -> None:
    lam1, mu1, phi, rho = rates(3)
    lam2, mu2, _, _ = rates(4)
    m1 = nat.score_matrix(lam1, mu1, phi, rho, max_goals=8)
    m2 = nat.score_matrix(lam2, mu2, phi, rho, max_goals=8)
    p = ties.before_leg1(m1, m2, lam2, mu2, True)
    mix = sum(m1[:, x, y] * ties.between_legs(m2, x, y, lam2, mu2, True).a
              for x in range(9) for y in range(9))  # fmt: skip
    assert np.abs(p.a - mix).max() < 1e-12
    assert np.abs(sum(p.path.values()) - 1).max() < 1e-9


def test_before_leg1_matches_sampling_both_legs() -> None:
    lam1, mu1, phi, rho = (np.full(1, v) for v in (1.6, 0.9, 30.0, -0.04))
    lam2, mu2 = np.full(1, 1.4), np.full(1, 1.1)  # leg 2: B at home
    p = ties.before_leg1(nat.score_matrix(lam1, mu1, phi, rho),
                         nat.score_matrix(lam2, mu2, phi, rho), lam2, mu2, True)  # fmt: skip
    n = 200_000
    rng = np.random.default_rng(5)
    one = np.ones((1, n))  # one draw, n samples
    x1, y1 = nat.nb_dc_sample(lam1[0] * one, mu1[0] * one, phi, rho, rng)
    b2, a2 = nat.nb_dc_sample(lam2[0] * one, mu2[0] * one, phi, rho, rng)
    d = ((x1 + a2) - (y1 + b2))[0]
    ea, eb = rng.poisson(mu2[0] / 3, n), rng.poisson(lam2[0] / 3, n)
    coin = rng.random(n) < 0.5
    adv = (d > 0) | ((d == 0) & ((ea > eb) | ((ea == eb) & coin)))
    se = np.sqrt(p.a[0] * (1 - p.a[0]) / n)
    assert abs(adv.mean() - p.a[0]) < 4 * se


def leg(mid: int, h: int, a: int, day: int, status: str = "NS",
        nt: tuple[int, int] | None = None, et: tuple[int, int] | None = None,
        pens: tuple[int, int] | None = None) -> ties.Leg:  # fmt: skip
    return ties.Leg(mid, h, a, datetime(2027, 3, day, 19, tzinfo=UTC), "home", status, nt, et,
                    pens)  # fmt: skip


def test_winner_and_path_checks() -> None:
    t = ties.Tie("5", 2026, "Quarter-finals", 1, 2, RULE,
                 [leg(10, 1, 2, 25, "FT", (2, 1)), leg(11, 2, 1, 28, "FT", (1, 0))])  # fmt: skip
    assert ties.winner(t) == (None, "level after 90' but no extra time stored")
    t.legs[1] = leg(11, 2, 1, 28, "AET", (1, 0), (0, 1))
    assert ties.winner(t) == (1, None)  # A scored in extra time away
    t.legs[1] = leg(11, 2, 1, 28, "PEN", (1, 0), (0, 0), (5, 4))
    assert ties.winner(t) == (2, None)  # B (leg 2's home side) won the shoot-out
    t.legs[1] = leg(11, 2, 1, 28, "AET", (0, 0), (1, 0))
    assert ties.winner(t)[1] == "decided in 90' but stored as AET"
    t.legs[1] = leg(11, 2, 1, 28, "NS")
    assert ties.winner(t) == (None, None)


def test_grouping_and_rules() -> None:
    rules = ties.load_rules()
    rows = [
        ("5", 2026, "Quarter-finals - League A", leg(1, 1, 2, 25)),
        ("5", 2026, "Quarter-finals - League A", leg(2, 2, 1, 28)),
        ("5", 2026, "Play-offs A/B", leg(3, 3, 4, 25)),
        ("5", 2026, "Play-offs A/B", leg(4, 3, 4, 28)),  # leg 2 not reversed
        ("5", 2026, "Final", leg(5, 5, 6, 28)),
    ]  # no written rule: never a tie
    found, irregular = ties.group(rows, rules)
    assert [(t.a, t.b, len(t.legs)) for t in found] == [(1, 2, 2)] and irregular == [3, 4]


@pytest.mark.parametrize("bad", ["away_goals: after_90", "legs: 3", "source: none"])
def test_unsupported_or_unsourced_rules_are_refused(tmp_path: Path, bad: str) -> None:
    key = bad.split(":")[0]
    fields = {"legs": "2", "away_goals": "none", "source": "https://example.org"}
    fields[key] = bad.split(": ")[1]
    text = (
        "ties:\n  '5':\n  - seasons: [2026, 2026]\n    rounds: ['Play-offs ?/?']\n"
        "    extra_time: true\n" + "".join(f"    {k}: {v}\n" for k, v in fields.items())
    )
    p = tmp_path / "f.yml"
    p.write_text(text)
    with pytest.raises(ValueError):
        ties.load_rules(p)


def test_tie_prob_uses_only_what_is_final_three_hours_before() -> None:
    teams = [1, 2]
    k = 40
    rng = np.random.default_rng(1)
    post = {"c": np.full(k, 0.1), "g": np.zeros((k, 2)), "h": np.full(k, 0.3),
            "h_u": np.full(k, 0.2), "phi": np.full(k, 50.0), "rho": np.full(k, -0.03),
            "teams": np.array(teams), "s_last": 0.1 * rng.standard_normal((k, 2)),
            "o_last": np.zeros((k, 2)), "sigma_w": np.full(k, 0.04), "sigma_u": np.full(k, 1.0),
            "sigma_o": np.full(k, 0.2), "conf": np.zeros((k, 5))}  # fmt: skip
    l1 = leg(1, 1, 2, 25, "FT", (3, 0))
    t = ties.Tie("5", 2026, "Play-offs A/B", 1, 2, RULE, [l1, leg(2, 2, 1, 28)])
    rt = ties.tie_rates(post, t, {1: "UEFA", 2: "UEFA"}, 100, 60, [100, 100], "qualifier")
    early = ties.tie_prob(rt, t, [100, 100], l1.kickoff + timedelta(hours=2))
    late = ties.tie_prob(rt, t, [100, 100], l1.kickoff + timedelta(hours=3))
    assert early is not None and early.stage == "before_leg1"
    assert late is not None and late.stage == "between_legs" and late.a.mean() > early.a.mean()
    t.legs[1] = leg(2, 2, 1, 28, "FT", (0, 0))
    assert ties.tie_prob(rt, t, [100, 100], t.legs[1].kickoff + timedelta(hours=3)) is None
