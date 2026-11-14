import type { Prob } from "@/lib/types";
import { fairOdds, interval, oddsInterval, pct } from "@/lib/format";

/** A probability with its 90% interval: the number, the range, and a bar showing both.
 * With `odds`, also the fair decimal odds 1/p and their interval (senior only, principle 10). */
export function ProbCell({ prob, label, compact = false, odds = false }:
  { prob: Prob; label?: string; compact?: boolean; odds?: boolean }) {
  const title = `${label ?? ""} 90% interval ${interval(prob.lo, prob.hi)}%` +
    (odds ? `; fair odds ${fairOdds(prob.p)} (${oddsInterval(prob.lo, prob.hi)})` : "");
  return (
    <div className={compact ? "prob prob-compact" : "prob"} title={title}>
      {label && <span className="prob-label">{label}</span>}
      <span className="num prob-value">{pct(prob.p)}<span className="unit">%</span></span>
      <span className="num prob-interval">{interval(prob.lo, prob.hi)}</span>
      {odds && (
        <span className="num prob-odds">
          <span className="prob-odds-at">@</span>{fairOdds(prob.p)}
          <span className="dim"> {oddsInterval(prob.lo, prob.hi)}</span>
        </span>
      )}
      <span className="prob-bar" aria-hidden>
        <span className="prob-band" style={{ left: `${100 * prob.lo}%`, width: `${100 * (prob.hi - prob.lo)}%` }} />
        <span className="prob-tick" style={{ left: `${100 * prob.p}%` }} />
      </span>
    </div>
  );
}
