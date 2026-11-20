"""D41: a refit milestone when a newer refit passes, milestones ordered by publication, and
the fixture row's freshness taken from the refit behind its forecast (docs/DASHBOARD_TASKS.md,
"Forecast freshness")."""

from datetime import timedelta
from pathlib import Path

from test_dashboard_api import ctx
from test_serve import AS_OF, K, World

from adatta.adapters.normalize import SOURCE_ID
from adatta.api import views
from adatta.db import models
from adatta.sim import serve

B_AS_OF = AS_OF + timedelta(days=7)  # the next Monday's refit
AFTER_B = B_AS_OF + timedelta(hours=6)
LABEL = serve.refit_label(B_AS_OF)


def _t7_from_a(tmp_path: Path) -> tuple[World, int]:
    """Initial and T-7d written from refit A (AS_OF); refit B not passed yet."""
    w = World(tmp_path)
    w.refit(AS_OF)
    m = w.fixture(K)
    w.tick(K - timedelta(days=10))
    w.tick(K - timedelta(days=6, hours=12))
    assert w.milestones(m) == ["T-7d", "initial"]
    return w, m


def test_a_newer_refit_writes_one_refit_milestone(tmp_path: Path) -> None:
    w, m = _t7_from_a(tmp_path)
    b = w.refit(B_AS_OF)
    later = w.fixture(K + timedelta(days=2), home=3, away=4)  # first projected from B
    assert LABEL == "refit 11-16 00h" and len(LABEL) <= 16
    rep = w.tick(AFTER_B)
    assert rep.written == {LABEL: 1, "initial": 1}
    assert w.milestones(m) == ["T-7d", "initial", LABEL]
    assert w.milestones(later) == ["initial"]  # already from B: no refit milestone
    pr = w.s.get(models.ProjectionRun, rep.projection_run_id)
    assert pr is not None and pr.component_versions["england"]["model_run_id"] == b
    assert w.tick(AFTER_B + timedelta(minutes=15)).written == {}  # once per refit
    # T-24h opens: written from B, and no further refit milestone
    assert w.tick(K - timedelta(hours=21)).written == {"T-24h": 1}
    assert w.milestones(m) == ["T-24h", "T-7d", "initial", LABEL]


def test_a_failed_or_future_refit_writes_nothing(tmp_path: Path) -> None:
    w, m = _t7_from_a(tmp_path)
    w.refit(B_AS_OF, status="failed")
    w.refit(K + timedelta(days=1))  # after now: never used
    assert w.tick(AFTER_B).written == {}
    assert w.milestones(m) == ["T-7d", "initial"]


def test_views_use_the_latest_published_milestone(tmp_path: Path) -> None:
    w, m = _t7_from_a(tmp_path)
    w.refit(B_AS_OF)
    w.tick(AFTER_B)
    now = AFTER_B + timedelta(hours=1)
    v = views.match_view(w.s, ctx(w), m, now)
    assert [x.milestone for x in v.milestones] == ["initial", "T-7d", LABEL]
    assert v.freshness.refit_as_of == B_AS_OF
    assert v.coherence is not None and v.coherence.coherent  # recomputed from B
    row = next(f for g in views.fixture_list(w.s, ctx(w), now).competitions
               for f in g.fixtures if f.match_id == m)  # fmt: skip
    assert row.headline is not None and row.headline.milestone == LABEL
    # played: the refit milestone is the latest forecast published before kickoff
    match = w.s.get(models.Match, m)
    assert match is not None
    match.status = "FT"
    w.s.add(models.MatchResult(match_id=m, source_id=SOURCE_ID, home_goals_nt=1,
                               away_goals_nt=0, observed_at=K + timedelta(hours=3)))  # fmt: skip
    w.s.commit()
    played = views.played(w.s, ctx(w), K + timedelta(hours=4))
    assert [p.milestone for p in played.matches] == [LABEL]


def test_row_freshness_is_the_refit_behind_its_forecast(tmp_path: Path) -> None:
    w, m = _t7_from_a(tmp_path)
    w.refit(B_AS_OF)
    w.s.commit()
    now = AFTER_B  # B has passed; the tick that re-projects has not run yet
    g = views.fixture_list(w.s, ctx(w), now).competitions[0]
    row = next(f for f in g.fixtures if f.match_id == m)
    fresh = next(c for c in row.confidence if c.key == "freshness")
    age = (now - AS_OF) / timedelta(days=1)
    assert fresh.value == f"refit {age:.1f} d old"  # A's age, not B's
    assert "newer refit (as of 2026-11-16)" in fresh.detail
    assert g.freshness.refit_as_of == B_AS_OF  # the competition header: the current refit
    v = views.match_view(w.s, ctx(w), m, now)
    page = next(c for c in v.confidence if c.key == "freshness")
    assert page.value == fresh.value and "newer refit" in page.detail
    # after the tick, both say B and name nothing newer
    w.tick(now)
    row = next(f for f in views.fixture_list(w.s, ctx(w), now).competitions[0].fixtures
               if f.match_id == m)  # fmt: skip
    fresh = next(c for c in row.confidence if c.key == "freshness")
    assert fresh.value == f"refit {(now - B_AS_OF) / timedelta(days=1):.1f} d old"
    assert "newer refit" not in fresh.detail
