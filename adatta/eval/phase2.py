"""The Phase 2 per-group acceptance test (docs/PHASE2_TASKS.md, "Per group"): one context
group's challenger against the 1b reference on the same dev grid, warm matches.

  1 mixing       refits over R-hat 1.01 / under ESS 400 / with divergences: no more than
                 the reference's on the same refits, give or take 2
  2 accuracy     on the group's active matches the primary score improves, paired 95%
                 interval excluding 0 (1X2 log loss for per-side groups, total CRPS for
                 total-goals groups)
  3 no harm      all warm matches: 1X2 log loss and RPS not worse (point estimate), OU2.5
                 log loss and total CRPS not worse than 0.1%, no bundle worse than 0.3% on
                 1X2 log loss
  4 calibration  active matches: expected calibration error (probability deciles) of each
                 1X2 outcome and of OU2.5 not worse than the reference's by more than 0.005
  5 sense        posterior coefficients per bundle, sign against the expectation, and
                 uninformed (posterior sd / prior sd over 0.9 in every bundle): reported for
                 the owner, not gated here
D25 / D26: rest and referee results for the Championship, La Liga and Segunda are reported
apart and labelled optimistic.

Metrics only: the report holds no match-level rows (CI logs are public).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from adatta.eval import harness
from adatta.model import covariates as cv

PER_SIDE = ("rest", "travel", "importance")
OPTIMISTIC_LEAGUES = (40, 140, 141)  # referee known under 24 h before kickoff (D26)
OPTIMISTIC_GROUPS = ("rest", "referee")
NO_HARM = {"1X2:log_loss": 0.0, "1X2:rps": 0.0, "OU2.5:log_loss": 0.001, "total:crps": 0.001}
BUNDLE_LIMIT = 0.003
ECE_LIMIT = 0.005
MIXING_SLACK = 2
UNINFORMED = 0.9
# expected sign of each coefficient on log lambda (docs/PHASE2_TASKS.md item 5)
EXPECTED = {
    ("rest", "short_rest"): -1, ("travel", "log_travel"): -1, ("travel", "tz_shift"): -1,
    ("travel", "altitude_climb"): -1, ("importance", "dead_rubber"): -1,
    ("weather", "heat"): -1, ("weather", "wind"): -1, ("weather", "rain"): -1,
}  # fmt: skip


def groups_of(model: str) -> tuple[str, ...]:
    """The context groups in a model name (`..._ctx-style-weather_hz` -> style, weather)."""
    if "_ctx-" not in model:
        return ()
    return tuple(model.split("_ctx-", 1)[1].split("_", 1)[0].split("-"))


def primary_metric(group: str) -> str:
    return "1X2:log_loss" if group in PER_SIDE else "total:crps"


def _rel(g: pl.DataFrame, metric: str, a: str, b: str) -> dict[str, float]:
    p = harness.paired(g, metric, a, b)
    base = float(g.filter(pl.col("model") == b)[metric].mean())  # type: ignore[arg-type]
    return {"n": p["n"], "rel": p["mean_diff"] / base, "lo": p["lo"] / base,
            "hi": p["hi"] / base}  # fmt: skip


def ece(prob: np.ndarray, hit: np.ndarray, bins: int = 10) -> float:
    """Expected calibration error over equal-count probability bins (deciles)."""
    if len(prob) == 0:
        return float("nan")
    order = np.argsort(prob, kind="stable")
    total = 0.0
    for chunk in np.array_split(order, bins):
        if len(chunk):
            total += len(chunk) * abs(float(prob[chunk].mean()) - float(hit[chunk].mean()))
    return total / len(prob)


def _ece_table(g: pl.DataFrame) -> dict[str, float]:
    hg, ag = g["hg"].to_numpy(), g["ag"].to_numpy()
    out = {}
    for name, col, hit in (
        ("home", "p_home", hg > ag),
        ("draw", "p_draw", hg == ag),
        ("away", "p_away", hg < ag),
        ("over2.5", "p_over25", hg + ag > 2),
    ):
        out[name] = ece(g[col].to_numpy(), hit.astype(float))
    return out


def _mixing(pred: pl.DataFrame, model: str) -> dict[str, int]:
    r = pred.filter(pl.col("model") == model).unique(["bundle", "as_of"])
    return {"rhat_over": int((r["refit_rhat_max"] > 1.01).sum()),
            "ess_under": int((r["refit_ess_bulk_min"] < 400).sum()),
            "with_divergences": int((r["refit_divergences"] > 0).sum()),
            "refits": r.height}  # fmt: skip


def sense(pred: pl.DataFrame, challenger: str, group: str) -> list[dict[str, Any]]:
    """Per bundle and coefficient: the mean over refits of the posterior mean and sd, the sd
    ratio to the prior (normal priors), and the sign against the expectation."""
    g = cv.GROUPS[group]
    refits = pred.filter(pl.col("model") == challenger).unique(["bundle", "as_of"])
    rows = []
    for col in g.columns:
        m, s = f"coef__{group}__{col}__mean", f"coef__{group}__{col}__sd"
        if m not in refits.columns:
            continue
        for (bundle,), b in refits.sort("bundle").group_by(["bundle"], maintain_order=True):
            mean, sd = float(b[m].mean()), float(b[s].mean())  # type: ignore[arg-type]
            ratio = sd / g.sd if g.prior == "normal" else float("nan")
            want = EXPECTED.get((group, col))
            rows.append(
                {
                    "bundle": str(bundle),
                    "coefficient": col,
                    "mean": mean,
                    "sd": sd,
                    "sd_ratio": ratio,
                    "expected_sign": want,
                    "sign_ok": None if want is None else bool(np.sign(mean) == want),
                }
            )
    return rows


def group_report(pred: pl.DataFrame, challenger: str, reference: str) -> dict[str, Any]:
    """The acceptance test for the challenger's single context group (see the docstring)."""
    groups = groups_of(challenger)
    if len(groups) != 1 or groups[0] not in cv.GROUPS:
        raise ValueError(f"{challenger}: one group per challenger, got {groups!r}")
    group = groups[0]
    both = pred.filter(pl.col("model").is_in([challenger, reference]) & ~pl.col("cold_start"))
    mine = both.filter(pl.col("model") == challenger)
    theirs = both.filter(pl.col("model") == reference)
    same_grid = set(zip(mine["match_id"], mine["as_of"], strict=True)) == set(
        zip(theirs["match_id"], theirs["as_of"], strict=True)
    )
    flag = f"active__{group}"
    active_ids = mine.filter(pl.col(flag))["match_id"].to_list()
    act = both.filter(pl.col("match_id").is_in(active_ids))
    metric = primary_metric(group)
    checks: list[dict[str, Any]] = []

    mx, mr = _mixing(pred, challenger), _mixing(pred, reference)
    for k in ("rhat_over", "ess_under", "with_divergences"):
        checks.append(
            {
                "item": 1,
                "name": f"mixing {k}",
                "value": mx[k],
                "limit": mr[k] + MIXING_SLACK,
                "passed": mx[k] <= mr[k] + MIXING_SLACK,
            }
        )
    acc = _rel(act, metric, challenger, reference)
    checks.append({"item": 2, "name": f"active {metric}", "value": acc["rel"],
                   "interval": [acc["lo"], acc["hi"]], "n": acc["n"], "limit": 0.0,
                   "passed": acc["hi"] < 0})  # fmt: skip
    for m, limit in NO_HARM.items():
        r = _rel(both, m, challenger, reference)
        checks.append({"item": 3, "name": f"all warm {m}", "value": r["rel"], "n": r["n"],
                       "limit": limit, "passed": r["rel"] <= limit})  # fmt: skip
    for (bundle,), b in both.sort("bundle").group_by(["bundle"], maintain_order=True):
        r = _rel(b, "1X2:log_loss", challenger, reference)
        checks.append(
            {
                "item": 3,
                "name": f"bundle {bundle} 1X2:log_loss",
                "value": r["rel"],
                "n": r["n"],
                "limit": BUNDLE_LIMIT,
                "passed": r["rel"] <= BUNDLE_LIMIT,
            }
        )
    ec, er = (_ece_table(act.filter(pl.col("model") == m).sort("match_id"))
              for m in (challenger, reference))  # fmt: skip
    for k in ec:
        checks.append({"item": 4, "name": f"ECE {k}", "value": ec[k] - er[k],
                       "challenger": ec[k], "reference": er[k], "limit": ECE_LIMIT,
                       "passed": ec[k] - er[k] <= ECE_LIMIT})  # fmt: skip
    optimistic = None
    if group in OPTIMISTIC_GROUPS:
        o = act.filter(pl.col("league_id").is_in(OPTIMISTIC_LEAGUES))
        rest = act.filter(~pl.col("league_id").is_in(OPTIMISTIC_LEAGUES))
        optimistic = {"optimistic (D25/D26)": _rel(o, metric, challenger, reference),
                      "other leagues": _rel(rest, metric, challenger, reference)}  # fmt: skip
    passed = same_grid and all(c["passed"] for c in checks)
    return {
        "group": group, "challenger": challenger, "reference": reference, "metric": metric,
        "same_grid": same_grid, "active_matches": len(set(active_ids)),
        "warm_matches": mine.height, "checks": checks, "sense": sense(pred, challenger, group),
        "optimistic": optimistic, "mixing": {"challenger": mx, "reference": mr},
        "verdict": "pass" if passed else "fail",
    }  # fmt: skip


