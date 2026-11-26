"""C2 step 3 (D54): milestone projections of the eight C2 top flights from Phase 1a.

The baseline model (`adatta.model.c2_live`), served where the full model was not proven
(principle 8). A deterministic point fit, so every row has a probability and no interval
(D54 choice 1a), and only the 90-minute markets (choice 2a). Only a top flight recorded as
champion (`phase1a_c2_<league>`, acceptance (g)) is projected. Milestones, their windows and
the D41 refit milestone are those of `adatta.sim.serve`, which this module never changes.

Fixtures in scope are the C2 round classes (`config/c2_bundles.yml`): regular-season rounds,
and post-split phases between two sides of that league-season's regular season (D52 1a); a
phase pairing of fewer than three sides is a play-off tie, not forecast.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import SOURCE_ID
from adatta.adapters.storage import RawStore
from adatta.db import models
from adatta.eval import c2
from adatta.model import c2_live
from adatta.sim import serve

LABEL = ("Baseline model (Phase 1a, results-only Dixon-Coles). The full model was not "
         "proven better in this league (C2 holdout, 2026-11-26), so the simpler one is "
         "served. A point forecast: no interval.")  # fmt: skip
NO_REFIT = "No passing baseline refit for this league yet."
NOT_SERVED = "This league is not served yet."


@dataclass(frozen=True)
class Scoped:
    fixture: serve.Fixture | None
    reason: str | None  # why it is not forecast (fixture None)


def _rows(session: Session, leagues: list[int], where: list[Any]) -> list[Any]:
    M, S, C = models.Match, models.Season, models.Competition
    return list(session.execute(
        select(M.match_id, M.kickoff_time, C.source_ref, M.home_team_id, M.away_team_id,
               M.status, M.round, S.year, M.season_id)
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .where(C.source_id == SOURCE_ID, C.source_ref.in_([str(x) for x in leagues]), *where)
        .order_by(M.kickoff_time, M.match_id)
    ).all())  # fmt: skip


def scope(session: Session, rows: list[Any], conf: c2.C2Config) -> dict[int, Scoped]:
    """Per match: its fixture if it is a league match in C2 scope, else why not."""
    out: dict[int, Scoped] = {}
    seasons = {int(r[8]) for r in rows}
    if not seasons:
        return out
    M = models.Match
    every = session.execute(
        select(M.season_id, M.home_team_id, M.away_team_id, M.round).where(
            M.season_id.in_(seasons))
    ).all()  # fmt: skip
    league_of = {int(r[8]): int(r[2]) for r in rows}
    members: dict[int, set[int]] = {}
    phase_pairs: dict[tuple[int, str], list[tuple[int, int]]] = {}
    for sid, h, a, rnd in every:
        fam = c2.family(rnd or "")
        klass = conf.rounds.get(league_of[sid], {}).get(fam)
        if klass == "regular":
            members.setdefault(sid, set()).update((h, a))
        elif klass == "phase":
            phase_pairs.setdefault((sid, fam), []).append((h, a))
    for mid, kick, lg, h, a, status, rnd, year, sid in rows:
        if status in serve.NO_MATCH:
            continue
        fam = c2.family(rnd or "")
        klass = conf.rounds.get(int(lg), {}).get(fam)
        fx = serve.Fixture(mid, serve._utc(kick), int(lg), h, a, year)
        if klass == "regular":
            out[mid] = Scoped(fx, None)
        elif klass == "phase":
            inside = members.get(sid, set())
            inner = [(x, y) for x, y in phase_pairs.get((sid, fam), [])
                     if x in inside and y in inside]  # fmt: skip
            comp = next((g for g in c2._components(inner) if h in g), set())
            if h not in inside or a not in inside:
                out[mid] = Scoped(None, "A play-off against a side from another league.")
            elif len(comp) < c2.MIN_GROUP:
                out[mid] = Scoped(None, "A play-off tie, not a league match.")
            else:
                out[mid] = Scoped(fx, None)
        elif klass == "excluded":
            out[mid] = Scoped(None, "Not a league match.")
        else:
            out[mid] = Scoped(None, f"Round {fam!r} is not classed for this league.")
    return out


def upcoming(session: Session, leagues: list[int], now: datetime, conf: c2.C2Config
             ) -> tuple[list[serve.Fixture], list[str]]:  # fmt: skip
    """League fixtures of `leagues` kicking off within serve.HORIZON, and problems (an
    unclassed round label)."""
    M = models.Match
    rows = _rows(session, leagues, [M.kickoff_time > now, M.kickoff_time <= now + serve.HORIZON])
    sc = scope(session, rows, conf)
    fixtures = [s.fixture for s in sc.values() if s.fixture is not None]
    problems = sorted({f"c2 serving: {s.reason}" for s in sc.values()
                       if s.reason and "not classed" in s.reason})  # fmt: skip
    return sorted(fixtures, key=lambda f: (f.kickoff, f.match_id)), problems


def latest(session: Session, now: datetime) -> tuple[dict[str, models.ModelRun], list[str]]:
    """The latest passing live refit per C2 bundle with as_of <= now, and problems (as
    `serve.latest_refits`)."""
    MR = models.ModelRun
    rows = session.scalars(
        select(MR).where(MR.component == c2_live.COMPONENT, MR.window == c2_live.WINDOW,
                         MR.model_name == c2_live.MODEL, MR.as_of <= now)
        .order_by(MR.as_of, MR.model_run_id)
    ).all()  # fmt: skip
    newest: dict[str, models.ModelRun] = {}
    passing: dict[str, models.ModelRun] = {}
    for r in rows:
        newest[r.manifest["bundle"]] = r
        if r.status == "ok":
            passing[r.manifest["bundle"]] = r
    problems = [f"c2 serving {b}: refit as of {r.as_of:%Y-%m-%d} {r.status}"
                for b, r in sorted(newest.items()) if r.status != "ok"]  # fmt: skip
    for b, r in sorted(passing.items()):
        as_of = serve._utc(r.as_of) if r.as_of else now
        if now - as_of > serve.STALE_AFTER:
            problems.append(f"c2 serving {b}: latest passing refit is as of {as_of:%Y-%m-%d}, "
                            f"older than {serve.STALE_AFTER.days} days")  # fmt: skip
    return passing, problems


def markets(fit: c2_live.Fit, f: serve.Fixture, max_goals: int
            ) -> tuple[dict[tuple[str, float | None, str], float], float]:  # fmt: skip
    """Every 90' selection of one fixture (`serve.market_draws` on the one matrix), and the
    matrix's tail mass beyond max_goals a side."""
    m, tail = fit.matrix(f.home, f.away, f.league_id, max_goals)
    return {k: float(v[0]) for k, v in serve.market_draws(m[None]).items()}, tail


