from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import polars as pl
import typer
from sqlalchemy import func
from sqlalchemy.orm import Session

from adatta.adapters import backfill
from adatta.adapters.api_football import ApiFootballClient
from adatta.adapters.open_meteo import OpenMeteoClient
from adatta.adapters.quota import QuotaPlanner
from adatta.adapters.storage import make_store
from adatta.audit import capability as cap_audit
from adatta.capture import alert
from adatta.capture.jobs import run_capture
from adatta.config import get_settings, load_competitions
from adatta.db.session import get_session

app = typer.Typer(help="adatta command line")
COMPETITIONS_OPTION = typer.Option(
    "config/competitions.yml",
    help="competition file; config/calendar_competitions.yml loads fixture calendars only",
)


def _client() -> ApiFootballClient:
    s = get_settings()
    quota = QuotaPlanner(s.api_football_daily_limit, s.api_football_per_minute_limit)
    return ApiFootballClient(s, make_store(s), quota)


@app.command()
def status() -> None:
    """Check API-Football connectivity and plan (uses 1 request)."""
    with get_session() as session:
        typer.echo(_client().status(session))
        session.commit()


@app.command("backfill-enqueue")
def backfill_enqueue(config: str = COMPETITIONS_OPTION) -> None:
    """Queue all (competition, season) jobs from config/competitions.yml."""
    with get_session() as session:
        n = backfill.enqueue(session, load_competitions(config))
        session.commit()
    typer.echo(f"queued {n} new jobs")


@app.command("backfill-run")
def backfill_run(
    limit: int = typer.Option(None, help="max jobs this run"), config: str = COMPETITIONS_OPTION
) -> None:
    """Queue any new (competition, season) jobs, then run unfinished ones within the backfill
    quota budget. Safe to stop and re-run."""
    comps = load_competitions(config)
    client = _client()
    with get_session() as session:
        queued = backfill.enqueue(session, comps)
        session.commit()
        result = backfill.run(session, client, comps, limit)
    typer.echo(
        f"queued {queued} new jobs; {result}; account quota used today ~{client.quota_used_today}"
    )


@app.command("backfill-report")
def backfill_report(
    out: str = typer.Option("reports/backfill", help="report folder"),
    config: str = COMPETITIONS_OPTION,
) -> None:
    """Completeness report per competition x season (no API requests)."""
    with get_session() as session:
        rows = backfill.completeness(session, load_competitions(config))
    df = pl.DataFrame(rows, infer_schema_length=None)
    folder = Path(out)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.csv"
    df.write_csv(path)
    done = df.filter(pl.col("job_status") == "done").height
    typer.echo(
        f"{done}/{df.height} seasons done; {df['finished'].sum()} finished matches; "
        f"{df['detail_returned'].sum()} with detail; report {path}"
    )
    flagged = df.filter(pl.col("flags") != "")
    with pl.Config(tbl_rows=-1, fmt_str_lengths=80, tbl_width_chars=160):
        typer.echo(
            flagged.select("competition", "season", "flags") if flagged.height else "no flags"
        )


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True
        )
        return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except OSError:
        return "unknown"


