"""Phase 2 M4 item 1: the inference gate with context groups. Without groups the gate is
the 1b gate unchanged (trace tag, headline); with groups each coefficient joins the
headline that the prior-sensitivity checks compare, and the fit and the targets carry
their own context rows. Pure numpy (no PyMC): the gate's fit itself runs in CI."""

from datetime import timedelta

import numpy as np
import polars as pl
from test_backtest import LEAGUES, matches
from test_covariates import CovFitter, with_context

from adatta.model import covariates as cv
from adatta.model import gate
from adatta.model import team_strength as ts


def _old_tag(structure: str, shifts: bool, form: bool, xg: bool, manager: bool, tune: int,
             draws: int) -> str:  # fmt: skip
    """The trace tag as the 1b gate built it before groups existed."""
    tag = "" if structure == "independent" else f"_{structure}"
    tag += "_d16" if shifts else ""
    tag += "_ar1" if form else ""
    tag += "_xg" if xg else ""
    tag += "_mgr" if manager else ""
    return tag + ("" if tune == draws else f"_t{tune}")


def test_without_groups_the_trace_tag_is_the_1b_tag() -> None:
    for structure in ("independent", "strength_open"):
        for flags in [(False, False, False, False), (True, False, True, False),
                      (False, True, True, True)]:  # fmt: skip
            for tune in (1000, 2000):
                assert gate.trace_tag(structure, *flags, (), tune, 2000) == _old_tag(
                    structure, *flags, tune, 2000
                )
    tag = gate.trace_tag("strength_open", False, False, True, False, ("style",), 2000, 2000)
    assert tag == "_strength_open_xg_ctx-style"


def test_every_group_coefficient_is_in_the_sensitivity_headline() -> None:
    for g in cv.GROUPS.values():
        assert cv.beta_name(g) in gate.HEADLINE
    # the 1b headline is unchanged: the new names come after it and only match beta_ vars
    assert gate.HEADLINE[:16] == ("delta", "mu_league", "h_league", "sigma_attack",
                                  "sigma_defence", "sigma_strength", "sigma_open",
                                  "sigma_walk", "sigma_home", "rho", "kappa", "phi",
                                  "sigma_form", "xg_shape", "xg_bias", "change_weeks")  # fmt: skip
    assert all(n.startswith("beta_") for n in gate.HEADLINE[16:])


def test_gate_scoring_uses_each_targets_own_context_row() -> None:
    """`predict`'s scoring path with groups: a target's context moves its own score only."""
    df = with_context(matches())
    as_of = df["kickoff"][900] + timedelta(days=1)
    fd = ts.prepare(df, LEAGUES, as_of, 1095, 3.0, ("style",))
    assert [b.group.name for b in fd.cov] == ["style"]
    draws, _ = CovFitter(beta=0.3)(fd, 0)
    targets = (
        df.filter(pl.col("kickoff") >= as_of)
        .head(20)
        .with_columns(mover_home=pl.lit(0), mover_away=pl.lit(0))
    )
    from adatta.model import backtest as bt

    base = bt.score_targets(draws, fd, targets, 10, ("style",))
    first = int(targets["match_id"][0])
    fast = targets.with_columns(
        home_tempo_rel=pl.when(pl.col("match_id") == first).then(0.3).otherwise("home_tempo_rel"),
        away_tempo_rel=pl.when(pl.col("match_id") == first).then(0.3).otherwise("away_tempo_rel"),
    )
    moved = bt.score_targets(draws, fd, fast, 10, ("style",))
    totals = np.array([r["exp_total"] for r in base]), np.array([r["exp_total"] for r in moved])
    changed = totals[0] != totals[1]
    assert changed.sum() == 1 and bool(changed[0]) and totals[1][0] > totals[0][0]
