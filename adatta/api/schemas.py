"""Response shapes of the dashboard API (docs/DASHBOARD_TASKS.md). Every probability is a
`Prob`, which cannot exist without its interval (spec §16.1: dropping uncertainty is a bug)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, model_validator

Status = Literal["ok", "limited", "stale", "missing", "not_modelled"]


class Prob(BaseModel):
    """A probability with its 90% interval. Only an explicit point forecast (the C2
    baseline model, D54 choice 1a: `point`) has none; any other Prob without one is
    refused."""

    p: float  # posterior mean, or the point forecast
    lo: float | None = None  # 5th percentile of the per-draw probability
    hi: float | None = None  # 95th percentile
    point: bool = False

    @model_validator(mode="after")
    def _interval(self) -> Prob:
        if self.point != (self.lo is None and self.hi is None) or (self.lo is None) != (
            self.hi is None
        ):
            raise ValueError("a probability carries its interval unless it is a point forecast")
        return self


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


class MarketLine(BaseModel):
    """The market beside an upcoming forecast, de-margined from each bookmaker's latest
    price: information only, never an edge flag and never a model input (principle 1)."""

    home: float
    draw: float
    away: float
    over25: float | None
    btts: float | None
    bookmakers: int


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
    market: MarketLine | None = None  # information only, no edge flag (owner, 2026-11-27)


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
    team_level: str  # shown with fair odds at every level (D60)
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
    pitch_formation: str | None = None  # from the grid, as the lineup table's formations
    positional: bool = False  # slot codes (the league-season passes the grid test), else lines
    pitch: list[PitchPlayer] = []  # empty if the grid is not a complete XI


class Absence(BaseModel):
    player_id: int  # names aren't unique: a team can list two "B. Traoré"
    name: str
    status: str  # "out" (missing) or "doubtful" (questionable)
    reason: str | None
    since: datetime  # the spell's start: when the feed first listed it


class ProjectedPlayer(BaseModel):
    """P3-b's projection for one pool player (no injury input: `status` is the feed's, shown
    beside it, not used by the model), or a registered squad player outside the pool (no
    numbers). The section is a display rule on P3-b's numbers: a player listed as out at
    the projection's as-of time is never in the XI or bench."""

    name: str
    pos: str
    p_start: float | None  # None: registered, not in the pool (not modelled)
    p_bench: float | None
    emin: float | None  # expected minutes overall
    status: str | None  # "out" / "doubtful" from the injury feed now, else None
    status_as_of: str | None = None  # the same at the projection's as-of time
    section: Literal["xi", "bench", "out", "squad"] = "xi"
    slot: str | None = None  # XI: the code of his slot on the projected pitch
    moved: bool = False  # XI: placed in a vacated slot of the last lineup (◆ on the pitch)


class Projection(BaseModel):
    generated_at: datetime
    as_of: datetime
    refit_as_of: datetime


class FormationOption(BaseModel):
    formation: str  # "other" for the formations outside P3-c's vocabulary
    p: float


class PitchPlayer(BaseModel):
    """One player on a pitch: the projected XI placed in the last lineup in the projected
    formation, or an announced starter."""

    name: str
    code: str  # slot code (LB, LCB...) if positional, else the line (G / D / M / F)
    row: int  # provider grid row, 1 = goalkeeper
    col: int
    p_start: float | None  # P3-b's, if in this fixture's projected pool
    status: str | None  # "out" / "doubtful" from the injury feed
    moved: bool = False  # projected XI: replaces a player of the last lineup in it
    usual: str | None = None  # projected XI: his usual slot (or line) if not this one


class FormationView(BaseModel):
    """P3-h: P3-c's three most likely formations, and the projected XI placed in the team's
    last lineup in the most likely named one as of the projection time."""

    generated_at: datetime
    as_of: datetime
    refit_as_of: datetime
    options: list[FormationOption]
    announced: str | None = None  # the formation of the announced lineup (from its grid)
    announced_p: float | None = None  # P3-c's probability for it ("other" if not in its list)
    pitch_formation: str
    last_kickoff: datetime | None  # None: no previous lineup in this formation (empty grid)
    positional: bool
    pitch: list[PitchPlayer]
    moved: int = 0  # projected-XI players placed in a vacated slot


