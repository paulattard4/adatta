"""Stage D v1 serving: national-team forecasts from the stored draws of the latest passing live
refit (docs/BEYOND_LEAGUES_PLAN.md, "D v1 serving acceptance"). Numpy only: it runs inside the
capture tick without the model dependency group, and never runs NUTS or reads odds.

Served: 90' 1X2 (market_projection) for upcoming matches between two servable sides, at the
club milestones, and, for a leg of a knockout tie with a written rule (Stage D v2,
`adatta.sim.ties`), P(advance) and its path in the same projection run. The score matrix
is recomputed from the same draws on demand. Over/Under and BTTS are withheld (not
validated for national teams). A match whose week has no refit from it or the week before
gets no forecast, with the reason, rather than a months-old one.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.storage import RawStore
from adatta.db import models
from adatta.eval.harness import week_start
from adatta.model import national as nt
from adatta.model import national_live as nl
from adatta.sim import serve
from adatta.sim import ties as tie
from adatta.sim import tournament as tm

LABEL = ("Team-only: results-based national model (Stage D v1), no squad or call-up "
         "information")  # fmt: skip
WITHHELD = "Over/Under and BTTS are not validated for national teams (Stage D v1 tested 1X2)."
NOT_COVERED = "Stage D covers senior men's national teams only."
YOUTH = "Not forecast: youth sides get nothing betting-facing (CLAUDE.md rule 10)."
NO_REFIT = "No passing national refit yet."
NO_CURRENT = "No current national refit: none from this match's week or the week before."
NOT_YET = "Not projected yet: the next capture tick writes it."
CURRENT = timedelta(days=7)  # a refit serves a match of its own week or the next one
BUNDLE = "national"  # the component_versions key of a national projection run

Selection = tuple[str, float | None, str]


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def project(post: dict[str, Any], row: dict[str, Any], as_of: datetime,
            months: int) -> dict[Selection, tuple[float, float, float]]:  # fmt: skip
    """Selection -> (posterior-mean probability, 5th, 95th percentile) for one match (a
    TARGET_SCHEMA row). Each match starts the walk's random stream at the seed, so a
    forecast is rebuilt from (draws, match) alone and equals `national.predict` on it."""
    p = nt.one_x_two(matrix_draws(post, row, as_of, months))
    out: dict[Selection, tuple[float, float, float]] = {}
    for j, s in enumerate(("home", "draw", "away")):
        lo, hi = np.quantile(p[:, j], serve.INTERVAL)
        out[("1X2", None, s)] = (float(p[:, j].mean()), float(lo), float(hi))
    return out


def matrix_draws(post: dict[str, Any], row: dict[str, Any], as_of: datetime,
                 months: int) -> Any:  # fmt: skip
    return nt.matrices(post, row, as_of, months, np.random.default_rng(nt.SEED))


def matrix(post: dict[str, Any], row: dict[str, Any], as_of: datetime, months: int) -> Any:
    """The posterior-mean 90' score matrix (MAX_GOALS + 1 square)."""
    return matrix_draws(post, row, as_of, months).mean(axis=0)


def latest(session: Session, now: datetime) -> tuple[models.ModelRun | None, list[str]]:
    """The latest passing live refit with as_of <= now, and problems (the newest failed, or
    the passing one is older than serve.STALE_AFTER)."""
    MR = models.ModelRun
    rows = session.scalars(
        select(MR).where(MR.component == nt.COMPONENT, MR.window == nl.WINDOW,
                         MR.model_name == nt.model_name(), MR.as_of <= now)
        .order_by(MR.as_of, MR.model_run_id)
    ).all()  # fmt: skip
    if not rows:
        return None, []
    passing = [r for r in rows if r.status == "ok"]
    problems = []
    if rows[-1].status != "ok":
        problems.append(f"serving national: refit as of {_utc(rows[-1].as_of):%Y-%m-%d} "  # type: ignore[arg-type]
                        f"{rows[-1].status}")  # fmt: skip
    run = passing[-1] if passing else None
    return run, problems


def current(run: models.ModelRun | None, kickoff: datetime) -> bool:
    """A refit serves a match of its own week or the following one."""
    return run is not None and run.as_of is not None and \
        _utc(run.as_of) >= week_start(kickoff) - CURRENT  # fmt: skip


