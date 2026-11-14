export const pct = (p: number, digits = 1) => (100 * p).toFixed(digits);

export const interval = (lo: number, hi: number, digits = 1) => `${pct(lo, digits)}–${pct(hi, digits)}`;

/** A competition's tier as shown beside its name: "1" -> "tier 1", "cup", "national teams". */
export const tierLabel = (tier: string | null | undefined) =>
  !tier ? "" : /^\d+$/.test(tier) ? `tier ${tier}` : tier === "national" ? "national teams" : tier;

/** The match clock from the provider's live status: 63', 45+2', HT, Pens... */
export function liveClock(status: string, elapsed: number | null, extra: number | null): string {
  const named: Record<string, string> = { HT: "HT", BT: "Break", P: "Pens", SUSP: "Susp.", INT: "Int." };
  if (named[status]) return named[status];
  if (elapsed == null) return status;
  return extra ? `${elapsed}+${extra}'` : `${elapsed}'`;
}

export const timeLocal = (iso: string) =>
  new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

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

/** Whether a selection came in at this 90-minute score (1X2, over/under a line, BTTS). */
export function cameIn(s: { market: string; line: number | null; selection: string }, h: number, a: number): boolean {
  if (s.market === "1X2") return s.selection === (h > a ? "home" : h < a ? "away" : "draw");
  if (s.market === "OU" && s.line != null) return (h + a > s.line) === (s.selection === "over");
  if (s.market === "BTTS") return (h > 0 && a > 0) === (s.selection === "yes");
  return false;
}

/** The other side of a two-way market: 1 − p, the interval flipped. */
export const complement = (x: { p: number; lo: number; hi: number }) => ({ p: 1 - x.p, lo: 1 - x.hi, hi: 1 - x.lo });
