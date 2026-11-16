"""Rebuild normalised detail tables from the raw store (principle 4: raw payloads are kept so
any normalised table can be rebuilt). Makes no API requests."""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_stats as stats_adapter
from adatta.adapters import normalize
from adatta.adapters.storage import RawStore
from adatta.db import models

BATCH_KEYS = 50  # payload files per commit
EXTRA_TIME = ("AET", "PEN")  # finished after extra time


def _pending(session: Session) -> dict[str, list[tuple[int, str]]]:
    """storage_key -> [(match_id, fixture ref)] for returned details with no stat rows."""
    D, M, T = models.MatchDetail, models.Match, models.TeamMatchStat
    has_stats = select(T.match_id).distinct()
    rows = session.execute(
        select(D.storage_key, D.match_id, M.source_ref)
        .join(M, M.match_id == D.match_id)
        .where(D.flags["returned"].as_boolean(), D.match_id.not_in(has_stats))
    ).all()
    out: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for key, mid, ref in rows:
        out[key].append((mid, ref))
    return out


def load_team_stats(session: Session, store: RawStore, workers: int = 8) -> dict[str, int]:
    """Parse team_match_stat rows from stored payloads for every match that lacks them.
    Resumable: commits every BATCH_KEYS files and skips matches already loaded."""
    pending = _pending(session)
    keys = sorted(pending)
    counts = {"files": 0, "matches": 0, "rows": 0, "no_stats": 0}

    def fetch(key: str) -> tuple[str, dict[str, Any]]:
        return key, json.loads(gzip.decompress(store.get(key)))

    with ThreadPoolExecutor(workers) as pool:
        for i in range(0, len(keys), BATCH_KEYS):
            for key, payload in pool.map(fetch, keys[i : i + BATCH_KEYS]):
                by_ref = {str(fx["fixture"]["id"]): fx for fx in payload.get("response", [])}
                detail_time = {
                    d.match_id: d.observed_at
                    for d in session.scalars(
                        select(models.MatchDetail).where(
                            models.MatchDetail.match_id.in_([m for m, _ in pending[key]])
                        )
                    )
                }
                # one team lookup per file, so each match below costs no extra round trip
                teams = normalize.teams_by_ref(
                    session,
                    {r["team_ref"]: r["team_ref"] for fx in by_ref.values()
                     for r in stats_adapter.team_stats(fx)},
                )  # fmt: skip
                for mid, ref in pending[key]:
                    fx = by_ref.get(ref)
                    rows = stats_adapter.team_stats(fx) if fx else []
                    if not rows:
                        counts["no_stats"] += 1
                        continue
                    counts["rows"] += normalize.ingest_team_stats(
                        session, mid, rows, key, detail_time[mid], new_match=True, teams=teams
                    )
                    counts["matches"] += 1
                counts["files"] += 1
            session.commit()
    return counts


def load_match_coaches(session: Session, store: RawStore, workers: int = 8) -> dict[str, int]:
    """Parse match_coach rows from stored payloads for every returned detail lacking them
    (D12). Resumable like load_team_stats; matches without a coach in either lineup are
    re-read on the next run, which costs reads only."""
    D, M, C = models.MatchDetail, models.Match, models.MatchCoach
    rows = session.execute(
        select(D.storage_key, D.match_id, M.source_ref, D.observed_at)
        .join(M, M.match_id == D.match_id)
        .where(D.flags["returned"].as_boolean(),
               D.match_id.not_in(select(C.match_id).distinct()))
    ).all()  # fmt: skip
    pending: dict[str, list[tuple[int, str, Any]]] = defaultdict(list)
    for key, mid, ref, seen in rows:
        pending[key].append((mid, ref, seen))
    keys = sorted(pending)
    counts = {"files": 0, "matches": 0, "rows": 0, "no_coach": 0}

    def fetch(key: str) -> tuple[str, dict[str, Any]]:
        return key, json.loads(gzip.decompress(store.get(key)))

    with ThreadPoolExecutor(workers) as pool:
        for i in range(0, len(keys), BATCH_KEYS):
            for key, payload in pool.map(fetch, keys[i : i + BATCH_KEYS]):
                by_ref = {str(fx["fixture"]["id"]): fx for fx in payload.get("response", [])}
                parsed = {ref: stats_adapter.match_coaches(by_ref[ref])
                          for _, ref, _ in pending[key] if ref in by_ref}  # fmt: skip
                teams = normalize.teams_by_ref(
                    session, {r["team_ref"]: r["team_ref"] for rs in parsed.values() for r in rs}
                )
                for mid, ref, seen in pending[key]:
                    found = parsed.get(ref) or []
                    if not found:
                        counts["no_coach"] += 1
                        continue
                    counts["rows"] += normalize.ingest_match_coaches(
                        session, mid, found, key, seen, new_match=True, teams=teams
                    )
                    counts["matches"] += 1
                counts["files"] += 1
            session.commit()
    return counts


