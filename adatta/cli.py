from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import httpx
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
    config: str = typer.Option("config/competitions.yml", help="competition list to audit"),
) -> None:
    """Audit API-Football coverage per competition x season; fill the capability table."""
    comps = load_competitions(config)
    if league is not None:
        comps = [c for c in comps if c.league_id == league]
        if not comps:
            raise typer.BadParameter(f"league {league} is not in {config}")
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


def _projector(session: Session, settings: Any) -> Any:
    """Milestone projections from the champion's latest passing refit (adatta.sim.serve)."""
    from adatta.eval import harness
    from adatta.eval.baseline import _file_hash, _git_sha
    from adatta.model import horizon as hz
    from adatta.model.backtest import production_spec
    from adatta.sim import serve

    cfg = harness.load_config()
    store = make_store(settings)
    manifest = {"git_commit": _git_sha(), "lockfile_hash": _file_hash("uv.lock"),
                "seed": production_spec(cfg).seed}  # fmt: skip

    spec = production_spec(cfg)
    walk = hz.Walk(spec.seed, spec.structure) if spec.widen else None

    from adatta.features import cup_ties

    cups = cup_ties.cup_bundles()  # Stage A's 90' cup forecasts (served 2026-11-21)

    def run(now: datetime) -> serve.ProjectReport:
        return serve.run_projection(session, store, cfg.bundles, spec.model_name,
                                    cfg.max_goals, manifest, now=now, walk=walk,
                                    cups=cups)  # fmt: skip

    return run


def _national_projector(session: Session, settings: Any) -> Any:
    """Stage D v1's national forecasts from the latest passing live refit, with Stage D v2's
    knockout ties in the same runs and its tournament forecasts (numpy only)."""
    from adatta.eval.baseline import _file_hash, _git_sha
    from adatta.model import national as nt
    from adatta.sim import national_serve, tournament_serve

    store = make_store(settings)
    manifest = {"git_commit": _git_sha(), "lockfile_hash": _file_hash("uv.lock"), "seed": nt.SEED}

    def run(now: datetime) -> Any:
        report = national_serve.run_projection(session, store, manifest, now=now)
        counts, problems = tournament_serve.run_tournaments(session, store, manifest, now=now)
        if counts["written"]:
            report.written["tournaments"] = counts["written"]
        report.problems += problems
        return report

    return run


def _refits_due(session: Session) -> Any:
    """Live refits due now (D33), as "bundle stamp" labels for the capture log."""
    from adatta.eval import harness
    from adatta.model import live
    from adatta.model.backtest import production_spec

    cfg = harness.load_config()
    name = production_spec(cfg).model_name

    def run(now: datetime) -> list[str]:
        due = live.due(session, cfg, name, now, live.triggers_enabled())
        return [f"{b} {harness.stamp(t)}" for b, t in due]

    return run


def _serve_dispatcher() -> Any:
    """Starts serve.yml on main with the run's GITHUB_TOKEN (capture.yml grants actions:
    write); None outside GitHub Actions, where the refit is run by hand, and while results
    refits are off (RESULTS_REFITS, until the D33 dev backtest passes)."""
    from adatta.model import live

    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo or not live.triggers_enabled():
        return None

    def run() -> None:
        r = httpx.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/serve.yml/dispatches",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"ref": "main"}, timeout=30,
        )  # fmt: skip
        r.raise_for_status()

    return run


# Competition files the capture tick reads. Only competitions.yml's bundle leagues get
# forecasts; the others are captured for data (national teams: Stage D; tier 3: Stage A;
# the C2 top flights: Stage C, D49). config/c2_history.yml (second tiers) is backfill only.
CAPTURE_FILES = (
    "config/competitions.yml",
    "config/national_competitions.yml",
    "config/tier3_competitions.yml",
    "config/c2_competitions.yml",
)


