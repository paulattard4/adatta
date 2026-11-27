"""Read model behind the dashboard: fixture browser (spec §16.2) and match projection view
(§16.3). Read-only; reads Postgres and the stored refit draws, never odds (docs/
DASHBOARD_TASKS.md). Published numbers come from `market_projection`; the correct-score
matrix and the decomposition are recomputed from the refit that produced the latest milestone
and checked against it (§11.2), so the page cannot show a matrix the projection didn't use."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.stats import poisson
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import SOURCE_ID
from adatta.adapters.storage import RawStore
from adatta.api import lineups as al
from adatta.api import schemas as sc
from adatta.db import models
from adatta.model.team_strength import FitData, rates_from_draws
from adatta.sim import half_serve as hsv
from adatta.sim import serve

Arr = NDArray[np.float64]
TIER = "weekly"  # every bundle refits weekly for now (D11, §12.1)
SHOWN_GOALS = 6  # heatmap cells 0..6 per side; the rest is reported as one mass
COHERENCE_TOL = 1e-9
QUANTILES = serve.INTERVAL

UNAVAILABLE = [
    sc.Unavailable(panel="Player projections",
                   reason="The lineup offset failed on dev (D40); scorer forecasts (P3-e) are "
                          "not shown before their live verdict."),
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
    _national: dict[int, dict[str, Any]] | None = None
    _shares: dict[int, hsv.Share] | None = None

    def share(self, run: models.ModelRun) -> hsv.Share:
        """A production share refit's draws (P3b-2), loaded once per process."""
        if self._shares is None:
            self._shares = {}
        if run.model_run_id not in self._shares:
            self._shares[run.model_run_id] = hsv.load_share(self.store, run)
        return self._shares[run.model_run_id]

    def national_draws(self, run: models.ModelRun) -> dict[str, Any]:
        """A national live refit's stored draws, loaded once per process."""
        from adatta.model import national_live

        if self._national is None:
            self._national = {}
        if run.model_run_id not in self._national:
            self._national[run.model_run_id] = national_live.load(self.store, run)
        return self._national[run.model_run_id]

    def refit(self, run: models.ModelRun) -> serve.Refit:
        """A refit's draws, loaded once per process (they never change once written)."""
        if self._refits is None:
            self._refits = {}
        if run.model_run_id not in self._refits:
            self._refits[run.model_run_id] = self.loader(self.store, run)
        return self._refits[run.model_run_id]

    @property
    def league_bundle(self) -> dict[int, str]:
        """Provider id -> bundle, for the bundle leagues and their cups (Stage A's 90' cup
        forecasts are served from the bundle's refit)."""
        out = {lid: b for b, ls in self.bundles.items() for lid in ls}
        return out | {c: b for c, b in self.cups.items() if b in self.bundles}

    @property
    def cups(self) -> dict[int, str]:
        from adatta.features import cup_ties

        return cup_ties.cup_bundles()


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
    draws: Arr  # (draws, n, n) score matrices, for the half split (P3b-2)


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
    return Recomputed(matrix=matrix, decomposition=dec, markets=serve.market_draws(m), draws=m)


def _half(session: Session, ctx: Context, m: MatchRow, fx: serve.Fixture, rc: Recomputed,
          last: sc.Milestone) -> tuple[sc.Coherence | None, str | None]:  # fmt: skip
    """P3b-2 (d): the half markets of the latest milestone recomputed from its Layer 2 refit
    and share refit and checked against the stored rows; or why there are none."""
    stored = [s for s in last.selections if s.market in hsv.MARKETS]
    if not stored:
        if m.tier == "cup" or not (m.round or "").startswith("Regular Season"):
            return None, "Half-time markets are forecast for league matches only."
        season = session.get(models.Season, m.season_id)
        year = season.year if season is not None else None
        if year is None or not hsv.in_scope(session, {(m.league_id, year)}):
            return None, "No half-time data for this league-season, so no half-time forecast."
        return None, ("No half-time forecast at this milestone: there was no usable share "
                      "refit, or it was made before half-time markets were served.")  # fmt: skip
    pr = session.get(models.ProjectionRun, last.projection_run_id)
    cv = (pr.component_versions or {}).get(serve.HALF_KEY, {}) if pr is not None else {}
    run = session.get(models.ModelRun, cv["model_run_id"]) if "model_run_id" in cv else None
    if run is None:
        return sc.Coherence(milestone=last.milestone, coherent=False,
                            max_abs_diff=1.0), None  # no share refit recorded  # fmt: skip
    share = ctx.share(run)
    ph, pa = share.sides(fx.league_id, fx.home, fx.away)
    n = len(rc.draws)
    half = hsv.draw_markets(rc.draws, hsv.pair(ph, n), hsv.pair(pa, n))
    diff = max(abs(float(half[(s.market, s.line, s.selection)].mean()) - s.prob.p)
               if (s.market, s.line, s.selection) in half else 1.0
               for s in stored)  # fmt: skip
    return sc.Coherence(milestone=last.milestone, coherent=diff <= COHERENCE_TOL,
                        max_abs_diff=diff), None  # fmt: skip


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
    tier: str  # competition tier: "1", "2", "cup", "national" (config/competitions.yml)
    source_ref: str  # the provider's fixture id
    season_id: int
    home_id: int
    away_id: int
    home: str
    away: str
    team_level: str
    venue_id: int | None


def competition_order(country: str, tier: str, name: str) -> tuple[str, int, int, str]:
    """Lists of competitions: by country, then numbered tiers in order (1 before 2), then
    cups, then national-team competitions, then anything else; name breaks ties."""
    if tier.isdigit():
        return (country, 0, int(tier), name)
    return (country, {"cup": 1, "national": 2}.get(tier, 3), 0, name)


