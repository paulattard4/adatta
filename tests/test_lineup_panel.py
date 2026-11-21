"""Lineups, bench, shape and how the projection did (docs/DASHBOARD_TASKS.md): the out rule,
the bench and squad sections, the measured bench limits, the announced lineup on its grid,
and the scores against the announced lineup."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
from test_dashboard_api import ctx
from test_serve import K, World

from adatta.adapters.normalize import SOURCE_ID
from adatta.api import lineups as al
from adatta.api import schemas as sc
from adatta.api import views
from adatta.db import models
from adatta.features.referees import RESULT_LAG
from adatta.features.selection import HORIZON
from adatta.sim import formations as sf


def _pool(out: set[int] = frozenset()) -> list[al.Pooled]:  # type: ignore[assignment]
    """Keepers 1 and 13; outfield 2-12 most likely to start in that order, then 14-16."""
    xs = [al.Pooled(1, "gk1", "G", 0.9, 0.05, 80, None)]
    xs += [al.Pooled(i, f"p{i}", "DMF"[i % 3], 0.95 - 0.04 * i, 0.04 * i, 70, None)
           for i in range(2, 13)]  # fmt: skip
    xs += [al.Pooled(13, "gk2", "G", 0.05, 0.9, 1, None)]
    xs += [al.Pooled(i, f"p{i}", "M", 0.1, 0.5 - 0.1 * (i - 14), 10, None) for i in (14, 15, 16)]
    xs = [al.Pooled(p.player_id, p.name, p.pos, p.p_start, p.p_bench, p.emin,
                    "missing" if p.player_id in out else None) for p in xs]  # fmt: skip
    return sorted(xs, key=lambda p: -p.p_start)


def test_sections_keep_out_players_out_and_fill_a_bench_with_a_keeper() -> None:
    xs = _pool()
    sec = al.sections(xs, limit=3, expected=4.4)
    assert {p.player_id for p in sec["xi"]} == {p.player_id for p in al.likely_xi(xs)}
    assert len(sec["xi"]) == 11 and sum(p.pos == "G" for p in sec["xi"]) == 1
    assert len(sec["bench"]) == 3 and any(p.pos == "G" for p in sec["bench"])  # limit, keeper
    ids = [p.player_id for s in sec.values() for p in s]
    assert sorted(ids) == sorted(p.player_id for p in xs)  # disjoint, the whole pool
    # nobody listed: the XI is today's; listed out: never in the XI or bench
    out = _pool(out={3, 13})
    sec = al.sections(out, limit=None, expected=3.0)
    named = {p.player_id for p in sec["xi"] + sec["bench"]}
    assert not named & {3, 13} and len(sec["xi"]) == 11 and len(sec["bench"]) == 3
    assert {3, 13} <= {p.player_id for p in sec["out"]}
    assert 12 in {p.player_id for p in sec["xi"]}  # the next player takes 3's place
    # without the team's bench size, P3-b's bench places among the pool
    assert al.bench_size(xs, None, None) == round(sum(p.p_bench for p in xs))


def _apps(season: int, n_matches: int, bench: int, kick: datetime,
          first_id: int = 0) -> list[dict[str, object]]:  # fmt: skip
    rows: list[dict[str, object]] = []
    for m in range(n_matches):
        for i in range(11 + bench):
            rows.append({"league_ref": "39", "season": season,
                         "match_id": season * 1000 + first_id + m, "team_id": 1,
                         "kickoff": kick + timedelta(days=m), "started": i < 11})  # fmt: skip
    return rows


def test_bench_limits_are_measured_as_of_the_cutoff() -> None:
    dt = {"kickoff": pl.Datetime("us", "UTC")}
    k0 = datetime(2025, 8, 1, tzinfo=UTC)
    k1 = k0 + timedelta(days=365)
    apps = pl.DataFrame(_apps(2025, 60, 7, k0) + _apps(2026, 30, 9, k1), schema_overrides=dt)
    cut = k1 + timedelta(days=40)
    assert sf.bench_limits(apps, 2026, cut)["39"] == {"limit": 7, "season": 2025, "n": 60}
    more = pl.concat([apps, pl.DataFrame(_apps(2026, 30, 9, k1 + timedelta(days=30), 30),
                                         schema_overrides=dt)])  # fmt: skip
    assert sf.bench_limits(more, 2026, cut + timedelta(days=30))["39"]["limit"] == 9
    # rare larger benches (under 5% of team-matches) don't set the limit
    odd = pl.concat([more, pl.DataFrame(_apps(2026, 2, 12, k1 + timedelta(days=60), 60),
                                        schema_overrides=dt)])  # fmt: skip
    assert sf.bench_limits(odd, 2026, cut + timedelta(days=60))["39"]["limit"] == 9
    # the team's last 5 benches final by the cutoff: three of 2026 (9) and two of 2025 (7)
    assert sf.team_benches(apps, k1 + timedelta(days=2) + RESULT_LAG)["1"] == 8.2


GRID_442 = ["1:1", "2:1", "2:2", "2:3", "2:4", "3:1", "3:2", "3:3", "3:4", "4:1", "4:2"]


def _starters(ids: list[int]) -> list[dict[str, object]]:
    return [{"name": f"p{pid}", "number": pid, "pos": "G" if g == "1:1" else "D", "grid": g,
             "player_ref": str(pid)}
            for pid, g in zip(ids, GRID_442, strict=True)]  # fmt: skip


def test_announced_lineup_on_its_grid() -> None:
    xi = _starters([1, *range(2, 12)])
    got = al.announced_pitch(xi, positional=True)
    assert got is not None and got[0] == "4-4-2"
    assert [c for _, c, _, _ in got[1]][:5] == ["GK", "LB", "LCB", "RCB", "RB"]
    lines = al.announced_pitch(xi, positional=False)
    assert lines is not None and [c for _, c, _, _ in lines[1]][::5] == ["G", "M", "F"]
    assert al.announced_pitch(xi[:10], True) is None
    assert al.announced_pitch([*xi[:10], {**xi[10], "grid": "4:3"}], True) is None


SUBS = [
    {"name": "p13", "player_ref": "13", "pos": "G"},
    {"name": "p14", "player_ref": "14", "pos": "M"},
]


def _world(tmp_path: Path) -> tuple[World, int, datetime]:
    w = World(tmp_path)
    mid = w.fixture(K, status="FT")
    for pid in range(1, 18):
        w.s.add(models.Player(player_id=pid, source_id=SOURCE_ID, source_ref=str(pid),
                              name=f"p{pid}"))  # fmt: skip
    w.s.flush()
    gen = K - 2 * timedelta(days=1)
    for p in _pool(out={3}):
        w.s.add(models.LineupProjection(
            match_id=mid, team_id=1, player_id=p.player_id, generated_at=gen, as_of=K - HORIZON,
            refit_key="k", refit_as_of=K - timedelta(days=9), pos=p.pos, p_start=p.p_start,
            p_bench=p.p_bench, p_out=max(0.0, 1 - p.p_start - p.p_bench), emin_start=80.0,
            emin_bench=20.0, emin=p.emin, status=p.status_as_of))  # fmt: skip
    w.s.add(models.FormationProjection(
        match_id=mid, team_id=1, generated_at=gen, as_of=K - HORIZON, refit_key="k",
        refit_as_of=K - timedelta(days=9), probs={"4-4-2": 0.6, "4-3-3": 0.3, "other": 0.1},
        pitch_formation="4-4-2", last_match_id=None, last_kickoff=None, positional=False,
        slots=None))  # fmt: skip
    w.s.add(models.PlayerRegistration(player_id=17, team_id=1, source_id=SOURCE_ID,
                                      position="Attacker", observed_at=K - timedelta(days=10),
                                      valid_from=K - timedelta(days=10)))  # fmt: skip
    # announced: keeper 1 and outfield 2, 4-12 start (3 was out); 13, 14 on the bench
    w.s.add(models.LineupSnapshot(match_id=mid, team_id=1, source_id=SOURCE_ID,
                                  observed_at=K - timedelta(hours=1), formation="4-4-2",
                                  starters=_starters([1, 2, *range(4, 13)]),
                                  substitutes=SUBS))  # fmt: skip
    w.s.commit()
    year = w.s.query(models.Season.year).filter(models.Season.season_id == w.season[39]).scalar()
    w.store.put(sf.rules_key(gen), json.dumps({
        "cutoff": "x", "bench": {"39": {"limit": 3, "season": year, "n": 100}},
        "team_bench": {"1": 3.2}, "positional": [["39", year]]}).encode())  # fmt: skip
    return w, mid, gen


def test_panel_sections_pitch_and_outcome(tmp_path: Path) -> None:
    w, mid, _ = _world(tmp_path)
    sq = views.match_view(w.s, ctx(w), mid, K + timedelta(hours=3)).squad
    assert sq is not None
    t = sq.home
    sec = {s: [p.name for p in t.projected if p.section == s]
           for s in ("xi", "bench", "out", "squad")}  # fmt: skip
    assert "p3" not in sec["xi"] + sec["bench"] and "p3" in sec["out"]
    assert len(sec["xi"]) == 11 and len(sec["bench"]) == 3 == t.bench_size and t.bench_limit == 3
    assert "p13" in sec["bench"] and sec["squad"] == ["p17"]  # p13: the second keeper
    assert next(p for p in t.projected if p.name == "p17").p_start is None
    assert t.lineup is not None and t.lineup.pitch_formation == "4-4-2" and t.lineup.positional
    assert [p.code for p in t.lineup.pitch][:2] == ["GK", "LB"]
    assert t.formation is not None and t.formation.announced == "4-4-2"
    assert t.formation.announced_p == 0.6
    # no earlier lineup in 4-4-2: the projected XI on an empty grid, in the list's order
    f = t.formation
    assert f.last_kickoff is None and f.positional and len(f.pitch) == 11
    xi = [p for p in t.projected if p.section == "xi"]
    assert sorted((p.slot, p.name) for p in xi) == sorted((p.code, p.name) for p in f.pitch)
    assert xi[0].slot == "GK" and xi[0].name == "p1" and all(p.moved for p in xi)
    o = t.outcome
    assert o is not None and (o.xi_hits, o.xi_hits_raw) == (11, 10)  # the out rule paid off
    assert (o.bench_n, o.bench_hits, o.bench_in_squad) == (3, 2, 2)
    assert (o.formation, o.formation_p, o.formation_top) == ("4-4-2", 0.6, True)


def test_played_scores_use_the_projection_as_of_kickoff_minus_24h(tmp_path: Path) -> None:
    w, mid, gen = _world(tmp_path)
    # a later run, generated after kickoff - 24 h with an earlier as-of, is not the one scored
    w.s.add(models.LineupProjection(
        match_id=mid, team_id=1, player_id=16, generated_at=gen + timedelta(hours=30),
        as_of=gen + timedelta(hours=1), refit_key="k", refit_as_of=gen, pos="M", p_start=1.0,
        p_bench=0.0, p_out=0.0, emin_start=90, emin_bench=0, emin=90))  # fmt: skip
    w.s.commit()
    m = sc.PlayedMatch.model_construct(  # type: ignore[call-arg]
        match_id=mid, kickoff=K, home_lineup=None, away_lineup=None)  # fmt: skip
    row = SimpleNamespace(home_id=1, away_id=2, league_id=39, season_id=w.season[39])
    views._lineup_scores(w.s, w.store, [m], {mid: row})  # type: ignore[dict-item]
    assert m.home_lineup is not None and m.away_lineup is None
    assert m.home_lineup.xi_hits == 11 and m.home_lineup.bench_n == 3
    s = views._lineup_summary([m])
    assert s is not None and (s.n, s.xi_mean, s.xi_raw_mean, s.formation_top) == (1, 11, 10, 1.0)


def _p(pid: int, pos: str, p_start: float, status: str | None = None) -> al.Pooled:
    return al.Pooled(pid, f"p{pid}", pos, p_start, 0.1, 60, status)


LAST_442 = [{"code": c, "row": r, "col": k, "player_id": pid} for c, r, k, pid in [
    ("GK", 1, 1, 1), ("LB", 2, 1, 2), ("LCB", 2, 2, 3), ("RCB", 2, 3, 4), ("RB", 2, 4, 5),
    ("LM", 3, 1, 6), ("LCM", 3, 2, 7), ("RCM", 3, 3, 8), ("RM", 3, 4, 9),
    ("LS", 4, 1, 10), ("RS", 4, 2, 11)]]  # fmt: skip


def test_projected_xi_is_placed_in_the_last_lineup_by_slot_then_line_then_anywhere() -> None:
    usual = {"usual": {"20": ["RB", "D"], "21": [None, "M"], "22": [None, "F"],
                       "30": [None, "G"]}}  # fmt: skip
    # 5 (RB), 7 (LCM) and 10 (LS) are not in the XI; 20 usually RB, 21 a midfielder,
    # 22 a forward; plus the keeper 1 kept
    xi = [_p(i, "D" if i < 6 else "M" if i < 10 else "F", 0.9)
          for i in (1, 2, 3, 4, 6, 8, 9, 11)]  # fmt: skip
    xi = [al.Pooled(1, "p1", "G", 0.9, 0.0, 90, None), *xi[1:],
          _p(20, "D", 0.8), _p(21, "D", 0.7), _p(22, "M", 0.6)]  # fmt: skip
    got = {x.code: x for x in al.place(LAST_442, xi, usual, positional=True)}
    assert got["RB"].player is not None and got["RB"].player.player_id == 20  # (1) usual slot
    assert got["LCM"].player is not None and got["LCM"].player.player_id == 21  # (2) his line
    assert got["LS"].player is not None and got["LS"].player.player_id == 22  # (2) usual line
    assert [x.code for x in got.values() if x.moved] == ["RB", "LCM", "LS"]
    assert got["LCM"].usual == "M" or got["LCM"].usual is None  # no usual slot code: no hover
    assert {x.player.player_id for x in got.values() if x.player} == {p.player_id for p in xi}
    # (3) any outfield slot when no slot of his line is free; a keeper only in goal
    xi2 = [p for p in xi if p.player_id != 22] + [_p(23, "D", 0.5)]
    got2 = {x.code: x for x in al.place(LAST_442, xi2, {}, positional=True)}
    assert got2["LS"].player is not None and got2["LS"].player.player_id == 23
    no_gk = [p for p in xi if p.player_id != 1] + [_p(24, "M", 0.4)]
    got3 = {x.code: x for x in al.place(LAST_442, no_gk, {}, positional=True)}
    assert got3["GK"].player is None and sum(x.player is not None for x in got3.values()) == 10
    # an XI that matches the lineup moves nobody
    same = [_p(int(str(s["player_id"])), "G" if s["code"] == "GK" else "D", 0.9) for s in LAST_442]
    assert not any(x.moved for x in al.place(LAST_442, same, {}, positional=True))


def test_empty_grid_usual_positions_and_slot_accuracy() -> None:
    g = al.grid_slots("4-2-3-1", positional=True)
    assert g is not None and [x["code"] for x in g][:7] == ["GK", "LB", "LCB", "RCB", "RB",
                                                            "CDM", "CDM"]  # fmt: skip
    assert al.grid_slots("4-4-3", True) is None and al.grid_slots("other", True) is None
    lines = al.grid_slots("4-4-2", positional=False)
    assert lines is not None and {x["code"] for x in lines} == {"G", "D", "M", "F"}
    assert al.slot_accuracy([("LB", 2), ("RB", 3), ("GK", None)], {2: "LB", 3: "LB"}) == 0.5
    k = datetime(2026, 9, 1, tzinfo=UTC)
    lu = pl.DataFrame({
        "player_id": [7] * 12 + [8],
        "kickoff": [k - timedelta(days=i) for i in range(12)] + [k - timedelta(days=500)],
        "slot": ["LB"] * 4 + ["RB"] * 6 + ["LB"] * 2 + ["CM"],
        "line": ["D"] * 12 + ["M"], "positional": [True] * 11 + [False, True],
    }, schema_overrides={"kickoff": pl.Datetime("us", "UTC")})  # fmt: skip
    got = sf.usual_positions(lu, k + RESULT_LAG + timedelta(hours=1))
    assert got == {"7": ["RB", "D"]}  # last 10 starts: RB 6, LB 4; 8's start is too old
