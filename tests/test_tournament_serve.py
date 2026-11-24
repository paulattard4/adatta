"""Stage D v2 serving (docs/BEYOND_LEAGUES_PLAN.md, "D v2 serving acceptance"), tests 1-4 and
6's views: knockout ties in D v1's projection runs, the tournament payload, eligibility and
reasons, leakage and the match view. Synthetic data only (D v1's test world)."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import select

from adatta.api import national as api_national
from adatta.api import views
from adatta.db import models
from adatta.eval import tournament as ev
from adatta.model import national as nt
from adatta.sim import national_serve as ns
from adatta.sim import ties
from adatta.sim import tournament as tm
from adatta.sim import tournament_serve as ts

sys.path.insert(0, str(Path(__file__).parent))
from test_national_serve import KICK, MONDAY, World, fake_post  # noqa: E402
from test_tournament import mini, mini_state  # noqa: E402

LEG2 = KICK + timedelta(days=3)


def tie_world(tmp_path: Path, round_: str = "Quarter-finals - League A") -> tuple[World, int, int]:
    w = World(tmp_path)
    w.refit(MONDAY)
    l1, l2 = w.match(KICK, 1, 2), w.match(LEG2, 2, 1)
    for mid in (l1, l2):
        m = w.s.get(models.Match, mid)
        assert m is not None
        m.round = round_
    w.s.flush()
    return w, l1, l2


def rows(w: World, mid: int, ms: str) -> dict[tuple[str, str], models.MarketProjection]:
    MP = models.MarketProjection
    got = w.s.scalars(select(MP).where(MP.match_id == mid, MP.milestone == ms)).all()
    return {(r.market_type, r.selection): r for r in got}


# --------------------------------------------------------------- 1, 2: identity, coherence


def test_tie_legs_get_p_advance_in_the_same_run_as_the_1x2(tmp_path: Path) -> None:
    w, l1, l2 = tie_world(tmp_path)
    now = MONDAY + timedelta(hours=6)
    rep = w.tick(now)
    assert rep.written == {"initial": 2, "T-7d": 1, "ties": 2}  # leg 2 is inside T-7d
    a, b = rows(w, l1, "initial"), rows(w, l2, "initial")
    assert len(a) == len(b) == 3 + 2 + 6
    assert len({r.projection_run_id for r in a.values()}) == 1  # one run with D v1's 1X2
    pr = w.s.get(models.ProjectionRun, a[("1X2", "home")].projection_run_id)
    assert pr is not None and set(pr.component_versions) == {ns.BUNDLE, ties.QUALIFY}
    # side 1 is leg 1's home side and leg 2's away side: the same tie, the same number
    side1 = (a[(ties.QUALIFY, "home")].probability, b[(ties.QUALIFY, "away")].probability)
    assert abs(side1[0] - side1[1]) < 1e-12
    for r in (a, b):
        assert abs(r[(ties.QUALIFY, "home")].probability + r[(ties.QUALIFY, "away")].probability
                   - 1) < 1e-9  # fmt: skip
        path = [v.probability for (m, _), v in r.items() if m == ties.PATH]
        assert abs(sum(path) - 1) < 1e-9
        home = sum(v.probability for (m, s), v in r.items() if m == ties.PATH and
                   s.startswith("home"))  # fmt: skip
        assert abs(home - r[(ties.QUALIFY, "home")].probability) < 1e-9
        q = r[(ties.QUALIFY, "home")]
        assert q.interval_lo is not None and q.interval_hi is not None
        assert q.interval_lo <= q.probability <= q.interval_hi
    # identity: what was written is `ties.project` on the refit's draws
    found, _, confs = ties.stored(w.s, w.store, ties.load_rules(), MONDAY, MONDAY + timedelta(30))
    want = ties.project(fake_post([1, 2, 3, 4]), found[0], l1, confs, MONDAY, 150, now)
    assert abs(want[(ties.QUALIFY, None, "home")][0] - a[(ties.QUALIFY, "home")].probability) < 1e-9


def test_between_legs_uses_leg_1_once_final_and_not_before(tmp_path: Path) -> None:
    w, l1, l2 = tie_world(tmp_path)
    w.tick(MONDAY + timedelta(hours=6))
    m = w.s.get(models.Match, l1)
    assert m is not None
    m.status = "FT"
    w.s.add(models.MatchResult(match_id=l1, source_id="api_football", home_goals_nt=3,
                               away_goals_nt=0, observed_at=KICK + timedelta(hours=2)))  # fmt: skip
    w.s.flush()
    found, _, confs = ties.stored(w.s, w.store, ties.load_rules(), MONDAY, MONDAY + timedelta(30))
    post = fake_post([1, 2, 3, 4])
    early = ties.project(post, found[0], l2, confs, MONDAY, 150, KICK + timedelta(hours=2))
    late = ties.project(post, found[0], l2, confs, MONDAY, 150, KICK + timedelta(hours=3))
    before = rows(w, l2, "initial")[(ties.QUALIFY, "away")].probability
    assert abs(early[(ties.QUALIFY, None, "away")][0] - before) < 1e-9  # not final 3 h before
    assert late[(ties.QUALIFY, None, "away")][0] > before + 0.1  # 3-0 up after leg 1
    rep = w.tick(LEG2 - timedelta(hours=23))  # leg 2's T-24h milestone
    assert rep.written.get("ties") == 1
    got = rows(w, l2, "T-24h")[(ties.QUALIFY, "away")].probability
    assert abs(got - late[(ties.QUALIFY, None, "away")][0]) < 1e-9


# ------------------------------------------------------------------- 3: eligibility, reasons


def test_rounds_without_a_written_rule_get_the_90_minute_forecast_only(tmp_path: Path) -> None:
    w, l1, _ = tie_world(tmp_path, round_="Final")
    w.tick(MONDAY + timedelta(hours=6))
    assert {k[0] for k in rows(w, l1, "initial")} == {"1X2"}
    ctx = views.Context(bundles={}, model_name="x", max_goals=10, store=w.store)
    mv = views.match_view(w.s, ctx, l1, MONDAY + timedelta(hours=6))
    assert mv.qualify is None
    assert [u.reason for u in mv.unavailable if u.panel == "Going through"] == [ties.NO_RULE]


def test_youth_ties_are_never_forecast(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.refit(MONDAY)
    l1, l2 = w.match(KICK, 5, 1), w.match(LEG2, 1, 5)  # 5 is an U21 side
    for mid in (l1, l2):
        m = w.s.get(models.Match, mid)
        assert m is not None
        m.round = "Play-offs A/B"
    w.s.flush()
    w.tick(MONDAY + timedelta(hours=6))
    assert rows(w, l1, "initial") == {} and rows(w, l2, "initial") == {}
    found, irregular, _ = ties.stored(w.s, w.store, ties.load_rules(), MONDAY,
                                      MONDAY + timedelta(30))  # fmt: skip
    assert found == [] and set(irregular) == {l1, l2}


# --------------------------------------------------------------------------- 6: the views


def test_match_view_shows_p_advance_with_its_path(tmp_path: Path) -> None:
    w, l1, l2 = tie_world(tmp_path)
    now = MONDAY + timedelta(hours=6)
    w.tick(now)
    ctx = views.Context(bundles={}, model_name="x", max_goals=10, store=w.store)
    mv = views.match_view(w.s, ctx, l2, now)
    q = mv.qualify
    assert q is not None and q.stage == "before leg 1" and q.label == ties.LABEL
    assert abs(q.home.p + q.away.p - 1) < 1e-9 and len(q.path) == 6
    assert [x.match_id for x in q.legs] == [l1, l2] and q.round == "Quarter-finals - League A"
    assert [s.market for s in mv.milestones[-1].selections] == ["1X2"] * 3  # 1X2 stays alone
    assert mv.unavailable == api_national.UNAVAILABLE  # no "going through" reason
    assert mv.coherence is not None and mv.coherence.coherent


# ------------------------------------------------------------------------ tournaments


def test_tournament_payload_is_the_simulation() -> None:
    ed = mini()
    teams = list(range(1, 9))
    rt = tm.rates(fake_post(teams), teams, {t: "UEFA" for t in teams}, 100, 60, [100], "finals",
                  np.random.default_rng(2))  # fmt: skip
    st = mini_state()
    res = tm.simulate(ed, st, rt, 2000, tm.SEED)
    again = tm.simulate(ed, st, rt, 2000, tm.SEED)
    assert np.array_equal(res.win, again.win)  # the same seed gives the same forecast
    d = ev.EdData(ed, st.groups, [], [], {t: "UEFA" for t in teams}, {t: None for t in teams},
                  {t: f"T{t}" for t in teams})  # fmt: skip
    t0 = datetime(2020, 6, 1, tzinfo=UTC)
    body = ts.payload(d, res, t0, t0, None, True, 0)
    for g in body["groups"]:
        for r in g["teams"]:
            i = res.teams.index(r["team_id"])
            assert r["win"][0] == res.win[i] and r["advance"][0] == res.advance[i]
            assert r["reach"]["F"][0] == res.reach["F"][i]
            assert abs(sum(p for p, _ in r["position"]) - 1) < 1e-9
    assert 0 < body["mc_se_max"] <= np.sqrt(0.25 / 2000) + 1e-12


def test_no_live_edition_writes_nothing(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.refit(MONDAY)
    counts, problems = ts.run_tournaments(w.s, w.store, {"git_commit": "a", "lockfile_hash": "l"},
                                          now=MONDAY)  # fmt: skip
    assert counts == {"written": 0, "unchanged": 0, "withheld": 0} and problems == []
    assert ts.served(w.s) == []


def test_tie_serving_runs_without_the_model_group() -> None:
    code = (
        "import sys\nsys.path.insert(0, 'tests')\n"
        "import numpy as np\n"
        "from adatta.sim import ties, tournament_serve\n"
        "from adatta.model import national as nat\n"
        "m = nat.score_matrix(np.full(3, 1.2), np.full(3, 0.9), np.full(3, 40.), np.zeros(3))\n"
        "ties.before_leg1(m, m, np.full(3, 1.2), np.full(3, 0.9), True)\n"
        "bad = [m for m in ('pymc', 'pytensor', 'jax', 'numpyro', 'arviz') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).parents[1])


def test_the_served_seed_and_label_are_fixed() -> None:
    assert tm.SEED == 20261124 and nt.SEED == 20261121
    assert "coin-flip" in ties.LABEL and "Team-only" in ts.LABEL
