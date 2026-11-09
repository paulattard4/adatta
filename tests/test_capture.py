"""Snapshot capture against a fake API-Football, driven through a simulated poll timeline."""

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from adatta.adapters.api_football import ApiFootballClient
from adatta.adapters.quota import QuotaPlanner
from adatta.adapters.storage import LocalStore
from adatta.capture import alert
from adatta.capture.jobs import run_capture
from adatta.config import Competition, Settings
from adatta.db import models
from adatta.db.models import Base

COMP = Competition(
    name="Test League", country="X", league_id=999, level="senior", tier=1, priority=1,
    seasons=[2026],
)  # fmt: skip
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
K = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)  # kickoff of fixture 1 (9+ days after T0)


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.injury_reason = "Knee Injury"
        self.injured = True
        self.lineups_published = True
        self.odds_offered = True
        self.n_fixtures = 1

    def fixture(self, fid: int, kickoff: datetime) -> dict[str, Any]:
        return {
            "fixture": {"id": fid, "date": kickoff.isoformat(), "referee": None,
                        "status": {"short": "NS"}, "venue": {"id": 5, "name": "V", "city": "C"}},
            "league": {"id": 999, "round": "Regular Season - 11"},
            "teams": {"home": {"id": 1, "name": "H"}, "away": {"id": 2, "name": "A"}},
            "goals": {"home": None, "away": None},
            "score": {"halftime": {"home": None, "away": None}},
        }  # fmt: skip

    def handler(self, req: httpx.Request, now: datetime) -> httpx.Response:
        self.calls.append(f"{req.url.path}?{req.url.query.decode()}")
        q = req.url.params
        body: Any
        if req.url.path == "/leagues":
            body = [
                {"seasons": [{"year": 2025, "current": False}, {"year": 2026, "current": True}]}
            ]
        elif req.url.path == "/fixtures" and "ids" in q:
            body = []
            for fid in q["ids"].split("-"):
                fx = self.fixture(int(fid), K)
                if self.lineups_published and now >= K - timedelta(minutes=60):
                    xi = [{"player": {"id": i, "name": f"P{i}", "grid": "1:1"}} for i in range(11)]
                    fx["lineups"] = [
                        {"team": {"id": t}, "formation": "4-4-2", "coach": {"id": 9, "name": "C"},
                         "startXI": xi, "substitutes": []} for t in (1, 2)
                    ]  # fmt: skip
                body.append(fx)
        elif req.url.path == "/fixtures":
            body = [self.fixture(i, K) for i in range(1, self.n_fixtures + 1)]
        elif req.url.path == "/injuries":
            body = []
            if self.injured:
                body = [{
                    "player": {"id": 77, "name": "Hurt", "type": "Missing Fixture",
                               "reason": self.injury_reason},
                    "team": {"id": 1, "name": "H"},
                    "fixture": {"id": 1, "date": K.isoformat()},
                    "league": {"id": 999, "season": 2026},
                }]  # fmt: skip
        elif req.url.path == "/odds":
            body = []
            if self.odds_offered and now >= K - timedelta(days=8):
                body = [{
                    "update": (now - timedelta(minutes=3)).isoformat(),
                    "bookmakers": [{"name": "Pinnacle", "bets": [
                        {"name": "Match Winner", "values": [
                            {"value": "Home", "odd": "2.10"}, {"value": "Draw", "odd": "3.40"},
                            {"value": "Away", "odd": "3.60"}]},
                        {"name": "Goals Over/Under", "values": [
                            {"value": "Over 2.5", "odd": "1.90"},
                            {"value": "Under 2.5", "odd": "1.95"},
                            {"value": "Over 3.5", "odd": "3.10"}]},
                        {"name": "Exact Score", "values": [{"value": "1:0", "odd": "8.0"}]},
                    ]}],
                }]  # fmt: skip
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"response": body})


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.api = FakeApi()
        self.now = T0
        self.tmp_path = tmp_path
        self.daily_limit = 7500
        self.fetch_delay = timedelta(0)  # how long after the tick's start each response arrives

    def tick(self, now: datetime, **kwargs: Any) -> Any:
        self.now = now
        http = httpx.Client(
            base_url="https://example.test",
            transport=httpx.MockTransport(lambda r: self.api.handler(r, self.now)),
        )
        client = ApiFootballClient(
            Settings(), LocalStore(self.tmp_path), QuotaPlanner(self.daily_limit, 300), http=http,
            clock=lambda: self.now + self.fetch_delay,
        )  # fmt: skip
        with Session(self.engine) as s:
            return run_capture(s, client, [COMP], now=now, **kwargs)

    def captures(self) -> dict[str, str]:
        with Session(self.engine) as s:
            rows = s.scalars(select(models.OddsCapture)).all()
            return {r.snapshot_kind: r.status for r in rows}

    def count(self, model: Any) -> int:
        with Session(self.engine) as s:
            return int(s.scalar(select(func.count()).select_from(model)) or 0)