def markdown(rep: dict[str, Any]) -> list[str]:
    def pct(x: float) -> str:
        return f"{100 * x:+.2f}%"

    lines = [f"## Phase 2 group {rep['group']}: {rep['challenger']} vs {rep['reference']} "
             f"— **{rep['verdict']}**", "",
             f"Grid identical: {'yes' if rep['same_grid'] else '**NO**'}; warm matches "
             f"{rep['warm_matches']}, active {rep['active_matches']}; primary metric "
             f"{rep['metric']}.", "",
             "| item | check | value | limit | pass |", "|---|---|---|---|---|"]  # fmt: skip
    for c in rep["checks"]:
        if c["item"] == 1:
            value, limit = str(c["value"]), str(c["limit"])
        elif c["item"] == 4:
            value = f"{c['value']:+.4f} ({c['challenger']:.4f} vs {c['reference']:.4f})"
            limit = f"+{c['limit']:.3f}"
        else:
            value = pct(c["value"]) + (f" [{pct(c['interval'][0])}, {pct(c['interval'][1])}]"
                                       if "interval" in c else "")  # fmt: skip
            limit = pct(c["limit"])
        lines.append(f"| {c['item']} | {c['name']} | {value} | {limit} | "
                     f"{'yes' if c['passed'] else '**no**'} |")  # fmt: skip
    if rep["optimistic"]:
        lines += ["", "Rest and referee leagues (D25/D26):", "", "| segment | n | rel | 95% |",
                  "|---|---|---|---|"]  # fmt: skip
        for name, r in rep["optimistic"].items():
            lines.append(f"| {name} | {r['n']:.0f} | {pct(r['rel'])} | "
                         f"[{pct(r['lo'])}, {pct(r['hi'])}] |")  # fmt: skip
    lines += ["", "Item 5, coefficients (mean over refits; owner review):", "",
              "| bundle | coefficient | mean | sd | sd / prior | expected sign | sign ok |",
              "|---|---|---|---|---|---|---|"]  # fmt: skip
    for s in rep["sense"]:
        lines.append(
            f"| {s['bundle']} | {s['coefficient']} | {s['mean']:+.3f} | {s['sd']:.3f} | "
            f"{s['sd_ratio']:.2f} | {s['expected_sign'] or '—'} | "
            f"{'—' if s['sign_ok'] is None else ('yes' if s['sign_ok'] else 'no')} |")  # fmt: skip
    uninformed = sorted(
        {s["coefficient"] for s in rep["sense"]}
        - {s["coefficient"] for s in rep["sense"] if not s["sd_ratio"] > UNINFORMED}
    )
    if uninformed:
        lines += ["", f"Uninformed (sd / prior over {UNINFORMED} in every bundle): "
                  + ", ".join(uninformed)]  # fmt: skip
    return lines


