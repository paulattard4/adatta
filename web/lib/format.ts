export const pct = (p: number, digits = 1) => (100 * p).toFixed(digits);

export const interval = (lo: number, hi: number, digits = 1) => `${pct(lo, digits)}–${pct(hi, digits)}`;

/** Fair decimal odds 1/p, no margin. Below p = 0.001 the price says little, so ">1000". */
export const fairOdds = (p: number) => (p < 0.001 ? ">1000" : (1 / p).toFixed(2));

/** The odds interval of a probability interval: 1/p is decreasing, so 1/hi to 1/lo. */
export const oddsInterval = (lo: number, hi: number) => `${fairOdds(hi)}–${fairOdds(lo)}`;

/** Principle 10: no betting-facing output for youth levels without review. */
export const showsOdds = (teamLevel: string) => teamLevel === "senior";

const CAP_LABELS: Record<string, string> = {
  has_lineups: "Lineups", has_player_match_stats: "Player stats", has_xg: "xG",
  has_corners_data: "Corners", has_cards_data: "Cards", has_half_split_stats: "Half-split",
  has_referee_history: "Referees", has_shot_stats: "Shots",
};

export const capLabel = (c: string) => CAP_LABELS[c];

export const kickoffLocal = (iso: string) =>
  new Date(iso).toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });

export const dateLocal = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

export function countdown(iso: string, now: number): string {
  const s = Math.round((new Date(iso).getTime() - now) / 1000);
  if (s <= 0) return "kicked off";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d > 0 ? `${d}d ${h}h ${m}m` : `${h}h ${m}m ${s % 60}s`;
}

export const MARKET_LABEL: Record<string, string> = {
  "1X2": "Match result", OU: "Total goals", BTTS: "Both teams score",
};
