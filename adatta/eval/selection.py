"""P3-b evaluation (docs/PHASE3_TASKS.md): the selection table's inputs, the two baselines,
coherent per-team probabilities, the minutes distribution, the metrics and the report.

No JAX here: the hierarchical model is in adatta/model/selection.py (model group, CI only).
Dev only: holdout seasons are never loaded (their appearance files are not read).
"""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy import stats
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.features import appearances as ap
from adatta.features import context_table as ct
from adatta.features import selection as sel
from adatta.features.referees import RESULT_LAG

Arr = NDArray[np.float64]
MINUTES = np.arange(91)  # the normal-time minutes grid, 0..90
EPS = 1e-6
WARMUP = (2016, 2017, 2018)
DEV = (2019, 2020, 2021, 2022, 2023)
CONTEXT_REF = "b29bb7f0915abf76"  # the final F3 h48 snapshot (PHASE2_TASKS.md)


class HoldoutRefused(RuntimeError):
    pass


# ----------------------------------------------------------------------------- inputs


def load_inputs(session: Session, store: RawStore, leagues: Sequence[str],
                max_season: int = DEV[-1]) -> sel.Inputs:  # fmt: skip
    """Appearances of club teams (all stored competitions, seasons <= max_season), coaches,
    the h48 context snapshot and the bundle-league targets of seasons 2016..max_season."""
    if max_season > DEV[-1]:
        raise HoldoutRefused(f"season {max_season} is past the dev window")
    groups = [g for g in ap.targets(session) if g[1] <= max_season]
    app = ap.load(store, sorted(groups))
    T = models.Team
    clubs = {t for (t,) in session.execute(
        select(T.team_id).where(T.is_national.is_(False), T.team_level == "senior"))}  # fmt: skip
    app = app.filter(pl.col("team_id").is_in(list(clubs))).select(
        "match_id", "team_id", "player_id", "kickoff", "started", "minutes_nt", "sent_off", "pos"
    )
    K = models.MatchCoach
    coaches = pl.DataFrame(
        session.execute(select(K.match_id, K.team_id, K.coach_ref)
                        .where(K.superseded_at.is_(None))).all(),
        schema={"match_id": pl.Int64, "team_id": pl.Int64, "coach_ref": pl.Utf8}, orient="row",
    )  # fmt: skip
    ctx, _ = ct.load(store, timedelta(hours=48), CONTEXT_REF)
    M, S, C = models.Match, models.Season, models.Competition
    targets = pl.DataFrame(
        session.execute(
            select(M.match_id, C.source_ref, S.year, M.kickoff_time, M.home_team_id,
                   M.away_team_id)
            .join(S, S.season_id == M.season_id).join(C, C.competition_id == S.competition_id)
            .where(C.source_ref.in_(list(leagues)), S.year.between(WARMUP[0], max_season),
                   M.status.in_(ap.FINISHED))
        ).all(),
        schema={"match_id": pl.Int64, "league_ref": pl.Utf8, "season": pl.Int32,
                "kickoff": pl.Datetime("us", "UTC"), "home_team_id": pl.Int64,
                "away_team_id": pl.Int64}, orient="row",
    )  # fmt: skip
    return sel.Inputs(app, coaches, ctx, targets)


def save_table(store: RawStore, players: pl.DataFrame, teams: pl.DataFrame) -> str:
    d = sel.digest(players)
    store.put(f"{sel.PREFIX}/{d}/players.parquet", sel.to_bytes(players))
    store.put(f"{sel.PREFIX}/{d}/teams.parquet", sel.to_bytes(teams))
    store.put(f"{sel.PREFIX}/latest.json", json.dumps({"digest": d}).encode())
    return d


