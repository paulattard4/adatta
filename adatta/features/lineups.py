"""P3-c (a) slot codes and the lineup table (docs/PHASE3_TASKS.md, spec §7.3).

The provider gives a formation string and a grid "row:col" per starter; slot codes are
derived from them deterministically. Row 1 is the goalkeeper, row 2 the back line and the
last row the forwards; column 1 is the left (P3-c amendment 1). The row counts after row 1
are the formation string, so formations come from the stored appearances.

Grid columns are pitch positions only from 2022/23 (amendment 1). A league-season gets full
slot codes only if it passes `grid_quality`; elsewhere a starter's code is their line from
the provider's G/D/M/F code (GK, D, M, F; U without one). Sides are never guessed.

Stored as Parquet in R2 (`players/lineups/v1/<digest>/`), with the temporal fields and
source of the appearances it comes from.
"""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any

import polars as pl

from adatta.adapters.storage import RawStore, try_get

PREFIX = "players/lineups/v1"
FB_SIDE_MIN = 0.90  # full-backs on their usual side, for a positional league-season
ROW_POS_MIN = 0.95  # provider G/D/M/F agreeing with the row's line
FB_ID_SEASONS = (2022, 2023)  # full-backs identified here (dev only, no holdout season)
FB_MIN_STARTS = 10
FB_EDGE_SHARE = 0.80
MIN_FB_ROWS = 50  # fewer full-back back-four starts than this: not shown positional
LINE_CODE = {"G": "GK", "D": "D", "M": "M", "F": "F"}
CODE_LINE = {
    "GK": "G", "LB": "D", "LCB": "D", "CB": "D", "RCB": "D", "RB": "D", "LWB": "D",
    "RWB": "D", "CDM": "M", "CM": "M", "CAM": "M", "LM": "M", "RM": "M", "LW": "F",
    "RW": "F", "ST": "F",
}  # fmt: skip
TEMPORAL = ["source_id", "storage_key", "observed_at", "ingested_at", "valid_from",
            "valid_to", "superseded_at"]  # fmt: skip


# ------------------------------------------------------------------------------- slot codes


def _wide(n: int, inner: str, left: str, right: str) -> list[str]:
    return [left, *[inner] * (n - 2), right]


def _back(n: int) -> list[str]:
    fixed = {1: ["CB"], 2: ["LCB", "RCB"], 3: ["LCB", "CB", "RCB"],
             4: ["LB", "LCB", "RCB", "RB"], 5: ["LWB", "LCB", "CB", "RCB", "RWB"]}  # fmt: skip
    return fixed.get(n) or _wide(n, "CB", "LWB", "RWB")


def _forwards(n: int) -> list[str]:
    if n <= 2:
        return ["ST"] * n
    return _wide(n, "ST", "LW", "RW")


def _middle(n: int, kind: str, back3: bool) -> list[str]:
    """kind: only (the one middle row), deep (first of several), mid (between), att (last
    before the forwards)."""
    left, right = ("LWB", "RWB") if back3 else ("LM", "RM")
    if n >= 4:
        return _wide(n, "CM", left, right)
    if kind == "deep":
        return ["CDM"] * n if n <= 2 else ["CM"] * n
    if kind == "att":
        return ["CAM"] * n if n <= 2 else ["LW", "CAM", "RW"]
    return ["CM"] * n


def formation_codes(rows: list[int]) -> list[list[str]]:
    """Slot codes per row, left to right, for row counts [1, back, ..., forwards]."""
    if not rows or rows[0] != 1:
        raise ValueError(f"row 1 must hold one goalkeeper: {rows}")
    out = [["GK"]]
    if len(rows) == 1:
        return out
    out.append(_back(rows[1]))
    back3 = rows[1] == 3
    middle = rows[2:-1]
    for i, n in enumerate(middle):
        if len(middle) == 1:
            kind = "only"
        elif i == 0:
            kind = "deep"
        elif i == len(middle) - 1:
            kind = "att"
        else:
            kind = "mid"
        out.append(_middle(n, kind, back3))
    if len(rows) > 2:
        out.append(_forwards(rows[-1]))
    return out


def formation_string(rows: list[int]) -> str:
    return "-".join(str(n) for n in rows[1:])


def row_line(row: int, last: int) -> str:
    if row == 1:
        return "G"
    if row == 2:
        return "D"
    return "F" if row == last and last > 2 else "M"


# --------------------------------------------------------------------------- the table


