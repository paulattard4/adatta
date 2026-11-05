"""Rebuild normalised detail tables from the raw store (principle 4: raw payloads are kept so
any normalised table can be rebuilt). Makes no API requests."""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_stats as stats_adapter
from adatta.adapters import normalize
from adatta.adapters.storage import RawStore
from adatta.db import models

BATCH_KEYS = 50  # payload files per commit


def _pending(session: Session) -> dict[str, list[tuple[int, str]]]:
    """storage_key -> [(match_id, fixture ref)] for returned details with no stat rows."""
    D, M, T = models.MatchDetail, models.Match, models.TeamMatchStat
    has_stats = select(T.match_id).distinct()
    rows = session.execute(
        select(D.storage_key, D.match_id, M.source_ref)
        .join(M, M.match_id == D.match_id)
        .where(D.flags["returned"].as_boolean(), D.match_id.not_in(has_stats))
    ).all()
    out: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for key, mid, ref in rows:
        out[key].append((mid, ref))
    return out


def load_team_stats(session: Session, store: RawStore, workers: int = 8) -> dict[str, int]:
    """Parse team_match_stat rows from stored payloads for every match that lacks them.
    Resumable: commits every BATCH_KEYS files and skips matches already loaded."""
    pending = _pending(session)
    keys = sorted(pending)
    counts = {"files": 0, "matches": 0, "rows": 0, "no_stats": 0}

    def fetch(key: str) -> tuple[str, dict[str, Any]]:
        return key, json.loads(gzip.decompress(store.get(key)))

    with ThreadPoolExecutor(workers) as pool:
        for i in range(0, len(keys), BATCH_KEYS):
            for key, payload in pool.map(fetch, keys[i : i + BATCH_KEYS]):
                by_ref = {str(fx["fixture"]["id"]): fx for fx in payload.get("response", [])}
                detail_time = {
                    d.match_id: d.observed_at
                    for d in session.scalars(
                        select(models.MatchDetail).where(
                            models.MatchDetail.match_id.in_([m for m, _ in pending[key]])
                        )
                    )
                }
                # one team lookup per file, so each match below costs no extra round trip
                teams = normalize.teams_by_ref(
                    session,
                    {r["team_ref"]: r["team_ref"] for fx in by_ref.values()
                     for r in stats_adapter.team_stats(fx)},
                )  # fmt: skip
                for mid, ref in pending[key]:
                    fx = by_ref.get(ref)
                    rows = stats_adapter.team_stats(fx) if fx else []
                    if not rows:
                        counts["no_stats"] += 1
                        continue
                    counts["rows"] += normalize.ingest_team_stats(
                        session, mid, rows, key, detail_time[mid], new_match=True, teams=teams
                    )
                    counts["matches"] += 1
                counts["files"] += 1
            session.commit()
    return counts
