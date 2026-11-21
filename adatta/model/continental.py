"""Stage C, C1: continental club matches between bundle teams on a common scale
(docs/BEYOND_LEAGUES_PLAN.md, Stage C, "C1 acceptance"; spec §10.1).

log lambda_home = c + h (1 - neutral) + att_home - def_away + Q[home bundle] - Q[away bundle]
(England Q = 0), each side's att / def from its own bundle's production weekly refit as of
the match's Monday (as Stage A's A1). Against B_own (Q = 0) and an Elo-style baseline.

CLI (CI, model group for `strengths` and `fit`; one backtest-type run at a time):
  strengths --bundle B --part i/n --upload RUN
            each side's attack / defence draws as of its match's Monday, from the cached
            refit or a new one (fitted and cached like any backtest refit)
            -> continental/strengths/RUN/<bundle>_<i>of<n>.parquet
  fit       --fetch RUN
            C1 and B_own walk-forward on the 28-day grid, the Elo, the gate; metrics only
            to stdout, the scored rows to R2 (continental/reports/RUN/)
Holdout seasons are never loaded. The repo is public: logs carry metrics, never data.
"""

from __future__ import annotations

import argparse
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.adapters.storage import RawStore, make_store, try_get
from adatta.config import get_settings

Arr = NDArray[np.float64]
PREFIX = "continental"
COMPS = {"2": "CL", "3": "EL", "848": "ECL"}
SEASONS = tuple(range(2016, 2024))  # warm-up history and dev; no holdout
DEV = tuple(range(2019, 2024))
REFIT_DAYS = 28  # D14
LAG = timedelta(hours=3)
SEED = 20261121
DRAWS, CHAINS = 2000, 4
SIDE_DRAWS = 400  # draws per side kept for prediction
MAX_GOALS = 10
ELO_START, ELO_K, ELO_HOME = 1500.0, 20.0, 60.0  # fixed in the acceptance, not tuned
ELO_FROM = datetime(2016, 7, 1, tzinfo=UTC)
LEAKAGE_N = 50
MIN_HISTORY = timedelta(days=365)  # a side's bundle needs this much stored league history

