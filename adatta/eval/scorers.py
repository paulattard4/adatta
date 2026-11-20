"""P3-e evaluation (docs/PHASE3_TASKS.md P3-e, restated: scorers): goalscorer and assist
probabilities allocated from P's team goals, the baselines, metrics and the pass rule (e).

Team T's goal count N has P's distribution pmf_T (Dixon-Coles, averaged over the draws).
Each goal is an own goal with probability o, else it goes to player i with share s_i, and
a non-own goal carries an assist with probability alpha, going to j with share s^A_j:
    P(X_i = x) = sum_k pmf_T(k) Binom(x; k, p_i),  p_i = (1 - o) s_i   (goals)
                                                  p_i = (1 - o) alpha s^A_i   (assists)
so sum_i E[X_i] = (1 - o) E[N] (goals): the team's non-own-goal rate, by construction.
Models (weights w_i, shares s_i = w_i / sum_j w_j over the team's population):
  M       w = e * q (P3-d0's rate as a starter or substitute)
  B_line  w = e * r0 (line x league rate)
  B_R1    w = e * r1 (own rate shrunk to r0)
  B0      no allocation: X_i ~ Poisson(e * q), with no team or opponent context
Dev only: seasons after 2023 are never in the table.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy.stats import binom, poisson

Arr = NDArray[np.float64]
STATS = ("goals", "assists")
HORIZONS = ("lk", "24")
MODELS = ("M", "B0", "B_line", "B_R1")
CATS = 4  # count categories 0, 1, 2, 3+
P_FLOOR = 1e-6  # every model's probabilities are clipped the same (zero-weight rows)
SEGMENT_MAX = 0.010  # (e)3: no worse than B_R1 by more than 1.0%
EO_BAND = (0.95, 1.05)
ECE_MAX, ECE_LEAGUE_MAX = 0.02, 0.03
CONSISTENCY_TOL = 1e-9
KEY = ["match_id", "team_id"]


# --------------------------------------------------------------------------- weights


def weights(t: pl.DataFrame, stat: str, horizon: str) -> dict[str, pl.Expr]:
    """Per model, the weight expression of a row at a horizon (lk: the announced squad
    and role; 24: P3-b's start and bench probabilities)."""
    qs, qb = pl.col(f"q_{stat}_start"), pl.col(f"q_{stat}_sub")
    r0, r1 = pl.col(f"r0_{stat}"), pl.col(f"r1_{stat}")
    if horizon == "lk":
        e = pl.col("e_lk")
        q = pl.when(pl.col("started")).then(qs).otherwise(qb)
        return {"M": e * q, "B_line": e * r0, "B_R1": e * r1}
    es, eb = pl.col("e24_start"), pl.col("e24_bench")
    return {"M": es * qs + eb * qb, "B_line": (es + eb) * r0, "B_R1": (es + eb) * r1}


def population(t: pl.DataFrame, horizon: str) -> pl.DataFrame:
    return t.filter(pl.col("in_squad") if horizon == "lk" else pl.col("in_pool"))


# ----------------------------------------------------------------------- probabilities


def pmf_matrix(pmfs: Sequence[Sequence[float]]) -> Arr:
    k = max(len(p) for p in pmfs)
    out = np.zeros((len(pmfs), k))
    for i, p in enumerate(pmfs):
        out[i, : len(p)] = p
    return out


def thinned(pmf: Arr, p: Arr) -> Arr:
    """(rows, CATS): P(X = 0, 1, 2, 3+) with X | N ~ Binom(N, p), N ~ pmf (rows, K)."""
    k = np.arange(pmf.shape[1])[None, :]
    out = np.zeros((len(p), CATS))
    for x in range(CATS - 1):
        out[:, x] = (pmf * binom.pmf(x, k, p[:, None])).sum(axis=1)
    out[:, CATS - 1] = np.clip(1.0 - out[:, : CATS - 1].sum(axis=1), 0.0, 1.0)
    return out


def poisson_cats(mu: Arr) -> Arr:
    out = np.stack([poisson.pmf(x, mu) for x in range(CATS - 1)], axis=1)
    return np.concatenate([out, np.clip(1.0 - out.sum(axis=1, keepdims=True), 0, 1)], axis=1)


def score_rows(rows: pl.DataFrame, pmf: Arr, stat: str, model: str) -> pl.DataFrame:
    """Scores one model on rows carrying w (weight), share, o, alpha and y; `pmf` is each
    row's team goal distribution."""
    y = rows[f"y_{stat}"].to_numpy().astype(np.int64)
    w = rows["w"].to_numpy()
    en = (pmf * np.arange(pmf.shape[1])[None, :]).sum(axis=1)
    if model == "B0":
        cats, mean = poisson_cats(w), w
    else:
        p = (1.0 - rows["o"].to_numpy()) * rows["share"].to_numpy()
        if stat == "assists":
            p = p * rows["alpha"].to_numpy()
        cats, mean = thinned(pmf, p), p * en
    p1 = np.clip(1.0 - cats[:, 0], P_FLOOR, 1.0 - P_FLOOR)
    hit = y >= 1
    ll1 = -np.where(hit, np.log(p1), np.log(1.0 - p1))
    pc = np.clip(cats[np.arange(len(y)), np.minimum(y, CATS - 1)], P_FLOOR, 1.0)
    return rows.select(
        "match_id", "team_id", "player_id", "league_ref", "season", "line", "started", "u18",
        "y_pen",
    ).with_columns(
        model=pl.lit(model), stat=pl.lit(stat), y=pl.Series(y), p1=pl.Series(p1),
        p2=pl.Series(cats[:, 2] + cats[:, 3]), mean=pl.Series(mean), log_loss=pl.Series(ll1),
        count_log_loss=pl.Series(-np.log(pc)), team_en=pl.Series(en),
        zero_w=pl.Series(w <= 0),
    )  # fmt: skip


def unallocatable(table: pl.DataFrame, stat: str, horizon: str) -> pl.DataFrame:
    """Team-matches (KEY) whose population has no positive M weight: no shares exist, so
    no player forecasts (amendment 2, principle 8)."""
    w = weights(table, stat, horizon)["M"].fill_null(0.0)
    tot = population(table, horizon).with_columns(w=w).group_by(KEY).agg(pl.col("w").sum())
    return tot.filter(pl.col("w") <= 0).select(KEY)


def score(table: pl.DataFrame, pmfs: pl.DataFrame, horizon: str) -> pl.DataFrame:
    """Every model and statistic at one horizon. `pmfs`: (match_id, team_id, pmf)."""
    pop = population(table, horizon).join(pmfs, on=KEY, how="inner")
    out = []
    for stat in STATS:
        rows = pop.filter(pl.col(f"y_{stat}").is_not_null() & pl.col("o").is_not_null())
        rows = rows.join(unallocatable(pop, stat, horizon), on=KEY, how="anti")
        if stat == "assists":
            rows = rows.filter(pl.col("alpha").is_not_null())
        # shares over the whole population of the team-match (y null rows still take a share)
        w = weights(rows, stat, horizon)
        full = pop.with_columns(**{f"_w_{m}": x.fill_null(0.0) for m, x in w.items()})
        tot = full.group_by(KEY).agg(*[pl.col(f"_w_{m}").sum().alias(f"_tot_{m}") for m in w])
        rows = rows.join(tot, on=KEY, how="left")
        pmf = pmf_matrix(rows["pmf"].to_list())
        for m in MODELS:
            src = "M" if m == "B0" else m
            r = rows.with_columns(w=w[src].fill_null(0.0)).with_columns(
                share=pl.when(pl.col(f"_tot_{src}") > 0)
                .then(pl.col("w") / pl.col(f"_tot_{src}")).otherwise(0.0),
            )  # fmt: skip
            out.append(score_rows(r, pmf, stat, m).with_columns(horizon=pl.lit(horizon)))
    return pl.concat(out)


def consistency(table: pl.DataFrame, pmfs: pl.DataFrame) -> dict[str, float]:
    """(e)5 at lineups-known: per team-match, M's summed player expectations plus the
    own-goal expectation against the team's expectation (goals), and the summed assist
    expectations against alpha (1 - o) E[N]; the largest absolute gap of each."""
    pop = population(table, "lk").join(pmfs, on=KEY, how="inner").filter(
        pl.col("o").is_not_null() & pl.col("alpha").is_not_null()
    )  # fmt: skip
    out = {}
    for stat in STATS:
        pop = pop.join(unallocatable(pop, stat, "lk"), on=KEY, how="anti")
        w = weights(pop, stat, "lk")["M"].fill_null(0.0)
        r = pop.with_columns(w=w).with_columns(share=pl.col("w") / pl.col("w").sum().over(KEY))
        pmf = pmf_matrix(r["pmf"].to_list())
        en = (pmf * np.arange(pmf.shape[1])[None, :]).sum(axis=1)
        o, a = r["o"].to_numpy(), r["alpha"].to_numpy()
        p = (1 - o) * r["share"].fill_nan(0.0).to_numpy() * (a if stat == "assists" else 1.0)
        g = r.select(KEY).with_columns(m=pl.Series(p * en), en=pl.Series(en), o=pl.Series(o),
                                       a=pl.Series(a))  # fmt: skip
        g = g.group_by(KEY).agg(pl.col("m").sum(), pl.col("en").first(), pl.col("o").first(),
                                pl.col("a").first())  # fmt: skip
        target = (1 - g["o"]) * g["en"] * (g["a"] if stat == "assists" else 1.0)
        out[stat] = float(np.max(np.abs((g["m"] - target).to_numpy()), initial=0.0))
    return out


# ---------------------------------------------------------------------------- metrics


def paired(s: pl.DataFrame, a: str, b: str, by: str | None = None,
           col: str = "log_loss") -> pl.DataFrame:  # fmt: skip
    """Mean per-row difference a - b with a normal 95% interval; rel = mean / b's mean.
    Negative favours a. `s` holds one statistic and horizon."""
    keys = ["match_id", "team_id", "player_id"] + ([by] if by else [])
    wide = s.filter(pl.col("model").is_in([a, b])).pivot(
        on="model", index=keys, values=col
    ).drop_nulls().with_columns(d=pl.col(a) - pl.col(b))  # fmt: skip
    agg = [pl.len().alias("n"), pl.col("d").mean().alias("mean"),
           (1.96 * pl.col("d").std() / pl.len().sqrt()).alias("half"),
           pl.col(b).mean().alias("base")]  # fmt: skip
    out = wide.group_by(by).agg(agg).sort(by) if by else wide.select(agg)
    return out.with_columns(lo=pl.col("mean") - pl.col("half"), hi=pl.col("mean") + pl.col("half"),
                            rel=pl.col("mean") / pl.col("base"))  # fmt: skip


def eo(s: pl.DataFrame, by: Sequence[str] = ()) -> pl.DataFrame:
    agg = (pl.col("mean").sum() / pl.col("y").sum()).alias("eo")
    return s.group_by(*by).agg(agg).sort(*by) if by else s.select(agg)


def ece(s: pl.DataFrame, by: Sequence[str] = (), bins: int = 10) -> pl.DataFrame:
    """ECE of P(at least one), equal-width bins on [0, 1]."""
    x = s.with_columns(obs=(pl.col("y") >= 1).cast(pl.Float64),
                       bin=(pl.col("p1") * bins).floor().clip(0, bins - 1))  # fmt: skip
    per = x.group_by(*by, "bin").agg(pl.len().alias("n"), pl.col("p1").mean().alias("pred"),
                                     pl.col("obs").mean())  # fmt: skip
    e = ((pl.col("pred") - pl.col("obs")).abs() * pl.col("n")).sum() / pl.col("n").sum()
    return per.group_by(*by).agg(e.alias("ece")).sort(*by) if by else per.select(e.alias("ece"))


# ---------------------------------------------------------------------------- verdict


@dataclass
class Verdict:
    passed: dict[str, bool]  # per statistic
    checks: list[dict[str, Any]]


def verdict(scored: pl.DataFrame, gaps: dict[str, float], leakage_ok: bool) -> Verdict:
    """The pass rule (e) at lineups-known, per statistic. Goals may pass alone."""
    checks: list[dict[str, Any]] = []
    passed = {}
    for stat in STATS:
        s = scored.filter((pl.col("horizon") == "lk") & (pl.col("stat") == stat))
        cs: list[dict[str, Any]] = []
        for i, base in ((1, "B0"), (2, "B_line")):
            r = paired(s, "M", base).row(0, named=True)
            cs.append({"check": f"(e){i} {stat}: M vs {base}, P(at least one) log loss",
                       "value": r["rel"], "lo": r["lo"] / r["base"], "hi": r["hi"] / r["base"],
                       "pass": r["hi"] < 0})  # fmt: skip
        r = paired(s, "M", "B_R1").row(0, named=True)
        cs.append({"check": f"(e)3 {stat}: M vs B_R1 (at most +1.0%)", "value": r["rel"],
                   "lo": r["lo"] / r["base"], "hi": r["hi"] / r["base"],
                   "pass": r["rel"] <= SEGMENT_MAX})  # fmt: skip
        for lg in paired(s, "M", "B_R1", by="league_ref").iter_rows(named=True):
            cs.append({"check": f"(e)3 {stat} league {lg['league_ref']} vs B_R1 (at most +1.0%)",
                       "value": lg["rel"], "pass": lg["rel"] <= SEGMENT_MAX})  # fmt: skip
        m = s.filter(pl.col("model") == "M")
        e = float(eo(m)["eo"][0])
        cs.append({"check": f"(e)4 {stat}: E/O", "value": e,
                   "pass": EO_BAND[0] <= e <= EO_BAND[1]})  # fmt: skip
        ec = float(ece(m)["ece"][0])
        cs.append({"check": f"(e)4 {stat}: ECE of P(at least one)", "value": ec,
                   "pass": ec <= ECE_MAX})  # fmt: skip
        worst = ece(m, ["league_ref"]).sort("ece", descending=True).row(0, named=True)
        cs.append({"check": f"(e)4 {stat}: worst league ECE ({worst['league_ref']})",
                   "value": worst["ece"], "pass": worst["ece"] <= ECE_LEAGUE_MAX})  # fmt: skip
        cs.append({"check": f"(e)5 {stat}: consistency, largest gap", "value": gaps[stat],
                   "pass": gaps[stat] <= CONSISTENCY_TOL})  # fmt: skip
        cs.append({"check": f"(e)6 {stat}: leakage", "value": float(leakage_ok),
                   "pass": leakage_ok})  # fmt: skip
        passed[stat] = all(c["pass"] for c in cs)
        checks += cs
    return Verdict(passed, checks)


# ----------------------------------------------------------------------------- report


def _f(x: Any, pct: bool = False) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "-"
    return f"{100 * x:+.2f}%" if pct else (f"{x:.4f}" if isinstance(x, float) else str(x))


def _rel_table(s: pl.DataFrame, by: str) -> list[str]:
    out = [
        f"| {by} | n | vs B0 | vs B_line | vs B_R1 | E/O | ECE |",
        "|---|---|---|---|---|---|---|",
    ]
    m = s.filter(pl.col("model") == "M")
    e, c = eo(m, [by]), ece(m, [by])
    p = {b: paired(s, "M", b, by=by) for b in ("B0", "B_line", "B_R1")}
    for r in p["B0"].iter_rows(named=True):
        k = r[by]
        cells = [_f(float(p[b].filter(pl.col(by) == k)["rel"][0]), True) for b in p]
        eov = e.filter(pl.col(by) == k)["eo"].to_list()
        ecv = c.filter(pl.col(by) == k)["ece"].to_list()
        out.append(f"| {k} | {r['n']} | " + " | ".join(cells) +
                   f" | {_f(eov[0] if eov else None)} "
                   f"| {_f(ecv[0] if ecv else None)} |")  # fmt: skip
    return out


def markdown(scored: pl.DataFrame, v: Verdict, meta: dict[str, Any]) -> str:
    ok = ", ".join(f"{s} {'pass' if p else 'fail'}" for s, p in v.passed.items())
    lines = [f"# P3-e scorers — dev, player table {meta.get('table', '?')}", "",
             f"**Verdict: {ok}**", "", "| check | value | 95% interval | pass |",
             "|---|---|---|---|"]  # fmt: skip
    for c in v.checks:
        pct = "log loss" in c["check"] or "B_R1" in c["check"]
        iv = f"[{_f(c['lo'], True)}, {_f(c['hi'], True)}]" if "lo" in c else ""
        lines.append(f"| {c['check']} | {_f(c['value'], pct)} | {iv} | "
                     f"{'yes' if c['pass'] else 'NO'} |")  # fmt: skip
    for h in HORIZONS:
        for stat in STATS:
            s = scored.filter((pl.col("horizon") == h) & (pl.col("stat") == stat))
            if s.is_empty():
                continue
            m = s.filter(pl.col("model") == "M")
            lines += ["", f"## {stat} at {h} (relative P(at least one) log loss; "
                          "negative = M better)", ""]  # fmt: skip
            lines.append(f"Rows {m.height}, realised {int(m['y'].sum())}, rows with zero "
                         f"weight {int(m['zero_w'].sum())} (their realised "
                         f"{int(m.filter(pl.col('zero_w'))['y'].sum())}), under-18 rows "
                         f"{int(m['u18'].sum())}.")  # fmt: skip
            lines += ["", "| comparison | n | P(>=1) log loss | 95% interval | count log loss |",
                      "|---|---|---|---|---|"]  # fmt: skip
            for b in ("B0", "B_line", "B_R1"):
                r = paired(s, "M", b).row(0, named=True)
                rc = paired(s, "M", b, col="count_log_loss").row(0, named=True)
                lines.append(f"| M vs {b} | {r['n']} | {_f(r['rel'], True)} | "
                             f"[{_f(r['lo'] / r['base'], True)}, {_f(r['hi'] / r['base'], True)}]"
                             f" | {_f(rc['rel'], True)} |")  # fmt: skip
            for by in ("league_ref", "line", "season", "started"):
                lines += [""] + _rel_table(s, by)
            dec = m.with_columns(dec=(pl.col("p1").rank("ordinal") * 10 / pl.len()).floor()
                                 .clip(0, 9).cast(pl.Int32))  # fmt: skip
            t = (
                dec.group_by("dec")
                .agg(
                    pl.col("p1").mean().alias("pred"),
                    (pl.col("y") >= 1).mean().alias("obs"),
                    pl.col("p2").mean().alias("pred2"),
                    (pl.col("y") >= 2).mean().alias("obs2"),
                )
                .sort("dec")
            )
            lines += ["", "| P(>=1) decile | mean P(>=1) | observed | mean P(2+) | observed 2+ |",
                      "|---|---|---|---|---|"]  # fmt: skip
            for r in t.iter_rows(named=True):
                lines.append(f"| {r['dec'] + 1} | {r['pred']:.4f} | {r['obs']:.4f} | "
                             f"{r['pred2']:.4f} | {r['obs2']:.4f} |")  # fmt: skip
            if stat == "goals":
                pens = int(m["y_pen"].sum())
                tot = int(m["y"].sum())
                pk = s.with_columns(pen=pl.col("y_pen") > 0)
                lines += ["", f"Penalty goals {pens} of {tot}. M vs B_R1 by penalty scored:"]
                for r in paired(pk, "M", "B_R1", by="pen").iter_rows(named=True):
                    lines.append(f"- penalty {r['pen']}: n {r['n']}, {_f(r['rel'], True)}")
    lines += ["", f"Leakage: {meta.get('leakage_changed', '?')} of "
                  f"{meta.get('leakage_sampled', '?')} sampled rows changed."]  # fmt: skip
    for k in ("coverage", "pmf_matches", "unallocatable_goals", "unallocatable_assists"):
        if k in meta:
            lines.append(f"{k}: {meta[k]}")
    return "\n".join(lines)
