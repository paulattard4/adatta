"""API-Football /fixtures?ids `statistics` block -> per-team stat rows (Phase 1b, D8)."""

from __future__ import annotations

from typing import Any

COUNTS = {
    "Total Shots": "shots_total",
    "Shots on Goal": "shots_on_target",
    "Shots insidebox": "shots_inside_box",
    "Corner Kicks": "corners",
    "Fouls": "fouls",
    "Yellow Cards": "yellow_cards",
    "Red Cards": "red_cards",
}


def _float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).rstrip("%"))
    except ValueError:
        return None


def team_stats(fx: dict[str, Any]) -> list[dict[str, Any]]:
    """One dict per team with a statistics block: team_ref, the typed columns and `stats`
    (every provider type -> raw value). A listed count with a null value is 0 (the provider
    omits zero counts); an unlisted count is None. xG null stays None."""
    out = []
    for block in fx.get("statistics") or []:
        entries = block.get("statistics") or []
        if not entries:
            continue
        raw = {e["type"]: e.get("value") for e in entries if e.get("type")}
        row: dict[str, Any] = {"team_ref": str(block["team"]["id"]), "stats": raw}
        for name, col in COUNTS.items():
            row[col] = (int(_float(raw[name]) or 0)) if name in raw else None
        row["xg"] = _float(raw.get("expected_goals"))
        row["possession"] = _float(raw.get("Ball Possession"))
        out.append(row)
    return out


SHOOTOUT = "Penalty Shootout"  # the comment the provider puts on shootout kicks
NORMAL_TIME = 90  # an event's `elapsed` minute; stoppage time sits in `extra`


def event_goals(fx: dict[str, Any]) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """(normal-time goals, goals in all play) per side (home, away) from the goal events of a
    detail payload: shootout kicks and missed penalties are not goals. None when the payload
    has no events or an event names neither side."""
    events = fx.get("events")
    if not events:
        return None
    home, away = fx["teams"]["home"]["id"], fx["teams"]["away"]["id"]
    nt, total = [0, 0], [0, 0]
    for e in events:
        if e.get("type") != "Goal" or e.get("detail") == "Missed Penalty":
            continue
        if e.get("comments") == SHOOTOUT:
            continue
        team = (e.get("team") or {}).get("id")
        if team not in (home, away):
            return None
        i = 0 if team == home else 1
        total[i] += 1
        if (e.get("time") or {}).get("elapsed", NORMAL_TIME + 1) <= NORMAL_TIME:
            nt[i] += 1
    return (nt[0], nt[1]), (total[0], total[1])


def normal_time_from_events(fx: dict[str, Any]) -> tuple[int, int] | None:
    """The 90' score from the goal events, only when the events' goals add up to the
    provider's total goals (shootout excluded); None otherwise (Stage A, A0)."""
    got = event_goals(fx)
    goals = fx.get("goals") or {}
    if got is None or goals.get("home") is None or goals.get("away") is None:
        return None
    nt, total = got
    return nt if total == (int(goals["home"]), int(goals["away"])) else None


def shootout_kicks(fx: dict[str, Any]) -> list[tuple[str, bool]] | None:
    """The shootout's kicks in the listed order, as (side, scored) with side "home" or
    "away" (Stage B shootout rule). None without shootout events, or when a kick names
    neither side."""
    home, away = fx["teams"]["home"]["id"], fx["teams"]["away"]["id"]
    kicks: list[tuple[str, bool]] = []
    for e in fx.get("events") or []:
        if e.get("type") != "Goal" or e.get("comments") != SHOOTOUT:
            continue
        team = (e.get("team") or {}).get("id")
        if team not in (home, away):
            return None
        kicks.append(("home" if team == home else "away", e.get("detail") != "Missed Penalty"))
    return kicks or None


EXTRA_TIME_LENGTH = 120
SENT_OFF = ("Red Card", "Second Yellow card")  # Card event details that end a player's match
OWN_GOAL = "Own Goal"

