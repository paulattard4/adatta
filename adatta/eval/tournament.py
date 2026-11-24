"""Stage D v2: rules replay, scoring and the gate of the tournament simulator
(docs/BEYOND_LEAGUES_PLAN.md, "D v2 acceptance"). No API requests and no fits: the served
model's cached refits (v1, friendlies 1.0) are read from the store, and a missing one stops
the run.

    python -m adatta.eval.tournament replay [--edition KEY ...]
    python -m adatta.eval.tournament run --edition KEY [--sims N] [--upload RUN]
    python -m adatta.eval.tournament assemble --fetch RUN

`run` writes per-team and per-tie rows to the store (match-level data stays out of logs) and
prints metrics only; `assemble` applies the gate to every edition of a run.
"""

from __future__ import annotations

import argparse
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from adatta.adapters.storage import RawStore, try_get
from adatta.sim import tournament as tm

LAG = timedelta(hours=3)
PREFIX = "tournament/v2"
ROUND_OF = {"round of 32": "R32", "round of 16": "R16", "8th finals": "R16",
            "quarter-finals": "QF", "semi-finals": "SF", "3rd place final": "3P",
            "3rd place": "3P", "final": "F"}  # fmt: skip
FINAL_STATUS = ("FT", "AET", "PEN")
ECE_BINS = 10
Arr = Any


def monday(t: datetime) -> datetime:
    return (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class Played:
    match_id: int
    home: int
    away: int
    kickoff: datetime
    status: str
    nt: tuple[int, int] | None
    et: tuple[int, int] | None
    pens: tuple[int, int] | None
    round: str  # group | R16 | QF | ...
    venue_country: str | None
    role: str

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUS


def winner(p: Played) -> int | None:
    """The side that went through: penalties, else the score after extra time; None for a
    walkover or a missing score (listed, never guessed)."""
    if not p.final:
        return None
    if p.pens is not None and p.pens[0] != p.pens[1]:
        return p.home if p.pens[0] > p.pens[1] else p.away
    if p.nt is None:
        return None
    h = p.nt[0] + (p.et[0] if p.et else 0)
    a = p.nt[1] + (p.et[1] if p.et else 0)
    return None if h == a else (p.home if h > a else p.away)


@dataclass
class EdData:
    ed: tm.Edition
    groups: dict[str, list[int]]  # published order, team ids
    group_matches: list[Played]
    ko: list[Played]
    confs: dict[int, str]
    country: dict[int, str | None]
    names: dict[int, str]


def _norm(x: str) -> str:
    import re
    import unicodedata

    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", x).encode("ascii", "ignore")
                  .decode().lower())  # fmt: skip


ALIASES = {_norm(a): _norm(b) for a, b in (
    ("USA", "United States"), ("Congo DR", "DR Congo"), ("Türkiye", "Turkey"),
    ("FYR Macedonia", "North Macedonia"), ("Cape Verde Islands", "Cape Verde"),
    ("St. Kitts and Nevis", "Saint Kitts and Nevis"),
    ("Bosnia & Herzegovina", "Bosnia and Herzegovina"))}  # fmt: skip