def _matches(session: Session, ctx: Context, where: list[Any], national: bool = False,
             leagues: list[int] | None = None) -> list[MatchRow]:  # fmt: skip
    """The bundle leagues' (and their cups') matches, with `national` those of the
    national-team competitions (Stage D), with `leagues` those leagues' (C2, D54)."""
    M, S, C, T = models.Match, models.Season, models.Competition, models.Team
    H, A = T.__table__.alias("h"), T.__table__.alias("a")
    refs = [str(x) for x in (ctx.league_bundle if leagues is None else leagues)]
    scope = C.is_national.is_(True) if national else C.source_ref.in_(refs)
    rows = session.execute(
        select(M.match_id, M.kickoff_time, M.status, M.round, C.source_ref, C.competition_id,
               C.name, C.country, C.level, M.season_id, M.home_team_id, M.away_team_id,
               H.c.name, A.c.name, H.c.team_level, M.venue_id, C.tier, M.source_ref)
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .join(H, H.c.team_id == M.home_team_id)
        .join(A, A.c.team_id == M.away_team_id)
        .where(C.source_id == SOURCE_ID, scope, *where)
        .order_by(M.kickoff_time, M.match_id)
    ).all()  # fmt: skip
    return [MatchRow(match_id=r[0], kickoff=_utc(r[1]), status=r[2], round=r[3],
                     league_id=int(r[4]), competition_id=r[5], competition=r[6], country=r[7],
                     level=r[8], season_id=r[9], home_id=r[10], away_id=r[11], home=r[12],
                     away=r[13], team_level=r[14], venue_id=r[15], tier=r[16],
                     source_ref=r[17])
            for r in rows]  # fmt: skip


def _cup_reasons(session: Session, ctx: Context, ids: list[int]) -> dict[int, str]:
    """Why a cup tie is not forecast (a side outside the bundle, a swapped venue)."""
    if not ids:
        return {}
    return {t.match_id: t.reason for t in serve.cup_ties(session, ctx.cups, ctx.bundles,
                                                         match_ids=ids) if t.reason}  # fmt: skip


def _projections(session: Session, ids: list[int]
                 ) -> dict[int, dict[str, list[models.MarketProjection]]]:  # fmt: skip
    MP = models.MarketProjection
    out: dict[int, dict[str, list[models.MarketProjection]]] = {}
    for i in range(0, len(ids), 500):
        for r in session.scalars(select(MP).where(MP.match_id.in_(ids[i : i + 500]))):
            out.setdefault(r.match_id, {}).setdefault(r.milestone, []).append(r)
    return out


def _published_order(by_ms: dict[str, list[models.MarketProjection]]) -> list[str]:
    """Milestones in the order they were published (D41: a refit milestone can fall between
    the timed ones). Each (match, milestone) is written by one projection run, and run ids
    increase with time."""
    return sorted(by_ms, key=lambda ms: min(r.projection_run_id for r in by_ms[ms]))


def _latest_milestone(by_ms: dict[str, list[models.MarketProjection]]) -> str:
    return _published_order(by_ms)[-1]


def _run_behind(session: Session, rows: list[models.MarketProjection], bundle: str,
                cache: dict[int, models.ModelRun | None]) -> models.ModelRun | None:  # fmt: skip
    """The refit that produced a milestone's rows (its projection run's model_run_id)."""
    pr = session.get(models.ProjectionRun, rows[0].projection_run_id)
    mid = (pr.component_versions or {}).get(bundle, {}).get("model_run_id") if pr else None
    if mid is None:
        return None
    if mid not in cache:
        cache[mid] = session.get(models.ModelRun, mid)
    return cache[mid]


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

# One checklist for every competition (owner, 2026-11-27): the same components in the same
# order, differing only in status. "Model" first, then the six of §13.2.
CHECKLIST = ("model", "history", "capability", "lineup", "cross_league", "stability",
             "freshness")  # fmt: skip


def checklist(parts: list[sc.Component]) -> list[sc.Component]:
    """The components in CHECKLIST order; refuses a missing, extra or repeated one."""
    by = {c.key: c for c in parts}
    if len(by) != len(parts) or set(by) != set(CHECKLIST):
        raise ValueError(f"confidence components {[c.key for c in parts]} != {CHECKLIST}")
    return [by[k] for k in CHECKLIST]


def capability_component(caps: list[models.CompetitionSourceCapability]) -> sc.Component:
    """Source capability: what the source has for this competition and season (the audit),
    the same for every competition. Which of it a model uses is the Model component's."""
    if not caps:
        return sc.Component(key="capability", label="Source capability", status="missing",
                            value="not audited",
                            detail="This competition's data for the season has not been "
                                   "audited yet.")  # fmt: skip
    xg = next((c for c in caps if c.capability == "has_xg"), None)
    xg_status = xg.status if xg else "unknown"
    cov = f" ({xg.coverage_ratio:.0%})" if xg and xg.coverage_ratio is not None else ""
    audited = max(_utc(c.last_audited_at) for c in caps)
    return sc.Component(
        key="capability", label="Source capability",
        status="ok" if xg_status == "available" else "limited",
        value=f"results + xG {xg_status}{cov}",
        detail=f"What the source has for this competition and season (audited "
               f"{audited:%Y-%m-%d}). Which of it the served model uses is under Model.",
    )  # fmt: skip


