"""Stage D, v0: senior men's national teams, team only, from results
(docs/BEYOND_LEAGUES_PLAN.md, Stage D, "D v0 acceptance"; spec §10.4).

log lambda_home = c + g[type] + H(role) + (s_home - s_away)/2 + (o_home + o_away)/2
s[i, month] = conf[i] + u[i] + w[i, month] (UEFA conf = 0; u sums to 0 within each
confederation; w a Gaussian random walk per calendar month), o static and shrunk; negative
binomial goals with the Dixon-Coles tau, renormalised; friendlies at likelihood weight 0.5.
Against B_elo (the eloratings.net method, settings fixed in the acceptance).

CLI (CI, model group; one backtest-type run at a time):
  time   --at YYYY-MM-DD        one refit, timed (cached for the backtest)
  refits --part i/n --upload RUN
                                the walk-forward refits times[i::n], each scoring the dev
                                targets of its week -> national/parts/RUN/<i>of<n>.*
  gate   --fetch RUN --parts n  the Elo, the gate, prior sensitivity and leakage; metrics
                                only to stdout, scored rows to R2 (national/reports/RUN/)
Holdout matches (from HOLDOUT_FROM) are never loaded here. Logs carry metrics, never data.
"""

from __future__ import annotations

import argparse
import io
import json
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray

from adatta.adapters.storage import RawStore, make_store, try_get
from adatta.config import get_settings
from adatta.model import continental as ct

Arr = NDArray[np.float64]
PREFIX = "national"
VERSION = "v0"
WARM_FROM = datetime(2014, 6, 1, tzinfo=UTC)
DEV_FROM = datetime(2019, 1, 1, tzinfo=UTC)
HOLDOUT_FROM = datetime(2024, 1, 1, tzinfo=UTC)  # locked: 2024-01-01 to 2026-07-31
LIVE_FROM = datetime(2026, 8, 1, tzinfo=UTC)
WINDOW = timedelta(days=3652)  # fit window cap: the last 10 years
LAG = timedelta(hours=3)
SEED = 20261121
DRAWS, CHAINS = 1000, 4
KEEP = 400  # posterior draws kept per refit for prediction
MAX_GOALS = 20
FRIENDLY_WEIGHT = 0.5
MIN_SEGMENT = 100  # segments with fewer competitive matches are reported, not checked
LEAKAGE_N = 50
FINISHED = ("FT", "AET", "PEN")

# Competitions by provider league id (config/national_competitions.yml)
FRIENDLY = "10"
# World Cup, Euro, Copa America, AFCON, Asian Cup, Gold Cup, Finalissima
FINALS = {"1", "4", "9", "6", "7", "22", "913"}
WORLD_CUP = "1"
# The confederation whose competition it is (the gate's segments); "world" for the World Cup,
# the intercontinental play-offs and the Finalissima
COMP_CONF = {
    "4": "UEFA", "960": "UEFA", "5": "UEFA", "32": "UEFA",
    "34": "CONMEBOL", "9": "CONMEBOL",
    "29": "CAF", "36": "CAF", "6": "CAF",
    "30": "AFC", "35": "AFC", "7": "AFC",
    "31": "CONCACAF", "22": "CONCACAF", "858": "CONCACAF", "536": "CONCACAF", "808": "CONCACAF",
    "33": "OFC",
    "1": "world", "37": "world", "913": "world",
}  # fmt: skip
# Competitions only a confederation's members play (Copa America and the Gold Cup have had
# guests): these assign a team its confederation
MEMBER_COMPS = {k for k, v in COMP_CONF.items() if v != "world"} - {"9", "22"}
CONFS = ("UEFA", "CONMEBOL", "CONCACAF", "CAF", "AFC", "OFC")  # UEFA is conf = 0
TYPES = ("friendly", "qualifier", "finals")  # friendly is g = 0

# B_elo (eloratings.net), fixed in the acceptance
ELO_START, ELO_HOME = 1500.0, 100.0

TARGET_SCHEMA: dict[str, Any] = {
    "match_id": pl.Int64, "league_id": pl.Utf8, "type": pl.Utf8, "comp_conf": pl.Utf8,
    "kickoff": pl.Datetime("us", "UTC"), "monday": pl.Datetime("us", "UTC"),
    "home": pl.Int64, "away": pl.Int64, "conf_home": pl.Utf8, "conf_away": pl.Utf8,
    "role": pl.Utf8, "hg": pl.Int64, "ag": pl.Int64,
}  # fmt: skip


# ----------------------------------------------------------------------------- rules


def comp_type(league_id: str) -> str:
    return "friendly" if league_id == FRIENDLY else "finals" if league_id in FINALS else "qualifier"


def elo_k(league_id: str) -> float:
    """60 World Cup finals, 50 other finals, 40 qualifiers and Nations Leagues, 20 friendlies."""
    if league_id == WORLD_CUP:
        return 60.0
    return {"finals": 50.0, "qualifier": 40.0, "friendly": 20.0}[comp_type(league_id)]


def goal_mult(margin: int) -> float:
    n = abs(margin)
    return 1.0 if n <= 1 else 1.5 if n == 2 else (11 + n) / 8


def host_sides(role: str) -> tuple[float, float, float]:
    """(host home, host away, unknown) indicators: h to the listed home side, h to the listed
    away side (swapped), h_u to the listed home side (role unknown)."""
    return (float(role == "home"), float(role == "swapped"), float(role == "unknown"))


