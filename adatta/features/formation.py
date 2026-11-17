"""P3-c (c): the formation table (docs/PHASE3_TASKS.md, spec §7.3).

One row per bundle-league team-match, as of kickoff - 24 h (the P3-b horizon): the team's
recency-weighted formation shares, its last formation, its coach's career shares (across
their earlier clubs) mixed with the team's by the coach's match count, opponent tier,
home/away, importance, and the actual formation (the outcome).

As-of: formations and coaches are read for matches final by the cutoff (kickoff <= as_of -
RESULT_LAG), as post-match facts. The coach is the coach of the team's last such match, so a
change since then is not seen. Opponent tier is the production model's pre-match
P(win) - P(loss) for the team from the stored dev backtest (run 43419004255), used only if
that prediction was made by the as-of time; otherwise 0 with `tier_known` = 0. It is a
model output, never odds (CLAUDE.md rule 1).
"""

from __future__ import annotations

import hashlib
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.features.referees import RESULT_LAG

HORIZON = timedelta(hours=24)
VOCAB_COVER = 0.97  # the vocabulary covers this share of warm-up team-matches
OTHER = "other"
TEAM_DECAY, TEAM_SPAN = 0.8, 10
COACH_DECAY, COACH_SPAN = 0.9, 30
COACH_KAPPA = 10.0  # the coach's shares weigh n / (n + kappa) against the team's
PREFIX = "features/formation/v1"
BASE = ["home", "tier", "tier_known", "must_win", "dead_rubber", "log_team_n", "coach_n",
        "tenure", "new_coach_5"]  # fmt: skip


@dataclass(frozen=True)
class Inputs:
    formations: pl.DataFrame  # match_id, team_id, kickoff, formation (all club matches)
    coaches: pl.DataFrame  # match_id, team_id, coach_ref
    context: pl.DataFrame  # match_id, home_importance, away_importance
    tier: pl.DataFrame  # match_id, as_of, p_home, p_away (dev backtest predictions)
    targets: pl.DataFrame  # match_id, league_ref, season, kickoff, home_team_id, away_team_id


def vocabulary(formations: pl.DataFrame, targets: pl.DataFrame,
               warmup: tuple[int, ...]) -> list[str]:  # fmt: skip
    """The fewest formations covering VOCAB_COVER of warm-up bundle team-matches."""
    tm = targets.filter(pl.col("season").is_in(warmup)).select("match_id")
    vc = formations.join(tm, on="match_id", how="semi")["formation"].value_counts(sort=True)
    share = (vc["count"].cum_sum() / vc["count"].sum()).to_list()
    k = next(i for i, c in enumerate(share) if c >= VOCAB_COVER) + 1
    return [*vc["formation"].to_list()[:k], OTHER]


def columns(vocab: list[str]) -> list[str]:
    k = range(len(vocab))
    return [*(f"ts_{i}" for i in k), *(f"cm_{i}" for i in k), *(f"last_{i}" for i in k)]


def _ew(idx: NDArray[np.int64], k: int, decay: float) -> NDArray[np.float64]:
    """Recency-weighted shares of the classes in idx (oldest first); zeros if empty."""
    out = np.zeros(k)
    if len(idx) == 0:
        return out
    w = decay ** np.arange(len(idx))[::-1]
    np.add.at(out, idx, w)
    return out / w.sum()


