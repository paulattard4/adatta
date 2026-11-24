"""Stage D v2: the tournament simulator (docs/BEYOND_LEAGUES_PLAN.md, "D v2 acceptance").

The format rules of each edition are in `config/tournament_formats.yml`: groups (published
order), the ranking criteria after points, the best-third ranking and its allocation table,
and the bracket. This module holds the rules engine (group tables, best thirds, slots) and
the simulator. Nothing in it is fitted.

A simulated tournament takes one posterior draw of the served national refit (§9.5). It
draws full scores from v1's negative binomial with Dixon-Coles (`national.nb_dc_sample`), so
goal difference and goals feed the tiebreakers. A knockout level after 90' plays extra time
(Poisson goals at kappa = 1 x the 90' rate / 3, Stage B's B1 fallback) where its round has
extra time, then a coin-flip shoot-out (B2's fallback). What the data can't decide (fair play,
rankings, lots, a last-round shoot-out) is drawn at random, and counted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import numpy as np
import yaml
from numpy.typing import NDArray

FORMATS_PATH = Path("config/tournament_formats.yml")
N_SIMS = 20_000  # simulated tournaments per forecast: 50 per kept draw (400)
KAPPA_ET = 1.0  # extra-time rate = kappa x 90' rate x 30/90 (Stage B, B1 fell back to 1)
P_SHOOTOUT = 0.5  # Stage B, B2 fell back to a coin flip
SEED = 20261124
GROUP_KEYS = ("pts", "gd", "gf", "wins")
CRITERIA = frozenset({"h2h", "h2h_pts", "h2h_gd", "h2h_gf", "gd", "gf", "wins", "random"})
THIRD_CRITERIA = frozenset({"pts", "gd", "gf", "wins", "random"})

Arr = NDArray[np.float64]
Ints = NDArray[np.int64]


# ------------------------------------------------------------------------------- formats


@dataclass(frozen=True)
class Round:
    name: str  # R32, R16, QF, SF, 3P, F
    basis: str  # stated | derived
    extra_time: bool
    matches: dict[str, tuple[str, str]]  # slot -> (listed home, listed away) references


@dataclass(frozen=True)
class Thirds:
    count: int
    rank: tuple[str, ...]
    columns: tuple[str, ...]  # the group winners ("1B") that meet a third-placed team
    table: dict[str, str]  # qualifying groups ("ABCD") -> the third's group per column


@dataclass(frozen=True)
class Edition:
    key: str
    name: str
    league_id: int
    season: int
    start: str
    end: str
    window: str  # dev | opened
    source: str
    groups: dict[str, tuple[str, ...]]  # letter -> team names, published final order
    group_rank: tuple[str, ...]
    thirds: Thirds | None
    rounds: tuple[Round, ...]

    @property
    def letters(self) -> tuple[str, ...]:
        return tuple(sorted(self.groups))

    def advancing(self) -> int:
        """Teams through to the first knockout round."""
        return 2 * len(self.rounds[0].matches)


def load_formats(path: Path | str = FORMATS_PATH) -> dict[str, Edition]:
    raw = yaml.safe_load(Path(path).read_text())["editions"]
    out = {}
    for key, e in raw.items():
        th = e.get("thirds")
        thirds = None if th is None else Thirds(
            int(th["count"]), tuple(th["rank"]), tuple(th["columns"]),
            {str(k): str(v) for k, v in th["table"].items()})  # fmt: skip
        rounds = tuple(
            Round(str(r["round"]), str(r["basis"]), bool(r["extra_time"]),
                  {str(k): (str(v[0]), str(v[1])) for k, v in r["matches"].items()})
            for r in e["rounds"]
        )  # fmt: skip
        ed = Edition(key, str(e["name"]), int(e["league_id"]), int(e["season"]), str(e["start"]),
                     str(e["end"]), str(e["window"]), str(e["source"]),
                     {str(k): tuple(v) for k, v in e["groups"].items()}, tuple(e["group_rank"]),
                     thirds, rounds)  # fmt: skip
        validate(ed)
        out[key] = ed
    return out


def is_third(ref: str) -> bool:
    """A best-third slot ("3ABCD": the third of one of these groups, by the table); "3A" is
    the third of group A as a fixed place."""
    return ref[0] == "3" and len(ref) > 2


def validate(ed: Edition) -> None:
    """Structural checks: known criteria, a thirds table that covers every combination with
    each column's third from the allowed groups, and a bracket whose references resolve."""
    bad = set(ed.group_rank) - CRITERIA
    if bad or ed.group_rank[-1] != "random":
        raise ValueError(f"{ed.key}: group_rank {ed.group_rank}")
    letters = ed.letters
    slots: dict[str, str] = {}
    for r in ed.rounds[:1]:
        for sid, refs in r.matches.items():
            for ref in refs:
                if is_third(ref):
                    if ed.thirds is None or not set(ref[1:]) <= set(letters):
                        raise ValueError(f"{ed.key}: {sid} {ref}")
                    slots[ref] = sid
                elif not (ref[0] in "12345" and ref[1:] in letters):
                    raise ValueError(f"{ed.key}: {sid} {ref}")
    if ed.thirds is not None:
        th = ed.thirds
        if set(th.rank) - THIRD_CRITERIA or th.rank[-1] != "random":
            raise ValueError(f"{ed.key}: thirds rank {th.rank}")
        combos = {"".join(c) for c in combinations(letters, th.count)}
        if set(th.table) != combos:
            raise ValueError(f"{ed.key}: thirds table covers {len(th.table)} of {len(combos)}")
        allowed = {}
        for refs in ed.rounds[0].matches.values():
            for x, y in (refs, refs[::-1]):
                if is_third(y):
                    allowed[x] = set(y[1:])
        for combo, row in th.table.items():
            if sorted(row) != sorted(combo):
                raise ValueError(f"{ed.key}: thirds row {combo} -> {row}")
            for col, g in zip(th.columns, row, strict=True):
                if g not in allowed.get(col, set()):
                    raise ValueError(f"{ed.key}: thirds row {combo}: {col} v 3{g} not allowed")
    seen = set(ed.rounds[0].matches)
    for r in ed.rounds[1:]:
        for sid, refs in r.matches.items():
            for ref in refs:
                kind, _, src = ref.partition(":")
                if kind not in ("W", "L") or src not in seen:
                    raise ValueError(f"{ed.key}: {sid} {ref}")
        seen |= set(r.matches)


