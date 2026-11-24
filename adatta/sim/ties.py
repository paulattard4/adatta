"""Stage D v2: national knockout ties outside a simulated tournament, such as Nations League
quarter-finals and play-offs (docs/BEYOND_LEAGUES_PLAN.md, "D v2 serving acceptance").

A tie's rule (legs, extra time, away goals) comes from `config/tournament_formats.yml`, under
`ties`, per competition, season range and round, with its source. A round without a written
rule is never forecast. Side A is the home side of the single match or of leg 1.

P(advance) per posterior draw, from the national model's score matrices (the same `Rates` as
the tournament simulator) and Stage B's fallbacks: extra-time goals Poisson(kappa x rate / 3)
with kappa = 1, and a coin-flip shoot-out.
- **Single match:** B3's closed form, as `eval.tournament.tie_probs`.
- **Between legs:** leg 1 fixed; Stage B's `two_legs` closed form on the leg-2 matrices.
- **Before leg 1:** the two legs' goal differences convolved (no away goals: only the
  aggregate difference matters), then the same extra time and shoot-out.

Each also gives the path: decided in normal time (or on aggregate), in extra time, or on
penalties, per side. Numpy and scipy only: it runs in the capture tick.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from numpy.typing import NDArray

from adatta.features import knockout
from adatta.sim import tournament as tm

Arr = NDArray[np.float64]
LAG = timedelta(hours=3)
FINAL = ("FT", "AET", "PEN")
HOW = ("90", "et", "pens")  # normal time (or the aggregate), extra time, penalties
LABEL = ("Team-only (Stage D v2): extra time at κ = 1 and a coin-flip shoot-out (Stage B "
         "fallbacks)")  # fmt: skip


@dataclass(frozen=True)
class Entry:
    first: int
    last: int
    rounds: tuple[str, ...]
    rule: knockout.Rule
    source: str


def load_rules(path: Path | str = tm.FORMATS_PATH) -> dict[str, list[Entry]]:
    """{league id: entries}. Only rules with no away goals are supported: every written
    national rule has none, and the before-leg-1 convolution relies on it."""
    raw = (yaml.safe_load(Path(path).read_text()) or {}).get("ties") or {}
    out: dict[str, list[Entry]] = {}
    for lg, entries in raw.items():
        out[str(lg)] = []
        for e in entries:
            rule = knockout.Rule(int(e["legs"]), False, bool(e["extra_time"]),
                                 str(e.get("away_goals", knockout.NONE)))  # fmt: skip
            if rule.legs not in (1, 2) or rule.away_goals != knockout.NONE:
                raise ValueError(f"ties {lg} {e['rounds']}: unsupported rule {rule}")
            if not str(e.get("source", "")).startswith("http"):
                raise ValueError(f"ties {lg} {e['rounds']}: no source")
            out[str(lg)].append(Entry(int(e["seasons"][0]), int(e["seasons"][1]),
                                      tuple(e["rounds"]), rule, str(e["source"])))  # fmt: skip
    return out


def rule_for(rules: Mapping[str, Sequence[Entry]], league_id: str, season: int,
             round_: str) -> knockout.Rule | None:  # fmt: skip
    for e in rules.get(str(league_id), ()):
        if e.first <= season <= e.last and any(fnmatchcase(round_ or "", p) for p in e.rounds):
            return e.rule
    return None


# ----------------------------------------------------------------------------------- ties


@dataclass
class Leg:
    match_id: int
    home: int
    away: int
    kickoff: datetime
    role: str  # the national model's host role (home, swapped, neutral, unknown)
    status: str
    nt: tuple[int, int] | None = None  # 90' (home, away)
    et: tuple[int, int] | None = None  # extra-time goals only
    pens: tuple[int, int] | None = None

    def final_by(self, t: datetime) -> bool:
        return self.status in FINAL and self.nt is not None and self.kickoff + LAG <= t


@dataclass
class Tie:
    league_id: str
    season: int
    round: str
    a: int
    b: int
    rule: knockout.Rule
    legs: list[Leg] = field(default_factory=list)


def group(rows: Sequence[tuple[str, int, str, Leg]], rules: Mapping[str, Sequence[Entry]]
          ) -> tuple[list[Tie], list[int]]:  # fmt: skip
    """Ties from (league id, season, round, leg) rows of rounds with a written rule, and the
    match ids that don't form a regular tie (a missing or extra leg, leg 2 not reversed)."""
    by: dict[tuple[str, int, str, frozenset[int]], list[Leg]] = {}
    for lg, season, rnd, leg in rows:
        if rule_for(rules, lg, season, rnd) is not None:
            by.setdefault((lg, season, rnd, frozenset((leg.home, leg.away))), []).append(leg)
    ties, irregular = [], []
    for (lg, season, rnd, _), legs in by.items():
        legs = sorted(legs, key=lambda x: (x.kickoff, x.match_id))
        rule = rule_for(rules, lg, season, rnd)
        assert rule is not None
        ok = len(legs) == rule.legs and (
            rule.legs == 1 or (legs[1].home, legs[1].away) == (legs[0].away, legs[0].home)
        )
        if not ok:
            irregular += [x.match_id for x in legs]
            continue
        ties.append(Tie(lg, season, rnd, legs[0].home, legs[0].away, rule, legs))
    ties.sort(key=lambda t: (t.legs[0].kickoff, t.legs[0].match_id))
    return ties, sorted(irregular)


