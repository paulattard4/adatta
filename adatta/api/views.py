"""Read model behind the dashboard: fixture browser (spec §16.2) and match projection view
(§16.3). Read-only; reads Postgres and the stored refit draws, never odds (docs/
DASHBOARD_TASKS.md). Published numbers come from `market_projection`; the correct-score
matrix and the decomposition are recomputed from the refit that produced the latest milestone
and checked against it (§11.2), so the page cannot show a matrix the projection didn't use."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.stats import poisson
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import SOURCE_ID
from adatta.adapters.storage import RawStore
from adatta.api import schemas as sc
from adatta.db import models
from adatta.model.team_strength import FitData, rates_from_draws
from adatta.sim import serve

Arr = NDArray[np.float64]
TIER = "weekly"  # every bundle refits weekly for now (D11, §12.1)
SHOWN_GOALS = 6  # heatmap cells 0..6 per side; the rest is reported as one mass
COHERENCE_TOL = 1e-9
QUANTILES = serve.INTERVAL

UNAVAILABLE = [
    sc.Unavailable(panel="Squad and lineup",
                   reason="The lineup layer arrives in Phase 3; projections are team-level."),
    sc.Unavailable(panel="Player projections", reason="Needs the lineup layer (Phase 3)."),
    sc.Unavailable(panel="Half-split",
                   reason="Halves are deferred until half markets are needed (§8.2)."),
    sc.Unavailable(panel="Knockout", reason="Only regular-season fixtures are projected so far."),
    sc.Unavailable(panel="Edge",
                   reason="Edge analytics is wave 2 (§16.4); odds never feed the model."),
    sc.Unavailable(panel="Override",
                   reason="The override drawer comes with the §8.5 override mechanisms."),
]  # fmt: skip


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _prob(x: Arr) -> sc.Prob:
    lo, hi = np.quantile(x, QUANTILES)
    return sc.Prob(p=float(x.mean()), lo=float(lo), hi=float(hi))


# --------------------------------------------------------------------------- draws


@dataclass
class Context:
    """What every view needs: bundles, champion name, truncation, and a refit cache."""

    bundles: dict[str, list[int]]
    model_name: str
    max_goals: int
    store: RawStore
    loader: Callable[[RawStore, models.ModelRun], serve.Refit] = serve.load_refit
    _refits: dict[int, serve.Refit] | None = None

    def refit(self, run: models.ModelRun) -> serve.Refit:
        """A refit's draws, loaded once per process (they never change once written)."""
        if self._refits is None:
            self._refits = {}
        if run.model_run_id not in self._refits:
            self._refits[run.model_run_id] = self.loader(self.store, run)
        return self._refits[run.model_run_id]

    @property
    def league_bundle(self) -> dict[int, str]:
        return {lid: b for b, ls in self.bundles.items() for lid in ls}


