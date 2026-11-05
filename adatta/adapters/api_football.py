"""API-Football adapter. The ONLY module that knows this provider's response shape."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import retry, stop_after_attempt, wait_exponential

from adatta.adapters.quota import QuotaPlanner
from adatta.adapters.storage import RawStore, encode_payload, storage_key
from adatta.config import Settings
from adatta.db import models

SOURCE_ID = "api_football"


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
        self._http = http or httpx.Client(
            base_url=settings.api_football_base_url,
            headers={"x-apisports-key": settings.api_football_key},
            timeout=30,
        )

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
        resp = self._get(endpoint, params)
        remaining = resp.headers.get("x-ratelimit-requests-remaining")
        if remaining is not None:
            self._quota.sync_from_header(int(remaining))
        payload: dict[str, Any] = resp.json()
        blob, digest = encode_payload(payload)
        now = datetime.now(UTC)
        key = storage_key(SOURCE_ID, endpoint, now.strftime("%Y-%m-%d"), digest)
        exists = session.scalar(
            select(models.RawPayload.payload_id).where(models.RawPayload.storage_key == key)
        )
        if exists is None:
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

    def fixtures(self, session: Session, league_id: int, season: int) -> list[dict[str, Any]]:
        data = self.fetch(session, "/fixtures", {"league": league_id, "season": season})
        out: list[dict[str, Any]] = data.get("response", [])
        return out
