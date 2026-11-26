"""C2 step 3 (D54): live Phase 1a refits of the eight C2 bundles.

Phase 1a is the results-only Dixon-Coles at `phase1a_selected` (half-life 365 days, team
prior sd 0.5, fixed on the five before the c2 holdout opened), the model the c2 dev and
holdout runs scored as the benchmark. Each bundle is refitted as of Monday 00:00 UTC with
exactly the training rows and arithmetic `harness.walk_forward` uses for one refit (second
tiers observable only from their summer load, step 2 (c)); a test checks the predictions
agree. It is a deterministic point fit, so there are no draws and no intervals (D54
choice 1a). The fit goes to the store; a `model_run` row (component c2, window live)
records it. `adatta.sim.c2_serve` projects from the latest passing refit, for the top
flights recorded as champion (`phase1a_c2_<league>`, acceptance (g)).

    python -m adatta.model.c2_live refit                       # every bundle, this Monday
    python -m adatta.model.c2_live refit --as-of 2026-11-30 --bundle portugal
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.eval import c2, harness
from adatta.model import dixon_coles as dc

Arr = NDArray[np.float64]
IntArr = NDArray[np.int64]

MODEL = "phase1a_c2"  # the live refits' model_name
COMPONENT = "c2"
WINDOW = "live"
PREFIX = "serving/c2_phase1a"
SEED = 0  # deterministic fit (L-BFGS from a fixed start); logged for the manifest
TAIL_MAX = 1e-4  # acceptance (c): a served matrix's tail beyond 20 goals a side
CODE = ("adatta/model/c2_live.py", "adatta/model/dixon_coles.py")
DAY = 86400.0


def champion_name(league: int) -> str:
    """The model_run name of a top flight served on Phase 1a (acceptance (g))."""
    return f"{MODEL}_{league}"


def scoring_config() -> harness.EvalConfig:
    """evaluation.yml at the C2 cap (D53: 20 goals a side)."""
    from adatta.model.backtest import C2_MAX_GOALS

    return harness.load_config().model_copy(update={"max_goals": C2_MAX_GOALS})


def config_hash(cfg: harness.EvalConfig) -> str:
    """Changes with the Phase 1a settings, the cap, the training window, the C2 scope and
    this code, so a stored fit is reused only for the same model."""
    sel = cfg.phase1a_selected
    h = hashlib.sha256(json.dumps({
        "model": MODEL, "half_life_days": sel.half_life_days, "prior_sd": sel.team_prior_sd,
        "max_goals": cfg.max_goals, "lookback_days": cfg.lookback_days,
        "lag_hours": cfg.result_available_after_hours, "c2": c2.digest(),
    }, sort_keys=True).encode())  # fmt: skip
    for f in CODE:
        h.update(Path(f).read_bytes())
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class Fit:
    """One Phase 1a refit: what prediction needs, nothing of the training data."""

    bundle: str
    as_of: datetime
    leagues: list[int]  # provider ids, top flight first
    team_ids: IntArr  # index -> team id
    counts: IntArr  # training matches per team (cold start below `min_warmup_matches`)
    params: dc.Params
    league_att: Arr  # per league: mean attack of its sides in the window (unseen teams)
    league_def: Arr
    n_train: int
    training_hash: str

    def matrix(self, home: int, away: int, league: int, max_goals: int) -> tuple[Arr, float]:
        """The score matrix of one fixture and its tail mass, as `walk_forward` scores it:
        a team with no history in the window gets its league's average sides."""
        tix = {int(t): i for i, t in enumerate(self.team_ids)}
        lg = self.leagues.index(league)
        p = self.params
        q = dc.Params(p.mu, p.mu_league, p.home_league, np.append(p.attack, self.league_att[lg]),
                      np.append(p.defence, self.league_def[lg]), p.rho, p.shift)  # fmt: skip
        lam, mu_ = q.rates(np.array([tix.get(home, -1)]), np.array([tix.get(away, -1)]),
                           np.array([lg]))  # fmt: skip
        return dc.score_matrix(float(lam[0]), float(mu_[0]), q.rho, max_goals)

    def n_prior(self, team: int) -> int:
        idx = np.flatnonzero(self.team_ids == team)
        return int(self.counts[idx[0]]) if len(idx) else 0


