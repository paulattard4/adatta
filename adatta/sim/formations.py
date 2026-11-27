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
         "col": int(c), "line": line}
        for pid, slot, line, r, c in rows.sort("row", "col")
        .select("player_id", "slot", "line", "row", "col").iter_rows()
    ]  # fmt: skip
    return {"match_id": mid, "kickoff": kick, "positional": positional, "slots": slots}


Pools = dict[int, set[str]]


def coded_starts(lineups: pl.DataFrame, cutoff: datetime) -> tuple[Pools, Pools]:
    """Per player, the slot codes (positional league-seasons) and lines he has started in,
    final by `cutoff` (kickoff <= cutoff - RESULT_LAG): P3-h2's replacement pools."""
    cut = _utc(cutoff).astimezone(UTC) - RESULT_LAG
    s = lineups.filter(pl.col("kickoff") <= cut)
    slots: dict[int, set[str]] = {}
    lines: dict[int, set[str]] = {}
    for pid, slot, line, pos in s.select("player_id", "slot", "line", "positional").iter_rows():
        if pos and slot is not None:
            slots.setdefault(int(pid), set()).add(str(slot))
        if line is not None:
            lines.setdefault(int(pid), set()).add(str(line))
    return slots, lines


def replace_unavailable(slots: list[dict[str, Any]], squad: dict[int, tuple[float, str | None]],
                        coded: dict[int, set[str]], lines: dict[int, set[str]],
                        positional: bool) -> tuple[list[dict[str, Any]], int]:  # fmt: skip
    """P3-h2 (D64): the pitch with each player the feed lists as out ("missing") replaced by
    the available squad player with the highest P3-b start probability who has started in
    the same slot code (positional) or line, else the same line; with none, the slot keeps
    its player, flagged. `squad`: player -> (P3-b p_start, feed status). Each player is used
    once. Nothing is fitted. Returns the slots and the number of replacements."""
    used = {int(x["player_id"]) for x in slots}
    out, n = [], 0
    for x in slots:
        pid = int(x["player_id"])
        st = squad.get(pid, (0.0, None))[1]
        if st != "missing":
            out.append({**x, "status": st} if st else dict(x))
            continue
        pool = {q: v for q, v in squad.items() if q not in used and v[1] != "missing"}
        pools = ([(x["code"], coded)] if positional else []) + [(x["line"], lines)]
        pick = None
        for want, have in pools:
            cands = [q for q in pool if want in have.get(q, set())]
            if cands:
                pick = max(cands, key=lambda q: (pool[q][0], -q))
                break
        if pick is None:
            out.append({**x, "status": st, "unfilled": True})
            continue
        used.add(pick)
        n += 1
        out.append({**x, "player_id": pick, "replaced": pid, "status": pool[pick][1]})
    return out, n


def _squads(session: Session, match_ids: list[int], now: datetime
            ) -> dict[tuple[int, int], dict[int, tuple[float, str | None]]]:  # fmt: skip
    """P3-b's latest stored projection published by `now` per (match, team): player ->
    (p_start, the feed status it carried)."""
    from sqlalchemy import func, select

    LP = models.LineupProjection
    if not match_ids:
        return {}
    last = (select(LP.match_id, LP.team_id, func.max(LP.generated_at).label("g"))
            .where(LP.match_id.in_(match_ids), LP.generated_at <= now)
            .group_by(LP.match_id, LP.team_id).subquery())  # fmt: skip
    rows = session.execute(
        select(LP.match_id, LP.team_id, LP.player_id, LP.p_start, LP.status)
        .join(last, (last.c.match_id == LP.match_id) & (last.c.team_id == LP.team_id)
              & (last.c.g == LP.generated_at))
    ).all()  # fmt: skip
    out: dict[tuple[int, int], dict[int, tuple[float, str | None]]] = {}
    for mid, tid, pid, p, st in rows:
        out.setdefault((int(mid), int(tid)), {})[int(pid)] = (float(p), st)
    return out


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


USUAL_STARTS = 10  # a player's usual position: his last 10 starts final by the cutoff
USUAL_WITHIN = timedelta(days=400)  # players with a start this recent (the file stays small)