def load(session: Any, store: RawStore, ed: tm.Edition) -> EdData:
    """The edition's stored matches (its finals window), its groups as team ids and the
    teams' confederations and countries. A published team that can't be matched stops."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.features import host
    from adatta.model import national as nat

    M, S, C, R, V, T = (models.Match, models.Season, models.Competition, models.MatchResult,
                        models.Venue, models.Team)  # fmt: skip
    lo = datetime.fromisoformat(ed.start).replace(tzinfo=UTC) - timedelta(days=1)
    hi = datetime.fromisoformat(ed.end).replace(tzinfo=UTC) + timedelta(days=2)
    rows = session.execute(
        select(M, R).join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .outerjoin(R, (R.match_id == M.match_id) & R.superseded_at.is_(None))
        .where(C.source_ref == str(ed.league_id), S.year == ed.season, M.kickoff_time >= lo,
               M.kickoff_time <= hi)
    ).all()  # fmt: skip
    names = {int(t): str(n) for t, n in session.execute(select(T.team_id, T.name))}
    vids = {m.venue_id for m, _ in rows if m.venue_id is not None}
    vc = {
        int(i): (pc or gc)
        for i, pc, gc in session.execute(
            select(V.venue_id, V.provider_country, V.geo_country).where(V.venue_id.in_(vids))
        )
    }
    frame = nat.national_frame(session, store, lambda m, g: True, [m.match_id for m, _ in rows])
    role = dict(zip(frame["match_id"].to_list(), frame["role"].to_list(), strict=True))
    confs = nat.sides(session)
    played = []
    for m, r in rows:
        rnd = m.round or ""
        kind = "group" if rnd.lower().startswith("group") else ROUND_OF[rnd.lower()]

        def pair(h: int | None, a: int | None) -> tuple[int, int] | None:
            return None if h is None or a is None else (int(h), int(a))

        played.append(Played(
            int(m.match_id), int(m.home_team_id), int(m.away_team_id),
            m.kickoff_time.astimezone(UTC), str(m.status),
            pair(r.home_goals_nt, r.away_goals_nt) if r is not None else None,
            pair(r.home_goals_et, r.away_goals_et) if r is not None else None,
            pair(r.home_pens, r.away_pens) if r is not None else None,
            kind, vc.get(m.venue_id) if m.venue_id is not None else None,
            role.get(m.match_id, "unknown")))  # fmt: skip
    gm = [p for p in played if p.round == "group"]
    ids = {t for p in gm for t in (p.home, p.away)}
    groups: dict[str, list[int]] = {}
    for letter, pub in ed.groups.items():
        want = [_norm(x) for x in pub]
        got: dict[int, int] = {}
        for t in ids:
            n = ALIASES.get(_norm(names[t]), _norm(names[t]))
            if n in want:
                got[want.index(n)] = t
        if len(got) != len(pub):
            raise ValueError(f"{ed.key} group {letter}: matched {len(got)} of {pub}")
        groups[letter] = [got[i] for i in range(len(pub))]
    teams = [t for g in groups.values() for t in g]
    missing = [names[t] for t in teams if t not in confs]
    if missing:
        raise ValueError(f"{ed.key}: teams outside the served sides: {missing}")
    countries = host.Countries(host.stored_country_names(session, store), host.load_aliases())
    ko = sorted((p for p in played if p.round != "group"), key=lambda p: (p.kickoff, p.match_id))
    return EdData(ed, groups, sorted(gm, key=lambda p: (p.kickoff, p.match_id)), ko,
                  {t: confs[t] for t in teams}, {t: countries.of(names[t]) for t in teams},
                  names)  # fmt: skip


# ------------------------------------------------------------------------------ replay


@dataclass
class Replay:
    problems: list[str]
    random_groups: list[str]  # groups whose order needed the random step (fair play, lots)
    slot_match: dict[str, Played]  # knockout slot -> the match played there
    unresolved: list[int]  # knockout matches without a winner (walkover, missing score)


def replay(d: EdData) -> Replay:
    """The rules fed the actual results: each group's order against the published one (the
    same tied set where the random step decided), the qualifying thirds, every first-round
    pairing, and each later slot's pairing through the bracket."""
    ed = d.ed
    rng = np.random.default_rng(tm.SEED)
    problems: list[str] = []
    rand: list[str] = []
    games = [(p.home, p.away, p.nt[0], p.nt[1]) for p in d.group_matches if p.nt is not None]
    if len(games) != len(d.group_matches):
        problems.append(f"{len(d.group_matches) - len(games)} group matches without a 90' score")
    places: dict[str, list[int]] = {}
    thirds: list[tuple[str, list[int]]] = []
    for g, pub in d.groups.items():
        gg = [x for x in games if x[0] in pub and x[1] in pub]
        n = len(pub)
        if len(gg) != n * (n - 1) // 2:
            problems.append(f"group {g}: {len(gg)} matches")
        tab = tm.rank_group(pub, gg, ed.group_rank, rng)
        if tab.order != pub:
            if tab.random and sorted(tab.order) == sorted(pub) and _same_but_random(
                    pub, gg, ed.group_rank, tab.order):  # fmt: skip
                rand.append(g)
            else:
                problems.append(f"group {g}: rules give {[d.names[t] for t in tab.order]}, "
                                f"published {[d.names[t] for t in pub]}")  # fmt: skip
        elif tab.random:
            rand.append(g)
        places[g] = pub  # the published order goes on (the random step can't be replayed)
        if ed.thirds is not None:
            thirds.append((g, tm._stats(pub, gg)[pub[2]]))
    third_groups: list[str] = []
    if ed.thirds is not None:
        third_groups, r = tm.rank_thirds(thirds, ed.thirds.rank, rng)
        if r:
            rand.append("thirds")
    first = ed.rounds[0]
    want = tm.first_round(ed, places, third_groups)
    by_round: dict[str, list[Played]] = {}
    for p in d.ko:
        by_round.setdefault(p.round, []).append(p)
    slot_match: dict[str, Played] = {}
    real = by_round.get(first.name, [])
    for sid, (h, a) in want.items():
        hit = [p for p in real if {p.home, p.away} == {h, a}]
        if len(hit) != 1:
            if "thirds" in rand and any(tm.is_third(x) for x in first.matches[sid]):
                # a third's slot decided by fair play or lots: the played pairing stands
                hit = [p for p in real if h in (p.home, p.away) or a in (p.home, p.away)]
            if len(hit) != 1:
                problems.append(f"{sid}: rules give {d.names[h]} v {d.names[a]}, not played")
                continue
        slot_match[sid] = hit[0]
    if len(real) != len(first.matches):
        problems.append(f"{first.name}: {len(real)} matches played, {len(first.matches)} slots")
    unresolved = [p.match_id for p in d.ko if winner(p) is None]
    for rd in ed.rounds[1:]:
        real = by_round.get(rd.name, [])
        for sid, (x, y) in rd.matches.items():
            sides: list[int] = []
            for ref in (x, y):
                kind, _, src = ref.partition(":")
                fed = slot_match.get(src)
                w = winner(fed) if fed is not None else None
                if fed is None or w is None:
                    break
                sides.append(w if kind == "W" else (fed.away if w == fed.home else fed.home))
            if len(sides) < 2:
                problems.append(f"{sid}: a feeding match has no winner")
                continue
            hit = [q for q in real if {q.home, q.away} == set(sides)]
            if len(hit) != 1:
                problems.append(f"{sid}: bracket gives "
                                f"{' v '.join(d.names[t] for t in sides)}, not played")  # fmt: skip
                continue
            slot_match[sid] = hit[0]
    return Replay(problems, rand, slot_match, unresolved)


