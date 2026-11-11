"""Weather for past matches, for backtests (D29 (c); register 2.12; docs/PHASE2_TASKS.md F2).

Live capture stores forecasts only from 2026-11-05. For earlier matches:

  as-issued   from AS_ISSUED_FROM (2024-02-01): Open-Meteo previous-runs forecasts, read
              from the runs 1 and 2 days before kickoff and stored as weather_snapshot rows
              (lead_hours 24 and 48, observed_at = kickoff - lead, hourly["run"] names the
              run). These are genuine pre-match forecasts: the context table's forecast
              rule (newest snapshot observed <= as_of) uses them unchanged.
  stand-in    from PROXY_FROM (2019-07-01) to AS_ISSUED_FROM: ERA5 realised weather at the
              venue point (the climatology's point and elevation). Not observable before
              kickoff: D29 admits it in backtests only, as a stand-in for a short-lead
              forecast, for horizons up to PROXY_MAX_HORIZON, labelled `realised_proxy` and
              reported as an optimistic look-ahead. Read from the raw store at build time,
              never stored as a forecast.
  check       ERA5 also for AS_ISSUED_FROM to CHECK_UNTIL, so the stand-in's error against
              the day-1 forecast can be measured (`proxy_error`).

Requests: one per venue point and window of at most DAYS_PER_CALL days holding match dates,
so each counts as one free-tier call. Runs are budgeted, paced and resumable.
"""

from __future__ import annotations

import gzip
import json
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters.open_meteo import (
    DAYS_PER_CALL,
    SOURCE_ID,
    MatchWeather,
    OpenMeteoClient,
    match_weather,
    previous_run_view,
)
from adatta.adapters.storage import RawStore
from adatta.db import models
from adatta.features import climatology, travel

AS_ISSUED_FROM = datetime(2024, 2, 1, tzinfo=UTC)
PROXY_FROM = datetime(2019, 7, 1, tzinfo=UTC)
CHECK_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)
PROXY_MAX_HORIZON = timedelta(hours=24)
RUNS = {1: 24.0, 2: 48.0}  # previous_dayN -> lead hours
PLAYED_AFTER = timedelta(days=1)  # fetch as-issued forecasts only for matches this long past
ERA5_SINCE = "2019-01-01"
MAX_CONSECUTIVE_FAILURES = 5  # archive payloads from here on are realised weather, not baseline


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Target:
    match_id: int
    venue_id: int
    kickoff: datetime
    latitude: float
    longitude: float
    elevation: float | None

    def days(self) -> set[date]:
        """UTC dates the match hours (kickoff to kickoff + 3 h) fall on."""
        return {self.kickoff.date(), (self.kickoff + timedelta(hours=3)).date()}


def targets(
    matches: Sequence[models.Match], venue_of: dict[int, int | None],
    venues: dict[int, models.Venue],
) -> list[Target]:  # fmt: skip
    """Matches whose venue (`venue_of`, e.g. travel_features' venue_id) has usable
    coordinates."""
    out: list[Target] = []
    for m in matches:
        vid = venue_of.get(m.match_id)
        v = venues.get(vid) if vid is not None else None
        if v is None or v.geocode_status not in travel.USABLE:
            continue
        if v.latitude is None or v.longitude is None:
            continue
        out.append(Target(m.match_id, v.venue_id, _utc(m.kickoff_time), v.latitude,
                          v.longitude, v.elevation_m))  # fmt: skip
    return out


def load_targets(session: Session, matches: Sequence[models.Match]) -> list[Target]:
    trav = travel.travel_features(session, matches)
    venue_of = dict(zip(trav["match_id"].to_list(), trav["venue_id"].to_list(), strict=True))
    ids = sorted({v for v in venue_of.values() if v is not None})
    V = models.Venue
    venues = {
        v.venue_id: v
        for part in normalize.chunks(ids)
        for v in session.scalars(select(V).where(V.venue_id.in_(part)))
    }
    return targets(matches, venue_of, venues)


def windows(days: Iterable[date], span: int = DAYS_PER_CALL) -> list[tuple[date, date]]:
    """Greedy windows of at most `span` days covering every date given."""
    out: list[tuple[date, date]] = []
    for d in sorted(set(days)):
        if out and (d - out[-1][0]).days < span:
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