def assign_confederations(pairs: Iterable[tuple[int, str]]) -> dict[int, str]:
    """team -> confederation from the member-only competitions it plays (team, league id);
    a team in two confederations stops the run (never guessed)."""
    seen: dict[int, set[str]] = defaultdict(set)
    for team, lg in pairs:
        if lg in MEMBER_COMPS:
            seen[team].add(COMP_CONF[lg])
    multi = {t: sorted(c) for t, c in seen.items() if len(c) > 1}
    if multi:
        raise ValueError(f"teams in more than one confederation: {multi}")
    return {t: next(iter(c)) for t, c in seen.items()}


def month_index(t: datetime) -> int:
    return (t.year - WARM_FROM.year) * 12 + t.month - 1


def refit_times(tg: pl.DataFrame, start: datetime, end: datetime) -> list[datetime]:
    """The Monday of every week with a target in [start, end)."""
    s = tg.filter((pl.col("kickoff") >= start) & (pl.col("kickoff") < end))
    return sorted(set(s["monday"].to_list()))


# --------------------------------------------------------------------------- targets


def targets(session: Any, store: RawStore, upto: datetime = HOLDOUT_FROM) -> pl.DataFrame:
    """Finished matches between two senior men's national sides that each have a
    confederation, kickoff in [WARM_FROM, upto), with the 90' score and the host role."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.features import host, team_levels

    if upto > HOLDOUT_FROM:
        raise ValueError("the international holdout is opened only by its own marker")
    M, S, C, R = models.Match, models.Season, models.Competition, models.MatchResult
    every = session.execute(
        select(M.home_team_id, M.away_team_id, C.source_ref)
        .join(S, S.season_id == M.season_id).join(C, C.competition_id == S.competition_id)
        .where(C.is_national)
    ).all()  # fmt: skip
    senior = team_levels.senior_national_ids(session)
    sides = assign_confederations(
        [(int(t), str(lg)) for h, a, lg in every for t in (h, a) if t in senior]
    )
    lg_of = {}
    for m_id, lg in session.execute(
        select(M.match_id, C.source_ref).join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id).where(C.is_national)
    ):  # fmt: skip
        lg_of[int(m_id)] = str(lg)
    goals = {
        int(m): (h, a)
        for m, h, a in session.execute(
            select(R.match_id, R.home_goals_nt, R.away_goals_nt).where(R.superseded_at.is_(None))
        )
    }
    rows = [
        r for r in host.national_matches(session)
        if r[0].status in FINISHED and r[0].home_team_id in sides and r[0].away_team_id in sides
        and WARM_FROM <= ct._utc(r[0].kickoff_time) < upto
        and goals.get(r[0].match_id, (None, None))[0] is not None
        and goals[r[0].match_id][1] is not None
    ]  # fmt: skip
    countries = host.Countries(host.stored_country_names(session, store), host.load_aliases())
    roles = host.host_roles(session, rows, countries)
    role = dict(zip(roles["match_id"].to_list(), roles["role"].to_list(), strict=True))
    out = []
    for m, *_ in rows:
        lg = lg_of[m.match_id]
        hg, ag = goals[m.match_id]
        out.append({
            "match_id": m.match_id, "league_id": lg, "type": comp_type(lg),
            "comp_conf": COMP_CONF.get(lg, "world"), "kickoff": ct._utc(m.kickoff_time),
            "monday": ct.monday(m.kickoff_time), "home": m.home_team_id,
            "away": m.away_team_id, "conf_home": sides[m.home_team_id],
            "conf_away": sides[m.away_team_id], "role": role.get(m.match_id, "unknown"),
            "hg": int(hg), "ag": int(ag),
        })  # fmt: skip
    tg = pl.DataFrame(out, schema=TARGET_SCHEMA).sort("kickoff", "match_id")
    assert tg.is_empty() or tg["kickoff"].max() < upto  # type: ignore[operator]
    return tg


# ------------------------------------------------------------------------------- Elo


def elo_at(hist: pl.DataFrame, queries: list[tuple[int, datetime]]) -> dict[int, float]:
    """For each query (match_id, Monday), the listed home side's Elo minus the away side's,
    with +100 to the host (listed home when the role is unknown), from the matches final by
    that Monday. `hist` is the targets frame (all of them, sorted by kickoff)."""
    rating: dict[int, float] = defaultdict(lambda: ELO_START)
    by_id = {r["match_id"]: r for r in hist.iter_rows(named=True)}
    out: dict[int, float] = {}
    rows = hist.iter_rows(named=True)
    pending = next(rows, None)

    def adv(role: str) -> tuple[float, float]:
        return (0.0, ELO_HOME) if role == "swapped" else (0.0, 0.0) if role == "neutral" \
            else (ELO_HOME, 0.0)  # fmt: skip

    for mid, mon in sorted(queries, key=lambda q: q[1]):
        while pending is not None and pending["kickoff"] + LAG <= mon:
            r = pending
            ah, aa = adv(r["role"])
            dr = rating[r["home"]] + ah - rating[r["away"]] - aa
            we = 1 / (10 ** (-dr / 400) + 1)
            w = 1.0 if r["hg"] > r["ag"] else 0.5 if r["hg"] == r["ag"] else 0.0
            d = elo_k(r["league_id"]) * goal_mult(r["hg"] - r["ag"]) * (w - we)
            rating[r["home"]] += d
            rating[r["away"]] -= d
            pending = next(rows, None)
        q = by_id[mid]
        ah, aa = adv(q["role"])
        out[mid] = rating[q["home"]] + ah - rating[q["away"]] - aa
    return out


# ------------------------------------------------------------------------------ model


@dataclass
class Design:
    """Index arrays for a set of matches against a team list and a month origin."""

    teams: NDArray[np.int64]  # team ids, model order
    team_conf: NDArray[np.int64]  # index into CONFS
    hi: NDArray[np.int64]
    ai: NDArray[np.int64]
    month: NDArray[np.int64]  # month index relative to the window's first month
    n_months: int  # walk length (months 0..n_months-1), the refit's month last
    typ: NDArray[np.int64]
    hh: Arr
    ha: Arr
    hu: Arr
    weight: Arr
    hg: NDArray[np.int64]
    ag: NDArray[np.int64]


def design(train: pl.DataFrame, t: datetime, friendly_weight: float) -> Design:
    teams = np.array(sorted(set(train["home"].to_list()) | set(train["away"].to_list())),
                     dtype=np.int64)  # fmt: skip
    pos = {int(x): i for i, x in enumerate(teams)}
    conf_of = {**dict(zip(train["home"], train["conf_home"], strict=True)),
               **dict(zip(train["away"], train["conf_away"], strict=True))}  # fmt: skip
    m0 = month_index(train["kickoff"].min())  # type: ignore[arg-type]
    roles = [host_sides(r) for r in train["role"]]
    return Design(
        teams=teams,
        team_conf=np.array([CONFS.index(conf_of[int(x)]) for x in teams], dtype=np.int64),
        hi=np.array([pos[x] for x in train["home"]], dtype=np.int64),
        ai=np.array([pos[x] for x in train["away"]], dtype=np.int64),
        month=np.array([month_index(k) - m0 for k in train["kickoff"]], dtype=np.int64),
        n_months=month_index(t) - m0 + 1,
        typ=np.array([TYPES.index(x) for x in train["type"]], dtype=np.int64),
        hh=np.array([r[0] for r in roles]), ha=np.array([r[1] for r in roles]),
        hu=np.array([r[2] for r in roles]),
        weight=np.where(train["type"].to_numpy() == "friendly", friendly_weight, 1.0),
        hg=train["hg"].to_numpy().astype(np.int64), ag=train["ag"].to_numpy().astype(np.int64),
    )  # fmt: skip


def nb_pmf(k: NDArray[np.int64], mu: Arr, phi: Arr) -> Arr:
    """Negative binomial (mean mu, dispersion phi: var = mu + mu^2 / phi) at k, broadcast."""
    from scipy.stats import nbinom

    return np.asarray(nbinom.pmf(k, phi, phi / (phi + mu)), dtype=np.float64)


def score_matrix(lam: Arr, mu: Arr, phi: Arr, rho: Arr, max_goals: int = MAX_GOALS) -> Arr:
    """(..., K, K) negative binomial x negative binomial with the Dixon-Coles tau on 0-0,
    0-1, 1-0, 1-1, renormalised."""
    lam, mu, phi, rho = (np.asarray(x, dtype=np.float64)
                         for x in np.broadcast_arrays(lam, mu, phi, rho))  # fmt: skip
    k = np.arange(max_goals + 1)
    ph, pa = nb_pmf(k, lam[..., None], phi[..., None]), nb_pmf(k, mu[..., None], phi[..., None])
    m = ph[..., :, None] * pa[..., None, :]
    m[..., 0, 0] *= 1 - lam * mu * rho
    m[..., 0, 1] *= 1 + lam * rho
    m[..., 1, 0] *= 1 + mu * rho
    m[..., 1, 1] *= 1 - rho
    m = np.maximum(m, 0.0)
    return np.asarray(m / m.sum(axis=(-2, -1), keepdims=True))


def _model(d: Design, prior_scale: float) -> Any:
    import pymc as pm
    import pytensor.tensor as pt

    n, nm, ps = len(d.teams), d.n_months, prior_scale
    onehot = np.eye(len(CONFS))[d.team_conf]
    counts = np.maximum(onehot.sum(axis=0), 1.0)
    with pm.Model() as model:
        c = pm.Normal("c", 0.25, 0.5 * ps)
        g_free = pm.Normal("g", 0.0, 0.3 * ps, shape=2)
        g = pt.concatenate([pt.zeros(1), g_free])
        h = pm.Normal("h", 0.25, 0.25 * ps)
        h_u = pm.Normal("h_u", 0.25, 0.25 * ps)
        conf_free = pm.Normal("conf", 0.0, 1.0 * ps, shape=len(CONFS) - 1)
        conf = pt.concatenate([pt.zeros(1), conf_free])
        sigma_u = pm.HalfNormal("sigma_u", 1.0 * ps)
        u_raw = pm.Normal("u_raw", 0.0, 1.0, shape=n)
        u = sigma_u * (u_raw - pt.dot(onehot, pt.dot(onehot.T, u_raw) / counts))
        sigma_w = pm.HalfNormal("sigma_w", 0.05 * ps)
        if nm > 1:
            z = pm.Normal("z", 0.0, 1.0, shape=(n, nm - 1))
            w = sigma_w * pt.concatenate([pt.zeros((n, 1)), pt.cumsum(z, axis=1)], axis=1)
        else:
            w = pt.zeros((n, 1))
        sigma_o = pm.HalfNormal("sigma_o", 0.1 * ps)
        o_raw = pm.Normal("o_raw", 0.0, 1.0, shape=n)
        o = sigma_o * o_raw
        phi = pm.Gamma("phi", 2.0, 0.1)
        rho = pm.Uniform("rho", -0.2, 0.2)
        s = conf[d.team_conf][:, None] + u[:, None] + w
        pm.Deterministic("s_last", s[:, -1])
        pm.Deterministic("o_last", o)
        sh, sa = s[d.hi, d.month], s[d.ai, d.month]
        op = (o[d.hi] + o[d.ai]) / 2
        base = c + g[d.typ] + op
        lam = pt.exp(base + h * d.hh + h_u * d.hu + (sh - sa) / 2)
        mu = pt.exp(base + h * d.ha + (sa - sh) / 2)
        lp_h = pm.logp(pm.NegativeBinomial.dist(mu=lam, alpha=phi), d.hg)
        lp_a = pm.logp(pm.NegativeBinomial.dist(mu=mu, alpha=phi), d.ag)
        p0h = (phi / (phi + lam)) ** phi
        p0a = (phi / (phi + mu)) ** phi
        p1h, p1a = p0h * phi * lam / (phi + lam), p0a * phi * mu / (phi + mu)
        tau = pt.switch(pt.eq(d.hg, 0) & pt.eq(d.ag, 0), 1 - lam * mu * rho,
              pt.switch(pt.eq(d.hg, 0) & pt.eq(d.ag, 1), 1 + lam * rho,
              pt.switch(pt.eq(d.hg, 1) & pt.eq(d.ag, 0), 1 + mu * rho,
              pt.switch(pt.eq(d.hg, 1) & pt.eq(d.ag, 1), 1 - rho, 1.0))))  # fmt: skip
        zn = 1 + rho * (-lam * mu * p0h * p0a + lam * p0h * p1a + mu * p1h * p0a - p1h * p1a)
        ll = lp_h + lp_a + pt.log(pt.maximum(tau, 1e-9)) - pt.log(zn)
        pm.Potential("ll", (d.weight * ll).sum())
    return model


Ints = NDArray[np.int64]


def nb_dc_sample(lam: Arr, mu: Arr, phi: Arr, rho: Arr,
                 rng: np.random.Generator) -> tuple[Ints, Ints]:  # fmt: skip
    """Scores from the fitted model by rejection (negative binomial marginals, tau)."""
    phi = np.broadcast_to(np.asarray(phi, float)[:, None], lam.shape)
    r = np.broadcast_to(np.asarray(rho, float)[:, None], lam.shape)

    def draw(m: Arr) -> NDArray[np.int64]:
        return rng.negative_binomial(phi, phi / (phi + m)).astype(np.int64)

    def tau(h: NDArray[np.int64], a: NDArray[np.int64]) -> Arr:
        t = np.ones_like(lam)
        t = np.where((h == 0) & (a == 0), 1 - lam * mu * r, t)
        t = np.where((h == 0) & (a == 1), 1 + lam * r, t)
        t = np.where((h == 1) & (a == 0), 1 + mu * r, t)
        return np.where((h == 1) & (a == 1), 1 - r, t)

    tmax = np.maximum.reduce([np.ones_like(lam), 1 - lam * mu * r, 1 + lam * r, 1 + mu * r,
                              1 - r])  # fmt: skip
    h, a = draw(lam), draw(mu)
    todo = rng.random(lam.shape) * tmax >= np.maximum(tau(h, a), 0.0)
    while todo.any():
        h, a = np.where(todo, draw(lam), h), np.where(todo, draw(mu), a)
        todo &= rng.random(lam.shape) * tmax >= np.maximum(tau(h, a), 0.0)
    return h, a


HEADLINE = ("c", "g", "h", "h_u", "conf", "sigma_w", "sigma_u", "sigma_o", "phi", "rho")


Post = tuple[dict[str, Arr], dict[str, Any]]


def fit(train: pl.DataFrame, t: datetime, friendly_weight: float = FRIENDLY_WEIGHT,
        prior_scale: float = 1.0, seed: int = SEED) -> Post:  # fmt: skip
    """One refit on `train` (every match final by t, within the window). Returns KEEP draws
    of the headline parameters, s at the refit's month and o per team (with `teams`,
    `team_conf`), and the §14.5 diagnostics."""
    import pymc as pm

    from adatta.model import diagnostics as dg

    d = design(train, t, friendly_weight)
    t0 = time.time()
    with _model(d, prior_scale):
        idata = pm.sample(draws=DRAWS, tune=DRAWS, chains=CHAINS, nuts_sampler="numpyro",
                          random_seed=seed, target_accept=0.9, progressbar=False)  # fmt: skip
    secs = time.time() - t0
    checks = dg.convergence(idata)
    post = idata["posterior"]

    def flat(v: str) -> Arr:
        x = np.asarray(post[v].values)
        return x.reshape(-1, *x.shape[2:])

    n_all = CHAINS * DRAWS
    idx = np.linspace(0, n_all - 1, KEEP).astype(np.int64)
    out: dict[str, Arr] = {k: flat(k)[idx] for k in HEADLINE}
    out["s_last"], out["o_last"] = flat("s_last")[idx], flat("o_last")[idx]
    out["teams"], out["team_conf"] = d.teams.astype(np.float64), d.team_conf.astype(np.float64)
    # PPC on 200 draws: rates at each training match's month
    rng = np.random.default_rng(seed)
    pidx = idx[:: max(1, KEEP // 200)]
    conf = np.concatenate([np.zeros((len(pidx), 1)), flat("conf")[pidx]], axis=1)
    onehot = np.eye(len(CONFS))[d.team_conf]
    ur = flat("u_raw")[pidx]
    u = flat("sigma_u")[pidx][:, None] * (ur - (ur @ onehot / onehot.sum(0)) @ onehot.T)
    if d.n_months > 1:
        zz = flat("z")[pidx]
        w = flat("sigma_w")[pidx][:, None, None] * np.concatenate(
            [np.zeros((*zz.shape[:2], 1)), np.cumsum(zz, axis=2)], axis=2
        )
    else:
        w = np.zeros((len(pidx), len(d.teams), 1))
    s = conf[:, d.team_conf][:, :, None] + u[:, :, None] + w
    o = flat("sigma_o")[pidx][:, None] * flat("o_raw")[pidx]
    gg = np.concatenate([np.zeros((len(pidx), 1)), flat("g")[pidx]], axis=1)
    sh, sa = s[:, d.hi, d.month], s[:, d.ai, d.month]
    base = flat("c")[pidx][:, None] + gg[:, d.typ] + (o[:, d.hi] + o[:, d.ai]) / 2
    lam = np.exp(base + flat("h")[pidx][:, None] * d.hh + flat("h_u")[pidx][:, None] * d.hu
                 + (sh - sa) / 2)  # fmt: skip
    mu = np.exp(base + flat("h")[pidx][:, None] * d.ha + (sa - sh) / 2)
    sim_h, sim_a = nb_dc_sample(lam, mu, flat("phi")[pidx], flat("rho")[pidx], rng)
    checks += dg.ppc(d.hg, d.ag, sim_h, sim_a, "national")
    big = (np.abs(sim_h - sim_a) >= 4).mean(axis=1)
    obs_big = float((np.abs(d.hg - d.ag) >= 4).mean())
    q = float((big < obs_big).mean() + 0.5 * (big == obs_big).mean())
    checks.append(dg.Check("ppc national margin_4plus", dg.PPC_BAND[0] <= q <= dg.PPC_BAND[1], q,
                           f"observed {obs_big:.4f}, PPC median {np.median(big):.4f}"))  # fmt: skip
    cf = flat("conf")
    for j, name in enumerate(CONFS[1:]):
        sd = float(cf[:, j].std())
        checks.append(dg.Check(f"identified conf {name}", sd < 1.0 * prior_scale, sd,
                               "posterior sd against the prior sd"))  # fmt: skip
    diag: dict[str, Any] = {c_.name: c_.value for c_ in checks}
    diag["failed"] = [c_.name for c_ in checks if c_.blocking and not c_.passed]
    diag["warnings"] = [c_.name for c_ in checks if not c_.blocking and not c_.passed]
    diag["gates_ok"] = not diag["failed"]
    diag.update(n_train=len(d.hg), n_teams=len(d.teams), n_months=d.n_months, seconds=secs,
                train_max_kickoff=str(train["kickoff"].max()))  # fmt: skip
    return out, diag


def _side(post: dict[str, Arr], team: int, conf: str, extra: int, months: int,
          rng: np.random.Generator) -> tuple[Arr, Arr]:  # fmt: skip
    """A side's strength and openness draws at the match month: its fitted s at the refit's
    month carried `extra` months on by its own walk; an unseen side starts at its
    confederation's level with the team and walk spread."""
    k = len(post["c"])
    teams = post["teams"].astype(np.int64)
    sw = post["sigma_w"]
    hit = np.flatnonzero(teams == team)
    if hit.size:
        i = int(hit[0])
        s = post["s_last"][:, i] + sw * np.sqrt(extra) * rng.standard_normal(k)
        return s, post["o_last"][:, i]
    cj = CONFS.index(conf)
    lvl = np.zeros(k) if cj == 0 else post["conf"][:, cj - 1]
    s = (lvl + post["sigma_u"] * rng.standard_normal(k)
         + sw * np.sqrt(months + extra) * rng.standard_normal(k))  # fmt: skip
    return s, post["sigma_o"] * rng.standard_normal(k)


