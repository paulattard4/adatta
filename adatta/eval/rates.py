"""P3-d0 evaluation (docs/PHASE3_TASKS.md, spec §7.4): inputs, the cell design, as-of player
history, the predictive distributions (given posterior draws as NumPy arrays), the R0 and R1
baselines, the dispersion choice, metrics, the pass rule, the leakage check and the report.

No JAX here: the hyperparameter fit is in adatta/model/rates.py (model group, CI only).
Dev only: seasons after 2023 are never loaded.

Rate of statistic s for appearance m of player i:
    E[y] = e_m · exp(L[cell_m]) · θ_i,   θ_i ~ Gamma(k[kline_i], k[kline_i]),
    L[cell] = log_mu[line] + log_l[league] + b_sub sub + b_new new_club + b_ret ret (+ slot)
so a player's posterior is Gamma(k + Y_i, k + Λ_i) with Λ_i = Σ_m e_m exp(L[cell_m]).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy import optimize, sparse
from scipy.special import gammaln
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.storage import RawStore
from adatta.db import models
from adatta.eval.selection import DEV, LIVE_FROM, WARMUP, check_max_season
from adatta.features import appearances as ap
from adatta.features import lineups as lu
from adatta.features import rates as fr
from adatta.features.referees import RESULT_LAG

Arr = NDArray[np.float64]
IntArr = NDArray[np.int64]
STATS = fr.STATS
LINES = fr.LINES
HISTORY_DAYS = 730
FIXED_LINE = 0  # goalkeepers (line G): no player term except for yellow cards
THETA_STATS = ("yellow",)  # statistics where goalkeepers keep a player term
EPS = 1e-12
RATE_FLOOR = 1e-6
C_GRID = (1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0)  # R1 pseudo-exposure, in 90s
SPARSE_E = 10.0  # under 900 minutes of history: a sparse player in the report
THETA_SAMPLES = 8  # per hyperparameter draw, for the NB-within predictive
EO_BAND = (0.95, 1.05)
ECE_MAX = 0.02
SEGMENT_MAX = 0.01  # no statistic or league worse than R1 by more than 1.0% (D28)
KEY = ["match_id", "team_id", "player_id"]


# ----------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Raw:
    appearances: pl.DataFrame
    lineups: pl.DataFrame
    scores: pl.DataFrame
    team_shots: pl.DataFrame


def load_raw(session: Session, store: RawStore, leagues: Sequence[str],
             max_season: int = DEV[-1], live: bool = False) -> Raw:  # fmt: skip
    """`live` loads through the live season, the holdout seasons as history (P3-e (f))."""
    check_max_season(max_season, live)
    groups = [(lg, y) for lg in leagues for y in range(WARMUP[0], max_season + 1)]
    app = ap.load(store, groups)
    lineups, _, _ = lu.load(store)
    M, R = models.Match, models.MatchResult
    scores = pl.DataFrame(
        session.execute(
            select(M.match_id, M.home_team_id, M.away_team_id, R.home_goals_nt, R.away_goals_nt)
            .join(R, R.match_id == M.match_id).where(R.superseded_at.is_(None))
        ).all(),
        schema={"match_id": pl.Int64, "home_team_id": pl.Int64, "away_team_id": pl.Int64,
                "home_goals": pl.Int64, "away_goals": pl.Int64}, orient="row",
    )  # fmt: skip
    scores = scores.filter(pl.col("match_id").is_in(app["match_id"].unique().implode()))
    T = models.TeamMatchStat
    shots = pl.DataFrame(
        session.execute(select(T.match_id, T.team_id, T.shots_on_target)
                        .where(T.superseded_at.is_(None))).all(),
        schema={"match_id": pl.Int64, "team_id": pl.Int64, "shots": pl.Int64}, orient="row",
    )  # fmt: skip
    shots = shots.filter(pl.col("match_id").is_in(app["match_id"].unique().implode()))
    return Raw(app, lineups, scores, shots)


# ------------------------------------------------------------------------------ cells


@dataclass(frozen=True)
class Design:
    """Encodes an appearance's (line, league, sub, new_club, ret, slot) as one cell id."""

    leagues: tuple[str, ...]
    slots: tuple[str, ...] = ()  # empty: the line-level model (no slot term)

    @property
    def n_slot(self) -> int:
        return len(self.slots) + 1

    def frame(self, rows: pl.DataFrame) -> pl.DataFrame:
        """Adds li, gi, si and cell."""
        li = pl.col("line").replace_strict({x: i for i, x in enumerate(LINES)},
                                           return_dtype=pl.Int64)  # fmt: skip
        gi = pl.col("league_ref").replace_strict({x: i for i, x in enumerate(self.leagues)},
                                                 return_dtype=pl.Int64)  # fmt: skip
        if self.slots:
            si = pl.col("slot").replace_strict({x: i + 1 for i, x in enumerate(self.slots)},
                                               default=0, return_dtype=pl.Int64)  # fmt: skip
        else:
            si = pl.lit(0, dtype=pl.Int64)
        ng, ns = len(self.leagues), self.n_slot
        bit = [pl.col(c).cast(pl.Int64) for c in ("sub", "new_club", "ret")]
        cell = ((((pl.col("li") * ng + pl.col("gi")) * 2 + bit[0]) * 2 + bit[1]) * 2
                + bit[2]) * ns + pl.col("si")  # fmt: skip
        return rows.with_columns(li=li, gi=gi, si=si).with_columns(cell=cell)

    def decode(self, cells: IntArr) -> dict[str, IntArr]:
        ns, ng = self.n_slot, len(self.leagues)
        si, r = cells % ns, cells // ns
        ret, r = r % 2, r // 2
        new, r = r % 2, r // 2
        sub, r = r % 2, r // 2
        gi, li = r % ng, r // ng
        return {"li": li, "gi": gi, "sub": sub, "new": new, "ret": ret, "si": si}