@app.command()
def audit(
    sample: int = typer.Option(cap_audit.DEFAULT_SAMPLE, help="finished fixtures per season"),
    seed: int = typer.Option(cap_audit.DEFAULT_SEED, help="sampling seed (logged)"),
    league: int | None = typer.Option(None, help="only this API-Football league id"),
    season: int | None = typer.Option(None, help="only this season start year"),
    out: str = typer.Option("reports/capability_audit", help="report folder"),
) -> None:
    """Audit API-Football coverage per competition x season; fill the capability table."""
    comps = load_competitions()
    if league is not None:
        comps = [c for c in comps if c.league_id == league]
        if not comps:
            raise typer.BadParameter(f"league {league} is not in config/competitions.yml")
    client = _client()
    started = datetime.now(UTC)
    with get_session() as session:
        audits, errors = cap_audit.run_audit(
            session, client, comps, sample=sample, seed=seed, season=season
        )
    rows = cap_audit.report_rows(audits)
    out_dir = Path(out)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    if rows:
        df = pl.DataFrame(rows)
        df.write_csv(out_dir / f"{stamp}.csv")
        grid = df.pivot(on="capability", index=["competition", "season"], values="status")
        with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=250):
            typer.echo(grid)
        mism = df.filter(pl.col("mismatch"))
        if mism.height:
            typer.echo(f"\n{mism.height} declared-vs-observed mismatches:")
            with pl.Config(tbl_rows=-1):
                typer.echo(mism.select("competition", "season", "capability", "declared", "status"))
    manifest = {
        "run": "capability_audit",
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "code_sha": _git_sha(),
        "seed": seed,
        "sample_per_season": sample,
        "available_min": cap_audit.AVAILABLE_MIN,
        "requests_made": client.requests_made,
        "account_requests_used_today": client.quota_used_today,
        "seasons_audited": [[a.league_id, a.season, a.sampled_ids] for a in audits],
        "errors": errors,
        "versions": {p: version(p) for p in ("adatta", "polars", "sqlalchemy", "httpx")},
    }
    (out_dir / f"{stamp}.manifest.json").write_text(json.dumps(manifest, indent=2))
    for e in errors:
        typer.echo(f"ERROR {e}", err=True)
    typer.echo(
        f"{len(audits)} seasons audited, {client.requests_made} requests; report in {out_dir}"
    )


@app.command()
def capture() -> None:
    """Snapshot capture tick: run whatever is due (designed for a ~15-minute schedule).

    Exits non-zero when a milestone was missed or a job errored, so a scheduled CI run fails
    visibly; the alert webhook (ALERT_WEBHOOK_URL) is notified as well.
    """
    settings = get_settings()
    client = _client()
    with get_session() as session:
        weather = OpenMeteoClient(make_store(settings))
        report = run_capture(session, client, load_competitions(), weather=weather)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.actions.items())) or "nothing due"
    typer.echo(
        f"capture {report.started_at:%Y-%m-%dT%H:%MZ}: {summary}; {report.requests} requests"
    )
    problems = report.missed + report.errors
    if problems:
        alert.send(settings, f"adatta capture: {len(problems)} problem(s)\n" + "\n".join(problems))
        raise typer.Exit(code=1)


@app.command("venues-load")
def venues_load(
    season: int | None = typer.Option(None, help="only this season start year"),
) -> None:
    """Link matches to venues from stored fixture lists of every configured competition
    (both competition files; no API requests). Idempotent."""
    from adatta.adapters.detail_store import load_venues

    files = ("config/competitions.yml", "config/calendar_competitions.yml")
    countries = {c.league_id: c.country for path in files for c in load_competitions(path)}
    with get_session() as session:
        counts = load_venues(
            session, make_store(get_settings()), countries, None if season is None else {season}
        )
    typer.echo(counts)