def horizon_weeks(fd: FitData, kickoff: datetime) -> int:
    """Weeks from the refit's last training step to kickoff, as `serve.project` uses."""
    return max((kickoff - fd.start).days // 7 - (fd.n_steps - 1), 0)


def decompose(d: serve.Draws, fd: FitData, fx: serve.Fixture
              ) -> dict[str, list[tuple[str, Arr]]]:  # fmt: skip
    """Per-draw additive terms of log lambda for each side (§13.1). Mirrors
    `rates_from_draws` term by term; the test holds exp(sum) equal to it per draw."""
    if "delta" in d:
        raise ValueError("D16 shifts are not served; the champion has none")
    tix = {int(t): i for i, t in enumerate(fd.team_ids)}
    lg = {lid: i for i, lid in enumerate(fd.leagues)}[fx.league_id]
    h, a = tix.get(fx.home, -1), tix.get(fx.away, -1)
    att, dfn = d["attack_last"], d["defence_last"]
    h_t = d["h_team"] * d["sigma_home"]
    zeros = np.zeros(att.shape[1])
    recent = fd.step > fd.step.max() - 52
    members = np.unique(np.concatenate([fd.home[recent & (fd.league == lg)],
                                        fd.away[recent & (fd.league == lg)]]))  # fmt: skip
    hz = horizon_weeks(fd, fx.kickoff)

    def team(t: int) -> tuple[Arr, Arr, Arr, Arr]:
        if t < 0:
            return att[members].mean(0), dfn[members].mean(0), zeros, zeros
        form = (np.asarray(d["phi"] ** hz * d["form_last"][t] / np.sqrt(2.0))
                if "form_last" in d else zeros)  # fmt: skip
        return att[t], dfn[t], h_t[t], form

    a_h, d_h, ht, f_h = team(h)
    a_a, d_a, _, f_a = team(a)
    base = np.asarray(d["mu_league"][lg])
    home = [("League baseline", base), ("Home advantage (league)", np.asarray(d["h_league"][lg])),
            ("Home advantage (team)", ht), ("Attack", a_h), ("Opponent defence", -d_a),
            ("Form", f_h - f_a)]  # fmt: skip
    away = [("League baseline", base), ("Attack", a_a), ("Opponent defence", -d_h),
            ("Form", f_a - f_h)]  # fmt: skip
    if "form_last" not in d:  # the model has no form term: leave it out rather than show 0
        home, away = home[:-1], away[:-1]
    return {"home": home, "away": away}


def _term(name: str, x: Arr) -> sc.Term:
    lo, hi = np.quantile(x, QUANTILES)
    return sc.Term(term=name, mean=float(x.mean()), lo=float(lo), hi=float(hi))


@dataclass
class Recomputed:
    matrix: sc.ScoreMatrix
    decomposition: list[sc.Decomposition]
    markets: dict[tuple[str, float | None, str], Arr]


def recompute(refit: serve.Refit, fx: serve.Fixture, names: tuple[str, str], max_goals: int
              ) -> Recomputed:  # fmt: skip
    """Matrix, markets and decomposition of one fixture from one refit's draws, exactly as
    `serve.project` computes the published rows."""
    d, fd = refit.draws, refit.fd
    tix = {int(t): i for i, t in enumerate(fd.team_ids)}
    lg = {lid: i for i, lid in enumerate(fd.leagues)}[fx.league_id]
    lam, mu_ = rates_from_draws(
        d, fd, np.array([tix.get(fx.home, -1)]), np.array([tix.get(fx.away, -1)]),
        np.array([lg]), horizon=np.array([horizon_weeks(fd, fx.kickoff)]),
    )  # fmt: skip
    rho = np.asarray(d["rho"])
    rho_k = rho[lg] if rho.ndim == 2 else rho
    kappa = np.asarray(d["kappa"])[lg] if "kappa" in d else np.zeros_like(lam[0])
    m = serve.score_matrices(lam[0], mu_[0], rho_k, kappa, max_goals)
    k = SHOWN_GOALS + 1
    cells = [[_prob(m[:, i, j]) for j in range(k)] for i in range(k)]
    rest = 1.0 - m[:, :k, :k].sum(axis=(1, 2))
    tail = 1.0 - poisson.cdf(max_goals, lam[0]) * poisson.cdf(max_goals, mu_[0])
    matrix = sc.ScoreMatrix(max_shown=SHOWN_GOALS, cells=cells, rest=_prob(rest),
                            tail_mass=float(tail.mean()), max_goals=max_goals)  # fmt: skip
    parts = decompose(d, fd, fx)
    dec = []
    for side, team, rate, t in (("home", names[0], lam[0], fx.home),
                                ("away", names[1], mu_[0], fx.away)):  # fmt: skip
        terms = parts[side]
        total = np.sum([x for _, x in terms], axis=0)
        dec.append(sc.Decomposition(
            side=side, team=team, cold_start=t not in tix,
            terms=[_term(n, x) for n, x in terms], log_rate=_term("log λ", total),
            goals=_prob(rate),
        ))  # fmt: skip
    return Recomputed(matrix=matrix, decomposition=dec, markets=serve.market_draws(m))


# --------------------------------------------------------------------------- queries


@dataclass
class MatchRow:
    match_id: int
    kickoff: datetime
    status: str
    round: str | None
    league_id: int
    competition_id: int
    competition: str
    country: str
    level: str
    season_id: int
    home_id: int
    away_id: int
    home: str
    away: str
    team_level: str
    venue_id: int | None


def _matches(session: Session, ctx: Context, where: list[Any]) -> list[MatchRow]:
    M, S, C, T = models.Match, models.Season, models.Competition, models.Team
    H, A = T.__table__.alias("h"), T.__table__.alias("a")
    leagues = [str(x) for x in ctx.league_bundle]
    rows = session.execute(
        select(M.match_id, M.kickoff_time, M.status, M.round, C.source_ref, C.competition_id,
               C.name, C.country, C.level, M.season_id, M.home_team_id, M.away_team_id,
               H.c.name, A.c.name, H.c.team_level, M.venue_id)
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .join(H, H.c.team_id == M.home_team_id)
        .join(A, A.c.team_id == M.away_team_id)
        .where(C.source_id == SOURCE_ID, C.source_ref.in_(leagues), *where)
        .order_by(M.kickoff_time, M.match_id)
    ).all()  # fmt: skip
    return [MatchRow(match_id=r[0], kickoff=_utc(r[1]), status=r[2], round=r[3],
                     league_id=int(r[4]), competition_id=r[5], competition=r[6], country=r[7],
                     level=r[8], season_id=r[9], home_id=r[10], away_id=r[11], home=r[12],
                     away=r[13], team_level=r[14], venue_id=r[15]) for r in rows]  # fmt: skip


def _projections(session: Session, ids: list[int]
                 ) -> dict[int, dict[str, list[models.MarketProjection]]]:  # fmt: skip
    MP = models.MarketProjection
    out: dict[int, dict[str, list[models.MarketProjection]]] = {}
    for i in range(0, len(ids), 500):
        for r in session.scalars(select(MP).where(MP.match_id.in_(ids[i : i + 500]))):
            out.setdefault(r.match_id, {}).setdefault(r.milestone, []).append(r)
    return out


def _latest_milestone(by_ms: dict[str, list[models.MarketProjection]]) -> str:
    return max(by_ms, key=lambda ms: serve.MILESTONES.index(ms))


def _p(r: models.MarketProjection) -> sc.Prob:
    lo, hi = r.interval_lo, r.interval_hi
    if lo is None or hi is None:
        raise ValueError(f"projection {r.projection_id} has no interval")  # never shown bare
    return sc.Prob(p=r.probability, lo=lo, hi=hi)


def _headline(ms: str, rows: list[models.MarketProjection]) -> sc.Headline:
    x = {r.selection: _p(r) for r in rows if r.market_type == "1X2"}
    return sc.Headline(milestone=ms, home=x["home"], draw=x["draw"], away=x["away"])


def _capabilities(session: Session, season_ids: set[int]
                  ) -> dict[int, list[models.CompetitionSourceCapability]]:  # fmt: skip
    CSC = models.CompetitionSourceCapability
    out: dict[int, list[models.CompetitionSourceCapability]] = {s: [] for s in season_ids}
    for r in session.scalars(select(CSC).where(CSC.season_id.in_(season_ids),
                                               CSC.valid_to.is_(None))
                             .order_by(CSC.capability)):  # fmt: skip
        out[r.season_id].append(r)
    return out


def _cap_view(rows: list[models.CompetitionSourceCapability]) -> list[sc.Capability]:
    return [sc.Capability(capability=r.capability, status=r.status, coverage=r.coverage_ratio)
            for r in rows]  # fmt: skip


def _champion(session: Session, model_name: str) -> models.ModelRun | None:
    MR = models.ModelRun
    return session.scalars(
        select(MR).where(MR.model_name == model_name, MR.status == "champion")
        .order_by(MR.model_run_id.desc())
    ).first()  # fmt: skip


def _freshness(bundle: str, run: models.ModelRun | None, now: datetime) -> sc.Freshness:
    if run is None or run.as_of is None:
        return sc.Freshness(tier=TIER, bundle=bundle, model_run_id=None, refit_as_of=None,
                            age_days=None, gates_pass=None, stale=True)  # fmt: skip
    as_of = _utc(run.as_of)
    age = (now - as_of) / timedelta(days=1)
    return sc.Freshness(tier=TIER, bundle=bundle, model_run_id=run.model_run_id,
                        refit_as_of=as_of, age_days=round(age, 2), gates_pass=run.status == "ok",
                        stale=now - as_of > serve.STALE_AFTER)  # fmt: skip


# --------------------------------------------------------------------------- confidence


def confidence(m: MatchRow, refit: serve.Refit | None, fresh: sc.Freshness,
               caps: list[models.CompetitionSourceCapability],
               champion: models.ModelRun | None, now: datetime) -> list[sc.Component]:  # fmt: skip
    """The six named components of §13.2, each with a plain status. No combined score:
    §13.2 requires the combining formula to be backtested first."""
    out = []
    if refit is None:
        out.append(sc.Component(key="history", label="Historical data strength",
                                status="missing", value="no refit",
                                detail="No passing refit covers this competition yet.",
                                ))  # fmt: skip
    else:
        fd = refit.fd
        tix = {int(t): i for i, t in enumerate(fd.team_ids)}
        counts = np.bincount(np.concatenate([fd.home, fd.away]), minlength=len(fd.team_ids))
        parts, cold = [], []
        for name, t in ((m.home, m.home_id), (m.away, m.away_id)):
            if t in tix:
                parts.append(f"{name} {int(counts[tix[t]])}")
            else:
                parts.append(f"{name} 0")
                cold.append(name)
        out.append(sc.Component(
            key="history", label="Historical data strength",
            status="limited" if cold else "ok", value=" · ".join(parts) + " matches",
            detail=(f"Cold start: {', '.join(cold)} not in the training window; rated as the "
                    "league average of the last 52 weeks." if cold else
                    f"Training matches in the refit window as of {refit.as_of:%Y-%m-%d}."),
        ))  # fmt: skip
    xg = next((c for c in caps if c.capability == "has_xg"), None)
    xg_status = xg.status if xg else "unknown"
    cov = f" ({xg.coverage_ratio:.0%})" if xg and xg.coverage_ratio is not None else ""
    out.append(sc.Component(
        key="capability", label="Source capability",
        status="ok" if xg_status == "available" else "limited",
        value=f"results + xG {xg_status}{cov}",
        detail="The served model uses results everywhere and xG where the source has it; "
               "without xG it runs on results alone.",
    ))  # fmt: skip
    out.append(sc.Component(key="lineup", label="Lineup certainty", status="not_modelled",
                            value="team-level",
                            detail="No lineup input yet (Phase 3): confirmed lineups do not "
                                   "change these projections."))  # fmt: skip
    out.append(sc.Component(key="cross_league", label="Cross-league prior dependence",
                            status="not_modelled", value=f"within {fresh.bundle} bundle",
                            detail="No cross-league layer yet (§10.1); both teams are rated "
                                   "within their own top-flight + second-tier bundle.",
                            ))  # fmt: skip
    if champion is None:
        out.append(sc.Component(key="stability", label="Model stability", status="missing",
                                value="no champion", detail="Nothing is promoted."))  # fmt: skip
    else:
        since = (now - _utc(champion.created_at)) / timedelta(days=1)
        ok = bool(fresh.gates_pass)
        out.append(sc.Component(
            key="stability", label="Model stability", status="ok" if ok else "limited",
            value=f"champion {since:.0f} d · gates {'pass' if ok else 'fail'}",
            detail=f"{champion.model_name} promoted {champion.created_at:%Y-%m-%d}; the serving "
                   "refit passes R-hat, ESS and divergence gates." if ok else
                   "The serving refit did not pass its inference gates.",
        ))  # fmt: skip
    audited = max((_utc(c.last_audited_at) for c in caps), default=None)
    if fresh.refit_as_of is None:
        out.append(sc.Component(key="freshness", label="Data freshness", status="missing",
                                value="no refit", detail="No passing refit yet."))  # fmt: skip
    else:
        out.append(sc.Component(
            key="freshness", label="Data freshness",
            status="stale" if fresh.stale else "ok",
            value=f"refit {fresh.age_days:.1f} d old",
            detail=f"Weekly refit as of {fresh.refit_as_of:%Y-%m-%d}"
                   + (f"; capabilities audited {audited:%Y-%m-%d}." if audited else
                      "; capabilities never audited."),
        ))  # fmt: skip
    return out


# --------------------------------------------------------------------------- views


def _serving(session: Session, ctx: Context, now: datetime) -> dict[str, models.ModelRun]:
    runs, _ = serve.latest_refits(session, now, ctx.model_name)
    return runs


def fixture_list(session: Session, ctx: Context, now: datetime, start: datetime | None = None,
                 end: datetime | None = None, competition_id: int | None = None,
                 team_level: str | None = None) -> sc.FixtureList:  # fmt: skip
    start = start or now
    end = end or now + serve.HORIZON
    M, C = models.Match, models.Competition
    where: list[Any] = [M.kickoff_time >= start, M.kickoff_time <= end]
    if competition_id is not None:
        where.append(C.competition_id == competition_id)
    rows = [r for r in _matches(session, ctx, where)
            if (r.round or "").startswith("Regular Season") and r.status not in serve.NO_MATCH
            and (team_level is None or r.team_level == team_level)]  # fmt: skip
    proj = _projections(session, [r.match_id for r in rows])
    caps = _capabilities(session, {r.season_id for r in rows})
    runs = _serving(session, ctx, now)
    champ = _champion(session, ctx.model_name)
    lb = ctx.league_bundle
    groups: dict[int, sc.CompetitionGroup] = {}
    for r in rows:
        bundle = lb[r.league_id]
        run = runs.get(bundle)
        fresh = _freshness(bundle, run, now)
        g = groups.get(r.competition_id)
        if g is None:
            g = groups[r.competition_id] = sc.CompetitionGroup(
                competition_id=r.competition_id, name=r.competition, country=r.country,
                level=r.level, capabilities=_cap_view(caps[r.season_id]), freshness=fresh,
                fixtures=[],
            )  # fmt: skip
        by_ms = proj.get(r.match_id)
        reason = None
        if not by_ms:
            reason = ("No passing refit for this bundle yet." if run is None else
                      "Not projected yet: the next capture tick writes it.")  # fmt: skip
        g.fixtures.append(sc.FixtureRow(
            match_id=r.match_id, kickoff=r.kickoff, round=r.round, home=r.home, away=r.away,
            team_level=r.team_level,
            headline=_headline(ms := _latest_milestone(by_ms), by_ms[ms]) if by_ms else None,
            no_projection_reason=reason,
            confidence=confidence(r, ctx.refit(run) if run else None, fresh, caps[r.season_id],
                                  champ, now),
        ))  # fmt: skip
    order = sorted(groups.values(), key=lambda g: (g.country, g.name))
    return sc.FixtureList(generated_at=now, start=start, end=end, competitions=order)


def _kickoff_then(session: Session, m: MatchRow, at: datetime) -> datetime:
    """Kickoff as known when a projection was generated: `serve.project` used that one."""
    FS = models.FixtureSnapshot
    t = session.scalars(
        select(FS.kickoff_time).where(FS.match_id == m.match_id, FS.observed_at <= at)
        .order_by(FS.observed_at.desc()).limit(1)
    ).first()  # fmt: skip
    return _utc(t) if t is not None else m.kickoff


def match_view(session: Session, ctx: Context, match_id: int, now: datetime) -> sc.MatchView:
    rows = _matches(session, ctx, [models.Match.match_id == match_id])
    if not rows:
        raise KeyError(match_id)
    m = rows[0]
    bundle = ctx.league_bundle[m.league_id]
    caps = _capabilities(session, {m.season_id})[m.season_id]
    venue = session.get(models.Venue, m.venue_id) if m.venue_id is not None else None
    WS, FS = models.WeatherSnapshot, models.FixtureSnapshot
    ws = session.scalars(select(WS).where(WS.match_id == match_id, WS.observed_at <= now)
                         .order_by(WS.observed_at.desc()).limit(1)).first()  # fmt: skip
    referee = session.scalars(select(FS.referee).where(FS.match_id == match_id,
                                                       FS.observed_at <= now)
                              .order_by(FS.observed_at.desc()).limit(1)).first()  # fmt: skip
    header = sc.Header(
        match_id=m.match_id, competition=m.competition, competition_id=m.competition_id,
        round=m.round, kickoff=m.kickoff, status=m.status, home=m.home, away=m.away,
        venue=f"{venue.name}, {venue.city}" if venue else None,
        weather=sc.Weather(observed_at=_utc(ws.observed_at), lead_hours=ws.lead_hours,
                           temperature_c=ws.temperature_c, precipitation_mm=ws.precipitation_mm,
                           wind_speed_kmh=ws.wind_speed_kmh) if ws else None,
        referee=referee, capabilities=_cap_view(caps),
    )  # fmt: skip
    by_ms = _projections(session, [match_id]).get(match_id, {})
    runs = _serving(session, ctx, now)
    champ = _champion(session, ctx.model_name)
    milestones: list[sc.Milestone] = []
    for ms in sorted(by_ms, key=lambda x: serve.MILESTONES.index(x)):
        pr = session.get(models.ProjectionRun, by_ms[ms][0].projection_run_id)
        assert pr is not None
        cv = pr.component_versions.get(bundle, {})
        milestones.append(sc.Milestone(
            milestone=ms, generated_at=_utc(pr.generated_at),
            projection_run_id=pr.projection_run_id, model_run_id=cv.get("model_run_id"),
            refit_as_of=datetime.fromisoformat(cv["as_of"]) if "as_of" in cv else None,
            selections=[sc.Selection(market=r.market_type, line=r.line, selection=r.selection,
                                     prob=_p(r)) for r in by_ms[ms]],
        ))  # fmt: skip
    # the refit that made the latest milestone; the current serving refit if none yet
    run = None
    if milestones and milestones[-1].model_run_id is not None:
        run = session.get(models.ModelRun, milestones[-1].model_run_id)
    serving = runs.get(bundle)
    fresh = _freshness(bundle, run or serving, now)
    refit = ctx.refit(run) if run else None
    matrix, dec, coh, reason = None, [], None, None
    if refit is not None and milestones:
        last = milestones[-1]
        fx = serve.Fixture(m.match_id, _kickoff_then(session, m, last.generated_at),
                           m.league_id, m.home_id, m.away_id)  # fmt: skip
        rc = recompute(refit, fx, (m.home, m.away), ctx.max_goals)
        matrix, dec = rc.matrix, rc.decomposition
        diff = max(abs(float(rc.markets[(s.market, s.line, s.selection)].mean()) - s.prob.p)
                   for s in last.selections)  # fmt: skip
        coh = sc.Coherence(milestone=last.milestone, coherent=diff <= COHERENCE_TOL,
                           max_abs_diff=diff)  # fmt: skip
    else:
        reason = ("No passing refit for this bundle yet." if serving is None else
                  "Not projected yet: the next capture tick writes it.")  # fmt: skip
    return sc.MatchView(
        generated_at=now, header=header, freshness=fresh,
        confidence=confidence(m, refit or (ctx.refit(serving) if serving else None), fresh,
                              caps, champ, now),
        # an incoherent recomputation is never shown (§11.2); the coherence flag says why
        milestones=milestones, matrix=matrix if coh is None or coh.coherent else None,
        decomposition=dec if coh is None or coh.coherent else [], coherence=coh,
        no_projection_reason=reason,
        unavailable=UNAVAILABLE,
    )  # fmt: skip


# --------------------------------------------------------------------------- played view


def _pre_kickoff_forecasts(
    session: Session, kickoffs: dict[int, datetime]
) -> dict[int, tuple[str, datetime, list[models.MarketProjection]]]:
    """Per match: the latest milestone whose projection run was generated before kickoff,
    its generation time and rows. A run generated at or after kickoff is never used."""
    MP, PR = models.MarketProjection, models.ProjectionRun
    found: dict[int, dict[str, tuple[datetime, list[models.MarketProjection]]]] = {}
    ids = list(kickoffs)
    for i in range(0, len(ids), 500):
        for r, gen in session.execute(
            select(MP, PR.generated_at)
            .join(PR, PR.projection_run_id == MP.projection_run_id)
            .where(MP.match_id.in_(ids[i : i + 500]))
        ):
            gen = _utc(gen)
            if gen >= kickoffs[r.match_id]:
                continue
            slot = found.setdefault(r.match_id, {})
            t, rows = slot.get(r.milestone, (gen, []))
            if gen > t:  # a newer run of the same milestone replaces the older one
                t, rows = gen, []
            if gen == t:
                rows.append(r)
            slot[r.milestone] = (t, rows)
    out = {}
    for mid, slot in found.items():
        ms = max(slot, key=lambda m: serve.MILESTONES.index(m))
        out[mid] = (ms, *slot[ms])
    return out


def _results(session: Session, ids: list[int]) -> dict[int, tuple[int, int]]:
    R = models.MatchResult
    out: dict[int, tuple[int, int]] = {}
    for i in range(0, len(ids), 500):
        for mid, hg, ag in session.execute(
            select(R.match_id, R.home_goals_nt, R.away_goals_nt).where(
                R.match_id.in_(ids[i : i + 500]), R.superseded_at.is_(None))
        ):  # fmt: skip
            if hg is not None and ag is not None:
                out[mid] = (int(hg), int(ag))
    return out


def _mean_score(metric: str, xs: list[float]) -> sc.MeanScore:
    a = np.asarray(xs, dtype=float)
    se = float(a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 1 else float("nan")
    m = float(a.mean())
    return sc.MeanScore(metric=metric, n=len(a), mean=m, lo=m - 1.96 * se, hi=m + 1.96 * se)


PLAYED_METRICS = ("log_loss_1x2", "rps_1x2", "brier_1x2", "log_loss_ou25", "log_loss_btts")


def _summary(segment: str, cid: int | None, ms: list[sc.PlayedMatch]) -> sc.ScoreSummary:
    scores = []
    for metric in PLAYED_METRICS:
        xs = [v for m in ms if (v := getattr(m.scores, metric)) is not None]
        if xs:
            scores.append(_mean_score(metric, xs))
    return sc.ScoreSummary(segment=segment, competition_id=cid, n=len(ms), scores=scores)


def played(
    session: Session,
    ctx: Context,
    now: datetime,
    start: datetime | None = None,
    end: datetime | None = None,
    competition_id: int | None = None,
) -> sc.PlayedList:
    """Finished bundle league matches against the forecast published before their kickoff
    (docs/DASHBOARD_TASKS.md, played view)."""
    from adatta.adapters.normalize import FINISHED
    from adatta.eval import metrics

    end = end or now
    PR, M, C = models.ProjectionRun, models.Match, models.Competition
    first = session.scalar(select(PR.generated_at).order_by(PR.generated_at).limit(1))
    since = _utc(first) if first is not None else None
    lo = max(t for t in (start, since) if t is not None) if (start or since) else None
    where: list[Any] = [M.status.in_(FINISHED), M.kickoff_time <= end]
    if lo is not None:
        where.append(M.kickoff_time >= lo)
    if competition_id is not None:
        where.append(C.competition_id == competition_id)
    rows = [r for r in _matches(session, ctx, where)
            if (r.round or "").startswith("Regular Season")] if since else []  # fmt: skip
    results = _results(session, [r.match_id for r in rows])
    forecasts = _pre_kickoff_forecasts(
        session, {r.match_id: r.kickoff for r in rows if r.match_id in results}
    )
    out: list[sc.PlayedMatch] = []
    for r in rows:
        if r.match_id not in forecasts or r.match_id not in results:
            continue
        ms, gen, prs = forecasts[r.match_id]
        sel = {(p.market_type, p.line, p.selection): p for p in prs}
        hg, ag = results[r.match_id]
        x = [sel[("1X2", None, s)] for s in ("home", "draw", "away")]
        p1x2 = np.array([p.probability for p in x])
        k = metrics.outcomes(hg, ag)
        over, btts = sel.get(("OU", 2.5, "over")), sel.get(("BTTS", None, "yes"))
        scores = sc.PlayedScores(
            log_loss_1x2=metrics.log_loss(p1x2, k["1X2"]), rps_1x2=metrics.rps(p1x2, k["1X2"]),
            brier_1x2=metrics.brier(p1x2, k["1X2"]),
            log_loss_ou25=None if over is None else metrics.log_loss(
                np.array([over.probability, 1 - over.probability]), k["OU2.5"]),
            log_loss_btts=None if btts is None else metrics.log_loss(
                np.array([btts.probability, 1 - btts.probability]), k["BTTS"]),
        )  # fmt: skip
        out.append(sc.PlayedMatch(
            match_id=r.match_id, kickoff=r.kickoff, competition_id=r.competition_id,
            competition=r.competition, home=r.home, away=r.away, home_goals=hg, away_goals=ag,
            outcome=("home", "draw", "away")[k["1X2"]], milestone=ms, generated_at=gen,
            home_p=_p(x[0]), draw_p=_p(x[1]), away_p=_p(x[2]),
            over25=None if over is None else _p(over), btts=None if btts is None else _p(btts),
            scores=scores,
        ))  # fmt: skip
    out.sort(key=lambda m: (m.kickoff, m.match_id), reverse=True)
    summary = [_summary("all", None, out)] if out else []
    comps: dict[int, list[sc.PlayedMatch]] = {}
    for m in out:
        comps.setdefault(m.competition_id, []).append(m)
    summary += [
        _summary(v[0].competition, cid, v)
        for cid, v in sorted(comps.items(), key=lambda kv: kv[1][0].competition)
    ]
    return sc.PlayedList(
        generated_at=now,
        forecasts_since=since,
        start=lo,
        end=end,
        uniform_log_loss_1x2=float(np.log(3)),
        summary=summary,
        matches=out,
    )