def winner(t: Tie) -> tuple[int | None, str | None]:
    """(the side that went through, why the stored path doesn't fit the rule). None when a
    leg has no final 90' score."""
    if any(x.status not in FINAL or x.nt is None for x in t.legs):
        return None, None
    last = t.legs[-1]
    if t.rule.legs == 2:
        l1, l2 = t.legs
        assert l1.nt is not None and l2.nt is not None
        a, b = l1.nt[0] + l2.nt[1], l1.nt[1] + l2.nt[0]
        a_home_last = False
    else:
        assert last.nt is not None
        a, b = last.nt
        a_home_last = True
    level = a == b
    if level and t.rule.extra_time and last.status not in ("AET", "PEN"):
        return None, "level after 90' but no extra time stored"
    if not level and last.status != "FT":
        return None, f"decided in 90' but stored as {last.status}"
    if not level:
        return (t.a if a > b else t.b), None
    if last.et is not None:
        ea, eb = (last.et[0], last.et[1]) if a_home_last else (last.et[1], last.et[0])
        if ea != eb:
            if last.status != "AET":
                return None, f"decided in extra time but stored as {last.status}"
            return (t.a if ea > eb else t.b), None
    if last.status != "PEN" or last.pens is None or last.pens[0] == last.pens[1]:
        return None, "level after extra time but no shoot-out stored"
    pa, pb = (last.pens[0], last.pens[1]) if a_home_last else (last.pens[1], last.pens[0])
    return (t.a if pa > pb else t.b), None


# -------------------------------------------------------------------------- probabilities


@dataclass
class TieProb:
    """Per draw: P(side A advances), the baseline "level = coin flip", and the path."""

    a: Arr
    base: Arr
    path: dict[tuple[str, str], Arr]  # ("a" | "b", HOW) -> per-draw probability
    stage: str  # single | before_leg1 | between_legs


def _paths(a90: Arr, b90: Arr, level: Arr, lam_a: Arr, lam_b: Arr, extra_time: bool
           ) -> dict[tuple[str, str], Arr]:  # fmt: skip
    """The path from the probabilities after 90' (or the aggregate): extra time with A's and
    B's rates (lam_a, lam_b), then the coin-flip shoot-out."""
    from adatta.eval import knockout as kb

    if extra_time:
        ea, el, eb = kb.et_probs(lam_a, lam_b, tm.KAPPA_ET)
        a_et, b_et, pens = level * ea, level * eb, level * el
    else:
        a_et = b_et = np.zeros_like(level)
        pens = level
    return {
        ("a", "90"): a90,
        ("b", "90"): b90,
        ("a", "et"): a_et,
        ("b", "et"): b_et,
        ("a", "pens"): pens * tm.P_SHOOTOUT,
        ("b", "pens"): pens * (1 - tm.P_SHOOTOUT),
    }


