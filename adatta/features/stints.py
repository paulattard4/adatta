"""Normal-time stints per team-match from the stored detail payloads (P3-i0,
docs/PHASE3_TASKS.md; register B.1). No API requests.

A team's match is cut at its own substitutions and sendings-off
(`api_football_stats.stints`); each stint has its players on the pitch and the goals the
team conceded in it. One Parquet file per competition-season in the object store
(`players/stints/v1/<league>/<season>.parquet`), rebuilt from the raw payloads (principle
4). A team-match whose stints don't add up to the goals it conceded in normal time (the
provider's 90-minute score) is dropped and counted.

As-of rule: stints are post-match facts, usable from kickoff + RESULT_LAG, like the result.
"""

from __future__ import annotations

import gzip
import io
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import polars as pl
from sqlalchemy.orm import Session

from adatta.adapters import api_football_stats as st
from adatta.adapters import normalize
from adatta.adapters.storage import RawStore, try_get
from adatta.features import appearances as ap

PREFIX = "players/stints/v1"
SCHEMA: dict[str, Any] = {
    "match_id": pl.Int64, "team_id": pl.Int64, "opp_id": pl.Int64, "league_ref": pl.Utf8,
    "season": pl.Int32, "kickoff": pl.Datetime("us", "UTC"), "stint": pl.Int32,
    "start": pl.Int32, "end": pl.Int32, "conceded": pl.Int32,
    "players": pl.List(pl.Int64), "source_id": pl.Utf8, "storage_key": pl.Utf8,
    "observed_at": pl.Datetime("us", "UTC"), "ingested_at": pl.Datetime("us", "UTC"),
}  # fmt: skip


def key(league_ref: str, season: int) -> str:
    return f"{PREFIX}/{league_ref}/{season}.parquet"


def rows_for(fx: dict[str, Any], t: ap.Target, ids: dict[str, int], league_ref: str,
             season: int, now: datetime) -> tuple[list[dict[str, Any]], Counter[str]]:  # fmt: skip
    """Stint rows of one match with database ids, checked against the 90-minute score."""
    got, counts = st.stints(fx)
    c: Counter[str] = Counter(counts)
    home, away = str(fx["teams"]["home"]["id"]), str(fx["teams"]["away"]["id"])
    side = {home: (t.home_team_id, t.away_team_id), away: (t.away_team_id, t.home_team_id)}
    ft = (fx.get("score") or {}).get("fulltime") or {}
    against = {home: ft.get("away"), away: ft.get("home")}
    out: list[dict[str, Any]] = []
    for ref in (home, away):
        rows = [r for r in got if r["team_ref"] == ref]
        if not rows:
            continue
        got_against = against[ref]
        if got_against is None or sum(r["conceded"] for r in rows) != int(got_against):
            c["goals_mismatch"] += 1
            continue
        if any(p not in ids for r in rows for p in r["players"]):
            c["unknown_player"] += 1
            continue
        team_id, opp_id = side[ref]
        for r in rows:
            out.append({
                "match_id": t.match_id, "team_id": team_id, "opp_id": opp_id,
                "league_ref": league_ref, "season": season, "kickoff": t.kickoff,
                "stint": r["stint"], "start": r["start"], "end": r["end"],
                "conceded": r["conceded"], "players": [ids[p] for p in r["players"]],
                "source_id": normalize.SOURCE_ID, "storage_key": t.storage_key,
                "observed_at": t.observed_at, "ingested_at": now,
            })  # fmt: skip
        c["team_matches"] += 1
    return out, c


def build(session: Session, store: RawStore, league_ref: str | None = None,
          season: int | None = None, rebuild: bool = False, workers: int = 8,
          now: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    """Write one Parquet file per competition-season; skip files already there unless
    `rebuild`. Players are looked up (and registered if new) as the appearances build."""
    now = now or datetime.now(UTC)
    totals: Counter[str] = Counter()

    def fetch(k: str) -> tuple[str, dict[str, Any]]:
        return k, json.loads(gzip.decompress(store.get(k)))

    with ThreadPoolExecutor(workers) as pool:
        for (lg, yr), ts in sorted(ap.targets(session, league_ref, season).items()):
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
            ids = {ref: p.player_id for ref, p in normalize.players_by_ref(session, names).items()}
            rows: list[dict[str, Any]] = []
            for t, fx in found:
                if fx is None:
                    totals["no_fixture"] += 1
                    continue
                got, c = rows_for(fx, t, ids, lg, yr, now)
                totals.update(c)
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
    """The stored stints of these competition-seasons (missing files are skipped)."""
    frames = []
    for lg, yr in groups:
        data = try_get(store, key(lg, yr))
        if data is not None:
            frames.append(pl.read_parquet(io.BytesIO(data)))
    return pl.concat(frames) if frames else pl.DataFrame(schema=SCHEMA)
