"""Provider team ids that hold two clubs (docs/BEYOND_LEAGUES_PLAN.md, Reggiana / Reggina
split; adatta/adapters/team_splits.py)."""

from datetime import UTC, datetime, timedelta
from itertools import permutations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters import team_splits as ts
from adatta.db import models
from adatta.db.models import Base

SHARED, OTHER = 99, 100
CITY = "Reggio Calabria"
OWN_GROUND, THEIR_GROUND = "Mapei Stadium Reggio Emilia", "Stadio Oreste Granillo (Reggio Calabria)"
B, C = (4, 5, 6), (7, 8, 9)


def _season() -> list[ts.Fx]:
    """One season of a league holding three groups under one id: A (1-3), B (4-6 and the
    shared id's own club) and C (7-9 and the other club, filed under the shared id)."""
    out: list[ts.Fx] = []

    def add(h: int, a: int, rnd: str = "Regular Season - 1", venue: str = "", comp: int = 1
            ) -> None:  # fmt: skip
        out.append(ts.Fx(len(out) + 1, comp, 2016, rnd, h, a, venue))

    for group in ((1, 2, 3), B, C):
        for h, a in permutations(group, 2):
            add(h, a)
    for t in B:
        add(SHARED, t, venue=OWN_GROUND)
        add(t, SHARED)
    for t in C:
        add(SHARED, t, venue=THEIR_GROUND)
        add(t, SHARED)
    return out


def _coaches(fx: list[ts.Fx]) -> dict[tuple[int, int], str]:
    """The shared side's coach: 'own' against group B, 'theirs' against group C."""
    out = {}
    for f in fx:
        if SHARED in (f.home, f.away):
            opp = f.away if f.home == SHARED else f.home
            out[(f.match_id, SHARED)] = "own" if opp in B else "theirs" if opp in C else "?"
    return out


def test_league_matches_move_by_the_group_hosted_in_the_city() -> None:
    fx = _season()
    plan = ts.plan_split(fx, SHARED, OTHER, CITY, {})
    by_id = {f.match_id: f for f in fx}
    assert len(plan.moves) == 6 and len(plan.kept) == 6 and not plan.unresolved
    for mid, why in plan.moves.items():
        f = by_id[mid]
        assert why == ts.GROUP and {f.home, f.away} - {SHARED} <= set(C)
    for mid in plan.kept:
        assert {by_id[mid].home, by_id[mid].away} - {SHARED} <= set(B)


def test_split_restores_a_double_round_robin_per_group() -> None:
    fx = _season()
    # merged B + C (7 teams): each of B and C plays 6 of the 12 a full group would give
    assert {(t, n, full) for _, _, t, n, full in ts.group_mismatch(fx)} == {
        (t, 6, 12) for t in B + C
    }
    after = ts.moved(fx, ts.plan_split(fx, SHARED, OTHER, CITY, {}).moves, SHARED, OTHER)
    assert ts.group_mismatch(after) == []


def test_other_matches_move_by_home_city_or_coach_and_are_never_guessed() -> None:
    fx = _season()
    n = len(fx)
    extra = [
        ts.Fx(n + 1, 2, 2016, "Promotion Play-offs - 1st Round", SHARED, 1, THEIR_GROUND),
        ts.Fx(n + 2, 3, 2016, "2nd Round", 2, SHARED, "Somewhere"),  # theirs by coach
        ts.Fx(n + 3, 3, 2016, "1st Round", 3, SHARED, "Elsewhere"),  # ours by coach
        ts.Fx(n + 4, 3, 2016, "3rd Round", 1, SHARED, "Far away"),  # no coach named
        ts.Fx(n + 5, 3, 2016, "4th Round", 2, SHARED, "Nowhere"),  # a coach of both
    ]
    coaches = _coaches(fx) | {(n + 2, SHARED): "theirs", (n + 3, SHARED): "own",
                              (n + 5, SHARED): "new"}  # fmt: skip
    plan = ts.plan_split(fx + extra, SHARED, OTHER, CITY, coaches)
    assert plan.moves[n + 1] == ts.HOME_CITY and plan.moves[n + 2] == ts.COACH
    assert plan.kept[n + 3] == ts.COACH
    assert sorted(plan.unresolved) == [n + 4, n + 5]