# ----------------------------------------------------------------------- group tables


@dataclass
class Table:
    """A group's order and how far down the criteria it had to go."""

    order: list[int]
    random: bool = False  # some places were drawn at random


def _stats(teams: Sequence[int],
           games: Sequence[tuple[int, int, int, int]]) -> dict[int, list[int]]:  # fmt: skip
    """team -> [pts, gd, gf, wins] over the games among `teams`."""
    st = {t: [0, 0, 0, 0] for t in teams}
    for h, a, x, y in games:
        if h in st and a in st:
            st[h][0] += 3 if x > y else 1 if x == y else 0
            st[a][0] += 3 if y > x else 1 if x == y else 0
            st[h][1] += x - y
            st[a][1] += y - x
            st[h][2] += x
            st[a][2] += y
            st[h][3] += x > y
            st[a][3] += y > x
    return st


def rank_group(teams: Sequence[int], games: Sequence[tuple[int, int, int, int]],
               criteria: Sequence[str], rng: np.random.Generator) -> Table:  # fmt: skip
    """The group's order: points, then `criteria` (see the config's header)."""
    overall = _stats(teams, games)
    tab = Table([])

    def by(vals: Mapping[int, float], tied: list[int]) -> list[list[int]]:
        out: dict[float, list[int]] = {}
        for t in tied:
            out.setdefault(-vals[t], []).append(t)
        return [out[k] for k in sorted(out)]

    def order(tied: list[int], i: int, h2h_set: list[int] | None) -> list[int]:
        if len(tied) == 1:
            return tied
        c = criteria[i]
        if c == "random":
            tab.random = True
            return [tied[j] for j in rng.permutation(len(tied))]
        if c == "h2h":
            return h2h_block(tied, i)
        if c in ("gd", "gf", "wins"):
            k = GROUP_KEYS.index(c)
            vals = {t: overall[t][k] for t in tied}
            nxt = None
        else:  # h2h_pts / h2h_gd / h2h_gf among the teams level when the first was reached
            basis = h2h_set if h2h_set is not None else tied
            hs = _stats(basis, games)
            k = ("h2h_pts", "h2h_gd", "h2h_gf").index(c)
            vals = {t: hs[t][k] for t in tied}
            nxt = basis
        return [t for part in by(vals, tied) for t in order(part, i + 1, nxt)]

    def h2h_block(tied: list[int], i: int) -> list[int]:
        """Head-to-head points, goal difference and goals among `tied`; a subset still level
        has them re-applied among itself; a set they can't split goes on to criterion i + 1."""
        hs = _stats(tied, games)
        parts: list[list[int]] = [tied]
        for k in range(3):
            parts = [p for part in parts for p in by({t: hs[t][k] for t in part}, part)]
        out = []
        for part in parts:
            if len(part) == 1:
                out += part
            elif len(part) < len(tied):
                out += h2h_block(part, i)
            else:
                out += order(part, i + 1, None)
        return out

    pts = {t: overall[t][0] for t in teams}
    tab.order = [t for part in by(pts, list(teams)) for t in order(part, 0, None)]
    return tab


