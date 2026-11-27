"""The bookmakers' view of played matches, for the played view's market benchmark (owner,
2026-11-27; docs/DASHBOARD_TASKS.md). Evaluation only: the one API module that reads odds,
imported by `views.played` alone (a test), and nothing here reaches a model (principle 1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.api import schemas as sc
from adatta.db import models


def _utc(t: datetime) -> datetime:
    from datetime import UTC

    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


MARKET_SELECTIONS = {  # the odds feed's markets as the played view scores them
    "1X2": (None, ("home", "draw", "away")),
    "OU": (2.5, ("over", "under")),
    "BTTS": (None, ("yes", "no")),
}


def market_view(session: Session, kickoffs: dict[int, datetime]) -> dict[int, dict[str, Any]]:
    """Per match and market, the de-margined probabilities averaged over the bookmakers
    whose last price at or before kickoff covers every selection, and their count.
    Evaluation only: nothing here reaches a model (principle 1)."""
    if not kickoffs:
        return {}
    OS, M = models.OddsSnapshot, models.Match
    rows = session.execute(
        select(OS.match_id, OS.bookmaker, OS.market, OS.line, OS.selection, OS.decimal_odds,
               OS.observed_at)
        .join(M, M.match_id == OS.match_id)
        .where(OS.match_id.in_(list(kickoffs)), OS.observed_at <= M.kickoff_time)
    ).all()  # fmt: skip
    last: dict[tuple[int, str, str, str], tuple[datetime, float]] = {}
    for mid, bk, mk, ln, sel, odds, obs in rows:
        spec = MARKET_SELECTIONS.get(mk)
        if spec is None or ln != spec[0] or odds is None or odds <= 1:
            continue
        if _utc(obs) > _utc(kickoffs[mid]):
            continue
        k = (mid, bk, mk, sel.lower())
        if k not in last or _utc(obs) > last[k][0]:
            last[k] = (_utc(obs), float(odds))
    books: dict[tuple[int, str], set[str]] = {}
    for mid, bk, mk, _ in last:
        books.setdefault((mid, mk), set()).add(bk)
    out: dict[int, dict[str, Any]] = {}
    for (mid, mk), bks in books.items():
        sels = MARKET_SELECTIONS[mk][1]
        per = []
        for bk in sorted(bks):
            q = [last.get((mid, bk, mk, x)) for x in sels]
            if any(v is None for v in q):
                continue
            inv = np.array([1 / v[1] for v in q if v is not None])
            per.append(inv / inv.sum())
        if per:
            out.setdefault(mid, {})[mk] = (np.mean(per, axis=0), len(per))
    return out


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
        scores=sc.PlayedScores(
            log_loss_1x2=metrics.log_loss(p, k["1X2"]), rps_1x2=metrics.rps(p, k["1X2"]),
            brier_1x2=metrics.brier(p, k["1X2"]),
            log_loss_ou25=None if ou is None else metrics.log_loss(ou[0], k["OU2.5"]),
            log_loss_btts=None if bt is None else metrics.log_loss(bt[0], k["BTTS"]),
        ),
    )  # fmt: skip