# per-player statistic -> (block, field) in `players[].players[].statistics[0]`
PLAYER_STATS = {
    "goals_provider": ("goals", "total"), "assists": ("goals", "assists"),
    "saves": ("goals", "saves"), "conceded": ("goals", "conceded"),
    "shots_total": ("shots", "total"), "shots_on": ("shots", "on"),
    "passes": ("passes", "total"), "key_passes": ("passes", "key"),
    "tackles": ("tackles", "total"), "interceptions": ("tackles", "interceptions"),
    "blocks": ("tackles", "blocks"), "duels": ("duels", "total"), "duels_won": ("duels", "won"),
    "dribbles": ("dribbles", "attempts"), "dribbles_won": ("dribbles", "success"),
    "fouls_committed": ("fouls", "committed"), "fouls_drawn": ("fouls", "drawn"),
    "yellow": ("cards", "yellow"), "red": ("cards", "red"), "offsides": ("offsides", None),
    "pen_scored": ("penalty", "scored"), "pen_missed": ("penalty", "missed"),
    "pen_won": ("penalty", "won"), "pen_committed": ("penalty", "commited"),
    "pen_saved": ("penalty", "saved"),
}  # fmt: skip


def _int(v: Any) -> int | None:
    f = _float(v)
    return None if f is None else int(f)


def _minute(e: dict[str, Any], length: int) -> int | None:
    """An event's match minute on the 0-length scale: stoppage time counts as the minute
    it extends (90+3 is 90), capped at the match length."""
    el = (e.get("time") or {}).get("elapsed")
    return None if el is None else min(int(el), length)


