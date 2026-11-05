"""Historical backfill (task 6) against a fake API-Football: complete, idempotent, resumable."""

from itertools import permutations
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from adatta.adapters import backfill
from adatta.adapters.api_football import ApiFootballClient
from adatta.adapters.quota import QuotaPlanner
from adatta.adapters.storage import LocalStore
from adatta.config import Competition, Settings
from adatta.db import models
from adatta.db.models import Base

LEAGUE = Competition(
    name="Test League", country="X", league_id=900, level="senior", tier=1, priority=1,
    seasons=[2023, 2024],
)  # fmt: skip
CUP = Competition(
    name="Test Cup", country="X", league_id=901, level="senior", tier="cup", priority=3,
    seasons=[2024],
)  # fmt: skip
TEAMS = range(1, 7)  # 6 teams: 30 regular-season matches


def fixture(fid: int, home: int, away: int, status: str, round_: str) -> dict[str, Any]:
    played = status == "FT"
    return {
        "fixture": {"id": fid, "date": "2024-09-01T15:00:00+00:00", "status": {"short": status}},
        "league": {"id": 900, "round": round_},
        "teams": {"home": {"id": home, "name": f"T{home}"},
                  "away": {"id": away, "name": f"T{away}"}},
        "goals": {"home": 1 if played else None, "away": 0 if played else None},
        "score": {"halftime": {"home": 0 if played else None, "away": 0 if played else None},
                  "fulltime": {"home": 1 if played else None, "away": 0 if played else None}},
    }  # fmt: skip


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.id_requests: list[int] = []
        self.withheld = 5  # provider omits fixture #5 of each season from /fixtures?ids
        self.broken_league: int | None = None

    def season(self, league: int, year: int) -> list[dict[str, Any]]:
        base = league * 10_000_000 + year * 1000
        if league == 901:  # cup: a few knockout ties
            return [fixture(base + i, i, i + 10, "FT", "Round of 16") for i in range(1, 5)]
        out = []
        for i, (h, a) in enumerate(permutations(TEAMS, 2), start=1):
            status = "PST" if i == 30 else "FT"  # one match never played
            out.append(fixture(base + i, h, a, status, f"Regular Season - {i % 10 + 1}"))
        return out

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(f"{req.url.path}?{req.url.query.decode()}")
        q = req.url.params
        if req.url.path != "/fixtures":
            return httpx.Response(404)
        if "ids" in q:
            ids = [int(x) for x in q["ids"].split("-")]
            self.id_requests += ids
            body = []
            for fid in ids:
                if fid % 1000 == self.withheld:
                    continue
                fx = fixture(fid, 1, 2, "FT", "Regular Season - 1")
                xi = [{"player": {"id": i}} for i in range(11)]
                fx["lineups"] = [{"team": {"id": t}, "startXI": xi} for t in (1, 2)]
                fx["events"] = [{"type": "subst", "time": {"elapsed": 60}}]
                fx["statistics"] = [
                    {"team": {"id": t}, "statistics": [{"type": "expected_goals", "value": "1.1"}]}
                    for t in (1, 2)
                ]
                body.append(fx)
            return httpx.Response(200, json={"response": body})
        league = int(q["league"])
        if league == self.broken_league:
            return httpx.Response(200, json={"response": [{"fixture": {"id": 1}}]})  # malformed
        return httpx.Response(200, json={"response": self.season(league, int(q["season"]))})


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.api = FakeApi()
        self.tmp_path = tmp_path

    def run(self, comps: list[Competition], daily_limit: int = 7500) -> dict[str, int]:
        http = httpx.Client(base_url="https://example.test",
                            transport=httpx.MockTransport(self.api.handler))  # fmt: skip
        client = ApiFootballClient(
            Settings(), LocalStore(self.tmp_path), QuotaPlanner(daily_limit, 300), http=http
        )
        with Session(self.engine) as s:
            backfill.enqueue(s, comps)
            s.commit()
            return backfill.run(s, client, comps)

    def count(self, model: Any) -> int:
        with Session(self.engine) as s:
            return int(s.scalar(select(func.count()).select_from(model)) or 0)

    def report(self, comps: list[Competition]) -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            return backfill.completeness(s, comps)


def test_backfill_completes_and_reports_gaps(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    result = h.run([LEAGUE, CUP])
    # per league season: 1 fixtures call + 29 finished / 20 = 2 detail calls; cup: 1 + 1
    assert result == {"attempted": 3, "detail": 62, "done": 3, "requests": 8}
    assert h.count(models.Match) == 64 and h.count(models.MatchDetail) == 62
    assert h.count(models.TeamMatchStat) == 2 * (62 - 2)  # parsed inline; 2 fixtures withheld
    rows = {(r["competition"], r["season"]): r for r in h.report([LEAGUE, CUP])}
    league = rows[("Test League", 2024)]
    assert league["regular_expected"] == league["regular_listed"] == 30
    assert league["finished"] == 29 and league["not_played"] == "PST=1"
    assert league["finished_with_nt_score"] == 29
    assert league["detail_returned"] == 28 and league["has_lineups"] == 28
    assert league["flags"] == "detail missing 1"  # the withheld fixture, flagged not filled
    cup = rows[("Test Cup", 2024)]
    assert cup["regular_expected"] is None and cup["flags"] == ""
    with Session(h.engine) as s:
        job = s.scalars(
            select(models.BackfillJob).where(models.BackfillJob.season_year == 2024)
        ).first()
        assert job is not None and job.requests == 3 and job.n_fixtures == 30


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run([LEAGUE, CUP])
    rows = (h.count(models.Match), h.count(models.MatchResult), h.count(models.MatchDetail))
    again = h.run([LEAGUE, CUP])
    assert again == {"requests": 0}
    assert (h.count(models.Match), h.count(models.MatchResult), h.count(models.MatchDetail)) == rows


def test_quota_stop_resumes_without_refetching(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    # backfill budget = 70% of 3 = 2 requests: the fixtures list and one detail batch
    first = h.run([LEAGUE], daily_limit=3)
    assert first["stopped_by_quota"] == 1 and first["requests"] == 2
    assert h.count(models.MatchDetail) == 20
    h.run([LEAGUE])
    assert len(h.api.id_requests) == len(set(h.api.id_requests))  # no detail fetched twice
    assert sum(c.startswith("/fixtures?league=900&season=2024") for c in h.api.calls) == 1
    assert all(r["job_status"] == "done" for r in h.report([LEAGUE]))


def test_a_broken_season_fails_alone_and_is_retried(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.api.broken_league = 901
    result = h.run([LEAGUE, CUP])
    assert result["failed"] == 1 and result["done"] == 2
    cup = h.report([CUP])[0]
    assert cup["job_status"] == "failed" and "KeyError" in cup["flags"]
    h.api.broken_league = None
    assert h.run([LEAGUE, CUP])["done"] == 1
