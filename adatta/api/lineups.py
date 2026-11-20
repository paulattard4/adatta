"""The squad panel's lineup sections, the announced lineup on the pitch, and how the stored
projection did (docs/DASHBOARD_TASKS.md, "Lineups, bench, shape and how the projection
did"). Display and scoring only: every probability is P3-b's or P3-c's as stored."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import SOURCE_ID
from adatta.adapters.storage import RawStore, try_get
from adatta.api import schemas as sc
from adatta.db import models
from adatta.features import lineups as flu
from adatta.features.selection import HORIZON
from adatta.sim import formations as sf

LINE_ORDER = {"G": 0, "D": 1, "M": 2, "F": 3, "U": 4}
REG_POS = {"Goalkeeper": "G", "Defender": "D", "Midfielder": "M", "Attacker": "F"}
OUT = "missing"  # the feed's status that takes a player out of the projected XI and bench


@dataclass(frozen=True)
class Pooled:
    """One pool player of a stored P3-b projection."""

    player_id: int
    name: str
    pos: str
    p_start: float
    p_bench: float
    emin: float
    status_as_of: str | None  # the feed's status at the projection's as-of time (stored)


def likely_xi(xs: list[Pooled]) -> list[Pooled]:
    """The keeper most likely to start plus the 10 outfield players most likely to start
    (11 by P(start) if the pool has no keeper). `xs` in P(start) order."""
    by_start = sorted(xs, key=lambda p: -p.p_start)  # stable: the stored order breaks ties
    gk = next((p for p in by_start if p.pos == "G"), None)
    outfield = [p for p in by_start if p is not gk and p.pos != "G"][: 10 if gk else 11]
    return ([gk] if gk else []) + outfield


def bench_size(xs: list[Pooled], limit: int | None, expected: float | None) -> int:
    """The team's expected bench size (the mean of its last 5 named benches, P3-b's), rounded,
    never above the league limit. Without it, P3-b's bench places among the pool."""
    n = round(expected if expected is not None else sum(p.p_bench for p in xs))
    return min(n, limit) if limit is not None else n


def sections(xs: list[Pooled], limit: int | None,
             expected: float | None = None) -> dict[str, list[Pooled]]:  # fmt: skip
    """xi / bench / out: players listed as missing at the as-of time are never in the XI
    or bench (rule 2); the bench is the rest by P(start) + P(bench), with a keeper when one
    is available; every other pool player is out of the matchday squad."""
    ok = [p for p in xs if p.status_as_of != OUT]
    xi = likely_xi(ok)
    chosen = {p.player_id for p in xi}
    rest = sorted((p for p in ok if p.player_id not in chosen),
                  key=lambda p: -(p.p_start + p.p_bench))  # fmt: skip
    n = bench_size(xs, limit, expected)
    bench = rest[:n]
    if n > 0 and not any(p.pos == "G" for p in bench):
        gk = next((p for p in rest[n:] if p.pos == "G"), None)
        if gk is not None:
            bench = [*rest[: n - 1], gk]
    named = chosen | {p.player_id for p in bench}
    out = sorted((p for p in xs if p.player_id not in named),
                 key=lambda p: -(p.p_start + p.p_bench))  # fmt: skip
    xi = sorted(xi, key=lambda p: (LINE_ORDER.get(p.pos, 4), -p.p_start))
    return {"xi": xi, "bench": bench, "out": out}


# ------------------------------------------------------------------------- stored rows


def pooled(session: Session, match_id: int, team_id: int | None,
           generated_at: datetime) -> list[tuple[int, Pooled, datetime]]:  # fmt: skip
    """(team, player, as_of) of one stored lineup projection run, in P(start) order."""
    LP, PL = models.LineupProjection, models.Player
    where = [LP.match_id == match_id, LP.generated_at == generated_at]
    if team_id is not None:
        where.append(LP.team_id == team_id)
    return [(r.team_id, Pooled(r.player_id, n or "unnamed", r.pos, r.p_start, r.p_bench, r.emin,
                               r.status), r.as_of)
            for r, n in session.execute(
                select(LP, PL.name).join(PL, PL.player_id == LP.player_id).where(*where)
                .order_by(LP.p_start.desc(), LP.emin.desc()))]  # fmt: skip


