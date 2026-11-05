import pytest

from adatta.adapters.quota import QuotaExceeded, QuotaPlanner


def test_backfill_reserve_is_kept_for_live() -> None:
    q = QuotaPlanner(daily_limit=10, per_minute_limit=100, reserve_fraction=0.3)
    for _ in range(7):
        q.acquire()
    with pytest.raises(QuotaExceeded):
        q.acquire()  # backfill budget (7) exhausted
    q.acquire(live=True)  # live capture may still use the reserve
    assert q.used_today == 8


def test_daily_limit_hard_stop() -> None:
    q = QuotaPlanner(daily_limit=2, per_minute_limit=100, reserve_fraction=0)
    q.acquire(live=True)
    q.acquire(live=True)
    with pytest.raises(QuotaExceeded):
        q.acquire(live=True)


def test_per_minute_limit_sleeps() -> None:
    t = [0.0]
    slept: list[float] = []
    q = QuotaPlanner(100, 2, 0, clock=lambda: t[0], sleep=lambda s: slept.append(s))
    q.acquire()
    q.acquire()
    q.acquire()
    assert slept and slept[0] == 60


def test_sync_from_header() -> None:
    q = QuotaPlanner(100, 10, 0)
    q.sync_from_header(remaining_today=40)
    assert q.used_today == 60
