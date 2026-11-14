"""API-Football adapter. The ONLY module that knows this provider's response shape."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import retry, stop_after_attempt, wait_exponential

from adatta.adapters.normalize import ensure_source
from adatta.adapters.quota import QuotaExceeded, QuotaPlanner
from adatta.adapters.storage import RawStore, encode_payload, storage_key
from adatta.config import Settings
from adatta.db import models

SOURCE_ID = "api_football"
IDS_BATCH_MAX = 20


@dataclass(frozen=True)
class LiveState:
    """One match in the provider's live feed: display only, never normalised into a table."""

    fixture_id: int
    status: str  # provider short status: 1H, HT, 2H, ET, BT, P, SUSP, INT, LIVE, ...
    elapsed: int | None  # minutes played
    extra: int | None  # added-time minutes, when the provider sends them
    home_goals: int | None
    away_goals: int | None


class ProviderError(RuntimeError):
    """The provider answered 200 but reported an error in the body (its `errors` field)."""


class ProviderQuotaExceeded(QuotaExceeded, ProviderError):
    """The provider's own request limit (daily or per minute), reported in the body: callers
    stop as they do on the planner's budget."""


# Keys of the body `errors` object that mean the account's request limit, not a bad request.
LIMIT_ERRORS = frozenset({"requests", "rateLimit"})


def check_errors(payload: dict[str, Any], what: str) -> None:
    """Raise if the body reports an error: a 200 with `errors` must never read as an empty
    answer (no fixtures, no detail, nothing in play). An empty `[]` or `{}` is normal."""
    errors = payload.get("errors")
    if not errors:
        return
    if isinstance(errors, dict) and LIMIT_ERRORS & errors.keys():
        raise ProviderQuotaExceeded(f"{what}: {errors}")
    raise ProviderError(f"{what}: {errors}")


def parse_live(payload: dict[str, Any], league_ids: Iterable[int]) -> list[LiveState]:
    """/fixtures?live=all response -> one LiveState per fixture in these leagues. A reported
    error raises: it must not read as "nothing in play"."""
    check_errors(payload, "live feed")
    leagues = set(league_ids)
    out = []
    for item in payload.get("response") or []:
        fx = item.get("fixture") or {}
        if fx.get("id") is None or (item.get("league") or {}).get("id") not in leagues:
            continue
        st, goals = fx.get("status") or {}, item.get("goals") or {}
        out.append(
            LiveState(
                fixture_id=int(fx["id"]), status=str(st.get("short") or ""),
                elapsed=st.get("elapsed"), extra=st.get("extra"),
                home_goals=goals.get("home"), away_goals=goals.get("away"),
            )
        )  # fmt: skip
    return out


