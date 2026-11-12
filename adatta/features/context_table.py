"""The context table as a stored snapshot (Phase 2 F3): context and style for every bundle
league match at one horizon, written once per data snapshot and read by the backtest.

Rows: every bundle league match from FROM_SEASON (the dev training windows reach back to
2016/17), not cancelled, postponed or walked over, at as_of = kickoff - horizon: the context
table (adatta/features/context.py) joined with team style (adatta/features/style.py). The
as_of column is `ctx_as_of`, the name the covariate check reads.

Storage: the private raw store (R2), never git or public artifacts (match-level data). The
key holds a digest of the table's content, so the same data gives the same name and a
backtest names the exact snapshot it used; `latest.json` per horizon points at the newest.
The digest enters the backtest config hash (RunSpec.context), so draws fitted on one
snapshot are never reused for another.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.features import style
from adatta.features.context import NOT_PLAYED, context_features
from adatta.features.league_rules import Rules

FROM_SEASON = 2016
PREFIX = "features/context"


def _hours(horizon: timedelta) -> str:
    return f"h{horizon.total_seconds() / 3600:g}"


def key(horizon: timedelta, digest: str) -> str:
    return f"{PREFIX}/{_hours(horizon)}/{digest}.parquet"


def latest_key(horizon: timedelta) -> str:
    return f"{PREFIX}/{_hours(horizon)}/latest.json"


def digest(df: pl.DataFrame) -> str:
    """Content digest: the rows sorted by match_id, written as CSV (stable across writers)."""
    return hashlib.sha256(df.sort("match_id").write_csv().encode()).hexdigest()[:16]


def matches(session: Session, competition_ids: Sequence[int],
            from_season: int = FROM_SEASON) -> list[models.Match]:  # fmt: skip
    M, S = models.Match, models.Season
    return list(
        session.scalars(
            select(M)
            .join(S, S.season_id == M.season_id)
            .where(S.competition_id.in_(competition_ids), S.year >= from_season,
                   M.status.not_in(NOT_PLAYED))
            .order_by(M.match_id)
        )
    )  # fmt: skip


def build(session: Session, store: RawStore, ms: Sequence[models.Match], rules: Rules,
          horizon: timedelta) -> pl.DataFrame:  # fmt: skip
    """Context and style of `ms` at kickoff - horizon, one row per match."""
    ctx = context_features(session, store, ms, rules, horizon).rename({"as_of": "ctx_as_of"})
    sty = style.style_features(style.StyleHistory.load(session), ms, horizon).drop("as_of")
    return ctx.join(sty, on="match_id", how="left").sort("match_id")


def save(store: RawStore, df: pl.DataFrame, horizon: timedelta, code_sha: str) -> str:
    """Write the snapshot and point `latest` at it; returns its digest."""
    d = digest(df)
    buf = io.BytesIO()
    df.sort("match_id").write_parquet(buf)
    store.put(key(horizon, d), buf.getvalue())
    pointer = {"digest": d, "rows": df.height, "built_at": datetime.now(UTC).isoformat(),
               "code_sha": code_sha, "horizon_hours": horizon.total_seconds() / 3600}  # fmt: skip
    store.put(latest_key(horizon), json.dumps(pointer).encode())
    return d


def resolve(store: RawStore, horizon: timedelta, ref: str = "latest") -> str:
    """A digest, from `latest` or as given."""
    if ref != "latest":
        return ref
    data = try_get(store, latest_key(horizon))
    if data is None:
        raise FileNotFoundError(f"no context snapshot at {_hours(horizon)}; run context-build")
    return str(json.loads(data)["digest"])


def load(store: RawStore, horizon: timedelta, ref: str = "latest") -> tuple[pl.DataFrame, str]:
    """The snapshot and its digest; refuses a file whose content does not match its name."""
    d = resolve(store, horizon, ref)
    data = try_get(store, key(horizon, d))
    if data is None:
        raise FileNotFoundError(f"context snapshot {d} at {_hours(horizon)} not found")
    df = pl.read_parquet(io.BytesIO(data))
    if digest(df) != d:
        raise ValueError(f"context snapshot {d} does not match its content")
    return df, d


def attach(df: pl.DataFrame, ctx: pl.DataFrame) -> pl.DataFrame:
    """The backtest frame with every context column (left join on match_id; a match without
    a row gets null ctx_as_of, which `covariates.check_as_of` refuses)."""
    cols = [c for c in ctx.columns if c == "match_id" or c not in df.columns]
    return df.join(ctx.select(cols), on="match_id", how="left")
