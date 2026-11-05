"""API-Football payload checks for the capability audit (spec §4.4).

This is the only place that knows where each capability lives in API-Football payloads.
`declared_flags` reads what /leagues *claims*; `observed_flags` reads what a /fixtures?ids=
detail payload *contains*. The audit compares the two, because they differ.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

# Spec §3.4 capabilities measurable from a fixture-detail payload.
OBSERVED_CAPABILITIES: tuple[str, ...] = (
    "has_lineups",
    "has_player_match_stats",
    "has_shot_stats",
    "has_xg",
    "has_corners_data",
    "has_cards_data",
    "has_half_split_stats",
    "has_substitution_timestamps",
    "has_referee_history",
    "has_event_coordinates",
    "has_physical_load_data",
    "has_attendance_data",
)

# Spec §3.4 player-prop capabilities. These are modelling-policy decisions (data depth plus a
# validated Layer 4), not payload facts, so the audit records them as 'unknown' until Layer 4.
POLICY_CAPABILITIES: tuple[str, ...] = (
    "supports_lineup_strength_adjustment",
    "supports_player_goal_props",
    "supports_player_shot_props",
    "supports_player_discipline_props",
)

FINISHED = {"FT", "AET", "PEN"}

# /leagues coverage flag -> capabilities it claims to cover. Capabilities with no declared
# flag map to None: xG has no flag of its own (statistics_fixtures is true for leagues with no
# xG at all), nor do referee, half-time score, coordinates...
_DECLARED: dict[str, tuple[str, ...]] = {
    "has_lineups": ("lineups",),
    "has_player_match_stats": ("statistics_players",),
    "has_shot_stats": ("statistics_fixtures",),
    "has_corners_data": ("statistics_fixtures",),
    "has_cards_data": ("events",),
    "has_substitution_timestamps": ("events",),
}


def declared_flags(season_coverage: dict[str, Any] | None) -> dict[str, bool | None]:
    """Map one /leagues season `coverage` object to capability -> declared bool (None = no flag)."""
    fx = (season_coverage or {}).get("fixtures") or {}
    out: dict[str, bool | None] = {}
    for cap in OBSERVED_CAPABILITIES:
        keys = _DECLARED.get(cap)
        out[cap] = None if keys is None or not fx else all(bool(fx.get(k)) for k in keys)
    return out


def _stat_map(team_stats: dict[str, Any]) -> dict[str, Any]:
    return {s.get("type"): s.get("value") for s in team_stats.get("statistics") or []}


def _team_stat_present(stats: list[dict[str, Any]], names: Iterable[str]) -> bool:
    """Stat type listed for both teams with a non-null value for at least one.

    The provider sends null for a zero count on some stats, so requiring both non-null would
    undercount; requiring the type on both sides rejects half-filled payloads.
    """
    if len(stats) < 2:
        return False
    maps = [_stat_map(t) for t in stats[:2]]
    for name in names:
        if all(name in m for m in maps) and any(m[name] is not None for m in maps):
            return True
    return False


def _team_count_listed(stats: list[dict[str, Any]], name: str) -> bool:
    """Count stat type listed for both teams. Null means zero for counts such as cards
    (verified: zero-card matches list 'Yellow Cards' as null with no Card events)."""
    if len(stats) < 2:
        return False
    return all(name in _stat_map(t) for t in stats[:2])


def _player_entries(fx: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for team in fx.get("players") or []:
        for p in team.get("players") or []:
            out.extend(p.get("statistics") or [])
    return out


def observed_flags(fx: dict[str, Any]) -> dict[str, bool]:
    """Which capabilities one fixture-detail payload actually carries."""
    fixture = fx.get("fixture") or {}
    stats = fx.get("statistics") or []
    lineups = fx.get("lineups") or []
    events = fx.get("events") or []
    player_stats = _player_entries(fx)
    halftime = (fx.get("score") or {}).get("halftime") or {}

    lineups_ok = len(lineups) == 2 and all(len(t.get("startXI") or []) == 11 for t in lineups)
    players_ok = len(fx.get("players") or []) == 2 and any(
        (s.get("games") or {}).get("minutes") is not None for s in player_stats
    )
    cards_events = any(e.get("type") == "Card" for e in events)
    subs_timed = any(
        e.get("type") == "subst" and (e.get("time") or {}).get("elapsed") is not None
        for e in events
    )
    coords = any(any(k in e for k in ("x", "y", "coordinates", "location")) for e in events)
    physical = any(any(k in s for k in ("distance", "sprints", "physical")) for s in player_stats)

    return {
        "has_lineups": lineups_ok,
        "has_player_match_stats": players_ok,
        "has_shot_stats": _team_stat_present(stats, ["Total Shots"]),
        "has_xg": _team_stat_present(stats, ["expected_goals"]),
        "has_corners_data": _team_stat_present(stats, ["Corner Kicks"]),
        "has_cards_data": cards_events or _team_count_listed(stats, "Yellow Cards"),
        "has_half_split_stats": halftime.get("home") is not None
        and halftime.get("away") is not None,
        "has_substitution_timestamps": subs_timed,
        "has_referee_history": bool(fixture.get("referee")),
        "has_event_coordinates": coords,
        "has_physical_load_data": physical,
        "has_attendance_data": fixture.get("attendance") is not None,
    }


def finished_fixtures(fixtures: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """(fixture id, ISO kickoff) for finished fixtures in a /fixtures?league&season list."""
    out = [
        (int(f["fixture"]["id"]), str(f["fixture"]["date"]))
        for f in fixtures
        if f["fixture"]["status"]["short"] in FINISHED
    ]
    return sorted(out)


def first_kickoff(fixtures: list[dict[str, Any]]) -> datetime | None:
    dates = [
        datetime.fromisoformat(f["fixture"]["date"])
        for f in fixtures
        if (f.get("fixture") or {}).get("date")
    ]
    return min(dates) if dates else None


def season_coverage(seasons: list[dict[str, Any]], year: int) -> dict[str, Any] | None:
    for s in seasons:
        if s.get("year") == year:
            cov: dict[str, Any] | None = s.get("coverage")
            return cov
    return None


def fixture_ids(details: list[dict[str, Any]]) -> list[int]:
    return [int(d["fixture"]["id"]) for d in details]