def rank_thirds(rows: Sequence[tuple[str, list[int]]], criteria: Sequence[str],
                rng: np.random.Generator) -> tuple[list[str], bool]:  # fmt: skip
    """Groups ordered by their third-placed team: rows are (letter, [pts, gd, gf, wins])."""
    keys = []
    for letter, st in rows:
        k = tuple(-st[GROUP_KEYS.index(c)] for c in criteria if c != "random")
        keys.append((k, rng.random(), letter))
    keys.sort()
    rand = any(keys[j][0] == keys[j + 1][0] for j in range(len(keys) - 1))
    return [k[2] for k in keys], rand


def first_round(ed: Edition, places: Mapping[str, Sequence[int]],
                third_groups: Sequence[str]) -> dict[str, tuple[int, int]]:  # fmt: skip
    """The first knockout round's slots -> (listed home, listed away) teams, from the group
    places (letter -> teams in order) and the qualifying thirds' groups (best first)."""
    alloc: dict[str, str] = {}
    if ed.thirds is not None:
        combo = "".join(sorted(third_groups[: ed.thirds.count]))
        alloc = dict(zip(ed.thirds.columns, ed.thirds.table[combo], strict=True))
    out = {}
    for sid, (x, y) in ed.rounds[0].matches.items():
        pair = []
        for ref, other in ((x, y), (y, x)):
            if is_third(ref):
                pair.append(places[alloc[other]][2])
            else:
                pair.append(places[ref[1]][int(ref[0]) - 1])
        out[sid] = (pair[0], pair[1])
    return out


# ------------------------------------------------------------------------- simulation


