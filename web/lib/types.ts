// Mirrors adatta/api/schemas.py. Every probability is a Prob with its interval, except the
// C2 baseline model's point forecasts (D54): lo and hi null, shown without an interval.

export type Status = "ok" | "limited" | "stale" | "missing" | "not_modelled";

export interface Prob { p: number; lo: number | null; hi: number | null; point?: boolean }

export interface Selection { market: string; line: number | null; selection: string; prob: Prob }

export interface Component { key: string; label: string; status: Status; value: string; detail: string }

export interface Capability { capability: string; status: string; coverage: number | null }

export interface Freshness {
  tier: string; bundle: string; model_run_id: number | null; refit_as_of: string | null;
  age_days: number | null; gates_pass: boolean | null; stale: boolean;
}

export interface Headline {
  milestone: string; home: Prob; draw: Prob; away: Prob;
  over25: Prob | null; btts: Prob | null; // null where withheld (internationals)
}

/** The bookmakers' latest de-margined prices beside an upcoming forecast: never a model input. */
/** One selection's actual bookmaker decimal odds (margin included): their mean and the best. */
export interface MarketPrice { mean: number; best: number }

export interface MarketLine {
  home: number; draw: number; away: number; over25: number | null; btts: number | null; bookmakers: number;
  prices: Record<string, MarketPrice>; // home, draw, away, over, under, yes, no
}

export interface FixtureRow {
  match_id: number; kickoff: string; round: string | null; home: string; away: string;
  team_level: string; headline: Headline | null; no_projection_reason: string | null;
  confidence: Component[]; market?: MarketLine | null;
}

export interface CompetitionGroup {
  competition_id: number; name: string; country: string; level: string; tier: string;
  capabilities: Capability[]; freshness: Freshness; fixtures: FixtureRow[];
}

export interface FixtureList { generated_at: string; start: string; end: string; competitions: CompetitionGroup[] }

export interface Weather {
  observed_at: string; lead_hours: number; temperature_c: number | null;
  precipitation_mm: number | null; wind_speed_kmh: number | null;
}

export interface Header {
  match_id: number; competition: string; competition_id: number; competition_tier: string; round: string | null;
  kickoff: string; status: string; home: string; away: string; team_level: string; venue: string | null;
  weather: Weather | null; referee: string | null; capabilities: Capability[];
  result: Result | null; // finished matches: the 90-minute score
}

export interface Result { home_goals: number; away_goals: number }

export interface Milestone {
  milestone: string; generated_at: string; projection_run_id: number;
  model_run_id: number | null; refit_as_of: string | null; selections: Selection[];
}

export interface Term { term: string; mean: number; lo: number; hi: number }

export interface Decomposition {
  side: "home" | "away"; team: string; cold_start: boolean; terms: Term[]; log_rate: Term; goals: Prob;
}

export interface ScoreMatrix { max_shown: number; cells: Prob[][]; rest: Prob; tail_mass: number; max_goals: number }

export interface Coherence { milestone: string; coherent: boolean; max_abs_diff: number }

export interface Unavailable { panel: string; reason: string }

export interface SquadPlayer { name: string; number: number | null; pos: string | null }
export interface Lineup {
  observed_at: string; formation: string | null; coach: string | null; starters: SquadPlayer[]; bench: SquadPlayer[];
  pitch_formation: string | null; positional: boolean; pitch: PitchPlayer[];
}
export interface Absence { player_id: number; name: string; status: "out" | "doubtful" | string; reason: string | null; since: string }
export interface ProjectedPlayer {
  name: string; pos: string; p_start: number | null; p_bench: number | null; emin: number | null;
  status: string | null; status_as_of: string | null; section: "xi" | "bench" | "out" | "squad";
  slot: string | null; moved: boolean; // XI: its code on the projected pitch; placed in a vacated slot
}
export interface Projection { generated_at: string; as_of: string; refit_as_of: string }
export interface FormationOption { formation: string; p: number }
export interface PitchPlayer {
  name: string; code: string; row: number; col: number; p_start: number | null; status: string | null;
  moved: boolean; usual: string | null; // projected XI: placed in a vacated slot; usual position if not this one
}
export interface FormationView {
  generated_at: string; as_of: string; refit_as_of: string;
  options: FormationOption[]; announced: string | null; announced_p: number | null; pitch_formation: string;
  last_kickoff: string | null; positional: boolean; pitch: PitchPlayer[]; moved: number;
}
export interface LineupScore {
  xi_hits: number; xi_hits_raw: number; bench_n: number; bench_hits: number; bench_in_squad: number;
  formation: string | null; formation_p: number | null; formation_top: boolean | null;
  slot_acc: number | null; slot_acc_last: number | null;
}
export interface TeamSquad {
  team: string; lineup: Lineup | null; absences: Absence[]; projected: ProjectedPlayer[];
  bench_size: number | null; bench_limit: number | null; formation?: FormationView | null; outcome: LineupScore | null;
}
export interface Squad {
  as_of: string; status_checked_at: string | null; lineup_note: string | null; hidden_reason: string | null;
  projection: Projection | null;
  home: TeamSquad; away: TeamSquad;
}