def log_rates(post: dict[str, Arr], d: Design, cells: IntArr) -> Arr:
    """(draws, len(cells)) log rate per 90 at θ = 1."""
    x = d.decode(np.asarray(cells, dtype=np.int64))
    n = len(post["log_mu"])
    lg = np.concatenate([np.zeros((n, 1)), post["log_l"]], axis=1)
    b = post["b"]
    out = (post["log_mu"][:, x["li"]] + lg[:, x["gi"]] + b[:, 0, None] * x["sub"]
           + b[:, 1, None] * x["new"] + b[:, 2, None] * x["ret"])  # fmt: skip
    if "slot" in post and d.slots:
        sl = np.concatenate([np.zeros((n, 1)), post["slot"]], axis=1)
        out = out + sl[:, x["si"]]
    return np.asarray(out, dtype=np.float64)


def fixed(stat: str, kline: IntArr) -> NDArray[np.bool_]:
    """Rows with no player term: goalkeepers, except for yellow cards."""
    return np.asarray((kline == FIXED_LINE) & (stat not in THETA_STATS), dtype=np.bool_)


# ---------------------------------------------------------------------------- history


def as_of(block: pl.DataFrame) -> pl.DataFrame:
    """Adds the history window of each row: appearances final by kickoff - 24 h, within
    the HISTORY_DAYS before it."""
    t = pl.col("kickoff") - fr.HORIZON
    return block.with_columns(cut=t - RESULT_LAG, start=t - timedelta(days=HISTORY_DAYS))


def history(hist: pl.DataFrame, block: pl.DataFrame, stat: str,
            ) -> tuple[pl.DataFrame, pl.DataFrame]:  # fmt: skip
    """As-of history of each block row (column j) for one statistic: pairs (j, cell, E) and
    per row (j, Y, E90, n, kline). `hist` carries cell and li; `block` carries j, cut and
    start (`as_of`). A row with no history is absent from both."""
    y = f"y_{stat}"
    h = hist.filter(pl.col(y).is_not_null()).select("player_id", "kickoff", "e", "cell", "li", y)
    j = block.select("j", "player_id", "cut", "start").join(h, on="player_id")
    j = j.filter((pl.col("kickoff") <= pl.col("cut")) & (pl.col("kickoff") > pl.col("start")))
    pairs = j.group_by("j", "cell").agg(pl.col("e").sum().alias("E")).sort("j", "cell")
    lines = j.group_by("j", "li").agg(pl.len().alias("c"))
    lines = lines.sort("j", "c", "li", descending=[False, True, False])
    summ = j.group_by("j").agg(pl.col(y).sum().cast(pl.Float64).alias("Y"),
                               pl.col("e").sum().alias("E90"), pl.len().alias("n"))  # fmt: skip
    summ = summ.join(lines.group_by("j").agg(pl.col("li").first().alias("kline")), on="j")
    return pairs, summ.sort("j")


