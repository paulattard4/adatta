"""Opening the locked holdout, once, and the pre-registered Phase 1b promotion gate
(spec §14.3; D18, D28; `config/evaluation.yml` phase1b_gate).

A holdout run first claims a marker in the store. Every later step of the same run (refit
jobs, their reruns, assembly) checks the marker carries its run id; any other run is refused.
Only `unlock` issues the `HoldoutKey` that `harness.refit_plan` needs for holdout seasons.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import polars as pl

from adatta.adapters.storage import RawStore, try_get
from adatta.eval import harness
from adatta.eval.harness import HoldoutKey, HoldoutLocked, Phase1bGate

MARKER = "backtest/holdout/opened.json"


def claim(store: RawStore, run_id: str, model: str, commit: str) -> dict[str, Any]:
    """Open the holdout for `run_id`. Refused if another run has opened it; the same run id
    may claim again (a rerun of its first job)."""
    raw = try_get(store, MARKER)
    if raw is not None:
        marker: dict[str, Any] = json.loads(raw)
        if marker["run_id"] != run_id:
            raise HoldoutLocked(
                f"the holdout was opened by run {marker['run_id']} ({marker['model']}, "
                f"{marker['opened_at']}); it is opened once"
            )
        return marker
    marker = {"run_id": run_id, "model": model, "commit": commit,
              "opened_at": datetime.now(UTC).isoformat(timespec="seconds")}  # fmt: skip
    store.put(MARKER, json.dumps(marker).encode())
    return marker


def unlock(store: RawStore, run_id: str, model: str) -> HoldoutKey:
    """The key for a run that has claimed the holdout for this model."""
    raw = try_get(store, MARKER)
    if raw is None:
        raise HoldoutLocked("the holdout has not been claimed; run the claim step first")
    marker = json.loads(raw)
    if marker["run_id"] != run_id or marker["model"] != model:
        raise HoldoutLocked(
            f"the holdout belongs to run {marker['run_id']} ({marker['model']}), "
            f"not {run_id} ({model})"
        )
    return HoldoutKey(run_id)


def verdict(cmp: pl.DataFrame, gate: Phase1bGate) -> tuple[str, list[dict[str, Any]]]:
    """Apply the gate to a `backtest.compare` table (challenger vs Phase 1a, warm matches).
    Returns ("champion" | "rejected", checks); every check has name, value, limit, passed.
    rel < 0 means the challenger is better."""
    if gate.paired_interval != 0.95:
        raise ValueError("compare() reports 95% intervals; the gate asks for another level")
    checks: list[dict[str, Any]] = []
    overall = cmp.filter(pl.col("segment_type") == "all")
    for metric, gain in (("1X2:log_loss", gate.min_rel_logloss_gain_1x2),
                         ("1X2:rps", gate.min_rel_rps_gain)):  # fmt: skip
        row = overall.filter(pl.col("metric") == metric)
        if row.height != 1:
            raise ValueError(f"no overall row for {metric}")
        r = row.row(0, named=True)
        checks.append({"name": f"overall {metric} gain", "value": r["rel"], "limit": -gain,
                       "passed": r["rel"] < -gain})  # fmt: skip
        checks.append({"name": f"overall {metric} interval upper bound", "value": r["rel_hi"],
                       "limit": 0.0, "passed": r["rel_hi"] < 0.0})  # fmt: skip
    seg = cmp.filter(
        (pl.col("segment_type") == "league_id") & pl.col("metric").is_in(gate.segment_metrics)
    )
    leagues = seg["seg"].unique().to_list()
    missing = [
        (lg, m)
        for lg in leagues
        for m in gate.segment_metrics
        if seg.filter((pl.col("seg") == lg) & (pl.col("metric") == m)).height == 0
    ]
    if not leagues or missing:
        raise ValueError(f"segment table incomplete: {missing or 'no league segments'}")
    for r in seg.sort("seg", "metric").iter_rows(named=True):
        checks.append({"name": f"league {r['seg']} {r['metric']}", "value": r["rel"],
                       "limit": gate.max_segment_rel_worse,
                       "passed": r["rel"] <= gate.max_segment_rel_worse})  # fmt: skip
    return ("champion" if all(c["passed"] for c in checks) else "rejected"), checks


def reliability(pred: pl.DataFrame) -> pl.DataFrame:
    """Reliability per 1X2 outcome for every model (acceptance item 4), warm matches."""
    return pl.concat([harness.reliability(pred, m).with_columns(model=pl.lit(m))
                      for m in sorted(pred["model"].unique())])  # fmt: skip
