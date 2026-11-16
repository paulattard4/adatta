"""Team level and clubs among national sides (docs/BEYOND_LEAGUES_PLAN.md, team level; before
any national-team model, Stage D).

Teams of national competitions (`is_national`) take `gender` and `team_level` from their
name (`normalize.gender_of`, `normalize.team_level_of`: women's sides, U15-U23, B sides).
A national side whose name matches no provider country may be a club that played a
national side (LAFC, DC United): the provider's own flag decides, one `/teams?id=` request
each (`fetch_teams`, the only place here that sends requests, through the client and its
quota planner); `national: false`
clears `is_national`, except where the provider's country is the side's own name
(Montserrat, Puerto Rico U19: a club's country is never its own name) or the side is listed
under `nations` in config/national_countries.yml (checked by hand: the provider's flag is
wrong there). No answer, or `national: true`, leaves the row as it is.

A senior men's national fit reads `senior_national_ids`: men's national sides at `senior`
level only.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.api_football import ProviderTeam, parse_team
from adatta.adapters.normalize import gender_of, name_key, team_level_of
from adatta.adapters.storage import RawStore
from adatta.db import models
from adatta.features import host

SENIOR = "senior"
MEN = "men"


def national_teams(session: Session) -> list[models.Team]:
    return list(session.scalars(select(models.Team).where(models.Team.is_national)))


def apply_levels(teams: Sequence[models.Team]) -> int:
    """Set gender and team_level from the name; returns how many teams changed."""
    changed = 0
    for t in teams:
        gender, level = gender_of(t.name), team_level_of(t.name)
        if (t.gender, t.team_level) != (gender, level):
            t.gender, t.team_level = gender, level
            changed += 1
    return changed


def club_candidates(teams: Sequence[models.Team], countries: host.Countries) -> list[models.Team]:
    """National sides whose name (suffix removed) matches no provider country."""
    return [t for t in teams if countries.of(t.name) is None]


def fetched_teams(session: Session) -> set[str]:
    return {str(p["id"]) for p, _ in host._latest_payloads(session, "/teams") if "id" in p}


class TeamSource(Protocol):
    def team(self, session: Session, team_id: int) -> ProviderTeam | None: ...


def fetch_teams(session: Session, client: TeamSource, teams: Sequence[models.Team]) -> int:
    """/teams?id= for each team not fetched yet (resumable); returns the number fetched."""
    done = fetched_teams(session)
    n = 0
    for t in sorted(teams, key=lambda t: t.source_ref):
        if t.source_ref in done or not t.source_ref.isdigit():
            continue
        client.team(session, int(t.source_ref))
        session.commit()
        n += 1
    return n


def stored_teams(session: Session, store: RawStore) -> dict[str, ProviderTeam]:
    """The provider's answer per team id, rebuilt from the newest stored /teams payloads."""
    out: dict[str, ProviderTeam] = {}
    for _, key in host._latest_payloads(session, "/teams"):
        got = parse_team(host._read(store, key))
        if got is not None:
            out[got.ref] = got
    return out


def load_nations(path: Path | str = host.ALIASES_PATH) -> frozenset[str]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return frozenset(str(n) for n in raw.get("nations") or [])


def is_nation(name: str, provider_country: str, nations: frozenset[str]) -> bool:
    base = host.base_name(name)
    return base in nations or name_key(base) == name_key(provider_country)


def apply_clubs(teams: Sequence[models.Team], provider: dict[str, ProviderTeam],
                nations: frozenset[str] = frozenset()) -> list[str]:  # fmt: skip
    """Clear is_national where the provider says the team is not a national side (unless it
    is a nation by `is_nation`), and set its country; returns the names cleared."""
    cleared: list[str] = []
    for t in teams:
        p = provider.get(t.source_ref)
        if (p is not None and not p.national and t.is_national
                and not is_nation(t.name, p.country, nations)):  # fmt: skip
            t.is_national, t.country = False, p.country
            cleared.append(t.name)
    return sorted(cleared)


def senior_national_ids(session: Session) -> set[int]:
    """The teams a senior men's national-team fit may read."""
    T = models.Team
    q = select(T.team_id).where(T.is_national, T.gender == MEN, T.team_level == SENIOR)
    return set(session.scalars(q))


def level_counts(teams: Sequence[models.Team]) -> dict[str, int]:
    """Teams by gender and level, e.g. {"men senior": 216, "women u17": 2}."""
    return dict(Counter(f"{t.gender} {t.team_level}" for t in teams).most_common())
