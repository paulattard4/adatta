"""Stage A, A2: cup terms on top of the A1 reference, one at a time (docs/BEYOND_LEAGUES_PLAN.md,
Stage A, "A2 design", fixed before any A1 result existed).

Each term is an offset on the log rates of A1's posterior draws (stored per tie by
`model.cup_backtest`), so a tie is rescored without refitting the league model; A1 itself is
the same rescoring with no offset, and must reproduce A1's stored probabilities.

- T1 (gamma): log lambda += gamma on ties not on a neutral ground.
- T2 (no parameter): log lambda -= h on neutral ties, h the draw's home term.
- T3 (theta_tier, theta_gap): rotation weakens a side; z_tier = a round before the
  quarter-finals against a lower-tier side, z_gap = its next league match within 3 days.
  log lambda += s and log mu -= s, s = sum(theta * (z_home - z_away)).

Parameters are fitted walk-forward on a grid (Normal(0, 0.25) prior) from the cup ties whose
results were available by the Monday of the tie's week, all cups pooled, and the tie is scored
at the posterior mean. Metrics only in the output (the repo and its logs are public).

    uv run python -m adatta.eval.cup_terms run --a1 RUN_ID
"""

from __future__ import annotations

import argparse
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy.stats import poisson

from adatta.eval import harness, metrics, phase2
from adatta.features import neutral

Arr = NDArray[np.float64]


def _mean(s: pl.Series) -> float:
    return float(s.cast(pl.Float64).to_numpy().mean())


# +-1.0: at +-0.6 the Normal(0, 0.25) prior alone puts 0.4% on the edge, so the 1e-3 edge
# check could never pass (changed before any A1 result existed)
GAMMA = np.round(np.arange(-1.0, 1.0 + 1e-9, 0.02), 4)
THETA = np.round(np.arange(-1.0, 1.0 + 1e-9, 0.05), 4)
PRIOR_SD = 0.25
SENSITIVITY_SD = (0.1, 0.25, 0.5)
FIT_DRAWS = 100
GAP_DAYS = 3.0
LAG = timedelta(hours=3)
EDGE_MASS = 1e-3
TERMS = ("T1", "T2", "T3")
D28 = ("1X2:log_loss", "1X2:rps", "OU2.5:log_loss", "BTTS:log_loss", "total:crps")
MAX_SEGMENT_WORSE = 0.010
ECE_MAX_WORSE = 0.005
OUT_DEFAULT = "reports/stage_a/a2"


# --------------------------------------------------------------------------- score matrices


def matrices(lam: Arr, mu: Arr, rho: Arr, kappa: Arr, max_goals: int) -> Arr:
    """Dixon-Coles score matrices (..., K, K), as `dixon_coles.score_matrix`, vectorised over
    the leading dimensions of the (broadcastable) rate arrays."""
    lam, mu, rho, kappa = (np.asarray(x, dtype=np.float64)
                           for x in np.broadcast_arrays(lam, mu, rho, kappa))  # fmt: skip
    k = np.arange(max_goals + 1)
    ph = np.asarray(poisson.pmf(k, lam[..., None]), dtype=np.float64)
    pa = np.asarray(poisson.pmf(k, mu[..., None]), dtype=np.float64)
    m = ph[..., :, None] * pa[..., None, :]
    m[..., 0, 0] *= 1 - lam * mu * rho
    m[..., 0, 1] *= 1 + lam * rho
    m[..., 1, 0] *= 1 + mu * rho
    m[..., 1, 1] *= 1 - rho
    m = np.maximum(m, 0.0)
    m[..., k, k] *= np.exp(kappa)[..., None]
    return np.asarray(m / m.sum(axis=(-2, -1), keepdims=True))


# --------------------------------------------------------------------------- ties


@dataclass
class Ties:
    """A1's rows, sorted by kickoff: the frame without draws, and the draws as (n, D)."""

    frame: pl.DataFrame
    lam: Arr
    mu: Arr
    rho: Arr
    kappa: Arr
    home_term: Arr
    hg: NDArray[np.int64]
    ag: NDArray[np.int64]

    @property
    def n(self) -> int:
        return self.frame.height


