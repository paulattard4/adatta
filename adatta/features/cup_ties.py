"""Cup ties: which matches are legs of one tie or replays (docs/BEYOND_LEAGUES_PLAN.md,
Stage A, A0 (c)).

Each leg of a two-legged tie and each replay is a 90' match in its own right in Stage A; this
module only labels them, for segments and for Stage B's aggregate (the leg-2 forecast does not
use the leg-1 score). The provider has no tie id, so ties come from the fixture list and the
competition's format (`config/cup_formats.yml`, `second_meeting`):
- a match in a round labelled "... Replay(s)" is a replay;
- otherwise the matches of one pair of teams in one round of a competition-season form a tie:
  one match is single; two with sides swapped are leg 1 and leg 2 where the format has legs,
  or the match and its replay where it has replays (the later meeting is the replay);
- anything else (same home side twice, more than two meetings, a second meeting where the
  format has neither) is listed as irregular and labelled single.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

FORMATS_PATH = Path("config/cup_formats.yml")
SINGLE, LEG1, LEG2, REPLAY = "single", "leg1", "leg2", "replay"
LEG, NONE = "leg", "none"  # second_meeting values, with REPLAY
_REPLAY = re.compile(r"\s+Replays?$")


@dataclass(frozen=True)
class CupMatch:
    match_id: int
    league_id: int  # the provider's competition id, as in config/cup_formats.yml
    season: int
    round: str
    home: int
    away: int
    kickoff: datetime


@dataclass(frozen=True)
class Tie:
    kind: str  # single, leg1, leg2 or replay
    tie: tuple[int, int, str, int, int]  # league, season, round (no Replay), team, team


def load_formats(path: Path | str = FORMATS_PATH) -> dict[int, str]:
    """{league_id: second_meeting}."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    out = {int(k): str(v["second_meeting"]) for k, v in (raw.get("formats") or {}).items()}
    bad = {k: v for k, v in out.items() if v not in (LEG, REPLAY, NONE)}
    if bad:
        raise ValueError(f"second_meeting must be leg, replay or none: {bad}")
    return out


def is_replay(round_: str) -> bool:
    return _REPLAY.search(round_ or "") is not None


def base_round(round_: str) -> str:
    return _REPLAY.sub("", round_ or "")


def label(
    matches: Sequence[CupMatch], formats: Mapping[int, str]
) -> tuple[dict[int, Tie], list[int]]:
    """{match_id: Tie} for every match given, and the irregular match ids."""
    groups: dict[tuple[int, int, str, int, int], list[CupMatch]] = defaultdict(list)
    out: dict[int, Tie] = {}
    for m in matches:
        a, b = sorted((m.home, m.away))
        key = (m.league_id, m.season, base_round(m.round), a, b)
        if is_replay(m.round):
            out[m.match_id] = Tie(REPLAY, key)
        else:
            groups[key].append(m)
    irregular: list[int] = []
    for key, ms in groups.items():
        ms = sorted(ms, key=lambda m: (m.kickoff, m.match_id))
        second = formats.get(key[0], NONE)
        swapped = len(ms) == 2 and (ms[0].home, ms[0].away) == (ms[1].away, ms[1].home)
        if swapped and second == LEG:
            out[ms[0].match_id], out[ms[1].match_id] = Tie(LEG1, key), Tie(LEG2, key)
            continue
        if swapped and second == REPLAY:
            out[ms[0].match_id], out[ms[1].match_id] = Tie(SINGLE, key), Tie(REPLAY, key)
            continue
        if len(ms) > 1:
            irregular += [m.match_id for m in ms]
        for m in ms:
            out[m.match_id] = Tie(SINGLE, key)
    return out, sorted(irregular)
