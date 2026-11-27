"""C2 step 3 (D54): baseline projections of the C2 top flights. Only champion leagues, point
forecasts (no interval), the C2 round scope, the truncation limit ((c)), the five's serving
untouched ((e)), and the champion rows ((g))."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from adatta.adapters.storage import LocalStore
from adatta.api import views as v
from adatta.db import models
from adatta.db.models import Base
from adatta.eval import c2
from adatta.model import c2_live
from adatta.model import dixon_coles as dc
from adatta.sim import c2_serve as cs
from adatta.sim import serve

LEAGUES = [94, 95]
AS_OF = datetime(2026, 11, 30, tzinfo=UTC)  # a Monday
K = datetime(2026, 12, 5, 15, 0, tzinfo=UTC)
CONF = c2.C2Config(
    second_tier_available="07-01", bundles={"portugal": LEAGUES},
    rounds={94: {"Regular Season": "regular", "Championship Round": "phase",
                 "Final": "excluded"}},
)  # fmt: skip
MANIFEST = {"git_commit": "abc", "lockfile_hash": "l", "seed": 0}


def a_fit(as_of: datetime = AS_OF, attack: float = 0.3) -> c2_live.Fit:
    n = 8
    params = dc.Params(mu=0.1, mu_league=np.array([0.0, -0.1]), home_league=np.array([0.25, 0.2]),
                       attack=np.linspace(-attack, attack, n), defence=np.linspace(-0.2, 0.2, n),
                       rho=-0.05)  # fmt: skip
    return c2_live.Fit(bundle="portugal", as_of=as_of, leagues=LEAGUES,
                       team_ids=np.arange(1, n + 1), counts=np.full(n, 30), params=params,
                       league_att=np.zeros(2), league_def=np.zeros(2), n_train=240,
                       training_hash="t")  # fmt: skip


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.store = LocalStore(tmp_path / "store")
        self.s = s = Session(self.engine)
        s.add(models.Source(source_id="api_football"))
        self.season: dict[int, int] = {}
        for lg in LEAGUES:
            c = models.Competition(source_id="api_football", source_ref=str(lg), name=f"L{lg}",
                                   country="Portugal", tier="1" if lg == 94 else "2")  # fmt: skip
            s.add(c)
            s.flush()
            se = models.Season(competition_id=c.competition_id, year=2026)
            s.add(se)
            s.flush()
            self.season[lg] = se.season_id
        for t in range(1, 11):
            s.add(models.Team(team_id=t, source_id="api_football", source_ref=str(t), name=f"T{t}"))
        s.flush()
        self.n = 0
        for h in range(1, 9):  # the regular season: sides 1-8
            self.fixture(K - timedelta(days=60 + h), h, h % 8 + 1, status="FT")

    def fixture(self, kickoff: datetime, home: int = 1, away: int = 2, status: str = "NS",
                rnd: str = "Regular Season - 12", league: int = 94) -> int:  # fmt: skip
        self.n += 1
        m = models.Match(source_id="api_football", source_ref=str(5000 + self.n),
                         season_id=self.season[league], home_team_id=home, away_team_id=away,
                         kickoff_time=kickoff, status=status, round=rnd)  # fmt: skip
        self.s.add(m)
        self.s.flush()
        return m.match_id

    def refit(self, fit: c2_live.Fit, status: str = "ok") -> int:
        key = c2_live.key("cfg", "portugal", fit.as_of)
        self.store.put(key, c2_live.encode(fit, {"model": c2_live.MODEL}))
        r = models.ModelRun(model_name=c2_live.MODEL, component=c2_live.COMPONENT, status=status,
                            created_at=fit.as_of, as_of=fit.as_of, window=c2_live.WINDOW,
                            git_commit="x", config_hash="cfg", lockfile_hash="x",
                            data_snapshot_hash="t", seed=0, trace_path=key,
                            manifest={"bundle": "portugal", "n_train": 240})  # fmt: skip
        self.s.add(r)
        self.s.flush()
        return r.model_run_id

    def champion(self) -> None:
        c2_live.record_champions(self.s, {94: {"bar": "pass"}}, {"run": "test"})

    def tick(self, now: datetime) -> cs.Report:
        rep = cs.run_projection(self.s, self.store, MANIFEST, now=now, conf=CONF)
        self.s.commit()
        return rep

    def rows(self, match_id: int) -> list[models.MarketProjection]:
        MP = models.MarketProjection
        return list(self.s.scalars(select(MP).where(MP.match_id == match_id)))


def test_nothing_is_projected_before_the_league_is_champion(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.refit(a_fit())
    m = w.fixture(K)
    assert w.tick(K - timedelta(days=3)).written == {} and w.rows(m) == []
    w.champion()
    assert w.tick(K - timedelta(days=3)).written == {"initial": 1}


def test_point_forecasts_equal_the_fit_and_carry_no_interval(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.champion()
    run_id = w.refit(a_fit())
    m = w.fixture(K, 3, 6)
    w.tick(K - timedelta(days=3))
    rows = w.rows(m)
    n_sel = 3 + 2 + 2 * len(serve.OU_LINES)
    assert len(rows) == n_sel and {r.milestone for r in rows} == {"initial"}
    assert all(r.interval_lo is None and r.interval_hi is None for r in rows)
    want, tail = cs.markets(a_fit(), serve.Fixture(m, K, 94, 3, 6), 20)
    got = {(r.market_type, r.line, r.selection): r.probability for r in rows}
    assert got == pytest.approx(want, abs=1e-15) and tail < 1e-10
    assert cs.sums_to_one(got) < 1e-12
    pr = w.s.scalars(select(models.ProjectionRun)).one()
    assert pr.component_versions["portugal"] == {
        "model_run_id": run_id, "model": c2_live.MODEL, "as_of": AS_OF.isoformat(),
        "baseline": True}  # fmt: skip


def test_milestones_and_a_newer_refit_follow_serve(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.champion()
    w.refit(a_fit())
    m = w.fixture(K + timedelta(days=7))
    w.tick(AS_OF + timedelta(hours=1))
    w.refit(a_fit(AS_OF + timedelta(days=7), attack=0.25))
    w.tick(AS_OF + timedelta(days=7, hours=1))
    assert {r.milestone for r in w.rows(m)} == {
        "initial",
        serve.refit_label(AS_OF + timedelta(days=7)),
    }
    w.tick(K + timedelta(days=7) - timedelta(minutes=45))
    assert "T-60m" in {r.milestone for r in w.rows(m)}


def test_round_scope_phases_ties_and_unclassed_labels(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.champion()
    w.refit(a_fit())
    group = [
        w.fixture(K, 1, 2, rnd="Championship Round - 1"),
        w.fixture(K, 3, 1, rnd="Championship Round - 2"),
    ]  # sides 1, 2, 3: a group
    tie = w.fixture(K, 4, 5, rnd="Final")  # excluded
    cross = w.fixture(K, 6, 9, rnd="Championship Round - 3")  # side 9: not in the season
    odd = w.fixture(K, 7, 8, rnd="Mystery Round - 1")
    rep = w.tick(K - timedelta(days=3))
    assert all(w.rows(x) for x in group)
    assert not any(w.rows(x) for x in (tie, cross, odd))
    assert any("not classed" in p for p in rep.problems)
    pair = World(tmp_path / "b")
    pair.champion()
    pair.refit(a_fit())
    two = pair.fixture(K, 1, 2, rnd="Championship Round - 1")  # two sides only: a tie
    pair.tick(K - timedelta(days=3))
    assert pair.rows(two) == []


def test_a_matrix_past_the_truncation_limit_is_not_served(tmp_path: Path) -> None:
    """(c): tail mass beyond 20 goals a side under 1e-4, or no forecast."""
    w = World(tmp_path)
    w.champion()
    w.refit(a_fit(attack=4.0))  # rates of dozens of goals
    m = w.fixture(K, 8, 1)
    rep = w.tick(K - timedelta(days=3))
    assert w.rows(m) == [] and any("tail" in p for p in rep.problems)


def test_failed_or_future_refits_are_not_served(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.champion()
    w.refit(a_fit(), status="failed")
    m = w.fixture(K)
    rep = w.tick(K - timedelta(days=3))
    assert w.rows(m) == [] and rep.no_refit == 1 and any("failed" in p for p in rep.problems)


def test_the_five_never_read_c2_rows_and_c2_never_reads_theirs(tmp_path: Path) -> None:
    """(e): the five's serving selects component phase1b; C2's selects c2."""
    w = World(tmp_path)
    w.champion()
    w.refit(a_fit())
    runs, _ = serve.latest_refits(w.s, K, "phase1b_strength_open_xg")
    assert runs == {}
    assert serve.latest_refits(w.s, K, c2_live.MODEL)[0] == {}
    assert set(cs.latest(w.s, K)[0]) == {"portugal"}
    assert v._champion(w.s, "phase1b_strength_open_xg") is None


def test_champion_rows_once_per_top_flight(tmp_path: Path) -> None:
    """(g): `phase1a_c2_<league>`, component c2, status champion; recorded once."""
    w = World(tmp_path)
    rows = c2_live.record_champions(w.s, {94: {"x": 1}, 88: {"x": 2}}, {"run": "t"})
    assert [r.model_name for r in rows] == ["phase1a_c2_88", "phase1a_c2_94"]
    assert c2_live.record_champions(w.s, {94: {"x": 3}}, {}) == []
    ch = c2_live.champions(w.s)
    assert set(ch) == {88, 94} and ch[94].backtest_scores == {"x": 1}
    assert all(r.component == "c2" and r.status == "champion" for r in ch.values())


def test_the_match_view_shows_the_baseline_without_intervals(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.champion()
    w.refit(a_fit())
    m = w.fixture(K, 3, 6)
    w.s.commit()
    rep = cs.run_projection(w.s, w.store, MANIFEST, now=K - timedelta(days=3))
    w.s.commit()
    assert rep.written == {"initial": 1}
    ctx = v.Context(bundles={}, model_name="phase1b_strength_open_xg", max_goals=15,
                    store=w.store)  # fmt: skip
    view = v.match_view(w.s, ctx, m, K - timedelta(days=2))
    assert view.coherence is not None and view.coherence.coherent
    assert view.matrix is not None and view.matrix.max_goals == 20
    assert [c.key for c in view.confidence] == list(v.CHECKLIST)
    model = next(c for c in view.confidence if c.key == "model")
    assert model.value.startswith("baseline") and "Phase 1a" in model.detail
    sel = view.milestones[-1].selections
    assert sel and all(s.prob.lo is None and s.prob.hi is None for s in sel)
    lst = v.fixture_list(w.s, ctx, K - timedelta(days=2), end=K + timedelta(days=1))
    rows: list[Any] = [f for g in lst.competitions for f in g.fixtures]
    assert [f.match_id for f in rows] == [m] and rows[0].headline is not None


def test_only_an_explicit_point_forecast_has_no_interval() -> None:
    from adatta.api import schemas as sc

    assert sc.Prob(p=0.4, point=True).lo is None
    for bad in ({"p": 0.4}, {"p": 0.4, "lo": 0.3}, {"p": 0.4, "lo": 0.3, "hi": 0.5, "point": True}):
        with pytest.raises(ValueError):
            sc.Prob(**bad)


def test_a_played_c2_match_is_scored_on_the_played_view(tmp_path: Path) -> None:
    """Owner, 2026-11-27: the played view covers every served competition."""
    w = World(tmp_path)
    w.champion()
    w.refit(a_fit())
    m = w.fixture(K, 3, 6)
    w.tick(K - timedelta(days=3))
    match = w.s.get(models.Match, m)
    assert match is not None
    match.status = "FT"
    w.s.add(models.MatchResult(match_id=m, source_id="api_football", home_goals_nt=1,
                               away_goals_nt=1, observed_at=K + timedelta(hours=2)))  # fmt: skip
    w.s.commit()
    ctx = v.Context(bundles={}, model_name="phase1b_strength_open_xg", max_goals=15,
                    store=w.store)  # fmt: skip
    out = v.played(w.s, ctx, K + timedelta(hours=3))
    assert [x.match_id for x in out.matches] == [m]
    got = out.matches[0]
    assert got.outcome == "draw" and got.home_p.point and got.home_p.lo is None
    assert out.summary[0].n == 1 and out.summary[1].segment == "L94"
