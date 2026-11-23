"""Stage A, A3: cups as training data, on the weekly dev walk-forward
(docs/BEYOND_LEAGUES_PLAN.md, "A3 acceptance", owner: "Approve as drafted").

A3 is P (`phase1b_strength_open_xg`) with the bundle's cup ties in the likelihood
(`model.cup_strength`). Per refit of one bundle x dev season (every part[1]-th):

- the refits: P's weekly dev grid, plus the Mondays of this season's dev cup ties that are
  not on it (no league target that week);
- P's draws come from the cache under P's fit hash (the P3-d run). A grid refit missing
  from it stops the job: P is not refitted there. A tie Monday off the grid is fitted once
  and cached under the same hash;
- the pairing check: P's cached fit has the same teams, leagues and training rows as this
  refit's league rows, or the job stops;
- A3 is fitted (or read from its own cache) on the same league rows plus the cup rows;
- league targets are scored by both from the same refit; cup ties of the week by both, P as
  A1 did (the listed home side keeps its home term) and A3 with delta_cup, or no home term
  on a neutral ground.

Then the gate (`verdict`): cups improve as in A2, leagues are no worse (D18 segments, and
the overall paired interval not wholly above zero), every A3 refit passes the sampler gates
(one redo at target_accept 0.99 and 4,000 draws, listed mechanically by `failing`), A3's
median fit time is at most 1.5 x P's, and no leakage (unit tests).

    uv run --frozen --group model python -m adatta.model.cup_train run \\
        --bundle england --season 2019 --part 0/4 --upload RUN_ID
    uv run --frozen --group model python -m adatta.model.cup_train sensitivity \\
        --bundle england --upload RUN_ID
    uv run python -m adatta.model.cup_train assemble --fetch RUN_ID [--override REDO_RUN]

Logs and the summary carry metrics only (the repo is public); match rows go to R2 (cups/a3).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

import numpy as np
import polars as pl
import yaml

from adatta.adapters.storage import RawStore, make_store, try_get
from adatta.config import get_settings
from adatta.eval import cup_terms, harness
from adatta.features import cup_ties
from adatta.model import backtest as bt
from adatta.model import cup_backtest as cb
from adatta.model import cup_strength as cs
from adatta.model import team_strength as ts

PREFIX = "cups/a3"
A1_RUN = "43903439721"
REDO_TARGET_ACCEPT = 0.99
REDO_DRAWS = 4000
TIMING_MAX = 1.5
SENSITIVITY_SD = (0.1, 0.5)  # 0.25 is the base run
PAIRING_GOALS_TOL = 0.05  # P's posterior-predictive goals against the observed, relative
OUTCOMES = ("p_home", "p_draw", "p_away")


# --------------------------------------------------------------------------- data


def training_ties(ties: pl.DataFrame, cfg: harness.EvalConfig) -> pl.DataFrame:
    """The bundle ties a dev refit may train on: every season before the holdout."""
    return ties.filter(pl.col("season") < min(cfg.holdout_seasons))


def table_digest(ties: pl.DataFrame) -> str:
    """Digest of the cup rows a bundle's refits may read (part of A3's cache key)."""
    cols = ["match_id", "kickoff", "home", "away", "hg", "ag", "role", "league_id"]
    t = ties.select(cols).sort("match_id")
    blob = json.dumps(t.rows(), default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def a3_spec(cfg: harness.EvalConfig, digest: str, prior_sd: float = cs.PRIOR_SD) -> bt.RunSpec:
    return replace(bt.production_spec(cfg), cups=True, cup_table=digest, cup_prior_sd=prior_sd)


# --------------------------------------------------------------------------- fitting


def a3_fitter(spec: bt.RunSpec, cr: cs.CupRows) -> bt.Fitter:
    """`bt.nuts_fitter` for A3: the same sampler settings, with delta_cup kept."""

    def fit(fd: ts.FitData, seed: int) -> tuple[bt.Draws, bt.Diag]:
        from adatta.model import diagnostics as dg

        t0 = time.time()
        idata = cs.sample(fd, cr, seed, draws=spec.draws, chains=spec.chains,
                          target_accept=spec.target_accept, tune=spec.tune, xg=spec.xg,
                          prior_sd=spec.cup_prior_sd)  # fmt: skip
        conv = {c.name: c.value for c in dg.convergence(idata)}
        diag = {"rhat_max": conv["rhat"], "ess_bulk_min": conv["ess_bulk"],
                "ess_tail_min": conv["ess_tail"], "divergences": conv["divergences"],
                "fit_s": round(time.time() - t0)}  # fmt: skip
        draws = ts.slim(idata, spec.max_pred_draws)
        if cr.n:
            post = ts.stacked(idata)
            n = post.sizes["sample"]
            take = np.linspace(0, n - 1, min(spec.max_pred_draws, n)).astype(int)
            draws["delta_cup"] = np.asarray(post["delta_cup"].values[take], dtype=np.float64)
        del idata
        bt._release_memory()
        return draws, diag

    return fit


def gates_ok(diag: dict[str, float]) -> bool:
    """§14.5 sampler gates (model.diagnostics thresholds)."""
    from adatta.model import diagnostics as dg

    return bool(diag["rhat_max"] <= dg.RHAT_MAX and diag["ess_bulk_min"] >= dg.ESS_MIN
                and diag["ess_tail_min"] >= dg.ESS_MIN
                and diag["divergences"] <= dg.DIVERGENCES_MAX)  # fmt: skip


def check_pairing(draws: bt.Draws, fd: ts.FitData) -> None:
    """P's cached fit was made on this refit's league rows: same teams and leagues, the same
    number of training rows, and its posterior-predictive goals within 5% of the observed."""
    n_team = np.asarray(draws["attack_last"]).shape[0]
    if n_team != len(fd.team_ids):
        raise SystemExit(f"pairing: P has {n_team} teams, the refit {len(fd.team_ids)}")
    if np.asarray(draws["mu_league"]).shape[0] != len(fd.leagues):
        raise SystemExit("pairing: P's leagues differ from the refit's")
    if "train_lam_home" in draws:
        lh, la = np.asarray(draws["train_lam_home"]), np.asarray(draws["train_lam_away"])
        if len(lh) != len(fd.hg):
            raise SystemExit(f"pairing: P has {len(lh)} training rows, the refit {len(fd.hg)}")
        for lam, g in ((lh, fd.hg), (la, fd.ag)):
            if abs(float(lam.sum()) - float(g.sum())) > PAIRING_GOALS_TOL * float(g.sum()):
                raise SystemExit("pairing: P's training goals differ from the refit's")


def cup_offsets(draws: bt.Draws, fd: ts.FitData, ties: pl.DataFrame
                ) -> tuple[np.ndarray, np.ndarray]:  # fmt: skip
    """A3's home term on cup ties, as an offset on P's league-match rates (which add the
    league and team home term): + delta_cup off a neutral ground, minus the whole home term
    on one."""
    n_draws = np.asarray(draws["mu_league"]).shape[-1]
    delta = np.asarray(draws.get("delta_cup", np.zeros(n_draws)), dtype=np.float64)
    home = np.asarray(cb.home_terms(draws, fd, ties), dtype=np.float64)
    home = home.reshape(ties.height, n_draws)
    neutral = (ties["role"] == "neutral").to_numpy()
    off_h = np.where(neutral[:, None], -home, delta[None, :])
    return off_h, np.zeros_like(off_h)


def total_var(r: dict[str, Any], max_goals: int) -> float:
    """Predictive variance of total goals from the posterior-mean score matrix (the cup PPC)."""
    m = cup_terms.matrices(np.asarray(r["draws_lam"]), np.asarray(r["draws_mu"]),
                           np.asarray(r["draws_rho"]), np.asarray(r["draws_kappa"]),
                           max_goals).mean(0)  # fmt: skip
    k = np.arange(m.shape[0])
    tot = k[:, None] + k[None, :]
    mean = float((m * tot).sum())
    return float((m * tot**2).sum() - mean**2)


def tier_gap(draws: bt.Draws, fd: ts.FitData) -> float:
    """Mean strength (attack - defence) of the second tier's teams minus the first's, each
    team in the league of its last training row (posterior mean)."""
    s = np.asarray(draws["attack_last"]).mean(-1) - np.asarray(draws["defence_last"]).mean(-1)
    last: dict[int, int] = {}
    for h, a, lg in zip(fd.home, fd.away, fd.league, strict=True):
        last[int(h)], last[int(a)] = int(lg), int(lg)
    t1 = [s[t] for t, lg in last.items() if lg == 0]
    t2 = [s[t] for t, lg in last.items() if lg == 1]
    return float(np.mean(t2) - np.mean(t1)) if t1 and t2 else float("nan")


def refit_times(plan: list[Any], tie_weeks: set[datetime]
                ) -> list[tuple[datetime, np.ndarray | None]]:  # fmt: skip
    """P's weekly grid (as_of, league target indices) plus the tie Mondays off it (no
    league targets), in time order."""
    on = {a: tgt for a, tgt, _ in plan}
    out: list[tuple[datetime, np.ndarray | None]] = list(on.items())
    out += [(w, None) for w in tie_weeks if w not in on]
    return sorted(out, key=lambda x: x[0])


# --------------------------------------------------------------------------- run


Save = Callable[[pl.DataFrame, pl.DataFrame, list[dict[str, Any]]], None]


def run_part(df: pl.DataFrame, leagues: list[int], bundle: str, season: int,
             store: RawStore, ties: pl.DataFrame, part: tuple[int, int],
             save: Save | None = None, only: set[str] | None = None,
             fitters: tuple[bt.Fitter, Callable[[cs.CupRows], bt.Fitter]] | None = None,
             cfg: harness.EvalConfig | None = None,
             ) -> tuple[pl.DataFrame, pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """P and A3 on one dev bundle-season's refits (every part[1]-th from part[0]). Returns
    league rows, cup rows and per-refit diagnostics. `only` redoes those dates' A3 fits
    with the redo settings; `fitters` replaces the NUTS fitters (tests)."""
    cfg = cfg or harness.load_config()
    if season not in cfg.dev_seasons:
        raise harness.HoldoutLocked(f"A3 runs on dev seasons {cfg.dev_seasons}, not {season}")
    P = bt.production_spec(cfg)
    train = training_ties(ties, cfg)
    digest = table_digest(train)
    A3 = a3_spec(cfg, digest)
    if only is not None:
        A3 = replace(A3, target_accept=REDO_TARGET_ACCEPT, draws=REDO_DRAWS)
    plan = harness.refit_plan(df, cfg, [season], P.every_days, None, P.grid)
    season_ties = ties.filter(pl.col("season") == season)
    weeks = {harness.week_start(k) for k in season_ties["kickoff"].to_list()}
    times = refit_times(plan, weeks)[part[0] :: part[1]]
    if only is not None:
        times = [x for x in times if f"{x[0]:%Y-%m-%d}" in only]
        if len(times) != len(only):
            raise ValueError("a redo date is not a refit of this part")
    p_fit = fitters[0] if fitters else bt.nuts_fitter(P)
    hP, hA = bt.fit_hash(P), bt.fit_hash(A3)
    lag, lb = cfg.result_available_after_hours, cfg.lookback_days
    print(f"{bundle} {season} part {part[0]}/{part[1]}: {len(times)} refits, P {hP}, "
          f"A3 {hA} (cup table {digest})", flush=True)  # fmt: skip
    league_rows: list[dict[str, Any]] = []
    cup_rows: list[dict[str, Any]] = []
    diags: list[dict[str, Any]] = []
    for as_of, tgt in times:
        fd = ts.prepare(df, leagues, as_of, lb, lag)
        cr, dropped = cs.cup_rows(fd, train, as_of, lb, lag, min(cfg.holdout_seasons))
        cached = try_get(store, bt.cache_key(hP, bundle, as_of))
        if cached is not None:
            dP, diagP = bt.decode(cached)
        elif tgt is not None:
            raise SystemExit(f"P fit {bundle} {as_of:%Y-%m-%d} is not cached (P is not refitted)")
        else:
            dP, diagP = p_fit(fd, P.seed)
            store.put(bt.cache_key(hP, bundle, as_of), bt.encode(dP, diagP))
        check_pairing(dP, fd)
        keyA = bt.cache_key(hA, bundle, as_of)
        cachedA = try_get(store, keyA)
        if cachedA is not None:
            dA, diagA = bt.decode(cachedA)
        else:
            fitA = fitters[1](cr) if fitters else a3_fitter(A3, cr)
            dA, diagA = fitA(fd, A3.seed)
            store.put(keyA, bt.encode(dA, diagA))
        counts = np.bincount(np.concatenate([fd.home, fd.away]), minlength=len(fd.team_ids))
        n_of = {int(t): int(counts[j]) for j, t in enumerate(fd.team_ids)}

        def cold(h: int, a: int, n_of: dict[int, int] = n_of) -> bool:
            return min(n_of.get(h, 0), n_of.get(a, 0)) < cfg.min_warmup_matches

        if tgt is not None:
            targets = df[tgt]
            for model, dd in (("P", dP), ("A3", dA)):
                sc = bt.score_targets(dd, fd, targets, cfg.max_goals)
                for r, s in zip(targets.iter_rows(named=True), sc, strict=True):
                    league_rows.append({
                        "match_id": int(r["match_id"]), "league_id": int(r["league_id"]),
                        "season": season, "kickoff": r["kickoff"], "as_of": as_of,
                        "hg": int(r["hg"]), "ag": int(r["ag"]), "bundle": bundle,
                        "model": model, "cold_start": cold(int(r["home"]), int(r["away"])),
                        **s,
                    })  # fmt: skip
        grp = season_ties.filter(
            pl.col("kickoff").map_elements(harness.week_start,
                                           return_dtype=pl.Datetime("us", "UTC")) == as_of
        )  # fmt: skip
        if grp.height:
            scored = {"P": bt.score_targets(dP, fd, grp, cfg.max_goals, exact=True),
                      "A3": bt.score_targets(dA, fd, grp, cfg.max_goals, exact=True,
                                             extra_offset=cup_offsets(dA, fd, grp))}  # fmt: skip
            for model, ss in scored.items():
                for r, s in zip(grp.iter_rows(named=True), ss, strict=True):
                    tv = total_var(s, cfg.max_goals)
                    cup_rows.append({
                        "match_id": r["match_id"], "cup_id": r["cup_id"], "season": season,
                        "kickoff": r["kickoff"], "as_of": as_of, "round": r["round"],
                        "kind": r["kind"], "role": r["role"], "neutral": r["role"] == "neutral",
                        "pairing": r["pairing"], "league_id": r["league_id"],
                        "hg": r["hg"], "ag": r["ag"], "bundle": bundle, "model": model,
                        "cold_start": cold(int(r["home"]), int(r["away"])), "total_var": tv,
                        **{k: v for k, v in s.items() if not k.startswith("draws_")},
                    })  # fmt: skip
        delta = np.asarray(dA.get("delta_cup", np.zeros(1)))
        for model, dd, dg, hit in (("P", dP, diagP, cached is not None),
                                   ("A3", dA, diagA, cachedA is not None)):  # fmt: skip
            g = {"model": model, "bundle": bundle, "season": season, "as_of": as_of.isoformat(),
                 "on_grid": tgt is not None, "cached": hit, **dg, "gates_ok": gates_ok(dg),
                 "tier_gap": tier_gap(dd, fd)}  # fmt: skip
            if model == "A3":
                g |= {"redone": only is not None, "cup_rows": cr.n, "cup_dropped": dropped,
                      "cup_neutral": int(cr.neutral.sum()), "delta_cup": float(delta.mean()),
                      "delta_cup_sd": float(delta.std())}  # fmt: skip
            diags.append(g)
        print(f"  refit {as_of:%Y-%m-%d}: {0 if tgt is None else len(tgt)} league targets, "
              f"{grp.height} ties, {cr.n} cup rows ({dropped} out); P {diagP.get('fit_s', 0):.0f}"
              f" s; A3 {diagA.get('fit_s', 0):.0f} s rhat {diagA['rhat_max']:.3f}, delta_cup "
              f"{float(delta.mean()):+.3f}", flush=True)  # fmt: skip
        if save is not None:
            save(pl.DataFrame(league_rows), pl.DataFrame(cup_rows), diags)
    return pl.DataFrame(league_rows), pl.DataFrame(cup_rows), diags


def sensitivity(df: pl.DataFrame, leagues: list[int], bundle: str, store: RawStore,
                ties: pl.DataFrame, fitter: Callable[[bt.RunSpec, cs.CupRows], bt.Fitter]
                | None = None, cfg: harness.EvalConfig | None = None,
                ) -> dict[str, Any]:  # fmt: skip
    """delta_cup at the bundle's last dev refit under prior sd 0.1 and 0.5 (0.25 is the base
    run's). Cached like any A3 fit."""
    cfg = cfg or harness.load_config()
    P = bt.production_spec(cfg)
    last = max(cfg.dev_seasons)
    train = training_ties(ties, cfg)
    plan = harness.refit_plan(df, cfg, [last], P.every_days, None, P.grid)
    weeks = {harness.week_start(k) for k in ties.filter(pl.col("season") == last)["kickoff"]}
    as_of = refit_times(plan, weeks)[-1][0]
    fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, cfg.result_available_after_hours)
    cr, _ = cs.cup_rows(fd, train, as_of, cfg.lookback_days, cfg.result_available_after_hours,
                        min(cfg.holdout_seasons))  # fmt: skip
    out: dict[str, Any] = {"bundle": bundle, "as_of": as_of.isoformat(), "delta_cup": {}}
    for sd in SENSITIVITY_SD:
        spec = a3_spec(cfg, table_digest(train), sd)
        key = bt.cache_key(bt.fit_hash(spec), bundle, as_of)
        got = try_get(store, key)
        if got is None:
            d, diag = (fitter or a3_fitter)(spec, cr)(fd, spec.seed)
            store.put(key, bt.encode(d, diag))
        else:
            d, diag = bt.decode(got)
        dc_ = np.asarray(d.get("delta_cup", np.zeros(1)))
        out["delta_cup"][str(sd)] = {"mean": float(dc_.mean()), "sd": float(dc_.std()),
                                     "gates_ok": gates_ok(diag)}  # fmt: skip
        print(f"{bundle} {as_of:%Y-%m-%d} prior sd {sd}: delta_cup {dc_.mean():+.3f} "
              f"(sd {dc_.std():.3f})", flush=True)  # fmt: skip
    return out


