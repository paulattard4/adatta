import type { Prob } from "@/lib/types";
import { fairOdds, gapPts, interval, oddsInterval, pct, twoWayPick, type Gap } from "@/lib/format";

/** A probability with its 90% interval: the number, the range, and a bar showing both.
 * With `odds`, also the fair decimal odds 1/p and their interval (every team level, D60). */
/** The mark on the selection we rate furthest above the market in its market. */
const GapTag = ({ gap, won }: { gap: Gap; won?: boolean }) =>
  <span className={won ? "num prob-gap won" : "num prob-gap"}
        aria-label={`${gapPts(gap.diff)} points above the market${won ? ", came in" : ""}`}>
    ▲{gapPts(gap.diff)}{won && " ✓"}
  </span>;

export function ProbCell({ prob, label, compact = false, odds = false, cameIn = false, gap, won = false }:
  { prob: Prob; label?: string; compact?: boolean; odds?: boolean; cameIn?: boolean; gap?: Gap; won?: boolean }) {
  const point = prob.lo == null || prob.hi == null; // C2 baseline (D54): no interval
  const title = (point ? `${label ?? ""} point forecast, no interval` :
    `${label ?? ""} 90% interval ${interval(prob.lo, prob.hi)}%`) +
    (odds ? `; fair odds ${fairOdds(prob.p)}` + (point ? "" : ` (${oddsInterval(prob.lo, prob.hi)})`) : "") +
    (cameIn ? "; came in" : "") +
    (gap ? `; furthest above the market in this market: ${gapPts(gap.diff)} points (market ${pct(gap.market)}%)` : "");
  return (
    <div className={(compact ? "prob prob-compact" : "prob") + (cameIn ? " came-in" : "") + (gap ? " has-gap" : "")} title={title}>
      {label && <span className="prob-label">{label}</span>}
      <span className="num prob-value">{pct(prob.p)}<span className="unit">%</span></span>
      <span className="num prob-interval">{interval(prob.lo, prob.hi)}</span>
      {odds && (
        <span className={gap ? "num prob-odds with-gap" : "num prob-odds"}>
          <span className="prob-price">
            <span className="prob-odds-at">@</span>{fairOdds(prob.p)}
            <span className="dim"> {oddsInterval(prob.lo, prob.hi)}</span>
          </span>
          {gap && <GapTag gap={gap} won={won} />}
        </span>
      )}
      {gap && !odds && <span className="prob-odds"><GapTag gap={gap} won={won} /></span>}
      <span className="prob-bar" aria-hidden>
        {prob.lo != null && prob.hi != null &&
          <span className="prob-band" style={{ left: `${100 * prob.lo}%`, width: `${100 * (prob.hi - prob.lo)}%` }} />}
        <span className="prob-tick" style={{ left: `${100 * prob.p}%` }} />
      </span>
    </div>
  );
}

/** A yes/no market (over/under, both teams score) in one cell: the selection we rate furthest
 * above the market when it has a price, else yes. `won` (played) says whether a side came in:
 * the cell and its mark are highlighted when the selection shown did. */
export function TwoWayCell({ prob, price, yes, no, won }:
  { prob: Prob | null; price: number | null | undefined; yes: string; no: string; won?: "yes" | "no" }) {
  if (!prob) return <span className="dim prob-none" title="not published for this match">—</span>;
  const pick = twoWayPick(prob, price, won ?? "yes");
  const came = won !== undefined && pick.side === won;
  return <ProbCell prob={pick.prob} label={pick.side === "yes" ? yes : no} compact odds gap={pick.gap}
                   won={came} />;
}
