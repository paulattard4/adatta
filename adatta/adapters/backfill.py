"""Resumable, idempotent historical backfill (Phase 0 task 6; spec §4.2, §19.2 step 5).

One job per (competition, season) in config/competitions.yml:

  pending  -> /fixtures?league&season: matches and results ingested         -> "fixtures"
  fixtures -> /fixtures?ids (20 per call) for finished matches that have no
              match_detail row: payload to the raw store, capabilities noted -> "done"

Every stage and every detail batch commits, so a run stopped by the quota or a crash resumes
where it stopped without re-fetching. Backfill uses the non-live budget: the quota planner
keeps 30% of the daily quota for live capture. Rows carry their true observed_at (ingest
time); nothing is back-dated.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_coverage as coverage
from adatta.adapters import api_football_stats as stats_adapter
from adatta.adapters import normalize
from adatta.adapters.api_football import IDS_BATCH_MAX, SOURCE_ID, ApiFootballClient
from adatta.adapters.quota import QuotaExceeded
from adatta.config import Competition
from adatta.db import models

REGULAR_SEASON = "Regular Season"
DETAIL_FLAGS = (
    "has_lineups",
    "has_player_match_stats",
    "has_shot_stats",
    "has_xg",
    "has_substitution_timestamps",
)


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


def _season(session: Session, comp: Competition, year: int) -> models.Season:
    db_comp = normalize.ensure_competition(
        session, league_id=comp.league_id, name=comp.name, country=comp.country,
        level=comp.level, tier=str(comp.tier),
    )  # fmt: skip
    return normalize.ensure_season(session, db_comp.competition_id, year)


def _pending_detail(session: Session, season_id: int) -> list[models.Match]:
    M, D = models.Match, models.MatchDetail
    return list(
        session.scalars(
            select(M)
            .outerjoin(D, D.match_id == M.match_id)
            .where(
                M.season_id == season_id,
                M.status.in_(normalize.FINISHED),
                D.match_id.is_(None),
            )
            .order_by(M.match_id)
        )
    )


def fetch_detail(
    session: Session, client: ApiFootballClient, season: models.Season,
    on_commit: Callable[[], None],
) -> int:  # fmt: skip
    """Store detail for finished matches that lack it; commit per batch. Returns matches done."""
    by_ref = {int(m.source_ref): m for m in _pending_detail(session, season.season_id)}
    refs = sorted(by_ref)
    done = 0
    for i in range(0, len(refs), IDS_BATCH_MAX):
        batch = refs[i : i + IDS_BATCH_MAX]
        got = {int(fx["fixture"]["id"]): fx for fx in client.fixtures_by_ids(session, batch)}
        now = datetime.now(UTC)
        for ref in batch:
            fx = got.get(ref)
            # A fixture the provider didn't return is recorded too (returned=False), so the
            # report shows the gap and resumption doesn't ask for it forever.
            flags: dict[str, Any] = (
                {**coverage.observed_flags(fx), "returned": True} if fx else {"returned": False}
            )
            session.add(
                models.MatchDetail(
                    match_id=by_ref[ref].match_id, source_id=SOURCE_ID,
                    storage_key=client.last_storage_key or "", observed_at=now, flags=flags,
                )
            )  # fmt: skip
            if fx:
                normalize.ingest_team_stats(
                    session, by_ref[ref].match_id, stats_adapter.team_stats(fx),
                    client.last_storage_key or "", now,
                )  # fmt: skip
                normalize.ingest_match_coaches(
                    session, by_ref[ref].match_id, stats_adapter.match_coaches(fx),
                    client.last_storage_key or "", now,
                )  # fmt: skip
        on_commit()
        session.commit()
        done += len(batch)
    return done


def run(
    session: Session, client: ApiFootballClient, comps: list[Competition], limit: int | None = None
) -> dict[str, int]:
    """Process unfinished jobs by priority (newest season first) until all are done, `limit`
    jobs have been attempted, or the backfill budget is spent."""
    by_ref = {str(c.league_id): c for c in comps}
    jobs = session.scalars(
        select(models.BackfillJob)
        .where(models.BackfillJob.status != "done")
        .order_by(models.BackfillJob.priority, models.BackfillJob.season_year.desc())
    ).all()
    counts: Counter[str] = Counter()
    start = client.requests_made
    for job in jobs:
        comp = by_ref.get(job.source_ref)
        if comp is None or job.season_year not in comp.seasons:
            continue
        if limit is not None and counts["attempted"] >= limit:
            break
        counts["attempted"] += 1
        base, before = job.requests or 0, client.requests_made

        def spent(job: models.BackfillJob = job, base: int = base, before: int = before) -> None:
            job.requests = base + client.requests_made - before
            job.updated_at = datetime.now(UTC)

        try:
            season = _season(session, comp, job.season_year)
            if job.status != "fixtures":
                fixtures = client.fixtures(session, comp.league_id, job.season_year)
                normalize.ingest_fixtures(session, season, fixtures, comp.country)
                job.n_fixtures, job.status, job.error = len(fixtures), "fixtures", None
                spent()
                session.commit()
            counts["detail"] += fetch_detail(session, client, season, spent)
            job.status = "done"
            spent()
            session.commit()
            counts["done"] += 1
        except QuotaExceeded:
            session.rollback()
            spent()
            session.commit()
            counts["stopped_by_quota"] = 1
            break
        except Exception as exc:  # one bad season must not stop the rest
            session.rollback()
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
            spent()
            session.commit()
            counts["failed"] += 1
    counts["requests"] = client.requests_made - start
    return dict(counts)


# --------------------------------------------------------------------------- report


def _is_regular(round_: str | None) -> bool:
    return bool(round_) and str(round_).startswith(REGULAR_SEASON)


def completeness(session: Session, comps: list[Competition]) -> list[dict[str, Any]]:
    """One row per configured (competition, season): what is loaded and what is missing.

    Gaps are flagged, never filled (principle 8)."""
    C, S, M = models.Competition, models.Season, models.Match
    R, D, J = models.MatchResult, models.MatchDetail, models.BackfillJob
    rows: list[dict[str, Any]] = []
    for comp in comps:
        is_league = str(comp.tier) != "cup"
        for year in comp.seasons:
            job = session.scalar(
                select(J).where(
                    J.source_id == SOURCE_ID, J.source_ref == str(comp.league_id),
                    J.season_year == year,
                )
            )  # fmt: skip
            season = session.scalar(
                select(S)
                .join(C, C.competition_id == S.competition_id)
                .where(
                    C.source_id == SOURCE_ID, C.source_ref == str(comp.league_id), S.year == year
                )
            )
            matches = (
                list(session.scalars(select(M).where(M.season_id == season.season_id)))
                if season
                else []
            )
            ids = [m.match_id for m in matches]
            results: dict[int, models.MatchResult] = {}
            details: dict[int, models.MatchDetail] = {}
            for part in normalize.chunks(ids):
                results.update(
                    (r.match_id, r)
                    for r in session.scalars(
                        select(R).where(R.match_id.in_(part), R.superseded_at.is_(None))
                    )
                )
                details.update(
                    (d.match_id, d) for d in session.scalars(select(D).where(D.match_id.in_(part)))
                )
            finished = [m for m in matches if m.status in normalize.FINISHED]
            not_played = Counter(m.status for m in matches if m.status not in normalize.FINISHED)
            regular = [m for m in matches if _is_regular(m.round)]
            teams = {t for m in regular for t in (m.home_team_id, m.away_team_id)}
            expected = len(teams) * (len(teams) - 1) if is_league and teams else None
            with_nt = sum(
                1
                for m in finished
                if (r := results.get(m.match_id)) is not None
                and r.home_goals_nt is not None
                and r.away_goals_nt is not None
            )
            stored = [details[m.match_id] for m in finished if m.match_id in details]
            returned = [d for d in stored if d.flags.get("returned")]
            row: dict[str, Any] = {
                "competition": comp.name,
                "league_id": comp.league_id,
                "season": year,
                "job_status": job.status if job else "not_queued",
                "requests": job.requests if job else 0,
                "listed": len(matches),
                "finished": len(finished),
                "not_played": ";".join(f"{k}={v}" for k, v in sorted(not_played.items())),
                "regular_listed": len(regular) if is_league else None,
                "regular_expected": expected,
                "finished_with_nt_score": with_nt,
                "detail_stored": len(stored),
                "detail_returned": len(returned),
                **{f: sum(1 for d in returned if d.flags.get(f)) for f in DETAIL_FLAGS},
            }
            row["flags"] = ";".join(_flags(row, job, is_league))
            rows.append(row)
    return rows


def _flags(row: dict[str, Any], job: models.BackfillJob | None, is_league: bool) -> list[str]:
    out = []
    if job is None or job.status != "done":
        out.append(f"job {row['job_status']}" + (f": {job.error}" if job and job.error else ""))
    if row["listed"] == 0:
        out.append("no fixtures")
    if is_league and row["listed"]:
        if row["regular_expected"] is None:
            out.append("no regular-season round labels")
        elif row["regular_listed"] != row["regular_expected"]:
            out.append(f"regular season {row['regular_listed']}/{row['regular_expected']}")
    if row["finished_with_nt_score"] < row["finished"]:
        out.append(f"nt score missing {row['finished'] - row['finished_with_nt_score']}")
    if row["detail_returned"] < row["finished"]:
        out.append(f"detail missing {row['finished'] - row['detail_returned']}")
    return out