def full_timeline() -> list[datetime]:
    return [
        T0,                                          # calendar, status; no market yet
        K - timedelta(days=6, hours=12),             # T-7d (+ initial, first market)
        K - timedelta(hours=22),                     # T-24h (next daily jobs too)
        K - timedelta(minutes=70),                   # lineup poll: not out yet
        K - timedelta(minutes=55),                   # T-60m; lineups out -> lineup kind
        K - timedelta(minutes=10),                   # close
        K + timedelta(hours=2),                      # after kickoff: nothing missed
    ]  # fmt: skip


def test_full_timeline_captures_every_milestone(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    reports = [h.tick(t) for t in full_timeline()]
    assert all(r.ok for r in reports), [(r.missed, r.errors) for r in reports]
    assert h.captures() == {k: "captured" for k in
                            ("initial", "T-7d", "T-24h", "T-60m", "lineup", "close")}  # fmt: skip
    assert h.count(models.LineupSnapshot) == 2
    # normalised subset only: 1X2 (3) + OU 2.5 (2); Exact Score and OU 3.5 stay in raw payload
    with Session(h.engine) as s:
        rows = s.scalars(
            select(models.OddsSnapshot).where(models.OddsSnapshot.snapshot_kind == "close")
        ).all()
        assert sorted((r.market, r.line, r.selection) for r in rows) == [
            ("1X2", None, "Away"), ("1X2", None, "Draw"), ("1X2", None, "Home"),
            ("OU", 2.5, "Over"), ("OU", 2.5, "Under"),
        ]  # fmt: skip
        cap = s.scalar(
            select(models.OddsCapture).where(models.OddsCapture.snapshot_kind == "close")
        )
        assert cap is not None and cap.storage_key and cap.published_at is not None


def test_rerunning_a_tick_is_idempotent(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t = K - timedelta(days=6, hours=12)
    h.tick(T0)
    h.tick(t)
    calls_before = len(h.api.calls)
    snapshot_rows = h.count(models.OddsSnapshot)
    again = h.tick(t)
    assert again.ok and again.requests == 0 and len(h.api.calls) == calls_before
    assert h.count(models.OddsSnapshot) == snapshot_rows


def test_skipped_poll_is_reported_missed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for t in full_timeline()[:4]:
        h.tick(t)
    # scheduler outage: no tick inside the T-60m window [K-60m, K-40m)
    r = h.tick(K - timedelta(minutes=30))
    assert len(r.missed) == 1 and "T-60m missed" in r.missed[0]
    assert h.captures()["T-60m"] == "missed"
    assert h.tick(K - timedelta(minutes=29)).missed == []  # reported once, not every tick


def test_unpublished_lineup_and_absent_market_are_not_alerts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.api.lineups_published = False
    h.api.odds_offered = False
    reports = [h.tick(t) for t in full_timeline()]
    assert all(r.ok for r in reports)
    caps = h.captures()
    assert caps["lineup"] == "not_published" and caps["initial"] == "no_market"
    assert caps["T-24h"] == "no_market"


def test_status_snapshots_version_and_clear(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.tick(T0)
    h.api.injury_reason = "Hamstring Injury"
    h.tick(T0 + timedelta(days=1))
    h.api.injured = False
    h.tick(T0 + timedelta(days=2))
    with Session(h.engine) as s:
        rows = s.scalars(
            select(models.PlayerStatusSnapshot).order_by(models.PlayerStatusSnapshot.status_id)
        ).all()
        assert [r.reason for r in rows] == ["Knee Injury", "Hamstring Injury"]
        assert rows[0].superseded_at is not None  # changed
        assert rows[1].valid_to is not None and rows[1].superseded_at is None  # cleared
        assert all(r.expected_return is None for r in rows)  # not provided by the source


def test_daily_jobs_run_once_per_day(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.tick(T0)
    n = sum(c.startswith("/leagues") for c in h.api.calls)
    h.tick(T0 + timedelta(hours=1))
    assert sum(c.startswith("/leagues") for c in h.api.calls) == n == 1
    assert h.count(models.FixtureSnapshot) == 1  # unchanged fixture: no new snapshot
    h.tick(T0 + timedelta(hours=21))
    assert sum(c.startswith("/leagues") for c in h.api.calls) == 2


def test_time_critical_work_runs_before_daily_jobs(tmp_path: Path) -> None:
    """2026-11-06: overdue daily jobs used a whole 10-minute run and odds were never reached."""
    h = Harness(tmp_path)
    h.tick(T0)
    h.tick(K - timedelta(days=6, hours=12))
    n = sum(c.startswith("/leagues") for c in h.api.calls)
    r = h.tick(K - timedelta(hours=22), daily_budget_s=0)  # daily jobs overdue, no time left
    assert h.captures()["T-24h"] == "captured"
    assert r.actions["daily_deferred"] == 1 and r.ok
    assert sum(c.startswith("/leagues") for c in h.api.calls) == n
    h.tick(K - timedelta(hours=21, minutes=45))  # the next tick picks the daily jobs up
    assert sum(c.startswith("/leagues") for c in h.api.calls) == n + 1


def test_odds_already_fetched_survive_a_later_failure(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.api.n_fixtures = 2
    h.tick(T0)
    h.daily_limit = 1  # the second fixture's odds request is refused
    r = h.tick(K - timedelta(hours=22))
    assert r.errors
    with Session(h.engine) as s:
        rows = s.scalars(select(models.OddsCapture)).all()
    assert sum(r.snapshot_kind == "T-24h" for r in rows) == 1


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)  # SQLite returns naive UTC


def test_rows_carry_the_response_time_not_the_tick_start(tmp_path: Path) -> None:
    """A busy tick can fetch minutes after it starts; stamping the start would claim the data
    was known before it was (principle 2)."""
    h = Harness(tmp_path)
    h.fetch_delay = timedelta(minutes=3)
    h.tick(T0)
    h.tick(K - timedelta(hours=22))
    with Session(h.engine) as s:
        fixture = s.scalars(select(models.FixtureSnapshot)).one()
        status = s.scalars(select(models.PlayerStatusSnapshot)).first()
        t24 = s.scalars(
            select(models.OddsCapture).where(models.OddsCapture.snapshot_kind == "T-24h")
        ).one()
        prices = s.scalars(
            select(models.OddsSnapshot).where(models.OddsSnapshot.capture_id == t24.capture_id)
        ).all()
    delay = timedelta(minutes=3)
    assert _naive(fixture.observed_at) == _naive(T0 + delay)
    assert status is not None
    assert _naive(status.observed_at) == _naive(status.valid_from) == _naive(T0 + delay)
    assert _naive(t24.observed_at) == _naive(K - timedelta(hours=22) + delay)
    assert prices and {_naive(p.observed_at) for p in prices} == {_naive(t24.observed_at)}


def test_window_closing_during_the_tick_is_not_labelled(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for t in full_timeline()[:4]:
        h.tick(t)
    h.fetch_delay = timedelta(minutes=2)
    h.tick(K - timedelta(minutes=41))  # T-60m closes at K-40m; the response arrives at K-39m
    assert "T-60m" not in h.captures()
    assert h.captures()["lineup"] == "captured"  # untimed: still before kickoff
    h.fetch_delay = timedelta(0)
    r = h.tick(K - timedelta(minutes=30))
    assert len(r.missed) == 1 and "T-60m missed" in r.missed[0]


def test_kickoff_during_the_tick_records_no_close(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for t in full_timeline()[:5]:
        h.tick(t)
    h.fetch_delay = timedelta(minutes=2)
    h.tick(K - timedelta(minutes=1))  # the response arrives after kickoff
    assert "close" not in h.captures()
    h.fetch_delay = timedelta(0)
    r = h.tick(K + timedelta(minutes=5))
    assert len(r.missed) == 1 and "close missed" in r.missed[0]


def test_alert_keeps_match_detail_out_of_public_logs(capsys: pytest.CaptureFixture[str]) -> None:
    sent: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req.content)
        return httpx.Response(200)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(alert_webhook_url="https://example.test/hook")
    assert alert.send(settings, "adatta capture: 1 problem(s)\nmatch 123: close missed", http)
    assert capsys.readouterr().err == "ALERT adatta capture: 1 problem(s)\n"
    assert "match 123" in json.loads(sent[0])["text"]


def _statements_per_tick(tmp_path: Path, n_fixtures: int) -> int:
    h = Harness(tmp_path / str(n_fixtures))
    h.api.n_fixtures = n_fixtures
    h.api.odds_offered = False  # odds calls are per match by design; count DB work only
    count = 0

    def on_execute(_conn: Any, _cur: Any, statement: str, *_: Any) -> None:
        nonlocal count
        sql = statement.lstrip().upper()
        # raw_payload lookups are one per API request (storage dedupe), not per fixture
        count += sql.startswith("SELECT") and "FROM RAW_PAYLOAD" not in sql

    event.listen(h.engine, "before_cursor_execute", on_execute)
    for t in (T0, K - timedelta(minutes=55), K + timedelta(hours=2)):
        h.tick(t)
    return count


def test_lookups_do_not_scale_with_fixture_count(tmp_path: Path) -> None:
    """Against a remote Postgres each statement is a network round trip; per-fixture lookups
    made one season's calendar take minutes. Lookups must be batched. (Counts SELECTs only:
    inserts are batched by the driver; storage lookups scale with API requests.)"""
    few, many = _statements_per_tick(tmp_path, 2), _statements_per_tick(tmp_path, 60)
    assert many - few <= 5, (few, many)


def test_odds_are_never_imported_by_model_feature_or_sim_code() -> None:
    """Principle 1: odds are evaluation-only, downstream of the model."""
    forbidden = {"OddsSnapshot", "OddsCapture", "odds_snapshot", "odds_capture"}
    pkg = Path(__file__).resolve().parents[1] / "adatta"
    offenders = []
    for sub in ("model", "eval", "sim", "features"):
        for path in (pkg / sub).rglob("*.py") if (pkg / sub).exists() else []:
            names = {
                n.id if isinstance(n, ast.Name) else n.attr
                for n in ast.walk(ast.parse(path.read_text()))
                if isinstance(n, ast.Name | ast.Attribute)
            }
            text_hits = {f for f in forbidden if f in path.read_text()}
            if names & forbidden or text_hits:
                offenders.append(str(path))
    assert offenders == []
