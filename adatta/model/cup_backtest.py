"""Stage A, A1: the reference forecast of domestic cup ties between bundle teams
(docs/BEYOND_LEAGUES_PLAN.md, Stage A).

The bundle's production 1b model, applied unchanged: each tie is scored from the weekly refit
as of the Monday of its week (`harness.week_start`), the same refit (and the same cache key)
as that week's league matches, so league and cup forecasts share their fits. A Monday without
a cached refit is fitted and cached like any backtest refit. The listed home side gets its
league home advantage, neutral ground or not (the neutral term is A2's T2); a tie the venue
role marks `swapped` (the listed away side at its own ground) is scored with the sides
swapped, so the side at home gets the home term. The tie's league (intercept and home
advantage) is the home side's league that season.

Dev seasons only: holdout seasons raise HoldoutLocked; their cup ties are opened once, with
their own marker, for the Stage A promotion decision.

    # one bundle x season (a CI matrix job; fitting needs the model group)
    uv run --frozen --group model python -m adatta.model.cup_backtest run \\
        --bundle england --season 2019 --upload RUN_ID
    # combine the parts, metrics only (CI logs are public)
    uv run python -m adatta.model.cup_backtest assemble --fetch RUN_ID
    # what a run would fit, without fitting (no model group)
    uv run python -m adatta.model.cup_backtest plan
"""

from __future__ import annotations

import argparse
import bisect
import io
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import yaml

from adatta.adapters.storage import RawStore, try_get
from adatta.eval import harness, phase2
from adatta.features import cup_ties
from adatta.model import backtest as bt
from adatta.model import team_strength as ts

CUP_PREFIX = "cups"
FINISHED = ("FT", "AET", "PEN")
OUT_DEFAULT = "reports/stage_a/a1"