@dataclass(frozen=True)
class Block:
    """One statistic's rows to score, with their as-of history, as arrays."""

    rows: pl.DataFrame  # the block rows with y not null, in j order
    y: Arr
    e: Arr
    cell: IntArr
    li: IntArr
    gi: IntArr
    kline: IntArr
    Y: Arr
    E90: Arr
    H: sparse.csr_matrix  # (rows, used cells) exposure by cell
    used: IntArr  # the cell ids of H's columns


def block_arrays(hist: pl.DataFrame, block: pl.DataFrame, stat: str) -> Block:
    """`hist` and `block` already carry the design columns (Design.frame)."""
    y = f"y_{stat}"
    rows = as_of(block.filter(pl.col(y).is_not_null())).with_row_index("j")
    pairs, summ = history(hist, rows, stat)
    rows = rows.join(summ, on="j", how="left").sort("j")
    rows = rows.with_columns(pl.col("Y").fill_null(0.0), pl.col("E90").fill_null(0.0),
                             pl.col("n").fill_null(0),
                             kline=pl.coalesce("kline", "li"))  # fmt: skip
    used = np.unique(np.concatenate([pairs["cell"].to_numpy(), rows["cell"].to_numpy()]))
    col = np.searchsorted(used, pairs["cell"].to_numpy())
    H = sparse.csr_matrix((pairs["E"].to_numpy(), (pairs["j"].to_numpy().astype(np.int64), col)),
                          shape=(len(rows), len(used)))  # fmt: skip
    return Block(rows, rows[y].to_numpy().astype(float), rows["e"].to_numpy(),
                 rows["cell"].to_numpy().astype(np.int64), rows["li"].to_numpy().astype(np.int64),
                 rows["gi"].to_numpy().astype(np.int64), rows["kline"].to_numpy().astype(np.int64),
                 rows["Y"].to_numpy(), rows["E90"].to_numpy(), H, used)  # fmt: skip


# ------------------------------------------------------------------------- predictive


def _nb_logpmf(y: Arr, r: Arr, mu: Arr) -> Arr:
    """Negative binomial with size r and mean mu."""
    return np.asarray(gammaln(y + r) - gammaln(r) - gammaln(y + 1) + r * np.log(r / (r + mu))
                      + y * np.log(mu / (r + mu) + EPS), dtype=np.float64)  # fmt: skip


def _pois_logpmf(y: Arr, mu: Arr) -> Arr:
    return np.asarray(y * np.log(mu + EPS) - mu - gammaln(y + 1), dtype=np.float64)


def _logmeanexp(a: Arr, axis: int) -> Arr:
    m = a.max(axis=axis, keepdims=True)
    return np.asarray(np.log(np.exp(a - m).mean(axis=axis)) + m.squeeze(axis), dtype=np.float64)


@dataclass(frozen=True)
class Prediction:
    mean: Arr
    p0: Arr
    log_loss: Arr


def predict(post: dict[str, Arr], d: Design, b: Block, stat: str,
            phi: dict[int, float] | None = None,
            rng: np.random.Generator | None = None) -> Prediction:  # fmt: skip
    """Posterior predictive of each block row, a mixture over the hyperparameter draws.
    Poisson within a player: the closed-form Poisson-Gamma predictive (NB). With `phi`
    (appearance line -> NB size) the within-player count is NB(θλ, φ), θ sampled."""
    L = log_rates(post, d, b.used)  # (D, used)
    lam_cell = np.exp(L)
    Lam = np.asarray(b.H @ lam_cell.T)  # (n, D): expected history count at θ = 1
    col = np.searchsorted(b.used, b.cell)
    lam = b.e[:, None] * lam_cell[:, col].T  # (n, D)
    k = np.exp(post["log_k"][:, b.kline]).T  # (n, D)
    a, rate = k + b.Y[:, None], k + Lam
    fx = fixed(stat, b.kline)
    mean = np.where(fx[:, None], lam, lam * a / rate).mean(axis=1)
    y = b.y[:, None]
    zero = np.zeros_like(y)

    def mix(yy: Arr) -> Arr:
        lp = np.where(fx[:, None], _pois_logpmf(yy, lam),
                      _nb_logpmf(yy, a, lam * a / rate))  # fmt: skip
        return _logmeanexp(lp, axis=1)

    lp_y, lp_0 = mix(y), mix(zero)
    if phi:
        ph = np.array([phi.get(int(x), np.nan) for x in b.li])
        on = np.isfinite(ph)
        if on.any():
            rng = rng or np.random.default_rng(0)
            n, D = lam[on].shape
            th = rng.gamma(np.repeat(a[on][..., None], THETA_SAMPLES, -1),
                           1.0 / np.repeat(rate[on][..., None], THETA_SAMPLES, -1))  # fmt: skip
            th = np.where(fx[on][:, None, None], 1.0, th)
            mu = (lam[on][..., None] * th).reshape(n, -1)
            r = np.broadcast_to(ph[on][:, None], mu.shape)
            yy = np.broadcast_to(b.y[on][:, None], mu.shape)
            lp_y[on] = _logmeanexp(_nb_logpmf(yy, r, mu), 1)
            lp_0[on] = _logmeanexp(_nb_logpmf(np.zeros_like(mu), r, mu), 1)
    return Prediction(mean, np.exp(lp_0), -lp_y)