# --------------------------------------------------------------------------- gate


def _ece(g: pl.DataFrame) -> dict[str, float]:
    return cup_terms._ece(g)


def _rel(g: pl.DataFrame, m: str) -> dict[str, float]:
    p = harness.paired(g, m, "A3", "P")
    base = float(g.filter(pl.col("model") == "P")[m].mean())  # type: ignore[arg-type]
    return {"rel": p["mean_diff"] / base, "lo": p["lo"] / base, "hi": p["hi"] / base,
            "n": p["n"]}  # fmt: skip


def verdict(
    league: pl.DataFrame,
    cups: pl.DataFrame,
    diags: list[dict[str, Any]],
    cfg: harness.EvalConfig,
    cup_names: dict[int, str],
) -> dict[str, Any]:
    """The A3 gate (checks 1-4; check 5, leakage, is the unit tests)."""
    # 1. cups improve, as in A2 (A2's compare, P in A1's place)
    P = cups.filter(pl.col("model") == "P").sort("match_id")
    A = cups.filter(pl.col("model") == "A3").sort("match_id")
    if P["match_id"].to_list() != A["match_id"].to_list():
        raise ValueError("P and A3 scored different cup ties")
    c1 = cup_terms.compare(P, A, cup_names)
    # 2. leagues no worse
    cmp = bt.compare(league, "A3", "P")
    seg = cmp.filter((pl.col("segment_type") == "league_id")
                     & pl.col("metric").is_in(cfg.phase1b_gate.segment_metrics))  # fmt: skip
    worst = seg.sort("rel", descending=True).row(0, named=True) if seg.height else None
    overall = {m: _rel(league.filter(~pl.col("cold_start")), m)
               for m in ("1X2:log_loss", "1X2:rps")}  # fmt: skip
    seg_ok = worst is None or worst["rel"] <= cfg.phase1b_gate.max_segment_rel_worse
    overall_ok = all(v["lo"] <= 0 for v in overall.values())
    # 3. sampler gates on every A3 refit (after any redo)
    a3 = [g for g in diags if g["model"] == "A3"]
    failing_ = [f"{g['bundle']} {g['as_of'][:10]}" for g in a3 if not g["gates_ok"]]
    # 4. timing: A3's median fit time against P's on the same refits
    tA = float(np.median([g["fit_s"] for g in a3 if not g.get("redone")]))
    tP = float(np.median([g["fit_s"] for g in diags if g["model"] == "P" and "fit_s" in g]))
    checks = {
        "1_cups_improve": bool(c1["passes"]),
        "2_leagues_no_worse": bool(seg_ok and overall_ok),
        "3_sampler_gates": not failing_,
        "4_timing": tA <= TIMING_MAX * tP,
    }
    # reported
    no_neutral = cups.filter(~pl.col("neutral"))
    hg, ag = P["hg"].to_numpy(), P["ag"].to_numpy()
    obs = {"home": float(np.mean(hg > ag)), "draw": float(np.mean(hg == ag)),
           "away": float(np.mean(hg < ag))}  # fmt: skip
    tot = (hg + ag).astype(np.float64)
    by: dict[str, dict[str, Any]] = {}
    for col in ("pairing", "cup_id"):
        for (v,), g in cups.group_by([col]):
            key = cup_names.get(int(v), str(v)) if col == "cup_id" else str(v)
            by[f"{col}:{key}"] = {m: _rel(g, m)["rel"] for m in ("1X2:log_loss", "1X2:rps")}
            by[f"{col}:{key}"]["n"] = g.height // 2
    shift = {}
    for b in sorted({g["bundle"] for g in diags}):
        pa = {g["as_of"]: g["tier_gap"] for g in diags if g["bundle"] == b and g["model"] == "P"}
        aa = {g["as_of"]: g["tier_gap"] for g in a3 if g["bundle"] == b}
        d = [aa[k] - pa[k] for k in aa if k in pa and np.isfinite(aa[k] - pa[k])]
        shift[b] = float(np.mean(d)) if d else float("nan")
    return {
        "passed": all(checks.values()), "checks": checks,
        "cups": {"n": P.height, "rel": c1["rel"], "worst_segment": c1["worst_segment"],
                 "ece_delta": c1["ece_delta"], "ece_P": _ece(P), "ece_A3": _ece(A)},
        "cups_without_neutral": {"n": no_neutral.height // 2,
                                 **{m: _rel(no_neutral, m) for m in ("1X2:log_loss",
                                                                     "1X2:rps")}},  # fmt: skip
        "leagues": {"n": int(overall["1X2:log_loss"]["n"]), "overall": overall,
                    "worst_segment": worst, "segment_ok": seg_ok, "overall_ok": overall_ok},
        "sampler": {"a3_refits": len(a3), "failing": failing_,
                    "redone": sum(bool(g.get("redone")) for g in a3)},
        "timing": {"a3_median_s": tA, "p_median_s": tP, "ratio": tA / tP if tP else None},
        "reported": {
            "score_log_loss": _rel(cups, "score:log_loss"), "by": by,
            "ppc": {"mean_p": {o: float(A[c].mean()) for o, c in  # type: ignore[arg-type]
                               zip(("home", "draw", "away"), OUTCOMES, strict=True)},
                    "observed": obs,
                    "total_dispersion_observed": float(tot.var(ddof=1) / tot.mean()),
                    "total_dispersion_predicted": float(np.mean(A["total_var"].to_numpy())
                                                        / np.mean(A["exp_total"].to_numpy()))},
            "delta_cup_last": {b: next((g["delta_cup"], g["delta_cup_sd"]) for g in
                                       sorted(a3, key=lambda x: x["as_of"], reverse=True)
                                       if g["bundle"] == b) for b in shift},
            "tier_gap_shift": shift,
            "cup_rows_dropped_max": max((g["cup_dropped"] for g in a3), default=0),
        },
    }  # fmt: skip