@app.command("venues-review")
def venues_review() -> None:
    """Venues whose geocoding needs a manual decision (no requests). Add the reviewed
    coordinates to config/venue_overrides.yml; the next capture tick applies them."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.features.travel import relevant_venues

    V = models.Venue
    with get_session() as session:
        rows = session.scalars(
            select(V).where(V.geocode_status.in_(["ambiguous", "unresolved"])).order_by(V.city)
        ).all()
        counts = session.execute(
            select(V.geocode_status, func.count()).group_by(V.geocode_status)
        ).all()
        used = relevant_venues(session, *_bundle_competition_ids(session))
    typer.echo(", ".join(f"{s}={n}" for s, n in counts) or "no venues")
    typer.echo("matches = bundle fixtures at the venue; most used first")
    for v in sorted(rows, key=lambda v: -used.get(v.venue_id, 0)):
        typer.echo(
            f"{used.get(v.venue_id, 0):5} {v.geocode_status:10} {v.name} | {v.city} | "
            f"{v.country}: {v.geocode_match}"
        )


def _bundle_competition_ids(session: Session) -> tuple[list[int], list[int]]:
    """Internal competition ids of the bundle leagues and cups (config/competitions.yml)."""
    from sqlalchemy import select

    from adatta.adapters.normalize import SOURCE_ID
    from adatta.db import models

    comps = load_competitions("config/competitions.yml")
    C = models.Competition
    ids = dict(
        session.execute(
            select(C.source_ref, C.competition_id).where(
                C.source_id == SOURCE_ID, C.source_ref.in_([str(c.league_id) for c in comps])
            )
        ).all()
    )
    split: tuple[list[int], list[int]] = ([], [])
    for c in comps:
        if str(c.league_id) in ids:
            split[c.tier == "cup"].append(ids[str(c.league_id)])
    return split


@app.command("venues-geocode")
def venues_geocode(
    limit: int = typer.Option(2000, help="most venues to geocode this run"),
) -> None:
    """Geocode the venues of bundle fixtures (league, and cup ties involving a league team),
    most used first: free Open-Meteo city searches, no API-Football quota. Ambiguous and
    unresolved results go to `venues-review`."""
    from adatta.capture.weather import apply_overrides, geocode_venues, load_overrides
    from adatta.features.travel import relevant_venues

    with get_session() as session:
        reviewed = apply_overrides(session, load_overrides(), datetime.now(UTC))
        session.commit()
        typer.echo(f"overrides applied: {reviewed}")
        leagues, cups = _bundle_competition_ids(session)
        venues = relevant_venues(session, leagues, cups)
        om = OpenMeteoClient(make_store(get_settings()))
        counts = geocode_venues(
            session, om, list(venues), datetime.now(UTC), limit=limit, backlog=True
        )
    typer.echo(f"{dict(counts)}; requests={om.requests_made}")


@app.command("venues-home")
def venues_home() -> None:
    """Rebuild the versioned home base of every team from its home league fixtures (no
    requests). Unchanged teams are left alone; changed ones are superseded, not deleted."""
    from adatta.features.travel import build_home_venues

    with get_session() as session:
        leagues, _ = _bundle_competition_ids(session)
        typer.echo(dict(build_home_venues(session, leagues)))


@app.command("venues-coverage")
def venues_coverage() -> None:
    """Share of bundle league fixtures with travel and altitude terms, by country, tier and
    season (no requests)."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.features.travel import travel_features

    M, S, C = models.Match, models.Season, models.Competition
    with get_session() as session:
        leagues, _ = _bundle_competition_ids(session)
        rows = session.execute(
            select(M, C.country, C.tier, S.year)
            .join(S, S.season_id == M.season_id)
            .join(C, C.competition_id == S.competition_id)
            .where(S.competition_id.in_(leagues))
        ).all()
        feats = travel_features(session, [r[0] for r in rows])
    keys = pl.DataFrame(
        {"match_id": [r[0].match_id for r in rows], "country": [r[1] for r in rows],
         "tier": [r[2] for r in rows], "season": [r[3] for r in rows]}
    )  # fmt: skip
    out = (
        keys.join(feats, on="match_id")
        .group_by("country", "tier", "season")
        .agg(
            pl.len().alias("matches"),
            (pl.col("venue_source") == "home_base").sum().alias("venue_from_base"),
            pl.col("away_travel_km").is_not_null().mean().round(3).alias("travel"),
            pl.col("away_tz_shift_h").is_not_null().mean().round(3).alias("tz"),
            pl.col("away_altitude_diff_m").is_not_null().mean().round(3).alias("altitude"),
        )
        .sort("country", "tier", "season")
    )
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=120):
        typer.echo(out)


