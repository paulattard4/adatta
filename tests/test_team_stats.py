"""Team match stats (Phase 1b task 1): parsing rules, versioning, rebuild from the raw store."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_stats as st
from adatta.adapters import normalize
from adatta.adapters.detail_store import load_team_stats
from adatta.adapters.storage import LocalStore, encode_payload
from adatta.db import models
from adatta.db.models import Base


def fx(fid: int, xg: str | None = "1.42", yellows: Any = None) -> dict[str, Any]:
    def block(team: int) -> dict[str, Any]:
        return {"team": {"id": team}, "statistics": [
            {"type": "Total Shots", "value": 12}, {"type": "Shots on Goal", "value": 5},
            {"type": "Yellow Cards", "value": yellows}, {"type": "Ball Possession", "value": "55%"},
            {"type": "expected_goals", "value": xg},
        ]}  # fmt: skip

    return {"fixture": {"id": fid}, "statistics": [block(10), block(20)]}


def test_parsing_rules() -> None:
    rows = st.team_stats(fx(1, xg=None))
    assert [r["team_ref"] for r in rows] == ["10", "20"]
    r = rows[0]
    assert r["shots_total"] == 12 and r["shots_on_target"] == 5 and r["possession"] == 55.0
    assert r["yellow_cards"] == 0  # listed with null: provider omits zero counts
    assert r["corners"] is None  # not listed at all: unknown, not zero
    assert r["xg"] is None  # xG null is absent, never imputed
    assert st.team_stats({"statistics": [{"team": {"id": 1}, "statistics": []}]}) == []


def setup() -> tuple[Session, int]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    normalize.ensure_source(s)
    comp = normalize.ensure_competition(
        s, league_id=39, name="PL", country="E", level="senior", tier="1"
    )
    season = normalize.ensure_season(s, comp.competition_id, 2023)
    normalize.ingest_fixtures(s, season, [{
        "fixture": {"id": 1, "date": "2023-09-01T15:00:00+00:00", "status": {"short": "FT"}},
        "teams": {"home": {"id": 10, "name": "A"}, "away": {"id": 20, "name": "B"}},
        "goals": {"home": 1, "away": 0}, "score": {"halftime": {"home": 0, "away": 0}},
    }], "E")  # fmt: skip
    mid = s.scalar(select(models.Match.match_id))
    assert mid is not None
    return s, mid


def test_ingest_versions_changed_stats_and_skips_identical() -> None:
    s, mid = setup()
    t0, t1 = datetime(2023, 9, 2, tzinfo=UTC), datetime(2023, 9, 3, tzinfo=UTC)
    assert normalize.ingest_team_stats(s, mid, st.team_stats(fx(1)), "k", t0) == 2
    assert normalize.ingest_team_stats(s, mid, st.team_stats(fx(1)), "k", t1) == 0
    assert normalize.ingest_team_stats(s, mid, st.team_stats(fx(1, xg="1.50")), "k2", t1) == 2
    rows = s.scalars(select(models.TeamMatchStat)).all()
    live = [r for r in rows if r.superseded_at is None]
    assert len(rows) == 4 and len(live) == 2 and {r.xg for r in live} == {1.5}


def test_rebuild_from_raw_store_is_resumable(tmp_path: Path) -> None:
    s, mid = setup()
    store = LocalStore(tmp_path)
    blob, _ = encode_payload({"response": [fx(1)]})
    store.put("api_football/fixtures/x.json.gz", blob)
    s.add(models.MatchDetail(match_id=mid, source_id="api_football",
                             storage_key="api_football/fixtures/x.json.gz",
                             observed_at=datetime(2023, 9, 2, tzinfo=UTC),
                             flags={"returned": True}))  # fmt: skip
    s.commit()
    assert load_team_stats(s, store, workers=2) == {
        "files": 1,
        "matches": 1,
        "rows": 2,
        "no_stats": 0,
    }
    assert load_team_stats(s, store, workers=2)["files"] == 0  # nothing left to do
