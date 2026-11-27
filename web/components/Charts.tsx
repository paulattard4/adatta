"use client";
import { useState } from "react";
import type { ReliabilityBin, TrendPoint } from "@/lib/types";

const W = 360, H = 240, PAD = { l: 40, r: 12, t: 12, b: 30 };
const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;

/** Reliability: mean forecast probability against the share that happened, per tenth.
 *  One series, so no legend; the diagonal is perfect calibration. Dot area grows with n. */
export function Reliability({ bins }: { bins: ReliabilityBin[] }) {
  const [hover, setHover] = useState<ReliabilityBin | null>(null);
  const x = (p: number) => PAD.l + p * iw;
  const y = (p: number) => PAD.t + (1 - p) * ih;
  const nmax = Math.max(1, ...bins.map((b) => b.n));
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="1X2 calibration: forecast probability against observed frequency">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(0)} x2={x(1)} y1={y(t)} y2={y(t)} className="grid" />
            <text x={x(0) - 6} y={y(t) + 4} className="tick" textAnchor="end">{Math.round(t * 100)}%</text>
            <text x={x(t)} y={H - 10} className="tick" textAnchor="middle">{Math.round(t * 100)}%</text>
          </g>
        ))}
        <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} className="ref" />
        {bins.map((b) => (
          <g key={b.lo} onMouseEnter={() => setHover(b)} onMouseLeave={() => setHover(null)}>
            <circle cx={x(b.predicted)} cy={y(b.observed)} r={14} className="hit" />
            <circle cx={x(b.predicted)} cy={y(b.observed)} r={4 + 6 * Math.sqrt(b.n / nmax)} className="dot-mark" />
          </g>
        ))}
      </svg>
      <figcaption className="dim">
        {hover
          ? <>Forecasts {Math.round(hover.lo * 100)}–{Math.round(hover.hi * 100)}%: said <span className="num">{(hover.predicted * 100).toFixed(1)}%</span>, happened <span className="num">{(hover.observed * 100).toFixed(1)}%</span> of <span className="num">{hover.n}</span></>
          : "Across: forecast probability. Up: how often it happened. On the diagonal is perfectly calibrated; hover a dot."}
      </figcaption>
    </figure>
  );
}

/** The running 1X2 log loss gap, ours minus the market's, with its 95% interval band. Below
 *  zero: ours better. One series, so no legend. */
export function GapTrend({ points }: { points: TrendPoint[] }) {
  const [hover, setHover] = useState<TrendPoint | null>(null);
  if (points.length === 0) return null;
  const vals = points.flatMap((p) => [p.diff, p.diff_lo ?? p.diff, p.diff_hi ?? p.diff, 0]);
  const top = Math.max(...vals), bot = Math.min(...vals);
  const span = top - bot || 0.01;
  const x = (i: number) => PAD.l + (points.length === 1 ? iw / 2 : (i / (points.length - 1)) * iw);
  const y = (v: number) => PAD.t + ((top - v) / span) * ih;
  const banded = points.filter((p) => p.diff_lo !== null && p.diff_hi !== null);
  const band = banded.length > 1
    ? [...banded.map((p) => `${x(points.indexOf(p))},${y(p.diff_hi!)}`),
       ...banded.slice().reverse().map((p) => `${x(points.indexOf(p))},${y(p.diff_lo!)}`)].join(" ")
    : null;
  const line = points.map((p, i) => `${x(i)},${y(p.diff)}`).join(" ");
  const ticks = [top, (top + bot) / 2, bot];
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Running 1X2 log loss gap to the market by day">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(0)} x2={PAD.l + iw} y1={y(t)} y2={y(t)} className="grid" />
            <text x={PAD.l - 6} y={y(t) + 4} className="tick" textAnchor="end">{t >= 0 ? "+" : ""}{t.toFixed(3)}</text>
          </g>
        ))}
        <line x1={PAD.l} x2={PAD.l + iw} y1={y(0)} y2={y(0)} className="ref" />
        {band && <polygon points={band} className="band" />}
        <polyline points={line} className="line" />
        {points.map((p, i) => (
          <g key={p.day} onMouseEnter={() => setHover(p)} onMouseLeave={() => setHover(null)}>
            <rect x={x(i) - Math.max(6, iw / points.length / 2)} y={PAD.t} width={Math.max(12, iw / points.length)} height={ih} className="hit" />
            {hover === p && <circle cx={x(i)} cy={y(p.diff)} r={4} className="dot-mark" />}
          </g>
        ))}
        <text x={x(0)} y={H - 10} className="tick" textAnchor="start">{points[0].day.slice(5)}</text>
        {points.length > 1 && <text x={x(points.length - 1)} y={H - 10} className="tick" textAnchor="end">{points.at(-1)!.day.slice(5)}</text>}
      </svg>
      <figcaption className="dim">
        {hover
          ? <>To {hover.day}: <span className="num">{hover.n}</span> matches, ours <span className="num">{hover.ours.toFixed(3)}</span>, market <span className="num">{hover.market.toFixed(3)}</span>, gap <span className="num">{hover.diff >= 0 ? "+" : ""}{hover.diff.toFixed(3)}</span>{hover.diff_lo !== null && hover.diff_hi !== null && <> [{hover.diff_lo.toFixed(3)}, {hover.diff_hi.toFixed(3)}]</>}</>
          : "Cumulative ours minus the market's 1X2 log loss, with its 95% interval. Below the zero line, ours scored better; hover a day."}
      </figcaption>
    </figure>
  );
}
