"""P3-f evaluation (docs/PHASE3_TASKS.md, P3-f (c)-(d)): the stored projections as of
kickoff - 24 h, P3-b against P3-f, scored against the matchday outcome from the final record.
Reads `lineup_projection` and appearances; no odds. Metrics only (the repo is public)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.db import models
from adatta.features import selection as sel

EPS = 1e-12
UNLISTED_MAX = 0.010  # (d)3: no worse than P3-b by more than 1.0% on unlisted rows
ECE_ALL, ECE_LISTED = 0.02, 0.05
MIN_LISTED = 1000  # (d): listed player-matches needed for the judgement
JUDGE_ON = datetime(2027, 1, 4)


def load(session: Session) -> pl.DataFrame:
    """Stored rows made as of kickoff - 24 h (one per player and match: the latest such
    run), for finished matches."""
    LP, M = models.LineupProjection, models.Match
    rows = session.execute(
        select(LP.match_id, LP.team_id, LP.player_id, LP.generated_at, LP.as_of, M.kickoff_time,
               M.status, LP.p_start, LP.p_bench, LP.p_out, LP.p_start_f, LP.p_bench_f,
               LP.p_out_f, LP.status)
        .join(M, M.match_id == LP.match_id).where(LP.p_start_f.is_not(None))
    ).all()  # fmt: skip

    def utc(t: datetime) -> datetime:
        return t if t.tzinfo else t.replace(tzinfo=UTC)

    dt = pl.Datetime("us", "UTC")
    df = pl.DataFrame(
        [(*r[:3], utc(r[3]), utc(r[4]), utc(r[5]), *r[6:]) for r in rows], orient="row",
        schema={"match_id": pl.Int64, "team_id": pl.Int64, "player_id": pl.Int64,
                "generated_at": dt, "as_of": dt, "kickoff": dt, "m_status": pl.Utf8,
                "p_start": pl.Float64, "p_bench": pl.Float64, "p_out": pl.Float64,
                "p_start_f": pl.Float64, "p_bench_f": pl.Float64, "p_out_f": pl.Float64,
                "status": pl.Utf8},
    )  # fmt: skip
    at24 = df.filter(pl.col("as_of") == pl.col("kickoff") - sel.HORIZON)
    return at24.sort("generated_at").unique(["match_id", "team_id", "player_id"], keep="last")


def outcomes(rows: pl.DataFrame, app: pl.DataFrame) -> pl.DataFrame:
    """y: START, BENCH or OUT from the final record (a pool player with no appearance row in
    a match whose squads are captured was out); matches without captured squads drop out."""
    known = app.select("match_id").unique()
    a = app.select("match_id", "team_id", "player_id", "started").unique(
        ["match_id", "team_id", "player_id"])  # fmt: skip
    out = rows.join(known, on="match_id").join(a, on=["match_id", "team_id", "player_id"],
                                               how="left")  # fmt: skip
    return out.with_columns(
        y=pl.when(pl.col("started").is_null()).then(sel.OUT)
        .when(pl.col("started")).then(sel.START).otherwise(sel.BENCH)
    ).drop("started")  # fmt: skip


def _ll(df: pl.DataFrame, suffix: str) -> np.ndarray:
    p = np.stack([df[f"p_start{suffix}"].to_numpy(), df[f"p_bench{suffix}"].to_numpy(),
                  df[f"p_out{suffix}"].to_numpy()], axis=1)  # fmt: skip
    col = np.select([df["y"].to_numpy() == sel.START, df["y"].to_numpy() == sel.BENCH], [0, 1], 2)
    return np.asarray(-np.log(np.maximum(p[np.arange(len(p)), col], EPS)), dtype=np.float64)


def paired(df: pl.DataFrame) -> dict[str, float]:
    """P3-f - P3-b three-way log loss per row, normal 95% interval; rel to P3-b's mean."""
    if df.is_empty():
        return {"n": 0, "mean": float("nan"), "lo": float("nan"), "hi": float("nan"),
                "rel": float("nan")}  # fmt: skip
    b, f = _ll(df, ""), _ll(df, "_f")
    d = f - b
    half = 1.96 * d.std(ddof=1) / np.sqrt(len(d)) if len(d) > 1 else float("nan")
    return {"n": len(d), "mean": float(d.mean()), "lo": float(d.mean() - half),
            "hi": float(d.mean() + half), "rel": float(d.mean() / b.mean())}  # fmt: skip


def ece_start(df: pl.DataFrame, col: str = "p_start_f", bins: int = 10) -> float:
    if df.is_empty():
        return float("nan")
    x = df.with_columns(obs=(pl.col("y") == sel.START).cast(pl.Float64),
                        bin=(pl.col(col) * bins).floor().clip(0, bins - 1))  # fmt: skip
    g = x.group_by("bin").agg(pl.len().alias("n"), pl.col(col).mean().alias("p"),
                              pl.col("obs").mean())  # fmt: skip
    n = g["n"].to_numpy().astype(float)
    return float((np.abs(g["p"].to_numpy() - g["obs"].to_numpy()) * n).sum() / n.sum())


