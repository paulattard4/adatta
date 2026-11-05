from __future__ import annotations

import hashlib
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
from adatta.capture import alert
from adatta.capture.jobs import run_capture
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
    """Queue any new (competition, season) jobs, then run unfinished ones within the backfill
    quota budget. Safe to stop and re-run."""
    comps = load_competitions()
    client = _client()
    with get_session() as session:
        queued = backfill.enqueue(session, comps)
        session.commit()
        result = backfill.run(session, client, comps, limit)
    typer.echo(
        f"queued {queued} new jobs; {result}; account quota used today ~{client.quota_used_today}"
    )


@app.command("backfill-report")
def backfill_report(out: str = typer.Option("reports/backfill", help="report folder")) -> None:
    """Completeness report per competition x season (no API requests)."""
    with get_session() as session:
        rows = backfill.completeness(session, load_competitions())
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
        report = run_capture(session, client, load_competitions())
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.actions.items())) or "nothing due"
    typer.echo(
        f"capture {report.started_at:%Y-%m-%dT%H:%MZ}: {summary}; {report.requests} requests"
    )
    problems = report.missed + report.errors
    if problems:
        alert.send(settings, f"adatta capture: {len(problems)} problem(s)\n" + "\n".join(problems))
        raise typer.Exit(code=1)


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