def _same_but_random(pub: list[int], games: list[tuple[int, int, int, int]],
                     criteria: Sequence[str], got: list[int]) -> bool:  # fmt: skip
    """True when the published order is one the random step can give: the rules, rerun with
    other seeds, reach it."""
    for seed in range(200):
        if tm.rank_group(pub, games, criteria, np.random.default_rng(seed)).order == pub:
            return True
    return False


# -------------------------------------------------------------------------- forecasting


def refit(store: RawStore, t: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    """The served model's cached refit at the latest Monday at or before t (never fitted
    here)."""
    from adatta.model import national as nat

    for back in range(0, 8):
        m = monday(t) - timedelta(days=7 * back)
        key = nat._cache_key(m, nat.FRIENDLY_WEIGHT, 1.0)
        raw, rawd = try_get(store, key), try_get(store, key + ".json")
        if raw is not None and rawd is not None:
            z = np.load(io.BytesIO(raw))
            diag = json.loads(rawd)
            if nat.sampler_failed(diag):
                continue  # a refit failing its gates is never used (as serving)
            post = {k: np.asarray(z[k]) for k in z.files}
            post["_as_of"] = np.array(m.timestamp())
            return post, diag
    raise SystemExit(f"no passing cached refit within 8 weeks before {t:%Y-%m-%d}")


def state_at(d: EdData, rp: Replay, t: datetime) -> tm.State:
    """What is final by t − 3 h: group results and knockout winners; the rest is
    simulated."""
    from adatta.model import national as nat

    fixtures: dict[str, list[tm.Fixture]] = {g: [] for g in d.groups}
    for p in d.group_matches:
        g = next(g for g, ts in d.groups.items() if p.home in ts)
        done = p.final and p.kickoff + LAG <= t and p.nt is not None
        fixtures[g].append(tm.Fixture(p.home, p.away, nat.month_index(p.kickoff), p.role,
                                      p.nt if done else None))  # fmt: skip
    slots = {}
    last_ko = max((p.kickoff for p in [*d.group_matches, *d.ko]), default=None)
    for rd in d.ed.rounds:
        for sid in rd.matches:
            q = rp.slot_match.get(sid)
            k = q.kickoff if q is not None else last_ko
            assert k is not None
            w = winner(q) if q is not None else None
            played = None
            if q is not None and w is not None and q.final and q.kickoff + LAG <= t:
                played = (q.home, q.away, w)
            slots[sid] = tm.Slot(nat.month_index(k), q.venue_country if q else None, played)
    finished = {g: list(d.groups[g]) for g, fx in fixtures.items()
                if all(f.score is not None for f in fx)}  # fmt: skip
    first = None
    if len(finished) == len(d.groups):
        first = {}
        for sid in d.ed.rounds[0].matches:
            p = rp.slot_match[sid]
            first[sid] = (p.home, p.away)
    return tm.State({g: list(v) for g, v in d.groups.items()}, fixtures, slots, d.country,
                    finished, first)  # fmt: skip


def rates_for(d: EdData, post: dict[str, Any], diag: dict[str, Any], st: tm.State,
              seed: int) -> tm.Rates:  # fmt: skip
    from adatta.model import national as nat

    as_of = datetime.fromtimestamp(float(post["_as_of"]), UTC)
    months = sorted({f.month for fx in st.fixtures.values() for f in fx}
                    | {s.month for s in st.slots.values()})  # fmt: skip
    m0 = nat.month_index(as_of)
    teams = [t for g in d.groups.values() for t in g]
    return tm.rates(post, teams, d.confs, m0, int(diag["n_months"]), [max(m, m0) for m in months],
                    "finals", np.random.default_rng(seed))  # fmt: skip


def _clamp_months(st: tm.State, m0: int) -> None:
    for fx in st.fixtures.values():
        for f in fx:
            f.month = max(f.month, m0)
    for s in st.slots.values():
        s.month = max(s.month, m0)


def forecast(store: RawStore, d: EdData, rp: Replay, t: datetime, n_sims: int,
             track: bool = False) -> tuple[tm.Result, tm.Rates, tm.State]:  # fmt: skip
    from adatta.model import national as nat

    post, diag = refit(store, t)
    st = state_at(d, rp, t)
    m0 = nat.month_index(datetime.fromtimestamp(float(post["_as_of"]), UTC))
    _clamp_months(st, m0)
    rt = rates_for(d, post, diag, st, tm.SEED)
    tr = ([(f.home, f.away) for fx in st.fixtures.values() for f in fx if f.score is None]
          if track else [])  # fmt: skip
    return tm.simulate(d.ed, st, rt, n_sims, tm.SEED, tr), rt, st


def closed_1x2(rt: tm.Rates, home: int, away: int, month: int, role: str) -> Arr:
    """(home, draw, away) averaged over draws from v1's score matrices, the same rates as the
    simulation."""
    from adatta.model import national as nat

    k = len(rt.base)
    dd = np.arange(k)
    lam, mu = rt.lam_mu(dd, np.full(k, rt.index(home)), np.full(k, rt.index(away)), month, role)
    m = nat.score_matrix(lam, mu, rt.phi, rt.rho)
    return np.asarray(nat.one_x_two(m).mean(axis=0))


def tie_probs(rt: tm.Rates, p: Played, month: int, extra_time: bool) -> tuple[float, float]:
    """(P(listed home goes through), baseline "level = coin flip") for a played knockout
    match, from its own Monday's refit: B3 with kappa = 1 and a coin-flip shoot-out."""
    from adatta.eval import knockout as kb
    from adatta.model import national as nat

    k = len(rt.base)
    dd = np.arange(k)
    lam, mu = rt.lam_mu(dd, np.full(k, rt.index(p.home)), np.full(k, rt.index(p.away)), month,
                        "neutral" if p.role == "unknown" else p.role)  # fmt: skip
    m = nat.score_matrix(lam, mu, rt.phi, rt.rho)
    ph, pd = np.tril(m, -1).sum(axis=(-2, -1)), np.trace(m, axis1=-2, axis2=-1)
    base = ph + 0.5 * pd
    if extra_time:
        eh, ed_, _ = kb.et_probs(lam, mu, tm.KAPPA_ET)
        prob = ph + pd * (eh + ed_ * tm.P_SHOOTOUT)
    else:
        prob = ph + pd * tm.P_SHOOTOUT
    return float(prob.mean()), float(base.mean())


# ------------------------------------------------------------------------------- scoring


MAIN = ("R32", "R16", "QF", "SF", "F")


def stage_reached(d: EdData, rp: Replay, team: int) -> int:
    """0 = out in the group, j = reached the j-th main knockout round, last + 1 = won."""
    rounds = [r.name for r in d.ed.rounds if r.name in MAIN]
    got = 0
    for j, rn in enumerate(rounds, start=1):
        if any(team in (rp.slot_match[s].home, rp.slot_match[s].away)
               for s in next(r for r in d.ed.rounds if r.name == rn).matches
               if s in rp.slot_match):  # fmt: skip
            got = j
    f = rp.slot_match.get("F")
    if f is not None and winner(f) == team:
        got = len(rounds) + 1
    return got


def rps(cum_pred: Arr, outcome: int) -> float:
    """cum_pred[j] = P(stage >= j + 1) for j = 0..K-2 (K ordered stages)."""
    k = len(cum_pred) + 1
    obs = np.array([1.0 if outcome >= j + 1 else 0.0 for j in range(k - 1)])
    return float(((cum_pred - obs) ** 2).sum() / (k - 1))


def team_rows(d: EdData, rp: Replay, res: tm.Result, t: datetime, label: str) -> pl.DataFrame:
    rounds = [r.name for r in d.ed.rounds if r.name in MAIN]
    first = d.ed.rounds[0].name
    adv = set()
    for s in d.ed.rounds[0].matches:
        p = rp.slot_match.get(s)
        if p is not None:
            adv |= {p.home, p.away}
    qf = set()
    if "QF" in [r.name for r in d.ed.rounds]:
        for s in next(r for r in d.ed.rounds if r.name == "QF").matches:
            p = rp.slot_match.get(s)
            if p is not None:
                qf |= {p.home, p.away}
    out = []
    n = len(res.teams)
    for i, team in enumerate(res.teams):
        cum = np.array([res.reach[r][i] for r in rounds] + [res.win[i]])
        stage = stage_reached(d, rp, team)
        ref_cum = np.array([2 * len(next(r for r in d.ed.rounds if r.name == rn).matches) / n
                            for rn in rounds] + [1 / n])  # fmt: skip
        out.append({
            "edition": d.ed.key, "window": d.ed.window, "as_of": t, "point": label, "team": team,
            "p_advance": float(res.advance[i]), "advanced": float(team in adv),
            "p_qf": float(res.reach["QF"][i]) if "QF" in res.reach else float(res.advance[i]),
            "reached_qf": float(team in qf) if qf else float(team in adv),
            "p_win": float(res.win[i]), "stage": stage, "rps": rps(cum, stage),
            "rps_ref": rps(ref_cum, stage),
            "p_advance_ref": 2 * len(d.ed.rounds[0].matches) / n, "first_round": first,
        })  # fmt: skip
    return pl.DataFrame(out)


def coherence(d: EdData, res: tm.Result, rt: tm.Rates, st: tm.State) -> list[str]:
    """Check 2: probabilities that must add up do, and each tracked group fixture's simulated
    1X2 is within 3 Monte Carlo standard errors of the model's."""
    bad = []
    n_adv = d.ed.advancing()
    if not np.allclose(res.position.sum(axis=1), 1.0):
        bad.append("a team's position probabilities don't sum to 1")
    for g, ts in d.groups.items():
        idx = [res.teams.index(t) for t in ts]
        if not np.allclose(res.position[idx, : len(ts)].sum(axis=0), 1.0):
            bad.append(f"group {g}: a position's probabilities don't sum to 1")
    if abs(res.advance.sum() - n_adv) > 1e-9:
        bad.append(f"advance sums to {res.advance.sum():.6f}, not {n_adv}")
    if abs(res.win.sum() - 1) > 1e-9:
        bad.append(f"P(win) sums to {res.win.sum():.6f}")
    for r in d.ed.rounds:
        if abs(res.reach[r.name].sum() - 2 * len(r.matches)) > 1e-9:
            bad.append(f"{r.name}: reach sums to {res.reach[r.name].sum():.4f}")
    for (h, a), sim in res.match_1x2.items():
        f = next(f for fx in st.fixtures.values() for f in fx if (f.home, f.away) == (h, a))
        p = closed_1x2(rt, h, a, f.month, f.role)
        se = np.sqrt(p * (1 - p) / res.n_sims)
        if (np.abs(sim - p) > 3 * se + 1e-12).any():
            bad.append(f"{h} v {a}: simulated 1X2 {np.round(sim, 4)} against {np.round(p, 4)}")
    return bad


def tie_coherence(rt: tm.Rates, p: Played, month: int, extra_time: bool, closed: float,
                  n_sims: int) -> list[str]:  # fmt: skip
    """A knockout tie simulated as the simulator plays it, against B3's closed form."""
    k = len(rt.base)
    d = np.arange(n_sims) % k
    role = np.full(n_sims, "neutral" if p.role == "unknown" else p.role)
    win = tm._knockout(rt, d, np.full(n_sims, rt.index(p.home)), np.full(n_sims, rt.index(p.away)),
                       month, role, extra_time, np.random.default_rng(tm.SEED))  # fmt: skip
    se = np.sqrt(closed * (1 - closed) / n_sims)
    sim = float(win.mean())
    return (
        []
        if abs(sim - closed) <= 3 * se
        else [f"tie {p.match_id}: simulated P(advance) {sim:.4f} against closed form {closed:.4f}"]
    )


def run_edition(session: Any, store: RawStore, key: str, n_sims: int = tm.N_SIMS
                ) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, Any]]:  # fmt: skip
    """Replay, the pre-tournament forecast (coherence checked), the in-tournament Mondays
    and every played knockout tie. Returns (team rows, tie rows, diagnostics)."""
    from adatta.model import national as nat

    ed = tm.load_formats()[key]
    d = load(session, store, ed)
    rp = replay(d)
    start = min(p.kickoff for p in d.group_matches)
    t0 = monday(start)
    res, rt, st = forecast(store, d, rp, t0, n_sims, track=True)
    coh = coherence(d, res, rt, st)
    teams = [team_rows(d, rp, res, t0, "pre")]
    end_groups = max(p.kickoff for p in d.group_matches)
    t = t0 + timedelta(days=7)
    last = max(p.kickoff for p in d.ko)
    while t <= last:
        r2, _, _ = forecast(store, d, rp, t, n_sims)
        teams.append(team_rows(d, rp, r2, t, "group" if t <= end_groups else "knockout"))
        t += timedelta(days=7)
    ties: list[dict[str, Any]] = []
    et_of = {sid: r.extra_time for r in ed.rounds for sid in r.matches}
    slot_of = {p.match_id: sid for sid, p in rp.slot_match.items()}
    for p in d.ko:
        w = winner(p)
        slot = slot_of.get(p.match_id)
        if w is None or slot is None:
            continue
        sid = slot
        post, diag = refit(store, p.kickoff)
        m0 = nat.month_index(datetime.fromtimestamp(float(post["_as_of"]), UTC))
        month = max(nat.month_index(p.kickoff), m0)
        rt2 = tm.rates(post, [p.home, p.away], d.confs, m0, int(diag["n_months"]), [month],
                       "finals", np.random.default_rng(tm.SEED))  # fmt: skip
        prob, base = tie_probs(rt2, p, month, et_of[sid])
        if not ties:  # check 2: the simulated tie against its closed form
            coh += tie_coherence(rt2, p, month, et_of[sid], prob, n_sims)
        ties.append({"edition": key, "window": ed.window, "match_id": p.match_id, "slot": sid,
                     "round": p.round, "as_of": monday(p.kickoff), "p": prob, "base": base,
                     "y": float(w == p.home), "extra_time": et_of[sid],
                     "level_90": float(p.nt is not None and p.nt[0] == p.nt[1])})  # fmt: skip
    diag_out = {
        "edition": key,
        "window": ed.window,
        "replay_problems": rp.problems,
        "replay_random": rp.random_groups,
        "unresolved": rp.unresolved,
        "coherence": coh,
        "random_groups": res.random_groups,
        "random_thirds": res.random_thirds,
        "n_sims": n_sims,
        "teams": len(res.teams),
        "ties": len(ties),
        "mondays": len(teams),
        "leakage": leakage(d, rp, t0),
    }
    return pl.concat(teams), pl.DataFrame(ties), diag_out


