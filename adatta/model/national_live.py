"""Stage D v1 serving: the promotion record and the live refits (docs/BEYOND_LEAGUES_PLAN.md,
"D v1 serving acceptance"; spec §10.4, §12.1).

The promoted model is v1 with friendlies at 1.0, exactly as run on the international holdout.
One live refit per Monday 00:00 UTC, only in weeks with a servable national match within the
projection horizon; the same model, priors, sampler, seed and rerun rule as the backtest. The
draws go to R2 under serving/national/, and a model_run row (component national, window live)
records the refit and its gates. A refit failing a sampler gate (after the rerun) is recorded
and never served; the PPC and dispersion checks are reported only, as on the holdout.
`adatta.sim.national_serve` projects from the stored draws with numpy alone.

    python -m adatta.model.national_live promote --holdout 45166498451
    python -m adatta.model.national_live refit                  # this week's, if due
    python -m adatta.model.national_live refit --as-of 2026-11-16
    python -m adatta.model.national_live shadow --as-of 2026-11-16   # no DB writes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.storage import RawStore
from adatta.capture import snapshots
from adatta.db import models
from adatta.eval import holdout as ho
from adatta.eval.harness import week_start
from adatta.model import continental as ct
from adatta.model import national as nt

SERVE_PREFIX = "serving/national"
WINDOW = "live"
HORIZON = snapshots.INITIAL_HORIZON  # the projection horizon (as for clubs)
RESULT_LOOKBACK = timedelta(days=14)  # unfinished matches this far back hold a refit
NO_MATCH = {"PST", "CANC", "ABD", "AWD", "WO", "TBD"}


class NotReady(RuntimeError):
    """The results a refit needs are not in yet (rerun later)."""


def config_hash() -> str:
    """Everything that defines a refit besides its data."""
    cfg = {"version": nt.VERSION, "cache_tag": nt.CACHE_TAG, "conf_sd": nt.CONF_SD,
           "sigma_o_sd": nt.SIGMA_O_SD, "friendly_weight": nt.FRIENDLY_WEIGHT,
           "draws": nt.DRAWS, "tune": nt.TUNE, "chains": nt.CHAINS,
           "target_accept": nt.TARGET_ACCEPT, "rerun_draws": nt.RERUN_DRAWS, "keep": nt.KEEP,
           "window_days": nt.WINDOW.days, "lag_hours": nt.LAG.total_seconds() / 3600,
           "seed": nt.SEED, "max_goals": nt.MAX_GOALS}  # fmt: skip
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def refit_key(as_of: datetime) -> str:
    return f"{SERVE_PREFIX}/{config_hash()[:12]}/{as_of:%Y%m%dT%H%M}.npz"


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ------------------------------------------------------------------------------ promotion


def champion(session: Session) -> models.ModelRun | None:
    MR = models.ModelRun
    return session.scalars(
        select(MR).where(MR.component == nt.COMPONENT, MR.status == "champion",
                         MR.model_name == nt.model_name())
        .order_by(MR.model_run_id.desc())
    ).first()  # fmt: skip


def promote(session: Session, store: RawStore, run_id: str) -> models.ModelRun:
    """Record the promoted model from the international holdout run's verdict: the run must
    hold the opening's marker for this model, and its verdict must pass. Idempotent."""
    have = champion(session)
    if have is not None:
        return have
    marker = json.loads(store.get(ho.MARKERS[nt.OPENING]))
    if marker["run_id"] != run_id or nt.model_name() not in (marker.get("models") or []):
        raise SystemExit(
            f"run {run_id} did not open the international holdout for "
            f"{nt.model_name()} (marker: {marker['run_id']}, {marker['model']})"
        )
    verdict_key = f"{nt.PREFIX}/reports/{run_id}/verdict.json"
    verdict = json.loads(store.get(verdict_key))
    if not verdict.get("passed"):
        raise SystemExit(f"the holdout verdict of run {run_id} does not pass; nothing promoted")
    rep = verdict["report"]
    row = models.ModelRun(
        model_name=nt.model_name(), component=nt.COMPONENT, status="champion",
        created_at=datetime.now(UTC), as_of=None, window="holdout",
        git_commit=marker["commit"], config_hash=config_hash(), lockfile_hash="",
        data_snapshot_hash="", seed=nt.SEED, report_path=verdict_key,
        backtest_scores={"main": rep["main"], "calibration": rep["calibration"],
                         "checks": verdict["checks"]},
        manifest={"holdout_run": run_id, "opened_at": marker["opened_at"],
                  "friendly_weight": nt.FRIENDLY_WEIGHT, "version": nt.VERSION},
    )  # fmt: skip
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------------- refits


