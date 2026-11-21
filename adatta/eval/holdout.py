"""Opening the locked holdout, once per opening, and the pre-registered Phase 1b promotion
gate (spec §14.3; D18, D28; `config/evaluation.yml` phase1b_gate). D30: a second opening for
the Phase 2 promotion, its own marker, judged with the D36 gate (`config/phase2_gate.yml`).

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

MARKER = "backtest/holdout/opened.json"  # the Phase 1b opening (2026-11-11)
MARKERS = {
    "phase1b": MARKER,
    "phase2": "backtest/holdout/opened_phase2.json",  # D30
    "cups": "backtest/holdout/opened_cups.json",  # Stage A/B: the cup ties, once
}


def _marker(opening: str) -> str:
    if opening not in MARKERS:
        raise ValueError(f"unknown holdout opening {opening!r}; known: {sorted(MARKERS)}")
    return MARKERS[opening]


def claim(store: RawStore, run_id: str, model: str, commit: str, opening: str = "phase1b",
          models: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    """Open the holdout for `run_id`, once per opening. Refused if another run has made this
    opening; the same run id may claim again (a rerun of its first job). The Phase 2 opening
    (D30) needs the 1b opening first and never touches its marker; `models` are the specs
    its run may score (the challenger `model` and the 1b reference)."""
    key = _marker(opening)
    raw = try_get(store, key)
    if raw is not None:
        marker: dict[str, Any] = json.loads(raw)
        if marker["run_id"] != run_id:
            raise HoldoutLocked(
                f"the holdout's {opening} opening was made by run {marker['run_id']} "
                f"({marker['model']}, {marker['opened_at']}); it is made once"
            )
        return marker
    if opening != "phase1b" and try_get(store, MARKER) is None:
        raise HoldoutLocked("the Phase 1b opening comes first (D30)")
    marker = {"run_id": run_id, "model": model, "commit": commit, "opening": opening,
              "models": sorted(set(models or [model])),
              "opened_at": datetime.now(UTC).isoformat(timespec="seconds")}  # fmt: skip
    store.put(key, json.dumps(marker).encode())
    return marker


def unlock(store: RawStore, run_id: str, model: str, opening: str = "phase1b") -> HoldoutKey:
    """The key for a run that has made this opening, for one of the specs it may score."""
    raw = try_get(store, _marker(opening))
    if raw is None:
        raise HoldoutLocked(f"the holdout's {opening} opening has not been claimed; run the "
                            "claim step first")  # fmt: skip
    marker = json.loads(raw)
    allowed = marker.get("models") or [marker["model"]]
    if marker["run_id"] != run_id or model not in allowed:
        raise HoldoutLocked(
            f"the holdout's {opening} opening belongs to run {marker['run_id']} "
            f"({', '.join(allowed)}), not {run_id} ({model})"
        )
    return HoldoutKey(run_id, opening)


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