def fit_as_of(df: pl.DataFrame, leagues: list[int], cfg: harness.EvalConfig, as_of: datetime,
              bundle: str = "") -> Fit:  # fmt: skip
    """Phase 1a as of `as_of` on a C2 bundle frame (`c2.load_matches`): the training rows
    and fit of one `harness.walk_forward` refit at that time, started cold."""
    sel = cfg.phase1a_selected
    secs = np.array([round(t.timestamp()) for t in df["kickoff"].to_list()], dtype=np.int64)
    lag = round(cfg.result_available_after_hours * 3600)
    avail = (np.array([round(t.timestamp()) for t in df["available_at"].to_list()],
                      dtype=np.int64) if "available_at" in df.columns else secs + lag)  # fmt: skip
    asof_s = round(as_of.timestamp())
    train = np.flatnonzero((avail <= asof_s) & (secs + lag <= asof_s)
                           & (secs >= asof_s - cfg.lookback_days * int(DAY)))  # fmt: skip
    if not len(train):
        raise ValueError(f"{bundle}: no training match observable as of {as_of:%Y-%m-%d}")
    kick = np.array([t.timestamp() for t in df["kickoff"].to_list()]) / DAY
    league_idx = {lid: i for i, lid in enumerate(leagues)}
    home_all, away_all = df["home"].to_numpy(), df["away"].to_numpy()
    lg_all = np.array([league_idx[x] for x in df["league_id"].to_list()], dtype=np.int64)
    teams = np.unique(np.concatenate([home_all[train], away_all[train]]))
    tix = {int(t): i for i, t in enumerate(teams)}
    home = np.array([tix[int(t)] for t in home_all[train]], dtype=np.int64)
    away = np.array([tix[int(t)] for t in away_all[train]], dtype=np.int64)
    league = lg_all[train]
    hg, ag = df["hg"].to_numpy()[train], df["ag"].to_numpy()[train]
    age = as_of.timestamp() / DAY - kick[train]
    weights = (np.ones_like(age) if sel.half_life_days is None
               else np.exp(-np.log(2) * age / sel.half_life_days))  # fmt: skip
    data = dc.MatchData(home=home, away=away, league=league, hg=hg, ag=ag, weight=weights,
                        n_teams=len(teams), n_leagues=len(leagues),
                        ridge=dc.ridge_for(sel.team_prior_sd), open_ridge=0.0,
                        mover_home=None, mover_away=None, shift_ridge=0.0)  # fmt: skip
    params, _ = dc.fit(data, None)
    league_att, league_def = np.zeros(len(leagues)), np.zeros(len(leagues))
    for li in range(len(leagues)):
        members = np.unique(np.concatenate([home[league == li], away[league == li]]))
        if len(members):
            league_att[li] = params.attack[members].mean()
            league_def[li] = params.defence[members].mean()
    h = hashlib.sha256()
    for a in (teams, home, away, league, hg, ag, weights):
        h.update(np.ascontiguousarray(a).tobytes())
    return Fit(bundle=bundle, as_of=as_of, leagues=list(leagues), team_ids=teams,
               counts=np.bincount(np.concatenate([home, away]), minlength=len(teams)),
               params=params, league_att=league_att, league_def=league_def,
               n_train=len(train), training_hash=h.hexdigest())  # fmt: skip


# --------------------------------------------------------------------------- storage


def key(h: str, bundle: str, as_of: datetime) -> str:
    return f"{PREFIX}/{h}/{bundle}/{harness.stamp(as_of)}.npz"


def encode(fit: Fit, meta: dict[str, Any]) -> bytes:
    p = fit.params
    arrays: dict[str, Any] = {
        "team_ids": fit.team_ids, "counts": fit.counts, "leagues": np.array(fit.leagues),
        "mu": np.array([p.mu]), "mu_league": p.mu_league, "home_league": p.home_league,
        "attack": p.attack, "defence": p.defence, "rho": np.array([p.rho]),
        "league_att": fit.league_att, "league_def": fit.league_def,
    }  # fmt: skip
    info = {**meta, "bundle": fit.bundle, "as_of": fit.as_of.isoformat(),
            "n_train": fit.n_train, "training_hash": fit.training_hash}  # fmt: skip
    arrays["meta"] = np.frombuffer(json.dumps(info, sort_keys=True).encode(), dtype=np.uint8)
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def decode(data: bytes) -> tuple[Fit, dict[str, Any]]:
    with np.load(io.BytesIO(data)) as z:
        a = {k: z[k] for k in z.files}
    meta: dict[str, Any] = json.loads(a.pop("meta").tobytes().decode())
    params = dc.Params(mu=float(a["mu"][0]), mu_league=a["mu_league"],
                       home_league=a["home_league"], attack=a["attack"], defence=a["defence"],
                       rho=float(a["rho"][0]))  # fmt: skip
    fit = Fit(bundle=meta["bundle"], as_of=datetime.fromisoformat(meta["as_of"]),
              leagues=[int(x) for x in a["leagues"]], team_ids=a["team_ids"],
              counts=a["counts"], params=params, league_att=a["league_att"],
              league_def=a["league_def"], n_train=int(meta["n_train"]),
              training_hash=meta["training_hash"])  # fmt: skip
    return fit, meta


