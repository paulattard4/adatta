"""P3b-5: penalty takers in the goalscorer forecast (docs/PHASE3B_TASKS.md P3b-5, D72).

M (P3-e) gives player i of a team-match a per-goal probability p_i = (1 - o) share_i and
thins the team's goal distribution N (P's) with it. M' splits each non-own team goal into
a penalty, with probability pi, and open play:

    p'_i = (1 - o) (pi tau_i + (1 - pi) share_open_i)

so the team's players still share (1 - o) of its goals, as in M.

  pi           the team's penalty goals over its non-own goals in the 730 days final by
               t (kickoff - 24 h, P3-e's as-of), shrunk to the league's share over the same
               window with K_TEAM pseudo-goals
  tau_i        e_i (a_i + KAPPA) / sum_j e_j (a_j + KAPPA) over the team-match's population:
               a_i the player's penalty attempts for the team (scored plus missed) in the
               TAKER_DAYS final by t, e_i his expected share of the match on the pitch
               (lineups-known: e_lk; T-24h: P3-b's expected minutes). A kick at a random
               minute finds him on the pitch with probability e_i (D72 (a), in expectation)
  share_open_i M's weight times (1 - the line's penalty share of its goals in the league
               over the 730 days), normalised over the population: P3-e's line rates with
               the penalty goals taken out of the history

X_i | N ~ Binomial(N, p'_i) as M. Assists are unchanged. Pure polars / numpy (the Mac):
P3-e's table (`features/scorers`), P's distributions (the pmf run) and the appearances.

    python -m adatta.eval.penalty_takers --table 0e81c3ec404d3635 --pmf 44688838616
"""

from __future__ import annotations

import argparse
from datetime import timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.eval import scorers as es
from adatta.features import rates as fr
from adatta.features import scorers as fs

Arr = NDArray[np.float64]
K_TEAM = 20.0  # pseudo-goals shrinking a team's penalty share to its league's (as C[goals])
KAPPA = 0.5  # Dirichlet pseudo-attempts per player in the taker probability
TAKER_DAYS = timedelta(days=365)  # D72 (a)
KEY = fs.KEY


def history(app: pl.DataFrame) -> pl.DataFrame:
    """Per appearance with minutes: league, team, player, kickoff, line, goals, penalty
    goals and attempts."""
    a = fr._line(app.select("league_ref", "match_id", "team_id", "player_id", "kickoff", "pos",
                            "goals", "pen_scored", "pen_missed", "minutes"))  # fmt: skip
    return a.select(
        "league_ref", "match_id", "team_id", "player_id", "kickoff", "line",
        g=pl.col("goals").fill_null(0), pg=pl.col("pen_scored").fill_null(0),
        att=pl.col("pen_scored").fill_null(0) + pl.col("pen_missed").fill_null(0),
    )  # fmt: skip


def _window(targets: pl.DataFrame, rows: pl.DataFrame, on: list[str], days: timedelta | None,
            cols: list[str]) -> pl.DataFrame:  # fmt: skip
    """Sums of `cols` of `rows` joined to targets on `on`, with kickoff in (start, cut]:
    cut = target kickoff - 24 h - RESULT_LAG, start = cut - days (P3-e's 730 days if None)."""
    cut, start = fs._cut(pl.col("kickoff"))
    t = targets.with_columns(_cut=cut, _start=start if days is None else cut - days)
    j = t.join(rows.rename({"kickoff": "_k"}), on=on, how="inner").filter(
        (pl.col("_k") > pl.col("_start")) & (pl.col("_k") <= pl.col("_cut")))  # fmt: skip
    extra = [
        c for c in rows.columns if c not in targets.columns and c not in cols and c != "kickoff"
    ]  # e.g. player_id: kept as a key
    return j.group_by(*targets.columns, *extra).agg([pl.col(c).sum() for c in cols])