TARGET_SCHEMA: dict[str, Any] = {
    "match_id": pl.Int64, "comp": pl.Utf8, "season": pl.Int32,
    "kickoff": pl.Datetime("us", "UTC"), "monday": pl.Datetime("us", "UTC"),
    "home": pl.Int64, "away": pl.Int64, "bundle_home": pl.Utf8, "bundle_away": pl.Utf8,
    "league_home": pl.Int64, "league_away": pl.Int64, "hg": pl.Int64, "ag": pl.Int64,
    "neutral": pl.Boolean, "cross": pl.Boolean, "swapped": pl.Boolean,
}  # fmt: skip


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def monday(t: datetime) -> datetime:
    t = _utc(t)
    return (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------------------ targets


def targets(session: Any, bundles: dict[str, list[int]]) -> pl.DataFrame:
    """Finished UEFA matches of SEASONS between two teams that each play a bundle league that
    season (league matches), with the 90' score; a swapped venue swaps the sides."""
    from sqlalchemy import select

    from adatta.cli import _bundle_competition_ids
    from adatta.db import models
    from adatta.features import neutral
    from adatta.sim import serve

    M, S, C, R = models.Match, models.Season, models.Competition, models.MatchResult
    rows = session.execute(
        select(M, C.source_ref, S.year, R.home_goals_nt, R.away_goals_nt)
        .join(S, S.season_id == M.season_id).join(C, C.competition_id == S.competition_id)
        .join(R, (R.match_id == M.match_id) & R.superseded_at.is_(None))
        .where(C.source_ref.in_(list(COMPS)), S.year.in_(SEASONS),
               M.status.in_(["FT", "AET", "PEN"]))
    ).all()  # fmt: skip
    rows = [r for r in rows if r[3] is not None and r[4] is not None]
    by_season: dict[int, dict[int, tuple[str, int]]] = {}
    for y in SEASONS:
        by_season[y] = {}
        for b, ls in bundles.items():
            for t, lg in serve.team_leagues(session, ls, y).items():
                by_season[y][t] = (b, lg)
    league_ids, _ = _bundle_competition_ids(session)
    roles = neutral.venue_roles(session, [r[0] for r in rows], league_ids, neutral.load_rules(),
                                {r[0].season_id: int(r[1]) for r in rows})  # fmt: skip
    role = dict(zip(roles["match_id"].to_list(), roles["role"].to_list(), strict=True))
    out = []
    for m, comp, y, hg, ag in rows:
        tl = by_season[int(y)]
        h, a = m.home_team_id, m.away_team_id
        if h not in tl or a not in tl:
            continue
        sw = role.get(m.match_id) == "swapped"
        if sw:
            h, a, hg, ag = a, h, ag, hg
        out.append({
            "match_id": m.match_id, "comp": COMPS[str(comp)], "season": int(y),
            "kickoff": _utc(m.kickoff_time), "monday": monday(m.kickoff_time), "home": h,
            "away": a, "bundle_home": tl[h][0], "bundle_away": tl[a][0],
            "league_home": tl[h][1], "league_away": tl[a][1], "hg": int(hg), "ag": int(ag),
            "neutral": role.get(m.match_id) == "neutral", "cross": tl[h][0] != tl[a][0],
            "swapped": sw,
        })  # fmt: skip
    return pl.DataFrame(out, schema=TARGET_SCHEMA).sort("kickoff", "match_id")


# ---------------------------------------------------------------------------- strengths


def side_strength(draws: dict[str, Arr], fd: Any, team: int, league: int,
                  idx: NDArray[np.int64]) -> dict[str, Any]:  # fmt: skip
    """A team's attack and defence draws at the refit's last step (form at horizon 0, as
    `rates_from_draws`), and its league's rho and kappa; an unseen team gets its league's
    mean over the last 52 weeks (cold)."""
    tix = {int(t): i for i, t in enumerate(fd.team_ids)}
    lix = {int(x): i for i, x in enumerate(fd.leagues)}
    lg = lix[league]
    att, dfn = np.asarray(draws["attack_last"]), np.asarray(draws["defence_last"])
    t = tix.get(team, -1)
    if t >= 0:
        a, d = att[t].copy(), dfn[t].copy()
        if "form_last" in draws:
            f = np.asarray(draws["form_last"])[t] / np.sqrt(2.0)
            a, d = a + f, d + f
    else:
        recent = fd.step > fd.step.max() - 52
        mem = np.unique(np.concatenate([fd.home[recent & (fd.league == lg)],
                                        fd.away[recent & (fd.league == lg)]]))  # fmt: skip
        a, d = att[mem].mean(0), dfn[mem].mean(0)
    rho = np.asarray(draws["rho"])
    rho = rho[lg] if rho.ndim == 2 else rho
    kap = np.asarray(draws["kappa"])[lg] if "kappa" in draws else np.zeros_like(a)
    return {"att_mean": float(a.mean()), "def_mean": float(d.mean()),
            "att": a[idx].tolist(), "def": d[idx].tolist(), "rho": rho[idx].tolist(),
            "kappa": kap[idx].tolist(), "cold": t < 0}  # fmt: skip


def strengths(store: RawStore, session: Any, bundle: str, part: tuple[int, int],
              log: Any = print) -> pl.DataFrame:  # fmt: skip
    """Rows (match, side) for this bundle's sides, from the weekly refit of each match's
    Monday: cached, else fitted and cached under the league backtest's key."""
    from adatta.eval import harness
    from adatta.model import backtest as bt
    from adatta.model import team_strength as ts

    cfg = harness.load_config()
    spec = bt.production_spec(cfg)
    leagues = cfg.bundles[bundle]
    tg = targets(session, cfg.bundles)
    session.rollback()
    sides = pl.concat([
        tg.filter(pl.col("bundle_home") == bundle).select(
            "match_id", "monday", team="home", league="league_home", side=pl.lit("home")),
        tg.filter(pl.col("bundle_away") == bundle).select(
            "match_id", "monday", team="away", league="league_away", side=pl.lit("away")),
    ])  # fmt: skip
    df = bt._load(leagues, spec.xg, spec.manager)
    first = _utc(df["kickoff"].min())  # type: ignore[arg-type]
    early = sides.filter(pl.col("monday") < first + MIN_HISTORY)
    if early.height:  # under a year of the bundle's stored league history: not used
        log(f"{bundle}: {early.height} sides before {first + MIN_HISTORY:%Y-%m-%d} skipped "
            f"(under {MIN_HISTORY.days} days of league history)")  # fmt: skip
    sides = sides.filter(pl.col("monday") >= first + MIN_HISTORY)
    mondays = sorted(set(sides["monday"].to_list()))[part[0] :: part[1]]
    h, lag = bt.fit_hash(spec), cfg.result_available_after_hours
    fitter = bt.nuts_fitter(spec)
    out: list[dict[str, Any]] = []
    log(f"{bundle} part {part[0]}/{part[1]}: {len(mondays)} Mondays, fit hash {h}")
    for i, mon in enumerate(mondays):
        fd = ts.prepare(df, leagues, mon, cfg.lookback_days, lag, spec.groups)
        key = bt.cache_key(h, bundle, mon)
        raw = try_get(store, key)
        if raw is not None:
            draws, diag = bt.decode(raw)
        else:
            draws, diag = fitter(fd, spec.seed)
            store.put(key, bt.encode(draws, diag))
        n = np.asarray(draws["attack_last"]).shape[1]
        idx = np.linspace(0, n - 1, SIDE_DRAWS).astype(np.int64)
        for r in sides.filter(pl.col("monday") == mon).iter_rows(named=True):
            sd = side_strength(draws, fd, int(r["team"]), int(r["league"]), idx)
            out.append({"match_id": r["match_id"], "side": r["side"], "team": r["team"],
                        "bundle": bundle, "monday": mon, "refit_as_of": mon,
                        "gates_ok": _gates_ok(diag), "cached": raw is not None, **sd})  # fmt: skip
        log(f"  {i + 1}/{len(mondays)} {mon:%Y-%m-%d} {'cached' if raw is not None else 'fitted'}")
    return pl.DataFrame(out)


def _gates_ok(diag: dict[str, float]) -> bool:
    from adatta.model import diagnostics as dg

    return bool(diag.get("rhat_max", 9) <= dg.RHAT_MAX and diag.get("ess_bulk_min", 0) >= dg.ESS_MIN
                and diag.get("divergences", 9) <= dg.DIVERGENCES_MAX)  # fmt: skip


# -------------------------------------------------------------------------------- C1 fit

BUNDLE_ORDER = ("england", "spain", "italy", "germany", "france")  # England is Q = 0


def fit_q(train: pl.DataFrame, with_q: bool, prior_scale: float = 1.0,
          seed: int = SEED) -> tuple[dict[str, Arr], dict[str, Any]]:  # fmt: skip
    """C1 (with_q) or B_own on the training matches: Poisson goals, att/def at their means.
    Returns thinned draws {c, h, Q (n, 5)} and diagnostics (§14.5 gates, PPC of goals)."""
    import pymc as pm

    from adatta.model import diagnostics as dg

    bi = {b: i for i, b in enumerate(BUNDLE_ORDER)}
    bh = np.array([bi[b] for b in train["bundle_home"]])
    ba = np.array([bi[b] for b in train["bundle_away"]])
    neu = train["neutral"].cast(pl.Float64).to_numpy()
    base_h = (train["att_home"] - train["def_away"]).to_numpy()
    base_a = (train["att_away"] - train["def_home"]).to_numpy()
    hg, ag = train["hg"].to_numpy(), train["ag"].to_numpy()
    with pm.Model():
        c = pm.Normal("c", 0.2, 0.5 * prior_scale)
        hh = pm.Normal("h", 0.25, 0.25 * prior_scale)
        if with_q:
            q4 = pm.Normal("q", 0.0, 0.5 * prior_scale, shape=4)
            import pytensor.tensor as pt

            q = pt.concatenate([pt.zeros(1), q4])
            dq = q[bh] - q[ba]
        else:
            dq = 0.0
        pm.Poisson("hg", pm.math.exp(c + hh * (1 - neu) + base_h + dq), observed=hg)
        pm.Poisson("ag", pm.math.exp(c + base_a - dq), observed=ag)
        idata = pm.sample(draws=DRAWS, tune=DRAWS, chains=CHAINS, nuts_sampler="numpyro",
                          random_seed=seed, target_accept=0.9, progressbar=False)  # fmt: skip
    checks = dg.convergence(idata)
    post = idata["posterior"]
    flat = {v: np.asarray(post[v].values).reshape(-1, *post[v].shape[2:]) for v in post.data_vars}
    idx = np.linspace(0, len(flat["c"]) - 1, SIDE_DRAWS).astype(np.int64)
    q = (np.concatenate([np.zeros((len(flat["c"]), 1)), flat["q"]], axis=1) if with_q
         else np.zeros((len(flat["c"]), 5)))  # fmt: skip
    out = {"c": flat["c"][idx], "h": flat["h"][idx], "Q": q[idx]}
    # PPC: total goals per match, posterior predictive mean against the observed
    lam = np.exp(out["c"][:, None] + out["h"][:, None] * (1 - neu) + base_h
                 + (out["Q"][:, bh] - out["Q"][:, ba]))  # fmt: skip
    mu = np.exp(out["c"][:, None] + base_a - (out["Q"][:, bh] - out["Q"][:, ba]))
    diag = {c_.name: c_.value for c_ in checks}
    diag["gates_ok"] = all(c_.passed for c_ in checks)
    diag["ppc_total_pred"] = float((lam + mu).mean())
    diag["ppc_total_obs"] = float((hg + ag).mean())
    diag["n_train"] = len(hg)
    return out, diag


def predict(post: dict[str, Arr], tg: pl.DataFrame) -> Arr:
    """(n, 3) 1X2 per target: posterior draw k of (c, h, Q) with draw k of both sides'
    att/def and the home side's rho and kappa (Dixon-Coles, as the league model)."""
    from adatta.eval import cup_terms

    bi = {b: i for i, b in enumerate(BUNDLE_ORDER)}
    out = []
    for r in tg.iter_rows(named=True):
        dq = post["Q"][:, bi[r["bundle_home"]]] - post["Q"][:, bi[r["bundle_away"]]]
        ah, dh = np.asarray(r["att_draws_home"]), np.asarray(r["def_draws_home"])
        aa, da = np.asarray(r["att_draws_away"]), np.asarray(r["def_draws_away"])
        lam = np.exp(post["c"] + post["h"] * (1 - float(r["neutral"])) + ah - da + dq)
        mu = np.exp(post["c"] + aa - dh - dq)
        m = cup_terms.matrices(lam, mu, np.asarray(r["rho_home"]), np.asarray(r["kappa_home"]),
                               MAX_GOALS)  # fmt: skip
        ph, pd = np.tril(m, -1).sum(axis=(-2, -1)), np.trace(m, axis1=-2, axis2=-1)
        out.append([ph.mean(), pd.mean(), 1 - ph.mean() - pd.mean()])
    return np.asarray(out)


# ------------------------------------------------------------------------------------ Elo


def elo_history(session: Any, bundles: dict[str, list[int]]) -> pl.DataFrame:
    """Every finished match since ELO_FROM of the bundle leagues, the five cups and the UEFA
    competitions, with its 90' result (or the stored total where no 90' split), in time
    order; and each team's league (bundle leagues) for new-club starts."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.features import cup_ties

    M, S, C, R = models.Match, models.Season, models.Competition, models.MatchResult
    leagues = [str(x) for ls in bundles.values() for x in ls]
    comps = [*leagues, *(str(c) for c in cup_ties.cup_bundles()), *COMPS]
    rows = session.execute(
        select(M.match_id, M.kickoff_time, M.home_team_id, M.away_team_id, C.source_ref,
               S.year, R.home_goals_nt, R.away_goals_nt)
        .join(S, S.season_id == M.season_id).join(C, C.competition_id == S.competition_id)
        .join(R, (R.match_id == M.match_id) & R.superseded_at.is_(None))
        .where(C.source_ref.in_(comps), M.kickoff_time >= ELO_FROM,
               M.status.in_(["FT", "AET", "PEN"]), S.year.in_(SEASONS))
    ).all()  # fmt: skip
    return pl.DataFrame(
        [{"match_id": r[0], "kickoff": _utc(r[1]), "home": r[2], "away": r[3],
          "league": r[4] if r[4] in leagues else None, "hg": r[6], "ag": r[7]}
         for r in rows if r[6] is not None and r[7] is not None],
        schema={"match_id": pl.Int64, "kickoff": pl.Datetime("us", "UTC"), "home": pl.Int64,
                "away": pl.Int64, "league": pl.Utf8, "hg": pl.Int64, "ag": pl.Int64},
    ).sort("kickoff", "match_id")  # fmt: skip


def elo_at(
    hist: pl.DataFrame,
    queries: list[tuple[int, datetime, int, int]],
    neutral: set[int] | None = None,
) -> dict[int, float]:
    """Home-minus-away Elo difference (with the home advantage unless neutral) for each query
    (match_id, Monday, home, away), from the matches final by that Monday."""
    neutral = neutral or set()
    qs = sorted(queries, key=lambda q: q[1])
    rating: dict[int, float] = {}
    team_league: dict[int, str] = {}
    total: dict[str, float] = {}  # sum and count of each league's current members' ratings
    count: dict[str, int] = {}
    out: dict[int, float] = {}
    rows = hist.iter_rows(named=True)
    pending = next(rows, None)

    def move(t: int, lg: str | None) -> None:
        old = team_league.get(t)
        if lg is None or old == lg:
            return
        if old is not None:
            total[old] -= rating[t]
            count[old] -= 1
        team_league[t] = lg
        total[lg] = total.get(lg, 0.0) + rating[t]
        count[lg] = count.get(lg, 0) + 1

    def bump(t: int, d: float) -> None:
        rating[t] += d
        lg = team_league.get(t)
        if lg is not None:
            total[lg] += d

    for mid, mon, h, a in qs:
        while pending is not None and pending["kickoff"] + LAG <= mon:
            r = pending
            for t in (r["home"], r["away"]):
                if t not in rating:  # a new club starts at its league's mean (else 1,500)
                    lg = r["league"]
                    rating[t] = (total[lg] / count[lg]) if lg is not None and count.get(lg) \
                        else ELO_START  # fmt: skip
                move(t, r["league"])
            eh = 1 / (1 + 10 ** (-(rating[r["home"]] + ELO_HOME - rating[r["away"]]) / 400))
            sc = 1.0 if r["hg"] > r["ag"] else 0.5 if r["hg"] == r["ag"] else 0.0
            bump(r["home"], ELO_K * (sc - eh))
            bump(r["away"], -ELO_K * (sc - eh))
            pending = next(rows, None)
        rh, ra = rating.get(h, ELO_START), rating.get(a, ELO_START)
        out[mid] = rh - ra + (0.0 if mid in neutral else ELO_HOME)
    return out


def ordered_logit(x: Arr, y: NDArray[np.int64]) -> tuple[float, float, float]:
    """MLE of P(home) = 1 - F(c2 - b x), P(away) = F(c1 - b x), F logistic, on Elo
    differences x (in 100s) and outcomes y (0 home, 1 draw, 2 away)."""
    from scipy.optimize import minimize

    def nll(p: Arr) -> float:
        b, c1, d = p
        c2 = c1 + np.exp(d)
        pa = 1 / (1 + np.exp(-(c1 - b * x)))
        pnot_h = 1 / (1 + np.exp(-(c2 - b * x)))
        probs = np.stack([1 - pnot_h, pnot_h - pa, pa], axis=1)
        return float(-np.log(np.clip(probs[np.arange(len(y)), y], 1e-12, 1)).sum())

    res = minimize(nll, np.array([0.5, -0.5, 0.0]), method="Nelder-Mead",
                   options={"maxiter": 4000, "xatol": 1e-6, "fatol": 1e-8})  # fmt: skip
    b, c1, d = res.x
    return float(b), float(c1), float(c1 + np.exp(d))


def logit_probs(x: Arr, b: float, c1: float, c2: float) -> Arr:
    pa = 1 / (1 + np.exp(-(c1 - b * x)))
    pnot_h = 1 / (1 + np.exp(-(c2 - b * x)))
    return np.stack([1 - pnot_h, pnot_h - pa, pa], axis=1)


# --------------------------------------------------------------------------------- scores


def outcome(hg: Arr, ag: Arr) -> NDArray[np.int64]:
    return np.where(hg > ag, 0, np.where(hg == ag, 1, 2)).astype(np.int64)


def log_loss(p: Arr, y: NDArray[np.int64]) -> Arr:
    return np.asarray(-np.log(np.clip(p[np.arange(len(y)), y], 1e-12, 1)))


def rps(p: Arr, y: NDArray[np.int64]) -> Arr:
    o = np.zeros_like(p)
    o[np.arange(len(y)), y] = 1
    cp, co = np.cumsum(p, axis=1)[:, :2], np.cumsum(o, axis=1)[:, :2]
    return np.asarray(((cp - co) ** 2).sum(axis=1) / 2)


def paired(a: Arr, b: Arr) -> dict[str, float]:
    d = a - b
    half = 1.96 * d.std(ddof=1) / np.sqrt(len(d)) if len(d) > 1 else float("nan")
    return {"n": len(d), "mean": float(d.mean()), "lo": float(d.mean() - half),
            "hi": float(d.mean() + half), "rel": float(d.mean() / b.mean())}  # fmt: skip


def calibration(p: Arr, y: NDArray[np.int64], n_sims: int = 2000) -> dict[str, Any]:
    """1X2 ECE per outcome against its noise floor (outcomes drawn from the forecast)."""
    from adatta.eval import knockout as ek

    rng = np.random.default_rng(SEED)
    cum = p.cumsum(axis=1)
    sims: list[list[float]] = [[], [], []]
    for _ in range(n_sims):
        ys = (rng.random(len(p))[:, None] > cum).sum(axis=1)
        for k in range(3):
            sims[k].append(ek.ece(p[:, k], (ys == k).astype(float)))
    out = {}
    for k, name in enumerate(("home", "draw", "away")):
        e = ek.ece(p[:, k], (y == k).astype(float))
        lim = float(np.quantile(sims[k], 0.95))
        out[name] = {"ece": e, "limit": lim, "pass": bool(e <= lim)}
    return out


# ------------------------------------------------------------------------------------ run


def join_sides(tg: pl.DataFrame, st: pl.DataFrame) -> pl.DataFrame:
    """One row per target with both sides' strengths (means and draws)."""

    def side(s: str) -> pl.DataFrame:
        x = st.filter(pl.col("side") == s).select(
            "match_id", pl.col("att_mean").alias(f"att_{s}"), pl.col("def_mean").alias(f"def_{s}"),
            pl.col("att").alias(f"att_draws_{s}"), pl.col("def").alias(f"def_draws_{s}"),
            pl.col("rho").alias(f"rho_{s}"), pl.col("kappa").alias(f"kappa_{s}"),
            pl.col("cold").alias(f"cold_{s}"), pl.col("gates_ok").alias(f"refit_ok_{s}"),
        )  # fmt: skip
        return x

    return tg.join(side("home"), on="match_id", how="inner").join(side("away"), on="match_id",
                                                                  how="inner")  # fmt: skip


def run_fit(tg: pl.DataFrame, elo: dict[int, float]) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """C1 and B_own on the 28-day grid over the dev targets, walk-forward; Elo's ordered
    logit refitted at the same times. Returns scored targets and per-refit diagnostics."""
    from adatta.eval import harness

    cross = tg.filter(pl.col("cross"))
    dev = cross.filter(pl.col("season").is_in(list(DEV)))
    times = harness.as_of_times(dev["monday"].min(), dev["monday"].max(), REFIT_DAYS)  # type: ignore[arg-type]
    rows: list[pl.DataFrame] = []
    diags: list[dict[str, Any]] = []
    for i, t in enumerate(times):
        nxt = times[i + 1] if i + 1 < len(times) else datetime.max.replace(tzinfo=UTC)
        tgt = dev.filter((pl.col("monday") >= t) & (pl.col("monday") < nxt))
        if tgt.is_empty():
            continue
        train = cross.filter(pl.col("kickoff") + LAG <= t)
        assert (train["kickoff"] + LAG <= t).all()
        post_q, dq = fit_q(train, True)
        post_0, d0 = fit_q(train, False)
        x = np.array([elo[m] for m in train["match_id"]]) / 100
        y = outcome(train["hg"].to_numpy(), train["ag"].to_numpy())
        b, c1, c2 = ordered_logit(x, y)
        pe = logit_probs(np.array([elo[m] for m in tgt["match_id"]]) / 100, b, c1, c2)
        pq, p0 = predict(post_q, tgt), predict(post_0, tgt)
        rows.append(tgt.select("match_id", "comp", "season", "hg", "ag", "neutral").with_columns(
            refit=pl.lit(t), **{f"{m}_{o}": pl.Series(p[:, k]) for m, p in
                                (("c1", pq), ("own", p0), ("elo", pe))
                                for k, o in enumerate(("home", "draw", "away"))}))  # fmt: skip
        qm = post_q["Q"].mean(axis=0)
        diags.append({"as_of": t.isoformat(), "n_train": dq["n_train"], "targets": tgt.height,
                      "c1": dq, "own": d0, "q": dict(zip(BUNDLE_ORDER, qm.tolist(), strict=True)),
                      "q_sd": dict(zip(BUNDLE_ORDER, post_q["Q"].std(axis=0).tolist(),
                                       strict=True)),
                      "elo_logit": [b, c1, c2]})  # fmt: skip
        print(f"refit {t:%Y-%m-%d}: {dq['n_train']} train, {tgt.height} targets; C1 rhat "
              f"{dq['rhat']:.3f} div {dq['divergences']:.0f}; Q " +
              " ".join(f"{k[:3]} {v:+.2f}" for k, v in zip(BUNDLE_ORDER, qm, strict=True)),
              flush=True)  # fmt: skip
    return pl.concat(rows), diags


def gate(scored: pl.DataFrame, diags: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    y = outcome(scored["hg"].to_numpy(), scored["ag"].to_numpy())
    p = {m: scored.select(f"{m}_home", f"{m}_draw", f"{m}_away").to_numpy()
         for m in ("c1", "own", "elo")}  # fmt: skip
    ll = {m: log_loss(v, y) for m, v in p.items()}
    rp = {m: rps(v, y) for m, v in p.items()}
    cmp = {b: {"log_loss": paired(ll["c1"], ll[b]), "rps": paired(rp["c1"], rp[b])}
           for b in ("own", "elo")}  # fmt: skip
    by_comp: dict[str, dict[str, float]] = {}
    for comp in sorted(set(scored["comp"].to_list())):
        s = (scored["comp"] == comp).to_numpy()
        by_comp[comp] = {"n": int(s.sum()),
                         **{f"vs_{b}": float((ll["c1"][s].mean() - ll[b][s].mean())
                                             / ll[b][s].mean())
                            for b in ("own", "elo")}}  # fmt: skip
    cal = calibration(p["c1"], y)
    checks = {
        "1 C1 beats B_own (log loss and RPS, intervals below 0)":
            cmp["own"]["log_loss"]["hi"] < 0 and cmp["own"]["rps"]["hi"] < 0,
        "2 C1 beats B_elo (log loss and RPS, intervals below 0)":
            cmp["elo"]["log_loss"]["hi"] < 0 and cmp["elo"]["rps"]["hi"] < 0,
        "3 no competition worse than either by > 1.0%":
            all(v[f"vs_{b}"] <= 0.01 for v in by_comp.values() for b in ("own", "elo")),
        "4 C1 1X2 ECE within its noise floor":
            all(cal[o]["pass"] for o in ("home", "draw", "away")),
        "5 inference gates on every C1 and B_own refit":
            all(d["c1"]["gates_ok"] and d["own"]["gates_ok"] for d in diags),
    }  # fmt: skip
    mll = {m: float(v.mean()) for m, v in ll.items()}
    mrp = {m: float(v.mean()) for m, v in rp.items()}
    rep: dict[str, Any] = {"n": len(y), "ll": mll, "rps": mrp, "cmp": cmp, "by_comp": by_comp,
                           "calibration": cal, "checks": checks}  # fmt: skip
    last = diags[-1]
    lines = ["# C1 on dev: continental matches between bundles", "",
             f"{len(y)} dev cross-bundle UEFA matches, {len(diags)} refits.",
             "| model | 1X2 log loss | RPS |", "|---|---|---|",
             *[f"| {m} | {mll[m]:.4f} | {mrp[m]:.4f} |" for m in ("c1", "own", "elo")],
             ""]  # fmt: skip
    for b in ("own", "elo"):
        lo_, rp_ = cmp[b]["log_loss"], cmp[b]["rps"]
        lines.append(f"C1 vs {b}: log loss {lo_['mean']:+.4f} [{lo_['lo']:+.4f}, {lo_['hi']:+.4f}] "
                     f"({lo_['rel']:+.2%}); RPS {rp_['mean']:+.4f} [{rp_['lo']:+.4f}, "
                     f"{rp_['hi']:+.4f}] ({rp_['rel']:+.2%})")  # fmt: skip
    lines += ["", "By competition (C1 relative): " + "; ".join(
        f"{c} n {v['n']} vs own {v['vs_own']:+.2%}, vs Elo {v['vs_elo']:+.2%}"
        for c, v in by_comp.items()),
        "Calibration (ECE / limit): " + ", ".join(
            f"{o} {cal[o]['ece']:.3f}/{cal[o]['limit']:.3f}" for o in ("home", "draw", "away")),
        "Q at the last refit (sd): " + ", ".join(
            f"{b} {last['q'][b]:+.3f} ({last['q_sd'][b]:.3f})" for b in BUNDLE_ORDER),
        f"PPC total goals at the last refit: predicted {last['c1']['ppc_total_pred']:.3f}, "
        f"observed {last['c1']['ppc_total_obs']:.3f}. Worst C1 R-hat "
        f"{max(d['c1']['rhat'] for d in diags):.3f}, divergences "
        f"{sum(d['c1']['divergences'] for d in diags):.0f}.", "",
        "Checks: " + "; ".join(f"{k}: {'pass' if v else 'FAIL'}"
                               for k, v in checks.items())]  # fmt: skip
    return "\n".join(lines) + "\n", rep


def sensitivity(tg: pl.DataFrame, t: datetime) -> dict[str, list[float]]:
    """Q at the last refit with every prior sd halved and doubled (reported)."""
    train = tg.filter(pl.col("cross") & (pl.col("kickoff") + LAG <= t))
    return {str(s): fit_q(train, True, s)[0]["Q"].mean(axis=0).tolist() for s in (0.5, 1.0, 2.0)}


def leakage(tg: pl.DataFrame, hist: pl.DataFrame, elo: dict[int, float],
            neutral: set[int]) -> dict[str, Any]:  # fmt: skip
    """(6) For sampled targets: the strengths come from the Monday refit (as_of = Monday <=
    kickoff) and the Elo recomputed from matches final by the Monday alone is the same."""
    rng = np.random.default_rng(SEED)
    dev = tg.filter(pl.col("cross") & pl.col("season").is_in(list(DEV)))
    idx = rng.choice(dev.height, min(LEAKAGE_N, dev.height), replace=False)
    bad = 0
    for i in idx:
        r = dev.row(int(i), named=True)
        cut = hist.filter(pl.col("kickoff") + LAG <= r["monday"])
        again = elo_at(cut, [(r["match_id"], r["monday"], r["home"], r["away"])], neutral)
        bad += abs(again[r["match_id"]] - elo[r["match_id"]]) > 1e-9
        bad += not (r["monday"] <= r["kickoff"])
    return {"n": len(idx), "changed": int(bad), "ok": bad == 0}


def main(argv: Sequence[str] | None = None) -> None:
    from adatta.db.session import get_session
    from adatta.eval import harness

    ap = argparse.ArgumentParser(prog="adatta.model.continental")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("strengths")
    s1.add_argument("--bundle", required=True)
    s1.add_argument("--part", default="0/1")
    s1.add_argument("--upload", required=True)
    s2 = sub.add_parser("fit")
    s2.add_argument("--fetch", required=True)
    s2.add_argument("--parts", type=int, default=4)
    args = ap.parse_args(argv)
    store = make_store(get_settings())
    cfg = harness.load_config()
    if args.cmd == "strengths":
        i, n = (int(x) for x in args.part.split("/"))
        with get_session() as s:
            rows = strengths(store, s, args.bundle, (i, n))
        buf = io.BytesIO()
        rows.write_parquet(buf, compression="zstd")
        store.put(
            f"{PREFIX}/strengths/{args.upload}/{args.bundle}_{i}of{n}.parquet", buf.getvalue()
        )
        print(f"{args.bundle} {i}/{n}: {rows.height} side rows")
        return
    parts = []
    for b in cfg.bundles:
        for i in range(args.parts):
            raw = try_get(store, f"{PREFIX}/strengths/{args.fetch}/{b}_{i}of{args.parts}.parquet")
            if raw is None:
                raise SystemExit(f"missing strengths {b} {i}/{args.parts}")
            parts.append(pl.read_parquet(io.BytesIO(raw)))
    st = pl.concat([p for p in parts if p.height], how="diagonal_relaxed")
    with get_session() as s:
        tg0 = targets(s, cfg.bundles)
        hist = elo_history(s, cfg.bundles)
        s.rollback()
    tg = join_sides(tg0, st)
    print(f"targets with both sides' strengths: {tg.height} of {tg0.height} (cross-bundle "
          f"{tg.filter(pl.col('cross')).height}); cold sides "
          f"{int(tg['cold_home'].cast(pl.Int64).sum()) + int(tg['cold_away'].cast(pl.Int64).sum())}"
          f"; sides from refits failing the gates "
          f"{int((~tg['refit_ok_home']).cast(pl.Int64).sum())
             + int((~tg['refit_ok_away']).cast(pl.Int64).sum())}")  # fmt: skip
    neutral = set(tg.filter(pl.col("neutral"))["match_id"].to_list())
    elo = elo_at(hist, [(r["match_id"], r["monday"], r["home"], r["away"])
                        for r in tg.iter_rows(named=True)], neutral)  # fmt: skip
    scored, diags = run_fit(tg, elo)
    text, rep = gate(scored, diags)
    sens = sensitivity(tg, datetime.fromisoformat(diags[-1]["as_of"]))
    leak = leakage(tg, hist, elo, neutral)
    rep["checks"]["6 leakage"] = leak["ok"]
    rep["passed"] = all(rep["checks"].values())
    text += (f"6 leakage: {leak['changed']} of {leak['n']} sampled targets changed: "
             f"{'pass' if leak['ok'] else 'FAIL'}.\n"
             "Prior sensitivity, Q at the last refit (prior sd x0.5 / x1 / x2): "
             + "; ".join(f"{b} " + " / ".join(f"{sens[k][j]:+.3f}" for k in ("0.5", "1.0", "2.0"))
                         for j, b in enumerate(BUNDLE_ORDER)) + "\n"
             f"**C1 {'PASSES' if rep['passed'] else 'FAILS'} on dev.**\n")  # fmt: skip
    buf = io.BytesIO()
    scored.write_parquet(buf, compression="zstd")
    store.put(f"{PREFIX}/reports/{args.fetch}/scored.parquet", buf.getvalue())
    store.put(f"{PREFIX}/reports/{args.fetch}/verdict.json",
              json.dumps({"passed": rep["passed"], "checks": rep["checks"], "diags": diags},
                         default=str).encode())  # fmt: skip
    print(text)


if __name__ == "__main__":
    main()