class ApiFootballClient:
    def __init__(
        self,
        settings: Settings,
        store: RawStore,
        quota: QuotaPlanner,
        http: httpx.Client | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._store = store
        self._quota = quota
        self._clock = clock
        self._requests_made = 0
        self.last_storage_key: str | None = None
        # When the last response arrived: the observed_at of whatever it carried (principle 2).
        self.last_response_at: datetime | None = None
        self._http = http or httpx.Client(
            base_url=settings.api_football_base_url,
            headers={"x-apisports-key": settings.api_football_key},
            timeout=30,
        )

    @property
    def requests_made(self) -> int:
        """Requests this client instance sent (the quota planner tracks account-wide usage)."""
        return self._requests_made

    @property
    def quota_used_today(self) -> int:
        """Account-wide usage today, as last reported by the provider's rate-limit header."""
        return self._quota.used_today

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20), reraise=True)
    def _get(self, endpoint: str, params: dict[str, Any]) -> httpx.Response:
        resp = self._http.get(endpoint, params=params)
        resp.raise_for_status()
        return resp

    def fetch(
        self, session: Session, endpoint: str, params: dict[str, Any], *, live: bool = False
    ) -> dict[str, Any]:
        """Fetch, persist the raw payload (object store + index row), return parsed JSON.

        A body that reports an error raises before anything is stored: it builds no table,
        and a replay must not read it as an empty answer."""
        self._quota.acquire(live=live)
        self._requests_made += 1
        resp = self._get(endpoint, params)
        self.last_response_at = self._clock()
        remaining = resp.headers.get("x-ratelimit-requests-remaining")
        if remaining is not None:
            self._quota.sync_from_header(int(remaining))
        payload: dict[str, Any] = resp.json()
        check_errors(payload, f"{endpoint} ({', '.join(sorted(params))})")  # names, not ids
        blob, digest = encode_payload(payload)
        key = storage_key(SOURCE_ID, endpoint, self.last_response_at.strftime("%Y-%m-%d"), digest)
        self.last_storage_key = key
        exists = session.scalar(
            select(models.RawPayload.payload_id).where(models.RawPayload.storage_key == key)
        )
        if exists is None:
            ensure_source(session)  # FK target for raw_payload.source_id
            self._store.put(key, blob)
            session.add(
                models.RawPayload(
                    source_id=SOURCE_ID,
                    endpoint=endpoint,
                    params=params,
                    storage_key=key,
                    content_hash=digest,
                )
            )
            session.flush()
        return payload

    def live_fixtures(self, league_ids: Iterable[int]) -> list[LiveState]:
        """/fixtures?live=all, kept to these leagues: every match in play, in one request (the
        provider rejects a single league id, so it is asked for all and filtered here). A
        display feed for the dashboard: through the quota planner like every request, but one
        attempt (the caller shows the last good state) and no raw payload kept, because
        nothing is normalised from it (principle 4 keeps the payloads that build tables)."""
        self._quota.acquire(live=True)
        self._requests_made += 1
        resp = self._http.get("/fixtures", params={"live": "all"})
        resp.raise_for_status()
        self.last_response_at = self._clock()
        remaining = resp.headers.get("x-ratelimit-requests-remaining")
        if remaining is not None:
            self._quota.sync_from_header(int(remaining))
        return parse_live(resp.json(), league_ids)

    def status(self, session: Session) -> dict[str, Any]:
        """Cheap connectivity/plan check (/status)."""
        return self.fetch(session, "/status", {}, live=True)

    def fixtures(
        self, session: Session, league_id: int, season: int, *, live: bool = False
    ) -> list[dict[str, Any]]:
        data = self.fetch(session, "/fixtures", {"league": league_id, "season": season}, live=live)
        out: list[dict[str, Any]] = data.get("response", [])
        return out

    def league_seasons(
        self, session: Session, league_id: int, *, live: bool = False
    ) -> list[dict[str, Any]]:
        """/leagues?id=: every season of one league, each with its declared `coverage` object."""
        data = self.fetch(session, "/leagues", {"id": league_id}, live=live)
        resp: list[dict[str, Any]] = data.get("response", [])
        seasons: list[dict[str, Any]] = resp[0].get("seasons", []) if resp else []
        return seasons

    def fixtures_by_ids(
        self, session: Session, ids: list[int], *, live: bool = False
    ) -> list[dict[str, Any]]:
        """/fixtures?ids=a-b-c: full detail (events, lineups, statistics, players) per fixture.

        The provider caps a batch at IDS_BATCH_MAX ids [verify]; callers must chunk.
        """
        if not ids:
            return []
        if len(ids) > IDS_BATCH_MAX:
            raise ValueError(f"at most {IDS_BATCH_MAX} ids per call, got {len(ids)}")
        data = self.fetch(session, "/fixtures", {"ids": "-".join(str(i) for i in ids)}, live=live)
        out: list[dict[str, Any]] = data.get("response", [])
        return out

    def odds(self, session: Session, fixture_id: int) -> dict[str, Any] | None:
        """/odds?fixture=: pre-match odds for one fixture (all bookmakers), or None if not
        offered. History is not retained by the provider, so this is capture-only."""
        data = self.fetch(session, "/odds", {"fixture": fixture_id}, live=True)
        resp: list[dict[str, Any]] = data.get("response", [])
        return resp[0] if resp else None

    def injuries(self, session: Session, league_id: int, season: int) -> list[dict[str, Any]]:
        """/injuries?league&season: per-fixture absences (Missing Fixture / Questionable)."""
        data = self.fetch(session, "/injuries", {"league": league_id, "season": season}, live=True)
        out: list[dict[str, Any]] = data.get("response", [])
        return out