def player_appearances(
    fx: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """One dict per player in a team's lineup (starters and bench) for a finished match,
    and counts of what could not be read (Phase 3, P3-0).

    Minutes come from the events: a starter is on from 0, a substitute from the
    substitution minute; a player leaves at a substitution or a sending-off, else at the
    end (90, or 120 after extra time). A substitution event's two players are told apart
    by who is on the pitch (the provider's player/assist order is not relied on); an event
    where that is unclear is counted and skipped. `minutes_nt` is the part within 90'.
    The provider's own `games.minutes` is kept beside it for comparison, never merged.

    Goals are counted from the goal events (own goals, missed penalties and shootout kicks
    excluded); own goals are counted apart and never credited as goals (spec §7.4). The
    provider's per-player goal total is kept as `goals_provider`.

    Counting statistics: the provider sends null for zero, so a null is 0 for a player who
    played when the statistic is collected for that team in that match (any player of the
    team has a value), and stays null otherwise; a player who did not play gets nulls.
    `rating` is never filled."""
    status = ((fx.get("fixture") or {}).get("status") or {}).get("short")
    length = EXTRA_TIME_LENGTH if status in ("AET", "PEN") else NORMAL_TIME
    counts: dict[str, int] = {"no_player_id": 0, "unclear_sub": 0, "not_in_lineup": 0}
    lineups = fx.get("lineups") or []
    out: dict[tuple[str, str], dict[str, Any]] = {}
    on_pitch: set[str] = set()
    for lu in lineups:
        team = str((lu.get("team") or {}).get("id"))
        for started, entries in ((True, lu.get("startXI")), (False, lu.get("substitutes"))):
            for e in entries or []:
                pl = e.get("player") or {}
                if pl.get("id") is None:
                    counts["no_player_id"] += 1
                    continue
                ref = str(pl["id"])
                out[(team, ref)] = {
                    "team_ref": team, "player_ref": ref, "player_name": pl.get("name") or "",
                    "started": started, "number": _int(pl.get("number")), "pos": pl.get("pos"),
                    "grid": pl.get("grid"), "minute_on": 0 if started else None,
                    "minute_off": None, "sent_off": False, "goals": 0, "own_goals": 0,
                }  # fmt: skip
                if started:
                    on_pitch.add(ref)
    by_player = {ref: row for (_, ref), row in out.items()}
    for e in fx.get("events") or []:
        kind, detail = e.get("type"), e.get("detail")
        m = _minute(e, length)
        a = str((e.get("player") or {}).get("id"))
        if kind == "subst" and m is not None:
            b = str((e.get("assist") or {}).get("id"))
            if a in on_pitch and b not in on_pitch and b in by_player:
                off, on = a, b
            elif b in on_pitch and a not in on_pitch and a in by_player:
                off, on = b, a
            else:
                counts["unclear_sub"] += 1
                continue
            on_pitch.discard(off)
            on_pitch.add(on)
            by_player[off]["minute_off"] = m
            by_player[on]["minute_on"] = m
        elif kind == "Card" and detail in SENT_OFF and a in on_pitch and m is not None:
            on_pitch.discard(a)
            by_player[a]["minute_off"] = m
            by_player[a]["sent_off"] = True
        elif kind == "Goal" and e.get("comments") != SHOOTOUT and a in by_player:
            if detail == OWN_GOAL:
                by_player[a]["own_goals"] += 1
            elif detail != "Missed Penalty":
                by_player[a]["goals"] += 1
    for row in out.values():
        start: int | None = row["minute_on"]
        if start is None:
            row["minutes"] = row["minutes_nt"] = 0
            continue
        end: int = row["minute_off"] if row["minute_off"] is not None else length
        row["minute_off"] = end
        row["minutes"] = max(end - start, 0)
        row["minutes_nt"] = max(min(end, NORMAL_TIME) - min(start, NORMAL_TIME), 0)
    for block in fx.get("players") or []:
        team = str((block.get("team") or {}).get("id"))
        entries = block.get("players") or []
        stats = {}
        for e in entries:
            pl = e.get("player") or {}
            st = (e.get("statistics") or [{}])[0]
            if pl.get("id") is not None:
                stats[str(pl["id"])] = st
        collected = {
            name for name, (blk, field) in PLAYER_STATS.items()
            if any(((st.get(blk) if field is None else (st.get(blk) or {}).get(field))
                    is not None) for st in stats.values())
        }  # fmt: skip
        for ref, st in stats.items():
            if (team, ref) not in out:
                counts["not_in_lineup"] += 1
                continue
            row = out[(team, ref)]
            games = st.get("games") or {}
            row["minutes_provider"] = _int(games.get("minutes"))
            row["rating"] = _float(games.get("rating"))
            row["captain"] = bool(games.get("captain"))
            played = row["minutes"] > 0 or (row["minutes_provider"] or 0) > 0
            for name, (blk, field) in PLAYER_STATS.items():
                v = st.get(blk) if field is None else (st.get(blk) or {}).get(field)
                row[name] = _int(v) if v is not None else (0 if played and name in collected
                                                            else None)  # fmt: skip
    for row in out.values():
        row.setdefault("minutes_provider", None)
        row.setdefault("rating", None)
        row.setdefault("captain", None)
        for name in PLAYER_STATS:
            row.setdefault(name, None)
    return list(out.values()), counts


def event_counts(fx: dict[str, Any]) -> dict[tuple[str, str], dict[str, int]]:
    """(team_ref, player_ref) -> assists, yellow cards and second yellows counted from the
    events (P3-d0 (a) check). An assist is the `assist` player of a goal event that is not
    an own goal, a missed penalty or a shootout kick; a yellow is a "Yellow Card" event, a
    second yellow a "Second Yellow card" event."""
    out: dict[tuple[str, str], dict[str, int]] = {}
    for e in fx.get("events") or []:
        team = str((e.get("team") or {}).get("id"))
        kind, detail = e.get("type"), e.get("detail")
        if kind == "Goal" and detail not in (OWN_GOAL, "Missed Penalty") \
                and e.get("comments") != SHOOTOUT:  # fmt: skip
            who, field = (e.get("assist") or {}).get("id"), "assists"
        elif kind == "Card" and detail in ("Yellow Card", "Second Yellow card"):
            who = (e.get("player") or {}).get("id")
            field = "yellow" if detail == "Yellow Card" else "second_yellow"
        else:
            continue
        if who is not None:
            row = out.setdefault((team, str(who)), {"assists": 0, "yellow": 0, "second_yellow": 0})
            row[field] += 1
    return out


def match_coaches(fx: dict[str, Any]) -> list[dict[str, Any]]:
    """One dict per team whose lineup names a coach: team_ref, coach_ref, coach_name (D12).
    A lineup without a coach id gives no row (absent, never imputed)."""
    out = []
    for lu in fx.get("lineups") or []:
        coach = lu.get("coach") or {}
        if coach.get("id") is None or not (lu.get("team") or {}).get("id"):
            continue
        out.append({"team_ref": str(lu["team"]["id"]), "coach_ref": str(coach["id"]),
                    "coach_name": coach.get("name")})  # fmt: skip
    return out
