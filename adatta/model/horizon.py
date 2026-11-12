"""Strength uncertainty ahead of the last refit step (D34; docs/PHASE1B_TASKS.md section 7c).

A refit's draws describe team strength at its last weekly step. A match `h` weeks later
gets `h` more weeks of the strength walk, and a match in a season with no training match
yet gets the season-start reset as well (RESET_WEEKS, D21), as the training likelihood
would. Each team in the match receives a strength innovation N(0, sigma_walk^2 * w) per
posterior draw, applied as an offset on log lambda:

  strength + openness (D15)   attack and defence both move by delta / sqrt 2, so
                              log lambda_home += (delta_home - delta_away) / sqrt 2
                              log lambda_away += (delta_away - delta_home) / sqrt 2
  independent                 attack and defence move apart:
                              log lambda_home += a_home - d_away, and the mirror

The innovations come from a generator seeded by (seed, team id), so the backtest and
serving give the same numbers. Cold-start teams (index -1, the league average) get none.
Pure numpy: serving runs without the model dependency group.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from adatta.model.team_strength import RESET_WEEKS, FitData

Arr = NDArray[np.float64]
IntArr = NDArray[np.int64]


@dataclass(frozen=True)
class Walk:
    seed: int
    structure: str = "strength_open"


def new_season(fd: FitData, seasons: IntArr) -> NDArray[np.bool_]:
    """Targets in a season with no training match: the reset step hasn't been fitted."""
    latest = max((s for _, s in fd.team_seasons), default=None)
    if latest is None:
        return np.zeros(len(seasons), dtype=bool)
    return np.asarray(seasons, dtype=np.int64) > latest


def weeks_ahead(horizon: IntArr, fresh: NDArray[np.bool_]) -> Arr:
    """Weeks of walk variance between the last refit step and each match."""
    out: Arr = np.maximum(np.asarray(horizon, dtype=np.float64), 0.0) + RESET_WEEKS * fresh
    return out


def _z(seed: int, team_id: int, k: int, n: int) -> Arr:
    return np.random.default_rng([seed, team_id & 0xFFFFFFFF, k]).standard_normal(n)


def offsets(draws: dict[str, Arr], fd: FitData, home: IntArr, away: IntArr, weeks: Arr,
            walk: Walk) -> tuple[Arr, Arr]:  # fmt: skip
    """Per (match, draw) offsets on log lambda_home and log lambda_away."""
    sw = np.asarray(draws["sigma_walk"], dtype=np.float64)
    n_draws = sw.shape[-1]
    off_h = np.zeros((len(home), n_draws))
    off_a = np.zeros((len(home), n_draws))
    cache: dict[tuple[int, int], Arr] = {}

    def z(team: int, k: int) -> Arr:
        if team < 0:  # cold start: no innovation
            return np.zeros(n_draws)
        key = (team, k)
        if key not in cache:
            cache[key] = _z(walk.seed, int(fd.team_ids[team]), k, n_draws)
        return cache[key]

    for i, (h, a, w) in enumerate(zip(home, away, weeks, strict=True)):
        if w <= 0:
            continue
        scale = sw * np.sqrt(w)
        if walk.structure == "independent":
            off_h[i] = (z(int(h), 1) - z(int(a), 2)) * scale
            off_a[i] = (z(int(a), 1) - z(int(h), 2)) * scale
        else:
            d = (z(int(h), 0) - z(int(a), 0)) * scale / np.sqrt(2.0)
            off_h[i], off_a[i] = d, -d
    return off_h, off_a


def combine(a: tuple[Arr, Arr] | None, b: tuple[Arr, Arr] | None) -> tuple[Arr, Arr] | None:
    if a is None:
        return b
    if b is None:
        return a
    return a[0] + b[0], a[1] + b[1]
