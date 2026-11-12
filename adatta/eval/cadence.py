"""The results-triggered refit acceptance test (D33; docs/PHASE1B_TASKS.md section 7b, item
3): the results-grid run (`_rd`) against a weekly run of the same spec, paired by match,
warm matches.

  affected       the matches the results grid predicts from a trigger refit (as_of not a
                 Monday 00:00): mean 1X2 log loss and RPS differences <= 0
  all            the same over every warm match
  unchanged      every other match gets the same probabilities from both runs (the Monday
                 fits are shared), reported as the largest difference
  calibration    ECE of each 1X2 outcome on the affected matches no worse by more than 0.005
  gates          the share of trigger refits failing the inference gates is no more than
                 the Monday refits' share + 2 points

A gain whose interval excludes 0 is not required: the grid adds information, not a model
term. Metrics only: no match-level rows (CI logs are public).
"""

from __future__ import annotations

from typing import Any

import polars as pl

from adatta.eval import phase2

METRICS = ("1X2:log_loss", "1X2:rps")
ECE_LIMIT = 0.005
GATE_SLACK = 0.02


def is_monday(col: str = "as_of") -> pl.Expr:
    c = pl.col(col)
    return (c.dt.weekday() == 1) & (c.dt.hour() == 0) & (c.dt.minute() == 0) & (c.dt.second() == 0)


def _gate_share(refits: pl.DataFrame) -> float:
    if refits.height == 0:
        return float("nan")
    bad = (
        (refits["refit_rhat_max"] > 1.01)
        | (refits["refit_ess_bulk_min"] < 400)
        | (refits["refit_divergences"] > 0)
    )
    return float(bad.sum()) / refits.height


def report(pred: pl.DataFrame, challenger: str, reference: str) -> dict[str, Any]:
    both = pred.filter(pl.col("model").is_in([challenger, reference]) & ~pl.col("cold_start"))
    mine = both.filter(pl.col("model") == challenger)
    affected_ids = mine.filter(~is_monday())["match_id"].unique().to_list()
    aff = both.filter(pl.col("match_id").is_in(affected_ids))
    checks: list[dict[str, Any]] = []
    for scope, g in (("affected", aff), ("all", both)):
        for m in METRICS:
            r = phase2._rel(g, m, challenger, reference)
            checks.append({"name": f"{scope} {m}", "value": r["rel"], "n": r["n"],
                           "interval": [r["lo"], r["hi"]], "limit": 0.0,
                           "passed": r["rel"] <= 0})  # fmt: skip
    ec, er = (
        phase2._ece_table(aff.filter(pl.col("model") == m).sort("match_id"))
        for m in (challenger, reference)
    )
    for k in ("home", "draw", "away"):
        checks.append({"name": f"ECE {k} (affected)", "value": ec[k] - er[k],
                       "challenger": ec[k], "reference": er[k], "limit": ECE_LIMIT,
                       "passed": ec[k] - er[k] <= ECE_LIMIT})  # fmt: skip
    refits = pred.filter(pl.col("model") == challenger).unique(["bundle", "as_of"])
    trig = _gate_share(refits.filter(~is_monday()))
    mon = _gate_share(refits.filter(is_monday()))
    checks.append({"name": "trigger refits failing the gates", "value": trig, "monday": mon,
                   "limit": mon + GATE_SLACK, "passed": trig <= mon + GATE_SLACK})  # fmt: skip
    rest = (
        both.filter(~pl.col("match_id").is_in(affected_ids))
        .pivot(on="model", index="match_id", values="p_home")
        .drop_nulls()
    )
    unchanged = (
        float((rest[challenger] - rest[reference]).abs().max() or 0.0)  # type: ignore[arg-type]
        if rest.height and {challenger, reference} <= set(rest.columns)
        else float("nan")
    )
    n_trig = refits.filter(~is_monday()).height
    return {
        "challenger": challenger, "reference": reference, "warm_matches": mine.height,
        "affected_matches": len(affected_ids), "trigger_refits": n_trig,
        "monday_refits": refits.height - n_trig, "unchanged_max_diff": unchanged,
        "checks": checks, "verdict": "pass" if all(c["passed"] for c in checks) else "fail",
    }  # fmt: skip


def markdown(rep: dict[str, Any]) -> list[str]:
    def pct(x: float) -> str:
        return f"{100 * x:+.2f}%"

    lines = [
        f"## Results-triggered refits: {rep['challenger']} vs {rep['reference']} — "
        f"**{rep['verdict']}**", "",
        f"Warm matches {rep['warm_matches']}, predicted from a trigger refit "
        f"{rep['affected_matches']}; refits: {rep['monday_refits']} Monday, "
        f"{rep['trigger_refits']} trigger. Largest p_home difference on the other matches "
        f"(should be 0): {rep['unchanged_max_diff']:.2g}.", "",
        "| check | value | limit | pass |", "|---|---|---|---|",
    ]  # fmt: skip
    for c in rep["checks"]:
        if "interval" in c:
            lo, hi = c["interval"]
            v = f"{pct(c['value'])} [{pct(lo)}, {pct(hi)}] (n {c['n']:.0f})"
            lim = pct(c["limit"])
        elif c["name"].startswith("ECE"):
            v = f"{c['value']:+.4f} ({c['challenger']:.4f} vs {c['reference']:.4f})"
            lim = f"+{c['limit']:.3f}"
        else:
            v = f"{100 * c['value']:.1f}% (Monday {100 * c['monday']:.1f}%)"
            lim = f"{100 * c['limit']:.1f}%"
        lines.append(f"| {c['name']} | {v} | {lim} | {'yes' if c['passed'] else '**no**'} |")
    return lines
