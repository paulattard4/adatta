"""National-team views (Stage D v1 serving; docs/BEYOND_LEAGUES_PLAN.md, "D v1 serving
acceptance"): the fixture list's national groups and the national match view, from the
national live refits and their published 1X2 milestones. Labelled team-only; Over/Under and
BTTS withheld; youth and non-servable pairings show why, with no probability."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from adatta.api import schemas as sc
from adatta.api import views as v
from adatta.db import models
from adatta.model import national as nt
from adatta.model import national_live as nl
from adatta.sim import national_serve as ns
from adatta.sim import serve, ties

BUNDLE = ns.BUNDLE
UNAVAILABLE = [
    sc.Unavailable(panel="Over/Under and BTTS", reason=ns.WITHHELD),
    sc.Unavailable(
        panel="Squad", reason="Team-only model: call-ups and lineups are not used (Stage D v1)."
    ),  # fmt: skip
    sc.Unavailable(panel="Decomposition", reason="Not built for the national model yet."),
    sc.Unavailable(
        panel="Edge", reason="Edge analytics is wave 2 (§16.4); odds never feed the model."
    ),  # fmt: skip
]


def qualify(session: Session, ctx: v.Context, m: v.MatchRow,
            by_ms: dict[str, list[models.MarketProjection]], now: datetime
            ) -> tuple[sc.Qualify | None, sc.Unavailable | None]:  # fmt: skip
    """The tie's P(advance) and path from the latest milestone that has them, or why there
    is none (no panel at all when the match is not a knockout)."""
    from datetime import timedelta

    rules = ties.load_rules()
    kick = v._utc(m.kickoff)
    found, _, _ = ties.stored(session, ctx.store, rules, kick - timedelta(days=60),
                              kick + timedelta(days=60))  # fmt: skip
    tie = next((t for t in found if any(x.match_id == m.match_id for x in t.legs)), None)
    with_q = [ms for ms in v._published_order(by_ms)
              if any(r.market_type == ties.QUALIFY for r in by_ms[ms])]  # fmt: skip
    if tie is None or not with_q:
        if tie is not None:
            why = ns.reason(session, ctx.store, [m.match_id], now).get(m.match_id, ns.NOT_COVERED)
        elif ties.is_knockout_round(m.round):
            why = ties.NO_RULE
        else:
            return None, None
        return None, sc.Unavailable(panel="Going through", reason=why)
    ms = with_q[-1]
    rows = by_ms[ms]
    pr = session.get(models.ProjectionRun, rows[0].projection_run_id)
    assert pr is not None
    gen = v._utc(pr.generated_at)
    q = {r.selection: v._p(r) for r in rows if r.market_type == ties.QUALIFY}
    path = {r.selection: v._p(r) for r in rows if r.market_type == ties.PATH}
    stage = ("single match" if tie.rule.legs == 1 else
             "between legs" if tie.legs[0].final_by(gen) else "before leg 1")  # fmt: skip
    name = {m.home_id: m.home, m.away_id: m.away}

    def score(x: ties.Leg) -> str | None:
        if x.nt is None or x.status not in ties.FINAL:
            return None
        s = f"{x.nt[0]}-{x.nt[1]}"
        if x.et is not None and x.status in ("AET", "PEN"):
            s += f" (aet {x.nt[0] + x.et[0]}-{x.nt[1] + x.et[1]})"
        if x.pens is not None and x.status == "PEN":
            s += f", {x.pens[0]}-{x.pens[1]} pens"
        return s

    legs = [sc.QualifyLeg(match_id=x.match_id, home=name.get(x.home, str(x.home)),
                          away=name.get(x.away, str(x.away)), kickoff=x.kickoff, score=score(x))
            for x in tie.legs]  # fmt: skip
    return sc.Qualify(milestone=ms, generated_at=gen, label=ties.LABEL, stage=stage,
                      home=q["home"], away=q["away"], path=path, legs=legs,
                      round=tie.round), None  # fmt: skip


def _run(session: Session, now: datetime) -> models.ModelRun | None:
    run, _ = ns.latest(session, now)
    return run


def _row_dict(session: Session, ctx: v.Context, m: v.MatchRow) -> dict[str, Any] | None:
    """The match as a TARGET_SCHEMA row (sides, confederations, host role), or None if it is
    not a servable pairing."""
    fr = nt.national_frame(session, ctx.store, lambda *_: True, match_ids=[m.match_id])
    return fr.row(0, named=True) if fr.height else None


def confidence(m: v.MatchRow, run: models.ModelRun | None, post: dict[str, Any] | None,
               fresh: sc.Freshness, champion: models.ModelRun | None,
               now: datetime) -> list[sc.Component]:  # fmt: skip
    out = [sc.Component(key="model", label="Model", status="limited", value="team-only",
                        detail=ns.LABEL + ". 90' only.")]  # fmt: skip
    if post is None or run is None:
        out.append(sc.Component(key="history", label="Historical data strength",
                                status="missing", value="no refit",
                                detail=ns.NO_REFIT))  # fmt: skip
    else:
        teams = set(np.asarray(post["teams"]).astype(np.int64).tolist())
        cold = [n for n, t in ((m.home, m.home_id), (m.away, m.away_id)) if t not in teams]
        out.append(sc.Component(
            key="history", label="Historical data strength",
            status="limited" if cold else "ok",
            value=f"{run.manifest.get('n_train', '?')} matches, 10-year window",
            detail=(f"Cold start: {', '.join(cold)} not in the training window; rated at the "
                    "confederation's level with the team and walk spread." if cold else
                    "Both sides are in the refit's training window."),
        ))  # fmt: skip
    out.append(sc.Component(key="lineup", label="Lineup certainty", status="not_modelled",
                            value="team-level",
                            detail="Call-ups and lineups are not used (Stage D v1)."))  # fmt: skip
    if champion is None:
        out.append(sc.Component(key="stability", label="Model stability", status="missing",
                                value="no champion", detail="Nothing is promoted."))  # fmt: skip
    else:
        ok = bool(fresh.gates_pass)
        out.append(sc.Component(
            key="stability", label="Model stability", status="ok" if ok else "limited",
            value=f"promoted {champion.created_at:%Y-%m-%d} · gates {'pass' if ok else 'fail'}",
            detail=f"{champion.model_name}, promoted on the international holdout; the serving "
                   "refit passes R-hat, ESS and divergence gates (PPC reported only)." if ok else
                   "The serving refit did not pass its sampler gates.",
        ))  # fmt: skip
    if fresh.refit_as_of is None:
        out.append(sc.Component(key="freshness", label="Data freshness", status="missing",
                                value="no refit", detail=ns.NO_REFIT))  # fmt: skip
    else:
        out.append(sc.Component(
            key="freshness", label="Data freshness", status="stale" if fresh.stale else "ok",
            value=f"refit {fresh.age_days:.1f} d old",
            detail=f"Forecast from the national refit as of {fresh.refit_as_of:%Y-%m-%d}.",
        ))  # fmt: skip
    return out


def groups(
    session: Session,
    ctx: v.Context,
    now: datetime,
    start: datetime,
    end: datetime,
    competition_id: int | None,
    team_level: str | None,
) -> list[sc.CompetitionGroup]:
    M, C = models.Match, models.Competition
    where: list[Any] = [M.kickoff_time >= start, M.kickoff_time <= end]
    if competition_id is not None:
        where.append(C.competition_id == competition_id)
    rows = [r for r in v._matches(session, ctx, where, national=True)
            if r.status not in serve.NO_MATCH
            and (team_level is None or r.team_level == team_level)]  # fmt: skip
    if not rows:
        return []
    proj = v._projections(session, [r.match_id for r in rows])
    why = ns.reason(session, ctx.store, [r.match_id for r in rows if r.match_id not in proj], now)
    caps = v._capabilities(session, {r.season_id for r in rows})
    run = _run(session, now)
    fresh = v._freshness(BUNDLE, run, now)
    champ = nl.champion(session)
    post = ctx.national_draws(run) if run is not None else None
    out: dict[int, sc.CompetitionGroup] = {}
    for r in rows:
        g = out.get(r.competition_id)
        if g is None:
            g = out[r.competition_id] = sc.CompetitionGroup(
                competition_id=r.competition_id, name=r.competition, country=r.country,
                level=r.level, tier=r.tier, capabilities=v._cap_view(caps[r.season_id]),
                freshness=fresh, fixtures=[],
            )  # fmt: skip
        by_ms = proj.get(r.match_id)
        g.fixtures.append(sc.FixtureRow(
            match_id=r.match_id, kickoff=r.kickoff, round=r.round, home=r.home, away=r.away,
            team_level=r.team_level,
            headline=v._headline(ms := v._latest_milestone(by_ms), by_ms[ms]) if by_ms else None,
            no_projection_reason=None if by_ms else why.get(r.match_id, ns.NOT_COVERED),
            confidence=confidence(r, run, post, fresh, champ, now),
        ))  # fmt: skip
    return list(out.values())


def match_view(session: Session, ctx: v.Context, m: v.MatchRow, header: sc.Header,
               now: datetime) -> sc.MatchView:  # fmt: skip
    by_ms = v._projections(session, [m.match_id]).get(m.match_id, {})
    milestones: list[sc.Milestone] = []
    for ms in v._published_order(by_ms):
        pr = session.get(models.ProjectionRun, by_ms[ms][0].projection_run_id)
        assert pr is not None
        cv = pr.component_versions.get(BUNDLE, {})
        milestones.append(sc.Milestone(
            milestone=ms, generated_at=v._utc(pr.generated_at),
            projection_run_id=pr.projection_run_id, model_run_id=cv.get("model_run_id"),
            refit_as_of=datetime.fromisoformat(cv["as_of"]) if "as_of" in cv else None,
            selections=[sc.Selection(market=r.market_type, line=r.line, selection=r.selection,
                                     prob=v._p(r)) for r in by_ms[ms]
                        if r.market_type not in (ties.QUALIFY, ties.PATH)],  # in `qualify`
        ))  # fmt: skip
    run = None
    if milestones and milestones[-1].model_run_id is not None:
        run = session.get(models.ModelRun, milestones[-1].model_run_id)
    serving = _run(session, now)
    fresh = v._freshness(BUNDLE, run or serving, now)
    behind = run or serving
    post = ctx.national_draws(behind) if behind is not None else None
    matrix, coh, reason = None, None, None
    row = _row_dict(session, ctx, m) if run is not None else None
    if run is not None and row is not None and post is not None and run.as_of is not None:
        last = milestones[-1]
        row["kickoff"] = v._kickoff_then(session, m, last.generated_at)
        md = ns.matrix_draws(post, row, v._utc(run.as_of), int(run.manifest["n_months"]))
        p = nt.one_x_two(md)
        served = {s.selection: s.prob.p for s in last.selections if s.market == "1X2"}
        diff = max(abs(float(p[:, j].mean()) - served[s])
                   for j, s in enumerate(("home", "draw", "away")))  # fmt: skip
        coh = sc.Coherence(milestone=last.milestone, coherent=diff <= v.COHERENCE_TOL,
                           max_abs_diff=diff)  # fmt: skip
        k = v.SHOWN_GOALS + 1
        cells = [[v._prob(md[:, i, j]) for j in range(k)] for i in range(k)]
        matrix = sc.ScoreMatrix(max_shown=v.SHOWN_GOALS, cells=cells,
                                rest=v._prob(1.0 - md[:, :k, :k].sum(axis=(1, 2))),
                                tail_mass=0.0, max_goals=nt.MAX_GOALS)  # fmt: skip
    else:
        reason = ns.reason(session, ctx.store, [m.match_id], now).get(m.match_id, ns.NOT_COVERED)
    q, q_why = qualify(session, ctx, m, by_ms, now)
    return sc.MatchView(
        generated_at=now, header=header, freshness=fresh,
        confidence=confidence(m, run or serving, post, fresh, nl.champion(session), now),
        milestones=milestones, matrix=matrix if coh is None or coh.coherent else None,
        decomposition=[], coherence=coh, no_projection_reason=reason, squad=None,
        unavailable=UNAVAILABLE + ([q_why] if q_why else []), qualify=q,
    )  # fmt: skip
