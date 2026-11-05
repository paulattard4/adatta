from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters.normalize import ScoreSplit, split_score
from adatta.db import models
from adatta.db.models import Base


def fx(
    status: str,
    goals: tuple[int | None, int | None],
    ft: tuple[int | None, int | None] | None,
    et: tuple[int | None, int | None] | None = None,
    pen: tuple[int | None, int | None] | None = None,
) -> dict[str, Any]:
    def side(t: tuple[int | None, int | None] | None) -> dict[str, int | None]:
        return {"home": t[0], "away": t[1]} if t else {"home": None, "away": None}

    return {
        "fixture": {"id": 7, "date": "2025-01-11T15:00:00+00:00", "status": {"short": status}},
        "teams": {"home": {"id": 1, "name": "H"}, "away": {"id": 2, "name": "A"}},
        "goals": side(goals),
        "score": {
            "halftime": {"home": 0, "away": 1},
            "fulltime": side(ft),
            "extratime": side(et),
            "penalty": side(pen),
        },
    }


def test_regular_time_uses_fulltime() -> None:
    assert split_score(fx("FT", (2, 1), (2, 1))) == ScoreSplit(0, 1, 2, 1, None, None, None, None)


def test_aet_separates_extra_time_goals() -> None:
    s = split_score(fx("AET", (2, 1), (1, 1), et=(1, 0)))
    assert (s.home_goals_nt, s.away_goals_nt, s.home_goals_et, s.away_goals_et) == (1, 1, 1, 0)


def test_aet_with_cumulative_fulltime_is_corrected() -> None:
    s = split_score(fx("AET", (2, 1), (2, 1), et=(1, 0)))
    assert (s.home_goals_nt, s.away_goals_nt) == (1, 1)


def test_pen_keeps_shootout_separate() -> None:
    s = split_score(fx("PEN", (1, 1), (1, 1), et=(0, 0), pen=(4, 3)))
    assert (s.home_goals_nt, s.home_goals_et, s.home_pens, s.away_pens) == (1, 0, 4, 3)


def test_aet_without_extratime_breakdown_is_not_imputed() -> None:
    s = split_score(fx("AET", (2, 1), None, et=None))
    assert s.home_goals_nt is None and s.away_goals_nt is None


def test_old_et_inclusive_row_is_superseded_on_reingest() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        normalize.ensure_source(s)
        comp = normalize.ensure_competition(
            s, league_id=45, name="FA Cup", country="England", level="senior", tier="cup"
        )
        season = normalize.ensure_season(s, comp.competition_id, 2024)
        normalize.ingest_fixtures(s, season, [fx("AET", (2, 1), (1, 1), et=(1, 0))], "England")
        live = s.scalars(
            select(models.MatchResult).where(models.MatchResult.superseded_at.is_(None))
        ).one()
        # simulate a row written before 0002 (ET-inclusive "full time", no ET split)
        live.home_goals_nt, live.home_goals_et, live.away_goals_et = 2, None, None
        s.flush()
        assert (
            normalize.ingest_fixtures(s, season, [fx("AET", (2, 1), (1, 1), et=(1, 0))], "England")
            == 1
        )
        rows = s.scalars(select(models.MatchResult)).all()
        current = [r for r in rows if r.superseded_at is None]
        assert len(rows) == 2 and len(current) == 1
        assert (current[0].home_goals_nt, current[0].home_goals_et) == (1, 1)