@app.command()
def capture() -> None:
    """Snapshot capture tick: run whatever is due (designed for a ~15-minute schedule).

    Exits non-zero when a milestone was missed or a job errored, so a scheduled CI run fails
    visibly; the alert webhook (ALERT_WEBHOOK_URL) is notified as well. A network failure
    alerts only once it repeats on capture.jobs.TRANSIENT_TICKS ticks in a row.
    """
    settings = get_settings()
    client = _client()
    with get_session() as session:
        weather = OpenMeteoClient(make_store(settings))
        comps = [c for path in CAPTURE_FILES for c in load_competitions(path)]
        report = run_capture(
            session, client, comps, weather=weather,
            projector=_projector(session, settings), refits_due=_refits_due(session),
            dispatch=_serve_dispatcher(), national=_national_projector(session, settings),
        )  # fmt: skip
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


@app.command("venues-neutral")
def venues_neutral(
    write: bool = typer.Option(False, help="set match.neutral from the roles"),
    review: bool = typer.Option(True, help="list the review and swapped matches"),
) -> None:
    """Venue role of every loaded club match (home, swapped, neutral, review, unknown) from the
    teams' league grounds and config/neutral_venues.yml (no requests). Prints counts by
    competition and round type, and the matches that need a look. National-competition
    matches are left to `venues-host`."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.features import neutral

    M, S, C = models.Match, models.Season, models.Competition
    rules = neutral.load_rules()
    with get_session() as session:
        leagues, _ = _bundle_competition_ids(session)
        rows = session.execute(
            select(M, C.source_ref, C.name, C.tier).join(S, S.season_id == M.season_id)
            .join(C, C.competition_id == S.competition_id).where(~C.is_national)
        ).all()  # fmt: skip
        matches = [r[0] for r in rows]
        league_of = {r[0].season_id: int(r[1]) for r in rows}
        roles = neutral.venue_roles(session, matches, leagues, rules, league_of)
        keys = pl.DataFrame(
            {"match_id": [m.match_id for m in matches], "competition": [r[2] for r in rows],
             "tier": [str(r[3]) for r in rows],
             "stage": [neutral.stage(m.round) for m in matches]}
        )  # fmt: skip
        out = keys.join(roles, on="match_id")
        table = (
            out.group_by("tier", "competition", "stage")
            .agg([pl.len().alias("matches")]
                 + [(pl.col("role") == r).sum().alias(r) for r in neutral.ROLES]
                 + [(pl.col("basis") == "rule").sum().alias("by_rule")])
            .sort("tier", "competition", "stage")
        )  # fmt: skip
        with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=160):
            typer.echo(table)
        ruled = out.filter(pl.col("basis") == "rule")
        decided = ruled.filter(pl.col("derived") == "neutral").height
        typer.echo(
            f"rule-covered matches: {ruled.height}; derivation alone said neutral for "
            f"{decided}, review {ruled.filter(pl.col('derived') == 'review').height}, "
            f"unknown {ruled.filter(pl.col('derived') == 'unknown').height}"
        )
        if review:
            names = dict(session.execute(select(models.Team.team_id, models.Team.name)).all())
            vnames = dict(session.execute(select(models.Venue.venue_id, models.Venue.name)).all())
            by_id = {m.match_id: m for m in matches}
            look = out.filter(
                pl.col("role").is_in(["review", "swapped"])
                | ((pl.col("role") == "neutral") & (pl.col("basis") == "derived"))
            )
            for r in look.sort("competition").iter_rows(named=True):  # fmt: skip
                m = by_id[r["match_id"]]
                typer.echo(
                    f"{r['role']:8} fixture {m.source_ref} {m.kickoff_time:%Y-%m-%d} "
                    f"{r['competition']} / {m.round}: {names.get(m.home_team_id)} v "
                    f"{names.get(m.away_team_id)} at {vnames.get(m.venue_id or 0)}"
                )
        if write:
            changed = neutral.write_flags(session, matches, roles)
            session.commit()
            typer.echo(f"match.neutral changed on {changed} matches")


@app.command("venues-host")
def venues_host(
    fetch: bool = typer.Option(
        False, help="fetch /countries and /venues per country first (about 150 requests, resumable)"
    ),
    geocode: bool = typer.Option(
        False,
        help="place venues /venues does not list by an "
        "Open-Meteo city search (no API-Football quota, resumable)",
    ),
    write: bool = typer.Option(False, help="set match.neutral on national matches"),
) -> None:
    """Host role of every national-competition match (home, swapped, neutral, unknown) from the
    venue's own country (/venues) and each side's country (adatta/features/host.py). Without
    --fetch no requests. Prints counts of finished matches by competition."""
    from adatta.features import host

    store = make_store(get_settings())
    with get_session() as session:
        rows = host.national_matches(session)
        if fetch:
            client = _client()
            countries = host.Countries(client.countries(session), host.load_aliases())
            session.commit()
            wanted = host.countries_needed({t for _, _, h, a in rows for t in (h, a)}, countries)
            n = host.fetch_venues(session, client, wanted)
            typer.echo(f"fetched venues of {n} countries ({len(wanted)} needed); "
                       f"account quota used today ~{client.quota_used_today}")  # fmt: skip
        countries = host.Countries(host.stored_country_names(session, store), host.load_aliases())
        if not countries.names:
            raise typer.BadParameter("no stored /countries payload: run with --fetch")
        applied = host.apply_countries(session, host.stored_venues(session, store))
        typer.echo(f"venue countries: {applied}")
        if geocode:
            from adatta.adapters.open_meteo import OpenMeteoClient

            om = OpenMeteoClient(store)
            typer.echo(f"geocoded: {host.geocode_fallback(session, om, rows, countries)}; "
                       f"{om.requests_made} Open-Meteo searches")  # fmt: skip
        roles = host.host_roles(session, rows, countries)
        fin = roles.filter(pl.col("status").is_in(["FT", "AET", "PEN"]))
        table = (
            fin.group_by("competition")
            .agg([pl.len().alias("finished")] + [(pl.col("role") == r).sum().alias(r)
                                                  for r in host.ROLES])
            .sort("finished", descending=True)
        )  # fmt: skip
        with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=160):
            typer.echo(table)
            typer.echo(fin.group_by("basis").len().sort("len", descending=True))
        if write:
            changed = host.write_flags(rows, roles)
            session.commit()
            typer.echo(f"match.neutral changed on {changed} national matches")
        else:
            session.commit()  # the venue countries


@app.command("teams-level")
def teams_level(
    fetch: bool = typer.Option(
        False,
        help="ask /teams?id= about national sides that match no country (about 85 "
        "requests, resumable)",
    ),
    write: bool = typer.Option(False, help="write gender, team_level and the clubs cleared"),
) -> None:
    """Gender and team_level of national sides from their names, and clubs among them by the
    provider's own national flag (adatta/features/team_levels.py). Without --fetch no
    requests; without --write nothing is changed."""
    from adatta.features import host
    from adatta.features import team_levels as tl

    store = make_store(get_settings())
    with get_session() as session:
        teams = tl.national_teams(session)
        countries = host.Countries(host.stored_country_names(session, store), host.load_aliases())
        if not countries.names:
            raise typer.BadParameter("no stored /countries payload: run venues-host --fetch")
        candidates = tl.club_candidates(teams, countries)
        if fetch:
            client = _client()
            n = tl.fetch_teams(session, client, candidates)
            typer.echo(f"fetched {n} teams ({len(candidates)} without a country match); "
                       f"account quota used today ~{client.quota_used_today}")  # fmt: skip
        changed = tl.apply_levels(teams)
        cleared = tl.apply_clubs(candidates, tl.stored_teams(session, store), tl.load_nations())
        asked = len(tl.fetched_teams(session) & {t.source_ref for t in candidates})
        typer.echo(f"{len(teams)} national sides; by gender and level {tl.level_counts(teams)}; "
                   f"{changed} changed")  # fmt: skip
        typer.echo(f"{len(candidates)} without a country match, {asked} answered by the "
                   f"provider; {len(cleared)} clubs: {', '.join(cleared) or '-'}")  # fmt: skip
        if write:
            session.commit()
            typer.echo(f"written; senior national sides now {len(tl.senior_national_ids(session))}")
        else:
            session.rollback()
            typer.echo("dry run: nothing written (--write to apply)")


@app.command("results-nt-repair")
def results_nt_repair(
    write: bool = typer.Option(False, help="write the new match_result versions"),
) -> None:
    """Stage A, A0 (a): the 90' score of matches that went to extra time from the goal events
    of the stored detail (adatta/adapters/detail_store.py). Prints counts by competition and
    season only, never scores, so holdout outcomes are not read. No requests; without --write
    nothing is changed."""
    from collections import Counter

    from sqlalchemy import select

    from adatta.adapters import detail_store
    from adatta.db import models

    M, S, C = models.Match, models.Season, models.Competition
    with get_session() as session:
        out = detail_store.repair_normal_time(session, make_store(get_settings()))
        ids = [m for m, _, _ in out["changed"]] + out["unscorable"]
        where = {
            mid: (name, year)
            for mid, name, year in session.execute(
                select(M.match_id, C.name, S.year)
                .join(S, S.season_id == M.season_id)
                .join(C, C.competition_id == S.competition_id)
                .where(M.match_id.in_(ids))
            )
        } if ids else {}  # fmt: skip
        typer.echo(f"{out['matches']} matches after extra time or without a 90' score: "
                   f"{len(out['changed'])} changed, {out['agree']} agree with the events, "
                   f"{out['provider_kept']} keep the provider's split, "
                   f"{len(out['unscorable'])} unscorable, "
                   f"{out['no_result']} without a result row")  # fmt: skip
        for label, mids in (("changed", [m for m, _, _ in out["changed"]]),
                            ("unscorable", out["unscorable"])):  # fmt: skip
            for (name, year), n in sorted(Counter(where[m] for m in mids).items()):
                typer.echo(f"  {label} {year} {name}: {n}")
        if write:
            session.commit()
            typer.echo("written")
        else:
            session.rollback()
            typer.echo("dry run: nothing written (--write to apply)")


@app.command("results-shootouts")
def results_shootouts() -> None:
    """Stage B shootout winner rule (adatta/features/shootouts.py) on the five domestic cups,
    dev seasons and earlier only (config/evaluation.yml), so no holdout outcome is read: per
    cup the shootouts, the basis of the winner, complete kicks, kick counts that differ from
    the provider's score, and the unresolved ones. No requests."""
    import gzip
    import json
    from collections import Counter, defaultdict

    from sqlalchemy import select

    from adatta.adapters import api_football_stats as st
    from adatta.db import models
    from adatta.eval import harness
    from adatta.features import cup_ties
    from adatta.features import shootouts as so

    last = max(harness.load_config().dev_seasons)
    cups = cup_ties.load_formats()
    store = make_store(get_settings())
    M, S, C, D, R = (models.Match, models.Season, models.Competition, models.MatchDetail,
                     models.MatchResult)  # fmt: skip
    with get_session() as session:
        rows = session.execute(
            select(M.match_id, M.source_ref, M.status, M.home_team_id, M.away_team_id,
                   M.kickoff_time, S.season_id, C.name, D.storage_key, R.home_pens, R.away_pens)
            .join(S, S.season_id == M.season_id)
            .join(C, C.competition_id == S.competition_id)
            .outerjoin(D, (D.match_id == M.match_id) & D.flags["returned"].as_boolean())
            .outerjoin(R, (R.match_id == M.match_id) & R.superseded_at.is_(None))
            .where(C.source_ref.in_([str(c) for c in cups]), S.year <= last)
        ).all()  # fmt: skip
        session.rollback()
    by_season: dict[int, list[Any]] = defaultdict(list)
    for r in rows:
        by_season[r[6]].append(r)
    pens = [r for r in rows if r[2] == "PEN"]
    payloads: dict[str, dict[str, Any]] = {}
    tally: Counter[tuple[str, str]] = Counter()
    notes: list[str] = []
    for r in pens:
        mid, ref, _, home, away, kick, sid, cup, key, hp, ap = r
        later = {t for x in by_season[sid] if x[5] > kick for t in (x[3], x[4])}
        kicks = None
        if key is not None:
            if key not in payloads:
                data = json.loads(gzip.decompress(store.get(key)))
                payloads[key] = {str(f["fixture"]["id"]): f for f in data.get("response", [])}
            fx = payloads[key].get(ref)
            kicks = st.shootout_kicks(fx) if fx is not None else None
        res = so.decide((hp, ap), kicks, so.advanced_side(home, away, later))
        tally[(cup, "shootouts")] += 1
        tally[(cup, f"basis {res.basis}")] += 1
        tally[(cup, "kicks listed")] += kicks is not None
        tally[(cup, "kicks complete")] += res.kicks is not None
        tally[(cup, "kick count differs from score")] += (
            res.kicks is not None and hp is not None and res.kicks != (hp, ap)
        )
        if res.basis == "unresolved":
            notes.append(f"{cup} match {mid}: {res.note}")
    typer.echo(f"Shootouts in the five cups, seasons to {last} (dev and earlier):")
    for cup in sorted({c for c, _ in tally}):
        typer.echo(f"  {cup}: " + ", ".join(f"{k} {v}" for (c, k), v in sorted(tally.items())
                                            if c == cup))  # fmt: skip
    for n in notes:
        typer.echo(f"  unresolved: {n}")