def cups_of(bundle: str, path: Path | str = cup_ties.FORMATS_PATH) -> list[int]:
    """The provider ids of the domestic cups of a bundle (`config/cup_formats.yml`)."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return sorted(
        int(k) for k, v in (raw.get("formats") or {}).items() if v.get("bundle") == bundle
    )


# --------------------------------------------------------------------------- targets

TIE_SCHEMA: dict[str, Any] = {
    "match_id": pl.Int64, "cup_id": pl.Int64, "season": pl.Int64,
    "kickoff": pl.Datetime("us", "UTC"), "round": pl.Utf8, "home": pl.Int64, "away": pl.Int64,
    "hg": pl.Int64, "ag": pl.Int64, "role": pl.Utf8, "kind": pl.Utf8,
}  # fmt: skip


def build_targets(ties: pl.DataFrame, df: pl.DataFrame, leagues: Sequence[int]) -> pl.DataFrame:
    """Cup ties (TIE_SCHEMA) between two teams that play a bundle league that season, as
    targets for `bt.score_targets`: league_id is the home side's league, tiers are 1-based
    positions in `leagues`, mover codes are each team's own in that league season, and a
    `swapped` tie has its sides (and score) swapped."""
    mh, ma = harness.mover_codes(df, list(leagues))
    sides = pl.concat([
        df.select("season", "league_id", team="home", mover=pl.Series(mh)),
        df.select("season", "league_id", team="away", mover=pl.Series(ma)),
    ])  # fmt: skip
    of = (
        sides.group_by("team", "season")
        .agg(pl.col("league_id").mode().first(), pl.col("mover").max())
    )  # fmt: skip
    swap = pl.col("role") == "swapped"
    t = ties.with_columns(
        home=pl.when(swap).then(pl.col("away")).otherwise(pl.col("home")),
        away=pl.when(swap).then(pl.col("home")).otherwise(pl.col("away")),
        hg=pl.when(swap).then(pl.col("ag")).otherwise(pl.col("hg")),
        ag=pl.when(swap).then(pl.col("hg")).otherwise(pl.col("ag")),
    )
    tier = {lid: i + 1 for i, lid in enumerate(leagues)}
    out = (
        t.join(of.rename({"team": "home", "mover": "mover_home"}),
               on=["home", "season"], how="inner")
        .join(of.rename({"team": "away", "league_id": "league_away", "mover": "mover_away"}),
              on=["away", "season"], how="inner")
        .with_columns(
            tier_home=pl.col("league_id").replace_strict(tier),
            tier_away=pl.col("league_away").replace_strict(tier),
        )
        .drop("league_away")
        .sort("kickoff", "match_id")
    )  # fmt: skip
    gaps = next_league_gaps(out, df)
    return out.join(gaps, on="match_id", how="left").with_columns(
        pairing=pl.concat_str(
            [
                pl.min_horizontal("tier_home", "tier_away"),
                pl.max_horizontal("tier_home", "tier_away"),
            ],
            separator="v",
        )
    )


def next_league_gaps(ties: pl.DataFrame, df: pl.DataFrame) -> pl.DataFrame:
    """Days from each tie to each side's next league match as played (A2's T3; null when the
    side plays no later league match in `df`)."""
    kicks: dict[int, list[Any]] = {}
    for h, a, k in zip(df["home"], df["away"], df["kickoff"], strict=True):
        kicks.setdefault(int(h), []).append(k)
        kicks.setdefault(int(a), []).append(k)
    for v in kicks.values():
        v.sort()

    def gap(team: int, at: Any) -> float | None:
        ks = kicks.get(team, [])
        i = bisect.bisect_right(ks, at)
        return (ks[i] - at).total_seconds() / 86400 if i < len(ks) else None

    kick = ties["kickoff"].to_list()
    return pl.DataFrame(
        {"match_id": ties["match_id"],
         "gap_home_days": [gap(int(t), k) for t, k in zip(ties["home"], kick, strict=True)],
         "gap_away_days": [gap(int(t), k) for t, k in zip(ties["away"], kick, strict=True)]},
        schema={"match_id": pl.Int64, "gap_home_days": pl.Float64, "gap_away_days": pl.Float64},
    )  # fmt: skip


# --------------------------------------------------------------------------- scoring


def score_season(
    df: pl.DataFrame, leagues: list[int], bundle: str, cfg: harness.EvalConfig, season: int,
    spec: bt.RunSpec, store: RawStore, targets: pl.DataFrame, fitter: bt.Fitter | None = None,
    log: Callable[[str], None] = print,
) -> pl.DataFrame:  # fmt: skip
    """Score one dev season's cup targets of one bundle. Returns one row per tie."""
    if season in cfg.holdout_seasons:
        raise harness.HoldoutLocked(f"season {season} is the locked holdout")
    if season not in cfg.dev_seasons:
        raise ValueError(f"A1 scores dev seasons {cfg.dev_seasons}, not {season}")
    if spec.groups or spec.widen or spec.grid != "weekly" or spec.every_days != 7:
        raise ValueError("A1 applies the production 1b model unchanged (weekly, no groups)")
    tg = targets.filter(pl.col("season") == season).with_columns(
        as_of=pl.col("kickoff").map_elements(
            harness.week_start, return_dtype=pl.Datetime("us", "UTC")
        )
    )
    fitter = fitter or bt.nuts_fitter(spec)
    h, lag = bt.fit_hash(spec), cfg.result_available_after_hours
    weeks = tg["as_of"].unique().sort().to_list()
    log(f"{bundle} {season}: {tg.height} cup ties in {len(weeks)} refit weeks, "
        f"model {spec.model_name}, fit hash {h}")  # fmt: skip
    rows: list[dict[str, Any]] = []
    for i, as_of in enumerate(weeks):
        grp = tg.filter(pl.col("as_of") == as_of)
        assert (grp["kickoff"] >= as_of).all(), "a tie before its refit's as-of time"
        fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, lag, spec.groups)
        key = bt.cache_key(h, bundle, as_of)
        cached = try_get(store, key)
        if cached is not None:
            draws, diag = bt.decode(cached)
        else:
            draws, diag = fitter(fd, spec.seed)
            store.put(key, bt.encode(draws, diag))
        counts = np.bincount(np.concatenate([fd.home, fd.away]), minlength=len(fd.team_ids))
        n_of = {int(t): int(counts[j]) for j, t in enumerate(fd.team_ids)}
        scored = bt.score_targets(draws, fd, grp, cfg.max_goals, exact=True)
        homes = home_terms(draws, fd, grp)
        for r, s, ht in zip(grp.iter_rows(named=True), scored, homes, strict=True):
            n_h, n_a = n_of.get(int(r["home"]), 0), n_of.get(int(r["away"]), 0)
            rows.append({
                "match_id": r["match_id"], "cup_id": r["cup_id"], "season": season,
                "kickoff": r["kickoff"], "as_of": as_of, "round": r["round"], "kind": r["kind"],
                "role": r["role"], "neutral": r["role"] == "neutral", "pairing": r["pairing"],
                "league_id": r["league_id"], "home": r["home"], "away": r["away"],
                "tier_home": r["tier_home"], "tier_away": r["tier_away"],
                "gap_home_days": r["gap_home_days"], "gap_away_days": r["gap_away_days"],
                "hg": r["hg"], "ag": r["ag"], "n_train": len(fd.hg),
                "cold_start": min(n_h, n_a) < cfg.min_warmup_matches,
                "bundle": bundle, "model": spec.model_name, "fit_hash": h,
                "cached": cached is not None, **{f"refit_{k}": v for k, v in diag.items()}, **s,
                "draws_home_term": ht,
            })  # fmt: skip
        log(f"  refit {i + 1}/{len(weeks)} as of {as_of:%Y-%m-%d}: {grp.height} ties, "
            f"{'cached' if cached is not None else 'fitted'}, "
            f"R-hat {diag.get('rhat_max', float('nan')):.3f}")  # fmt: skip
    return pl.DataFrame(rows)