def upcoming(session: Session, store: RawStore, now: datetime,
             horizon: timedelta = HORIZON) -> pl.DataFrame:  # fmt: skip
    """Servable fixtures (two of the servable sides, not played, not called off) kicking off
    in (now, now + horizon]. Cheap when there are none (the capture tick calls it)."""
    M, S, C = models.Match, models.Season, models.Competition
    ids = list(session.scalars(
        select(M.match_id).join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .where(C.is_national, M.kickoff_time > now, M.kickoff_time <= now + horizon)
    ))  # fmt: skip
    if not ids:
        return pl.DataFrame(schema=nt.TARGET_SCHEMA)

    def keep(m: Any, _: tuple[int | None, int | None]) -> bool:
        k = ct._utc(m.kickoff_time)
        return now < k <= now + horizon and m.status not in NO_MATCH | set(nt.FINISHED)

    return nt.national_frame(session, store, keep, match_ids=ids)


def recorded(session: Session, as_of: datetime) -> models.ModelRun | None:
    MR = models.ModelRun
    return session.scalars(
        select(MR).where(MR.component == nt.COMPONENT, MR.window == WINDOW,
                         MR.config_hash == config_hash(), MR.as_of == as_of,
                         MR.status.in_(("ok", "failed")))
        .order_by(MR.model_run_id.desc())
    ).first()  # fmt: skip


def due(session: Session, store: RawStore, now: datetime) -> datetime | None:
    """This week's Monday refit, if a servable match kicks off within the horizon and the
    refit is not recorded yet."""
    monday = week_start(now)
    if recorded(session, monday) is not None:
        return None
    return monday if upcoming(session, store, now).height else None


def pending(session: Session, as_of: datetime) -> int:
    """Matches between two servable sides final by as_of (kickoff + LAG) but not finished."""
    side = nt.sides(session)
    M, S, C = models.Match, models.Season, models.Competition
    rows = session.execute(
        select(M.home_team_id, M.away_team_id, M.status)
        .join(S, S.season_id == M.season_id).join(C, C.competition_id == S.competition_id)
        .where(C.is_national, M.kickoff_time >= as_of - RESULT_LOOKBACK,
               M.kickoff_time <= as_of - nt.LAG)
    ).all()  # fmt: skip
    return sum(h in side and a in side and st not in nt.FINISHED and st not in NO_MATCH
               for h, a, st in rows)  # fmt: skip


def training_hash(train: pl.DataFrame) -> str:
    cols = train.select("match_id", "hg", "ag", "role", "type").sort("match_id")
    return hashlib.sha256(cols.write_csv().encode()).hexdigest()


def refit(session: Session, store: RawStore, as_of: datetime, git_commit: str = "unknown",
          lockfile_hash: str = "unknown",
          now: datetime | None = None) -> models.ModelRun:  # fmt: skip
    """The live refit as of `as_of` (stored draws reused), recorded in model_run. An existing
    row for the same refit is returned unchanged."""
    if as_of > (now or datetime.now(UTC)):
        raise ValueError(f"a refit as of {as_of:%Y-%m-%d %H:%M} is in the future")
    have = recorded(session, as_of)
    if have is not None:
        return have
    champ = champion(session)
    if champ is None:
        raise SystemExit("no promoted national model in model_run; run promote first")
    n = pending(session, as_of)
    if n:
        raise NotReady(f"{n} national matches before {as_of:%Y-%m-%d} have no result yet")
    tg = nt.live_targets(session, store, as_of, champ)
    train = nt.training(tg, as_of)
    # end the read transaction before the fit: Neon drops a connection idle in a transaction
    session.commit()
    key = refit_key(as_of)
    post, diag = nt.cached_fit(store, tg, as_of, nt.FRIENDLY_WEIGHT, key=key)
    failures = [c for c in nt.SAMPLER_CHECKS if c in diag["failed"]]
    row = models.ModelRun(
        model_name=nt.model_name(), component=nt.COMPONENT,
        status="failed" if failures else "ok", created_at=datetime.now(UTC), as_of=as_of,
        window=WINDOW, git_commit=git_commit, config_hash=config_hash(),
        lockfile_hash=lockfile_hash, data_snapshot_hash=training_hash(train), seed=nt.SEED,
        trace_path=key,
        manifest={"champion_run": champ.model_run_id, "n_train": diag["n_train"],
                  "n_teams": diag["n_teams"], "n_months": diag["n_months"],
                  "train_max_kickoff": diag["train_max_kickoff"], "seconds": diag["seconds"],
                  "draws": diag.get("draws"), "rerun": "first_attempt" in diag,
                  "gate_failures": failures,
                  "reported": [f for f in diag["failed"] if f not in failures],
                  "warnings": diag.get("warnings", []),
                  "rhat": diag.get("rhat"), "ess_bulk": diag.get("ess_bulk"),
                  "ess_tail": diag.get("ess_tail"), "divergences": diag.get("divergences"),
                  "draws_stored": int(np.asarray(post["c"]).shape[0])},
    )  # fmt: skip
    session.add(row)
    session.flush()
    return row


