"""P3-c evaluation (docs/PHASE3_TASKS.md): inputs and the stored tables, the formation
baselines F1/F2, metrics, the role-assignment check (d), the verdict and the report.

No JAX here: the formation model is in adatta/model/formation.py (model group, CI only).
Dev only: holdout seasons are never loaded.

CLI: `python -m adatta.eval.formation build-table` builds and stores the lineup table, the
formation table, the (a)/(b) checks, the role-assignment result and the leakage check
(metrics only on stdout).
"""

from __future__ import annotations

import io
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy.optimize import linear_sum_assignment
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.eval.selection import CONTEXT_REF, DEV, WARMUP, HoldoutRefused
from adatta.features import appearances as ap
from adatta.features import context_table as ct
from adatta.features import formation as fe
from adatta.features import lineups as lu
from adatta.features import tenure as tn
from adatta.features.referees import RESULT_LAG

Arr = NDArray[np.float64]
EPS = 1e-9
TIER_RUN = "43419004255"  # production spec, 28-day dev grid (PHASE2_TASKS.md reference)
TIER_MODEL = "phase1b_strength_open_xg"
ROLE_A = 5.0  # pseudo-count shrinking a player's (or team's) slot shares to the prior
SPARSE = 5  # fewer coded starts than this: a sparse-history player in the report
ECE_MAX = 0.03


# ----------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Raw:
    appearances: pl.DataFrame
    inputs: fe.Inputs