def leakage(d: EdData, rp: Replay, t: datetime) -> list[str]:
    """Check 5 at run time: a state as of t fixes nothing final after t − 3 h, and changing
    every later result leaves it unchanged."""
    st = state_at(d, rp, t)
    bad = []
    for fx in st.fixtures.values():
        for f in fx:
            p = next(p for p in d.group_matches if (p.home, p.away) == (f.home, f.away))
            if f.score is not None and p.kickoff + LAG > t:
                bad.append(f"group match {p.match_id} fixed before it was final")
    for sid, sl in st.slots.items():
        q = rp.slot_match.get(sid)
        if sl.played is not None and q is not None and q.kickoff + LAG > t:
            bad.append(f"{sid} fixed before it was final")
    later = [p for p in d.group_matches if p.kickoff + LAG > t]
    saved = [(p, p.nt) for p in later]
    for p in later:
        p.nt = (9, 0)
    st2 = state_at(d, rp, t)
    for p, nt in saved:
        p.nt = nt
    if [[f.score for f in fx] for fx in st.fixtures.values()] != [
            [f.score for f in fx] for fx in st2.fixtures.values()]:  # fmt: skip
        bad.append("a later result changed the state")
    return bad


# ---------------------------------------------------------------------------------- gate


def _ece(p: Arr, y: Arr) -> tuple[float, float]:
    from adatta.eval import knockout as kb

    return kb.ece(p, y, ECE_BINS), kb.ece_null(p)


