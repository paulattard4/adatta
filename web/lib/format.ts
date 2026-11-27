import type { MarketPrice, Prob } from "@/lib/types";

export const pct = (p: number, digits = 1) => (100 * p).toFixed(digits);

/** A 90% interval as "lo–hi" in %; empty for a point forecast (the C2 baseline, D54). */
export const interval = (lo: number | null, hi: number | null, digits = 1) =>
  lo == null || hi == null ? "" : `${pct(lo, digits)}–${pct(hi, digits)}`;

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
export const oddsInterval = (lo: number | null, hi: number | null) =>
  lo == null || hi == null ? "" : `${fairOdds(hi)}–${fairOdds(lo)}`;

/** Principle 10: no betting-facing output for youth levels without review. */

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
  HT1X2: "Half-time result", HTFT: "Half time / full time", HTOU: "Half-time goals",
};

/** The half markets (P3b-2): shown in their own panel, not with the 90-minute markets. */
export const HALF_MARKETS = ["HT1X2", "HTFT", "HTOU"];

/** Whether a selection came in at this 90-minute score (1X2, over/under a line, BTTS). */
export function cameIn(s: { market: string; line: number | null; selection: string }, h: number, a: number): boolean {
  if (s.market === "1X2") return s.selection === (h > a ? "home" : h < a ? "away" : "draw");
  if (s.market === "OU" && s.line != null) return (h + a > s.line) === (s.selection === "over");
  if (s.market === "BTTS") return (h > 0 && a > 0) === (s.selection === "yes");
  return false;
}

/** The other side of a two-way market: 1 − p, the interval flipped. */
export const complement = (x: Prob) =>
  ({ p: 1 - x.p, lo: x.hi == null ? null : 1 - x.hi, hi: x.lo == null ? null : 1 - x.lo });

/** The selection of one market whose probability is furthest above the market's (ours − the
 * de-margined market), if any is above it; null when none is or the market has no price.
 * Display only: odds never reach the model (principle 1). */
export function topGap<K extends string>(ours: Record<K, number>, market: NoInfer<Record<K, number | null | undefined>>):
  { key: K; diff: number; market: number } | null {
  let best: { key: K; diff: number; market: number } | null = null;
  for (const key of Object.keys(ours) as K[]) {
    const m = market[key];
    if (m == null) return null;
    const diff = ours[key] - m;
    if (diff > 0 && (best === null || diff > best.diff)) best = { key, diff, market: m };
  }
  return best;
}

/** The two selections of a yes/no market from the probability of the first. */
export const twoWay = (p: number | null | undefined) => (p == null ? null : { yes: p, no: 1 - p });

/** A gap as signed percentage points: "+3.2". */
export const gapPts = (d: number) => `${d >= 0 ? "+" : "−"}${Math.abs(100 * d).toFixed(1)}`;

export type Gap = { diff: number; market: number };

/** The selection of a yes/no market to show: the one we rate furthest above the market when it
 * has a price (with its gap), else `fallback`. `p` is our probability of yes. */
export function twoWayPick(p: Prob, price: number | null | undefined, fallback: "yes" | "no" = "yes"):
  { side: "yes" | "no"; prob: Prob; gap?: Gap } {
  const both = twoWay(price);
  const g = both ? topGap({ yes: p.p, no: 1 - p.p }, both) : null;
  const side = g ? g.key : fallback;
  return { side, prob: side === "yes" ? p : { ...complement(p), point: p.point }, gap: g ?? undefined };
}

/** Every selection of a market that we rate above the market (ours − the de-margined market
 * > 0), with its gap; a three-way market can have two. Display only (principle 1). */
export function positiveGaps<K extends string>(ours: Record<K, number>,
  market: NoInfer<Record<K, number | null | undefined>> | null | undefined): Partial<Record<K, Gap>> {
  const out: Partial<Record<K, Gap>> = {};
  if (!market) return out;
  for (const key of Object.keys(ours) as K[]) {
    const m = market[key];
    if (m == null) return {};
    if (ours[key] - m > 0) out[key] = { diff: ours[key] - m, market: m };
  }
  return out;
}

/** The market as the API sends it beside a forecast (upcoming, in play or played). */
export type Priced = {
  home: number; draw: number; away: number; over25: number | null; btts: number | null; bookmakers: number;
  prices?: Record<string, MarketPrice>;
};

/** The market beside one selection: its probability with the margin removed, and the same
 * bookmakers' actual odds (margin included). */
export type Mkt = { p: number; price?: MarketPrice; books: number };

export const mktOf = (m: Priced | null | undefined, key: "home" | "draw" | "away"): Mkt | undefined =>
  m ? { p: m[key], price: m.prices?.[key], books: m.bookmakers } : undefined;

const TWO_WAY = {
  ou: { field: "over25", yes: "Over 2.5", no: "Under 2.5", keys: ["over", "under"] },
  btts: { field: "btts", yes: "BTTS Yes", no: "BTTS No", keys: ["yes", "no"] },
} as const;
export type TwoWayKind = keyof typeof TWO_WAY;

/** A yes/no market's cell: the selection we rate above the market (else `fallback`), its
 * label, gap and market. */
export function twoWaySide(kind: TwoWayKind, p: Prob, market: Priced | null | undefined, fallback: "yes" | "no" = "yes") {
  const t = TWO_WAY[kind];
  const price = market?.[t.field];
  const pick = twoWayPick(p, price, fallback);
  const key = pick.side === "yes" ? t.keys[0] : t.keys[1];
  const mkt: Mkt | undefined = market && price != null
    ? { p: pick.side === "yes" ? price : 1 - price, price: market.prices?.[key], books: market.bookmakers } : undefined;
  return { ...pick, label: pick.side === "yes" ? t.yes : t.no, mkt };
}

/** Decimal odds as shown: two decimals. */
export const odds2 = (x: number) => x.toFixed(2);