@dataclass
class Job:
    kind: str  # as_issued | era5
    point: tuple[float, float]
    start: date
    end: date
    elevation: float | None = None
    targets: list[Target] = field(default_factory=list)


def _as_issued_done(session: Session, match_ids: Iterable[int]) -> set[int]:
    W = models.WeatherSnapshot
    done: set[int] = set()
    for part in normalize.chunks(sorted(set(match_ids))):
        for mid, hourly in session.execute(
            select(W.match_id, W.hourly).where(W.match_id.in_(part), W.lead_hours == RUNS[1])
        ):
            if isinstance(hourly, dict) and hourly.get("run") == "previous_day1":
                done.add(mid)
    return done


def era5_payloads(session: Session) -> dict[tuple[float, float], list[tuple[date, date, str]]]:
    """{point: [(start, end, storage key)]} for realised-weather archive payloads."""
    R = models.RawPayload
    out: dict[tuple[float, float], list[tuple[date, date, str]]] = defaultdict(list)
    for params, key in session.execute(
        select(R.params, R.storage_key).where(R.endpoint == "/v1/archive")
    ):
        if str(params.get("start_date", "")) >= ERA5_SINCE:
            point = (float(params["latitude"]), float(params["longitude"]))
            out[point].append((date.fromisoformat(params["start_date"]),
                               date.fromisoformat(params["end_date"]), key))  # fmt: skip
    return out


def plan(session: Session, all_targets: Sequence[Target], now: datetime) -> list[Job]:
    """The requests still needed: as-issued for played matches from AS_ISSUED_FROM without
    a stored day-1 run; ERA5 for match dates from PROXY_FROM to CHECK_UNTIL not covered."""
    jobs: list[Job] = []
    issued = [t for t in all_targets if AS_ISSUED_FROM <= t.kickoff <= now - PLAYED_AFTER]
    done = _as_issued_done(session, (t.match_id for t in issued))
    by_point: dict[tuple[float, float], list[Target]] = defaultdict(list)
    for t in issued:
        if t.match_id not in done:
            by_point[(round(t.latitude, 4), round(t.longitude, 4))].append(t)
    for point, ts in sorted(by_point.items()):
        for start, end in windows(d for t in ts for d in t.days()):
            jobs.append(Job("as_issued", point, start, end,
                            targets=[t for t in ts if t.days() <= _span(start, end)]))  # fmt: skip
    have = era5_payloads(session)
    cells: dict[tuple[float, float], list[Target]] = defaultdict(list)
    for t in all_targets:
        if PROXY_FROM <= t.kickoff < CHECK_UNTIL:
            cells[climatology.cell(t.latitude, t.longitude)].append(t)
    for point, ts in sorted(cells.items()):
        covered = {d for s, e, _ in have.get(point, []) for d in _span(s, e)}
        need = {d for t in ts for d in t.days()} - covered
        elevation = next((t.elevation for t in ts if t.elevation is not None), None)
        for start, end in windows(need):
            jobs.append(Job("era5", point, start, end, elevation))
    return jobs


def _span(start: date, end: date) -> set[date]:
    return {start + timedelta(days=i) for i in range((end - start).days + 1)}