def _ll(p: Arr, y: Arr) -> Arr:
    q = np.clip(p, 1e-12, 1 - 1e-12)
    return np.asarray(-(y * np.log(q) + (1 - y) * np.log(1 - q)))


def gate(teams: pl.DataFrame, ties: pl.DataFrame, diags: list[dict[str, Any]],
         window: str) -> dict[str, Any]:  # fmt: skip
    """The acceptance's checks 1-5 on one window's editions (dev, or the opened 2024-2026)."""
    tw = teams.filter((pl.col("window") == window) & (pl.col("point") == "pre"))
    kw = ties.filter(pl.col("window") == window)
    dw = [x for x in diags if x["window"] == window]
    c1 = all(not x["replay_problems"] for x in dw)
    c2 = all(not x["coherence"] for x in dw)
    pa, ya = tw["p_advance"].to_numpy(), tw["advanced"].to_numpy()
    pq, yq = tw["p_qf"].to_numpy(), tw["reached_qf"].to_numpy()
    ece_a, null_a = _ece(pa, ya)
    ece_q, null_q = _ece(pq, yq)
    c3 = ece_a <= null_a and ece_q <= null_q
    p, b, y = kw["p"].to_numpy(), kw["base"].to_numpy(), kw["y"].to_numpy()
    diff = _ll(p, y) - _ll(b, y)
    se = diff.std(ddof=1) / np.sqrt(len(diff)) if len(diff) > 1 else float("nan")
    ece_k, null_k = _ece(p, y)
    c4 = float(diff.mean()) <= 0 and ece_k <= null_k
    c5 = all(not x["leakage"] for x in dw)
    rps_m = float(np.mean(tw["rps"].to_numpy()))
    rps_ref = float(np.mean(tw["rps_ref"].to_numpy()))
    by_ed = (tw.group_by("edition").agg(pl.len().alias("n"), pl.col("rps").mean(),
                                        pl.col("rps_ref").mean()).sort("edition"))  # fmt: skip
    inside = teams.filter((pl.col("window") == window) & (pl.col("point") != "pre"))
    return {
        "window": window,
        "editions": len(dw),
        "team_entries": tw.height,
        "ties": kw.height,
        "checks": {
            "1_replay": c1,
            "2_coherence": c2,
            "3_calibration": c3,
            "4_knockout": c4,
            "5_leakage": c5,
        },  # fmt: skip
        "passes": c1 and c2 and c3 and c4 and c5,
        "replay_problems": {x["edition"]: x["replay_problems"] for x in dw if x["replay_problems"]},
        "replay_random": {x["edition"]: x["replay_random"] for x in dw if x["replay_random"]},
        "unresolved": {x["edition"]: x["unresolved"] for x in dw if x["unresolved"]},
        "coherence": {x["edition"]: x["coherence"] for x in dw if x["coherence"]},
        "ece_advance": [ece_a, null_a],
        "ece_qf": [ece_q, null_q],
        "ko_logloss": [float(_ll(p, y).mean()), float(_ll(b, y).mean())],
        "ko_diff": [
            float(diff.mean()),
            float(diff.mean() - 1.96 * se),
            float(diff.mean() + 1.96 * se),
        ],  # fmt: skip
        "ko_ece": [ece_k, null_k],
        "ko_level_90": int(kw["level_90"].sum()),
        "brier_advance": float(((pa - ya) ** 2).mean()),
        "brier_advance_ref": float(((tw["p_advance_ref"].to_numpy() - ya) ** 2).mean()),
        "rps_stage": [rps_m, rps_ref],
        "rps_by_edition": by_ed.to_dicts(),
        "in_tournament_rps": float(np.mean(inside["rps"].to_numpy())) if inside.height else None,
        "random_groups": float(np.mean([x["random_groups"] for x in dw])),
        "random_thirds": float(np.mean([x["random_thirds"] for x in dw])),
    }


