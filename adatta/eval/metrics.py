"""Market probabilities from a full-time score matrix, and proper scoring rules (§14.1).

The full-time matrix is primary (principle 6): every market here is derived from it.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

Arr = NDArray[np.float64]
EPS = 1e-15


def markets(m: Arr) -> dict[str, Arr]:
    """1X2 (home, draw, away), OU 2.5 (over, under), BTTS (yes, no), total-goals pmf."""
    n = m.shape[0]
    i, j = np.indices(m.shape)
    home, draw = float(m[i > j].sum()), float(np.trace(m))
    total = np.bincount((i + j).ravel(), m.ravel(), 2 * n - 1)
    over = float(total[3:].sum())
    btts = float(m[1:, 1:].sum())
    return {
        "1X2": np.array([home, draw, 1.0 - home - draw]),
        "OU2.5": np.array([over, 1.0 - over]),
        "BTTS": np.array([btts, 1.0 - btts]),
        "total": total,
    }


def outcomes(hg: int, ag: int) -> dict[str, int]:
    """Index of the realised outcome in each market's probability vector."""
    return {
        "1X2": 0 if hg > ag else 1 if hg == ag else 2,
        "OU2.5": 0 if hg + ag > 2 else 1,
        "BTTS": 0 if hg > 0 and ag > 0 else 1,
        "total": hg + ag,
    }


def brier(p: Arr, k: int) -> float:
    """Multi-class Brier score: sum over classes of (p - indicator)^2."""
    y = np.zeros_like(p)
    y[k] = 1.0
    return float(((p - y) ** 2).sum())


def log_loss(p: Arr, k: int) -> float:
    return float(-np.log(max(p[k], EPS)))


def rps(p: Arr, k: int) -> float:
    """Ranked probability score for ordered outcomes (1X2: home < draw < away)."""
    y = np.zeros_like(p)
    y[k] = 1.0
    cp, cy = np.cumsum(p)[:-1], np.cumsum(y)[:-1]
    return float(((cp - cy) ** 2).sum() / (len(p) - 1))


def crps_discrete(pmf: Arr, k: int) -> float:
    """CRPS for an integer outcome: sum over thresholds of (F(t) - 1{k <= t})^2."""
    cdf = np.cumsum(pmf)
    t = np.arange(len(pmf))
    return float(((cdf - (k <= t)) ** 2).sum())


def score_all(m: Arr, hg: int, ag: int) -> dict[str, float]:
    return score_markets(markets(m), hg, ag)


def score_markets(mk: dict[str, Arr], hg: int, ag: int) -> dict[str, float]:
    oc = outcomes(hg, ag)
    out: dict[str, float] = {}
    for market in ("1X2", "OU2.5", "BTTS"):
        p, k = mk[market], oc[market]
        out[f"{market}:brier"] = brier(p, k)
        out[f"{market}:log_loss"] = log_loss(p, k)
    out["1X2:rps"] = rps(mk["1X2"], oc["1X2"])
    total = mk["total"]
    if oc["total"] >= len(total):  # beyond truncation: extend with zero mass
        total = np.concatenate([total, np.zeros(oc["total"] - len(total) + 1)])
    out["total:crps"] = crps_discrete(total, oc["total"])
    return out