def test_hosting_two_groups_in_the_city_stops() -> None:
    fx = [f if not (f.home == SHARED and f.away in B) else
          ts.Fx(f.match_id, f.competition_id, f.season, f.round, f.home, f.away, THEIR_GROUND)
          for f in _season()]  # fmt: skip
    with pytest.raises(ts.SplitError):
        ts.plan_split(fx, SHARED, OTHER, CITY, {})


def test_second_run_moves_nothing_and_coaches_are_disjoint() -> None:
    fx = _season()
    coaches = _coaches(fx)
    plan = ts.plan_split(fx, SHARED, OTHER, CITY, coaches)
    after = ts.moved(fx, plan.moves, SHARED, OTHER)
    moved_coaches = {((m, OTHER) if m in plan.moves else (m, t)): c
                     for (m, t), c in coaches.items()}  # fmt: skip
    again = ts.plan_split(after, SHARED, OTHER, CITY, moved_coaches)
    assert again.moves == {} and set(again.kept) == set(plan.kept)
    assert ts.shared_coaches(after, SHARED, OTHER, moved_coaches) == {}
    assert ts.shared_coaches(fx, SHARED, OTHER, coaches) == {}  # one id: nothing to compare


def test_apply_moves_the_match_and_its_per_match_rows_only() -> None:
    s = Session(create_engine("sqlite://"))
    Base.metadata.create_all(s.get_bind())
    normalize.ensure_source(s)
    comp = normalize.ensure_competition(s, league_id=138, name="L", country="Italy",
                                        level="senior", tier="3")  # fmt: skip
    season = normalize.ensure_season(s, comp.competition_id, 2016)
    shared, other, opp = (
        normalize.ensure_team(s, r, n).team_id
        for r, n in (("880", "Reggiana"), ("1692", "Reggina"), ("7", "T7"))
    )
    t0 = datetime(2016, 9, 1, 15, tzinfo=UTC)
    ms = []
    for i in range(2):
        m = models.Match(source_id=normalize.SOURCE_ID, source_ref=str(i + 1),
                         season_id=season.season_id, home_team_id=shared, away_team_id=opp,
                         kickoff_time=t0 + timedelta(days=7 * i))  # fmt: skip
        s.add(m)
        s.flush()
        ms.append(m.match_id)
        for t, c in ((shared, "c1"), (opp, "c2")):
            s.add(
                models.MatchCoach(
                    match_id=m.match_id,
                    team_id=t,
                    source_id=normalize.SOURCE_ID,
                    storage_key="k",
                    coach_ref=c,
                    observed_at=t0,
                    valid_from=t0,
                )
            )
    s.flush()
    assert "match_coach" in ts.per_match_tables() and "lineup_snapshot" in ts.per_match_tables()
    assert ts.team_id(s, normalize.SOURCE_ID, "1692") == other
    counts = ts.apply(s, [ms[0]], shared, other)
    assert counts["match"] == 1 and counts["match_coach"] == 1
    moved, kept = (s.get(models.Match, mid) for mid in ms)
    assert moved is not None and kept is not None
    assert (moved.home_team_id, moved.away_team_id) == (other, opp)
    assert (kept.home_team_id, kept.away_team_id) == (shared, opp)
    C = models.MatchCoach
    rows = {(m, t) for m, t in s.execute(select(C.match_id, C.team_id))}
    assert rows == {(ms[0], other), (ms[0], opp), (ms[1], shared), (ms[1], opp)}
    assert ts.apply(s, [ms[0]], shared, other) == {"match": 0} | {
        t: 0 for t in ts.per_match_tables()}  # fmt: skip


def test_shipped_config_names_the_reggina_split() -> None:
    (sp,) = ts.load_splits()
    assert (sp.provider, sp.team_ref, sp.other_ref, sp.last_season, sp.other_city) == (
        "api_football",
        "880",
        "1692",
        2019,
        "Reggio Calabria",
    )