def predict(post: dict[str, Arr], tg: pl.DataFrame, t: datetime,
            months: int) -> tuple[Arr, Arr]:  # fmt: skip
    """(n, 3) 1X2 and (n, 2) expected goals per target, from the refit at t (walk length
    `months`), each draw's walk carried to the match's month."""
    rng = np.random.default_rng(SEED)
    g = np.concatenate([np.zeros((len(post["c"]), 1)), post["g"]], axis=1)
    p_out, xg = [], []
    for r in tg.iter_rows(named=True):
        extra = max(0, month_index(r["kickoff"]) - month_index(t))
        sh, oh = _side(post, r["home"], r["conf_home"], extra, months, rng)
        sa, oa = _side(post, r["away"], r["conf_away"], extra, months, rng)
        hh, ha, hu = host_sides(r["role"])
        base = post["c"] + g[:, TYPES.index(r["type"])] + (oh + oa) / 2
        lam = np.exp(base + post["h"] * hh + post["h_u"] * hu + (sh - sa) / 2)
        mu = np.exp(base + post["h"] * ha + (sa - sh) / 2)
        m = score_matrix(lam, mu, post["phi"], post["rho"])
        ph, pd = np.tril(m, -1).sum(axis=(-2, -1)), np.trace(m, axis1=-2, axis2=-1)
        p_out.append([ph.mean(), pd.mean(), 1 - ph.mean() - pd.mean()])
        k = np.arange(MAX_GOALS + 1)
        xg.append([float((m.sum(axis=-1) * k).sum(-1).mean()),
                   float((m.sum(axis=-2) * k).sum(-1).mean())])  # fmt: skip
    return np.asarray(p_out), np.asarray(xg)