@dataclass
class Rates:
    """Per-draw scoring rates for any pairing: log-rate pieces at the tournament's months.

    strength[d, i], openness[d, i] per draw d and team index i (the walk carried to the
    month, one innovation per draw, team and month so a team's matches agree), base[d] =
    c + g[finals], h[d] home term, phi[d], rho[d]."""

    teams: list[int]
    strength: dict[int, Arr]  # month index -> (draws, teams)
    openness: Arr
    base: Arr
    h: Arr
    h_u: Arr
    phi: Arr
    rho: Arr

    def index(self, team: int) -> int:
        return self.teams.index(team)

    def lam_mu(self, d: Ints, i: Ints, j: Ints, month: int, role: NDArray[np.str_] | str
               ) -> tuple[Arr, Arr]:  # fmt: skip
        """Rates of listed home i and away j for draws d (arrays of the same shape)."""
        s = self.strength[month]
        si, sj = s[d, i], s[d, j]
        oi, oj = self.openness[d, i], self.openness[d, j]
        r = np.asarray(role)
        hh = (r == "home").astype(float)
        ha = (r == "swapped").astype(float)
        hu = (r == "unknown").astype(float)
        base = self.base[d] + (oi + oj) / 2
        lam = np.exp(base + self.h[d] * hh + self.h_u[d] * hu + (si - sj) / 2)
        mu = np.exp(base + self.h[d] * ha + (sj - si) / 2)
        return lam, mu


def rates(post: Mapping[str, Arr], teams: Sequence[int], confs: Mapping[int, str],
          refit_month: int, walk_months: int, months: Sequence[int], comp_type: str,
          rng: np.random.Generator) -> Rates:  # fmt: skip
    """The served refit's draws as `Rates` for `teams` at `months` (month indices, at or after
    the refit's month). As `national._side`: a fitted team's last strength carried on by its
    own walk; an unseen team starts at its confederation's level with the team and walk
    spread."""
    from adatta.model import national as nat

    k = len(post["c"])
    fitted = {int(t): i for i, t in enumerate(post["teams"].astype(np.int64))}
    sw = post["sigma_w"]
    n = len(teams)
    s0 = np.empty((k, n))
    o = np.empty((k, n))
    for j, t in enumerate(teams):
        if t in fitted:
            s0[:, j] = post["s_last"][:, fitted[t]]
            o[:, j] = post["o_last"][:, fitted[t]]
        else:
            cj = nat.CONFS.index(confs[t])
            lvl = np.zeros(k) if cj == 0 else post["conf"][:, cj - 1]
            s0[:, j] = (lvl + post["sigma_u"] * rng.standard_normal(k)
                        + sw * np.sqrt(walk_months) * rng.standard_normal(k))  # fmt: skip
            o[:, j] = post["sigma_o"] * rng.standard_normal(k)
    last = max(max(months), refit_month)
    steps = rng.standard_normal((last - refit_month, k, n))
    strength = {}
    for m in sorted(set(months)):
        e = m - refit_month
        if e < 0:
            raise ValueError("a match month before the refit's")
        strength[m] = s0 + sw[:, None] * steps[:e].sum(axis=0)
    g = np.concatenate([np.zeros((k, 1)), post["g"]], axis=1)
    base = post["c"] + g[:, nat.TYPES.index(comp_type)]
    return Rates(list(teams), strength, o, base, post["h"], post["h_u"], post["phi"],
                 post["rho"])  # fmt: skip


@dataclass
class Fixture:
    """A group match: fixed (a result) or to be simulated."""

    home: int
    away: int
    month: int
    role: str
    score: tuple[int, int] | None = None


@dataclass
class Slot:
    """A knockout slot's venue for the role of whoever plays there."""

    month: int
    venue_country: str | None
    played: tuple[int, int, int] | None = None  # (listed home, listed away, winner) once final


@dataclass
class State:
    """What is fixed at the forecast's time: group results and finished knockout matches."""

    groups: dict[str, list[int]]  # letter -> teams
    fixtures: dict[str, list[Fixture]]  # letter -> its matches
    slots: dict[str, Slot]  # knockout slot -> venue (and result once played)
    country: dict[int, str | None]  # team -> country, for the host role
    done_groups: dict[str, list[int]] = field(default_factory=dict)  # final: published order
    first: dict[str, tuple[int, int]] | None = None  # every group final: the first round


