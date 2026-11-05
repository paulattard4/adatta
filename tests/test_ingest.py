from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.db import models
from adatta.db.models import Base


def fx(hg: int, ag: int) -> dict:  # type: ignore[type-arg]
    return {
        "fixture": {"id": 1, "date": "2025-08-16T14:00:00+00:00", "status": {"short": "FT"}},
        "teams": {"home": {"id": 10, "name": "A"}, "away": {"id": 20, "name": "B"}},
        "goals": {"home": hg, "away": ag},
        "score": {"halftime": {"home": 0, "away": 0}},
    }


def test_ingest_is_idempotent_and_versions_corrections() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        normalize.ensure_source(s)
        comp = normalize.ensure_competition(
            s, league_id=39, name="PL", country="England", level="senior", tier="1"
        )
        season = normalize.ensure_season(s, comp.competition_id, 2025)
        assert normalize.ingest_fixtures(s, season, [fx(1, 0)], "England") == 1
        assert normalize.ingest_fixtures(s, season, [fx(1, 0)], "England") == 0  # idempotent
        assert normalize.ingest_fixtures(s, season, [fx(2, 0)], "England") == 1  # correction
        rows = s.scalars(select(models.MatchResult)).all()
        assert len(rows) == 2
        live = [r for r in rows if r.superseded_at is None]
        assert len(live) == 1 and live[0].home_goals_ft == 2
        assert datetime.now(UTC)  # sanity
