"""P3-g: projected formation and XI by position (docs/PHASE3_TASKS.md P3-g), and its dev check.

For a team-match as of t (kickoff - 24 h, P3-b's horizon):
  formation   P3-c's most likely class (the next one when the top is "other")
  fit(i, s)   player i's as-of share of starts in slot code s over positional matches,
              smoothed (pseudo-count FIT_A) towards his line's team shares (P3-c's R1: the
              team's codes for starters with his provider line, shrunk to the league-wide
              ones); keepers fit only GK, outfield players never GK
  W(i, s)     P(start_i) fit(i, s), scaled by iterative proportional fitting so that every
              slot sums to 1 and every player to P(start_i) (rescaled so the pool totals 11:
              the formation's 11 slots are filled from the pool, amendment 1)
  XI          the one-player-per-slot assignment maximising sum log W (Hungarian)
Baselines: B_top, the keeper plus the 10 outfield players most likely to start (the panel
before P3-g); B_last, each slot's player in the team's last match in that formation.
Dev only (positional league-seasons); reads stored tables; metrics only.
"""

from __future__ import annotations

import io
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy.optimize import linear_sum_assignment

from adatta.eval.selection import DEV
from adatta.features import lineups as fl
from adatta.features import selection as sel
from adatta.features.referees import RESULT_LAG

Arr = NDArray[np.float64]
FIT_A = 3.0  # pseudo-count of a player's slot shares towards his line's team shares
TEAM_A = 5.0  # P3-c's ROLE_A: the team's line shares shrunk to the league-wide ones
FLOOR = 1e-6  # any outfield player in any outfield slot, so the scaling always converges
IPF_ITERS = 200
IPF_TOL = 1e-10
OVERLAP_MAX = 0.05  # (d)1: players a team-match
ECE_MAX = 0.03
EPS = 1e-12


def slots_of(formation: str) -> list[str]:
    """The 11 slot codes of a formation string, keeper first (P3-c's rules)."""
    rows = [1, *[int(x) for x in formation.split("-")]]
    return [c for row in fl.formation_codes(rows) for c in row]


def projected_formation(top: str, p: Sequence[float], vocab: Sequence[str]) -> str:
    """P3-c's most likely class; when that is "other" (no slot rules), the next one."""
    if top != "other":
        return top
    order = np.argsort(-np.asarray(p))
    return str(next(vocab[i] for i in order if vocab[i] != "other"))


# ------------------------------------------------------------------------- slot history


class History:
    """As-of counts of coded starts: per player, per (team, line) and per line (league-wide),
    added in kickoff order from positional matches."""

    def __init__(self, codes: Sequence[str]) -> None:
        self.codes = list(codes)
        self.ix = {c: i for i, c in enumerate(self.codes)}
        n = len(self.codes)
        self.player: dict[int, Arr] = defaultdict(lambda: np.zeros(n))
        self.team: dict[tuple[int, str], Arr] = defaultdict(lambda: np.zeros(n))
        self.line: dict[str, Arr] = defaultdict(lambda: np.zeros(n))

    def add(self, team: int, rows: list[tuple[int, str | None, str]]) -> None:
        for pid, pos, code in rows:
            if code not in self.ix:
                continue
            v = self.ix[code]
            self.player[pid][v] += 1
            self.team[(team, pos or "U")][v] += 1
            self.line[pos or "U"][v] += 1

    def fit(self, team: int, pid: int, pos: str) -> Arr:
        """fit(i, .) over every code (module docstring)."""
        n = len(self.codes)
        g = self.line[pos]
        league = (g + 1.0 / n) / (g.sum() + 1.0)
        t = self.team[(team, pos)]
        r1 = (t + TEAM_A * league) / (t.sum() + TEAM_A)
        h = self.player[pid]
        out = (h + FIT_A * r1) / (h.sum() + FIT_A)
        return np.asarray(out, dtype=np.float64)


# --------------------------------------------------------------------------- W and XI


