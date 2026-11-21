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
    """The league's bench limit (amendment 2: fill the bench where the squad allows).
    Without one, the team's expected bench size (the mean of its last 5 named benches),
    rounded; without that, P3-b's bench places among the pool."""
    if limit is not None:
        return limit
    return round(expected if expected is not None else sum(p.p_bench for p in xs))


def sections(xs: list[Pooled], limit: int | None, expected: float | None = None,
             xi: list[Pooled] | None = None) -> dict[str, list[Pooled]]:  # fmt: skip
    """xi / bench / out: players listed as missing at the as-of time are never in the XI
    or bench (rule 2); the bench is the rest by P(start) + P(bench), with a keeper when one
    is available; every other pool player is out of the matchday squad. `xi` replaces the
    likely XI (the XI fitted to the projected formation, `fit`)."""
    ok = [p for p in xs if p.status_as_of != OUT]
    xi = likely_xi(ok) if xi is None else xi
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
    out = [p for p in xs if p.player_id not in named]

    def by_line(ps: list[Pooled]) -> list[Pooled]:  # by position, then the most likely
        return sorted(ps, key=lambda p: (LINE_ORDER.get(p.pos, 4), -(p.p_start + p.p_bench)))

    xi = sorted(xi, key=lambda p: (LINE_ORDER.get(p.pos, 4), -p.p_start))
    return {"xi": xi, "bench": by_line(bench), "out": by_line(out)}


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


def usual(rl: dict[str, Any] | None, player_id: int) -> tuple[str | None, str | None]:
    """A player's usual (slot code, line) from the rules file; (None, None) if unknown."""
    got = (rl or {}).get("usual", {}).get(str(player_id))
    return (got[0], got[1]) if got else (None, None)


# ------------------------------------------------------------------ projected XI pitch


@dataclass(frozen=True)
class Placed:
    """One slot of the projected-XI pitch."""

    code: str  # slot code if positional, else the line
    row: int
    col: int
    player: Pooled | None  # None: the keeper slot when the XI has no keeper
    moved: bool  # replaces a player of the last lineup (or fills an empty grid)
    usual: str | None  # the player's usual slot (or line) when it differs from `code`


def grid_slots(formation: str, positional: bool) -> list[dict[str, Any]] | None:
    """An empty grid for a formation string ("4-2-3-1"), as the lineup table's codes."""
    try:
        counts = [1, *(int(x) for x in formation.split("-"))]
        codes = flu.formation_codes(counts)
    except ValueError:
        return None
    if sum(counts) != 11:
        return None
    return [{"code": codes[r][c] if positional else flu.row_line(r + 1, len(counts)),
             "row": r + 1, "col": c + 1, "player_id": None}
            for r, n in enumerate(counts) for c in range(n)]  # fmt: skip


NEIGHBOUR = {"D": ("M",), "M": ("D", "F"), "F": ("M",)}  # lines next to each other