def load(store: RawStore, run: models.ModelRun) -> Fit:
    if run.trace_path is None:
        raise ValueError(f"model_run {run.model_run_id} has no stored fit")
    fit, _ = decode(store.get(run.trace_path))
    return fit


# --------------------------------------------------------------------------- live refits


class NotReady(RuntimeError):
    """A top-flight match due by the refit time has no result yet."""


def pending_results(session: Session, league: int, as_of: datetime, lag_hours: float,
                    conf: c2.C2Config) -> int:  # fmt: skip
    """Top-flight league matches (C2 round classes: regular or phase) that should be final by
    as_of but are not: the calendar job hasn't picked their result up."""
    from adatta.adapters.api_football_capture import NO_MATCH
    from adatta.adapters.normalize import FINISHED, SOURCE_ID
    from adatta.model.live import RESULT_LOOKBACK

    M, S, C = models.Match, models.Season, models.Competition
    rows = session.execute(
        select(M.status, M.round)
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .where(C.source_id == SOURCE_ID, C.source_ref == str(league),
               M.kickoff_time >= as_of - RESULT_LOOKBACK,
               M.kickoff_time <= as_of - timedelta(hours=lag_hours))
    ).all()  # fmt: skip
    fams = conf.rounds.get(league, {})
    return sum(1 for status, rnd in rows
               if status not in FINISHED and status not in NO_MATCH
               and fams.get(c2.family(rnd or "")) in ("regular", "phase"))  # fmt: skip


def gate_failures(fit: Fit) -> list[str]:
    """A refit passes when every parameter is finite and rho is inside its bounds."""
    p = fit.params
    out = []
    vals = np.concatenate([[p.mu, p.rho], p.mu_league, p.home_league, p.attack, p.defence,
                           fit.league_att, fit.league_def])  # fmt: skip
    if not np.isfinite(vals).all():
        out.append("non-finite parameter")
    if not abs(p.rho) < dc.RHO_BOUND:
        out.append(f"rho {p.rho:.4f} at its bound")
    return out


def refit(session: Session, store: RawStore, cfg: harness.EvalConfig, bundle: str,
          as_of: datetime, df: pl.DataFrame, conf: c2.C2Config,
          git_commit: str = "unknown") -> models.ModelRun:  # fmt: skip
    """Fit one bundle as of `as_of` (or reuse its stored fit) and record it. An existing
    passing row for the same refit is returned unchanged."""
    from adatta.model.backtest import _file_hash

    h = config_hash(cfg)
    MR = models.ModelRun
    for row in session.scalars(
        select(MR).where(MR.component == COMPONENT, MR.window == WINDOW, MR.model_name == MODEL,
                         MR.config_hash == h, MR.as_of == as_of, MR.status == "ok")
    ):  # fmt: skip
        if row.manifest.get("bundle") == bundle:
            return row
    leagues = conf.bundles[bundle]
    n = pending_results(session, leagues[0], as_of, cfg.result_available_after_hours, conf)
    if n:
        raise NotReady(f"{bundle}: {n} matches before {as_of:%Y-%m-%d} have no result yet")
    session.commit()  # end the read transaction before the fit (Neon idle limit)
    k = key(h, bundle, as_of)
    cached = try_get(store, k)
    if cached is not None:
        fit, _ = decode(cached)
    else:
        fit = fit_as_of(df, leagues, cfg, as_of, bundle)
        store.put(k, encode(fit, {"model": MODEL, "config_hash": h}))
    failures = gate_failures(fit)
    row = MR(
        model_name=MODEL, component=COMPONENT, status="failed" if failures else "ok",
        created_at=datetime.now(UTC), as_of=as_of, window=WINDOW, git_commit=git_commit,
        config_hash=h, lockfile_hash=_file_hash("uv.lock"),
        data_snapshot_hash=fit.training_hash, seed=SEED, trace_path=k,
        manifest={"bundle": bundle, "leagues": leagues, "n_train": fit.n_train,
                  "n_teams": len(fit.team_ids), "gate_failures": failures,
                  "half_life_days": cfg.phase1a_selected.half_life_days,
                  "team_prior_sd": cfg.phase1a_selected.team_prior_sd,
                  "max_goals": cfg.max_goals, "rho": fit.params.rho},
    )  # fmt: skip
    session.add(row)
    session.flush()
    return row


