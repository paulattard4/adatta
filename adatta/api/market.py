"""The bookmakers' view, for evaluation only (owner, 2026-11-27; docs/DASHBOARD_TASKS.md):
the played view's market benchmark and closing-line value, and the market line beside
upcoming forecasts. The one API module that reads odds; only `adatta/api/views.py` imports
it (a test), and nothing here reaches a model (principle 1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.api import schemas as sc
from adatta.db import models

MARKET_SELECTIONS = {  # the odds feed's markets as the views score them
    "1X2": (None, ("home", "draw", "away")),
    "OU": (2.5, ("over", "under")),
    "BTTS": (None, ("yes", "no")),
}
CLV_BOOT, CLV_SEED = 2000, 20261127

Price = tuple[datetime, float]  # observed at, decimal odds
Key = tuple[int, str, str, str]  # match, bookmaker, market, selection


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _prices(session: Session, ids: list[int]) -> list[tuple[int, str, str, str, Price]]:
    """Every usable price of these matches: (match, bookmaker, market, selection, price)."""
    OS = models.OddsSnapshot
    out = []
    for i in range(0, len(ids), 500):
        for mid, bk, mk, ln, sel, odds, obs in session.execute(
            select(OS.match_id, OS.bookmaker, OS.market, OS.line, OS.selection, OS.decimal_odds,
                   OS.observed_at).where(OS.match_id.in_(ids[i : i + 500]))
        ):  # fmt: skip
            spec = MARKET_SELECTIONS.get(mk)
            if spec is None or ln != spec[0] or odds is None or odds <= 1:
                continue
            out.append((mid, bk, mk, sel.lower(), (_utc(obs), float(odds))))
    return out


def _pick(rows: list[tuple[int, str, str, str, Price]], upto: dict[int, datetime],
          since: dict[int, datetime] | None = None) -> dict[Key, Price]:  # fmt: skip
    """Per (match, bookmaker, market, selection): the last price at or before `upto`, or with
    `since` the first at or after `since` (and at or before `upto`)."""
    out: dict[Key, Price] = {}
    for mid, bk, mk, sel, (obs, odds) in rows:
        if obs > _utc(upto[mid]) or (since is not None and obs < _utc(since[mid])):
            continue
        k = (mid, bk, mk, sel)
        if k not in out or (obs < out[k][0] if since is not None else obs > out[k][0]):
            out[k] = (obs, odds)
    return out


def _fair(
    picked: dict[Key, Price], mid: int, bk: str, mk: str
) -> tuple[np.ndarray, datetime] | None:
    """One bookmaker's whole market, margin removed proportionally, and its latest price time."""
    q = [picked.get((mid, bk, mk, x)) for x in MARKET_SELECTIONS[mk][1]]
    if any(v is None for v in q):
        return None
    inv = np.array([1 / v[1] for v in q if v is not None])
    return inv / inv.sum(), max(v[0] for v in q if v is not None)


def _average(picked: dict[Key, Price]) -> dict[int, dict[str, Any]]:
    """Per match and market: the de-margined probabilities averaged over the bookmakers
    quoting every selection, and their count; under "prices", per selection, those same
    bookmakers' actual decimal odds (margin included): their mean and the best."""
    books: dict[tuple[int, str], set[str]] = {}
    for mid, bk, mk, _ in picked:
        books.setdefault((mid, mk), set()).add(bk)
    out: dict[int, dict[str, Any]] = {}
    for (mid, mk), bks in books.items():
        whole = [bk for bk in sorted(bks) if _fair(picked, mid, bk, mk) is not None]
        if not whole:
            continue
        per = [f[0] for bk in whole if (f := _fair(picked, mid, bk, mk)) is not None]
        out.setdefault(mid, {})[mk] = (np.mean(per, axis=0), len(per))
        prices = out[mid].setdefault("prices", {})
        for x in MARKET_SELECTIONS[mk][1]:
            odds = [picked[(mid, bk, mk, x)][1] for bk in whole]
            prices[x] = sc.MarketPrice(mean=float(np.mean(odds)), best=float(max(odds)),
                                       bookmakers=len(odds))  # fmt: skip
    return out


def market_view(session: Session, cutoffs: dict[int, datetime]) -> dict[int, dict[str, Any]]:
    """Per match: the market as of its cutoff (kickoff for a played match, now for an
    upcoming one), from each bookmaker's last price at or before it."""
    if not cutoffs:
        return {}
    return _average(_pick(_prices(session, list(cutoffs)), cutoffs))