def pooled_many(session: Session, runs: dict[int, datetime],
                ) -> dict[tuple[int, int], list[Pooled]]:  # fmt: skip
    """(match, team) -> pool players of each match's given lineup projection run, in
    P(start) order."""
    LP, PL = models.LineupProjection, models.Player
    out: dict[tuple[int, int], list[Pooled]] = {}
    ids = list(runs)
    for i in range(0, len(ids), 200):
        for r, n in session.execute(
            select(LP, PL.name).join(PL, PL.player_id == LP.player_id)
            .where(LP.match_id.in_(ids[i : i + 200]))
            .order_by(LP.match_id, LP.p_start.desc(), LP.emin.desc())
        ):  # fmt: skip
            if _utc(r.generated_at) != _utc(runs[r.match_id]):
                continue
            out.setdefault((r.match_id, r.team_id), []).append(
                Pooled(r.player_id, n or "unnamed", r.pos, r.p_start, r.p_bench, r.emin, r.status)
            )
    return out


def registered(session: Session, team_id: int, as_of: datetime) -> list[tuple[int, str, str]]:
    """(player, name, line) of the team's open registrations at as_of (the squad list)."""
    R, PL = models.PlayerRegistration, models.Player
    rows = session.execute(
        select(R.player_id, PL.name, R.position).join(PL, PL.player_id == R.player_id)
        .where(R.team_id == team_id, R.valid_from <= as_of,
               (R.valid_to.is_(None)) | (R.valid_to > as_of),
               (R.superseded_at.is_(None)) | (R.superseded_at > as_of))
    ).all()  # fmt: skip
    seen: dict[int, tuple[int, str, str]] = {}
    for pid, name, pos in rows:
        seen[pid] = (pid, name or "unnamed", REG_POS.get(pos or "", "U"))
    return sorted(seen.values(), key=lambda x: (LINE_ORDER.get(x[2], 4), x[1]))


_RULES: dict[str, dict[str, Any] | None] = {}


def rules(store: RawStore, formation_generated_at: datetime | None) -> dict[str, Any] | None:
    """The squad rules written with a formation run (bench limits, positional league-
    seasons), cached per process once found (written once, never changed)."""
    if formation_generated_at is None:
        return None
    key = sf.rules_key(formation_generated_at)
    if _RULES.get(key) is None:  # a missing file is looked up again (it may be written later)
        raw = try_get(store, key)
        _RULES[key] = json.loads(raw) if raw is not None else None
    return _RULES[key]


def bench_limit(rl: dict[str, Any] | None, league_ref: str) -> int | None:
    if rl is None or league_ref not in rl["bench"]:
        return None
    return int(rl["bench"][league_ref]["limit"])


def team_bench(rl: dict[str, Any] | None, team_id: int) -> float | None:
    if rl is None or str(team_id) not in rl.get("team_bench", {}):
        return None
    return float(rl["team_bench"][str(team_id)])


def is_positional(rl: dict[str, Any] | None, league_ref: str, season: int) -> bool:
    return rl is not None and [league_ref, season] in rl["positional"]


# ----------------------------------------------------------------------- announced pitch


def announced_pitch(starters: list[dict[str, Any]], positional: bool,
                    ) -> tuple[str, list[tuple[dict[str, Any], str, int, int]]] | None:  # fmt: skip
    """An announced XI on its grid: (formation, [(starter, code, row, col)]), codes as the
    lineup table's (slot codes if the league-season is positional, else lines). None if
    the grid is not a complete 11 with one keeper."""
    cells = []
    for x in starters:
        g = x.get("grid")
        if not g or ":" not in str(g):
            return None
        r, c = (int(v) for v in str(g).split(":"))
        cells.append((x, r, c))
    if len(cells) != 11:
        return None
    rows: dict[int, list[int]] = {}
    for _, r, c in cells:
        rows.setdefault(r, []).append(c)
    if sorted(rows) != list(range(1, len(rows) + 1)) or len(rows[1]) != 1:
        return None
    if any(sorted(v) != list(range(1, len(v) + 1)) for v in rows.values()):
        return None
    counts = [len(rows[r]) for r in sorted(rows)]
    codes = flu.formation_codes(counts)
    last = len(counts)
    out = []
    for x, r, c in sorted(cells, key=lambda t: (t[1], t[2])):
        code = codes[r - 1][c - 1] if positional else flu.row_line(r, last)
        out.append((x, code, r, c))
    return flu.formation_string(counts), out


