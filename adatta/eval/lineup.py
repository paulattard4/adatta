"""P3-d evaluation (docs/PHASE3_TASKS.md P3-d (c)-(e)): the serving-only coefficient, the
pass rule and the report. Pure numpy / polars.

Models in the scored frame (column `model`): P (production 1b), and per horizon h in
(lk, 24) S_h (P's draws + beta_S x delta_h) and L_h (offset in the likelihood, its own
beta draws x delta_h). P has no offset, so one P row per match serves both horizons.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.eval import harness

Arr = NDArray[np.float64]
HORIZONS = ("lk", "24")
SEGMENT_MAX = 0.010  # D28: no league x market segment worse than 1.0% (point estimate)
SEGMENT_METRICS = ("1X2:log_loss", "1X2:rps", "OU2.5:log_loss", "BTTS:log_loss", "total:crps")
BUDGET = 1.5  # L's median refit time at most 1.5x P's on the same runners
BIG_Q = 0.9  # matches with |delta| above its 90th percentile: reported apart
BETA_BOUND = 5.0


def serving_beta(lam_h: Arr, lam_a: Arr, hg: Arr, ag: Arr, dh: Arr, da: Arr,
                 iters: int = 50) -> float:  # fmt: skip
    """Maximum-likelihood beta of y ~ Poisson(lam x exp(beta x delta)) over both sides of
    the training matches, lam fixed at P's posterior mean (Newton, bounded)."""
    lam = np.concatenate([lam_h, lam_a])
    y = np.concatenate([hg, ag]).astype(np.float64)
    d = np.concatenate([dh, da])
    on = (d != 0) & np.isfinite(d) & np.isfinite(lam) & (lam > 0)
    if on.sum() < 10:
        return 0.0
    lam, y, d = lam[on], y[on], d[on]
    b = 0.0
    for _ in range(iters):
        m = lam * np.exp(b * d)
        g = float(np.sum((y - m) * d))
        h = float(np.sum(m * d * d))
        if h <= 0:
            break
        step = g / h
        b = float(np.clip(b + step, -BETA_BOUND, BETA_BOUND))
        if abs(step) < 1e-8:
            break
    return b


def _rel(g: pl.DataFrame, metric: str, a: str, b: str) -> dict[str, float]:
    p = harness.paired(g, metric, a, b)
    base = float(g.filter(pl.col("model") == b)[metric].mean())  # type: ignore[arg-type]
    return {"n": p["n"], "rel": p["mean_diff"] / base, "lo": p["lo"] / base,
            "hi": p["hi"] / base}  # fmt: skip


def pair_frame(pred: pl.DataFrame, a: str, b: str) -> pl.DataFrame:
    """Warm rows of models a and b on the matches both scored."""
    w = pred.filter(~pl.col("cold_start") & pl.col("model").is_in([a, b]))
    both = w.group_by("match_id").agg(pl.col("model").n_unique().alias("k"))
    return w.join(both.filter(pl.col("k") == 2).select("match_id"), on="match_id")


@dataclass(frozen=True)
class Verdict:
    passed: bool
    checks: list[dict[str, Any]]


