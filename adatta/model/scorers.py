"""P3-e: goalscorer and assist forecasts allocated from P (docs/PHASE3_TASKS.md P3-e, restated
2026-11-19; spec §7.5 steps 3-4).

CLI (CI, model group; one backtest-type run at a time):
  pmf      --bundle B --season S --upload RUN
           P's team goal distributions on the weekly dev grid, from the fit cache only
           (a missing fit is an error, never a refit): scorers/pmf/RUN/<bundle>_<season>
  table    --goals Q1 --assists Q2 --export REPORT [--rates latest]
           the player table (features.scorers) and its leakage check, to R2. Q1, Q2 are
           `lineup quality` runs (goals: P3-d's; assists: `--stat assists`)
  assemble --pmf RUN --bundles ... --seasons ... [--table latest]
           the pass rule (e); metrics only to stdout, scored rows to R2
Holdout seasons are refused. The repo is public: logs carry metrics, never data.
"""

from __future__ import annotations

import argparse
import io
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import polars as pl

from adatta.adapters.storage import RawStore, make_store, try_get
from adatta.config import get_settings
from adatta.eval import rates as er
from adatta.eval import scorers as es
from adatta.eval.selection import DEV, HoldoutRefused
from adatta.features import lineup_offset as lo
from adatta.features import rates as fr
from adatta.features import scorers as fs
from adatta.features.referees import RESULT_LAG

PREFIX = "scorers"
LEAKAGE_N = 100
SEED = 20261119
LEAK_COLS = ("e_lk", "e24_start", "e24_bench", "o", "alpha",
             *[f"{c}_{s}" for s in fs.STATS for c in ("r0", "r1")],
             *[f"q_{s}_{r}" for s in fs.STATS for r in ("start", "sub")])  # fmt: skip


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


# --------------------------------------------------------------------------------- pmf


def pmf_key(run: str, bundle: str, season: int) -> str:
    return f"{PREFIX}/pmf/{run}/{bundle}_{season}.parquet"


def run_pmf(df: pl.DataFrame, leagues: list[int], bundle: str, season: int,
            store: RawStore) -> pl.DataFrame:  # fmt: skip
    """P's goal distribution of each side of every dev target on the weekly grid, from the
    cached P3-d fits of the production spec."""
    from adatta.eval import harness
    from adatta.model import backtest as bt
    from adatta.model import team_strength as ts

    if season not in DEV:
        raise HoldoutRefused(f"season {season} is not a dev season")
    cfg = harness.load_config()
    P = bt.production_spec(cfg)
    plan = harness.refit_plan(df, cfg, [season], P.every_days, None, P.grid)
    mh, ma = harness.mover_codes(df, leagues)
    df = df.with_columns(mover_home=pl.Series(mh), mover_away=pl.Series(ma))
    h = bt.fit_hash(P)
    print(f"{bundle} {season}: {len(plan)} refits, P {h}", flush=True)
    rows: list[dict[str, Any]] = []
    for as_of, tgt, _ in plan:
        cached = try_get(store, bt.cache_key(h, bundle, as_of))
        if cached is None:
            raise SystemExit(f"P fit {bundle} {as_of:%Y-%m-%d} is not cached (no refits in P3-e)")
        draws, _ = bt.decode(cached)
        fd = ts.prepare(df, leagues, as_of, cfg.lookback_days, cfg.result_available_after_hours)
        targets = df[tgt]
        t0 = time.monotonic()
        scored = bt.score_targets(draws, fd, targets, cfg.max_goals, pmf=True)
        for r, s in zip(targets.iter_rows(named=True), scored, strict=True):
            for side, team, pmf in (("home", r["home"], s["pmf_home"]),
                                    ("away", r["away"], s["pmf_away"])):  # fmt: skip
                rows.append({"match_id": int(r["match_id"]), "team_id": int(team),
                             "side": side, "league_id": int(r["league_id"]),
                             "season": int(r["season"]), "kickoff": r["kickoff"],
                             "as_of": as_of, "bundle": bundle, "pmf": pmf})  # fmt: skip
        print(f"  refit {as_of:%Y-%m-%d}: {len(targets)} targets, "
              f"{time.monotonic() - t0:.0f} s", flush=True)  # fmt: skip
    return pl.DataFrame(rows)


# ------------------------------------------------------------------------------- table