def home_terms(draws: bt.Draws, fd: ts.FitData, targets: pl.DataFrame) -> list[list[float]]:
    """Per target, each draw's home term on log lambda_home (league h plus the home side's own,
    as `ts.rates_from_draws` adds it; an unseen home side has none of its own), so A2 can
    take it off a neutral tie (T2)."""
    tix = {int(t): i for i, t in enumerate(fd.team_ids)}
    lix = {lid: i for i, lid in enumerate(fd.leagues)}
    h_l, h_t = draws["h_league"], draws["h_team"] * draws["sigma_home"]
    out = []
    for home, lid in zip(targets["home"], targets["league_id"], strict=True):
        j = tix.get(int(home), -1)
        h = h_l[lix[int(lid)]] + (h_t[j] if j >= 0 else 0.0)
        out.append(np.asarray(h, dtype=np.float64).tolist())
    return out


# --------------------------------------------------------------------------- report

METRICS = ("1X2:log_loss", "1X2:rps", "1X2:brier", "score:log_loss", "OU2.5:log_loss",
           "total:crps")  # fmt: skip


def _ece(g: pl.DataFrame) -> dict[str, float]:
    hg, ag = g["hg"].to_numpy(), g["ag"].to_numpy()
    return {name: phase2.ece(g[col].to_numpy(), hit.astype(float))
            for name, col, hit in (("home", "p_home", hg > ag), ("draw", "p_draw", hg == ag),
                                   ("away", "p_away", hg < ag))}  # fmt: skip


def _block(g: pl.DataFrame) -> dict[str, float]:
    hg, ag = g["hg"].to_numpy(), g["ag"].to_numpy()
    return {
        "n": g.height,
        **{m: bt._mean(g[m]) for m in METRICS},
        "p_home": bt._mean(g["p_home"]), "home_rate": float((hg > ag).mean()),
        "p_draw": bt._mean(g["p_draw"]), "draw_rate": float((hg == ag).mean()),
        "p_away": bt._mean(g["p_away"]), "away_rate": float((hg < ag).mean()),
        **{f"ece_{k}": v for k, v in _ece(g).items()},
    }  # fmt: skip


def summarise(pred: pl.DataFrame, cup_names: dict[int, str]) -> dict[str, Any]:
    """Overall and per-segment metrics (cup, tier pairing, tie kind, neutral, season), the
    1X2 reliability table, and the inference diagnostics of the refits used."""
    p = pred.with_columns(cup=pl.col("cup_id").replace_strict(cup_names, default="?"))
    seg: dict[str, dict[str, dict[str, float]]] = {}
    for col in ("cup", "pairing", "kind", "neutral", "season", "cold_start"):
        seg[col] = {str(k[0]): _block(g) for k, g in p.group_by(col, maintain_order=True)}
    refits = p.unique(["bundle", "as_of"])
    rel = []
    for name, col, hit in (("home", "p_home", p["hg"] > p["ag"]),
                           ("draw", "p_draw", p["hg"] == p["ag"]),
                           ("away", "p_away", p["hg"] < p["ag"])):  # fmt: skip
        d = pl.DataFrame({"p": p[col], "hit": hit.cast(pl.Float64)}).sort("p")
        for k, chunk in enumerate(np.array_split(np.arange(d.height), 5)):
            c = d[chunk.tolist()]
            rel.append(
                {
                    "outcome": name,
                    "bin": k + 1,
                    "n": c.height,
                    "mean_p": bt._mean(c["p"]),
                    "rate": bt._mean(c["hit"]),
                }
            )
    return {
        "overall": _block(p),
        "segments": seg,
        "reliability": rel,
        "refits": {
            "n": refits.height,
            "fitted": int((~refits["cached"]).sum()),
            "rhat_over_1.01": int((refits["refit_rhat_max"] > 1.01).sum()),
            "ess_under_400": int((refits["refit_ess_bulk_min"] < 400).sum()),
            "with_divergences": int((refits["refit_divergences"] > 0).sum()),
        },
        "tail_mass_max": float(p["tail_mass"].cast(pl.Float64).to_numpy().max()),
    }