def markdown(v: dict[str, Any], meta: dict[str, Any]) -> str:
    def pct(x: float) -> str:
        return f"{100 * x:+.2f}%"

    def iv(r: dict[str, float]) -> str:
        return f"{pct(r['rel'])} [{pct(r['lo'])}, {pct(r['hi'])}]"

    c, lg = v["cups"], v["leagues"]
    lines = [
        f"# A3, cups as training data: {'PASSES' if v['passed'] else 'FAILS'}", "",
        *(f"- check {k}: {'pass' if ok else 'FAIL'}" for k, ok in v["checks"].items()), "",
        f"**Cups** ({c['n']} dev bundle ties, A3 vs P):",
        f"- 1X2 log loss {iv(c['rel']['1X2:log_loss'])}, RPS {iv(c['rel']['1X2:rps'])}",
        f"- worst cup segment: {c['worst_segment']['cup']} {c['worst_segment']['metric']} "
        f"{pct(c['worst_segment']['rel'])}",
        "- ECE delta " + ", ".join(f"{k} {x:+.4f}" for k, x in c["ece_delta"].items()),
        f"- without neutral ties ({v['cups_without_neutral']['n']}): 1X2 log loss "
        f"{iv(v['cups_without_neutral']['1X2:log_loss'])}", "",
        f"**Leagues** ({lg['n']} warm matches): 1X2 log loss {iv(lg['overall']['1X2:log_loss'])},"
        f" RPS {iv(lg['overall']['1X2:rps'])}",
    ]  # fmt: skip
    if lg["worst_segment"] is not None:
        w = lg["worst_segment"]
        lines.append(f"- worst league segment: {w['seg']} {w['metric']} {pct(w['rel'])}")
    s, t, r = v["sampler"], v["timing"], v["reported"]
    lines += [
        "", f"**Sampler:** {s['a3_refits']} A3 refits, {len(s['failing'])} failing, "
        f"{s['redone']} redone" + (f" (failing: {', '.join(s['failing'][:10])})"
                                    if s["failing"] else ""),
        f"**Timing:** A3 median {t['a3_median_s']:.0f} s, P {t['p_median_s']:.0f} s "
        f"(x{t['ratio']:.2f}, limit {TIMING_MAX})", "",
        "**Reported:**",
        f"- 90' score log loss {iv(r['score_log_loss'])}",
        *(f"- {k} (n {x['n']}): 1X2 log loss {pct(x['1X2:log_loss'])}, RPS {pct(x['1X2:rps'])}"
          for k, x in sorted(r["by"].items())),
        "- PPC mean p " + ", ".join(f"{k} {x:.3f}/{r['ppc']['observed'][k]:.3f}"
                                    for k, x in r["ppc"]["mean_p"].items())
        + f"; total-goals dispersion {r['ppc']['total_dispersion_predicted']:.3f} predicted, "
        f"{r['ppc']['total_dispersion_observed']:.3f} observed",
        *(f"- delta_cup at the last refit, {b}: {m:+.3f} (sd {sd:.3f})"
          for b, (m, sd) in r["delta_cup_last"].items()),
        *(f"- tier-2 minus tier-1 strength, A3 - P, {b}: {x:+.4f}"
          for b, x in r["tier_gap_shift"].items()),
        "- cup rows left out of a window (side with no league row), at most "
        f"{r['cup_rows_dropped_max']}",
    ]  # fmt: skip
    for b, sens in meta.get("sensitivity", {}).items():
        lines.append(
            f"- prior sensitivity {b}: "
            + ", ".join(f"sd {k}: {x['mean']:+.3f}" for k, x in sens["delta_cup"].items())
        )
    if "a1_max_diff" in meta:
        lines.append(
            f"- P's cup forecasts against A1's stored rows: max difference "
            f"{meta['a1_max_diff']:.2e}"
        )
    if meta.get("override"):
        lines.append(
            f"- A3 refits redone by run {meta['override']} (target_accept "
            f"{REDO_TARGET_ACCEPT}, {REDO_DRAWS} draws)"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- parts


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def _part_keys(run: str, bundles: Sequence[str], seasons: Sequence[int],
               parts: int) -> list[tuple[str, int, int, str]]:  # fmt: skip
    return [(b, s, i, f"{PREFIX}/parts/{run}/{b}_{s}_{i}of{parts}")
            for b in bundles for s in seasons for i in range(parts)]  # fmt: skip


def _read_part(store: RawStore, key: str
               ) -> tuple[pl.DataFrame, pl.DataFrame, list[dict[str, Any]]] | None:  # fmt: skip
    meta = try_get(store, f"{key}.json")
    if meta is None:
        return None
    lg = pl.read_parquet(io.BytesIO(store.get(f"{key}_league.parquet")))
    cu = pl.read_parquet(io.BytesIO(store.get(f"{key}_cups.parquet")))
    return lg, cu, json.loads(meta)["diags"]


def merge_redo(base: tuple[pl.DataFrame, pl.DataFrame, list[dict[str, Any]]],
               over: tuple[pl.DataFrame, pl.DataFrame, list[dict[str, Any]]],
               ) -> tuple[pl.DataFrame, pl.DataFrame, list[dict[str, Any]]]:  # fmt: skip
    """A part with some A3 refits redone: the redo's A3 rows and diagnostics replace the
    base run's for those refits; P is kept from the base run (the same cached draws)."""
    redone = {g["as_of"] for g in over[2] if g["model"] == "A3"}
    if not redone <= {g["as_of"] for g in base[2] if g["model"] == "A3"}:
        raise ValueError("a redone refit is not in the base part")
    times = [datetime.fromisoformat(t) for t in redone]
    out = []
    for b, o in ((base[0], over[0]), (base[1], over[1])):
        if b.height == 0:
            out.append(b)
            continue
        hit = (pl.col("model") == "A3") & pl.col("as_of").is_in(times)
        new = o.filter(hit).select(b.columns) if o.height else o
        out.append(pl.concat([b.filter(~hit), new]) if new.height else b.filter(~hit))
    diags = [g for g in base[2] if not (g["model"] == "A3" and g["as_of"] in redone)]
    diags += [g for g in over[2] if g["model"] == "A3"]
    return out[0], out[1], diags


def failing(store: RawStore, run: str, bundles: Sequence[str], seasons: Sequence[int],
            parts: int) -> list[dict[str, Any]]:  # fmt: skip
    """The A3 refits of a run that fail the sampler gates, one entry per part."""
    out = []
    for b, s, i, key in _part_keys(run, bundles, seasons, parts):
        raw = try_get(store, f"{key}.json")
        if raw is None:
            raise SystemExit(f"missing part {key}")
        bad = sorted(g["as_of"][:10] for g in json.loads(raw)["diags"]
                     if g["model"] == "A3" and not g["gates_ok"])  # fmt: skip
        if bad:
            out.append({"bundle": b, "season": s, "part": f"{i}/{parts}", "only": ",".join(bad)})
    return out


def cup_names() -> dict[int, str]:
    raw = yaml.safe_load(cup_ties.FORMATS_PATH.read_text())["formats"]
    return {int(k): str(v["name"]) for k, v in raw.items()}


def a1_max_diff(store: RawStore, cups: pl.DataFrame) -> float | None:
    """P's cup forecasts against A1's stored rows (the same model and refits)."""
    raw = try_get(store, f"{cb.CUP_PREFIX}/reports/{A1_RUN}/predictions.parquet")
    if raw is None:
        return None
    a1 = pl.read_parquet(io.BytesIO(raw)).select("match_id", *OUTCOMES)
    j = (
        cups.filter(pl.col("model") == "P")
        .select("match_id", *OUTCOMES)
        .join(a1, on="match_id", suffix="_a1")
    )
    if j.height == 0:
        return None
    return max(float((j[c] - j[f"{c}_a1"]).abs().max()) for c in OUTCOMES)  # type: ignore[arg-type]


def assemble(store: RawStore, run: str, bundles: Sequence[str], seasons: Sequence[int],
             parts: int, override: str = "") -> str:  # fmt: skip
    cfg = harness.load_config()
    leagues, cups, diags, missing = [], [], [], []
    for b, s, i, key in _part_keys(run, bundles, seasons, parts):
        got = _read_part(store, key)
        if got is None:
            missing.append(key)
            continue
        if override:
            o = _read_part(store, f"{PREFIX}/parts/{override}/{b}_{s}_{i}of{parts}")
            if o is not None:
                got = merge_redo(got, o)
        leagues.append(got[0])
        cups.append(got[1])
        diags += got[2]
    if missing:
        raise SystemExit(f"missing parts: {len(missing)} (first {missing[0]})")
    lg = pl.concat([x for x in leagues if x.height], how="diagonal_relaxed")
    cu = pl.concat([x for x in cups if x.height], how="diagonal_relaxed")
    v = verdict(lg, cu, diags, cfg, cup_names())
    meta: dict[str, Any] = {"override": override, "sensitivity": {}}
    for b in bundles:
        raw = try_get(store, f"{PREFIX}/sensitivity/{run}/{b}.json")
        if raw is not None:
            meta["sensitivity"][b] = json.loads(raw)
    d = a1_max_diff(store, cu)
    if d is not None:
        meta["a1_max_diff"] = d
    out = markdown(v, meta)
    rid = override or run
    store.put(f"{PREFIX}/reports/{rid}/league.parquet", _parquet(lg))
    store.put(f"{PREFIX}/reports/{rid}/cups.parquet", _parquet(cu))
    store.put(f"{PREFIX}/reports/{rid}/verdict.json",
              json.dumps({**v, "meta": meta}, default=str).encode())  # fmt: skip
    return out


# --------------------------------------------------------------------------- CLI


def _bundle_data(bundle: str) -> tuple[list[int], pl.DataFrame, pl.DataFrame]:
    from adatta.db.session import get_session

    cfg = harness.load_config()
    leagues = cfg.bundles[bundle]
    P = bt.production_spec(cfg)
    df = bt._load(leagues, P.xg, P.manager)
    with get_session() as s:
        ties = cb.bundle_targets(s, bundle, df, leagues)
        s.rollback()
    return leagues, df, ties


def main(argv: Sequence[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m adatta.model.cup_train")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--season", type=int, required=True)
    r.add_argument("--part", default="0/1")
    r.add_argument("--upload", required=True)
    r.add_argument("--only", default="", help="comma-separated A3 refit dates to redo")
    sn = sub.add_parser("sensitivity")
    sn.add_argument("--bundle", required=True)
    sn.add_argument("--upload", required=True)
    sub.add_parser("plan", help="refits per bundle-season and P's cache coverage; fits nothing")
    for name in ("failing", "assemble"):
        a = sub.add_parser(name)
        a.add_argument("--fetch", required=True)
        a.add_argument("--bundles", required=True)
        a.add_argument("--seasons", required=True)
        a.add_argument("--parts", type=int, default=1)
        if name == "assemble":
            a.add_argument("--override", default="", help="a redo run whose A3 refits replace")
    args = p.parse_args(argv)
    store = make_store(get_settings())
    if args.cmd == "run":
        leagues, df, ties = _bundle_data(args.bundle)
        i, n = (int(x) for x in args.part.split("/"))
        key = f"{PREFIX}/parts/{args.upload}/{args.bundle}_{args.season}_{i}of{n}"

        def save(lg: pl.DataFrame, cu: pl.DataFrame, diags: list[dict[str, Any]]) -> None:
            store.put(f"{key}_league.parquet", _parquet(lg))
            store.put(f"{key}_cups.parquet", _parquet(cu))
            store.put(f"{key}.json", json.dumps({"diags": diags}, default=str).encode())

        only = set(args.only.split(",")) if args.only else None
        lg, cu, diags = run_part(df, leagues, args.bundle, args.season, store, ties, (i, n),
                                 save, only)  # fmt: skip
        save(lg, cu, diags)
        a3 = [d for d in diags if d["model"] == "A3"]
        print(f"{args.bundle} {args.season}: {len(a3)} refits, A3 passing the gates "
              f"{sum(d['gates_ok'] for d in a3)}")  # fmt: skip
    elif args.cmd == "sensitivity":
        leagues, df, ties = _bundle_data(args.bundle)
        out = sensitivity(df, leagues, args.bundle, store, ties)
        store.put(f"{PREFIX}/sensitivity/{args.upload}/{args.bundle}.json",
                  json.dumps(out).encode())  # fmt: skip
    elif args.cmd == "plan":
        cfg = harness.load_config()
        P = bt.production_spec(cfg)
        hP = bt.fit_hash(P)
        for b in cfg.bundles:
            leagues, df, ties = _bundle_data(b)
            for season in cfg.dev_seasons:
                plan = harness.refit_plan(df, cfg, [season], P.every_days, None, P.grid)
                weeks = {harness.week_start(k)
                         for k in ties.filter(pl.col("season") == season)["kickoff"]}  # fmt: skip
                times = refit_times(plan, weeks)
                cached = sum(try_get(store, bt.cache_key(hP, b, a)) is not None for a, _ in times)
                off = sum(t is None for _, t in times)
                print(f"{b} {season}: {len(times)} refits ({off} tie Mondays off the grid), "
                      f"P cached {cached}")  # fmt: skip
    elif args.cmd == "failing":
        seasons = [int(s) for s in args.seasons.split(",")]
        print(json.dumps(failing(store, args.fetch, args.bundles.split(","), seasons,
                                 args.parts)))  # fmt: skip
    else:
        seasons = [int(s) for s in args.seasons.split(",")]
        print(assemble(store, args.fetch, args.bundles.split(","), seasons, args.parts,
                       args.override))  # fmt: skip


if __name__ == "__main__":
    main()
