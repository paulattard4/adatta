"""Stage B, B0: how a cup tie is decided, and the ties as decided (docs/BEYOND_LEAGUES_PLAN.md,
Stage B). No API requests.

The rules (`config/cup_formats.yml`, `rules`) give per cup, season range and round the legs,
replays, extra time and away goals; penalties decide what is still level. A tie groups its
matches (`cup_ties.label`: a single match with its replay, or two legs); side A is the home
side of the single match or of leg 1. `check` compares a finished tie's path with its rule,
and `advanced` names the side that went through: the one seen later in the same cup-season,
else (a final, or the last stored round) the one its own result names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from adatta.features import cup_ties
from adatta.features import shootouts as so

NONE, AFTER_90, ALSO_ET = "none", "after_90", "also_et"


@dataclass(frozen=True)
class Rule:
    legs: int = 1
    replay: bool = False
    extra_time: bool = True
    away_goals: str = NONE


@dataclass(frozen=True)
class Entry:
    first: int
    last: int
    rounds: tuple[str, ...]
    rule: Rule


def load_rules(path: Path | str = cup_ties.FORMATS_PATH) -> dict[int, list[Entry]]:
    raw = yaml.safe_load(Path(path).read_text())["rules"]
    out: dict[int, list[Entry]] = {}
    for cup, entries in raw.items():
        out[int(cup)] = [
            Entry(int(e["seasons"][0]), int(e["seasons"][1]), tuple(e["rounds"]),
                  Rule(int(e.get("legs", 1)), bool(e.get("replay", False)),
                       bool(e.get("extra_time", True)), str(e.get("away_goals", NONE))))
            for e in entries
        ]  # fmt: skip
    return out


def rule_for(rules: Mapping[int, Sequence[Entry]], cup: int, season: int,
             round_: str) -> Rule | None:  # fmt: skip
    """The first entry of the cup covering the season whose round pattern matches."""
    base = cup_ties.base_round(round_)
    for e in rules.get(cup, ()):
        if e.first <= season <= e.last and any(fnmatchcase(base, p) for p in e.rounds):
            return e.rule
    return None


# ----------------------------------------------------------------------------- ties


@dataclass(frozen=True)
class Played:
    """One finished match of a tie, with its scores as stored."""

    match_id: int
    kind: str  # single, replay, leg1, leg2
    home: int
    away: int
    kickoff: datetime
    status: str  # FT, AET, PEN
    nt: tuple[int, int] | None  # 90' (home, away)
    et: tuple[int | None, int | None]  # extra-time goals only
    pens: tuple[int | None, int | None]


@dataclass
class Tie:
    cup: int
    season: int
    round: str
    a: int  # the home side of the single match or of leg 1
    b: int
    matches: list[Played] = field(default_factory=list)

    @property
    def last_kickoff(self) -> datetime:
        return max(m.kickoff for m in self.matches)

    def first(self, kind: str) -> Played | None:
        return next((m for m in self.matches if m.kind == kind), None)


def group(matches: Sequence[tuple[cup_ties.CupMatch, Played]],
          formats: Mapping[int, str]) -> tuple[list[Tie], list[int]]:  # fmt: skip
    """Ties from labelled cup matches (one competition or several); irregular match ids."""
    labels, irregular = cup_ties.label([c for c, _ in matches], formats)
    by: dict[tuple[int, int, str, int, int], list[tuple[cup_ties.CupMatch, Played]]] = {}
    for c, p in matches:
        by.setdefault(labels[c.match_id].tie, []).append((c, p))
    ties = []
    for (cup, season, rnd, _, _), ms in sorted(
        by.items(), key=lambda x: min(p.kickoff for _, p in x[1])
    ):
        ms = sorted(ms, key=lambda x: x[1].kickoff)
        lead = next((p for _, p in ms if p.kind in (cup_ties.SINGLE, cup_ties.LEG1)), ms[0][1])
        ties.append(Tie(cup, season, rnd, lead.home, lead.away, [p for _, p in ms]))
    return ties, irregular


def _level(m: Played) -> bool:
    return m.nt is not None and m.nt[0] == m.nt[1]


def check(t: Tie, rule: Rule | None) -> str | None:
    """Why a finished tie's path doesn't fit its rule, or None."""
    if rule is None:
        return "no rule"
    kinds = [m.kind for m in t.matches]
    if any(m.nt is None for m in t.matches):
        return "no 90' score"
    if rule.legs == 2:
        if sorted(kinds) != [cup_ties.LEG1, cup_ties.LEG2]:
            return f"two-legged round with {kinds}"
        l1, l2 = t.first(cup_ties.LEG1), t.first(cup_ties.LEG2)
        assert l1 is not None and l2 is not None and l1.nt and l2.nt
        if l1.status != "FT":
            return "leg 1 not FT"
        agg_a, agg_b = l1.nt[0] + l2.nt[1], l1.nt[1] + l2.nt[0]
        away_a, away_b = l2.nt[1], l1.nt[1]
        level = agg_a == agg_b and (rule.away_goals == NONE or away_a == away_b)
        if level and l2.status == "FT":
            return "level aggregate, leg 2 ended at 90'"
        if not level and l2.status != "FT":
            return "decided aggregate, leg 2 went on"
        if level and not rule.extra_time and l2.status == "AET":
            return "extra time where the rule has none"
        return None
    if cup_ties.LEG1 in kinds or cup_ties.LEG2 in kinds:
        return f"single-match round with {kinds}"
    first = t.first(cup_ties.SINGLE) or t.matches[0]
    replays = [m for m in t.matches if m.kind == cup_ties.REPLAY]
    if _level(first) and rule.replay:
        if first.status != "FT":
            return "level, went on where a replay was due"
        if not replays:
            return "level, no replay"
        decider = replays[-1]
    elif replays:
        return "replay where none was due"
    else:
        decider = first
    if not _level(decider):
        return None if decider.status == "FT" else f"decided at 90', status {decider.status}"
    if rule.extra_time and decider.status == "FT":
        return "level, ended at 90' where extra time applies"
    if not rule.extra_time and decider.status == "AET":
        return "extra time where the rule has none"
    return None


def advanced(t: Tie, later_teams: set[int]) -> tuple[int | None, str]:
    """(the team that went through, basis): a later match of the cup-season, else the tie's
    own last match (90', extra time, or the shootout winner rule)."""
    side = so.advanced_side(t.a, t.b, later_teams)
    if side is not None:
        return (t.a if side == "home" else t.b), "later"
    last = t.matches[-1]
    if last.nt is None:
        return None, "no score"
    h = last.nt[0] + (last.et[0] or 0)
    a = last.nt[1] + (last.et[1] or 0)
    if len(t.matches) == 2 and t.matches[0].kind == cup_ties.LEG1 and t.matches[0].nt:
        h += t.matches[0].nt[1]  # leg 2's home side is B: add its leg-1 away goals
        a += t.matches[0].nt[0]
    if h != a and last.status != "PEN":
        return (last.home if h > a else last.away), "result"
    s = so.decide(last.pens, None)
    if s.winner is None:
        return None, "unresolved"
    return (last.home if s.winner == "home" else last.away), "shootout"


def to_dict(t: Tie) -> dict[str, Any]:
    return {"cup": t.cup, "season": t.season, "round": t.round, "a": t.a, "b": t.b,
            "match_ids": [m.match_id for m in t.matches]}  # fmt: skip
