"""Live feed for the dashboard's "In play" view (docs/DASHBOARD_TASKS.md, option 1).

Shows the live score beside the pre-match projection; in-play recalculation stays out of
scope (spec §20.1). The provider is asked at most once per MIN_INTERVAL however many viewers
poll, and at most DAILY_CAP times per UTC day; a failed or capped request leaves the last
good state in place with a note saying so."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx

from adatta.adapters.api_football import LiveState, ProviderError
from adatta.adapters.quota import QuotaExceeded

MIN_INTERVAL = timedelta(seconds=60)
DAILY_CAP = 500  # dashboard requests per UTC day: under 7% of the 7,500 quota
WINDOW_BEFORE = timedelta(hours=4)  # a bundle match that kicked off this recently may be live
WINDOW_AHEAD = timedelta(minutes=5)  # or kicks off this soon


@dataclass
class Snapshot:
    states: list[LiveState]
    fetched_at: datetime | None  # when the provider answered; None if it never has
    note: str | None  # why the state may be stale or missing


@dataclass
class LiveFeed:
    fetch: Callable[[list[int]], list[LiveState]]
    min_interval: timedelta = MIN_INTERVAL
    daily_cap: int = DAILY_CAP
    _states: list[LiveState] = field(default_factory=list)
    _fetched_at: datetime | None = None
    _tried_at: datetime | None = None
    _note: str | None = None
    _day: date | None = None
    _used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def used_today(self) -> int:
        return self._used

    def get(self, now: datetime, league_ids: list[int]) -> Snapshot:
        with self._lock:
            if self._tried_at is None or now - self._tried_at >= self.min_interval:
                self._refresh(now, league_ids)
            return Snapshot(list(self._states), self._fetched_at, self._note)

    def _refresh(self, now: datetime, league_ids: list[int]) -> None:
        if self._day != now.date():
            self._day, self._used = now.date(), 0
        if self._used >= self.daily_cap:
            self._note = f"daily limit of {self.daily_cap} live requests reached"
            return
        self._tried_at = now
        self._used += 1
        try:
            states = self.fetch(league_ids)
        except (httpx.HTTPError, QuotaExceeded, ProviderError) as exc:
            self._note = f"live feed unavailable ({type(exc).__name__})"
            return
        self._states, self._fetched_at, self._note = states, now, None
