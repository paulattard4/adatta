"""Stage D v1 serving (docs/BEYOND_LEAGUES_PLAN.md, "D v1 serving acceptance"), tests 1-5
and 7: identity with the backtest's prediction, coherence, eligibility and reasons, the
sampler gate and staleness, no leakage, and the dashboard views. Synthetic data only."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from adatta.adapters.storage import LocalStore
from adatta.api import national as api_national
from adatta.api import views
from adatta.db import models
from adatta.db.models import Base
from adatta.eval import holdout as ho
from adatta.eval.harness import HoldoutLocked
from adatta.model import continental as ct
from adatta.model import national as nt
from adatta.model import national_live as nl
from adatta.sim import national_serve as ns

MONDAY = datetime(2027, 3, 22, tzinfo=UTC)
KICK = datetime(2027, 3, 25, 19, 45, tzinfo=UTC)  # a Thursday in the March window
K = 60  # draws


def fake_post(teams: list[int], seed: int = 3) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    n = len(teams)
    return {
        "c": rng.normal(0.1, 0.05, K), "g": rng.normal(0.05, 0.03, (K, 2)),
        "h": rng.normal(0.3, 0.05, K), "h_u": rng.normal(0.25, 0.05, K),
        "conf": rng.normal(-1.0, 0.3, (K, 5)), "sigma_w": np.abs(rng.normal(0.04, 0.005, K)),
        "sigma_u": np.abs(rng.normal(1.0, 0.05, K)), "sigma_o": np.abs(rng.normal(0.2, 0.02, K)),
        "phi": rng.uniform(50, 90, K), "rho": rng.uniform(-0.08, 0.0, K),
        "s_last": rng.normal(0, 1, (K, n)), "o_last": rng.normal(0, 0.2, (K, n)),
        "teams": np.asarray(teams, dtype=np.float64), "team_conf": np.zeros(n),
    }  # fmt: skip


def row(home: int, away: int, kickoff: datetime = KICK, role: str = "home",
        type_: str = "qualifier", match_id: int = 1) -> dict[str, Any]:  # fmt: skip
    return {"match_id": match_id, "league_id": "5", "type": type_, "comp_conf": "UEFA",
            "kickoff": kickoff, "monday": ct.monday(kickoff), "home": home, "away": away,
            "conf_home": "UEFA", "conf_away": "UEFA", "role": role, "hg": None,
            "ag": None}  # fmt: skip


# ------------------------------------------------------------------- 1, 2: identity, coherence


def test_served_1x2_equals_the_backtest_prediction() -> None:
    post = fake_post([1, 2, 3])
    for r in (row(1, 2), row(3, 1, role="neutral", type_="finals"),
              row(2, 4, role="unknown", kickoff=KICK + timedelta(days=60)),  # 4 unseen
              row(4, 1, role="swapped", type_="friendly")):  # fmt: skip
        got = ns.project(post, r, MONDAY, 150)
        want, _ = nt.predict(post, pl.DataFrame([r], schema=nt.TARGET_SCHEMA), MONDAY, 150)
        for j, s in enumerate(("home", "draw", "away")):
            p, lo, hi = got[("1X2", None, s)]
            assert abs(p - want[0][j]) <= 1e-9
            assert lo <= p <= hi
        assert set(got) == {("1X2", None, s) for s in ("home", "draw", "away")}  # 1X2 only


def test_matrix_sums_to_one_and_reproduces_the_served_1x2() -> None:
    post = fake_post([1, 2])
    r = row(1, 2)
    md = ns.matrix_draws(post, r, MONDAY, 150)
    assert md.shape == (K, nt.MAX_GOALS + 1, nt.MAX_GOALS + 1)
    np.testing.assert_allclose(md.sum(axis=(1, 2)), 1.0, atol=1e-12)
    m = ns.matrix(post, r, MONDAY, 150)
    served = ns.project(post, r, MONDAY, 150)
    assert abs(np.tril(m, -1).sum() - served[("1X2", None, "home")][0]) <= 1e-12
    assert abs(np.trace(m) - served[("1X2", None, "draw")][0]) <= 1e-12


def test_national_serving_runs_without_the_model_group() -> None:
    code = (
        "import sys\nsys.path.insert(0, 'tests')\n"
        "from test_national_serve import fake_post, row, MONDAY\n"
        "from adatta.sim import national_serve as ns\n"
        "ns.project(fake_post([1, 2]), row(1, 2), MONDAY, 150)\n"
        "bad = [m for m in ('pymc', 'pytensor', 'jax', 'numpyro', 'arviz') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).parents[1])


# ---------------------------------------------------------------------------- database world


class World:
    """UEFA Nations League (5) and Friendlies (10); teams 1-4 senior men's national sides,
    5 an U21 side, 6 a club; one promoted national model."""

    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.store = LocalStore(tmp_path / "store")
        self.s = s = Session(self.engine)
        s.add(models.Source(source_id="api_football"))
        self.season: dict[str, int] = {}
        for lg, name in (("5", "UEFA Nations League"), ("10", "Friendlies")):
            c = models.Competition(source_id="api_football", source_ref=lg, name=name,
                                   country="World", tier="national", is_national=True)  # fmt: skip
            s.add(c)
            s.flush()
            se = models.Season(competition_id=c.competition_id, year=2026)
            s.add(se)
            s.flush()
            self.season[lg] = se.season_id
        for t in range(1, 7):
            s.add(models.Team(team_id=t, source_id="api_football", source_ref=str(t),
                              name=f"N{t}", is_national=t != 6,
                              team_level="u21" if t == 5 else "senior"))  # fmt: skip
        s.flush()
        self.n = 0
        # history: each side has played a Nations League match (its confederation)
        old = datetime(2026, 10, 10, 18, tzinfo=UTC)
        for h, a in ((1, 2), (3, 4), (5, 1)):
            self.match(old, h, a, status="FT", score=(1, 0))
        self.champion = models.ModelRun(
            model_name=nt.model_name(), component=nt.COMPONENT, status="champion",
            created_at=datetime(2026, 11, 23, tzinfo=UTC), window="holdout", git_commit="x",
            config_hash=nl.config_hash(), lockfile_hash="", data_snapshot_hash="", seed=nt.SEED,
            manifest={"holdout_run": "1"})  # fmt: skip
        s.add(self.champion)
        s.flush()

    def match(self, kickoff: datetime, home: int, away: int, lg: str = "5",
              status: str = "NS", score: tuple[int, int] | None = None) -> int:  # fmt: skip
        self.n += 1
        m = models.Match(source_id="api_football", source_ref=str(5000 + self.n),
                         season_id=self.season[lg], home_team_id=home, away_team_id=away,
                         kickoff_time=kickoff, status=status, round="League A - 5")  # fmt: skip
        self.s.add(m)
        self.s.flush()
        if score is not None:
            self.s.add(models.MatchResult(match_id=m.match_id, source_id="api_football",
                                          home_goals_nt=score[0], away_goals_nt=score[1],
                                          observed_at=kickoff + timedelta(hours=2)))  # fmt: skip
            self.s.flush()
        return m.match_id

    def refit(self, as_of: datetime, status: str = "ok") -> int:
        post = fake_post([1, 2, 3, 4])
        buf = io.BytesIO()
        np.savez_compressed(buf, **post)
        key = nl.refit_key(as_of)
        self.store.put(key, buf.getvalue())
        r = models.ModelRun(
            model_name=nt.model_name(), component=nt.COMPONENT, status=status,
            created_at=as_of, as_of=as_of, window=nl.WINDOW, git_commit="x",
            config_hash=nl.config_hash(), lockfile_hash="x", data_snapshot_hash="d",
            seed=nt.SEED, trace_path=key, manifest={"n_months": 150, "n_train": 9000},
        )  # fmt: skip
        self.s.add(r)
        self.s.flush()
        return r.model_run_id

    def tick(self, now: datetime) -> Any:
        rep = ns.run_projection(self.s, self.store, {"git_commit": "a", "lockfile_hash": "l",
                                                     "seed": nt.SEED}, now=now)  # fmt: skip
        self.s.commit()
        return rep

    def written(self, match_id: int) -> dict[str, int]:
        MP = models.MarketProjection
        rows = self.s.execute(select(MP.milestone, func.count()).where(MP.match_id == match_id)
                              .group_by(MP.milestone)).all()  # fmt: skip
        return {m: c for m, c in rows}


# ---------------------------------------------------------------------- 3: eligibility, reasons


def test_only_servable_pairings_are_projected_and_others_say_why(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.refit(MONDAY)
    ok = w.match(KICK, 1, 2)
    friendly = w.match(KICK, 3, 4, lg="10")
    youth = w.match(KICK, 5, 1)
    club = w.match(KICK, 1, 6, lg="10")
    far = w.match(KICK + timedelta(days=30), 1, 3)
    off = w.match(KICK, 2, 3, status="PST")
    rep = w.tick(MONDAY + timedelta(hours=6))
    assert rep.written == {"initial": 2}
    assert w.written(ok) == {"initial": 3} and w.written(friendly) == {"initial": 3}
    for m in (youth, club, far, off):
        assert w.written(m) == {}
    why = ns.reason(w.s, w.store, [youth, club], MONDAY)
    assert why[youth] == ns.YOUTH and why[club] == ns.NOT_COVERED
    assert ns.reason(w.s, w.store, [far], MONDAY)[far] == ns.NO_CURRENT
    assert w.tick(MONDAY + timedelta(hours=7)).written == {}  # idempotent


# ------------------------------------------------------------------------ 4: gates, staleness


def test_a_failed_refit_is_never_served_and_a_stale_one_not_used(tmp_path: Path) -> None:
    w = World(tmp_path)
    m = w.match(KICK, 1, 2)
    assert ns.reason(w.s, w.store, [m], MONDAY)[m] == ns.NO_REFIT
    w.refit(MONDAY - timedelta(days=7))  # last week's: current for this match
    bad = w.refit(MONDAY, status="failed")
    run, problems = ns.latest(w.s, MONDAY + timedelta(hours=6))
    assert run is not None and run.model_run_id != bad and problems
    rep = w.tick(MONDAY + timedelta(hours=6))
    assert rep.written == {"initial": 1}
    MP, PR = models.MarketProjection, models.ProjectionRun
    cv = w.s.scalars(select(PR.component_versions).join(MP, MP.projection_run_id ==
                                                        PR.projection_run_id)).first()  # fmt: skip
    assert cv is not None and cv[ns.BUNDLE]["model_run_id"] == run.model_run_id
    # a refit two weeks before the match's week is not current: no months-old forecast
    w2 = World(tmp_path / "b")
    m2 = w2.match(KICK, 1, 2)
    w2.refit(MONDAY - timedelta(days=14))
    rep2 = w2.tick(MONDAY + timedelta(hours=6))
    assert rep2.written == {} and rep2.no_refit == 1
    assert ns.reason(w2.s, w2.store, [m2], MONDAY)[m2] == ns.NO_CURRENT


# ------------------------------------------------------------------------------ 5: no leakage


def test_no_leakage(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.match(KICK, 1, 2)
    w.refit(MONDAY)
    # a refit after `now` is never used, and never projected from
    assert ns.latest(w.s, MONDAY - timedelta(hours=1))[0] is None
    with pytest.raises(ValueError, match="future"):
        nl.refit(w.s, w.store, datetime.now(UTC) + timedelta(days=7))
    # live targets only for the promoted champion; training ends at the Monday minus the lag
    not_champ = models.ModelRun(model_name=nt.model_name(), component=nt.COMPONENT,
                                status="ok")  # fmt: skip
    with pytest.raises(HoldoutLocked):
        nt.live_targets(w.s, w.store, MONDAY, not_champ)
    tg = nt.live_targets(w.s, w.store, MONDAY, w.champion)
    assert tg.height == 2  # the finished senior pairings; the U21 side is not a target
    late = tg.with_columns(pl.lit(MONDAY - timedelta(hours=1)).alias("kickoff"))
    assert nt.training(late, MONDAY).is_empty()


def test_pending_results_hold_the_refit(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.match(MONDAY - timedelta(days=2), 1, 2, status="NS")  # played, no result yet
    with pytest.raises(nl.NotReady):
        nl.refit(w.s, w.store, MONDAY, now=MONDAY + timedelta(hours=2))
    assert nl.due(w.s, w.store, MONDAY + timedelta(hours=2)) is None  # nothing upcoming
    w.match(KICK, 1, 3)
    assert nl.due(w.s, w.store, MONDAY + timedelta(hours=2)) == MONDAY
    w.refit(MONDAY)
    assert nl.due(w.s, w.store, MONDAY + timedelta(hours=2)) is None  # recorded


def test_promotion_needs_the_opening_and_a_passing_verdict(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.s.delete(w.champion)
    w.s.flush()
    ho.claim(w.store, "1", "phase1b_strength_open_xg", "abc")
    ho.claim(w.store, "77", nt.model_name(), "sha", nt.OPENING)
    key = f"{nt.PREFIX}/reports/77/verdict.json"
    rep: dict[str, Any] = {"main": {}, "calibration": {}}
    w.store.put(key, json.dumps({"passed": False, "checks": {}, "report": rep}).encode())
    with pytest.raises(SystemExit, match="does not pass"):
        nl.promote(w.s, w.store, "77")
    with pytest.raises(SystemExit, match="did not open"):
        nl.promote(w.s, w.store, "78")
    w.store.put(key, json.dumps({"passed": True, "checks": {}, "report": rep}).encode())
    row = nl.promote(w.s, w.store, "77")
    assert row.status == "champion" and row.git_commit == "sha"
    assert nl.promote(w.s, w.store, "77").model_run_id == row.model_run_id  # once


# ------------------------------------------------------------------------------ 7: dashboard


def test_dashboard_shows_national_forecasts_and_reasons(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.refit(MONDAY)
    ok = w.match(KICK, 1, 2)
    youth = w.match(KICK, 5, 1)
    now = MONDAY + timedelta(hours=6)
    w.tick(now)
    ctx = views.Context(bundles={}, model_name="phase1b_strength_open_xg", max_goals=10,
                        store=w.store)  # fmt: skip
    fl = views.fixture_list(w.s, ctx, now, end=now + timedelta(days=14))
    rows = {f.match_id: f for g in fl.competitions for f in g.fixtures}
    assert rows[ok].headline is not None and rows[ok].no_projection_reason is None
    assert any(c.key == "model" and c.value == "team-only" for c in rows[ok].confidence)
    assert rows[youth].headline is None and rows[youth].no_projection_reason == ns.YOUTH
    mv = views.match_view(w.s, ctx, ok, now)
    assert mv.coherence is not None and mv.coherence.coherent and mv.matrix is not None
    assert mv.matrix.max_goals == nt.MAX_GOALS and mv.squad is None
    assert any(u.panel == "Over/Under and BTTS" for u in mv.unavailable)
    assert mv.unavailable == api_national.UNAVAILABLE
    assert [s.market for s in mv.milestones[-1].selections] == ["1X2"] * 3
    mv_y = views.match_view(w.s, ctx, youth, now)
    assert mv_y.milestones == [] and mv_y.matrix is None
    assert mv_y.no_projection_reason == ns.YOUTH
