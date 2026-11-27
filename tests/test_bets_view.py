"""The bets page (docs/DASHBOARD_TASKS.md, "Bets above the market"): every selection we rated
above the market before kickoff, 1 unit at the bookmakers' odds as quoted, settled at 90'."""

from datetime import timedelta
from pathlib import Path

import pytest
from test_dashboard_api import ctx
from test_played_view import _odds, played_world
from test_serve import K

from adatta.api import schemas as sc
from adatta.api import views


def _prob(p: float) -> sc.Prob:
    return sc.Prob(p=p, lo=max(p - 0.1, 0.0), hi=min(p + 0.1, 1.0))


def _match(hg: int, ag: int, market: sc.PlayedMarket | None) -> sc.PlayedMatch:
    outcome = "home" if hg > ag else "draw" if hg == ag else "away"
    scores = sc.PlayedScores(log_loss_1x2=1.0, rps_1x2=0.2, brier_1x2=0.6, log_loss_ou25=None,
                             log_loss_btts=None)  # fmt: skip
    return sc.PlayedMatch(
        match_id=7, kickoff=K, competition_id=1, competition="L39", competition_tier="1",
        team_level="senior", home="A", away="B", home_goals=hg, away_goals=ag, outcome=outcome,
        milestone="close", generated_at=K - timedelta(hours=1), home_p=_prob(0.5),
        draw_p=_prob(0.3), away_p=_prob(0.2), over25=_prob(0.6), btts=_prob(0.4), scores=scores,
        market=market,
    )  # fmt: skip


def _market(prices: dict[str, tuple[float, float]]) -> sc.PlayedMarket:
    scores = sc.PlayedScores(log_loss_1x2=1.0, rps_1x2=0.2, brier_1x2=0.6, log_loss_ou25=None,
                             log_loss_btts=None)  # fmt: skip
    return sc.PlayedMarket(
        home=0.4, draw=0.25, away=0.35, over25=0.55, btts=0.5, bookmakers=3, scores=scores,
        prices={k: sc.MarketPrice(mean=a, best=b, bookmakers=3) for k, (a, b) in prices.items()},
    )  # fmt: skip


ALL = {"home": (2.4, 2.5), "draw": (3.8, 4.0), "away": (2.7, 2.8), "over": (1.75, 1.8),
       "under": (2.05, 2.1), "yes": (1.9, 2.0), "no": (1.85, 1.9)}  # fmt: skip


def test_every_selection_above_the_market_is_one_bet_settled_at_90_minutes() -> None:
    # ours 0.50 / 0.30 / 0.20 against 0.40 / 0.25 / 0.35: home and draw are above it (two
    # in a three-way market); over 0.60 against 0.55; BTTS no 0.60 against 0.50.
    bets = views.bet_rows(_match(2, 1, _market(ALL)), "England")
    got = {(b.market, b.selection): b for b in bets}
    assert set(got) == {("1X2", "home"), ("1X2", "draw"), ("OU2.5", "over"), ("BTTS", "no")}
    home = got[("1X2", "home")]
    assert home.won and home.gap == pytest.approx(0.1) and home.market_p == 0.4
    assert (home.odds_mean, home.odds_best, home.bookmakers) == (2.4, 2.5, 3)
    assert home.country == "England" and not home.point
    assert not got[("1X2", "draw")].won
    assert got[("OU2.5", "over")].won  # three goals
    assert not got[("BTTS", "no")].won  # both scored
    assert got[("BTTS", "no")].ours == pytest.approx(0.6)
    # 0-0: under, draw and BTTS no come in
    zero = {(b.market, b.selection): b.won for b in views.bet_rows(_match(0, 0, _market(ALL)), "")}
    assert zero == {("1X2", "home"): False, ("1X2", "draw"): True, ("OU2.5", "over"): False,
                    ("BTTS", "no"): True}  # fmt: skip


def test_no_market_or_no_quoted_price_is_no_bet() -> None:
    assert views.bet_rows(_match(1, 0, None), "") == []
    partial = {k: v for k, v in ALL.items() if k not in ("draw", "over")}
    got = {(b.market, b.selection) for b in views.bet_rows(_match(1, 0, _market(partial)), "")}
    assert got == {("1X2", "home"), ("BTTS", "no")}


def test_bets_endpoint_reads_the_last_quoted_prices_before_kickoff(tmp_path: Path) -> None:
    w, mid = played_world(tmp_path, result=(2, 1))
    # two bookmakers price the home side long, so the market is below ours on it
    _odds(w, mid, "b1", "1X2", {"home": 6.0, "draw": 4.0, "away": 1.6}, timedelta(hours=-3))
    _odds(w, mid, "b2", "1X2", {"home": 7.0, "draw": 4.2, "away": 1.5}, timedelta(hours=-1))
    _odds(w, mid, "b2", "1X2", {"home": 1.1, "draw": 9.0, "away": 20.0}, timedelta(minutes=5))
    out = views.bets(w.s, ctx(w), K + timedelta(days=1))
    rows = [b for b in out.bets if b.match_id == mid]
    home = next(b for b in rows if b.selection == "home")
    assert home.market == "1X2" and home.won  # 2-1
    assert home.odds_mean == pytest.approx(6.5) and (home.odds_best, home.bookmakers) == (7.0, 2)
    assert all(b.gap > 0 and b.ours > b.market_p for b in rows)
    assert home.country == "X"  # the competition's country
