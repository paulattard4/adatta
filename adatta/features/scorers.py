"""P3-e player table (docs/PHASE3_TASKS.md P3-e, restated: scorers; spec §7.5 steps 3-4).

One row per (match, team, player) of a target team-match: every player in the announced
matchday squad (the lineups-known population) and every player in P3-b's pool (the T-24h
population). For player i, with t = kickoff - 24 h (P3-d0's as-of time):
  e_lk         expected minutes / 90 given the announced role (P3-b's export, with P3-d's
               role fallback outside its pool), as P3-d's delta_lk; 0 outside the squad
  e24_start    P(start) x expected minutes as a starter / 90 (P3-b's pool; 0 outside it)
  e24_bench    P(bench) x expected minutes from the bench / 90
  q_{s}_start  P3-d0's posterior-mean per-90 rate of statistic s (goals, assists) at the
  q_{s}_sub    player's line in the match's league as of t, as a starter or a substitute
               (line level, P3-d amendment 1)
  r0_{s}       P3-d0's R0: the line x league per-90 rate over the refit's 730-day window
  r1_{s}       P3-d0's R1: the player's own per-90 rate shrunk to R0 by C[s] 90s
  y_{s}        the realised count (own goals excluded); 0 for a squad or pool player who
               did not play, null where the statistic is not collected (P3-d0's rule)
and per team-match the league shares over the 730 days final by t:
  o            own goals / all goals
  alpha        assists / non-own goals (team-matches with assists collected)
Pure polars / numpy (runs on the Mac); the P3-d0 hyperparameter draws come from CI.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.adapters.storage import RawStore, try_get
from adatta.eval import rates as er
from adatta.features import lineup_offset as lo
from adatta.features import rates as fr
from adatta.features.referees import RESULT_LAG

Arr = NDArray[np.float64]
PREFIX = "features/scorers/v1"
STATS = ("goals", "assists")
C = {"goals": 20.0, "assists": 50.0}  # R1 pseudo-exposure in 90s, P3-d0's warm-up choice
WINDOW = timedelta(days=730)  # as P3-d0's refits and history
KEY = lo.KEY
PKEY = [*KEY, "player_id"]
U18_YEARS = 18


def _cut(kickoff: pl.Expr) -> tuple[pl.Expr, pl.Expr]:
    """(cut, start) of the as-of window: final by kickoff - 24 h, within WINDOW of it."""
    t = kickoff - fr.HORIZON
    return t - RESULT_LAG, t - WINDOW


# ------------------------------------------------------------------------- population


def population(app: pl.DataFrame, export: pl.DataFrame | None,
               targets: pl.DataFrame) -> pl.DataFrame:  # fmt: skip
    """Squad and pool rows of the target team-matches (KEY), with roles and minutes."""
    sq = lo.squads(app).join(targets.select(KEY), on=KEY)
    tm = lo.team_matches(lo.squads(app)).select(*KEY, "kickoff", "league_ref", "season")
    sq = sq.select(*PKEY, "line", "started", played=pl.col("e") > 0, in_squad=pl.lit(True))
    if export is None:
        f64 = ("p_start", "p_bench", "emin_start", "emin_bench")
        schema = {
            **{k: pl.Int64 for k in PKEY},
            "line_p": pl.Utf8,
            **dict.fromkeys(f64, pl.Float64),
        }
        pool = pl.DataFrame(schema=schema)
    else:
        pool = lo.p3b_rows(export).join(targets.select(KEY), on=KEY).rename({"line": "line_p"})
    rows = sq.join(pool, on=PKEY, how="full", coalesce=True)
    rows = rows.join(tm, on=KEY, how="left").with_columns(
        line=pl.coalesce("line", "line_p"),
        in_squad=pl.col("in_squad").fill_null(False),
        in_pool=pl.col("p_start").is_not_null(),
        started=pl.col("started").fill_null(False),
        played=pl.col("played").fill_null(False),
    ).drop("line_p")  # fmt: skip
    rows = rows.filter(pl.col("line").is_not_null() & pl.col("kickoff").is_not_null())
    rows = rows.unique(PKEY, keep="first", maintain_order=True)
    if export is not None:
        p3b_all = lo.p3b_rows(export).join(tm.select(*KEY, "kickoff"), on=KEY)
        fallback = lo.role_fallback(p3b_all, rows)
    else:
        fallback = pl.Series([None] * rows.height, dtype=pl.Float64)
    emin = (pl.when(pl.col("started")).then(pl.col("emin_start"))
            .otherwise(pl.col("emin_bench")).fill_null(fallback).fill_null(0.0))  # fmt: skip
    return rows.with_columns(
        e_lk=pl.when(pl.col("in_squad")).then(emin / 90.0).otherwise(0.0),
        lk_fallback=pl.col("in_squad") & ~pl.col("in_pool"),
        e24_start=(pl.col("p_start") * pl.col("emin_start")).fill_null(0.0) / 90.0,
        e24_bench=(pl.col("p_bench") * pl.col("emin_bench")).fill_null(0.0) / 90.0,
    ).drop("emin_start", "emin_bench")  # fmt: skip


# ------------------------------------------------------------------------------ rates


def refit_windows(hist: pl.DataFrame, times: list[datetime]) -> dict[datetime, pl.DataFrame]:
    """Each refit's training window, as P3-d0 (`model.rates._window`): appearances final
    by the refit time, within the 730 days before it. `hist` carries the design columns."""
    final = pl.col("kickoff") + RESULT_LAG
    return {t: hist.filter((final <= t) & (pl.col("kickoff") > t - WINDOW)) for t in times}


def rates(rows: pl.DataFrame, hist: pl.DataFrame, posts: Mapping[str, Mapping[datetime, Any]],
          leagues: tuple[str, ...]) -> pl.DataFrame:  # fmt: skip
    """q (starter and substitute), r0 and r1 of every row for each statistic in `posts`."""
    d = er.Design(leagues)
    h = d.frame(hist)
    keys = ["match_id", "team_id", "player_id", "line"]
    base = rows.select(*keys, "kickoff", "league_ref").unique(keys)
    out = base.select(keys)
    for stat, p in posts.items():
        q0 = lo.quality(base, hist, p, leagues, stat, sub=False)
        q1 = lo.quality(base, hist, p, leagues, stat, sub=True).select(*keys, q_sub="q")
        times = sorted(p)
        ref = [lo.refit_for(times, k - fr.HORIZON) for k in base["kickoff"].to_list()]
        b = d.frame(base.with_columns(
            sub=pl.lit(False), new_club=pl.lit(False), ret=pl.lit(False),
            slot=pl.lit(None, pl.Utf8), ref=pl.Series(ref, dtype=pl.Datetime("us", "UTC")),
        ))  # fmt: skip
        r0 = np.full(b.height, np.nan)
        refs = b["ref"].to_numpy()
        li, gi = b["li"].to_numpy(), b["gi"].to_numpy()
        for t, win in refit_windows(h, times).items():
            on = refs == np.datetime64(t.replace(tzinfo=None), "us")
            if on.any():
                tab = er.r0_table(win, stat, len(leagues))
                r0[on] = tab[li[on], gi[on]]
        s = b.select(keys).with_columns(r0=pl.Series(r0))
        s = s.join(q0, on=keys, how="left").join(q1, on=keys, how="left")
        s = s.with_columns(
            pl.col("hist_y").fill_null(0.0), pl.col("hist_e").fill_null(0.0),
        ).with_columns(
            r1=(pl.col("hist_y") + C[stat] * pl.col("r0")) / (pl.col("hist_e") + C[stat]),
        )  # fmt: skip
        out = out.join(s.select(keys, **{f"q_{stat}_start": "q", f"q_{stat}_sub": "q_sub",
                                         f"r0_{stat}": "r0", f"r1_{stat}": "r1",
                                         f"hist_e_{stat}": "hist_e"}),
                       on=keys, how="left")  # fmt: skip
    return out


# --------------------------------------------------------------------------- outcomes


def outcomes(rows: pl.DataFrame, hist: pl.DataFrame, stats: tuple[str, ...]) -> pl.DataFrame:
    """y_{s}: the counts table's value for a played row; 0 for a row that did not play in
    a team-match where s is collected (any played row of it has a value); else null."""
    out = rows
    for s in stats:
        y = f"y_{s}"
        h = hist.select(*PKEY, y).rename({y: "_y"})
        ok = hist.group_by(KEY).agg(pl.col(y).is_not_null().any().alias("_ok"))
        out = out.join(h, on=PKEY, how="left").join(ok, on=KEY, how="left").with_columns(
            pl.when(pl.col("played")).then(pl.col("_y"))
            .when(pl.col("_ok").fill_null(False)).then(0)
            .otherwise(None).cast(pl.Int32).alias(y),
        ).drop("_y", "_ok")  # fmt: skip
    return out


def penalties(rows: pl.DataFrame, app: pl.DataFrame) -> pl.DataFrame:
    """y_pen: penalty goals scored in the match (reported only, §8.3 is Phase 3b)."""
    p = app.select(*PKEY, _pen=pl.col("pen_scored"))
    return rows.join(p, on=PKEY, how="left").with_columns(
        y_pen=pl.col("_pen").fill_null(0).cast(pl.Int32)
    ).drop("_pen")  # fmt: skip


def shares(app: pl.DataFrame, targets: pl.DataFrame) -> pl.DataFrame:
    """o and alpha per target team-match (KEY, kickoff, league_ref) from the league's
    team-matches final by kickoff - 24 h, within the 730 days before then."""
    tm = app.group_by("league_ref", "match_id", "team_id").agg(
        pl.col("kickoff").first(),
        pl.col("goals").fill_null(0).sum().alias("g"),
        pl.col("own_goals").fill_null(0).sum().alias("og"),
        pl.col("assists").fill_null(0).sum().alias("a"),
        pl.col("assists").is_not_null().any().alias("a_ok"),
    ).sort("league_ref", "kickoff")  # fmt: skip
    cut, start = _cut(pl.col("kickoff"))
    tg = targets.select(*KEY, "kickoff", "league_ref").with_columns(_cut=cut, _start=start)
    parts = []
    for (lg,), t in tg.group_by(["league_ref"]):
        g = tm.filter(pl.col("league_ref") == lg)
        k = g["kickoff"].dt.epoch("us").to_numpy()

        def cum(x: Arr) -> Arr:
            return np.concatenate([[0.0], np.cumsum(x)])

        a_ok = g["a_ok"].to_numpy()
        G, OG = cum(g["g"].to_numpy().astype(float)), cum(g["og"].to_numpy().astype(float))
        GA = cum(np.where(a_ok, g["g"].to_numpy(), 0).astype(float))
        A = cum(np.where(a_ok, g["a"].to_numpy(), 0).astype(float))
        hi = np.searchsorted(k, t["_cut"].dt.epoch("us").to_numpy(), side="right")
        lo_ = np.searchsorted(k, t["_start"].dt.epoch("us").to_numpy(), side="right")
        goals, own = G[hi] - G[lo_], OG[hi] - OG[lo_]
        ga, a = GA[hi] - GA[lo_], A[hi] - A[lo_]
        with np.errstate(divide="ignore", invalid="ignore"):
            o = np.where(goals + own > 0, own / (goals + own), np.nan)
            alpha = np.where(ga > 0, a / ga, np.nan)
        parts.append(t.select(KEY).with_columns(o=pl.Series(o), alpha=pl.Series(alpha)))
    return pl.concat(parts).fill_nan(None)


# ------------------------------------------------------------------------------ build


def build(app: pl.DataFrame, hist: pl.DataFrame, posts: Mapping[str, Mapping[datetime, Any]],
          leagues: tuple[str, ...], export: pl.DataFrame | None, targets: pl.DataFrame,
          birth: pl.DataFrame | None = None) -> pl.DataFrame:  # fmt: skip
    """The player table for `targets` (KEY). `app` holds the ten leagues' squad rows, `hist`
    the P3-d0 counts table, `posts` each statistic's refit draws, `export` P3-b's scored
    rows from the export rerun, `birth` (player_id, birth_date) for the under-18 flag."""
    rows = population(app, export, targets)
    r = rates(rows, hist, posts, leagues)
    rows = rows.join(r, on=["match_id", "team_id", "player_id", "line"], how="left")
    rows = penalties(outcomes(rows, hist, tuple(posts)), app)
    rows = rows.join(shares(app, rows.select(*KEY, "kickoff", "league_ref").unique(KEY)),
                     on=KEY, how="left")  # fmt: skip
    if birth is not None:
        b = birth.select("player_id", "birth_date")
        rows = rows.join(b, on="player_id", how="left").with_columns(
            u18=(pl.col("birth_date").is_not_null()
                 & (pl.col("birth_date").dt.offset_by(f"{U18_YEARS}y") > pl.col("kickoff"))),
        ).drop("birth_date")  # fmt: skip
    else:
        rows = rows.with_columns(u18=pl.lit(False))
    return rows.sort("kickoff", *PKEY)


# ------------------------------------------------------------------------------ store


def digest(table: pl.DataFrame) -> str:
    return hashlib.sha256(to_bytes(table)).hexdigest()[:16]


def to_bytes(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def save(store: RawStore, table: pl.DataFrame, meta: dict[str, Any]) -> str:
    h = digest(table)
    store.put(f"{PREFIX}/{h}/table.parquet", to_bytes(table))
    store.put(f"{PREFIX}/{h}/meta.json", json.dumps({**meta, "digest": h}, default=str).encode())
    store.put(f"{PREFIX}/latest", h.encode())
    return h


def load(store: RawStore, ref: str = "latest") -> tuple[pl.DataFrame, dict[str, Any], str]:
    h = store.get(f"{PREFIX}/latest").decode() if ref == "latest" else ref
    table = pl.read_parquet(io.BytesIO(store.get(f"{PREFIX}/{h}/table.parquet")))
    meta = try_get(store, f"{PREFIX}/{h}/meta.json")
    return table, json.loads(meta) if meta else {}, h
