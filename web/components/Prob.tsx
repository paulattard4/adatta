import type { Prob } from "@/lib/types";
import { interval, pct } from "@/lib/format";

/** A probability with its 90% interval: the number, the range, and a bar showing both. */
export function ProbCell({ prob, label, compact = false }: { prob: Prob; label?: string; compact?: boolean }) {
  return (
    <div className={compact ? "prob prob-compact" : "prob"} title={`${label ?? ""} 90% interval ${interval(prob.lo, prob.hi)}%`}>
      {label && <span className="prob-label">{label}</span>}
      <span className="num prob-value">{pct(prob.p)}<span className="unit">%</span></span>
      <span className="num prob-interval">{interval(prob.lo, prob.hi)}</span>
      <span className="prob-bar" aria-hidden>
        <span className="prob-band" style={{ left: `${100 * prob.lo}%`, width: `${100 * (prob.hi - prob.lo)}%` }} />
        <span className="prob-tick" style={{ left: `${100 * prob.p}%` }} />
      </span>
    </div>
  );
}