def confidence(m: MatchRow, refit: serve.Refit | None, fresh: sc.Freshness,
               caps: list[models.CompetitionSourceCapability],
               champion: models.ModelRun | None, now: datetime,
               newer: models.ModelRun | None = None) -> list[sc.Component]:  # fmt: skip
    """The six named components of §13.2, each with a plain status. No combined score:
    §13.2 requires the combining formula to be backtested first."""
    out = []
    if champion is None:
        out.append(sc.Component(key="model", label="Model", status="missing",
                                value="no champion", detail="Nothing is promoted."))  # fmt: skip
    elif m.tier == "cup":
        out.append(sc.Component(
            key="model", label="Model", status="limited", value="league model, 90' cup tie",
            detail="The bundle's league model applied unchanged to the 90 minutes (Stage A, "
                   "passed on the cup holdout): no lineups, no rotation term, and no chance "
                   "of going through (Stage B was not promoted).",
        ))  # fmt: skip
    else:
        out.append(sc.Component(
            key="model", label="Model", status="ok", value="full model · results + xG",
            detail=f"{champion.model_name}: the production team model (Phase 1b), posterior "
                   "draws with 90% intervals; it uses results, and xG where the source has "
                   "it.",
        ))  # fmt: skip
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
    out.append(capability_component(caps))
    out.append(sc.Component(key="lineup", label="Lineup certainty", status="not_modelled",
                            value="team-level",
                            detail="The score forecasts don't use lineups: the lineup offset "
                                   "failed on dev (D40). Projected and announced lineups are "
                                   "shown beside the forecast, not in it."))  # fmt: skip
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
            detail=f"Forecast from the weekly refit as of {fresh.refit_as_of:%Y-%m-%d}"
                   + (f"; a newer refit (as of {_utc(newer.as_of):%Y-%m-%d}) has passed and is "
                      "used from the next capture tick" if newer and newer.as_of else "")
                   + (f"; capabilities audited {audited:%Y-%m-%d}." if audited else
                      "; capabilities never audited."),
        ))  # fmt: skip
    return checklist(out)


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
            if ((r.round or "").startswith("Regular Season") or r.tier == "cup")
            and r.status not in serve.NO_MATCH
            and (team_level is None or r.team_level == team_level)]  # fmt: skip
    proj = _projections(session, [r.match_id for r in rows])
    cup_why = _cup_reasons(session, ctx, [r.match_id for r in rows if r.tier == "cup"])
    caps = _capabilities(session, {r.season_id for r in rows})
    runs = _serving(session, ctx, now)
    champ = _champion(session, ctx.model_name)
    lb = ctx.league_bundle
    groups: dict[int, sc.CompetitionGroup] = {}
    behind: dict[int, models.ModelRun | None] = {}
    for r in rows:
        bundle = lb[r.league_id]
        run = runs.get(bundle)
        fresh = _freshness(bundle, run, now)
        g = groups.get(r.competition_id)
        if g is None:
            g = groups[r.competition_id] = sc.CompetitionGroup(
                competition_id=r.competition_id, name=r.competition, country=r.country,
                level=r.level, tier=r.tier, capabilities=_cap_view(caps[r.season_id]),
                freshness=fresh, fixtures=[],
            )  # fmt: skip
        by_ms = proj.get(r.match_id)
        reason = None
        if not by_ms:
            reason = cup_why.get(r.match_id) or (
                "No passing refit for this bundle yet." if run is None else
                "Not projected yet: the next capture tick writes it.")  # fmt: skip
        # the row's freshness is that of the refit behind its forecast (the serving one if none)
        src = _run_behind(session, by_ms[_latest_milestone(by_ms)], bundle, behind) if by_ms \
            else None  # fmt: skip
        row_run = src or run
        newer = run if (src and run and run.as_of and src.as_of
                        and _utc(run.as_of) > _utc(src.as_of)) else None  # fmt: skip
        g.fixtures.append(sc.FixtureRow(
            match_id=r.match_id, kickoff=r.kickoff, round=r.round, home=r.home, away=r.away,
            team_level=r.team_level,
            headline=_headline(ms := _latest_milestone(by_ms), by_ms[ms]) if by_ms else None,
            no_projection_reason=reason,
            confidence=confidence(r, ctx.refit(row_run) if row_run else None,
                                  _freshness(bundle, row_run, now), caps[r.season_id], champ,
                                  now, newer),
        ))  # fmt: skip
    from adatta.api import c2, national

    every = [*groups.values(), *national.groups(session, ctx, now, start, end, competition_id,
                                                 team_level),
             *c2.groups(session, ctx, now, start, end, competition_id, team_level)]  # fmt: skip
    order = sorted(every, key=lambda g: competition_order(g.country, g.tier, g.name))
    lines = _market_lines(session, {f.match_id: f.kickoff for g in order for f in g.fixtures
                                    if f.headline is not None},
                          now)  # fmt: skip
    for g in order:
        for f in g.fixtures:
            f.market = lines.get(f.match_id)
    return sc.FixtureList(generated_at=now, start=start, end=end, competitions=order)


def _market_lines(session: Session, upcoming: dict[int, datetime], now: datetime
                  ) -> dict[int, sc.MarketLine]:  # fmt: skip
    """The bookmakers' latest de-margined prices for upcoming forecasts {match: kickoff},
    shown beside ours for information only (owner, 2026-11-27): no edge flag until an edge
    test is written and passed."""
    from adatta.api.market import market_view, upcoming_market

    view = market_view(session, {mid: min(now, _utc(k)) for mid, k in upcoming.items()
                                 if _utc(k) > now})  # fmt: skip
    return {mid: line for mid in view if (line := upcoming_market(view[mid])) is not None}


def _kickoff_then(session: Session, m: MatchRow, at: datetime) -> datetime:
    """Kickoff as known when a projection was generated: `serve.project` used that one."""
    FS = models.FixtureSnapshot
    t = session.scalars(
        select(FS.kickoff_time).where(FS.match_id == m.match_id, FS.observed_at <= at)
        .order_by(FS.observed_at.desc()).limit(1)
    ).first()  # fmt: skip
    return _utc(t) if t is not None else m.kickoff


def match_view(session: Session, ctx: Context, match_id: int, now: datetime) -> sc.MatchView:
    """The match page, with the market line beside an upcoming senior forecast."""
    view = _match_view(session, ctx, match_id, now)
    h = view.header
    if view.milestones:
        view.market = _market_lines(session, {h.match_id: h.kickoff}, now).get(h.match_id)
    return view


