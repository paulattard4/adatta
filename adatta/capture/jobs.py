"""Snapshot capture (Phase 0 task 5; spec §7.1, §12.2–12.3, §15.2, §19.2 step 4).

`run_capture` is idempotent and safe to run on any schedule (designed for a ~15-minute
poller). Each run does only what is due, time-critical work first, because a missed window
can't be recovered and a daily job can simply run on the next tick:

  every run              lineups   fixtures within LINEUP_POLL_FROM of kickoff with no lineup
                         odds      milestone kinds that are due (capture.snapshots)
                         missed    milestone windows that passed uncaptured -> alert
  daily per competition  calendar  current-season fixtures -> match rows, results, and a
                                    fixture_snapshot whenever kickoff/status/referee/venue
                                    changes
                         status    /injuries -> player_status_snapshot for unplayed fixtures
                         detail    post-match detail + team stats for newly finished matches

Rows are stamped with the time the API response arrived (client.last_response_at), not the
tick's start, which can be minutes earlier on a busy run: an earlier stamp would claim the data
was known before it was. Windows and filters still use the tick's `now`.

Daily jobs stop starting once a run has used DAILY_BUDGET_S, so a backlog spreads over several
ticks and the run still finishes inside the workflow's timeout.

Odds are written only to odds_capture / odds_snapshot. They are evaluation data and are
never read by model, feature or simulation code (principle 1; enforced by a test).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_capture as afc
from adatta.adapters import backfill, normalize
from adatta.adapters.api_football import IDS_BATCH_MAX, SOURCE_ID, ApiFootballClient
from adatta.adapters.quota import QuotaExceeded
from adatta.capture import snapshots
from adatta.config import Competition
from adatta.db import models

DAILY = timedelta(hours=20)  # "daily" jobs re-run once this long has passed since the last ok
MISSED_LOOKBACK = timedelta(days=2)
DAILY_BUDGET_S = 480.0  # no new competition's daily jobs after this much of a run (timeout 14 min)


@dataclass
class CaptureReport:
    started_at: datetime
    requests: int = 0
    actions: Counter[str] = field(default_factory=Counter)
    missed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missed and not self.errors


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _observed(client: ApiFootballClient) -> datetime:
    """Arrival time of the client's last response."""
    if client.last_response_at is None:
        raise RuntimeError("no response received yet")
    return client.last_response_at


def _log(
    session: Session, job: str, scope: str, now: datetime, outcome: str, requests: int,
    detail: str = "",
) -> None:  # fmt: skip
    session.add(
        models.CaptureLog(
            job=job, scope=scope, run_at=now, outcome=outcome, requests=requests,
            detail=detail[:500],
        )
    )  # fmt: skip


def _ran_recently(session: Session, job: str, scope: str, now: datetime) -> bool:
    L = models.CaptureLog
    return (
        session.scalar(
            select(L.log_id).where(
                L.job == job, L.scope == scope, L.outcome == "ok", L.run_at > now - DAILY
            )
        )
        is not None
    )


def _season(session: Session, comp: Competition, year: int) -> models.Season:
    db_comp = normalize.ensure_competition(
        session, league_id=comp.league_id, name=comp.name, country=comp.country,
        level=comp.level, tier=str(comp.tier),
    )  # fmt: skip
    return normalize.ensure_season(session, db_comp.competition_id, year)


def _current_season_row(session: Session, comp: Competition) -> models.Season | None:
    C, S = models.Competition, models.Season
    return session.scalar(
        select(S)
        .join(C, C.competition_id == S.competition_id)
        .where(C.source_id == SOURCE_ID, C.source_ref == str(comp.league_id))
        .order_by(S.year.desc())
    )


def _done_by_match(session: Session, ids: list[int]) -> dict[int, set[str]]:
    """Snapshot kinds already recorded, for many matches in one query per 500 ids."""
    oc = models.OddsCapture
    out: dict[int, set[str]] = {i: set() for i in ids}
    for part in normalize.chunks(ids):
        for mid, kind in session.execute(
            select(oc.match_id, oc.snapshot_kind).where(oc.match_id.in_(part))
        ):
            out[mid].add(kind)
    return out


