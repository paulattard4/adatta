"""Live refits of the champion (spec §12.1, §14.6; D11, D33; PHASE1B_TASKS sections 7, 7b).

Each bundle is refitted as of Monday 00:00 UTC and as of every results trigger of the week
(`harness.refit_triggers`: after a match day whose results the next forecasts need),
the same refit times as the backtest's results grid, with the production spec the locked
holdout promoted. The draws and the training index go to the store; a `model_run` row
(component phase1b, window live) records each refit, its convergence numbers and whether it
passed the inference gates. `adatta.sim.serve` projects from the latest passing refit.

    python -m adatta.model.live refit --bundle england            # everything due this week
    python -m adatta.model.live refit --bundle england --as-of 2026-11-09
"""

from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.api_football_capture import NO_MATCH
from adatta.adapters.normalize import FINISHED, SOURCE_ID
from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.eval import harness
from adatta.model import backtest as bt
from adatta.model import team_strength as ts
from adatta.sim import serve

GATES = {"rhat_max": 1.01, "ess_bulk_min": 400.0, "ess_tail_min": 400.0, "divergences": 0.0}
RESULT_LOOKBACK = timedelta(days=14)  # how far back to look for results not yet ingested
FIXTURE_WINDOW = timedelta(days=8)  # fixtures read around this week for the trigger rule


def triggers_enabled() -> bool:
    """Results-triggered refits are live only once the D33 dev backtest has passed: the
    repository variable RESULTS_REFITS=on, passed to serve.yml and capture.yml."""
    return os.environ.get("RESULTS_REFITS", "").strip().lower() == "on"


class NotReady(RuntimeError):
    """The data a refit needs is not in yet (rerun later)."""


week_start = harness.week_start  # Monday 00:00 UTC: the as_of of the week's first refit


def bundle_fixtures(session: Session, leagues: list[int], lo: datetime, hi: datetime
                    ) -> pl.DataFrame:  # fmt: skip
    """Regular-season matches of the leagues kicking off in [lo, hi], played or scheduled,
    without the cancelled and postponed ones: the trigger rule's input."""
    M, S, C = models.Match, models.Season, models.Competition
    rows = session.execute(
        select(M.kickoff_time, M.home_team_id, M.away_team_id, M.status, M.round)
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .where(C.source_id == SOURCE_ID, C.source_ref.in_([str(x) for x in leagues]),
               M.kickoff_time >= lo, M.kickoff_time <= hi)
    ).all()  # fmt: skip
    keep = [
        (k if k.tzinfo else k.replace(tzinfo=UTC), h, a) for k, h, a, status, rnd in rows
        if status not in serve.NO_MATCH and (rnd or "").startswith(harness.REGULAR_SEASON)
    ]  # fmt: skip
    return pl.DataFrame(keep, schema={"kickoff": pl.Datetime("us", "UTC"), "home": pl.Int64,
                                      "away": pl.Int64}, orient="row")  # fmt: skip


def refit_times(session: Session, cfg: harness.EvalConfig, bundle: str, now: datetime,
                triggers: bool = True) -> list[datetime]:  # fmt: skip
    """This week's refit times up to `now`: Monday 00:00 and every results trigger since
    (D33). The rule restarts each Monday, so a window around this week is enough."""
    monday = week_start(now)
    if not triggers:
        return [monday]
    fx = bundle_fixtures(session, cfg.bundles[bundle], monday - FIXTURE_WINDOW,
                         monday + 2 * FIXTURE_WINDOW)  # fmt: skip
    trig = harness.refit_triggers(fx, cfg.result_available_after_hours)
    return [monday, *(t for t in trig if monday < t <= now)]


def recorded(session: Session, model_name: str, bundle: str) -> set[datetime]:
    """as_of of every live refit row of the bundle, passing or not (a failed refit is not
    retried: serving keeps the last passing one and the alert reports it)."""
    MR = models.ModelRun
    rows = session.execute(
        select(MR.as_of, MR.manifest)
        .where(MR.component == serve.COMPONENT, MR.window == serve.WINDOW,
               MR.model_name == model_name)
    ).all()  # fmt: skip
    return {serve._utc(a) for a, man in rows if a is not None and man.get("bundle") == bundle}


