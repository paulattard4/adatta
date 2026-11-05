"""Snapshot capture: decide which odds/lineup snapshots are due for upcoming matches.

Designed for a 10-15 minute GitHub Actions poll: each run captures whatever is due and has
not yet been captured. Odds are stored for evaluation only and never used as model features.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# snapshot kind -> time before kickoff at which it is due. 'initial' and 'close' are handled
# separately (first sighting of the market, and the last pre-kickoff capture).
OFFSETS: dict[str, timedelta] = {
    "T-7d": timedelta(days=7),
    "T-24h": timedelta(hours=24),
    "T-60m": timedelta(minutes=60),
    "close": timedelta(minutes=5),
}


def due_kinds(kickoff: datetime, now: datetime, already: set[str]) -> list[str]:
    """Kinds whose due time has passed, that have not been captured, and kickoff not yet passed."""
    if now >= kickoff:
        return []
    due = [k for k, off in OFFSETS.items() if now >= kickoff - off and k not in already]
    # If several are overdue (e.g. a missed run), only the most recent one is still meaningful.
    due.sort(key=lambda k: OFFSETS[k])
    return due[:1]