def build(inp: Inputs, vocab: list[str]) -> pl.DataFrame:
    k = len(vocab)
    code = {f: i for i, f in enumerate(vocab)}
    fm = (
        inp.formations.join(inp.coaches, on=["match_id", "team_id"], how="left")
        .with_columns(cls=pl.col("formation").replace_strict(code, default=k - 1))
        .sort("kickoff", "match_id")
    )
    ko_us = fm["kickoff"].dt.epoch("us").to_numpy()
    cls = fm["cls"].to_numpy().astype(np.int64)
    team = fm["team_id"].to_numpy()
    coach = fm["coach_ref"].to_list()
    by_team: dict[int, list[int]] = defaultdict(list)
    by_coach: dict[str, list[int]] = defaultdict(list)
    for i, (t, c) in enumerate(zip(team, coach, strict=True)):
        by_team[int(t)].append(i)
        if c is not None:
            by_coach[c].append(i)
    t_idx = {t: np.array(v) for t, v in by_team.items()}
    c_idx = {c: np.array(v) for c, v in by_coach.items()}
    # all formations final by a cutoff, for a team with no history of its own
    cum = np.vstack([np.zeros(k), np.cumsum(np.eye(k)[cls], axis=0)])

    ctx = {r["match_id"]: r for r in inp.context.iter_rows(named=True)}
    tier = {r["match_id"]: r for r in inp.tier.iter_rows(named=True)}
    own = {(r[0], r[1]): r[2] for r in fm.select("match_id", "team_id", "cls").iter_rows()}
    out = []
    for r in inp.targets.iter_rows(named=True):
        as_of = r["kickoff"] - HORIZON
        cut = int((as_of - RESULT_LAG).timestamp() * 1e6)
        for side, tid in (("home", r["home_team_id"]), ("away", r["away_team_id"])):
            y = own.get((r["match_id"], tid))
            if y is None:
                continue  # no complete lineup: no outcome
            ix = t_idx.get(tid, np.array([], dtype=np.int64))
            hist = ix[: int(np.searchsorted(ko_us[ix], cut, side="right"))] if len(ix) else ix
            ts = _ew(cls[hist[-TEAM_SPAN:]], k, TEAM_DECAY)
            if not len(hist):
                seen = cum[int(np.searchsorted(ko_us, cut, side="right"))]
                ts = seen / seen.sum() if seen.sum() else np.full(k, 1.0 / k)
            last = np.zeros(k)
            coach_ref, n_c, tenure = None, 0, 0
            if len(hist):
                last[cls[hist[-1]]] = 1.0
                coach_ref = coach[hist[-1]]
                if coach_ref is not None:
                    for j in hist[::-1]:
                        if coach[j] != coach_ref:
                            break
                        tenure += 1
            cm = ts
            if coach_ref is not None:
                cx = c_idx[coach_ref]
                ch = cx[: int(np.searchsorted(ko_us[cx], cut, side="right"))]
                n_c = min(len(ch), COACH_SPAN)
                if n_c:
                    sh = _ew(cls[ch[-COACH_SPAN:]], k, COACH_DECAY)
                    cm = (n_c * sh + COACH_KAPPA * ts) / (n_c + COACH_KAPPA)
            c = ctx.get(r["match_id"], {})
            imp = c.get(f"{side}_importance")
            p = tier.get(r["match_id"])
            known = p is not None and p["as_of"] <= as_of
            t_val = 0.0
            if known and p is not None:
                t_val = (p["p_home"] - p["p_away"]) * (1 if side == "home" else -1)
            out.append({
                "match_id": r["match_id"], "team_id": tid, "league_ref": r["league_ref"],
                "season": r["season"], "kickoff": r["kickoff"], "y": y,
                "home": float(side == "home"), "tier": t_val, "tier_known": float(known),
                "must_win": float(imp == "must_win"), "dead_rubber": float(imp == "dead_rubber"),
                "log_team_n": float(np.log1p(len(hist))), "coach_n": float(n_c),
                "tenure": float(tenure), "new_coach_5": float(0 < tenure <= 5),
                "coach_ref": coach_ref,
                **{f"ts_{i}": float(v) for i, v in enumerate(ts)},
                **{f"cm_{i}": float(v) for i, v in enumerate(cm)},
                **{f"last_{i}": float(v) for i, v in enumerate(last)},
            })  # fmt: skip
    schema: dict[str, Any] = {"kickoff": pl.Datetime("us", "UTC"), "coach_ref": pl.Utf8}
    return pl.DataFrame(out, schema_overrides=schema).sort("kickoff", "match_id", "team_id")


def digest(table: pl.DataFrame) -> str:
    data = table.drop("coach_ref").sort("match_id", "team_id").write_csv()
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def to_bytes(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()
