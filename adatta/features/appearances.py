"""Player appearances per team-match from the stored detail payloads (Phase 3, P3-0;
`docs/PHASE3_TASKS.md`). No API requests.

One row per player in a team's lineup (starters and bench) of a finished match, parsed by
`api_football_stats.player_appearances`. Stored as one Parquet file per competition-season
in the object store (`players/appearances/v1/<league>/<season>.parquet`), not in Postgres:
about 3.7M rows would not fit the database, and the model reads them in bulk. Players are
registered in the `player` table (one row per provider id, across clubs). Every file can be
rebuilt from the raw payloads (principle 4); a rebuild replaces it.

As-of rule: appearances are post-match facts, usable from kickoff + RESULT_LAG, like the
result (`AppearanceHistory.as_of`).
"""

from __future__ import annotations

import gzip
import io
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_stats as st
from adatta.adapters import normalize
from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.features.referees import RESULT_LAG

PREFIX = "players/appearances/v1"
FINISHED = ("FT", "AET", "PEN")

INT_COLS = ["number", "minute_on", "minute_off", "minutes", "minutes_nt", "minutes_provider",
            "goals", "own_goals", *st.PLAYER_STATS]  # fmt: skip
SCHEMA: dict[str, Any] = {
    "match_id": pl.Int64, "team_id": pl.Int64, "player_id": pl.Int64, "player_ref": pl.Utf8,
    "league_ref": pl.Utf8, "season": pl.Int32, "kickoff": pl.Datetime("us", "UTC"),
    "started": pl.Boolean, "pos": pl.Utf8, "grid": pl.Utf8, "sent_off": pl.Boolean,
    "captain": pl.Boolean, "rating": pl.Float64,
    **{c: pl.Int32 for c in INT_COLS},
    "source_id": pl.Utf8, "storage_key": pl.Utf8, "observed_at": pl.Datetime("us", "UTC"),
    "ingested_at": pl.Datetime("us", "UTC"), "valid_from": pl.Datetime("us", "UTC"),
    "valid_to": pl.Datetime("us", "UTC"), "superseded_at": pl.Datetime("us", "UTC"),
}  # fmt: skip


def key(league_ref: str, season: int) -> str:
    return f"{PREFIX}/{league_ref}/{season}.parquet"


@dataclass(frozen=True)
class Target:
    storage_key: str
    match_id: int
    fixture_ref: str
    observed_at: datetime
    kickoff: datetime
    home_team_id: int
    away_team_id: int


def targets(session: Session, league_ref: str | None = None,
            season: int | None = None) -> dict[tuple[str, int], list[Target]]:  # fmt: skip
    """(league ref, season) -> finished matches with a returned detail payload."""
    D, M, S, C = models.MatchDetail, models.Match, models.Season, models.Competition
    q = (
        select(D.storage_key, D.match_id, M.source_ref, D.observed_at, M.kickoff_time,
               M.home_team_id, M.away_team_id, C.source_ref, S.year)
        .join(M, M.match_id == D.match_id).join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .where(D.flags["returned"].as_boolean(), M.status.in_(FINISHED))
    )  # fmt: skip
    if league_ref is not None:
        q = q.where(C.source_ref == league_ref)
    if season is not None:
        q = q.where(S.year == season)
    out: dict[tuple[str, int], list[Target]] = defaultdict(list)
    for k, mid, ref, seen, ko, h, a, lg, yr in session.execute(q):
        out[(lg, yr)].append(Target(k, mid, ref, _utc(seen), _utc(ko), h, a))
    return dict(out)


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def rows_for(fx: dict[str, Any], t: Target, players: dict[str, models.Player],
             league_ref: str, season: int, now: datetime,
             ) -> tuple[list[dict[str, Any]], Counter[str]]:  # fmt: skip
    """Appearance rows of one match with database ids, and its parse counts. Teams are
    the match's own home and away ids (so a split team keeps its split id), matched by
    the payload's home and away refs."""
    parsed, counts = st.player_appearances(fx)
    c: Counter[str] = Counter(counts)
    side = {str(fx["teams"]["home"]["id"]): t.home_team_id,
            str(fx["teams"]["away"]["id"]): t.away_team_id}  # fmt: skip
    out = []
    for r in parsed:
        team_id = side.get(r["team_ref"])
        if team_id is None:
            c["unknown_team"] += 1
            continue
        row = {k: r[k] for k in SCHEMA if k in r}
        row.update(
            match_id=t.match_id, team_id=team_id, player_id=players[r["player_ref"]].player_id,
            player_ref=r["player_ref"], league_ref=league_ref, season=season,
            kickoff=t.kickoff, source_id=normalize.SOURCE_ID, storage_key=t.storage_key,
            observed_at=t.observed_at, ingested_at=now, valid_from=t.observed_at,
            valid_to=None, superseded_at=None,
        )  # fmt: skip
        out.append(row)
    return out, c