def fetch(
    session: Session, om: OpenMeteoClient, jobs: Sequence[Job], budget_calls: int,
    sleep: Callable[[float], None] = time.sleep, calls_per_hour: int = 4000,
) -> Counter[str]:  # fmt: skip
    """Run jobs (one call each) until the budget is spent; stops on a rate limit. A server
    error, timeout or dropped connection skips the window, stopping after
    MAX_CONSECUTIVE_FAILURES in a row. Commits after each job, so an interrupted run resumes
    where it stopped."""
    counts: Counter[str] = Counter(jobs=len(jobs))
    pause = 3600 / calls_per_hour
    failures = 0
    for i, job in enumerate(jobs):
        if counts["calls"] + 1 > budget_calls:
            counts["left_for_later"] = len(jobs) - i
            break
        if i:
            sleep(pause)
        lat, lon = job.point
        try:
            if job.kind == "as_issued":
                payload = om.previous_runs(session, lat, lon, job.start.isoformat(),
                                           job.end.isoformat(), tuple(RUNS))  # fmt: skip
                counts["snapshots"] += _store_as_issued(session, om, payload, job)
            else:
                om.archive(session, lat, lon, job.start.isoformat(), job.end.isoformat(),
                           elevation=job.elevation)  # fmt: skip
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
            if status == 429:
                counts["rate_limited"] += 1
                counts["left_for_later"] = len(jobs) - i
                break
            if status is not None and status < 500:
                raise
            # A server error, timeout or dropped connection after the client's retries:
            # skip the window (the next run plans it again); stop if the service looks down.
            session.rollback()
            counts["server_errors"] += 1
            counts["calls"] += 1
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                counts["left_for_later"] = len(jobs) - i - 1
                break
            continue
        failures = 0
        session.commit()
        counts[job.kind] += 1
        counts["calls"] += 1
    return counts


def _store_as_issued(
    session: Session, om: OpenMeteoClient, payload: dict[str, Any], job: Job
) -> int:
    n = 0
    for t in job.targets:
        for day, lead in RUNS.items():
            mw = match_weather(previous_run_view(payload, day), t.kickoff)
            session.add(models.WeatherSnapshot(
                match_id=t.match_id, venue_id=t.venue_id, source_id=SOURCE_ID,
                observed_at=t.kickoff - timedelta(hours=lead), kickoff_time=t.kickoff,
                lead_hours=lead, latitude=job.point[0], longitude=job.point[1],
                temperature_c=mw.temperature_c if mw else None,
                precipitation_mm=mw.precipitation_mm if mw else None,
                wind_speed_kmh=mw.wind_speed_kmh if mw else None,
                hourly={**(mw.hourly if mw else {}), "run": f"previous_day{day}"},
                storage_key=om.last_storage_key or "",
            ))  # fmt: skip
            n += 1
    session.flush()
    return n


def load_realised(
    session: Session, store: RawStore, wanted: Sequence[Target]
) -> dict[int, MatchWeather]:
    """{match_id: ERA5 realised weather over the match} for targets whose dates a stored
    payload covers. Backtest stand-in only (D29): see the module docstring."""
    have = era5_payloads(session)
    cache: dict[str, dict[str, Any]] = {}
    out: dict[int, MatchWeather] = {}
    for t in wanted:
        point = climatology.cell(t.latitude, t.longitude)
        for start, end, key in have.get(point, []):
            if t.days() <= _span(start, end):
                if key not in cache:
                    cache[key] = json.loads(gzip.decompress(store.get(key)))
                mw = match_weather(cache[key], t.kickoff)
                if mw is not None and mw.temperature_c is not None:
                    out[t.match_id] = mw
                    break
    return out


def proxy_error(
    session: Session, store: RawStore, wanted: Sequence[Target]
) -> dict[str, dict[str, float]]:
    """Stand-in minus day-1 forecast over matches that have both (AS_ISSUED_FROM to
    CHECK_UNTIL): mean absolute difference and mean difference per variable, and n."""
    W = models.WeatherSnapshot
    check = [t for t in wanted if AS_ISSUED_FROM <= t.kickoff < CHECK_UNTIL]
    realised = load_realised(session, store, check)
    fc: dict[int, models.WeatherSnapshot] = {}
    for part in normalize.chunks(sorted(realised)):
        for w in session.scalars(select(W).where(W.match_id.in_(part), W.lead_hours == RUNS[1])):
            if isinstance(w.hourly, dict) and w.hourly.get("run") == "previous_day1":
                fc[w.match_id] = w
    out: dict[str, dict[str, float]] = {}
    for var in ("temperature_c", "wind_speed_kmh", "precipitation_mm"):
        diffs = [
            getattr(r, var) - getattr(fc[mid], var)
            for mid, r in realised.items()
            if mid in fc and getattr(r, var) is not None and getattr(fc[mid], var) is not None
        ]
        if diffs:
            out[var] = {"n": len(diffs), "mae": round(sum(map(abs, diffs)) / len(diffs), 3),
                        "bias": round(sum(diffs) / len(diffs), 3)}  # fmt: skip
    return out