def _cum_window(targets: pl.DataFrame, rows: pl.DataFrame, on: list[str],
                cols: list[str]) -> pl.DataFrame:  # fmt: skip
    """As `_window` over P3-e's 730 days, by cumulative sums and binary search per group of
    `on` (as `scorers.shares`): memory linear in the rows, for dense histories."""
    cut, start = fs._cut(pl.col("kickoff"))
    t = targets.with_columns(_cut=cut, _start=start)
    groups = {k: g.sort("kickoff") for k, g in rows.partition_by(on, as_dict=True).items()}
    parts = []
    for k, tg in t.partition_by(on, as_dict=True).items():
        g = groups.get(k)
        sums: dict[str, Arr] = {}
        if g is None or g.is_empty():
            sums = {c: np.zeros(tg.height) for c in cols}
        else:
            kk = g["kickoff"].dt.epoch("us").to_numpy()
            hi = np.searchsorted(kk, tg["_cut"].dt.epoch("us").to_numpy(), side="right")
            lo = np.searchsorted(kk, tg["_start"].dt.epoch("us").to_numpy(), side="right")
            for c in cols:
                cs = np.concatenate([[0.0], np.cumsum(g[c].to_numpy().astype(np.float64))])
                sums[c] = cs[hi] - cs[lo]
        parts.append(tg.drop("_cut", "_start").with_columns(
            **{c: pl.Series(v) for c, v in sums.items()}))  # fmt: skip
    return pl.concat(parts)


def columns(table: pl.DataFrame, app: pl.DataFrame) -> pl.DataFrame:
    """`table` with pen_att (the player's attempts for the team over TAKER_DAYS), pi (the
    team's shrunk penalty share of its goals) and line_pen (the league-line's penalty share
    of its goals), all from matches final by t."""
    h = history(app)
    tm = table.select(*KEY, "kickoff", "league_ref").unique(KEY)
    att = _window(tm.select(*KEY, "kickoff"), h.filter(pl.col("att") > 0).select(
        "team_id", "player_id", "kickoff", "att"), ["team_id"], TAKER_DAYS, ["att"])  # fmt: skip
    team = h.group_by("league_ref", "match_id", "team_id").agg(
        pl.col("kickoff").first(), pl.col("g").sum(), pl.col("pg").sum())  # fmt: skip
    tg = _cum_window(tm.select(*KEY, "kickoff"), team.select("team_id", "kickoff", "g", "pg"),
                     ["team_id"], ["g", "pg"])  # fmt: skip
    lg = _cum_window(tm.select(*KEY, "kickoff", "league_ref"),
                     team.select("league_ref", "kickoff", "g", "pg"), ["league_ref"],
                     ["g", "pg"]).rename({"g": "lg_g", "pg": "lg_pg"})  # fmt: skip
    by_line = ["league_ref", "line", "kickoff", "match_id"]
    line = h.group_by(*by_line).agg(pl.col("g").sum(), pl.col("pg").sum())
    targets_line = table.select(*KEY, "kickoff", "league_ref", "line").unique()
    ln = _cum_window(targets_line, line.drop("match_id"), ["league_ref", "line"], ["g", "pg"])
    share = (pl.col("lg_pg") / pl.col("lg_g")).fill_nan(None)
    pi = tm.join(lg, on=[*KEY, "kickoff", "league_ref"], how="left").join(
        tg, on=[*KEY, "kickoff"], how="left").with_columns(
        pi=((pl.col("pg").fill_null(0) + K_TEAM * share) / (pl.col("g").fill_null(0) + K_TEAM)),
    ).select(*KEY, "pi")  # fmt: skip
    out = table.join(pi, on=KEY, how="left")
    out = out.join(att.select(*KEY, "player_id", pen_att="att"), on=[*KEY, "player_id"],
                   how="left")  # fmt: skip
    out = out.join(ln.select(*KEY, "line", line_pen=(pl.col("pg") / pl.col("g")).fill_nan(None)),
                   on=[*KEY, "line"], how="left")  # fmt: skip
    return out.with_columns(pen_att=pl.col("pen_att").fill_null(0).cast(pl.Float64),
                            pi=pl.col("pi").fill_null(0.0),
                            line_pen=pl.col("line_pen").fill_null(0.0))  # fmt: skip