# ------------------------------------------------------- the combined-model gate (D36)


def combined_report(pred: pl.DataFrame, cmp: pl.DataFrame, challenger: str, reference: str,
                    gate: harness.Phase2Gate) -> dict[str, Any]:  # fmt: skip
    """The pre-registered Phase 2 combined-model gate (`config/evaluation.yml` phase2_gate;
    D36 routes, D28 segments) for `challenger` against the 1b `reference`. `cmp` is
    `backtest.compare(pred, challenger, reference)`: rel < 0 means the challenger is
    better. Passes if any route passes; every route also needs the league segments."""
    if gate.paired_interval != 0.95:
        raise ValueError("compare() reports 95% intervals; the gate asks for another level")
    overall = cmp.filter(pl.col("segment_type") == "all")

    def row(metric: str) -> dict[str, Any]:
        r = overall.filter(pl.col("metric") == metric)
        if r.height != 1:
            raise ValueError(f"no overall row for {metric}")
        return r.row(0, named=True)

    seg = cmp.filter(
        (pl.col("segment_type") == "league_id") & pl.col("metric").is_in(gate.segment_metrics)
    )
    leagues = seg["seg"].unique().to_list()
    have = set(zip(seg["seg"].to_list(), seg["metric"].to_list(), strict=True))
    missing = [(lg, m) for lg in leagues for m in gate.segment_metrics if (lg, m) not in have]
    if not leagues or missing:
        raise ValueError(f"segment table incomplete: {missing or 'no league segments'}")
    segments = [{"name": f"league {r['seg']} {r['metric']}", "value": r["rel"],
                 "limit": gate.max_segment_rel_worse,
                 "passed": r["rel"] <= gate.max_segment_rel_worse}
                for r in seg.sort("seg", "metric").iter_rows(named=True)]  # fmt: skip
    warm = pred.filter(pl.col("model").is_in([challenger, reference]) & ~pl.col("cold_start"))
    ec, er = (_ece_table(warm.filter(pl.col("model") == m).sort("match_id", "as_of"))
              for m in (challenger, reference))  # fmt: skip
    routes = []
    for route in gate.routes:
        checks: list[dict[str, Any]] = []
        for m in route.improve:
            r = row(m)
            checks.append({"name": f"{m} improves", "value": r["rel"],
                           "interval": [r["rel_lo"], r["rel_hi"]], "limit": 0.0,
                           "passed": r["rel_hi"] < 0})  # fmt: skip
        for m in route.no_worse:
            r = row(m)
            checks.append({"name": f"{m} no worse", "value": r["rel"], "limit": 0.0,
                           "passed": r["rel"] <= 0})  # fmt: skip
        if route.ece_limit is not None:
            for k in ec:
                checks.append({"name": f"ECE {k}", "value": ec[k] - er[k],
                               "challenger": ec[k], "reference": er[k],
                               "limit": route.ece_limit,
                               "passed": ec[k] - er[k] <= route.ece_limit})  # fmt: skip
        ok = all(c["passed"] for c in checks) and all(s["passed"] for s in segments)
        routes.append({"route": route.name, "checks": checks, "passed": ok})
    passed = [r["route"] for r in routes if r["passed"]]
    return {"challenger": challenger, "reference": reference, "groups": list(groups_of(challenger)),
            "warm_matches": int(row("1X2:log_loss")["n"]), "routes": routes,
            "segments": segments, "passed_routes": passed,
            "verdict": "pass" if passed else "fail",
            "gate": gate.model_dump()}  # fmt: skip


