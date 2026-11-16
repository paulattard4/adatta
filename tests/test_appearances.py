"""Phase 3, P3-0: player appearances from stored detail payloads (docs/PHASE3_TASKS.md):
minutes from events, goals and own goals, statistics nulls, one player across clubs, and
the as-of rule."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_stats as st
from adatta.adapters import normalize
from adatta.adapters.storage import LocalStore, encode_payload
from adatta.db import models
from adatta.db.models import Base
from adatta.features import appearances as ap
from adatta.features.referees import RESULT_LAG

H, A = 10, 20


def lineup(team: int, starters: list[int], bench: list[int]) -> dict[str, Any]:
    def p(i: int) -> dict[str, Any]:
        return {"player": {"id": i, "name": f"P{i}", "number": i % 99, "pos": "M",
                           "grid": None}}  # fmt: skip

    return {"team": {"id": team}, "formation": "4-4-2", "coach": {"id": 1},
            "startXI": [p(i) for i in starters], "substitutes": [p(i) for i in bench]}  # fmt: skip


def ev(kind: str, team: int, player: int, minute: int, detail: str = "",
       assist: int | None = None, extra: int | None = None,
       comments: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {"type": kind, "detail": detail, "comments": comments, "team": {"id": team},
            "player": {"id": player}, "assist": {"id": assist},
            "time": {"elapsed": minute, "extra": extra}}  # fmt: skip


def stats(team: int, rows: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """A players block: {player id: {"minutes": .., "key": .., "rating": .., ...}}."""

    def one(pid: int, r: dict[str, Any]) -> dict[str, Any]:
        return {"player": {"id": pid}, "statistics": [{
            "games": {"minutes": r.get("minutes"), "rating": r.get("rating"), "captain": False},
            "goals": {"total": r.get("goals"), "assists": r.get("assists")},
            "passes": {"key": r.get("key"), "total": r.get("passes")},
            "tackles": {"total": r.get("tackles")}, "cards": {"yellow": 0, "red": 0},
        }]}  # fmt: skip

    return {"team": {"id": team}, "players": [one(p, r) for p, r in rows.items()]}


def fixture(fid: int, status: str, home: int, away: int, lineups: list[dict[str, Any]],
            events: list[dict[str, Any]], players: list[dict[str, Any]] | None = None,
            date: str = "2023-01-07T15:00:00+00:00") -> dict[str, Any]:  # fmt: skip
    return {
        "fixture": {"id": fid, "date": date, "status": {"short": status}},
        "teams": {"home": {"id": home, "name": f"T{home}"},
                  "away": {"id": away, "name": f"T{away}"}},
        "goals": {"home": 0, "away": 0},
        "score": {k: {"home": 0, "away": 0} for k in ("halftime", "fulltime")}
        | {k: {"home": None, "away": None} for k in ("extratime", "penalty")},
        "lineups": lineups, "events": events, "players": players or [],
    }  # fmt: skip


XI_H, XI_A = list(range(101, 112)), list(range(201, 212))


def by_ref(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["player_ref"]: r for r in rows}


def test_minutes_from_substitutions_in_either_order_and_sendings_off() -> None:
    events = [
        ev("subst", H, 101, 65, assist=112),  # provider order: player off, assist on
        ev("subst", H, 113, 80, assist=102),  # reversed: the one on the pitch goes off
        ev("Card", A, 201, 30, "Red Card"),
        ev("Card", A, 202, 88, "Second Yellow card", extra=None),
        ev("Card", H, 101, 70, "Red Card"),  # already off: changes nothing
        ev("Card", A, 212, 50, "Red Card"),  # unused substitute on the bench
        ev("subst", H, 150, 60, assist=151),  # neither listed: unclear, counted
        ev("subst", A, 203, 90, assist=213, extra=4),  # stoppage time counts as 90
    ]
    fx = fixture(1, "FT", H, A, [lineup(H, XI_H, [112, 113, 114]),
                                 lineup(A, XI_A, [212, 213])], events)  # fmt: skip
    rows, counts = st.player_appearances(fx)
    r = by_ref(rows)
    assert counts["unclear_sub"] == 1 and len(rows) == 27
    assert (r["101"]["minute_off"], r["101"]["minutes"]) == (65, 65)
    assert (r["112"]["minute_on"], r["112"]["minutes"]) == (65, 25)
    assert (r["102"]["minutes"], r["113"]["minutes"]) == (80, 10)
    assert r["201"]["minutes"] == 30 and r["201"]["sent_off"]
    assert r["202"]["minutes"] == 88 and r["202"]["sent_off"]
    assert not r["101"]["sent_off"] and not r["212"]["sent_off"]
    assert r["114"]["minute_on"] is None and r["114"]["minutes"] == 0
    assert (r["203"]["minutes"], r["213"]["minutes"]) == (90, 0)
    assert sum(x["started"] for x in rows if x["team_ref"] == str(H)) == 11


def test_extra_time_runs_to_120_and_minutes_nt_stops_at_90() -> None:
    fx = fixture(
        1,
        "AET",
        H,
        A,
        [lineup(H, XI_H, [112]), lineup(A, XI_A, [])],
        [ev("subst", H, 101, 100, assist=112), ev("subst", H, 102, 85, assist=999)],
    )
    r = by_ref(st.player_appearances(fx)[0])
    assert (r["101"]["minutes"], r["101"]["minutes_nt"]) == (100, 90)
    assert (r["112"]["minutes"], r["112"]["minutes_nt"]) == (20, 0)
    assert (r["103"]["minutes"], r["103"]["minutes_nt"]) == (120, 90)


def test_goals_leave_out_own_goals_missed_penalties_and_shootout_kicks() -> None:
    events = [
        ev("Goal", H, 103, 10, "Normal Goal"),
        ev("Goal", H, 103, 50, "Penalty"),
        ev("Goal", A, 104, 60, "Own Goal"),  # 104 (home) scores into his own net
        ev("Goal", H, 105, 70, "Missed Penalty"),
        ev("Goal", H, 105, 120, "Penalty", extra=1, comments=st.SHOOTOUT),
    ]
    fx = fixture(1, "PEN", H, A, [lineup(H, XI_H, []), lineup(A, XI_A, [])], events)
    r = by_ref(st.player_appearances(fx)[0])
    assert r["103"]["goals"] == 2
    assert (r["104"]["goals"], r["104"]["own_goals"]) == (0, 1)
    assert r["105"]["goals"] == 0


def test_statistics_nulls_are_zero_only_when_collected_and_played() -> None:
    blocks = [
        stats(H, {101: {"minutes": 90, "key": 2, "rating": "7.1", "tackles": None},
                  102: {"minutes": 90, "key": None, "rating": None},
                  112: {"minutes": None, "key": None}}),  # unused substitute
        stats(A, {201: {"minutes": 90, "key": None}, 999: {"minutes": 5}}),  # 999 not listed
    ]  # fmt: skip
    fx = fixture(1, "FT", H, A, [lineup(H, XI_H, [112]), lineup(A, XI_A, [])], [], blocks)
    rows, counts = st.player_appearances(fx)
    r = by_ref(rows)
    assert counts["not_in_lineup"] == 1
    assert (r["101"]["key_passes"], r["102"]["key_passes"]) == (2, 0)  # collected: 0
    assert r["112"]["key_passes"] is None  # did not play
    assert r["201"]["key_passes"] is None  # not collected for that team
    assert r["101"]["tackles"] is None  # nobody in the team has a value
    assert r["101"]["rating"] == 7.1 and r["102"]["rating"] is None  # never filled
    assert r["101"]["minutes_provider"] == 90 and r["103"]["minutes_provider"] is None


def _world(tmp_path: Path, fixtures: list[dict[str, Any]]) -> tuple[Session, LocalStore]:
    s = Session(create_engine("sqlite://"))
    Base.metadata.create_all(s.get_bind())
    normalize.ensure_source(s)
    comp = normalize.ensure_competition(s, league_id=39, name="PL", country="England",
                                        level="senior", tier="1")  # fmt: skip
    season = normalize.ensure_season(s, comp.competition_id, 2022)
    lists = [{k: v for k, v in fx.items() if k not in ("events", "lineups", "players")}
             for fx in fixtures]  # fmt: skip
    normalize.ingest_fixtures(s, season, lists, "England")
    store = LocalStore(tmp_path)
    blob, _ = encode_payload({"response": fixtures})
    store.put("api_football/fixtures/d.json.gz", blob)
    for (mid,) in s.execute(select(models.Match.match_id)):
        s.add(models.MatchDetail(match_id=mid, source_id="api_football",
                                 storage_key="api_football/fixtures/d.json.gz",
                                 observed_at=datetime(2023, 2, 1, tzinfo=UTC),
                                 flags={"returned": True}))  # fmt: skip
    s.commit()
    return s, store


def test_one_player_across_clubs_and_the_as_of_rule(tmp_path: Path) -> None:
    C = 30
    fixtures = [
        fixture(1, "FT", H, A, [lineup(H, XI_H, []), lineup(A, [7, *XI_A[1:]], [])], [],
                date="2023-01-07T15:00:00+00:00"),
        fixture(2, "FT", C, H, [lineup(C, [7, *range(302, 312)], []), lineup(H, XI_H, [])],
                [], date="2023-01-21T15:00:00+00:00"),
        fixture(3, "NS", H, A, [], [], date="2023-02-04T15:00:00+00:00"),  # not finished
    ]  # fmt: skip
    s, store = _world(tmp_path, fixtures)
    counts = ap.build(s, store)
    assert counts["files"] == 1 and counts["matches"] == 2 and counts["rows"] == 44
    frame = ap.load(store, [("39", 2022)])
    seven = frame.filter(pl.col("player_ref") == "7")
    assert seven["player_id"].n_unique() == 1 and seven["team_id"].n_unique() == 2
    assert s.scalar(select(func.count()).select_from(models.Player)) == 32  # 22, then 10 new
    assert frame["source_id"].unique().to_list() == ["api_football"]
    assert frame["valid_to"].null_count() == frame.height
    # a second build skips the stored file; --rebuild replaces it
    assert ap.build(s, store)["files_skipped"] == 1
    assert ap.build(s, store, rebuild=True)["files"] == 1
    hist = ap.AppearanceHistory(frame)
    second = datetime(2023, 1, 21, 15, tzinfo=UTC)
    assert hist.as_of(second + RESULT_LAG - timedelta(seconds=1))["match_id"].n_unique() == 1
    assert hist.as_of(second + RESULT_LAG)["match_id"].n_unique() == 2