def theta_hat(post: dict[str, Arr], d: Design, cells: IntArr, e: Arr, player: IntArr,
              Y: Arr, kline: IntArr, stat: str) -> tuple[Arr, Arr]:  # fmt: skip
    """Plug-in (per row) λ and θ̂ = (k + Y_i) / (k + Λ_i) at the posterior-mean
    hyperparameters, for the within-player dispersion fit. `player` indexes Y and kline."""
    mean = {k_: v.mean(axis=0, keepdims=True) for k_, v in post.items()}
    lam = e * np.exp(log_rates(mean, d, cells)[0])
    Lam = np.bincount(player, weights=lam, minlength=len(Y))
    k = np.exp(mean["log_k"][0, kline])
    th = np.where(fixed(stat, kline), 1.0, (k + Y) / (k + Lam))
    return lam, th[player]


def fit_phi(y: Arr, mu: Arr) -> float:
    """Maximum-likelihood NB size for counts y with means mu (bounded 0.05..1e4)."""
    if len(y) == 0:
        return float("nan")

    def nll(lp: float) -> float:
        return float(-_nb_logpmf(y, np.full_like(mu, np.exp(lp)), mu).sum())

    r = optimize.minimize_scalar(nll, bounds=(np.log(0.05), np.log(1e4)), method="bounded")
    return float(np.exp(r.x))


# ----------------------------------------------------------------------------- baselines


def r0_table(train: pl.DataFrame, stat: str, n_leagues: int) -> Arr:
    """(4 lines, leagues) per-90 rate over the window; a cell with no exposure takes its
    line's rate; floored at RATE_FLOOR."""
    y = f"y_{stat}"
    t = train.filter(pl.col(y).is_not_null())
    out = np.full((len(LINES), n_leagues), np.nan)
    agg = t.group_by("li", "gi").agg(pl.col(y).sum().alias("Y"), pl.col("e").sum().alias("E"))
    for li, gi, Y, E in agg.iter_rows():
        if E > 0:
            out[li, gi] = Y / E
    line = t.group_by("li").agg(pl.col(y).sum().alias("Y"), pl.col("e").sum().alias("E"))
    overall = float(t[y].sum()) / max(float(t["e"].sum()), EPS)
    for li in range(len(LINES)):
        r = line.filter(pl.col("li") == li)
        lr = float(r["Y"][0] / r["E"][0]) if len(r) and r["E"][0] > 0 else overall
        out[li] = np.where(np.isnan(out[li]), lr, out[li])
    return np.asarray(np.maximum(out, RATE_FLOOR), dtype=np.float64)


def baselines(b: Block, r0: Arr, c: float) -> dict[str, Prediction]:
    base = r0[b.li, b.gi]
    out = {}
    for name, rate in (("R0", base), ("R1", (b.Y + c * base) / (b.E90 + c))):
        mu = b.e * np.maximum(rate, RATE_FLOOR)
        out[name] = Prediction(mu, np.exp(-mu), -_pois_logpmf(b.y, mu))
    return out


# ------------------------------------------------------------------------------ scoring


def scored(b: Block, stat: str, model: str, p: Prediction) -> pl.DataFrame:
    r = b.rows
    return pl.DataFrame({
        "model": model, "match_id": r["match_id"], "team_id": r["team_id"],
        "player_id": r["player_id"], "league_ref": r["league_ref"], "season": r["season"],
        "line": r["line"], "stat": stat, "y": b.y, "mean": p.mean, "p0": p.p0,
        "log_loss": p.log_loss, "sub": r["sub"], "new_club": r["new_club"], "ret": r["ret"],
        "hist_e": b.E90, "new_player": r["n"] == 0, "positional": r["slot"].is_not_null(),
    })  # fmt: skip