@app.command("rules-check")
def rules_check() -> None:
    """Check config/league_rules.yml against the newest stored /standings table of each
    league-season (final, or current for the season in play; no requests): team counts,
    every points gap, and each table rebuilt from our regular-season results with
    deductions and awarded results."""
    import gzip

    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from adatta.adapters.normalize import FINISHED
    from adatta.db import models
    from adatta.features.league_rules import (
        check_against_standings,
        check_results_against_standings,
        load_rules,
    )

    rules = load_rules()
    R = models.RawPayload
    store = make_store(get_settings())
    newest: dict[tuple[int, int], str] = {}
    with get_session() as session:
        for params, key in session.execute(
            select(R.params, R.storage_key).where(R.endpoint == "/standings").order_by(R.fetched_at)
        ):
            newest[(int(params["league"]), int(params["season"]))] = key
    tables: dict[tuple[int, int], list[dict[str, object]]] = {}
    for k, key in newest.items():
        response = json.loads(gzip.decompress(store.get(key)))["response"]
        if response:
            tables[k] = response[0]["league"]["standings"][0]
        else:
            typer.echo(f"{k[0]} {k[1]}: provider has no final table")
    results: dict[tuple[int, int], list[tuple[str, str, int, int]]] = {}
    M, S, C, T, MR = (
        models.Match, models.Season, models.Competition, models.Team, models.MatchResult
    )  # fmt: skip
    H, A = aliased(T), aliased(T)
    with get_session() as session:
        for lg, yr, h, a, hg, ag in session.execute(
            select(C.source_ref, S.year, H.source_ref, A.source_ref, MR.home_goals_nt,
                   MR.away_goals_nt)
            .join(S, S.competition_id == C.competition_id)
            .join(M, M.season_id == S.season_id)
            .join(MR, MR.match_id == M.match_id)
            .join(H, H.team_id == M.home_team_id)
            .join(A, A.team_id == M.away_team_id)
            .where(MR.superseded_at.is_(None), M.status.in_(FINISHED),
                   M.round.like("Regular Season%"), MR.home_goals_nt.is_not(None),
                   MR.away_goals_nt.is_not(None))
        ):  # fmt: skip
            if hg is not None and ag is not None:
                results.setdefault((int(lg), yr), []).append((h, a, hg, ag))
    problems = check_against_standings(rules, tables)
    problems += check_results_against_standings(rules, tables, results)
    typer.echo(f"{len(tables)} tables checked, {len(problems)} problems")
    for p in problems:
        typer.echo(f"  {p}")
    if problems:
        raise typer.Exit(1)


@app.command("standings-refresh")
def standings_refresh(
    season: int | None = typer.Option(None, help="season start year; default: the newest"),
) -> None:
    """Fetch the current /standings of every bundle league (one request each, through the
    quota planner) and run rules-check, so a deduction or awarded result imposed mid-season
    shows up as a points gap. Exits 1 when rules-check finds a problem. Run monthly."""
    from sqlalchemy import func, select

    from adatta.db import models

    leagues = [c for c in load_competitions("config/competitions.yml") if c.tier != "cup"]
    client = _client()
    with get_session() as session:
        if season is None:
            season = session.scalar(select(func.max(models.Season.year)))
        for c in leagues:
            payload = client.fetch(session, "/standings", {"league": c.league_id, "season": season})
            session.commit()
            rows = payload.get("response") or []
            n = len(rows[0]["league"]["standings"][0]) if rows else 0
            typer.echo(f"{c.name} {season}: {n} teams")
    rules_check()


def _modelled_venue_ids(session: Session) -> set[int]:
    """Venues of bundle league matches from 2019 on, plus every team's home bases."""
    from sqlalchemy import select

    from adatta.db import models

    M, S, H = models.Match, models.Season, models.TeamHomeVenue
    leagues, _ = _bundle_competition_ids(session)
    ids = set(
        session.scalars(
            select(M.venue_id)
            .join(S, S.season_id == M.season_id)
            .where(S.competition_id.in_(leagues), S.year >= 2019, M.venue_id.is_not(None))
        )
    )
    ids |= set(session.scalars(select(H.venue_id).where(H.superseded_at.is_(None))))
    return {i for i in ids if i is not None}


@app.command("climatology-fetch")
def climatology_fetch(
    budget: int = typer.Option(6000, help="most Open-Meteo calls to use this run"),
) -> None:
    """Fetch the 2016-2018 ERA5 baseline for grid cells of modelled venues not yet stored.
    Resumable: run it on later days until nothing is left. Free tier: 10,000 calls a day."""
    from adatta.features.climatology import fetch_baseline, needed_cells

    with get_session() as session:
        cells = needed_cells(session, _modelled_venue_ids(session))
        om = OpenMeteoClient(make_store(get_settings()))
        counts = fetch_baseline(session, om, cells, budget)
    typer.echo(f"{len(cells)} cells needed; {dict(counts)}")