def early(round_: str) -> bool:
    """A round before the quarter-finals."""
    return neutral.stage(round_) not in ("semi", "final") and "quarter" not in round_.casefold()


def from_a1(pred: pl.DataFrame) -> Ties:
    p = pred.sort("kickoff", "match_id")
    cols = ("draws_lam", "draws_mu", "draws_rho", "draws_kappa", "draws_home_term")
    draws = {c: np.array(p[c].to_list(), dtype=np.float64) for c in cols}
    gap_h = p["gap_home_days"].fill_null(np.inf).to_numpy()
    gap_a = p["gap_away_days"].fill_null(np.inf).to_numpy()
    th, ta = p["tier_home"].to_numpy(), p["tier_away"].to_numpy()
    e = np.array([early(r) for r in p["round"].to_list()])
    z_gap_h, z_gap_a = (gap_h <= GAP_DAYS).astype(np.int64), (gap_a <= GAP_DAYS).astype(np.int64)
    frame = p.drop([c for c in p.columns if c.startswith("draws_")]).with_columns(
        early=pl.Series(e),
        d_tier=pl.Series((e & (th < ta)).astype(np.int64) - (e & (ta < th)).astype(np.int64)),
        d_gap=pl.Series(z_gap_h - z_gap_a),
    )  # fmt: skip
    return Ties(frame, draws["draws_lam"], draws["draws_mu"], draws["draws_rho"],
                draws["draws_kappa"], draws["draws_home_term"], p["hg"].to_numpy(),
                p["ag"].to_numpy())  # fmt: skip


# --------------------------------------------------------------------------- terms

Offsets = tuple[Arr, Arr]  # on log lambda and log mu, each broadcastable to (n, D)
Design = tuple[Arr, Arr]  # (n, k) each: offset = design @ params, for log lambda and log mu


@dataclass(frozen=True)
class Term:
    """A term linear in its parameters (`design`) or fixed (`fixed`, no parameter)."""

    name: str
    grid: tuple[Arr, ...]  # one axis per parameter; () for none
    design: Callable[[Ties], Design] | None = None
    fixed: Callable[[Ties], Offsets] | None = None

    @property
    def n_params(self) -> int:
        return len(self.grid)

    def offsets(self, t: Ties, params: Arr) -> Offsets:
        """Offsets with each tie's own parameters (params (n, k); ignored when fixed)."""
        if self.fixed is not None:
            return self.fixed(t)
        assert self.design is not None
        xh, xa = self.design(t)
        return (xh * params).sum(1)[:, None], (xa * params).sum(1)[:, None]


def _t1(t: Ties) -> Design:
    xh = (~np.asarray(t.frame["neutral"].to_numpy(), dtype=bool)).astype(np.float64)[:, None]
    return xh, np.zeros_like(xh)


def _t2(t: Ties) -> Offsets:
    neu = np.asarray(t.frame["neutral"].to_numpy(), dtype=bool)[:, None]
    off = np.where(neu, -t.home_term, 0.0)
    return off, np.zeros_like(off)


def _t3(t: Ties) -> Design:
    xh = np.stack([t.frame["d_tier"].to_numpy(), t.frame["d_gap"].to_numpy()], 1)
    xh = xh.astype(np.float64)
    return xh, -xh


TERM = {
    "T1": Term("T1", (GAMMA,), design=_t1),
    "T2": Term("T2", (), fixed=_t2),
    "T3": Term("T3", (THETA, THETA), design=_t3),
}


def active(term: str, t: Ties) -> NDArray[np.bool_]:
    """Ties the term can move."""
    f = t.frame
    neu = np.asarray(f["neutral"].to_numpy(), dtype=bool)
    if term == "T1":
        return ~neu
    if term == "T2":
        return neu
    return np.asarray((f["d_tier"].to_numpy() != 0) | (f["d_gap"].to_numpy() != 0), dtype=bool)


# --------------------------------------------------------------------------- fit