def weights(p_start: Arr, fits: Arr, is_gk: NDArray[np.bool_], slot_gk: NDArray[np.bool_],
            ) -> Arr:  # fmt: skip
    """W0(i, s) = P(start_i) fit(i, s); keepers only in the GK slot and never elsewhere."""
    w = p_start[:, None] * np.maximum(fits, FLOOR)
    w[np.ix_(is_gk, ~slot_gk)] = 0.0
    w[np.ix_(~is_gk, slot_gk)] = 0.0
    return w


def ipf(w0: Arr, rows: Arr) -> Arr:
    """Scale W0 so each slot (column) sums to 1 and each player (row) to `rows` (which must
    total the number of slots). A row of zeros stays zero."""
    w = w0.copy()
    for _ in range(IPF_ITERS):
        c = w.sum(axis=0)
        w = w / np.where(c > 0, c, 1.0)[None, :]
        r = w.sum(axis=1)
        w = w * np.where(r > 0, rows / np.where(r > 0, r, 1.0), 0.0)[:, None]
        if np.abs(w.sum(axis=0) - 1.0).max() < IPF_TOL:
            break
    return w


def row_targets(p_start: Arr, is_gk: NDArray[np.bool_], n_gk_slots: int, n_slots: int) -> Arr:
    """Player totals for the scaling: P(start), rescaled within keepers to the GK slots and
    within outfield players to the rest (amendment 1: the slots are filled from the pool)."""
    out = np.zeros_like(p_start)
    for mask, total in ((is_gk, n_gk_slots), (~is_gk, n_slots - n_gk_slots)):
        s = p_start[mask].sum()
        if s > 0:
            out[mask] = p_start[mask] * total / s
    return out


def assign(w: Arr) -> NDArray[np.int64]:
    """Player index per slot maximising sum log W (rectangular Hungarian)."""
    cost = -np.log(np.maximum(w, EPS))
    rows, cols = linear_sum_assignment(cost)
    out = np.full(w.shape[1], -1, dtype=np.int64)
    out[cols] = rows
    return out


def b_top(p_start: Arr, is_gk: NDArray[np.bool_]) -> set[int]:
    """The keeper most likely to start and the 10 outfield players most likely to start."""
    order = np.argsort(-p_start, kind="stable")
    gk = next((int(i) for i in order if is_gk[i]), None)
    out = [int(i) for i in order if not is_gk[i]][: 10 if gk is not None else 11]
    return set(out) | ({gk} if gk is not None else set())


# ------------------------------------------------------------------------------- dev


@dataclass
class Inputs:
    """lineups: P3-c's lineup table (starters with slot, positional, kickoff); pool: P3-b's
    model rows (match_id, team_id, player_id, pos, p_start); formations: P3-c's scored model
    rows with `top` and `p`; vocab: P3-c's vocabulary (the order of `p`)."""

    lineups: pl.DataFrame
    pool: pl.DataFrame
    formations: pl.DataFrame
    vocab: list[str]


