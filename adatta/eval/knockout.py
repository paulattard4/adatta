"""Stage B: knockout resolution on dev (docs/BEYOND_LEAGUES_PLAN.md, Stage B acceptance).

- B0: every stored tie of the five cups (seasons 2016-2023 and 2026; the holdout seasons are
  not read) against its format rule (`features.knockout`).
- B1: extra-time goals per side Poisson(kappa x rate / 3), the rate the draw's 90' rate from
  A1, kappa one pooled parameter on a grid, walk-forward; used only if it beats kappa = 1.
- B2: the shootout won by the listed home side with probability logistic(delta) at a home
  ground, 0.5 at a neutral one; delta walk-forward on a grid; used only if it beats 0.5.
- B3: P(advance) per tie and posterior draw from A1's score matrices and the rule, against
  the baseline "90' model, level = coin flip" (the gate).

A1's rows (with each tie's posterior draws) come from R2 (`cups/reports/<run>/`). Metrics
only in the output: the repo and its logs are public.

    uv run python -m adatta.eval.knockout run --a1 43903439721
"""

from __future__ import annotations

import argparse
import io
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy.stats import poisson

from adatta.eval import cup_terms
from adatta.features import cup_ties, knockout
from adatta.features import shootouts as so

Arr = NDArray[np.float64]
CUPS = (45, 143, 137, 81, 66)
SEASONS = (*range(2016, 2024), 2026)  # warm-up and dev, and the live season; no holdout
DEV = tuple(range(2019, 2024))
RESULT_LAG = timedelta(hours=3)
LOG_KAPPA = np.round(np.arange(-1.5, 1.5001, 0.02), 4)  # B1 grid
KAPPA_PRIOR_SD = 0.5
DELTA = np.round(np.arange(-1.0, 1.0001, 0.02), 4)  # B2 grid
DELTA_PRIOR_SD = 0.25
FIT_DRAWS = 100  # draws per tie in the B1 likelihood (as A2)
ET_MAX = 8  # extra-time goals per side considered
MAX_GOALS = 10
ECE_MAX = 0.05
CUP_WORSE_MAX = 0.01


