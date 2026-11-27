"""P3b-3's dev gate and window (i) (docs/PHASE3B_TASKS.md P3b-3 (c)-(f), D66). Pure numpy /
polars. Rows: one per match, model (B_clim, C0, C1, C2) and horizon ("60", "24"; "" for
B_clim), with the scores of `model.cards.score`. Diagnostics: one per refit and variant,
`ok` = the inference gates, the dispersion PPC and (C2) the prior sensitivity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import polars as pl

from adatta.eval import knockout as kb

VARIANTS = ("C0", "C1", "C2")
FALLBACK = {"C2": "C1", "C1": "C0", "C0": "B_clim"}
REFIT_PASS = 0.97  # the chosen variant passes (c) at >= 97% of refits
ECE_MARGIN = 0.005
SEGMENT_MAX = 0.010
LINES = (3.5, 4.5, 5.5)
TEAM_LINES = (1.5, 2.5)
Z95 = 1.959963984540054


def effective(pred: pl.DataFrame, diags: list[dict[str, Any]], v: str,
              horizon: str = "60") -> pl.DataFrame:  # fmt: skip
    """Variant v's rows at the horizon, a refit failing (c) replaced by the next simpler
    variant's rows for that refit (down to B_clim)."""
    if v == "B_clim":
        return pred.filter(pl.col("model") == "B_clim")
    bad = [datetime.fromisoformat(g["as_of"]) for g in diags
           if g["variant"] == v and not g["ok"]]  # fmt: skip
    rows = pred.filter((pl.col("model") == v) & (pl.col("horizon") == horizon))
    keep = rows.filter(~pl.col("as_of").is_in(bad))
    if not bad:
        return rows
    lower = effective(pred, diags, FALLBACK[v], horizon)
    swap = lower.join(rows.filter(~pl.col("match_id").is_in(keep["match_id"].implode()))
                      .select("match_id"), on="match_id")  # fmt: skip
    return pl.concat([keep, swap.select(keep.columns)])