def _bundle_league_matches(session: Session) -> list[Any]:
    """Bundle league matches from 2019 that were played or are to be played."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.features.context import NOT_PLAYED

    M, S = models.Match, models.Season
    leagues, _ = _bundle_competition_ids(session)
    return list(
        session.scalars(
            select(M)
            .join(S, S.season_id == M.season_id)
            .where(S.competition_id.in_(leagues), S.year >= 2019, M.status.not_in(NOT_PLAYED))
        )
    )


@app.command("weather-history")
def weather_history_fetch(
    budget: int = typer.Option(8000, help="most Open-Meteo calls to use this run"),
    dry_run: bool = typer.Option(False, help="only count the requests still needed"),
) -> None:
    """Past-match weather for backtests (D29; adatta/features/weather_history.py): as-issued
    previous-runs forecasts from 2024-02-01 and ERA5 realised weather 2019-07 to 2024-05.
    Resumable: run it on later days until nothing is left. Free tier: 10,000 calls a day."""
    from collections import Counter

    from adatta.features import weather_history as wh

    with get_session() as session:
        targets = wh.load_targets(session, _bundle_league_matches(session))
        jobs = wh.plan(session, targets, datetime.now(UTC))
        typer.echo(
            f"{len(targets)} placed matches; jobs needed: {dict(Counter(j.kind for j in jobs))}"
        )
        if dry_run:
            return
        om = OpenMeteoClient(make_store(get_settings()))
        counts = wh.fetch(session, om, jobs, budget)
    typer.echo(dict(counts))


@app.command("weather-history-report")
def weather_history_report() -> None:
    """The D29 stand-in's error: ERA5 realised minus the day-1 as-issued forecast, Feb-May
    2024 (no requests)."""
    from adatta.features import weather_history as wh

    with get_session() as session:
        targets = wh.load_targets(session, _bundle_league_matches(session))
        typer.echo(wh.proxy_error(session, make_store(get_settings()), targets))


@app.command("importance-report")
def importance_report() -> None:
    """Importance labels (dead rubber, must-win, live) for every bundle league match from
    2019, by league and season stage (no requests)."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.features.importance import importance_features
    from adatta.features.league_rules import load_rules

    M, S, C = models.Match, models.Season, models.Competition
    with get_session() as session:
        leagues, _ = _bundle_competition_ids(session)
        rows = session.execute(
            select(M, C.name, S.year)
            .join(S, S.season_id == M.season_id)
            .join(C, C.competition_id == S.competition_id)
            .where(S.competition_id.in_(leagues), S.year >= 2019)
        ).all()
        feats = importance_features(session, [r[0] for r in rows], load_rules())
    keys = pl.DataFrame(
        {"match_id": [r[0].match_id for r in rows], "league": [r[1] for r in rows],
         "season": [r[2] for r in rows], "round": [r[0].round or "" for r in rows]}
    )  # fmt: skip
    df = (
        keys.join(feats, on="match_id")
        .filter(pl.col("home_importance").is_not_null())
        .unpivot(
            index=["league", "season", "round"],
            on=["home_importance", "away_importance"],
            value_name="importance",
        )
    )
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=120):
        typer.echo(
            df.group_by("league")
            .agg(
                pl.len().alias("team_matches"),
                (pl.col("importance") == "dead_rubber").mean().round(3).alias("dead_rubber"),
                (pl.col("importance") == "must_win").mean().round(3).alias("must_win"),
            )
            .sort("league")
        )


