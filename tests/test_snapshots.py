from datetime import UTC, datetime, timedelta

from adatta.capture.snapshots import due_kinds, missed_kinds

K = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)


def test_nothing_due_early() -> None:
    assert due_kinds(K, K - timedelta(days=10), set()) == []


def test_each_window_due_once() -> None:
    assert due_kinds(K, K - timedelta(days=6, hours=12), set()) == ["T-7d"]
    assert due_kinds(K, K - timedelta(hours=23, minutes=50), set()) == ["T-24h"]
    assert due_kinds(K, K - timedelta(hours=23, minutes=50), {"T-24h"}) == []
    assert due_kinds(K, K - timedelta(minutes=50), set()) == ["T-60m"]
    assert due_kinds(K, K - timedelta(minutes=10), set()) == ["close"]


def test_every_window_is_wide_enough_for_a_15_minute_poller() -> None:
    from adatta.capture.snapshots import TIMED

    assert all(open_ - close >= timedelta(minutes=20) for open_, close in TIMED.values())


def test_after_kickoff_nothing_due() -> None:
    assert due_kinds(K, K + timedelta(minutes=1), set()) == []


def test_late_poll_does_not_mislabel_a_missed_window() -> None:
    # a poll at T-30m is past the T-60m window: T-60m is missed, close is not yet due
    now = K - timedelta(minutes=30)
    assert due_kinds(K, now, {"T-7d", "T-24h"}) == []
    assert missed_kinds(K, now, {"T-7d", "T-24h"}) == ["T-60m"]


def test_windows_closed_before_first_sighting_are_not_missed() -> None:
    first_seen = K - timedelta(days=3)  # fixture appeared after the T-7d window closed
    assert missed_kinds(K, K + timedelta(hours=1), set(), first_seen) == [
        "T-24h", "T-60m", "close", "initial", "lineup",
    ]  # fmt: skip


def test_fixture_first_seen_after_kickoff_expects_nothing() -> None:
    # first run of the poller sees matches already played: nothing could have been captured
    assert missed_kinds(K, K + timedelta(hours=3), set(), K + timedelta(hours=1)) == []
