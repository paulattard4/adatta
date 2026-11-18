"""P3-d0 (a) the counts table for the Layer 5 player rates (docs/PHASE3_TASKS.md, spec §7.4).

One row per played appearance (minutes > 0) in a bundle league match, seasons 2016-2023,
built from the stored appearances (P3-0) and the lineup table (P3-c), 0 requests:
- exposure e = event minutes / 90;
- the six statistics, a null meaning 0 where the statistic is collected (the parser does
  this for "any player of the team has a value"; for assists and yellow cards, which can
  be zero for a whole team, a player with a statistics block gets 0; for shots on target,
  a player with a statistics block in a team-match whose shots were collected);
- shots are shots on target (amendment 1: the provider's per-player total shots include
  blocked shots up to 2019 and leave them out from 2020; on target is the same throughout);
- line (the provider's G/D/M/F, else the player's most common earlier line), substitute,
  slot code (positional starters only);
- the ramps: first NEW_CLUB_N appearances for a club with none for it in the previous
  365 days, and the first RETURN_N after RETURN_GAP or more consecutive club league matches
  out of the matchday squad (a proxy: the absence reason is not known in dev, D39).
Every ramp and line value uses only the player's earlier rows (as-of by construction).

Checks (a): goals reconcile with the score, provider assists and yellows with the events
(a sample of stored payloads), summed player shots on target with the team's. A statistic that
fails in a league-season drops out there (set to null), never filled.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, cast

import polars as pl

from adatta.adapters import api_football_stats as st
from adatta.adapters.storage import RawStore

PREFIX = "features/rates/v1"
STATS = ("goals", "assists", "shots_on", "key_passes", "tackles", "yellow")
LINES = ("G", "D", "M", "F")
NEW_CLUB_N = 5
NEW_CLUB_GAP = timedelta(days=365)
RETURN_N = 3
RETURN_GAP = 3  # consecutive club league matches out of the matchday squad
HORIZON = timedelta(hours=24)  # player posteriors and ramps updated at kickoff - 24 h
# (a) thresholds
GOALS_MIN = 0.995
EVENTS_MIN = 0.95
SHOTS_MIN = 0.95
EVENT_KEYS_PER_GROUP = 3  # stored payloads (about 20 matches each) per league-season
TEMPORAL = ["source_id", "storage_key", "observed_at"]


def _counts(app: pl.DataFrame) -> pl.DataFrame:
    """Null rule for assists, yellow cards and shots on target (module docstring)."""
    block = pl.col("minutes_provider").is_not_null()
    shots = (pl.col("shots_total").is_not_null() | pl.col("shots_on").is_not_null()).any()
    app = app.with_columns(_shots=shots.over("match_id", "team_id"))
    fill = {"assists": block, "yellow": block, "shots_on": block & pl.col("_shots")}
    return app.with_columns(
        [pl.when(pl.col(c).is_null() & cond).then(0).otherwise(pl.col(c)).alias(c)
         for c, cond in fill.items()]
    ).drop("_shots")  # fmt: skip


def _line(played: pl.DataFrame) -> pl.DataFrame:
    """The appearance's G/D/M/F, else the player's most common line in earlier rows."""
    p = played.sort("player_id", "kickoff", "match_id")
    prev = [((pl.col("pos") == x).fill_null(False).cast(pl.Int32).cum_sum().over("player_id")
             - (pl.col("pos") == x).fill_null(False).cast(pl.Int32)).alias(f"_n{x}")
            for x in LINES]  # fmt: skip
    p = p.with_columns(prev)
    n = pl.concat_list([f"_n{x}" for x in LINES])
    modal = pl.when(n.list.max() > 0).then(
        n.list.arg_max().replace_strict(dict(enumerate(LINES)), return_dtype=pl.Utf8)
    )
    return p.with_columns(
        line=pl.when(pl.col("pos").is_in(LINES)).then(pl.col("pos")).otherwise(modal)
    ).drop([f"_n{x}" for x in LINES])


def _new_club(played: pl.DataFrame, start: datetime | None = None) -> pl.DataFrame:
    """First NEW_CLUB_N appearances of a stint at a club (no appearance for it in the
    previous 365 days). A stint that starts within 365 days of the first stored match has
    an unknown past and is not flagged."""
    if start is None:
        start = cast(datetime | None, played["kickoff"].min())
    p = played.sort("player_id", "team_id", "kickoff", "match_id")
    prev = pl.col("kickoff").shift(1).over("player_id", "team_id")
    p = p.with_columns(_new=prev.is_null() | (pl.col("kickoff") - prev > NEW_CLUB_GAP))
    p = p.with_columns(_stint=pl.col("_new").cast(pl.Int32).cum_sum().over("player_id", "team_id"))
    first = pl.col("kickoff").min().over("player_id", "team_id", "_stint")
    rank = pl.int_range(1, pl.len() + 1).over("player_id", "team_id", "_stint")
    known = first - start > NEW_CLUB_GAP if start is not None else pl.lit(False)
    return p.with_columns(new_club=(rank <= NEW_CLUB_N) & known).drop("_new", "_stint")


def _returns(squad: pl.DataFrame) -> pl.DataFrame:
    """(match_id, team_id, player_id, ret) for played rows: the first RETURN_N played
    appearances after RETURN_GAP or more consecutive club league matches outside the
    matchday squad."""
    tm = squad.select("team_id", "match_id", "kickoff").unique(["team_id", "match_id"])
    tm = tm.sort("team_id", "kickoff", "match_id").with_columns(
        tidx=pl.int_range(pl.len()).over("team_id")
    )
    s = squad.join(tm.select("team_id", "match_id", "tidx"), on=["team_id", "match_id"])
    s = s.sort("team_id", "player_id", "tidx")
    gap = pl.col("tidx") - pl.col("tidx").shift(1).over("team_id", "player_id") - 1
    s = s.with_columns(_ev=(gap >= RETURN_GAP).fill_null(False))
    s = s.with_columns(_stint=pl.col("_ev").cast(pl.Int32).cum_sum().over("team_id", "player_id"))
    played = (pl.col("minutes") > 0).cast(pl.Int32)
    s = s.with_columns(_rank=played.cum_sum().over("team_id", "player_id", "_stint"))
    s = s.with_columns(ret=(pl.col("_stint") > 0) & (pl.col("minutes") > 0)
                       & (pl.col("_rank") <= RETURN_N))  # fmt: skip
    return s.filter(pl.col("minutes") > 0).select("match_id", "team_id", "player_id", "ret")


def build(app: pl.DataFrame, lineups: pl.DataFrame,
          data_start: datetime | None = None) -> pl.DataFrame:  # fmt: skip
    """The counts table from bundle-league appearances (all squad rows) and the lineup
    table. Rows without a line (no provider code and no earlier one) are dropped.
    `data_start` is the first stored kickoff (default: the first in `app`)."""
    app = _counts(app)
    played = app.filter(pl.col("minutes") > 0)
    played = _new_club(_line(played), data_start)
    played = played.join(_returns(app), on=["match_id", "team_id", "player_id"], how="left")
    slots = lineups.filter(pl.col("positional")).select("match_id", "team_id", "player_id",
                                                        "slot")  # fmt: skip
    played = played.join(slots, on=["match_id", "team_id", "player_id"], how="left")
    out = played.filter(pl.col("line").is_not_null()).select(
        "match_id", "team_id", "player_id", "league_ref", "season", "kickoff",
        sub=~pl.col("started"), e=pl.col("minutes").cast(pl.Float64) / 90.0,
        line="line", new_club="new_club", ret=pl.col("ret").fill_null(False), slot="slot",
        **{f"y_{s}": pl.col(s).cast(pl.Int32) for s in STATS},
        **{c: pl.col(c) for c in TEMPORAL if c in played.columns},
    )  # fmt: skip
    return out.sort("kickoff", "match_id", "team_id", "player_id")


# ----------------------------------------------------------------------------- checks


def goal_check(app: pl.DataFrame, scores: pl.DataFrame) -> pl.DataFrame:
    """Per league-season: share of team-matches where the players' goals plus the
    opponent's own goals equal the team's 90' score. `scores`: match_id, home_team_id,
    away_team_id, home_goals, away_goals."""
    tm = app.group_by("league_ref", "season", "match_id", "team_id").agg(
        pl.col("goals").sum().alias("g"), pl.col("own_goals").sum().alias("og")
    )
    sides = pl.concat([
        scores.select("match_id", team_id="home_team_id", opp="away_team_id",
                      score="home_goals"),
        scores.select("match_id", team_id="away_team_id", opp="home_team_id",
                      score="away_goals"),
    ])  # fmt: skip
    opp = tm.select("match_id", opp="team_id", opp_og="og")
    j = tm.join(sides, on=["match_id", "team_id"]).join(opp, on=["match_id", "opp"], how="left")
    j = j.filter(pl.col("score").is_not_null())
    ok = (pl.col("g") + pl.col("opp_og").fill_null(0)) == pl.col("score")
    return j.group_by("league_ref", "season").agg(pl.len().alias("n"), ok.mean().alias("value"))


def shots_check(app: pl.DataFrame, team_shots: pl.DataFrame) -> pl.DataFrame:
    """Per league-season: share of team-matches (both counts present) where the players'
    shots on target sum to within 1 of the team's. `team_shots`: match_id, team_id, shots
    (the team's shots on goal)."""
    tm = (_counts(app).filter(pl.col("shots_on").is_not_null())
          .group_by("league_ref", "season", "match_id", "team_id")
          .agg(pl.col("shots_on").sum().alias("p")))  # fmt: skip
    j = tm.join(team_shots.drop_nulls("shots"), on=["match_id", "team_id"])
    ok = (pl.col("p") - pl.col("shots")).abs() <= 1
    return j.group_by("league_ref", "season").agg(pl.len().alias("n"), ok.mean().alias("value"))


def event_sample(store: RawStore, app: pl.DataFrame, per_group: int = EVENT_KEYS_PER_GROUP,
                 seed: int = 20261118, workers: int = 8) -> pl.DataFrame:  # fmt: skip
    """Per league-season: share of played appearances (with a statistics block) whose
    provider assists and yellows equal the events' counts, on `per_group` stored payloads
    per league-season."""
    rng = random.Random(seed)
    groups: dict[tuple[str, int], list[str]] = {}
    for lg, yr, keys in (app.group_by("league_ref", "season")
                         .agg(pl.col("storage_key").unique().sort()).iter_rows()):  # fmt: skip
        groups[(lg, yr)] = rng.sample(keys, min(per_group, len(keys)))

    def one(item: tuple[tuple[str, int], str]) -> list[dict[str, Any]]:
        (lg, yr), k = item
        rows = []
        for fx in json.loads(gzip.decompress(store.get(k))).get("response", []):
            ev = st.event_counts(fx)
            for r in st.player_appearances(fx)[0]:
                if r["minutes"] <= 0 or r["minutes_provider"] is None:
                    continue
                e = ev.get((r["team_ref"], r["player_ref"]), {"assists": 0, "yellow": 0})
                rows.append({"league_ref": lg, "season": yr,
                             "assists": (r["assists"] or 0) == e["assists"],
                             "yellow": (r["yellow"] or 0) == e["yellow"]})  # fmt: skip
        return rows

    items = [(g, k) for g, keys in sorted(groups.items()) for k in keys]
    with ThreadPoolExecutor(workers) as pool:
        rows = [r for part in pool.map(one, items) for r in part]
    d = pl.DataFrame(rows, schema={"league_ref": pl.Utf8, "season": pl.Int32,
                                   "assists": pl.Boolean, "yellow": pl.Boolean})  # fmt: skip
    return d.group_by("league_ref", "season").agg(
        pl.len().alias("n"), pl.col("assists").mean(), pl.col("yellow").mean()
    )


def checks(goals: pl.DataFrame, shots: pl.DataFrame, events: pl.DataFrame) -> pl.DataFrame:
    """One row per league-season x check: value, threshold, passed, and the statistic that
    drops out where it fails."""
    parts = [
        goals.with_columns(check=pl.lit("goals vs score"), stat=pl.lit("goals"),
                           threshold=pl.lit(GOALS_MIN)),
        shots.with_columns(check=pl.lit("shots on target vs team"), stat=pl.lit("shots_on"),
                           threshold=pl.lit(SHOTS_MIN)),
        events.select("league_ref", "season", "n", value="assists").with_columns(
            check=pl.lit("assists vs events"), stat=pl.lit("assists"),
            threshold=pl.lit(EVENTS_MIN)),
        events.select("league_ref", "season", "n", value="yellow").with_columns(
            check=pl.lit("yellow vs events"), stat=pl.lit("yellow"),
            threshold=pl.lit(EVENTS_MIN)),
    ]  # fmt: skip
    cols = ["league_ref", "season", "check", "stat", "n", "value", "threshold"]
    out = pl.concat([p.select(cols).with_columns(pl.col("n").cast(pl.Int64)) for p in parts])
    return out.with_columns(passed=pl.col("value") >= pl.col("threshold")).sort(
        "league_ref", "season", "check"
    )


def apply_drops(table: pl.DataFrame, chk: pl.DataFrame) -> pl.DataFrame:
    """Null a statistic in every league-season where its check failed."""
    for r in chk.filter(~pl.col("passed")).iter_rows(named=True):
        hit = (pl.col("league_ref") == r["league_ref"]) & (pl.col("season") == r["season"])
        c = f"y_{r['stat']}"
        table = table.with_columns(pl.when(hit).then(None).otherwise(pl.col(c)).alias(c))
    return table


# ----------------------------------------------------------------------------- store


def digest(table: pl.DataFrame) -> str:
    h = hashlib.sha256()
    h.update(table.select(pl.all().exclude(TEMPORAL)).hash_rows(seed=0).to_numpy().tobytes())
    return h.hexdigest()[:16]


def to_bytes(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def save(store: RawStore, table: pl.DataFrame, meta: dict[str, Any]) -> str:
    d = digest(table)
    store.put(f"{PREFIX}/{d}/table.parquet", to_bytes(table))
    store.put(f"{PREFIX}/{d}/meta.json", json.dumps(meta).encode())
    store.put(f"{PREFIX}/latest.json", json.dumps({"digest": d}).encode())
    return d


def load(store: RawStore, ref: str = "latest") -> tuple[pl.DataFrame, dict[str, Any], str]:
    from adatta.adapters.storage import try_get

    if ref == "latest":
        raw = try_get(store, f"{PREFIX}/latest.json")
        if raw is None:
            raise FileNotFoundError("no rates table stored")
        ref = json.loads(raw)["digest"]
    table = pl.read_parquet(io.BytesIO(store.get(f"{PREFIX}/{ref}/table.parquet")))
    meta = json.loads(store.get(f"{PREFIX}/{ref}/meta.json"))
    return table, meta, ref