def champions(session: Session) -> dict[int, models.ModelRun]:
    """Top flight -> its `phase1a_c2_<league>` champion row (acceptance (g)); only these
    leagues are served."""
    MR = models.ModelRun
    rows = session.scalars(
        select(MR).where(MR.component == COMPONENT, MR.model_name.like(f"{MODEL}\\_%", escape="\\"),
                         MR.status == "champion").order_by(MR.model_run_id)
    ).all()  # fmt: skip
    return {int(r.manifest["league_id"]): r for r in rows}


def record_champions(session: Session, scores: dict[int, dict[str, Any]], manifest: dict[str, Any],
                     git_commit: str = "unknown") -> list[models.ModelRun]:  # fmt: skip
    """One `phase1a_c2_<league>` champion row per top flight that passed (b)-(d), with its
    scores. A league already recorded is left as it is."""
    from adatta.model.backtest import _file_hash

    have = champions(session)
    cfg = scoring_config()
    out = []
    for league, s in sorted(scores.items()):
        if league in have:
            continue
        row = models.ModelRun(
            model_name=champion_name(league), component=COMPONENT, status="champion",
            created_at=datetime.now(UTC), as_of=None, window="dev", git_commit=git_commit,
            config_hash=config_hash(cfg), lockfile_hash=_file_hash("uv.lock"),
            data_snapshot_hash=manifest.get("data_snapshot", ""), seed=SEED,
            backtest_scores=s, manifest={**manifest, "league_id": league, "decision": "D54"},
        )  # fmt: skip
        session.add(row)
        out.append(row)
    session.flush()
    return out


def _parse_as_of(text: str) -> datetime:
    t = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    if t.weekday() != 0:
        raise SystemExit(f"--as-of {text}: the C2 refit is Mondays only")
    return t


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refit", help="this Monday's Phase 1a refit of every C2 bundle")
    r.add_argument("--bundle", action="append", default=None)
    r.add_argument("--as-of", default=None, help="a Monday YYYY-MM-DD (default: this week's)")
    r.add_argument("--cache", default="data/cache", help="local store root (if not R2)")
    args = ap.parse_args()

    from adatta.db.session import get_session
    from adatta.eval.baseline import _git_sha
    from adatta.model import backtest as bt

    cfg, conf = scoring_config(), c2.load_config()
    as_of = _parse_as_of(args.as_of) if args.as_of else harness.week_start(datetime.now(UTC))
    store = bt._store(args.cache)
    failed, not_ready = [], []
    with get_session() as s:
        for bundle in args.bundle or list(conf.bundles):
            df = bt._load(conf.bundles[bundle])
            try:
                row = refit(s, store, cfg, bundle, as_of, df, conf, git_commit=_git_sha())
            except NotReady as exc:
                print(f"not ready: {exc}")  # the next dispatch or schedule retries
                not_ready.append(bundle)
                s.rollback()
                continue
            s.commit()
            print(f"{bundle} as of {as_of:%Y-%m-%d}: model_run {row.model_run_id} {row.status}; "
                  f"{row.manifest['n_train']} training matches, "
                  f"{row.manifest['n_teams']} teams")  # fmt: skip
            if row.status != "ok":
                failed.append(f"{bundle}: {row.manifest['gate_failures']}")
    if failed:
        raise SystemExit(f"C2 Phase 1a refits failed: {failed}")
    if not_ready and args.as_of:
        raise SystemExit(f"not ready: {not_ready}")


if __name__ == "__main__":
    main()