@app.command("teams-split")
def teams_split(
    write: bool = typer.Option(False, help="move the matches to the other club"),
) -> None:
    """Split provider team ids that hold two clubs (config/team_splits.yml,
    adatta/adapters/team_splits.py): prints the moves by competition-season and reason, the
    unresolved matches, and the checks after the move. No requests; without --write nothing
    is changed."""
    from collections import Counter

    from sqlalchemy import select

    from adatta.adapters import team_splits as ts
    from adatta.db import models

    with get_session() as session:
        comp_names = dict(session.execute(
            select(models.Competition.competition_id, models.Competition.name)).all())  # fmt: skip
        for sp in ts.load_splits():
            shared = ts.team_id(session, sp.provider, sp.team_ref)
            other = ts.team_id(session, sp.provider, sp.other_ref)
            fx = ts.load_fixtures(session, shared, other, sp.last_season)
            coaches = ts.load_coaches(session, (f.match_id for f in fx), (shared, other))
            plan = ts.plan_split(fx, shared, other, sp.other_city, coaches)
            by_id = {f.match_id: f for f in fx}
            typer.echo(f"{sp.provider}:{sp.team_ref} -> {sp.other_ref} up to {sp.last_season}: "
                       f"{len(plan.moves)} to move, {len(plan.kept)} kept, "
                       f"{len(plan.unresolved)} unresolved")  # fmt: skip
            tally = Counter((comp_names[by_id[m].competition_id], by_id[m].season, why)
                            for m, why in plan.moves.items())  # fmt: skip
            for (comp, season, why), n in sorted(tally.items(), key=lambda kv: kv[0][1]):
                typer.echo(f"  move {season} {comp}: {n} by {why}")
            for m in sorted(plan.unresolved):
                f = by_id[m]
                typer.echo(f"  unresolved {f.season} {comp_names[f.competition_id]} {f.round}: "
                           f"match {m}, {'home' if f.home == shared else 'away'} "
                           f"({f.venue or 'no venue'})")  # fmt: skip
            after = ts.moved(fx, plan.moves, shared, other)
            touched = {(f.competition_id, f.season) for f in fx if f.match_id in plan.moves}
            before = [e for e in ts.group_mismatch(fx) if (e[0], e[1]) in touched]
            mismatch = [e for e in ts.group_mismatch(after) if (e[0], e[1]) in touched]
            both = ts.shared_coaches(after, shared, other, coaches)
            typer.echo(f"  teams not playing a double round robin of their group: {len(before)} "
                       f"before, after {mismatch or 'none'}; coaches named for both "
                       f"clubs {both or 'none'}")  # fmt: skip
            if write:
                typer.echo(f"  written: {ts.apply(session, plan.moves, shared, other)}")
        if write:
            session.commit()
        else:
            session.rollback()
            typer.echo("dry run: nothing written (--write to apply)")


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
    from_season: int = typer.Option(2016, help="first season (dev training reaches 2016/17)"),
    out: str = typer.Option("data/features/context.parquet", help="local parquet copy"),
    upload: bool = typer.Option(False, help="store it as a snapshot for the backtest (F3)"),
) -> None:
    """Build the match context table with team style (adatta/features/context_table.py) for
    bundle league matches (not cancelled, postponed or walked over), report coverage by league
    and season, and optionally store it as a snapshot (no requests)."""
    from datetime import timedelta
    from pathlib import Path

    from sqlalchemy import select

    from adatta.db import models
    from adatta.features import context_table
    from adatta.features.league_rules import load_rules

    horizon = timedelta(hours=horizon_hours)
    store = make_store(get_settings())
    with get_session() as session:
        leagues, _ = _bundle_competition_ids(session)
        ms = context_table.matches(session, leagues, from_season)
        S, C = models.Season, models.Competition
        season_of = {
            sid: (name, year)
            for sid, name, year in session.execute(
                select(S.season_id, C.name, S.year).join(C, C.competition_id == S.competition_id)
            )
        }
        keys = pl.DataFrame({
            "match_id": [m.match_id for m in ms],
            "league": [season_of[m.season_id][0] for m in ms],
            "season": [season_of[m.season_id][1] for m in ms],
            "playoff": [not (m.round or "").startswith("Regular Season") for m in ms],
        })  # fmt: skip
        table = context_table.build(session, store, ms, load_rules(), horizon)
        session.rollback()
    df = keys.join(table, on="match_id")
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
            (pl.col("home_tempo_rel").is_not_null() & pl.col("away_tempo_rel").is_not_null())
            .mean().round(3).alias("style"),
        )
        .sort("league", "season")
    )  # fmt: skip
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=180):
        typer.echo(cov)
    typer.echo(f"{df.height} matches at kickoff - {horizon_hours:g} h written to {out}")
    if upload:
        from adatta.eval.baseline import _git_sha

        d = context_table.save(store, table, horizon, _git_sha())
        typer.echo(f"snapshot {d} stored at {context_table.key(horizon, d)}")


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