def load(store: RawStore, run: models.ModelRun) -> dict[str, Any]:
    import io

    if run.trace_path is None:
        raise ValueError(f"model_run {run.model_run_id} has no stored draws")
    z = np.load(io.BytesIO(store.get(run.trace_path)))
    return {k: np.asarray(z[k]) for k in z.files}


# -------------------------------------------------------------------------------- shadow


def shadow(session: Session, store: RawStore, as_of: datetime) -> dict[str, Any]:
    """Acceptance test 6, metrics only and no DB writes: the live refit as of `as_of` (draws
    stored under serving/), that week's servable matches projected from it, against the
    backtest's `predict` on the same draws."""
    from adatta.sim import national_serve as ns

    champ = champion(session)
    if champ is None:
        raise SystemExit("no promoted national model in model_run; run promote first")
    tg = nt.live_targets(session, store, as_of, champ)
    session.commit()
    t0 = time.time()
    post, diag = nt.cached_fit(store, tg, as_of, nt.FRIENDLY_WEIGHT, key=refit_key(as_of))
    secs = time.time() - t0

    def week(m: Any, _: tuple[int | None, int | None]) -> bool:
        k = ct._utc(m.kickoff_time)
        return as_of <= k < as_of + timedelta(days=7) and m.status not in NO_MATCH

    fx = nt.national_frame(session, store, week)
    months = int(diag["n_months"])
    worst = 0.0
    for r in fx.iter_rows(named=True):
        served = ns.project(post, r, as_of, months)
        p, _ = nt.predict(post, fx.filter(pl.col("match_id") == r["match_id"]), as_of, months)
        mine = [served[("1X2", None, s)][0] for s in ("home", "draw", "away")]
        worst = max(worst, float(np.abs(np.asarray(mine) - p[0]).max()))
    sampler_fail = [c for c in nt.SAMPLER_CHECKS if c in diag["failed"]]
    return {"as_of": as_of.isoformat(), "fixtures": fx.height, "max_abs_diff": worst,
            "identity_ok": worst <= 1e-9, "seconds": secs, "fit_seconds": diag["seconds"],
            "within_budget": diag["seconds"] < 7200, "n_train": diag["n_train"],
            "sampler_failures": sampler_fail, "reported": diag["failed"],
            "warnings": diag.get("warnings", []), "rerun": "first_attempt" in diag}  # fmt: skip


# ----------------------------------------------------------------------------------- CLI


def _parse(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC)


def main() -> None:
    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session
    from adatta.eval.baseline import _file_hash, _git_sha

    ap = argparse.ArgumentParser(prog="adatta.model.national_live")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("promote")
    p.add_argument("--holdout", required=True, help="the international holdout run id")
    r = sub.add_parser("refit")
    r.add_argument("--as-of", default="", help="a Monday (YYYY-MM-DD); default: this week's if due")
    sh = sub.add_parser("shadow")
    sh.add_argument("--as-of", required=True)
    args = ap.parse_args()
    store = make_store(get_settings())
    now = datetime.now(UTC)
    with get_session() as s:
        if args.cmd == "promote":
            row = promote(s, store, args.holdout)
            s.commit()
            print(f"promoted {row.model_name}: model_run {row.model_run_id} ({row.status}, "
                  f"holdout run {row.manifest['holdout_run']})")  # fmt: skip
            return
        if args.cmd == "shadow":
            out = shadow(s, store, week_start(_parse(args.as_of)))
            s.rollback()
            print(json.dumps(out, indent=1, default=str))
            if not (out["identity_ok"] and out["within_budget"] and not out["sampler_failures"]):
                raise SystemExit("shadow run: FAIL")
            print("shadow run: pass")
            return
        as_of = week_start(_parse(args.as_of)) if args.as_of else due(s, store, now)
        if as_of is None:
            print(f"no national refit due ({now:%Y-%m-%d %H:%M}): no servable match within "
                  f"{HORIZON.days} days, or this week's refit is recorded")  # fmt: skip
            return
        try:
            row = refit(s, store, as_of, _git_sha(), _file_hash("uv.lock"))
        except NotReady as exc:
            print(f"not ready: {exc}")
            return
        s.commit()
        m = row.manifest
        print(f"refit {as_of:%Y-%m-%d}: model_run {row.model_run_id} {row.status}; "
              f"{m['n_train']} train, {m['n_teams']} teams; {m['seconds']:.0f} s; rhat "
              f"{m['rhat']:.3f}, divergences {m['divergences']:.0f}; gate failures "
              f"{', '.join(m['gate_failures']) or 'none'}; reported "
              f"{', '.join(m['reported']) or 'none'}; warnings "
              f"{', '.join(m['warnings']) or 'none'}")  # fmt: skip


if __name__ == "__main__":
    main()