def load_table(store: RawStore, ref: str = "latest") -> tuple[pl.DataFrame, pl.DataFrame, str]:
    if ref == "latest":
        raw = try_get(store, f"{sel.PREFIX}/latest.json")
        if raw is None:
            raise FileNotFoundError("no selection table stored")
        ref = json.loads(raw)["digest"]
    players = pl.read_parquet(io.BytesIO(store.get(f"{sel.PREFIX}/{ref}/players.parquet")))
    teams = pl.read_parquet(io.BytesIO(store.get(f"{sel.PREFIX}/{ref}/teams.parquet")))
    return players, teams, ref


# ------------------------------------------------------------------- coherent probabilities


def waterfill(w: Arr, total: float, cap: Arr) -> Arr:
    """p_i = min(cap_i, c * w_i) with sum p = total (all caps if they sum to less)."""
    w = np.maximum(w, 0.0)
    if total <= 0 or w.sum() <= 0:
        return np.zeros_like(w)
    if cap.sum() <= total:
        return cap.copy()
    fixed = np.zeros(len(w), bool)
    p = np.zeros_like(w)
    for _ in range(len(w) + 1):
        free = ~fixed
        rest = total - cap[fixed].sum()
        c = rest / w[free].sum() if w[free].sum() > 0 else 0.0
        p = np.where(fixed, cap, np.minimum(cap, c * w))
        over = free & (c * w >= cap)
        if not over.any():
            break
        fixed |= over
    return p


def coherent(raw: Arr, gk: NDArray[np.bool_], starters: float, bench: float) -> Arr:
    """Three-way probabilities (start, bench, out) for one team-match's pool from raw
    per-player probabilities: starts sum to `starters` (one goalkeeper, if the pool has
    one), bench to `bench`, each player's three sum to 1; clipped away from 0 and 1."""
    one = np.ones(len(raw))
    s = np.zeros(len(raw))
    if gk.any() and starters >= 1:
        s[gk] = waterfill(raw[gk, 0], 1.0, one[gk])
        s[~gk] = waterfill(raw[~gk, 0], starters - 1.0, one[~gk])
    else:
        s = waterfill(raw[:, 0], starters, one)
    rest = 1.0 - s
    share = raw[:, 1] / np.maximum(raw[:, 1] + raw[:, 2], EPS)
    b = waterfill(share * rest, bench, rest)
    out = np.stack([s, b, 1.0 - s - b], axis=1)
    out = np.clip(out, EPS, 1.0)
    return out / out.sum(axis=1, keepdims=True)


def coherent_all(rows: pl.DataFrame, raw: Arr, teams: pl.DataFrame,
                 nu: dict[str, tuple[float, float]]) -> Arr:  # fmt: skip
    """`coherent` per team-match. nu: league -> expected newcomer starts and bench places
    (taken off the pool's totals, the same for every model)."""
    out = np.empty_like(raw)
    tm = {(r["match_id"], r["team_id"]): r for r in teams.iter_rows(named=True)}
    keys = rows.select("match_id", "team_id", "league_ref", "pos").with_row_index("i")
    for (m, t, lg), g in keys.group_by("match_id", "team_id", "league_ref"):
        i = g["i"].to_numpy()
        ns, nb = nu.get(str(lg), (0.0, 0.0))
        bench = max(tm[(m, t)]["bench_expected"] - nb, 0.0)
        out[i] = coherent(raw[i], (g["pos"] == "G").to_numpy(), 11.0 - ns, bench)
    return out


def newcomer_rates(teams: pl.DataFrame) -> dict[str, tuple[float, float]]:
    r = teams.group_by("league_ref").agg(pl.col("new_start").mean(), pl.col("new_bench").mean())
    return {str(a): (float(b), float(c)) for a, b, c in r.iter_rows()}


# -------------------------------------------------------------------------- baselines


@dataclass(frozen=True)
class Baselines:
    """B1: P(outcome | outcome in the team's previous match, position family); B2: the
    exponentially weighted start and squad shares, shrunk toward the position family's
    mean by lam (fitted on warm-up). Minutes: the player's last-10 empirical minutes, or the
    position family's warm-up distribution with fewer than 3 club matches."""

    b1: dict[tuple[int, str], Arr]
    pos_mean: dict[str, Arr]
    lam: float
    pos_minutes: dict[str, Arr]


