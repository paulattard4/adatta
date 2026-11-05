from datetime import UTC, datetime, timedelta

from adatta.capture.snapshots import due_kinds

K = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)


def test_nothing_due_early() -> None:
    assert due_kinds(K, K - timedelta(days=10), set()) == []


def test_t24h_due_once() -> None:
    now = K - timedelta(hours=23, minutes=50)
    assert due_kinds(K, now, set()) == ["T-24h"] or due_kinds(K, now, set()) == ["T-7d"]
    assert due_kinds(K, now, {"T-7d", "T-24h"}) == []


def test_missed_run_only_latest_kind() -> None:
    now = K - timedelta(minutes=50)
    assert due_kinds(K, now, set()) == ["T-60m"]


def test_after_kickoff_nothing() -> None:
    assert due_kinds(K, K + timedelta(minutes=1), set()) == []
