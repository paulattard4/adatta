"""Projected lineups (docs/DASHBOARD_TASKS.md): P3-b's features for a fixture not played yet,
the serving path against the dev path, the serving refit's storage, the fixtures projected,
and the squad panel's projection with injury tags."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from test_dashboard_api import ctx
from test_selection import T0, TEAM, world
from test_serve import K, World

from adatta.adapters.normalize import SOURCE_ID
from adatta.adapters.storage import LocalStore
from adatta.api import views
from adatta.db import models
from adatta.eval import selection as ev
from adatta.features import selection as sel
from adatta.model import selection as ms
from adatta.sim import lineups as sl

TARGET_KO = T0 + timedelta(weeks=6)


def test_a_fixture_not_played_yet_gets_the_dev_features() -> None:
    dev, _ = sel.build(world())
    inp = world()
    # the target match not played: its appearances removed, built as of kickoff - 24 h
    unplayed = sel.Inputs(
        inp.appearances.filter(pl.col("match_id") != 200), inp.coaches, inp.context,
        inp.targets.with_columns(as_of=pl.lit(TARGET_KO - sel.HORIZON)),
    )  # fmt: skip
    served, teams = sel.build(unplayed)
    cols = ["player_id", *sel.FEATURES, "pos"]
    pick = pl.col("team_id") == TEAM
    a = dev.filter(pick).select(cols).sort("player_id")
    b = served.filter(pick).select(cols).sort("player_id")
    assert a.equals(b)
    assert (served.filter(pick)["y"] == -1).all()
    t = teams.filter(pl.col("team_id") == TEAM).row(0, named=True)
    assert t["starters"] == -1 and t["bench_expected"] > 0
    # an earlier as-of never sees later matches: built a week out, the last match is unseen
    early, _ = sel.build(sel.Inputs(unplayed.appearances, inp.coaches, inp.context,
                                    unplayed.targets.with_columns(
                                        as_of=pl.lit(TARGET_KO - timedelta(days=8)))))  # fmt: skip
    r13 = early.filter(pick & (pl.col("player_id") == 13)).row(0, named=True)
    assert r13["last_start"] == 0  # 13's start in the last match came after that as-of


def _post(design: ms.Design, n: int = 6, seed: int = 1) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    nf, nl, nm = len(sel.FEATURES), len(design.leagues), len(ms.MINUTES_X)
    npos = len(sel.POS) + 1
    return {
        "a": rng.normal(0, 0.3, (n, nl, 2)), "b": rng.normal(0, 0.1, (n, nf, 2)),
        "pos_raw": rng.normal(0, 0.2, (n, npos - 1, 2)),
        "c0": rng.normal(1, 0.2, (n, npos)), "g": rng.normal(0, 0.1, (n, nm)),
        "e0": rng.normal(-0.5, 0.2, (n, npos)), "k": rng.normal(0, 0.1, (n, nm)),
        "start_m_d0": rng.normal(1, 0.2, (n, npos)), "start_m_h": rng.normal(0, 0.1, (n, nm)),
        "start_m_phi": rng.uniform(5, 10, (n, npos)),
        "on_m_d0": rng.normal(-0.5, 0.2, (n, npos)), "on_m_h": rng.normal(0, 0.1, (n, nm)),
        "on_m_phi": rng.uniform(5, 10, (n, npos)),
    }  # fmt: skip


def test_the_serving_path_gives_the_dev_numbers(tmp_path: Path) -> None:
    players, teams = sel.build(world())
    d = ms.design(players)
    post = _post(d)
    nu = {"39": (0.4, 0.6)}
    store = LocalStore(tmp_path)
    key = sl.refit_key("england", TARGET_KO)
    store.put(key, sl.encode(post, d, nu, {"gates_ok": True}))
    post2, d2, nu2, meta = sl.decode(store.get(key))
    assert d2 == d and nu2 == nu and meta["gates_ok"]
    got = sl.project(post2, d2, nu2, players, teams)
    p, pmf = ms.predict(d, post, players, teams, nu)
    e_start, e_bench = ms.role_minutes(d, post, players)
    assert np.allclose(got["p_start"].to_numpy(), p[:, 0], atol=1e-12)
    assert np.allclose(got["p_out"].to_numpy(), p[:, 2], atol=1e-12)
    assert np.allclose(got["emin_start"].to_numpy(), e_start, atol=1e-12)
    assert np.allclose(got["emin"].to_numpy(), pmf @ ev.MINUTES, atol=1e-12)
    team = got.join(
        players.select("match_id", "team_id", "player_id"), on=["match_id", "team_id", "player_id"]
    ).filter(pl.col("team_id") == TEAM)
    assert team["p_start"].sum() == pytest.approx(11.0 - nu["39"][0])  # coherent


def test_refits_are_due_every_28_days(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    assert sl.due(store, "england", K)
    store.put(sl.latest_key("england"),
              b'{"key": "k", "as_of": "2026-11-02T00:00:00+00:00"}')  # fmt: skip
    assert not sl.due(store, "england", K)  # 21 Nov: 19 days on
    assert sl.due(store, "england", K + timedelta(days=9))


def test_upcoming_fixtures_and_their_as_of(tmp_path: Path) -> None:
    w = World(tmp_path)
    near = w.fixture(K)
    far = w.fixture(K + timedelta(days=10), home=3, away=4)
    cup = w.fixture(K, home=5, away=6, rnd="Relegation Round")
    w.s.commit()
    now = K - timedelta(days=3)
    tg = sl.upcoming(w.s, [39, 40], now)
    assert tg["match_id"].to_list() == [near] and far != near and cup != near
    assert tg["as_of"][0] == now  # more than 24 h out: as of now
    tg = sl.upcoming(w.s, [39, 40], K - timedelta(hours=5))
    assert tg["as_of"][0] == K - sel.HORIZON  # inside 24 h: P3-b's horizon


def _projection(w: World, mid: int, gen: object, p_by_player: dict[int, float]) -> None:
    for pid, p in p_by_player.items():
        w.s.add(models.LineupProjection(
            match_id=mid, team_id=1, player_id=pid, generated_at=gen, as_of=gen,
            refit_key="serving/lineups/england/x.npz", refit_as_of=K - timedelta(days=12),
            pos="F", p_start=p, p_bench=0.1, p_out=0.9 - p if p < 0.9 else 0.0,
            emin_start=80.0, emin_bench=20.0, emin=80.0 * p,
        ))  # fmt: skip


def test_squad_panel_shows_the_latest_projection_with_injury_tags(tmp_path: Path) -> None:
    w = World(tmp_path)
    mid = w.fixture(K)
    for pid, name in ((1, "Ana"), (2, "Bea"), (3, "Cal")):
        w.s.add(models.Player(player_id=pid, source_id=SOURCE_ID, source_ref=str(pid), name=name))
    w.s.flush()
    day = timedelta(days=1)
    _projection(w, mid, K - 3 * day, {1: 0.2, 2: 0.8})
    _projection(w, mid, K - 2 * day, {1: 0.9, 2: 0.5, 3: 0.3})
    w.s.add(models.PlayerStatusSnapshot(player_id=2, team_id=1, match_id=mid,
                                        source_id=SOURCE_ID, status="questionable",
                                        observed_at=K - 4 * day,
                                        valid_from=K - 4 * day))  # fmt: skip
    w.s.commit()
    sq = views.match_view(w.s, ctx(w), mid, K - day).squad
    assert sq is not None and sq.projection is not None
    assert sq.projection.generated_at == K - 2 * day
    got = [(p.name, p.p_start, p.status) for p in sq.home.projected]
    assert got == [("Ana", 0.9, None), ("Bea", 0.5, "doubtful"), ("Cal", 0.3, None)]
    # before the second run, the first is shown
    sq = views.match_view(w.s, ctx(w), mid, K - 2 * day - timedelta(hours=1)).squad
    assert sq is not None and [p.name for p in sq.home.projected] == ["Bea", "Ana"]
