"""P3-h (docs/PHASE3_TASKS.md): P3-c served, and the last lineup in that formation.

P3-c as it passed on dev, with opponent tier unknown in training and prediction
(amendment 1), refitted per bundle every 28 days on CI (`model.formation serve-refit`);
its draws are stored here. A daily run gives every regular-season bundle fixture of the
next 7 days (the fixtures P3-b projects) the formation distribution per team, and the pitch:
the team's last lineup in the most likely named formation, final by the as-of time. The
pitch is history, not a forecast of who starts. Rows are appended to
`formation_projection`. No odds, no API requests. Numpy only (the fit is the model group's).
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from sqlalchemy.orm import Session

from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.features.referees import RESULT_LAG
from adatta.sim.lineups import _utc

Arr = NDArray[np.float64]
PREFIX = "serving/formation"
REFIT_DAYS = 28  # D14, as P3-c on dev
DEV_TABLE = "bd4423912693aa6d"  # P3-c's dev table with tier unknown (amendment 1): its vocab


# ----------------------------------------------------------------------------- refits


def refit_key(bundle: str, as_of: datetime) -> str:
    return f"{PREFIX}/{bundle}/{as_of:%Y%m%dT%H%M}.npz"


def latest_key(bundle: str) -> str:
    return f"{PREFIX}/{bundle}/latest.json"


def encode(post: dict[str, Arr], vocab: list[str], meta: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    extra = json.dumps({"vocab": vocab, "meta": meta}, default=str)
    np.savez_compressed(buf, **{f"post__{k}": v for k, v in post.items()},  # type: ignore[arg-type]
                        extra=np.array(extra))  # fmt: skip
    return buf.getvalue()


def decode(data: bytes) -> tuple[dict[str, Arr], list[str], dict[str, Any]]:
    with np.load(io.BytesIO(data)) as z:
        post = {k[6:]: z[k] for k in z.files if k.startswith("post__")}
        extra = json.loads(str(z["extra"]))
    return post, list(extra["vocab"]), extra["meta"]


def latest(store: RawStore, bundle: str) -> tuple[str, datetime] | None:
    """The latest passing serving refit of a bundle: (key, as_of)."""
    raw = try_get(store, latest_key(bundle))
    if raw is None:
        return None
    x = json.loads(raw)
    return x["key"], _utc(datetime.fromisoformat(x["as_of"]))


def due(store: RawStore, bundle: str, now: datetime) -> bool:
    got = latest(store, bundle)
    return got is None or now - got[1] >= timedelta(days=REFIT_DAYS)


# ------------------------------------------------------------------------------ pitch


def last_lineups(lineups: pl.DataFrame) -> dict[tuple[int, str], list[tuple[datetime, int]]]:
    """(team, formation) -> [(kickoff, match_id)] in kickoff order."""
    fm = (lineups.group_by("match_id", "team_id")
          .agg(pl.col("kickoff", "formation").first()).sort("kickoff", "match_id"))  # fmt: skip
    out: dict[tuple[int, str], list[tuple[datetime, int]]] = {}
    for mid, tid, k, f in fm.select("match_id", "team_id", "kickoff", "formation").iter_rows():
        out.setdefault((int(tid), str(f)), []).append((_utc(k), int(mid)))
    return out


def pitch(lineups: pl.DataFrame, index: dict[tuple[int, str], list[tuple[datetime, int]]],
          team: int, formation: str, as_of: datetime) -> dict[str, Any] | None:  # fmt: skip
    """The team's last lineup in `formation` final by `as_of` (kickoff <= as_of -
    RESULT_LAG, as P3-g's history): its match, kickoff, whether its league-season passes the
    grid test, and per starter the slot code (or line, if not) with grid row and column."""
    cut = as_of - RESULT_LAG
    got = [x for x in index.get((team, formation), []) if x[0] <= cut]
    if not got:
        return None
    kick, mid = got[-1]
    rows = lineups.filter((pl.col("match_id") == mid) & (pl.col("team_id") == team))
    positional = bool(rows["positional"][0])
    slots = [
        {"code": (slot if positional else line), "player_id": int(pid), "row": int(r),
         "col": int(c)}
        for pid, slot, line, r, c in rows.sort("row", "col")
        .select("player_id", "slot", "line", "row", "col").iter_rows()
    ]  # fmt: skip
    return {"match_id": mid, "kickoff": kick, "positional": positional, "slots": slots}


# ----------------------------------------------------------------- squad rules (panel)

RULES_PREFIX = "serving/squad_rules"
LIMIT_SHARE = 0.05  # a bench size named in at least this share of team-matches counts
LIMIT_MIN_N = 50  # fewer team-matches this season: last season's limit


def rules_key(generated_at: datetime) -> str:
    """One rules file per formation run, found from any of its rows' generated_at."""
    return f"{RULES_PREFIX}/{_utc(generated_at).astimezone(UTC):%Y%m%dT%H%M%S%f}.json"