class LineupScore(BaseModel):
    """How one side's projection as of kickoff - 24 h did against the announced lineup."""

    xi_hits: int  # projected XI (out rule applied) who started, of 11
    xi_hits_raw: int  # P3-b's own top XI (keeper + 10 outfield by P(start), no out rule)
    bench_n: int  # projected bench size
    bench_hits: int  # projected bench players named on the bench
    bench_in_squad: int  # projected bench players named at all (XI or bench)
    formation: str | None  # played, from the announced grid
    formation_p: float | None  # P3-c's probability for it ("other" if outside the vocabulary)
    formation_top: bool | None  # it was P3-c's top pick
    # positional grids and the announced formation the pitch's: share of the 11 slots right
    slot_acc: float | None = None  # the projected-XI pitch shown
    slot_acc_last: float | None = None  # the last lineup in that formation, unchanged


class TeamSquad(BaseModel):
    team: str
    lineup: Lineup | None
    absences: list[Absence]
    projected: list[ProjectedPlayer] = []
    bench_size: int | None = None  # projected bench places shown
    bench_limit: int | None = None  # the league's measured limit (None: not known)
    formation: FormationView | None = None
    outcome: LineupScore | None = None  # finished matches with a projection as of kickoff - 24 h


class Squad(BaseModel):
    """Captured availability and lineups as they stood at `as_of` = min(now, kickoff)."""

    as_of: datetime
    status_checked_at: datetime | None  # last successful status capture of the competition
    lineup_note: str | None  # why there is no lineup yet, when there isn't one
    hidden_reason: str | None  # set (and no players listed) for non-senior team levels
    projection: Projection | None = None  # the latest projected lineup run shown
    home: TeamSquad
    away: TeamSquad


class QualifyLeg(BaseModel):
    match_id: int
    home: str
    away: str
    kickoff: datetime
    score: str | None  # the 90' score once final (extra time and penalties after it)


class Qualify(BaseModel):
    """Stage D v2: a knockout tie's P(advance) with its path, from the latest milestone."""

    milestone: str
    generated_at: datetime
    label: str
    stage: str  # before leg 1 | between legs | single match
    home: Prob  # this match's listed home side goes through
    away: Prob
    path: dict[str, Prob]  # home_90 / home_et / home_pens / away_... (sum to 1)
    legs: list[QualifyLeg]
    round: str


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
    qualify: Qualify | None = None  # national knockout ties (Stage D v2)
    # P3b-2: the half markets (HT1X2, HTFT, HTOU) of the latest milestone recompute from
    # its refits; shown only when coherent. Otherwise `half_reason` says why there are none.
    half_coherence: Coherence | None = None
    half_reason: str | None = None
    market: MarketLine | None = None  # information only, no edge flag (owner, 2026-11-27)


class TournamentEntry(BaseModel):
    edition: str
    name: str
    as_of: datetime


class TournamentList(BaseModel):
    tournaments: list[TournamentEntry]


class TournamentTeam(BaseModel):
    team_id: int
    name: str
    position: list[list[float]]  # [p, Monte Carlo SE] per finishing place
    advance: list[float]
    reach: dict[str, list[float]]
    win: list[float]


class TournamentGroup(BaseModel):
    group: str
    teams: list[TournamentTeam]


class TournamentView(BaseModel):
    """Stage D v2: a tournament forecast as served (or a past edition's shadow)."""

    edition: str
    name: str
    as_of: datetime
    refit_as_of: datetime
    model_run_id: int | None
    shadow: bool
    label: str
    n_sims: int
    mc_se_max: float
    finals_fixed: int
    rounds: list[str]
    groups: list[TournamentGroup]
    random_groups: float
    random_thirds: float


class PlayedScores(BaseModel):
    """Proper scores of one forecast against its result (adatta.eval.metrics)."""

    log_loss_1x2: float
    rps_1x2: float
    brier_1x2: float
    log_loss_ou25: float | None
    log_loss_btts: float | None


