"""Serving: market projections from the champion's stored draws (spec §12.1–12.3, §14.8,
§15.2; PHASE1B_TASKS section 7).

Posterior-predictive only: a projection reads the draws of the latest passing weekly refit
(`adatta.model.live`) and never runs NUTS. Pure numpy, so it runs inside the capture tick
without the model dependency group.

Milestones are those of the odds schedule (`capture.snapshots`): `initial` the first time a
fixture can be projected, then T-7d, T-24h, T-60m and close, each inside its window, once,
never late. They are tracked from `market_projection` itself; this module never reads odds
(principle 1). Intermediate resimulations are not stored (§12.3).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import SOURCE_ID
from adatta.adapters.storage import RawStore
from adatta.capture import snapshots
from adatta.db import models
from adatta.eval import harness
from adatta.model import horizon as hz
from adatta.model.team_strength import FitData, rates_from_draws

Arr = NDArray[np.float64]
Draws = dict[str, Arr]

PREFIX = "serving"
COMPONENT = "phase1b"
WINDOW = "live"
HORIZON = snapshots.INITIAL_HORIZON  # project fixtures this far ahead
STALE_AFTER = timedelta(days=9)  # a weekly refit older than this is reported
MILESTONES: tuple[str, ...] = ("initial", *snapshots.TIMED)
OU_LINES = (1.5, 2.5, 3.5)
INTERVAL = (0.05, 0.95)  # 90% credible interval of the per-draw probability
NO_MATCH = {"PST", "CANC", "ABD", "AWD", "WO", "TBD"}  # no kickoff time to project at
NO_OVERRIDES = hashlib.sha256(b"[]").hexdigest()  # overrides arrive with §8.5 / §16.3


def refit_key(config_hash: str, bundle: str, as_of: datetime) -> str:
    return f"{PREFIX}/{config_hash}/{bundle}/{harness.stamp(as_of)}.npz"


# --------------------------------------------------------------------------- refit files


def encode_refit(draws: Draws, fd: FitData, meta: dict[str, Any]) -> bytes:
    """Draws plus the part of the training index prediction reads (team ids, which teams
    played in which league and step), so serving needs no training data."""
    arrays: dict[str, Any] = {f"draw__{k}": v for k, v in draws.items()}
    arrays |= {"idx__home": fd.home, "idx__away": fd.away, "idx__league": fd.league,
               "idx__step": fd.step, "idx__team_ids": fd.team_ids,
               "idx__leagues": np.array(fd.leagues, dtype=np.int64)}  # fmt: skip
    info = {**meta, "start": fd.start.isoformat(), "n_steps": fd.n_steps}
    arrays["meta"] = np.frombuffer(json.dumps(info, sort_keys=True).encode(), dtype=np.uint8)
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def decode_refit(data: bytes) -> tuple[Draws, FitData, dict[str, Any]]:
    with np.load(io.BytesIO(data)) as z:
        draws = {k[6:]: z[k] for k in z.files if k.startswith("draw__")}
        idx = {k[5:]: z[k] for k in z.files if k.startswith("idx__")}
        meta: dict[str, Any] = json.loads(z["meta"].tobytes().decode())
    n, n_steps = len(idx["home"]), int(meta["n_steps"])
    zeros = np.zeros(n, dtype=np.int64)
    fd = FitData(
        home=idx["home"], away=idx["away"], league=idx["league"], step=idx["step"],
        hg=zeros, ag=zeros, season_start=np.zeros(n_steps, dtype=bool),
        team_ids=idx["team_ids"], leagues=[int(x) for x in idx["leagues"]],
        start=datetime.fromisoformat(meta["start"]), n_steps=n_steps,
    )  # fmt: skip
    return draws, fd, meta


# --------------------------------------------------------------------------- markets


def score_matrices(lam: Arr, mu_: Arr, rho: Arr, kappa: Arr, max_goals: int) -> Arr:
    """`dixon_coles.score_matrix` for a vector of draws at once: (draws, n, n)."""
    k = np.arange(max_goals + 1, dtype=np.float64)
    lgam = np.array([math.lgamma(x + 1.0) for x in k])

    def pmf(r: Arr) -> Arr:
        out: Arr = np.exp(np.log(r)[:, None] * k - r[:, None] - lgam)
        return out

    m = pmf(lam)[:, :, None] * pmf(mu_)[:, None, :]
    m[:, 0, 0] *= 1 - lam * mu_ * rho
    m[:, 0, 1] *= 1 + lam * rho
    m[:, 1, 0] *= 1 + mu_ * rho
    m[:, 1, 1] *= 1 - rho
    m = np.maximum(m, 0.0)
    d = np.arange(max_goals + 1)
    m[:, d, d] *= np.exp(kappa)[:, None]
    out: Arr = m / m.sum(axis=(1, 2))[:, None, None]
    return out


def market_draws(m: Arr) -> dict[tuple[str, float | None, str], Arr]:
    """Per-draw probability of every served selection, from (draws, n, n) score matrices.
    Keys are (market_type, line, selection)."""
    n = m.shape[1]
    i, j = np.indices((n, n))
    home = m[:, i > j].sum(axis=1)
    draw = np.trace(m, axis1=1, axis2=2)
    total = np.stack([m[:, (i + j) == t].sum(axis=1) for t in range(2 * n - 1)], axis=1)
    btts = m[:, 1:, 1:].sum(axis=(1, 2))
    out: dict[tuple[str, float | None, str], Arr] = {
        ("1X2", None, "home"): home, ("1X2", None, "draw"): draw,
        ("1X2", None, "away"): 1.0 - home - draw,
        ("BTTS", None, "yes"): btts, ("BTTS", None, "no"): 1.0 - btts,
    }  # fmt: skip
    for line in OU_LINES:
        over = total[:, math.ceil(line) :].sum(axis=1)
        out[("OU", line, "over")] = over
        out[("OU", line, "under")] = 1.0 - over
    return out


@dataclass(frozen=True)
class Fixture:
    match_id: int
    kickoff: datetime
    league_id: int
    home: int  # team ids
    away: int
    season: int | None = None  # season start year (D34: a season with no training match)


def project(draws: Draws, fd: FitData, fixtures: list[Fixture], max_goals: int,
            walk: hz.Walk | None = None,
            ) -> list[dict[tuple[str, float | None, str], tuple[float, float, float]]]:  # fmt: skip
    """Per fixture: selection -> (posterior-mean probability, 5th, 95th percentile).
    Rates as `backtest.score_targets` computes them, for the production model (no D16 shift
    pending at prediction: the champion has no `delta`). `walk` (D34) adds the strength
    drift ahead of the refit; draws stored before D34 have no sigma_walk and get none."""
    if "delta" in draws:
        raise ValueError("D16 shifts are not served; the champion has none")
    tix = {int(t): i for i, t in enumerate(fd.team_ids)}
    lix = {lid: i for i, lid in enumerate(fd.leagues)}
    home = np.array([tix.get(f.home, -1) for f in fixtures], dtype=np.int64)
    away = np.array([tix.get(f.away, -1) for f in fixtures], dtype=np.int64)
    league = np.array([lix[f.league_id] for f in fixtures], dtype=np.int64)
    horizon = np.array([(f.kickoff - fd.start).days // 7 - (fd.n_steps - 1) for f in fixtures],
                       dtype=np.int64)  # fmt: skip
    offset = None
    if walk is not None and "sigma_walk" in draws:
        seasons = np.array([f.season if f.season is not None else -1 for f in fixtures])
        weeks = hz.weeks_ahead(horizon, hz.new_season(fd, seasons))
        offset = hz.offsets(draws, fd, home, away, weeks, walk)
    lam, mu_ = rates_from_draws(draws, fd, home, away, league, horizon=horizon, offset=offset)
    rho = np.asarray(draws["rho"])
    rho = rho[league] if rho.ndim == 2 else np.broadcast_to(rho, lam.shape)
    kappa = np.asarray(draws["kappa"])[league] if "kappa" in draws else np.zeros_like(lam)
    out = []
    for k in range(len(fixtures)):
        mk = market_draws(score_matrices(lam[k], mu_[k], rho[k], kappa[k], max_goals))
        out.append({
            sel: (float(p.mean()), *(float(q) for q in np.quantile(p, INTERVAL)))
            for sel, p in mk.items()
        })  # fmt: skip
    return out  # type: ignore[return-value]


# --------------------------------------------------------------------------- refits


@dataclass(frozen=True)
class Refit:
    bundle: str
    as_of: datetime
    model_run_id: int
    config_hash: str
    training_hash: str
    draws: Draws
    fd: FitData


def latest_refits(session: Session, now: datetime, model_name: str
                  ) -> tuple[dict[str, models.ModelRun], list[str]]:  # fmt: skip
    """The latest passing live refit per bundle with as_of <= now, and problems: a bundle
    whose newest refit failed, or whose serving refit is older than STALE_AFTER."""
    MR = models.ModelRun
    rows = session.scalars(
        select(MR)
        .where(MR.component == COMPONENT, MR.window == WINDOW, MR.model_name == model_name,
               MR.as_of <= now)
        .order_by(MR.as_of, MR.model_run_id)
    ).all()  # fmt: skip
    newest: dict[str, models.ModelRun] = {}
    passing: dict[str, models.ModelRun] = {}
    for r in rows:
        b = r.manifest["bundle"]
        newest[b] = r
        if r.status == "ok":
            passing[b] = r
    problems = []
    for b, r in sorted(newest.items()):
        if r.status != "ok":
            problems.append(f"serving {b}: refit as of {r.as_of:%Y-%m-%d} {r.status}")
    for b, r in sorted(passing.items()):
        as_of = _utc(r.as_of) if r.as_of else now
        if now - as_of > STALE_AFTER:
            problems.append(f"serving {b}: latest passing refit is as of {as_of:%Y-%m-%d}, "
                            f"older than {STALE_AFTER.days} days")  # fmt: skip
    return passing, problems


def load_refit(store: RawStore, run: models.ModelRun) -> Refit:
    if run.trace_path is None:
        raise ValueError(f"model_run {run.model_run_id} has no stored draws")
    draws, fd, meta = decode_refit(store.get(run.trace_path))
    if run.as_of is None:
        raise ValueError(f"model_run {run.model_run_id} has no as_of")
    return Refit(bundle=meta["bundle"], as_of=_utc(run.as_of), model_run_id=run.model_run_id,
                 config_hash=run.config_hash, training_hash=run.data_snapshot_hash,
                 draws=draws, fd=fd)  # fmt: skip


# --------------------------------------------------------------------------- milestones


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def due_milestones(kickoff: datetime, now: datetime, done: set[str]) -> list[str]:
    """`initial` until written, then the timed milestones whose window contains `now`."""
    if now >= kickoff:
        return []
    out = [] if "initial" in done else ["initial"]
    return out + snapshots.due_kinds(kickoff, now, done)


REFIT_PREFIX = "refit "  # D41: a fresh forecast when a newer refit passes


def refit_label(as_of: datetime) -> str:
    """The refit milestone's name, from the refit's as_of (fits market_projection's 16)."""
    return f"{REFIT_PREFIX}{_utc(as_of):%m-%d %H}h"


def _refits_used(session: Session, ids: list[int], bundle_of: dict[int, str]
                 ) -> dict[int, datetime]:  # fmt: skip
    """Per match: the newest refit as_of among the projection runs that wrote its rows."""
    MP, PR = models.MarketProjection, models.ProjectionRun
    out: dict[int, datetime] = {}
    for i in range(0, len(ids), 500):
        for mid, cv in session.execute(
            select(MP.match_id, PR.component_versions)
            .join(PR, PR.projection_run_id == MP.projection_run_id)
            .where(MP.match_id.in_(ids[i : i + 500])).distinct()
        ):  # fmt: skip
            v = (cv or {}).get(bundle_of[mid], {})
            if "as_of" in v:
                t = _utc(datetime.fromisoformat(v["as_of"]))
                out[mid] = max(out.get(mid, t), t)
    return out


def upcoming(session: Session, leagues: dict[int, str], now: datetime) -> list[Fixture]:
    """Regular-season fixtures of the bundle leagues kicking off within HORIZON."""
    M, S, C = models.Match, models.Season, models.Competition
    rows = session.execute(
        select(M.match_id, M.kickoff_time, C.source_ref, M.home_team_id, M.away_team_id,
               M.status, M.round, S.year)
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .where(C.source_id == SOURCE_ID, C.source_ref.in_([str(x) for x in leagues]),
               M.kickoff_time > now, M.kickoff_time <= now + HORIZON)
        .order_by(M.kickoff_time, M.match_id)
    ).all()  # fmt: skip
    return [
        Fixture(r[0], _utc(r[1]), int(r[2]), r[3], r[4], r[7])
        for r in rows
        if r[5] not in NO_MATCH and (r[6] or "").startswith("Regular Season")
    ]


# --------------------------------------------------------------------------- cup ties


@dataclass(frozen=True)
class CupTie:
    """An upcoming cup match: the fixture to project (its league the home side's), or why
    it is not forecast."""

    match_id: int
    cup_id: int
    fixture: Fixture | None
    reason: str | None


def team_leagues(session: Session, league_ids: list[int], season: int) -> dict[int, int]:
    """Team -> the bundle league it plays in the season (league matches; the most frequent
    if more than one, as A1's targets)."""
    M, S, C = models.Match, models.Season, models.Competition
    count: dict[tuple[int, int], int] = {}
    for h, a, lg in session.execute(
        select(M.home_team_id, M.away_team_id, C.source_ref).join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .where(C.source_id == SOURCE_ID, C.source_ref.in_([str(x) for x in league_ids]),
               S.year == season)
    ):  # fmt: skip
        for t in (h, a):
            count[(int(t), int(lg))] = count.get((int(t), int(lg)), 0) + 1
    best: dict[int, tuple[int, int]] = {}
    for (team, league), n in count.items():
        if team not in best or n > best[team][1]:
            best[team] = (league, n)
    return {team: league for team, (league, _) in best.items()}


def cup_ties(session: Session, cups: dict[int, str], bundles: dict[str, list[int]],
             match_ids: list[int] | None = None, now: datetime | None = None,
             ) -> list[CupTie]:  # fmt: skip
    """Cup matches (the given ids, or those kicking off within HORIZON of now) with the
    fixture A1 scores (docs/BEYOND_LEAGUES_PLAN.md, Stage B, serving A1): both sides in a
    league of the cup's bundle that season; the home side's league; a "swapped" venue is
    not served."""
    from adatta.cli import _bundle_competition_ids
    from adatta.features import neutral

    M, S, C = models.Match, models.Season, models.Competition
    q = (select(M, C.source_ref, S.year).join(S, S.season_id == M.season_id)
         .join(C, C.competition_id == S.competition_id)
         .where(C.source_id == SOURCE_ID, C.source_ref.in_([str(c) for c in cups])))  # fmt: skip
    if match_ids is not None:
        q = q.where(M.match_id.in_(match_ids))
    else:
        assert now is not None
        q = q.where(M.kickoff_time > now, M.kickoff_time <= now + HORIZON)
    rows = [r for r in session.execute(q.order_by(M.kickoff_time, M.match_id)).all()
            if r[0].status not in NO_MATCH]  # fmt: skip
    if not rows:
        return []
    league_ids, _ = _bundle_competition_ids(session)
    roles = neutral.venue_roles(session, [r[0] for r in rows], league_ids, neutral.load_rules(),
                                {r[0].season_id: int(r[1]) for r in rows})  # fmt: skip
    role = dict(zip(roles["match_id"].to_list(), roles["role"].to_list(), strict=True))
    leagues_of: dict[tuple[str, int], dict[int, int]] = {}
    out = []
    for m, ref, year in rows:
        cup, b = int(ref), cups[int(ref)]
        key = (b, int(year))
        if key not in leagues_of:
            leagues_of[key] = team_leagues(session, bundles[b], int(year))
        tl = leagues_of[key]
        if m.home_team_id not in tl or m.away_team_id not in tl:
            why = f"Not forecast: a side is not in the {b} bundle this season (principle 8)."
            out.append(CupTie(m.match_id, cup, None, why))
        elif role.get(m.match_id) == "swapped":
            out.append(CupTie(m.match_id, cup, None, "Not forecast: the listed away side is at "
                              "its own ground (swapped venue), which is not served."))  # fmt: skip
        else:
            fx = Fixture(m.match_id, _utc(m.kickoff_time), tl[m.home_team_id], m.home_team_id,
                         m.away_team_id, int(year))  # fmt: skip
            out.append(CupTie(m.match_id, cup, fx, None))
    return out


def _done(session: Session, ids: list[int]) -> dict[int, set[str]]:
    MP = models.MarketProjection
    out: dict[int, set[str]] = {i: set() for i in ids}
    for i in range(0, len(ids), 500):
        for mid, ms in session.execute(
            select(MP.match_id, MP.milestone).where(MP.match_id.in_(ids[i : i + 500])).distinct()
        ):
            out[mid].add(ms)
    return out


@dataclass
class ProjectReport:
    written: dict[str, int] = field(default_factory=dict)  # milestone -> fixtures
    no_refit: int = 0  # fixtures whose bundle has no passing refit yet
    problems: list[str] = field(default_factory=list)
    projection_run_id: int | None = None


def run_projection(
    session: Session, store: RawStore, bundles: dict[str, list[int]], model_name: str,
    max_goals: int, manifest: dict[str, Any], now: datetime | None = None,
    loader: Callable[[RawStore, models.ModelRun], Refit] = load_refit,
    walk: hz.Walk | None = None, cups: dict[int, str] | None = None,
) -> ProjectReport:  # fmt: skip
    """Write the due milestone projections for every upcoming bundle fixture. Idempotent:
    a (match, milestone) already in market_projection is never written again. `manifest`
    carries git_commit, lockfile_hash and seed (§14.8)."""
    now = now or datetime.now(UTC)
    report = ProjectReport()
    league_bundle = {lid: b for b, ls in bundles.items() for lid in ls}
    fixtures = upcoming(session, league_bundle, now)
    if cups:  # A1: cup ties between bundle teams, scored as the home side's league fixture
        fixtures += [t.fixture for t in cup_ties(session, cups, bundles, now=now)
                     if t.fixture is not None]  # fmt: skip
    done = _done(session, [f.match_id for f in fixtures])
    due = {f.match_id: due_milestones(f.kickoff, now, done[f.match_id]) for f in fixtures}
    runs, problems = latest_refits(session, now, model_name)
    # D41: a fixture with a forecast and nothing due gets one from a newer passing refit
    stale = [f for f in fixtures if not due[f.match_id] and done[f.match_id]
             and league_bundle[f.league_id] in runs]  # fmt: skip
    used = _refits_used(session, [f.match_id for f in stale],
                        {f.match_id: league_bundle[f.league_id] for f in stale})  # fmt: skip
    for f in stale:
        run = runs[league_bundle[f.league_id]]
        if run.as_of is None or f.match_id not in used:
            continue
        label = refit_label(run.as_of)
        if _utc(run.as_of) > used[f.match_id] and label not in done[f.match_id]:
            due[f.match_id] = [label]
    fixtures = [f for f in fixtures if due[f.match_id]]
    if not fixtures:
        return report
    report.problems = problems
    by_bundle: dict[str, list[Fixture]] = {}
    for f in fixtures:
        by_bundle.setdefault(league_bundle[f.league_id], []).append(f)
    refits: dict[str, Refit] = {}
    results: dict[int, dict[tuple[str, float | None, str], tuple[float, float, float]]] = {}
    for b, fx in sorted(by_bundle.items()):
        if b not in runs:
            report.no_refit += len(fx)
            continue
        refit = loader(store, runs[b])
        if refit.as_of > now:
            raise ValueError(f"refit {refit.model_run_id} is from the future")  # never leak
        refits[b] = refit
        for f, proj in zip(fx, project(refit.draws, refit.fd, fx, max_goals, walk), strict=True):
            results[f.match_id] = proj
    if not results:
        return report
    pr = models.ProjectionRun(
        generated_at=now, as_of=now, git_commit=manifest["git_commit"],
        lockfile_hash=manifest["lockfile_hash"],
        config_hash=",".join(sorted({r.config_hash for r in refits.values()})),
        component_versions={b: {"model_run_id": r.model_run_id, "model": model_name,
                                "as_of": r.as_of.isoformat()}
                            for b, r in sorted(refits.items())},
        training_snapshot_hash=hashlib.sha256(
            "".join(f"{b}:{r.training_hash}" for b, r in sorted(refits.items())).encode()
        ).hexdigest(),
        source_snapshot_ids={}, override_set_hash=NO_OVERRIDES, seed=manifest["seed"],
    )  # fmt: skip
    session.add(pr)
    session.flush()
    rows = []
    for f in fixtures:
        if f.match_id not in results:
            continue
        for ms in due[f.match_id]:
            report.written[ms] = report.written.get(ms, 0) + 1
            for (market, line, sel), (p, lo, hi) in results[f.match_id].items():
                rows.append(models.MarketProjection(
                    projection_run_id=pr.projection_run_id, match_id=f.match_id, milestone=ms,
                    market_type=market, line=line, selection=sel, probability=p,
                    interval_lo=lo, interval_hi=hi,
                ))  # fmt: skip
    session.add_all(rows)
    session.flush()
    report.projection_run_id = pr.projection_run_id
    return report