def bench_limits(appearances: pl.DataFrame, season: int,
                 cutoff: datetime) -> dict[str, dict[str, int]]:  # fmt: skip
    """Per league: the largest bench size named in at least LIMIT_SHARE of its team-matches
    with 11 starters in `season`, final by `cutoff` (kickoff <= cutoff - RESULT_LAG); last
    season's if fewer than LIMIT_MIN_N. {league: {limit, season, n}}."""
    cut = _utc(cutoff).astimezone(UTC) - RESULT_LAG
    final = appearances.filter(pl.col("kickoff") <= cut)
    tm = final.group_by("league_ref", "season", "match_id", "team_id").agg(
        xi=pl.col("started").sum(), bench=(~pl.col("started")).sum()
    ).filter(pl.col("xi") == 11)  # fmt: skip
    out: dict[str, dict[str, int]] = {}
    for lg in sorted(tm["league_ref"].unique().to_list()):
        for yr in (season, season - 1):
            b = tm.filter((pl.col("league_ref") == lg) & (pl.col("season") == yr))["bench"]
            if b.len() < LIMIT_MIN_N:
                continue
            counts = b.value_counts()
            ok = counts.filter(pl.col("count") >= LIMIT_SHARE * b.len())["bench"]
            out[str(lg)] = {"limit": int(ok.max()), "season": yr, "n": b.len()}  # type: ignore[arg-type]
            break
    return out


def team_benches(appearances: pl.DataFrame, cutoff: datetime, last: int = 5) -> dict[str, float]:
    """Per team: the mean size of its last `last` named benches final by `cutoff` (P3-b's
    expected bench size, `features.selection`)."""
    cut = _utc(cutoff).astimezone(UTC) - RESULT_LAG
    tm = (appearances.filter(pl.col("kickoff") <= cut)
          .group_by("match_id", "team_id")
          .agg(pl.col("kickoff").first(), xi=pl.col("started").sum(),
               bench=(~pl.col("started")).sum())
          .filter(pl.col("xi") == 11).sort("kickoff"))  # fmt: skip
    got = tm.group_by("team_id").agg(pl.col("bench").tail(last).mean())
    return {str(t): round(float(b), 3) for t, b in got.iter_rows()}


def rules(appearances: pl.DataFrame, quality: pl.DataFrame, season: int,
          cutoff: datetime) -> dict[str, Any]:  # fmt: skip
    """The panel's squad rules as of `cutoff`: bench limits per league, each team's
    expected bench size, and the league-seasons whose grid passes the positional test
    (P3-h's pitch, and announced lineups)."""
    pos = quality.filter(pl.col("positional")).select("league_ref", "season").rows()
    return {"cutoff": _utc(cutoff).isoformat(), "bench": bench_limits(appearances, season, cutoff),
            "team_bench": team_benches(appearances, cutoff),
            "positional": sorted([str(a), int(b)] for a, b in pos)}  # fmt: skip


# ------------------------------------------------------------------------- projection


