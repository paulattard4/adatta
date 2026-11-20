"""P3-e (f): the live verdict of the scorer forecasts (docs/PHASE3_TASKS.md, P3-e "Live
verdict (f)"). As-of batch reruns over the live season, judged once after its last matchday.

Targets: finished regular-season bundle league matches of the live season whose two lineups
were captured before kickoff (`lineup_snapshot`). Population: the snapshot's XI and bench.
Inputs as of their times: P from the serving refit behind the latest forecast published
before the lineup was first seen; P3-b minutes and P3-d0 rates from their 28-day grids
continued through the live season (CI runs, `--live`); o and alpha as on dev. Holdout seasons
load as history only and are never targets.

CLI (CI; one backtest-type run at a time):
  table    --goals Q1 --assists Q2 --minutes RUN --rates DIGEST
           the live player table, P's distributions and the leakage check, to R2
           (scorers/live/<digest>/...)
  assemble --table DIGEST [--final]
           (e)1-5 on the live targets; metrics only to stdout. The market comparison is
           downstream, in adatta.edge.scorers (principle 1: no odds in model code)
The repo is public: logs carry metrics, never data.
"""

from __future__ import annotations

import argparse
import io
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import FINISHED, SOURCE_ID
from adatta.adapters.storage import RawStore, make_store, try_get
from adatta.config import get_settings
from adatta.db import models
from adatta.eval import rates as er
from adatta.eval import scorers as es
from adatta.eval.selection import LIVE_FROM, WARMUP
from adatta.features import appearances as ap
from adatta.features import rates as fr
from adatta.features import scorers as fs
from adatta.features.referees import RESULT_LAG

PREFIX = "scorers/live"
COHERENCE_TOL = 1e-9
LEAKAGE_N = 100
SEED = 20261120
KEY = fs.KEY


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ----------------------------------------------------------------------------- targets


def live_targets(session: Session, leagues: Sequence[str], season_from: int = LIVE_FROM,
                 ) -> pl.DataFrame:  # fmt: skip
    """One row per side of every target: KEY, side, kickoff, league_ref, season, league_id
    and lineup_seen (when the first of the two lineups was captured, before kickoff)."""
    M, S, C, R, L = (models.Match, models.Season, models.Competition, models.MatchResult,
                     models.LineupSnapshot)  # fmt: skip
    rows = session.execute(
        select(M.match_id, C.source_ref, S.year, M.kickoff_time, M.home_team_id,
               M.away_team_id, M.round)
        .join(S, S.season_id == M.season_id).join(C, C.competition_id == S.competition_id)
        .where(C.source_id == SOURCE_ID, C.source_ref.in_(list(leagues)),
               S.year >= season_from, M.status.in_(FINISHED))
    ).all()  # fmt: skip
    rows = [r for r in rows if (r[6] or "").startswith("Regular Season")]
    ids = [r[0] for r in rows]
    with_result = set(session.scalars(select(R.match_id).where(
        R.match_id.in_(ids), R.superseded_at.is_(None))))  # fmt: skip
    seen: dict[tuple[int, int], datetime] = {
        (m, t): _utc(o) for m, t, o in session.execute(
            select(L.match_id, L.team_id, L.observed_at).where(L.match_id.in_(ids)))
    }  # fmt: skip
    out = []
    for mid, lg, yr, k, h, a, _ in rows:
        k = _utc(k)
        sh, sa = seen.get((mid, h)), seen.get((mid, a))
        if mid not in with_result or sh is None or sa is None or max(sh, sa) >= k:
            continue  # both lineups captured before kickoff, and a result
        for side, team in (("home", h), ("away", a)):
            out.append({"match_id": mid, "team_id": team, "side": side, "kickoff": k,
                        "league_ref": str(lg), "season": int(yr), "league_id": int(lg),
                        "lineup_seen": min(sh, sa)})  # fmt: skip
    return pl.DataFrame(out, schema={
        "match_id": pl.Int64, "team_id": pl.Int64, "side": pl.Utf8,
        "kickoff": pl.Datetime("us", "UTC"), "league_ref": pl.Utf8, "season": pl.Int32,
        "league_id": pl.Int64, "lineup_seen": pl.Datetime("us", "UTC"),
    })  # fmt: skip