@app.command("context-build")
def context_build(
    horizon_hours: float = typer.Option(1.0, help="as_of = kickoff - this many hours"),
    out: str = typer.Option("data/features/context.parquet", help="parquet output"),
) -> None:
    """Build the match context table (adatta/features/context.py) for bundle league matches
    from 2019 (not cancelled, postponed or walked over) and report coverage by league and
    season (no requests)."""
    from datetime import timedelta
    from pathlib import Path

    from sqlalchemy import select

    from adatta.db import models
    from adatta.features.context import NOT_PLAYED, context_features
    from adatta.features.league_rules import load_rules

    M, S, C = models.Match, models.Season, models.Competition
    with get_session() as session:
        leagues, _ = _bundle_competition_ids(session)
        rows = session.execute(
            select(M, C.name, S.year)
            .join(S, S.season_id == M.season_id)
            .join(C, C.competition_id == S.competition_id)
            .where(S.competition_id.in_(leagues), S.year >= 2019, M.status.not_in(NOT_PLAYED))
        ).all()
        ctx = context_features(
            session, make_store(get_settings()), [r[0] for r in rows], load_rules(),
            timedelta(hours=horizon_hours),
        )  # fmt: skip
    keys = pl.DataFrame(
        {"match_id": [r[0].match_id for r in rows], "league": [r[1] for r in rows],
         "season": [r[2] for r in rows],
         "playoff": [not (r[0].round or "").startswith("Regular Season") for r in rows]}
    )  # fmt: skip
    df = keys.join(ctx, on="match_id")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)

    def share(c: str) -> pl.Expr:
        return pl.col(c).is_not_null().mean().round(3)

    cov = (
        df.group_by("league", "season")
        .agg(
            pl.len().alias("n"), share("home_travel_km").alias("travel"),
            share("home_days_since_last").alias("since"),
            share("home_days_to_next").alias("to_next"),
            share("temperature_c").alias("weather"),
            (pl.col("weather_source") == "forecast").mean().round(3).alias("forecast"),
            share("kickoff_hour_local").alias("hour"), share("referee").alias("referee"),
            pl.col("home_importance").filter(~pl.col("playoff")).is_not_null().mean().round(3)
            .alias("importance"),
            share("has_var").alias("var"),
        )
        .sort("league", "season")
    )  # fmt: skip
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=160):
        typer.echo(cov)
    typer.echo(f"{df.height} matches written to {out}")


@app.command("referees-report")
def referees_report() -> None:
    """Referee identities (adatta/features/referees.py): distinct raw names and identities by
    league and season, the names left unmerged for review, and the busiest identities' card
    and foul rates (no requests; reads stored fixture lists)."""
    from collections import defaultdict
    from datetime import UTC, datetime

    from sqlalchemy import select

    from adatta.db import models
    from adatta.features.referees import CONTINENTAL, RefereeHistory, fixture_referees

    S, C, M = models.Season, models.Competition, models.Match
    store = make_store(get_settings())
    with get_session() as session:
        history = RefereeHistory.load(session, store)
        leagues, _ = _bundle_competition_ids(session)
        seasons = {
            sid: (name, country, year, league)
            for sid, name, country, year, league in session.execute(
                select(S.season_id, C.name, C.country, S.year, C.source_ref)
                .join(C, C.competition_id == S.competition_id)
                .where(S.competition_id.in_(leagues), S.year >= 2019)
            )
            if country not in CONTINENTAL
        }
        names = fixture_referees(session, store, seasons)
        per: dict[tuple[str, int], tuple[set[str], set[str]]] = defaultdict(lambda: (set(), set()))
        for ref, sid in session.execute(
            select(M.source_ref, M.season_id).where(M.season_id.in_(list(seasons)))
        ):
            if ref in names:
                name, country, year, league = seasons[sid]
                raw, ids = per[(name, year)]
                raw.add(names[ref])
                ids.add(history.identify(country, names[ref], league))
    years = sorted({y for _, y in per})
    typer.echo("raw names -> identities, by season: " + " ".join(str(y) for y in years))
    for league in sorted({n for n, _ in per}):
        cells = []
        for y in years:
            raw, ids = per.get((league, y), (set(), set()))
            cells.append(f"{len(raw)}->{len(ids)}")
        typer.echo(f"  {league:17} " + " ".join(cells))
    for country, review in sorted(history.review.items()):
        if review:
            typer.echo(f"unmerged for review, {country}: {', '.join(review)}")
    now = datetime.now(UTC)
    busiest = sorted(
        ((rid, *history.referee(rid, now)) for rid in history.identities()),
        key=lambda r: -r[1],
    )[:10]
    typer.echo("busiest referees (matches, cards/match, fouls/match):")
    for rid, n, cards, fouls in busiest:
        typer.echo(f"  {rid:40} {n:4} {cards} {fouls}")