def _match_view(session: Session, ctx: Context, match_id: int, now: datetime) -> sc.MatchView:
    from adatta.adapters.normalize import FINISHED

    rows = _matches(session, ctx, [models.Match.match_id == match_id])
    is_c2 = False
    if not rows:  # C2 step 3 (D54): a C2 top flight's match (adatta.api.c2)
        from adatta.api import c2

        rows = _matches(session, ctx, [models.Match.match_id == match_id],
                        leagues=list(c2.leagues()))  # fmt: skip
        is_c2 = bool(rows)
    is_national = not rows
    if is_national:  # Stage D v1: a national-team match (adatta.api.national)
        rows = _matches(session, ctx, [models.Match.match_id == match_id], national=True)
    if not rows:
        raise KeyError(match_id)
    m = rows[0]
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
        competition_tier=m.tier,
        round=m.round, kickoff=m.kickoff, status=m.status, home=m.home, away=m.away,
        team_level=m.team_level,
        venue=f"{venue.name}, {venue.city}" if venue else None,
        weather=sc.Weather(observed_at=_utc(ws.observed_at), lead_hours=ws.lead_hours,
                           temperature_c=ws.temperature_c, precipitation_mm=ws.precipitation_mm,
                           wind_speed_kmh=ws.wind_speed_kmh) if ws else None,
        referee=referee, capabilities=_cap_view(caps),
    )  # fmt: skip
    score = _results(session, [match_id]).get(match_id) if m.status in FINISHED else None
    if score is not None:
        header.result = sc.Result(home_goals=score[0], away_goals=score[1])
    if is_national:
        from adatta.api import national

        return national.match_view(session, ctx, m, header, now)
    if is_c2:
        from adatta.api import c2

        return c2.match_view(session, ctx, m, header, now)
    bundle = ctx.league_bundle[m.league_id]
    by_ms = _projections(session, [match_id]).get(match_id, {})
    runs = _serving(session, ctx, now)
    champ = _champion(session, ctx.model_name)
    milestones: list[sc.Milestone] = []
    for ms in _published_order(by_ms):
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
    half_coh: sc.Coherence | None = None
    half_reason: str | None = None
    if refit is not None and milestones:
        last = milestones[-1]
        league = m.league_id
        if m.tier == "cup":  # scored as the home side's league fixture (Stage A)
            tie = serve.cup_ties(session, ctx.cups, ctx.bundles, match_ids=[m.match_id])
            if tie and tie[0].fixture is not None:
                league = tie[0].fixture.league_id
        fx = serve.Fixture(m.match_id, _kickoff_then(session, m, last.generated_at),
                           league, m.home_id, m.away_id)  # fmt: skip
        rc = recompute(refit, fx, (m.home, m.away), ctx.max_goals)
        matrix, dec = rc.matrix, rc.decomposition
        diff = max(abs(float(rc.markets[(s.market, s.line, s.selection)].mean()) - s.prob.p)
                   for s in last.selections if s.market not in hsv.MARKETS)  # fmt: skip
        coh = sc.Coherence(milestone=last.milestone, coherent=diff <= COHERENCE_TOL,
                           max_abs_diff=diff)  # fmt: skip
        half_coh, half_reason = _half(session, ctx, m, fx, rc, last)
    else:
        reason = _cup_reasons(session, ctx, [m.match_id]).get(m.match_id) if m.tier == "cup" \
            else None  # fmt: skip
        reason = reason or ("No passing refit for this bundle yet." if serving is None else
                            "Not projected yet: the next capture tick writes it.")  # fmt: skip
    return sc.MatchView(
        generated_at=now, header=header, freshness=fresh,
        confidence=confidence(m, refit or (ctx.refit(serving) if serving else None), fresh,
                              caps, champ, now,
                              serving if (run and serving and run.as_of and serving.as_of
                                          and _utc(serving.as_of) > _utc(run.as_of)) else None),
        # an incoherent recomputation is never shown (§11.2); the coherence flag says why
        milestones=milestones, matrix=matrix if coh is None or coh.coherent else None,
        decomposition=dec if coh is None or coh.coherent else [], coherence=coh,
        no_projection_reason=reason,
        # P3b-2: the half markets are shown only when they recompute (§11.2)
        half_coherence=half_coh if coh is not None and coh.coherent else None,
        half_reason=half_reason,
        squad=squad(session, m, now, ctx.store),
        unavailable=UNAVAILABLE,
    )  # fmt: skip


# --------------------------------------------------------------------------- squad panel

LINEUP_POLL_FROM = timedelta(minutes=75)  # capture.snapshots.LINEUP_POLL_FROM
STATUS_LABEL = {"missing": "out", "questionable": "doubtful"}


def _lineup(row: models.LineupSnapshot | None) -> sc.Lineup | None:
    if row is None:
        return None

    def players(xs: list[dict[str, Any]]) -> list[sc.SquadPlayer]:
        return [sc.SquadPlayer(name=x.get("name") or "unnamed", number=x.get("number"),
                               pos=x.get("pos")) for x in xs]  # fmt: skip

    return sc.Lineup(observed_at=_utc(row.observed_at), formation=row.formation,
                     coach=row.coach_name, starters=players(row.starters),
                     bench=players(row.substitutes))  # fmt: skip