def snapshot_squads(session: Session, match_ids: list[int]
                    ) -> tuple[pl.DataFrame, pl.DataFrame, int]:  # fmt: skip
    """(squad rows: KEY, player_id, started, pos; names: match_id, player_id, name; players
    the snapshot names but our player table doesn't know)."""
    L, P = models.LineupSnapshot, models.Player
    snaps = session.execute(select(L.match_id, L.team_id, L.starters, L.substitutes)
                            .where(L.match_id.in_(match_ids))).all()  # fmt: skip
    refs = {str(x["player_ref"]) for s in snaps for xs in (s[2], s[3]) for x in xs
            if x.get("player_ref")}  # fmt: skip
    known: dict[str, tuple[int, str]] = {}
    for part in [sorted(refs)[i : i + 1000] for i in range(0, len(refs), 1000)]:
        for pid, ref, name in session.execute(
            select(P.player_id, P.source_ref, P.name)
            .where(P.source_id == SOURCE_ID, P.source_ref.in_(part))
        ):  # fmt: skip
            known[ref] = (pid, name)
    rows, names, unknown = [], [], 0
    for mid, tid, starters, subs in snaps:
        for started, xs in ((True, starters), (False, subs)):
            for x in xs:
                xref = str(x.get("player_ref")) if x.get("player_ref") else None
                if xref is None or xref not in known:
                    unknown += 1
                    continue
                pid, pname = known[xref]
                rows.append({"match_id": mid, "team_id": tid, "player_id": pid,
                             "started": started, "pos": x.get("pos")})  # fmt: skip
                names.append({"match_id": mid, "player_id": pid, "name": x.get("name")})
                names.append({"match_id": mid, "player_id": pid, "name": pname})
    sq = pl.DataFrame(rows, schema={"match_id": pl.Int64, "team_id": pl.Int64,
                                    "player_id": pl.Int64, "started": pl.Boolean,
                                    "pos": pl.Utf8})  # fmt: skip
    nm = pl.DataFrame(names, schema={"match_id": pl.Int64, "player_id": pl.Int64,
                                     "name": pl.Utf8}).unique()  # fmt: skip
    return sq, nm, unknown


# ------------------------------------------------------------------------- P as served


def live_pmfs(session: Session, store: RawStore, targets: pl.DataFrame, max_goals: int,
              bundle_of: dict[int, str]) -> tuple[pl.DataFrame, dict[str, Any]]:  # fmt: skip
    """Per target side: P's goal distribution from the serving refit behind the latest
    forecast published before the lineup was first seen, recomputed from its stored draws
    and checked against the published 1X2 (to COHERENCE_TOL); an incoherent one is dropped."""
    from adatta.model.team_strength import rates_from_draws
    from adatta.sim import serve

    MP, PR, FS = models.MarketProjection, models.ProjectionRun, models.FixtureSnapshot
    first = targets.group_by("match_id").agg(pl.col("lineup_seen").first(),
                                             pl.col("league_id").first())  # fmt: skip
    ids = first["match_id"].to_list()
    runs: dict[int, list[tuple[datetime, int, str, dict[str, Any]]]] = {}
    for part in [ids[i : i + 500] for i in range(0, len(ids), 500)]:
        for mid, prid, ms, gen, cv in session.execute(
            select(MP.match_id, MP.projection_run_id, MP.milestone, PR.generated_at,
                   PR.component_versions)
            .join(PR, PR.projection_run_id == MP.projection_run_id)
            .where(MP.match_id.in_(part)).distinct()
        ):  # fmt: skip
            runs.setdefault(mid, []).append((_utc(gen), prid, ms, cv or {}))
    refits: dict[int, Any] = {}
    out, stats = [], {"no_forecast": 0, "incoherent": 0, "max_diff": 0.0, "refit_after": 0}
    for mid, seen, lid in first.select("match_id", "lineup_seen", "league_id").iter_rows():
        before = [r for r in runs.get(mid, []) if r[0] < seen]
        if not before:
            stats["no_forecast"] += 1
            continue
        gen, prid, ms, cv = max(before, key=lambda r: (r[0], r[1]))
        v = cv.get(bundle_of[lid], {})
        mrid = v.get("model_run_id")
        mr = session.get(models.ModelRun, mrid) if mrid is not None else None
        if mr is None:
            stats["no_forecast"] += 1
            continue
        if mrid not in refits:
            refits[mrid] = serve.load_refit(store, mr)
        rf = refits[mrid]
        if rf.as_of > gen:
            stats["refit_after"] += 1  # never: a refit from after the forecast
            continue
        m = session.get(models.Match, mid)
        assert m is not None
        k = session.scalars(select(FS.kickoff_time).where(FS.match_id == mid,
                                                          FS.observed_at <= gen)
                            .order_by(FS.observed_at.desc()).limit(1)).first()  # fmt: skip
        kickoff = _utc(k) if k is not None else _utc(m.kickoff_time)
        d, fd = rf.draws, rf.fd
        tix = {int(t): i for i, t in enumerate(fd.team_ids)}
        lg = {x: i for i, x in enumerate(fd.leagues)}[lid]
        hz_ = (kickoff - fd.start).days // 7 - (fd.n_steps - 1)
        lam, mu_ = rates_from_draws(
            d, fd, np.array([tix.get(m.home_team_id, -1)]), np.array([tix.get(m.away_team_id, -1)]),
            np.array([lg]), horizon=np.array([hz_]),
        )  # fmt: skip
        rho = np.asarray(d["rho"])
        rho_k = rho[lg] if rho.ndim == 2 else rho
        kappa = np.asarray(d["kappa"])[lg] if "kappa" in d else np.zeros_like(lam[0])
        mats = serve.score_matrices(lam[0], mu_[0], rho_k, kappa, max_goals)
        mk = serve.market_draws(mats)
        pub = {s: p for s, p in session.execute(
            select(MP.selection, MP.probability).where(
                MP.match_id == mid, MP.projection_run_id == prid, MP.milestone == ms,
                MP.market_type == "1X2"))}  # fmt: skip
        diff = max(abs(float(mk[("1X2", None, s)].mean()) - p) for s, p in pub.items())
        stats["max_diff"] = max(stats["max_diff"], diff)
        if diff > COHERENCE_TOL:
            stats["incoherent"] += 1
            continue
        mean = mats.mean(axis=0)
        for team, pmf in ((m.home_team_id, mean.sum(axis=1)), (m.away_team_id, mean.sum(axis=0))):
            out.append({"match_id": mid, "team_id": team, "pmf": pmf.tolist(), "milestone": ms,
                        "generated_at": gen, "refit_as_of": rf.as_of})  # fmt: skip
    return pl.DataFrame(out), stats


