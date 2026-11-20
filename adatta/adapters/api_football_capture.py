"""API-Football payload parsing for snapshot capture (task 5). Provider shape lives here only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Markets normalised into odds_snapshot. Everything else stays in the raw payload (R2) and can
# be rebuilt later: one snapshot carries ~3,700 prices across ~10 books, too many to normalise.
# Phase 1a markets (spec §19.3): 1X2, over/under 2.5, both teams to score.
NORMALISED_MARKETS: dict[str, str] = {
    "Match Winner": "1X2",
    "Goals Over/Under": "OU",
    "Both Teams Score": "BTTS",
}
OU_LINES: frozenset[float] = frozenset({2.5})

NOT_PLAYED = {"TBD", "NS", "PST"}  # statuses that still have a pre-match window
NO_MATCH = {"PST", "CANC", "ABD", "AWD", "WO"}  # no kickoff will happen at this time


@dataclass(frozen=True)
class FixtureState:
    fixture_id: int
    kickoff: datetime
    status: str
    referee: str | None
    venue_ref: str | None
    round: str | None


@dataclass(frozen=True)
class Price:
    bookmaker: str
    market: str
    line: float | None
    selection: str
    decimal_odds: float


@dataclass(frozen=True)
class Absence:
    player_ref: str
    player_name: str
    team_ref: str
    team_name: str
    fixture_id: int
    fixture_kickoff: datetime
    status: str  # missing / questionable
    reason: str | None


@dataclass(frozen=True)
class Lineup:
    team_ref: str
    formation: str | None
    coach_ref: str | None
    coach_name: str | None
    starters: list[dict[str, Any]]
    substitutes: list[dict[str, Any]]


def current_season(seasons: list[dict[str, Any]]) -> int | None:
    years = [int(s["year"]) for s in seasons if s.get("current")]
    return max(years) if years else None


def fixture_state(fx: dict[str, Any]) -> FixtureState:
    f = fx["fixture"]
    venue = f.get("venue") or {}
    return FixtureState(
        fixture_id=int(f["id"]),
        kickoff=datetime.fromisoformat(f["date"]),
        status=f["status"]["short"],
        referee=f.get("referee") or None,
        venue_ref=str(venue["id"]) if venue.get("id") is not None else None,
        round=(fx.get("league") or {}).get("round"),
    )


def _line(value: str) -> tuple[str, float | None]:
    """'Over 2.5' -> ('Over', 2.5); 'Home' -> ('Home', None)."""
    head, _, tail = value.rpartition(" ")
    try:
        return head, float(tail)
    except ValueError:
        return value, None


def odds_published_at(odds: dict[str, Any]) -> datetime | None:
    upd = odds.get("update")
    return datetime.fromisoformat(upd) if upd else None


SCORER_MARKET = "Anytime Goal Scorer"  # P3-e (f): read from kept raw snapshots, Edge only


def parse_scorer_odds(payload: dict[str, Any]) -> list[Price]:
    """Anytime goalscorer prices from a stored /odds payload (raw snapshot), one Price per
    bookmaker x player (selection = the provider's player name). Never a model input
    (principle 1): read only for P3-e's market comparison."""
    out: list[Price] = []

    def items(o: Any) -> Any:
        if isinstance(o, dict):
            if "bookmakers" in o:
                yield o
            for v in o.values():
                yield from items(v)
        elif isinstance(o, list):
            for v in o:
                yield from items(v)

    for it in items(payload):
        for book in it.get("bookmakers") or []:
            for bet in book.get("bets") or []:
                if bet.get("name") != SCORER_MARKET:
                    continue
                for v in bet.get("values") or []:
                    try:
                        price = float(v["odd"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    name = v.get("value")
                    if name and price > 1.0:
                        out.append(Price(str(book.get("name")), "ANYTIME", None, str(name), price))
    return out


def parse_odds(odds: dict[str, Any]) -> list[Price]:
    """Normalised prices for NORMALISED_MARKETS from one /odds?fixture response item."""
    out: list[Price] = []
    for book in odds.get("bookmakers") or []:
        for bet in book.get("bets") or []:
            market = NORMALISED_MARKETS.get(bet.get("name", ""))
            if market is None:
                continue
            for v in bet.get("values") or []:
                try:
                    price = float(v["odd"])
                except (KeyError, TypeError, ValueError):
                    continue
                selection, line = str(v.get("value")), None
                if market == "OU":
                    selection, line = _line(selection)
                    if line not in OU_LINES:
                        continue
                out.append(Price(str(book["name"]), market, line, selection, price))
    return out


def n_bookmakers(odds: dict[str, Any] | None) -> int:
    return len((odds or {}).get("bookmakers") or [])


@dataclass(frozen=True)
class SquadEntry:
    player_ref: str
    player_name: str
    number: int | None
    position: str | None  # Goalkeeper / Defender / Midfielder / Attacker


def parse_squad(rows: list[dict[str, Any]]) -> list[SquadEntry]:
    """/players/squads?team: the team's current squad. The provider's `age` and photo are
    not kept (not needed, and personal data is kept to what the model uses)."""
    out: list[SquadEntry] = []
    for r in rows:
        for p in r.get("players") or []:
            if p.get("id") is None:
                continue
            num = p.get("number")
            out.append(SquadEntry(player_ref=str(p["id"]), player_name=str(p.get("name") or ""),
                                  number=int(num) if isinstance(num, int) else None,
                                  position=p.get("position") or None))  # fmt: skip
    return out


def parse_injuries(rows: list[dict[str, Any]]) -> list[Absence]:
    out: list[Absence] = []
    for r in rows:
        p, t, f = r["player"], r["team"], r["fixture"]
        kind = (p.get("type") or "").lower()
        status = "questionable" if kind == "questionable" else "missing"
        out.append(
            Absence(
                player_ref=str(p["id"]),
                player_name=str(p.get("name") or ""),
                team_ref=str(t["id"]),
                team_name=str(t.get("name") or ""),
                fixture_id=int(f["id"]),
                fixture_kickoff=datetime.fromisoformat(f["date"]),
                status=status,
                reason=p.get("reason") or None,
            )
        )
    return out


def _players(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for e in entries or []:
        pl = e.get("player") or {}
        out.append(
            {
                "player_ref": str(pl.get("id")) if pl.get("id") is not None else None,
                "name": pl.get("name"),
                "number": pl.get("number"),
                "pos": pl.get("pos"),
                "grid": pl.get("grid"),
            }
        )
    return out


def parse_lineups(fx: dict[str, Any]) -> list[Lineup]:
    """Lineups from a /fixtures?ids detail item; empty until the provider publishes them."""
    out: list[Lineup] = []
    for lu in fx.get("lineups") or []:
        starters = _players(lu.get("startXI"))
        if len(starters) < 11:
            continue  # partial lineup: not yet confirmed
        coach = lu.get("coach") or {}
        out.append(
            Lineup(
                team_ref=str(lu["team"]["id"]),
                formation=lu.get("formation"),
                coach_ref=str(coach["id"]) if coach.get("id") is not None else None,
                coach_name=coach.get("name"),
                starters=starters,
                substitutes=_players(lu.get("substitutes")),
            )
        )
    return out