def _logged(session: Session, job: str, ids: list[int], since: datetime | None = None) -> set[int]:
    """Match ids that have a capture_log row for `job` (optionally since a time)."""
    L = models.CaptureLog
    out: set[int] = set()
    for part in normalize.chunks([str(i) for i in ids]):
        q = select(L.scope).where(L.job == job, L.scope.in_(part))
        if since is not None:
            q = q.where(L.run_at > since)
        out.update(int(scope) for scope in session.scalars(q))
    return out


# --------------------------------------------------------------------------- daily jobs


def capture_calendar(
    session: Session, client: ApiFootballClient, comp: Competition, now: datetime
) -> int:
    """Refresh the current season's calendar; return fixture_snapshot rows added."""
    year = afc.current_season(client.league_seasons(session, comp.league_id, live=True))
    if year is None:
        raise RuntimeError(f"/leagues reports no current season for league {comp.league_id}")
    fixtures = client.fixtures(session, comp.league_id, year, live=True)
    observed = _observed(client)
    season = _season(session, comp, year)
    normalize.ingest_fixtures(session, season, fixtures, comp.country)
    added = 0
    F = models.FixtureSnapshot
    states = [afc.fixture_state(fx) for fx in fixtures]
    matches = normalize.matches_by_ref(session, (str(st.fixture_id) for st in states))
    latest: dict[int, models.FixtureSnapshot] = {}
    for part in normalize.chunks([m.match_id for m in matches.values()]):
        newest = (
            select(F.match_id, func.max(F.observed_at).label("at"))
            .where(F.match_id.in_(part))
            .group_by(F.match_id)
            .subquery()
        )
        latest.update(
            (f.match_id, f)
            for f in session.scalars(
                select(F).join(
                    newest, (F.match_id == newest.c.match_id) & (F.observed_at == newest.c.at)
                )
            )
        )
    for st in states:
        match = matches.get(str(st.fixture_id))
        if match is None:
            continue
        last = latest.get(match.match_id)
        new = (st.kickoff, st.status, st.referee, st.venue_ref, st.round)
        if last is not None:
            old = (_utc(last.kickoff_time), last.status, last.referee, last.venue_ref, last.round)
            if old == new:
                continue
        session.add(
            F(
                match_id=match.match_id, source_id=SOURCE_ID, observed_at=observed,
                kickoff_time=st.kickoff, status=st.status, referee=st.referee,
                venue_ref=st.venue_ref, round=st.round,
            )
        )  # fmt: skip
        added += 1
    session.flush()
    return added


