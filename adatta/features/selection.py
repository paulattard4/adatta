"""Selection table for P3-b (docs/PHASE3_TASKS.md): for every bundle-league team-match, the
team's candidate pool with covariates as of kickoff - 24 h, and each player's outcome
(start / bench / out of the matchday squad) and normal-time minutes.

Inputs, all as of the prediction time:
- appearances (adatta/features/appearances.py) of club matches final by as_of
  (kickoff <= as_of - RESULT_LAG), in every stored competition;
- the coach of each team-match (match_coach, known at lineup release, used only for matches
  already final);
- the h48 context snapshot (adatta/features/context_table.py): congestion, the next
  match's kind and importance for the team's side. It is taken as of kickoff - 48 h, a day
  earlier than the forecast, so it carries less information, never more (amendment
  recorded in PHASE3_TASKS.md).

Pool: players with a matchday-squad appearance for the team in the 365 days before as_of,
dropped once they have appeared for another club after their last appearance for this
one. National and youth sides are not clubs here.

Rows hold match-level data: the table is stored only in the private raw store
(`features/selection/v1/<digest>.parquet`), never in git or public artifacts.
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
POOL_DAYS = 365
EW_DECAY = 0.8  # weight of the k-th previous team match: 0.8**k, k = 0..EW_SPAN-1
EW_SPAN = 10
CAP = 10  # cap on streak and gap counts
CLUB_CAP = 20  # cap on matches since a player's first appearance for the club
POS = ("G", "D", "M", "F")
IMPORTANCE = ("dead_rubber", "must_win")  # "live" and missing are the base
NEXT_KIND = ("cup", "continental")  # "league" and missing are the base
PREFIX = "features/selection/v1"

OUT, BENCH, START = 0, 1, 2

FEATURES = [
    "last_start", "last_bench", "ew_start", "ew_squad", "last_minutes", "log_days_since",
    "out_streak", "returned_gap", "log_club_matches", "sent_off_last", "coach_changed",
    "never_started", "rest_days", "days_to_next", "next_cup", "next_continental",
    "dead_rubber", "must_win",
]  # fmt: skip


@dataclass(frozen=True)
class Inputs:
    """appearances: match_id, team_id, player_id, kickoff, started, minutes_nt, sent_off, pos
    (club matches only); coaches: match_id, team_id, coach_ref; context: the h48 snapshot;
    targets: match_id, league_ref, season, kickoff, home_team_id, away_team_id."""

    appearances: pl.DataFrame
    coaches: pl.DataFrame
    context: pl.DataFrame
    targets: pl.DataFrame


def _ctx_sides(ctx: pl.DataFrame) -> dict[tuple[int, str], dict[str, Any]]:
    out: dict[tuple[int, str], dict[str, Any]] = {}
    for r in ctx.iter_rows(named=True):
        for side in ("home", "away"):
            out[(r["match_id"], side)] = {
                "rest_days": r[f"{side}_days_since_last"],
                "days_to_next": r[f"{side}_days_to_next"],
                "next_kind": r[f"{side}_next_kind"],
                "importance": r[f"{side}_importance"],
            }
    return out


def _other_club_times(app: pl.DataFrame) -> dict[int, tuple[NDArray[np.int64], NDArray[np.int64]]]:
    """player -> (sorted kickoff times in µs, team ids) of every club appearance."""
    out = {}
    a = app.select("player_id", "team_id", pl.col("kickoff").dt.epoch("us").alias("t")).sort(
        "player_id", "t"
    )
    for (pid,), g in a.group_by("player_id", maintain_order=True):
        out[int(pid)] = (g["t"].to_numpy(), g["team_id"].to_numpy())
    return out


def build(inp: Inputs) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(player rows, team-match rows) for every target team-match whose team has a lineup in
    that match. Player rows: the pool with FEATURES, pos, y (OUT/BENCH/START) and minutes_nt.
    Team-match rows: pool size, the squad's newcomers (not in the pool) by outcome, actual
    and expected bench size (mean of the team's last 5 matches).
    A target with an `as_of` (served projections, a fixture not played yet) is built as of
    that time instead of kickoff - 24 h, with or without a lineup; its outcomes are -1 / NaN."""
    app = inp.appearances.sort("kickoff", "match_id")
    us = 1_000_000
    ctx = _ctx_sides(inp.context)
    coach = {(m, t): c for m, t, c in inp.coaches.select("match_id", "team_id", "coach_ref")
             .iter_rows()}  # fmt: skip
    clubs = _other_club_times(app)
    want: dict[int, list[tuple[int, str, int, str, int | None]]] = defaultdict(list)
    tg = inp.targets
    if "as_of" not in tg.columns:
        tg = tg.with_columns(as_of=pl.lit(None, pl.Datetime("us", "UTC")))
    for m, lg, yr, h, a, t in tg.select(
        "match_id",
        "league_ref",
        "season",
        "home_team_id",
        "away_team_id",
        pl.col("as_of").dt.epoch("us"),
    ).iter_rows():
        want[h].append((m, lg, yr, "home", t))
        want[a].append((m, lg, yr, "away", t))
    rows: list[pl.DataFrame] = []
    tms: list[dict[str, object]] = []
    for (team,), g in app.filter(pl.col("team_id").is_in(list(want))).group_by(
        "team_id", maintain_order=True
    ):
        team = int(team)
        matches = g.select("match_id", "kickoff").unique().sort("kickoff", "match_id")
        mids = matches["match_id"].to_numpy()
        times = matches["kickoff"].dt.epoch("us").to_numpy()
        col = {int(m): i for i, m in enumerate(mids)}
        players = np.sort(g["player_id"].unique().to_numpy())
        row = {int(p): i for i, p in enumerate(players)}
        n_p, n_k = len(players), len(mids)
        o = np.zeros((n_p, n_k), np.int8)
        mins = np.zeros((n_p, n_k), np.float64)
        sent = np.zeros((n_p, n_k), np.int8)
        posn = np.full((n_p, n_k), -1, np.int8)
        for m, p, st, mn, so, ps in g.select(
            "match_id", "player_id", "started", "minutes_nt", "sent_off", "pos"
        ).iter_rows():
            i, k = row[p], col[m]
            o[i, k] = START if st else BENCH
            mins[i, k] = mn or 0
            sent[i, k] = 1 if so else 0
            posn[i, k] = POS.index(ps) if ps in POS else -1
        coaches = [coach.get((int(m), team)) for m in mids]
        squad = o > OUT
        for m, lg, yr, side, t_given in want[team]:
            j = col.get(m)
            if t_given is not None:
                as_of = int(t_given)  # a served projection: as of its generation time
            elif j is None:
                continue  # no lineup for this team in this match
            else:
                as_of = times[j] - int(HORIZON.total_seconds()) * us
            cutoff = as_of - int(RESULT_LAG.total_seconds()) * us
            p_idx = int(np.searchsorted(times, cutoff, "right")) - 1
            if p_idx < 0:
                continue  # no history for this team yet
            lo = int(np.searchsorted(times, as_of - POOL_DAYS * 86_400 * us, "left"))
            hist = slice(lo, p_idx + 1)
            in_window = squad[:, hist].any(axis=1)
            last_k = np.where(squad[:, : p_idx + 1], np.arange(p_idx + 1), -1).max(axis=1)
            pool = in_window.copy()
            for pi in np.flatnonzero(pool):
                t_all, team_all = clubs[int(players[pi])]
                after = (t_all > times[last_k[pi]]) & (t_all <= cutoff) & (team_all != team)
                if after.any():
                    pool[pi] = False
            idx = np.flatnonzero(pool)
            ks = np.arange(p_idx, max(p_idx - EW_SPAN, -1), -1)
            w = EW_DECAY ** np.arange(len(ks))
            oh = o[np.ix_(idx, ks)]
            ew_start = (oh == START) @ w / w.sum()
            ew_squad = (oh > OUT) @ w / w.sum()
            played = mins[idx, : p_idx + 1] > 0
            last_play = np.where(played, np.arange(p_idx + 1), -1).max(axis=1)
            days_since = np.where(
                last_play >= 0, (as_of - times[np.maximum(last_play, 0)]) / (86_400 * us), POOL_DAYS
            )
            out_streak = np.zeros(len(idx))
            gap = np.zeros(len(idx))
            first = np.argmax(squad[idx, : p_idx + 1], axis=1)
            for q, pi in enumerate(idx):
                kk = p_idx
                while kk >= 0 and not squad[pi, kk] and out_streak[q] < CAP:
                    out_streak[q] += 1
                    kk -= 1
                r = int(last_k[pi])
                if p_idx - r < 3:
                    kk = r - 1
                    while kk >= 0 and not squad[pi, kk] and gap[q] < CAP:
                        gap[q] += 1
                        kk -= 1
            starts = o[idx, : p_idx + 1] == START
            last_start_k = np.where(starts, np.arange(p_idx + 1), -1).max(axis=1)
            coach_now = coaches[p_idx]
            coach_changed = np.array([
                1.0 if ls >= 0 and coach_now is not None and coaches[ls] is not None
                and coaches[ls] != coach_now else 0.0 for ls in last_start_k
            ])  # fmt: skip
            pos_counts = np.stack(
                [(posn[idx, : p_idx + 1] == q).sum(axis=1) for q in range(len(POS))], axis=1
            )
            pos = np.where(pos_counts.sum(axis=1) > 0, pos_counts.argmax(axis=1), -1)
            c = ctx.get((m, side), {})
            imp, nk = c.get("importance"), c.get("next_kind")
            rest, nxt = c.get("rest_days"), c.get("days_to_next")
            n = len(idx)
            df = pl.DataFrame({
                "match_id": np.full(n, m), "team_id": np.full(n, team),
                "player_id": players[idx], "league_ref": [lg] * n, "season": np.full(n, yr),
                "pos": [POS[q] if q >= 0 else "U" for q in pos],
                "last_start": (o[idx, p_idx] == START).astype(float),
                "last_bench": (o[idx, p_idx] == BENCH).astype(float),
                "ew_start": ew_start, "ew_squad": ew_squad,
                "last_minutes": mins[idx, p_idx] / 90,
                "log_days_since": np.log1p(np.minimum(days_since, POOL_DAYS)),
                "out_streak": out_streak, "returned_gap": np.log1p(gap),
                "log_club_matches": np.log1p(np.minimum(p_idx - first + 1, CLUB_CAP)),
                "sent_off_last": sent[idx, p_idx].astype(float),
                "coach_changed": coach_changed,
                "never_started": (last_start_k < 0).astype(float),
                "rest_days": np.full(n, float(rest) if rest is not None else np.nan),
                "days_to_next": np.full(n, float(nxt) if nxt is not None else np.nan),
                "next_cup": np.full(n, float(nk == "cup")),
                "next_continental": np.full(n, float(nk == "continental")),
                "dead_rubber": np.full(n, float(imp == "dead_rubber")),
                "must_win": np.full(n, float(imp == "must_win")),
                "y": o[idx, j].astype(np.int64) if j is not None else np.full(len(idx), -1),
                "minutes_nt": mins[idx, j] if j is not None else np.full(len(idx), np.nan),
                # minutes in the team's last EW_SPAN matches (NaN before the team's history)
                "m_hist": np.concatenate(
                    [mins[np.ix_(idx, ks)], np.full((n, EW_SPAN - len(ks)), np.nan)], axis=1
                ),
                "n_club": np.minimum(p_idx - first + 1, EW_SPAN),
            })  # fmt: skip
            rows.append(df)
            bench_hist = (o[:, max(p_idx - 4, 0) : p_idx + 1] == BENCH).sum(axis=0)
            if j is not None:
                new = squad[:, j] & ~pool
                out_j = {"new_start": int((o[new, j] == START).sum()),
                         "new_bench": int((o[new, j] == BENCH).sum()),
                         "bench_actual": int((o[:, j] == BENCH).sum()),
                         "starters": int((o[:, j] == START).sum())}  # fmt: skip
            else:
                out_j = {"new_start": -1, "new_bench": -1, "bench_actual": -1, "starters": -1}
            tms.append({
                "match_id": m, "team_id": team, "league_ref": lg, "season": yr,
                "as_of_us": int(as_of), "pool": n,
                "bench_expected": float(bench_hist.mean()), **out_j,
            })  # fmt: skip
    players_df = pl.concat(rows) if rows else pl.DataFrame()
    return players_df, pl.DataFrame(tms)


def digest(players: pl.DataFrame) -> str:
    flat = players.with_columns(
        pl.col(c).cast(pl.List(pl.Float64)).list.eval(pl.element().cast(pl.Utf8)).list.join(",")
        for c, t in players.schema.items()
        if isinstance(t, pl.Array)
    )  # CSV can't hold array columns
    return hashlib.sha256(
        flat.sort("match_id", "team_id", "player_id").write_csv().encode()
    ).hexdigest()[:16]


def to_bytes(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()