# ------------------------------------------------------------------------------- table


def collect_minutes(store: RawStore, run: str, bundles: Sequence[str], season: int) -> pl.DataFrame:
    """P3-b's model rows of the live run (`model.selection run --live`), every bundle."""
    parts = []
    for b in bundles:
        data = try_get(store, f"selection/parts/{run}/{b}_{season}_0of1.parquet")
        if data is None:
            raise SystemExit(f"missing P3-b live part {b} {season} of run {run}")
        parts.append(pl.read_parquet(io.BytesIO(data)))
    return pl.concat(parts, how="diagonal")


def live_leakage(app: pl.DataFrame, hist: pl.DataFrame, export: pl.DataFrame,
                 posts: dict[str, Any], table: pl.DataFrame, targets: pl.DataFrame,
                 squad: pl.DataFrame, leagues: tuple[str, ...], n: int = LEAKAGE_N,
                 ) -> dict[str, Any]:  # fmt: skip
    """As the dev check (model.scorers.leakage_check), with the live squad: rebuild a sampled
    row's team-match from data final by the as-of time; no input column may change."""
    from adatta.model.scorers import LEAK_COLS

    rows = table.filter(pl.col("in_squad"))
    sample = rows.sample(min(n, rows.height), seed=SEED)
    counts = ["minutes", "goals", "own_goals", "assists", "pen_scored", "minutes_provider"]
    changed = 0
    for r in sample.iter_rows(named=True):
        cut = r["kickoff"] - fr.HORIZON - RESULT_LAG
        late = (pl.col("kickoff") > cut) & (pl.col("kickoff") <= r["kickoff"])
        a = pl.concat([
            app.filter(pl.col("kickoff") <= cut),
            app.filter(late).with_columns([pl.lit(None, pl.Int32).alias(c) for c in counts]),
        ], how="vertical_relaxed")  # fmt: skip
        h = hist.filter(pl.col("kickoff") <= cut)
        ex = export.join(app.filter(pl.col("kickoff") <= r["kickoff"]).select(KEY).unique(),
                         on=KEY)  # fmt: skip
        key = pl.DataFrame({"match_id": [r["match_id"]], "team_id": [r["team_id"]]})
        tg = targets.join(key, on=KEY).select(*KEY, "kickoff", "league_ref", "season")
        re_ = fs.build(a, h, posts, leagues, ex, tg, None, squad.join(key, on=KEY))
        re_ = re_.filter(pl.col("player_id") == r["player_id"])
        if re_.is_empty():
            changed += 1
            continue
        x = re_.row(0, named=True)
        same = all(abs((x[c] if x[c] is not None else -1.0) - (r[c] if r[c] is not None else -1.0))
                   < 1e-9 for c in LEAK_COLS)  # fmt: skip
        changed += 0 if same else 1
    return {"leakage_sampled": sample.height, "leakage_changed": changed,
            "leakage_ok": changed == 0}  # fmt: skip


