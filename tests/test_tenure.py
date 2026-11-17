"""P3-c (b): tenures from match_coach rows, their checks, and versioned storage
(adatta/features/tenure.py)."""

from datetime import UTC, datetime, timedelta

import polars as pl
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.db import models
from adatta.db.models import Base
from adatta.features import tenure as tn

T0 = datetime(2021, 8, 7, 15, tzinfo=UTC)


def rows(seq: dict[int, list[str]]) -> pl.DataFrame:
    out = []
    for team, coaches in seq.items():
        for i, c in enumerate(coaches):
            out.append({"match_id": team * 100 + i, "team_id": team, "coach_ref": c,
                        "coach_name": f"Coach {c}", "source_id": "api_football",
                        "observed_at": T0 + timedelta(weeks=i, hours=3),
                        "kickoff": T0 + timedelta(weeks=i)})  # fmt: skip
    return pl.DataFrame(out, schema=tn.ROW_SCHEMA)


def test_a_coach_who_returns_has_two_tenures() -> None:
    r = rows({1: ["a", "a", "b", "b", "b", "a"], 2: ["b", "c"]})
    t = tn.tenures(r)
    one = t.filter(pl.col("team_id") == 1)
    assert one["coach_ref"].to_list() == ["a", "b", "a"]
    assert one["n_matches"].to_list() == [2, 3, 1]
    assert one["open"].to_list() == [False, False, True]
    assert one["first_match_id"][1] == 102 and one["last_match_id"][1] == 104
    c = tn.check(r, t)
    assert c["passed"] and c["tenures"] == 5 and c["managers"] == 3


def test_a_growing_tenure_supersedes_its_row() -> None:
    s = Session(create_engine("sqlite://"))
    Base.metadata.create_all(s.get_bind())
    normalize.ensure_source(s)
    r = rows({1: ["a", "a"]})
    first = tn.apply(s, r, tn.tenures(r), now=T0)
    assert first == {"managers_added": 1, "tenures_new": 1, "tenures_superseded": 0,
                     "tenures_unchanged": 0}  # fmt: skip
    again = tn.apply(s, r, tn.tenures(r), now=T0)
    assert again["tenures_unchanged"] == 1
    r2 = rows({1: ["a", "a", "a", "b"]})
    third = tn.apply(s, r2, tn.tenures(r2), now=T0 + timedelta(weeks=4))
    assert third["tenures_superseded"] == 1 and third["tenures_new"] == 1
    T = models.ManagerTenure
    live = s.scalars(select(T).where(T.superseded_at.is_(None)).order_by(T.valid_from)).all()
    assert [(t.n_matches, t.valid_to is None) for t in live] == [(3, False), (1, True)]
