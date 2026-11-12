import type { ScoreMatrix } from "@/lib/types";
import { interval, pct } from "@/lib/format";

/** Correct-score heatmap (§16.3): posterior-mean P(home = i, away = j), each cell's 90%
 *  interval on hover and in the cell's accessible label. */
export function Heatmap({ m, home, away }: { m: ScoreMatrix; home: string; away: string }) {
  const max = Math.max(...m.cells.flat().map((c) => c.p));
  const goals = Array.from({ length: m.max_shown + 1 }, (_, i) => i);
  return (
    <div className="heatmap-wrap">
      <table className="heatmap">
        <caption className="sr-only">Correct score probabilities</caption>
        <thead>
          <tr>
            <th className="corner"><span>{home} ↓</span><span>{away} →</span></th>
            {goals.map((j) => <th key={j} className="num">{j}</th>)}
          </tr>
        </thead>
        <tbody>
          {goals.map((i) => (
            <tr key={i}>
              <th className="num">{i}</th>
              {goals.map((j) => {
                const c = m.cells[i][j];
                const a = c.p / max;
                const cls = i > j ? "hm-home" : i === j ? "hm-draw" : "hm-away";
                return (
                  <td key={j} className={`num ${cls}`} style={{ ["--a" as string]: a.toFixed(3) }}
                    title={`${i}–${j}: ${pct(c.p, 2)}% (90%: ${interval(c.lo, c.hi, 2)})`}
                    aria-label={`${i}–${j}: ${pct(c.p, 2)} percent, 90 percent interval ${interval(c.lo, c.hi, 2)}`}>
                    {c.p < 0.0005 ? "<0.1" : pct(c.p, 1)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="note">
        Any side above {m.max_shown}: <span className="num">{pct(m.rest.p, 2)}%</span>{" "}
        (<span className="num">{interval(m.rest.lo, m.rest.hi, 2)}</span>). Truncated at {m.max_goals} goals a
        side; mass beyond, before renormalising: <span className="num">{m.tail_mass.toExponential(1)}</span>.
      </p>
    </div>
  );
}
