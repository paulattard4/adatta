"""Odds milestone schedule (spec §12.3, §15.2): which snapshot kinds are due or missed.

Each timed kind has a window [kickoff - open, kickoff - close). A capture inside the window
counts; once the window has passed without one, the kind is *missed*. It is never captured
late under the wrong label, because CLV compares specific decision times with the close.

Windows are sized for a ~15-minute poller: every window is at least 20 minutes, so one tick
always lands inside it unless the scheduler skips or is delayed. That case is exactly what
the missed-capture alert reports. The actual observed_at of every capture is stored, so
analysis uses true timestamps, not nominal ones.

Untimed kinds:
- initial: the first time a market is seen for the fixture (tried daily up to kickoff).
- lineup:  the poll on which the confirmed XI is first seen (§12.3 "lineup-confirmed").
  §12.3 merges lineup-confirmed with T-60m, while §15.2 lists it separately; both are
  captured, which is the superset.
"""

from __future__ import annotations

from datetime import datetime, timedelta

TIMED: dict[str, tuple[timedelta, timedelta]] = {
    # kind: (window opens before kickoff, window closes before kickoff)
    "T-7d": (timedelta(days=7), timedelta(days=6)),
    "T-24h": (timedelta(hours=24), timedelta(hours=20)),
    "T-60m": (timedelta(minutes=60), timedelta(minutes=40)),
    "close": (timedelta(minutes=20), timedelta(0)),
}
UNTIMED = ("initial", "lineup")
KINDS: tuple[str, ...] = ("initial", *TIMED, "lineup")

INITIAL_HORIZON = timedelta(days=14)  # look for a first market this far ahead
LINEUP_POLL_FROM = timedelta(minutes=75)  # start polling for lineups this long before kickoff


def due_kinds(kickoff: datetime, now: datetime, done: set[str]) -> list[str]:
    """Timed kinds whose window contains `now` and that have no capture row yet."""
    return [
        k
        for k, (open_, close) in TIMED.items()
        if k not in done and kickoff - open_ <= now < kickoff - close
    ]


def missed_kinds(
    kickoff: datetime, now: datetime, done: set[str], first_seen: datetime | None = None
) -> list[str]:
    """Kinds whose window has passed with no capture row.

    `first_seen` is when the fixture entered the calendar. A window that had already closed
    by then could never have been captured, so it isn't reported as missed.
    """
    out = []
    for k, (_open, close) in TIMED.items():
        if k in done or now < kickoff - close:
            continue
        if first_seen is not None and first_seen >= kickoff - close:
            continue
        out.append(k)
    # Untimed kinds can only be expected for a fixture seen before kickoff.
    if now >= kickoff and (first_seen is None or first_seen < kickoff):
        out += [k for k in UNTIMED if k not in done]
    return out