def _prob(path: dict[tuple[str, str], Arr], base: Arr, stage: str) -> TieProb:
    return TieProb(sum((path[("a", h)] for h in HOW), np.zeros_like(base)), base, path, stage)


def single(m: Arr, lam: Arr, mu: Arr, extra_time: bool) -> TieProb:
    """A single match, A the listed home side with rate lam."""
    ph, pd = np.tril(m, -1).sum(axis=(-2, -1)), np.trace(m, axis1=-2, axis2=-1)
    pa = np.triu(m, 1).sum(axis=(-2, -1))
    return _prob(_paths(ph, pa, pd, lam, mu, extra_time), ph + 0.5 * pd, "single")


def _diff_pmf(m: Arr, sign: int) -> Arr:
    """(draws, 2n - 1) pmf of the goal difference row - column (sign 1) or column - row
    (sign -1), index n - 1 for a difference of zero."""
    n = m.shape[-1]
    return np.stack([np.trace(m, offset=-sign * k, axis1=-2, axis2=-1)
                     for k in range(-(n - 1), n)], axis=-1)  # fmt: skip


def _aggregate(d: Arr, mid: int) -> tuple[Arr, Arr, Arr]:
    """(A ahead, B ahead, level) from the pmf of A's aggregate difference."""
    return d[..., mid + 1 :].sum(-1), d[..., :mid].sum(-1), d[..., mid]


def between_legs(m2: Arr, x1: int, y1: int, lam2: Arr, mu2: Arr, extra_time: bool) -> TieProb:
    """Leg 1 ended x1-y1 with A at home; leg 2 has B at home, matrices m2 [B goals, A goals]
    with B's rate lam2 and A's mu2."""
    d2 = _diff_pmf(m2, -1)  # A's goal difference in leg 2: column - row
    n = m2.shape[-1]
    shift = x1 - y1
    d = np.zeros(d2.shape[:-1] + (2 * (2 * n - 1) - 1,))
    mid = 2 * (n - 1)
    d[..., mid - (n - 1) + shift : mid + n + shift] = d2
    ahead, behind, level = _aggregate(d, mid)
    return _prob(_paths(ahead, behind, level, mu2, lam2, extra_time), ahead + 0.5 * level,
                 "between_legs")  # fmt: skip


def before_leg1(m1: Arr, m2: Arr, lam2: Arr, mu2: Arr, extra_time: bool) -> TieProb:
    """Both legs to play: leg 1 matrices m1 [A goals, B goals], leg 2 m2 [B goals, A goals];
    the aggregate difference is the sum of the legs' (no away goals)."""
    d1 = _diff_pmf(m1, 1)  # A - B in leg 1
    d2 = _diff_pmf(m2, -1)  # A - B in leg 2
    w = d1.shape[-1]
    d = np.zeros(d1.shape[:-1] + (2 * w - 1,))
    for k in range(w):
        d[..., k : k + w] += d1[..., k : k + 1] * d2
    ahead, behind, level = _aggregate(d, w - 1)
    return _prob(_paths(ahead, behind, level, mu2, lam2, extra_time), ahead + 0.5 * level,
                 "before_leg1")  # fmt: skip


def leg_matrix(rt: tm.Rates, leg: Leg, month: int) -> tuple[Arr, Arr, Arr]:
    """(score matrices, home rate, away rate) per draw of one leg."""
    from adatta.model import national as nat

    k = len(rt.base)
    dd = np.arange(k)
    lam, mu = rt.lam_mu(dd, np.full(k, rt.index(leg.home)), np.full(k, rt.index(leg.away)),
                        month, leg.role)  # fmt: skip
    return nat.score_matrix(lam, mu, rt.phi, rt.rho), lam, mu


