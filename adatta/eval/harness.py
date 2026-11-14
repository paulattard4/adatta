"""Walk-forward harness (spec §14.1–14.4) for the Phase 1a baseline.

For each weekly as-of time t, every model is refitted on results observable at t (kickoff +
`result_available_after_hours` <= t, within the lookback) and predicts the matches kicking
off in [t, t + refit_every_days). Holdout seasons are refused: they are opened only for a
promotion decision (§14.3).

Models scored on the same harness:
  dixon_coles     static Dixon-Coles, time-decayed (the Phase 1a benchmark)
  league_poisson  independent Poisson with league home/away rates only (no team effects)
  base_rate       per-league empirical outcome frequencies
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import yaml
from numpy.typing import NDArray
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import FINISHED, SOURCE_ID
from adatta.db import models
from adatta.eval import metrics
from adatta.model import dixon_coles as dc

Arr = NDArray[np.float64]
DAY = 86_400.0
MODELS = ("dixon_coles", "league_poisson", "base_rate")
REGULAR_SEASON = "Regular Season"


class HoldoutLocked(RuntimeError):
    pass


@dataclass(frozen=True)
class HoldoutKey:
    """Permission to plan the locked holdout, issued only by `adatta.eval.holdout.unlock`
    after the opened-once marker is checked. `run_id` is the run that opened it."""

    run_id: str


class Phase1bGate(BaseModel):
    min_rel_logloss_gain_1x2: float
    min_rel_rps_gain: float
    paired_interval: float
    max_segment_rel_worse: float
    segment_metrics: list[str]


class Phase2Route(BaseModel):
    """One way through the Phase 2 combined-model gate (D36), on all warm matches against
    the 1b reference: every `improve` metric's paired interval excludes 0 on the better
    side, every `no_worse` metric's point estimate is <= 0, and (if set) the ECE of each
    1X2 outcome and of OU2.5 is no worse than the reference's by more than `ece_limit`."""

    name: str
    improve: list[str]
    no_worse: list[str] = []
    ece_limit: float | None = None


class Phase2Gate(BaseModel):
    paired_interval: float
    max_segment_rel_worse: float
    segment_metrics: list[str]
    routes: list[Phase2Route]


class Phase1aSelected(BaseModel):
    half_life_days: float | None
    team_prior_sd: float | None


class EvalConfig(BaseModel):
    backfill_start: int
    warmup_seasons: list[int]
    dev_seasons: list[int]
    holdout_seasons: list[int]
    live_from_season: int
    lookback_days: int
    half_life_grid_days: list[float]
    team_prior_sd_grid: list[float | None]
    refit_every_days: int
    dev_iteration_refit_every_days: int
    result_available_after_hours: float
    min_warmup_matches: int
    report_cold_start_separately: bool
    max_goals: int
    bundles: dict[str, list[int]]
    headline_bundle: str
    phase1b_gate: Phase1bGate
    phase2_gate: Phase2Gate
    phase1a_selected: Phase1aSelected


def load_config(path: Path | str = "config/evaluation.yml") -> EvalConfig:
    """evaluation.yml plus the Phase 2 gate from phase2_gate.yml beside it (kept apart
    because evaluation.yml is part of every fit's cache hash)."""
    raw = yaml.safe_load(Path(path).read_text())
    gate = Path(path).parent / "phase2_gate.yml"
    return EvalConfig(**raw, **yaml.safe_load(gate.read_text()))


# --------------------------------------------------------------------------- data