def markdown(rep: dict[str, Any]) -> str:
    def row(name: str, b: dict[str, float]) -> str:
        return (f"| {name} | {b['n']} | {b['1X2:log_loss']:.4f} | {b['1X2:rps']:.4f} | "
                f"{b['score:log_loss']:.3f} | {b['p_home']:.3f} / {b['home_rate']:.3f} | "
                f"{b['p_draw']:.3f} / {b['draw_rate']:.3f} | {b['p_away']:.3f} / "
                f"{b['away_rate']:.3f} | {b['ece_home']:.3f} {b['ece_draw']:.3f} "
                f"{b['ece_away']:.3f} |")  # fmt: skip

    head = ("| segment | n | 1X2 log loss | RPS | 90' score log loss | home p / rate | "
            "draw p / rate | away p / rate | ECE h d a |\n"
            "|---|---|---|---|---|---|---|---|---|")  # fmt: skip
    lines = ["# Stage A, A1: the 1b model applied unchanged to dev cup ties", "", head,
             row("all", rep["overall"])]  # fmt: skip
    for col, blocks in rep["segments"].items():
        lines += [row(f"{col}: {k}", b) for k, b in sorted(blocks.items())]
    r = rep["refits"]
    lines += ["", f"Refits used: {r['n']} ({r['fitted']} fitted here); R-hat > 1.01 in "
              f"{r['rhat_over_1.01']}, ESS < 400 in {r['ess_under_400']}, divergences in "
              f"{r['with_divergences']}. Largest tail mass {rep['tail_mass_max']:.2e}.", "",
              "Reliability (quintiles of each outcome's probability: mean p / observed "
              "rate):", ""]  # fmt: skip
    for name in ("home", "draw", "away"):
        cells = [f"{x['mean_p']:.2f}/{x['rate']:.2f}" for x in rep["reliability"]
                 if x["outcome"] == name]  # fmt: skip
        lines.append(f"- {name}: " + ", ".join(cells))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- database