def distribution(post: dict[str, Arr], rows: pl.DataFrame, vocab: list[str]) -> Arr:
    """(n, k) posterior-mean formation probabilities (P3-c's dev path)."""
    from adatta.model import formation as mf

    return np.asarray(mf.probs(post, rows, len(vocab)).mean(axis=0), dtype=np.float64)


def run(session: Session, store: RawStore, bundles: dict[str, list[int]],
        now: datetime) -> dict[str, Any]:  # fmt: skip
    """Project the next 7 days of every bundle with a passing serving refit into
    `formation_projection`; returns counts (no match-level data)."""
    from adatta.eval import formation as ef
    from adatta.eval import positions as ps
    from adatta.features import formation as fe
    from adatta.features import lineups as lu
    from adatta.sim import lineups as sl

    report: dict[str, Any] = {"fixtures": 0, "rows": 0, "no_refit": [], "no_last": 0,
                              "positional": 0}  # fmt: skip
    leagues = [lg for ls in bundles.values() for lg in ls]
    tg = sl.upcoming(session, leagues, now)
    if tg.is_empty():
        return report
    season = max(int(x) for x in tg["season"].to_list())
    raw = ef.load_raw(session, store, [str(x) for x in leagues], list(bundles), season,
                      live=True)  # fmt: skip
    session.rollback()  # end the read transaction before the build (Neon's idle timeout)
    lineups, formations, quality = lu.build(raw.appearances)
    cutoff = _utc(tg["as_of"].min())  # type: ignore[arg-type]
    rl = rules(raw.appearances, quality, season, cutoff)
    store.put(rules_key(now), json.dumps(rl).encode())
    report["bench_limits"] = {lg: x["limit"] for lg, x in rl["bench"].items()}
    r_in = raw.inputs
    inp = fe.Inputs(formations.select("match_id", "team_id", "kickoff", "formation"),
                    r_in.coaches, r_in.context, r_in.tier,
                    tg.select("match_id", "league_ref", "season", "kickoff", "home_team_id",
                              "away_team_id", "as_of"))  # fmt: skip
    _, fmeta, _ = ef.load_table(store, DEV_TABLE)
    vocab = list(fmeta["vocab"])
    table = fe.build(inp, vocab, unplayed=True).join(tg.select("match_id", "as_of"),
                                                     on="match_id")  # fmt: skip
    index = last_lineups(lineups)
    out: list[models.FormationProjection] = []
    for b, ls in bundles.items():
        block = table.filter(pl.col("league_ref").is_in([str(x) for x in ls]))
        if block.is_empty():
            continue
        got = latest(store, b)
        if got is None:
            report["no_refit"].append(b)
            continue
        key, refit_as_of = got
        post, r_vocab, _ = decode(store.get(key))
        if r_vocab != vocab:
            raise ValueError(f"{b}: the refit's vocabulary is not P3-c's")
        p = distribution(post, block, vocab)
        for i, r in enumerate(block.iter_rows(named=True)):
            top = vocab[int(np.argmax(p[i]))]
            f_pitch = ps.projected_formation(top, p[i].tolist(), vocab)
            pt = pitch(lineups, index, int(r["team_id"]), f_pitch, _utc(r["as_of"]))
            report["no_last"] += pt is None
            report["positional"] += bool(pt and pt["positional"])
            out.append(models.FormationProjection(
                match_id=r["match_id"], team_id=r["team_id"], generated_at=now,
                as_of=r["as_of"], refit_key=key, refit_as_of=refit_as_of,
                probs={f: round(float(x), 6) for f, x in zip(vocab, p[i], strict=True)},
                pitch_formation=f_pitch,
                last_match_id=pt["match_id"] if pt else None,
                last_kickoff=pt["kickoff"] if pt else None,
                positional=bool(pt and pt["positional"]),
                slots=pt["slots"] if pt else None,
            ))  # fmt: skip
    session.add_all(out)
    session.flush()
    report["fixtures"] = len({x.match_id for x in out})
    report["rows"] = len(out)
    return report