def due(session: Session, cfg: harness.EvalConfig, model_name: str, now: datetime,
        triggers: bool = True) -> list[tuple[str, datetime]]:  # fmt: skip
    """(bundle, as_of) of this week's refits that have no model_run row yet."""
    out = []
    for bundle in cfg.bundles:
        done = recorded(session, model_name, bundle)
        times = refit_times(session, cfg, bundle, now, triggers)
        out += [(bundle, t) for t in times if t not in done]
    return out


def gate_failures(diag: dict[str, float]) -> list[str]:
    out = []
    for k, limit in GATES.items():
        v = diag.get(k, float("nan"))
        bad = not np.isfinite(v) or (v < limit if k.startswith("ess") else v > limit)
        if bad:
            out.append(f"{k} {v:g} (limit {limit:g})")
    return out


def training_hash(fd: ts.FitData) -> str:
    """Identifies the training data of a refit (§14.8 data snapshot)."""
    h = hashlib.sha256()
    for a in (fd.team_ids, fd.home, fd.away, fd.league, fd.step, fd.hg, fd.ag, fd.xg_home,
              fd.xg_away):  # fmt: skip
        h.update(b"-" if a is None else np.ascontiguousarray(a).tobytes())
    h.update(f"{fd.leagues}{fd.start.isoformat()}{fd.n_steps}".encode())
    return h.hexdigest()


def pending_results(session: Session, leagues: list[int], as_of: datetime, lag_hours: float
                    ) -> int:  # fmt: skip
    """Regular-season matches that should be final by as_of (kickoff + lag) but have no final
    status yet: the calendar job hasn't picked their result up."""
    M, S, C = models.Match, models.Season, models.Competition
    rows = session.execute(
        select(M.status, M.round)
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .where(C.source_id == SOURCE_ID, C.source_ref.in_([str(x) for x in leagues]),
               M.kickoff_time >= as_of - RESULT_LOOKBACK,
               M.kickoff_time <= as_of - timedelta(hours=lag_hours))
    ).all()  # fmt: skip
    return sum(
        1 for status, rnd in rows
        if status not in FINISHED and status not in NO_MATCH
        and (rnd or "").startswith(harness.REGULAR_SEASON)
    )  # fmt: skip


def champion(session: Session, model_name: str) -> models.ModelRun:
    MR = models.ModelRun
    row = session.scalars(
        select(MR).where(MR.model_name == model_name, MR.status == "champion")
        .order_by(MR.model_run_id.desc())
    ).first()  # fmt: skip
    if row is None:
        raise SystemExit(f"{model_name} is not a champion in model_run; only a champion is served")
    return row