def markdown(v: dict[str, Any]) -> str:
    c = v["checks"]
    lines = [f"## D v2, {v['window']}: {'PASSES' if v['passes'] else 'FAILS'}", ""]
    lines += [f"- check {k}: {'pass' if ok else 'FAIL'}" for k, ok in c.items()]
    lines += ["", f"{v['editions']} editions, {v['team_entries']} team-entries, {v['ties']} ties",
              f"- P(advance) ECE {v['ece_advance'][0]:.4f} (noise floor "
              f"{v['ece_advance'][1]:.4f}); P(QF) ECE {v['ece_qf'][0]:.4f} ({v['ece_qf'][1]:.4f})",
              f"- knockout log loss {v['ko_logloss'][0]:.4f} against {v['ko_logloss'][1]:.4f}: "
              f"{v['ko_diff'][0]:+.4f} [{v['ko_diff'][1]:+.4f}, {v['ko_diff'][2]:+.4f}]; ECE "
              f"{v['ko_ece'][0]:.4f} ({v['ko_ece'][1]:.4f}); {v['ko_level_90']} level after 90'",
              f"- Brier P(advance) {v['brier_advance']:.4f} (equal chances "
              f"{v['brier_advance_ref']:.4f}); RPS of stage reached {v['rps_stage'][0]:.4f} "
              f"(equal chances {v['rps_stage'][1]:.4f})",
              f"- random step: {v['random_groups']:.3%} of simulated group tables, "
              f"{v['random_thirds']:.3%} of third rankings"]  # fmt: skip
    for k in ("replay_problems", "replay_random", "unresolved", "coherence"):
        if v[k]:
            lines.append(f"- {k}: {json.dumps(v[k])}")
    lines.append("- RPS by edition: " + "; ".join(
        f"{r['edition']} {r['rps']:.4f} ({r['rps_ref']:.4f}, n {r['n']})"
        for r in v["rps_by_edition"]))  # fmt: skip
    return "\n".join(lines)