def played_market(view: dict[str, Any] | None, hg: int, ag: int) -> sc.PlayedMarket | None:
    from adatta.eval import metrics

    if not view or "1X2" not in view:
        return None
    p, n = view["1X2"]
    k = metrics.outcomes(hg, ag)
    ou, bt = view.get("OU"), view.get("BTTS")
    return sc.PlayedMarket(
        home=float(p[0]), draw=float(p[1]), away=float(p[2]),
        over25=None if ou is None else float(ou[0][0]),
        btts=None if bt is None else float(bt[0][0]), bookmakers=n,
        prices=view.get("prices", {}),
        scores=sc.PlayedScores(
            log_loss_1x2=metrics.log_loss(p, k["1X2"]), rps_1x2=metrics.rps(p, k["1X2"]),
            brier_1x2=metrics.brier(p, k["1X2"]),
            log_loss_ou25=None if ou is None else metrics.log_loss(ou[0], k["OU2.5"]),
            log_loss_btts=None if bt is None else metrics.log_loss(bt[0], k["BTTS"]),
        ),
    )  # fmt: skip


def upcoming_market(view: dict[str, Any] | None) -> sc.MarketLine | None:
    """The market beside an upcoming forecast: never a model input; the page marks the
    selection we rate furthest above it (display only, owner 2026-11-27)."""
    if not view or "1X2" not in view:
        return None
    p, n = view["1X2"]
    ou, bt = view.get("OU"), view.get("BTTS")
    return sc.MarketLine(home=float(p[0]), draw=float(p[1]), away=float(p[2]),
                         over25=None if ou is None else float(ou[0][0]),
                         btts=None if bt is None else float(bt[0][0]),
                         bookmakers=n, prices=view.get("prices", {}))  # fmt: skip


def clv_inputs(session: Session, first: dict[int, tuple[datetime, datetime, np.ndarray]]
               ) -> dict[int, sc.PlayedClv]:  # fmt: skip
    """Closing-line value per played match, from `first` = {match: (our first forecast's
    publication, kickoff, our first 1X2)}. Per bookmaker, the opening market is its first
    whole 1X2 at or after our first forecast and the closing market its last at or before
    kickoff; a bookmaker counts only if its closing price is later than its opening one.
    The two are averaged over those bookmakers. d = ours - opening, m = closing - opening:
    d.m > 0 means the market moved towards our forecast."""
    if not first:
        return {}
    rows = [r for r in _prices(session, list(first)) if r[2] == "1X2"]
    kick = {mid: v[1] for mid, v in first.items()}
    opened = _pick(rows, kick, since={mid: v[0] for mid, v in first.items()})
    closed = _pick(rows, kick)
    books: dict[int, set[str]] = {}
    for mid, bk, _, _ in closed:
        books.setdefault(mid, set()).add(bk)
    out: dict[int, sc.PlayedClv] = {}
    for mid, bks in books.items():
        o_, c_ = [], []
        for bk in sorted(bks):
            fo, fc = _fair(opened, mid, bk, "1X2"), _fair(closed, mid, bk, "1X2")
            if fo is not None and fc is not None and fc[1] > fo[1]:
                o_.append(fo[0])
                c_.append(fc[0])
        if not o_:
            continue
        q_open, q_close = np.mean(o_, axis=0), np.mean(c_, axis=0)
        d, m = first[mid][2] - q_open, q_close - q_open
        out[mid] = sc.PlayedClv(moved=float(d @ m), gap=float(d @ d), bookmakers=len(o_))
    return out


def clv_summary(xs: list[sc.PlayedClv]) -> sc.ClvSummary | None:
    """The market's movement towards our first forecasts over a segment:
    - toward: the share of matches where it moved towards us (of those where it moved);
    - closed: the share of our disagreement it closed by kickoff, sum(d.m) / sum(d.d),
      with a bootstrap 95% interval over matches. 0: no information; 1: it moved all the way."""
    if not xs:
        return None
    moved = np.array([x.moved for x in xs])
    gap = np.array([x.gap for x in xs])
    nz = moved[moved != 0]
    toward = float((nz > 0).mean()) if len(nz) else None
    half = 1.96 * np.sqrt(toward * (1 - toward) / len(nz)) if toward is not None else None
    closed = float(moved.sum() / gap.sum()) if gap.sum() > 0 else None
    lo = hi = None
    if closed is not None and len(xs) > 1:
        rng = np.random.default_rng(CLV_SEED)
        idx = rng.integers(0, len(xs), size=(CLV_BOOT, len(xs)))
        g = gap[idx].sum(axis=1)
        boot = moved[idx].sum(axis=1)[g > 0] / g[g > 0]
        lo, hi = float(np.quantile(boot, 0.025)), float(np.quantile(boot, 0.975))
    return sc.ClvSummary(
        n=len(xs), n_moved=len(nz), toward=toward,
        toward_lo=None if half is None or toward is None else max(0.0, toward - half),
        toward_hi=None if half is None or toward is None else min(1.0, toward + half),
        closed=closed, closed_lo=lo, closed_hi=hi,
    )  # fmt: skip
