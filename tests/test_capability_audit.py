from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_coverage as cov
from adatta.adapters.api_football import ApiFootballClient
from adatta.adapters.quota import QuotaPlanner
from adatta.adapters.storage import LocalStore
from adatta.audit import capability as audit
from adatta.config import Competition, Settings
from adatta.db import models
from adatta.db.models import Base

COMP = Competition(
    name="Test League", country="X", league_id=999, level="senior", tier=1, priority=1,
    seasons=[2024],
)  # fmt: skip


def detail(fid: int, *, rich: bool) -> dict[str, Any]:
    """A /fixtures?ids= item. rich=True carries every capability API-Football can provide."""

    def stats(xg: bool) -> dict[str, Any]:
        s = [
            {"type": "Total Shots", "value": 12},
            {"type": "Corner Kicks", "value": 5},
            {"type": "Yellow Cards", "value": None},
        ]
        if xg:
            s.append({"type": "expected_goals", "value": "1.42"})
        return {"team": {"id": 1}, "statistics": s}

    base: dict[str, Any] = {
        "fixture": {
            "id": fid,
            "date": "2024-08-17T14:00:00+00:00",
            "referee": "A. Ref" if rich else None,
            "status": {"short": "FT"},
        },
        "goals": {"home": 1, "away": 0},
        "score": {"halftime": {"home": 1, "away": 0}, "fulltime": {"home": 1, "away": 0}},
        "events": [],
        "lineups": [],
        "statistics": [],
        "players": [],
    }
    if rich:
        xi = [{"player": {"id": i, "grid": "1:1"}} for i in range(11)]
        base["lineups"] = [
            {"formation": "4-3-3", "startXI": xi},
            {"formation": "4-4-2", "startXI": xi},
        ]
        base["statistics"] = [stats(xg=fid % 2 == 0), stats(xg=fid % 2 == 0)]
        base["events"] = [
            {"time": {"elapsed": 60}, "type": "subst", "detail": "Substitution 1"},
            {"time": {"elapsed": 70}, "type": "Card", "detail": "Yellow Card"},
        ]
        p = {"player": {"id": 1}, "statistics": [{"games": {"minutes": 90}, "shots": {"total": 2}}]}
        base["players"] = [{"team": {"id": 1}, "players": [p]}, {"team": {"id": 2}, "players": [p]}]
    return base


def test_observed_flags_rich_and_bare() -> None:
    rich = cov.observed_flags(detail(2, rich=True))
    assert rich["has_lineups"] and rich["has_player_match_stats"] and rich["has_xg"]
    assert rich["has_cards_data"] and rich["has_substitution_timestamps"]
    assert not rich["has_event_coordinates"] and not rich["has_attendance_data"]
    bare = cov.observed_flags(detail(1, rich=False))
    assert bare["has_half_split_stats"]
    assert not any(v for k, v in bare.items() if k != "has_half_split_stats")


def test_zero_card_match_still_has_card_data() -> None:
    fx = detail(2, rich=True)
    fx["events"] = [e for e in fx["events"] if e["type"] != "Card"]
    assert cov.observed_flags(fx)["has_cards_data"]  # "Yellow Cards" listed as null = zero
    fx["statistics"] = []
    assert not cov.observed_flags(fx)["has_cards_data"]


def test_declared_flags_mapping() -> None:
    d = cov.declared_flags(
        {"fixtures": {"events": True, "lineups": False, "statistics_fixtures": True,
                      "statistics_players": True}}
    )  # fmt: skip
    assert d["has_lineups"] is False and d["has_shot_stats"] is True
    assert d["has_xg"] is None and d["has_referee_history"] is None
    assert all(v is None for v in cov.declared_flags(None).values())


@pytest.mark.parametrize(
    ("n_with", "n", "expected"),
    [(0, 0, ("unknown", None)), (40, 40, ("available", 1.0)), (20, 40, ("partial", 0.5)),
     (0, 40, ("absent", 0.0)), (38, 40, ("available", 0.95))],
)  # fmt: skip
def test_classify(n_with: int, n: int, expected: tuple[str, float | None]) -> None:
    assert audit.classify(n_with, n) == expected


def test_sample_is_deterministic_and_bounded() -> None:
    ids = list(range(1, 381))
    a = audit.sample_ids(ids, 40, seed=1, league_id=39, season=2024)
    assert a == audit.sample_ids(list(reversed(ids)), 40, seed=1, league_id=39, season=2024)
    assert len(a) == 40 and a != audit.sample_ids(ids, 40, seed=2, league_id=39, season=2024)
    assert audit.sample_ids([3, 1], 40, seed=1, league_id=39, season=2024) == [1, 3]