def squad(session: Session, m: MatchRow, now: datetime,
          store: RawStore | None = None) -> sc.Squad:  # fmt: skip
    """The squad panel (docs/DASHBOARD_TASKS.md): status spells valid at min(now, kickoff),
    the last status capture of the competition by then, and lineups seen by now. Every team
    level (D60)."""
    from adatta.adapters.normalize import FINISHED

    t = min(now, m.kickoff)
    L, P, PL, CL = (models.LineupSnapshot, models.PlayerStatusSnapshot, models.Player,
                    models.CaptureLog)  # fmt: skip
    checked = session.scalar(
        select(CL.run_at).where(CL.job == "status", CL.scope == str(m.league_id),
                                CL.outcome == "ok", CL.run_at <= t)
        .order_by(CL.run_at.desc()).limit(1)
    )  # fmt: skip
    note = None
    if m.kickoff > now:
        note = f"Not announced yet: polled from {int(LINEUP_POLL_FROM.total_seconds() // 60)} "
        note += "minutes before kickoff."
    teams = {"home": (m.home_id, m.home), "away": (m.away_id, m.away)}
    lineups = {r.team_id: r for r in session.scalars(
        select(L).where(L.match_id == m.match_id, L.observed_at <= now))}  # fmt: skip
    spells = session.execute(
        select(P, PL.name).join(PL, PL.player_id == P.player_id)
        .where(P.match_id == m.match_id, P.observed_at <= t,
               (P.superseded_at.is_(None)) | (P.superseded_at > t),
               (P.valid_to.is_(None)) | (P.valid_to > t))
        .order_by(P.status, PL.name)
    ).all()  # fmt: skip
    status = {r.player_id: STATUS_LABEL.get(r.status, r.status) for r, _ in spells}
    raw_now = {r.player_id: r.status for r, _ in spells}
    projection = _projected(session, m.match_id, now)
    pool = al.pooled(session, m.match_id, None, projection.generated_at) if projection else []
    league = str(m.league_id)
    season = session.scalar(select(models.Season.year).where(
        models.Season.season_id == m.season_id))  # fmt: skip
    finished = m.status in FINISHED
    scoring = al.scoring_runs(session, {m.match_id: m.kickoff}) if finished else ({}, {})
    out = {}
    for side, (tid, name) in teams.items():
        snap = lineups.get(tid)
        # the out rule reads the feed as the page stands (amendment 2), not as at the
        # projection's as-of time; P3-b's numbers are as stored. Played scores keep as-of.
        stored = {p.player_id: p.status_as_of for t, p, _ in pool if t == tid}
        xs = [replace(p, status_as_of=raw_now.get(p.player_id)) for t, p, _ in pool if t == tid]
        p_start = {p.player_id: p.p_start for p in xs}
        fv, frow = _formation(session, m.match_id, tid, now, p_start, status)
        rl = al.rules(store, fv.generated_at) if store is not None and fv else None
        limit = al.bench_limit(rl, league)
        lu = _lineup(snap)
        if lu is not None and snap is not None:
            # the grid test is a display property, not a forecast: without this fixture's
            # formation run, the latest rules by now say which league-seasons are positional
            FP = models.FormationProjection
            rl_pos = rl if rl is not None or store is None else al.rules(
                store, session.scalar(select(func.max(FP.generated_at))
                                      .where(FP.generated_at <= now)))  # fmt: skip
            positional = al.is_positional(rl_pos, league, int(season or 0))
            got = al.announced_pitch(snap.starters, positional)
            if got is not None:
                ids = al.player_ids(session, {str(x.get("player_ref")) for x, *_ in got[1]})
                lu.pitch_formation, lu.positional = got[0], positional
                lu.pitch = [sc.PitchPlayer(
                    name=x.get("name") or "unnamed", code=code, row=r, col=c,
                    p_start=p_start.get(ids.get(str(x.get("player_ref")), -1)),
                    status=status.get(ids.get(str(x.get("player_ref")), -1)))
                    for x, code, r, c in got[1]]  # fmt: skip
                if fv is not None:
                    fv.announced, fv.announced_p = (
                        got[0],
                        _announced_p(session, m.match_id, tid, now, got[0]),
                    )
        projected: list[sc.ProjectedPlayer] = []
        bench_n = None
        if xs:
            slot_of: dict[int, tuple[str, bool]] = {}
            if fv is not None and frow is not None:
                sec, slot_of = _place_xi(fv, frow, xs, limit, al.team_bench(rl, tid), rl, league,
                                         int(season or 0), status)  # fmt: skip
                order = {pid: i for i, pid in enumerate(slot_of)}
                sec["xi"] = sorted(sec["xi"], key=lambda p: order.get(p.player_id, 99))
            else:
                sec = al.sections(xs, limit, al.team_bench(rl, tid))
            bench_n = len(sec["bench"])
            for key in ("xi", "bench", "out"):
                projected += [sc.ProjectedPlayer(
                    name=p.name, pos=p.pos, p_start=p.p_start, p_bench=p.p_bench, emin=p.emin,
                    status=status.get(p.player_id),
                    status_as_of=STATUS_LABEL.get(stored.get(p.player_id) or "",
                                                  stored.get(p.player_id)),
                    section=key,
                    slot=slot_of.get(p.player_id, (None, False))[0] if key == "xi" else None,
                    moved=key == "xi" and slot_of.get(p.player_id, (None, False))[1])
                    for p in sec[key]]  # fmt: skip
            as_of = next(a for t, _, a in pool if t == tid)
            in_pool = {p.player_id for p in xs}
            projected += [sc.ProjectedPlayer(name=n, pos=pos, p_start=None, p_bench=None,
                                             emin=None, status=status.get(pid), section="squad")
                          for pid, n, pos in al.registered(session, tid, as_of)
                          if pid not in in_pool]  # fmt: skip
        absences = [sc.Absence(player_id=r.player_id, name=n,
                               status=STATUS_LABEL.get(r.status, r.status),
                               reason=r.reason, since=_utc(r.valid_from))
                    for r, n in spells if r.team_id == tid]  # fmt: skip
        outcome = None
        gen24 = scoring[0].get(m.match_id)
        if finished and snap is not None and gen24 is not None:
            xs24 = [p for _, p, _ in al.pooled(session, m.match_id, tid, gen24)]
            refs = {str(x.get("player_ref")) for x in [*snap.starters, *snap.substitutes]}
            f24 = scoring[1].get((m.match_id, tid))
            rl24 = al.rules(store, f24.generated_at) if store is not None and f24 else None
            outcome = al.score(xs24, al.bench_limit(rl24, league), al.team_bench(rl24, tid),
                               snap, al.player_ids(session, refs), f24, rl24,
                               al.is_positional(rl24, league, int(season or 0)))  # fmt: skip
        out[side] = sc.TeamSquad(team=name, lineup=lu, absences=absences, projected=projected,
                                 bench_size=bench_n, bench_limit=limit, formation=fv,
                                 outcome=outcome)  # fmt: skip
    if note is None and (out["home"].lineup is None or out["away"].lineup is None):
        note = "Not captured: no lineup was seen before kickoff."
    if out["home"].lineup is not None and out["away"].lineup is not None:
        note = None
    return sc.Squad(as_of=t, status_checked_at=_utc(checked) if checked else None,
                    lineup_note=note, hidden_reason=None, projection=projection,
                    **out)  # fmt: skip