@dataclass
class Result:
    """Per-team probabilities over the simulated tournaments."""

    teams: list[int]
    position: Arr  # (teams, group size): P(finish k-th)
    advance: Arr  # P(through the group)
    reach: dict[str, Arr]  # round -> P(play in it)
    win: Arr
    n_sims: int
    random_groups: float  # share of simulated group tables that needed the random step
    random_thirds: float
    match_1x2: dict[tuple[int, int], Arr] = field(default_factory=dict)  # simulated group 1X2


def _role(country: Mapping[int, str | None], venue: str | None, h: Ints, a: Ints
          ) -> NDArray[np.str_]:  # fmt: skip
    from adatta.features import host

    out = np.empty(h.shape, dtype=object)
    for idx in np.ndindex(h.shape):
        out[idx] = host.role(venue, country.get(int(h[idx])), country.get(int(a[idx])))
        if out[idx] == "unknown":  # a venue country unknown: neither side is at home
            out[idx] = "neutral"
    return out.astype(str)


def _knockout(rt: Rates, d: Ints, i: Ints, j: Ints, month: int, role: NDArray[np.str_],
              extra_time: bool, rng: np.random.Generator) -> NDArray[np.bool_]:  # fmt: skip
    """True where listed home i goes through (single match)."""
    lam, mu = rt.lam_mu(d, i, j, month, role)
    x, y = _scores(rt, d, lam, mu, rng)
    win = x > y
    level = x == y
    if extra_time and level.any():
        ex = rng.poisson(KAPPA_ET * lam / 3)
        ey = rng.poisson(KAPPA_ET * mu / 3)
        win = win | (level & (ex > ey))
        level = level & (ex == ey)
    out: NDArray[np.bool_] = win | (level & (rng.random(len(i)) < P_SHOOTOUT))
    return out


def _scores(rt: Rates, d: Ints, lam: Arr, mu: Arr, rng: np.random.Generator
            ) -> tuple[Ints, Ints]:  # fmt: skip
    """One score per entry from v1's negative binomial with Dixon-Coles (by rejection)."""
    from adatta.model import national as nat

    x, y = nat.nb_dc_sample(lam[:, None], mu[:, None], rt.phi[d], rt.rho[d], rng)
    return x[:, 0], y[:, 0]


