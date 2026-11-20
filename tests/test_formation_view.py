"""P3-h (docs/PHASE3_TASKS.md): P3-c served with tier unknown, the formation table's
unplayed rows, the last lineup in a formation as of the projection, the refit store and
the squad panel's formation view."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from test_dashboard_api import ctx
from test_formation import VOCAB, world
from test_serve import K, World

from adatta.adapters.normalize import SOURCE_ID
from adatta.adapters.storage import LocalStore
from adatta.api import views
from adatta.db import models
from adatta.eval import formation as ev
from adatta.features import formation as fe
from adatta.features.referees import RESULT_LAG
from adatta.model import formation as mf
from adatta.sim import formations as sf

T1 = datetime(2026, 9, 5, 15, tzinfo=UTC)


def test_served_rows_have_tier_unknown_and_keep_unplayed_fixtures() -> None:
    inp = world()
    no_tier = fe.Inputs(inp.formations, inp.coaches, inp.context, ev.no_tier_input(),
                        inp.targets)  # fmt: skip
    built = fe.build(no_tier, VOCAB)
    assert built.equals(ev.no_tier(fe.build(inp, VOCAB)))  # the dev rerun's table is the same
    # an unplayed fixture (no lineup yet) is kept with outcome -1 when serving only
    ko = inp.targets["kickoff"][0] + timedelta(weeks=1)
    tg = pl.DataFrame({"match_id": [100], "league_ref": ["39"], "season": [2018],
                       "kickoff": [ko], "home_team_id": [1], "away_team_id": [2]},
                      schema_overrides={"kickoff": pl.Datetime("us", "UTC")})  # fmt: skip
    both = pl.concat([inp.targets, tg])
    mixed = fe.Inputs(inp.formations, inp.coaches, inp.context, ev.no_tier_input(), both)
    assert set(fe.build(mixed, VOCAB)["match_id"]) == {99}  # the dev path skips it
    assert fe.build(mixed, VOCAB, unplayed=True).height == 4
    nxt = fe.Inputs(inp.formations, inp.coaches, inp.context, ev.no_tier_input(), tg)
    t = fe.build(nxt, VOCAB, unplayed=True)
    assert t.height == 2 and (t["y"] == -1).all()
    # its own as_of: a week earlier, match 99 (a week before kickoff) is not final yet
    early = tg.with_columns(as_of=pl.lit(ko - timedelta(weeks=1)).cast(pl.Datetime("us", "UTC")))
    e = fe.build(fe.Inputs(inp.formations, inp.coaches, inp.context, ev.no_tier_input(), early),
                 VOCAB, unplayed=True)  # fmt: skip
    home_t, home_e = (x.filter(pl.col("team_id") == 1).row(0, named=True) for x in (t, e))
    assert home_t["log_team_n"] > home_e["log_team_n"]
    # (d)1: a formation played after the as-of time changes nothing
    later = pl.DataFrame({"match_id": [98], "team_id": [1],
                          "kickoff": [ko - timedelta(days=3)], "formation": ["4-3-3"]},
                         schema_overrides={"kickoff": pl.Datetime("us", "UTC")})  # fmt: skip
    plus = fe.Inputs(pl.concat([inp.formations, later]), inp.coaches, inp.context,
                     ev.no_tier_input(), early)  # fmt: skip
    assert fe.build(plus, VOCAB, unplayed=True).equals(e)


def _lineups() -> pl.DataFrame:
    """Team 1: 4-4-2 on T1, 4-3-3 a week later, 4-4-2 again two weeks later (a different
    keeper); positional except the last one."""
    rows = []
    for w, f, gk, positional in ((0, "4-4-2", 1, True), (1, "4-3-3", 1, True),
                                 (2, "4-4-2", 12, False)):  # fmt: skip
        ko = T1 + timedelta(weeks=w)
        for i in range(11):
            row = 1 if i == 0 else 2 if i < 5 else 3 if i < 9 else 4
            rows.append({"match_id": 10 + w, "team_id": 1, "player_id": gk if i == 0 else i + 1,
                         "kickoff": ko, "formation": f, "positional": positional,
                         "slot": "GK" if i == 0 else f"S{i}", "line": "GDDDDMMMMFF"[i],
                         "row": row, "col": 1 + (i - 1) % 4 if i else 1})  # fmt: skip
    return pl.DataFrame(rows, schema_overrides={"kickoff": pl.Datetime("us", "UTC")})


def test_pitch_is_the_last_lineup_in_that_formation_final_by_the_as_of() -> None:
    lu = _lineups()
    idx = sf.last_lineups(lu)
    after_first = T1 + timedelta(days=10)
    p = sf.pitch(lu, idx, 1, "4-4-2", after_first)
    assert p is not None and p["match_id"] == 10 and p["positional"]
    assert p["slots"][0] == {"code": "GK", "player_id": 1, "row": 1, "col": 1}
    # the third match is final only RESULT_LAG after kickoff
    third = T1 + timedelta(weeks=2)
    assert sf.pitch(lu, idx, 1, "4-4-2", third + RESULT_LAG / 2)["match_id"] == 10  # type: ignore[index]
    late = sf.pitch(lu, idx, 1, "4-4-2", third + RESULT_LAG)
    assert late is not None and late["match_id"] == 12 and not late["positional"]
    assert late["slots"][0]["code"] == "G" and late["slots"][0]["player_id"] == 12  # lines only
    assert sf.pitch(lu, idx, 1, "3-5-2", late["kickoff"] + timedelta(days=9)) is None
    assert sf.pitch(lu, idx, 1, "4-4-2", T1) is None  # nothing final yet


def test_refit_store_and_distribution(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    rng = np.random.default_rng(0)
    k = len(VOCAB)
    post = {"a": rng.normal(size=(20, k - 1)), "b": rng.normal(size=(20, 3)),
            "g": rng.normal(size=(20, len(mf.PER_CLASS), k - 1))}  # fmt: skip
    now = datetime(2026, 11, 20, 5, tzinfo=UTC)
    assert sf.due(store, "england", now)
    key = sf.refit_key("england", now - timedelta(days=3))
    store.put(key, sf.encode(post, VOCAB, {"gates_ok": True}))
    store.put(
        sf.latest_key("england"),
        f'{{"key": "{key}", "as_of": "{(now - timedelta(days=3)).isoformat()}"}}'.encode(),
    )
    assert not sf.due(store, "england", now)
    assert sf.due(store, "england", now + timedelta(days=26))
    p2, v2, meta = sf.decode(store.get(key))
    assert v2 == VOCAB and meta["gates_ok"] and np.allclose(p2["g"], post["g"])
    rows = ev.no_tier(fe.build(world(), VOCAB))
    d = sf.distribution(p2, rows, VOCAB)
    assert d.shape == (2, k) and np.allclose(d.sum(axis=1), 1.0)
    assert np.allclose(d, mf.probs(post, rows, k).mean(axis=0))


def test_squad_panel_shows_three_formations_and_the_pitch(tmp_path: Path) -> None:
    w = World(tmp_path)
    mid = w.fixture(K)
    for pid, name in ((1, "Ana"), (2, "Bea")):
        w.s.add(models.Player(player_id=pid, source_id=SOURCE_ID, source_ref=str(pid), name=name))
    w.s.flush()
    day = timedelta(days=1)
    for gen, top in ((K - 3 * day, "4-3-3"), (K - 2 * day, "4-4-2"), (K + day, "3-5-2")):
        w.s.add(models.FormationProjection(
            match_id=mid, team_id=1, generated_at=gen, as_of=gen, refit_key="k",
            refit_as_of=K - 9 * day,
            probs={"4-4-2": 0.1, "4-3-3": 0.2, "3-5-2": 0.05, "other": 0.15} | {top: 0.5},
            pitch_formation=top, last_match_id=None, last_kickoff=K - 9 * day, positional=True,
            slots=[{"code": "GK", "player_id": 1, "row": 1, "col": 1},
                   {"code": "ST", "player_id": 2, "row": 4, "col": 1}],
        ))  # fmt: skip
    w.s.add(models.PlayerStatusSnapshot(player_id=2, team_id=1, match_id=mid,
                                        source_id=SOURCE_ID, status="missing",
                                        observed_at=K - 4 * day,
                                        valid_from=K - 4 * day))  # fmt: skip
    w.s.commit()
    sq = views.match_view(w.s, ctx(w), mid, K - day).squad
    assert sq is not None and sq.away.formation is None
    f = sq.home.formation
    assert f is not None and f.pitch_formation == "4-4-2"  # the latest generated by now
    assert [(o.formation, o.p) for o in f.options] == [("4-4-2", 0.5), ("4-3-3", 0.2),
                                                       ("other", 0.15)]  # fmt: skip
    assert [(p.name, p.code, p.p_start, p.status) for p in f.pitch] == [
        ("Ana", "GK", None, None), ("Bea", "ST", None, "out")]  # fmt: skip
    assert sum(o.p for o in f.options) == pytest.approx(0.85)