def _targets(session: Session, leagues: Sequence[str], max_season: int) -> pl.DataFrame:
    M, S, C = models.Match, models.Season, models.Competition
    return pl.DataFrame(
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


def tier_predictions(store: RawStore, bundles: Sequence[str]) -> pl.DataFrame:
    """Pre-match P(home), P(away) and their as-of time from the stored dev backtest."""
    frames = []
    for b in bundles:
        for y in DEV:
            raw = try_get(store, f"backtest/parts/{TIER_RUN}/{TIER_MODEL}_{b}_{y}.parquet")
            if raw is not None:
                cols = ["match_id", "as_of", "p_home", "p_away"]
                frames.append(pl.read_parquet(io.BytesIO(raw), columns=cols))
    if not frames:
        raise FileNotFoundError(f"no stored predictions of run {TIER_RUN}")
    return pl.concat(frames).unique("match_id", keep="last")


def load_raw(session: Session, store: RawStore, leagues: Sequence[str],
             bundles: Sequence[str], max_season: int = DEV[-1]) -> Raw:  # fmt: skip
    if max_season > DEV[-1]:
        raise HoldoutRefused(f"season {max_season} is past the dev window")
    groups = [g for g in ap.targets(session) if g[1] <= max_season]
    app = ap.load(store, sorted(groups))
    T = models.Team
    clubs = {t for (t,) in session.execute(
        select(T.team_id).where(T.is_national.is_(False), T.team_level == "senior"))}  # fmt: skip
    app = app.filter(pl.col("team_id").is_in(list(clubs)))
    K = models.MatchCoach
    coaches = pl.DataFrame(
        session.execute(select(K.match_id, K.team_id, K.coach_ref)
                        .where(K.superseded_at.is_(None))).all(),
        schema={"match_id": pl.Int64, "team_id": pl.Int64, "coach_ref": pl.Utf8}, orient="row",
    )  # fmt: skip
    ctx, _ = ct.load(store, timedelta(hours=48), CONTEXT_REF)
    ctx = ctx.select("match_id", "home_importance", "away_importance")
    empty = pl.DataFrame(schema={
        "match_id": pl.Int64, "team_id": pl.Int64, "kickoff": pl.Datetime("us", "UTC"),
        "formation": pl.Utf8,
    })  # fmt: skip
    inp = fe.Inputs(empty, coaches, ctx, tier_predictions(store, bundles),
                    _targets(session, leagues, max_season))  # fmt: skip
    return Raw(app, inp)


# ------------------------------------------------------------------------------ the store


def save_table(store: RawStore, table: pl.DataFrame, meta: dict[str, Any]) -> str:
    d = fe.digest(table)
    store.put(f"{fe.PREFIX}/{d}/table.parquet", fe.to_bytes(table))
    store.put(f"{fe.PREFIX}/{d}/meta.json", json.dumps(meta).encode())
    store.put(f"{fe.PREFIX}/latest.json", json.dumps({"digest": d}).encode())
    return d


def load_table(store: RawStore, ref: str = "latest") -> tuple[pl.DataFrame, dict[str, Any], str]:
    if ref == "latest":
        raw = try_get(store, f"{fe.PREFIX}/latest.json")
        if raw is None:
            raise FileNotFoundError("no formation table stored")
        ref = json.loads(raw)["digest"]
    table = pl.read_parquet(io.BytesIO(store.get(f"{fe.PREFIX}/{ref}/table.parquet")))
    meta = json.loads(store.get(f"{fe.PREFIX}/{ref}/meta.json"))
    return table, meta, ref


# ----------------------------------------------------------------------------- baselines


def matrix(table: pl.DataFrame, prefix: str, k: int) -> Arr:
    return table.select(f"{prefix}_{i}" for i in range(k)).to_numpy().astype(float)


@dataclass(frozen=True)
class Baselines:
    prior: list[float]  # warm-up class shares
    w_last: float  # F1: weight of the prior against the last formation
    w_ew: float  # F2: weight of the prior against the team's recency-weighted shares


def _mix(x: Arr, prior: Arr, w: float) -> Arr:
    p = (1 - w) * x + w * prior[None, :]
    return p / p.sum(axis=1, keepdims=True)


def fit_baselines(table: pl.DataFrame, k: int) -> Baselines:
    if table.is_empty() or not table["season"].is_in(WARMUP).all():
        raise ValueError("baselines are fitted on warm-up rows only")
    warm = table
    y = warm["y"].to_numpy()
    prior = np.bincount(y, minlength=k) / len(y)
    grid = np.linspace(0.01, 0.99, 99)

    def best(x: Arr) -> float:
        ll = [-np.log(_mix(x, prior, w)[np.arange(len(y)), y] + EPS).mean() for w in grid]
        return float(grid[int(np.argmin(ll))])

    return Baselines(prior.tolist(), best(matrix(warm, "last", k)), best(matrix(warm, "ts", k)))


def baseline_probs(b: Baselines, rows: pl.DataFrame, k: int) -> dict[str, Arr]:
    prior = np.array(b.prior)
    return {"F1": _mix(matrix(rows, "last", k), prior, b.w_last),
            "F2": _mix(matrix(rows, "ts", k), prior, b.w_ew)}  # fmt: skip


# ------------------------------------------------------------------------------- metrics


def family(vocab: list[str]) -> NDArray[np.int64]:
    """Back-line family of each class: 3, 4 or 5 at the back; 0 for other."""
    return np.array([int(f.split("-")[0]) if f != fe.OTHER else 0 for f in vocab])


def score(rows: pl.DataFrame, p: Arr, vocab: list[str]) -> pl.DataFrame:
    y = rows["y"].to_numpy()
    fam = family(vocab)
    pf = np.stack([p[:, fam == f].sum(axis=1) for f in fam], axis=1)  # P(own family) per class
    top = p.argmax(axis=1)
    return rows.select(
        "match_id", "team_id", "league_ref", "season", "tier", "new_coach_5"
    ).with_columns(  # fmt: skip
        log_loss=-np.log(p[np.arange(len(y)), y] + EPS),
        fam_log_loss=-np.log(pf[np.arange(len(y)), y] + EPS),
        p_top=p.max(axis=1),
        top_correct=top == y,
        # P3-g: the predicted class and the whole distribution (vocab order)
        top=pl.Series([vocab[int(i)] for i in top], dtype=pl.Utf8),
        p=pl.Series([list(map(float, r)) for r in p], dtype=pl.List(pl.Float64)),
    )


def paired(scored: pl.DataFrame, a: str, b: str, metric: str,
           by: str | None = None) -> pl.DataFrame:  # fmt: skip
    keys = ["match_id", "team_id"] + ([by] if by else [])
    wide = scored.pivot(on="model", index=keys, values=metric).drop_nulls()
    wide = wide.with_columns(d=pl.col(a) - pl.col(b))
    agg = [pl.len().alias("n"), pl.col("d").mean().alias("mean"),
           (1.96 * pl.col("d").std() / pl.len().sqrt()).alias("half"),
           pl.col(b).mean().alias("base")]  # fmt: skip
    out = wide.group_by(by).agg(agg) if by else wide.select(agg)
    return out.with_columns(lo=pl.col("mean") - pl.col("half"), hi=pl.col("mean") + pl.col("half"),
                            rel=pl.col("mean") / pl.col("base"))  # fmt: skip


def ece(scored: pl.DataFrame, bins: int = 10) -> float:
    p = scored["p_top"].to_numpy()
    c = scored["top_correct"].to_numpy().astype(float)
    edges = np.minimum((p * bins).astype(int), bins - 1)
    tot = 0.0
    for b in range(bins):
        m = edges == b
        if m.any():
            tot += m.mean() * abs(c[m].mean() - p[m].mean())
    return float(tot)


# --------------------------------------------------------------------- role assignment (d)


def role_assignment(lineups: pl.DataFrame, targets: pl.DataFrame) -> pl.DataFrame:
    """At the lineups-known horizon (as of kickoff, history final by kickoff - RESULT_LAG):
    each target team-match's starters are assigned the formation's slots, by the model
    (the player's coded history, shrunk to R1) and by R1 (provider G/D/M/F code and the
    team's slot shares, shrunk to the league-wide shares). One row per target team-match:
    accuracy of each, and of each on sparse-history players."""
    pos = lineups.filter(pl.col("positional")).sort("kickoff", "match_id", "team_id")
    slots = sorted(set(pos["slot"].to_list()))
    s_ix = {s: i for i, s in enumerate(slots)}
    n_s = len(slots)
    groups = pos.partition_by(["match_id", "team_id"], as_dict=True, maintain_order=True)
    order = sorted(groups, key=lambda k: groups[k]["kickoff"][0])
    tgt = {(r[0], r[1]) for r in targets.select("match_id", "team_id").iter_rows()}
    c_pos: dict[str, Arr] = defaultdict(lambda: np.zeros(n_s))
    c_team: dict[tuple[int, str], Arr] = defaultdict(lambda: np.zeros(n_s))
    c_pl: dict[int, Arr] = defaultdict(lambda: np.zeros(n_s))
    lag_us = int(RESULT_LAG.total_seconds() * 1e6)
    kick = [int(groups[k]["kickoff"][0].timestamp() * 1e6) for k in order]
    added = 0
    out = []
    for gi, key in enumerate(order):
        while added < gi and kick[added] + lag_us <= kick[gi]:
            g = groups[order[added]]
            team = int(order[added][1])
            for p, ps, sl in g.select("player_id", "pos", "slot").iter_rows():
                v = s_ix[sl]
                c_pos[ps or "U"][v] += 1
                c_team[(team, ps or "U")][v] += 1
                c_pl[p][v] += 1
            added += 1
        if (key[0], key[1]) not in tgt:
            continue
        g = groups[key]
        team = int(key[1])
        actual = [s_ix[s] for s in g["slot"].to_list()]
        cost_m = np.zeros((11, 11))
        cost_r = np.zeros((11, 11))
        sparse = []
        for i, (p, ps) in enumerate(g.select("player_id", "pos").iter_rows()):
            ps = ps or "U"
            q = (c_pos[ps] + 1.0 / n_s) / (c_pos[ps].sum() + 1.0)
            t = c_team[(team, ps)]
            r1 = (t + ROLE_A * q) / (t.sum() + ROLE_A)
            h = c_pl[p]
            mdl = (h + ROLE_A * r1) / (h.sum() + ROLE_A)
            cost_r[i] = -np.log(r1[actual] + EPS)
            cost_m[i] = -np.log(mdl[actual] + EPS)
            sparse.append(h.sum() < SPARSE)
        res = {}
        for name, cost in (("model", cost_m), ("R1", cost_r)):
            _, col = linear_sum_assignment(cost)
            got = np.array([actual[c] for c in col])
            hit = got == np.array(actual)
            res[name] = hit
        sp = np.array(sparse)
        out.append({"match_id": key[0], "team_id": key[1], "league_ref": g["league_ref"][0],
                    "season": g["season"][0], "acc_model": res["model"].mean(),
                    "acc_R1": res["R1"].mean(), "n_sparse": int(sp.sum()),
                    "hit_sparse_model": int(res["model"][sp].sum()),
                    "hit_sparse_R1": int(res["R1"][sp].sum())})  # fmt: skip
    return pl.DataFrame(out)


def role_summary(roles: pl.DataFrame) -> dict[str, float]:
    am = roles["acc_model"].to_numpy().astype(float)
    ar = roles["acc_R1"].to_numpy().astype(float)
    d = am - ar
    half = 1.96 * d.std(ddof=1) / np.sqrt(len(d)) if len(d) > 1 else float("nan")
    ns = max(int(roles["n_sparse"].sum()), 1)
    return {
        "team_matches": float(len(roles)), "acc_model": float(am.mean()),
        "acc_R1": float(ar.mean()), "diff": float(d.mean()), "lo": float(d.mean() - half),
        "hi": float(d.mean() + half), "sparse_players": float(roles["n_sparse"].sum()),
        "sparse_acc_model": float(roles["hit_sparse_model"].sum()) / ns,
        "sparse_acc_R1": float(roles["hit_sparse_R1"].sum()) / ns,
    }  # fmt: skip


# --------------------------------------------------------------------------- leakage


def leakage_check(inp: fe.Inputs, table: pl.DataFrame, vocab: list[str], n: int = 100,
                  seed: int = 20261117) -> dict[str, Any]:  # fmt: skip
    """For n sampled targets, rebuild with every formation and coach row after the cutoff
    removed (the target's own formation kept, for its outcome): the features must not
    change."""
    tgt = inp.targets.filter(pl.col("match_id").is_in(table["match_id"].unique().implode()))
    sample = tgt.sample(min(n, len(tgt)), seed=seed)
    cols = [c for c in table.columns if c not in ("coach_ref",)]
    bad = 0
    for r in sample.iter_rows(named=True):
        cutoff = r["kickoff"] - fe.HORIZON - RESULT_LAG
        keep = (pl.col("kickoff") <= cutoff) | (pl.col("match_id") == r["match_id"])
        fm = inp.formations.filter(keep)
        ids = fm.select("match_id", "team_id")
        co = inp.coaches.join(ids, on=["match_id", "team_id"], how="semi")
        one = fe.Inputs(
            fm, co, inp.context, inp.tier, tgt.filter(pl.col("match_id") == r["match_id"])
        )
        got = fe.build(one, vocab).select(cols).sort("team_id")
        want = table.filter(pl.col("match_id") == r["match_id"]).select(cols).sort("team_id")
        if got.shape != want.shape or not got.equals(want):
            bad += 1
    return {"leakage_checked": len(sample), "leakage_changed": bad, "leakage_ok": bad == 0}


# ---------------------------------------------------------------------- verdict and report


@dataclass(frozen=True)
class Verdict:
    passed: bool
    checks: list[dict[str, Any]]


def _rel(scored: pl.DataFrame, a: str, b: str, metric: str) -> dict[str, float]:
    r = paired(scored, a, b, metric).row(0, named=True)
    return {"value": r["rel"], "lo": r["lo"] / r["base"], "hi": r["hi"] / r["base"]}


def verdict(scored: pl.DataFrame, meta: dict[str, Any], strong: str, inference_ok: bool,
            vocab: list[str]) -> Verdict:  # fmt: skip
    checks: list[dict[str, Any]] = []
    ab = meta["checks_a"]
    a_ok = all(r["passed"] for r in ab)
    checks.append({"check": "(a) slot codes, positional league-seasons", "value": float(a_ok),
                   "pass": a_ok})  # fmt: skip
    checks.append({"check": "(b) tenures", "value": float(meta["checks_b"]["passed"]),
                   "pass": bool(meta["checks_b"]["passed"])})  # fmt: skip
    for b in ("F1", "F2"):
        r = _rel(scored, "model", b, "log_loss")
        checks.append({"check": f"log loss vs {b}", **r, "pass": r["hi"] < 0})
    lg = paired(scored, "model", strong, "log_loss", by="league_ref")
    for row in lg.sort("league_ref").iter_rows(named=True):
        checks.append({"check": f"log loss vs {strong}, league {row['league_ref']}",
                       "value": row["rel"], "pass": row["mean"] < 0})  # fmt: skip
    fam = paired(scored, "model", strong, "fam_log_loss").row(0, named=True)
    checks.append({"check": f"family log loss vs {strong}", "value": fam["rel"],
                   "pass": fam["mean"] <= 0})  # fmt: skip
    e = ece(scored.filter(pl.col("model") == "model"))
    checks.append({"check": "ECE top class", "value": e, "pass": e <= ECE_MAX})
    checks.append({"check": "inference gates", "value": float(inference_ok), "pass": inference_ok})
    role = meta["role"]
    checks.append({"check": "role assignment vs R1", "value": role["diff"], "lo": role["lo"],
                   "hi": role["hi"], "pass": role["lo"] > 0})  # fmt: skip
    checks.append({"check": "leakage", "value": float(meta["leakage_ok"]),
                   "pass": bool(meta["leakage_ok"])})  # fmt: skip
    return Verdict(all(c["pass"] for c in checks), checks)


def markdown(scored: pl.DataFrame, v: Verdict, diags: list[dict[str, Any]],
             meta: dict[str, Any], digest: str) -> str:  # fmt: skip
    """The P3-c report: metrics only (the repo and its logs are public)."""
    lines = [f"# P3-c formation, slot codes, tenure — dev, table {digest}", "",
             f"**Verdict: {'pass' if v.passed else 'fail'}**", "",
             "| check | value | 95% interval | pass |", "|---|---|---|---|"]  # fmt: skip
    for c in v.checks:
        ci = f"[{c['lo']:+.4f}, {c['hi']:+.4f}]" if "lo" in c else ""
        lines.append(
            f"| {c['check']} | {c['value']:+.4f} | {ci} | {'yes' if c['pass'] else '**no**'} |"
        )
    lines += ["", "## Means per team-match", "",
              "| model | log loss | family log loss | top-class accuracy |",
              "|---|---|---|---|"]  # fmt: skip
    m = scored.group_by("model").agg(pl.col("log_loss", "fam_log_loss").mean(),
                                     pl.col("top_correct").mean())  # fmt: skip
    for r in m.sort("model").iter_rows(named=True):
        lines.append(f"| {r['model']} | {r['log_loss']:.4f} | {r['fam_log_loss']:.4f} | "
                     f"{r['top_correct']:.3f} |")  # fmt: skip
    seg = scored.with_columns(
        tier_band=pl.when(pl.col("tier") < -0.3).then(pl.lit("weaker"))
        .when(pl.col("tier") > 0.3).then(pl.lit("stronger")).otherwise(pl.lit("level")),
        coach=pl.when(pl.col("new_coach_5") > 0).then(pl.lit("first 5 under coach"))
        .otherwise(pl.lit("later")),
    )  # fmt: skip
    for by in ("league_ref", "tier_band", "coach"):
        lines += ["", f"## Log loss by {by}", "", "| segment | model | F1 | F2 | n |",
                  "|---|---|---|---|---|"]  # fmt: skip
        t = seg.group_by(by, "model").agg(pl.col("log_loss").mean(), pl.len().alias("n"))
        w = t.pivot(on="model", index=by, values="log_loss").join(
            t.filter(pl.col("model") == "model").select(by, "n"), on=by
        )
        for r in w.sort(by).iter_rows(named=True):
            lines.append(
                f"| {r[by]} | {r['model']:.4f} | {r['F1']:.4f} | {r['F2']:.4f} | {r['n']} |"
            )
    role = meta["role"]
    lines += ["", "## Role assignment (lineups known, positional league-seasons)", "",
              f"Team-matches {role['team_matches']:.0f}; accuracy model {role['acc_model']:.3f}, "
              f"R1 {role['acc_R1']:.3f}; sparse-history players {role['sparse_players']:.0f}: "
              f"model {role['sparse_acc_model']:.3f}, R1 {role['sparse_acc_R1']:.3f}."]  # fmt: skip
    fit = [d["fit_s"] for d in diags]
    lines += ["", f"Refits: {len(diags)}; passing the gates: {sum(d['gates_ok'] for d in diags)}; "
              f"fit time median {np.median(fit):.0f} s, max {max(fit):.0f} s.",
              f"Vocabulary: {', '.join(meta['vocab'])}."]  # fmt: skip
    q = np.array([d["ppc_q"] for d in diags if "ppc_q" in d])
    if len(q):
        out = float(((q < 0.05) | (q > 0.95)).mean())
        lines.append(f"PPC of the top class's count (warning only): {out:.1%} of refits outside "
                     "the central 90%.")  # fmt: skip
    return "\n".join(lines)


# ------------------------------------------------------------------------------- CLI


def _leagues_and_bundles() -> tuple[list[str], list[str]]:
    from adatta.eval import harness

    cfg = harness.load_config()
    bundles = sorted(cfg.bundles)
    return sorted({str(x) for b in bundles for x in cfg.bundles[b]}), bundles


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session

    p = argparse.ArgumentParser(prog="adatta.eval.formation")
    p.add_argument("cmd", choices=["build-table"])
    args = p.parse_args(argv)
    del args
    store = make_store(get_settings())
    leagues, bundles = _leagues_and_bundles()
    with get_session() as session:
        raw = load_raw(session, store, leagues, bundles)
        rows = tn.coach_rows(session)
    lineups, formations, quality = lu.build(raw.appearances)
    lineup_digest = lu.save(store, lineups, formations, quality, {})
    r_in = raw.inputs
    inp = fe.Inputs(formations.select("match_id", "team_id", "kickoff", "formation"),
                    r_in.coaches, r_in.context, r_in.tier, r_in.targets)  # fmt: skip
    vocab = fe.vocabulary(inp.formations, inp.targets, WARMUP)
    table = fe.build(inp, vocab)
    bundle_lineups = lineups.filter(pl.col("league_ref").is_in(leagues))
    checks_a = lu.checks(bundle_lineups).to_dicts()
    checks_b = tn.check(rows, tn.tenures(rows))
    dev_pos = table.filter(pl.col("season").is_in(DEV)).join(
        formations.filter(pl.col("positional")).select("match_id", "team_id"),
        on=["match_id", "team_id"], how="semi",
    )  # fmt: skip
    roles = role_assignment(lineups, dev_pos)
    meta = {"vocab": vocab, "lineups": lineup_digest, "checks_a": checks_a,
            "checks_b": checks_b, "role": role_summary(roles),
            "quality": quality.filter(pl.col("league_ref").is_in(leagues)).to_dicts(),
            **leakage_check(inp, table, vocab)}  # fmt: skip
    digest = save_table(store, table, meta)
    print(json.dumps({"digest": digest, "lineups": lineup_digest, "rows": len(table),
                      "vocab": len(vocab), "checks_a": all(r["passed"] for r in checks_a),
                      "positional_league_seasons": len(checks_a),
                      "checks_b": checks_b["passed"], "leakage_ok": meta["leakage_ok"],
                      "role_team_matches": meta["role"]["team_matches"]}))  # fmt: skip


if __name__ == "__main__":
    main()
