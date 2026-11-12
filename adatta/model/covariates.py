"""Phase 2 context covariates on log lambda (docs/PHASE2_TASKS.md, groups G1-G7).

Turns the context columns of a match (the context table at kickoff - horizon, joined with
the style table) into one design block per group. Pure numpy and polars: the backtest
harness and serving use it on the Mac; the PyMC terms live in team_strength.build.

Each group has a coefficient vector shared by the bundle's leagues, and a block per side:
  own   the side's own covariates act on the side's own lambda (x_home for the home rate,
        x_away for the away rate), one coefficient for both sides;
  both  one match-level covariate vector acts on both lambdas (x_home == x_away).

Coding (fixed constants, never statistics of the data, so no scaling can leak):
  G1 rest        short_rest = 1 if days_since_last < 4; long_break = 1 if >= 14
  G2 travel      log(1 + km / 100); |timezone shift| in hours; max(0, altitude diff) / 1000 m
  G3 weather     heat = max(0, t - 22) / 5; cold = max(0, 5 - t) / 5;
                 wind = max(0, w - 20) / 10; rain = log(1 + mm)
  G4 referee     shrunk = (n r + 20 pool) / (n + 20); x = shrunk / pool - 1;
                 0 under referee_basis pool_prior. Prior Normal(0, 0.1)
  G5 importance  dead_rubber, must_win indicators of the side's own importance
  G6 kickoff     early = local hour < 14; late = local hour >= 20. Regularised horseshoe
  G7 style       home tempo_rel + away tempo_rel (null if either side is null)
A null input adds 0 to log lambda: the match falls back to the 1b model for that term,
never imputed (principle 8). Priors Normal(0, 0.2) unless stated (§6.1).

As-of contract (Phase 2 acceptance, combined item 3): every row's context carries
`ctx_as_of`, the time its values were observable; `check_as_of` refuses a frame in which
any row's context is later than its kickoff - horizon. Training rows are final by the
refit's as-of (kickoff + lag), so their kickoff - 1 h context is earlier still.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import numpy as np
import polars as pl
from numpy.typing import NDArray

F64 = NDArray[np.float64]
HEADLINE_HORIZON = timedelta(hours=1)
REFEREE_SHRINK = 20.0  # pseudo-matches of the pool prior (G4)
HORSESHOE_TAU0 = 0.01  # global scale: ~1 of 2 terms active, Poisson sd ~0.9, ~10^4 rates
HORSESHOE_SLAB = 0.2  # slab scale, the Normal(0, 0.2) of the other groups
HORSESHOE_SLAB_DF = 4.0


@dataclass(frozen=True)
class Group:
    name: str
    kind: Literal["own", "both"]
    columns: tuple[str, ...]  # coded covariate names
    inputs: tuple[str, ...]  # context columns it reads ("{side}" is home / away)
    prior: Literal["normal", "horseshoe"] = "normal"
    sd: float = 0.2


GROUPS: dict[str, Group] = {
    g.name: g
    for g in (
        Group("rest", "own", ("short_rest", "long_break"), ("{side}_days_since_last",)),
        Group("travel", "own", ("log_travel", "tz_shift", "altitude_climb"),
              ("{side}_travel_km", "{side}_tz_shift_h", "{side}_altitude_diff_m")),
        Group("weather", "both", ("heat", "cold", "wind", "rain"),
              ("temperature_c", "wind_speed_kmh", "precipitation_mm")),
        Group("referee", "both", ("referee_pace",),
              ("referee_fouls_pm", "referee_matches", "pool_fouls_pm", "referee_basis"),
              sd=0.1),
        Group("importance", "own", ("dead_rubber", "must_win"), ("{side}_importance",)),
        Group("kickoff", "both", ("early", "late"), ("kickoff_hour_local",),
              prior="horseshoe"),
        Group("style", "both", ("tempo",), ("home_tempo_rel", "away_tempo_rel")),
    )
}  # fmt: skip
CODES = {"G1": "rest", "G2": "travel", "G3": "weather", "G4": "referee", "G5": "importance",
         "G6": "kickoff", "G7": "style"}  # fmt: skip


@dataclass(frozen=True)
class Block:
    """One group's design: (rows, columns) for the home rate and for the away rate."""

    group: Group
    x_home: F64
    x_away: F64


Design = tuple[Block, ...]


def _col(df: pl.DataFrame, name: str) -> F64:
    """A float column with nulls as NaN (missing columns are all-NaN)."""
    if name not in df.columns:
        return np.full(df.height, np.nan)
    return df[name].cast(pl.Float64).fill_null(np.nan).to_numpy().astype(np.float64)


def _zero_nan(x: F64) -> F64:
    return np.where(np.isfinite(x), x, 0.0)


def _rest(df: pl.DataFrame, side: str) -> F64:
    d = _col(df, f"{side}_days_since_last")
    ok = np.isfinite(d)
    return np.column_stack([(ok & (d < 4)).astype(float), (ok & (d >= 14)).astype(float)])