@app.command("appearances-build")
def appearances_build(
    league: str = typer.Option(None, help="provider league id; all when omitted"),
    season: int = typer.Option(None, help="season start year; all when omitted"),
    rebuild: bool = typer.Option(False, help="replace files already stored"),
    workers: int = typer.Option(8, help="parallel raw-store reads"),
) -> None:
    """Build player appearances (Phase 3, P3-0) from stored detail payloads into one Parquet
    file per competition-season in the object store. No API requests. Resumable."""
    from adatta.features import appearances

    with get_session() as session:
        counts = appearances.build(
            session, make_store(get_settings()), league, season, rebuild, workers
        )
    typer.echo(counts)


@app.command("stints-build")
def stints_build(
    league: str = typer.Option(None, help="provider league id; all when omitted"),
    season: int = typer.Option(None, help="season start year; all when omitted"),
    rebuild: bool = typer.Option(False, help="replace files already stored"),
    workers: int = typer.Option(8, help="parallel raw-store reads"),
) -> None:
    """Build normal-time stints per team-match (P3-i0) from stored detail payloads into one
    Parquet file per competition-season in the object store. No API requests. Counts only."""
    from adatta.features import stints

    with get_session() as session:
        counts = stints.build(session, make_store(get_settings()), league, season, rebuild,
                              workers)  # fmt: skip
    typer.echo(counts)


