"""A quick check of a candidate team-match signal before a full backtest (improvement 3,
2026-11-27). Minutes on the Mac, no PyMC, no CI, 0 API requests. Guidance only: it decides
nothing, and the full pre-registered test stays as it is.

On the dev seasons' 28-day grid, per bundle, Phase 1a (Dixon-Coles point fit, the selected
half-life and prior) is fitted at each refit. Then:
  slope   team goals of the refit's targets ~ Poisson(exp(a + b x + log rate)), the rate
          from that refit (out of sample): does the signal predict goals beyond strength?
  gain    b fitted on each refit's training rows (offset by the fit's in-sample rates),
          applied to its targets as b x on each side's log rate; 1X2 log loss and RPS
          against the fit without it, paired over every target, relative.
The signal is standardised by its sd over the rows used and is 0 where absent (rule 8).
Values known after kickoff are refused (counted), so a leaky table shows up as missing
rows, not as a gain; the full test's leakage check still applies.

    python -m adatta.eval.quick_check --signal data/scratch/x.parquet --expect -
      (columns: match_id, team_id, value, known_at)
"""

from __future__ import annotations

import argparse
import math
import time
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.eval import harness, metrics
from adatta.features.lineup_signals import poisson_slope
from adatta.model import dixon_coles as dc

Arr = NDArray[np.float64]
Z95 = 1.959963984540054


def _rates(p: dc.Params, h: NDArray[np.int64], a: NDArray[np.int64], lg: NDArray[np.int64],
           league_att: Arr, league_def: Arr) -> tuple[Arr, Arr]:  # fmt: skip
    """Rates with an unseen team (-1) at its league's mean, as `harness.walk_forward`."""
    att = np.append(p.attack, 0.0)
    dfn = np.append(p.defence, 0.0)
    hh, aa = np.where(h < 0, len(att) - 1, h), np.where(a < 0, len(att) - 1, a)
    att_h = np.where(h < 0, league_att[lg], att[hh])
    att_a = np.where(a < 0, league_att[lg], att[aa])
    def_h = np.where(h < 0, league_def[lg], dfn[hh])
    def_a = np.where(a < 0, league_def[lg], dfn[aa])
    base = p.mu + p.mu_league[lg]
    lam = np.exp(base + p.home_league[lg] + att_h - def_a)
    mu_ = np.exp(base + att_a - def_h)
    return lam, mu_


def _league_means(p: dc.Params, h: NDArray[np.int64], a: NDArray[np.int64],
                  lg: NDArray[np.int64], n: int) -> tuple[Arr, Arr]:  # fmt: skip
    """Each league's mean attack and defence over its teams in the window."""
    att, dfn = np.zeros(n), np.zeros(n)
    for k in range(n):
        members = np.unique(np.r_[h[lg == k], a[lg == k]])
        if len(members):
            att[k], dfn[k] = p.attack[members].mean(), p.defence[members].mean()
    return att, dfn


def _beta(y: Arr, off: Arr, x: Arr) -> float:
    """b in y ~ Poisson(exp(a + b x + off)); 0 when x has no spread."""
    if not np.any(x != 0) or len(y) < 50:
        return 0.0
    b, _ = poisson_slope(y, off, x)
    return float(b) if np.isfinite(b) else 0.0


