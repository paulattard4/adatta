import type { Decomposition } from "@/lib/types";

/** The decomposition strip as a waterfall (§13.1, §16.3): additive terms of log λ, each a
 *  step from the running total, with its 90% interval as a whisker; the last bar is log λ. */
export function Waterfall({ dec }: { dec: Decomposition }) {
  const rows = [] as { name: string; from: number; to: number; lo: number; hi: number; total?: boolean }[];
  let acc = 0;
  for (const t of dec.terms) {
    rows.push({ name: t.term, from: acc, to: acc + t.mean, lo: acc + t.lo, hi: acc + t.hi });
    acc += t.mean;
  }
  rows.push({ name: "log λ", from: 0, to: dec.log_rate.mean, lo: dec.log_rate.lo, hi: dec.log_rate.hi, total: true });
  const xs = rows.flatMap((r) => [r.from, r.to, r.lo, r.hi, 0]);
  const min = Math.min(...xs), max = Math.max(...xs);
  const pad = (max - min) * 0.06 || 0.1;
  const lo = min - pad, hi = max + pad;
  const W = 420, L = 0, rowH = 26;
  const x = (v: number) => L + ((v - lo) / (hi - lo)) * W;
  return (
    <div className="waterfall">
      <div className="wf-head">
        <span className="wf-team">{dec.team}</span>
        <span className="wf-side">{dec.side}</span>
        {dec.cold_start && <span className="tag s-limited" title="Not in the training window: league average">cold start</span>}
        <span className="wf-goals">
          λ <span className="num">{dec.goals.p.toFixed(2)}</span>{" "}
          <span className="num dim">({dec.goals.lo.toFixed(2)}–{dec.goals.hi.toFixed(2)})</span>
        </span>
      </div>
      <div className="wf-body">
        <div className="wf-labels">
          {rows.map((r) => <div key={r.name} className={r.total ? "wf-label total" : "wf-label"} style={{ height: rowH }}>{r.name}</div>)}
        </div>
        <svg className="wf-svg" viewBox={`0 0 ${W} ${rows.length * rowH}`} preserveAspectRatio="none" style={{ height: rows.length * rowH }} role="img"
          aria-label={`Decomposition of log expected goals for ${dec.team}`}>
          <line x1={x(0)} x2={x(0)} y1={0} y2={rows.length * rowH} className="wf-zero" />
          {rows.map((r, i) => {
            const y = i * rowH;
            const a = Math.min(x(r.from), x(r.to)), b = Math.max(x(r.from), x(r.to));
            const up = r.to >= r.from;
            return (
              <g key={r.name}>
                <rect x={a} y={y + 6} width={Math.max(b - a, 1.5)} height={rowH - 12}
                  className={r.total ? "wf-total" : up ? "wf-up" : "wf-down"} />
                <line x1={x(r.lo)} x2={x(r.hi)} y1={y + rowH / 2} y2={y + rowH / 2} className="wf-whisker" />
                {i < rows.length - 1 && !r.total && !rows[i + 1].total && (
                  <line x1={x(r.to)} x2={x(r.to)} y1={y + rowH - 6} y2={y + rowH + 6} className="wf-link" />
                )}
              </g>
            );
          })}
        </svg>
        <div className="wf-values">
          {rows.map((r) => (
            <div key={r.name} className={r.total ? "wf-value num total" : "wf-value num"} style={{ height: rowH }}
              title={`90% interval: ${(r.total ? r.lo : r.lo - r.from).toFixed(3)} to ${(r.total ? r.hi : r.hi - r.from).toFixed(3)}`}>
              {r.total ? r.to.toFixed(3) : `${r.to - r.from >= 0 ? "+" : "−"}${Math.abs(r.to - r.from).toFixed(3)}`}
              <span className="dim"> [{r.total ? r.lo.toFixed(2) : (r.lo - r.from).toFixed(2)}, {r.total ? r.hi.toFixed(2) : (r.hi - r.from).toFixed(2)}]</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