def place(slots: list[dict[str, Any]], xi: list[Pooled], rl: dict[str, Any] | None,
          positional: bool, pool: list[Pooled] | None = None,
          team_id: int | None = None) -> list[Placed]:  # fmt: skip
    """The projected XI fitted to a grid (docs/DASHBOARD_TASKS.md, "Projected XI on the
    pitch", amendment 1). Players of the grid's lineup who are in the XI keep their slot;
    the rest of the XI, highest P(start) first, takes a vacated slot: (1) his usual slot
    code (positional only), (2) else a slot in his usual line. A slot still free goes to
    (3) the player of its line in `pool` outside the XI with the most starts for the team
    this season, then the most likely (amendment 2; never one listed out), (4) else an
    unplaced XI player of a neighbouring line, then a pool player of one (same order),
    (5) else any unplaced XI player. XI players left without a slot are not placed (the
    caller moves them to the bench). The keeper slot takes only a keeper; it stays empty
    if there is none. Slots in grid order."""
    last = max(int(s["row"]) for s in slots)
    line_of = {(int(s["row"]), int(s["col"])): flu.row_line(int(s["row"]), last) for s in slots}
    by_id = {p.player_id: p for p in xi}
    got: dict[tuple[int, int], tuple[Pooled, bool]] = {}
    for s in slots:
        pid = s.get("player_id")
        if pid is not None and int(pid) in by_id:
            got[(int(s["row"]), int(s["col"]))] = (by_id[int(pid)], False)
    kept = {p.player_id for p, _ in got.values()}
    incoming = sorted((p for p in xi if p.player_id not in kept), key=lambda p: -p.p_start)
    free = sorted((int(s["row"]), int(s["col"])) for s in slots
                  if (int(s["row"]), int(s["col"])) not in got)  # fmt: skip
    code_at = {(int(s["row"]), int(s["col"])): str(s["code"]) for s in slots}
    gk = (1, 1)

    def line(p: Pooled) -> str:
        return "G" if p.pos == "G" else (usual(rl, p.player_id)[1] or p.pos)

    def take(p: Pooled, cell: tuple[int, int]) -> None:
        got[cell] = (p, True)
        free.remove(cell)

    starts = (rl or {}).get("starts", {}).get(str(team_id), {})
    avail = sorted((p for p in pool or [] if p.player_id not in by_id and p.status_as_of != OUT),
                   key=lambda p: (-starts.get(str(p.player_id), 0), -p.p_start))  # fmt: skip
    waiting: list[Pooled] = []
    for p in incoming:  # keepers only in goal, outfield players never there
        if line(p) == "G":
            if gk in free:
                take(p, gk)
        else:
            waiting.append(p)
    if gk in free:
        cand = next((p for p in avail if line(p) == "G"), None)
        if cand is not None:
            take(cand, gk)
            avail.remove(cand)
    avail = [p for p in avail if line(p) != "G"]
    rest: list[Pooled] = []
    for p in waiting:  # (1) his usual slot code
        slot = usual(rl, p.player_id)[0] if positional else None
        cell = next((c for c in free if c != gk and code_at[c] == slot), None)
        if cell is not None:
            take(p, cell)
        else:
            rest.append(p)
    left: list[Pooled] = []
    for p in rest:  # (2) a slot in his usual line
        cell = next((c for c in free if c != gk and line_of[c] == line(p)), None)
        if cell is not None:
            take(p, cell)
        else:
            left.append(p)
    for c in [c for c in free if c != gk]:  # (3) the pool's most likely of the slot's line
        cand = next((p for p in avail if line(p) == line_of[c]), None)
        if cand is not None:
            take(cand, c)
            avail.remove(cand)
    for c in [c for c in free if c != gk]:  # (4) a neighbouring line: the XI's, then the pool's
        near = NEIGHBOUR.get(line_of[c], ())
        cand = next((p for p in left if line(p) in near), None)
        src = left
        if cand is None:
            cand, src = next((p for p in avail if line(p) in near), None), avail
        if cand is not None:
            take(cand, c)
            src.remove(cand)
    for c in [c for c in free if c != gk]:  # (5) any unplaced XI player
        if left:
            take(left.pop(0), c)
    out = []
    for s in sorted(slots, key=lambda s: (int(s["row"]), int(s["col"]))):
        cell = (int(s["row"]), int(s["col"]))
        hit = got.get(cell)
        who, moved = (hit[0], hit[1]) if hit else (None, False)
        u = None
        if who is not None:
            u_slot, u_line = usual(rl, who.player_id)
            mine = u_slot if positional else (u_line or who.pos)
            u = mine if mine is not None and mine != code_at[cell] else None
        out.append(Placed(code_at[cell], cell[0], cell[1], who, moved, u))
    return out