def signal_columns(df: pl.DataFrame, signal: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """df with x_home and x_away (0 where absent); values known after kickoff refused."""
    s = signal.join(df.select("match_id", "kickoff"), on="match_id")
    late = s.filter(pl.col("known_at") > pl.col("kickoff"))
    s = s.filter(pl.col("known_at") <= pl.col("kickoff")).select("match_id", "team_id", "value")
    out = df
    for side in ("home", "away"):
        out = out.join(s.rename({"team_id": side, "value": f"x_{side}"}), on=["match_id", side],
                       how="left")  # fmt: skip
    zero = [pl.col(f"x_{x}").fill_null(0.0) for x in ("home", "away")]
    return out.with_columns(zero), late.height


def run_bundle(df: pl.DataFrame, leagues: list[int], cfg: harness.EvalConfig,
               seasons: list[int]) -> pl.DataFrame:  # fmt: skip
    """Per target match: goals, both rates, the signal, b, and both models' 1X2 scores."""
    sel = cfg.phase1a_selected
    plan = harness.refit_plan(df, cfg, seasons, cfg.dev_iteration_refit_every_days)
    li = {lid: i for i, lid in enumerate(leagues)}
    kick = np.array([t.timestamp() for t in df["kickoff"].to_list()]) / harness.DAY
    home, away = df["home"].to_numpy(), df["away"].to_numpy()
    lg = np.array([li[x] for x in df["league_id"].to_list()], dtype=np.int64)
    hg, ag = df["hg"].to_numpy(), df["ag"].to_numpy()
    xh, xa = df["x_home"].to_numpy(), df["x_away"].to_numpy()
    rows: list[dict[str, Any]] = []
    for as_of, tgt, train in plan:
        teams = np.unique(np.concatenate([home[train], away[train]]))
        tix = {int(t): i for i, t in enumerate(teams)}
        h_tr = np.array([tix[int(t)] for t in home[train]], dtype=np.int64)
        a_tr = np.array([tix[int(t)] for t in away[train]], dtype=np.int64)
        w = harness._weights(as_of.timestamp() / harness.DAY - kick[train], sel.half_life_days)
        p, _ = dc.fit(dc.MatchData(home=h_tr, away=a_tr, league=lg[train], hg=hg[train],
                                   ag=ag[train], weight=w, n_teams=len(teams),
                                   n_leagues=len(leagues),
                                   ridge=dc.ridge_for(sel.team_prior_sd)))  # fmt: skip
        l_att, l_def = _league_means(p, h_tr, a_tr, lg[train], len(leagues))
        lt, mt = _rates(p, h_tr, a_tr, lg[train], l_att, l_def)
        b = _beta(np.r_[hg[train], ag[train]].astype(np.float64), np.log(np.r_[lt, mt]),
                  np.r_[xh[train], xa[train]])  # fmt: skip
        h_t = np.array([tix.get(int(t), -1) for t in home[tgt]], dtype=np.int64)
        a_t = np.array([tix.get(int(t), -1) for t in away[tgt]], dtype=np.int64)
        lam, mu_ = _rates(p, h_t, a_t, lg[tgt], l_att, l_def)
        for k, i in enumerate(tgt):
            r = {"match_id": int(df["match_id"][int(i)]), "league_id": leagues[int(lg[i])],
                 "hg": int(hg[i]), "ag": int(ag[i]), "lam": float(lam[k]), "mu": float(mu_[k]),
                 "x_home": float(xh[i]), "x_away": float(xa[i]), "beta": b}  # fmt: skip
            for name, (l1, m1) in (("base", (lam[k], mu_[k])),
                                   ("signal", (lam[k] * math.exp(b * xh[i]),
                                               mu_[k] * math.exp(b * xa[i])))):  # fmt: skip
                m, _ = dc.score_matrix(float(l1), float(m1), p.rho, cfg.max_goals)
                s = metrics.score_markets(metrics.markets(m), int(hg[i]), int(ag[i]))
                r[f"ll_{name}"], r[f"rps_{name}"] = s["1X2:log_loss"], s["1X2:rps"]
            rows.append(r)
    return pl.DataFrame(rows)


def summary(t: pl.DataFrame, expect: int) -> dict[str, Any]:
    y = np.r_[t["hg"].to_numpy(), t["ag"].to_numpy()].astype(np.float64)
    off = np.log(np.r_[t["lam"].to_numpy(), t["mu"].to_numpy()])
    x = np.r_[t["x_home"].to_numpy(), t["x_away"].to_numpy()]
    b, se = poisson_slope(y, off, x)
    out: dict[str, Any] = {"targets": t.height, "with_signal": float(np.mean(x != 0)),
                           "slope": b, "slope_lo": b - Z95 * se, "slope_hi": b + Z95 * se,
                           "per_sd_goals": math.exp(b) - 1.0,
                           "beta_median": float(np.median(t["beta"].to_numpy()))}  # fmt: skip
    for m in ("ll", "rps"):
        d = (t[f"{m}_signal"] - t[f"{m}_base"]).to_numpy()
        base = float(np.mean(t[f"{m}_base"].to_numpy()))
        sd = float(d.std(ddof=1) / math.sqrt(len(d)))
        out[m] = {"rel": float(d.mean()) / base, "lo": (float(d.mean()) - Z95 * sd) / base,
                  "hi": (float(d.mean()) + Z95 * sd) / base}  # fmt: skip
    clear = out["slope_lo"] > 0 or out["slope_hi"] < 0
    sign_ok = expect == 0 or (np.sign(b) == expect and clear)
    gain = out["ll"]["rel"] < 0 and out["rps"]["rel"] < 0
    out["verdict"] = ("worth a full test" if sign_ok and gain
                      else "unlikely to pass: check before spending a backtest")  # fmt: skip
    return out


def markdown(s: dict[str, Any], late: int, secs: float) -> str:
    def rel(r: dict[str, float]) -> str:
        return f"{r['rel']:+.3%} [{r['lo']:+.3%}, {r['hi']:+.3%}]"

    return "\n".join([
        f"Quick check (dev, 28-day grid, Phase 1a offset): **{s['verdict']}**",
        f"- targets {s['targets']}, side-rows with a signal {s['with_signal']:.0%}; "
        f"refused as known after kickoff: {late}",
        f"- slope per sd {s['slope']:+.4f} [{s['slope_lo']:+.4f}, {s['slope_hi']:+.4f}] "
        f"({s['per_sd_goals']:+.1%} goals); median refit b {s['beta_median']:+.4f}",
        f"- 1X2 log loss {rel(s['ll'])}; RPS {rel(s['rps'])} (negative = the signal helps)",
        f"- {secs:.0f} s. Guidance only: the pre-registered test decides.",
    ]) + "\n"  # fmt: skip


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="adatta.eval.quick_check")
    ap.add_argument("--signal", required=True, help="parquet: match_id, team_id, value, known_at")
    ap.add_argument("--expect", choices=("+", "-", "0"), default="0", help="the expected sign")
    ap.add_argument("--bundles", default="", help="comma-separated; default all")
    args = ap.parse_args(argv)
    from adatta.db.session import get_session

    t0 = time.time()
    cfg = harness.load_config()
    bundles = args.bundles.split(",") if args.bundles else list(cfg.bundles)
    sig = pl.read_parquet(args.signal)
    if getattr(sig.schema["known_at"], "time_zone", None) is None:
        sig = sig.with_columns(pl.col("known_at").dt.replace_time_zone("UTC"))
    sd = float(np.std(sig["value"].to_numpy(), ddof=1)) or 1.0
    sig = sig.with_columns(value=pl.col("value") / sd)
    parts, late = [], 0
    with get_session() as s:
        for b in bundles:
            leagues = cfg.bundles[b]
            df, _ = harness.load_matches(s, leagues)
            df, n_late = signal_columns(df, sig)
            late += n_late
            parts.append(run_bundle(df, leagues, cfg, list(cfg.dev_seasons)))
    t = pl.concat([p for p in parts if p.height])
    expect = {"+": 1, "-": -1, "0": 0}[args.expect]
    print(markdown(summary(t, expect), late, time.time() - t0))


if __name__ == "__main__":
    main()