# ---------------------------------------------------------------------------- refits


def training(tg: pl.DataFrame, t: datetime) -> pl.DataFrame:
    tr = tg.filter((pl.col("kickoff") + LAG <= t) & (pl.col("kickoff") >= t - WINDOW))
    assert (tr["kickoff"] + LAG <= t).all()
    return tr


def _cache_key(t: datetime, friendly_weight: float, prior_scale: float) -> str:
    return (f"{PREFIX}/refits/{VERSION}/w{friendly_weight:g}_s{prior_scale:g}/"
            f"{t:%Y%m%d}.npz")  # fmt: skip


def cached_fit(store: RawStore, tg: pl.DataFrame, t: datetime, friendly_weight: float,
               prior_scale: float = 1.0) -> tuple[dict[str, Arr], dict[str, Any]]:  # fmt: skip
    key = _cache_key(t, friendly_weight, prior_scale)
    raw, rawd = try_get(store, key), try_get(store, key + ".json")
    if raw is not None and rawd is not None:
        z = np.load(io.BytesIO(raw))
        return {k: np.asarray(z[k]) for k in z.files}, json.loads(rawd)
    post, diag = fit(training(tg, t), t, friendly_weight, prior_scale)
    buf = io.BytesIO()
    np.savez_compressed(buf, **post)  # type: ignore[arg-type]
    store.put(key, buf.getvalue())
    store.put(key + ".json", json.dumps(diag, default=str).encode())
    return post, diag