def starters(app: pl.DataFrame) -> pl.DataFrame:
    """Starters with a grid, with row, column, the row's size and the team's last row."""
    s = app.filter(pl.col("started") & pl.col("grid").is_not_null()).with_columns(
        row=pl.col("grid").str.split(":").list.get(0).cast(pl.Int32),
        col=pl.col("grid").str.split(":").list.get(1).cast(pl.Int32),
    )
    keys = ["match_id", "team_id"]
    s = s.join(s.group_by(*keys, "row").agg(pl.len().cast(pl.Int32).alias("n_row")),
               on=[*keys, "row"])  # fmt: skip
    return s.join(s.group_by(*keys).agg(pl.col("row").max().alias("last_row")), on=keys)


def complete(s: pl.DataFrame) -> pl.DataFrame:
    """Team-matches with 11 starters, one in row 1, rows 1..R and columns 1..n in every
    row."""
    keys = ["match_id", "team_id"]
    rows = s.group_by(*keys, "row").agg(
        ok=(pl.col("col").sort() == pl.int_range(1, pl.len() + 1)).all()
    )
    good = rows.group_by(keys).agg(pl.col("ok").all(), n_rows=pl.len())
    one_gk = s.group_by(keys).agg(
        n=pl.len(), gk=(pl.col("row") == 1).sum(), top=pl.col("row").max()
    )
    ok = good.join(one_gk, on=keys).filter(
        pl.col("ok") & (pl.col("n") == 11) & (pl.col("gk") == 1)
        & (pl.col("top") == pl.col("n_rows"))  # rows 1..R with none skipped
    )  # fmt: skip
    return s.join(ok.select(keys), on=keys, how="semi")


def grid_quality(s: pl.DataFrame) -> pl.DataFrame:
    """Per league-season: full-backs (identified in FB_ID_SEASONS) on their usual edge of a
    back four, the provider's G/D/M/F agreeing with the row's line, and the verdict."""
    s = s.with_columns(
        line=pl.struct("row", "last_row").map_elements(
            lambda r: row_line(r["row"], r["last_row"]), return_dtype=pl.Utf8
        )
    )
    b4 = s.filter((pl.col("row") == 2) & (pl.col("n_row") == 4))
    ident = (
        b4.filter(pl.col("season").is_in(FB_ID_SEASONS))
        .group_by("player_id")
        .agg(n=pl.len(), e1=(pl.col("col") == 1).mean(), e4=(pl.col("col") == 4).mean())
        .filter((pl.col("n") >= FB_MIN_STARTS)
                & ((pl.col("e1") >= FB_EDGE_SHARE) | (pl.col("e4") >= FB_EDGE_SHARE)))
    )  # fmt: skip
    fb = b4.join(ident.select("player_id", "e1"), on="player_id").with_columns(
        same=pl.when(pl.col("e1") >= FB_EDGE_SHARE)
        .then(pl.col("col") == 1)
        .otherwise(pl.col("col") == 4)
    )
    side = fb.group_by("league_ref", "season").agg(fb_rows=pl.len(), fb_side=pl.col("same").mean())
    agree = (
        s.filter(pl.col("pos").is_in(list(LINE_CODE)))
        .group_by("league_ref", "season")
        .agg(row_pos=(pl.col("pos") == pl.col("line")).mean())
    )
    q = (
        s.select("league_ref", "season").unique()
        .join(side, on=["league_ref", "season"], how="left")
        .join(agree, on=["league_ref", "season"], how="left")
        .with_columns(pl.col("fb_rows").fill_null(0))
    )  # fmt: skip
    return q.with_columns(
        positional=(pl.col("fb_rows") >= MIN_FB_ROWS)
        & (pl.col("fb_side").fill_null(0) >= FB_SIDE_MIN)
        & (pl.col("row_pos").fill_null(0) >= ROW_POS_MIN)
    ).sort("league_ref", "season")