@dataclass
class Report:
    written: dict[str, int] = field(default_factory=dict)
    no_refit: int = 0
    problems: list[str] = field(default_factory=list)
    projection_run_id: int | None = None


def run_projection(session: Session, store: RawStore, manifest: dict[str, Any],
                   now: datetime | None = None, conf: c2.C2Config | None = None,
                   loader: Any = c2_live.load) -> Report:  # fmt: skip
    """Write the due milestone projections of every upcoming fixture of a champion C2 top
    flight. Idempotent, as `serve.run_projection`."""
    now = now or datetime.now(UTC)
    conf = conf or c2.load_config()
    cfg = c2_live.scoring_config()
    report = Report()
    champs = c2_live.champions(session)
    bundle_of = {ls[0]: b for b, ls in conf.bundles.items() if ls[0] in champs}
    if not bundle_of:
        return report
    fixtures, report.problems = upcoming(session, sorted(bundle_of), now, conf)
    done = serve._done(session, [f.match_id for f in fixtures])
    due = {f.match_id: serve.due_milestones(f.kickoff, now, done[f.match_id]) for f in fixtures}
    runs, problems = latest(session, now)
    report.problems += problems
    stale = [f for f in fixtures if not due[f.match_id] and done[f.match_id]
             and bundle_of[f.league_id] in runs]  # fmt: skip
    used = serve._refits_used(session, [f.match_id for f in stale],
                              {f.match_id: bundle_of[f.league_id] for f in stale})  # fmt: skip
    for f in stale:  # D41: a newer passing refit gives a fresh forecast
        run = runs[bundle_of[f.league_id]]
        if run.as_of is None or f.match_id not in used:
            continue
        label = serve.refit_label(run.as_of)
        if serve._utc(run.as_of) > used[f.match_id] and label not in done[f.match_id]:
            due[f.match_id] = [label]
    fixtures = [f for f in fixtures if due[f.match_id]]
    fits: dict[str, tuple[models.ModelRun, c2_live.Fit]] = {}
    results: dict[int, dict[tuple[str, float | None, str], float]] = {}
    for f in fixtures:
        b = bundle_of[f.league_id]
        if b not in runs:
            report.no_refit += 1
            continue
        if b not in fits:
            fit = loader(store, runs[b])
            if fit.as_of > now:
                raise ValueError(f"refit {runs[b].model_run_id} is from the future")
            fits[b] = (runs[b], fit)
        probs, tail = markets(fits[b][1], f, cfg.max_goals)
        if tail >= c2_live.TAIL_MAX:  # acceptance (c): never served past the truncation limit
            report.problems.append(f"c2 serving: match {f.match_id} tail {tail:.1e} beyond "
                                   f"{cfg.max_goals} goals")  # fmt: skip
            continue
        results[f.match_id] = probs
    if not results:
        return report
    pr = models.ProjectionRun(
        generated_at=now, as_of=now, git_commit=manifest["git_commit"],
        lockfile_hash=manifest["lockfile_hash"],
        config_hash=",".join(sorted({r.config_hash for r, _ in fits.values()})),
        component_versions={b: {"model_run_id": r.model_run_id, "model": c2_live.MODEL,
                                "as_of": fit.as_of.isoformat(), "baseline": True}
                            for b, (r, fit) in sorted(fits.items())},
        training_snapshot_hash=hashlib.sha256(
            "".join(f"{b}:{fit.training_hash}" for b, (_, fit) in sorted(fits.items())).encode()
        ).hexdigest(),
        source_snapshot_ids={}, override_set_hash=serve.NO_OVERRIDES, seed=c2_live.SEED,
    )  # fmt: skip
    session.add(pr)
    session.flush()
    rows = []
    for f in fixtures:
        if f.match_id not in results:
            continue
        for ms in due[f.match_id]:
            report.written[ms] = report.written.get(ms, 0) + 1
            for (market, line, sel), p in results[f.match_id].items():
                rows.append(models.MarketProjection(
                    projection_run_id=pr.projection_run_id, match_id=f.match_id, milestone=ms,
                    market_type=market, line=line, selection=sel, probability=p,
                    interval_lo=None, interval_hi=None,
                ))  # fmt: skip
    session.add_all(rows)
    session.flush()
    report.projection_run_id = pr.projection_run_id
    return report


def sums_to_one(probs: dict[tuple[str, float | None, str], float]) -> float:
    """Largest |1 - sum| over the served markets of one fixture ((d))."""
    groups: dict[tuple[str, float | None], float] = {}
    for (market, line, _), p in probs.items():
        groups[(market, line)] = groups.get((market, line), 0.0) + p
    return max(abs(1.0 - s) for s in groups.values())