def score_goals(table: pl.DataFrame, pmfs: pl.DataFrame, horizon: str) -> pl.DataFrame:
    """M and M' on goals at one horizon, on es.score's rows (same population, same
    exclusions), with each row's team penalty-goal flag."""
    pop = es.population(table, horizon).join(pmfs, on=KEY, how="inner")
    rows = pop.filter(pl.col("y_goals").is_not_null() & pl.col("o").is_not_null())
    rows = rows.join(es.unallocatable(pop, "goals", horizon), on=KEY, how="anti")
    w_m = es.weights(rows, "goals", horizon)["M"]
    e = pl.col("e_lk") if horizon == "lk" else pl.col("e24_start") + pl.col("e24_bench")
    full = pop.with_columns(
        _w=es.weights(pop, "goals", horizon)["M"].fill_null(0.0),
        _e=e.fill_null(0.0),
    ).with_columns(_wo=pl.col("_w") * (1.0 - pl.col("line_pen")),
                   _t=pl.col("_e") * (pl.col("pen_att") + KAPPA))  # fmt: skip
    tot = full.group_by(KEY).agg(pl.col("_w").sum().alias("_tw"), pl.col("_wo").sum().alias("_two"),
                                 pl.col("_t").sum().alias("_tt"),
                                 pl.col("y_pen").sum().alias("team_pen"))  # fmt: skip
    rows = rows.join(tot, on=KEY, how="left").with_columns(
        w=w_m.fill_null(0.0), _e=e.fill_null(0.0))  # fmt: skip
    share_m = pl.when(pl.col("_tw") > 0).then(pl.col("w") / pl.col("_tw")).otherwise(0.0)
    share_o = pl.when(pl.col("_two") > 0).then(
        pl.col("w") * (1.0 - pl.col("line_pen")) / pl.col("_two")).otherwise(0.0)  # fmt: skip
    tau = pl.when(pl.col("_tt") > 0).then(
        pl.col("_e") * (pl.col("pen_att") + KAPPA) / pl.col("_tt")).otherwise(0.0)  # fmt: skip
    p_pen = (1.0 - pl.col("o")) * (pl.col("pi") * tau + (1.0 - pl.col("pi")) * share_o)
    pmf = es.pmf_matrix(rows["pmf"].to_list())
    out = []
    for model, share in (("M", share_m), ("M_pen", p_pen / (1.0 - pl.col("o")))):
        r = rows.with_columns(share=share)
        out.append(es.score_rows(r, pmf, "goals", model).with_columns(
            horizon=pl.lit(horizon), team_pen=r["team_pen"]))  # fmt: skip
    return pl.concat(out)