def _last(rows: pl.DataFrame) -> NDArray[np.int64]:
    return np.where(rows["last_start"].to_numpy() > 0, sel.START,
                    np.where(rows["last_bench"].to_numpy() > 0, sel.BENCH, sel.OUT))  # fmt: skip


def _b2_raw(rows: pl.DataFrame) -> Arr:
    s = rows["ew_start"].to_numpy()
    q = rows["ew_squad"].to_numpy()
    return np.stack([s, q - s, 1 - q], axis=1)


def _pmf(minutes: Arr) -> Arr:
    h = np.bincount(np.clip(np.round(minutes), 0, 90).astype(int), minlength=91).astype(float)
    return np.asarray(h / max(h.sum(), 1.0), dtype=np.float64)


def fit_baselines(warm: pl.DataFrame) -> Baselines:
    """Fitted on warm-up rows only (the caller passes seasons 2016-2018)."""
    if warm["season"].max() > WARMUP[-1]:  # type: ignore[operator]
        raise ValueError("baselines are fitted on warm-up seasons only")
    y = warm["y"].to_numpy()
    last = _last(warm)
    pos = warm["pos"].to_numpy()
    b1: dict[tuple[int, str], Arr] = {}
    pos_mean: dict[str, Arr] = {}
    pos_minutes: dict[str, Arr] = {}
    # order: start, bench, out
    onehot = np.stack([y == sel.START, y == sel.BENCH, y == sel.OUT], axis=1).astype(float)
    for p in sorted(set(pos)):
        mp = pos == p
        pos_mean[p] = (onehot[mp].sum(0) + 1) / (mp.sum() + 3)
        pos_minutes[p] = _pmf(warm["minutes_nt"].to_numpy()[mp])
        for lo in (sel.START, sel.BENCH, sel.OUT):
            m = mp & (last == lo)
            b1[(lo, p)] = (onehot[m].sum(0) + 1) / (m.sum() + 3)
    raw = _b2_raw(warm)
    prior = np.stack([pos_mean[p] for p in pos])
    best, best_ll = 0.0, -np.inf
    for lam in np.linspace(0.0, 1.0, 101):
        q = np.clip((1 - lam) * raw + lam * prior, EPS, 1)
        ll = float(np.log((q * onehot).sum(1)).sum())
        if ll > best_ll:
            best, best_ll = float(lam), ll
    return Baselines(b1, pos_mean, best, pos_minutes)


def baseline_raw(b: Baselines, rows: pl.DataFrame) -> dict[str, Arr]:
    last = _last(rows)
    pos = rows["pos"].to_list()
    fallback = np.ones(3) / 3
    b1 = np.stack([b.b1.get((lo, p), fallback) for lo, p in zip(last, pos, strict=True)])
    prior = np.stack([b.pos_mean.get(p, fallback) for p in pos])
    b2 = (1 - b.lam) * _b2_raw(rows) + b.lam * prior
    return {"B1": b1, "B2": b2}


def baseline_minutes(b: Baselines, rows: pl.DataFrame) -> Arr:
    """(n, 91) pmf: the player's empirical minutes over their last 10 team matches (as many
    as the club history has), or the position family's warm-up pmf with fewer than 3."""
    hist = np.asarray(rows["m_hist"].to_list(), dtype=float)
    n_club = rows["n_club"].to_numpy()
    pos = rows["pos"].to_list()
    flat = np.ones(91) / 91
    out = np.empty((len(rows), 91))
    for i in range(len(rows)):
        h = hist[i][~np.isnan(hist[i])][: int(n_club[i])]
        out[i] = _pmf(h) if len(h) >= 3 else b.pos_minutes.get(pos[i], flat)
    return out


# --------------------------------------------------------------------------- minutes