def fit(xs: list[Pooled], limit: int | None, expected: float | None,
        slots: list[dict[str, Any]] | None, rl: dict[str, Any] | None,
        positional: bool, team_id: int | None = None,
        ) -> tuple[dict[str, list[Pooled]], list[Placed] | None]:  # fmt: skip
    """The sections with the XI fitted to the projected formation's grid (`place`, with
    the whole pool as candidates): the XI is the placed players, and a likely-XI player
    left without a slot goes back among the rest. Without a grid, `sections` as is."""
    sec = sections(xs, limit, expected)
    if not slots:
        return sec, None
    placed = place(slots, sec["xi"], rl, positional, pool=xs, team_id=team_id)
    xi = [x.player for x in placed if x.player is not None]
    return sections(xs, limit, expected, xi=xi), placed


def slot_accuracy(placed: list[tuple[str, int | None]], announced: dict[int, str]) -> float | None:
    """Share of the shown (code, player) slots whose player started in that code."""
    xs = [(c, p) for c, p in placed if p is not None]
    if not xs:
        return None
    return sum(announced.get(p) == c for c, p in xs) / len(xs)


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


def pitch_scores(placed: list[Placed] | None, snap: models.LineupSnapshot,
                 ids: dict[str, int], form: models.FormationProjection | None,
                 positional: bool) -> tuple[float | None, float | None]:  # fmt: skip
    """Slot accuracy of the projected-XI pitch and of the last lineup unchanged, when both
    grids are positional and the announced formation is the pitch's (else None, None)."""
    if form is None or not form.positional or not form.slots or not positional or not placed:
        return None, None
    got = announced_pitch(snap.starters, positional=True)
    if got is None or got[0] != form.pitch_formation:
        return None, None
    ann = {ids[str(x["player_ref"])]: code for x, code, _, _ in got[1]
           if str(x.get("player_ref")) in ids}  # fmt: skip
    shown = [(x.code, x.player.player_id if x.player else None) for x in placed]
    last = [(str(s["code"]), int(s["player_id"])) for s in form.slots]
    return slot_accuracy(shown, ann), slot_accuracy(last, ann)  # type: ignore[arg-type]


def score(xs: list[Pooled], limit: int | None, expected: float | None,
          snap: models.LineupSnapshot, ids: dict[str, int],
          form: models.FormationProjection | None, rl: dict[str, Any] | None = None,
          positional: bool = False) -> sc.LineupScore:  # fmt: skip
    """How one side's stored projection did against the announced lineup."""
    start = {ids[str(x["player_ref"])] for x in snap.starters if str(x.get("player_ref")) in ids}
    subs = {ids[str(x["player_ref"])] for x in snap.substitutes
            if str(x.get("player_ref")) in ids}  # fmt: skip
    grid, g_pos = None, positional
    if form is not None:  # the pitch the panel showed: the last lineup, else an empty grid
        g_pos = form.positional if form.slots else positional
        grid = form.slots or grid_slots(form.pitch_formation, g_pos)
    sec, placed = fit(xs, limit, expected, grid, rl, g_pos, snap.team_id)
    bench = {p.player_id for p in sec["bench"]}
    f_played, f_p, f_top = formation_played(snap), None, None
    if form is not None and f_played is not None:
        probs = {k: float(v) for k, v in form.probs.items()}
        key = f_played if f_played in probs else "other"
        f_p = probs.get(key)
        f_top = max(probs, key=lambda k: probs[k]) == key
    acc = pitch_scores(placed, snap, ids, form, positional)
    return sc.LineupScore(
        xi_hits=len({p.player_id for p in sec["xi"]} & start),
        xi_hits_raw=len({p.player_id for p in likely_xi(xs)} & start),
        bench_n=len(bench), bench_hits=len(bench & subs),
        bench_in_squad=len(bench & (start | subs)),
        formation=f_played, formation_p=f_p, formation_top=f_top,
        slot_acc=acc[0], slot_acc_last=acc[1],
    )  # fmt: skip


def _utc(t: datetime) -> datetime:
    from adatta.sim.lineups import _utc as u

    return u(t)