def load_ties(session: Any, cup_id: int) -> pl.DataFrame:
    """Finished ties of one cup with a 90' score (TIE_SCHEMA), with the venue role
    (`features.neutral`) and the tie kind (`features.cup_ties`)."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.features import neutral

    M, S, C, R = models.Match, models.Season, models.Competition, models.MatchResult
    rows = session.execute(
        select(M, S.year, R.home_goals_nt, R.away_goals_nt)
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .outerjoin(R, (R.match_id == M.match_id) & R.superseded_at.is_(None))
        .where(C.source_ref == str(cup_id))
    ).all()  # fmt: skip
    if not rows:
        return pl.DataFrame(schema=TIE_SCHEMA)
    labels, _ = cup_ties.label(
        [cup_ties.CupMatch(m.match_id, cup_id, y, m.round or "", m.home_team_id, m.away_team_id,
                           m.kickoff_time) for m, y, _, _ in rows],
        cup_ties.load_formats(),
    )  # fmt: skip
    from adatta.cli import _bundle_competition_ids

    league_ids, _ = _bundle_competition_ids(session)
    done = [r for r in rows if r[0].status in FINISHED and r[2] is not None and r[3] is not None]
    roles = neutral.venue_roles(session, [r[0] for r in done], league_ids, neutral.load_rules(),
                                {r[0].season_id: cup_id for r in done})  # fmt: skip
    role_of = dict(zip(roles["match_id"].to_list(), roles["role"].to_list(), strict=True))
    return pl.DataFrame(
        [{"match_id": m.match_id, "cup_id": cup_id, "season": y,
          "kickoff": m.kickoff_time.astimezone(UTC),
          "round": m.round or "", "home": m.home_team_id, "away": m.away_team_id, "hg": hg,
          "ag": ag, "role": role_of.get(m.match_id, "unknown"), "kind": labels[m.match_id].kind}
         for m, y, hg, ag in done],
        schema=TIE_SCHEMA,
    )  # fmt: skip


def bundle_targets(session: Any, bundle: str, df: pl.DataFrame, leagues: list[int]) -> pl.DataFrame:
    ties = [load_ties(session, c) for c in cups_of(bundle)]
    return build_targets(pl.concat(ties) if ties else pl.DataFrame(schema=TIE_SCHEMA), df, leagues)


# --------------------------------------------------------------------------- CLI


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf)
    return buf.getvalue()


def main() -> None:
    from adatta.db.session import get_session

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--season", type=int, required=True)
    r.add_argument("--draws", type=int, default=2000)
    r.add_argument("--upload", default=None, metavar="RUN_ID",
                   help="put the part in the store under cups/parts/RUN_ID/ "
                        "(never artifacts)")  # fmt: skip
    r.add_argument("--cache", default="data/cache", help="local cache root (if not R2)")
    p = sub.add_parser("plan", help="ties, refit weeks and cached refits; fits nothing")
    p.add_argument("--cache", default="data/cache")
    a = sub.add_parser("assemble")
    a.add_argument("--fetch", required=True, metavar="RUN_ID")
    a.add_argument(
        "--bundles", default=",".join(("england", "spain", "italy", "germany", "france"))
    )
    a.add_argument("--seasons", default="2019,2020,2021,2022,2023")
    a.add_argument("--out", default=OUT_DEFAULT)
    a.add_argument("--cache", default="data/cache")
    args = ap.parse_args()
    cfg = harness.load_config()
    spec = bt.production_spec(cfg)

    if args.cmd in ("run", "plan"):
        store = bt._store(args.cache)
        bundles = [args.bundle] if args.cmd == "run" else list(cfg.bundles)
        for b in bundles:
            leagues = cfg.bundles[b]
            df = bt._load(leagues, spec.xg, spec.manager)
            with get_session() as s:
                targets = bundle_targets(s, b, df, leagues)
                s.rollback()
            if args.cmd == "plan":
                dev = targets.filter(pl.col("season").is_in(cfg.dev_seasons))
                weeks = {harness.week_start(k) for k in dev["kickoff"].to_list()}
                h = bt.fit_hash(spec)
                cached = sum(try_get(store, bt.cache_key(h, b, w)) is not None for w in weeks)
                print(f"{b}: {dev.height} dev cup ties, {len(weeks)} refit weeks, "
                      f"{cached} cached under {h}")  # fmt: skip
                continue
            sp = replace(spec, draws=args.draws)
            pred = score_season(df, leagues, b, cfg, args.season, sp, store, targets)
            name = f"{b}_{args.season}.parquet"
            if args.upload:
                store.put(f"{CUP_PREFIX}/parts/{args.upload}/{name}", _parquet(pred))
            print(f"{b} {args.season}: {pred.height} ties scored")
        return

    store = bt._store(args.cache)
    frames = []
    for b in args.bundles.split(","):
        for season in args.seasons.split(","):
            data = try_get(store, f"{CUP_PREFIX}/parts/{args.fetch}/{b}_{season}.parquet")
            if data is None:
                print(f"missing part {b} {season}")
                continue
            frames.append(pl.read_parquet(io.BytesIO(data)))
    if not frames:
        raise SystemExit("no parts")
    pred = pl.concat(frames, how="diagonal_relaxed")
    names = {int(k): str(v["name"]) for k, v in (yaml.safe_load(
        cup_ties.FORMATS_PATH.read_text())["formats"]).items()}  # fmt: skip
    rep = summarise(pred, names)
    out = Path(args.out) / args.fetch
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text(markdown(rep))
    (out / "summary.json").write_text(json.dumps(rep, indent=1, default=str))
    store.put(f"{CUP_PREFIX}/reports/{args.fetch}/predictions.parquet", _parquet(pred))
    print(markdown(rep))


if __name__ == "__main__":
    main()