def run_part(store: RawStore, tg: pl.DataFrame, part: tuple[int, int],
             friendly_weight: float) -> tuple[pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    times = refit_times(tg, DEV_FROM, HOLDOUT_FROM)
    mine = times[part[0] :: part[1]]
    rows, diags = [], []
    for t in mine:
        tgt = tg.filter((pl.col("monday") == t) & (pl.col("kickoff") >= DEV_FROM))
        post, diag = cached_fit(store, tg, t, friendly_weight)
        p, xg = predict(post, tgt, t, int(diag["n_months"]))
        rows.append(tgt.with_columns(
            refit=pl.lit(t), v0_home=pl.Series(p[:, 0]), v0_draw=pl.Series(p[:, 1]),
            v0_away=pl.Series(p[:, 2]), xg_home=pl.Series(xg[:, 0]),
            xg_away=pl.Series(xg[:, 1])))  # fmt: skip
        diags.append({"as_of": t.isoformat(), "targets": tgt.height, **diag,
                      **{f"mean_{k}": np.asarray(post[k]).mean(axis=0).tolist()
                         for k in HEADLINE}})  # fmt: skip
        print(f"refit {t:%Y-%m-%d}: {diag['n_train']} train, {diag['n_teams']} teams, "
              f"{diag['n_months']} months, {tgt.height} targets; {diag['seconds']:.0f} s; rhat "
              f"{diag['rhat']:.3f} div {diag['divergences']:.0f}; failed "
              f"{','.join(diag['failed']) or 'none'}", flush=True)  # fmt: skip
    return (pl.concat(rows) if rows else pl.DataFrame()), diags


# ------------------------------------------------------------------------------ gate


def gate(
    scored: pl.DataFrame, elo_p: Arr, diags: list[dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    y = ct.outcome(scored["hg"].to_numpy(), scored["ag"].to_numpy())
    pv = scored.select("v0_home", "v0_draw", "v0_away").to_numpy()
    ll = {"v0": ct.log_loss(pv, y), "elo": ct.log_loss(elo_p, y)}
    rp = {"v0": ct.rps(pv, y), "elo": ct.rps(elo_p, y)}
    comp = (scored["type"] != "friendly").to_numpy()
    fr = ~comp

    def cmp(mask: NDArray[np.bool_]) -> dict[str, dict[str, float]]:
        return {"log_loss": ct.paired(ll["v0"][mask], ll["elo"][mask]),
                "rps": ct.paired(rp["v0"][mask], rp["elo"][mask])}  # fmt: skip

    main_, friend = cmp(comp), cmp(fr)
    segs: dict[str, dict[str, Any]] = {}
    for col in ("comp_conf", "type"):
        for v in sorted(set(scored.filter(pl.col("type") != "friendly")[col].to_list())):
            s = comp & (scored[col] == v).to_numpy()
            n = int(s.sum())
            rel = float((ll["v0"][s].mean() - ll["elo"][s].mean()) / ll["elo"][s].mean())
            segs[f"{col}={v}"] = {"n": n, "rel": rel, "checked": n >= MIN_SEGMENT}
    roles: dict[str, dict[str, float]] = {}
    for v in ("home", "swapped", "neutral", "unknown"):
        s = comp & (scored["role"] == v).to_numpy()
        if s.any():
            roles[v] = {
                "n": int(s.sum()),
                "rel": float((ll["v0"][s].mean() - ll["elo"][s].mean()) / ll["elo"][s].mean()),
            }
    qual = (scored["type"] == "qualifier").to_numpy()
    cal = ct.calibration(pv[qual], y[qual])
    checks = {
        "1 v0 beats B_elo on competitive matches (log loss and RPS, intervals below 0)":
            main_["log_loss"]["hi"] < 0 and main_["rps"]["hi"] < 0,
        f"3 no checked segment (n >= {MIN_SEGMENT}) worse than B_elo by > 1.0%":
            all(v["rel"] <= 0.01 for v in segs.values() if v["checked"]),
        "4 1X2 ECE within its noise floor on qualifiers and Nations Leagues":
            all(cal[o]["pass"] for o in ("home", "draw", "away")),
        "5 inference gates on every refit": all(d["gates_ok"] for d in diags),
    }  # fmt: skip
    rep: dict[str, Any] = {"n": len(y), "n_comp": int(comp.sum()), "main": main_,
                           "friendlies": friend, "segments": segs, "roles": roles,
                           "calibration": cal, "checks": checks}  # fmt: skip

    def line(name: str, c: dict[str, dict[str, float]]) -> str:
        lo_, rp_ = c["log_loss"], c["rps"]
        return (f"{name} (n {lo_['n']}): log loss {lo_['mean']:+.4f} [{lo_['lo']:+.4f}, "
                f"{lo_['hi']:+.4f}] ({lo_['rel']:+.2%}); RPS {rp_['mean']:+.4f} [{rp_['lo']:+.4f}, "
                f"{rp_['hi']:+.4f}] ({rp_['rel']:+.2%})")  # fmt: skip

    last = diags[-1]
    failed = Counter(f for d in diags for f in d["failed"])
    warned = Counter(f for d in diags for f in d.get("warnings", []))
    lines = [
        "# Stage D v0 on dev: senior men's national teams", "",
        f"{len(y)} dev matches ({int(comp.sum())} competitive, {int(fr.sum())} friendlies), "
        f"{len(diags)} refits.", "| model | segment | 1X2 log loss | RPS |", "|---|---|---|---|",
        *[f"| {m} | {nm} | {ll[m][s].mean():.4f} | {rp[m][s].mean():.4f} |"
          for nm, s in (("competitive", comp), ("friendlies", fr)) for m in ("v0", "elo")], "",
        line("v0 vs Elo, competitive", main_), line("v0 vs Elo, friendlies (reported)", friend), "",
        "Segments (competitive, v0 log loss relative to Elo): " + "; ".join(
            f"{k} n {v['n']} {v['rel']:+.2%}{'' if v['checked'] else ' (reported)'}"
            for k, v in segs.items()),
        "Host role (competitive): " + "; ".join(
            f"{k} n {v['n']} {v['rel']:+.2%}" for k, v in roles.items()),
        "Calibration on qualifiers / Nations Leagues (ECE / limit): " + ", ".join(
            f"{o} {cal[o]['ece']:.3f}/{cal[o]['limit']:.3f}" for o in ("home", "draw", "away")),
        f"Refits failing a gate: {sum(not d['gates_ok'] for d in diags)} of {len(diags)}"
        + (f" ({', '.join(f'{k} {v}' for k, v in failed.most_common())})" if failed else "")
        + (f"; warnings {', '.join(f'{k} {v}' for k, v in warned.most_common())}"
           if warned else ""),
        f"Worst R-hat {max(d['rhat'] for d in diags):.3f}, divergences "
        f"{sum(d['divergences'] for d in diags):.0f}, slowest refit "
        f"{max(d['seconds'] for d in diags):.0f} s.",
        "At the last refit: conf " + ", ".join(
            f"{c} {v:+.3f}" for c, v in zip(CONFS[1:], last["mean_conf"], strict=True))
        + f"; h {last['mean_h']:+.3f}, h_u {last['mean_h_u']:+.3f}, g (qualifier, finals) "
        + ", ".join(f"{v:+.3f}" for v in last["mean_g"])
        + f"; sigma_w {last['mean_sigma_w']:.4f}, sigma_u {last['mean_sigma_u']:.3f}, sigma_o "
        f"{last['mean_sigma_o']:.3f}, phi {last['mean_phi']:.1f}, rho {last['mean_rho']:+.3f}",
        "", "Checks: " + "; ".join(f"{k}: {'pass' if v else 'FAIL'}" for k, v in checks.items()),
    ]  # fmt: skip
    return "\n".join(lines) + "\n", rep


def elo_probs(tg: pl.DataFrame, scored: pl.DataFrame, elo: dict[int, float]) -> Arr:
    """B_elo 1X2 per scored target: the ordered logit refitted at the target's refit time on
    the earlier target matches."""
    out = np.zeros((scored.height, 3))
    for t in sorted(set(scored["refit"].to_list())):
        tr = tg.filter(pl.col("kickoff") + LAG <= t)
        x = np.array([elo[m] for m in tr["match_id"]]) / 100
        b, c1, c2 = ct.ordered_logit(x, ct.outcome(tr["hg"].to_numpy(), tr["ag"].to_numpy()))
        idx = np.flatnonzero((scored["refit"] == t).to_numpy())
        xs = np.array([elo[m] for m in scored["match_id"].gather(idx)]) / 100
        out[idx] = ct.logit_probs(xs, b, c1, c2)
    return out


def sensitivity(store: RawStore, tg: pl.DataFrame, t: datetime, friendly_weight: float,
                base: dict[str, Arr]) -> dict[str, Any]:  # fmt: skip
    """Headline parameters at the last refit with every prior sd x0.5 and x2: the shift of
    each posterior mean in base posterior sds (< 1 passes, §14.5)."""
    from adatta.model import diagnostics as dg

    def flat(p: dict[str, Arr]) -> tuple[dict[str, float], dict[str, float]]:
        m, s = {}, {}
        for k in ("c", "g", "h", "h_u", "conf", "sigma_w", "sigma_u", "sigma_o"):
            v = np.asarray(p[k]).reshape(len(p[k]), -1)
            for j in range(v.shape[1]):
                m[f"{k}{j}"], s[f"{k}{j}"] = float(v[:, j].mean()), float(v[:, j].std())
        return m, s

    bm, bs = flat(base)
    out: dict[str, Any] = {}
    for sc in (0.5, 2.0):
        alt, _ = cached_fit(store, tg, t, friendly_weight, sc)
        am, _ = flat(alt)
        chk = dg.sensitivity(bm, bs, am, f"x{sc:g}")
        out[f"x{sc:g}"] = {"passed": chk.passed, "worst": chk.value, "detail": chk.detail}
    return out


def leakage(tg: pl.DataFrame, scored: pl.DataFrame, elo: dict[int, float],
            diags: list[dict[str, Any]]) -> dict[str, Any]:  # fmt: skip
    """(6) On sampled targets: the refit is at the target's Monday (<= kickoff), its training
    ended by then, and the Elo recomputed from the matches final by that Monday alone is
    the same."""
    rng = np.random.default_rng(SEED)
    idx = rng.choice(scored.height, min(LEAKAGE_N, scored.height), replace=False)
    end = {d["as_of"]: d["train_max_kickoff"] for d in diags}
    bad = 0
    for i in idx:
        r = scored.row(int(i), named=True)
        t = r["refit"]
        bad += not (t == r["monday"] <= r["kickoff"])
        bad += not (datetime.fromisoformat(end[t.isoformat()]) + LAG <= t)
        again = elo_at(tg.filter(pl.col("kickoff") + LAG <= t).vstack(
            tg.filter(pl.col("match_id") == r["match_id"])), [(r["match_id"], t)])  # fmt: skip
        bad += abs(again[r["match_id"]] - elo[r["match_id"]]) > 1e-9
    return {"n": len(idx), "changed": int(bad), "ok": bad == 0}


# ------------------------------------------------------------------------------- CLI


def _load(store: RawStore) -> pl.DataFrame:
    from adatta.db.session import get_session

    with get_session() as s:
        tg = targets(s, store)
        s.rollback()
    return tg


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="adatta.model.national")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s0 = sub.add_parser("time")
    s0.add_argument("--at", required=True)
    s0.add_argument("--friendly-weight", type=float, default=FRIENDLY_WEIGHT)
    s1 = sub.add_parser("refits")
    s1.add_argument("--part", default="0/1")
    s1.add_argument("--upload", required=True)
    s1.add_argument("--friendly-weight", type=float, default=FRIENDLY_WEIGHT)
    s2 = sub.add_parser("gate")
    s2.add_argument("--fetch", required=True)
    s2.add_argument("--parts", type=int, required=True)
    s2.add_argument("--friendly-weight", type=float, default=FRIENDLY_WEIGHT)
    args = ap.parse_args(argv)
    store = make_store(get_settings())
    tg = _load(store)
    fw = args.friendly_weight
    times = refit_times(tg, DEV_FROM, HOLDOUT_FROM)
    print(f"targets {tg.height} (warm-up {tg.filter(pl.col('kickoff') < DEV_FROM).height}, dev "
          f"{tg.filter(pl.col('kickoff') >= DEV_FROM).height}); teams "
          f"{len(set(tg['home'].to_list()) | set(tg['away'].to_list()))}; dev refits "
          f"{len(times)}; friendly weight {fw:g}", flush=True)  # fmt: skip
    if args.cmd == "time":
        at = ct.monday(datetime.fromisoformat(args.at).replace(tzinfo=UTC))
        _, diag = cached_fit(store, tg, at, fw)
        print(f"refit {at:%Y-%m-%d}: {diag['n_train']} train, {diag['n_teams']} teams, "
              f"{diag['n_months']} months; {diag['seconds']:.0f} s; rhat {diag['rhat']:.3f}, "
              f"ess_bulk {diag['ess_bulk']:.0f}, divergences {diag['divergences']:.0f}; failed "
              f"{', '.join(diag['failed']) or 'none'}; warnings "
              f"{', '.join(diag['warnings']) or 'none'}")  # fmt: skip
        return
    if args.cmd == "refits":
        i, n = (int(x) for x in args.part.split("/"))
        scored, diags = run_part(store, tg, (i, n), fw)
        base = f"{PREFIX}/parts/{args.upload}/{i}of{n}"
        buf = io.BytesIO()
        scored.write_parquet(buf, compression="zstd")
        store.put(base + ".parquet", buf.getvalue())
        store.put(base + ".json", json.dumps(diags, default=str).encode())
        print(f"part {i}/{n}: {len(diags)} refits, {scored.height} targets scored")
        return
    frames, diags = [], []
    for i in range(args.parts):
        base = f"{PREFIX}/parts/{args.fetch}/{i}of{args.parts}"
        raw, rawd = try_get(store, base + ".parquet"), try_get(store, base + ".json")
        if raw is None or rawd is None:
            raise SystemExit(f"missing part {i}/{args.parts}")
        f = pl.read_parquet(io.BytesIO(raw))
        if f.height:
            frames.append(f)
        diags += json.loads(rawd)
    scored = pl.concat(frames).sort("kickoff", "match_id")
    diags.sort(key=lambda d: d["as_of"])
    if len(diags) != len(times) or scored.height != tg.filter(pl.col("kickoff") >= DEV_FROM).height:
        raise SystemExit(f"incomplete: {len(diags)} of {len(times)} refits, {scored.height} rows")
    elo = elo_at(tg, [(r["match_id"], r["monday"]) for r in tg.iter_rows(named=True)])
    pe = elo_probs(tg, scored, elo)
    text, rep = gate(scored, pe, diags)
    last_t = datetime.fromisoformat(diags[-1]["as_of"])
    base_post, _ = cached_fit(store, tg, last_t, fw)
    sens = sensitivity(store, tg, last_t, fw, base_post)
    leak = leakage(tg, scored, elo, diags)
    rep["checks"]["5 inference gates on every refit"] = (
        rep["checks"]["5 inference gates on every refit"]
        and all(v["passed"] for v in sens.values()))  # fmt: skip
    rep["checks"]["6 leakage"] = leak["ok"]
    rep["passed"] = all(rep["checks"].values())
    text += ("Prior sensitivity at the last refit (worst shift in posterior sds): "
             + "; ".join(f"{k} {v['worst']:.2f} ({v['detail']}) {'pass' if v['passed'] else 'FAIL'}"
                         for k, v in sens.items()) + "\n"
             f"6 leakage: {leak['changed']} of {leak['n']} sampled targets changed: "
             f"{'pass' if leak['ok'] else 'FAIL'}.\n"
             f"**Stage D v0 {'PASSES' if rep['passed'] else 'FAILS'} on dev.**\n")  # fmt: skip
    out = scored.with_columns(elo_home=pl.Series(pe[:, 0]), elo_draw=pl.Series(pe[:, 1]),
                              elo_away=pl.Series(pe[:, 2]))  # fmt: skip
    buf = io.BytesIO()
    out.write_parquet(buf, compression="zstd")
    store.put(f"{PREFIX}/reports/{args.fetch}/scored.parquet", buf.getvalue())
    store.put(f"{PREFIX}/reports/{args.fetch}/verdict.json",
              json.dumps({"passed": rep["passed"], "checks": rep["checks"], "report": rep,
                          "sensitivity": sens, "diags": diags}, default=str).encode())  # fmt: skip
    print(text)


if __name__ == "__main__":
    main()
