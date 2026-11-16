"""Who won a penalty shootout (docs/BEYOND_LEAGUES_PLAN.md, Stage B, shootout winner rule).

The provider gives a shootout score (`score.penalty`, stored as match_result.home_pens /
away_pens) and, for some matches, the kicks as events (`api_football_stats.shootout_kicks`).
On the dev seasons of the five cups the score names the right winner in 485 of 486 shootouts
(against who advanced), while its count differs from the kicks in 5 of 145. So:

- the winner is the score's when both values are there and differ, else the kicks' when they
  are complete, else none;
- kicks are complete when replaying them in the listed order (best of five, then sudden
  death) decides the shootout at the last listed kick and not before; this holds for ABAB
  and ABBA orders alike;
- where a later round shows who advanced, it must agree, or the shootout is unresolved;
- the score kept for a kick model is the complete kicks', never the field's count.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

REGULATION = 5  # kicks per side before sudden death

Kick = tuple[str, bool]  # ("home" | "away", scored)


def _decided(gh: int, ga: int, nh: int, na: int) -> bool:
    if nh <= REGULATION and na <= REGULATION:
        return gh > ga + (REGULATION - na) or ga > gh + (REGULATION - nh)
    return nh == na and gh != ga


def kicks_score(kicks: Sequence[Kick]) -> tuple[int, int] | None:
    """The shootout score if the kicks are complete (decided exactly at the last kick), else
    None (incomplete, level, or kicks listed after the shootout was over)."""
    gh = ga = nh = na = 0
    for i, (side, scored) in enumerate(kicks):
        if side == "home":
            nh, gh = nh + 1, gh + scored
        else:
            na, ga = na + 1, ga + scored
        if _decided(gh, ga, nh, na):
            return (gh, ga) if i == len(kicks) - 1 else None
    return None


@dataclass(frozen=True)
class Shootout:
    winner: str | None  # "home", "away" or None
    basis: str  # "score", "kicks", "unresolved"
    kicks: tuple[int, int] | None  # the complete kicks' score, for a kick model
    note: str = ""


def decide(field: tuple[int | None, int | None] | None, kicks: Sequence[Kick] | None,
           advanced: str | None = None) -> Shootout:  # fmt: skip
    """The shootout's winner by the rule above. `advanced`: the side seen in a later round of
    the same competition-season, when there is one."""
    ks = kicks_score(kicks) if kicks else None
    winner, basis = None, "unresolved"
    fh, fa = field if field is not None else (None, None)
    if fh is not None and fa is not None and fh != fa:
        winner, basis = ("home" if fh > fa else "away"), "score"
    elif ks is not None:
        winner, basis = ("home" if ks[0] > ks[1] else "away"), "kicks"
    if winner is not None and advanced is not None and advanced != winner:
        return Shootout(None, "unresolved", ks, f"{basis} names {winner}, {advanced} advanced")
    if winner is None:
        return Shootout(None, "unresolved", ks, "no score and no complete kicks")
    return Shootout(winner, basis, ks)


def advanced_side(home: int, away: int, later_teams: set[int]) -> str | None:
    """The side of a tie that plays a later match of the same competition-season, if exactly
    one does (a final, or the last stored round, has none)."""
    h, a = home in later_teams, away in later_teams
    return "home" if h and not a else "away" if a and not h else None