@app.command("lineups-project")
def lineups_project() -> None:
    """Projected lineups (docs/DASHBOARD_TASKS.md): P3-b's selection and minutes for every
    regular-season bundle fixture of the next 7 days, from each bundle's latest passing
    serving refit, appended to lineup_projection. No API requests. Counts only."""
    from adatta.eval import harness
    from adatta.sim import lineups

    cfg = harness.load_config()
    with get_session() as session:
        rep = lineups.run(session, make_store(get_settings()), cfg.bundles)
        session.commit()
    typer.echo(json.dumps(rep))
    if rep["no_refit"]:
        raise typer.Exit(code=1)  # a bundle with fixtures but no passing serving refit


@app.command("formations-project")
def formations_project() -> None:
    """P3-h (docs/PHASE3_TASKS.md): P3-c's formation distribution and the last lineup in
    the most likely formation for every regular-season bundle fixture of the next 7 days,
    from each bundle's latest passing serving refit, appended to formation_projection. No
    API requests. Counts only."""
    from datetime import UTC, datetime

    from adatta.eval import harness
    from adatta.sim import formations

    cfg = harness.load_config()
    with get_session() as session:
        rep = formations.run(session, make_store(get_settings()), cfg.bundles,
                             datetime.now(UTC))  # fmt: skip
        session.commit()
    typer.echo(json.dumps(rep))
    if rep["no_refit"]:
        raise typer.Exit(code=1)  # a bundle with fixtures but no passing serving refit