def build(app: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """(lineups: one row per starter with its slot code; formations: one row per
    team-match; quality: the per league-season grid test)."""
    s = complete(starters(app))
    quality = grid_quality(s)
    keys = ["match_id", "team_id"]
    shape = s.group_by(keys).agg(pl.col("row").sort().unique_counts().alias("counts"))
    # unique_counts on sorted rows gives the row sizes in row order
    codes: dict[tuple[int, ...], list[list[str]]] = {}
    for c in shape["counts"].to_list():
        t = tuple(int(x) for x in c)
        if t not in codes:
            codes[t] = formation_codes(list(t))
    shape = shape.with_columns(
        formation=pl.col("counts").map_elements(
            lambda c: formation_string([int(x) for x in c]), return_dtype=pl.Utf8
        )
    )
    s = s.join(shape, on=keys).join(quality.select("league_ref", "season", "positional"),
                                    on=["league_ref", "season"])  # fmt: skip
    grid_code = [
        codes[tuple(int(x) for x in c)][r - 1][k - 1]
        for c, r, k in zip(s["counts"].to_list(), s["row"].to_list(), s["col"].to_list(),
                           strict=True)
    ]  # fmt: skip
    s = s.with_columns(grid_code=pl.Series(grid_code, dtype=pl.Utf8))
    line_code = pl.col("pos").replace_strict(LINE_CODE, default="U")
    lineups = s.with_columns(
        slot=pl.when(pl.col("positional")).then(pl.col("grid_code")).otherwise(line_code),
        line=pl.struct("row", "last_row").map_elements(
            lambda r: row_line(r["row"], r["last_row"]), return_dtype=pl.Utf8
        ),
    ).select(
        "match_id", "team_id", "player_id", "league_ref", "season", "kickoff", "pos", "row",
        "col", "line", "formation", "positional", "slot", *[c for c in TEMPORAL if c in s.columns],
    )  # fmt: skip
    formations = lineups.group_by(keys).agg(
        pl.col("league_ref", "season", "kickoff", "formation", "positional").first()
    )
    return lineups.sort(*keys, "slot"), formations.sort("kickoff", *keys), quality


def checks(lineups: pl.DataFrame) -> pl.DataFrame:
    """Per league-season, on positional team-matches: one code per starter, one GK, each
    row's coded starters equal to the formation string, the row's line agreeing with the
    provider's G/D/M/F code for at least ROW_POS_MIN of starters."""
    pos = lineups.filter(pl.col("positional"))
    keys = ["league_ref", "season", "match_id", "team_id"]
    per_row = (
        pos.filter(pl.col("row") > 1)
        .group_by(*keys, "row")
        .agg(n=pl.col("slot").is_not_null().sum())
    )
    shape = per_row.sort("row").group_by(keys).agg(coded=pl.col("n").cast(pl.Utf8).str.join("-"))
    tm = (
        pos.group_by(keys)
        .agg(
            one_code=(pl.col("slot").is_not_null().sum() == 11) & (pl.len() == 11),
            one_gk=(pl.col("slot") == "GK").sum() == 1,
            formation=pl.col("formation").first(),
        )
        .join(shape, on=keys)
        .with_columns(rows_ok=pl.col("coded") == pl.col("formation"))
    )
    agree = (
        pos.filter(pl.col("pos").is_in(list(LINE_CODE)))
        .group_by("league_ref", "season")
        .agg(line_agree=(pl.col("pos") == pl.col("line")).mean())
    )
    out = tm.group_by("league_ref", "season").agg(
        team_matches=pl.len(), one_code=pl.col("one_code").mean(),
        one_gk=pl.col("one_gk").mean(), rows_ok=pl.col("rows_ok").mean(),
    ).join(agree, on=["league_ref", "season"], how="left")  # fmt: skip
    return out.with_columns(
        passed=(pl.col("one_code") == 1) & (pl.col("one_gk") == 1) & (pl.col("rows_ok") == 1)
        & (pl.col("line_agree").fill_null(0) >= ROW_POS_MIN)
    ).sort("league_ref", "season")  # fmt: skip


# ----------------------------------------------------------------------------- the store


def digest(lineups: pl.DataFrame) -> str:
    cols = ["match_id", "team_id", "player_id", "formation", "slot", "positional"]
    data = lineups.select(cols).sort("match_id", "team_id", "player_id").write_csv()
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd")
    return buf.getvalue()


def save(store: RawStore, lineups: pl.DataFrame, formations: pl.DataFrame,
         quality: pl.DataFrame, meta: dict[str, Any]) -> str:  # fmt: skip
    d = digest(lineups)
    store.put(f"{PREFIX}/{d}/lineups.parquet", _parquet(lineups))
    store.put(f"{PREFIX}/{d}/formations.parquet", _parquet(formations))
    store.put(f"{PREFIX}/{d}/quality.parquet", _parquet(quality))
    store.put(f"{PREFIX}/{d}/meta.json", json.dumps(meta).encode())
    store.put(f"{PREFIX}/latest.json", json.dumps({"digest": d}).encode())
    return d


def load(store: RawStore, ref: str = "latest") -> tuple[pl.DataFrame, pl.DataFrame, str]:
    """(lineups, formations, digest)."""
    if ref == "latest":
        raw = try_get(store, f"{PREFIX}/latest.json")
        if raw is None:
            raise FileNotFoundError("no lineup table stored")
        ref = json.loads(raw)["digest"]
    lu = pl.read_parquet(io.BytesIO(store.get(f"{PREFIX}/{ref}/lineups.parquet")))
    fm = pl.read_parquet(io.BytesIO(store.get(f"{PREFIX}/{ref}/formations.parquet")))
    return lu, fm, ref