@app.command("climatology-coverage")
def climatology_coverage() -> None:
    """Share of bundle league matches from 2019 with a climatology value, and the mean
    values, by country (no requests; reads the stored baseline)."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.features.climatology import load_cells, match_climatology, needed_cells
    from adatta.features.travel import travel_features

    M, S, C = models.Match, models.Season, models.Competition
    with get_session() as session:
        leagues, _ = _bundle_competition_ids(session)
        rows = session.execute(
            select(M, C.country)
            .join(S, S.season_id == M.season_id)
            .join(C, C.competition_id == S.competition_id)
            .where(S.competition_id.in_(leagues), S.year >= 2019)
        ).all()
        matches = [r[0] for r in rows]
        venue_of = dict(
            travel_features(session, matches).select("match_id", "venue_id").iter_rows()
        )
        points = needed_cells(session, {v for v in venue_of.values() if v is not None})
        climates = load_cells(session, make_store(get_settings()), points)
        clim = match_climatology(session, matches, venue_of, climates)
    typer.echo(f"{len(climates)} of {len(points)} venue points fetched")
    keys = pl.DataFrame(
        {"match_id": [m.match_id for m in matches], "country": [r[1] for r in rows]}
    )
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=120):
        typer.echo(
            keys.join(clim, on="match_id")
            .group_by("country")
            .agg(
                pl.len().alias("matches"),
                pl.col("clim_temperature_c").is_not_null().mean().round(3).alias("covered"),
                pl.col("clim_temperature_c").mean().round(1).alias("temp_c"),
                pl.col("clim_precipitation_mm").mean().round(2).alias("rain_mm"),
                pl.col("clim_wind_speed_kmh").mean().round(1).alias("wind_kmh"),
            )
            .sort("country")
        )


if __name__ == "__main__":
    app()


@app.command()
def baseline(
    out: str = typer.Option("reports/baseline", help="report folder"),
) -> None:
    """Phase 1a: tune the half-life on the dev window, score Dixon-Coles and the baselines,
    write the report and manifest. Never opens the locked holdout. No API requests."""
    from adatta.eval import baseline as phase1a
    from adatta.eval.harness import load_config

    with get_session() as session:
        path = phase1a.run(session, load_config(), Path(out))
    typer.echo(f"report written to {path}")


@app.command("register-run")
def register_run(report: str = typer.Argument(..., help="baseline report folder")) -> None:
    """Record an existing baseline report in model_run (and sync bundles). No API requests."""
    from adatta.eval import baseline as phase1a
    from adatta.eval.harness import load_config

    folder = Path(report)
    manifest = json.loads((folder / "manifest.json").read_text())
    # Reports written before these fields existed: hash the files as of the run's commit.
    commit = str(manifest["code_sha"]).removesuffix("-dirty")
    for key, path in (("config_hash", "config/evaluation.yml"), ("lockfile_hash", "uv.lock")):
        if key not in manifest:
            blob = subprocess.run(["git", "show", f"{commit}:{path}"], capture_output=True)
            manifest[key] = (
                hashlib.sha256(blob.stdout).hexdigest() if blob.returncode == 0 else "unknown"
            )
    summary = pl.read_csv(folder / "metrics.csv")
    with get_session() as session:
        phase1a.sync_bundles(session, load_config())
        row = phase1a.record_run(session, manifest, str(folder), phase1a.headline_scores(summary))
        session.commit()
        typer.echo(f"model_run {row.model_run_id} recorded for {folder}")


@app.command("coach-load")
def coach_load(workers: int = typer.Option(8, help="parallel raw-store reads")) -> None:
    """Build match_coach from stored detail payloads (no API requests, D12). Resumable."""
    from adatta.adapters.detail_store import load_match_coaches

    with get_session() as session:
        counts = load_match_coaches(session, make_store(get_settings()), workers)
    typer.echo(counts)


@app.command("stats-load")
def stats_load(workers: int = typer.Option(8, help="parallel raw-store reads")) -> None:
    """Build team_match_stat from stored detail payloads (no API requests). Resumable."""
    from adatta.adapters.detail_store import load_team_stats

    with get_session() as session:
        counts = load_team_stats(session, make_store(get_settings()), workers)
    typer.echo(counts)
