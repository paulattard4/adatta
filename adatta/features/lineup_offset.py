"""P3-d lineup offset table (docs/PHASE3_TASKS.md P3-d (a)-(b); spec §7.5).

For team T in match m, with t = kickoff - 24 h (P3-d0's as-of time):
  quality q_i    P3-d0 posterior-mean expected goals per 90 of player i at a line, in m's
                 league, as of t: hyperparameters of the latest P3-d0 goals refit at or
                 before t (the 2018-07-01 warm-up refit before that), player history final
                 by t (closed form, `eval.rates.predict` at e = 1, no sub/new/return flag).
  A              sum over outfield players of (minutes / 90) x q (goalkeepers excluded)
  A_bar          mean of A over T's previous PREV_N league matches final by t, with their
                 realised lineups and minutes and the same as-of quality (the typical lineup)
  delta          log(A / A_bar)
Three versions of A:
  real   the realised minutes of m (reported only: substitutions follow the score, so it
         carries the outcome; P3-d amendment 8)
  lk     lineups-known: the announced XI and bench, with P3-b's expected minutes given
         the role (exported by the P3-b rerun, `model.selection.role_minutes`)
  t24    T-24h: P3-b's start and bench probabilities times those minutes, over its pool
A missing input gives delta = 0 and a flag (CLAUDE.md rule 8: no imputed offset):
fewer than MIN_PREV previous matches, A or A_bar not positive, or (lk, t24) no P3-b rows.
Pure polars / numpy (runs on the Mac); the P3-d0 hyperparameter draws come from CI.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.adapters.storage import RawStore, try_get
from adatta.eval import rates as er
from adatta.features import rates as fr
from adatta.features.referees import RESULT_LAG

Arr = NDArray[np.float64]
PREFIX = "features/lineup_offset/v1"
PREV_N = 10  # the typical lineup: the team's previous 10 league matches
MIN_PREV = 3  # fewer previous matches: no baseline, delta = 0 (flagged)
STAT = "goals"
OUTFIELD = ("D", "M", "F")
CHUNK = 40_000  # quality rows per predict call (memory)
TRAIN = "lk"  # the version training rows read (amendment 8: pre-kickoff, as served at lk)
KEY = ["match_id", "team_id"]


# ------------------------------------------------------------------------------ squads


def squads(app: pl.DataFrame) -> pl.DataFrame:
    """Every matchday-squad row: role (started), realised e = minutes / 90 (0 if not
    played), and line: the row's G/D/M/F code, else the player's most common earlier one
    (as P3-d0)."""
    cols = ["match_id", "team_id", "player_id", "league_ref", "season", "kickoff", "started",
            "pos", "minutes"]  # fmt: skip
    s = fr._line(app.select(cols))
    return s.with_columns(
        e=pl.col("minutes").fill_null(0).cast(pl.Float64).clip(0) / 90.0,
    ).filter(pl.col("line").is_not_null())


def team_matches(sq: pl.DataFrame) -> pl.DataFrame:
    tm = sq.select(*KEY, "kickoff", "league_ref", "season").unique(KEY)
    return tm.sort("team_id", "kickoff", "match_id").with_columns(
        tidx=pl.int_range(pl.len()).over("team_id")
    )


def previous(tm: pl.DataFrame) -> pl.DataFrame:
    """(match_id, team_id, prev_id): the team's last PREV_N league matches whose result is
    final by the as-of time (kickoff - 24 h - RESULT_LAG)."""
    cand = tm.select(*KEY, "kickoff", "tidx").join(
        pl.DataFrame({"off": list(range(1, PREV_N + 3))}), how="cross"
    ).with_columns(ptidx=pl.col("tidx") - pl.col("off"))  # fmt: skip
    prev = tm.select("team_id", ptidx="tidx", prev_id="match_id", prev_kick="kickoff")
    j = cand.join(prev, on=["team_id", "ptidx"])
    j = j.filter(pl.col("prev_kick") <= pl.col("kickoff") - fr.HORIZON - RESULT_LAG)
    j = j.sort("off").group_by(KEY, maintain_order=True).head(PREV_N)
    return j.select(*KEY, "prev_id")


# ----------------------------------------------------------------------------- quality


def refit_for(times: list[datetime], t: datetime) -> datetime:
    """The latest refit at or before t; the first (warm-up) refit for anything earlier."""
    earlier = [x for x in times if x <= t]
    return max(earlier) if earlier else min(times)


def quality(rows: pl.DataFrame, hist: pl.DataFrame, posts: Mapping[datetime, dict[str, Arr]],
            leagues: tuple[str, ...]) -> pl.DataFrame:  # fmt: skip
    """q per row of (match_id, team_id, player_id, line, kickoff, league_ref): expected goals
    per 90 as of kickoff - 24 h. `hist` is the P3-d0 counts table; `posts` maps each goals
    refit time to its hyperparameter draws (log_mu, log_l, b, log_k)."""
    d = er.Design(leagues)
    h = d.frame(hist)
    times = sorted(posts)
    keys = ["match_id", "team_id", "player_id", "line"]
    base = rows.select(*keys, "kickoff", "league_ref").unique(keys).with_columns(
        sub=pl.lit(False), new_club=pl.lit(False), ret=pl.lit(False), slot=pl.lit(None, pl.Utf8),
        e=pl.lit(1.0), **{f"y_{STAT}": pl.lit(0, pl.Int32)},
    )  # fmt: skip
    ref = [refit_for(times, k - fr.HORIZON) for k in base["kickoff"].to_list()]
    base = base.with_columns(ref=pl.Series(ref, dtype=pl.Datetime("us", "UTC")))
    out = []
    for (r,), g in base.group_by(["ref"]):
        post = posts[r]
        for i in range(0, g.height, CHUNK):
            chunk = d.frame(g.slice(i, CHUNK))
            b = er.block_arrays(h, chunk, STAT)
            p = er.predict(post, d, b, STAT)
            out.append(b.rows.select(*keys).with_columns(q=pl.Series(p.mean),
                                                         hist_e=pl.Series(b.E90)))  # fmt: skip
    return pl.concat(out) if out else pl.DataFrame(
        schema={**{k: rows.schema[k] for k in keys}, "q": pl.Float64, "hist_e": pl.Float64}
    )  # fmt: skip


# ------------------------------------------------------------------------------- table


def p3b_rows(export: pl.DataFrame) -> pl.DataFrame:
    """P3-b's model rows with the exported columns, line from its position family."""
    e = export.filter(pl.col("model") == "model")
    return e.select(
        *KEY, "player_id", "p_start", "p_bench", "emin_start", "emin_bench",
        line=pl.when(pl.col("pos").is_in(fr.LINES)).then(pl.col("pos")),
    ).filter(pl.col("line").is_not_null())  # fmt: skip


