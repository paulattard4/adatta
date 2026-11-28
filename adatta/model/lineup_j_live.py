"""P3-j step 3: J's live refits, in shadow (docs/PHASE3_TASKS.md P3-j (d), D65).

J (P plus the lineup offset, `lineup_strength`) is fitted per bundle at every as-of time of
P's passing live refits (`adatta.model.live`: Mondays and the results triggers), so each
shadow pair (`adatta.sim.lineup_j_shadow`) compares the two models from refits of the same
as-of time (comparator (a)). The first run fits only each bundle's latest passing P refit;
later runs fit every newer one. The training signals are step 2's definitions
(`lineup_signals.train_table`), scaled and shrunk with step 2's warm-up numbers
(`lineup_signals.J_TABLE`). A J refit failing the inference gates is redone once at
target_accept 0.99 and 4,000 draws (as step 2); a refit still failing is recorded and
not used. The draws go to the store under PREFIX; a `model_run` row (component COMPONENT,
window WINDOW) records each refit. Nothing here is served.

    python -m adatta.model.lineup_j_live refit --bundle england   # every J refit due

CI: serve.yml, after P's refits. The repo is public: the log carries metrics only.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.eval import harness
from adatta.features import lineup_signals as sig
from adatta.model import backtest as bt
from adatta.model import lineup_j as mj
from adatta.model import live
from adatta.model import team_strength as ts
from adatta.sim import serve

COMPONENT = "p3j"
WINDOW = "shadow"
MODEL_NAME = "P3-j J"
PREFIX = "serving/lineup_j"
HISTORY_DAYS = 420  # signals need each team's last 10 league matches and ratings before lo


def j_live_hash(p_hash: str, target_accept: float, draws: int) -> str:
    """J's live config hash: step 2's (P's fit hash, the signals table, J's model file,
    sampler settings, prior and signals)."""
    return mj.j_hash(p_hash, sig.J_TABLE, target_accept, draws)


def refit_key(h: str, bundle: str, as_of: datetime) -> str:
    return f"{PREFIX}/{h}/{bundle}/{as_of:%Y%m%dT%H%M}.npz"


def _rows(session: Session, component: str, window: str, model_name: str, bundle: str
          ) -> list[models.ModelRun]:  # fmt: skip
    MR = models.ModelRun
    rows = session.scalars(
        select(MR).where(MR.component == component, MR.window == window,
                         MR.model_name == model_name).order_by(MR.as_of, MR.model_run_id)
    ).all()  # fmt: skip
    return [r for r in rows if r.manifest.get("bundle") == bundle and r.as_of is not None]


def todo(session: Session, p_model: str, bundle: str) -> list[models.ModelRun]:
    """P's passing live refits of the bundle that J still needs: the latest one on the
    first run, then every one newer than J's latest (a failed J refit is not retried)."""
    p_rows = [r for r in _rows(session, serve.COMPONENT, serve.WINDOW, p_model, bundle)
              if r.status == "ok"]  # fmt: skip
    j_rows = _rows(session, COMPONENT, WINDOW, MODEL_NAME, bundle)
    if not p_rows:
        return []
    if not j_rows:
        return [p_rows[-1]]
    last = max(serve._utc(r.as_of) for r in j_rows if r.as_of is not None)
    return [r for r in p_rows if r.as_of is not None and serve._utc(r.as_of) > last]


def training(df: pl.DataFrame, app: pl.DataFrame, cfg: harness.EvalConfig, bundle: str,
             as_of: datetime, scale: dict[str, float], means: dict[str, float],
             ) -> tuple[ts.FitData, np.ndarray, np.ndarray]:  # fmt: skip
    """P's training rows as of `as_of` (as `live.refit` prepares them) and each side's
    scaled signals, 0 where absent (rule 8)."""
    leagues = cfg.bundles[bundle]
    as_of = serve._utc(as_of).astimezone(UTC)
    lo = as_of - timedelta(days=cfg.lookback_days)
    bundle_of = {int(x): bundle for x in leagues}
    table = sig.train_table(app, df, bundle_of, lo, means)
    d = mj.with_row_index(sig.attach(df, table, scale))
    fd0 = ts.prepare(d, leagues, as_of, cfg.lookback_days, cfg.result_available_after_hours)
    return mj.training_signals(fd0, sig.side_signals(d, "home"), sig.side_signals(d, "away"))