def loglik_table(term: Term, t: Ties, max_goals: int, draws: int = FIT_DRAWS) -> Arr:
    """(n, *grid) log P(observed 90' score) under the term at each grid point, from `draws`
    of each tie's draws; 0 where the term does not move the tie (a constant drops out)."""
    shape = tuple(len(g) for g in term.grid)
    pts = np.stack(np.meshgrid(*term.grid, indexing="ij"), -1).reshape(-1, term.n_params)
    out = np.zeros((t.n, len(pts)))
    act = active(term.name, t)
    sl = slice(0, draws)
    assert term.design is not None, "a fixed term has nothing to fit"
    xh, xa = term.design(t)
    for i in np.flatnonzero(act):
        lam, mu = t.lam[i, sl], t.mu[i, sl]
        oh, oa = pts @ xh[i], pts @ xa[i]  # (P,): the tie's offsets at every grid point
        uniq, inv = np.unique(np.stack([oh, oa], 1), axis=0, return_inverse=True)
        m = matrices(lam[None, :] * np.exp(uniq[:, :1]), mu[None, :] * np.exp(uniq[:, 1:]),
                     t.rho[i, sl][None, :], t.kappa[i, sl][None, :], max_goals)  # fmt: skip
        lp = np.log(np.maximum(m[:, :, t.hg[i], t.ag[i]].mean(1), 1e-300))
        out[i] = lp[inv.ravel()]
    return out.reshape((t.n, *shape))


def log_prior(term: Term, sd: float) -> Arr:
    axes = np.meshgrid(*term.grid, indexing="ij")
    return np.asarray(-0.5 * sum((a / sd) ** 2 for a in axes))


def posterior(ll_sum: Arr, prior: Arr) -> Arr:
    lp = ll_sum + prior
    w = np.exp(lp - lp.max())
    return np.asarray(w / w.sum())


def summary(term: Term, w: Arr) -> dict[str, Any]:
    """Posterior mean and sd per parameter, and the mass on the grid's edge."""
    axes = np.meshgrid(*term.grid, indexing="ij")
    mean = [float((w * a).sum()) for a in axes]
    sd = [float(np.sqrt((w * (a - m) ** 2).sum())) for a, m in zip(axes, mean, strict=True)]
    edge = np.zeros_like(w, dtype=bool)
    for d in range(term.n_params):
        view = np.moveaxis(edge, d, 0)  # a view: setting its ends marks edge
        view[0], view[-1] = True, True
    return {"mean": mean, "sd": sd, "edge_mass": float(w[edge].sum())}


def walk_forward(term: Term, t: Ties, table: Arr, sd: float = PRIOR_SD
                 ) -> tuple[Arr, list[dict[str, Any]]]:  # fmt: skip
    """Per tie, the posterior-mean parameters from the ties available by its week's Monday;
    and the fit summary per refit week."""
    kick = t.frame["kickoff"].to_list()
    avail = np.array([(k + LAG).timestamp() for k in kick])
    order = np.argsort(avail, kind="stable")
    cum = np.concatenate([np.zeros((1, *table.shape[1:])), np.cumsum(table[order], 0)])
    prior = log_prior(term, sd)
    params = np.zeros((t.n, term.n_params))
    fits: list[dict[str, Any]] = []
    weeks = [harness.week_start(k) for k in kick]
    for w_start in sorted(set(weeks)):
        k = int(np.searchsorted(avail[order], w_start.timestamp(), side="right"))
        s = summary(term, posterior(cum[k], prior))
        fits.append({"as_of": w_start, "n_train": k, **s})
        for i in (j for j, w in enumerate(weeks) if w == w_start):
            params[i] = s["mean"]
    return params, fits


# --------------------------------------------------------------------------- score


def score(t: Ties, off: Offsets, max_goals: int) -> pl.DataFrame:
    """Every tie scored from all its draws with the offsets (A1 with none)."""
    oh = np.broadcast_to(off[0], t.lam.shape)
    oa = np.broadcast_to(off[1], t.lam.shape)
    rows = []
    for i in range(t.n):
        m = matrices(t.lam[i] * np.exp(oh[i]), t.mu[i] * np.exp(oa[i]), t.rho[i], t.kappa[i],
                     max_goals).mean(0)  # fmt: skip
        mk = metrics.markets(m)
        hg, ag = int(t.hg[i]), int(t.ag[i])
        rows.append({
            "p_home": mk["1X2"][0], "p_draw": mk["1X2"][1], "p_away": mk["1X2"][2],
            "p_over25": mk["OU2.5"][0], **metrics.score_markets(mk, hg, ag),
            "score:log_loss": -float(np.log(max(float(m[hg, ag]), 1e-12))),
        })  # fmt: skip
    return pl.concat([t.frame.select("match_id", "cup_id", "hg", "ag"), pl.DataFrame(rows)],
                     how="horizontal")  # fmt: skip