def build(session: Session, store: RawStore, league_ref: str | None = None,
          season: int | None = None, rebuild: bool = False, workers: int = 8,
          now: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    """Write one Parquet file per competition-season; skip files already there unless
    `rebuild`. Registers new players and commits once per file."""
    now = now or datetime.now(UTC)
    totals: Counter[str] = Counter()
    groups = targets(session, league_ref, season)

    def fetch(k: str) -> tuple[str, dict[str, Any]]:
        return k, json.loads(gzip.decompress(store.get(k)))

    with ThreadPoolExecutor(workers) as pool:
        for (lg, yr), ts in sorted(groups.items()):
            if not rebuild and try_get(store, key(lg, yr)) is not None:
                totals["files_skipped"] += 1
                continue
            payloads = dict(pool.map(fetch, sorted({t.storage_key for t in ts})))
            fixtures = {
                (k, str(fx["fixture"]["id"])): fx
                for k, p in payloads.items() for fx in p.get("response", [])
            }  # fmt: skip
            found = [(t, fixtures.get((t.storage_key, t.fixture_ref))) for t in ts]
            names = {
                r["player_ref"]: r["player_name"]
                for _, fx in found if fx for r in st.player_appearances(fx)[0]
            }  # fmt: skip
            players = normalize.players_by_ref(session, names)
            rows: list[dict[str, Any]] = []
            for t, fx in found:
                if fx is None:
                    totals["no_fixture"] += 1
                    continue
                got, c = rows_for(fx, t, players, lg, yr, now)
                totals.update(c)
                totals["matches" if got else "no_lineup"] += 1
                rows.extend(got)
            frame = pl.DataFrame(rows, schema=SCHEMA) if rows else pl.DataFrame(schema=SCHEMA)
            buf = io.BytesIO()
            frame.write_parquet(buf, compression="zstd")
            store.put(key(lg, yr), buf.getvalue())
            session.commit()
            totals["files"] += 1
            totals["rows"] += frame.height
    return dict(totals)


def load(store: RawStore, groups: list[tuple[str, int]]) -> pl.DataFrame:
    """The stored appearances of these competition-seasons (missing files are skipped)."""
    frames = []
    for lg, yr in groups:
        data = try_get(store, key(lg, yr))
        if data is not None:
            frames.append(pl.read_parquet(io.BytesIO(data)))
    return pl.concat(frames) if frames else pl.DataFrame(schema=SCHEMA)


@dataclass(frozen=True)
class AppearanceHistory:
    frame: pl.DataFrame

    def as_of(self, as_of: datetime) -> pl.DataFrame:
        """Appearances of matches final by as_of (kickoff <= as_of - RESULT_LAG)."""
        return self.frame.filter(pl.col("kickoff") <= _utc(as_of) - RESULT_LAG)


def report(frame: pl.DataFrame, tolerance: int = 3) -> pl.DataFrame:
    """Per competition-season: matches, team-matches with 11 starters, rows, the share of
    players who played whose event minutes are within `tolerance` of the provider's,
    goal agreement (events vs the provider's per-player total), and the share of team
    matches where key passes, tackles and rating are collected."""
    played = pl.col("minutes").gt(0) | pl.col("minutes_provider").fill_null(0).gt(0)
    tm = frame.group_by("league_ref", "season", "match_id", "team_id").agg(
        pl.col("started").sum().alias("starters"),
        pl.col("key_passes").is_not_null().any().alias("kp"),
        pl.col("tackles").is_not_null().any().alias("tk"),
        pl.col("rating").is_not_null().any().alias("rt"),
    )
    per_tm = tm.group_by("league_ref", "season").agg(
        pl.col("match_id").n_unique().alias("matches"),
        (pl.col("starters") == 11).mean().alias("eleven"),
        pl.col("kp").mean().alias("key_passes"), pl.col("tk").mean().alias("tackles"),
        pl.col("rt").mean().alias("rating"),
    )  # fmt: skip
    p = frame.filter(played)
    per_row = p.group_by("league_ref", "season").agg(
        pl.len().alias("played"),
        ((pl.col("minutes") - pl.col("minutes_provider")).abs() <= tolerance)
        .mean().alias("minutes_agree"),
        pl.col("minutes_provider").is_null().mean().alias("no_provider_minutes"),
        (pl.col("goals") == pl.col("goals_provider").fill_null(0)).mean().alias("goals_agree"),
        pl.col("own_goals").sum().alias("own_goals"),
    )  # fmt: skip
    rows = frame.group_by("league_ref", "season").agg(pl.len().alias("rows"))
    return (per_tm.join(per_row, on=["league_ref", "season"], how="left")
            .join(rows, on=["league_ref", "season"]).sort("league_ref", "season"))  # fmt: skip