def paired(a: pl.DataFrame, b: pl.DataFrame, metric: str) -> dict[str, float]:
    """Mean of a - b over shared matches, its 95% interval, and relative to b's mean."""
    j = a.select("match_id", x=metric).join(b.select("match_id", y=metric), on="match_id")
    d = (j["x"] - j["y"]).to_numpy()
    m = float(d.mean()) if len(d) else float("nan")
    se = float(d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 1 else float("nan")
    base = float(j["y"].mean()) if len(d) else float("nan")  # type: ignore[arg-type]
    return {"n": float(len(d)), "diff": m, "lo": m - Z95 * se, "hi": m + Z95 * se,
            "rel": m / base if base else float("nan")}  # fmt: skip


def calibration(rows: pl.DataFrame) -> dict[str, Any]:
    """ECE of each total O/U line against its noise floor (+ ECE_MARGIN)."""
    y = (rows["y_home"] + rows["y_away"]).to_numpy()
    out: dict[str, Any] = {}
    for line in LINES:
        p = rows[f"p_over_{line}"].to_numpy()
        e, null = kb.ece(p, (y > line).astype(float)), kb.ece_null(p)
        out[str(line)] = {"ece": e, "noise_floor": null, "pass": e <= null + ECE_MARGIN}
    return out


def team_calibration(rows: pl.DataFrame) -> dict[str, Any]:
    """Reported (D67): ECE of the team O/U lines, both sides pooled, against the noise
    floor. Team markets are not served without their own check."""
    out: dict[str, Any] = {}
    for line in TEAM_LINES:
        p = np.concatenate([rows[f"p_team_home_{line}"].to_numpy(),
                            rows[f"p_team_away_{line}"].to_numpy()])  # fmt: skip
        y = np.concatenate([rows["y_home"].to_numpy(), rows["y_away"].to_numpy()]) > line
        e, null = kb.ece(p, y.astype(float)), kb.ece_null(p)
        out[str(line)] = {"ece": e, "noise_floor": null, "within": e <= null + ECE_MARGIN}
    return out


def gate(pred: pl.DataFrame, diags: list[dict[str, Any]]) -> dict[str, Any]:
    """(e) 1-6 on dev at T-60m."""
    clim = effective(pred, diags, "B_clim")
    eff = {v: effective(pred, diags, v) for v in VARIANTS}
    out: dict[str, Any] = {"n_matches": clim.height}
    # 2. the variant, by total-cards RPS
    chosen, steps = "C0", []
    for v in ("C1", "C2"):
        r = paired(eff[v], eff[chosen], "rps_total")
        steps.append({"challenger": v, "against": chosen, **r, "replaces": r["hi"] < 0})
        if r["hi"] < 0:
            chosen = v
    out["c2"] = {"steps": steps, "chosen": chosen}
    ok_share = {v: float(np.mean([g["ok"] for g in diags if g["variant"] == v]))
                for v in VARIANTS}  # fmt: skip
    out["refits_ok"] = ok_share
    out["c2"]["passes"] = ok_share[chosen] >= REFIT_PASS
    # 1. coherence: the totals are convolutions (asserted when scored); the cap's tail
    out["c1"] = {"max_tail": float(pred.filter(pl.col("model") != "B_clim")["tail"].max()  # type: ignore[arg-type]
                                   or 0.0), "passes": True}  # fmt: skip
    # 3. better than climatology
    c = eff[chosen]
    c3 = {m: paired(c, clim, m) for m in ("rps_total", "ll_over_4.5")}
    out["c3"] = {**c3, "passes": all(x["hi"] < 0 for x in c3.values())}
    # 4. calibrated
    cal = calibration(c)
    out["c4"] = {"ece": cal, "passes": all(x["pass"] for x in cal.values())}
    # 5. segments
    seg = []
    for (lg,), g in sorted(c.group_by(["league_id"]), key=lambda x: int(x[0][0])):
        ids = g.select("match_id")
        r = paired(g, clim.join(ids, on="match_id"), "rps_total")
        item = {"league": int(lg), "vs_clim": r["rel"]}
        if chosen == "C2":
            item["vs_C1"] = paired(g, eff["C1"].join(ids, on="match_id"), "rps_total")["rel"]
        seg.append(item)
    out["c5"] = {
        "leagues": seg,
        "passes": all(
            s["vs_clim"] <= SEGMENT_MAX and s.get("vs_C1", 0.0) <= SEGMENT_MAX for s in seg
        ),
    }
    # 6. leakage: the unit tests (as P3b-1)
    out["c6"] = {"passes": True, "how": "tests/test_cards.py"}
    out["passed"] = all(out[k]["passes"] for k in ("c1", "c2", "c3", "c4", "c5", "c6"))
    out["chosen"] = chosen
    # reported
    out["scores"] = {v: {m: float(effective(pred, diags, v, h)[m].mean() or 0.0)  # type: ignore[arg-type]
                         for m in ("rps_total", "logs_total", "ll_over_4.5", "ll_team_1.5")}
                     | {"horizon": h}
                     for v in ("B_clim", *VARIANTS) for h in (("",) if v == "B_clim"
                                                              else ("60",))}  # fmt: skip
    out["t24"] = {v: paired(effective(pred, diags, v, "24"), clim, "rps_total")
                  for v in VARIANTS}  # fmt: skip
    med = {k: [g[k] for g in diags if k in g] for k in ("sigma_ref", "sigma_a", "sigma_b",
                                                          "phi")}  # fmt: skip
    out["sigmas"] = {k: [float(np.median(x)) if x else float("nan")] for k, x in med.items()}
    out["team_ece"] = team_calibration(c)
    out["ppc_team_q"] = float(np.median([g["ppc"]["disp_team"]["q"] for g in diags
                                         if g["variant"] == chosen]))  # fmt: skip
    out["ppc_corr"] = float(np.median([g["ppc"]["corr"]["obs"] - g["ppc"]["corr"]["rep_mean"]
                                       for g in diags if g["variant"] == chosen]))  # fmt: skip
    return out


def window(pred: pl.DataFrame, diags: list[dict[str, Any]], chosen: str) -> dict[str, Any]:
    """(f): checks 1, 3 and 4 on window (i) with the dev-chosen variant."""
    clim = effective(pred, diags, "B_clim")
    c = effective(pred, diags, chosen)
    c3 = {m: paired(c, clim, m) for m in ("rps_total", "ll_over_4.5")}
    cal = calibration(c)
    out = {"chosen": chosen, "n_matches": c.height,
           "c1": {"max_tail": float(pred.filter(pl.col("model") == chosen)["tail"].max()  # type: ignore[arg-type]
                                    or 0.0), "passes": True},
           "c3": {**c3, "passes": all(x["hi"] < 0 for x in c3.values())},
           "c4": {"ece": cal, "passes": all(x["pass"] for x in cal.values())}}  # fmt: skip
    out["passed"] = all(out[k]["passes"] for k in ("c1", "c3", "c4"))  # type: ignore[index]
    return out


def _pct(r: dict[str, float]) -> str:
    return (f"{r['rel']:+.2%} (diff {r['diff']:+.4f} [{r['lo']:+.4f}, {r['hi']:+.4f}], "
            f"n {int(r['n'])})")  # fmt: skip


def markdown(v: dict[str, Any], digest: str) -> str:
    """Metrics only."""
    lines = [f"# P3b-3 cards on dev (T-60m), cards table {digest}", "",
             f"**Dev gate: {'PASS' if v['passed'] else 'FAIL'}; chosen {v['chosen']}** "
             f"({v['n_matches']} matches)", ""]  # fmt: skip
    for s in v["c2"]["steps"]:
        lines.append(f"- 2 {s['challenger']} vs {s['against']}, total-cards RPS: {_pct(s)}: "
                     f"{'replaces' if s['replaces'] else 'stays out'}")  # fmt: skip
    lines.append(
        "- 2 refits passing (c): "
        + ", ".join(f"{k} {x:.1%}" for k, x in v["refits_ok"].items())
        + f" (chosen needs {REFIT_PASS:.0%}): {'pass' if v['c2']['passes'] else 'FAIL'}"
    )
    lines.append(f"- 1 coherence: largest tail beyond the caps {v['c1']['max_tail']:.1e}")
    for m in ("rps_total", "ll_over_4.5"):
        lines.append(f"- 3 {v['chosen']} vs B_clim, {m}: {_pct(v['c3'][m])}")
    lines.append(f"- 3: {'pass' if v['c3']['passes'] else 'FAIL'}")
    lines.append(
        "- 4 ECE (floor): "
        + "; ".join(
            f"O/U {k} {x['ece']:.4f} ({x['noise_floor']:.4f})" for k, x in v["c4"]["ece"].items()
        )
        + f": {'pass' if v['c4']['passes'] else 'FAIL'}"
    )
    lines.append("- 5 by league, RPS vs B_clim" + (" / vs C1" if v["chosen"] == "C2" else "")
                 + ": " + "; ".join(
                     f"{s['league']} {s['vs_clim']:+.2%}"
                     + (f" / {s['vs_C1']:+.2%}" if "vs_C1" in s else "")
                     for s in v["c5"]["leagues"])
                 + f": {'pass' if v['c5']['passes'] else 'FAIL'}")  # fmt: skip
    lines.append("- 6 leakage: the unit tests")
    lines += ["", "Reported:"]
    for k, s in v["scores"].items():
        lines.append(f"- {k}: " + ", ".join(f"{m} {x:.4f}" for m, x in s.items()
                                             if m != "horizon"))  # fmt: skip
    lines.append("- T-24h vs B_clim, RPS: " + "; ".join(f"{k} {r['rel']:+.2%}"
                                                       for k, r in v["t24"].items()))  # fmt: skip
    lines.append(
        "- team O/U ECE (floor), reported (D67): "
        + "; ".join(
            f"{k} {x['ece']:.4f} ({x['noise_floor']:.4f})" for k, x in v["team_ece"].items()
        )
        + f"; team-dispersion PPC quantile, median over refits {v['ppc_team_q']:.3f}"
    )
    lines.append(
        "- medians: "
        + ", ".join(f"{k} {x[0]:.3f}" for k, x in v["sigmas"].items())
        + f"; teams' count correlation, observed minus replicated {v['ppc_corr']:+.3f}"
    )
    return "\n".join(lines) + "\n"


def window_markdown(w: dict[str, Any], digest: str) -> str:
    lines = [f"# P3b-3 window (i): 2026/27 to 2026-11-27, cards table {digest}", "",
             f"**{'PASS' if w['passed'] else 'FAIL'}** ({w['chosen']}, {w['n_matches']} "
             "matches)", ""]  # fmt: skip
    for m in ("rps_total", "ll_over_4.5"):
        lines.append(f"- 3 vs B_clim, {m}: {_pct(w['c3'][m])}")
    lines.append(
        "- 4 ECE (floor): "
        + "; ".join(
            f"O/U {k} {x['ece']:.4f} ({x['noise_floor']:.4f})" for k, x in w["c4"]["ece"].items()
        )
    )
    lines.append(f"- 1 largest tail {w['c1']['max_tail']:.1e}")
    return "\n".join(lines) + "\n"
