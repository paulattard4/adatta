// Mirrors adatta/api/schemas.py. Every probability is a Prob: never without its interval.

export type Status = "ok" | "limited" | "stale" | "missing" | "not_modelled";

export interface Prob { p: number; lo: number; hi: number }

export interface Selection { market: string; line: number | null; selection: string; prob: Prob }

export interface Component { key: string; label: string; status: Status; value: string; detail: string }

export interface Capability { capability: string; status: string; coverage: number | null }

export interface Freshness {
  tier: string; bundle: string; model_run_id: number | null; refit_as_of: string | null;
  age_days: number | null; gates_pass: boolean | null; stale: boolean;
}

export interface Headline { milestone: string; home: Prob; draw: Prob; away: Prob }

export interface FixtureRow {
  match_id: number; kickoff: string; round: string | null; home: string; away: string;
  team_level: string; headline: Headline | null; no_projection_reason: string | null;
  confidence: Component[];
}

export interface CompetitionGroup {
  competition_id: number; name: string; country: string; level: string;
  capabilities: Capability[]; freshness: Freshness; fixtures: FixtureRow[];
}

export interface FixtureList { generated_at: string; start: string; end: string; competitions: CompetitionGroup[] }

export interface Weather {
  observed_at: string; lead_hours: number; temperature_c: number | null;
  precipitation_mm: number | null; wind_speed_kmh: number | null;
}

export interface Header {
  match_id: number; competition: string; competition_id: number; round: string | null;
  kickoff: string; status: string; home: string; away: string; venue: string | null;
  weather: Weather | null; referee: string | null; capabilities: Capability[];
}

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

export interface MatchView {
  generated_at: string; header: Header; freshness: Freshness; confidence: Component[];
  milestones: Milestone[]; matrix: ScoreMatrix | null; decomposition: Decomposition[];
  coherence: Coherence | null; no_projection_reason: string | null; unavailable: Unavailable[];
}

export interface PlayedScores {
  log_loss_1x2: number; rps_1x2: number; brier_1x2: number;
  log_loss_ou25: number | null; log_loss_btts: number | null;
}

export interface PlayedMatch {
  match_id: number; kickoff: string; competition_id: number; competition: string;
  home: string; away: string; home_goals: number; away_goals: number;
  outcome: "home" | "draw" | "away"; milestone: string; generated_at: string;
  home_p: Prob; draw_p: Prob; away_p: Prob; over25: Prob | null; btts: Prob | null;
  scores: PlayedScores;
}

export interface MeanScore { metric: string; n: number; mean: number; lo: number; hi: number }

export interface ScoreSummary { segment: string; competition_id: number | null; n: number; scores: MeanScore[] }

export interface PlayedList {
  generated_at: string; forecasts_since: string | null; start: string | null; end: string;
  uniform_log_loss_1x2: number; summary: ScoreSummary[]; matches: PlayedMatch[];
}