def tie_prob(rt: tm.Rates, t: Tie, months: Sequence[int], as_of: datetime) -> TieProb | None:
    """P(advance) as of `as_of`: the legs final by as_of - 3 h are fixed. None when the tie
    is decided (or its last leg has started)."""
    legs = t.legs
    if t.rule.legs == 1:
        if legs[0].final_by(as_of):
            return None
        m, lam, mu = leg_matrix(rt, legs[0], months[0])
        return single(m, lam, mu, t.rule.extra_time)
    l1, l2 = legs
    if l2.final_by(as_of):
        return None
    m2, lam2, mu2 = leg_matrix(rt, l2, months[1])
    if l1.final_by(as_of):
        assert l1.nt is not None
        return between_legs(m2, l1.nt[0], l1.nt[1], lam2, mu2, t.rule.extra_time)
    m1, _, _ = leg_matrix(rt, l1, months[0])
    return before_leg1(m1, m2, lam2, mu2, t.rule.extra_time)


def tie_rates(post: Mapping[str, Any], t: Tie, confs: Mapping[int, str], refit_month: int,
              walk_months: int, months: Sequence[int], comp_type: str) -> tm.Rates:  # fmt: skip
    """The refit's draws for the tie's two sides at its legs' months, the simulator's seed."""
    return tm.rates(post, [t.a, t.b], confs, refit_month, walk_months,
                    [max(m, refit_month) for m in months], comp_type,
                    np.random.default_rng(tm.SEED))  # fmt: skip


def summary(p: TieProb, interval: tuple[float, float] = (0.05, 0.95)) -> dict[str, Any]:
    """Posterior means with the 5-95% interval of P(advance), and the mean path."""
    lo, hi = np.quantile(p.a, interval)
    return {"a": float(p.a.mean()), "a_lo": float(lo), "a_hi": float(hi),
            "b": float(1 - p.a.mean()), "b_lo": float(1 - hi), "b_hi": float(1 - lo),
            "path": {f"{s}_{h}": float(v.mean()) for (s, h), v in p.path.items()},
            "stage": p.stage}  # fmt: skip


# --------------------------------------------------------------------------------- stored