@app.command("lineups-score")
def lineups_score(final: bool = typer.Option(False, help="the judgement (P3-f (d))")) -> None:
    """P3-f's live comparison with P3-b on the stored projections as of kickoff - 24 h, against
    the final record. Aggregates only."""
    from adatta.eval import availability as av
    from adatta.eval import harness
    from adatta.features import appearances as ap
    from adatta.sim import lineups

    cfg = harness.load_config()
    store = make_store(get_settings())
    with get_session() as session:
        rows = av.load(session)
        spells = lineups.load_spells(session)
        kicks = lineups._kickoffs(session, spells["match_id"].unique().to_list())
        session.rollback()
    if rows.is_empty():
        typer.echo("no projections as of kickoff - 24 h yet")
        return
    leagues = [str(x) for ls in cfg.bundles.values() for x in ls]
    seasons = sorted({k.year if k.month >= 7 else k.year - 1 for k in rows["kickoff"].to_list()})
    app = ap.load(store, [(lg, y) for lg in leagues for y in seasons])
    df = av.outcomes(rows, app)
    v = av.verdict(df, av.leakage(df, spells))
    now = datetime.now(UTC)
    rhos = lineups.rho(spells, app.select("match_id", "player_id"), kicks, now)
    typer.echo(av.report(df, v, final, rhos))


@app.command("appearances-report")
def appearances_report(
    max_season: int = typer.Option(2023, help="last season reported (dev ends 2023)"),
    out: str = typer.Option("", help="also write the report as CSV to this path"),
) -> None:
    """Coverage and checks of the stored appearances per competition-season (P3-0 (d)).
    Aggregates only; no match-level data is printed."""
    import polars as pl

    from adatta.features import appearances

    with get_session() as session:
        groups = [g for g in appearances.targets(session) if g[1] <= max_season]
    frame = appearances.load(make_store(get_settings()), sorted(groups))
    rep = appearances.report(frame)
    if out:
        rep.write_csv(out)
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=200, float_precision=3):
        typer.echo(rep)


@app.command("stats-load")
def stats_load(workers: int = typer.Option(8, help="parallel raw-store reads")) -> None:
    """Build team_match_stat from stored detail payloads (no API requests). Resumable."""
    from adatta.adapters.detail_store import load_team_stats

    with get_session() as session:
        counts = load_team_stats(session, make_store(get_settings()), workers)
    typer.echo(counts)