def beta_grid(mean: Arr, phi: Arr) -> Arr:
    """(n, 91) pmf of 90 * Beta(mean, phi) on whole minutes 1..90 (minute m holds
    (m-1, m]); minute 0 gets no mass."""
    a, b = mean * phi, (1 - mean) * phi
    edges = np.arange(0, 91) / 90
    cdf = stats.beta.cdf(edges[None, :], a[:, None], b[:, None])
    pmf = np.zeros((len(mean), 91))
    pmf[:, 1:] = np.diff(cdf, axis=1)
    return pmf / np.maximum(pmf.sum(1, keepdims=True), EPS)


def minutes_pmf(p: Arr, q_full: Arr, start_beta: Arr, q_on: Arr, on_beta: Arr) -> Arr:
    """Zero-inflated minutes pmf from the three-way probabilities p (start, bench, out):
    a starter plays 90 with q_full, else start_beta; a substitute comes on with q_on and
    then plays on_beta; out of the squad plays 0."""
    pmf = np.zeros((len(p), 91))
    pmf[:, 0] = p[:, 2] + p[:, 1] * (1 - q_on)
    pmf[:, 90] += p[:, 0] * q_full
    pmf += (p[:, 0] * (1 - q_full))[:, None] * start_beta
    pmf += (p[:, 1] * q_on)[:, None] * on_beta
    return pmf / pmf.sum(1, keepdims=True)


# --------------------------------------------------------------------------- metrics


def score(rows: pl.DataFrame, p: Arr, pmf: Arr) -> pl.DataFrame:
    """Per player row: three-way log loss, Brier of start, minutes CRPS on the 0..90 grid."""
    y = rows["y"].to_numpy()
    col = np.select([y == sel.START, y == sel.BENCH], [0, 1], 2)
    ll = -np.log(p[np.arange(len(p)), col])
    brier = (p[:, 0] - (y == sel.START)) ** 2
    m = np.clip(np.round(rows["minutes_nt"].to_numpy()), 0, 90)
    cdf = np.cumsum(pmf, axis=1)
    crps = ((cdf - (MINUTES[None, :] >= m[:, None])) ** 2).sum(axis=1)
    keep = ("match_id", "team_id", "player_id", "league_ref", "season", "pos")
    return rows.select(*keep).with_columns(
        p_start=p[:, 0], start=(y == sel.START), log_loss=ll, brier=brier, crps=crps,
        cold=pl.Series(rows["log_club_matches"].to_numpy() <= np.log1p(2)),
        returning=pl.Series(rows["returned_gap"].to_numpy() > 0),
    )  # fmt: skip


def xi_overlap(scored: pl.DataFrame) -> pl.DataFrame:
    """Per team-match: how many actual starters are among the 11 highest p_start."""
    return (
        scored.sort("p_start", descending=True)
        .group_by("model", "match_id", "team_id", "league_ref")
        .agg(pl.col("start").head(11).sum().alias("overlap"))
    )


def paired(scored: pl.DataFrame, metric: str, a: str, b: str,
           by: str | None = None) -> pl.DataFrame:  # fmt: skip
    """Mean per-team-match difference a - b (summed over the team's pool) with a normal
    95% interval; negative favours a."""
    keys = ["match_id", "team_id"] + ([by] if by else [])
    tm = scored.group_by("model", *keys).agg(pl.col(metric).sum())
    wide = tm.pivot(on="model", index=keys, values=metric).drop_nulls()
    wide = wide.with_columns(d=pl.col(a) - pl.col(b))
    grp = [by] if by else []
    agg = [pl.len().alias("n"), pl.col("d").mean().alias("mean"),
           (1.96 * pl.col("d").std() / pl.len().sqrt()).alias("half")]  # fmt: skip
    out = wide.group_by(grp).agg(agg) if grp else wide.select(agg)
    return out.with_columns(lo=pl.col("mean") - pl.col("half"), hi=pl.col("mean") + pl.col("half"),
                            rel=pl.col("mean") / wide[b].mean())  # fmt: skip


