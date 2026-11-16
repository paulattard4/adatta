"""Stage A, A0 (a): the 90' score of a match that went to extra time from the goal events of
its stored detail (docs/BEYOND_LEAGUES_PLAN.md)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_stats as st
from adatta.adapters import normalize
from adatta.adapters.detail_store import repair_normal_time
from adatta.adapters.storage import LocalStore, encode_payload
from adatta.db import models
from adatta.db.models import Base

HOME, AWAY = 10, 20


def goal(team: int, minute: int, extra: int | None = None, detail: str = "Normal Goal",
         comments: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {"type": "Goal", "detail": detail, "comments": comments, "team": {"id": team},
            "time": {"elapsed": minute, "extra": extra}}  # fmt: skip


def shootout(team: int, kick: int, scored: bool) -> dict[str, Any]:
    return goal(team, 120, kick, "Penalty" if scored else "Missed Penalty", st.SHOOTOUT)


def fixture(fid: int, status: str, events: list[dict[str, Any]], total: tuple[int, int],
            fulltime: tuple[int | None, int | None], extratime: tuple[int | None, int | None],
            pens: tuple[int | None, int | None] = (None, None)) -> dict[str, Any]:  # fmt: skip
    def pair(p: tuple[int | None, int | None]) -> dict[str, int | None]:
        return {"home": p[0], "away": p[1]}

    return {
        "fixture": {"id": fid, "date": "2023-01-07T15:00:00+00:00", "status": {"short": status}},
        "teams": {"home": {"id": HOME, "name": "A"}, "away": {"id": AWAY, "name": "B"}},
        "goals": pair(total),
        "score": {"halftime": pair((0, 0)), "fulltime": pair(fulltime),
                  "extratime": pair(extratime), "penalty": pair(pens)},
        "events": events,
    }  # fmt: skip


def test_event_goals_leave_out_shootout_kicks_and_missed_penalties() -> None:
    events = [
        goal(HOME, 12), goal(AWAY, 90, 4),  # stoppage time is still normal time
        goal(AWAY, 77, detail="Own Goal"),  # credited to the side the event names
        goal(HOME, 60, detail="Missed Penalty"), goal(HOME, 105),
        shootout(HOME, 1, True), shootout(AWAY, 1, False), shootout(HOME, 2, True),
        {"type": "Card", "detail": "Yellow Card", "team": {"id": HOME}, "time": {"elapsed": 30}},
    ]  # fmt: skip
    fx = fixture(1, "PEN", events, (2, 2), (None, None), (None, None), (2, 0))
    assert st.event_goals(fx) == ((1, 2), (2, 2))
    assert st.normal_time_from_events(fx) == (1, 2)


def test_events_that_do_not_add_up_to_the_total_are_not_used() -> None:
    assert st.normal_time_from_events(fixture(1, "AET", [goal(HOME, 10)], (2, 1),
                                              (1, 1), (1, 0))) is None  # fmt: skip
    assert st.normal_time_from_events(fixture(1, "AET", [], (1, 0), (0, 0), (1, 0))) is None
    stranger = fixture(1, "AET", [goal(99, 10)], (1, 0), (1, 0), (0, 0))
    assert st.event_goals(stranger) is None


def _world(
    tmp_path: Path, fixtures: list[dict[str, Any]]
) -> tuple[Session, LocalStore, dict[int, int]]:
    s = Session(create_engine("sqlite://"))
    Base.metadata.create_all(s.get_bind())
    normalize.ensure_source(s)
    comp = normalize.ensure_competition(s, league_id=45, name="Cup", country="England",
                                        level="senior", tier="cup")  # fmt: skip
    season = normalize.ensure_season(s, comp.competition_id, 2022)
    lists = [{k: v for k, v in fx.items() if k != "events"} for fx in fixtures]
    normalize.ingest_fixtures(s, season, lists, "England")
    store = LocalStore(tmp_path)
    blob, _ = encode_payload({"response": fixtures})
    store.put("api_football/fixtures/d.json.gz", blob)
    ids = dict(
        (int(r), m) for m, r in s.execute(select(models.Match.match_id, models.Match.source_ref))
    )
    for mid in ids.values():
        s.add(models.MatchDetail(match_id=mid, source_id="api_football",
                                 storage_key="api_football/fixtures/d.json.gz",
                                 observed_at=datetime(2023, 1, 8, tzinfo=UTC),
                                 flags={"returned": True}))  # fmt: skip
    s.commit()
    return s, store, ids


def _current(s: Session, mid: int) -> models.MatchResult:
    R = models.MatchResult
    row = s.scalar(select(R).where(R.match_id == mid, R.superseded_at.is_(None)))
    assert row is not None
    return row


def test_repair_versions_the_result_and_lists_the_unscorable(tmp_path: Path) -> None:
    fixtures = [
        # no ET split from the provider: the 90' score is unknown until the events give it
        fixture(1, "AET", [goal(HOME, 30), goal(AWAY, 88), goal(HOME, 101)], (2, 1),
                (2, 1), (None, None)),
        # the provider split already agrees with the events
        fixture(2, "PEN", [goal(HOME, 30), goal(AWAY, 50), shootout(HOME, 1, True)], (1, 1),
                (1, 1), (0, 0), (1, 0)),
        # no events, no split: unscorable, listed
        fixture(3, "AET", [], (1, 0), (1, 0), (None, None)),
        # no events, a provider split: kept
        fixture(4, "AET", [], (1, 0), (0, 0), (1, 0)),
        # a 90' win is not touched
        fixture(5, "FT", [goal(HOME, 3)], (1, 0), (1, 0), (None, None)),
        # marked FT, but the total is not the 90' score: extra time the provider did not mark
        fixture(6, "FT", [goal(HOME, 10), goal(AWAY, 20), goal(AWAY, 100)], (1, 2),
                (1, 1), (None, None)),
    ]  # fmt: skip
    s, store, ids = _world(tmp_path, fixtures)
    assert _current(s, ids[1]).home_goals_nt is None
    out = repair_normal_time(s, store, datetime(2026, 11, 16, tzinfo=UTC))
    s.commit()
    assert out["matches"] == 5 and out["agree"] == 1 and out["provider_kept"] == 1
    assert sorted(out["changed"]) == [
        (ids[1], (None, None), (1, 1)),
        (ids[6], (1, None), (1, 1)),
    ]
    assert out["unscorable"] == [ids[3]]
    r = _current(s, ids[1])
    assert (r.home_goals_nt, r.away_goals_nt, r.home_goals_et, r.away_goals_et) == (1, 1, 1, 0)
    assert r.nt_basis == normalize.NT_EVENTS and (r.home_goals_ht, r.away_goals_ht) == (0, 0)
    assert len(s.scalars(select(models.MatchResult).where(
        models.MatchResult.match_id == ids[1])).all()) == 2  # fmt: skip
    # a second run changes nothing
    again = repair_normal_time(s, store)
    assert again["changed"] == [] and again["agree"] == 2  # match 6 now has a 90' score


def test_fixture_reingest_keeps_the_events_split_until_the_provider_changes(tmp_path: Path) -> None:
    fx = fixture(1, "AET", [goal(HOME, 30), goal(AWAY, 88), goal(HOME, 101)], (2, 1),
                 (2, 1), (None, None))  # fmt: skip
    s, store, ids = _world(tmp_path, [fx])
    repair_normal_time(s, store)
    s.commit()
    season = s.scalar(select(models.Season))
    assert season is not None
    listing = {k: v for k, v in fx.items() if k != "events"}
    assert normalize.ingest_fixtures(s, season, [listing], "England") == 0
    assert _current(s, ids[1]).nt_basis == normalize.NT_EVENTS
    corrected = listing | {"goals": {"home": 3, "away": 1}}  # the provider changes the total
    assert normalize.ingest_fixtures(s, season, [corrected], "England") == 1
    assert _current(s, ids[1]).nt_basis == normalize.NT_PROVIDER
