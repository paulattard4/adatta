"""Stage C, C1: continental club matches between bundle teams on a common scale
(docs/BEYOND_LEAGUES_PLAN.md, Stage C, "C1 acceptance"; spec §10.1).

log lambda_home = c + h (1 - neutral) + att_home - def_away + Q[home bundle] - Q[away bundle]
(England Q = 0), each side's att / def from its own bundle's production weekly refit as of
the match's Monday (as Stage A's A1). Against B_own (Q = 0) and an Elo-style baseline.

C2 step 4 (D56): the rerun over all 13 bundles, the five and the eight C2 bundles (12 free
Q). A C2 side's att / def come from Phase 1a as served (`c2_live.fit_as_of` at the match's
Monday; a point fit, so constant across draws, kappa 0). The venue rule sees every bundle's
league grounds, and matrices are scored to 20 goals a side (D53), the tail reported.

CLI (CI, model group for `strengths` and `fit`; one backtest-type run at a time):
  strengths --bundle B --part i/n --upload RUN
            each side's attack / defence draws as of its match's Monday, from the cached
            refit or a new one (fitted and cached like any backtest refit)
            -> continental/strengths/RUN/<bundle>_<i>of<n>.parquet
  fit       --fetch RUN
            C1 and B_own walk-forward on the 28-day grid, the Elo, the gate; metrics only
            to stdout, the scored rows to R2 (continental/reports/RUN/)
  claim     --run RUN --commit SHA
            open the UEFA holdout once (marker "uefa", D58; `eval.holdout`)
  strengths / fit with --holdout RUN
            the same stages over the holdout seasons too, scored on the holdout alone and
            judged by checks 1-4 (config/uefa_gate.yml)
Holdout seasons are loaded only with the "uefa" opening's key. The repo is public: logs
carry metrics, never data.
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
from adatta.eval.harness import HoldoutKey, HoldoutLocked

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
MAX_GOALS = 20  # D53 / D56 (C1 scored to 10)
TAIL_MAX = 1e-4  # reported per prediction
ELO_START, ELO_K, ELO_HOME = 1500.0, 20.0, 60.0  # fixed in the acceptance, not tuned
ELO_FROM = datetime(2016, 7, 1, tzinfo=UTC)
LEAKAGE_N = 50
MIN_HISTORY = timedelta(days=365)  # a side's bundle needs this much stored league history
OPENING = "uefa"  # the holdout opening (D58), made once
MODEL_NAME = "c1_continental"
GATE_FILE = "config/uefa_gate.yml"
ECE_SIMS, ECE_QUANTILE = 2000, 0.95
# C1 v2 (D62): a UEFA low-score dependence term per format phase, read from the round label
PHASES = ("group_stage", "league_phase", "qualifying", "knockout")
RHO_PRIOR_SD = 0.1
LIVE_GATE_FILE = "config/uefa_live_gate.yml"
REDO_TARGET_ACCEPT, REDO_DRAWS = 0.99, 4000  # one redo of a failing side refit (as A3)

TARGET_SCHEMA: dict[str, Any] = {
    "match_id": pl.Int64, "comp": pl.Utf8, "season": pl.Int32,
    "kickoff": pl.Datetime("us", "UTC"), "monday": pl.Datetime("us", "UTC"),
    "home": pl.Int64, "away": pl.Int64, "bundle_home": pl.Utf8, "bundle_away": pl.Utf8,
    "league_home": pl.Int64, "league_away": pl.Int64, "hg": pl.Int64, "ag": pl.Int64,
    "neutral": pl.Boolean, "cross": pl.Boolean, "swapped": pl.Boolean, "phase": pl.Utf8,
}  # fmt: skip


def bundles() -> dict[str, list[int]]:
    """The 13 bundles of the rerun (D56): the five (evaluation.yml), then the eight C2
    bundles (config/c2_bundles.yml), top flight first in each."""
    from adatta.eval import c2, harness

    return {**harness.load_config().bundles, **c2.load_config().bundles}


def league_competition_ids(session: Any, bundles_: dict[str, list[int]]) -> list[int]:
    """Internal competition ids of every bundle league (the venue rule's grounds, D56 (b))."""
    from sqlalchemy import select

    from adatta.adapters.normalize import SOURCE_ID
    from adatta.db import models

    C = models.Competition
    refs = [str(x) for ls in bundles_.values() for x in ls]
    return sorted(session.scalars(
        select(C.competition_id).where(C.source_id == SOURCE_ID, C.source_ref.in_(refs))
    ).all())  # fmt: skip


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def monday(t: datetime) -> datetime:
    t = _utc(t)
    return (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def phase_of(round_label: str | None) -> str | None:
    """The UEFA format phase of a round label (D62), known before kickoff; None if unclassed.
    Play-offs in August are qualifying; the February knockout round play-offs are knockout."""
    import re

    r = (round_label or "").strip()
    if re.match(r"^League Stage", r):
        return "league_phase"
    if re.match(r"^Group", r):
        return "group_stage"
    if re.search(r"Qualifying|Preliminary|^Play-offs|^Playoff round", r, re.IGNORECASE):
        return "qualifying"
    if re.search(r"Finals|^Round of|Quarter|Semi|^Final|Knockout", r):
        return "knockout"
    return None


def load_live_gate(path: str = LIVE_GATE_FILE) -> dict[str, Any]:
    """C1 v2's pre-registered gate (D62); refused unless it describes this code."""
    from pathlib import Path

    import yaml

    g: dict[str, Any] = yaml.safe_load(Path(path).read_text())["uefa_live_gate"]
    if tuple(g["rho_phases"]) != PHASES or g["rho_prior_sd"] != RHO_PRIOR_SD:
        raise ValueError(f"{path} does not match C1 v2's draw term: {g}")
    return g


def seasons(holdout: HoldoutKey | None = None) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """(loaded, scored) seasons: dev without a key; with the "uefa" opening's key, every
    season through the holdout, scored on the holdout seasons alone."""
    if holdout is None:
        return SEASONS, DEV
    if holdout.opening != OPENING:
        raise HoldoutLocked(f"the UEFA holdout needs the {OPENING!r} opening, not "
                            f"{holdout.opening!r}")  # fmt: skip
    from adatta.eval import harness

    ho = tuple(harness.load_config().holdout_seasons)
    return SEASONS + ho, ho


def load_gate(path: str = GATE_FILE) -> dict[str, Any]:
    """The pre-registered UEFA holdout gate; refused unless it describes this code's scoring."""
    from pathlib import Path

    import yaml

    from adatta.eval import harness

    g: dict[str, Any] = yaml.safe_load(Path(path).read_text())["uefa_gate"]
    want = {"seasons": list(harness.load_config().holdout_seasons), "grid_days": REFIT_DAYS,
            "max_goals": MAX_GOALS, "paired_interval": 0.95, "max_competition_rel_worse": 0.01,
            "ece_sims": ECE_SIMS, "ece_quantile": ECE_QUANTILE, "checks": [1, 2, 3, 4]}  # fmt: skip
    if g != want:
        raise ValueError(f"{path} does not match the scoring code: {g} != {want}")
    return g


# ------------------------------------------------------------------------------ targets


def targets(session: Any, bundles: dict[str, list[int]],
            holdout: HoldoutKey | None = None) -> pl.DataFrame:  # fmt: skip
    """Finished UEFA matches of the loaded seasons (`seasons`) between two teams that each
    play a bundle league that season (league matches), with the 90' score; a swapped venue
    swaps the sides."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.features import neutral
    from adatta.sim import serve

    loaded = seasons(holdout)[0]
    M, S, C, R = models.Match, models.Season, models.Competition, models.MatchResult
    rows = session.execute(
        select(M, C.source_ref, S.year, R.home_goals_nt, R.away_goals_nt)
        .join(S, S.season_id == M.season_id).join(C, C.competition_id == S.competition_id)
        .join(R, (R.match_id == M.match_id) & R.superseded_at.is_(None))
        .where(C.source_ref.in_(list(COMPS)), S.year.in_(loaded),
               M.status.in_(["FT", "AET", "PEN"]))
    ).all()  # fmt: skip
    rows = [r for r in rows if r[3] is not None and r[4] is not None]
    by_season: dict[int, dict[int, tuple[str, int]]] = {}
    for y in loaded:
        by_season[y] = {}
        for b, ls in bundles.items():
            for t, lg in serve.team_leagues(session, ls, y).items():
                by_season[y][t] = (b, lg)
    league_ids = league_competition_ids(session, bundles)  # D56 (b): every bundle's grounds
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
            "swapped": sw, "phase": phase_of(m.round),
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
              log: Any = print, holdout: HoldoutKey | None = None,
              redo: bool = False) -> pl.DataFrame:  # fmt: skip
    """Rows (match, side) for this bundle's sides, from the weekly refit of each match's
    Monday: cached, else fitted and cached under the league backtest's key."""
    from adatta.eval import c2, harness
    from adatta.model import backtest as bt
    from adatta.model import team_strength as ts

    cfg = harness.load_config()
    spec = bt.production_spec(cfg)
    every = bundles()
    leagues = every[bundle]
    tg = targets(session, every, holdout)
    session.rollback()
    sides = pl.concat([
        tg.filter(pl.col("bundle_home") == bundle).select(
            "match_id", "monday", team="home", league="league_home", side=pl.lit("home")),
        tg.filter(pl.col("bundle_away") == bundle).select(
            "match_id", "monday", team="away", league="league_away", side=pl.lit("away")),
    ])  # fmt: skip
    is_c2 = c2.is_c2(bundle)
    df = bt._load(leagues) if is_c2 else bt._load(leagues, spec.xg, spec.manager)
    first = _utc(df["kickoff"].min())  # type: ignore[arg-type]
    early = sides.filter(pl.col("monday") < first + MIN_HISTORY)
    if early.height:  # under a year of the bundle's stored league history: not used
        log(f"{bundle}: {early.height} sides before {first + MIN_HISTORY:%Y-%m-%d} skipped "
            f"(under {MIN_HISTORY.days} days of league history)")  # fmt: skip
    sides = sides.filter(pl.col("monday") >= first + MIN_HISTORY)
    mondays = sorted(set(sides["monday"].to_list()))[part[0] :: part[1]]
    if is_c2:
        return c2_strengths(df, leagues, bundle, sides, mondays, log)
    h, lag = bt.fit_hash(spec), cfg.result_available_after_hours
    fitter = bt.nuts_fitter(spec)
    from dataclasses import replace

    redo_spec = replace(spec, target_accept=REDO_TARGET_ACCEPT, draws=REDO_DRAWS)
    h_redo = bt.fit_hash(redo_spec)
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
        redone = False
        if redo and not _gates_ok(diag):  # C1 v2 (D62): one redo, cached under its own spec
            rkey = bt.cache_key(h_redo, bundle, mon)
            rraw = try_get(store, rkey)
            if rraw is not None:
                draws, diag = bt.decode(rraw)
            else:
                draws, diag = bt.nuts_fitter(redo_spec)(fd, spec.seed)
                store.put(rkey, bt.encode(draws, diag))
            redone = True
        n = np.asarray(draws["attack_last"]).shape[1]
        idx = np.linspace(0, n - 1, SIDE_DRAWS).astype(np.int64)
        for r in sides.filter(pl.col("monday") == mon).iter_rows(named=True):
            sd = side_strength(draws, fd, int(r["team"]), int(r["league"]), idx)
            out.append({"match_id": r["match_id"], "side": r["side"], "team": r["team"],
                        "bundle": bundle, "monday": mon, "refit_as_of": mon,
                        "gates_ok": _gates_ok(diag), "cached": raw is not None,
                        "redone": redone, **sd})  # fmt: skip
        log(f"  {i + 1}/{len(mondays)} {mon:%Y-%m-%d} {'cached' if raw is not None else 'fitted'}")
    return pl.DataFrame(out)


def c2_side(fit: Any, team: int, league: int) -> dict[str, Any]:
    """A C2 side from its bundle's Phase 1a fit (D56 choice 1a): att / def at the point fit,
    constant across the SIDE_DRAWS draws; an unseen team gets its league's average (the fit's
    rule, as served); rho is the fit's, kappa 0 (Phase 1a has none)."""
    tix = {int(t): i for i, t in enumerate(fit.team_ids)}
    lg = fit.leagues.index(league)
    t = tix.get(team, -1)
    p = fit.params
    a = float(p.attack[t]) if t >= 0 else float(fit.league_att[lg])
    d = float(p.defence[t]) if t >= 0 else float(fit.league_def[lg])
    return {
        "att_mean": a,
        "def_mean": d,
        "att": [a] * SIDE_DRAWS,
        "def": [d] * SIDE_DRAWS,
        "rho": [float(p.rho)] * SIDE_DRAWS,
        "kappa": [0.0] * SIDE_DRAWS,
        "cold": t < 0,
    }


def c2_strengths(df: pl.DataFrame, leagues: list[int], bundle: str, sides: pl.DataFrame,
                 mondays: list[datetime], log: Any = print) -> pl.DataFrame:  # fmt: skip
    """Rows (match, side) of a C2 bundle's sides from Phase 1a as of each match's Monday,
    the code path that serves these leagues (`c2_live.fit_as_of`)."""
    from adatta.model import c2_live

    cfg = c2_live.scoring_config()
    out: list[dict[str, Any]] = []
    log(f"{bundle}: {len(mondays)} Mondays, Phase 1a (D56)")
    for mon in mondays:
        fit = c2_live.fit_as_of(df, leagues, cfg, mon, bundle)
        ok = not c2_live.gate_failures(fit)
        for r in sides.filter(pl.col("monday") == mon).iter_rows(named=True):
            sd = c2_side(fit, int(r["team"]), int(r["league"]))
            out.append({"match_id": r["match_id"], "side": r["side"], "team": r["team"],
                        "bundle": bundle, "monday": mon, "refit_as_of": mon, "gates_ok": ok,
                        "cached": False, **sd})  # fmt: skip
    return pl.DataFrame(out)


def _gates_ok(diag: dict[str, float]) -> bool:
    from adatta.model import diagnostics as dg

    return bool(diag.get("rhat_max", 9) <= dg.RHAT_MAX and diag.get("ess_bulk_min", 0) >= dg.ESS_MIN
                and diag.get("divergences", 9) <= dg.DIVERGENCES_MAX)  # fmt: skip


# -------------------------------------------------------------------------------- C1 fit

# England is Q = 0; then the five, then the eight C2 bundles (D56; a test checks the configs)
BUNDLE_ORDER = ("england", "spain", "italy", "germany", "france", "portugal", "netherlands",
                "belgium", "austria", "scotland", "turkey", "czechia", "denmark")  # fmt: skip


def fit_q(train: pl.DataFrame, with_q: bool, prior_scale: float = 1.0, seed: int = SEED,
          rho_u: bool = False) -> tuple[dict[str, Arr], dict[str, Any]]:  # fmt: skip
    """C1 (with_q) or B_own on the training matches: Poisson goals, att/def at their means.
    With `rho_u` (C1 v2, D62) the Dixon-Coles low-score term rho_U per format phase is
    fitted too, prior Normal(the training matches' mean domestic rho, RHO_PRIOR_SD).
    Returns thinned draws {c, h, Q (n, 13)[, rho_u (n, 4)]} and diagnostics (§14.5 gates,
    PPC of goals)."""
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
            q4 = pm.Normal("q", 0.0, 0.5 * prior_scale, shape=len(BUNDLE_ORDER) - 1)
            import pytensor.tensor as pt

            q = pt.concatenate([pt.zeros(1), q4])
            dq = q[bh] - q[ba]
        else:
            dq = 0.0
        lam_t = pm.math.exp(c + hh * (1 - neu) + base_h + dq)
        mu_t = pm.math.exp(c + base_a - dq)
        pm.Poisson("hg", lam_t, observed=hg)
        pm.Poisson("ag", mu_t, observed=ag)
        if rho_u:
            import pytensor.tensor as pt

            ph = np.array([PHASES.index(x) for x in train["phase"]])
            rho0 = float(train["rho_home"].list.mean().mean())  # type: ignore[arg-type]
            r_all = pm.Normal("rho_u", rho0, RHO_PRIOR_SD * prior_scale, shape=len(PHASES))
            r = r_all[ph]
            m00, m01 = (hg == 0) & (ag == 0), (hg == 0) & (ag == 1)
            m10, m11 = (hg == 1) & (ag == 0), (hg == 1) & (ag == 1)
            tau = (m00 * (1 - lam_t * mu_t * r) + m01 * (1 + lam_t * r) + m10 * (1 + mu_t * r)
                   + m11 * (1 - r) + ~(m00 | m01 | m10 | m11) * 1.0)  # fmt: skip
            pm.Potential("dixon_coles", pt.log(pt.maximum(tau, 1e-10)).sum())
        idata = pm.sample(draws=DRAWS, tune=DRAWS, chains=CHAINS, nuts_sampler="numpyro",
                          random_seed=seed, target_accept=0.9, progressbar=False)  # fmt: skip
    checks = dg.convergence(idata)
    post = idata["posterior"]
    flat = {v: np.asarray(post[v].values).reshape(-1, *post[v].shape[2:]) for v in post.data_vars}
    idx = np.linspace(0, len(flat["c"]) - 1, SIDE_DRAWS).astype(np.int64)
    q = (np.concatenate([np.zeros((len(flat["c"]), 1)), flat["q"]], axis=1) if with_q
         else np.zeros((len(flat["c"]), len(BUNDLE_ORDER))))  # fmt: skip
    out = {"c": flat["c"][idx], "h": flat["h"][idx], "Q": q[idx]}
    if rho_u:
        out["rho_u"] = flat["rho_u"][idx]
    # PPC: total goals per match, posterior predictive mean against the observed
    lam = np.exp(out["c"][:, None] + out["h"][:, None] * (1 - neu) + base_h
                 + (out["Q"][:, bh] - out["Q"][:, ba]))  # fmt: skip
    mu = np.exp(out["c"][:, None] + base_a - (out["Q"][:, bh] - out["Q"][:, ba]))
    diag: dict[str, Any] = {c_.name: c_.value for c_ in checks}
    diag["gates_ok"] = all(c_.passed for c_ in checks)
    diag["ppc_total_pred"] = float((lam + mu).mean())
    diag["ppc_total_obs"] = float((hg + ag).mean())
    diag["n_train"] = len(hg)
    if rho_u:
        diag["rho_u"] = dict(zip(PHASES, out["rho_u"].mean(axis=0).tolist(), strict=True))
    return out, diag


def tails(post: dict[str, Arr], tg: pl.DataFrame) -> Arr:
    """Per target, the largest mass beyond MAX_GOALS a side over the draws (D53), before
    renormalising: 1 - P(home <= MAX_GOALS) P(away <= MAX_GOALS) at each draw's rates."""
    from scipy.stats import poisson

    bi = {b: i for i, b in enumerate(BUNDLE_ORDER)}
    out = []
    for r in tg.iter_rows(named=True):
        dq = post["Q"][:, bi[r["bundle_home"]]] - post["Q"][:, bi[r["bundle_away"]]]
        lam = np.exp(
            post["c"]
            + post["h"] * (1 - float(r["neutral"]))
            + np.asarray(r["att_draws_home"])
            - np.asarray(r["def_draws_away"])
            + dq
        )
        mu = np.exp(post["c"] + np.asarray(r["att_draws_away"]) - np.asarray(r["def_draws_home"])
                    - dq)  # fmt: skip
        both = np.asarray(poisson.cdf(MAX_GOALS, lam) * poisson.cdf(MAX_GOALS, mu))
        out.append(float((1 - both).max()))
    return np.asarray(out)


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
        rho = (
            post["rho_u"][:, PHASES.index(r["phase"])]
            if "rho_u" in post
            else np.asarray(r["rho_home"])
        )  # C1 v2 (D62): the UEFA phase's rho
        m = cup_terms.matrices(lam, mu, rho, np.asarray(r["kappa_home"]), MAX_GOALS)
        ph, pd = np.tril(m, -1).sum(axis=(-2, -1)), np.trace(m, axis1=-2, axis2=-1)
        out.append([ph.mean(), pd.mean(), 1 - ph.mean() - pd.mean()])
    return np.asarray(out)


# ------------------------------------------------------------------------------------ Elo


def elo_history(session: Any, bundles: dict[str, list[int]],
                holdout: HoldoutKey | None = None) -> pl.DataFrame:  # fmt: skip
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
               M.status.in_(["FT", "AET", "PEN"]), S.year.in_(seasons(holdout)[0]))
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


def calibration(p: Arr, y: NDArray[np.int64], n_sims: int = ECE_SIMS) -> dict[str, Any]:
    """1X2 ECE per outcome against its noise floor (outcomes drawn from the forecast)."""
    from adatta.eval import knockout as ek

    rng = np.random.default_rng(SEED)
    cum = p.cumsum(axis=1)  # ECE_SIMS draws, ECE_QUANTILE: the gate file's values
    sims: list[list[float]] = [[], [], []]
    for _ in range(n_sims):
        ys = (rng.random(len(p))[:, None] > cum).sum(axis=1)
        for k in range(3):
            sims[k].append(ek.ece(p[:, k], (ys == k).astype(float)))
    out = {}
    for k, name in enumerate(("home", "draw", "away")):
        e = ek.ece(p[:, k], (y == k).astype(float))
        lim = float(np.quantile(sims[k], ECE_QUANTILE))
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


def run_fit(tg: pl.DataFrame, elo: dict[int, float], scored: Sequence[int] = DEV,
            variant: str = "c1") -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """C1 and B_own on the 28-day grid over the targets of the `scored` seasons (dev, or the
    holdout), walk-forward from the first one's Monday, trained on every cross-bundle match
    final before each refit; Elo's ordered logit refitted at the same times. Returns scored
    targets and per-refit diagnostics."""
    from adatta.eval import harness

    cross = tg.filter(pl.col("cross"))
    dev = cross.filter(pl.col("season").is_in(list(scored)))
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
        post_q, dq = fit_q(train, True, rho_u=variant == "v2")
        post_0, d0 = fit_q(train, False)
        x = np.array([elo[m] for m in train["match_id"]]) / 100
        y = outcome(train["hg"].to_numpy(), train["ag"].to_numpy())
        b, c1, c2 = ordered_logit(x, y)
        pe = logit_probs(np.array([elo[m] for m in tgt["match_id"]]) / 100, b, c1, c2)
        pq, p0 = predict(post_q, tgt), predict(post_0, tgt)
        rows.append(tgt.select("match_id", "comp", "season", "hg", "ag", "neutral",
                               "bundle_home", "bundle_away", "phase").with_columns(
            refit=pl.lit(t), c1_tail=pl.Series(tails(post_q, tgt)),
            **{f"{m}_{o}": pl.Series(p[:, k]) for m, p in
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


def gate(scored: pl.DataFrame, diags: list[dict[str, Any]],
         label: str = "dev") -> tuple[str, dict[str, Any]]:  # fmt: skip
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
    # D56 (d): C1's 384 (both sides in the five) apart from the matches with a C2 side
    five = set(BUNDLE_ORDER[:5])
    in_five = (scored["bundle_home"].is_in(five) & scored["bundle_away"].is_in(five)).to_numpy() \
        if "bundle_home" in scored.columns else np.ones(len(y), dtype=bool)  # fmt: skip
    split: dict[str, dict[str, Any]] = {}
    for name, sel in (("five only (C1's matches)", in_five), ("with a C2 side", ~in_five)):
        if sel.sum() > 1:
            split[name] = {
                "n": int(sel.sum()),
                **{b: paired(ll["c1"][sel], ll[b][sel]) for b in ("own", "elo")},
            }
    tail = float(scored["c1_tail"].max()) if "c1_tail" in scored.columns else 0.0  # type: ignore[arg-type]
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
                           "calibration": cal, "checks": checks, "split": split,
                           "max_tail": tail}  # fmt: skip
    last = diags[-1]
    lines = [f"# C1 on {label}: continental matches between bundles (13 bundles, D56)", "",
             f"{len(y)} {label} cross-bundle UEFA matches, {len(diags)} refits.",
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
        f"{sum(d['c1']['divergences'] for d in diags):.0f}.",
        "Log loss, C1 minus each baseline, by subset (D56, reported): " + "; ".join(
            f"{k} n {v['n']}: vs own {v['own']['mean']:+.4f} [{v['own']['lo']:+.4f}, "
            f"{v['own']['hi']:+.4f}], vs Elo {v['elo']['mean']:+.4f} [{v['elo']['lo']:+.4f}, "
            f"{v['elo']['hi']:+.4f}]" for k, v in split.items()),
        f"Largest C1 tail beyond {MAX_GOALS} goals a side: {tail:.1e} "
        f"({'under' if tail < TAIL_MAX else 'OVER'} {TAIL_MAX:.0e}).", "",
        "Checks: " + "; ".join(f"{k}: {'pass' if v else 'FAIL'}"
                               for k, v in checks.items())]  # fmt: skip
    return "\n".join(lines) + "\n", rep


def sensitivity(tg: pl.DataFrame, t: datetime, variant: str = "c1"
                ) -> dict[str, dict[str, list[float]]]:  # fmt: skip
    """Q (and with C1 v2, rho_U) at the last refit with every prior sd halved and doubled."""
    train = tg.filter(pl.col("cross") & (pl.col("kickoff") + LAG <= t))
    out: dict[str, dict[str, list[float]]] = {}
    for s_ in (0.5, 1.0, 2.0):
        post = fit_q(train, True, s_, rho_u=variant == "v2")[0]
        out[str(s_)] = {"Q": post["Q"].mean(axis=0).tolist(),
                        **({"rho_u": post["rho_u"].mean(axis=0).tolist()} if "rho_u" in post
                           else {})}  # fmt: skip
    return out


def venue_check(session: Any, tg: pl.DataFrame, five: dict[str, list[int]],
                holdout: HoldoutKey | None = None) -> dict[str, Any]:  # fmt: skip
    """D56 (b): with every bundle's grounds, (i) C1's cross-five dev matches keep their venue
    roles (neutral, swapped) from C1's five-only rule, and (ii) every C2 club with a UEFA home
    match in dev that is not on neutral ground has a known league ground."""
    from adatta.features import neutral

    sc = list(seasons(holdout)[1])
    dev = tg.filter(pl.col("season").is_in(sc))
    old = targets(session, five, holdout).filter(pl.col("cross") & pl.col("season").is_in(sc))
    both = old.select("match_id", n0="neutral", s0="swapped").join(
        dev.select("match_id", "neutral", "swapped"), on="match_id", how="inner"
    )
    changed = both.filter((pl.col("n0") != pl.col("neutral")) | (pl.col("s0") != pl.col("swapped")))
    c2b = set(BUNDLE_ORDER[5:])
    homes = dev.filter(pl.col("cross") & ~pl.col("neutral") & pl.col("bundle_home").is_in(c2b))
    clubs = sorted(set(homes["home"].to_list()))
    grounds = neutral.team_grounds(session, league_competition_ids(session, bundles()), clubs)
    unknown = [c for c in clubs if not grounds.get(c)]
    # D57: C2 home games the rule calls neutral (no home advantage), a known limit
    c2_neutral = dev.filter(pl.col("cross") & pl.col("neutral") & pl.col("bundle_home").is_in(c2b))
    return {
        "c1_matches": both.height,
        "roles_changed": changed.height,
        "c2_home_clubs": len(clubs),
        "c2_unknown_ground": len(unknown),
        "c2_home_neutral": c2_neutral.height,
        "ok": changed.is_empty() and not unknown,
    }


def venue_lines(v: dict[str, Any], label: str = "dev") -> str:
    return (
        f"Venues (D56 (b)): the five's {v['c1_matches']} {label} matches, roles changed "
        f"{v['roles_changed']}; C2 clubs with a {label} UEFA home match {v['c2_home_clubs']}, "
        f"without a known ground {v['c2_unknown_ground']}: "
        f"{'pass' if v['ok'] else 'FAIL'}. Known limit (D57): {v['c2_home_neutral']} {label} "
        "matches with a C2 club at home are neutral by the city rule (no home advantage)."
    )


def leakage(tg: pl.DataFrame, hist: pl.DataFrame, elo: dict[int, float],
            neutral: set[int], scored: Sequence[int] = DEV) -> dict[str, Any]:  # fmt: skip
    """(6) For sampled targets: the strengths come from the Monday refit (as_of = Monday <=
    kickoff) and the Elo recomputed from matches final by the Monday alone is the same."""
    rng = np.random.default_rng(SEED)
    dev = tg.filter(pl.col("cross") & pl.col("season").is_in(list(scored)))
    idx = rng.choice(dev.height, min(LEAKAGE_N, dev.height), replace=False)
    bad = 0
    for i in idx:
        r = dev.row(int(i), named=True)
        cut = hist.filter(pl.col("kickoff") + LAG <= r["monday"])
        again = elo_at(cut, [(r["match_id"], r["monday"], r["home"], r["away"])], neutral)
        bad += abs(again[r["match_id"]] - elo[r["match_id"]]) > 1e-9
        bad += not (r["monday"] <= r["kickoff"])
    return {"n": len(idx), "changed": int(bad), "ok": bad == 0}


def draw_lines(scored: pl.DataFrame) -> str:
    """Draw calibration by competition and format phase (reported, D62 (d))."""
    y = outcome(scored["hg"].to_numpy(), scored["ag"].to_numpy())
    pd_ = scored["c1_draw"].to_numpy()
    parts = []
    for col in ("comp", "phase"):
        if col not in scored.columns:
            continue
        for v in sorted({x for x in scored[col].to_list() if x is not None}):
            s_ = (scored[col] == v).to_numpy()
            parts.append(f"{v} n {int(s_.sum())} predicted {pd_[s_].mean():.3f} observed "
                         f"{(y[s_] == 1).mean():.3f}")  # fmt: skip
    return "Draws, C1 predicted against observed (reported): " + "; ".join(parts) + "\n"


def compare_lines(new: pl.DataFrame, old: pl.DataFrame) -> str:
    """D62 (c): C1 v2 against C1 on the dev matches both scored (paired by match)."""
    j = new.select("match_id", "hg", "ag", *[f"c1_{o}" for o in ("home", "draw", "away")]).join(
        old.select("match_id", *[pl.col(f"c1_{o}").alias(f"old_{o}")
                                 for o in ("home", "draw", "away")]),
        on="match_id", how="inner")  # fmt: skip
    y = outcome(j["hg"].to_numpy(), j["ag"].to_numpy())
    a = log_loss(j.select("c1_home", "c1_draw", "c1_away").to_numpy(), y)
    b = log_loss(j.select("old_home", "old_draw", "old_away").to_numpy(), y)
    d = paired(a, b)
    ok = d["mean"] <= 0
    return (
        f"Dev check (c), C1 v2 minus C1 on {d['n']} paired dev matches: log loss "
        f"{d['mean']:+.4f} [{d['lo']:+.4f}, {d['hi']:+.4f}] ({d['rel']:+.2%}): "
        f"{'pass' if ok else 'FAIL'} (point estimate <= 0).\n"
    )


def passed(checks: dict[str, bool], holdout: bool) -> bool:
    """Dev: all six checks. The holdout: the gate file's checks (1-4) alone, 5 and 6
    reported."""
    if not holdout:
        return all(checks.values())
    want = {str(n) for n in load_gate()["checks"]}
    gating = {k: v for k, v in checks.items() if k.split()[0] in want}
    if len(gating) != len(want):
        raise ValueError(f"checks {sorted(want)} not all reported: {sorted(checks)}")
    return all(gating.values())


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
    for sp in (s1, s2):
        sp.add_argument("--holdout", default=None, metavar="RUN_ID",
                        help="the run that made the UEFA opening (D58)")  # fmt: skip
        sp.add_argument("--variant", choices=("c1", "v2"), default="c1",
                        help="v2: C1 with the UEFA draw term, side refit redo (D62)")  # fmt: skip
    s2.add_argument("--compare", default=None, metavar="RUN_ID",
                    help="v2 dev check (c): C1's scored rows of that strengths run")  # fmt: skip
    s3 = sub.add_parser("claim", help="open the UEFA holdout once (marker uefa, D58)")
    s3.add_argument("--run", required=True, metavar="RUN_ID")
    s3.add_argument("--commit", required=True)
    args = ap.parse_args(argv)
    store = make_store(get_settings())
    cfg = harness.load_config()
    if args.cmd == "claim":
        from adatta.eval import holdout as ho

        load_gate()  # the pre-registered gate exists and matches this code before opening
        m = ho.claim(store, args.run, MODEL_NAME, args.commit, OPENING)
        print(f"UEFA holdout opened by run {m['run_id']} at {m['opened_at']}")
        return
    key = None
    if args.holdout:
        from adatta.eval import holdout as ho

        load_gate()
        key = ho.unlock(store, args.holdout, MODEL_NAME, OPENING)
    label = "holdout" if key else "dev"
    if args.cmd == "strengths":
        i, n = (int(x) for x in args.part.split("/"))
        with get_session() as s:
            rows = strengths(store, s, args.bundle, (i, n), holdout=key,
                             redo=args.variant == "v2")  # fmt: skip
        buf = io.BytesIO()
        rows.write_parquet(buf, compression="zstd")
        store.put(
            f"{PREFIX}/strengths/{args.upload}/{args.bundle}_{i}of{n}.parquet", buf.getvalue()
        )
        print(f"{args.bundle} {i}/{n}: {rows.height} side rows")
        return
    every = bundles()
    parts = []
    for b in every:
        for i in range(args.parts):
            raw = try_get(store, f"{PREFIX}/strengths/{args.fetch}/{b}_{i}of{args.parts}.parquet")
            if raw is None:
                raise SystemExit(f"missing strengths {b} {i}/{args.parts}")
            parts.append(pl.read_parquet(io.BytesIO(raw)))
    st = pl.concat([p for p in parts if p.height], how="diagonal_relaxed")
    with get_session() as s:
        tg0 = targets(s, every, key)
        hist = elo_history(s, every, key)
        venues = venue_check(s, tg0, cfg.bundles, key)
        s.rollback()
    print(venue_lines(venues, label))
    scored_seasons = seasons(key)[1]
    tg = join_sides(tg0, st)
    print(f"targets with both sides' strengths: {tg.height} of {tg0.height} (cross-bundle "
          f"{tg.filter(pl.col('cross')).height}); cold sides "
          f"{int(tg['cold_home'].cast(pl.Int64).sum()) + int(tg['cold_away'].cast(pl.Int64).sum())}"
          f"; sides from refits failing the gates "
          f"{int((~tg['refit_ok_home']).cast(pl.Int64).sum())
             + int((~tg['refit_ok_away']).cast(pl.Int64).sum())}")  # fmt: skip
    if args.variant == "v2":  # D62: a side still failing after its redo is left out
        bad = tg.filter(~pl.col("refit_ok_home") | ~pl.col("refit_ok_away"))
        unclassed = tg.filter(pl.col("phase").is_null())
        print(f"C1 v2: {bad.height} targets left out (a side's refit fails after its redo); "
              f"{unclassed.height} with an unclassed round left out")  # fmt: skip
        tg = tg.filter(pl.col("refit_ok_home") & pl.col("refit_ok_away")
                       & pl.col("phase").is_not_null())  # fmt: skip
    neutral = set(tg.filter(pl.col("neutral"))["match_id"].to_list())
    elo = elo_at(hist, [(r["match_id"], r["monday"], r["home"], r["away"])
                        for r in tg.iter_rows(named=True)], neutral)  # fmt: skip
    scored, diags = run_fit(tg, elo, scored_seasons, args.variant)
    text, rep = gate(scored, diags, label)
    text += draw_lines(scored)
    sens = sensitivity(tg, datetime.fromisoformat(diags[-1]["as_of"]), args.variant)
    leak = leakage(tg, hist, elo, neutral, scored_seasons)
    rep["checks"]["6 leakage"] = leak["ok"]
    rep["passed"] = passed(rep["checks"], key is not None)
    text += (f"6 leakage: {leak['changed']} of {leak['n']} sampled targets changed: "
             f"{'pass' if leak['ok'] else 'FAIL'}.\n"
             "Prior sensitivity, Q at the last refit (prior sd x0.5 / x1 / x2): "
             + "; ".join(f"{b} " + " / ".join(f"{sens[k]['Q'][j]:+.3f}"
                                               for k in ("0.5", "1.0", "2.0"))
                         for j, b in enumerate(BUNDLE_ORDER)) + "\n"
             + ("rho_U at the last refit (prior sd x0.5 / x1 / x2): " + "; ".join(
                 f"{ph} " + " / ".join(f"{sens[k]['rho_u'][j]:+.3f}" for k in ("0.5", "1.0", "2.0"))
                 for j, ph in enumerate(PHASES)) + "\n" if args.variant == "v2" else "")
             + ("Gating on the holdout: checks 1-4 (config/uefa_gate.yml); 5 and 6 reported.\n"
                if key else "")
             + f"**C1{' v2' if args.variant == 'v2' else ''} "
               f"{'PASSES' if rep['passed'] else 'FAILS'} on {label}.**\n")  # fmt: skip
    if args.compare:  # D62 (c): C1 v2 not worse than C1 on dev log loss (point estimate)
        old = pl.read_parquet(
            io.BytesIO(store.get(f"{PREFIX}/reports/{args.compare}/scored.parquet"))
        )
        text += compare_lines(scored, old)
    out = f"{PREFIX}/{'holdout' if key else 'reports'}/{args.fetch}" + (
        "/v2" if args.variant == "v2" else ""
    )
    buf = io.BytesIO()
    scored.write_parquet(buf, compression="zstd")
    store.put(f"{out}/scored.parquet", buf.getvalue())
    store.put(f"{out}/verdict.json",
              json.dumps({"passed": rep["passed"], "checks": rep["checks"], "diags": diags},
                         default=str).encode())  # fmt: skip
    print(text)


if __name__ == "__main__":
    main()