def ece(scored: pl.DataFrame, bins: int = 10, by: str | None = None) -> pl.DataFrame:
    grp = [by] if by else []
    b = scored.with_columns(bin=(pl.col("p_start") * bins).floor().clip(0, bins - 1))
    per = b.group_by("model", *grp, "bin").agg(
        pl.len().alias("n"), pl.col("p_start").mean().alias("pred"),
        pl.col("start").cast(pl.Float64).mean().alias("obs"),
    )  # fmt: skip
    return per.group_by("model", *grp).agg(
        ((pl.col("pred") - pl.col("obs")).abs() * pl.col("n")).sum().truediv(pl.col("n").sum())
        .alias("ece")
    )  # fmt: skip


@dataclass
class Verdict:
    passed: bool
    checks: list[dict[str, Any]]


def verdict(scored: pl.DataFrame, strong: str, inference_ok: bool,
            leakage_ok: bool) -> Verdict:  # fmt: skip
    """The P3-b pass rule (g): 1-2 log loss, 3 minutes CRPS, 4 ECE, 5 gates, 6 leakage."""
    checks: list[dict[str, Any]] = []
    for base in ("B1", "B2"):
        r = paired(scored, "log_loss", "model", base).row(0, named=True)
        checks.append({"check": f"log loss vs {base}", "value": r["mean"], "lo": r["lo"],
                       "hi": r["hi"], "pass": r["hi"] < 0})  # fmt: skip
    per = paired(scored, "log_loss", "model", strong, by="league_ref")
    for r in per.iter_rows(named=True):
        checks.append({"check": f"log loss vs {strong}, league {r['league_ref']}",
                       "value": r["mean"], "pass": r["mean"] < 0})  # fmt: skip
    for base in ("B1", "B2"):
        r = paired(scored, "crps", "model", base).row(0, named=True)
        checks.append({"check": f"minutes CRPS vs {base}", "value": r["mean"], "lo": r["lo"],
                       "hi": r["hi"], "pass": r["hi"] < 0})  # fmt: skip
    m = scored.filter(pl.col("model") == "model")
    overall = float(ece(m)["ece"][0])
    checks.append({"check": "ECE overall", "value": overall, "pass": overall <= 0.02})
    for r in ece(m, by="league_ref").iter_rows(named=True):
        checks.append({"check": f"ECE league {r['league_ref']}", "value": r["ece"],
                       "pass": r["ece"] <= 0.03})  # fmt: skip
    checks.append({"check": "inference gates", "value": float(inference_ok),
                   "pass": inference_ok})  # fmt: skip
    checks.append({"check": "leakage", "value": float(leakage_ok), "pass": leakage_ok})
    return Verdict(all(c["pass"] for c in checks), checks)


def leakage_check(inp: sel.Inputs, players: pl.DataFrame, n: int = 100,
                  seed: int = 20261116) -> dict[str, Any]:  # fmt: skip
    """For n sampled target matches, rebuild with every appearance after the prediction's
    cutoff removed (the target match's own rows kept, for its outcome): the features must
    not change. A pass means no later appearance reaches any prediction."""
    tgt = inp.targets.filter(pl.col("match_id").is_in(players["match_id"].unique().implode()))
    sample = tgt.sample(min(n, len(tgt)), seed=seed)
    cols = ["match_id", "team_id", "player_id", "pos", *sel.FEATURES]
    bad = 0
    for r in sample.iter_rows(named=True):
        cutoff = r["kickoff"] - sel.HORIZON - RESULT_LAG
        app = inp.appearances.filter(
            (pl.col("kickoff") <= cutoff) | (pl.col("match_id") == r["match_id"])
        )
        one = sel.Inputs(
            app, inp.coaches, inp.context, tgt.filter(pl.col("match_id") == r["match_id"])
        )
        got, _ = sel.build(one)
        want = players.filter(pl.col("match_id") == r["match_id"])
        a = got.select(cols).sort("team_id", "player_id")
        b = want.select(cols).sort("team_id", "player_id")
        if a.shape != b.shape or not a.equals(b):
            bad += 1
    return {"leakage_checked": len(sample), "leakage_changed": bad, "leakage_ok": bad == 0}