def _projected(session: Session, match_id: int, now: datetime) -> sc.Projection | None:
    """The latest projected-lineup run for a match generated by now (P3-b, append-only
    rows): the run's times."""
    LP = models.LineupProjection
    r0 = session.scalars(
        select(LP).where(LP.match_id == match_id, LP.generated_at <= now)
        .order_by(LP.generated_at.desc()).limit(1)
    ).first()  # fmt: skip
    if r0 is None:
        return None
    return sc.Projection(generated_at=_utc(r0.generated_at), as_of=_utc(r0.as_of),
                         refit_as_of=_utc(r0.refit_as_of))  # fmt: skip


def _announced_p(session: Session, match_id: int, team_id: int, now: datetime,
                 formation: str) -> float | None:  # fmt: skip
    """P3-c's probability for an announced formation, from the row the panel shows."""
    FP = models.FormationProjection
    r = session.scalars(
        select(FP).where(FP.match_id == match_id, FP.team_id == team_id, FP.generated_at <= now)
        .order_by(FP.generated_at.desc(), FP.formation_projection_id.desc()).limit(1)
    ).first()  # fmt: skip
    if r is None:
        return None
    key = formation if formation in r.probs else "other"
    return float(r.probs[key]) if key in r.probs else None


def _place_xi(fv: sc.FormationView, frow: models.FormationProjection, xs: list[al.Pooled],
              limit: int | None, expected: float | None, rl: dict[str, Any] | None,
              league: str, season: int, status: dict[int, str],
              ) -> tuple[dict[str, list[al.Pooled]], dict[int, tuple[str, bool]]]:  # fmt: skip
    """Fit the projected XI to the pitch (docs/DASHBOARD_TASKS.md, "Projected XI on the
    pitch"): the last lineup in the pitch formation, or an empty grid of it when there is
    none. Sets fv.pitch, fv.positional and fv.moved; returns the sections with the fitted
    XI and player -> (slot code, moved in) in grid order."""
    slots = frow.slots
    if not slots:
        fv.positional = al.is_positional(rl, league, season)
        slots = al.grid_slots(fv.pitch_formation, fv.positional)
    sec, placed = al.fit(xs, limit, expected, slots, rl, fv.positional, frow.team_id)
    if not placed:
        return sec, {}
    fv.pitch = [sc.PitchPlayer(
        name=x.player.name if x.player else "no keeper projected", code=x.code, row=x.row,
        col=x.col, p_start=x.player.p_start if x.player else None,
        status=status.get(x.player.player_id) if x.player else None, moved=x.moved,
        usual=x.usual) for x in placed]  # fmt: skip
    fv.moved = sum(x.moved for x in placed if x.player is not None)
    return sec, {x.player.player_id: (x.code, x.moved) for x in placed if x.player is not None}


def _formation(session: Session, match_id: int, team_id: int, now: datetime,
               p_start: dict[int, float], status: dict[int, str],
               ) -> tuple[sc.FormationView | None, models.FormationProjection | None]:  # fmt: skip
    """P3-h: the latest formation projection of a team for a match generated by now, with
    the last lineup's pitch (replaced by the projected XI's in `squad`) and its row."""
    FP, PL = models.FormationProjection, models.Player
    r = session.scalars(
        select(FP).where(FP.match_id == match_id, FP.team_id == team_id, FP.generated_at <= now)
        .order_by(FP.generated_at.desc(), FP.formation_projection_id.desc()).limit(1)
    ).first()  # fmt: skip
    if r is None:
        return None, None
    opts = sorted(r.probs.items(), key=lambda x: -float(x[1]))[:3]
    slots = r.slots or []
    ids = [int(x["player_id"]) for x in slots]
    names = dict(session.execute(select(PL.player_id, PL.name).where(PL.player_id.in_(ids))).all())
    pitch = [sc.PitchPlayer(name=names.get(int(x["player_id"])) or "unnamed", code=x["code"],
                            row=int(x["row"]), col=int(x["col"]),
                            p_start=p_start.get(int(x["player_id"])),
                            status=status.get(int(x["player_id"])))
             for x in slots]  # fmt: skip
    return sc.FormationView(
        generated_at=_utc(r.generated_at), as_of=_utc(r.as_of), refit_as_of=_utc(r.refit_as_of),
        options=[sc.FormationOption(formation=f, p=float(p)) for f, p in opts],
        pitch_formation=r.pitch_formation,
        last_kickoff=_utc(r.last_kickoff) if r.last_kickoff else None,
        positional=r.positional, pitch=pitch,
    ), r  # fmt: skip


# --------------------------------------------------------------------------- played view


def _pre_kickoff_forecasts(
    session: Session, kickoffs: dict[int, datetime], first: bool = False
) -> dict[int, tuple[str, datetime, list[models.MarketProjection]]]:
    """Per match: the latest milestone whose projection run was generated before kickoff
    (with `first`, the earliest), its generation time and rows. A run generated at or after
    kickoff is never used."""
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
        pick = min if first else max
        ms = pick(slot, key=lambda m: slot[m][0])  # the latest (or first) before kickoff
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
    m = float(a.mean())
    if len(a) < 2:  # one match has no interval (JSON has no NaN; the page shows the mean)
        return sc.MeanScore(metric=metric, n=len(a), mean=m, lo=None, hi=None)
    se = float(a.std(ddof=1) / np.sqrt(len(a)))
    return sc.MeanScore(metric=metric, n=len(a), mean=m, lo=m - 1.96 * se, hi=m + 1.96 * se)


PLAYED_METRICS = ("log_loss_1x2", "rps_1x2", "brier_1x2", "log_loss_ou25", "log_loss_btts")


def _lineup_summary(ms: list[sc.PlayedMatch]) -> sc.LineupSummary | None:
    xs = [x for m in ms for x in (m.home_lineup, m.away_lineup) if x is not None]
    if not xs:
        return None
    fs = [x.formation_top for x in xs if x.formation_top is not None]
    sl = [x for x in xs if x.slot_acc is not None and x.slot_acc_last is not None]
    return sc.LineupSummary(
        n=len(xs), xi_mean=float(np.mean([x.xi_hits for x in xs])),
        xi_raw_mean=float(np.mean([x.xi_hits_raw for x in xs])),
        bench_hits=sum(x.bench_hits for x in xs), bench_n=sum(x.bench_n for x in xs),
        n_formation=len(fs), formation_top=float(np.mean(fs)) if fs else None,
        n_slot=len(sl), slot_acc=float(np.mean([x.slot_acc for x in sl])) if sl else None,
        slot_acc_last=float(np.mean([x.slot_acc_last for x in sl])) if sl else None,
    )  # fmt: skip