def verdict(pred: pl.DataFrame, diags: list[dict[str, Any]], leakage_ok: bool) -> Verdict:
    """The pass rule (e) 1-5."""
    checks: list[dict[str, Any]] = []
    lk = pair_frame(pred, "L_lk", "P")
    for m in ("1X2:log_loss", "1X2:rps"):
        r = _rel(lk, m, "L_lk", "P")
        checks.append({"check": f"(e)1 L vs P at lineups-known, {m}", "value": r["rel"],
                       "lo": r["lo"], "hi": r["hi"], "passed": r["hi"] < 0})  # fmt: skip
    for (lg,), g in sorted(lk.group_by(["league_id"]), key=lambda x: int(x[0][0])):
        for m in SEGMENT_METRICS:
            r = _rel(g, m, "L_lk", "P")
            checks.append({"check": f"(e)1 league {lg} {m} vs P (at most +1.0%)",
                           "value": r["rel"], "passed": r["rel"] <= SEGMENT_MAX})  # fmt: skip
    ls = pair_frame(pred, "L_lk", "S_lk")
    for m in ("1X2:log_loss", "1X2:rps"):
        r = _rel(ls, m, "L_lk", "S_lk")
        checks.append({"check": f"(e)2 L vs S at lineups-known, {m}", "value": r["rel"],
                       "lo": r["lo"], "hi": r["hi"], "passed": r["rel"] < 0})  # fmt: skip
    t24 = pair_frame(pred, "L_24", "P")
    for m in ("1X2:log_loss", "1X2:rps"):
        r = _rel(t24, m, "L_24", "P")
        checks.append({"check": f"(e)3 L vs P at T-24h, {m} (no worse)", "value": r["rel"],
                       "lo": r["lo"], "hi": r["hi"], "passed": r["rel"] <= 0})  # fmt: skip
    gates = [g for g in diags if g["model"] == "L"]
    failing = [g for g in gates if not g["gates_ok"]]
    checks.append({"check": "(e)4 inference gates, every L refit", "value": float(len(failing)),
                   "passed": bool(gates) and not failing})  # fmt: skip
    t_l = float(np.median([g["fit_s"] for g in gates])) if gates else float("nan")
    p_fits = [g["fit_s"] for g in diags if g["model"] == "P" and not g["cached"]]
    t_p = float(np.median(p_fits)) if p_fits else float("nan")
    checks.append({"check": f"(e)4 L median refit within {BUDGET}x P's", "value": t_l / t_p,
                   "passed": bool(np.isfinite(t_l / t_p)) and t_l / t_p <= BUDGET})  # fmt: skip
    checks.append({"check": "(e)5 leakage", "value": 1.0 if leakage_ok else 0.0,
                   "passed": leakage_ok})  # fmt: skip
    return Verdict(all(c["passed"] for c in checks), checks)


def _f(x: Any, pct: bool = False) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return ""
    return f"{x:+.2%}" if pct else f"{x:.4f}"


def comparison_table(pred: pl.DataFrame) -> list[str]:
    rows = ["| horizon | comparison | n | 1X2 log loss | 95% interval | RPS | 95% interval |",
            "|---|---|---|---|---|---|---|"]  # fmt: skip
    for h in HORIZONS:
        for a, b in ((f"L_{h}", "P"), (f"S_{h}", "P"), (f"L_{h}", f"S_{h}")):
            g = pair_frame(pred, a, b)
            if g.is_empty():
                continue
            r1, r2 = _rel(g, "1X2:log_loss", a, b), _rel(g, "1X2:rps", a, b)
            rows.append(f"| {h} | {a} vs {b} | {int(r1['n'])} | {_f(r1['rel'], True)} | "
                        f"[{_f(r1['lo'], True)}, {_f(r1['hi'], True)}] | {_f(r2['rel'], True)} | "
                        f"[{_f(r2['lo'], True)}, {_f(r2['hi'], True)}] |")  # fmt: skip
    return rows


def segment_table(pred: pl.DataFrame, by: str, a: str = "L_lk", b: str = "P") -> list[str]:
    g = pair_frame(pred, a, b)
    rows = [f"| {by} | n | 1X2 log loss | RPS |", "|---|---|---|---|"]
    for (v,), s in sorted(g.group_by([by]), key=lambda x: str(x[0][0])):
        r1, r2 = _rel(s, "1X2:log_loss", a, b), _rel(s, "1X2:rps", a, b)
        rows.append(f"| {v} | {int(r1['n'])} | {_f(r1['rel'], True)} | {_f(r2['rel'], True)} |")
    return rows


def big_change(pred: pl.DataFrame, h: str = "lk") -> list[str]:
    """Matches with a large lineup change (|delta| of either side above its 90th percentile
    among the L rows), where the offset should matter most."""
    lr = pred.filter((pl.col("model") == f"L_{h}") & ~pl.col("cold_start"))
    if lr.is_empty():
        return []
    mx = lr.select(pl.max_horizontal(pl.col("delta_home").abs(), pl.col("delta_away").abs()))
    cut = float(np.quantile(mx.to_series().to_numpy(), BIG_Q))
    ids = lr.filter(pl.max_horizontal(pl.col("delta_home").abs(), pl.col("delta_away").abs())
                    > cut).select("match_id")  # fmt: skip
    rows = [f"Big lineup changes at {h} (|delta| above {cut:.3f}, {ids.height} matches):", "",
            "| comparison | 1X2 log loss | 95% interval | RPS |", "|---|---|---|---|"]  # fmt: skip
    for a, b in ((f"L_{h}", "P"), (f"S_{h}", "P"), (f"L_{h}", f"S_{h}")):
        g = pair_frame(pred.join(ids, on="match_id"), a, b)
        if g.is_empty():
            continue
        r1, r2 = _rel(g, "1X2:log_loss", a, b), _rel(g, "1X2:rps", a, b)
        rows.append(f"| {a} vs {b} | {_f(r1['rel'], True)} | [{_f(r1['lo'], True)}, "
                    f"{_f(r1['hi'], True)}] | {_f(r2['rel'], True)} |")  # fmt: skip
    return rows