@dataclass
class Verdict:
    passed: bool
    checks: list[dict[str, Any]]


def verdict(df: pl.DataFrame, leakage_ok: bool) -> Verdict:
    listed = df.filter(pl.col("status").is_not_null())
    unlisted = df.filter(pl.col("status").is_null())
    a, li, un = paired(df), paired(listed), paired(unlisted)
    checks = [
        {"check": "(d)1 all rows: P3-f vs P3-b log loss", "value": a["rel"], "lo": a["lo"],
         "hi": a["hi"], "pass": a["hi"] < 0},
        {"check": "(d)2 listed rows: P3-f vs P3-b", "value": li["rel"], "lo": li["lo"],
         "hi": li["hi"], "pass": li["hi"] < 0},
        {"check": "(d)3 unlisted rows: at most +1.0%", "value": un["rel"],
         "pass": un["rel"] <= UNLISTED_MAX},
        {"check": "(d)4 ECE of P(start)", "value": ece_start(df), "pass": ece_start(df) <= ECE_ALL},
        {"check": "(d)4 ECE of P(start), listed rows", "value": ece_start(listed),
         "pass": ece_start(listed) <= ECE_LISTED},
        {"check": "(d)5 leakage", "value": float(leakage_ok), "pass": leakage_ok},
    ]  # fmt: skip
    return Verdict(all(c["pass"] for c in checks), checks)


def leakage(df: pl.DataFrame, spells: pl.DataFrame, n: int = 100, seed: int = 20261120) -> bool:
    """(d)5: for n sampled rows, the status recomputed from spells cut at the row's as_of
    (later observations dropped, later closings undone) equals the stored one."""
    from adatta.sim import lineups as sl

    if df.is_empty():
        return True
    sample = df.sample(min(n, df.height), seed=seed)
    for r in sample.iter_rows(named=True):
        t = r["as_of"]
        cut = spells.filter((pl.col("match_id") == r["match_id"])
                            & (pl.col("player_id") == r["player_id"])
                            & (pl.col("observed_at") <= t)).with_columns(
            valid_to=pl.when(pl.col("valid_to") > t).then(None).otherwise(pl.col("valid_to")),
            superseded_at=pl.when(pl.col("superseded_at") > t).then(None)
            .otherwise(pl.col("superseded_at")),
        )  # fmt: skip
        key = pl.DataFrame(
            {"match_id": [r["match_id"]], "player_id": [r["player_id"]], "as_of": [t]},
            schema_overrides={"as_of": pl.Datetime("us", "UTC")},
        )
        st = sl.status_as_of(cut, key)
        got = st["status"][0] if st.height else None
        if got != r["status"]:
            return False
    return True


def report(df: pl.DataFrame, v: Verdict, final: bool, rhos: dict[str, float]) -> str:
    def f(x: Any, pct: bool = False) -> str:
        if x is None or (isinstance(x, float) and not np.isfinite(x)):
            return "-"
        return f"{100 * x:+.2f}%" if pct else f"{x:.4f}"

    listed = df.filter(pl.col("status").is_not_null())
    label = "FINAL" if final else "INTERIM (reported only; judged once, P3-f (d))"
    out = [f"# P3-f status-informed availability — live, {label}", "",
           f"**Verdict: {'pass' if v.passed else 'fail'}**", "",
           "| check | value | 95% interval | pass |", "|---|---|---|---|"]  # fmt: skip
    for c in v.checks:
        pct = "log loss" in c["check"] or "rows" in c["check"] and "ECE" not in c["check"]
        iv = f"[{f(c['lo'], True)}, {f(c['hi'], True)}]" if "lo" in c else ""
        out.append(f"| {c['check']} | {f(c['value'], pct)} | {iv} | "
                   f"{'yes' if c['pass'] else 'NO'} |")  # fmt: skip
    started = listed.filter(pl.col("y") == sel.START)
    out += ["", f"Rows {df.height} in {df['match_id'].n_unique()} matches; listed player-matches "
            f"{listed.height} (needed {MIN_LISTED}), of which started {started.height}.",
            f"rho now: missing {rhos.get('missing', float('nan')):.3f}, questionable "
            f"{rhos.get('questionable', float('nan')):.3f}."]  # fmt: skip
    for s_ in ("missing", "questionable"):
        g = listed.filter(pl.col("status") == s_)
        if g.height:
            pb, pf = g["p_start"].to_numpy().mean(), g["p_start_f"].to_numpy().mean()
            obs = (g["y"] == sel.START).to_numpy().mean()
            out.append(f"- {s_}: {g.height} rows, mean P(start) P3-b {pb:.3f}, P3-f {pf:.3f}, "
                       f"observed {obs:.3f}")  # fmt: skip
    return "\n".join(out)
