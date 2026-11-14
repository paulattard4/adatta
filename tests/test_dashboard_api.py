"""Dashboard API (docs/DASHBOARD_TASKS.md): the matrix shown reproduces the published
milestone, the decomposition is exact, probabilities never travel without intervals,
confidence stays as named components, and the API is read-only and never reads odds."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import numpy as np
import pydantic
import pytest
import test_serve
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_backtest import LEAGUES
from test_serve import AS_OF, PROD, K, World, fit_data, lively_draws

from adatta.api import schemas as sc
from adatta.api import views
from adatta.api.app import create_app, readonly_engine
from adatta.db import models
from adatta.model.team_strength import rates_from_draws
from adatta.sim import serve

NOW = AS_OF + timedelta(days=2)


def ctx(w: World) -> views.Context:
    return views.Context(bundles={"england": LEAGUES}, model_name=PROD, max_goals=10,
                         store=w.store)  # fmt: skip


def published(tmp_path: Path) -> tuple[World, int, int]:
    """A world with one projected fixture and one in the window not projected yet."""
    w = World(tmp_path)
    w.refit(AS_OF)
    mid = w.fixture(K)
    w.tick(NOW)
    later = w.fixture(K + timedelta(days=1), home=3, away=4)
    w.s.commit()
    return w, mid, later


def test_decomposition_reproduces_the_rates_per_draw() -> None:
    df, fd = fit_data()
    d = lively_draws(fd)
    rng = np.random.default_rng(3)
    d["form_last"] = rng.normal(0, 0.2, d["attack_last"].shape)
    d["phi"] = np.asarray(rng.uniform(0.5, 0.9, (d["rho"].shape[1],)))
    tid = [int(t) for t in fd.team_ids]
    kickoff = fd.start + timedelta(weeks=int(fd.n_steps) + 2)
    for home, away in ((tid[0], tid[1]), (999, tid[2]), (tid[3], 998)):  # incl. cold starts
        fx = serve.Fixture(1, kickoff, LEAGUES[0], home, away)
        parts = views.decompose(d, fd, fx)
        tix = {t: i for i, t in enumerate(tid)}
        lam, mu_ = rates_from_draws(d, fd, np.array([tix.get(home, -1)]),
                                    np.array([tix.get(away, -1)]), np.array([0]),
                                    horizon=np.array([views.horizon_weeks(fd, kickoff)]),
                                    )  # fmt: skip
        np.testing.assert_allclose(np.exp(sum(x for _, x in parts["home"])), lam[0], rtol=1e-9)
        np.testing.assert_allclose(np.exp(sum(x for _, x in parts["away"])), mu_[0], rtol=1e-9)


def test_match_view_reproduces_the_published_milestone(tmp_path: Path) -> None:
    w, mid, _ = published(tmp_path)
    v = views.match_view(w.s, ctx(w), mid, NOW)
    assert [m.milestone for m in v.milestones] == ["initial"]
    assert v.header.team_level == "senior"  # gates the fair odds (principle 10)
    assert v.coherence is not None and v.coherence.coherent
    assert v.coherence.max_abs_diff <= 1e-9
    assert v.matrix is not None
    shown = sum(c.p for row in v.matrix.cells for c in row)
    assert abs(shown + v.matrix.rest.p - 1.0) < 1e-9  # renormalised matrix
    assert 0 <= v.matrix.tail_mass < 1e-3
    for row in v.matrix.cells:
        for c in row:
            assert c.lo <= c.p <= c.hi
    # the waterfall sums to the log rate, and both sides are labelled
    for dec in v.decomposition:
        assert abs(sum(t.mean for t in dec.terms) - dec.log_rate.mean) < 1e-9
        assert not dec.cold_start
    assert [d.side for d in v.decomposition] == ["home", "away"]
    assert {u.panel for u in v.unavailable} >= {"Squad and lineup", "Edge", "Override"}


def test_incoherent_recomputation_is_not_shown(tmp_path: Path) -> None:
    w, mid, _ = published(tmp_path)
    c = ctx(w)
    real = c.loader

    def other_draws(store: object, run: object) -> serve.Refit:
        r = real(w.store, run)  # type: ignore[arg-type]
        d = dict(r.draws)
        d["mu_league"] = d["mu_league"] + 0.1
        return serve.Refit(r.bundle, r.as_of, r.model_run_id, r.config_hash, r.training_hash,
                           d, r.fd)  # fmt: skip

    c.loader = other_draws
    v = views.match_view(w.s, c, mid, NOW)
    assert v.coherence is not None and not v.coherence.coherent
    assert v.matrix is None and v.decomposition == []
    assert v.milestones  # the published numbers are still shown


def test_fixture_list_groups_and_carries_components(tmp_path: Path) -> None:
    w, mid, later = published(tmp_path)
    fl = views.fixture_list(w.s, ctx(w), NOW)
    assert len(fl.competitions) == 1
    g = fl.competitions[0]
    assert g.freshness.tier == "weekly" and g.freshness.gates_pass and not g.freshness.stale
    rows = {f.match_id: f for f in g.fixtures}
    h = rows[mid].headline
    assert h is not None
    assert abs(h.home.p + h.draw.p + h.away.p - 1.0) < 1e-9
    assert rows[later].headline is None
    assert rows[later].no_projection_reason
    keys = [c.key for c in rows[mid].confidence]
    assert keys == ["history", "capability", "lineup", "cross_league", "stability", "freshness"]


def test_cold_start_team_is_flagged(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.s.add(models.Team(team_id=99, source_id="api_football", source_ref="99", name="New"))
    w.refit(AS_OF)
    mid = w.fixture(K, home=99, away=2)
    w.tick(NOW)
    v = views.match_view(w.s, ctx(w), mid, NOW)
    assert v.decomposition[0].cold_start and not v.decomposition[1].cold_start
    hist = next(c for c in v.confidence if c.key == "history")
    assert hist.status == "limited" and "Cold start" in hist.detail


def test_probabilities_cannot_lose_their_interval() -> None:
    with pytest.raises(pydantic.ValidationError):
        sc.Prob(p=0.5)  # type: ignore[call-arg]


def test_http_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # one shared in-memory database across the test client's worker thread
    monkeypatch.setattr(
        test_serve,
        "create_engine",
        lambda url: create_engine(
            url, poolclass=StaticPool, connect_args={"check_same_thread": False}
        ),
    )
    w, mid, _ = published(tmp_path)

    def sessions() -> Session:
        return Session(w.engine)

    client = TestClient(create_app(sessions, ctx(w), clock=lambda: NOW))
    r = client.get("/api/fixtures")
    assert r.status_code == 200
    assert r.json()["competitions"][0]["fixtures"][0]["headline"]["home"]["lo"] is not None
    r = client.get(f"/api/matches/{mid}")
    assert r.status_code == 200 and r.json()["coherence"]["coherent"]
    assert client.get("/api/matches/99999").status_code == 404


def test_read_only_and_never_reads_odds() -> None:
    eng = readonly_engine("postgresql+psycopg://u:p@localhost/x")
    assert eng.get_execution_options().get("postgresql_readonly") is True
    for f in Path("adatta/api").glob("*.py"):
        tree = ast.parse(f.read_text())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not {x for x in names if "Odds" in x}, f
