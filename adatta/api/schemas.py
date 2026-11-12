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


class Header(BaseModel):
    match_id: int
    competition: str
    competition_id: int
    round: str | None
    kickoff: datetime
    status: str
    home: str
    away: str
    venue: str | None
    weather: Weather | None
    referee: str | None  # None = not announced yet
    capabilities: list[Capability]


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
    lo: float  # 95% interval of the mean
    hi: float


class ScoreSummary(BaseModel):
    segment: str  # "all" or a competition name
    competition_id: int | None
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