def build_table(store: RawStore, goals_run: str, assists_run: str, minutes_run: str,
                rates_ref: str) -> str:  # fmt: skip
    from adatta.db.session import get_session
    from adatta.eval import harness
    from adatta.model import lineup as ml
    from adatta.model import rates as mr
    from adatta.model.scorers import birth_dates

    cfg = harness.load_config()
    leagues = tuple(er.leagues())
    bundle_of = {lid: b for b, ls in cfg.bundles.items() for lid in ls}
    hist, _, rates_digest = fr.load(store, rates_ref)
    with get_session() as session:
        targets = live_targets(session, leagues, cfg.live_from_season)
        if targets.is_empty():
            raise SystemExit("no live targets with captured lineups yet")
        last = int(targets["season"].max())  # type: ignore[arg-type]
        squad, names, unknown = snapshot_squads(session, targets["match_id"].unique().to_list())
        pmfs, pstats = live_pmfs(session, store, targets, cfg.max_goals, bundle_of)
    app = ap.load(store, [(lg, y) for lg in leagues for y in range(WARMUP[0], last + 1)])
    export = collect_minutes(store, minutes_run, list(cfg.bundles), last)
    times = ml.quality_times(mr._frame(hist, er.Design(leagues)), live=True)
    posts = {"goals": ml.load_posts(store, goals_run, times),
             "assists": ml.load_posts(store, assists_run, times)}  # fmt: skip
    t0 = time.monotonic()
    tg = targets.select(*KEY, "kickoff", "league_ref", "season")
    birth = birth_dates(squad["player_id"].unique().to_list())
    table = fs.build(app, hist, posts, leagues, export, tg, birth, squad)
    print(f"live player table: {table.height} rows, {tg.height} team-matches, "
          f"{targets['match_id'].n_unique()} matches in {time.monotonic() - t0:.0f} s; "
          f"snapshot players unknown to the player table {unknown}", flush=True)  # fmt: skip
    leak = live_leakage(app, hist, export, posts, table, targets, squad, leagues)
    print(f"leakage: {leak['leakage_changed']} of {leak['leakage_sampled']} changed", flush=True)
    print(f"P as served: {pmfs['match_id'].n_unique() if pmfs.height else 0} matches; "
          f"no forecast before the lineup {pstats['no_forecast']}, incoherent "
          f"{pstats['incoherent']} (max diff {pstats['max_diff']:.1e}), refit after the "
          f"forecast {pstats['refit_after']}", flush=True)  # fmt: skip
    meta = {"rates": rates_digest, "quality_goals": goals_run, "quality_assists": assists_run,
            "minutes": minutes_run, "live_from": cfg.live_from_season, "last_season": last,
            "snapshot_unknown": unknown, "pmf": pstats, **leak,
            "built_at": datetime.now(UTC).isoformat()}  # fmt: skip
    h = fs.save(store, table, meta, latest=False)
    store.put(f"{PREFIX}/{h}/pmfs.parquet", _parquet(pmfs))
    print(f"stored live player table {h}")
    return h


# ---------------------------------------------------------------------------- assemble


def assemble(store: RawStore, table_ref: str, final: bool) -> str:
    table, meta, digest = fs.load(store, table_ref)
    pmfs = pl.read_parquet(io.BytesIO(store.get(f"{PREFIX}/{digest}/pmfs.parquet")))
    pm = pmfs.select(*KEY, "pmf")
    scored = pl.concat([es.score(table, pm, h) for h in es.HORIZONS])
    gaps = es.consistency(table, pm)
    v = es.verdict(scored, gaps, bool(meta.get("leakage_ok")))
    label = "FINAL live verdict" if final else "INTERIM (reported only; judged once at season end)"
    md = es.markdown(scored, v, {**meta, "table": digest})
    md = md.replace("# P3-e scorers — dev", f"# P3-e scorers — live, {label}", 1)
    scored_ids = scored["match_id"].unique()
    hit = table.filter(pl.col("match_id").is_in(scored_ids.implode()))
    k = hit["kickoff"].dt.strftime("%Y-%m-%d")
    md += (
        f"\n\nMatches scored: {scored_ids.len()} (with P as served) of "
        f"{table['match_id'].n_unique()} with lineups captured before kickoff; kickoffs "
        f"{k.min()!s} to {k.max()!s}."
    )
    store.put(f"{PREFIX}/reports/{digest}/scored.parquet", _parquet(scored))
    store.put(f"{PREFIX}/reports/{digest}/verdict.json",
              json.dumps({"final": final, "passed": v.passed, "checks": v.checks},
                         default=str).encode())  # fmt: skip
    return md


# --------------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m adatta.model.scorers_live")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("table")
    t.add_argument("--goals", required=True, help="lineup quality run (--live, goals)")
    t.add_argument("--assists", required=True, help="lineup quality run (--live, assists)")
    t.add_argument("--minutes", required=True, help="model.selection run (--live)")
    t.add_argument("--rates", required=True, help="counts table digest (build-table --live)")
    a = sub.add_parser("assemble")
    a.add_argument("--table", required=True)
    a.add_argument("--final", action="store_true")
    args = p.parse_args(argv)
    store = make_store(get_settings())
    if args.cmd == "table":
        build_table(store, args.goals, args.assists, args.minutes, args.rates)
    else:
        print(assemble(store, args.table, args.final))


if __name__ == "__main__":
    main()