def capture_status(
    session: Session, client: ApiFootballClient, comp: Competition, now: datetime
) -> Counter[str]:
    """Daily availability snapshot for unplayed fixtures of the current season."""
    season = _current_season_row(session, comp)
    if season is None:
        raise RuntimeError("no season row yet: calendar must run first")
    # The feed can list the same player and fixture more than once; keep the last entry so
    # one run can't flip-flop a status.
    latest = {
        (a.player_ref, a.fixture_id): a
        for a in afc.parse_injuries(client.injuries(session, comp.league_id, season.year))
        if a.fixture_kickoff > now
    }
    observed = _observed(client)
    absences = list(latest.values())
    P, M = models.PlayerStatusSnapshot, models.Match
    matches = normalize.matches_by_ref(session, (str(a.fixture_id) for a in absences))
    players = normalize.players_by_ref(session, {a.player_ref: a.player_name for a in absences})
    teams = normalize.teams_by_ref(
        session, {a.team_ref: a.team_name for a in absences}, comp.country
    )
    # Every open spell for an upcoming fixture of this season, loaded once.
    open_rows = session.scalars(
        select(P)
        .join(M, M.match_id == P.match_id)
        .where(P.valid_to.is_(None), M.season_id == season.season_id, M.kickoff_time > now)
    ).all()
    open_by_key = {(r.player_id, r.match_id): r for r in open_rows}
    counts: Counter[str] = Counter()
    seen: set[tuple[int, int]] = set()
    for a in absences:
        match = matches.get(str(a.fixture_id))
        if match is None:
            counts["unknown_fixture"] += 1
            continue
        player, team = players[a.player_ref], teams[a.team_ref]
        seen.add((player.player_id, match.match_id))
        cur = open_by_key.get((player.player_id, match.match_id))
        if cur is not None and (cur.status, cur.reason) == (a.status, a.reason):
            counts["unchanged"] += 1
            continue
        if cur is not None:
            cur.valid_to = observed
            cur.superseded_at = observed
            counts["changed"] += 1
        else:
            counts["new"] += 1
        session.add(
            P(
                player_id=player.player_id, team_id=team.team_id, match_id=match.match_id,
                source_id=SOURCE_ID, observed_at=observed, valid_from=observed, status=a.status,
                reason=a.reason,
            )
        )  # fmt: skip
    # Players no longer listed for an upcoming fixture of this season: their spell has ended.
    for row in open_rows:
        if row.match_id is not None and (row.player_id, row.match_id) not in seen:
            row.valid_to = observed
            counts["cleared"] += 1
    session.flush()
    return counts


def capture_detail(session: Session, client: ApiFootballClient, comp: Competition) -> int:
    """Daily: post-match detail (lineups, events, statistics incl. xG) for finished matches of
    the current season that lack it, 20 per request; team stats are parsed as they arrive."""
    season = _current_season_row(session, comp)
    if season is None:
        raise RuntimeError("no season row yet: calendar must run first")
    return backfill.fetch_detail(session, client, season, lambda: None)


# --------------------------------------------------------------------------- per-match jobs


def _with_lineup(session: Session, ids: list[int]) -> set[int]:
    L = models.LineupSnapshot
    out: set[int] = set()
    for part in normalize.chunks(ids):
        out.update(session.scalars(select(L.match_id).where(L.match_id.in_(part))))
    return out


def _upcoming(session: Session, now: datetime, until: timedelta) -> list[models.Match]:
    M = models.Match
    rows = session.scalars(
        select(M)
        .where(M.kickoff_time > now, M.kickoff_time <= now + until)
        .order_by(M.kickoff_time)
    ).all()
    return [m for m in rows if m.status not in afc.NO_MATCH]


def capture_lineups(session: Session, client: ApiFootballClient, now: datetime) -> list[int]:
    """Poll fixtures close to kickoff that have no lineup yet; return match ids newly confirmed."""
    soon = [m for m in _upcoming(session, now, snapshots.LINEUP_POLL_FROM) if m.status != "TBD"]
    have = _with_lineup(session, [m.match_id for m in soon])
    pending = [m for m in soon if m.match_id not in have]
    by_ref = {int(m.source_ref): m for m in pending}
    confirmed: list[int] = []
    refs = sorted(by_ref)
    found: list[tuple[models.Match, list[afc.Lineup], datetime]] = []
    for i in range(0, len(refs), IDS_BATCH_MAX):
        batch = client.fixtures_by_ids(session, refs[i : i + IDS_BATCH_MAX], live=True)
        observed = _observed(client)
        for fx in batch:
            match = by_ref.get(afc.fixture_state(fx).fixture_id)
            lineups = afc.parse_lineups(fx)
            if match is not None and lineups:
                found.append((match, lineups, observed))
    teams = normalize.teams_by_ref(
        session, {lu.team_ref: lu.team_ref for _, lus, _ in found for lu in lus}
    )
    for match, lineups, observed in found:
        for lu in lineups:
            team = teams[lu.team_ref]
            session.add(
                models.LineupSnapshot(
                    match_id=match.match_id, team_id=team.team_id, source_id=SOURCE_ID,
                    observed_at=observed, formation=lu.formation, coach_ref=lu.coach_ref,
                    coach_name=lu.coach_name, starters=lu.starters,
                    substitutes=lu.substitutes,
                )
            )  # fmt: skip
        confirmed.append(match.match_id)
    for m in pending:
        _log(session, "lineups", str(m.match_id), now, "ok", 0,
             "confirmed" if m.match_id in confirmed else "not yet")  # fmt: skip
    session.flush()
    return confirmed


