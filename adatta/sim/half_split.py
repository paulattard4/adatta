"""P3b-1, the half split for goals (docs/PHASE3B_TASKS.md; spec §8.2). Numpy only.

The full-time matrix is primary. Within a draw, each side's h full-time goals fall in the
first half independently with probability p (binomial thinning given h), so half scores sum
to full time and the full-time matrix keeps Dixon-Coles rho and the draw inflation:

    P(HT = (x, y), FT = (h, a)) = M[h, a] Bin(x; h, p_home) Bin(y; a, p_away)

Second-half goals are FT - HT, which is thinning with 1 - p. Every half market is linear in
M for a given share, so the posterior-mean probability over (FT draw, share draw) pairs from
independent fits equals the mean over share draws of the split of the posterior-mean M.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

Arr = NDArray[np.float64]
EPS = 1e-15
RESULTS = ("home", "draw", "away")
HTFT = tuple(f"{a}/{b}" for a in RESULTS for b in RESULTS)  # half time / full time
OU_LINES = (0.5, 1.5)


def thinning(p: Arr, g: int) -> Arr:
    """B[s, n, x] = Bin(x; n, p_s) for n, x < g (zero above the diagonal): (S, g, g)."""
    p = np.atleast_1d(np.asarray(p, dtype=np.float64))
    if not ((p > 0) & (p < 1)).all():
        raise ValueError("a share must be strictly between 0 and 1")
    lg = np.array([math.lgamma(v + 1.0) for v in range(g)])
    nn, xx = np.indices((g, g))
    valid = xx <= nn
    comb = lg[nn] - lg[xx] - lg[np.where(valid, nn - xx, 0)]
    lp = (comb[None] + xx[None] * np.log(p)[:, None, None]
          + (nn - xx)[None] * np.log1p(-p)[:, None, None])  # fmt: skip
    return np.asarray(np.where(valid[None], np.exp(lp), 0.0), dtype=np.float64)


def half_matrix(m: Arr, ph: Arr, pa: Arr) -> Arr:
    """(S, g, g) half-score matrices: H_s = B_home(s)^T M B_away(s)."""
    g = m.shape[0]
    bh, ba = thinning(ph, g), thinning(pa, g)
    t = np.einsum("snx,na->sxa", bh, m)
    return np.asarray(np.einsum("sxa,say->sxy", t, ba), dtype=np.float64)


def result(h: Arr) -> Arr:
    """(..., 3) home, draw, away from (..., g, g) matrices."""
    g = h.shape[-1]
    i, j = np.indices((g, g))
    home = (h * (i > j)).sum(axis=(-2, -1))
    draw = np.trace(h, axis1=-2, axis2=-1)
    away = (h * (i < j)).sum(axis=(-2, -1))
    return np.stack([home, draw, away], axis=-1)


def totals(h: Arr) -> Arr:
    """(..., 2g - 1) total-goals pmf."""
    g = h.shape[-1]
    i, j = np.indices((g, g))
    return np.stack([(h * ((i + j) == t)).sum(axis=(-2, -1)) for t in range(2 * g - 1)], axis=-1)


def markets(m: Arr, ph: Arr, pa: Arr) -> dict[str, Arr]:
    """Posterior-mean half markets of one match from its posterior-mean full-time matrix and
    the share draws (S,) of each side:
    - ht_1x2 (3), ht_ft (9, HTFT order), ht_ou (over 0.5, over 1.5), second_1x2 (3);
    - ht_matrix (g, g); ft_1x2 (3), the full-time 1X2 of the same matrix."""
    g = m.shape[0]
    i, j = np.indices((g, g))
    h1 = half_matrix(m, ph, pa)
    h2 = half_matrix(m, 1.0 - np.asarray(ph), 1.0 - np.asarray(pa))
    joint = []
    for region in (i > j, i == j, i < j):
        joint.append(result(half_matrix(m * region, ph, pa)))  # (S, 3) HT result, this FT
    htft = np.stack(joint, axis=-1).mean(axis=0)  # (3 HT, 3 FT)
    tot = totals(h1).mean(axis=0)
    return {
        "ht_1x2": result(h1).mean(axis=0),
        "ht_ft": htft.reshape(9),
        "ht_ou": np.array([1.0 - tot[0], 1.0 - tot[0] - tot[1]]),
        "second_1x2": result(h2).mean(axis=0),
        "ht_matrix": h1.mean(axis=0),
        "ft_1x2": result(m),
    }


def coherence(mk: dict[str, Arr], ft_1x2: Arr, tol: float = 1e-9) -> list[str]:
    """Check (e)1: the nine HT/FT combinations sum to 1 and marginalise to the full-time 1X2
    (`ft_1x2`, the forecast's own) and to the half-time 1X2; the half-time matrix sums to 1
    (the full-time matrix is renormalised); the half-time 1X2 sums to 1."""
    bad = []
    j = mk["ht_ft"].reshape(3, 3)
    if abs(j.sum() - 1) > tol:
        bad.append(f"HT/FT sums to {j.sum():.12f}")
    if np.abs(j.sum(axis=0) - ft_1x2).max() > tol:
        bad.append("HT/FT does not marginalise to the FT 1X2")
    if np.abs(j.sum(axis=1) - mk["ht_1x2"]).max() > tol:
        bad.append("HT/FT does not marginalise to the HT 1X2")
    if abs(mk["ht_matrix"].sum() - 1) > tol:
        bad.append(f"HT matrix sums to {mk['ht_matrix'].sum():.12f}")
    if abs(mk["second_1x2"].sum() - 1) > tol:
        bad.append("2H 1X2 does not sum to 1")
    return bad


def conditional(x: tuple[int, int], n: tuple[int, int], ph: Arr, pa: Arr) -> float:
    """-log P(HT score | FT score), the share integrated over its draws (jointly, both sides
    from the same draw)."""

    def pmf(k: int, nn: int, p: Arr) -> Arr:
        c = math.comb(nn, k)
        return np.asarray(c * np.power(p, k) * np.power(1.0 - p, nn - k), dtype=np.float64)

    v = float((pmf(x[0], n[0], np.asarray(ph)) * pmf(x[1], n[1], np.asarray(pa))).mean())
    return float(-np.log(max(v, EPS)))


def ht_index(x: int, y: int) -> int:
    return 0 if x > y else 1 if x == y else 2


def htft_index(ht: tuple[int, int], ft: tuple[int, int]) -> int:
    return 3 * ht_index(*ht) + ht_index(*ft)