def monday(t: datetime) -> datetime:
    return (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------------------ loading


@dataclass
class Data:
    ties: list[knockout.Tie]
    later: dict[tuple[int, int], list[tuple[datetime, int, int]]]  # (cup, season) -> matches
    roles: dict[int, str]  # match_id -> venue role
    irregular: list[int]

    def later_teams(self, t: knockout.Tie) -> set[int]:
        end = t.last_kickoff
        return {x for k, h, a in self.later.get((t.cup, t.season), []) if k > end for x in (h, a)}


def load(session: Any, seasons: Sequence[int] = SEASONS) -> Data:
    """The five cups' matches of `seasons` (SEASONS: no holdout): finished ones as ties, every
    one (any status) as the 'later' index, and venue roles of the finished ones."""
    from sqlalchemy import select

    from adatta.cli import _bundle_competition_ids
    from adatta.db import models
    from adatta.features import neutral

    M, S, C, R = models.Match, models.Season, models.Competition, models.MatchResult
    formats = cup_ties.load_formats()
    pairs: list[tuple[cup_ties.CupMatch, knockout.Played]] = []
    later: dict[tuple[int, int], list[tuple[datetime, int, int]]] = defaultdict(list)
    done_rows: list[Any] = []
    league_of: dict[int, int] = {}
    for cup in CUPS:
        rows = session.execute(
            select(M, S.year, R).join(S, S.season_id == M.season_id)
            .join(C, C.competition_id == S.competition_id)
            .outerjoin(R, (R.match_id == M.match_id) & R.superseded_at.is_(None))
            .where(C.source_ref == str(cup), S.year.in_(list(seasons)))
        ).all()  # fmt: skip
        for m, y, r in rows:
            k = m.kickoff_time.astimezone(UTC)
            later[(cup, int(y))].append((k, m.home_team_id, m.away_team_id))
            if m.status not in ("FT", "AET", "PEN"):
                continue
            nt: tuple[int, int] | None = None
            if r is not None and r.home_goals_nt is not None and r.away_goals_nt is not None:
                nt = (int(r.home_goals_nt), int(r.away_goals_nt))
            played = knockout.Played(
                m.match_id, "", m.home_team_id, m.away_team_id, k, m.status,
                nt,
                (r.home_goals_et, r.away_goals_et) if r is not None else (None, None),
                (r.home_pens, r.away_pens) if r is not None else (None, None))  # fmt: skip
            cm = cup_ties.CupMatch(m.match_id, cup, int(y), m.round or "", m.home_team_id,
                                   m.away_team_id, k)  # fmt: skip
            pairs.append((cm, played))
            done_rows.append(m)
            league_of[m.season_id] = cup
    labels, _ = cup_ties.label([c for c, _ in pairs], formats)
    pairs = [(c, _with_kind(p, labels[c.match_id].kind)) for c, p in pairs]
    ties, irregular = knockout.group(pairs, formats)
    league_ids, _ = _bundle_competition_ids(session)
    roles = neutral.venue_roles(session, done_rows, league_ids, neutral.load_rules(), league_of)
    return Data(ties, dict(later), dict(zip(roles["match_id"].to_list(), roles["role"].to_list(),
                                            strict=True)), irregular)  # fmt: skip


def _with_kind(p: knockout.Played, kind: str) -> knockout.Played:
    return knockout.Played(p.match_id, kind, p.home, p.away, p.kickoff, p.status, p.nt, p.et,
                           p.pens)  # fmt: skip


def bundle_teams(session: Any) -> dict[tuple[str, int], set[int]]:
    """(bundle, season) -> teams with a league match in the bundle's leagues that season."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.eval import harness

    M, S, C = models.Match, models.Season, models.Competition
    out: dict[tuple[str, int], set[int]] = defaultdict(set)
    for b, leagues in harness.load_config().bundles.items():
        for h, a, y in session.execute(
            select(M.home_team_id, M.away_team_id, S.year).join(S, S.season_id == M.season_id)
            .join(C, C.competition_id == S.competition_id)
            .where(C.source_ref.in_([str(x) for x in leagues]), S.year.in_(SEASONS))
        ):  # fmt: skip
            out[(b, int(y))] |= {h, a}
    return dict(out)


# ---------------------------------------------------------------------------------- B0


def b0(d: Data, rules: dict[int, list[knockout.Entry]], bundle: set[int]) -> dict[str, Any]:
    """Each tie against its rule; problems counted by cup, and listed for bundle ties."""
    by_cup: dict[int, Counter[str]] = defaultdict(Counter)
    listed: list[str] = []
    for t in d.ties:
        rule = knockout.rule_for(rules, t.cup, t.season, t.round)
        why = knockout.check(t, rule)
        is_b = any(m.match_id in bundle for m in t.matches)
        by_cup[t.cup]["ties"] += 1
        by_cup[t.cup]["bundle_ties"] += is_b
        if why is not None:
            by_cup[t.cup][f"problem: {why}"] += 1
            if is_b:
                listed.append(f"{t.cup} {t.season} {cup_ties.base_round(t.round)}: {why} "
                              f"(matches {[m.match_id for m in t.matches]})")  # fmt: skip
    return {"by_cup": {c: dict(v) for c, v in by_cup.items()}, "bundle_problems": listed,
            "irregular": len(d.irregular)}  # fmt: skip


# ---------------------------------------------------------------------------------- B1


def et_probs(lam: Arr, mu: Arr, kappa: float) -> tuple[Arr, Arr, Arr]:
    """(home, level, away) after extra time per draw, goals Poisson(kappa x rate / 3)."""
    k = np.arange(ET_MAX + 1)
    ph = np.asarray(poisson.pmf(k, kappa * np.asarray(lam)[..., None] / 3), dtype=np.float64)
    pa = np.asarray(poisson.pmf(k, kappa * np.asarray(mu)[..., None] / 3), dtype=np.float64)
    m = ph[..., :, None] * pa[..., None, :]
    m = m / m.sum(axis=(-2, -1), keepdims=True)
    return (np.tril(m, -1).sum(axis=(-2, -1)), np.trace(m, axis1=-2, axis2=-1),
            np.triu(m, 1).sum(axis=(-2, -1)))  # fmt: skip


def grid_loglik(lam: Arr, mu: Arr, eh: int, ea: int, grid: Arr = LOG_KAPPA) -> Arr:
    """log mean over draws of P(extra-time goals | kappa) at each grid point."""
    kap = np.exp(grid)[:, None]
    p = np.asarray(poisson.pmf(eh, kap * lam[None, :] / 3) * poisson.pmf(ea, kap * mu[None, :] / 3),
                   dtype=np.float64)  # fmt: skip
    return np.asarray(np.log(np.maximum(p.mean(axis=1), 1e-300)))


def posterior_mean(loglik: Arr, grid: Arr, prior_sd: float) -> tuple[float, float, float]:
    """(mean, sd, mass on the grid's two edge points) of a grid posterior."""
    lp = loglik - 0.5 * (grid / prior_sd) ** 2
    w = np.exp(lp - lp.max())
    w /= w.sum()
    m = float((w * grid).sum())
    return m, float(np.sqrt((w * (grid - m) ** 2).sum())), float(w[0] + w[-1])


@dataclass
class EtObs:
    match_id: int
    kickoff: datetime
    lam: Arr  # the A1 draws' 90' rates, oriented as the stored match
    mu: Arr
    eh: int
    ea: int


def walk(obs_times: list[datetime], ll: Arr, at: list[datetime], grid: Arr,
         prior_sd: float) -> list[tuple[float, float, float]]:  # fmt: skip
    """Grid posterior at each time in `at` from the observations final by its Monday."""
    out = []
    for t in at:
        cut = monday(t)
        use = [i for i, k in enumerate(obs_times) if k + RESULT_LAG <= cut]
        total = ll[use].sum(axis=0) if use else np.zeros(len(grid))
        out.append(posterior_mean(total, grid, prior_sd))
    return out


def b1(obs: list[EtObs]) -> dict[str, Any]:
    """Walk-forward kappa on the extra-time observations, scored against kappa = 1."""
    if not obs:
        return {"n": 0, "use": False}
    obs = sorted(obs, key=lambda o: o.kickoff)
    rng = np.random.default_rng(20261121)
    rows = []
    for o in obs:
        idx = rng.choice(len(o.lam), min(FIT_DRAWS, len(o.lam)), replace=False)
        rows.append(grid_loglik(o.lam[idx], o.mu[idx], o.eh, o.ea))
    ll = np.stack(rows)
    fits = walk([o.kickoff for o in obs], ll, [o.kickoff for o in obs], LOG_KAPPA, KAPPA_PRIOR_SD)
    d = []
    for o, (m, _, _) in zip(obs, fits, strict=True):
        p1 = float(np.mean(poisson.pmf(o.eh, o.lam / 3) * poisson.pmf(o.ea, o.mu / 3)))
        k = float(np.exp(m))
        pk = float(np.mean(poisson.pmf(o.eh, k * o.lam / 3) * poisson.pmf(o.ea, k * o.mu / 3)))
        d.append(-np.log(pk) + np.log(p1))
    diff = np.asarray(d)
    half = 1.96 * diff.std(ddof=1) / np.sqrt(len(diff)) if len(diff) > 1 else np.nan
    last = posterior_mean(ll.sum(axis=0), LOG_KAPPA, KAPPA_PRIOR_SD)
    sens = {sd: posterior_mean(ll.sum(axis=0), LOG_KAPPA, sd)[0] for sd in (0.25, 0.5, 1.0)}
    pred = float(np.mean([(np.exp(last[0]) * (o.lam + o.mu) / 3).mean() for o in obs]))
    return {"n": len(obs), "diff": float(diff.mean()), "lo": float(diff.mean() - half),
            "hi": float(diff.mean() + half), "use": bool(diff.mean() + half < 0),
            "log_kappa": last[0], "sd": last[1], "edge_max": max(f[2] for f in fits),
            "sens": sens, "goals_pred": pred,
            "goals_obs": float(np.mean([o.eh + o.ea for o in obs])),
            "fits": [(o.kickoff, f[0]) for o, f in zip(obs, fits, strict=True)]}  # fmt: skip


# ---------------------------------------------------------------------------------- B2


@dataclass
class Shoot:
    kickoff: datetime
    home_ground: bool
    home_won: bool


def shootouts(d: Data, rules: dict[int, list[knockout.Entry]]) -> list[Shoot]:
    """Every decided shootout of the loaded ties: the deciding match (leg 2, replay or the
    single match) went to penalties, with a winner by the shootout rule, or (a level match
    recorded as FT where the rule has no extra time) the side that went through."""
    out = []
    for t in d.ties:
        rule = knockout.rule_for(rules, t.cup, t.season, t.round)
        if rule is None or knockout.check(t, rule) is not None:
            continue
        dec = t.matches[-1]
        adv, _ = knockout.advanced(t, d.later_teams(t))
        side_adv = None if adv is None else ("home" if adv == dec.home else "away")
        if dec.status == "PEN":
            s = so.decide(dec.pens, None, side_adv)
            won = s.winner
        elif (dec.status == "FT" and not rule.extra_time and dec.nt is not None
              and dec.nt[0] == dec.nt[1] and dec.kind != cup_ties.LEG2):  # fmt: skip
            won = side_adv
        else:
            continue
        if won is None:
            continue
        out.append(Shoot(dec.kickoff, d.roles.get(dec.match_id, "home") != "neutral",
                         won == "home"))  # fmt: skip
    return sorted(out, key=lambda s: s.kickoff)


def b2(shots: list[Shoot], dev_from: datetime) -> dict[str, Any]:
    """Walk-forward delta, scored on the dev shootouts against the coin flip."""
    ll = np.stack([
        np.log(1 / (1 + np.exp(-DELTA))) if s.home_won else np.log(1 / (1 + np.exp(DELTA)))
        if s.home_ground else np.full(len(DELTA), np.log(0.5)) for s in shots
    ])  # fmt: skip
    fits = walk([s.kickoff for s in shots], ll, [s.kickoff for s in shots], DELTA, DELTA_PRIOR_SD)
    diff = []
    for s, (m, _, _) in zip(shots, fits, strict=True):
        if s.kickoff < dev_from or not s.home_ground:
            continue
        p = 1 / (1 + np.exp(-m))
        diff.append(-np.log(p if s.home_won else 1 - p) - np.log(2))
    a = np.asarray(diff)
    half = 1.96 * a.std(ddof=1) / np.sqrt(len(a)) if len(a) > 1 else np.nan
    last = posterior_mean(ll.sum(axis=0), DELTA, DELTA_PRIOR_SD)
    hg = [s for s in shots if s.home_ground]
    return {"n": len(shots), "n_dev_home_ground": len(a), "diff": float(a.mean()),
            "lo": float(a.mean() - half), "hi": float(a.mean() + half),
            "use": bool(a.mean() + half < 0), "delta": last[0], "sd": last[1],
            "edge_max": max(f[2] for f in fits),
            "sens": {sd: posterior_mean(ll.sum(axis=0), DELTA, sd)[0] for sd in (0.1, 0.25, 0.5)},
            "home_rate": float(np.mean([s.home_won for s in hg])) if hg else float("nan"),
            "fits": [(s.kickoff, f[0]) for s, f in zip(shots, fits, strict=True)]}  # fmt: skip


def fit_at(fits: list[tuple[datetime, float]], t: datetime) -> float:
    """A walk-forward estimate for a tie at time t: the last fit made in its week or before
    (each fit uses only results final by its own Monday); 0 (the prior) before any."""
    got = [m for k, m in fits if monday(k) <= monday(t)]
    return got[-1] if got else 0.0


# ---------------------------------------------------------------------------------- B3


def single(lam: Arr, mu: Arr, rho: Arr, kap: Arr, rule: knockout.Rule, kappa_et: float,
           p_so_home: float, h_term: Arr, neutral: bool) -> tuple[Arr, Arr]:  # fmt: skip
    """(P(home side advances), baseline) per draw for a single-match tie."""
    m = cup_terms.matrices(lam, mu, rho, kap, MAX_GOALS)
    ph, pd = np.tril(m, -1).sum(axis=(-2, -1)), np.trace(m, axis1=-2, axis2=-1)
    base = ph + 0.5 * pd
    if rule.replay:  # level: a replay at the other ground (sides swapped, same home term)
        h = np.zeros_like(lam) if neutral else h_term
        lr, mr = mu * np.exp(h), lam * np.exp(-h)
        mrep = cup_terms.matrices(lr, mr, rho, kap, MAX_GOALS)
        rh, rd = np.tril(mrep, -1).sum(axis=(-2, -1)), np.trace(mrep, axis1=-2, axis2=-1)
        ra = 1 - rh - rd
        eh, ed, ea = et_probs(lr, mr, kappa_et)
        p_so_r = 0.5 if neutral else p_so_home  # the replay's home side is the old away side
        win_r = ra + rd * (ea + ed * (1 - p_so_r))
        return ph + pd * win_r, base
    if rule.extra_time:
        eh, ed, _ = et_probs(lam, mu, kappa_et)
        return ph + pd * (eh + ed * p_so_home), base
    return ph + pd * p_so_home, base


def two_legs(x1: int, y1: int, lam2: Arr, mu2: Arr, rho: Arr, kap: Arr, rule: knockout.Rule,
             kappa_et: float, p_so_home2: float) -> tuple[Arr, Arr]:  # fmt: skip
    """(P(side A advances), baseline) per draw before leg 2: leg 1 ended x1-y1 with A at
    home; leg 2 has B at home with rates (lam2, mu2)."""
    m = cup_terms.matrices(lam2, mu2, rho, kap, MAX_GOALS)  # [i = B goals, j = A goals]
    g = np.arange(MAX_GOALS + 1)
    i, j = np.meshgrid(g, g, indexing="ij")
    agg_a, agg_b = x1 + j, y1 + i
    a_win = agg_a > agg_b
    level = agg_a == agg_b
    base = (m * a_win).sum(axis=(-2, -1)) + 0.5 * (m * level).sum(axis=(-2, -1))
    if rule.away_goals != knockout.NONE:
        a_win = a_win | (level & (j > y1))
        level = level & (j == y1)
    p = (m * a_win).sum(axis=(-2, -1))
    pl_ = (m * level).sum(axis=(-2, -1))  # level after 90' and away goals: extra time
    if not rule.extra_time:
        return p + pl_ * (1 - p_so_home2), base
    k = np.arange(ET_MAX + 1)
    eb = np.asarray(poisson.pmf(k, kappa_et * np.asarray(lam2)[..., None] / 3), dtype=np.float64)
    ea = np.asarray(poisson.pmf(k, kappa_et * np.asarray(mu2)[..., None] / 3), dtype=np.float64)
    e = eb[..., :, None] * ea[..., None, :]
    e = e / e.sum(axis=(-2, -1), keepdims=True)
    ki, kj = np.meshgrid(k, k, indexing="ij")
    a_et = kj > ki
    lev = kj == ki
    if rule.away_goals == knockout.ALSO_ET:  # level after extra time: A's away goals scored in it
        a_et = a_et | (lev & (kj > 0))
        lev = lev & (kj == 0)
    pa_et = (e * a_et).sum(axis=(-2, -1))
    pe_lev = (e * lev).sum(axis=(-2, -1))
    return p + pl_ * (pa_et + pe_lev * (1 - p_so_home2)), base


def ece(p: Arr, y: Arr, bins: int = 10) -> float:
    b = np.clip((p * bins).astype(int), 0, bins - 1)
    out = 0.0
    for k in range(bins):
        s = b == k
        if s.any():
            out += s.sum() * abs(p[s].mean() - y[s].mean())
    return float(out / len(p))


def _ll(p: Arr, y: Arr) -> Arr:
    q = np.clip(p, 1e-12, 1 - 1e-12)
    return np.asarray(-(y * np.log(q) + (1 - y) * np.log(1 - q)))


def b3(a1: pl.DataFrame, d: Data, rules: dict[int, list[knockout.Entry]],
       kappa_fits: list[tuple[datetime, float]], use_kappa: bool,
       delta_fits: list[tuple[datetime, float]], use_delta: bool,
       targets: Sequence[int] = DEV) -> tuple[pl.DataFrame, dict[str, Any]]:  # fmt: skip
    """P(advance) of side A for every dev bundle tie in A1 (single and replay ties as of the
    first match, two legs before leg 2), against the baseline."""
    rows = {int(r["match_id"]): r for r in a1.iter_rows(named=True)}
    out = []
    skipped: Counter[str] = Counter()
    for t in d.ties:
        lead = t.matches[0]
        if lead.match_id not in rows or t.season not in targets:
            continue
        rule = knockout.rule_for(rules, t.cup, t.season, t.round)
        if rule is None or knockout.check(t, rule) is not None:
            skipped["rule problem"] += 1
            continue
        adv, basis = knockout.advanced(t, d.later_teams(t))
        if adv is None:
            skipped[f"no label ({basis})"] += 1
            continue
        if rule.legs == 2:
            l1, l2 = t.first(cup_ties.LEG1), t.first(cup_ties.LEG2)
            if l1 is None or l2 is None or l2.match_id not in rows or l1.nt is None:
                skipped["leg missing"] += 1
                continue
            r = rows[l2.match_id]
            lam, mu = np.asarray(r["draws_lam"]), np.asarray(r["draws_mu"])
            if int(r["home"]) != t.b:  # A1 swapped leg 2's sides: back to B at home
                lam, mu = mu, lam
            k_et = float(np.exp(fit_at(kappa_fits, l2.kickoff))) if use_kappa else 1.0
            dl = fit_at(delta_fits, l2.kickoff) if use_delta else 0.0
            neutral = r["role"] == "neutral"
            p_so = 0.5 if neutral else float(1 / (1 + np.exp(-dl)))
            p, base = two_legs(l1.nt[0], l1.nt[1], lam, mu, np.asarray(r["draws_rho"]),
                               np.asarray(r["draws_kappa"]), rule, k_et, p_so)  # fmt: skip
            at, kind = l2.kickoff, "two legs"
        else:
            r = rows[lead.match_id]
            lam, mu = np.asarray(r["draws_lam"]), np.asarray(r["draws_mu"])
            neutral = r["role"] == "neutral"
            k_et = float(np.exp(fit_at(kappa_fits, lead.kickoff))) if use_kappa else 1.0
            dl = fit_at(delta_fits, lead.kickoff) if use_delta else 0.0
            p_so = 0.5 if neutral else float(1 / (1 + np.exp(-dl)))
            p, base = single(lam, mu, np.asarray(r["draws_rho"]), np.asarray(r["draws_kappa"]),
                             rule, k_et, p_so, np.asarray(r["draws_home_term"]),
                             neutral)  # fmt: skip
            if int(r["home"]) != t.a:  # A1's home side is B: A advances when it doesn't
                p, base = 1 - p, 1 - base
            at, kind = lead.kickoff, "replay" if rule.replay else "single"
        out.append({"cup": t.cup, "season": t.season, "round": cup_ties.base_round(t.round),
                    "kickoff": at, "kind": kind, "pairing": r.get("pairing"),
                    "p": float(np.mean(p)), "p_lo": float(np.quantile(p, 0.05)),
                    "p_hi": float(np.quantile(p, 0.95)), "base": float(np.mean(base)),
                    "y": float(adv == t.a), "basis": basis, "end": t.last_kickoff})  # fmt: skip
    df = pl.DataFrame(out)
    return df, gate(df, skipped)


def gate(df: pl.DataFrame, skipped: Counter[str]) -> dict[str, Any]:
    y, p, base = df["y"].to_numpy(), df["p"].to_numpy(), df["base"].to_numpy()
    d = _ll(p, y) - _ll(base, y)
    half = 1.96 * d.std(ddof=1) / np.sqrt(len(d))
    rel = float(d.mean() / _ll(base, y).mean())
    by_cup = {}
    for (cup,), g in df.group_by("cup"):
        gy, gp, gb = g["y"].to_numpy(), g["p"].to_numpy(), g["base"].to_numpy()
        by_cup[int(cup)] = {"n": g.height, "ll": float(_ll(gp, gy).mean()),
                            "ll_base": float(_ll(gb, gy).mean()),
                            "rel": float((_ll(gp, gy) - _ll(gb, gy)).mean()
                                         / _ll(gb, gy).mean())}  # fmt: skip
    e = ece(p, y)
    # reported, not gated: ECE of a perfectly calibrated forecast on these ties (y drawn from p)
    rng = np.random.default_rng(20261121)
    null = np.array([ece(p, (rng.random(len(p)) < p).astype(float)) for _ in range(2000)])
    rel_tab = []
    for k in range(10):
        sel = np.clip((p * 10).astype(int), 0, 9) == k
        if sel.any():
            rel_tab.append((k, int(sel.sum()), float(p[sel].mean()), float(y[sel].mean())))
    checks = {
        "1 log loss no worse (point)": bool(d.mean() <= 0),
        f"2 ECE <= {ECE_MAX}": bool(e <= ECE_MAX),
        "3 no cup worse by > 1.0%": all(v["rel"] <= CUP_WORSE_MAX for v in by_cup.values()),
    }
    return {"n": len(d), "ll": float(_ll(p, y).mean()), "ll_base": float(_ll(base, y).mean()),
            "diff": float(d.mean()), "lo": float(d.mean() - half), "hi": float(d.mean() + half),
            "rel": rel, "brier": float(((p - y) ** 2).mean()),
            "brier_base": float(((base - y) ** 2).mean()), "ece": e, "ece_base": ece(base, y),
            "by_cup": by_cup, "checks": checks, "passed": all(checks.values()),
            "ece_null_mean": float(null.mean()), "ece_null_95": float(np.quantile(null, 0.95)),
            "ece_null_share_above": float((null >= e).mean()), "reliability": rel_tab,
            "skipped": dict(skipped)}  # fmt: skip


# ------------------------------------------------------------------ B3' (amendment 1)

LOG_S = np.round(np.arange(-1.0, 1.5001, 0.01), 4)
S_PRIOR_SD = 0.5


def _logit(p: Arr) -> Arr:
    q = np.clip(p, 1e-9, 1 - 1e-9)
    return np.asarray(np.log(q / (1 - q)))


def stretch(p: Arr, s: float) -> Arr:
    """logit(p') = s x logit(p)."""
    return np.asarray(1 / (1 + np.exp(-s * _logit(p))))


def recalibrate(df: pl.DataFrame) -> tuple[pl.DataFrame, list[tuple[datetime, float]]]:
    """Walk-forward s per tie (from ties final by its Monday), and p' = stretch(p, s); the
    interval stretched the same way. Rows in time order; returns the fits for later ties."""
    df = df.sort("kickoff")
    z = _logit(df["p"].to_numpy())
    y = df["y"].to_numpy()
    ends = df["end"].to_list()
    ss = np.exp(LOG_S)[:, None]
    ll = (y * -np.log1p(np.exp(-ss * z)) + (1 - y) * -np.log1p(np.exp(ss * z))).T  # (n, G)
    fits = walk(ends, ll, df["kickoff"].to_list(), LOG_S, S_PRIOR_SD)
    s_at = np.exp([f[0] for f in fits])

    def st(col: str) -> pl.Series:
        return pl.Series(1 / (1 + np.exp(-s_at * _logit(df[col].to_numpy()))))

    out = df.with_columns(s=pl.Series(s_at), p_raw=pl.col("p"), p=st("p"), p_lo=st("p_lo"),
                          p_hi=st("p_hi"))  # fmt: skip
    return out, [(k, f[0]) for k, f in zip(df["kickoff"].to_list(), fits, strict=True)]


# ------------------------------------------------------------------------------- report


CUP_NAME = {45: "FA Cup", 143: "Copa del Rey", 137: "Coppa Italia", 81: "DFB-Pokal",
            66: "Coupe de France"}  # fmt: skip


def markdown(r0: dict[str, Any], r1: dict[str, Any], r2: dict[str, Any], r3: dict[str, Any],
             df: pl.DataFrame) -> str:  # fmt: skip
    lines = ["# Stage B on dev", "", "## B0 format rules", ""]
    for cup, c in sorted(r0["by_cup"].items()):
        probs = {k[9:]: v for k, v in c.items() if k.startswith("problem: ")}
        lines.append(f"- {CUP_NAME[cup]}: {c['ties']} ties ({c['bundle_ties']} bundle); "
                     f"problems {sum(probs.values())}: {probs or 'none'}")  # fmt: skip
    lines.append(f"- Bundle ties with a problem: {len(r0['bundle_problems'])}")
    lines += [f"  - {x}" for x in r0["bundle_problems"]]
    lines += ["", "## B1 extra-time goals", "",
              f"{r1['n']} extra-time periods. kappa (last) {np.exp(r1['log_kappa']):.3f} "
              f"(log {r1['log_kappa']:+.3f}, sd {r1['sd']:.3f}); prior sd 0.25 / 0.5 / 1.0: "
              + " / ".join(f"{np.exp(v):.3f}" for v in r1["sens"].values()) + ".",
              f"Goals per extra time: predicted {r1['goals_pred']:.3f}, observed "
              f"{r1['goals_obs']:.3f}. Max edge mass {r1['edge_max']:.1e}.",
              f"Log loss against kappa = 1, per period: {r1['diff']:+.4f} "
              f"[{r1['lo']:+.4f}, {r1['hi']:+.4f}]. **Used: {r1['use']}.**",
              "", "## B2 shootouts", "",
              f"{r2['n']} shootouts ({r2['n_dev_home_ground']} dev at a home ground). Home side "
              f"won {r2['home_rate']:.3f} at home grounds. delta (last) {r2['delta']:+.3f} (sd "
              f"{r2['sd']:.3f}); prior sd 0.1 / 0.25 / 0.5: "
              + " / ".join(f"{v:+.3f}" for v in r2["sens"].values()) + ".",
              f"Log loss against the coin flip: {r2['diff']:+.4f} [{r2['lo']:+.4f}, "
              f"{r2['hi']:+.4f}]. Max edge mass {r2['edge_max']:.1e}. **Used: {r2['use']}.**",
              "", "## B3 P(advance), the gate", "",
              f"{r3['n']} dev bundle ties. Log loss {r3['ll']:.4f} against the baseline "
              f"{r3['ll_base']:.4f}: {r3['diff']:+.5f} [{r3['lo']:+.5f}, {r3['hi']:+.5f}], "
              f"{r3['rel']:+.2%}. Brier {r3['brier']:.4f} / {r3['brier_base']:.4f}. ECE "
              f"{r3['ece']:.4f} (baseline {r3['ece_base']:.4f}).",
              f"Skipped: {r3['skipped'] or 'none'}.",
              f"ECE of a perfectly calibrated forecast on these ties (y drawn from p, 2,000 "
              f"draws; reported, not gated): mean {r3['ece_null_mean']:.4f}, 95th percentile "
              f"{r3['ece_null_95']:.4f}; {r3['ece_null_share_above']:.1%} of them at or above "
              f"the observed {r3['ece']:.4f}.", "",
              "| p(A) bin | ties | mean p | observed |", "|---|---|---|---|",
              *[f"| {k / 10:.1f}-{(k + 1) / 10:.1f} | {n} | {mp:.3f} | {ob:.3f} |"
                for k, n, mp, ob in r3["reliability"]], "",
              "| cup | ties | log loss | baseline | relative |",
              "|---|---|---|---|---|"]  # fmt: skip
    for cup, v in sorted(r3["by_cup"].items()):
        lines.append(f"| {CUP_NAME[cup]} | {v['n']} | {v['ll']:.4f} | {v['ll_base']:.4f} | "
                     f"{v['rel']:+.2%} |")  # fmt: skip
    lines += ["", "| kind | ties | mean p(A) | observed |", "|---|---|---|---|"]
    for (k,), g in sorted(df.group_by("kind")):
        mp, ob = float(np.mean(g["p"].to_numpy())), float(np.mean(g["y"].to_numpy()))
        lines.append(f"| {k} | {g.height} | {mp:.3f} | {ob:.3f} |")
    lines += ["", "Checks: " + "; ".join(f"{k}: {'pass' if v else 'FAIL'}"
                                         for k, v in r3["checks"].items()),
              f"**B3 {'PASSES' if r3['passed'] else 'FAILS'}.**"]  # fmt: skip
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------ holdout

HOLDOUT = (2024, 2025)


def ece_null(p: Arr, n_sims: int = 2000, seed: int = 20261121) -> float:
    """95th percentile of a perfectly calibrated forecast's ECE on these forecasts."""
    rng = np.random.default_rng(seed)
    return float(np.quantile([ece(p, (rng.random(len(p)) < p).astype(float))
                              for _ in range(n_sims)], 0.95))  # fmt: skip


def a1_calibration(pred: pl.DataFrame, n_sims: int = 2000, seed: int = 20261121) -> dict[str, Any]:
    """A1's 1X2 ECE per outcome against its noise floor (outcomes drawn from the forecast)."""
    p = pred.select("p_home", "p_draw", "p_away").to_numpy()
    p = p / p.sum(axis=1, keepdims=True)
    y = np.where(pred["hg"] > pred["ag"], 0, np.where(pred["hg"] == pred["ag"], 1, 2))
    rng = np.random.default_rng(seed)
    cum = p.cumsum(axis=1)
    sims = [[] for _ in range(3)]  # type: list[list[float]]
    for _ in range(n_sims):
        ys = (rng.random(len(p))[:, None] > cum).sum(axis=1)
        for k in range(3):
            sims[k].append(ece(p[:, k], (ys == k).astype(float)))
    out: dict[str, Any] = {}
    for k, name in enumerate(("home", "draw", "away")):
        e = ece(p[:, k], (y == k).astype(float))
        lim = float(np.quantile(sims[k], 0.95))
        out[name] = {"ece": e, "limit": lim, "pass": bool(e <= lim)}
    ll = -np.log(np.clip(p[np.arange(len(p)), y], 1e-12, 1))
    out["n"] = len(p)
    out["log_loss_1x2"] = float(ll.mean())
    return out


def judge(dev_a1: pl.DataFrame, ho_a1: pl.DataFrame, d: Data,
          rules: dict[int, list[knockout.Entry]]) -> tuple[str, dict[str, Any]]:  # fmt: skip
    """The Stage B promotion on the cup holdout (amendment 1, owner: noise-floor calibration).
    B3 with kappa = 1 and the coin-flip shootout (B1 and B2 not used on dev); B3' stretches it
    with s walk-forward through the dev ties and on through the holdout ties."""
    a1 = pl.concat([dev_a1, ho_a1], how="diagonal_relaxed")
    r0 = b0(Data([t for t in d.ties if t.season in HOLDOUT], d.later, d.roles, d.irregular),
            rules, set(ho_a1["match_id"].to_list()))  # fmt: skip
    df, _ = b3(a1, d, rules, [], False, [], False, targets=(*DEV, *HOLDOUT))
    cal, _ = recalibrate(df)
    hc = cal.filter(pl.col("season").is_in(list(HOLDOUT)))
    raw = df.filter(pl.col("season").is_in(list(HOLDOUT)))
    g = gate(hc, Counter())
    lim = ece_null(hc["p"].to_numpy())
    checks = {
        "B3' log loss no worse than the baseline (point)": g["diff"] <= 0,
        "B3' ECE within the noise floor (95th percentile)": g["ece"] <= lim,
        "B3' no cup worse by more than 1.0%": all(v["rel"] <= CUP_WORSE_MAX
                                                  for v in g["by_cup"].values()),
    }  # fmt: skip
    a1c = a1_calibration(ho_a1)
    for o in ("home", "draw", "away"):
        checks[f"A1 1X2 ECE ({o}) within the noise floor"] = a1c[o]["pass"]
    graw = gate(raw, Counter())
    rep = {"b0": r0, "n_ties": g["n"], "b3prime": g, "ece_limit": lim, "b3": graw, "a1": a1c,
           "s_range": (float(hc["s"].min() or 0), float(hc["s"].max() or 0)),  # type: ignore[arg-type]
           "checks": checks, "passed": all(checks.values())}  # fmt: skip
    lines = ["# Stage A/B promotion on the cup holdout (opened once)", "",
             f"B0: bundle ties with a problem {len(r0['bundle_problems'])}"]  # fmt: skip
    reasons = Counter(x.split(": ", 1)[1].split(" (matches")[0] for x in r0["bundle_problems"])
    lines += [f"  - {why}: {n}" for why, n in sorted(reasons.items())]  # counts only: CI is public
    lines += [f"A1 on {a1c['n']} holdout bundle ties: 1X2 log loss {a1c['log_loss_1x2']:.4f}; ECE "
              + ", ".join(f"{o} {a1c[o]['ece']:.4f} (limit {a1c[o]['limit']:.4f})"
                          for o in ("home", "draw", "away")),
              f"B3' on {g['n']} holdout ties (s {rep['s_range'][0]:.3f}-{rep['s_range'][1]:.3f}): "
              f"log loss {g['ll']:.4f} against the baseline {g['ll_base']:.4f}: {g['diff']:+.5f} "
              f"[{g['lo']:+.5f}, {g['hi']:+.5f}], {g['rel']:+.2%}; ECE {g['ece']:.4f} (limit "
              f"{lim:.4f}); Brier {g['brier']:.4f} / {g['brier_base']:.4f}.",
              f"B3 unstretched (reported): log loss {graw['ll']:.4f}, ECE {graw['ece']:.4f}.",
              "By cup (B3' against the baseline): " + "; ".join(
                  f"{CUP_NAME[c]} {v['n']} ties {v['rel']:+.2%}"
                  for c, v in sorted(g["by_cup"].items())),
              "", "| p(A) bin | ties | mean p | observed |", "|---|---|---|---|",
              *[f"| {k / 10:.1f}-{(k + 1) / 10:.1f} | {n} | {mp:.3f} | {ob:.3f} |"
                for k, n, mp, ob in g["reliability"]],
              "", "Checks: " + "; ".join(f"{k}: {'pass' if v else 'FAIL'}"
                                         for k, v in checks.items()),
              f"**{'PROMOTE' if rep['passed'] else 'NOT PROMOTED'}.**"]  # fmt: skip
    return "\n".join(lines) + "\n", rep


def main(argv: Sequence[str] | None = None) -> None:
    from adatta.adapters.storage import make_store, try_get
    from adatta.config import get_settings
    from adatta.db.session import get_session

    ap = argparse.ArgumentParser(prog="adatta.eval.knockout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--a1", required=True)
    h = sub.add_parser("holdout", help="the promotion on the opened cup holdout")
    h.add_argument("--a1", required=True, help="the dev A1 run")
    h.add_argument("--run", required=True, help="the run that opened the cup holdout")
    args = ap.parse_args(argv)
    store = make_store(get_settings())
    if args.cmd == "holdout":
        import json

        from adatta.eval import harness
        from adatta.eval import holdout as ho
        from adatta.model import backtest as bt

        model = bt.production_spec(harness.load_config()).model_name
        ho.unlock(store, args.run, model, "cups")
        dev_raw = try_get(store, f"cups/reports/{args.a1}/predictions.parquet")
        ho_raw = try_get(store, f"cups/reports/{args.run}/predictions.parquet")
        if dev_raw is None or ho_raw is None:
            raise SystemExit("missing A1 predictions (dev or holdout)")
        dev_a1, ho_a1 = pl.read_parquet(io.BytesIO(dev_raw)), pl.read_parquet(io.BytesIO(ho_raw))
        if not ho_a1["season"].is_in(list(HOLDOUT)).all():
            raise SystemExit("the holdout run holds other seasons")
        with get_session() as s:
            d = load(s, (*SEASONS, *HOLDOUT))
            s.rollback()
        text, rep = judge(dev_a1, ho_a1, d, knockout.load_rules())
        verdict = {"passed": rep["passed"], "checks": rep["checks"]}
        store.put(f"cups/reports/{args.run}/stage_b_verdict.json", json.dumps(verdict).encode())
        print(text)
        return
    raw = try_get(store, f"cups/reports/{args.a1}/predictions.parquet")
    if raw is None:
        raise SystemExit("no A1 predictions")
    a1 = pl.read_parquet(io.BytesIO(raw))
    if a1["season"].is_in([2024, 2025]).any():
        raise SystemExit("A1 rows hold holdout seasons")
    rules = knockout.load_rules()
    with get_session() as s:
        d = load(s)
        s.rollback()
    bundle = set(a1["match_id"].to_list())
    r0 = b0(d, rules, bundle)
    by_id = {m.match_id: m for t in d.ties for m in t.matches}
    obs = []
    for row in a1.iter_rows(named=True):
        m = by_id.get(int(row["match_id"]))
        if m is None or m.status not in ("AET", "PEN") or None in m.et:
            continue
        t = next(t for t in d.ties if m in t.matches)
        rule = knockout.rule_for(rules, t.cup, t.season, t.round)
        if rule is None or not rule.extra_time or knockout.check(t, rule) is not None:
            continue
        lam, mu = np.asarray(row["draws_lam"]), np.asarray(row["draws_mu"])
        eh, ea = m.et[0], m.et[1]
        assert eh is not None and ea is not None
        if int(row["home"]) != m.home:  # A1 swapped the sides
            eh, ea = ea, eh
        obs.append(EtObs(m.match_id, m.kickoff, lam, mu, int(eh), int(ea)))
    r1 = b1(obs)
    r2 = b2(shootouts(d, rules), datetime(2019, 7, 1, tzinfo=UTC))
    df, r3 = b3(a1, d, rules, r1.get("fits", []), r1["use"], r2["fits"], r2["use"])
    print(markdown(r0, r1, r2, r3, df))
    cal, fits = recalibrate(df)
    r3c = gate(cal, Counter())
    print("## B3' (amendment 1) on dev, walk-forward; disclosed, not its judge\n")
    print(
        f"s at the last dev tie {np.exp(fits[-1][1]):.3f}; s range over the walk "
        f"{float(np.min(cal['s'].to_numpy())):.3f}-{float(np.max(cal['s'].to_numpy())):.3f}."
    )
    print(
        f"{r3c['n']} ties. Log loss {r3c['ll']:.4f} against the baseline {r3c['ll_base']:.4f}:"
        f" {r3c['diff']:+.5f} [{r3c['lo']:+.5f}, {r3c['hi']:+.5f}], {r3c['rel']:+.2%}; B3 "
        f"{r3['ll']:.4f}. ECE {r3c['ece']:.4f} (perfect calibration: mean "
        f"{r3c['ece_null_mean']:.4f}, 95th percentile {r3c['ece_null_95']:.4f})."
    )
    print("\n| p(A) bin | ties | mean p | observed |\n|---|---|---|---|")
    for k, n, mp, ob in r3c["reliability"]:
        print(f"| {k / 10:.1f}-{(k + 1) / 10:.1f} | {n} | {mp:.3f} | {ob:.3f} |")
    print(
        "\nBy cup: "
        + "; ".join(f"{CUP_NAME[c]} {v['rel']:+.2%}" for c, v in sorted(r3c["by_cup"].items()))
    )


if __name__ == "__main__":
    main()
