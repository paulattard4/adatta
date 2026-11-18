"""P3-d0: the counts table (null rule, line, ramps), its checks, the cell design, as-of
history, the predictive, the baselines, the dispersion choice and the pass rule
(adatta/features/rates.py, adatta/eval/rates.py, adatta.adapters.api_football_stats).
The NumPyro fit runs in the CI model job (tests/test_rates_model.py)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest
from scipy import stats

from adatta.adapters import api_football_stats as st
from adatta.eval import rates as ev
from adatta.features import rates as fr

T0 = datetime(2019, 8, 3, 15, tzinfo=UTC)
LEAGUES = ("39", "40")


def app_row(match: int, team: int, player: int, week: int, minutes: int,
            **kw: Any) -> dict[str, Any]:  # fmt: skip
    row: dict[str, Any] = {
        "match_id": match, "team_id": team, "player_id": player, "league_ref": "39",
        "season": 2019, "kickoff": T0 + timedelta(weeks=week), "started": minutes >= 90,
        "pos": "F", "minutes": minutes, "minutes_provider": minutes or None, "goals": 0,
        "own_goals": 0, "assists": None, "shots_total": 1, "shots_on": 1, "key_passes": 0,
        "tackles": 0, "yellow": None, "source_id": "api_football", "storage_key": "k",
        "observed_at": T0 + timedelta(weeks=week, hours=3),
    }  # fmt: skip
    row.update(kw)
    return row


def frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    utc = pl.Datetime("us", "UTC")
    sch: dict[str, Any] = {"kickoff": utc, "observed_at": utc, "assists": pl.Int32,
                           "yellow": pl.Int32, "pos": pl.Utf8,
                           "minutes_provider": pl.Int32}  # fmt: skip
    return pl.DataFrame(rows, schema_overrides=sch)


NO_LINEUPS = pl.DataFrame(schema={"match_id": pl.Int64, "team_id": pl.Int64,
                                  "player_id": pl.Int64, "slot": pl.Utf8,
                                  "positional": pl.Boolean})  # fmt: skip


def season(player_weeks: dict[int, list[int]], team: int = 1,
           weeks: int = 20) -> list[dict[str, Any]]:  # fmt: skip
    """Team plays every week; each player is in the squad (and plays) in the listed weeks;
    player 99 is always there (so the team has matches)."""
    out = [app_row(team * 1000 + w, team, 99, w, 90) for w in range(weeks)]
    for p, ws in player_weeks.items():
        out += [app_row(team * 1000 + w, team, p, w, 90) for w in ws]
    return out


def test_ramps_null_rule_and_line() -> None:
    rows = season({7: [0, 1, 2, 3, 4, 5, 6, 10, 11, 12, 13]})
    rows[0]["pos"] = None  # player 99's first: no line yet, dropped
    rows[1]["pos"] = None  # player 99's second: takes its earlier line... none yet either
    rows.append(app_row(1000, 1, 8, 0, 0))  # unused sub: not a played row
    start = T0 - timedelta(days=800)  # the stint starts more than 365 days after the data
    t = fr.build(frame(rows), NO_LINEUPS, data_start=start)
    p7 = t.filter(pl.col("player_id") == 7).sort("kickoff")
    assert p7["new_club"].to_list() == [True] * 5 + [False] * 6
    # out for weeks 7-9 (3 club matches), back in week 10: the first 3 are a return
    assert p7["ret"].to_list() == [False] * 7 + [True] * 3 + [False]
    assert p7["y_assists"].to_list() == [0] * 11  # null with a statistics block -> 0
    assert t.filter(pl.col("player_id") == 8).is_empty()
    p99 = t.filter(pl.col("player_id") == 99).sort("kickoff")
    assert len(p99) == 18 and set(p99["line"].to_list()) == {"F"}
    unknown = fr.build(frame(rows), NO_LINEUPS)  # data starts with this season: not new
    assert not unknown.filter(pl.col("player_id") == 7)["new_club"].any()


def test_line_falls_back_to_earlier_appearances() -> None:
    rows = season({7: [0, 1, 2, 3]})
    rows[-1]["pos"] = None
    rows[-2]["pos"] = "M"
    t = fr.build(frame(rows), NO_LINEUPS)
    got = t.filter(pl.col("player_id") == 7).sort("kickoff")["line"].to_list()
    assert got == ["F", "F", "M", "F"]  # earlier: F twice, M once


def test_shots_on_target_null_is_zero_only_where_shots_were_collected() -> None:
    a = frame([app_row(1, 1, 1, 0, 90, shots_on=None), app_row(1, 1, 2, 0, 90, shots_on=None),
               app_row(2, 1, 1, 1, 90, shots_on=None, shots_total=None),
               app_row(2, 1, 2, 1, 90, shots_on=None, shots_total=None)])  # fmt: skip
    t = fr.build(a, NO_LINEUPS).sort("match_id", "player_id")
    assert t["y_shots_on"].to_list() == [0, 0, None, None]


def test_goal_and_shot_checks_and_drops() -> None:
    a = frame([app_row(1, 1, 1, 0, 90, goals=2), app_row(1, 2, 2, 0, 90, own_goals=1),
               app_row(2, 1, 1, 1, 90, goals=1), app_row(2, 2, 2, 1, 90)])  # fmt: skip
    scores = pl.DataFrame({"match_id": [1, 2], "home_team_id": [1, 1], "away_team_id": [2, 2],
                           "home_goals": [3, 2], "away_goals": [0, 0]})  # fmt: skip
    g = fr.goal_check(a, scores)
    assert g["value"][0] == pytest.approx(0.75)  # match 2 home: 1 scored, score says 2
    shots = pl.DataFrame({"match_id": [1, 1], "team_id": [1, 2], "shots": [1, 5]})
    s = fr.shots_check(a, shots)
    assert s["value"][0] == pytest.approx(0.5)
    ev_ = pl.DataFrame({"league_ref": ["39"], "season": [2019], "n": [10], "assists": [1.0],
                        "yellow": [0.9]})  # fmt: skip
    chk = fr.checks(g, s, ev_)
    failed = set(chk.filter(~pl.col("passed"))["stat"].to_list())
    assert failed == {"goals", "shots_on", "yellow"}
    t = fr.build(a, NO_LINEUPS)
    dropped = fr.apply_drops(t, chk)
    assert dropped["y_goals"].is_null().all() and dropped["y_key_passes"].is_not_null().all()


def test_event_counts() -> None:
    fx = {"events": [
        {"type": "Goal", "detail": "Normal Goal", "team": {"id": 1}, "player": {"id": 5},
         "assist": {"id": 6}},
        {"type": "Goal", "detail": "Own Goal", "team": {"id": 1}, "player": {"id": 9},
         "assist": {"id": 6}},
        {"type": "Goal", "detail": "Penalty", "team": {"id": 1}, "player": {"id": 5},
         "assist": {"id": None}, "comments": "Penalty Shootout"},
        {"type": "Card", "detail": "Yellow Card", "team": {"id": 2}, "player": {"id": 7}},
        {"type": "Card", "detail": "Second Yellow card", "team": {"id": 2}, "player": {"id": 7}},
    ]}  # fmt: skip
    c = st.event_counts(fx)
    assert c[("1", "6")] == {"assists": 1, "yellow": 0, "second_yellow": 0}
    assert c[("2", "7")] == {"assists": 0, "yellow": 1, "second_yellow": 1}


def test_design_round_trip() -> None:
    d = ev.Design(LEAGUES, ("LB", "ST"))
    rows = pl.DataFrame({"line": ["G", "F", "D"], "league_ref": ["40", "39", "39"],
                         "sub": [True, False, False], "new_club": [False, True, False],
                         "ret": [True, False, True], "slot": [None, "ST", "LB"]})  # fmt: skip
    f = d.frame(rows)
    assert f["cell"].n_unique() == 3
    x = d.decode(f["cell"].to_numpy())
    assert x["li"].tolist() == [0, 3, 1] and x["gi"].tolist() == [1, 0, 0]
    assert x["sub"].tolist() == [1, 0, 0] and x["new"].tolist() == [0, 1, 0]
    assert x["ret"].tolist() == [1, 0, 1] and x["si"].tolist() == [0, 2, 1]


def rate_table(n_players: int = 30, weeks: int = 40, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    theta = rng.gamma(4.0, 1 / 4.0, n_players)
    rows = []
    for w in range(weeks):
        for p in range(n_players):
            e = 1.0 if (w + p) % 3 else 0.5
            rows.append({"match_id": w * 100 + p // 2, "team_id": p % 2, "player_id": p,
                         "league_ref": LEAGUES[p % 2], "season": 2019 + w // 20,
                         "kickoff": T0 + timedelta(weeks=w), "sub": e < 1, "e": e,
                         "line": "F" if p % 3 else "D", "new_club": False, "ret": False,
                         "slot": None, **{f"y_{s}": int(rng.poisson(0.4 * theta[p] * e))
                                          for s in fr.STATS}})  # fmt: skip
    return pl.DataFrame(rows, schema_overrides={"kickoff": pl.Datetime("us", "UTC"),
                                                "slot": pl.Utf8})  # fmt: skip


def post_for(d: ev.Design, n: int = 6, log_k: float = np.log(4.0)) -> dict[str, np.ndarray]:
    return {"log_mu": np.full((n, 4), np.log(0.4)), "log_l": np.zeros((n, len(d.leagues) - 1)),
            "b": np.zeros((n, 3)), "log_k": np.full((n, 4), log_k)}  # fmt: skip


def test_history_is_as_of() -> None:
    d = ev.Design(LEAGUES)
    t = d.frame(rate_table())
    block = t.filter(pl.col("kickoff") == T0 + timedelta(weeks=30))
    b = ev.block_arrays(t, block, "goals")
    p0 = b.rows["player_id"][0]
    hist = t.filter((pl.col("player_id") == p0) & (pl.col("kickoff") < T0 + timedelta(weeks=30)))
    assert b.Y[0] == hist["y_goals"].sum() and b.E90[0] == pytest.approx(hist["e"].sum())
    later = t.with_columns(y_goals=pl.when(pl.col("kickoff") >= T0 + timedelta(weeks=30))
                           .then(99).otherwise(pl.col("y_goals")))  # fmt: skip
    b2 = ev.block_arrays(later, block, "goals")
    assert np.array_equal(b.Y, b2.Y)


def test_predictive_is_the_poisson_gamma_posterior() -> None:
    d = ev.Design(LEAGUES)
    t = d.frame(rate_table())
    block = t.filter(pl.col("kickoff") == T0 + timedelta(weeks=30))
    b = ev.block_arrays(t, block, "shots_on")
    post = post_for(d)
    p = ev.predict(post, d, b, "shots_on")
    k = 4.0
    lam = b.e * 0.4
    Lam = b.E90 * 0.4
    want = stats.nbinom(k + b.Y, (k + Lam) / (k + Lam + lam))
    assert np.allclose(p.log_loss, -want.logpmf(b.y))
    assert np.allclose(p.mean, lam * (k + b.Y) / (k + Lam))
    assert np.allclose(p.p0, want.pmf(0))
    # goalkeepers: no player term for goals
    gk = b.kline.copy()
    gk[:] = ev.FIXED_LINE
    bg = ev.Block(**{**b.__dict__, "kline": gk})
    pg = ev.predict(post, d, bg, "goals")
    assert np.allclose(pg.mean, lam)
    # NB within: a heavier zero
    pn = ev.predict(post, d, b, "shots_on", {i: 0.5 for i in range(4)},
                    np.random.default_rng(0))  # fmt: skip
    assert (pn.p0 > p.p0).mean() > 0.9


def test_phi_and_baselines() -> None:
    rng = np.random.default_rng(1)
    mu = np.full(20000, 0.8)
    y = rng.negative_binomial(2.0, 2.0 / (2.0 + mu)).astype(float)
    assert ev.fit_phi(y, mu) == pytest.approx(2.0, rel=0.15)
    assert ev.fit_phi(rng.poisson(mu).astype(float), mu) > 50
    d = ev.Design(LEAGUES)
    t = d.frame(rate_table())
    r0 = ev.r0_table(t.filter(pl.col("season") == 2019), "goals", 2)
    block = t.filter(pl.col("kickoff") == T0 + timedelta(weeks=30))
    b = ev.block_arrays(t, block, "goals")
    base = ev.baselines(b, r0, 10.0)
    want = b.e * (b.Y + 10.0 * r0[b.li, b.gi]) / (b.E90 + 10.0)
    assert np.allclose(base["R1"].mean, want)
    assert np.allclose(base["R0"].mean, b.e * r0[b.li, b.gi])


def scored_world(gain: float = 0.05, n: int = 3000, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    parts = []
    for stat in fr.STATS:
        y = rng.poisson(0.3, n)
        for model, worse in (("model", 0.0), ("R1", gain / 2), ("R0", gain)):
            mean = np.full(n, y.mean())  # E/O exactly 1
            ll = -stats.poisson(mean).logpmf(y) + worse + rng.normal(0, 0.001, n)
            parts.append(pl.DataFrame({
                "model": model, "match_id": np.arange(n), "team_id": 1, "player_id": 1,
                "league_ref": np.where(np.arange(n) % 2, "39", "40"), "season": 2019,
                "line": "F", "stat": stat, "y": y.astype(float), "mean": mean,
                "p0": np.exp(-mean), "log_loss": ll, "sub": False, "new_club": False,
                "ret": False, "hist_e": 20.0, "new_player": False, "positional": False,
            }))  # fmt: skip
    return pl.concat(parts)


def test_dispersion_choice_and_verdict() -> None:
    s = scored_world()
    pois = s.filter(pl.col("model") == "R0").with_columns(model=pl.lit("pois"))
    nb = s.filter(pl.col("model") == "model").with_columns(model=pl.lit("nb"))
    ch = ev.dispersion_choice(pois, nb)
    assert {r["family"] for r in ch} == {"nb"} and ev.phi_lines(ch, "goals") == [3]
    meta = {"checks_a": [{"passed": True}], "drops_applied": True, "leakage_ok": True}
    v = ev.verdict(s, meta, inference_ok=True)
    assert v.passed, [c for c in v.checks if not c["pass"]]
    bad = ev.verdict(scored_world(gain=-0.05), meta, inference_ok=True)
    assert not bad.passed
    assert not ev.verdict(s, {**meta, "leakage_ok": False}, True).passed
    choices = {"dispersion": ch, "c": {x: 10.0 for x in fr.STATS}}
    text = ev.markdown(s, pl.DataFrame(), v, [], meta, choices, "d")
    assert "**Verdict: pass**" in text and "Dispersion" in text