# --------------------------------------------------------------------------- scoring


FormRows = dict[tuple[int, int], models.FormationProjection]


def scoring_runs(session: Session, kickoffs: dict[int, datetime],
                 ) -> tuple[dict[int, datetime], FormRows]:  # fmt: skip
    """Per match, the stored projections as of kickoff - 24 h (the latest generated, as
    P3-f's scoring): the lineup run's generated_at, and the formation row per team."""
    LP, FP = models.LineupProjection, models.FormationProjection
    ids = list(kickoffs)
    lineup: dict[int, datetime] = {}
    form: FormRows = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        for mid, gen, as_of in session.execute(
            select(LP.match_id, func.max(LP.generated_at), LP.as_of)
            .where(LP.match_id.in_(chunk)).group_by(LP.match_id, LP.as_of)
        ):  # fmt: skip
            if _utc(as_of) == kickoffs[mid] - HORIZON and _utc(gen) < kickoffs[mid]:
                lineup[mid] = gen
        for r in session.scalars(select(FP).where(FP.match_id.in_(chunk))):
            if _utc(r.as_of) != kickoffs[r.match_id] - HORIZON:
                continue
            if _utc(r.generated_at) >= kickoffs[r.match_id]:
                continue
            k = (r.match_id, r.team_id)
            if k not in form or r.generated_at > form[k].generated_at:
                form[k] = r
    return lineup, form


def announced(session: Session, match_ids: list[int],
              ) -> dict[tuple[int, int], models.LineupSnapshot]:  # fmt: skip
    """The latest captured lineup per (match, team)."""
    L = models.LineupSnapshot
    out: dict[tuple[int, int], models.LineupSnapshot] = {}
    for i in range(0, len(match_ids), 500):
        for r in session.scalars(select(L).where(L.match_id.in_(match_ids[i : i + 500]))):
            k = (r.match_id, r.team_id)
            if k not in out or r.observed_at > out[k].observed_at:
                out[k] = r
    return out


def player_ids(session: Session, refs: set[str]) -> dict[str, int]:
    PL = models.Player
    out: dict[str, int] = {}
    xs = sorted(refs)
    for i in range(0, len(xs), 500):
        out.update({str(ref): pid for pid, ref in session.execute(
            select(PL.player_id, PL.source_ref)
            .where(PL.source_id == SOURCE_ID, PL.source_ref.in_(xs[i : i + 500])))})  # fmt: skip
    return out


def formation_played(snap: models.LineupSnapshot) -> str | None:
    got = announced_pitch(snap.starters, positional=False)
    return got[0] if got else None


def score(xs: list[Pooled], limit: int | None, expected: float | None,
          snap: models.LineupSnapshot, ids: dict[str, int],
          form: models.FormationProjection | None) -> sc.LineupScore:  # fmt: skip
    """How one side's stored projection did against the announced lineup."""
    start = {ids[str(x["player_ref"])] for x in snap.starters if str(x.get("player_ref")) in ids}
    subs = {ids[str(x["player_ref"])] for x in snap.substitutes
            if str(x.get("player_ref")) in ids}  # fmt: skip
    sec = sections(xs, limit, expected)
    bench = {p.player_id for p in sec["bench"]}
    f_played, f_p, f_top = formation_played(snap), None, None
    if form is not None and f_played is not None:
        probs = {k: float(v) for k, v in form.probs.items()}
        key = f_played if f_played in probs else "other"
        f_p = probs.get(key)
        f_top = max(probs, key=lambda k: probs[k]) == key
    return sc.LineupScore(
        xi_hits=len({p.player_id for p in sec["xi"]} & start),
        xi_hits_raw=len({p.player_id for p in likely_xi(xs)} & start),
        bench_n=len(bench), bench_hits=len(bench & subs),
        bench_in_squad=len(bench & (start | subs)),
        formation=f_played, formation_p=f_p, formation_top=f_top,
    )  # fmt: skip


def _utc(t: datetime) -> datetime:
    from adatta.sim.lineups import _utc as u

    return u(t)
