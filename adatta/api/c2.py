"""C2 step 3 views (D54; docs/BEYOND_LEAGUES_PLAN.md "C2 step 3 amendment"): the eight C2
top flights' fixture groups and match view, from the Phase 1a live refits and their published
milestones. Labelled as the baseline model; a point forecast, so every probability is shown
without an interval (choice 1a); 90-minute markets only (choice 2a)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from adatta.api import schemas as sc
from adatta.api import views as v
from adatta.db import models
from adatta.eval import c2, harness
from adatta.model import c2_live
from adatta.sim import c2_serve as cs
from adatta.sim import serve

NOT_YET = "Not projected yet: the next capture tick writes it."
UNAVAILABLE = [
    sc.Unavailable(panel="Intervals", reason="The baseline model is a point fit: no interval."),
    sc.Unavailable(panel="Half-time markets",
                   reason="Not served for these leagues yet (D54)."),
    sc.Unavailable(panel="Decomposition", reason="Not built for the baseline model."),
    sc.Unavailable(panel="Squad", reason="The baseline model uses results only."),
    sc.Unavailable(panel="Edge",
                   reason="Edge analytics is wave 2 (§16.4); odds never feed the model."),
]  # fmt: skip


def leagues(conf: c2.C2Config | None = None) -> dict[int, str]:
    """C2 top flight -> its bundle."""
    conf = conf or c2.load_config()
    return {ls[0]: b for b, ls in conf.bundles.items()}


def _p(r: models.MarketProjection) -> sc.Prob:
    return sc.Prob(p=r.probability, lo=r.interval_lo, hi=r.interval_hi, point=r.interval_lo is None)


def _headline(ms: str, rows: list[models.MarketProjection]) -> sc.Headline:
    x = {r.selection: _p(r) for r in rows if r.market_type == "1X2"}
    return sc.Headline(milestone=ms, home=x["home"], draw=x["draw"], away=x["away"])


def confidence(m: v.MatchRow, run: models.ModelRun | None, fit: c2_live.Fit | None,
               fresh: sc.Freshness, champion: models.ModelRun | None, warmup: int,
               now: datetime,
               caps: list[models.CompetitionSourceCapability]) -> list[sc.Component]:  # fmt: skip
    out = [sc.Component(key="model", label="Model", status="limited",
                        value="baseline · results only",
                        detail=cs.LABEL.rstrip(".") + ". It uses results alone, not the "
                               "xG or other data the source has (D54).")]  # fmt: skip
    if fit is None or run is None:
        out.append(
            sc.Component(
                key="history",
                label="Historical data strength",
                status="missing",
                value="no refit",
                detail=cs.NO_REFIT,
            )
        )
    else:
        cold = [n for n, t in ((m.home, m.home_id), (m.away, m.away_id))
                if fit.n_prior(t) < warmup]  # fmt: skip
        out.append(sc.Component(
            key="history", label="Historical data strength",
            status="limited" if cold else "ok",
            value=f"{run.manifest.get('n_train', '?')} matches, top flight and second tier",
            detail=(f"Cold start: {', '.join(cold)} with fewer than {warmup} matches in the "
                    "training window." if cold else
                    "Both sides have a full history in the training window."),
        ))  # fmt: skip
    out.append(v.capability_component(caps))
    out.append(sc.Component(key="lineup", label="Lineup certainty", status="not_modelled",
                            value="results only",
                            detail="The baseline model uses results only."))  # fmt: skip
    out.append(sc.Component(key="cross_league", label="Cross-league prior dependence",
                            status="not_modelled", value=f"within {fresh.bundle} bundle",
                            detail="No cross-league layer is served; both teams are rated "
                                   "within their league's top flight + second tier.",
                            ))  # fmt: skip
    if champion is None:
        out.append(sc.Component(key="stability", label="Model stability", status="missing",
                                value="not served", detail=cs.NOT_SERVED))  # fmt: skip
    else:
        ok = bool(fresh.gates_pass)
        out.append(sc.Component(
            key="stability", label="Model stability", status="ok" if ok else "limited",
            value=f"served {champion.created_at:%Y-%m-%d} · fit {'ok' if ok else 'failed'}",
            detail=f"{champion.model_name}: beats league base rates and a league-only Poisson "
                   "on dev (D54).",
        ))  # fmt: skip
    if fresh.refit_as_of is None:
        out.append(sc.Component(key="freshness", label="Data freshness", status="missing",
                                value="no refit", detail=cs.NO_REFIT))  # fmt: skip
    else:
        out.append(sc.Component(
            key="freshness", label="Data freshness", status="stale" if fresh.stale else "ok",
            value=f"refit {fresh.age_days:.1f} d old",
            detail=f"Forecast from the baseline refit as of {fresh.refit_as_of:%Y-%m-%d}.",
        ))  # fmt: skip
    return v.checklist(out)


def _fit(ctx: v.Context, run: models.ModelRun | None, cache: dict[int, c2_live.Fit]
         ) -> c2_live.Fit | None:  # fmt: skip
    if run is None:
        return None
    if run.model_run_id not in cache:
        cache[run.model_run_id] = c2_live.load(ctx.store, run)
    return cache[run.model_run_id]


_FITS: dict[int, c2_live.Fit] = {}  # stored fits never change once written


def groups(
    session: Session,
    ctx: v.Context,
    now: datetime,
    start: datetime,
    end: datetime,
    competition_id: int | None,
    team_level: str | None,
) -> list[sc.CompetitionGroup]:
    conf = c2.load_config()
    tops = leagues(conf)
    champs = c2_live.champions(session)
    if not champs:
        return []  # nothing is shown for these leagues before it is served
    M, C = models.Match, models.Competition
    where: list[Any] = [M.kickoff_time >= start, M.kickoff_time <= end]
    if competition_id is not None:
        where.append(C.competition_id == competition_id)
    rows = [r for r in v._matches(session, ctx, where, leagues=sorted(champs))
            if r.status not in serve.NO_MATCH
            and (team_level is None or r.team_level == team_level)]  # fmt: skip
    if not rows:
        return []
    raw = cs._rows(session, sorted(champs), [M.match_id.in_([r.match_id for r in rows])])
    scoped = cs.scope(session, raw, conf)
    rows = [r for r in rows if r.match_id in scoped]
    proj = v._projections(session, [r.match_id for r in rows])
    caps = v._capabilities(session, {r.season_id for r in rows})
    runs, _ = cs.latest(session, now)
    warmup = harness.load_config().min_warmup_matches
    out: dict[int, sc.CompetitionGroup] = {}
    for r in rows:
        bundle = tops[r.league_id]
        run = runs.get(bundle)
        fresh = v._freshness(bundle, run, now)
        g = out.get(r.competition_id)
        if g is None:
            g = out[r.competition_id] = sc.CompetitionGroup(
                competition_id=r.competition_id, name=r.competition, country=r.country,
                level=r.level, tier=r.tier, capabilities=v._cap_view(caps[r.season_id]),
                freshness=fresh, fixtures=[],
            )  # fmt: skip
        by_ms = proj.get(r.match_id)
        why = scoped[r.match_id].reason
        if not by_ms and why is None:
            why = cs.NO_REFIT if run is None else NOT_YET
        g.fixtures.append(sc.FixtureRow(
            match_id=r.match_id, kickoff=r.kickoff, round=r.round, home=r.home, away=r.away,
            team_level=r.team_level,
            headline=_headline(ms := v._latest_milestone(by_ms), by_ms[ms]) if by_ms else None,
            no_projection_reason=None if by_ms else why,
            confidence=confidence(r, run, _fit(ctx, run, _FITS), fresh,
                                  champs.get(r.league_id), warmup, now, caps[r.season_id]),
        ))  # fmt: skip
    return list(out.values())


def match_view(session: Session, ctx: v.Context, m: v.MatchRow, header: sc.Header,
               now: datetime) -> sc.MatchView:  # fmt: skip
    conf = c2.load_config()
    bundle = leagues(conf)[m.league_id]
    champ = c2_live.champions(session).get(m.league_id)
    by_ms = v._projections(session, [m.match_id]).get(m.match_id, {})
    milestones: list[sc.Milestone] = []
    for ms in v._published_order(by_ms):
        pr = session.get(models.ProjectionRun, by_ms[ms][0].projection_run_id)
        assert pr is not None
        cv = pr.component_versions.get(bundle, {})
        milestones.append(sc.Milestone(
            milestone=ms, generated_at=v._utc(pr.generated_at),
            projection_run_id=pr.projection_run_id, model_run_id=cv.get("model_run_id"),
            refit_as_of=datetime.fromisoformat(cv["as_of"]) if "as_of" in cv else None,
            selections=[sc.Selection(market=r.market_type, line=r.line, selection=r.selection,
                                     prob=_p(r)) for r in by_ms[ms]],
        ))  # fmt: skip
    run = None
    if milestones and milestones[-1].model_run_id is not None:
        run = session.get(models.ModelRun, milestones[-1].model_run_id)
    runs, _ = cs.latest(session, now)
    serving = runs.get(bundle)
    fresh = v._freshness(bundle, run or serving, now)
    fit = _fit(ctx, run or serving, _FITS)
    matrix, coh, reason = None, None, None
    if run is not None and fit is not None:
        last = milestones[-1]
        cfg = c2_live.scoring_config()
        mat, tail = fit.matrix(m.home_id, m.away_id, m.league_id, cfg.max_goals)
        served = {(s.market, s.line, s.selection): s.prob.p for s in last.selections}
        again = {k: float(x[0]) for k, x in serve.market_draws(mat[None]).items()}
        diff = max(abs(again[k] - p) for k, p in served.items())
        coh = sc.Coherence(milestone=last.milestone, coherent=diff <= v.COHERENCE_TOL,
                           max_abs_diff=diff)  # fmt: skip
        k = v.SHOWN_GOALS + 1
        cells = [[sc.Prob(p=float(mat[i, j]), point=True) for j in range(k)] for i in range(k)]
        matrix = sc.ScoreMatrix(max_shown=v.SHOWN_GOALS, cells=cells,
                                rest=sc.Prob(p=float(1.0 - mat[:k, :k].sum()), point=True),
                                tail_mass=tail, max_goals=cfg.max_goals)  # fmt: skip
    else:
        raw = cs._rows(session, [m.league_id], [models.Match.match_id == m.match_id])
        scoped = cs.scope(session, raw, conf).get(m.match_id)
        reason = (scoped.reason if scoped is not None and scoped.reason else
                  cs.NOT_SERVED if champ is None else
                  cs.NO_REFIT if serving is None else
                  NOT_YET)  # fmt: skip
    warmup = harness.load_config().min_warmup_matches
    return sc.MatchView(
        generated_at=now, header=header, freshness=fresh,
        confidence=confidence(m, run or serving, fit, fresh, champ, warmup, now,
                              v._capabilities(session, {m.season_id})[m.season_id]),
        milestones=milestones, matrix=matrix if coh is None or coh.coherent else None,
        decomposition=[], coherence=coh, no_projection_reason=reason, squad=None,
        unavailable=UNAVAILABLE,
    )  # fmt: skip