def paired(s: pl.DataFrame, a: str, b: str, by: str | None = None) -> pl.DataFrame:
    """Mean per-appearance difference a - b of the log loss summed over the statistics in
    `s`, with a normal 95% interval; rel = mean / b's mean. Negative favours a."""
    keys = KEY + ([by] if by else [])
    per = (
        s.filter(pl.col("model").is_in([a, b]))
        .group_by("model", *keys)
        .agg(pl.col("log_loss").sum())
    )
    wide = per.pivot(on="model", index=keys, values="log_loss").drop_nulls()
    wide = wide.with_columns(d=pl.col(a) - pl.col(b))
    agg = [pl.len().alias("n"), pl.col("d").mean().alias("mean"),
           (1.96 * pl.col("d").std() / pl.len().sqrt()).alias("half"),
           pl.col(b).mean().alias("base")]  # fmt: skip
    out = wide.group_by(by).agg(agg).sort(by) if by else wide.select(agg)
    return out.with_columns(lo=pl.col("mean") - pl.col("half"), hi=pl.col("mean") + pl.col("half"),
                            rel=pl.col("mean") / pl.col("base"))  # fmt: skip


def eo(s: pl.DataFrame, by: Sequence[str] = ("stat",)) -> pl.DataFrame:
    return s.group_by(*by).agg((pl.col("mean").sum() / pl.col("y").sum()).alias("eo")).sort(*by)


def ece(s: pl.DataFrame, bins: int = 10) -> pl.DataFrame:
    """Per statistic: ECE of P(count >= 1)."""
    x = s.with_columns(p1=1 - pl.col("p0"), obs=(pl.col("y") >= 1).cast(pl.Float64))
    x = x.with_columns(bin=(pl.col("p1") * bins).floor().clip(0, bins - 1))
    per = x.group_by("stat", "bin").agg(pl.len().alias("n"), pl.col("p1").mean().alias("pred"),
                                        pl.col("obs").mean())  # fmt: skip
    return per.group_by("stat").agg(
        ((pl.col("pred") - pl.col("obs")).abs() * pl.col("n")).sum().truediv(pl.col("n").sum())
        .alias("ece")
    ).sort("stat")  # fmt: skip


def dispersion_choice(pois: pl.DataFrame, nb: pl.DataFrame) -> list[dict[str, Any]]:
    """Per statistic x line: NB - Poisson paired log loss on the warm-up scoring season;
    NB where the 95% interval is below zero."""
    both = pl.concat([pois.with_columns(model=pl.lit("pois")), nb.with_columns(model=pl.lit("nb"))])
    out = []
    for stat in both["stat"].unique().sort():
        per = paired(both.filter(pl.col("stat") == stat), "nb", "pois", by="line")
        for r in per.iter_rows(named=True):
            out.append({"stat": stat, "line": r["line"], "n": r["n"], "diff": r["mean"],
                        "lo": r["lo"], "hi": r["hi"], "rel": r["rel"],
                        "family": "nb" if r["hi"] < 0 else "poisson"})  # fmt: skip
    return out


def phi_lines(choice: list[dict[str, Any]], stat: str) -> list[int]:
    return [LINES.index(r["line"]) for r in choice if r["stat"] == stat and r["family"] == "nb"]


# ------------------------------------------------------------------------------ verdict


@dataclass
class Verdict:
    passed: bool
    checks: list[dict[str, Any]]


def verdict(s: pl.DataFrame, meta: dict[str, Any], inference_ok: bool) -> Verdict:
    """The P3-d0 pass rule (f). `s` holds the model, R0 and R1 rows (no slot rows)."""
    checks: list[dict[str, Any]] = []
    failed = [c for c in meta["checks_a"] if not c["passed"]]
    checks.append({"check": "(a) checks pass or the statistic drops out", "value": len(failed),
                   "pass": bool(meta.get("drops_applied", False))})  # fmt: skip
    for stat in STATS:
        r = paired(s.filter(pl.col("stat") == stat), "model", "R0").row(0, named=True)
        checks.append({"check": f"{stat} vs R0", "value": r["rel"], "lo": r["lo"],
                       "hi": r["hi"], "pass": r["hi"] < 0})  # fmt: skip
    r = paired(s, "model", "R1").row(0, named=True)
    checks.append({"check": "six-statistic sum vs R1", "value": r["rel"], "lo": r["lo"],
                   "hi": r["hi"], "pass": r["hi"] < 0})  # fmt: skip
    for stat in STATS:
        r = paired(s.filter(pl.col("stat") == stat), "model", "R1").row(0, named=True)
        checks.append({"check": f"{stat} vs R1 (at most +1.0%)", "value": r["rel"],
                       "pass": r["rel"] <= SEGMENT_MAX})  # fmt: skip
    for r in paired(s, "model", "R1", by="league_ref").iter_rows(named=True):
        checks.append({"check": f"league {r['league_ref']} vs R1 (at most +1.0%)",
                       "value": r["rel"], "pass": r["rel"] <= SEGMENT_MAX})  # fmt: skip
    m = s.filter(pl.col("model") == "model")
    for r in eo(m).iter_rows(named=True):
        checks.append({"check": f"{r['stat']} E/O", "value": r["eo"],
                       "pass": EO_BAND[0] <= r["eo"] <= EO_BAND[1]})  # fmt: skip
    for r in ece(m).iter_rows(named=True):
        checks.append({"check": f"{r['stat']} ECE of P(>=1)", "value": r["ece"],
                       "pass": r["ece"] <= ECE_MAX})  # fmt: skip
    checks.append({"check": "inference gates", "value": float(inference_ok),
                   "pass": inference_ok})  # fmt: skip
    checks.append({"check": "leakage", "value": float(meta["leakage_ok"]),
                   "pass": bool(meta["leakage_ok"])})  # fmt: skip
    return Verdict(all(c["pass"] for c in checks), checks)


