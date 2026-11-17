"""P3-c (a): slot codes from formation plus grid, the positional test, the lineup table and
its checks (adatta/features/lineups.py)."""

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from adatta.features import lineups as lu

VOCAB = ["4-2-3-1", "4-3-3", "4-4-2", "4-1-4-1", "4-3-1-2", "3-5-2", "4-4-1-1", "3-4-2-1",
         "3-4-1-2", "3-4-3", "5-3-2", "3-1-4-2", "5-4-1", "3-5-1-1", "4-5-1", "4-2-2-2",
         "4-3-2-1", "4-1-3-2", "3-3-1-3", "4-1-2-1-2", "4-6", "3-2-4-1"]  # fmt: skip
MIRROR = {"LB": "RB", "LCB": "RCB", "LWB": "RWB", "LM": "RM", "LW": "RW"}
MIRROR |= {v: k for k, v in MIRROR.items()}


def rows_of(f: str) -> list[int]:
    return [1, *(int(x) for x in f.split("-"))]


@pytest.mark.parametrize("f", VOCAB)
def test_every_formation_gets_eleven_mirrored_codes(f: str) -> None:
    codes = lu.formation_codes(rows_of(f))
    flat = [c for row in codes for c in row]
    assert len(flat) == 11 and flat.count("GK") == 1
    assert all(c in lu.CODE_LINE for c in flat)
    for row in codes:  # left to right, so each row reads the same mirrored
        assert [MIRROR.get(c, c) for c in reversed(row)] == row
    assert lu.formation_string(rows_of(f)) == f


def test_common_shapes_read_as_expected() -> None:
    assert lu.formation_codes(rows_of("4-2-3-1")) == [
        ["GK"],
        ["LB", "LCB", "RCB", "RB"],
        ["CDM", "CDM"],
        ["LW", "CAM", "RW"],
        ["ST"],
    ]
    assert lu.formation_codes(rows_of("3-5-2")) == [
        ["GK"],
        ["LCB", "CB", "RCB"],
        ["LWB", "CM", "CM", "CM", "RWB"],
        ["ST", "ST"],
    ]
    assert lu.formation_codes(rows_of("4-1-4-1"))[2:] == [["CDM"], ["LM", "CM", "CM", "RM"], ["ST"]]
    assert lu.formation_codes(rows_of("4-3-3"))[2:] == [["CM", "CM", "CM"], ["LW", "ST", "RW"]]
    with pytest.raises(ValueError):
        lu.formation_codes([2, 4, 4])


T0 = datetime(2022, 8, 6, 15, tzinfo=UTC)
F433 = [(1, 1), (2, 1), (2, 2), (2, 3), (2, 4), (3, 1), (3, 2), (3, 3), (4, 1), (4, 2), (4, 3)]
POS = ["G", "D", "D", "D", "D", "M", "M", "M", "F", "F", "F"]


def app(season: int, matches: int, positional: bool, league: str = "39") -> list[dict[str, object]]:
    """Team 1's XI: player 2 always a left-back, 5 a right-back; in a non-positional season
    their columns are shuffled into the middle."""
    out = []
    for m in range(matches):
        grid = list(F433)
        if not positional:  # listing order: full-backs in the centre
            grid[1], grid[2] = grid[2], grid[1]
            grid[4], grid[3] = grid[3], grid[4]
        for i, (r, c) in enumerate(grid):
            out.append({"match_id": season * 1000 + m, "team_id": 1, "player_id": i + 1,
                        "league_ref": league, "season": season,
                        "kickoff": T0.replace(year=season) + timedelta(weeks=m),
                        "started": True, "pos": POS[i], "grid": f"{r}:{c}",
                        "source_id": "api_football", "storage_key": "k"})  # fmt: skip
    return out


def frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema_overrides={"kickoff": pl.Datetime("us", "UTC")})


def test_positional_seasons_get_full_codes_and_others_line_codes() -> None:
    a = frame(app(2022, 30, True) + app(2023, 30, True) + app(2019, 30, False))
    lineups, formations, quality = lu.build(a)
    q = {r["season"]: r["positional"] for r in quality.iter_rows(named=True)}
    assert q == {2019: False, 2022: True, 2023: True}
    s22 = lineups.filter((pl.col("season") == 2022) & (pl.col("player_id") == 2))
    assert set(s22["slot"].to_list()) == {"LB"}
    s19 = lineups.filter(pl.col("season") == 2019)
    assert set(s19["slot"].to_list()) <= {"GK", "D", "M", "F"}
    assert set(formations["formation"].to_list()) == {"4-3-3"}
    c = lu.checks(lineups)
    assert c["passed"].all() and set(c["season"].to_list()) == {2022, 2023}


def test_incomplete_grids_drop_out() -> None:
    rows = app(2022, 1, True)
    rows[3]["grid"] = "2:7"  # columns no longer 1..n
    lineups, _, _ = lu.build(frame(rows + app(2023, 1, True)))
    assert set(lineups["season"].to_list()) == {2023}