def _record_capture(
    session: Session, match_id: int, kinds: Iterable[str], odds: dict[str, Any] | None,
    key: str | None, now: datetime,
) -> None:  # fmt: skip
    published = afc.odds_published_at(odds) if odds else None
    prices = afc.parse_odds(odds) if odds else []
    for kind in kinds:
        cap = models.OddsCapture(
            match_id=match_id, source_id=SOURCE_ID, snapshot_kind=kind,
            status="captured" if odds else "no_market", observed_at=now,
            published_at=published, storage_key=key, n_bookmakers=afc.n_bookmakers(odds),
        )  # fmt: skip
        session.add(cap)
        session.flush()
        session.add_all(
            models.OddsSnapshot(
                match_id=match_id, source_id=SOURCE_ID, capture_id=cap.capture_id,
                bookmaker=p.bookmaker, market=p.market, line=p.line, selection=p.selection,
                decimal_odds=p.decimal_odds, snapshot_kind=kind, observed_at=now,
                published_at=published,
            )
            for p in prices
        )  # fmt: skip


def capture_odds(
    session: Session, client: ApiFootballClient, now: datetime, lineup_confirmed: set[int]
) -> Counter[str]:
    counts: Counter[str] = Counter()
    # Plain values: the per-match commit below expires ORM objects, and reloading them would
    # cost a database round trip per fixture.
    upcoming = [
        (m.match_id, int(m.source_ref), m.status, _utc(m.kickoff_time))
        for m in _upcoming(session, now, snapshots.INITIAL_HORIZON)
    ]
    ids = [match_id for match_id, *_ in upcoming]
    done_by = _done_by_match(session, ids)
    tried = _logged(session, "odds_initial", ids, since=now - DAILY)
    for match_id, fixture_ref, status, kickoff in upcoming:
        done = done_by[match_id]
        kinds = [] if status == "TBD" else snapshots.due_kinds(kickoff, now, done)
        if match_id in lineup_confirmed and "lineup" not in done:
            kinds.append("lineup")
        want_initial = "initial" not in done and match_id not in tried
        if not kinds and not want_initial:
            continue
        odds = client.odds(session, fixture_ref)
        observed = _observed(client)
        if observed >= kickoff:
            continue  # kicked off while this tick ran: no pre-match label fits; next tick reports
        # A window can close between the tick's start and the response: never label late.
        still_open = set(snapshots.due_kinds(kickoff, observed, done))
        kinds = [k for k in kinds if k in still_open or k not in snapshots.TIMED]
        has_market = afc.n_bookmakers(odds) > 0
        if "initial" not in done and has_market:
            kinds.insert(0, "initial")
        elif want_initial:
            _log(session, "odds_initial", str(match_id), now, "ok", 1, "no market yet")
        if kinds:
            _record_capture(
                session, match_id, kinds, odds if has_market else None, client.last_storage_key,
                observed,
            )  # fmt: skip
            for k in kinds:
                counts[f"{k}:{'captured' if has_market else 'no_market'}"] += 1
        session.commit()  # a later failure or a timeout must not lose this window
    session.flush()
    return counts