# --------------------------------------------------------------------------- gate


def _ece(g: pl.DataFrame) -> dict[str, float]:
    hg, ag = g["hg"].to_numpy(), g["ag"].to_numpy()
    return {name: phase2.ece(g[col].to_numpy(), hit.astype(float))
            for name, col, hit in (("home", "p_home", hg > ag), ("draw", "p_draw", hg == ag),
                                   ("away", "p_away", hg < ag))}  # fmt: skip


def compare(a1: pl.DataFrame, cand: pl.DataFrame, cup_names: dict[int, str]) -> dict[str, Any]:
    """The term against A1 on the same ties: paired relative differences (negative = better),
    the worst cup x D28 segment, ECE deltas, and the gate verdict."""
    long = pl.concat([a1.with_columns(model=pl.lit("a1")), cand.with_columns(model=pl.lit("term"))])
    rel: dict[str, dict[str, float]] = {}
    for m in ("1X2:log_loss", "1X2:rps", "score:log_loss", "OU2.5:log_loss"):
        p = harness.paired(long, m, "term", "a1")
        base = _mean(a1[m])
        rel[m] = {"rel": p["mean_diff"] / base, "lo": p["lo"] / base, "hi": p["hi"] / base}
    segs: list[dict[str, Any]] = []
    for cup in sorted(a1["cup_id"].unique().to_list()):
        a, c = a1.filter(pl.col("cup_id") == cup), cand.filter(pl.col("cup_id") == cup)
        for m in D28:
            b = _mean(a[m])
            segs.append({"cup": cup_names.get(int(cup), str(cup)), "metric": m,
                         "rel": (_mean(c[m]) - b) / b})  # fmt: skip
    worst = max(segs, key=lambda s: float(s["rel"]))
    ea, ec = _ece(a1), _ece(cand)
    ece_delta = {k: ec[k] - ea[k] for k in ea}
    improves = rel["1X2:log_loss"]["hi"] < 0 or rel["1X2:rps"]["hi"] < 0
    passes = (improves and worst["rel"] <= MAX_SEGMENT_WORSE
              and all(v <= ECE_MAX_WORSE for v in ece_delta.values()))  # fmt: skip
    return {"rel": rel, "worst_segment": worst, "segments": segs, "ece_delta": ece_delta,
            "improves": improves, "passes": passes}  # fmt: skip


def evaluate(pred: pl.DataFrame, cup_names: dict[int, str], max_goals: int,
             terms: tuple[str, ...] = TERMS, log: Callable[[str], None] = print,
             ) -> dict[str, Any]:  # fmt: skip
    """A1 rescored (checked against its stored probabilities) and each term against it."""
    t = from_a1(pred)
    a1 = score(t, (np.zeros((t.n, 1)), np.zeros((t.n, 1))), max_goals)
    stored = t.frame.select("match_id", "p_home", "p_draw", "p_away")
    check = a1.select("match_id", "p_home", "p_draw", "p_away").join(stored, on="match_id",
                                                                       suffix="_a1")  # fmt: skip
    drift = max(float(np.abs(check[c].to_numpy() - check[f"{c}_a1"].to_numpy()).max())
                for c in ("p_home", "p_draw", "p_away"))  # fmt: skip
    out: dict[str, Any] = {"n": t.n, "a1_rescore_max_diff": drift, "terms": {}}
    obs = {"home": float((t.hg > t.ag).mean()), "draw": float((t.hg == t.ag).mean()),
           "away": float((t.hg < t.ag).mean())}  # fmt: skip
    for name in terms:
        term = TERM[name]
        log(f"{name}: {int(active(name, t).sum())} of {t.n} ties moved by the term")
        fit: dict[str, Any] = {}
        if term.n_params:
            table = loglik_table(term, t, max_goals)
            params, fits = walk_forward(term, t, table)
            last = fits[-1]
            fit = {
                "last": {k: last[k] for k in ("mean", "sd", "n_train")},
                "max_edge_mass": max(f["edge_mass"] for f in fits),
                "prior_sensitivity": {
                    str(sd): summary(term, posterior(table.sum(0), log_prior(term, sd)))["mean"]
                    for sd in SENSITIVITY_SD
                },
            }  # fmt: skip
            off = term.offsets(t, params)
        else:
            off = term.offsets(t, np.zeros((t.n, 0)))
        cand = score(t, off, max_goals)
        out["terms"][name] = {
            "moved": int(active(name, t).sum()), "fit": fit,
            "ppc": {"home": _mean(cand["p_home"]), "draw": _mean(cand["p_draw"]),
                    "away": _mean(cand["p_away"]), "observed": obs},
            **compare(a1, cand, cup_names),
        }  # fmt: skip
    return out