def birth_dates(player_ids: Sequence[int]) -> pl.DataFrame:
    from sqlalchemy import select

    from adatta.db import models
    from adatta.db.session import get_session

    with get_session() as s:
        got = s.execute(select(models.Player.player_id, models.Player.birth_date)
                        .where(models.Player.birth_date.is_not(None))).all()  # fmt: skip
        s.rollback()
    ids = set(player_ids)
    return pl.DataFrame([(p, b) for p, b in got if p in ids], orient="row",
                        schema={"player_id": pl.Int64,
                                "birth_date": pl.Datetime("us", "UTC")})  # fmt: skip


def targets_of(app: pl.DataFrame) -> pl.DataFrame:
    """Dev team-matches of the ten leagues with an announced squad."""
    return lo.team_matches(lo.squads(app)).filter(pl.col("season").is_in(DEV)).select(fs.KEY)


def leakage_check(app: pl.DataFrame, hist: pl.DataFrame, export: pl.DataFrame,
                  posts: dict[str, dict[datetime, Any]], table: pl.DataFrame,
                  n: int = LEAKAGE_N) -> dict[str, Any]:  # fmt: skip
    """For n sampled squad rows: rebuild their team-match from appearances final by the
    as-of time (kickoff - 24 h - RESULT_LAG). Matches from then up to the kickoff (the
    match itself included) keep their announced roles with minutes and every count
    removed; later matches are dropped. The counts table is cut the same way; P3-b rows
    are those of team-matches up to the kickoff (each made at its own T-24h). No input
    column (LEAK_COLS) may change."""
    leagues = tuple(er.leagues())
    dev = table.filter(pl.col("season").is_in(DEV) & pl.col("in_squad"))
    sample = dev.sample(min(n, dev.height), seed=SEED)
    counts = ["minutes", "goals", "own_goals", "assists", "pen_scored"]
    changed = 0
    for r in sample.iter_rows(named=True):
        cut = r["kickoff"] - fr.HORIZON - RESULT_LAG
        late = (pl.col("kickoff") > cut) & (pl.col("kickoff") <= r["kickoff"])
        a = pl.concat([
            app.filter(pl.col("kickoff") <= cut),
            app.filter(late).with_columns([pl.lit(None, pl.Int32).alias(c) for c in counts]),
        ])  # fmt: skip
        h = hist.filter(pl.col("kickoff") <= cut)
        ex = export.join(
            app.filter(pl.col("kickoff") <= r["kickoff"]).select(fs.KEY).unique(), on=fs.KEY
        )  # fmt: skip
        key = pl.DataFrame({"match_id": [r["match_id"]], "team_id": [r["team_id"]]})
        re_ = fs.build(a, h, posts, leagues, ex, key).filter(pl.col("player_id") == r["player_id"])
        if re_.is_empty():
            changed += 1
            continue
        x = re_.row(0, named=True)
        same = all(abs((x[c] if x[c] is not None else -1.0) - (r[c] if r[c] is not None else -1.0))
                   < 1e-9 for c in LEAK_COLS)  # fmt: skip
        changed += 0 if same else 1
    return {"leakage_sampled": sample.height, "leakage_changed": changed,
            "leakage_ok": changed == 0}  # fmt: skip


def build_table(store: RawStore, goals_run: str, assists_run: str, export_ref: str,
                rates_ref: str) -> None:  # fmt: skip
    from adatta.model import lineup as ml
    from adatta.model import rates as mr

    app, hist, export, rates_digest = ml.load_inputs(store, rates_ref, export_ref)
    leagues = tuple(er.leagues())
    times = ml.quality_times(mr._frame(hist, er.Design(leagues)))
    posts = {
        "goals": ml.load_posts(store, goals_run, times),
        "assists": ml.load_posts(store, assists_run, times),
    }
    t0 = time.monotonic()
    tg = targets_of(app)
    birth = birth_dates(app["player_id"].unique().to_list())
    table = fs.build(app, hist, posts, leagues, export, tg, birth)
    print(f"player table: {table.height} rows, {tg.height} team-matches in "
          f"{time.monotonic() - t0:.0f} s", flush=True)  # fmt: skip
    leak = leakage_check(app, hist, export, posts, table)
    print(f"leakage: {leak['leakage_changed']} of {leak['leakage_sampled']} changed", flush=True)
    cov = coverage(table)
    meta = {"rates": rates_digest, "quality_goals": goals_run, "quality_assists": assists_run,
            "export": export_ref, "coverage": cov, **leak,
            "built_at": datetime.now(UTC).isoformat()}  # fmt: skip
    h = fs.save(store, table, meta)
    print(f"stored player table {h}")
    for c in cov:
        print(f"  {c['league_ref']}: squad rows {c['squad']}, lk fallback {c['fallback']:.3f}, "
              f"zero e_lk {c['zero_lk']:.3f}, squad outside pool {c['outside_pool']:.3f}, "
              f"o {c['o']:.3f}, alpha {c['alpha']:.3f}")  # fmt: skip