def role_fallback(p3b: pl.DataFrame, rows: pl.DataFrame) -> pl.Series:
    """For announced players outside P3-b's pool (newcomers): the mean exported minutes for
    their line and role over P3-b rows of team-matches that kicked off strictly earlier
    (those predictions existed by then), so nothing later leaks in. Null if there are
    none: the player then adds nothing and is still counted. `p3b` and `rows` carry
    kickoff, line; `rows` also started."""
    out = np.full(rows.height, np.nan)
    rk = rows["kickoff"].dt.epoch("us").to_numpy()
    for line in fr.LINES:
        g = p3b.filter(pl.col("line") == line).sort("kickoff")
        k = g["kickoff"].dt.epoch("us").to_numpy()
        for started, col in ((True, "emin_start"), (False, "emin_bench")):
            on = ((rows["line"] == line) & (rows["started"] == started)).to_numpy()
            if not on.any() or len(k) == 0:
                continue
            cs = np.concatenate([[0.0], np.cumsum(g[col].fill_null(0.0).to_numpy())])
            i = np.searchsorted(k, rk[on], side="left")
            out[on] = np.where(i > 0, cs[i] / np.maximum(i, 1), np.nan)
    return pl.Series(out).fill_nan(None)


def build(app: pl.DataFrame, hist: pl.DataFrame, posts: Mapping[datetime, dict[str, Arr]],
          leagues: tuple[str, ...], export: pl.DataFrame | None = None,
          only: pl.DataFrame | None = None) -> pl.DataFrame:  # fmt: skip
    """One row per team-match (KEY) of `app` (or of `only`'s keys): A and delta for real,
    lk and t24. `app` holds the ten leagues' squad rows, `hist` the P3-d0 counts table,
    `export` P3-b's scored rows from the export rerun (None: lk and t24 not built)."""
    sq = squads(app)
    tm = team_matches(sq)
    prev = previous(tm)
    p3b_all = None
    if export is not None:
        p3b_all = p3b_rows(export).join(tm.select(*KEY, "kickoff"), on=KEY)
    if only is not None:
        keys = only.select(KEY).unique()
        tm = tm.join(keys, on=KEY)
        prev = prev.join(keys, on=KEY)
    out_f = sq.filter(pl.col("line").is_in(OUTFIELD))
    played = out_f.filter(pl.col("e") > 0)
    # rows whose quality is needed, all at the target team-match's as-of time
    tgt = tm.select(*KEY, "kickoff", "league_ref")
    need = [
        out_f.join(tm.select(KEY), on=KEY).select(*KEY, "player_id", "line"),
        prev.join(played.rename({"match_id": "prev_id"}), on=["team_id", "prev_id"])
        .select(*KEY, "player_id", "line"),
    ]  # fmt: skip
    p3b = None
    if p3b_all is not None:
        p3b = p3b_all.join(tm.select(KEY), on=KEY)
        need.append(p3b.filter(pl.col("line").is_in(OUTFIELD)).select(*KEY, "player_id", "line"))
    q_rows = pl.concat(need).unique().join(tgt, on=KEY)
    q = quality(q_rows, hist, posts, leagues)
    qk = ["match_id", "team_id", "player_id", "line"]

    def index(rows: pl.DataFrame, w: pl.Expr, name: str) -> pl.DataFrame:
        r = rows.join(q, on=qk, how="left")
        return r.group_by(KEY).agg((w * pl.col("q")).sum().alias(name),
                                   pl.col("q").is_null().sum().alias(f"{name}_noq"))  # fmt: skip

    real = index(played.join(tm.select(KEY), on=KEY), pl.col("e"), "a_real")
    pp = prev.join(played.rename({"match_id": "prev_id"}), on=["team_id", "prev_id"])
    pp = pp.join(q, on=qk, how="left")
    a_prev = pp.group_by(*KEY, "prev_id").agg((pl.col("e") * pl.col("q")).sum().alias("a"))
    bar = a_prev.group_by(KEY).agg(pl.col("a").mean().alias("a_bar"),
                                   pl.len().alias("n_prev"))  # fmt: skip
    t = tm.select(*KEY, "kickoff", "league_ref", "season").join(real, on=KEY, how="left")
    t = t.join(bar, on=KEY, how="left").with_columns(pl.col("n_prev").fill_null(0))
    if p3b is not None and p3b_all is not None:
        sq_m = out_f.join(tm.select(KEY), on=KEY).join(
            p3b.select(*KEY, "player_id", "emin_start", "emin_bench"),
            on=[*KEY, "player_id"], how="left",
        )  # fmt: skip
        fallback = role_fallback(p3b_all, sq_m)
        sq_m = sq_m.with_columns(
            in_pool=pl.col("emin_start").is_not_null(),
            emin=pl.when(pl.col("started")).then(pl.col("emin_start"))
            .otherwise(pl.col("emin_bench")).fill_null(fallback).fill_null(0.0),
        )  # fmt: skip
        lk = index(sq_m, pl.col("emin") / 90.0, "a_lk").join(
            sq_m.group_by(KEY).agg((~pl.col("in_pool")).sum().alias("n_lk_fallback")), on=KEY
        )  # fmt: skip
        pool = p3b.filter(pl.col("line").is_in(OUTFIELD))
        w24 = pl.col("p_start") * pl.col("emin_start") + pl.col("p_bench") * pl.col("emin_bench")
        a24 = index(pool, w24 / 90.0, "a_24")
        t = t.join(lk, on=KEY, how="left").join(a24, on=KEY, how="left")
    else:
        t = t.with_columns(a_lk=pl.lit(None, pl.Float64), a_24=pl.lit(None, pl.Float64),
                           n_lk_fallback=pl.lit(None, pl.UInt32))  # fmt: skip
    base_ok = (pl.col("n_prev") >= MIN_PREV) & (pl.col("a_bar") > 0)
    out = {}
    for v in ("real", "lk", "24"):
        a = pl.col(f"a_{v}")
        ok = base_ok & a.is_not_null() & (a > 0)
        out[f"ok_{v}"] = ok.fill_null(False)
        out[f"delta_{v}"] = pl.when(ok).then((a / pl.col("a_bar")).log()).otherwise(0.0)
    return t.with_columns(**out).sort("kickoff", *KEY)


