"""P3b-2, serving the half markets (docs/PHASE3B_TASKS.md (e); D51): the per-draw markets
equal P3b-1's split, the 90' rows are untouched, coherence holds and blocks on purpose, the
scope rules leave no half rows, a future or stale or failed share refit is not used, writes
are idempotent, the page recomputes them, and the production share refit records itself."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import polars as pl
import pytest
from sqlalchemy import select
from test_backtest import LEAGUES
from test_serve import AS_OF, PROD, K, World

from adatta.api import views
from adatta.db import models
from adatta.eval import harness
from adatta.model import half_split as hm
from adatta.model import live
from adatta.sim import half_serve as hsv
from adatta.sim import half_split as hs
from adatta.sim import serve

NOW = AS_OF + timedelta(days=2)
HALF = set(hsv.MARKETS)


def add_share(w: World, as_of: datetime, status: str = "ok", n: int = 50, seed: int = 0) -> int:
    rng = np.random.default_rng(seed)
    post = {"beta": np.stack([rng.normal(hm.LOGIT0, 0.03, n), rng.normal(-0.27, 0.03, n)], 1),
            "alpha": np.zeros((n, len(LEAGUES))), "u": np.zeros((n, 6))}  # fmt: skip
    o = SimpleNamespace(league_ids=list(LEAGUES), team_ids=list(range(1, 7)))
    key = hm.live_key(as_of, "npz")
    w.store.put(key, hm.encode(post, o))  # type: ignore[arg-type]
    r = models.ModelRun(model_name=hm.LIVE_MODEL, component=hm.LIVE_COMPONENT, status=status,
                        created_at=as_of, as_of=as_of, window="live", git_commit="x",
                        config_hash="h", lockfile_hash="x", data_snapshot_hash="d", seed=1,
                        trace_path=key, manifest={})  # fmt: skip
    w.s.add(r)
    w.s.flush()
    return r.model_run_id


def add_cap(w: World, league: int, status: str = "available") -> None:
    se = w.s.get(models.Season, w.season[league])
    assert se is not None
    w.s.add(models.CompetitionSourceCapability(
        competition_id=se.competition_id, season_id=se.season_id, source_id="api_football",
        capability="has_half_split_stats", coverage_ratio=1.0, status=status,
        valid_from=AS_OF - timedelta(days=90), last_audited_at=AS_OF))  # fmt: skip
    w.s.flush()


def world(tmp_path: Path, share: bool = True) -> World:
    w = World(tmp_path)
    w.refit(AS_OF)
    for lg in LEAGUES:
        add_cap(w, lg)
    if share:
        add_share(w, AS_OF)
    return w


def tick(w: World, now: datetime, half: bool = True, **kw: Any) -> serve.ProjectReport:
    rep = serve.run_projection(w.s, w.store, {"england": LEAGUES}, PROD, 10,
                               {"git_commit": "abc", "lockfile_hash": "l", "seed": 1},
                               now=now, half_split=half, **kw)  # fmt: skip
    w.s.commit()
    return rep


def rows(w: World, mid: int) -> dict[tuple[str, str, float | None, str], models.MarketProjection]:
    MP = models.MarketProjection
    return {(r.milestone, r.market_type, r.line, r.selection): r
            for r in w.s.scalars(select(MP).where(MP.match_id == mid))}  # fmt: skip


# ---------------------------------------------------------------------------- numerics


def test_per_draw_markets_equal_the_p3b1_split() -> None:
    rng = np.random.default_rng(1)
    g = 9
    m = rng.dirichlet(np.ones(g * g), size=4).reshape(4, g, g)
    ph, pa = rng.uniform(0.35, 0.5, 4), rng.uniform(0.35, 0.5, 4)
    dm = hsv.draw_markets(m, ph, pa)
    for d in range(4):
        want = hs.markets(m[d], ph[d : d + 1], pa[d : d + 1])
        got_ht = [dm[("HT1X2", None, c)][d] for c in hsv.CODES]
        np.testing.assert_allclose(got_ht, want["ht_1x2"], atol=1e-12)
        got_htft = [dm[("HTFT", None, s)][d] for s in hsv.HTFT_SELS]
        np.testing.assert_allclose(got_htft, want["ht_ft"], atol=1e-12)
        assert abs(dm[("HTOU", 1.5, "over")][d] - want["ht_ou"][1]) < 1e-12


def test_pair_thins_and_repeats_evenly() -> None:
    p = np.arange(10.0)
    assert hsv.pair(p, 5).tolist() == [0, 2, 4, 6, 8]
    assert hsv.pair(p, 20).tolist() == [v for v in range(10) for _ in range(2)]
    assert hsv.pair(p, 10).tolist() == p.tolist()


def test_coherence_breaks_on_purpose() -> None:
    rng = np.random.default_rng(2)
    m = rng.dirichlet(np.ones(64), size=6).reshape(6, 8, 8)
    half = hsv.summarise(hsv.draw_markets(m, np.full(6, 0.44), np.full(6, 0.43)), (0.05, 0.95))
    i, j = np.indices((8, 8))
    ft = (float(m[:, i > j].sum(1).mean()), float(np.trace(m, axis1=1, axis2=2).mean()),
          float(m[:, i < j].sum(1).mean()))  # fmt: skip
    assert hsv.coherence(half, ft) == []
    assert hsv.coherence(half, (ft[0] + 1e-6, ft[1] - 1e-6, ft[2]))  # not the 90' 1X2
    bad = dict(half)
    k = ("HT1X2", None, "1")
    bad[k] = (bad[k][0] + 1e-6, *bad[k][1:])
    assert hsv.coherence(bad, ft)  # HT/FT no longer marginalises to the half-time 1X2


# ---------------------------------------------------------------------------- serving


def test_90_minute_rows_are_untouched_by_the_half_split(tmp_path: Path) -> None:
    """(e) 1."""
    off, on = world(tmp_path / "off"), world(tmp_path / "on")
    a, b = off.fixture(K), on.fixture(K)
    tick(off, NOW, half=False)
    rep = tick(on, NOW)
    assert rep.half == 1
    ra, rb = rows(off, a), rows(on, b)
    assert not any(k[1] in HALF for k in ra)
    ft = {k: r for k, r in rb.items() if k[1] not in HALF}
    assert ft.keys() == ra.keys()
    for k, r in ra.items():
        assert (ft[k].probability, ft[k].interval_lo, ft[k].interval_hi) == (
            r.probability, r.interval_lo, r.interval_hi)  # fmt: skip
    assert sum(k[1] in HALF for k in rb) == 3 + 9 + 2


def test_half_rows_are_coherent_and_name_the_share_refit(tmp_path: Path) -> None:
    """(e) 3, (b)."""
    w = world(tmp_path)
    mid = w.fixture(K)
    tick(w, NOW)
    r = rows(w, mid)
    p = {k[1:]: v.probability for k, v in r.items()}
    j = np.array([[p[("HTFT", None, f"{a}/{b}")] for b in hsv.CODES] for a in hsv.CODES])
    assert abs(j.sum() - 1) < 1e-9
    np.testing.assert_allclose(j.sum(0), [p[("1X2", None, s)] for s in serve.RESULTS], atol=1e-9)
    np.testing.assert_allclose(j.sum(1), [p[("HT1X2", None, c)] for c in hsv.CODES], atol=1e-9)
    for v in r.values():
        assert v.interval_lo is not None and v.interval_hi is not None
        assert v.interval_lo <= v.probability <= v.interval_hi
    pr = w.s.get(models.ProjectionRun, next(iter(r.values())).projection_run_id)
    share = w.s.scalars(select(models.ModelRun).where(
        models.ModelRun.component == hm.LIVE_COMPONENT)).one()  # fmt: skip
    assert pr is not None
    assert pr.component_versions[serve.HALF_KEY]["model_run_id"] == share.model_run_id


def test_incoherent_fixture_keeps_its_90_minute_rows_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_path)
    mid = w.fixture(K)
    monkeypatch.setattr(hsv, "coherence", lambda half, ft: ["broken on purpose"])
    rep = tick(w, NOW)
    assert rep.half == 0 and any("incoherent" in p for p in rep.problems)
    r = rows(w, mid)
    assert r and not any(k[1] in HALF for k in r)


def test_scope_no_half_rows(tmp_path: Path) -> None:
    """(e) 4: a league-season without the capability, a failed share refit, one older than
    28 days, none at all."""
    w = World(tmp_path / "cap")
    w.refit(AS_OF)
    add_cap(w, 39)
    add_cap(w, 40, status="partial")
    add_share(w, AS_OF)
    a, b = w.fixture(K, league=39), w.fixture(K, league=40, home=3, away=4)
    tick(w, NOW)
    assert any(k[1] in HALF for k in rows(w, a))
    assert not any(k[1] in HALF for k in rows(w, b))

    for name, setup in (
        ("failed", lambda v: add_share(v, AS_OF, status="failed")),
        ("old", lambda v: add_share(v, AS_OF - timedelta(days=29))),
        ("none", lambda v: None),
    ):
        v = world(tmp_path / name, share=False)
        setup(v)
        mid = v.fixture(K)
        rep = tick(v, NOW)
        assert rows(v, mid) and not any(k[1] in HALF for k in rows(v, mid)), name
        assert any(p.startswith("half split") for p in rep.problems), name


def test_cup_ties_get_no_half_rows(tmp_path: Path) -> None:
    w = world(tmp_path)
    c = models.Competition(source_id="api_football", source_ref="45", name="FA Cup",
                           country="X", tier="cup")  # fmt: skip
    w.s.add(c)
    w.s.flush()
    se = models.Season(competition_id=c.competition_id, year=2026)
    w.s.add(se)
    w.s.flush()
    w.season[45] = se.season_id
    for h, a, lg in ((1, 2, 39), (3, 4, 39), (5, 6, 40)):
        w.fixture(AS_OF - timedelta(days=3), league=lg, home=h, away=a, status="FT")
    tie = w.fixture(K, league=45, home=5, away=1, rnd="3rd Round")
    tick(w, NOW, cups={45: "england"})
    r = rows(w, tie)
    assert r and not any(k[1] in HALF for k in r)


def test_future_share_refit_is_not_used_and_writes_are_idempotent(tmp_path: Path) -> None:
    """(e) 5."""
    w = world(tmp_path, share=False)
    add_share(w, NOW + timedelta(hours=1))  # after the projection time
    mid = w.fixture(K)
    tick(w, NOW)
    assert not any(k[1] in HALF for k in rows(w, mid))
    v = world(tmp_path / "again")
    m2 = v.fixture(K)
    tick(v, NOW)
    n = len(rows(v, m2))
    tick(v, NOW)
    assert len(rows(v, m2)) == n


def test_last_passing_share_refit_is_used(tmp_path: Path) -> None:
    w = world(tmp_path, share=False)
    first = add_share(w, AS_OF - timedelta(days=7))
    add_share(w, AS_OF, status="failed")
    mid = w.fixture(K)
    rep = tick(w, NOW)
    assert rep.half == 1
    pr = w.s.get(models.ProjectionRun, rep.projection_run_id)
    assert pr is not None and pr.component_versions[serve.HALF_KEY]["model_run_id"] == first
    assert any("failed" in p for p in rep.problems)
    assert any(k[1] in HALF for k in rows(w, mid))


# ---------------------------------------------------------------------------- the page


def ctx(w: World) -> views.Context:
    return views.Context(bundles={"england": LEAGUES}, model_name=PROD, max_goals=10,
                         store=w.store)  # fmt: skip


def test_match_view_recomputes_the_half_markets(tmp_path: Path) -> None:
    w = world(tmp_path)
    mid = w.fixture(K)
    tick(w, NOW)
    v = views.match_view(w.s, ctx(w), mid, NOW)
    assert v.coherence is not None and v.coherence.coherent
    assert v.half_coherence is not None and v.half_coherence.coherent
    assert v.half_coherence.max_abs_diff <= 1e-9 and v.half_reason is None
    assert "Half-split" not in {u.panel for u in v.unavailable}


def test_match_view_hides_half_markets_that_do_not_recompute(tmp_path: Path) -> None:
    w = world(tmp_path)
    mid = w.fixture(K)
    tick(w, NOW)
    MP = models.MarketProjection
    r = w.s.scalars(select(MP).where(MP.match_id == mid, MP.market_type == "HT1X2")).first()
    assert r is not None
    r.probability += 1e-6
    w.s.commit()
    v = views.match_view(w.s, ctx(w), mid, NOW)
    assert v.half_coherence is not None and not v.half_coherence.coherent


def test_match_view_says_why_there_are_no_half_markets(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.refit(AS_OF)
    add_share(w, AS_OF)
    mid = w.fixture(K)  # no capability row: out of scope
    tick(w, NOW)
    v = views.match_view(w.s, ctx(w), mid, NOW)
    assert v.half_coherence is None and "no half-time forecast" in (v.half_reason or "")


# ---------------------------------------------------------------------------- the refit


def fake_fitter(df: pl.DataFrame, train: Any, as_of: datetime) -> Any:
    post = {"beta": np.full((8, 2), hm.LOGIT0), "alpha": np.zeros((8, 2)), "u": np.zeros((8, 3))}
    o = SimpleNamespace(league_ids=list(LEAGUES), team_ids=[1, 2, 3])
    g = {"passes": True, "failed": [], "n_obs": int(len(train)), "n_matches": int(len(train)),
         "convergence": [], "ppc": {}, "sensitivity_max_move": {}}  # fmt: skip
    return post, o, g


def test_production_share_refit_records_itself_once(tmp_path: Path) -> None:
    w = World(tmp_path)
    cfg = harness.load_config().model_copy(update={"bundles": {"england": LEAGUES}})
    df = pl.DataFrame({"match_id": [1, 2, 3], "xh": [0, 1, 1], "xa": [1, 0, 0],
                       "hg": [1, 2, 1], "ag": [1, 0, 2],
                       "kickoff": [AS_OF - timedelta(days=d) for d in (40, 10, 0)]})  # fmt: skip
    row = hm.live_refit(w.s, w.store, cfg, df, AS_OF, "sha", fitter=fake_fitter)
    assert row.status == "ok" and row.component == hm.LIVE_COMPONENT and row.as_of == AS_OF
    assert row.manifest["n_obs"] == 2  # the match at the as_of is not final yet
    assert w.store.get(hm.live_key(AS_OF, "npz"))
    again = hm.live_refit(w.s, w.store, cfg, df, AS_OF, "sha",
                          fitter=lambda *a: pytest.fail("refit twice"))  # fmt: skip
    assert again.model_run_id == row.model_run_id
    with pytest.raises(SystemExit):
        hm.live_refit(w.s, w.store, cfg, df, AS_OF + timedelta(days=1), fitter=fake_fitter)


def test_production_share_refit_waits_for_results(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.fixture(AS_OF - timedelta(days=1))  # not final, should be by the as_of
    cfg = harness.load_config().model_copy(update={"bundles": {"england": LEAGUES}})
    df = pl.DataFrame({"match_id": [1], "xh": [0], "xa": [0], "hg": [0], "ag": [0],
                       "kickoff": [AS_OF - timedelta(days=10)]})  # fmt: skip
    with pytest.raises(live.NotReady):
        hm.live_refit(w.s, w.store, cfg, df, AS_OF, fitter=fake_fitter)


def test_utc_as_of() -> None:
    assert hm.live_key(datetime(2026, 11, 30, tzinfo=UTC), "npz") == "serving/half/S0/20261130.npz"