def markdown(rep: dict[str, Any]) -> str:
    lines = ["# Stage A, A2: cup terms against A1", "",
             f"{rep['n']} dev cup ties. A1 rescored from its draws: largest probability "
             f"difference from the stored A1 {rep['a1_rescore_max_diff']:.1e}.", "",
             "| term | ties moved | 1X2 log loss | RPS | 90' score log loss | worst cup segment "
             "| ECE h d a | passes |", "|---|---|---|---|---|---|---|---|"]  # fmt: skip

    def cell(r: dict[str, Any], m: str) -> str:
        x = r["rel"][m]
        return f"{x['rel']:+.2%} [{x['lo']:+.2%}, {x['hi']:+.2%}]"

    for name, r in rep["terms"].items():
        w, e = r["worst_segment"], r["ece_delta"]
        lines.append(f"| {name} | {r['moved']} | {cell(r, '1X2:log_loss')} | "
                     f"{cell(r, '1X2:rps')} | {cell(r, 'score:log_loss')} | "
                     f"{w['cup']} {w['metric']} {w['rel']:+.2%} | "
                     f"{e['home']:+.4f} {e['draw']:+.4f} {e['away']:+.4f} | "
                     f"{'yes' if r['passes'] else 'no'} |")  # fmt: skip
    lines.append("")
    for name, r in rep["terms"].items():
        f = r["fit"]
        if f:
            lines.append(f"- {name} fit: last posterior mean {f['last']['mean']} sd "
                         f"{f['last']['sd']} on {f['last']['n_train']} ties; largest edge mass "
                         f"{f['max_edge_mass']:.1e}; prior sd 0.1 / 0.25 / 0.5 gives "
                         f"{list(f['prior_sensitivity'].values())}.")  # fmt: skip
        pp = r["ppc"]
        lines.append(f"- {name} predictive home / draw / away {pp['home']:.3f} / "
                     f"{pp['draw']:.3f} / {pp['away']:.3f}; observed "
                     f"{pp['observed']['home']:.3f} / {pp['observed']['draw']:.3f} / "
                     f"{pp['observed']['away']:.3f}.")  # fmt: skip
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI


def main() -> None:
    import yaml

    from adatta.adapters.storage import try_get
    from adatta.features import cup_ties
    from adatta.model import backtest as bt

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--a1", required=True, metavar="RUN_ID", help="the cups.yml run of A1")
    r.add_argument("--terms", default=",".join(TERMS))
    r.add_argument("--out", default=OUT_DEFAULT)
    r.add_argument("--cache", default="data/cache")
    args = ap.parse_args()
    cfg = harness.load_config()
    data = try_get(bt._store(args.cache), f"cups/reports/{args.a1}/predictions.parquet")
    if data is None:
        raise SystemExit(f"no A1 predictions for run {args.a1}")
    pred = pl.read_parquet(io.BytesIO(data))
    names = {int(k): str(v["name"]) for k, v in (yaml.safe_load(
        cup_ties.FORMATS_PATH.read_text())["formats"]).items()}  # fmt: skip
    rep = evaluate(pred, names, cfg.max_goals, tuple(args.terms.split(",")))
    out = Path(args.out) / args.a1
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(rep, indent=1, default=str))
    (out / "summary.md").write_text(markdown(rep))
    print(markdown(rep))


if __name__ == "__main__":
    main()