def refit(session: Session, store: RawStore, cfg: harness.EvalConfig, bundle: str,
          p_row: models.ModelRun, df: pl.DataFrame, app: pl.DataFrame,
          meta2: dict[str, Any], git_commit: str = "unknown",
          fitter: Any = None) -> models.ModelRun:  # fmt: skip
    """J fitted at P's refit's as-of time (or its stored draws reused) and recorded."""
    P = bt.production_spec(cfg)
    assert p_row.as_of is not None
    as_of = serve._utc(p_row.as_of).astimezone(UTC)  # the database's tz is named GMT
    scale = {k: float(v) for k, v in meta2["scale"].items()}
    means = {str(k): float(v) for k, v in meta2["line_means"].items()}
    fd, s_home, s_away = training(df, app, cfg, bundle, as_of, scale, means)
    session.commit()  # no idle transaction during the fit (as live.refit)
    fit = fitter or mj.fit_j
    tries = [(P.target_accept, P.draws), (mj.REDO_TARGET_ACCEPT, mj.REDO_DRAWS)]
    diag: dict[str, Any] = {}
    key, h = "", ""
    redone = False
    for k, (ta, nd) in enumerate(tries):
        h = j_live_hash(bt.fit_hash(P), ta, nd)
        key = refit_key(h, bundle, as_of)
        cached = try_get(store, key)
        if cached is not None:
            _, _, meta = serve.decode_refit(cached)
            diag = meta["diag"]
        else:
            draws, diag = fit(fd, s_home, s_away, P.seed, draws=nd, chains=P.chains,
                              target_accept=ta, tune=P.tune,
                              max_pred_draws=P.max_pred_draws)  # fmt: skip
            meta = {"bundle": bundle, "as_of": as_of.isoformat(), "model": MODEL_NAME,
                    "config_hash": h, "diag": diag, "p_run": p_row.model_run_id}  # fmt: skip
            store.put(key, serve.encode_refit(draws, fd, meta))
        redone = k > 0
        if mj.gates_ok(diag):
            break
    failures = live.gate_failures(diag)
    p_train = p_row.data_snapshot_hash
    row = models.ModelRun(
        model_name=MODEL_NAME, component=COMPONENT, status="failed" if failures else "ok",
        created_at=datetime.now(UTC), as_of=as_of, window=WINDOW, git_commit=git_commit,
        config_hash=h, lockfile_hash=bt._file_hash("uv.lock"),
        data_snapshot_hash=live.training_hash(fd), seed=P.seed, trace_path=key,
        manifest={"bundle": bundle, "leagues": cfg.bundles[bundle], "p_run": p_row.model_run_id,
                  "p_spec": asdict(P), "signals_table": sig.J_TABLE, "scale": scale,
                  "n_train": int(fd.hg.size), "diag": diag, "gate_failures": failures,
                  "redone": redone, "same_training_as_p": live.training_hash(fd) == p_train,
                  "signal_cover": float(np.mean(np.any(np.c_[s_home, s_away] != 0, axis=1)))},
    )  # fmt: skip
    session.add(row)
    session.flush()
    return row


def load_app(store: RawStore, leagues: list[int], as_of: datetime) -> pl.DataFrame:
    """Stored appearances of the bundle's leagues, from the season HISTORY_DAYS + the
    lookback before as_of up to the live season (rebuilt daily by projected_lineups.yml)."""
    from adatta.features import appearances as ap

    cfg = harness.load_config()
    first = (as_of - timedelta(days=cfg.lookback_days + HISTORY_DAYS)).year - 1
    return ap.load(store, [(str(lg), y) for lg in leagues for y in range(first, as_of.year + 1)])


def main() -> None:
    ap_ = argparse.ArgumentParser()
    sub = ap_.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refit", help="J's live refits of one bundle (shadow)")
    r.add_argument("--bundle", required=True)
    r.add_argument("--cache", default="data/cache", help="local store root (if not R2)")
    args = ap_.parse_args()

    from adatta.db.session import get_session
    from adatta.eval.baseline import _git_sha

    cfg = harness.load_config()
    P = bt.production_spec(cfg)
    store = bt._store(args.cache)
    _, meta2, _ = sig.load_step2(store, sig.J_TABLE)
    with get_session() as s:
        due = todo(s, P.model_name, args.bundle)
        s.rollback()
        if not due:
            print(f"{args.bundle}: no J refit due")
            return
        leagues = cfg.bundles[args.bundle]
        df = bt._load(leagues, P.xg, P.manager)
        app = load_app(store, leagues, max(serve._utc(x.as_of) for x in due if x.as_of))
        failed = []
        for p_row in due:
            row = refit(s, store, cfg, args.bundle, p_row, df, app, meta2, git_commit=_git_sha())
            s.commit()
            d = row.manifest["diag"]
            print(f"{args.bundle} J as of {row.as_of:%Y-%m-%d %H:%M}: model_run "
                  f"{row.model_run_id} {row.status}; R-hat {d.get('rhat_max', float('nan')):.3f}, "
                  f"ESS {d.get('ess_bulk_min', 0):.0f}, div {d.get('divergences', 0):.0f}, "
                  f"fit {d.get('fit_s', 0):.0f} s; redone {row.manifest['redone']}; same "
                  f"training as P {row.manifest['same_training_as_p']}")  # fmt: skip
            if row.status != "ok":
                failed.append(f"{row.as_of:%Y-%m-%d %H:%M}: {row.manifest['gate_failures']}")
        if failed:
            raise SystemExit(f"J inference gates failed: {failed}")


if __name__ == "__main__":
    main()