# ----------------------------------------------------------------------------------- CLI


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf)
    return buf.getvalue()


def main(argv: Sequence[str] | None = None) -> None:
    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session

    ap = argparse.ArgumentParser(prog="python -m adatta.eval.tournament")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s0 = sub.add_parser("replay")
    s0.add_argument("--edition", nargs="*")
    s1 = sub.add_parser("run")
    s1.add_argument("--edition", required=True)
    s1.add_argument("--sims", type=int, default=tm.N_SIMS)
    s1.add_argument("--upload", default="")
    s2 = sub.add_parser("assemble")
    s2.add_argument("--fetch", required=True)
    s2.add_argument("--edition", nargs="*")
    args = ap.parse_args(argv)
    store = make_store(get_settings())
    eds = tm.load_formats()
    if args.cmd == "replay":
        session = get_session()
        for key in args.edition or list(eds):
            d = load(session, store, eds[key])
            rp = replay(d)
            print(f"{key}: {len(rp.problems)} problems; random {rp.random_groups}; "
                  f"unresolved {len(rp.unresolved)}; slots {len(rp.slot_match)}")  # fmt: skip
            for pr in rp.problems:
                print("   ", pr)
        return
    if args.cmd == "run":
        session = get_session()
        teams, ties, diag = run_edition(session, store, args.edition, args.sims)
        if args.upload:
            base = f"{PREFIX}/{args.upload}/{args.edition}"
            store.put(base + "_teams.parquet", _parquet(teams))
            store.put(base + "_ties.parquet", _parquet(ties))
            store.put(base + ".json", json.dumps(diag, default=str).encode())
        print(json.dumps({k: v for k, v in diag.items()}, default=str))
        return
    teams_l, ties_l, diags = [], [], []
    for key in args.edition or list(eds):
        base = f"{PREFIX}/{args.fetch}/{key}"
        raw = try_get(store, base + ".json")
        if raw is None:
            raise SystemExit(f"missing part {base}")
        diags.append(json.loads(raw))
        teams_l.append(pl.read_parquet(io.BytesIO(store.get(base + "_teams.parquet"))))
        ties_l.append(pl.read_parquet(io.BytesIO(store.get(base + "_ties.parquet"))))
    teams, ties = pl.concat(teams_l, how="diagonal"), pl.concat(ties_l, how="diagonal")
    out = {}
    for window in ("dev", "opened"):
        if any(x["window"] == window for x in diags):
            out[window] = gate(teams, ties, diags, window)
            print(markdown(out[window]))
            print()
    store.put(f"{PREFIX}/{args.fetch}/verdict.json", json.dumps(out, default=str).encode())


if __name__ == "__main__":
    main()
