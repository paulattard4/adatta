"""P3-i0: defensive player rating (with-without-you, register B.1) and its dev check
(docs/PHASE3_TASKS.md, P3-i). Numpy and Polars only; nothing is sampled.

Per player i, goals conceded while on the pitch G_s ~ Poisson(E_s θ_i) with
θ_i ~ Gamma(k, k), k per line (G / D / M / F). E_s = (stint minutes / 90) x the opponent's
λ in the match, P's posterior mean from the latest P refit of the bundle before the rating
refit (its in-sample training rates). θ_i's posterior mean is (k + G_i) / (k + E_i), G_i and
E_i summed over the player's stints; k is the marginal-likelihood maximum (the
Poisson-Gamma marginal, in closed form). Players are rated one at a time: their team-mates
on the pitch are not adjusted for.

Dev check: each held-out team-match's normal-time goals conceded, R0 = the opponent's
expected goals from P's walk-forward refit before the match, R1 = R0 x the stints' minute-
weighted exp(mean log θ over the players on the pitch); Poisson log loss, paired by
team-match. R1 passes if it is better with the 95% interval excluding 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import lgamma
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.features.referees import RESULT_LAG
from adatta.features.selection import HORIZON

Arr = NDArray[np.float64]
LINES = ("G", "D", "M", "F")
FALLBACK_LINE = "M"  # a player with no position in appearances final by the refit
LOG_K = np.linspace(np.log(0.5), np.log(20000.0), 4001)  # the k search grid


def _opp_lam(home: str, away: str) -> pl.Expr:
    """The opponent's rate for the defending team of each row (team_id vs home_id)."""
    return pl.when(pl.col("team_id") == pl.col("home_id")).then(pl.col(away)).otherwise(
        pl.col(home))  # fmt: skip


def exposure(stints: pl.DataFrame, lam: pl.DataFrame) -> pl.DataFrame:
    """Per player: G (goals conceded on the pitch) and E (expected), over the stints of the
    matches in `lam` (match_id, home_id, away_id, lam_home, lam_away)."""
    s = stints.join(lam.select("match_id", "home_id", "away_id", "lam_home", "lam_away"),
                    on="match_id", how="inner")  # fmt: skip
    bad = s.filter((pl.col("team_id") != pl.col("home_id"))
                   & (pl.col("team_id") != pl.col("away_id")))  # fmt: skip
    if bad.height:
        raise ValueError(f"{bad.height} stints of a team not in their match")
    s = s.with_columns(
        e=(pl.col("end") - pl.col("start")).cast(pl.Float64) / 90.0
        * _opp_lam("lam_home", "lam_away")
    )  # fmt: skip
    return (s.select("players", "conceded", "e").explode("players")
            .rename({"players": "player_id"})
            .group_by("player_id").agg(G=pl.col("conceded").sum().cast(pl.Float64),
                                       E=pl.col("e").sum()))  # fmt: skip


def lines(appearances: pl.DataFrame, cutoff: datetime) -> pl.DataFrame:
    """Each player's most frequent position (G / D / M / F) in appearances final by
    `cutoff`; ties go to the earlier line in LINES."""
    final = appearances.filter((pl.col("kickoff") <= cutoff - RESULT_LAG)
                               & pl.col("pos").is_in(list(LINES)))  # fmt: skip
    c = final.group_by("player_id", "pos").agg(n=pl.len())
    c = c.with_columns(rank=pl.col("pos").replace_strict({x: i for i, x in enumerate(LINES)}))
    return (c.sort(["player_id", "n", "rank"], descending=[False, True, False])
            .group_by("player_id", maintain_order=True).first().select("player_id", "pos")
            .rename({"pos": "line"}))  # fmt: skip


def marginal(G: Arr, E: Arr, log_k: Arr = LOG_K) -> Arr:
    """Σ_i of the Poisson-Gamma marginal log likelihood at each k (terms free of k left
    out)."""
    from scipy.special import gammaln

    k = np.exp(log_k)[:, None]
    return (gammaln(k + G) - gammaln(k) + k * np.log(k) - (k + G) * np.log(k + E)).sum(axis=1)  # type: ignore[no-any-return]


@dataclass(frozen=True)
class Rating:
    as_of: datetime
    k: dict[str, float]
    at_bound: list[str]  # lines whose k is at the grid's edge
    log_theta: pl.DataFrame  # player_id, line, G, E, log_theta
    fallback: int  # players with no position (FALLBACK_LINE)