def usual_positions(lineups: pl.DataFrame, cutoff: datetime) -> dict[str, list[str | None]]:
    """Per player with a start within USUAL_WITHIN: [slot code, line] played most often in
    his last USUAL_STARTS starts final by `cutoff` (ties to the most recent). The slot
    counts only starts in positional league-seasons; None if he has none there."""
    cut = _utc(cutoff).astimezone(UTC) - RESULT_LAG
    s = (lineups.filter(pl.col("kickoff") <= cut)
         .sort("kickoff", descending=True)
         .group_by("player_id", maintain_order=True).head(USUAL_STARTS))  # fmt: skip
    recent = s.group_by("player_id").agg(pl.col("kickoff").max()).filter(
        pl.col("kickoff") >= cut - USUAL_WITHIN)["player_id"]  # fmt: skip
    s = s.filter(pl.col("player_id").is_in(recent.implode()))

    def mode(df: pl.DataFrame, col: str) -> dict[int, str]:
        g = df.group_by("player_id", col).agg(n=pl.len(), last=pl.col("kickoff").max())
        g = g.sort(["player_id", "n", "last"], descending=[False, True, True])
        return {int(p): str(v) for p, v in
                g.group_by("player_id", maintain_order=True).first()
                .select("player_id", col).iter_rows()}  # fmt: skip

    slot = mode(s.filter(pl.col("positional")), "slot")
    line = mode(s, "line")
    return {str(p): [slot.get(p), ln] for p, ln in sorted(line.items())}


def season_starts(appearances: pl.DataFrame, season: int,
                  cutoff: datetime) -> dict[str, dict[str, int]]:  # fmt: skip
    """Per team: each player's starts for it in `season`, final by `cutoff` (the line fill
    of the projected-XI pitch ranks by these)."""
    cut = _utc(cutoff).astimezone(UTC) - RESULT_LAG
    g = (appearances.filter((pl.col("season") == season) & pl.col("started")
                            & (pl.col("kickoff") <= cut))
         .group_by("team_id", "player_id").agg(n=pl.len()))  # fmt: skip
    out: dict[str, dict[str, int]] = {}
    for t, pid, n in g.sort("team_id", "player_id").iter_rows():
        out.setdefault(str(t), {})[str(pid)] = int(n)
    return out


def rules(appearances: pl.DataFrame, quality: pl.DataFrame, season: int,
          cutoff: datetime, lineups: pl.DataFrame | None = None) -> dict[str, Any]:  # fmt: skip
    """The panel's squad rules as of `cutoff`: bench limits per league, each team's
    expected bench size, the league-seasons whose grid passes the positional test (P3-h's
    pitch, and announced lineups) and each recent starter's usual slot and line."""
    pos = quality.filter(pl.col("positional")).select("league_ref", "season").rows()
    return {"cutoff": _utc(cutoff).isoformat(), "bench": bench_limits(appearances, season, cutoff),
            "team_bench": team_benches(appearances, cutoff),
            "positional": sorted([str(a), int(b)] for a, b in pos),
            "usual": usual_positions(lineups, cutoff) if lineups is not None else {},
            "starts": season_starts(appearances, season, cutoff)}  # fmt: skip


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
    rl = rules(raw.appearances, quality, season, cutoff, lineups)
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
    coded, lines = coded_starts(lineups, cutoff)  # P3-h2 (D64) pools, final by the cutoff
    squads = _squads(session, [int(x) for x in tg["match_id"].to_list()], now)
    report["replaced"] = 0
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
            sq = squads.get((int(r["match_id"]), int(r["team_id"])), {})
            rs, n_rep = (replace_unavailable(pt["slots"], sq, coded, lines,
                                             bool(pt["positional"]))
                         if pt else (None, None))  # fmt: skip
            report["replaced"] += n_rep or 0
            out.append(models.FormationProjection(
                match_id=r["match_id"], team_id=r["team_id"], generated_at=now,
                as_of=r["as_of"], refit_key=key, refit_as_of=refit_as_of,
                probs={f: round(float(x), 6) for f, x in zip(vocab, p[i], strict=True)},
                pitch_formation=f_pitch,
                last_match_id=pt["match_id"] if pt else None,
                last_kickoff=pt["kickoff"] if pt else None,
                positional=bool(pt and pt["positional"]),
                slots=pt["slots"] if pt else None,
                slots_replaced=rs, replacements=n_rep,
            ))  # fmt: skip
    session.add_all(out)
    session.flush()
    report["fixtures"] = len({x.match_id for x in out})
    report["rows"] = len(out)
    return report
