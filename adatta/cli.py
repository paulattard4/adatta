from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import polars as pl
import typer

from adatta.adapters import backfill
from adatta.adapters.api_football import ApiFootballClient
from adatta.adapters.quota import QuotaPlanner
from adatta.adapters.storage import make_store
from adatta.audit import capability as cap_audit
from adatta.config import get_settings, load_competitions
from adatta.db.session import get_session

app = typer.Typer(help="adatta command line")


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
def backfill_enqueue() -> None:
    """Queue all (competition, season) jobs from config/competitions.yml."""
    with get_session() as session:
        n = backfill.enqueue(session, load_competitions())
        session.commit()
    typer.echo(f"queued {n} new jobs")


@app.command("backfill-run")
def backfill_run(limit: int = typer.Option(None, help="max jobs this run")) -> None:
    """Run pending backfill jobs within the quota budget. Safe to re-run."""
    with get_session() as session:
        result = backfill.run(session, _client(), load_competitions(), limit)
    typer.echo(result)


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
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


if __name__ == "__main__":
    app()