def load_venues(
    session: Session, store: RawStore, league_country: dict[int, str], seasons: set[int] | None
) -> dict[str, int]:
    """Link matches to venues from the newest stored /fixtures?league&season list of each
    competition-season (only venue rows and match.venue_id are written; no API requests)."""
    R = models.RawPayload
    rows = session.execute(
        select(R.params, R.storage_key, R.fetched_at)
        .where(R.source_id == normalize.SOURCE_ID, R.endpoint == "/fixtures")
        .order_by(R.fetched_at)
    ).all()
    newest: dict[tuple[int, int], str] = {}
    for params, key, _ in rows:  # ascending, so the newest list per season wins
        if "league" in params and "season" in params:
            lg, yr = int(params["league"]), int(params["season"])
            if lg in league_country and (seasons is None or yr in seasons):
                newest[(lg, yr)] = key
    counts: dict[str, int] = defaultdict(int)
    for (lg, _), key in sorted(newest.items()):
        fixtures: list[dict[str, Any]] = json.loads(gzip.decompress(store.get(key)))["response"]
        keys = {str(fx["fixture"]["id"]): normalize.venue_key(fx) for fx in fixtures}
        venues = normalize.venues_by_key(
            session, {(k[0], k[1]): k[2] for k in keys.values() if k is not None},
            league_country[lg],
        )  # fmt: skip
        matches = normalize.matches_by_ref(session, keys)
        for ref, k in keys.items():
            m: models.Match | None = matches.get(ref)
            if k is None or m is None:
                counts["no_venue" if k is None else "unknown_match"] += 1
                continue
            vid = venues[(k[0], k[1])].venue_id
            if m.venue_id != vid:
                m.venue_id = vid
                counts["linked"] += 1
        counts["seasons"] += 1
        session.commit()
    counts["venues"] = int(session.scalar(select(func.count()).select_from(models.Venue)) or 0)
    return dict(counts)


def repair_normal_time(
    session: Session, store: RawStore, now: datetime | None = None
) -> dict[str, Any]:
    """Stage A, A0 (a): the 90' score of every finished match that went to extra time (or is
    finished without a 90' score: the provider marks some extra-time ties FT) comes from the goal
    events of its stored detail when they add up to the provider's total
    (`api_football_stats.normal_time_from_events`), otherwise from the provider's split;
    otherwise it is unscorable and listed, never imputed. A changed score is a new
    `match_result` version (nt_basis "events", ET = total minus 90'), the old one superseded.
    Returns counts and the changed and unscorable match ids; the caller commits."""
    now = now or datetime.now(UTC)
    M, D, R = models.Match, models.MatchDetail, models.MatchResult
    no_nt = select(R.match_id).where(
        R.superseded_at.is_(None), or_(R.home_goals_nt.is_(None), R.away_goals_nt.is_(None))
    )
    rows = session.execute(
        select(M.match_id, M.source_ref, D.storage_key)
        .outerjoin(D, (D.match_id == M.match_id) & D.flags["returned"].as_boolean())
        .where(or_(M.status.in_(EXTRA_TIME),
                   M.status.in_(normalize.FINISHED) & M.match_id.in_(no_nt)))
    ).all()  # fmt: skip
    current = {
        r.match_id: r
        for part in normalize.chunks([r[0] for r in rows])
        for r in session.scalars(select(R).where(R.match_id.in_(part), R.superseded_at.is_(None)))
    }  # fmt: skip
    pending: dict[str, list[tuple[int, str]]] = defaultdict(list)
    out: dict[str, Any] = {"matches": len(rows), "changed": [], "unscorable": [],
                           "no_result": 0, "agree": 0, "provider_kept": 0}  # fmt: skip
    for mid, ref, key in rows:
        if mid not in current:
            out["no_result"] += 1
        elif key is None:
            _keep_provider(out, current[mid])
        else:
            pending[key].append((mid, ref))
    for key in sorted(pending):
        payload = json.loads(gzip.decompress(store.get(key)))
        by_ref = {str(fx["fixture"]["id"]): fx for fx in payload.get("response", [])}
        for mid, ref in pending[key]:
            cur, fx = current[mid], by_ref.get(ref)
            ev = stats_adapter.normal_time_from_events(fx) if fx is not None else None
            if ev is None or fx is None:
                _keep_provider(out, cur)
                continue
            total = (int(fx["goals"]["home"]), int(fx["goals"]["away"]))
            et = (total[0] - ev[0], total[1] - ev[1])
            if (cur.home_goals_nt, cur.away_goals_nt, cur.home_goals_et, cur.away_goals_et) == (
                ev + et
            ):
                out["agree"] += 1
                continue
            cur.superseded_at = cur.valid_to = now
            session.add(R(
                match_id=mid, source_id=cur.source_id, published_at=cur.published_at,
                observed_at=now, valid_from=now, home_goals_ht=cur.home_goals_ht,
                away_goals_ht=cur.away_goals_ht, home_goals_nt=ev[0], away_goals_nt=ev[1],
                home_goals_et=et[0], away_goals_et=et[1], home_pens=cur.home_pens,
                away_pens=cur.away_pens, nt_basis=normalize.NT_EVENTS,
            ))  # fmt: skip
            out["changed"].append((mid, (cur.home_goals_nt, cur.away_goals_nt), ev))
    session.flush()
    return out


def _keep_provider(out: dict[str, Any], cur: models.MatchResult) -> None:
    if cur.home_goals_nt is None or cur.away_goals_nt is None:
        out["unscorable"].append(cur.match_id)
    else:
        out["provider_kept"] += 1