def make_client(tmp_path: Path, n_fixtures: int, calls: list[str]) -> ApiFootballClient:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        q = req.url.params
        if req.url.path == "/leagues":
            fx_cov = {"events": True, "lineups": True, "statistics_fixtures": True,
                      "statistics_players": False}  # fmt: skip
            body: Any = [{"seasons": [{"year": 2024, "coverage": {"fixtures": fx_cov}}]}]
        elif "ids" in q:
            body = [detail(int(i), rich=int(i) <= 30) for i in q["ids"].split("-")]
        else:
            start = datetime(2024, 8, 16, 19, tzinfo=UTC)
            body = [
                {"fixture": {"id": i, "date": (start + timedelta(days=i)).isoformat(),
                             "status": {"short": "FT" if i <= n_fixtures - 5 else "NS"}}}
                for i in range(1, n_fixtures + 1)
            ]  # fmt: skip
        return httpx.Response(200, json={"response": body})

    http = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))
    return ApiFootballClient(Settings(), LocalStore(tmp_path), QuotaPlanner(7500, 300), http=http)


def caps(s: Session) -> dict[str, models.CompetitionSourceCapability]:
    rows = s.scalars(select(models.CompetitionSourceCapability)).all()
    return {r.capability: r for r in rows if r.valid_to is None}


def test_run_audit_fills_table_and_reaudit_is_idempotent(tmp_path: Path) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls: list[str] = []
    client = make_client(tmp_path, n_fixtures=45, calls=calls)  # 40 finished, ids 1..40
    with Session(engine) as s:
        audits, errors = audit.run_audit(s, client, [COMP], sample=40, seed=7)
        assert errors == []
        # /leagues + /fixtures list + two ids batches of 20
        assert len(calls) == 4 and client.requests_made == 4
        got = caps(s)
        n_caps = len(cov.OBSERVED_CAPABILITIES) + len(cov.POLICY_CAPABILITIES)
        assert len(got) == n_caps
        assert got["has_lineups"].status == "partial" and got["has_lineups"].coverage_ratio == 0.75
        assert got["has_half_split_stats"].status == "available"
        assert got["has_event_coordinates"].status == "absent"
        assert got["supports_player_goal_props"].status == "unknown"
        assert got["supports_player_goal_props"].coverage_ratio is None
        assert got["has_lineups"].valid_from.replace(tzinfo=UTC) == datetime(
            2024, 8, 17, 19, tzinfo=UTC
        )
        # declared statistics_players=False but 75% of payloads have player stats
        mism = {r.capability for r in audits[0].results if r.mismatch}
        assert "has_player_match_stats" in mism

        first_audit = got["has_lineups"].last_audited_at
        audit.run_audit(s, client, [COMP], sample=40, seed=7)
        rows = s.scalars(select(models.CompetitionSourceCapability)).all()
        assert len(rows) == n_caps  # confirmed, not duplicated
        assert caps(s)["has_lineups"].last_audited_at >= first_audit


def test_status_change_supersedes_open_row(tmp_path: Path) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        a = audit.SeasonAudit(999, "Test League", 2024, 10, [], None)
        a.results = [audit.CapabilityResult("has_xg", True, 10, 0, "absent", 0.0)]
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        audit.write_capabilities(s, COMP, a, now=t0)
        a.results = [audit.CapabilityResult("has_xg", True, 10, 10, "available", 1.0)]
        counts = audit.write_capabilities(s, COMP, a, now=t0 + timedelta(days=30))
        assert counts == {"inserted": 0, "confirmed": 0, "superseded": 1}
        rows = s.scalars(
            select(models.CompetitionSourceCapability).order_by(
                models.CompetitionSourceCapability.capability_id
            )
        ).all()
        assert [r.status for r in rows] == ["absent", "available"]
        assert rows[0].valid_to is not None and rows[1].valid_to is None


def test_missing_ids_in_batch_is_an_error_not_a_silent_gap(tmp_path: Path) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/leagues":
            return httpx.Response(200, json={"response": []})
        if "ids" in req.url.params:
            return httpx.Response(200, json={"response": [detail(1, rich=True)]})
        fx = [{"fixture": {"id": i, "date": "2024-08-17T14:00:00+00:00",
                           "status": {"short": "FT"}}} for i in (1, 2)]  # fmt: skip
        return httpx.Response(200, json={"response": fx})

    http = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))
    client = ApiFootballClient(Settings(), LocalStore(tmp_path), QuotaPlanner(7500, 300), http=http)
    with Session(engine) as s:
        audits, errors = audit.run_audit(s, client, [COMP])
        assert audits == [] and len(errors) == 1 and "did not return" in errors[0]
        assert s.scalars(select(models.CompetitionSourceCapability)).all() == []


def test_ratio_change_at_exact_tolerance_does_not_supersede() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        a = audit.SeasonAudit(999, "Test League", 2024, 40, [], None)
        a.results = [audit.CapabilityResult("has_cards_data", True, 40, 38, "available", 0.95)]
        audit.write_capabilities(s, COMP, a)
        a.results = [audit.CapabilityResult("has_cards_data", True, 40, 40, "available", 1.0)]
        assert audit.write_capabilities(s, COMP, a)["confirmed"] == 1