def stored(session: Any, store: Any, rules: Mapping[str, Sequence[Entry]], lo: datetime,
           hi: datetime) -> tuple[list[Tie], list[int], dict[int, str]]:  # fmt: skip
    """The ties of rounds with a written rule whose matches kick off in [lo, hi), between two
    servable sides (others are irregular: a youth or unknown side never gets a forecast), the
    irregular match ids, and the sides' confederations. Scores only where stored."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.model import national as nat

    M, S, C, R = models.Match, models.Season, models.Competition, models.MatchResult
    rows = session.execute(
        select(M, R, C.source_ref, S.year).join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .outerjoin(R, (R.match_id == M.match_id) & R.superseded_at.is_(None))
        .where(C.source_ref.in_(list(rules)), M.kickoff_time >= lo, M.kickoff_time < hi)
    ).all()  # fmt: skip
    rows = [r for r in rows if rule_for(rules, str(r[2]), int(r[3]), r[0].round or "")]
    frame = nat.national_frame(session, store, lambda m, g: True, [m.match_id for m, *_ in rows])
    role = dict(zip(frame["match_id"].to_list(), frame["role"].to_list(), strict=True))
    confs = nat.sides(session)

    def pair(h: int | None, a: int | None) -> tuple[int, int] | None:
        return None if h is None or a is None else (int(h), int(a))

    legs, outside = [], []
    for m, r, lg, yr in rows:
        if m.match_id not in role:
            outside.append(int(m.match_id))
            continue
        kick = m.kickoff_time if m.kickoff_time.tzinfo else m.kickoff_time.replace(tzinfo=UTC)
        legs.append((str(lg), int(yr), str(m.round or ""), Leg(
            int(m.match_id), int(m.home_team_id), int(m.away_team_id), kick,
            str(role[m.match_id]), str(m.status),
            pair(r.home_goals_nt, r.away_goals_nt) if r is not None else None,
            pair(r.home_goals_et, r.away_goals_et) if r is not None else None,
            pair(r.home_pens, r.away_pens) if r is not None else None)))  # fmt: skip
    ties, irregular = group(legs, rules)
    return ties, sorted(irregular + outside), {t: confs[t] for t in confs}


# -------------------------------------------------------------------------------- serving

QUALIFY = "to_qualify"  # market_projection: selections home / away (the match's listed sides)
PATH = "qualify_path"  # selections home_90, home_et, home_pens, away_90, away_et, away_pens
NO_RULE = ("No written tie rule for this round: Stage D v2 serves knockout ties whose rules "
           "are sourced (the UEFA Nations League two-legged rounds so far).")  # fmt: skip
KNOCKOUT_WORDS = ("final", "play-off", "play-out", "quarter", "semi", "round of", "8th", "knockout",
                  "play-in")  # fmt: skip
SERVE_BACK = timedelta(days=60)  # a leg 2 inside the horizon finds its leg 1 this far back
SERVE_AHEAD = timedelta(days=120)


def rules_hash(path: Path | str = tm.FORMATS_PATH) -> str:
    import hashlib
    import json

    raw = (yaml.safe_load(Path(path).read_text()) or {}).get("ties") or {}
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()


def is_knockout_round(round_: str | None) -> bool:
    r = (round_ or "").lower()
    return any(w in r for w in KNOCKOUT_WORDS)


def serving_ties(session: Any, store: Any, match_ids: Sequence[int], now: datetime
                 ) -> tuple[dict[int, Tie], dict[int, str]]:  # fmt: skip
    """{match id: its tie} for the given upcoming matches that are legs of a regular tie with
    a written rule, and the sides' confederations."""
    rules = load_rules()
    if not rules or not match_ids:
        return {}, {}
    found, _, confs = stored(session, store, rules, now - SERVE_BACK, now + SERVE_AHEAD)
    want = set(match_ids)
    return {x.match_id: t for t in found for x in t.legs if x.match_id in want}, confs


def selections(p: TieProb, home_is_a: bool, interval: tuple[float, float] = (0.05, 0.95)
               ) -> dict[tuple[str, float | None, str], tuple[float, float, float]]:  # fmt: skip
    """market_projection selections for one match of the tie, relative to its listed sides:
    (posterior mean, 5th, 95th percentile) per selection."""
    side = {"a": "home" if home_is_a else "away", "b": "away" if home_is_a else "home"}

    def stat(x: Arr) -> tuple[float, float, float]:
        lo, hi = np.quantile(x, interval)
        return float(np.clip(x.mean(), 0, 1)), float(np.clip(lo, 0, 1)), float(np.clip(hi, 0, 1))

    out: dict[tuple[str, float | None, str], tuple[float, float, float]] = {
        (QUALIFY, None, side["a"]): stat(p.a),
        (QUALIFY, None, side["b"]): stat(1 - p.a),
    }
    for (s, how), x in p.path.items():
        out[(PATH, None, f"{side[s]}_{how}")] = stat(x)
    return out


def project(post: Mapping[str, Any], t: Tie, match_id: int, confs: Mapping[int, str],
            refit_as_of: datetime, walk_months: int, now: datetime
            ) -> dict[tuple[str, float | None, str], tuple[float, float, float]]:  # fmt: skip
    """The tie's selections for one of its upcoming matches as of now, from the refit's draws
    (the simulator's seed, so the same refit and state always give the same numbers)."""
    from adatta.model import national as nat

    m0 = nat.month_index(refit_as_of)
    months = [max(nat.month_index(x.kickoff), m0) for x in t.legs]
    rt = tie_rates(post, t, confs, m0, walk_months, months, nat.comp_type(t.league_id))
    p = tie_prob(rt, t, months, now)
    if p is None:
        return {}
    leg = next(x for x in t.legs if x.match_id == match_id)
    return selections(p, leg.home == t.a)
