"""Venue role per match: home, swapped, neutral, review, unknown (adatta/features/neutral.py)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.db import models
from adatta.db.models import Base
from adatta.features import neutral

T0 = datetime(2023, 8, 12, 14, 0, tzinfo=UTC)
LEAGUE, CUP = 39, 45


def week(n: int) -> datetime:
    return T0 + timedelta(weeks=n)


class World:
    def __init__(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.s = Session(engine)
        normalize.ensure_source(self.s)
        self.seasons: dict[int, int] = {}
        comps: dict[int, int] = {}
        for lid in (LEAGUE, CUP):
            c = normalize.ensure_competition(
                self.s, league_id=lid, name=str(lid), country="England", level="senior", tier="1"
            )
            comps[lid] = c.competition_id
            self.seasons[lid] = normalize.ensure_season(self.s, c.competition_id, 2023).season_id
        self.league_comp = comps[LEAGUE]
        self.team: dict[str, int] = {}
        for n in ("a", "b", "c", "lower"):
            t = models.Team(source_id=normalize.SOURCE_ID, source_ref=n, name=n)
            self.s.add(t)
            self.s.flush()
            self.team[n] = t.team_id
        self.venue: dict[str, int] = {}
        for key, name, city in (
            ("alpha", "Alpha Park Stadium", "Exeter"),
            ("alpha_town", "Alpha Park Stadium, Exeter", ""),
            ("alpha_new", "New Alpha Arena", "Exeter"),
            ("beta", "Beta Ground", "York"),
            ("gamma", "Gamma Arena", "Exeter"),
            ("big", "National Stadium", "Exeter"),
            ("far", "Far Stadium", "Leeds"),
        ):
            v = models.Venue(source_id=normalize.SOURCE_ID, name=name, city=city,
                             country="England", observed_at=T0)  # fmt: skip
            self.s.add(v)
            self.s.flush()
            self.venue[key] = v.venue_id
        self.n = 0
        # league grounds: a at Alpha Park (weeks 0-9) then New Alpha Arena (weeks 20-29),
        # b at Beta Ground, c at Gamma Arena (same city as a)
        for i in range(10):
            self.match("a", "b", "alpha", week(i), LEAGUE)
            self.match("b", "c", "beta", week(i), LEAGUE)
            self.match("c", "a", "gamma", week(i), LEAGUE)
        for i in range(20, 30):
            self.match("a", "b", "alpha_new", week(i), LEAGUE)
        self.s.commit()

    def match(self, home: str, away: str, venue: str | None, at: datetime, lid: int,
              round_: str = "Regular Season - 1") -> models.Match:  # fmt: skip
        self.n += 1
        vid = None if venue is None else self.venue[venue]
        m = models.Match(source_id=normalize.SOURCE_ID, source_ref=str(self.n),
                         season_id=self.seasons[lid], home_team_id=self.team[home],
                         away_team_id=self.team[away], kickoff_time=at, status="FT",
                         venue_id=vid, round=round_)  # fmt: skip
        self.s.add(m)
        self.s.flush()
        return m

    def roles(self, matches: list[models.Match], rules: neutral.Rules) -> pl.DataFrame:
        league_of = {v: k for k, v in self.seasons.items()}
        return neutral.venue_roles(self.s, matches, [self.league_comp], rules, league_of)


NO_RULES = neutral.Rules((), {})


def test_roles_from_venue_identity_and_city() -> None:
    w = World()
    at = week(5)
    cases = {
        ("a", "b", "alpha_town"): ("home", "home", "ground"),  # town appended: prefix match
        ("a", "b", "beta"): ("swapped", "swapped", "ground"),  # at the away side's ground
        ("a", "b", "far"): ("neutral", "neutral", "derived"),
        ("a", "b", "big"): ("home_city", "home", "city"),  # another ground in a's city
        ("b", "a", "big"): ("away_city", "review", "derived"),  # in the away side's city
        ("a", "c", "big"): ("review", "review", "derived"),  # a city both teams share
        ("a", "lower", "far"): ("unknown", "unknown", "derived"),  # no grounds for lower
        ("a", "b", None): ("unknown", "unknown", "derived"),  # no venue listed
    }
    ms = [w.match(h, a, v, at, CUP, "3rd Round") for h, a, v in cases]
    got = w.roles(ms, NO_RULES)
    for want, row in zip(cases.values(), got.iter_rows(named=True), strict=True):
        assert (row["derived"], row["role"], row["basis"]) == want
        assert row["neutral"] == (want[1] == "neutral")


def test_grounds_follow_moves_in_time() -> None:
    w = World()
    old = w.match("a", "b", "alpha", week(25), CUP, "3rd Round")  # after the move
    new = w.match("a", "b", "alpha_new", week(25), CUP, "3rd Round")
    early_new = w.match("a", "b", "alpha_new", week(5), CUP, "3rd Round")
    got = w.roles([old, new, early_new], NO_RULES)["role"].to_list()
    # Alpha Park is still in a's city, so home by city rather than by ground
    assert got == ["home", "home", "home"]
    basis = w.roles([old, new, early_new], NO_RULES)["basis"].to_list()
    assert basis == ["city", "ground", "city"]


def test_rules_and_overrides(tmp_path: Path) -> None:
    w = World()
    cfg = tmp_path / "neutral.yml"
    cfg.write_text(
        f"rules:\n  - {{league_ids: [{CUP}], round: 'Final', note: finals}}\n"
        f"overrides:\n  - {{fixture: 'OVR', role: neutral, note: moved}}\n"
    )
    rules = neutral.load_rules(cfg)
    at = week(5)
    final_city = w.match("a", "b", "big", at, CUP, "Final")  # Wembley-style: rule wins
    final_own = w.match("a", "b", "alpha", at, CUP, "Final")  # a finalist's ground: home
    final_review = w.match("a", "c", "big", at, CUP, "Final")
    not_final = w.match("a", "b", "big", at, CUP, "Semi-finals")
    over = w.match("a", "b", "alpha", at, CUP, "3rd Round")
    over.source_ref = "OVR"
    got = w.roles([final_city, final_own, final_review, not_final, over], rules)
    assert got["role"].to_list() == ["neutral", "home", "neutral", "home", "neutral"]
    assert got["basis"].to_list() == ["rule", "ground", "rule", "city", "override"]


def test_write_flags_is_idempotent() -> None:
    w = World()
    ms = [w.match("a", "b", "far", week(5), CUP, "3rd Round"),
          w.match("a", "b", "alpha", week(5), CUP, "3rd Round")]  # fmt: skip
    roles = w.roles(ms, NO_RULES)
    assert neutral.write_flags(w.s, ms, roles) == 1
    assert [m.neutral for m in ms] == [True, False]
    assert neutral.write_flags(w.s, ms, roles) == 0


def test_shipped_rules_load_and_stage_labels() -> None:
    rules = neutral.load_rules()
    assert rules.rule_for(45, "Final") and rules.rule_for(45, "Semi-finals")
    assert rules.rule_for(45, "Quarter-finals") is None
    assert rules.rule_for(143, "Semi-finals") is None  # Copa del Rey semis are two-legged
    assert neutral.stage("Regular Season - 3") == "league"
    assert neutral.stage("Promotion Play-offs - Final") == "qualifying"
    assert neutral.stage("Final") == "final" and neutral.stage("Semi-finals") == "semi"