def run_projection(session: Session, store: RawStore, manifest: dict[str, Any],
                   now: datetime | None = None) -> serve.ProjectReport:  # fmt: skip
    """Write the due milestone 1X2 projections for every upcoming servable national match.
    Idempotent: a (match, milestone) already written is never written again."""
    now = now or datetime.now(UTC)
    report = serve.ProjectReport()
    fx = nl.upcoming(session, store, now)
    if fx.is_empty():
        return report
    ids = fx["match_id"].to_list()
    done = serve._done(session, ids)
    due = {r["match_id"]: serve.due_milestones(r["kickoff"], now, done[r["match_id"]])
           for r in fx.iter_rows(named=True)}  # fmt: skip
    if not any(due.values()):
        return report
    run, problems = latest(session, now)
    report.problems = problems
    rows = [r for r in fx.iter_rows(named=True) if due[r["match_id"]]]
    servable = [r for r in rows if current(run, r["kickoff"])]
    report.no_refit = len(rows) - len(servable)
    if run is None or not servable:
        return report
    if _utc(run.as_of) > now:  # type: ignore[arg-type]
        raise ValueError(f"refit {run.model_run_id} is from the future")  # never leak
    post = nl.load(store, run)
    months = int(run.manifest["n_months"])
    as_of = _utc(run.as_of)  # type: ignore[arg-type]
    tie_of, confs = tie.serving_ties(session, store, [r["match_id"] for r in servable], now)
    versions: dict[str, Any] = {
        BUNDLE: {
            "model_run_id": run.model_run_id,
            "model": nt.model_name(),
            "as_of": as_of.isoformat(),
        }
    }
    if tie_of:
        versions[tie.QUALIFY] = {
            "rules_hash": tie.rules_hash(),
            "kappa_et": tm.KAPPA_ET,
            "p_shootout": tm.P_SHOOTOUT,
            "seed": tm.SEED,
        }
    pr = models.ProjectionRun(
        generated_at=now, as_of=now, git_commit=manifest["git_commit"],
        lockfile_hash=manifest["lockfile_hash"], config_hash=run.config_hash,
        component_versions=versions,
        training_snapshot_hash=hashlib.sha256(run.data_snapshot_hash.encode()).hexdigest(),
        source_snapshot_ids={}, override_set_hash=serve.NO_OVERRIDES, seed=nt.SEED,
    )  # fmt: skip
    session.add(pr)
    session.flush()
    out = []
    for r in servable:
        proj = project(post, r, as_of, months)
        t = tie_of.get(r["match_id"])
        if t is not None:
            proj = {**proj, **tie.project(post, t, r["match_id"], confs, as_of, months, now)}
            report.written["ties"] = report.written.get("ties", 0) + 1
        for ms in due[r["match_id"]]:
            report.written[ms] = report.written.get(ms, 0) + 1
            for (market, line, sel), (p, lo, hi) in proj.items():
                out.append(models.MarketProjection(
                    projection_run_id=pr.projection_run_id, match_id=r["match_id"],
                    milestone=ms, market_type=market, line=line, selection=sel, probability=p,
                    interval_lo=lo, interval_hi=hi,
                ))  # fmt: skip
    session.add_all(out)
    session.flush()
    report.projection_run_id = pr.projection_run_id
    return report


def reason(session: Session, store: RawStore, match_ids: list[int], now: datetime
           ) -> dict[int, str]:  # fmt: skip
    """Why a national match has no forecast: not a servable pairing (youth apart), no
    refit, no current refit, or not projected yet."""
    M, T = models.Match, models.Team
    if not match_ids:
        return {}
    side = nt.sides(session)
    matches = session.scalars(select(M).where(M.match_id.in_(match_ids))).all()
    team_ids = {t for m in matches for t in (m.home_team_id, m.away_team_id)}
    q = select(T.team_id, T.team_level).where(T.team_id.in_(team_ids))
    level = {int(t): str(lv) for t, lv in session.execute(q)}
    run, _ = latest(session, now)
    out = {}
    for m in matches:
        lv = (level.get(m.home_team_id) or "", level.get(m.away_team_id) or "")
        if any(v.startswith("u") for v in lv):
            out[m.match_id] = YOUTH
        elif m.home_team_id not in side or m.away_team_id not in side:
            out[m.match_id] = NOT_COVERED
        elif run is None:
            out[m.match_id] = NO_REFIT
        elif not current(run, _utc(m.kickoff_time)):
            out[m.match_id] = NO_CURRENT
        else:
            out[m.match_id] = NOT_YET
    return out