def verdict(scored: pl.DataFrame) -> tuple[bool, list[dict[str, Any]]]:
    """D72's dev rule at lineups known: M' beats M on P(at least one) log loss overall and
    on team-matches with a penalty goal (point estimates); no league worse than M by more
    than 1.0%; E/O and ECE within P3-e's limits. Leakage is the unit tests'."""
    s = scored.filter(pl.col("horizon") == "lk")
    cs: list[dict[str, Any]] = []
    for label, part in (("all rows", s), ("team-matches with a penalty goal",
                                          s.filter(pl.col("team_pen") > 0))):  # fmt: skip
        r = es.paired(part, "M_pen", "M").row(0, named=True)
        cs.append({"check": f"M' vs M, {label}", "value": r["rel"], "n": r["n"],
                   "lo": r["lo"] / r["base"], "hi": r["hi"] / r["base"],
                   "pass": r["rel"] < 0})  # fmt: skip
    for lg in es.paired(s, "M_pen", "M", by="league_ref").iter_rows(named=True):
        cs.append({"check": f"league {lg['league_ref']} vs M (at most +1.0%)", "value": lg["rel"],
                   "pass": lg["rel"] <= es.SEGMENT_MAX})  # fmt: skip
    m = s.filter(pl.col("model") == "M_pen")
    e = float(es.eo(m)["eo"][0])
    cs.append({"check": "E/O", "value": e, "pass": es.EO_BAND[0] <= e <= es.EO_BAND[1]})
    ec = float(es.ece(m)["ece"][0])
    cs.append({"check": "ECE of P(at least one)", "value": ec, "pass": ec <= es.ECE_MAX})
    worst = es.ece(m, ["league_ref"]).sort("ece", descending=True).row(0, named=True)
    cs.append({"check": f"worst league ECE ({worst['league_ref']})", "value": worst["ece"],
               "pass": worst["ece"] <= es.ECE_LEAGUE_MAX})  # fmt: skip
    return all(c["pass"] for c in cs), cs


def markdown(passed: bool, checks: list[dict[str, Any]], scored: pl.DataFrame) -> str:
    lines = ["# P3b-5: penalty takers (M') vs M on dev, goals, lineups known", "",
             f"**Judge live: {'yes' if passed else 'no'}**", "",
             "| check | value | 95% interval | pass |", "|---|---|---|---|"]  # fmt: skip
    for c in checks:
        pct = "ECE" not in c["check"] and "E/O" not in c["check"]
        v = f"{c['value']:+.2%}" if pct else f"{c['value']:.4f}"
        iv = f"[{c['lo']:+.2%}, {c['hi']:+.2%}]" if "lo" in c else ""
        lines.append(f"| {c['check']} | {v} | {iv} | {'yes' if c['pass'] else 'NO'} |")
    t24 = scored.filter(pl.col("horizon") == "24")
    if t24.height:
        r = es.paired(t24, "M_pen", "M").row(0, named=True)
        lines += ["", f"T-24h (reported): M' vs M {r['rel']:+.2%} "
                  f"[{r['lo'] / r['base']:+.2%}, {r['hi'] / r['base']:+.2%}]"]  # fmt: skip
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap_ = argparse.ArgumentParser(prog="adatta.eval.penalty_takers")
    ap_.add_argument("--table", required=True, help="P3-e's player table digest")
    ap_.add_argument("--pmf", required=True, help="P3-e's pmf run (P's goal distributions)")
    args = ap_.parse_args(argv)
    import io

    from adatta.adapters.storage import make_store, try_get
    from adatta.config import get_settings
    from adatta.eval import harness
    from adatta.features import appearances as ap
    from adatta.model import scorers as ms

    store = make_store(get_settings())
    cfg = harness.load_config()
    table, _, digest = fs.load(store, args.table)
    seasons = sorted({int(s) for s in table["season"]})
    parts = []
    for b in cfg.bundles:
        for s in seasons:
            data = try_get(store, ms.pmf_key(args.pmf, b, s))
            if data is None:
                raise SystemExit(f"missing pmf part {b} {s}")
            parts.append(pl.read_parquet(io.BytesIO(data)))
    pmfs = pl.concat(parts).select(*KEY, "pmf")
    leagues = [str(x) for xs in cfg.bundles.values() for x in xs]
    app = ap.load(
        store, [(lg, y) for lg in leagues for y in range(seasons[0] - 3, seasons[-1] + 1)]
    )
    t = columns(table, app)
    scored = pl.concat([score_goals(t, pmfs, h) for h in es.HORIZONS])
    passed, checks = verdict(scored)
    print(markdown(passed, checks, scored) + f"\nTable {digest}, pmf run {args.pmf}; "
          f"K_TEAM {K_TEAM}, KAPPA {KAPPA}, taker window {TAKER_DAYS.days} days.")  # fmt: skip


if __name__ == "__main__":
    main()
