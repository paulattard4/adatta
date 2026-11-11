"""Weekly live refit of the champion (spec §12.1, §14.6; D11; PHASE1B_TASKS section 7).

One refit per bundle as of Monday 00:00 UTC, the backtest's grid, with the production spec
the locked holdout promoted. The draws and the training index go to the store; a `model_run`
row (component phase1b, window live) records the refit, its convergence numbers and whether
it passed the inference gates. `adatta.sim.serve` projects from the latest passing refit.

    python -m adatta.model.live refit --bundle england [--as-of 2026-11-09]
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
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


class NotReady(RuntimeError):
    """The data a refit needs is not in yet (rerun later)."""


def week_start(now: datetime) -> datetime:
    """Monday 00:00 UTC of `now`'s week: the as_of of the week's refit (harness grid)."""
    d = now.astimezone(UTC) - timedelta(days=now.astimezone(UTC).weekday())
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


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


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refit", help="weekly live refit of one bundle")
    r.add_argument("--bundle", required=True)
    r.add_argument("--as-of", default=None, help="YYYY-MM-DD, a Monday (default: this week's)")
    r.add_argument("--cache", default="data/cache", help="local store root (if not R2)")
    args = ap.parse_args()

    from adatta.db.session import get_session
    from adatta.eval.baseline import _git_sha

    cfg = harness.load_config()
    as_of = (datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=UTC) if args.as_of
             else week_start(datetime.now(UTC)))  # fmt: skip
    if as_of != week_start(as_of):
        raise SystemExit("--as-of must be a Monday (the refit grid)")
    spec = bt.production_spec(cfg)
    df = bt._load(cfg.bundles[args.bundle], spec.xg, spec.manager)
    with get_session() as s:
        try:
            row = refit(s, bt._store(args.cache), cfg, args.bundle, as_of, df,
                        git_commit=_git_sha())  # fmt: skip
        except NotReady as exc:
            raise SystemExit(f"not ready: {exc}") from None
        s.commit()
        d = row.manifest["diag"]
        print(f"{args.bundle} as of {as_of:%Y-%m-%d}: model_run {row.model_run_id} {row.status}; "
              f"R-hat {d.get('rhat_max', float('nan')):.3f}, ESS {d.get('ess_bulk_min', 0):.0f}, "
              f"div {d.get('divergences', 0):.0f}, fit {d.get('fit_s', 0):.0f} s")  # fmt: skip
        if row.status != "ok":
            raise SystemExit(f"inference gates failed: {row.manifest['gate_failures']}")


if __name__ == "__main__":
    main()