def load_matches(session: Session, league_ids: list[int]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Finished regular-season league matches with a known 90' score, plus the ones that
    can't be scored (finished but 90' score unknown) with the reason."""
    M, S, C, R = models.Match, models.Season, models.Competition, models.MatchResult
    rows = session.execute(
        select(
            M.match_id, C.source_ref, S.year, M.kickoff_time, M.home_team_id, M.away_team_id,
            M.round, M.status, R.home_goals_nt, R.away_goals_nt,
        )
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .outerjoin(R, (R.match_id == M.match_id) & R.superseded_at.is_(None))
        .where(
            C.source_id == SOURCE_ID,
            C.source_ref.in_([str(i) for i in league_ids]),
            M.status.in_(FINISHED),
        )
    ).all()  # fmt: skip
    df = pl.DataFrame(
        [
            {
                "match_id": r[0], "league_id": int(r[1]), "season": r[2],
                "kickoff": r[3] if r[3].tzinfo else r[3].replace(tzinfo=UTC),
                "home": r[4], "away": r[5], "round": r[6] or "", "hg": r[8], "ag": r[9],
            }
            for r in rows
        ],
        schema={
            "match_id": pl.Int64, "league_id": pl.Int64, "season": pl.Int64,
            "kickoff": pl.Datetime("us", "UTC"), "home": pl.Int64, "away": pl.Int64,
            "round": pl.Utf8, "hg": pl.Int64, "ag": pl.Int64,
        },
    )  # fmt: skip
    df = df.filter(pl.col("round").str.starts_with(REGULAR_SEASON))
    known = pl.col("hg").is_not_null() & pl.col("ag").is_not_null()
    unscorable = df.filter(~known).with_columns(reason=pl.lit("90' score unknown"))
    return df.filter(known).sort("kickoff", "match_id"), unscorable


def load_xg(session: Session, league_ids: list[int]) -> pl.DataFrame:
    """Match xG per side, only where the league-season has `has_xg` available or partial and
    both teams have a value (the capability table's gate; absent, never imputed)."""
    M, S, C = models.Match, models.Season, models.Competition
    T, K = models.TeamMatchStat, models.CompetitionSourceCapability
    rows = session.execute(
        select(M.match_id, M.home_team_id, M.away_team_id, T.team_id, T.xg)
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .join(K, (K.competition_id == C.competition_id) & (K.season_id == S.season_id))
        .join(T, T.match_id == M.match_id)
        .where(
            C.source_id == SOURCE_ID,
            C.source_ref.in_([str(i) for i in league_ids]),
            K.capability == "has_xg",
            K.valid_to.is_(None),
            K.status.in_(("available", "partial")),
            T.superseded_at.is_(None),
            T.xg.is_not(None),
        )
    ).all()  # fmt: skip
    sides: dict[int, dict[str, float]] = {}
    for match_id, home, away, team, xg in rows:
        side = "xg_home" if team == home else "xg_away" if team == away else None
        if side is not None and xg is not None:
            sides.setdefault(match_id, {})[side] = float(xg)
    return pl.DataFrame(
        [{"match_id": m, **v} for m, v in sides.items() if len(v) == 2],
        schema={"match_id": pl.Int64, "xg_home": pl.Float64, "xg_away": pl.Float64},
    )


def load_coaches(session: Session, league_ids: list[int]) -> pl.DataFrame:
    """The coach in each side's lineup per match (D12), as coach_home / coach_away refs; a
    side with no coach row is null (carried forward by the model, never imputed)."""
    M, S, C, K = models.Match, models.Season, models.Competition, models.MatchCoach
    rows = session.execute(
        select(M.match_id, M.home_team_id, M.away_team_id, K.team_id, K.coach_ref)
        .join(S, S.season_id == M.season_id)
        .join(C, C.competition_id == S.competition_id)
        .join(K, K.match_id == M.match_id)
        .where(
            C.source_id == SOURCE_ID,
            C.source_ref.in_([str(i) for i in league_ids]),
            K.superseded_at.is_(None),
        )
    ).all()  # fmt: skip
    sides: dict[int, dict[str, str]] = {}
    for match_id, home, away, team, coach in rows:
        side = "coach_home" if team == home else "coach_away" if team == away else None
        if side is not None:
            sides.setdefault(match_id, {})[side] = str(coach)
    return pl.DataFrame(
        [{"match_id": m, "coach_home": v.get("coach_home"), "coach_away": v.get("coach_away")}
         for m, v in sides.items()],
        schema={"match_id": pl.Int64, "coach_home": pl.Utf8, "coach_away": pl.Utf8},
    )  # fmt: skip


# --------------------------------------------------------------------------- models


@dataclass
class Window:
    """Training data observable at one as-of time, as numpy arrays."""

    home: NDArray[np.int64]
    away: NDArray[np.int64]
    league: NDArray[np.int64]
    hg: NDArray[np.int64]
    ag: NDArray[np.int64]
    age_days: Arr
    team_ids: NDArray[np.int64]  # index -> team id


def _weights(age_days: Arr, half_life: float | None) -> Arr:
    if half_life is None:
        return np.ones_like(age_days)
    return np.asarray(np.exp(-np.log(2) * age_days / half_life))


def _league_rates(w: Window, weights: Arr, n_leagues: int) -> tuple[Arr, Arr]:
    tot = np.bincount(w.league, weights, n_leagues)
    tot = np.where(tot > 0, tot, 1.0)
    return (
        np.bincount(w.league, weights * w.hg, n_leagues) / tot,
        np.bincount(w.league, weights * w.ag, n_leagues) / tot,
    )


def _base_rate(w: Window, league: int, max_goals: int) -> dict[str, Arr]:
    sel = w.league == league
    hg, ag = w.hg[sel], w.ag[sel]
    n = max(len(hg), 1)
    res = np.array([(hg > ag).sum(), (hg == ag).sum(), (hg < ag).sum()], dtype=float)
    # add-one smoothing keeps every probability positive (a zero would make log loss infinite)
    p1x2 = (res + 1) / (n + 3)
    over = ((hg + ag > 2).sum() + 1) / (n + 2)
    btts = (((hg > 0) & (ag > 0)).sum() + 1) / (n + 2)
    total = np.bincount(np.minimum(hg + ag, 2 * max_goals), minlength=2 * max_goals + 1) + 0.5
    return {
        "1X2": p1x2,
        "OU2.5": np.array([over, 1 - over]),
        "BTTS": np.array([btts, 1 - btts]),
        "total": total / total.sum(),
    }


# --------------------------------------------------------------------------- walk-forward


def as_of_times(first: datetime, last: datetime, every_days: int) -> list[datetime]:
    """Mondays 00:00 UTC covering [first, last]."""
    start = (first - timedelta(days=first.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    out, t = [], start
    while t <= last:
        out.append(t)
        t += timedelta(days=every_days)
    return out


GRIDS = ("weekly", "results")


def week_start(t: datetime) -> datetime:
    """Monday 00:00 UTC of t's week: the weekly refit's as_of."""
    t = t.astimezone(UTC)
    return (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def stamp(as_of: datetime) -> str:
    """Storage stamp of a refit time: the date for midnight (Mondays, as before D33), date
    and time otherwise (results triggers)."""
    a = as_of.astimezone(UTC)
    return f"{a:%Y%m%d}" if (a.hour, a.minute, a.second) == (0, 0, 0) else f"{a:%Y%m%dT%H%M%S}"


TRIGGER_LEAD = timedelta(hours=26)  # the T-24h milestone, plus time for the refit to run


def refit_triggers(fixtures: pl.DataFrame, lag_hours: float) -> list[datetime]:
    """Results-triggered refit times (D33, docs/PHASE1B_TASKS.md section 7b), the same rule
    in production and in the backtest.

    `fixtures`: one bundle's matches (kickoff, home, away), played or scheduled; cancelled
    and postponed ones left out. For each UTC day D with matches, t = the last kickoff on D
    + the result lag. A team is pending when its latest result is newer than the latest
    refit (this week's Monday 00:00 or an earlier trigger) and its next match kicks off
    after t and before the next Monday. A refit as of t is triggered when a team is pending
    and waiting for the next day's t would come later than TRIGGER_LEAD before a pending
    team's next kickoff. So the results of a midweek block are refitted once, after its
    last day, still in time for the next forecasts from T-24h on, and a weekend triggers
    nothing (Monday covers it). The rule reads kickoff times only, and every match on or
    before D kicked off at least `lag_hours` before t."""
    lag = timedelta(hours=lag_hours)
    by_day: dict[Any, list[tuple[datetime, int, int]]] = {}
    kicks: dict[int, list[datetime]] = {}
    for k, h, a in fixtures.select("kickoff", "home", "away").sort("kickoff").iter_rows():
        by_day.setdefault(k.date(), []).append((k, int(h), int(a)))
        kicks.setdefault(int(h), []).append(k)
        kicks.setdefault(int(a), []).append(k)
    days = sorted(by_day)
    ends = [max(k for k, _, _ in by_day[d]) + lag for d in days]
    last_played: dict[int, datetime] = {}
    last_trigger: datetime | None = None
    out = []
    for i, d in enumerate(days):
        for k, h, a in by_day[d]:
            last_played[h] = last_played[a] = k
        t = ends[i]
        monday = week_start(t)
        if t == monday:
            continue  # the Monday refit itself
        refit = monday if last_trigger is None or last_trigger < monday else last_trigger
        upcoming = [
            nk for team, played in last_played.items() if played + lag > refit
            for nk in kicks[team][bisect.bisect_right(kicks[team], t):][:1]
            if nk < monday + timedelta(days=7)
        ]  # fmt: skip
        if not upcoming:
            continue
        wait_until = ends[i + 1] if i + 1 < len(days) else None
        if wait_until is None or wait_until > min(upcoming) - TRIGGER_LEAD:
            out.append(t)
            last_trigger = t
    return out


def grid_times(df: pl.DataFrame, first: datetime, last: datetime, every_days: int,
               grid: str = "weekly", lag_hours: float = 3.0) -> list[datetime]:  # fmt: skip
    """Refit times covering [first, last]: Mondays every `every_days`, plus the results
    triggers (`refit_triggers`) on the "results" grid (weekly cadence only)."""
    if grid not in GRIDS:
        raise ValueError(f"grid {grid!r} not in {GRIDS}")
    weekly = as_of_times(first, last, every_days)
    if grid == "weekly":
        return weekly
    if every_days != 7:
        raise ValueError("the results grid adds triggers to the weekly (7-day) grid")
    trig = [t for t in refit_triggers(df, lag_hours) if weekly[0] < t <= last]
    return sorted(set(weekly) | set(trig))


def mover_codes(df: pl.DataFrame, leagues: list[int]) -> tuple[NDArray[np.int64], ...]:
    """D16 status of each match's home and away team: 1 = first season after promotion into
    this league's tier, 2 = after relegation, 0 = otherwise (same tier, or new to the bundle).
    Tier = position in the bundle's league list. Uses only the team's tier in the previous
    season (complete before this season starts) and this fixture's own league, so it is
    observable at any as-of time before kickoff."""
    tier = {lid: i for i, lid in enumerate(leagues)}
    seen = pl.concat([df.select(team="home", season="season", league_id="league_id"),
                      df.select(team="away", season="season", league_id="league_id")])  # fmt: skip
    # a team's tier in a season = its tier in most of that season's matches
    prev = (seen.group_by("team", "season").agg(pl.col("league_id").mode().first())
            .select("team", season=pl.col("season") + 1,
                    prev_tier=pl.col("league_id").replace_strict(tier)))  # fmt: skip
    cur = pl.col("league_id").replace_strict(tier)
    out = []
    for side in ("home", "away"):
        j = (
            df.select("season", "league_id", team=pl.col(side))
            .with_row_index("row")
            .join(prev, on=["team", "season"], how="left")
            .sort("row")  # joins don't promise row order
        )
        code = (pl.when(pl.col("prev_tier").is_null()).then(0)
                .when(cur < pl.col("prev_tier")).then(1)
                .when(cur > pl.col("prev_tier")).then(2).otherwise(0))  # fmt: skip
        out.append(j.select(code.alias("c"))["c"].to_numpy().astype(np.int64))
    return out[0], out[1]


def refit_plan(
    df: pl.DataFrame,
    cfg: EvalConfig,
    seasons: list[int],
    every_days: int,
    holdout: HoldoutKey | None = None,
    grid: str = "weekly",
) -> list[tuple[datetime, NDArray[np.int64], NDArray[np.int64]]]:
    """The shared walk-forward grid: (as_of, target row indices, training row indices) for
    every refit with at least one target and one observable training match. Every model
    (1a, 1b) is scored on this plan, so their (match, as_of) pairs are identical. Targets
    of a refit kick off from its as_of until the next refit time (`grid_times`).
    Holdout seasons need a HoldoutKey, holdout seasons only and the weekly cadence (D11)."""
    locked = set(seasons) & set(cfg.holdout_seasons)
    if locked and holdout is None:
        raise HoldoutLocked(f"seasons {sorted(locked)} are the locked holdout")
    if holdout is not None:
        if not locked or set(seasons) - set(cfg.holdout_seasons):
            raise HoldoutLocked(f"a holdout run scores holdout seasons only, not {seasons}")
        if every_days != cfg.refit_every_days or grid != "weekly":
            raise HoldoutLocked(
                f"the holdout uses the {cfg.refit_every_days}-day weekly grid, not "
                f"{every_days} days on the {grid} grid"
            )
    # whole seconds, so the comparisons are exact and agree with `prepare`'s datetimes
    kick = np.array([round(t.timestamp()) for t in df["kickoff"].to_list()], dtype=np.int64)
    targets_mask = np.isin(df["season"].to_numpy(), seasons)
    if not targets_mask.any():
        return []
    lag = round(cfg.result_available_after_hours * 3600)
    lookback = cfg.lookback_days * int(DAY)
    t_first = datetime.fromtimestamp(int(kick[targets_mask].min()), UTC)
    t_last = datetime.fromtimestamp(int(kick[targets_mask].max()), UTC)
    times = grid_times(df, t_first, t_last, every_days, grid, cfg.result_available_after_hours)
    starts = [round(t.timestamp()) for t in times]
    ends = starts[1:] + [starts[-1] + every_days * int(DAY)]
    if grid == "weekly":
        ends = [t + every_days * int(DAY) for t in starts]
    plan = []
    for asof_dt, asof, end in zip(times, starts, ends, strict=True):
        tgt = np.flatnonzero(targets_mask & (kick >= asof) & (kick < end))
        train = np.flatnonzero((kick + lag <= asof) & (kick >= asof - lookback))
        if len(tgt) and len(train):
            # leakage guard: nothing in training was unobservable at the as-of time
            assert (kick[train] + lag <= asof).all(), "training row not observable at as-of"
            plan.append((asof_dt, tgt, train))
    return plan


def walk_forward(
    df: pl.DataFrame, leagues: list[int], cfg: EvalConfig, half_life: float | None,
    seasons: list[int], models_: tuple[str, ...] = MODELS, prior_sd: float | None = None,
    open_sd: float | None = None,
    shift_sd: float | None = None,
    holdout: HoldoutKey | None = None,
) -> pl.DataFrame:  # fmt: skip
    """Predict and score every match of `seasons` in `df` (one bundle). Returns one row per
    match x model."""
    plan = refit_plan(df, cfg, seasons, cfg.refit_every_days, holdout)
    league_idx = {lid: i for i, lid in enumerate(leagues)}
    kick = np.array([t.timestamp() for t in df["kickoff"].to_list()]) / DAY
    home_all, away_all = df["home"].to_numpy(), df["away"].to_numpy()
    lg_all = np.array([league_idx[x] for x in df["league_id"].to_list()], dtype=np.int64)
    hg_all, ag_all = df["hg"].to_numpy(), df["ag"].to_numpy()
    season_all = df["season"].to_numpy()
    mid_all = df["match_id"].to_numpy()
    mh_all, ma_all = mover_codes(df, leagues)
    movers = shift_sd is not None  # D16 probe: Normal(0, shift_sd) prior on the two shifts
    if not plan:
        return pl.DataFrame()
    rows: list[dict[str, Any]] = []
    warm: Arr | None = None
    prev_teams: NDArray[np.int64] | None = None
    for asof_dt, tgt, train in plan:
        asof = asof_dt.timestamp() / DAY
        teams = np.unique(np.concatenate([home_all[train], away_all[train]]))
        tix = {int(t): i for i, t in enumerate(teams)}
        win = Window(
            home=np.array([tix[int(t)] for t in home_all[train]], dtype=np.int64),
            away=np.array([tix[int(t)] for t in away_all[train]], dtype=np.int64),
            league=lg_all[train], hg=hg_all[train], ag=ag_all[train],
            age_days=asof - kick[train], team_ids=teams,
        )  # fmt: skip
        counts = np.bincount(np.concatenate([win.home, win.away]), minlength=len(teams))
        weights = _weights(win.age_days, half_life)
        fitted: dc.Params | None = None
        if "dixon_coles" in models_:
            init = warm if prev_teams is not None and np.array_equal(prev_teams, teams) else None
            data = dc.MatchData(
                home=win.home, away=win.away, league=win.league, hg=win.hg, ag=win.ag,
                weight=weights, n_teams=len(teams), n_leagues=len(leagues),
                ridge=dc.ridge_for(prior_sd),
                open_ridge=0.0 if open_sd is None else 1.0 / (2.0 * open_sd**2),
                mover_home=mh_all[train] if movers else None,
                mover_away=ma_all[train] if movers else None,
                shift_ridge=1.0 / (2.0 * shift_sd**2) if shift_sd else 0.0,
            )  # fmt: skip
            fitted, warm = dc.fit(data, init)
            prev_teams = teams
        rate_h, rate_a = _league_rates(win, weights, len(leagues))
        league_att, league_def = np.zeros(len(leagues)), np.zeros(len(leagues))
        if fitted is not None:
            for li in range(len(leagues)):
                sel = win.league == li
                members = np.unique(np.concatenate([win.home[sel], win.away[sel]]))
                if len(members):
                    league_att[li] = fitted.attack[members].mean()
                    league_def[li] = fitted.defence[members].mean()
        for k in tgt:
            h, a, lg = int(home_all[k]), int(away_all[k]), int(lg_all[k])
            n_h = int(counts[tix[h]]) if h in tix else 0
            n_a = int(counts[tix[a]]) if a in tix else 0
            base = {
                "match_id": int(mid_all[k]), "league_id": leagues[lg],
                "season": int(season_all[k]), "kickoff": datetime.fromtimestamp(kick[k] * DAY, UTC),
                "as_of": asof_dt, "hg": int(hg_all[k]), "ag": int(ag_all[k]),
                "n_train": len(train), "n_prior_home": n_h, "n_prior_away": n_a,
                "cold_start": min(n_h, n_a) < cfg.min_warmup_matches,
                "exp_total": float("nan"),
                "mover_home": int(mh_all[k]), "mover_away": int(ma_all[k]),
                "home": h, "away": a,
                "shift_up": float(fitted.shift[0]) if fitted is not None else 0.0,
                "shift_down": float(fitted.shift[1]) if fitted is not None else 0.0,
            }  # fmt: skip
            for model in models_:
                tail = 0.0
                if model == "dixon_coles":
                    assert fitted is not None
                    ia = np.array([tix.get(h, -1)]), np.array([tix.get(a, -1)])
                    # index -1: a team with no history in the window (typically promoted from
                    # a tier we don't load) gets the average of the league it now plays in,
                    # not the bundle average. Such matches are cold start, reported apart.
                    att = np.append(fitted.attack, league_att[lg])
                    dfn = np.append(fitted.defence, league_def[lg])
                    p = dc.Params(fitted.mu, fitted.mu_league, fitted.home_league, att, dfn,
                                  fitted.rho, fitted.shift)  # fmt: skip
                    mv = (np.array([mh_all[k]]), np.array([ma_all[k]])) if movers else (None, None)
                    lam, mu_ = p.rates(ia[0], ia[1], np.array([lg]), *mv)
                    base["exp_total"] = float(lam[0] + mu_[0])
                    m, tail = dc.score_matrix(float(lam[0]), float(mu_[0]), p.rho, cfg.max_goals)
                    mk = metrics.markets(m)
                elif model == "league_poisson":
                    m, tail = dc.independent_matrix(rate_h[lg], rate_a[lg], cfg.max_goals)
                    mk = metrics.markets(m)
                else:
                    mk = _base_rate(win, lg, cfg.max_goals)
                rows.append(
                    {
                        **base, "model": model, "tail_mass": tail,
                        "p_home": mk["1X2"][0], "p_draw": mk["1X2"][1], "p_away": mk["1X2"][2],
                        "p_over25": mk["OU2.5"][0], "p_btts": mk["BTTS"][0],
                        **metrics.score_markets(mk, int(hg_all[k]), int(ag_all[k])),
                    }
                )  # fmt: skip
    return pl.DataFrame(rows)


# --------------------------------------------------------------------------- summaries

METRICS = ("1X2:log_loss", "1X2:brier", "1X2:rps", "OU2.5:log_loss", "OU2.5:brier",
           "BTTS:log_loss", "BTTS:brier", "total:crps")  # fmt: skip


def summarise(pred: pl.DataFrame, bundle_of: dict[int, str]) -> pl.DataFrame:
    """Long table: model x segment x metric -> n, mean. Cold-start rows are a separate
    segment and excluded from the headline ('all') segments."""
    p = pred.with_columns(
        bundle=pl.col("league_id").replace_strict(bundle_of, return_dtype=pl.Utf8),
        team_level=pl.lit("senior"), confederation=pl.lit("UEFA"),
    )  # fmt: skip
    warm, cold = p.filter(~pl.col("cold_start")), p.filter(pl.col("cold_start"))
    parts = [("all", warm.with_columns(seg=pl.lit("all")))]
    for col in ("bundle", "league_id", "season", "team_level", "confederation"):
        parts.append((col, warm.with_columns(seg=pl.col(col).cast(pl.Utf8))))
    parts.append(("cold_start", cold.with_columns(seg=pl.lit("cold_start"))))
    out = []
    for seg_type, frame in parts:
        if frame.height == 0:
            continue
        out.append(
            frame.group_by("model", "seg")
            .agg(pl.len().alias("n"), *[pl.col(m).mean() for m in METRICS])
            .unpivot(index=["model", "seg", "n"], on=list(METRICS), variable_name="metric")
            .with_columns(segment_type=pl.lit(seg_type))
        )
    return (
        pl.concat(out)
        .select("model", "segment_type", "seg", "metric", "n", "value")
        .sort("segment_type", "seg", "metric", "model")
    )


def paired(pred: pl.DataFrame, metric: str, a: str, b: str) -> dict[str, float]:
    """Mean per-match difference (a - b) with a normal 95% interval; negative favours a."""
    wide = (
        pred.filter(pl.col("model").is_in([a, b]))
        .pivot(on="model", index="match_id", values=metric)
        .drop_nulls()
    )
    d = (wide[a] - wide[b]).to_numpy()
    se = float(d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 1 else float("nan")
    m = float(d.mean())
    return {"n": float(len(d)), "mean_diff": m, "lo": m - 1.96 * se, "hi": m + 1.96 * se}


def reliability(pred: pl.DataFrame, model: str = "dixon_coles", bins: int = 10) -> pl.DataFrame:
    """1X2 calibration: predicted vs observed frequency by probability decile, per outcome."""
    p = pred.filter((pl.col("model") == model) & ~pl.col("cold_start"))
    out = []
    for col, outcome in (("p_home", "home"), ("p_draw", "draw"), ("p_away", "away")):
        hit = {"home": pl.col("hg") > pl.col("ag"), "draw": pl.col("hg") == pl.col("ag"),
               "away": pl.col("hg") < pl.col("ag")}[outcome]  # fmt: skip
        out.append(
            p.with_columns(bin=(pl.col(col) * bins).floor().clip(0, bins - 1), hit=hit)
            .group_by("bin")
            .agg(
                pl.len().alias("n"),
                pl.col(col).mean().alias("predicted"),
                pl.col("hit").mean().alias("observed"),
            )  # fmt: skip
            .with_columns(outcome=pl.lit(outcome))
        )
    return pl.concat(out).sort("outcome", "bin")


def baseline_flags(summary: pl.DataFrame) -> list[str]:
    """Leagues where Dixon-Coles does not beat both baselines on dev 1X2 log loss (§14.4)."""
    s = summary.filter(
        (pl.col("segment_type") == "league_id") & (pl.col("metric") == "1X2:log_loss")
    ).pivot(on="model", index="seg", values="value")
    flags = []
    for row in s.iter_rows(named=True):
        for base in ("league_poisson", "base_rate"):
            if row["dixon_coles"] >= row[base]:
                flags.append(f"league {row['seg']}: dixon_coles {row['dixon_coles']:.4f} "
                             f">= {base} {row[base]:.4f}")  # fmt: skip
    return flags