export interface MatchView {
  generated_at: string; header: Header; freshness: Freshness; confidence: Component[];
  milestones: Milestone[]; matrix: ScoreMatrix | null; decomposition: Decomposition[];
  coherence: Coherence | null; no_projection_reason: string | null; squad: Squad | null; unavailable: Unavailable[];
  qualify?: Qualify | null;
  half_coherence?: Coherence | null; half_reason?: string | null; // P3b-2 half markets
  market?: MarketLine | null;
}

export interface QualifyLeg { match_id: number; home: string; away: string; kickoff: string; score: string | null }

/** Stage D v2: a knockout tie's P(advance) with its path (path keys home_90, home_et, home_pens, away_...). */
export interface Qualify {
  milestone: string; generated_at: string; label: string; stage: string;
  home: Prob; away: Prob; path: Record<string, Prob>; legs: QualifyLeg[]; round: string;
}

export interface TournamentEntry { edition: string; name: string; as_of: string }
export interface TournamentList { tournaments: TournamentEntry[] }

/** [probability, Monte Carlo SE] */
export type McProb = [number, number];
export interface TournamentTeam { team_id: number; name: string; position: McProb[]; advance: McProb; reach: Record<string, McProb>; win: McProb }
export interface TournamentView {
  edition: string; name: string; as_of: string; refit_as_of: string; model_run_id: number | null; shadow: boolean;
  label: string; n_sims: number; mc_se_max: number; finals_fixed: number; rounds: string[];
  groups: { group: string; teams: TournamentTeam[] }[]; random_groups: number; random_thirds: number;
}

export interface PlayedScores {
  log_loss_1x2: number; rps_1x2: number; brier_1x2: number;
  log_loss_ou25: number | null; log_loss_btts: number | null;
}

/** The bookmakers' de-margined view, last price before kickoff (evaluation only, principle 1). */
export interface PlayedMarket {
  home: number; draw: number; away: number; over25: number | null; btts: number | null;
  bookmakers: number; prices: Record<string, MarketPrice>; scores: PlayedScores;
}

/** Closing-line value inputs: d = our first 1X2 − the market then, m = closing − that market. */
export interface PlayedClv { moved: number; gap: number; bookmakers: number }
export interface ClvSummary {
  n: number; n_moved: number;
  toward: number | null; toward_lo: number | null; toward_hi: number | null;
  closed: number | null; closed_lo: number | null; closed_hi: number | null;
}
export interface ReliabilityBin { lo: number; hi: number; n: number; predicted: number; observed: number }
export interface TrendPoint { day: string; n: number; ours: number; market: number; diff: number; diff_lo: number | null; diff_hi: number | null }

export interface PlayedMatch {
  match_id: number; kickoff: string; competition_id: number; competition: string; competition_tier: string; team_level: string;
  home: string; away: string; home_goals: number; away_goals: number;
  outcome: "home" | "draw" | "away"; milestone: string; generated_at: string;
  home_p: Prob; draw_p: Prob; away_p: Prob; over25: Prob | null; btts: Prob | null;
  scores: PlayedScores; market: PlayedMarket | null; clv: PlayedClv | null;
  home_lineup: LineupScore | null; away_lineup: LineupScore | null;
}

export interface MeanScore { metric: string; n: number; mean: number; lo: number | null; hi: number | null } // no interval below two matches

export interface LineupSummary {
  n: number; xi_mean: number; xi_raw_mean: number; bench_hits: number; bench_n: number;
  n_formation: number; formation_top: number | null;
  n_slot: number; slot_acc: number | null; slot_acc_last: number | null;
}
export interface ScoreSummary {
  segment: string; competition_id: number | null; tier: string | null; n: number; scores: MeanScore[];
  market_scores: MeanScore[]; vs_market: MeanScore[]; // ours minus the market's, paired (< 0: ours better)
  clv: ClvSummary | null;
  lineup: LineupSummary | null;
}

export interface PlayedList {
  generated_at: string; forecasts_since: string | null; start: string | null; end: string;
  uniform_log_loss_1x2: number; summary: ScoreSummary[]; reliability: ReliabilityBin[]; trend: TrendPoint[];
  matches: PlayedMatch[];
}

// In play (docs/DASHBOARD_TASKS.md, option 1): live score beside the pre-match projection.
export interface LiveForecast {
  milestone: string; generated_at: string; home: Prob; draw: Prob; away: Prob; over25: Prob | null; btts: Prob | null;
}
export interface LiveMatch {
  team_level: string;
  match_id: number; kickoff: string; competition_id: number; competition: string; competition_tier: string;
  home: string; away: string; status: string; elapsed: number | null; extra: number | null;
  home_goals: number | null; away_goals: number | null; forecast: LiveForecast | null;
  market: MarketLine | null; // the last price before kickoff
}
export interface LiveList { generated_at: string; fetched_at: string | null; note: string | null; matches: LiveMatch[] }