def _summary(
    segment: str, cid: int | None, ms: list[sc.PlayedMatch], tier: str | None = None
) -> sc.ScoreSummary:
    scores, market, diff = [], [], []
    for metric in PLAYED_METRICS:
        xs = [v for m in ms if (v := getattr(m.scores, metric)) is not None]
        if xs:
            scores.append(_mean_score(metric, xs))
        pairs = [(a, b) for m in ms if m.market is not None
                 and (a := getattr(m.scores, metric)) is not None
                 and (b := getattr(m.market.scores, metric)) is not None]  # fmt: skip
        if pairs:
            market.append(_mean_score(metric, [b for _, b in pairs]))
            diff.append(_mean_score(metric, [a - b for a, b in pairs]))
    from adatta.api.market import clv_summary

    return sc.ScoreSummary(segment=segment, competition_id=cid, tier=tier, n=len(ms),
                           scores=scores, market_scores=market, vs_market=diff,
                           clv=clv_summary([m.clv for m in ms if m.clv is not None]),
                           lineup=_lineup_summary(ms))  # fmt: skip


def _lineup_scores(session: Session, store: RawStore, ms: list[sc.PlayedMatch],
                   rows: dict[int, MatchRow]) -> None:  # fmt: skip
    """Sets each played match's home_lineup / away_lineup: the stored projections as of
    kickoff - 24 h against the announced lineup (nothing reconstructed)."""
    gen, form = al.scoring_runs(session, {m.match_id: m.kickoff for m in ms})
    if not gen:
        return
    snaps = al.announced(session, list(gen))
    pools = al.pooled_many(session, gen)
    refs = {str(x.get("player_ref")) for s in snaps.values() for x in [*s.starters, *s.substitutes]}
    ids = al.player_ids(session, refs)
    year = dict(session.execute(select(models.Season.season_id, models.Season.year).where(
        models.Season.season_id.in_({rows[m].season_id for m in gen}))).all())  # fmt: skip
    for m in ms:
        if m.match_id not in gen:
            continue
        r = rows[m.match_id]
        for side, tid in (("home", r.home_id), ("away", r.away_id)):
            snap, xs = snaps.get((m.match_id, tid)), pools.get((m.match_id, tid))
            if snap is None or not xs:
                continue
            frow = form.get((m.match_id, tid))
            rl = al.rules(store, frow.generated_at) if frow else None
            pos = al.is_positional(rl, str(r.league_id), int(year.get(r.season_id) or 0))
            got = al.score(xs, al.bench_limit(rl, str(r.league_id)), al.team_bench(rl, tid),
                           snap, ids, frow, rl, pos)  # fmt: skip
            setattr(m, f"{side}_lineup", got)


def played(
    session: Session,
    ctx: Context,
    now: datetime,
    start: datetime | None = None,
    end: datetime | None = None,
    competition_id: int | None = None,
) -> sc.PlayedList:
    """Finished matches of every served competition (the five's leagues and cups, the C2
    top flights, senior internationals) against the forecast published before their kickoff
    (docs/DASHBOARD_TASKS.md, played view)."""
    from adatta.adapters.normalize import FINISHED
    from adatta.api.market import clv_inputs, market_view, played_market
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
            if (r.round or "").startswith("Regular Season") or r.tier == "cup"] \
        if since else []  # fmt: skip
    if since:  # every served competition (owner, 2026-11-27): C2 top flights, internationals
        from adatta.api import c2 as c2v

        c2_rows = _matches(session, ctx, where, leagues=list(c2v.leagues()))
        point_ids = {r.match_id for r in c2_rows}  # baseline point forecasts (D54)
        rows += c2_rows
        rows += _matches(session, ctx, where, national=True)  # every team level (D60)
    else:
        point_ids = set()
    results = _results(session, [r.match_id for r in rows])
    forecasts = _pre_kickoff_forecasts(
        session, {r.match_id: r.kickoff for r in rows if r.match_id in results}
    )
    priced = {
        r.match_id: r.kickoff for r in rows if r.match_id in forecasts and r.match_id in results
    }
    market = market_view(session, priced)
    clv_in: dict[int, tuple[datetime, datetime, Any]] = {}
    for mid, (_, gen0, prs0) in _pre_kickoff_forecasts(session, priced, first=True).items():
        x0 = {q.selection: q.probability for q in prs0 if q.market_type == "1X2"}
        if {"home", "draw", "away"} <= x0.keys():
            clv_in[mid] = (gen0, priced[mid], np.array([x0["home"], x0["draw"], x0["away"]]))
    clv = clv_inputs(session, clv_in)
    out: list[sc.PlayedMatch] = []
    for r in rows:
        if r.match_id not in forecasts or r.match_id not in results:
            continue
        ms, gen, prs = forecasts[r.match_id]
        sel = {(p.market_type, p.line, p.selection): p for p in prs}
        hg, ag = results[r.match_id]
        pp = c2v._p if r.match_id in point_ids else _p  # a C2 point forecast has no interval
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
            competition=r.competition, competition_tier=r.tier, team_level=r.team_level,
            home=r.home, away=r.away, home_goals=hg, away_goals=ag,
            outcome=("home", "draw", "away")[k["1X2"]], milestone=ms, generated_at=gen,
            home_p=pp(x[0]), draw_p=pp(x[1]), away_p=pp(x[2]),
            over25=None if over is None else pp(over), btts=None if btts is None else pp(btts),
            scores=scores, market=played_market(market.get(r.match_id), hg, ag),
            clv=clv.get(r.match_id),
        ))  # fmt: skip
    _lineup_scores(session, ctx.store, out, {r.match_id: r for r in rows})
    out.sort(key=lambda m: (m.kickoff, m.match_id), reverse=True)
    summary = [_summary("all", None, out)] if out else []
    comps: dict[int, list[sc.PlayedMatch]] = {}
    for m in out:
        comps.setdefault(m.competition_id, []).append(m)
    country = {r.competition_id: r.country for r in rows}
    summary += [
        _summary(v[0].competition, cid, v, v[0].competition_tier)
        for cid, v in sorted(comps.items(), key=lambda kv: competition_order(
            country[kv[0]], kv[1][0].competition_tier, kv[1][0].competition))
    ]  # fmt: skip
    return sc.PlayedList(
        generated_at=now,
        forecasts_since=since,
        start=lo,
        end=end,
        uniform_log_loss_1x2=float(np.log(3)),
        summary=summary,
        reliability=_reliability(out),
        trend=_trend(out),
        matches=out,
    )


