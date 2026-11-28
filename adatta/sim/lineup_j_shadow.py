"""P3-j step 3: J's live shadow forecasts beside P's (docs/PHASE3_TASKS.md P3-j (d), D65).

Numpy and polars only, so it runs in the capture tick. For every regular-season fixture of
the five bundles:
  lk   on the first tick that sees both announced XIs before kickoff (`lineup_snapshot`);
       at the last call (LAST_CALL before kickoff) a side still without one uses P3-b's
       projected XI, flagged (as step 2's (a))
  24   inside the T-24h milestone window, from P3-b's projected XIs (`lineup_projection`,
       the latest run at or before then: the XI_N likeliest starters, ties lower id)
J is projected from its latest passing shadow refit (`adatta.model.lineup_j_live`) with the
lineup offset beta . x on each side's log rate, and P from its passing live refit of the
same as-of time with none (comparator (a)): the two rows differ only by the offset. The
signals come from the daily basis (`build_basis`, after the appearances rebuild) and the
XI, with step 2's warm-up scales and line means; a signal without a baseline is 0 (rule 8).
Rows go to `lineup_j_shadow`, append-only, once per match x horizon x model. Not shown
(the API never reads the table) until the season-end verdict (`adatta.eval.lineup_j_live`).
No odds are read (principle 1).

    python -m adatta.sim.lineup_j_shadow basis     # daily, projected_lineups.yml
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import SOURCE_ID
from adatta.adapters.storage import RawStore, try_get
from adatta.capture import snapshots
from adatta.db import models
from adatta.features import lineup_signals as sig
from adatta.model import horizon as hz
from adatta.sim import serve

PREFIX = "serving/lineup_j"
BASIS_KEY = f"{PREFIX}/basis/latest"
BASIS_MAX_AGE = timedelta(days=2)  # an older basis is reported and no row is written
LAST_CALL = timedelta(minutes=20)  # lk with what is announced by then
COMPONENT, WINDOW, MODEL_NAME = "p3j", "shadow", "P3-j J"  # adatta.model.lineup_j_live's
BETA = "beta_signals"  # lineup_strength.BETA (that module needs the model group)
LINES = ("G", "D", "M", "F")
HORIZONS = ("lk", "24")


# ------------------------------------------------------------------------------- basis


def save_basis(store: RawStore, usual_: pl.DataFrame, players: pl.DataFrame,
               meta: dict[str, Any]) -> None:  # fmt: skip
    for name, frame in (("usual", usual_), ("players", players)):
        buf = io.BytesIO()
        frame.write_parquet(buf, compression="zstd")
        store.put(f"{PREFIX}/basis/{name}.parquet", buf.getvalue())
    store.put(BASIS_KEY, json.dumps(meta, sort_keys=True, default=str).encode())


def load_basis(store: RawStore) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, Any]] | None:
    raw = try_get(store, BASIS_KEY)
    if raw is None:
        return None
    meta = json.loads(raw)
    u = pl.read_parquet(io.BytesIO(store.get(f"{PREFIX}/basis/usual.parquet")))
    p = pl.read_parquet(io.BytesIO(store.get(f"{PREFIX}/basis/players.parquet")))
    return u, p, meta


def build_basis(app: pl.DataFrame, matches: pl.DataFrame, bundle_of: dict[int, str],
                now: datetime, meta2: dict[str, Any]) -> tuple[pl.DataFrame, pl.DataFrame,
                                                               dict[str, Any]]:  # fmt: skip
    """The daily basis as of `now`, with step 2's scales and line means (J's)."""
    u, p = sig.live_basis(app, matches, bundle_of, now)
    meta = {"as_of": now.isoformat(), "table": sig.J_TABLE,
            "scale": {k: float(v) for k, v in meta2["scale"].items()},
            "line_means": {str(k): float(v) for k, v in meta2["line_means"].items()},
            "teams": int(u["team_id"].n_unique()), "players": p.height}  # fmt: skip
    return u, p, meta


# ------------------------------------------------------------------------------ inputs


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def player_ids(session: Session, refs: set[str]) -> dict[str, int]:
    PL = models.Player
    out: dict[str, int] = {}
    xs = sorted(refs)
    for i in range(0, len(xs), 500):
        out.update({str(ref): pid for pid, ref in session.execute(
            select(PL.player_id, PL.source_ref)
            .where(PL.source_id == SOURCE_ID, PL.source_ref.in_(xs[i : i + 500])))})  # fmt: skip
    return out


def announced(session: Session, ids: list[int], now: datetime
              ) -> dict[tuple[int, int], models.LineupSnapshot]:  # fmt: skip
    """The lineup first seen per (match, team), observed by now."""
    L = models.LineupSnapshot
    out: dict[tuple[int, int], models.LineupSnapshot] = {}
    for r in session.scalars(select(L).where(L.match_id.in_(ids), L.observed_at <= now)):
        k = (r.match_id, r.team_id)
        if k not in out or _utc(r.observed_at) < _utc(out[k].observed_at):
            out[k] = r
    return out


def announced_xi(session: Session, snap: models.LineupSnapshot) -> pl.DataFrame | None:
    """(team_id, player_id, line) of an announced XI, None unless all 11 are known."""
    refs = {str(x["player_ref"]) for x in snap.starters if x.get("player_ref") is not None}
    ids = player_ids(session, refs)
    rows = [(snap.team_id, ids[str(x["player_ref"])],
             x.get("pos") if x.get("pos") in LINES else None)
            for x in snap.starters if str(x.get("player_ref")) in ids]  # fmt: skip
    if len({r[1] for r in rows}) != sig.XI_N:
        return None
    return pl.DataFrame(rows, schema={"team_id": pl.Int64, "player_id": pl.Int64,
                                      "line": pl.Utf8}, orient="row")  # fmt: skip


def projected_xi(session: Session, match_id: int, team_id: int, now: datetime
                 ) -> pl.DataFrame | None:  # fmt: skip
    """P3-b's XI_N likeliest starters from its latest run at or before now (ties: lower
    player id), with their position family; None with fewer than XI_N in the pool."""
    LP = models.LineupProjection
    rows = session.execute(
        select(LP.as_of, LP.player_id, LP.p_start, LP.pos)
        .where(LP.match_id == match_id, LP.team_id == team_id, LP.as_of <= now)
    ).all()  # fmt: skip
    if not rows:
        return None
    last = max(_utc(r[0]) for r in rows)
    pool = sorted(((-float(p), int(pid), pos) for a, pid, p, pos in rows if _utc(a) == last))
    if len(pool) < sig.XI_N:
        return None
    return pl.DataFrame([(team_id, pid, pos if pos in LINES else None)
                         for _, pid, pos in pool[: sig.XI_N]],
                        schema={"team_id": pl.Int64, "player_id": pl.Int64, "line": pl.Utf8},
                        orient="row")  # fmt: skip


Pair = tuple[models.ModelRun, models.ModelRun]


def pairs(session: Session, now: datetime, p_model: str) -> dict[str, Pair]:
    """Per bundle: J's latest passing shadow refit by now and P's passing live refit of the
    same as-of time (none when P has no passing refit then)."""
    MR = models.ModelRun
    j_rows = session.scalars(
        select(MR).where(MR.component == COMPONENT, MR.window == WINDOW,
                         MR.model_name == MODEL_NAME, MR.status == "ok", MR.as_of <= now)
        .order_by(MR.as_of, MR.model_run_id)
    ).all()  # fmt: skip
    latest: dict[str, models.ModelRun] = {}
    for r in j_rows:
        latest[r.manifest["bundle"]] = r
    out = {}
    for b, j in latest.items():
        p = session.scalars(
            select(MR).where(MR.component == serve.COMPONENT, MR.window == serve.WINDOW,
                             MR.model_name == p_model, MR.status == "ok", MR.as_of == j.as_of)
            .order_by(MR.model_run_id.desc())
        ).all()  # fmt: skip
        p_b = [x for x in p if x.manifest.get("bundle") == b]
        if p_b:
            out[b] = (j, p_b[0])
    return out


# --------------------------------------------------------------------------------- run


@dataclass
class Report:
    written: dict[str, int] = field(default_factory=dict)  # horizon -> fixtures
    no_pair: int = 0  # fixtures whose bundle has no J/P pair yet
    problems: list[str] = field(default_factory=list)


def due(f: serve.Fixture, now: datetime, done: set[str], seen: set[int]) -> list[str]:
    """The horizons to write now: lk once both XIs are seen (or at the last call), 24 in
    the T-24h milestone window."""
    out = []
    o24, c24 = snapshots.TIMED["T-24h"]
    if "24" not in done and f.kickoff - o24 <= now < f.kickoff - c24:
        out.append("24")
    both = {f.home, f.away} <= seen
    if "lk" not in done and now < f.kickoff and (both or f.kickoff - now <= LAST_CALL):
        out.append("lk")
    return out


def side_signals(u: pl.DataFrame, p: pl.DataFrame, xi: pl.DataFrame | None,
                 means: dict[str, float], scale: dict[str, float]) -> dict[str, Any]:  # fmt: skip
    """Raw and scaled S_miss, S_rat of one side; scaled 0 where null (rule 8)."""
    out: dict[str, Any] = {"n_xi": 0, "n_prev": 0}
    raw: dict[str, float | None] = {k: None for k in sig.STEP2_SIGNALS}
    if xi is not None:
        got = sig.live_signals(u, p, xi, means)
        if got.height:
            r = got.row(0, named=True)
            out |= {"n_xi": int(r["n_xi"]), "n_prev": int(r["n_prev"])}
            raw = {k: (None if r[k] is None else float(r[k])) for k in sig.STEP2_SIGNALS}
    out |= {k: raw[k] for k in sig.STEP2_SIGNALS}
    for k in sig.STEP2_SIGNALS:
        v = raw[k]
        out[f"x_{k}"] = 0.0 if v is None or not np.isfinite(v) else v / scale[k]
    return out


def mean_matrix(m: Any) -> tuple[list[float], float, float, float]:
    mm = np.asarray(m).mean(axis=0)
    i, j = np.indices(mm.shape)
    return ([float(x) for x in mm.ravel()], float(mm[i > j].sum()), float(np.trace(mm)),
            float(mm[i < j].sum()))  # fmt: skip


def run(session: Session, store: RawStore, bundles: dict[str, list[int]], p_model: str,
        max_goals: int, now: datetime, walk: hz.Walk | None = None,
        loader: Any = serve.load_refit) -> Report:  # fmt: skip
    """Write the due shadow rows of every upcoming bundle fixture (idempotent)."""
    report = Report()
    league_bundle = {lid: b for b, ls in bundles.items() for lid in ls}
    fixtures = serve.upcoming(session, league_bundle, now)
    if not fixtures:
        return report
    ids = [f.match_id for f in fixtures]
    S = models.LineupJShadow
    done: dict[int, set[str]] = {}
    for mid, h in session.execute(select(S.match_id, S.horizon).where(S.match_id.in_(ids))):
        done.setdefault(mid, set()).add(h)
    snaps = announced(session, ids, now)
    kick = {f.match_id: f.kickoff for f in fixtures}
    snaps = {k: v for k, v in snaps.items() if _utc(v.observed_at) < kick[k[0]]}
    seen: dict[int, set[int]] = {}
    for mid, tid in snaps:
        seen.setdefault(mid, set()).add(tid)
    todo = [(f, h) for f in fixtures for h in due(f, now, done.get(f.match_id, set()),
                                                  seen.get(f.match_id, set()))]  # fmt: skip
    if not todo:
        return report
    basis = load_basis(store)
    if basis is None:
        report.problems.append("lineup_j shadow: no signal basis stored yet")
        return report
    u, pp, meta = basis
    if not (now - BASIS_MAX_AGE <= datetime.fromisoformat(meta["as_of"]) <= now):
        report.problems.append(f"lineup_j shadow: signal basis as of {meta['as_of']} is stale")
        return report
    scale, means = meta["scale"], meta["line_means"]
    runs = pairs(session, now, p_model)
    loaded: dict[str, tuple[serve.Refit, serve.Refit]] = {}
    rows: list[models.LineupJShadow] = []
    for f, h in todo:
        b = league_bundle[f.league_id]
        if b not in runs:
            report.no_pair += 1
            continue
        if b not in loaded:
            jr, pr = (loader(store, r) for r in runs[b])
            if jr.as_of > now or pr.as_of > now:
                raise ValueError(f"refit of {b} is from the future")  # never leak
            loaded[b] = (jr, pr)
        jr, pr = loaded[b]
        sides: dict[str, Any] = {}
        seen_at = None
        for side, team in (("home", f.home), ("away", f.away)):
            xi, projected = None, True
            if h == "lk" and (f.match_id, team) in snaps:
                snap = snaps[(f.match_id, team)]
                xi = announced_xi(session, snap)
                projected = xi is None
                if xi is not None:
                    seen_at = max(seen_at or _utc(snap.observed_at), _utc(snap.observed_at))
            if xi is None:
                xi = projected_xi(session, f.match_id, team,
                                  min(now, f.kickoff - sig.H_24) if h == "24" else now)  # fmt: skip
            sides[side] = side_signals(u, pp, xi, means, scale) | {"projected": projected}
        beta = np.asarray(jr.draws[BETA], dtype=np.float64).reshape(len(sig.STEP2_SIGNALS), -1)
        x = {s: np.array([sides[s][f"x_{k}"] for k in sig.STEP2_SIGNALS]) for s in sides}
        off = ((x["home"] @ beta)[None, :], (x["away"] @ beta)[None, :])
        mj = serve.fixture_matrices(jr.draws, jr.fd, [f], max_goals, walk, extra_offset=off)[0]
        mp = serve.fixture_matrices(pr.draws, pr.fd, [f], max_goals, walk)[0]
        info = {**sides, "basis_as_of": meta["as_of"], "table": meta["table"],
                "beta_mean": [float(v) for v in beta.mean(axis=1)]}  # fmt: skip
        for model, m, run_ in (("J", mj, jr), ("P", mp, pr)):
            mat, ph, pd_, pa = mean_matrix(m)
            rows.append(S(match_id=f.match_id, horizon=h, model=model,
                          model_run_id=run_.model_run_id, refit_as_of=run_.as_of,
                          generated_at=now, lineup_seen_at=seen_at if h == "lk" else None,
                          signals=info, matrix=mat, p_home=ph, p_draw=pd_, p_away=pa))  # fmt: skip
        report.written[h] = report.written.get(h, 0) + 1
    session.add_all(rows)
    session.flush()
    return report


def main() -> None:
    import argparse

    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session
    from adatta.eval import harness
    from adatta.features import appearances as ap

    ap_ = argparse.ArgumentParser(prog="adatta.sim.lineup_j_shadow")
    sub = ap_.add_subparsers(dest="cmd", required=True)
    sub.add_parser("basis", help="the daily signal basis (after the appearances rebuild)")
    ap_.parse_args()
    cfg = harness.load_config()
    bundle_of = {int(x): b for b, xs in cfg.bundles.items() for x in xs}
    now = datetime.now(UTC)
    store = make_store(get_settings())
    with get_session() as s:
        matches, _ = harness.load_matches(s, list(bundle_of))
        s.rollback()
    season = now.year if now.month >= 7 else now.year - 1
    app = ap.load(store, [(str(lg), y) for lg in bundle_of for y in (season - 1, season)])
    _, meta2, _ = sig.load_step2(store, sig.J_TABLE)
    u, p, meta = build_basis(app, matches, bundle_of, now, meta2)
    save_basis(store, u, p, meta)
    print(f"lineup_j basis as of {now:%Y-%m-%d %H:%M}: {meta['teams']} teams, "
          f"{meta['players']} players with a rating history")  # fmt: skip


if __name__ == "__main__":
    main()