def simulate(ed: Edition, st: State, rt: Rates, n_sims: int = N_SIMS, seed: int = SEED,
             track: Sequence[tuple[int, int]] = ()) -> Result:  # fmt: skip
    """`n_sims` tournaments from `st`, sim s on draw s mod draws. `track`: group fixtures
    (home, away) whose simulated 1X2 is kept for the coherence check."""
    rng = np.random.default_rng(seed)
    k = len(rt.base)
    d = np.arange(n_sims) % k
    teams = sorted({t for g in st.groups.values() for t in g})
    tix = {t: n for n, t in enumerate(teams)}
    size = max(len(g) for g in st.groups.values())
    position = np.zeros((len(teams), size))
    advance = np.zeros(len(teams))
    reach: dict[str, Arr] = {r.name: np.zeros(len(teams)) for r in ed.rounds}
    win = np.zeros(len(teams))
    rix = {t: rt.index(t) for t in teams}
    # group scores, (sims,) per fixture
    scores: dict[str, list[tuple[int, int, Ints, Ints]]] = {}
    match_1x2: dict[tuple[int, int], Arr] = {}
    for g, fx in st.fixtures.items():
        rows = []
        for f in fx:
            if f.score is not None:
                rows.append((f.home, f.away, np.full(n_sims, f.score[0]),
                             np.full(n_sims, f.score[1])))  # fmt: skip
                continue
            ii = np.full(n_sims, rix[f.home])
            jj = np.full(n_sims, rix[f.away])
            lam, mu = rt.lam_mu(d, ii, jj, f.month, f.role)
            x, y = _scores(rt, d, lam, mu, rng)
            rows.append((f.home, f.away, x, y))
            if (f.home, f.away) in track:
                match_1x2[(f.home, f.away)] = np.array([(x > y).mean(), (x == y).mean(),
                                                        (x < y).mean()])  # fmt: skip
        scores[g] = rows
    rand_g = rand_t = 0
    letters = ed.letters
    places = np.zeros((n_sims, len(letters), size), dtype=np.int64)
    third_rows: list[list[tuple[str, list[int]]]] = [[] for _ in range(n_sims)]
    for gi, g in enumerate(letters):
        gt = st.groups[g]
        for s in range(n_sims):
            games = [(h, a, int(x[s]), int(y[s])) for h, a, x, y in scores[g]]
            if g in st.done_groups:  # final: the published order (fair play, lots included)
                order = st.done_groups[g]
            else:
                tab = rank_group(gt, games, ed.group_rank, rng)
                rand_g += tab.random
                order = tab.order
            places[s, gi, : len(order)] = order
            for pos, t in enumerate(order):
                position[tix[t], pos] += 1
            if ed.thirds is not None and len(order) > 2:
                third_rows[s].append((g, _stats(gt, games)[order[2]]))
    first = ed.rounds[0]
    pair_h = {sid: np.zeros(n_sims, dtype=np.int64) for sid in first.matches}
    pair_a = {sid: np.zeros(n_sims, dtype=np.int64) for sid in first.matches}
    for s in range(n_sims):
        if st.first is not None:  # every group final: the pairings as drawn up
            pairs = st.first
        else:
            pl_ = {g: [int(t) for t in places[s, gi]] for gi, g in enumerate(letters)}
            tg: list[str] = []
            if ed.thirds is not None:
                tg, rnd = rank_thirds(third_rows[s], ed.thirds.rank, rng)
                rand_t += rnd
            pairs = first_round(ed, pl_, tg)
        for sid, (th, ta) in pairs.items():
            pair_h[sid][s], pair_a[sid][s] = th, ta
    for sid in first.matches:
        for arr in (pair_h[sid], pair_a[sid]):
            np.add.at(advance, [tix[int(t)] for t in arr], 1)
    winners: dict[str, Ints] = {}
    losers: dict[str, Ints] = {}
    for rd in ed.rounds:
        for sid, (x_ref, y_ref) in rd.matches.items():
            if rd is first:
                hs, as_ = pair_h[sid], pair_a[sid]
            else:
                hs = (winners if x_ref[0] == "W" else losers)[x_ref[2:]]
                as_ = (winners if y_ref[0] == "W" else losers)[y_ref[2:]]
            np.add.at(reach[rd.name], [tix[int(t)] for t in hs], 1)
            np.add.at(reach[rd.name], [tix[int(t)] for t in as_], 1)
            slot = st.slots[sid]
            if slot.played is not None:
                ph, pa, pw = slot.played
                if not np.isin(hs, (ph, pa)).all() or not np.isin(as_, (ph, pa)).all():
                    raise ValueError(f"{sid}: a played slot reached by other teams")
                home_wins = np.where(hs == ph, pw == ph, pw == pa)
            else:
                ii = np.array([rix[int(t)] for t in hs])
                jj = np.array([rix[int(t)] for t in as_])
                role = _role(st.country, slot.venue_country, hs, as_)
                home_wins = _knockout(rt, d, ii, jj, slot.month, role, rd.extra_time, rng)
            winners[sid] = np.where(home_wins, hs, as_)
            losers[sid] = np.where(home_wins, as_, hs)
            if rd.name == "F":
                np.add.at(win, [tix[int(t)] for t in winners[sid]], 1)
    n = float(n_sims)
    return Result(teams, position / n, advance / n, {k_: v / n for k_, v in reach.items()},
                  win / n, n_sims, rand_g / (n * len(letters)), rand_t / n, match_1x2)  # fmt: skip