def evaluate(inp: Inputs, targets: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(one row per target team-match, one row per (player, code) pair for calibration).
    Targets: match_id, team_id of positional dev team-matches with a P3-b pool and a P3-c
    prediction. History is added as of each target's kickoff - 24 h - RESULT_LAG."""
    pos = inp.lineups.filter(pl.col("positional")).sort("kickoff", "match_id", "team_id")
    codes = sorted(fl.CODE_LINE)
    hist = History(codes)
    groups = pos.partition_by(["match_id", "team_id"], as_dict=True, maintain_order=True)
    order = sorted(groups, key=lambda k: groups[k]["kickoff"][0])
    kick = {k: groups[k]["kickoff"][0] for k in order}
    last_in: dict[tuple[int, str], list[tuple[str, int]]] = {}  # (team, formation) -> slots
    pool = inp.pool.partition_by(["match_id", "team_id"], as_dict=True)
    fm = {(r["match_id"], r["team_id"]): r for r in inp.formations.iter_rows(named=True)}
    tgt = {(int(a), int(b)) for a, b in targets.select("match_id", "team_id").iter_rows()}
    added = 0
    rows: list[dict[str, Any]] = []
    calib: list[dict[str, Any]] = []
    for gi, key in enumerate(order):
        t = kick[key] - sel.HORIZON - RESULT_LAG
        while added < gi and kick[order[added]] <= t:
            k = order[added]
            g = groups[k]
            team = int(k[1])
            hist.add(team, list(g.select("player_id", "pos", "slot").iter_rows()))
            f = g["formation"][0]
            last_in[(team, f)] = list(g.select("slot", "player_id").iter_rows())
            added += 1
        mk = (int(key[0]), int(key[1]))
        if mk not in tgt or mk not in pool or mk not in fm:
            continue
        g, pl_ = groups[key], pool[mk]
        team = mk[1]
        actual = dict(g.select("player_id", "slot").iter_rows())
        f_hat = projected_formation(fm[mk]["top"], fm[mk]["p"], inp.vocab)
        slots = slots_of(f_hat)
        slot_gk = np.array([c == "GK" for c in slots])
        pids = pl_["player_id"].to_numpy()
        ppos = [p if p in ("G", "D", "M", "F") else "U" for p in pl_["pos"].to_list()]
        is_gk = np.array([p == "G" for p in ppos])
        p_start = pl_["p_start"].to_numpy().astype(float)
        full = np.stack([hist.fit(team, int(i), ps) for i, ps in zip(pids, ppos, strict=True)])
        fits = full[:, [hist.ix[c] for c in slots]]
        w = ipf(weights(p_start, fits, is_gk, slot_gk),
                row_targets(p_start, is_gk, int(slot_gk.sum()), len(slots)))  # fmt: skip
        pick = assign(w)
        xi = {int(pids[i]) for i in pick if i >= 0}
        top = {int(pids[i]) for i in b_top(p_start, is_gk)}
        starters = set(actual)
        same = f_hat == g["formation"][0]
        res: dict[str, Any] = {
            "match_id": mk[0], "team_id": team, "league_ref": g["league_ref"][0],
            "season": g["season"][0], "formation_hit": same,
            "overlap_g": len(xi & starters), "overlap_top": len(top & starters),
            "gk_in_xi": int(sum(1 for i in pick if i >= 0 and is_gk[i])),
            "slot_g": None, "slot_last": None,
        }  # fmt: skip
        if same:
            act_code = defaultdict(set)
            for pid, code in actual.items():
                act_code[code].add(pid)
            got = defaultdict(set)
            for s_i, i in enumerate(pick):
                if i >= 0:
                    got[slots[s_i]].add(int(pids[i]))
            res["slot_g"] = sum(len(got[c] & act_code[c]) for c in set(slots)) / 11.0
            prev = last_in.get((team, f_hat))
            if prev is not None:
                pc = defaultdict(set)
                for code, pid in prev:
                    pc[code].add(int(pid))
                res["slot_last"] = sum(len(pc[c] & act_code[c]) for c in set(slots)) / 11.0
            # calibration: P(player i starts in code c) = sum of W over that code's slots
            for c in set(slots):
                col = np.array([x == c for x in slots])
                pc_ = w[:, col].sum(axis=1)
                for j, pid in enumerate(pids):
                    if pc_[j] > 1e-4:
                        calib.append({"p": float(pc_[j]), "y": actual.get(int(pid)) == c})
        rows.append(res)
    return pl.DataFrame(rows), pl.DataFrame(calib, schema={"p": pl.Float64, "y": pl.Boolean})


def ece(calib: pl.DataFrame, bins: int = 10) -> float:
    if calib.is_empty():
        return float("nan")
    x = calib.with_columns(b=(pl.col("p") * bins).floor().clip(0, bins - 1),
                           o=pl.col("y").cast(pl.Float64))  # fmt: skip
    g = x.group_by("b").agg(pl.len().alias("n"), pl.col("p").mean(), pl.col("o").mean())
    n = g["n"].to_numpy().astype(float)
    return float((np.abs(g["p"].to_numpy() - g["o"].to_numpy()) * n).sum() / n.sum())


def _paired(a: Arr, b: Arr) -> dict[str, float]:
    d = a - b
    half = 1.96 * d.std(ddof=1) / np.sqrt(len(d)) if len(d) > 1 else float("nan")
    return {"n": len(d), "mean": float(d.mean()), "lo": float(d.mean() - half),
            "hi": float(d.mean() + half)}  # fmt: skip


@dataclass
class Verdict:
    passed: bool
    checks: list[dict[str, Any]]


def verdict(res: pl.DataFrame, calib: pl.DataFrame, leakage_ok: bool) -> Verdict:
    ov = _paired(res["overlap_g"].to_numpy().astype(float),
                 res["overlap_top"].to_numpy().astype(float))  # fmt: skip
    both = res.filter(pl.col("slot_g").is_not_null() & pl.col("slot_last").is_not_null())
    sl = _paired(both["slot_g"].to_numpy(), both["slot_last"].to_numpy())
    e = ece(calib)
    gk = res["gk_in_xi"].to_numpy() == 1
    checks = [
        {"check": "(d)1 XI overlap, P3-g - B_top (players)", "value": ov["mean"],
         "lo": ov["lo"], "hi": ov["hi"], "pass": ov["mean"] >= -OVERLAP_MAX},
        {"check": "(d)2 slot accuracy, P3-g - B_last", "value": sl["mean"], "lo": sl["lo"],
         "hi": sl["hi"], "pass": sl["lo"] > 0},
        {"check": "(d)3 per-slot ECE", "value": e, "pass": e <= ECE_MAX},
        {"check": "(d)4 leakage", "value": float(leakage_ok), "pass": leakage_ok},
        {"check": "keeper in every projected XI", "value": float(gk.mean()),
         "pass": bool(gk.all())},
    ]  # fmt: skip
    return Verdict(all(c["pass"] for c in checks), checks)


def markdown(res: pl.DataFrame, calib: pl.DataFrame, v: Verdict) -> str:
    out = ["# P3-g projected formation and XI by position — dev (positional league-seasons)",
           "", f"**Verdict: {'pass' if v.passed else 'fail'}**", "",
           "| check | value | 95% interval | pass |", "|---|---|---|---|"]  # fmt: skip
    for c in v.checks:
        iv = f"[{c['lo']:+.4f}, {c['hi']:+.4f}]" if "lo" in c else ""
        out.append(f"| {c['check']} | {c['value']:+.4f} | {iv} | {'yes' if c['pass'] else 'NO'} |")
    both = res.filter(pl.col("slot_g").is_not_null())

    def m(x: pl.Series) -> float:
        a = x.drop_nulls().to_numpy().astype(float)
        return float(a.mean()) if len(a) else float("nan")

    out += ["",
            f"Team-matches {res.height}; projected formation right in "
            f"{m(res['formation_hit']):.3f}; with a slot comparison {both.height} "
            f"(B_last available in {res['slot_last'].is_not_null().sum()}).",
            f"Mean XI overlap: P3-g {m(res['overlap_g']):.3f}, B_top "
            f"{m(res['overlap_top']):.3f} of 11.",
            f"Mean slot accuracy (formation right): P3-g {m(both['slot_g']):.3f}, B_last "
            f"{m(both['slot_last']):.3f}.", "",
            "| league | team-matches | formation right | overlap P3-g | overlap B_top | "
            "slot P3-g | slot B_last |", "|---|---|---|---|---|---|---|"]  # fmt: skip
    for (lg,), g in sorted(res.group_by(["league_ref"]), key=lambda x: str(x[0][0])):
        out.append(f"| {lg!s} | {g.height} | {m(g['formation_hit']):.3f} | "
                   f"{m(g['overlap_g']):.3f} | {m(g['overlap_top']):.3f} | "
                   f"{m(g['slot_g']):.3f} | {m(g['slot_last']):.3f} |")  # fmt: skip
    return "\n".join(out)


def leakage(inp: Inputs, targets: pl.DataFrame, full: pl.DataFrame, n: int = 100,
            seed: int = 20261120) -> bool:  # fmt: skip
    """(d)4: for n sampled targets, the evaluation rebuilt from lineups final by the as-of
    time (plus the target's own lineup, needed for its outcome) gives the same XI, overlap
    and slot accuracy."""
    sample = targets.sample(min(n, targets.height), seed=seed)
    for r in sample.iter_rows(named=True):
        own = (pl.col("match_id") == r["match_id"]) & (pl.col("team_id") == r["team_id"])
        k = inp.lineups.filter(own)["kickoff"]
        if k.is_empty():
            continue
        cut = k[0] - sel.HORIZON - RESULT_LAG
        lu = inp.lineups.filter((pl.col("kickoff") <= cut) | own)
        key = pl.DataFrame({"match_id": [r["match_id"]], "team_id": [r["team_id"]]})
        re_, _ = evaluate(Inputs(lu, inp.pool, inp.formations, inp.vocab), key)
        a = full.join(key, on=["match_id", "team_id"])
        if re_.is_empty() or a.is_empty():
            continue
        for c in ("overlap_g", "slot_g", "slot_last"):
            if re_[c][0] != a[c][0]:
                return False
    return True


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.eval import formation as ef

    p = argparse.ArgumentParser(prog="python -m adatta.eval.positions")
    p.add_argument(
        "--formation-runs",
        required=True,
        help="comma-separated P3-c reruns storing top and p (their parts are read)",
    )
    p.add_argument("--export", default="44406512773", help="P3-b export report")
    p.add_argument("--formation-table", default="9a05c70602047e45")
    p.add_argument("--lineups", default="d1d11c0a7f3ce0a7")
    args = p.parse_args(argv)
    store = make_store(get_settings())
    lineups, _, _ = fl.load(store, args.lineups)
    _, fmeta, _ = ef.load_table(store, args.formation_table)
    vocab = list(fmeta["vocab"])
    from adatta.adapters.storage import try_get
    from adatta.eval import harness

    parts = []
    for run in args.formation_runs.split(","):
        for b in harness.load_config().bundles:
            for y in DEV:
                data = try_get(store, f"formation/parts/{run}/{b}_{y}_0of1.parquet")
                if data is not None:
                    parts.append(pl.read_parquet(io.BytesIO(data)))
    if not parts:
        raise SystemExit("no P3-c parts found for these runs")
    sc = pl.concat(parts, how="diagonal").filter(pl.col("model") == "model")
    if "top" not in sc.columns:
        raise SystemExit("these P3-c parts predate the stored distribution (P3-g amendment 1)")
    sc = sc.select("match_id", "team_id", "top", "p").unique(["match_id", "team_id"])
    ex = pl.read_parquet(io.BytesIO(store.get(f"selection/reports/{args.export}/scored.parquet")))
    ex = ex.filter(pl.col("model") == "model").select("match_id", "team_id", "player_id", "pos",
                                                      "p_start")  # fmt: skip
    pos_ls = lineups.filter(pl.col("positional")).select("league_ref", "season").unique()
    key = ["match_id", "team_id"]
    dev = pl.col("positional") & pl.col("season").is_in(list(DEV))
    targets = (lineups.filter(dev).select(key).unique().join(sc.select(key), on=key)
               .join(ex.select(key).unique(), on=key))  # fmt: skip
    inp = Inputs(lineups, ex, sc, vocab)
    res, calib = evaluate(inp, targets)
    v = verdict(res, calib, leakage(inp, targets, res))
    print(f"positional league-seasons {pos_ls.height}")
    print(markdown(res, calib, v))


if __name__ == "__main__":
    main()