def markdown(pred: pl.DataFrame, v: Verdict, diags: list[dict[str, Any]],
             table_meta: dict[str, Any], digest: str) -> str:  # fmt: skip
    lines = [f"# P3-d lineup offset — dev, offset table {digest}", "",
             f"**Verdict: {'pass' if v.passed else 'fail'}**", "",
             "| check | value | 95% interval | pass |", "|---|---|---|---|"]  # fmt: skip
    for c in v.checks:
        pct = "log loss" in c["check"] or "rps" in c["check"] or "crps" in c["check"]
        iv = f"[{_f(c['lo'], True)}, {_f(c['hi'], True)}]" if "lo" in c else ""
        lines.append(f"| {c['check']} | {_f(c['value'], pct)} | {iv} | "
                     f"{'yes' if c['passed'] else 'NO'} |")  # fmt: skip
    lines += ["", "## Comparisons (warm matches, relative; negative = first model better)", ""]
    lines += comparison_table(pred)
    for by in ("league_id", "season"):
        lines += ["", f"## L vs P at lineups-known by {by}", ""] + segment_table(pred, by)
    lines += [""] + big_change(pred, "lk") + [""] + big_change(pred, "24")
    lines += ["", "## Fits", ""]
    for m in ("P", "L"):
        g = [d for d in diags if d["model"] == m]
        fresh = [d for d in g if not d["cached"]]
        if not g:
            continue
        med = float(np.median([d["fit_s"] for d in fresh])) if fresh else float("nan")
        lines.append(
            f"- {m}: {len(g)} refits ({len(g) - len(fresh)} cached), "
            f"{sum(not d['gates_ok'] for d in g)} failing the gates; "
            f"worst R-hat {max(d['rhat_max'] for d in g):.3f}, "
            f"min ESS {min(min(d['ess_bulk_min'], d['ess_tail_min']) for d in g):.0f}, "
            f"divergences {sum(d['divergences'] for d in g):.0f}; "
            f"median fit {med:.0f} s; "
            f"sigma_walk mean {np.mean([d['sigma_walk'] for d in g]):.4f}"
        )  # fmt: skip
    bl = [d["beta_lineup"] for d in diags if d["model"] == "L"]
    bs = [d["beta_serving"] for d in diags if d["model"] == "P"]
    if bl:
        lines.append(f"- beta (L): median {np.median(bl):.3f}, range {min(bl):.3f} to "
                     f"{max(bl):.3f}")  # fmt: skip
    if bs:
        lines.append(f"- beta_S (S): median {np.median(bs):.3f}, range {min(bs):.3f} to "
                     f"{max(bs):.3f}")  # fmt: skip
    for f in [d for d in diags if d["model"] == "L" and not d["gates_ok"]][:20]:
        lines.append(f"- gates fail: L {f['bundle']} {f['as_of']}: rhat {f['rhat_max']:.3f}, "
                     f"ess {min(f['ess_bulk_min'], f['ess_tail_min']):.0f}, "
                     f"div {f['divergences']:.0f}")  # fmt: skip
    cov = table_meta.get("coverage")
    if cov:
        lines += ["", "## Offset coverage (share of dev team-matches with an offset)", "",
                  "| league | real | lk | t24 | lk fallback players per team-match |",
                  "|---|---|---|---|---|"]  # fmt: skip
        for r in cov:
            lines.append(f"| {r['league_ref']} | {r['ok_real']:.3f} | {r['ok_lk']:.3f} | "
                         f"{r['ok_24']:.3f} | {r['fallback']:.2f} |")  # fmt: skip
    return "\n".join(lines)


def coverage(table: pl.DataFrame, dev: tuple[int, ...]) -> list[dict[str, Any]]:
    t = table.filter(pl.col("season").is_in(dev))
    g = t.group_by("league_ref").agg(
        pl.col("ok_real").mean(), pl.col("ok_lk").mean(), pl.col("ok_24").mean(),
        fallback=pl.col("n_lk_fallback").cast(pl.Float64).mean(),
    ).sort("league_ref")  # fmt: skip
    return [{k: (float(v) if isinstance(v, float | int) and k != "league_ref" else v)
             for k, v in r.items()} for r in g.iter_rows(named=True)]  # fmt: skip