class PlayedMarket(BaseModel):
    """The bookmakers' view of a played match, for evaluation only (principle 1: odds are
    never a model input). Per bookmaker, the last price observed at or before kickoff for
    every selection of a market, de-margined proportionally; averaged over the bookmakers
    quoting the whole market."""

    home: float
    draw: float
    away: float
    over25: float | None  # P(over 2.5)
    btts: float | None  # P(both teams score)
    bookmakers: int  # quoting the whole 1X2
    scores: PlayedScores


class PlayedClv(BaseModel):
    """Closing-line value inputs of one played match (adatta.api.market.clv_inputs): with
    d = our first 1X2 - the market when it was published, m = the closing market - that
    opening market: moved = d.m (> 0: towards us), gap = d.d."""

    moved: float
    gap: float
    bookmakers: int


class ClvSummary(BaseModel):
    """The market's movement towards our first forecasts over a segment."""

    n: int  # matches with an opening and a later closing market
    n_moved: int  # of them, where the market moved relative to our forecast
    toward: float | None  # share of n_moved where it moved towards us
    toward_lo: float | None
    toward_hi: float | None
    closed: float | None  # share of our disagreement closed by kickoff: sum(d.m) / sum(d.d)
    closed_lo: float | None  # bootstrap 95% interval over matches
    closed_hi: float | None


class ReliabilityBin(BaseModel):
    """1X2 calibration: every outcome's forecast probability, pooled, in tenths."""

    lo: float
    hi: float
    n: int
    predicted: float  # mean forecast probability in the bin
    observed: float  # share of those outcomes that happened


class TrendPoint(BaseModel):
    """The running 1X2 log loss gap to the market, by kickoff day (cumulative)."""

    day: str  # YYYY-MM-DD, UTC
    n: int  # matches so far with a market
    ours: float
    market: float
    diff: float  # ours minus the market's (< 0: ours better)
    diff_lo: float | None
    diff_hi: float | None


class PlayedMatch(BaseModel):
    match_id: int
    kickoff: datetime
    competition_id: int
    competition: str
    competition_tier: str
    team_level: str  # shown with fair odds at every level (D60)
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
    market: PlayedMarket | None = None  # None without a pre-kickoff 1X2 price
    clv: PlayedClv | None = None  # None without an opening and a later closing market
    home_lineup: LineupScore | None = None  # how the projection as of kickoff - 24 h did
    away_lineup: LineupScore | None = None


class MeanScore(BaseModel):
    metric: str
    n: int
    mean: float
    lo: float | None  # 95% interval of the mean; None with fewer than two matches
    hi: float | None


class LineupSummary(BaseModel):
    """Projected lineups as of kickoff - 24 h over a segment's matches (reported, not a gate)."""

    n: int  # team-matches with a stored projection and an announced lineup
    xi_mean: float  # mean projected starters who started, of 11
    xi_raw_mean: float  # the same for P3-b's own top XI without the out rule
    bench_hits: int
    bench_n: int
    n_formation: int  # team-matches with a formation projection and an announced grid
    formation_top: float | None  # share where the played formation was P3-c's top pick
    n_slot: int = 0  # team-matches with slot accuracy (positional, formation as the pitch)
    slot_acc: float | None = None  # mean slot accuracy of the projected-XI pitch shown
    slot_acc_last: float | None = None  # the same for the last lineup unchanged


class ScoreSummary(BaseModel):
    segment: str  # "all" or a competition name
    competition_id: int | None
    tier: str | None  # the competition's tier; None for "all"
    n: int
    scores: list[MeanScore]
    market_scores: list[MeanScore] = []  # the market's, over the matches it priced
    vs_market: list[MeanScore] = []  # ours minus the market's, paired by match (< 0: ours better)
    clv: ClvSummary | None = None
    lineup: LineupSummary | None = None


class PlayedList(BaseModel):
    generated_at: datetime
    forecasts_since: datetime | None  # first projection run; nothing earlier was forecast
    start: datetime | None
    end: datetime
    uniform_log_loss_1x2: float  # ln 3: a forecast of 1/3 each
    summary: list[ScoreSummary]
    reliability: list[ReliabilityBin] = []
    trend: list[TrendPoint] = []
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
    team_level: str  # shown with fair odds at every level (D60)
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
