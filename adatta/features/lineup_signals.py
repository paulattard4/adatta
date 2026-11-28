"""P3-j lineup signals and the step 1 dev signal check (docs/PHASE3_TASKS.md P3-j (a)-(b),
D65). Pure polars / numpy: runs on the Mac, no API requests.

Per team-match, with the announced starting XI and as of t = kickoff - H_LK (lineups known),
from matches final by t - RESULT_LAG only:
  usual XI   the team's XI_N players with the largest minute shares over its last PREV_N
             league matches final by then (share = minutes / 90, averaged over them)
  S_miss     1 - (the XI's summed shares / the usual XI's summed shares); 0 = the usual XI
  S_apm      adjusted plus-minus on xG: ridge regression, per bundle on a weekly grid, of
             home xG minus away xG per match on the players' minute shares (+ home side,
             - away side) and a home term, over the last APM_DAYS of the bundle's league
             matches final by the Monday; the XI's summed coefficients minus the usual
             XI's. The penalty is chosen once by time-ordered cross-validation on dev
             (owner choice 2): each weekly refit predicts the next week's matches.
  S_rat      each player's mean provider rating over his last RATING_N rated appearances
             final by then, shrunk towards his line's mean (warm-up seasons) with
             RATING_PSEUDO pseudo-appearances; the XI's mean minus the usual XI's.
A signal that can't be built is null and the row is left out of that signal's test
(CLAUDE.md rule 8: no imputed value). The check never reads minutes of the match itself:
the XI is who started (announced before kickoff), not who played.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.features import rates as fr
from adatta.features.referees import RESULT_LAG

Arr = NDArray[np.float64]
PREFIX = "features/lineup_signals/v1"
KEY = ["match_id", "team_id"]
H_LK = timedelta(minutes=60)  # the lineups-known horizon, T-60m
H_24 = timedelta(hours=24)  # the T-24h horizon (P3-b's projected XI)
PREV_N, MIN_PREV, XI_N = 10, 3, 11
RATING_N, RATING_PSEUDO = 10, 5
APM_DAYS, APM_MIN_TRAIN = 365, 200
LAMBDAS = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0)
SIGNALS = ("s_miss", "s_apm", "s_rat")
EXPECTED_SIGN = {"s_miss": -1, "s_apm": 1, "s_rat": 1}
Z95 = 1.959963984540054


# ------------------------------------------------------------------------------ frames


def team_matches(matches: pl.DataFrame, bundle_of: Mapping[int, str]) -> pl.DataFrame:
    """Both sides of every league match: (match_id, team_id, opp_id, is_home, goals,
    kickoff, season, league_id, bundle, tidx = the team's match index)."""
    cols = ["match_id", "kickoff", "season", "league_id"]
    home = matches.select(*cols, team_id="home", opp_id="away", goals="hg",
                          is_home=pl.lit(True))  # fmt: skip
    away = matches.select(*cols, team_id="away", opp_id="home", goals="ag",
                          is_home=pl.lit(False))  # fmt: skip
    tm = pl.concat([home, away]).with_columns(
        bundle=pl.col("league_id").replace_strict(dict(bundle_of), return_dtype=pl.Utf8)
    )
    return tm.sort("team_id", "kickoff", "match_id").with_columns(
        tidx=pl.int_range(pl.len()).over("team_id")
    )


def squads(app: pl.DataFrame, match_ids: pl.Series) -> pl.DataFrame:
    """Matchday-squad rows of these matches: started, minute share (minutes / 90, in
    [0, 1]), rating and line (the row's G/D/M/F, else the player's most common earlier)."""
    cols = ["match_id", "team_id", "player_id", "kickoff", "started", "pos", "minutes",
            "rating"]  # fmt: skip
    s = fr._line(app.filter(pl.col("match_id").is_in(match_ids.implode())).select(cols))
    return s.with_columns(
        share=(pl.col("minutes").fill_null(0).cast(pl.Float64) / 90.0).clip(0.0, 1.0),
        started=pl.col("started").fill_null(False),
    )


def previous(tm: pl.DataFrame, horizon: timedelta = H_LK) -> pl.DataFrame:
    """(match_id, team_id, prev_id): the team's last PREV_N league matches final by the
    as-of time, kickoff - horizon - RESULT_LAG."""
    cand = tm.select(*KEY, "kickoff", "tidx").join(
        pl.DataFrame({"off": list(range(1, PREV_N + 3))}), how="cross"
    ).with_columns(ptidx=pl.col("tidx") - pl.col("off"))  # fmt: skip
    prev = tm.select("team_id", ptidx="tidx", prev_id="match_id", prev_kick="kickoff")
    j = cand.join(prev, on=["team_id", "ptidx"])
    j = j.filter(pl.col("prev_kick") <= pl.col("kickoff") - horizon - RESULT_LAG)
    j = j.sort("off").group_by(KEY, maintain_order=True).head(PREV_N)
    return j.select(*KEY, "prev_id")


def usual(prev: pl.DataFrame, sq: pl.DataFrame) -> pl.DataFrame:
    """Per team-match and player: the minute share over the previous matches (n_prev of
    them), and `usual` for the XI_N largest (ties: lower player id)."""
    pp = prev.join(sq.select("team_id", "player_id", "share", prev_id="match_id"),
                   on=["team_id", "prev_id"])  # fmt: skip
    n = prev.group_by(KEY).agg(n_prev=pl.len())
    u = pp.group_by(*KEY, "player_id").agg(pl.col("share").sort().sum()).join(n, on=KEY)
    u = u.with_columns(share=pl.col("share") / pl.col("n_prev"))
    u = u.sort(*KEY, "share", "player_id", descending=[False, False, True, False])
    return u.with_columns(usual=pl.int_range(pl.len()).over(KEY) < XI_N)


def starters(sq: pl.DataFrame, tm: pl.DataFrame) -> pl.DataFrame:
    """The announced XI of each team-match (who started), with its line."""
    return (
        sq.filter(pl.col("started")).join(tm.select(KEY), on=KEY).select(*KEY, "player_id", "line")
    )


def s_miss(xi: pl.DataFrame, u: pl.DataFrame) -> pl.DataFrame:
    """(match_id, team_id, s_miss, n_prev, n_xi); s_miss null without a baseline."""
    xs = xi.join(u.select(*KEY, "player_id", "share"), on=[*KEY, "player_id"], how="left")
    num = xs.group_by(KEY).agg(xi_share=pl.col("share").fill_null(0.0).sort().sum(),
                               n_xi=pl.len())  # fmt: skip
    den = u.filter(pl.col("usual")).group_by(KEY).agg(
        usual_share=pl.col("share").sort().sum(), n_prev=pl.col("n_prev").first()
    )  # fmt: skip
    j = num.join(den, on=KEY, how="left")
    ok = (pl.col("n_prev") >= MIN_PREV) & (pl.col("usual_share") > 0) & (pl.col("n_xi") == XI_N)
    return j.select(
        *KEY, pl.col("n_prev").fill_null(0), "n_xi",
        s_miss=pl.when(ok).then(1.0 - pl.col("xi_share") / pl.col("usual_share")),
    )  # fmt: skip


# ----------------------------------------------------------------------------- ratings


def line_means(sq: pl.DataFrame, before: datetime) -> dict[str, float]:
    """Mean provider rating per line over rated appearances before `before` (the warm-up
    seasons), and "" for the overall mean."""
    r = sq.filter((pl.col("kickoff") < before) & pl.col("rating").is_not_null()
                  & (pl.col("minutes").fill_null(0) > 0))  # fmt: skip
    by_line = r.group_by("line").agg(pl.col("rating").sort().mean())
    out = {str(k): float(v) for k, v in by_line.iter_rows() if k is not None}
    out[""] = float(r["rating"].sort().mean()) if r.height else 6.7  # type: ignore[arg-type]
    return out


def rating_history(sq: pl.DataFrame) -> pl.DataFrame:
    """Per rated appearance: the player's mean rating over his last RATING_N rated
    appearances up to and including it, their count, and the appearance's line."""
    r = sq.filter(pl.col("rating").is_not_null() & (pl.col("minutes").fill_null(0) > 0))
    r = r.sort("player_id", "kickoff", "match_id").with_columns(
        r_sum=pl.col("rating").rolling_sum(RATING_N, min_samples=1).over("player_id"),
        r_n=(pl.int_range(pl.len()).over("player_id") + 1).clip(upper_bound=RATING_N),
    )
    return r.select("player_id", "kickoff", "line", r_mean=pl.col("r_sum") / pl.col("r_n"),
                    r_n=pl.col("r_n").cast(pl.Float64))  # fmt: skip


def ratings_asof(rows: pl.DataFrame, hist: pl.DataFrame, means: Mapping[str, float],
                 horizon: timedelta = H_LK) -> pl.DataFrame:  # fmt: skip
    """`rows` (match_id, team_id, player_id, kickoff, line) with r: the shrunk rating from
    appearances final by kickoff - horizon - RESULT_LAG. The line is the row's, else the
    player's last appearance's, else the overall mean is the target."""
    left = rows.with_columns(cut=pl.col("kickoff") - horizon - RESULT_LAG).sort("cut")
    right = hist.rename({"kickoff": "h_kick", "line": "h_line"}).sort("h_kick")
    j = left.join_asof(right, left_on="cut", right_on="h_kick", by="player_id",
                       strategy="backward", check_sortedness=False)  # fmt: skip
    line = pl.coalesce(pl.col("line"), pl.col("h_line"), pl.lit(""))
    target = line.replace_strict(dict(means), default=means[""], return_dtype=pl.Float64)
    n = pl.col("r_n").fill_null(0.0)
    r = (n * pl.col("r_mean").fill_null(0.0) + RATING_PSEUDO * target) / (n + RATING_PSEUDO)
    return j.with_columns(r=r).drop("cut", "h_kick", "h_line", "r_mean", "r_n")


def s_rat(xi: pl.DataFrame, u: pl.DataFrame, tm: pl.DataFrame, hist: pl.DataFrame,
          means: Mapping[str, float], horizon: timedelta = H_LK) -> pl.DataFrame:  # fmt: skip
    """(match_id, team_id, s_rat): the XI's mean shrunk rating minus the usual XI's. A
    usual-XI player's line is his last rated appearance's by then."""
    kick = tm.select(*KEY, "kickoff")
    ux = u.filter(pl.col("usual")).select(*KEY, "player_id", line=pl.lit(None, pl.Utf8))
    a = ratings_asof(xi.join(kick, on=KEY), hist, means, horizon)
    b = ratings_asof(ux.join(kick, on=KEY), hist, means, horizon)
    ma = a.group_by(KEY).agg(r_xi=pl.col("r").sort().mean())
    mb = b.group_by(KEY).agg(r_usual=pl.col("r").sort().mean())
    return ma.join(mb, on=KEY, how="inner").select(*KEY, s_rat=pl.col("r_xi") - pl.col("r_usual"))


# --------------------------------------------------------------------------------- APM


def ridge_path(x: Arr, y: Arr, lambdas: Sequence[float]) -> tuple[Arr, Arr]:
    """Ridge coefficients for each penalty (rows) with an unpenalised intercept:
    (betas (L, p), intercepts (L,)), through one SVD of the centred design."""
    xm, ym = x.mean(axis=0), float(y.mean())
    u, s, vt = np.linalg.svd(x - xm, full_matrices=False)
    uy = u.T @ (y - ym)
    lam = np.asarray(lambdas, dtype=np.float64)[:, None]
    betas = (s / (s**2 + lam) * uy) @ vt
    return betas, ym - betas @ xm


def mondays(start: datetime, end: datetime) -> list[datetime]:
    first = (start - timedelta(days=start.weekday())).replace(hour=0, minute=0, second=0,
                                                              microsecond=0)  # fmt: skip
    out = []
    while first <= end:
        out.append(first)
        first += timedelta(days=7)
    return out


def _design(rows: pl.DataFrame, ids: Sequence[int], index: Mapping[int, int],
            p: int) -> Arr:  # fmt: skip
    """(len(ids), p): each match's signed minute shares (rows: match_id, player_id, v);
    players outside `index` are left out (coefficient 0)."""
    x = np.zeros((len(ids), p))
    r = rows.join(
        pl.DataFrame(
            {"match_id": list(ids), "_i": range(len(ids))},
            schema={"match_id": pl.Int64, "_i": pl.Int64},
        ),
        on="match_id",
    )
    r = r.join(
        pl.DataFrame(
            {"player_id": list(index), "_j": list(index.values())},
            schema={"player_id": pl.Int64, "_j": pl.Int64},
        ),
        on="player_id",
    )
    np.add.at(x, (r["_i"].to_numpy(), r["_j"].to_numpy()), r["v"].to_numpy())
    return x


def apm(sq: pl.DataFrame, tm: pl.DataFrame, xg: pl.DataFrame, xi: pl.DataFrame,
        u: pl.DataFrame, targets: pl.DataFrame,
        lambdas: Sequence[float] = LAMBDAS) -> tuple[pl.DataFrame, Arr, int]:  # fmt: skip
    """S_apm for every penalty: (frame of KEY + s_apm_<i> per lambda, the summed squared
    next-week prediction errors per lambda, the number of matches predicted). One refit
    per bundle and Monday with a target in the following week."""
    home = tm.filter(pl.col("is_home")).select("match_id", "team_id", "bundle", "kickoff")
    pool = home.join(xg, on="match_id").filter(
        pl.col("xg_home").is_not_null() & pl.col("xg_away").is_not_null()
    ).with_columns(y=pl.col("xg_home") - pl.col("xg_away")).sort("kickoff", "match_id")  # fmt: skip
    signed = sq.join(home.select("match_id", home_team="team_id"), on="match_id").select(
        "match_id", "player_id",
        v=pl.when(pl.col("team_id") == pl.col("home_team")).then(pl.col("share"))
        .otherwise(-pl.col("share")),
    ).filter(pl.col("v") != 0)  # fmt: skip
    sel = pl.concat([
        xi.select(*KEY, "player_id", w=pl.lit(1.0)),
        u.filter(pl.col("usual")).select(*KEY, "player_id", w=pl.lit(-1.0)),
    ])  # fmt: skip
    tg = targets.select(*KEY, "bundle", "kickoff")
    sse = np.zeros(len(lambdas))
    n_pred = 0
    out: list[pl.DataFrame] = []
    lo, hi = tg["kickoff"].min(), tg["kickoff"].max()
    if lo is None or hi is None:
        return pl.DataFrame(schema={"match_id": pl.Int64, "team_id": pl.Int64}), sse, 0
    for b in sorted(set(tg["bundle"].to_list())):
        bp, bt = pool.filter(pl.col("bundle") == b), tg.filter(pl.col("bundle") == b)
        for mon in mondays(lo, hi):  # type: ignore[arg-type]
            nxt = mon + timedelta(days=7)
            week = bt.filter((pl.col("kickoff") >= mon) & (pl.col("kickoff") < nxt))
            if week.is_empty():
                continue
            train = bp.filter((pl.col("kickoff") > mon - timedelta(days=APM_DAYS))
                              & (pl.col("kickoff") <= mon - RESULT_LAG))  # fmt: skip
            if train.height < APM_MIN_TRAIN:
                continue
            ids = train["match_id"].to_list()
            rows = signed.filter(pl.col("match_id").is_in(ids))
            players = sorted(set(rows["player_id"].to_list()))
            index = {pid: j for j, pid in enumerate(players)}
            betas, icpt = ridge_path(_design(rows, ids, index, len(players)),
                                     train["y"].to_numpy(), lambdas)  # fmt: skip
            test = bp.filter((pl.col("kickoff") >= mon) & (pl.col("kickoff") < nxt))
            if test.height:
                tid = test["match_id"].to_list()
                xt = _design(signed.filter(pl.col("match_id").is_in(tid)), tid, index,
                             len(players))  # fmt: skip
                err = test["y"].to_numpy()[None, :] - (betas @ xt.T + icpt[:, None])
                sse += (err**2).sum(axis=1)
                n_pred += test.height
            coef = pl.DataFrame({"player_id": players, **{f"b{i}": betas[i] for i in
                                                          range(len(lambdas))}})  # fmt: skip
            w = sel.join(week.select(KEY), on=KEY).join(coef, on="player_id", how="left")
            out.append(w.group_by(KEY).agg(
                [(pl.col("w") * pl.col(f"b{i}").fill_null(0.0)).sort().sum().alias(f"s_apm_{i}")
                 for i in range(len(lambdas))]))  # fmt: skip
    if not out:
        return pl.DataFrame(schema={"match_id": pl.Int64, "team_id": pl.Int64}), sse, n_pred
    return pl.concat(out), sse, n_pred


# ------------------------------------------------------------------------------- build


def build(app: pl.DataFrame, matches: pl.DataFrame, xg: pl.DataFrame,
          bundle_of: Mapping[int, str], seasons: Sequence[int],
          warmup_end: datetime) -> tuple[pl.DataFrame, dict[str, Any]]:  # fmt: skip
    """The signals of every team-match in `seasons` (the announced XI at T-60m), and the
    penalty choice. `matches` holds every league match (history included)."""
    tm = team_matches(matches, bundle_of)
    sq = squads(app, matches["match_id"])
    targets = tm.filter(pl.col("season").is_in(list(seasons)))
    prev = previous(tm).join(targets.select(KEY), on=KEY)
    u = usual(prev, sq)
    xi = starters(sq, targets)
    hist = rating_history(sq)
    means = line_means(sq, warmup_end)
    t = targets.select(*KEY, "opp_id", "is_home", "goals", "kickoff", "season", "league_id",
                       "bundle")  # fmt: skip
    t = t.join(s_miss(xi, u), on=KEY, how="left")
    rat = s_rat(xi, u, targets, hist, means)
    t = t.join(rat, on=KEY, how="left").with_columns(
        s_rat=pl.when(pl.col("s_miss").is_not_null()).then(pl.col("s_rat"))
    )
    ap_, sse, n_pred = apm(sq, tm, xg, xi, u, targets)
    best = int(np.argmin(sse)) if n_pred else 0
    if ap_.height:
        t = t.join(ap_.select(*KEY, s_apm=f"s_apm_{best}"), on=KEY, how="left")
    else:
        t = t.with_columns(s_apm=pl.lit(None, pl.Float64))
    t = t.with_columns(s_apm=pl.when(pl.col("s_miss").is_not_null()).then(pl.col("s_apm")))
    meta = {"lambdas": list(LAMBDAS), "cv_mse": (sse / max(n_pred, 1)).tolist(),
            "cv_n": n_pred, "lambda": LAMBDAS[best], "line_means": means,
            "h_lk_minutes": H_LK.total_seconds() / 60, "prev_n": PREV_N,
            "rating_n": RATING_N, "rating_pseudo": RATING_PSEUDO, "apm_days": APM_DAYS}  # fmt: skip
    return t.sort("kickoff", *KEY), meta


# ------------------------------------------------------------------------- step 2 table

# The signals that passed step 1 (2026-11-27, docs/PHASE3_TASKS.md P3-j): J's K, in order
STEP2_SIGNALS = ("s_miss", "s_rat")
STEP2_PREFIX = "features/lineup_signals/v2"
LEAKAGE_N = 20


def projected_xi(export: pl.DataFrame) -> pl.DataFrame:
    """P3-b's projected XI at T-24h: per team-match, the XI_N players with the largest
    start probability in its pool (ties: lower player id), with the line of their position
    family. Team-matches whose pool has fewer than XI_N players have none (rule 8)."""
    e = export.filter(pl.col("model") == "model").select(
        *KEY, "player_id", "p_start",
        line=pl.when(pl.col("pos").is_in(fr.LINES)).then(pl.col("pos")),
    )  # fmt: skip
    e = e.sort(*KEY, "p_start", "player_id", descending=[False, False, True, False])
    e = e.with_columns(rank=pl.int_range(pl.len()).over(KEY), n=pl.len().over(KEY))
    return e.filter((pl.col("n") >= XI_N) & (pl.col("rank") < XI_N)).select(
        *KEY, "player_id", "line"
    )


def signals_at(tm: pl.DataFrame, sq: pl.DataFrame, targets: pl.DataFrame,
               xi: pl.DataFrame, hist: pl.DataFrame, means: Mapping[str, float],
               horizon: timedelta) -> pl.DataFrame:  # fmt: skip
    """S_miss and S_rat of `targets` for the XI `xi`, as of kickoff - horizon. S_rat is
    null where S_miss is (no baseline)."""
    prev = previous(tm, horizon).join(targets.select(KEY), on=KEY)
    u = usual(prev, sq)
    t = targets.select(KEY).join(s_miss(xi, u), on=KEY, how="left")
    t = t.join(s_rat(xi, u, targets, hist, means, horizon), on=KEY, how="left")
    return t.with_columns(s_rat=pl.when(pl.col("s_miss").is_not_null()).then(pl.col("s_rat")))


def build_step2(app: pl.DataFrame, matches: pl.DataFrame, bundle_of: Mapping[int, str],
                seasons: Sequence[int], dev: Sequence[int], warmup_end: datetime,
                export: pl.DataFrame, only: pl.DataFrame | None = None,
                ) -> tuple[pl.DataFrame, dict[str, Any]]:  # fmt: skip
    """J's table: per team-match of `seasons`, S_miss and S_rat at T-60m from the announced
    XI (training rows and the lineups-known targets), and for `dev` team-matches at T-24h
    from P3-b's projected XI (`export`, its rerun with the P3-d export). Scales: each
    signal's sd over the warm-up team-matches (kickoff before warmup_end). `only` (KEY
    rows) restricts the targets (the leakage check)."""
    tm = team_matches(matches, bundle_of)
    sq = squads(app, matches["match_id"])
    targets = tm.filter(pl.col("season").is_in(list(seasons)))
    if only is not None:
        targets = targets.join(only.select(KEY), on=KEY)
    hist = rating_history(sq)
    means = line_means(sq, warmup_end)
    lk = signals_at(tm, sq, targets, starters(sq, targets), hist, means, H_LK)
    t24 = targets.filter(pl.col("season").is_in(list(dev)))
    proj = projected_xi(export).join(t24.select(KEY), on=KEY)
    p24 = signals_at(tm, sq, t24, proj, hist, means, H_24).select(
        *KEY, s_miss_24="s_miss", s_rat_24="s_rat"
    )
    t = targets.select(*KEY, "opp_id", "is_home", "kickoff", "season", "league_id", "bundle")
    t = t.join(lk.select(*KEY, "n_prev", *STEP2_SIGNALS), on=KEY, how="left")
    t = t.join(p24, on=KEY, how="left").sort("kickoff", *KEY)
    warm = t.filter(pl.col("kickoff") < warmup_end)
    scale = {s: float(warm[s].drop_nulls().std() or 0.0) for s in STEP2_SIGNALS}  # type: ignore[arg-type]
    meta = {"signals": list(STEP2_SIGNALS), "scale": scale, "line_means": means,
            "h_lk_minutes": H_LK.total_seconds() / 60, "h_24_hours": 24, "prev_n": PREV_N,
            "rating_n": RATING_N, "rating_pseudo": RATING_PSEUDO,
            "n_warm": warm.height}  # fmt: skip
    return t, meta


def step2_leakage(app: pl.DataFrame, matches: pl.DataFrame, bundle_of: Mapping[int, str],
                  table: pl.DataFrame, dev: Sequence[int], warmup_end: datetime,
                  export: pl.DataFrame, n: int = LEAKAGE_N, seed: int = 20261127,
                  ) -> dict[str, Any]:  # fmt: skip
    """For n sampled dev team-matches: rebuild their signals from the matches before their
    kickoff plus the match itself with its minutes and ratings removed (only who started
    is kept), and compare with the table. Any difference means the table read the match's
    own play or a later match."""
    cand = table.filter(pl.col("season").is_in(list(dev)) & pl.col("s_miss").is_not_null())
    pick = cand.sample(n=min(n, cand.height), seed=seed)
    changed = 0
    for r in pick.iter_rows(named=True):
        k = r["kickoff"]
        own = pl.col("match_id") == r["match_id"]
        m2 = matches.filter((pl.col("kickoff") < k) | own)
        a2 = app.filter((pl.col("kickoff") < k) | own).with_columns(
            minutes=pl.when(own).then(None).otherwise(pl.col("minutes")),
            rating=pl.when(own).then(None).otherwise(pl.col("rating")),
        )  # fmt: skip
        only = pl.DataFrame({"match_id": [r["match_id"]], "team_id": [r["team_id"]]},
                            schema={"match_id": pl.Int64, "team_id": pl.Int64})  # fmt: skip
        got, _ = build_step2(a2, m2, bundle_of, [r["season"]], dev, warmup_end, export, only)
        cols = [*STEP2_SIGNALS, *(f"{s}_24" for s in STEP2_SIGNALS)]
        a = [r[c] for c in cols]
        b = [got.row(0, named=True)[c] for c in cols] if got.height else [None] * len(cols)
        same = all((x is None and y is None) or (x is not None and y is not None
                                                 and abs(x - y) < 1e-9)
                   for x, y in zip(a, b, strict=True))  # fmt: skip
        changed += not same
    return {"n": pick.height, "changed": changed, "ok": changed == 0}


def attach(df: pl.DataFrame, table: pl.DataFrame, scale: Mapping[str, float]) -> pl.DataFrame:
    """A match frame (match_id, home, away) with each side's scaled signals, 0 where absent
    (rule 8: the term falls back to P): j_<signal>_<h>_<side> for h in lk (training rows
    and lineups-known targets) and 24 (T-24h targets)."""
    out = df
    for side in ("home", "away"):
        sel = table.select(
            pl.col("match_id"), pl.col("team_id").alias(side),
            *[(pl.col(s) / scale[s]).alias(f"j_{s}_lk_{side}") for s in STEP2_SIGNALS],
            *[(pl.col(f"{s}_24") / scale[s]).alias(f"j_{s}_24_{side}") for s in STEP2_SIGNALS],
        )  # fmt: skip
        out = out.join(sel, on=["match_id", side], how="left")
    cols = [f"j_{s}_{h}_{side}" for s in STEP2_SIGNALS for h in ("lk", "24")
            for side in ("home", "away")]  # fmt: skip
    return out.with_columns([pl.col(c).fill_null(0.0).fill_nan(0.0) for c in cols])


def side_signals(df: pl.DataFrame, side: str, h: str = "lk") -> Arr:
    """(rows, K) scaled signals of one side, in STEP2_SIGNALS order."""
    return np.column_stack([df[f"j_{s}_{h}_{side}"].to_numpy().astype(np.float64)
                            for s in STEP2_SIGNALS])  # fmt: skip


# ------------------------------------------------------------------- live (P3-j step 3)

J_TABLE = "021eb8f8c77ed8f1"  # step 2's table: its warm-up scales and line means are J's


def train_table(app: pl.DataFrame, matches: pl.DataFrame, bundle_of: Mapping[int, str],
                lo: datetime, means: Mapping[str, float]) -> pl.DataFrame:  # fmt: skip
    """A live J refit's training signals: S_miss and S_rat at T-60m from the announced XI
    of every team-match kicking off from `lo` (step 2's definitions, `signals_at`), with
    the T-24h columns empty (training reads lk only)."""
    tm = team_matches(matches, bundle_of)
    sq = squads(app, matches["match_id"])
    targets = tm.filter(pl.col("kickoff") >= lo.astimezone(UTC))  # polars compares tz names
    lk = signals_at(tm, sq, targets, starters(sq, targets), rating_history(sq), means, H_LK)
    t = targets.select(*KEY).join(lk.select(*KEY, *STEP2_SIGNALS), on=KEY, how="left")
    return t.with_columns([pl.lit(None, pl.Float64).alias(f"{x}_24") for x in STEP2_SIGNALS])


def live_basis(app: pl.DataFrame, matches: pl.DataFrame, bundle_of: Mapping[int, str],
               now: datetime) -> tuple[pl.DataFrame, pl.DataFrame]:  # fmt: skip
    """What a live forecast needs besides its XI, from matches final by now - RESULT_LAG:
    per team, the players' minute shares over its last PREV_N league matches (`usual` for
    the XI_N largest, as `usual`) with n_prev; per player, his rating history at its last
    rated appearance (r_mean, r_n, h_line), as `ratings_asof` reads it."""
    cut = (now - RESULT_LAG).astimezone(UTC)
    tm = team_matches(matches, bundle_of).filter(pl.col("kickoff") <= cut)
    sq = squads(app, tm["match_id"].unique())
    prev = (tm.sort("kickoff", "match_id", descending=True)
            .group_by("team_id", maintain_order=True).head(PREV_N)
            .select("team_id", prev_id="match_id"))  # fmt: skip
    pp = prev.join(sq.select("team_id", "player_id", "share", prev_id="match_id"),
                   on=["team_id", "prev_id"])  # fmt: skip
    n = prev.group_by("team_id").agg(n_prev=pl.len())
    u = pp.group_by("team_id", "player_id").agg(pl.col("share").sort().sum()).join(n, on="team_id")
    u = u.with_columns(share=pl.col("share") / pl.col("n_prev"))
    u = u.sort("team_id", "share", "player_id", descending=[False, True, False])
    u = u.with_columns(usual=pl.int_range(pl.len()).over("team_id") < XI_N)
    hist = rating_history(sq).filter(pl.col("kickoff") <= cut)
    last = (hist.sort("player_id", "kickoff").group_by("player_id", maintain_order=True).last()
            .select("player_id", "r_mean", "r_n", h_line="line"))  # fmt: skip
    return u, last


def live_signals(usual_: pl.DataFrame, players: pl.DataFrame, xi: pl.DataFrame,
                 means: Mapping[str, float]) -> pl.DataFrame:  # fmt: skip
    """(team_id, s_miss, s_rat, n_prev, n_xi) for `xi` (team_id, player_id, line) from the
    basis (`live_basis`), as `s_miss` and `s_rat` compute them. Null without a baseline
    (rule 8)."""
    xs = xi.join(usual_.select("team_id", "player_id", "share"), on=["team_id", "player_id"],
                 how="left")  # fmt: skip
    num = xs.group_by("team_id").agg(xi_share=pl.col("share").fill_null(0.0).sort().sum(),
                                     n_xi=pl.len())  # fmt: skip
    den = usual_.filter(pl.col("usual")).group_by("team_id").agg(
        usual_share=pl.col("share").sort().sum(), n_prev=pl.col("n_prev").first()
    )  # fmt: skip
    j = num.join(den, on="team_id", how="left")
    ok = (pl.col("n_prev") >= MIN_PREV) & (pl.col("usual_share") > 0) & (pl.col("n_xi") == XI_N)
    frac = pl.col("xi_share") / pl.col("usual_share")
    miss = j.select("team_id", pl.col("n_prev").fill_null(0), "n_xi",
                    s_miss=pl.when(ok).then(1.0 - frac))  # fmt: skip

    def shrunk(rows: pl.DataFrame) -> pl.DataFrame:
        r = rows.join(players, on="player_id", how="left")
        line = pl.coalesce(pl.col("line"), pl.col("h_line"), pl.lit(""))
        target = line.replace_strict(dict(means), default=means[""], return_dtype=pl.Float64)
        nn = pl.col("r_n").fill_null(0.0)
        return r.with_columns(
            r=(nn * pl.col("r_mean").fill_null(0.0) + RATING_PSEUDO * target) / (nn + RATING_PSEUDO)
        )

    ux = usual_.filter(pl.col("usual")).select("team_id", "player_id", line=pl.lit(None, pl.Utf8))
    ma = shrunk(xi).group_by("team_id").agg(r_xi=pl.col("r").sort().mean())
    mb = shrunk(ux).group_by("team_id").agg(r_usual=pl.col("r").sort().mean())
    rat = ma.join(mb, on="team_id", how="inner").select(
        "team_id", s_rat=pl.col("r_xi") - pl.col("r_usual"))  # fmt: skip
    out = miss.join(rat, on="team_id", how="left")
    return out.with_columns(s_rat=pl.when(pl.col("s_miss").is_not_null()).then(pl.col("s_rat")))


def save_step2(store: Any, table: pl.DataFrame, meta: dict[str, Any]) -> str:
    h = digest(table)
    buf = io.BytesIO()
    table.write_parquet(buf, compression="zstd")
    store.put(f"{STEP2_PREFIX}/{h}/table.parquet", buf.getvalue())
    store.put(f"{STEP2_PREFIX}/{h}/meta.json",
              json.dumps({**meta, "digest": h}, default=str).encode())  # fmt: skip
    store.put(f"{STEP2_PREFIX}/latest", h.encode())
    return h


def load_step2(store: Any, ref: str = "latest") -> tuple[pl.DataFrame, dict[str, Any], str]:
    h = store.get(f"{STEP2_PREFIX}/latest").decode() if ref == "latest" else ref
    table = pl.read_parquet(io.BytesIO(store.get(f"{STEP2_PREFIX}/{h}/table.parquet")))
    return table, json.loads(store.get(f"{STEP2_PREFIX}/{h}/meta.json")), h


# ------------------------------------------------------------------------------- check


def poisson_slope(y: Arr, offset: Arr, x: Arr, iters: int = 50) -> tuple[float, float]:
    """Slope b and its standard error in y ~ Poisson(exp(a + b x + offset)) (Newton)."""
    z = np.column_stack([np.ones_like(x), x])
    beta = np.array([float(np.log(y.mean() / np.exp(offset).mean())), 0.0])
    for _ in range(iters):
        m = np.exp(z @ beta + offset)
        info = z.T @ (z * m[:, None])
        step = np.linalg.solve(info, z.T @ (y - m))
        beta += step
        if np.abs(step).max() < 1e-10:
            break
    m = np.exp(z @ beta + offset)
    cov = np.linalg.inv(z.T @ (z * m[:, None]))
    return float(beta[1]), float(np.sqrt(cov[1, 1]))


def check(table: pl.DataFrame) -> dict[str, Any]:
    """Step 1: each signal alone, the Poisson slope of a team's goals with the team-season
    mean goals as offset; a pass needs the expected sign and the 95% interval excluding
    zero. Also reported: per standard deviation, coverage and correlations."""
    ts = table.group_by("team_id", "season").agg(ts_mean=pl.col("goals").mean())
    t = table.join(ts, on=["team_id", "season"]).filter(pl.col("ts_mean") > 0)
    out: dict[str, Any] = {"n_team_matches": table.height, "signals": {}}
    for s in SIGNALS:
        d = t.filter(pl.col(s).is_not_null() & pl.col(s).is_finite())
        x = d[s].to_numpy().astype(np.float64)
        sd = float(x.std())
        if d.height < 100 or sd == 0:
            out["signals"][s] = {"n": d.height, "pass": False}
            continue
        b, se = poisson_slope(d["goals"].to_numpy().astype(np.float64),
                              np.log(d["ts_mean"].to_numpy()), x)  # fmt: skip
        lo, hi = b - Z95 * se, b + Z95 * se
        sign = EXPECTED_SIGN[s]
        ok = (lo > 0) if sign > 0 else (hi < 0)
        out["signals"][s] = {"n": d.height, "mean": float(x.mean()), "sd": sd, "slope": b,
                             "se": se, "lo": lo, "hi": hi, "per_sd": b * sd,
                             "per_sd_lo": lo * sd, "per_sd_hi": hi * sd,
                             "pass": bool(ok)}  # fmt: skip
    both = t.drop_nulls(list(SIGNALS))
    pairs = [(a, b) for i, a in enumerate(SIGNALS) for b in SIGNALS[i + 1 :]]
    out["corr"] = ({f"{a}~{b}": float(np.corrcoef(both[a], both[b])[0, 1]) for a, b in pairs}
                   if both.height > 2 else {})  # fmt: skip
    out["passed"] = [s for s in SIGNALS if out["signals"][s].get("pass")]
    return out


def report(res: dict[str, Any], meta: dict[str, Any]) -> str:
    """Markdown, aggregates only (no team or player names)."""
    lines = ["# P3-j step 1: the dev signal check (D65)", "",
             f"{res['n_team_matches']} dev team-matches (the five bundles, 2019-2023), "
             "announced XI at T-60m.", "",
             "| signal | n | mean | sd | slope | 95% interval | per sd | expected | result |",
             "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for s in SIGNALS:
        r = res["signals"][s]
        exp = "-" if EXPECTED_SIGN[s] < 0 else "+"
        if "slope" not in r:
            lines.append(f"| {s} | {r['n']} | | | | | | {exp} | FAIL (too few rows) |")
            continue
        lines.append(f"| {s} | {r['n']} | {r['mean']:+.4f} | {r['sd']:.4f} | {r['slope']:+.4f} "
                     f"| [{r['lo']:+.4f}, {r['hi']:+.4f}] | {r['per_sd']:+.4f} | {exp} "
                     f"| {'pass' if r['pass'] else 'FAIL'} |")  # fmt: skip
    cv = ", ".join(f"{lam:g}: {m:.4f}" for lam, m in zip(meta["lambdas"], meta["cv_mse"],
                                                          strict=True))  # fmt: skip
    lines += ["", f"S_apm ridge penalty by time-ordered CV on dev: {meta['lambda']:g} "
              f"(next-week xG difference MSE over {meta['cv_n']} matches; {cv}).",
              "Correlations (rows with all three): " + ", ".join(
                  f"{k} {v:+.3f}" for k, v in res["corr"].items()), "",
              f"**Passed: {', '.join(res['passed']) if res['passed'] else 'none'}.** "
              + ("Step 2 uses these." if res["passed"]
                 else "P3-j stops here (D65 (b)).")]  # fmt: skip
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------- store


def digest(table: pl.DataFrame) -> str:
    buf = io.BytesIO()
    table.write_ipc(buf)
    return hashlib.sha256(buf.getvalue()).hexdigest()[:16]


def save(store: Any, table: pl.DataFrame, meta: dict[str, Any]) -> str:
    h = digest(table)
    buf = io.BytesIO()
    table.write_parquet(buf, compression="zstd")
    store.put(f"{PREFIX}/{h}/table.parquet", buf.getvalue())
    store.put(f"{PREFIX}/{h}/meta.json", json.dumps({**meta, "digest": h}, default=str).encode())
    store.put(f"{PREFIX}/latest", h.encode())
    return h


def main(argv: Sequence[str] | None = None) -> None:
    import argparse
    import subprocess
    from datetime import UTC

    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session
    from adatta.eval import harness
    from adatta.eval.selection import DEV, WARMUP
    from adatta.features import appearances as ap

    ap_ = argparse.ArgumentParser(prog="adatta.features.lineup_signals")
    ap_.add_argument("--out", default=None, help="also write the report here")
    ap_.add_argument(
        "--step2",
        action="store_true",
        help="build J's table (all loaded seasons, T-60m and T-24h) instead",
    )
    ap_.add_argument("--export", default="",
                     help="step 2: the selection report holding P3-b's P3-d export")  # fmt: skip
    args = ap_.parse_args(argv)
    cfg = harness.load_config()
    bundle_of = {int(x): b for b, ids in cfg.bundles.items() for x in ids}
    with get_session() as session:
        matches, _ = harness.load_matches(session, list(bundle_of))
        xg = harness.load_xg(session, list(bundle_of))
    matches = matches.filter(pl.col("season") <= DEV[-1])
    store = make_store(get_settings())
    app = ap.load(store, [(str(lg), y) for lg in bundle_of for y in range(WARMUP[0], DEV[-1] + 1)])
    warmup_end = datetime(WARMUP[-1] + 1, 7, 1, tzinfo=UTC)
    git = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    sha = git.stdout.strip()
    if args.step2:
        if not args.export:
            raise SystemExit("--step2 needs --export")
        export = pl.read_parquet(io.BytesIO(
            store.get(f"selection/reports/{args.export}/scored.parquet")))  # fmt: skip
        seasons = list(range(WARMUP[0], DEV[-1] + 1))
        t2, m2 = build_step2(app, matches, bundle_of, seasons, DEV, warmup_end, export)
        leak = step2_leakage(app, matches, bundle_of, t2, DEV, warmup_end, export)
        m2 = {**m2, "leakage": leak, "export": args.export, "code_sha": sha,
              "polars": pl.__version__, "numpy": np.__version__, "seasons": seasons}  # fmt: skip
        h2 = save_step2(store, t2, m2)
        dev_rows = t2.filter(pl.col("season").is_in(list(DEV)))
        cols = [*STEP2_SIGNALS, *(f"{x}_24" for x in STEP2_SIGNALS)]
        cover = ", ".join(f"{c} {dev_rows[c].is_not_null().sum() / max(dev_rows.height, 1):.1%}"
                          for c in cols)  # fmt: skip
        print(f"J table {h2} ({STEP2_PREFIX}), code {sha[:7]}: {t2.height} team-matches "
              f"({dev_rows.height} dev); dev coverage {cover}; scales "
              + ", ".join(f"{k} {v:.4f}" for k, v in m2["scale"].items())
              + f"; leakage {leak['changed']} of {leak['n']} changed.")  # fmt: skip
        return
    table, meta = build(app, matches, xg, bundle_of, DEV, warmup_end)
    res = check(table)
    meta = {**meta, "check": res, "code_sha": sha, "polars": pl.__version__,
            "numpy": np.__version__, "seasons": list(DEV)}  # fmt: skip
    h = save(store, table, meta)
    text = report(res, meta) + f"\nSignals table {h} ({PREFIX}), code {sha[:7]}.\n"
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)


if __name__ == "__main__":
    main()