def coverage(table: pl.DataFrame) -> list[dict[str, Any]]:
    sq = table.filter(pl.col("in_squad"))
    return sq.group_by("league_ref").agg(
        pl.len().alias("squad"),
        pl.col("lk_fallback").mean().alias("fallback"),
        (pl.col("e_lk") <= 0).mean().alias("zero_lk"),
        (~pl.col("in_pool")).mean().alias("outside_pool"),
        pl.col("o").mean(), pl.col("alpha").mean(),
    ).sort("league_ref").to_dicts()  # fmt: skip


# ----------------------------------------------------------------------------- assemble


def assemble(store: RawStore, pmf_run: str, bundles: list[str], seasons: list[int],
             table_ref: str) -> str:  # fmt: skip
    table, meta, digest = fs.load(store, table_ref)
    parts = []
    for b in bundles:
        for s in seasons:
            data = try_get(store, pmf_key(pmf_run, b, s))
            if data is None:
                raise SystemExit(f"missing pmf part {b} {s} of run {pmf_run}")
            parts.append(pl.read_parquet(io.BytesIO(data)))
    pmfs = pl.concat(parts).select(*fs.KEY, "pmf")
    table = table.filter(pl.col("season").is_in(seasons))
    scored = pl.concat([es.score(table, pmfs, h) for h in es.HORIZONS])
    gaps = es.consistency(table, pmfs)
    v = es.verdict(scored, gaps, bool(meta.get("leakage_ok")))
    squad = table.filter(pl.col("in_squad")).select(fs.KEY).unique()
    covered = squad.join(pmfs.select(fs.KEY).unique(), on=fs.KEY).height
    out = es.markdown(
        scored,
        v,
        {
            **meta,
            "table": digest,
            "pmf_matches": f"{covered} of {squad.height} squad team-matches have P's distribution",
        },
    )
    store.put(f"{PREFIX}/reports/{pmf_run}/scored.parquet", _parquet(scored))
    store.put(
        f"{PREFIX}/reports/{pmf_run}/verdict.json",
        json.dumps({"passed": v.passed, "checks": v.checks}, default=str).encode(),
    )
    return out


# --------------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m adatta.model.scorers")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("pmf")
    m.add_argument("--bundle", required=True)
    m.add_argument("--season", type=int, required=True)
    m.add_argument("--upload", required=True)
    t = sub.add_parser("table")
    t.add_argument("--goals", required=True, help="lineup quality run (goals)")
    t.add_argument("--assists", required=True, help="lineup quality run (--stat assists)")
    t.add_argument("--export", required=True, help="selection report id with the P3-d export")
    t.add_argument("--rates", default="latest")
    a = sub.add_parser("assemble")
    a.add_argument("--pmf", required=True)
    a.add_argument("--bundles", required=True)
    a.add_argument("--seasons", required=True)
    a.add_argument("--table", default="latest")
    args = p.parse_args(argv)
    store = make_store(get_settings())
    if args.cmd == "pmf":
        from adatta.eval import harness
        from adatta.model import backtest as bt

        cfg = harness.load_config()
        leagues = cfg.bundles[args.bundle]
        rows = run_pmf(bt._load(leagues, xg=True), leagues, args.bundle, args.season, store)
        store.put(pmf_key(args.upload, args.bundle, args.season), _parquet(rows))
        print(f"{args.bundle} {args.season}: {rows.height // 2} matches")
    elif args.cmd == "table":
        build_table(store, args.goals, args.assists, args.export, args.rates)
    elif args.cmd == "assemble":
        print(
            assemble(
                store,
                args.pmf,
                args.bundles.split(","),
                [int(x) for x in args.seasons.split(",")],
                args.table,
            )
        )


if __name__ == "__main__":
    main()