def _travel(df: pl.DataFrame, side: str) -> F64:
    km = _col(df, f"{side}_travel_km")
    tz = _col(df, f"{side}_tz_shift_h")
    alt = _col(df, f"{side}_altitude_diff_m")
    return np.column_stack([
        _zero_nan(np.log1p(np.maximum(km, 0.0) / 100.0)),
        _zero_nan(np.abs(tz)),
        _zero_nan(np.maximum(alt, 0.0) / 1000.0),
    ])  # fmt: skip


def _weather(df: pl.DataFrame) -> F64:
    t, w, mm = _col(df, "temperature_c"), _col(df, "wind_speed_kmh"), _col(df, "precipitation_mm")
    return np.column_stack([
        _zero_nan(np.maximum(0.0, t - 22.0) / 5.0),
        _zero_nan(np.maximum(0.0, 5.0 - t) / 5.0),
        _zero_nan(np.maximum(0.0, w - 20.0) / 10.0),
        _zero_nan(np.log1p(np.maximum(mm, 0.0))),
    ])  # fmt: skip


def _referee(df: pl.DataFrame) -> F64:
    r, n, pool = (
        _col(df, "referee_fouls_pm"),
        _col(df, "referee_matches"),
        _col(df, "pool_fouls_pm"),
    )
    n = np.where(np.isfinite(n), n, 0.0)
    known = np.isfinite(r) & np.isfinite(pool) & (pool > 0) & (n > 0)
    if "referee_basis" in df.columns:
        known &= (df["referee_basis"].fill_null("pool_prior") != "pool_prior").to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        shrunk = (n * r + REFEREE_SHRINK * pool) / (n + REFEREE_SHRINK)
        x = shrunk / pool - 1.0
    return np.where(known, x, 0.0)[:, None]


def _importance(df: pl.DataFrame, side: str) -> F64:
    name = f"{side}_importance"
    if name not in df.columns:
        return np.zeros((df.height, 2))
    lab = df[name].fill_null("").to_numpy()
    return np.column_stack(
        [(lab == "dead_rubber").astype(float), (lab == "must_win").astype(float)]
    )


def _kickoff(df: pl.DataFrame) -> F64:
    h = _col(df, "kickoff_hour_local")
    ok = np.isfinite(h)
    return np.column_stack([(ok & (h < 14)).astype(float), (ok & (h >= 20)).astype(float)])


def _style(df: pl.DataFrame) -> F64:
    return _zero_nan(_col(df, "home_tempo_rel") + _col(df, "away_tempo_rel"))[:, None]


_OWN: Mapping[str, Callable[[pl.DataFrame, str], F64]] = {
    "rest": _rest, "travel": _travel, "importance": _importance,
}  # fmt: skip
_BOTH: Mapping[str, Callable[[pl.DataFrame], F64]] = {
    "weather": _weather, "referee": _referee, "kickoff": _kickoff, "style": _style,
}  # fmt: skip


def resolve(groups: Sequence[str]) -> tuple[str, ...]:
    """Group names (or codes G1-G7) in the canonical order, without duplicates."""
    names = {CODES.get(g, g) for g in groups}
    unknown = names - set(GROUPS)
    if unknown:
        raise ValueError(f"unknown covariate groups {sorted(unknown)}; known: {list(GROUPS)}")
    return tuple(g for g in GROUPS if g in names)


def design(df: pl.DataFrame, groups: Sequence[str]) -> Design:
    """The design blocks of `groups` for the rows of df, in row order."""
    blocks = []
    for name in resolve(groups):
        g = GROUPS[name]
        if g.kind == "own":
            xh, xa = _OWN[name](df, "home"), _OWN[name](df, "away")
        else:
            xh = xa = _BOTH[name](df)
        blocks.append(Block(g, np.ascontiguousarray(xh, float), np.ascontiguousarray(xa, float)))
    return tuple(blocks)


def beta_name(group: Group) -> str:
    return f"beta_{group.name}"


def offsets(draws: Mapping[str, F64], blocks: Design) -> tuple[F64, F64]:
    """Per (row, draw): the covariate terms added to log lambda_home and log lambda_away.
    Coefficient draws are (columns, draw), the sample axis last as in `slim`."""
    n = blocks[0].x_home.shape[0] if blocks else 0
    first = next(iter(draws.values()))
    n_draws = first.shape[-1]
    off_h, off_a = np.zeros((n, n_draws)), np.zeros((n, n_draws))
    for b in blocks:
        beta = np.asarray(draws[beta_name(b.group)]).reshape(len(b.group.columns), n_draws)
        off_h += b.x_home @ beta
        off_a += b.x_away @ beta
    return off_h, off_a


def check_as_of(df: pl.DataFrame, horizon: timedelta = HEADLINE_HORIZON) -> None:
    """Refuse context observed after its row's prediction time (kickoff - horizon)."""
    if "ctx_as_of" not in df.columns:
        raise ValueError("covariate groups need the context's ctx_as_of column")
    late = df.filter(
        pl.col("ctx_as_of").is_null() | (pl.col("ctx_as_of") > pl.col("kickoff") - horizon)
    )
    if late.height:
        raise ValueError(f"{late.height} rows have context observed after kickoff - {horizon}")