def _reliability(ms: list[sc.PlayedMatch], bins: int = 10) -> list[sc.ReliabilityBin]:
    """1X2 calibration: every outcome's forecast probability pooled, in equal-width bins."""
    p = np.array([[m.home_p.p, m.draw_p.p, m.away_p.p] for m in ms]).reshape(-1)
    y = np.array([[m.outcome == o for o in ("home", "draw", "away")] for m in ms],
                 dtype=float).reshape(-1)  # fmt: skip
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        sel = (p >= lo) & ((p < hi) if i < bins - 1 else (p <= hi))
        if sel.any():
            out.append(sc.ReliabilityBin(lo=lo, hi=hi, n=int(sel.sum()),
                                         predicted=float(p[sel].mean()),
                                         observed=float(y[sel].mean())))  # fmt: skip
    return out


TREND_MIN = 10  # the running gap starts once this many matches have a market


def _trend(ms: list[sc.PlayedMatch]) -> list[sc.TrendPoint]:
    """The cumulative 1X2 log loss of ours and the market's, by kickoff day (UTC)."""
    xs = sorted((m for m in ms if m.market is not None), key=lambda m: (m.kickoff, m.match_id))
    ours, mkt, out = [], [], []
    for i, m in enumerate(xs):
        assert m.market is not None
        ours.append(m.scores.log_loss_1x2)
        mkt.append(m.market.scores.log_loss_1x2)
        day = _utc(m.kickoff).strftime("%Y-%m-%d")
        if i + 1 < len(xs) and _utc(xs[i + 1].kickoff).strftime("%Y-%m-%d") == day:
            continue
        if len(ours) < TREND_MIN:  # a handful of matches would set the scale
            continue
        d = _mean_score("diff", [a - b for a, b in zip(ours, mkt, strict=True)])
        out.append(sc.TrendPoint(day=day, n=len(ours), ours=float(np.mean(ours)),
                                 market=float(np.mean(mkt)), diff=d.mean, diff_lo=d.lo,
                                 diff_hi=d.hi))  # fmt: skip
    return out


def live(session: Session, ctx: Context, now: datetime, feed: Any | None) -> sc.LiveList:
    """Matches in play in every served competition (the played view's scope: the bundles
    and their cups, the C2 top flights, the national competitions at every team level): the
    provider's live score beside the latest projection published before kickoff
    (docs/DASHBOARD_TASKS.md, in play; no in-play recalculation). The provider is not asked
    unless one of these matches is in the live window; one request covers them all."""
    from adatta.adapters.api_football_capture import NO_MATCH
    from adatta.adapters.normalize import FINISHED
    from adatta.api import c2 as c2v
    from adatta.api import live as lv

    M = models.Match
    window = [M.kickoff_time >= now - lv.WINDOW_BEFORE, M.kickoff_time <= now + lv.WINDOW_AHEAD]
    c2_rows = _matches(session, ctx, window, leagues=list(c2v.leagues()))
    point_ids = {r.match_id for r in c2_rows}  # baseline point forecasts (D54)
    every = _matches(session, ctx, window) + c2_rows + _matches(session, ctx, window,
                                                                national=True)  # fmt: skip
    rows = [r for r in every if r.status not in FINISHED | NO_MATCH]
    if not rows:
        return sc.LiveList(generated_at=now, fetched_at=None, note=None, matches=[])
    if feed is None:
        return sc.LiveList(generated_at=now, fetched_at=None, note="live feed not configured",
                           matches=[])  # fmt: skip
    snap = feed.get(now, sorted({r.league_id for r in rows}))
    by_ref = {str(s.fixture_id): s for s in snap.states}
    playing = [(r, by_ref[r.source_ref]) for r in rows if r.source_ref in by_ref]
    forecasts = _pre_kickoff_forecasts(session, {r.match_id: r.kickoff for r, _ in playing})
    out = []
    for r, s in playing:
        forecast = None
        if r.match_id in forecasts:
            ms, gen, prs = forecasts[r.match_id]
            x = {p.selection: p for p in prs if p.market_type == "1X2"}
            pp = c2v._p if r.match_id in point_ids else _p  # a C2 point forecast: no interval
            if {"home", "draw", "away"} <= x.keys():
                forecast = sc.LiveForecast(milestone=ms, generated_at=gen, home=pp(x["home"]),
                                           draw=pp(x["draw"]), away=pp(x["away"]))  # fmt: skip
        out.append((r, sc.LiveMatch(
            match_id=r.match_id, kickoff=r.kickoff, competition_id=r.competition_id,
            competition=r.competition, competition_tier=r.tier, team_level=r.team_level,
            home=r.home, away=r.away, status=s.status, elapsed=s.elapsed, extra=s.extra,
            home_goals=s.home_goals, away_goals=s.away_goals, forecast=forecast,
        )))  # fmt: skip
    out.sort(key=lambda t: (competition_order(t[0].country, t[0].tier, t[0].competition),
                            t[0].kickoff, t[0].match_id))  # fmt: skip
    return sc.LiveList(generated_at=now, fetched_at=snap.fetched_at, note=snap.note,
                       matches=[m for _, m in out])  # fmt: skip