def rate(expo: pl.DataFrame, line: pl.DataFrame, as_of: datetime) -> Rating:
    """k per line by the marginal-likelihood maximum on LOG_K, then log θ per player."""
    x = expo.join(line, on="player_id", how="left")
    fallback = int(x["line"].null_count())
    x = x.with_columns(pl.col("line").fill_null(FALLBACK_LINE))
    k: dict[str, float] = {}
    at_bound: list[str] = []
    for ln in LINES:
        g = x.filter(pl.col("line") == ln)
        if g.is_empty():
            k[ln] = float(np.exp(LOG_K[-1]))
            continue
        m = marginal(g["G"].to_numpy(), g["E"].to_numpy())
        i = int(np.argmax(m))
        k[ln] = float(np.exp(LOG_K[i]))
        if i in (0, len(LOG_K) - 1):
            at_bound.append(ln)
    kk = pl.col("line").replace_strict(k, return_dtype=pl.Float64)
    x = x.with_columns(log_theta=((kk + pl.col("G")) / (kk + pl.col("E"))).log())
    return Rating(as_of, k, at_bound, x.select("player_id", "line", "G", "E", "log_theta"),
                  fallback)  # fmt: skip


def rating_for(times: list[datetime], kickoff: datetime) -> datetime | None:
    """The latest rating refit at or before kickoff - HORIZON (24 h)."""
    got = [t for t in times if t <= kickoff - HORIZON]
    return got[-1] if got else None


def poisson_ll(y: Arr, mu: Arr) -> Arr:
    """Poisson log loss per row: -log P(y | mu)."""
    return -(y * np.log(mu) - mu - np.array([lgamma(v + 1.0) for v in y]))  # type: ignore[no-any-return]


def score(stints: pl.DataFrame, eg: pl.DataFrame, log_theta: pl.DataFrame) -> pl.DataFrame:
    """R0 and R1 per held-out team-match (the stints of the targets in `eg`: match_id,
    home_id, away_id, eg_home, eg_away). Unrated players count as θ = 1."""
    s = stints.join(eg.select("match_id", "home_id", "away_id", "eg_home", "eg_away"),
                    on="match_id", how="inner")  # fmt: skip
    s = s.with_row_index("sid")
    on = (s.select("sid", "players").explode("players").rename({"players": "player_id"})
          .join(log_theta.select("player_id", "log_theta"), on="player_id", how="left")
          .group_by("sid").agg(m=pl.col("log_theta").fill_null(0.0).mean(),
                               rated=pl.col("log_theta").is_not_null().mean()))  # fmt: skip
    s = s.join(on, on="sid", how="left").with_columns(
        w=(pl.col("end") - pl.col("start")).cast(pl.Float64) / 90.0,
        mu0=_opp_lam("eg_home", "eg_away"),
    )  # fmt: skip
    tm = s.group_by("match_id", "team_id").agg(
        pl.col("season").first(), pl.col("kickoff").first(), y=pl.col("conceded").sum(),
        mu0=pl.col("mu0").first(), mult=(pl.col("w") * pl.col("m").exp()).sum(),
        shift=(pl.col("w") * pl.col("m")).sum(), rated=(pl.col("w") * pl.col("rated")).sum(),
    ).with_columns(mu1=pl.col("mu0") * pl.col("mult"))  # fmt: skip
    y = tm["y"].to_numpy().astype(np.float64)
    return tm.with_columns(ll0=pl.Series(poisson_ll(y, tm["mu0"].to_numpy())),
                           ll1=pl.Series(poisson_ll(y, tm["mu1"].to_numpy())))  # fmt: skip


def paired(d: Arr, clusters: NDArray[np.int64] | None = None) -> dict[str, float]:
    """Mean of d with a normal 95% interval; with `clusters`, the cluster-robust one."""
    n = len(d)
    m = float(d.mean()) if n else float("nan")
    if n < 2:
        return {"n": n, "mean": m, "lo": float("nan"), "hi": float("nan")}
    if clusters is None:
        se = float(d.std(ddof=1) / np.sqrt(n))
    else:
        _, inv = np.unique(clusters, return_inverse=True)
        sums = np.bincount(inv, weights=d - m)
        g = len(sums)
        se = float(np.sqrt(g / (g - 1) * (sums**2).sum()) / n)
    return {"n": n, "mean": m, "lo": m - 1.96 * se, "hi": m + 1.96 * se}


def verdict(scored: pl.DataFrame) -> dict[str, Any]:
    """The gate: R1 - R0 Poisson log loss per team-match, interval excluding 0 below."""
    d = (scored["ll1"] - scored["ll0"]).to_numpy()
    p = paired(d)
    pm = paired(d, scored["match_id"].to_numpy())
    base = float(np.mean(scored["ll0"].to_numpy()))
    return {"passed": bool(p["hi"] < 0), "team_match": p, "by_match": pm,
            "rel": p["mean"] / base, "rel_lo": p["lo"] / base, "rel_hi": p["hi"] / base,
            "ll0": base}  # fmt: skip
