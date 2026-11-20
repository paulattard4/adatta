"""Projected lineups: P3-b served for upcoming fixtures (docs/DASHBOARD_TASKS.md, "Projected
lineups"). P3-b as it passed on dev, refitted per bundle every 28 days on CI
(`model.selection serve-refit`), its draws stored here; a daily run projects every
regular-season bundle fixture of the next 7 days into `lineup_projection` (append-only).

P3-b uses neither odds nor the injury feed; the dashboard shows the feed beside the
projection, labelled as not used by the model. Numpy only (the fit is the model group's).
"""

from __future__ import annotations

import io
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import SOURCE_ID
from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.features import selection as sel

Arr = NDArray[np.float64]
PREFIX = "serving/lineups"
AHEAD = timedelta(days=7)  # fixtures projected this far ahead
REFIT_DAYS = 28  # D14, as P3-b on dev
NO_MATCH = {"PST", "CANC", "ABD", "AWD", "WO", "TBD"}


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ----------------------------------------------------------------------------- refits


def refit_key(bundle: str, as_of: datetime) -> str:
    return f"{PREFIX}/{bundle}/{as_of:%Y%m%dT%H%M}.npz"


def latest_key(bundle: str) -> str:
    return f"{PREFIX}/{bundle}/latest.json"


def encode(post: dict[str, Arr], design: Any, nu: dict[str, tuple[float, float]],
           meta: dict[str, Any]) -> bytes:  # fmt: skip
    """A serving refit: the thinned posterior draws, the design (means, sds, leagues), the
    newcomer allowances and the refit's metadata."""
    buf = io.BytesIO()
    extra = json.dumps({"design": asdict(design), "nu": nu, "meta": meta}, default=str)
    np.savez_compressed(buf, **{f"post__{k}": v for k, v in post.items()},  # type: ignore[arg-type]
                        extra=np.array(extra))  # fmt: skip
    return buf.getvalue()


def decode(data: bytes) -> tuple[dict[str, Arr], Any, dict[str, tuple[float, float]],
                                 dict[str, Any]]:  # fmt: skip
    from adatta.model.selection import Design

    with np.load(io.BytesIO(data)) as z:
        post = {k[6:]: z[k] for k in z.files if k.startswith("post__")}
        extra = json.loads(str(z["extra"]))
    nu = {k: (float(v[0]), float(v[1])) for k, v in extra["nu"].items()}
    return post, Design(**extra["design"]), nu, extra["meta"]


def latest(store: RawStore, bundle: str) -> tuple[str, datetime] | None:
    """The latest passing serving refit of a bundle: (key, as_of)."""
    raw = try_get(store, latest_key(bundle))
    if raw is None:
        return None
    x = json.loads(raw)
    return x["key"], _utc(datetime.fromisoformat(x["as_of"]))


def due(store: RawStore, bundle: str, now: datetime) -> bool:
    got = latest(store, bundle)
    return got is None or now - got[1] >= timedelta(days=REFIT_DAYS)


# ------------------------------------------------------------------------- projection


def upcoming(session: Session, leagues: list[int], now: datetime) -> pl.DataFrame:
    """Regular-season fixtures of these leagues kicking off in (now, now + AHEAD], with the
    projection's as-of time: kickoff - 24 h (P3-b's horizon) or now, whichever is earlier."""
    M, S, C = models.Match, models.Season, models.Competition
    rows = session.execute(
        select(M.match_id, C.source_ref, S.year, M.kickoff_time, M.home_team_id,
               M.away_team_id, M.status, M.round)
        .join(S, S.season_id == M.season_id).join(C, C.competition_id == S.competition_id)
        .where(C.source_id == SOURCE_ID, C.source_ref.in_([str(x) for x in leagues]),
               M.kickoff_time > now, M.kickoff_time <= now + AHEAD)
    ).all()  # fmt: skip
    out = []
    for mid, lg, yr, k, h, a, st, rnd in rows:
        if st in NO_MATCH or not (rnd or "").startswith("Regular Season"):
            continue
        k = _utc(k)
        out.append({"match_id": mid, "league_ref": str(lg), "season": int(yr), "kickoff": k,
                    "home_team_id": h, "away_team_id": a,
                    "as_of": min(now, k - sel.HORIZON)})  # fmt: skip
    return pl.DataFrame(out, schema={
        "match_id": pl.Int64, "league_ref": pl.Utf8, "season": pl.Int32,
        "kickoff": pl.Datetime("us", "UTC"), "home_team_id": pl.Int64,
        "away_team_id": pl.Int64, "as_of": pl.Datetime("us", "UTC"),
    })  # fmt: skip


