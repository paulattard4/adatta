import type { Prob } from "@/lib/types";
import {
  fairOdds, gapPts, interval, odds2, oddsInterval, pct, twoWaySide, type Gap, type Mkt, type Priced, type TwoWayKind,
} from "@/lib/format";

/** The mark on a selection we rate above the market; green with a check when it came in. */
const GapTag = ({ gap, won }: { gap: Gap; won?: boolean }) =>
  <span className={won ? "num prob-gap won" : "num prob-gap"}
        aria-label={`${gapPts(gap.diff)} points above the market${won ? ", came in" : ""}`}>
    ▲{gapPts(gap.diff)}{won && " ✓"}
  </span>;

/** The market's hover text: its probability (margin removed) and the bookmakers' actual odds. */
const mktTitle = (m: Mkt) =>
  `; market probability ${pct(m.p)}% with the margin removed` +
  (m.price ? `; bookmakers' odds as quoted (margin included): ${odds2(m.price.mean)} on average, best ${odds2(m.price.best)}` : "") +
  `; ${m.books} bookmaker${m.books === 1 ? "" : "s"}`;

/** A probability with its 90% interval: the number, the range, and a bar showing both.
 * With `odds`, also the fair decimal odds 1/p and their interval (every team level, D60).
 * `gap` marks a selection we rate above the market; `mkt` adds the bookmakers' average odds
 * under ours ("mkt @"), with the best price and the margin-free market in the hover. */
export function ProbCell({ prob, label, compact = false, odds = false, cameIn = false, gap, won = false, mkt }:
  { prob: Prob; label?: string; compact?: boolean; odds?: boolean; cameIn?: boolean; gap?: Gap; won?: boolean;
    mkt?: Mkt }) {
  const point = prob.lo == null || prob.hi == null; // C2 baseline (D54): no interval
  const title = (point ? `${label ?? ""} point forecast, no interval` :
    `${label ?? ""} 90% interval ${interval(prob.lo, prob.hi)}%`) +
    (odds ? `; fair odds ${fairOdds(prob.p)}` + (point ? "" : ` (${oddsInterval(prob.lo, prob.hi)})`) : "") +
    (cameIn ? "; came in" : "") +
    (gap ? `; ${gapPts(gap.diff)} points above the market` : "") +
    (mkt ? mktTitle(mkt) : "");
  const price = mkt?.price;
  return (
    <div className={(compact ? "prob prob-compact" : "prob") + (cameIn ? " came-in" : "") + (gap ? " has-gap" : "")} title={title}>
      {label && <span className="prob-label">{label}</span>}
      <span className="num prob-value">{pct(prob.p)}<span className="unit">%</span></span>
      <span className="num prob-interval">{interval(prob.lo, prob.hi)}</span>
      {(odds || gap || price) && (
        <span className={gap || price ? "num prob-odds with-gap" : "num prob-odds"}>
          {odds && (
            <span className="prob-price">
              <span className="prob-odds-at">@</span>{fairOdds(prob.p)}
              <span className="dim"> {oddsInterval(prob.lo, prob.hi)}</span>
            </span>
          )}
          {gap && <GapTag gap={gap} won={won} />}
          {price && <span className="prob-mkt">mkt <span className="prob-odds-at">@</span>{odds2(price.mean)}</span>}
        </span>
      )}
      <span className="prob-bar" aria-hidden>
        {prob.lo != null && prob.hi != null &&
          <span className="prob-band" style={{ left: `${100 * prob.lo}%`, width: `${100 * (prob.hi - prob.lo)}%` }} />}
        <span className="prob-tick" style={{ left: `${100 * prob.p}%` }} />
      </span>
    </div>
  );
}

/** A yes/no market (over/under 2.5, both teams score) in one cell: the selection we rate above
 * the market when it has a price, else yes, with the market's odds for it. */
export function TwoWayCell({ prob, market, kind }:
  { prob: Prob | null; market: Priced | null | undefined; kind: TwoWayKind }) {
  if (!prob) return <span className="dim prob-none" title="not published for this match">—</span>;
  const s = twoWaySide(kind, prob, market);
  return <ProbCell prob={s.prob} label={s.label} compact odds gap={s.gap} mkt={s.mkt} />;
}
