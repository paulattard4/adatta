"""D68 acceptance (docs/PHASE1B_TASKS.md "8. Faster refits"): the warm refits (W) against
P's cold refits of the same dev weeks, scored on the same targets. Pure functions over the
rows and per-refit diagnostics `adatta.model.warm.run_part` writes.

Checks (stated before building; dev only; 1a as amended before any run, owner 2026-11-28:
"Approve as drafted"):
  1a  per refit, z_mc = |mean difference| / its Monte Carlo standard error: every refit's
      largest z_mc <= Z_MC_ALL, and the mean z_mc^2 over every parameter and refit
      <= Z_MC_SQ_MEAN (the cached cold draws are 400, thinned)
  1b  the paired mean difference in 1X2 log loss and in RPS, W - P, within +/-REL of P's
  2   W's share of refits passing the gates >= P's
  3   median W refit time <= (1 - SPEED) x P's
Reported beside 1a: the approved form it replaced, |mean difference| / cold sd <= Z_REFIT on
>= Z_SHARE of refits and <= Z_ALL on all, which the cached draws' noise alone nears.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from adatta.eval import harness

Z_REFIT, Z_SHARE, Z_ALL = 0.10, 0.95, 0.20
REL = 0.001  # +/-0.10% of P's value (D59's margin, as an equivalence bound)
SPEED = 0.25
METRICS = ("1X2:log_loss", "1X2:rps")
Z_MC_ALL = 5.0  # no refit's largest z_mc above this
Z_MC_SQ_MEAN = 1.5  # mean z_mc^2 over every parameter and refit (1 when equal)


def _refits(diags: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    return [g for g in diags if g["model"] == model]


def verdict(pred: pl.DataFrame, diags: list[dict[str, Any]]) -> dict[str, Any]:
    w, p = _refits(diags, "W"), _refits(diags, "P")
    if not w or len(w) != len(p):
        raise ValueError(f"{len(w)} W refits against {len(p)} P refits")
    z = np.array([g["z_max"] for g in w])
    zmc = np.array([g["z_mc_max"] for g in w])
    sq = sum(g["z_mc_sq_sum"] for g in w) / sum(g["n_params"] for g in w)
    share = float(np.mean(z <= Z_REFIT))
    scores = {}
    for m in METRICS:
        d = harness.paired(pred, m, "W", "P")
        base = float(pred.filter(pl.col("model") == "P")[m].mean())  # type: ignore[arg-type]
        scores[m] = {**d, "p": base, "rel": d["mean_diff"] / base,
                     "rel_lo": d["lo"] / base, "rel_hi": d["hi"] / base}  # fmt: skip
    pass_w = float(np.mean([g["gates_ok"] for g in w]))
    pass_p = float(np.mean([g["gates_ok"] for g in p]))
    t_w = float(np.median([g["fit_s"] for g in w]))
    t_p = float(np.median([g["fit_s"] for g in p]))
    warm = [g for g in w if g.get("warm_weeks", -1) >= 0]
    checks = {
        f"1a max z_mc <= {Z_MC_ALL:g} on every refit and mean z_mc^2 <= {Z_MC_SQ_MEAN:g}":
            bool(zmc.max() <= Z_MC_ALL and sq <= Z_MC_SQ_MEAN),
        **{f"1b {m} within +/-{REL:.2%} of P": bool(abs(s["rel"]) <= REL)
           for m, s in scores.items()},
        "2 W passes the gates at least as often as P": bool(pass_w >= pass_p),
        f"3 median W refit time at least {SPEED:.0%} below P's": bool(t_w <= (1 - SPEED) * t_p),
    }  # fmt: skip
    return {
        "refits": len(w), "warm": len(warm), "cold": len(w) - len(warm),
        "z": {"share_le": share, "max": float(z.max()), "median": float(np.median(z)),
              "replaced_form_pass": bool(share >= Z_SHARE and z.max() <= Z_ALL)},
        "z_mc": {"max": float(zmc.max()), "median": float(np.median(zmc)),
                 "sq_mean": float(sq)},
        "scores": scores,
        "gates": {"W": pass_w, "P": pass_p},
        "fit_s": {"W": t_w, "P": t_p,
                  "W_warm": float(np.median([g["fit_s"] for g in warm])) if warm else None,
                  "W_cold": float(np.median([g["fit_s"] for g in w if g not in warm]))
                  if len(warm) < len(w) else None},
        "checks": checks,
        "passed": all(checks.values()),
    }  # fmt: skip


def markdown(v: dict[str, Any]) -> str:
    z, zm, t = v["z"], v["z_mc"], v["fit_s"]
    lines = [
        "# D68 warm refits: dev check", "",
        f"{v['refits']} refits ({v['warm']} warm, {v['cold']} cold starts).",
        f"Posterior agreement against Monte Carlo error: max z_mc {zm['max']:.2f}, median "
        f"{zm['median']:.2f}, mean z_mc^2 {zm['sq_mean']:.3f}.",
        f"In cold sds (reported; the replaced form): max z {z['max']:.3f}, median "
        f"{z['median']:.3f}, {z['share_le']:.1%} of refits <= {Z_REFIT} "
        f"({'pass' if z['replaced_form_pass'] else 'fail'}).",
    ]  # fmt: skip
    for m, s in v["scores"].items():
        lines.append(f"{m}: P {s['p']:.5f}, W - P {s['mean_diff']:+.6f} ({s['rel']:+.4%}, "
                     f"[{s['rel_lo']:+.4%}, {s['rel_hi']:+.4%}]), n {int(s['n'])}.")  # fmt: skip
    lines += [
        f"Gates passed: W {v['gates']['W']:.1%}, P {v['gates']['P']:.1%}.",
        f"Median refit time: W {t['W']:.0f} s (warm "
        + (f"{t['W_warm']:.0f}" if t["W_warm"] is not None else "-") + " s, cold "
        + (f"{t['W_cold']:.0f}" if t["W_cold"] is not None else "-")
        + f" s), P {t['P']:.0f} s.", "",
        "Checks: " + "; ".join(f"{k}: {'pass' if ok else 'FAIL'}"
                               for k, ok in v["checks"].items()),
        f"**D68 warm refits {'PASS' if v['passed'] else 'FAIL'}.**",
    ]  # fmt: skip
    return "\n".join(lines) + "\n"
