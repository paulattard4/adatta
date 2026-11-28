"""P3-j step 2 (docs/PHASE3_TASKS.md P3-j (c), D65): whether J goes to the live shadow.
Pure numpy / polars.

Models in the scored frame (column `model`): P (production 1b, its cached weekly dev
refits), J_lk (J with the targets' announced-XI signals at T-60m) and J_24 (P3-b's
projected XI at T-24h). Dev is the tuning window: it decides only whether J is shadowed
live, where D59 judges it once.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from adatta.eval import lineup as el

BUDGET = 1.5  # J's median refit time at most 1.5x P's (as P3-d)
LIVE_N = 1800  # league matches expected in the live window (D65 power statement)
Z_ALPHA, Z_POWER = 1.959963984540054, 0.8416212335729143  # two-sided 5%, 80% power


def power(pred: pl.DataFrame, n_live: int = LIVE_N, ch: str = "J_lk") -> dict[str, float]:
    """The relative 1X2 log loss gain detectable at n_live matches (80% power, two-sided
    5%), from the per-match spread of the challenger (J_lk) - P on dev."""
    g = el.pair_frame(pred, ch, "P").pivot(on="model", index="match_id",
                                           values="1X2:log_loss")  # fmt: skip
    d = (g[ch] - g["P"]).to_numpy()
    base = float(g["P"].mean())  # type: ignore[arg-type]
    sd = float(d.std(ddof=1)) if len(d) > 1 else float("nan")
    return {"n_dev": float(len(d)), "sd_diff": sd, "base": base, "n_live": float(n_live),
            "detectable": (Z_ALPHA + Z_POWER) * sd / np.sqrt(n_live) / base}  # fmt: skip


def verdict(pred: pl.DataFrame, diags: list[dict[str, Any]], leakage_ok: bool | None,
            ch: str = "J_lk", fitted: str = "J") -> el.Verdict:  # fmt: skip
    """Step 2's rule: at T-60m J's 1X2 log loss and RPS point estimates beat P's, no league
    x market segment is worse than 1.0%, every J refit passes the gates (after its one
    redo), J's median refit is within BUDGET x P's, and the signals' leakage check
    passes. `ch` / `fitted` name another challenger judged by the same rule (P3-k's Q);
    leakage_ok None leaves out the signals check (no new pre-match signal)."""
    checks: list[dict[str, Any]] = []
    lk = el.pair_frame(pred, ch, "P")
    for m in ("1X2:log_loss", "1X2:rps"):
        r = el._rel(lk, m, ch, "P")
        checks.append({"check": f"{fitted} vs P at T-60m, {m} (point estimate below 0)",
                       "value": r["rel"], "lo": r["lo"], "hi": r["hi"],
                       "passed": r["rel"] < 0})  # fmt: skip
    for (lg,), g in sorted(lk.group_by(["league_id"]), key=lambda x: int(x[0][0])):
        for m in el.SEGMENT_METRICS:
            r = el._rel(g, m, ch, "P")
            checks.append({"check": f"league {lg} {m} vs P (at most +1.0%)",
                           "value": r["rel"], "passed": r["rel"] <= el.SEGMENT_MAX})  # fmt: skip
    gates = [g for g in diags if g["model"] == fitted]
    failing = [g for g in gates if not g["gates_ok"]]
    checks.append({"check": f"inference gates, every {fitted} refit",
                   "value": float(len(failing)),
                   "passed": bool(gates) and not failing})  # fmt: skip
    timed = [g["fit_s"] for g in gates if not g.get("redone")]
    t_j = float(np.median(timed)) if timed else float("nan")
    p_fit = [g["fit_s"] for g in diags if g["model"] == "P" and g.get("fit_s")]
    t_p = float(np.median(p_fit)) if p_fit else float("nan")
    ratio = t_j / t_p if t_p else float("nan")
    checks.append({"check": f"{fitted} median refit within {BUDGET}x P's", "value": ratio,
                   "passed": bool(np.isfinite(ratio)) and ratio <= BUDGET})  # fmt: skip
    if leakage_ok is not None:
        checks.append({"check": "signals leakage check", "value": 1.0 if leakage_ok else 0.0,
                       "passed": leakage_ok})  # fmt: skip
    return el.Verdict(all(c["passed"] for c in checks), checks)


def _rel_row(pred: pl.DataFrame, a: str, b: str, label: str) -> str:
    g = el.pair_frame(pred, a, b)
    if g.is_empty():
        return f"| {label} | 0 | | | | |"
    r1, r2 = el._rel(g, "1X2:log_loss", a, b), el._rel(g, "1X2:rps", a, b)
    return (f"| {label} | {int(r1['n'])} | {el._f(r1['rel'], True)} | "
            f"[{el._f(r1['lo'], True)}, {el._f(r1['hi'], True)}] | {el._f(r2['rel'], True)} | "
            f"[{el._f(r2['lo'], True)}, {el._f(r2['hi'], True)}] |")  # fmt: skip


def markdown(pred: pl.DataFrame, v: el.Verdict, diags: list[dict[str, Any]],
             meta: dict[str, Any], digest: str) -> str:  # fmt: skip
    """Metrics only (the repo is public)."""
    lines = [f"# P3-j step 2: J vs P on dev, signals table {digest}", "",
             f"**Go live (shadow): {'yes' if v.passed else 'no'}**", "",
             "| check | value | 95% interval | pass |", "|---|---|---|---|"]  # fmt: skip
    for c in v.checks:
        pct = any(x in c["check"] for x in ("log_loss", "rps", "crps"))
        iv = f"[{el._f(c['lo'], True)}, {el._f(c['hi'], True)}]" if "lo" in c else ""
        lines.append(f"| {c['check']} | {el._f(c['value'], pct)} | {iv} | "
                     f"{'yes' if c['passed'] else 'NO'} |")  # fmt: skip
    lines += ["", "## J vs P (warm matches, relative; negative = J better)", "",
              "| horizon | n | 1X2 log loss | 95% interval | RPS | 95% interval |",
              "|---|---|---|---|---|---|",
              _rel_row(pred, "J_lk", "P", "T-60m (announced XI)"),
              _rel_row(pred, "J_24", "P", "T-24h (projected XI, reported)")]  # fmt: skip
    for by in ("league_id", "season"):
        lines += ["", f"## J vs P at T-60m by {by}", ""]
        lines += el.segment_table(pred, by, "J_lk", "P")
    pw = power(pred)
    lines += ["", "## Power for the live window (D59, recomputed from dev)", "",
              f"Per-match J - P log loss sd {pw['sd_diff']:.4f} over {int(pw['n_dev'])} dev "
              f"matches, P's mean {pw['base']:.4f}: at {int(pw['n_live'])} live matches the "
              f"detectable gain is {pw['detectable']:.2%} (80% power, two-sided 5%)."]  # fmt: skip
    lines += ["", "## Fits", ""]
    for m in ("P", "J"):
        g = [d for d in diags if d["model"] == m]
        if not g:
            continue
        lines.append(
            f"- {m}: {len(g)} refits ({sum(bool(d.get('cached')) for d in g)} cached, "
            f"{sum(bool(d.get('redone')) for d in g)} redone), "
            f"{sum(not d['gates_ok'] for d in g)} failing the gates; worst R-hat "
            f"{max(d['rhat_max'] for d in g):.3f}, min ESS "
            f"{min(min(d['ess_bulk_min'], d['ess_tail_min']) for d in g):.0f}, divergences "
            f"{sum(d['divergences'] for d in g):.0f}; median fit "
            f"{float(np.median([d['fit_s'] for d in g])):.0f} s"
        )  # fmt: skip
    for s in meta.get("signals", []):
        b = [d[f"beta_{s}"] for d in diags if d["model"] == "J" and f"beta_{s}" in d]
        if b:
            lines.append(f"- beta {s} (per warm-up sd): median {np.median(b):+.3f}, range "
                         f"{min(b):+.3f} to {max(b):+.3f}")  # fmt: skip
    for f in [d for d in diags if d["model"] == "J" and not d["gates_ok"]][:20]:
        lines.append(f"- gates fail: J {f['bundle']} {f['as_of']}: rhat {f['rhat_max']:.3f}, "
                     f"ess {min(f['ess_bulk_min'], f['ess_tail_min']):.0f}, "
                     f"div {f['divergences']:.0f}")  # fmt: skip
    leak = meta.get("leakage", {})
    lines += ["", f"Signals: {', '.join(meta.get('signals', []))}; warm-up sds "
              + ", ".join(f"{k} {v:.4f}" for k, v in meta.get("scale", {}).items())
              + f"; leakage {leak.get('changed')} of {leak.get('n')} rebuilt changed."]  # fmt: skip
    return "\n".join(lines) + "\n"
