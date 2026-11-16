"""Team level and clubs among national sides (docs/BEYOND_LEAGUES_PLAN.md, team level)."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters.api_football import ProviderTeam, parse_team
from adatta.adapters.storage import LocalStore, encode_payload
from adatta.db import models
from adatta.db.models import Base
from adatta.features import host
from adatta.features import team_levels as tl


@pytest.mark.parametrize(
    ("name", "level"),
    [
        ("England", "senior"),
        ("England U21", "u21"),
        ("Brazil U-20", "u20"),
        ("St. Vincent / Gren. U20", "u20"),
        ("Canada W U17", "women_u17"),
        ("Mexico W", "women"),
        ("England B", "b"),
        ("Korea DPR U23", "u23"),
        ("Rangers U17", "u17"),
        ("Netherlands U14", "senior"),  # outside U15-U23: not read as an age
        ("Bosnia & Herzegovina", "senior"),
        ("Los Angeles FC", "senior"),
    ],
)
def test_level_from_the_name(name: str, level: str) -> None:
    assert normalize.team_level_of(name) == level


def test_parse_team_reads_the_national_flag() -> None:
    p = {"response": [{"team": {"id": 1616, "name": "Los Angeles FC", "national": False,
                                "country": "USA"}}]}  # fmt: skip
    assert parse_team(p) == ProviderTeam("1616", "Los Angeles FC", False, "USA")
    assert parse_team({"response": []}) is None
    assert parse_team({"response": [{"team": {"id": 1, "name": "X"}}]}) is None  # no flag


def test_new_national_sides_get_their_level_at_load() -> None:
    s = _session()
    got = normalize.teams_by_ref(s, {"1": "Spain U19", "2": "Spain"}, national=True)
    assert got["1"].team_level == "u19" and got["2"].team_level == "senior"
    club = normalize.teams_by_ref(s, {"3": "Barcelona B"}, country="Spain")
    assert club["3"].team_level == "senior" and not club["3"].is_national  # a club: untouched


class FakeClient:
    def __init__(self, store: LocalStore, answers: dict[int, dict[str, Any]]) -> None:
        self.store, self.answers = store, answers
        self.asked: list[int] = []

    def team(self, session: Session, team_id: int) -> ProviderTeam | None:
        self.asked.append(team_id)
        payload = {"response": [{"team": self.answers[team_id]}]} if team_id in self.answers \
            else {"response": []}  # fmt: skip
        blob, digest = encode_payload(payload)
        key = f"api_football/teams/{digest}.json.gz"
        self.store.put(key, blob)
        session.add(models.RawPayload(source_id=normalize.SOURCE_ID, endpoint="/teams",
                                      params={"id": team_id}, storage_key=key,
                                      content_hash=digest))  # fmt: skip
        session.flush()
        return parse_team(payload)


def _session() -> Session:
    s = Session(create_engine("sqlite://"))
    Base.metadata.create_all(s.get_bind())
    normalize.ensure_source(s)
    return s


def test_youth_women_b_and_clubs_drop_out_of_the_senior_fit(tmp_path: Path) -> None:
    s, store = _session(), LocalStore(tmp_path)
    names = {"10": "England", "11": "England U21", "12": "Canada W U17", "13": "England B",
             "14": "Los Angeles FC", "15": "Bahamas"}  # fmt: skip
    for ref, n in names.items():  # stored before this change: all senior
        s.add(models.Team(source_id=normalize.SOURCE_ID, source_ref=ref, name=n, country=n,
                          is_national=True))  # fmt: skip
    s.flush()
    teams = tl.national_teams(s)
    countries = host.Countries(["England", "Canada", "USA"], {})
    cands = tl.club_candidates(teams, countries)
    assert sorted(t.name for t in cands) == ["Bahamas", "Los Angeles FC"]  # Bahamas: no league
    client = FakeClient(store, {14: {"id": 14, "name": "Los Angeles FC", "national": False,
                                     "country": "USA"}})  # Bahamas: no answer  # fmt: skip
    assert tl.fetch_teams(s, client, cands) == 2
    assert tl.fetch_teams(s, client, cands) == 0 and client.asked == [14, 15]  # resumable
    assert tl.apply_levels(teams) == 3
    assert tl.apply_clubs(cands, tl.stored_teams(s, store)) == ["Los Angeles FC"]
    by = {t.name: t for t in teams}
    assert by["Los Angeles FC"].country == "USA" and by["Bahamas"].is_national  # not guessed
    senior = {by[n].team_id for n in ("England", "Bahamas")}
    assert tl.senior_national_ids(s) == senior
    assert tl.level_counts(teams) == {"senior": 3, "u21": 1, "women_u17": 1, "b": 1}


def test_a_nation_the_provider_flags_as_a_club_stays_national() -> None:
    def team(ref: str, name: str) -> models.Team:
        return models.Team(source_id=normalize.SOURCE_ID, source_ref=ref, name=name,
                           country=name, is_national=True)  # fmt: skip

    teams = [team("1", "Montserrat"), team("2", "Puerto Rico U19"),
             team("3", "Sao Tome and Principe"), team("4", "Catalonia Selection"),
             team("5", "Hull City")]  # fmt: skip
    said = {"1": "Montserrat", "2": "Puerto-Rico", "3": "São-Tomé-e-Príncipe",
            "4": "Catalonia", "5": "England"}  # fmt: skip
    provider = {r: ProviderTeam(r, "", False, c) for r, c in said.items()}
    nations = frozenset({"Sao Tome and Principe"})
    assert tl.apply_clubs(teams, provider, nations) == ["Catalonia Selection", "Hull City"]
    assert [t.is_national for t in teams] == [True, True, True, False, False]
    assert tl.load_nations() >= nations  # the hand-checked list in the config
