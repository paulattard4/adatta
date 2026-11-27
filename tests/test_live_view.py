"""The dashboard's "In play" view (docs/DASHBOARD_TASKS.md, option 1): the provider's live
score beside the latest projection published before kickoff; no in-play recalculation."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import test_serve
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_dashboard_api import ctx, published
from test_serve import K

from adatta.adapters.api_football import (
    ApiFootballClient,
    LiveState,
    ProviderError,
    ProviderQuotaExceeded,
    parse_live,
)
from adatta.adapters.quota import QuotaExceeded, QuotaPlanner
from adatta.adapters.storage import LocalStore
from adatta.api import views
from adatta.api.app import create_app
from adatta.api.live import LiveFeed
from adatta.config import Settings
from adatta.db import models

IN_PLAY = K + timedelta(minutes=30)


def payload(*items: tuple[int, str, int | None, int | None, int | None],
            league: int = 39) -> dict[str, Any]:  # fmt: skip
    return {"errors": [], "response": [
        {"fixture": {"id": fid, "status": {"short": st, "elapsed": el, "extra": None}},
         "league": {"id": league}, "goals": {"home": hg, "away": ag},
         "teams": {"home": {"name": "x"}}}
        for fid, st, el, hg, ag in items
    ]}  # fmt: skip


def test_parse_live_reads_score_status_and_minute() -> None:
    got = parse_live(payload((1001, "2H", 63, 1, 0), (1002, "HT", 45, None, None)), [39])
    assert got == [LiveState(1001, "2H", 63, None, 1, 0), LiveState(1002, "HT", 45, None, None,
                                                                    None)]  # fmt: skip
    assert parse_live(payload((1003, "1H", 3, 0, 0), league=5), [39]) == []  # not our league
    assert parse_live({"response": [{"fixture": {}}]}, [39]) == [] and parse_live({}, [39]) == []
    # The provider reports bad parameters in a 200 body: that must not read as "none live".
    bad = {"errors": {"live": "does not match [id-id-id...] or all"}, "response": []}
    with pytest.raises(ProviderError):
        parse_live(bad, [39])


def test_live_request_goes_through_the_quota_planner_and_keeps_no_payload(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=payload((1001, "1H", 12, 0, 0)),
                              headers={"x-ratelimit-requests-remaining": "7000"})  # fmt: skip

    quota = QuotaPlanner(7500, 300)
    store = LocalStore(tmp_path)
    client = ApiFootballClient(
        Settings(), store, quota,
        http=httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(handler)),
    )  # fmt: skip
    assert client.live_fixtures([40, 39, 39]) == [LiveState(1001, "1H", 12, None, 0, 0)]
    assert seen[0].url.params["live"] == "all"  # one request; a single id is rejected
    assert quota.used_today == 500  # synced from the provider's account-wide count
    assert not any(tmp_path.rglob("*.gz"))  # display feed: nothing kept
    full = QuotaPlanner(1, 300)
    full.acquire(live=True)
    with pytest.raises(QuotaExceeded):
        ApiFootballClient(Settings(), store, full,
                          http=httpx.Client(base_url="https://x.test",
                                            transport=httpx.MockTransport(handler)),
                          ).live_fixtures([39])  # fmt: skip


class FakeProvider:
    def __init__(self, states: list[LiveState]) -> None:
        self.states = states
        self.calls = 0
        self.fail = False

    def __call__(self, league_ids: list[int]) -> list[LiveState]:
        self.calls += 1
        if self.fail:
            raise httpx.ConnectTimeout("handshake")
        return self.states


def test_feed_asks_once_a_minute_and_stops_at_the_daily_cap() -> None:
    p = FakeProvider([LiveState(1001, "1H", 5, None, 0, 0)])
    feed = LiveFeed(p, daily_cap=3)
    t = IN_PLAY
    for dt in (0, 10, 59):  # many viewers within a minute: one request
        snap = feed.get(t + timedelta(seconds=dt), [39])
    assert p.calls == 1 and snap.fetched_at == t and snap.note is None
    feed.get(t + timedelta(seconds=60), [39])
    assert p.calls == 2
    p.fail = True  # a failed request keeps the last good state and says so
    snap = feed.get(t + timedelta(seconds=120), [39])
    assert p.calls == 3 and snap.states and snap.fetched_at == t + timedelta(seconds=60)
    assert snap.note and "unavailable" in snap.note
    p.fail = False
    snap = feed.get(t + timedelta(seconds=180), [39])  # cap of 3 reached: no request
    assert p.calls == 3 and snap.note and "daily limit" in snap.note
    snap = feed.get(t + timedelta(days=1), [39])  # a new UTC day resets it
    assert p.calls == 4 and snap.note is None


def test_live_view_shows_the_pre_match_projection_beside_the_live_score(
    tmp_path: Path,
) -> None:
    w, mid, later = published(tmp_path)
    ref = w.s.get(models.Match, mid).source_ref  # type: ignore[union-attr]
    p = FakeProvider([LiveState(int(ref), "2H", 63, None, 1, 0), LiveState(5, "1H", 3, None, 0,
                                                                           0)])  # fmt: skip
    feed = LiveFeed(p)
    v = views.live(w.s, ctx(w), IN_PLAY, feed)
    assert [m.match_id for m in v.matches] == [mid]  # matches not in the bundle are ignored
    m = v.matches[0]
    assert (m.status, m.elapsed, m.home_goals, m.away_goals) == ("2H", 63, 1, 0)
    assert m.forecast is not None and m.forecast.generated_at < K  # published before kickoff
    assert abs(m.forecast.home.p + m.forecast.draw.p + m.forecast.away.p - 1) < 1e-9
    assert m.competition_tier == "1" and v.fetched_at == IN_PLAY and v.note is None
    assert m.team_level == "senior"  # the page shows pre-match fair odds for senior only


def test_no_request_when_no_bundle_match_can_be_live(tmp_path: Path) -> None:
    w, _, _ = published(tmp_path)
    p = FakeProvider([])
    for now in (K - timedelta(hours=1), K + timedelta(hours=5)):  # before the window, after
        v = views.live(w.s, ctx(w), now, LiveFeed(p))
        assert v.matches == [] and v.note is None
    assert p.calls == 0
    v = views.live(w.s, ctx(w), IN_PLAY, None)  # in the window but no feed configured
    assert v.matches == [] and v.note == "live feed not configured"


def test_live_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        test_serve, "create_engine",
        lambda url: create_engine(url, poolclass=StaticPool,
                                  connect_args={"check_same_thread": False}),
    )  # fmt: skip
    w, mid, _ = published(tmp_path)
    ref = w.s.get(models.Match, mid).source_ref  # type: ignore[union-attr]
    feed = LiveFeed(FakeProvider([LiveState(int(ref), "HT", 45, 2, 0, 0)]))

    def sessions() -> Session:
        return Session(w.engine)

    clock: list[datetime] = [IN_PLAY]
    client = TestClient(create_app(sessions, ctx(w), clock=lambda: clock[0], live_feed=feed))
    body = client.get("/api/live").json()
    assert body["matches"][0]["status"] == "HT" and body["matches"][0]["extra"] == 2
    assert body["matches"][0]["forecast"]["home"]["lo"] is not None


def test_feed_reports_a_provider_error_as_unavailable() -> None:
    def rejects(league_ids: list[int]) -> list[LiveState]:
        raise ProviderError("live feed: {'live': 'bad'}")

    snap = LiveFeed(rejects).get(IN_PLAY, [39])
    assert snap.states == [] and snap.fetched_at is None
    assert snap.note == "live feed unavailable (ProviderError)"


def test_fetch_raises_on_a_body_error_and_keeps_nothing(tmp_path: Path) -> None:
    """The main fetch path (capture, backfill): an error body raises before anything is
    stored, the quota still syncs, and a request-limit error is a QuotaExceeded."""
    body: dict[str, Any] = {"errors": {"season": "bad season"}, "response": []}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, headers={"x-ratelimit-requests-remaining": "7400"})

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    quota = QuotaPlanner(7500, 300)
    client = ApiFootballClient(
        Settings(), LocalStore(tmp_path), quota,
        http=httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(handler)),
    )  # fmt: skip
    with Session(engine) as s:
        with pytest.raises(ProviderError) as err:
            client.fixtures(s, 39, 1999, live=True)
        assert not isinstance(err.value, QuotaExceeded)
        assert "/fixtures (league, season)" in str(err.value)
        assert s.query(models.RawPayload).count() == 0 and not any(tmp_path.rglob("*.gz"))
        assert quota.used_today == 100  # synced before the check
        body["errors"] = {"rateLimit": "Too many requests"}
        with pytest.raises(ProviderQuotaExceeded):
            client.fixtures(s, 39, 2026, live=True)
        body["errors"] = {}
        assert client.fixtures(s, 39, 2026, live=True) == []
        assert s.query(models.RawPayload).count() == 1


def _competition_match(w: Any, ref: str, kickoff: datetime, national: bool = False,
                       tier: str = "1") -> int:  # fmt: skip
    c = models.Competition(source_id="api_football", source_ref=ref, name=f"L{ref}",
                           country="Y", tier=tier, is_national=national)  # fmt: skip
    w.s.add(c)
    w.s.flush()
    se = models.Season(competition_id=c.competition_id, year=2026)
    w.s.add(se)
    w.s.flush()
    w.n += 1
    m = models.Match(source_id="api_football", source_ref=str(1000 + w.n),
                     season_id=se.season_id, home_team_id=3, away_team_id=4,
                     kickoff_time=kickoff, status="NS", round="Regular Season - 12")  # fmt: skip
    w.s.add(m)
    w.s.flush()
    return m.match_id


def test_live_view_covers_c2_top_flights_and_national_competitions(tmp_path: Path) -> None:
    """Every served competition, as on the played view: a C2 top flight (Portugal, 94) and a
    national competition are in play beside the bundle match; a C2 second tier is not."""
    from adatta.api import c2 as c2v

    w, mid, _ = published(tmp_path)
    assert 94 in c2v.leagues() and 95 not in c2v.leagues()
    c2_mid = _competition_match(w, "94", K)
    second = _competition_match(w, "95", K, tier="2")
    nat = _competition_match(w, "10", K, national=True)
    w.s.commit()
    refs = {
        i: w.s.get(models.Match, i).source_ref  # type: ignore[union-attr]
        for i in (mid, c2_mid, second, nat)
    }
    p = FakeProvider([LiveState(int(r), "1H", 20, None, 0, 0) for r in refs.values()])
    v = views.live(w.s, ctx(w), IN_PLAY, LiveFeed(p))
    assert {m.match_id for m in v.matches} == {mid, c2_mid, nat}
    assert p.calls == 1  # one provider request covers every competition
    assert next(m for m in v.matches if m.match_id == c2_mid).forecast is None  # none published