# ------------------------------------------------------------------------------- store


def digest(table: pl.DataFrame) -> str:
    cols = ["match_id", "team_id", "delta_real", "delta_lk", "delta_24"]
    blob = table.select(cols).sort(KEY).write_csv().encode()
    return hashlib.sha256(blob).hexdigest()[:16]


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


def attach(df: pl.DataFrame, table: pl.DataFrame, horizons: tuple[str, ...] = ()) -> pl.DataFrame:
    """Joins a match frame (match_id, home, away) with each side's delta: lu_home / lu_away
    from the TRAIN version (what training rows read), and for each horizon h in
    `horizons` ("lk", "24") the target's own version as d{h}_home / d{h}_away. 0 where
    absent (rule 8: the term falls back to the 1b model)."""
    # pairs, not a dict: the training version is also a horizon's
    cols = [(f"delta_{TRAIN}", "lu"), *[(f"delta_{h}", f"d{h}") for h in horizons]]
    out = df
    for side in ("home", "away"):
        sel = table.select(
            pl.col("match_id"), pl.col("team_id").alias(side),
            *[pl.col(c).alias(f"{p}_{side}") for c, p in cols],
        )  # fmt: skip
        out = out.join(sel, on=["match_id", side], how="left")
    return out.with_columns([pl.col(f"{p}_{s}").fill_null(0.0) for _, p in cols
                             for s in ("home", "away")])  # fmt: skip
