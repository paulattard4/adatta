"""P3b-3 cards table (docs/PHASE3B_TASKS.md P3b-3, D66): one row per team per regular-season
league match of the ten bundle leagues, with the team's cards (yellow + red as the provider
counts) and the referee's identity.

Rule 8: a league-season without `has_cards_data` (available or partial) has no rows; a
match without both teams' statistics has none. The referee is the identity across name
formats (`features.referees`), from the season fixture lists, else the newest snapshot;
null where the provider never named one. Identity uses every name the provider has written
(it only says which names are one person); no count attached to it comes from after an
as-of (the model reads only matches final by then).

Built locally (it reads the database) and stored with a manifest; CI reads it by digest.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping
from typing import Any

import polars as pl

PREFIX = "features/cards/v1"
_TYPES: dict[str, Any] = {
    "match_id": pl.Int64,
    "team_id": pl.Int64,
    "opp_id": pl.Int64,
    "is_home": pl.Boolean,
    "league_id": pl.Int64,
    "season": pl.Int64,
    "kickoff": pl.Datetime("us", "UTC"),
    "bundle": pl.Utf8,
    "cards": pl.Int64,
    "referee": pl.Utf8,
}
SCHEMA = pl.Schema(_TYPES)  # fmt: skip


def rows(matches: pl.DataFrame, stats: pl.DataFrame, referee: Mapping[int, str | None],
         bundle_of: Mapping[int, str], cards_ok: set[tuple[int, int]]) -> pl.DataFrame:  # fmt: skip
    """Both sides of every match with both teams' card counts, in a league-season with
    cards data. `stats`: match_id, team_id, yellow, red (null counts as 0 when the team's
    block exists, as stored); `referee`: match -> identity."""
    st = stats.with_columns(cards=pl.col("yellow").fill_null(0) + pl.col("red").fill_null(0))
    n = st.group_by("match_id").agg(n=pl.len())
    m = matches.filter(
        pl.struct("league_id", "season").map_elements(
            lambda x: (int(x["league_id"]), int(x["season"])) in cards_ok, return_dtype=pl.Boolean
        )
    )
    m = m.join(n.filter(pl.col("n") == 2).select("match_id"), on="match_id")
    cols = ["match_id", "league_id", "season", "kickoff"]
    sides = pl.concat([
        m.select(*cols, team_id="home", opp_id="away", is_home=pl.lit(True)),
        m.select(*cols, team_id="away", opp_id="home", is_home=pl.lit(False)),
    ])  # fmt: skip
    out = sides.join(st.select("match_id", "team_id", "cards"), on=["match_id", "team_id"])
    ref = pl.DataFrame({"match_id": list(referee), "referee": list(referee.values())},
                       schema={"match_id": pl.Int64, "referee": pl.Utf8})  # fmt: skip
    out = out.join(ref, on="match_id", how="left").with_columns(
        bundle=pl.col("league_id").replace_strict(dict(bundle_of), return_dtype=pl.Utf8)
    )
    return out.select(list(SCHEMA)).cast(SCHEMA).sort("kickoff", "match_id", "is_home",
                                                      descending=[False, False, True])  # fmt: skip


def load(session: Any, store: Any) -> tuple[pl.DataFrame, dict[str, Any]]:
    """The table from the database and the stored fixture lists."""
    from sqlalchemy import select

    from adatta.db import models
    from adatta.eval import harness
    from adatta.features import referees as rf

    cfg = harness.load_config()
    bundle_of = {int(x): b for b, ids in cfg.bundles.items() for x in ids}
    matches, _ = harness.load_matches(session, list(bundle_of))
    T = models.TeamMatchStat
    stats = pl.DataFrame(
        [{"match_id": a, "team_id": b, "yellow": c, "red": d} for a, b, c, d in session.execute(
            select(T.match_id, T.team_id, T.yellow_cards, T.red_cards)
            .where(T.superseded_at.is_(None), T.match_id.in_(matches["match_id"].to_list())))],
        schema={"match_id": pl.Int64, "team_id": pl.Int64, "yellow": pl.Int64, "red": pl.Int64},
    )  # fmt: skip
    M, S, C, K = models.Match, models.Season, models.Competition, models.CompetitionSourceCapability
    cards_ok = {(int(ref), int(year)) for ref, year in session.execute(
        select(C.source_ref, S.year).join(S, S.competition_id == C.competition_id)
        .join(K, (K.competition_id == C.competition_id) & (K.season_id == S.season_id))
        .where(K.capability == "has_cards_data", K.status.in_(("available", "partial")),
               K.valid_to.is_(None)))
        if str(ref).isdigit() and int(ref) in bundle_of}  # fmt: skip
    seasons = {sid: (country, str(ref)) for sid, country, ref in session.execute(
        select(S.season_id, C.country, C.source_ref).join(C, C.competition_id == S.competition_id))
        if str(ref).isdigit() and int(ref) in bundle_of}  # fmt: skip
    names = rf.fixture_referees(session, store, seasons)
    info = {
        mid: (ref, sid)
        for mid, ref, sid in session.execute(
            select(M.match_id, M.source_ref, M.season_id).where(M.season_id.in_(list(seasons)))
        )
    }
    F = models.FixtureSnapshot
    snap: dict[int, str] = {}
    for mid, nm in session.execute(
        select(F.match_id, F.referee)
        .where(F.referee.is_not(None), F.match_id.in_(list(info)))
        .order_by(F.observed_at)
    ):
        if nm:
            snap[mid] = nm  # the newest wins
    hist = rf.RefereeHistory.load(session, store)
    referee: dict[int, str | None] = {}
    for mid in matches["match_id"].to_list():
        src, sid = info.get(mid, (None, None))
        raw = names.get(str(src)) or snap.get(mid)
        if raw and sid in seasons:
            country, league = seasons[sid]
            referee[mid] = hist.identify(country, raw, league)
        else:
            referee[mid] = None
    table = rows(matches, stats, referee, bundle_of, cards_ok)
    meta = {"cards_ok": sorted(cards_ok), "n": table.height,
            "referee_named": table["referee"].is_not_null().sum()
            / max(table.height, 1)}  # fmt: skip
    return table, meta


def digest(table: pl.DataFrame) -> str:
    buf = io.BytesIO()
    table.write_ipc(buf)
    return hashlib.sha256(buf.getvalue()).hexdigest()[:16]


def save(store: Any, table: pl.DataFrame, meta: dict[str, Any]) -> str:
    h = digest(table)
    buf = io.BytesIO()
    table.write_parquet(buf, compression="zstd")
    store.put(f"{PREFIX}/{h}/table.parquet", buf.getvalue())
    store.put(f"{PREFIX}/{h}/meta.json", json.dumps({**meta, "digest": h}, default=str).encode())
    store.put(f"{PREFIX}/latest", h.encode())
    return h


def load_table(store: Any, ref: str = "latest") -> tuple[pl.DataFrame, dict[str, Any], str]:
    h = store.get(f"{PREFIX}/latest").decode() if ref == "latest" else ref
    table = pl.read_parquet(io.BytesIO(store.get(f"{PREFIX}/{h}/table.parquet")))
    return table, json.loads(store.get(f"{PREFIX}/{h}/meta.json")), h


def main() -> None:
    import subprocess

    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session

    store = make_store(get_settings())
    with get_session() as session:
        table, meta = load(session, store)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()  # fmt: skip
    h = save(store, table, {**meta, "code_sha": sha, "polars": pl.__version__})
    by = table.group_by("league_id").agg(rows=pl.len(), referee=pl.col("referee").is_not_null()
                                         .mean()).sort("league_id")  # fmt: skip
    print(
        f"cards table {h} ({PREFIX}), code {sha[:7]}: {table.height} team-matches; "
        + ", ".join(f"{a} {b} ({c:.1%} named)" for a, b, c in by.iter_rows())
    )


if __name__ == "__main__":
    main()