def _f(x: Any, pct: bool = False) -> str:
    if x is None:
        return ""
    return f"{x:+.2%}" if pct else f"{x:.4f}"


def markdown(s: pl.DataFrame, slot: pl.DataFrame, v: Verdict, diags: list[dict[str, Any]],
             meta: dict[str, Any], choices: dict[str, Any], digest: str) -> str:  # fmt: skip
    """The P3-d0 report: metrics only (the repo and its logs are public)."""
    L = [f"# P3-d0 player rates — dev, table {digest}", "",
         f"**Verdict: {'pass' if v.passed else 'fail'}**", "",
         "| check | value | 95% interval | pass |", "|---|---|---|---|"]  # fmt: skip
    for c in v.checks:
        rel = "vs" in c["check"]
        iv = f"[{_f(c['lo'])}, {_f(c['hi'])}]" if "lo" in c else ""
        L.append(
            f"| {c['check']} | {_f(c['value'], rel)} | {iv} | {'yes' if c['pass'] else 'NO'} |"
        )
    m = s.filter(pl.col("model") == "model")
    L += ["", "## Log loss per appearance by statistic", "",
          "| statistic | n | model | R0 | R1 | vs R0 | vs R1 | E/O | ECE |",
          "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    eo_ = dict(eo(m).iter_rows())
    ece_ = dict(ece(m).iter_rows())
    for stat in STATS:
        x = s.filter(pl.col("stat") == stat)
        mean = dict(x.group_by("model").agg(pl.col("log_loss").mean()).iter_rows())
        r0 = paired(x, "model", "R0").row(0, named=True)
        r1 = paired(x, "model", "R1").row(0, named=True)
        L.append(f"| {stat} | {r0['n']} | {mean['model']:.4f} | {mean['R0']:.4f} | "
                 f"{mean['R1']:.4f} | {r0['rel']:+.2%} | {r1['rel']:+.2%} | "
                 f"{eo_.get(stat, np.nan):.3f} | {ece_.get(stat, np.nan):.4f} |")  # fmt: skip
    for by in ("league_ref", "line", "season"):
        L += ["", f"## Six-statistic sum by {by}", "", f"| {by} | n | vs R0 | vs R1 |",
              "|---|---|---|---|"]  # fmt: skip
        a = paired(s, "model", "R0", by=by)
        b = paired(s, "model", "R1", by=by).select(by, r1="rel")
        for r in a.join(b, on=by).iter_rows(named=True):
            L.append(f"| {r[by]} | {r['n']} | {r['rel']:+.2%} | {r['r1']:+.2%} |")
    L += ["", "## Segments (six-statistic sum, reported)", "",
          "| segment | n | vs R0 | vs R1 |", "|---|---|---|---|"]  # fmt: skip
    segs = {"sparse (under 900 min of history)": pl.col("hist_e") < SPARSE_E,
            "new to the ten leagues": pl.col("new_player"),
            "new signing (first 5)": pl.col("new_club"), "returner (first 3)": pl.col("ret"),
            "substitute appearance": pl.col("sub")}  # fmt: skip
    for name, cond in segs.items():
        x = s.filter(cond)
        if x.is_empty():
            continue
        ra = paired(x, "model", "R0").row(0, named=True)
        rb = paired(x, "model", "R1").row(0, named=True)
        L.append(f"| {name} | {ra['n']} | {ra['rel']:+.2%} | {rb['rel']:+.2%} |")
    L += ["", "## Dispersion (warm-up: fit 2016-2017, score 2018)", "",
          "| statistic | line | n | NB - Poisson | 95% interval | family |",
          "|---|---|---|---|---|---|"]  # fmt: skip
    for r in choices["dispersion"]:
        L.append(
            f"| {r['stat']} | {r['line']} | {r['n']} | {r['diff']:+.5f} | "
            f"[{r['lo']:+.5f}, {r['hi']:+.5f}] | {r['family']} |"
        )
    L += [
        "",
        "R1 pseudo-exposure c (90s, warm-up): "
        + ", ".join(f"{k} {v:g}" for k, v in choices["c"].items()),
    ]
    if not slot.is_empty():
        L += [
            "",
            "## Slot codes, positional starters 2022-2023 (d)",
            "",
            "| statistic | n | slot vs line | 95% interval |",
            "|---|---|---|---|",
        ]
        for stat in STATS:
            x = slot.filter(pl.col("stat") == stat)
            if x.is_empty():
                continue
            r = paired(x, "slot", "model").row(0, named=True)
            L.append(f"| {stat} | {r['n']} | {r['rel']:+.2%} | [{r['lo']:+.5f}, {r['hi']:+.5f}] |")
        r = paired(slot, "slot", "model").row(0, named=True)
        adopt = r["hi"] < 0
        L.append(f"| sum | {r['n']} | {r['rel']:+.2%} | [{r['lo']:+.5f}, {r['hi']:+.5f}] |")
        L += ["", f"Slot codes adopted for positional seasons: **{'yes' if adopt else 'no'}**"]
    g = (
        m.filter(pl.col("stat") == "goals")
        .group_by("match_id", "team_id")
        .agg(pl.col("mean").sum(), pl.col("y").sum())
    )
    if not g.is_empty():
        corr = float(np.corrcoef(g["mean"].to_numpy(), g["y"].to_numpy())[0, 1])
        ratio = float(g["mean"].sum()) / float(g["y"].sum())
        L += ["", f"For P3-e: summed player expected goals per team-match E/O {ratio:.3f}, "
              f"correlation with team goals {corr:.3f} ({len(g)} team-matches)."]  # fmt: skip
    fits = [x for d in diags for x in d.get("fits", [])]
    if fits:
        fs = np.array([f["fit_s"] for f in fits])
        bad = [f for f in fits if not f["gates_ok"]]
        L += ["", f"Inference: {len(fits)} fits, {len(bad)} failing the gates; "
              f"fit median {np.median(fs):.0f} s, max {fs.max():.0f} s."]  # fmt: skip
        L += [
            "",
            "| statistic | b_sub | b_new | b_ret | PPC outside the band |",
            "|---|---|---|---|---|",
        ]
        for stat in STATS:
            fx = [f for f in fits if f["stat"] == stat and f.get("variant", "line") == "line"]
            if not fx:
                continue
            bm = np.mean([f["b"] for f in fx], axis=0)
            ppc = np.mean([not f["ppc_ok"] for f in fx])
            L.append(f"| {stat} | {bm[0]:+.3f} | {bm[1]:+.3f} | {bm[2]:+.3f} | {ppc:.0%} |")
        for f in bad[:20]:
            L.append(f"- gates fail: {f['refit']} {f['stat']} {f.get('variant', 'line')}: "
                     f"rhat {f['rhat']:.3f}, ess {min(f['ess_bulk'], f['ess_tail']):.0f}, "
                     f"div {f['divergences']:.0f}")  # fmt: skip
    return "\n".join(L)


# ------------------------------------------------------------------------------ leakage


def leakage_check(app: pl.DataFrame, lineups: pl.DataFrame, table: pl.DataFrame,
                  leagues: Sequence[str], n: int = 100, seed: int = 20261118,
                  ) -> dict[str, Any]:  # fmt: skip
    """For n sampled dev rows: (i) the row's table inputs rebuilt from appearances final by
    its cutoff (plus its own match) are unchanged; (ii) its history sums for every
    statistic are unchanged when every later appearance's counts and minutes are altered."""
    start = cast(datetime, app.filter(pl.col("minutes") > 0)["kickoff"].min())
    dev = table.filter(pl.col("season").is_in(DEV))
    sample = dev.sample(min(n, len(dev)), seed=seed)
    d = Design(tuple(leagues))
    hist = d.frame(table)
    cols = ["line", "sub", "e", "new_club", "ret", "slot", *[f"y_{s}" for s in STATS]]
    bad: list[str] = []
    for r in sample.iter_rows(named=True):
        cut = r["kickoff"] - fr.HORIZON - RESULT_LAG
        mine = (pl.col("team_id") == r["team_id"]) | (pl.col("player_id") == r["player_id"])
        known = (pl.col("kickoff") <= cut) | (pl.col("match_id") == r["match_id"])
        sub = app.filter(mine & known)
        got = fr.build(sub, lineups, data_start=start).filter(
            (pl.col("match_id") == r["match_id"]) & (pl.col("player_id") == r["player_id"])
        )
        want = table.filter((pl.col("match_id") == r["match_id"])
                            & (pl.col("player_id") == r["player_id"]))  # fmt: skip
        if not got.select(cols).equals(want.select(cols)):
            bad.append(f"inputs {r['match_id']}/{r['player_id']}")
        one = d.frame(pl.DataFrame([r], schema=table.schema))
        later = pl.col("kickoff") > cut
        altered = hist.with_columns(
            e=pl.when(later).then(pl.col("e") * 2).otherwise(pl.col("e")),
            **{f"y_{s}": pl.when(later).then(pl.col(f"y_{s}") + 7).otherwise(pl.col(f"y_{s}"))
               for s in STATS},
        )  # fmt: skip
        for stat in STATS:
            if one[f"y_{stat}"][0] is None:
                continue
            a = block_arrays(hist, one, stat)
            b = block_arrays(altered, one, stat)
            same = (np.allclose(a.Y, b.Y) and np.allclose(a.E90, b.E90)
                    and np.array_equal(a.used, b.used) and np.allclose(a.H.toarray(), b.H.toarray())
                    and np.array_equal(a.kline, b.kline))  # fmt: skip
            if not same:
                bad.append(f"history {stat} {r['match_id']}/{r['player_id']}")
    return {"leakage_checked": len(sample), "leakage_changed": len(bad),
            "leakage_ok": not bad, "leakage_examples": bad[:5]}  # fmt: skip


# ------------------------------------------------------------------------------- CLI


def leagues() -> list[str]:
    from adatta.eval import harness

    cfg = harness.load_config()
    return sorted(str(x) for ids in cfg.bundles.values() for x in ids)


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session

    p = argparse.ArgumentParser(prog="adatta.eval.rates")
    p.add_argument("cmd", choices=["build-table"])
    p.add_argument("--leakage-n", type=int, default=100)
    p.add_argument(
        "--live",
        type=int,
        default=0,
        help="P3-e (f): build through this live season (holdout seasons as history)",
    )
    args = p.parse_args(argv)
    store = make_store(get_settings())
    lg = leagues()
    live = args.live > 0
    with get_session() as session:
        raw = load_raw(session, store, lg, args.live or DEV[-1], live)
    app = raw.appearances
    start = cast(datetime, app.filter(pl.col("minutes") > 0)["kickoff"].min())
    table = fr.build(app, raw.lineups, data_start=start)
    chk = fr.checks(fr.goal_check(app, raw.scores), fr.shots_check(app, raw.team_shots),
                    fr.event_sample(store, app))  # fmt: skip
    # the (a) checks drop a failing statistic in the scored seasons: dev, and live ones
    scored = pl.col("season").is_in(DEV) | ((pl.col("season") >= LIVE_FROM) & pl.lit(live))
    chk = chk.filter(scored)
    leak = leakage_check(app, raw.lineups, table, lg, n=args.leakage_n)  # before the drops
    table = fr.apply_drops(table, chk)
    meta: dict[str, Any] = {
        "leagues": lg, "rows": len(table), "data_start": str(start),
        "dropped_no_line": int(app.filter(pl.col("minutes") > 0).height - len(table)),
        "checks_a": chk.to_dicts(), "drops_applied": True,
        "slots": sorted(x for x in table["slot"].drop_nulls().unique().to_list()),
        **leak,
    }  # fmt: skip
    digest = fr.save(store, table, meta, latest=not live)
    print(json.dumps({"digest": digest, "rows": len(table),
                      "dropped_no_line": meta["dropped_no_line"],
                      "checks_failed": [f"{c['league_ref']} {c['season']} {c['check']}"
                                        for c in meta["checks_a"] if not c["passed"]],
                      "leakage_ok": meta["leakage_ok"],
                      "leakage_examples": meta["leakage_examples"]}, default=str))  # fmt: skip


if __name__ == "__main__":
    main()
