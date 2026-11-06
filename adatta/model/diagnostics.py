"""Inference-quality gate for the Phase 1b model (spec §14.5; acceptance item 1 in
docs/PHASE1B_TASKS.md). The diagnostics gate comes first, the backtest comparison second.

Pure-numpy checks are importable anywhere; `convergence` needs ArviZ (model group, D13).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

Arr = NDArray[np.float64]
Counts = NDArray[Any]  # goal counts (int or float)

RHAT_MAX = 1.01
ESS_MIN = 400
DIVERGENCES_MAX = 0
PPC_BAND = (0.05, 0.95)  # observed statistic must sit in the central 90% of the PPC
CORR_MAX = 0.9  # |posterior corr| between a league level and its teams' mean effect
SENS_SHIFT_SD = 1.0  # headline posterior mean may move < 1 posterior sd under x0.5 / x2
SENS_LOGLOSS_REL = 0.002  # predictive 1X2 log loss may move < 0.2% relative
TAIL_MAX, TAIL_SHARE = 1e-4, 0.999  # truncated tail mass < 1e-4 for 99.9% of predictions


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    value: float
    detail: str = ""
    # False = reported as a warning only; it doesn't decide the gate (e.g. a proxy measured
    # on a small sample until the real measurement exists)
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def convergence(idata: Any) -> list[Check]:
    """R-hat and bulk/tail ESS over every free parameter (including each walk innovation),
    plus divergences after tuning."""
    import arviz as az

    post = idata["posterior"]
    post = post.to_dataset() if hasattr(post, "to_dataset") else post
    names = [v for v in post.data_vars if not v.endswith("_last")]  # deterministics
    summ = az.summary(idata, var_names=names, kind="diagnostics")
    worst_rhat = summ["r_hat"].idxmax()
    worst_ess = summ["ess_bulk"].idxmin()
    div = int(idata.sample_stats["diverging"].sum())
    return [
        Check("rhat", bool(summ["r_hat"].max() <= RHAT_MAX), float(summ["r_hat"].max()),
              f"worst {worst_rhat}; {int((summ['r_hat'] > RHAT_MAX).sum())} of {len(summ)} "
              f"over {RHAT_MAX}"),
        Check("ess_bulk", bool(summ["ess_bulk"].min() >= ESS_MIN), float(summ["ess_bulk"].min()),
              f"worst {worst_ess}"),
        Check("ess_tail", bool(summ["ess_tail"].min() >= ESS_MIN), float(summ["ess_tail"].min())),
        Check("divergences", div <= DIVERGENCES_MAX, float(div)),
    ]  # fmt: skip


def goal_stats(hg: Counts, ag: Counts) -> dict[str, Arr]:
    """Per-draw statistics; hg/ag have shape (draws, matches) (or (matches,) for observed)."""
    hg, ag = np.atleast_2d(hg).astype(float), np.atleast_2d(ag).astype(float)
    total = hg + ag
    return {
        "total_mean": total.mean(1),
        "home_mean": hg.mean(1),
        "away_mean": ag.mean(1),
        "draw_rate": (hg == ag).mean(1),
        "total_dispersion": total.var(1) / np.maximum(total.mean(1), 1e-9),
    }


def ppc(obs_hg: Counts, obs_ag: Counts, sim_hg: Counts, sim_ag: Counts, label: str) -> list[Check]:
    """Observed statistic's quantile within the posterior-predictive distribution."""
    obs, sim = goal_stats(obs_hg, obs_ag), goal_stats(sim_hg, sim_ag)
    out = []
    for k in obs:
        q = float((sim[k] < obs[k][0]).mean() + 0.5 * (sim[k] == obs[k][0]).mean())
        detail = f"observed {obs[k][0]:.3f}, PPC median {np.median(sim[k]):.3f}"
        out.append(Check(f"ppc {label} {k}", PPC_BAND[0] <= q <= PPC_BAND[1], q, detail))
    return out


def identifiability(level: Arr, team_mean: Arr, label: str) -> Check:
    c = float(np.corrcoef(level, team_mean)[0, 1])
    return Check(f"identifiability {label}", abs(c) <= CORR_MAX, c,
                 "posterior corr(league level, mean team effect)")  # fmt: skip


def sensitivity(base_mean: dict[str, float], base_sd: dict[str, float],
                alt_mean: dict[str, float], label: str) -> Check:  # fmt: skip
    shifts = {k: abs(alt_mean[k] - base_mean[k]) / max(base_sd[k], 1e-12) for k in base_mean}
    worst = max(shifts, key=lambda k: shifts[k])
    return Check(f"prior sensitivity {label}", shifts[worst] < SENS_SHIFT_SD, shifts[worst],
                 f"worst {worst} (in posterior sds)")  # fmt: skip


def predictive_sensitivity(ll_base: float, ll_alt: float, label: str,
                           blocking: bool = True) -> Check:  # fmt: skip
    """§14.5 measures this on dev 1X2 log loss (the walk-forward harness). A smaller target
    set is only a proxy: pass blocking=False so it is reported as a warning."""
    rel = abs(ll_alt - ll_base) / ll_base
    return Check(f"predictive sensitivity {label}", rel < SENS_LOGLOSS_REL, rel,
                 f"1X2 log loss {ll_base:.4f} -> {ll_alt:.4f}", blocking)  # fmt: skip


def truncation(tails: Arr) -> Check:
    share = float((np.asarray(tails) < TAIL_MAX).mean())
    return Check("score-matrix truncation", share >= TAIL_SHARE, share,
                 f"share of predictions with tail mass < {TAIL_MAX:g}")  # fmt: skip


def reproducible(a: Arr, b: Arr) -> Check:
    same = bool(np.array_equal(a, b))
    return Check("reproducible seed", same, float(same), "identical draws from the same seed")