def combined_markdown(rep: dict[str, Any]) -> list[str]:
    def pct(x: float) -> str:
        return f"{100 * x:+.2f}%"

    via = f" via route {', '.join(rep['passed_routes'])}" if rep["passed_routes"] else ""
    lines = [f"## Phase 2 combined-model gate (D36): {rep['challenger']} vs {rep['reference']} "
             f"— **{rep['verdict']}**{via}", "",
             f"All warm matches ({rep['warm_matches']}), paired by match; passes if any route "
             "passes, and every route needs the league segments.", "",
             "| route | check | value | limit | pass |", "|---|---|---|---|---|"]  # fmt: skip
    for r in rep["routes"]:
        for c in r["checks"]:
            if "challenger" in c:
                value = f"{c['value']:+.4f} ({c['challenger']:.4f} vs {c['reference']:.4f})"
                limit = f"+{c['limit']:.3f}"
            else:
                value = pct(c["value"]) + (f" [{pct(c['interval'][0])}, {pct(c['interval'][1])}]"
                                           if "interval" in c else "")  # fmt: skip
                limit = pct(c["limit"])
            lines.append(f"| {r['route']} | {c['name']} | {value} | {limit} | "
                         f"{'yes' if c['passed'] else '**no**'} |")  # fmt: skip
    bad = [s for s in rep["segments"] if not s["passed"]]
    worst = max(rep["segments"], key=lambda s: s["value"])
    failing = ", ".join(f"{s['name']} {pct(s['value'])}" for s in bad)
    lines += ["", f"League segments: {len(rep['segments'])} checked, worst {worst['name']} "
              f"{pct(worst['value'])} (limit {pct(rep['gate']['max_segment_rel_worse'])}); "
              + (f"**failing: {failing}**" if bad else "all pass.")]  # fmt: skip
    return lines