def detect_missed(session: Session, now: datetime) -> list[str]:
    """Record and return milestones whose window passed without a capture row."""
    M, F = models.Match, models.FixtureSnapshot
    matches = session.scalars(
        select(M).where(
            M.kickoff_time > now - MISSED_LOOKBACK, M.kickoff_time <= now + timedelta(days=8)
        )
    ).all()
    matches = [m for m in matches if m.status not in afc.NO_MATCH and m.status != "TBD"]
    ids = [m.match_id for m in matches]
    first_seen_by: dict[int, datetime] = {}
    for part in normalize.chunks(ids):
        first_seen_by.update(
            (mid, at)
            for mid, at in session.execute(
                select(F.match_id, func.min(F.observed_at))
                .where(F.match_id.in_(part))
                .group_by(F.match_id)
            )
        )
    done_by = _done_by_match(session, ids)
    polled_lineups = _logged(session, "lineups", ids)
    out: list[str] = []
    for match in matches:
        first_seen = first_seen_by.get(match.match_id)
        if first_seen is None:
            continue  # not captured through the calendar job; nothing was expected
        kickoff = _utc(match.kickoff_time)
        done = done_by[match.match_id]
        for kind in snapshots.missed_kinds(kickoff, now, done, _utc(first_seen)):
            if kind == "initial":
                status = "no_market"  # no capture ever found a market before kickoff
            elif kind == "lineup":
                polled = match.match_id in polled_lineups
                status = "not_published" if polled else "missed"
            else:
                status = "missed"
            session.add(
                models.OddsCapture(
                    match_id=match.match_id, source_id=SOURCE_ID, snapshot_kind=kind,
                    status=status, observed_at=now, n_bookmakers=0,
                )
            )  # fmt: skip
            if status == "missed":
                msg = f"match {match.source_ref} kickoff {kickoff:%Y-%m-%d %H:%M}Z: {kind} missed"
                _log(session, "odds", str(match.match_id), now, "missed", 0, msg)
                out.append(msg)
    session.flush()
    return out


# --------------------------------------------------------------------------- orchestration


def run_capture(
    session: Session, client: ApiFootballClient, comps: list[Competition],
    now: datetime | None = None, daily_budget_s: float = DAILY_BUDGET_S,
    clock: Callable[[], float] = time.monotonic,
) -> CaptureReport:  # fmt: skip
    now = now or datetime.now(UTC)
    started = clock()
    report = CaptureReport(started_at=now)
    start_requests = client.requests_made

    def step(job: str, scope: str, fn: Any) -> Any:
        before = client.requests_made
        try:
            result = fn()
            _log(session, job, scope, now, "ok", client.requests_made - before, str(result))
            session.commit()
            return result
        except QuotaExceeded as exc:
            session.rollback()
            report.errors.append(f"{job} {scope}: quota: {exc}")
            raise
        except Exception as exc:  # one failing competition must not stop the others
            session.rollback()
            report.errors.append(f"{job} {scope}: {type(exc).__name__}: {exc}")
            _log(session, job, scope, now, "error", client.requests_made - before, str(exc))
            session.commit()
            return None

    try:
        confirmed = step("lineups", "all", lambda: capture_lineups(session, client, now)) or []
        odds = step("odds", "all", lambda: capture_odds(session, client, now, set(confirmed)))
        report.actions.update(odds or {})
        report.actions["lineups_confirmed"] += len(confirmed)
        report.missed = step("missed", "all", lambda: detect_missed(session, now)) or []
        for comp in comps:
            scope = str(comp.league_id)
            if clock() - started >= daily_budget_s:
                report.actions["daily_deferred"] += 1  # picked up by the next tick
                continue
            if not _ran_recently(session, "calendar", scope, now):
                n = step(
                    "calendar", scope, lambda c=comp: capture_calendar(session, client, c, now)
                )
                report.actions["calendar"] += 1 if n is not None else 0
            if not _ran_recently(session, "status", scope, now):
                step("status", scope, lambda c=comp: capture_status(session, client, c, now))
            if not _ran_recently(session, "detail", scope, now):
                step("detail", scope, lambda c=comp: capture_detail(session, client, c))
    except QuotaExceeded:
        pass
    report.requests = client.requests_made - start_requests
    return report
