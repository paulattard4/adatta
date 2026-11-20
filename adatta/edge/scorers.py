"""P3-e (f), reported only: the scorer forecasts against the anytime-goalscorer market
(docs/PHASE3_TASKS.md, P3-e "Live verdict (f)" (d)). Downstream of the model: odds are read
here from the kept raw snapshots, never by model, eval, sim or feature code (principle 1),
and never part of the pass rule.

CLI (CI, after `scorers_live table` and `scorers_live assemble`):
  market --table DIGEST   the median implied probability per matched player, to R2
  report --table DIGEST   M against the market on goals at lineups-known; metrics only
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
from collections.abc import Sequence
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy import stats
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_capture as afc
from adatta.adapters.storage import RawStore, make_store, try_get
from adatta.config import get_settings
from adatta.db import models
from adatta.eval import scorers as es
from adatta.features import scorers as fs
from adatta.model import scorers_live as sl

Arr = NDArray[np.float64]
MARKET_KINDS = ("T-60m", "close")  # the odds milestones around lineups-known


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def market(session: Session, store: RawStore, names: pl.DataFrame, match_ids: list[int],
           ) -> tuple[pl.DataFrame, dict[str, Any]]:  # fmt: skip
    """Anytime-scorer prices (raw snapshots at MARKET_KINDS) per player: the median implied
    probability 1/odds across books, no margin removed. Names matched within each match."""
    OC = models.OddsCapture
    caps = session.execute(
        select(OC.match_id, OC.snapshot_kind, OC.storage_key).where(
            OC.match_id.in_(match_ids), OC.snapshot_kind.in_(MARKET_KINDS),
            OC.storage_key.is_not(None))
    ).all()  # fmt: skip
    by_match = {mid: [(pid, list(g["name"].drop_nulls()))
                      for (pid,), g in grp.group_by(["player_id"])]
                for (mid,), grp in names.group_by(["match_id"])}  # fmt: skip
    rows, priced, matched = [], 0, 0
    for mid, kind, key in caps:
        raw = try_get(store, key) if key else None
        if raw is None:
            continue
        payload = json.loads(gzip.decompress(raw))
        prices: dict[str, list[float]] = {}
        for p in afc.parse_scorer_odds(payload):
            prices.setdefault(p.selection, []).append(1.0 / p.decimal_odds)
        if not prices:
            continue
        hit = fs.match_names(list(prices), by_match.get(mid, []))
        priced += len(prices)
        matched += len(hit)
        for name, pid in hit.items():
            rows.append(
                {
                    "match_id": mid,
                    "player_id": pid,
                    "kind": kind,
                    "p_mkt": float(np.median(prices[name])),
                    "books": len(prices[name]),
                }
            )
    df = pl.DataFrame(rows, schema={"match_id": pl.Int64, "player_id": pl.Int64, "kind": pl.Utf8,
                                    "p_mkt": pl.Float64, "books": pl.Int64})  # fmt: skip
    # a player matched under two odds names: keep the one with more books
    df = df.sort("books", descending=True).unique(["match_id", "player_id", "kind"], keep="first")
    return df, {"odds_names": priced, "matched": matched,
                "matches_priced": df["match_id"].n_unique()}  # fmt: skip


BOOT = 1000  # bootstrap resamples of matches for the AUC difference
BOOT_SEED = 20261120


def auc(p: Arr, hit: NDArray[np.bool_]) -> float:
    """Area under the ROC curve: the chance a scorer is ranked above a non-scorer, ties
    counting half (Mann-Whitney). A margin that keeps the market's order leaves it unchanged."""
    pos, neg = p[hit], p[~hit]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    r = stats.rankdata(np.concatenate([pos, neg]))
    return float((r[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def auc_diff(p_m: Arr, p_q: Arr, hit: NDArray[np.bool_], match: NDArray[np.int64],
             n: int = BOOT, seed: int = BOOT_SEED) -> tuple[float, float, float]:  # fmt: skip
    """AUC(M) - AUC(market) with a 95% interval from resampling whole matches (players of a
    match share its goals)."""
    rng = np.random.default_rng(seed)
    ids = np.unique(match)
    rows = {m: np.flatnonzero(match == m) for m in ids}
    d0 = auc(p_m, hit) - auc(p_q, hit)
    ds = []
    for _ in range(n):
        idx = np.concatenate([rows[m] for m in rng.choice(ids, len(ids))])
        x = auc(p_m[idx], hit[idx]) - auc(p_q[idx], hit[idx])
        if np.isfinite(x):
            ds.append(x)
    if not ds:
        return d0, float("nan"), float("nan")
    lo, hi = np.quantile(ds, [0.025, 0.975])
    return d0, float(lo), float(hi)


def market_report(scored: pl.DataFrame, mkt: pl.DataFrame) -> list[str]:
    """Reported only (principle 1): M's P(scores) against the market's implied probability,
    goals at lineups-known, on the players the market priced."""
    m = scored.filter((pl.col("model") == "M") & (pl.col("stat") == "goals")
                      & (pl.col("horizon") == "lk"))  # fmt: skip
    out = [
        "",
        "## Market (anytime goalscorer, reported only; odds are never an input)",
        "",
        "| snapshot | rows | mean P(M) | mean P(market) | observed | log loss M | "
        "log loss market | M - market | 95% interval | AUC M | AUC market | M - market "
        "| 95% interval |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for kind in MARKET_KINDS:
        j = m.join(mkt.filter(pl.col("kind") == kind), on=["match_id", "player_id"])
        if j.is_empty():
            out.append(f"| {kind} | 0 |" + " - |" * 11)
            continue
        q = j["p_mkt"].clip(es.P_FLOOR, 1 - es.P_FLOOR).to_numpy()
        hit = (j["y"] >= 1).to_numpy()
        ll_m = j["log_loss"].to_numpy()
        ll_q = -np.where(hit, np.log(q), np.log(1 - q))
        d = ll_m - ll_q
        half = 1.96 * d.std(ddof=1) / np.sqrt(len(d)) if len(d) > 1 else float("nan")
        pm = j["p1"].to_numpy()
        da, alo, ahi = auc_diff(pm, q, hit, j["match_id"].to_numpy())
        out.append(
            f"| {kind} | {len(d)} | {pm.mean():.4f} | {q.mean():.4f} | "
            f"{hit.mean():.4f} | {ll_m.mean():.4f} | {ll_q.mean():.4f} | "
            f"{d.mean():+.4f} | [{d.mean() - half:+.4f}, {d.mean() + half:+.4f}] | "
            f"{auc(pm, hit):.3f} | {auc(q, hit):.3f} | {da:+.3f} | [{alo:+.3f}, {ahi:+.3f}] |"
        )
    out += ["", "Log loss compares levels, so the bookmakers' margin (no margin removed) "
            "weighs on the market's. AUC compares only the order of players by chance to "
            "score, which a margin barely moves. 95% intervals: log loss normal, AUC from "
            f"{BOOT} resamples of matches."]  # fmt: skip
    return out


def main(argv: Sequence[str] | None = None) -> None:
    from adatta.db.session import get_session

    p = argparse.ArgumentParser(prog="python -m adatta.edge.scorers")
    p.add_argument("cmd", choices=["market", "report"])
    p.add_argument("--table", required=True)
    args = p.parse_args(argv)
    store = make_store(get_settings())
    table, _, digest = fs.load(store, args.table)
    if args.cmd == "market":
        ids = table["match_id"].unique().to_list()
        with get_session() as session:
            _, names, _ = sl.snapshot_squads(session, ids)
            mkt, stats = market(session, store, names, ids)
        store.put(f"{sl.PREFIX}/{digest}/market.parquet", _parquet(mkt))
        print(f"market: {stats['matches_priced']} matches priced, {stats['matched']} of "
              f"{stats['odds_names']} odds names matched to players")  # fmt: skip
        return
    scored = pl.read_parquet(io.BytesIO(store.get(f"{sl.PREFIX}/reports/{digest}/scored.parquet")))
    mkt = pl.read_parquet(io.BytesIO(store.get(f"{sl.PREFIX}/{digest}/market.parquet")))
    print("\n".join(market_report(scored, mkt)))


if __name__ == "__main__":
    main()
