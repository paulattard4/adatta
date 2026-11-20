"""Response shapes of the dashboard API (docs/DASHBOARD_TASKS.md). Every probability is a
`Prob`, which cannot exist without its interval (spec §16.1: dropping uncertainty is a bug)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Status = Literal["ok", "limited", "stale", "missing", "not_modelled"]


class Prob(BaseModel):
    p: float  # posterior mean
    lo: float  # 5th percentile of the per-draw probability
    hi: float  # 95th percentile


class Selection(BaseModel):
    market: str  # 1X2 / OU / BTTS
    line: float | None
    selection: str
    prob: Prob


class Component(BaseModel):
    """One named confidence component (§13.2); never collapsed into a score."""

    key: str
    label: str
    status: Status
    value: str
    detail: str


class Capability(BaseModel):
    capability: str
    status: str  # available / partial / absent / unknown
    coverage: float | None


class Freshness(BaseModel):
    tier: str  # refit tier (§12.1); every bundle is weekly for now (D11)
    bundle: str
    model_run_id: int | None
    refit_as_of: datetime | None
    age_days: float | None
    gates_pass: bool | None
    stale: bool


class Headline(BaseModel):
    milestone: str
    home: Prob
    draw: Prob
    away: Prob


class FixtureRow(BaseModel):
    match_id: int
    kickoff: datetime
    round: str | None
    home: str
    away: str
    team_level: str
    headline: Headline | None
    no_projection_reason: str | None
    confidence: list[Component]


class CompetitionGroup(BaseModel):
    competition_id: int
    name: str
    country: str
    level: str
    tier: str  # "1", "2", "cup", "national" (config/competitions.yml)
    capabilities: list[Capability]
    freshness: Freshness
    fixtures: list[FixtureRow]


class FixtureList(BaseModel):
    generated_at: datetime
    start: datetime
    end: datetime
    competitions: list[CompetitionGroup]


class Weather(BaseModel):
    observed_at: datetime
    lead_hours: float
    temperature_c: float | None
    precipitation_mm: float | None
    wind_speed_kmh: float | None


class Result(BaseModel):
    """A finished match's 90-minute score (the model's target), from the current result."""

    home_goals: int
    away_goals: int


class Header(BaseModel):
    match_id: int
    competition: str
    competition_id: int
    competition_tier: str
    round: str | None
    kickoff: datetime
    status: str
    home: str
    away: str
    team_level: str  # fair odds are shown for senior only (principle 10)
    venue: str | None
    weather: Weather | None
    referee: str | None  # None = not announced yet
    capabilities: list[Capability]
    result: Result | None = None  # finished matches only


class Milestone(BaseModel):
    milestone: str
    generated_at: datetime
    projection_run_id: int
    model_run_id: int | None
    refit_as_of: datetime | None
    selections: list[Selection]


class Term(BaseModel):
    """One additive term of log lambda (§13.1), posterior mean and 90% interval."""

    term: str
    mean: float
    lo: float
    hi: float


class Decomposition(BaseModel):
    side: Literal["home", "away"]
    team: str
    cold_start: bool  # unseen team: league average of the last 52 weeks
    terms: list[Term]
    log_rate: Term  # the sum
    goals: Prob  # lambda itself (expected goals), mean and interval


class ScoreMatrix(BaseModel):
    max_shown: int  # cells 0..max_shown for each side
    cells: list[list[Prob]]  # [home goals][away goals]
    rest: Prob  # every score with a side above max_shown
    tail_mass: float  # mass beyond the max_goals truncation before renormalising (§14.5)
    max_goals: int


class Coherence(BaseModel):
    """§11.2: the matrix shown reproduces the published milestone."""

    milestone: str
    coherent: bool
    max_abs_diff: float


class Unavailable(BaseModel):
    panel: str
    reason: str


class SquadPlayer(BaseModel):
    name: str
    number: int | None
    pos: str | None


class Lineup(BaseModel):
    """As announced: the lineup snapshot first seen before kickoff."""

    observed_at: datetime
    formation: str | None
    coach: str | None
    starters: list[SquadPlayer]
    bench: list[SquadPlayer]


class Absence(BaseModel):
    name: str
    status: str  # "out" (missing) or "doubtful" (questionable)
    reason: str | None
    since: datetime  # the spell's start: when the feed first listed it


class TeamSquad(BaseModel):
    team: str
    lineup: Lineup | None
    absences: list[Absence]


class Squad(BaseModel):
    """Captured availability and lineups as they stood at `as_of` = min(now, kickoff)."""

    as_of: datetime
    status_checked_at: datetime | None  # last successful status capture of the competition
    lineup_note: str | None  # why there is no lineup yet, when there isn't one
    hidden_reason: str | None  # set (and no players listed) for non-senior team levels
    home: TeamSquad
    away: TeamSquad


class MatchView(BaseModel):
    generated_at: datetime
    header: Header
    freshness: Freshness
    confidence: list[Component]
    milestones: list[Milestone]  # oldest first; the last is the headline
    matrix: ScoreMatrix | None
    decomposition: list[Decomposition]
    coherence: Coherence | None
    no_projection_reason: str | None
    squad: Squad | None = None
    unavailable: list[Unavailable]


class PlayedScores(BaseModel):
    """Proper scores of one forecast against its result (adatta.eval.metrics)."""

    log_loss_1x2: float
    rps_1x2: float
    brier_1x2: float
    log_loss_ou25: float | None
    log_loss_btts: float | None


class PlayedMatch(BaseModel):
    match_id: int
    kickoff: datetime
    competition_id: int
    competition: str
    competition_tier: str
    team_level: str  # fair odds are shown for senior only (principle 10)
    home: str
    away: str
    home_goals: int
    away_goals: int
    outcome: Literal["home", "draw", "away"]
    milestone: str  # the latest milestone published before kickoff
    generated_at: datetime
    home_p: Prob
    draw_p: Prob
    away_p: Prob
    over25: Prob | None
    btts: Prob | None
    scores: PlayedScores


class MeanScore(BaseModel):
    metric: str
    n: int
    mean: float
    lo: float | None  # 95% interval of the mean; None with fewer than two matches
    hi: float | None


class ScoreSummary(BaseModel):
    segment: str  # "all" or a competition name
    competition_id: int | None
    tier: str | None  # the competition's tier; None for "all"
    n: int
    scores: list[MeanScore]


class PlayedList(BaseModel):
    generated_at: datetime
    forecasts_since: datetime | None  # first projection run; nothing earlier was forecast
    start: datetime | None
    end: datetime
    uniform_log_loss_1x2: float  # ln 3: a forecast of 1/3 each
    summary: list[ScoreSummary]
    matches: list[PlayedMatch]  # newest first


class LiveForecast(BaseModel):
    """The latest projection published before kickoff: not updated in play (spec §20.1)."""

    milestone: str
    generated_at: datetime
    home: Prob
    draw: Prob
    away: Prob


class LiveMatch(BaseModel):
    match_id: int
    kickoff: datetime
    competition_id: int
    competition: str
    competition_tier: str
    team_level: str  # fair odds are shown for senior only (principle 10)
    home: str
    away: str
    status: str  # provider short status: 1H, HT, 2H, ET, BT, P, ...
    elapsed: int | None
    extra: int | None
    home_goals: int | None
    away_goals: int | None
    forecast: LiveForecast | None  # None: no projection was published before kickoff


class LiveList(BaseModel):
    generated_at: datetime
    fetched_at: datetime | None  # when the live state was read from the provider
    note: str | None  # why it may be stale or missing
    matches: list[LiveMatch]