def project(post: dict[str, Arr], design: Any, nu: dict[str, tuple[float, float]],
            rows: pl.DataFrame, teams: pl.DataFrame) -> pl.DataFrame:  # fmt: skip
    """P3-b's served numbers for pool rows: the dev path's coherent probabilities
    (`model.selection.predict`) and role minutes (`role_minutes`), and expected minutes
    from the minutes pmf."""
    from adatta.eval import selection as ev
    from adatta.model import selection as ms

    p, pmf = ms.predict(design, post, rows, teams, nu)
    e_start, e_bench = ms.role_minutes(design, post, rows)
    return rows.select("match_id", "team_id", "player_id", "pos").with_columns(
        p_start=pl.Series(p[:, 0]), p_bench=pl.Series(p[:, 1]), p_out=pl.Series(p[:, 2]),
        emin_start=pl.Series(e_start), emin_bench=pl.Series(e_bench),
        emin=pl.Series(pmf @ ev.MINUTES),
    )  # fmt: skip


def run(session: Session, store: RawStore, bundles: dict[str, list[int]],
        now: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    """Project the next AHEAD of every bundle with a passing serving refit into
    `lineup_projection`; returns counts (no match-level data)."""
    from adatta.eval import selection as ev

    now = now or datetime.now(UTC)
    report: dict[str, Any] = {"fixtures": 0, "rows": 0, "no_refit": [], "context_missing": 0}
    leagues = [lg for ls in bundles.values() for lg in ls]
    tg = upcoming(session, leagues, now)
    if tg.is_empty():
        return report
    season = max(int(x) for x in tg["season"].to_list())
    inp = ev.load_inputs(session, store, [str(x) for x in leagues], season, live=True)
    session.rollback()  # end the read transaction before the build (Neon's idle timeout)
    players, teams = sel.build(sel.Inputs(inp.appearances, inp.coaches, inp.context,
                                          tg.drop("kickoff")))  # fmt: skip
    if players.is_empty():
        return report
    rows_out = []
    for b, ls in bundles.items():
        got = latest(store, b)
        mine = [str(x) for x in ls]
        block = players.filter(pl.col("league_ref").is_in(mine))
        if block.is_empty():
            continue
        if got is None:
            report["no_refit"].append(b)
            continue
        key, as_of = got
        post, design, nu, _ = decode(store.get(key))
        tm = teams.join(block.select("match_id", "team_id").unique(), on=["match_id", "team_id"])
        proj = project(post, design, nu, block, tm)
        rows_out.append(proj.with_columns(refit_key=pl.lit(key), refit_as_of=pl.lit(as_of)))
        report["context_missing"] += int(block["rest_days"].is_nan().sum())
    if not rows_out:
        return report
    out = pl.concat(rows_out).join(
        tg.select(pl.col("match_id"), "as_of"), on="match_id", how="left"
    )  # fmt: skip
    session.add_all([
        models.LineupProjection(
            match_id=r["match_id"], team_id=r["team_id"], player_id=r["player_id"],
            generated_at=now, as_of=r["as_of"], refit_key=r["refit_key"],
            refit_as_of=r["refit_as_of"], pos=r["pos"], p_start=r["p_start"],
            p_bench=r["p_bench"], p_out=r["p_out"], emin_start=r["emin_start"],
            emin_bench=r["emin_bench"], emin=r["emin"],
        ) for r in out.iter_rows(named=True)
    ])  # fmt: skip
    session.flush()
    report["fixtures"] = int(out["match_id"].n_unique())
    report["rows"] = out.height
    return report
