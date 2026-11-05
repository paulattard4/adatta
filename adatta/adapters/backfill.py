"""Resumable, idempotent backfill driven by config/competitions.yml and the quota planner."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters.api_football import SOURCE_ID, ApiFootballClient
from adatta.adapters.quota import QuotaExceeded
from adatta.config import Competition
from adatta.db import models


def enqueue(session: Session, comps: list[Competition]) -> int:
    normalize.ensure_source(session)
    added = 0
    for c in comps:
        for year in c.seasons:
            exists = session.scalar(
                select(models.BackfillJob.job_id).where(
                    models.BackfillJob.source_id == SOURCE_ID,
                    models.BackfillJob.source_ref == str(c.league_id),
                    models.BackfillJob.season_year == year,
                )
            )
            if exists is None:
                session.add(
                    models.BackfillJob(
                        source_id=SOURCE_ID,
                        source_ref=str(c.league_id),
                        season_year=year,
                        priority=c.priority,
                    )
                )
                added += 1
    session.flush()
    return added


def run(
    session: Session, client: ApiFootballClient, comps: list[Competition], limit: int | None = None
) -> dict[str, int]:
    """Process pending jobs by priority until done, `limit` reached, or backfill budget is spent."""
    by_ref = {str(c.league_id): c for c in comps}
    jobs = session.scalars(
        select(models.BackfillJob)
        .where(models.BackfillJob.status != "done")
        .order_by(models.BackfillJob.priority, models.BackfillJob.season_year.desc())
    ).all()
    done = failed = 0
    for job in jobs:
        if limit is not None and done + failed >= limit:
            break
        comp = by_ref.get(job.source_ref)
        if comp is None:
            continue
        try:
            fixtures = client.fixtures(session, int(job.source_ref), job.season_year)
            db_comp = normalize.ensure_competition(
                session,
                league_id=comp.league_id,
                name=comp.name,
                country=comp.country,
                level=comp.level,
                tier=str(comp.tier),
            )
            season = normalize.ensure_season(session, db_comp.competition_id, job.season_year)
            normalize.ingest_fixtures(session, season, fixtures, comp.country)
            job.status = "done"
            done += 1
        except QuotaExceeded:
            session.commit()
            break
        except Exception:
            job.status = "failed"
            failed += 1
        session.commit()
    return {"done": done, "failed": failed}
