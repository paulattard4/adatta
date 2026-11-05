"""Quota planner: daily + per-minute budgets with a reserve for the live capture jobs."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class QuotaExceeded(RuntimeError):
    pass


class QuotaPlanner:
    def __init__(
        self,
        daily_limit: int,
        per_minute_limit: int,
        reserve_fraction: float = 0.3,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.daily_limit = daily_limit
        self.per_minute_limit = per_minute_limit
        self.reserve_fraction = reserve_fraction
        self._clock = clock
        self._sleep = sleep
        self._used_today = 0
        self._recent: deque[float] = deque()

    @property
    def used_today(self) -> int:
        return self._used_today

    def sync_from_header(self, remaining_today: int) -> None:
        """Trust the provider's reported remaining count (x-ratelimit-requests-remaining)."""
        self._used_today = max(self._used_today, self.daily_limit - remaining_today)

    def backfill_budget_left(self) -> int:
        """Requests backfill may still use today (the reserve is kept for live capture)."""
        allowed = int(self.daily_limit * (1 - self.reserve_fraction))
        return max(0, allowed - self._used_today)

    def acquire(self, *, live: bool = False) -> None:
        if self._used_today >= self.daily_limit:
            raise QuotaExceeded("daily limit reached")
        if not live and self.backfill_budget_left() <= 0:
            raise QuotaExceeded("backfill budget exhausted (reserve kept for live capture)")
        now = self._clock()
        while self._recent and now - self._recent[0] >= 60:
            self._recent.popleft()
        if len(self._recent) >= self.per_minute_limit:
            self._sleep(max(0.0, 60 - (now - self._recent[0])))
            self._recent.popleft()
        self._recent.append(self._clock())
        self._used_today += 1
