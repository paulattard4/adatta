"""API-Football adapter. The ONLY module that knows this provider's response shape."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import retry, stop_after_attempt, wait_exponential

from adatta.adapters.normalize import ensure_source
from adatta.adapters.quota import QuotaPlanner
from adatta.adapters.storage import RawStore, encode_payload, storage_key
from adatta.config import Settings
from adatta.db import models

SOURCE_ID = "api_football"
IDS_BATCH_MAX = 20


class ApiFootballClient:
    def __init__(
        self,
        settings: Settings,
        store: RawStore,
        quota: QuotaPlanner,
        http: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._quota = quota
        self._requests_made = 0
        self.last_storage_key: str | None = None
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
        """Fetch, persist the raw payload (object store + index row), return parsed JSON."""
        self._quota.acquire(live=live)
        self._requests_made += 1
        resp = self._get(endpoint, params)
        remaining = resp.headers.get("x-ratelimit-requests-remaining")
        if remaining is not None:
            self._quota.sync_from_header(int(remaining))
        payload: dict[str, Any] = resp.json()
        blob, digest = encode_payload(payload)
        now = datetime.now(UTC)
        key = storage_key(SOURCE_ID, endpoint, now.strftime("%Y-%m-%d"), digest)
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