def markdown(scored: pl.DataFrame, v: Verdict, diags: list[dict[str, Any]],
             digest: str) -> str:  # fmt: skip
    """The P3-b report: metrics only (the repo and its logs are public)."""
    lines = [f"# P3-b selection and minutes — dev, table {digest}", "",
             f"**Verdict: {'pass' if v.passed else 'fail'}**", "",
             "| check | value | 95% interval | pass |", "|---|---|---|---|"]  # fmt: skip
    for c in v.checks:
        ci = f"[{c['lo']:+.4f}, {c['hi']:+.4f}]" if "lo" in c else ""
        lines.append(
            f"| {c['check']} | {c['value']:+.4f} | {ci} | {'yes' if c['pass'] else '**no**'} |"
        )
    lines += ["", "## Means per player row", "", "| model | log loss | Brier | CRPS | XI overlap |",
              "|---|---|---|---|---|"]  # fmt: skip
    xi = xi_overlap(scored).group_by("model").agg(pl.col("overlap").mean())
    means = (
        scored.group_by("model")
        .agg(pl.col("log_loss", "brier", "crps").mean())
        .join(xi, on="model")
    )
    for r in means.sort("model").iter_rows(named=True):
        lines.append(f"| {r['model']} | {r['log_loss']:.4f} | {r['brier']:.4f} | {r['crps']:.3f} | "
                     f"{r['overlap']:.2f} |")  # fmt: skip
    for by in ("league_ref", "pos", "cold", "returning"):
        lines += [
            "",
            f"## Log loss by {by}",
            "",
            "| segment | model | B1 | B2 | n |",
            "|---|---|---|---|---|",
        ]
        t = scored.group_by(by, "model").agg(pl.col("log_loss").mean(), pl.len().alias("n"))
        w = t.pivot(on="model", index=by, values="log_loss").join(
            t.filter(pl.col("model") == "model").select(by, "n"), on=by
        )
        for r in w.sort(by).iter_rows(named=True):
            lines.append(
                f"| {r[by]} | {r['model']:.4f} | {r['B1']:.4f} | {r['B2']:.4f} | {r['n']} |"
            )
    fit = [d["fit_s"] for d in diags]
    lines += ["", f"Refits: {len(diags)}; passing the gates: {sum(d['gates_ok'] for d in diags)}; "
              f"fit time median {np.median(fit):.0f} s, max {max(fit):.0f} s."]  # fmt: skip
    return "\n".join(lines)


# ------------------------------------------------------------------------------- CLI


def _leagues() -> list[str]:
    from adatta.eval import harness

    cfg = harness.load_config()
    return [str(lg) for ids in cfg.bundles.values() for lg in ids]


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session

    ap_ = argparse.ArgumentParser(prog="adatta.eval.selection")
    sub = ap_.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build-table", help="build and store the selection table (dev seasons)")
    lk = sub.add_parser("leakage", help="rerun the leakage check on a stored table")
    lk.add_argument("--table", default="latest")
    args = ap_.parse_args(argv)
    store = make_store(get_settings())
    if args.cmd == "build-table":
        with get_session() as session:
            inp = load_inputs(session, store, _leagues())
        players, teams = sel.build(inp)
        d = save_table(store, players, teams)
        meta = {"digest": d, "rows": players.height, "team_matches": teams.height,
                **leakage_check(inp, players)}  # fmt: skip
        store.put(f"{sel.PREFIX}/{d}/meta.json", json.dumps(meta).encode())
        print(json.dumps(meta))

    if args.cmd == "leakage":
        players, teams, d = load_table(store, args.table)
        with get_session() as session:
            inp = load_inputs(session, store, _leagues())
        meta = {"digest": d, "rows": players.height, "team_matches": teams.height,
                **leakage_check(inp, players)}  # fmt: skip
        store.put(f"{sel.PREFIX}/{d}/meta.json", json.dumps(meta).encode())
        print(json.dumps(meta))


if __name__ == "__main__":
    main()