def refit(
    session: Session, store: RawStore, cfg: harness.EvalConfig, bundle: str,
    as_of: datetime, df: Any, fitter: bt.Fitter | None = None, git_commit: str = "unknown",
) -> models.ModelRun:  # fmt: skip
    """Fit one bundle as of `as_of` (or reuse the stored draws of the same config) and
    record it. An existing passing row for the same refit is returned unchanged."""
    spec = bt.production_spec(cfg)
    champ = champion(session, spec.model_name)
    h = bt.config_hash(spec)
    MR = models.ModelRun
    for row in session.scalars(
        select(MR).where(MR.component == serve.COMPONENT, MR.window == serve.WINDOW,
                         MR.config_hash == h, MR.as_of == as_of, MR.status == "ok")
    ):  # fmt: skip
        if row.manifest.get("bundle") == bundle:
            return row
    leagues = cfg.bundles[bundle]
    lag = cfg.result_available_after_hours
    n_pending = pending_results(session, leagues, as_of, lag)
    if n_pending:
        raise NotReady(f"{bundle}: {n_pending} matches before {as_of:%Y-%m-%d} have no result yet")
    # End the read transaction before the fit: Neon drops a connection left idle in a
    # transaction (the first live refit lost its model_run row after a 7-minute fit).
    session.commit()
    fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, lag)
    key = serve.refit_key(h, bundle, as_of)
    cached = try_get(store, key)
    if cached is not None:  # stored by an earlier attempt that failed before recording
        draws, _, meta = serve.decode_refit(cached)
        diag = meta["diag"]
    else:
        draws, diag = (fitter or bt.nuts_fitter(spec))(fd, spec.seed)
        meta = {"bundle": bundle, "as_of": as_of.isoformat(), "model": spec.model_name,
                "config_hash": h, "diag": diag}  # fmt: skip
        store.put(key, serve.encode_refit(draws, fd, meta))
    failures = gate_failures(diag)
    row = MR(
        model_name=spec.model_name, component=serve.COMPONENT,
        status="failed" if failures else "ok", created_at=datetime.now(UTC), as_of=as_of,
        window=serve.WINDOW, git_commit=git_commit, config_hash=h,
        lockfile_hash=bt._file_hash("uv.lock"), data_snapshot_hash=training_hash(fd),
        seed=spec.seed, trace_path=key,
        manifest={"bundle": bundle, "leagues": leagues, "spec": asdict(spec),
                  "champion_run": champ.model_run_id, "n_train": int(fd.hg.size),
                  "n_teams": len(fd.team_ids), "diag": diag, "gate_failures": failures,
                  "draws_stored": int(np.asarray(draws["rho"]).shape[-1])},
    )  # fmt: skip
    session.add(row)
    session.flush()
    return row


def _parse_as_of(text: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    raise SystemExit(f"--as-of {text!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM (UTC)")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refit", help="live refits of one bundle")
    r.add_argument("--bundle", required=True)
    r.add_argument("--as-of", default=None,
                   help="one refit time (a Monday, or a trigger time YYYY-MM-DDTHH:MM); "
                        "default: every refit due this week")  # fmt: skip
    r.add_argument("--cache", default="data/cache", help="local store root (if not R2)")
    args = ap.parse_args()

    from adatta.db.session import get_session
    from adatta.eval.baseline import _git_sha

    cfg = harness.load_config()
    spec = bt.production_spec(cfg)
    now = datetime.now(UTC)
    with get_session() as s:
        if args.as_of:
            as_of = _parse_as_of(args.as_of)
            times = refit_times(s, cfg, args.bundle, max(now, as_of))
            week = refit_times(s, cfg, args.bundle, as_of + timedelta(days=7))
            if as_of not in times and as_of not in week and as_of != week_start(as_of):
                raise SystemExit(f"{as_of:%Y-%m-%dT%H:%M} is not a Monday or a results "
                                 f"trigger of {args.bundle} (the refit grid, D33)")  # fmt: skip
            todo, strict = [as_of], True
        else:
            todo = [t for b, t in due(s, cfg, spec.model_name, now, triggers_enabled())
                    if b == args.bundle]  # fmt: skip
            strict = False
        s.rollback()
        if not todo:
            print(f"{args.bundle}: nothing due")
            return
        df = bt._load(cfg.bundles[args.bundle], spec.xg, spec.manager)
        failed = []
        for as_of in todo:
            try:
                row = refit(s, bt._store(args.cache), cfg, args.bundle, as_of, df,
                            git_commit=_git_sha())  # fmt: skip
            except NotReady as exc:
                if strict:
                    raise SystemExit(f"not ready: {exc}") from None
                print(f"not ready: {exc}")  # the next dispatch or schedule retries
                break
            s.commit()
            d = row.manifest["diag"]
            print(f"{args.bundle} as of {as_of:%Y-%m-%d %H:%M}: model_run {row.model_run_id} "
                  f"{row.status}; R-hat {d.get('rhat_max', float('nan')):.3f}, "
                  f"ESS {d.get('ess_bulk_min', 0):.0f}, div {d.get('divergences', 0):.0f}, "
                  f"fit {d.get('fit_s', 0):.0f} s")  # fmt: skip
            if row.status != "ok":
                failed.append(f"{as_of:%Y-%m-%d %H:%M}: {row.manifest['gate_failures']}")
        if failed:
            raise SystemExit(f"inference gates failed: {failed}")


if __name__ == "__main__":
    main()
