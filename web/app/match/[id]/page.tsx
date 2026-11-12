"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { CapabilityBadge, FreshnessBadge } from "@/components/Badges";
import { ConfidencePanel } from "@/components/Confidence";
import { Heatmap } from "@/components/Heatmap";
import { ProbCell } from "@/components/Prob";
import { Waterfall } from "@/components/Waterfall";
import { useJson, useNow } from "@/lib/fetch";
import { countdown, dateLocal, interval, kickoffLocal, MARKET_LABEL, pct } from "@/lib/format";
import type { MatchView, Milestone, Selection } from "@/lib/types";

const SEL_LABEL: Record<string, string> = { home: "Home", draw: "Draw", away: "Away", over: "Over", under: "Under", yes: "Yes", no: "No" };

function Markets({ m }: { m: Milestone }) {
  const groups = new Map<string, Selection[]>();
  for (const s of m.selections) {
    const k = s.line == null ? s.market : `${s.market} ${s.line}`;
    groups.set(k, [...(groups.get(k) ?? []), s]);
  }
  const order = ["1X2", "OU 1.5", "OU 2.5", "OU 3.5", "BTTS"];
  return (
    <div className="markets">
      {[...groups.entries()].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0])).map(([k, sels]) => (
        <div key={k} className="market">
          <div className="market-name">{MARKET_LABEL[sels[0].market] ?? sels[0].market}{sels[0].line != null && <span className="num"> {sels[0].line}</span>}</div>
          <div className="market-sels">
            {sels.map((s) => <ProbCell key={s.selection} prob={s.prob} label={SEL_LABEL[s.selection] ?? s.selection} />)}
          </div>
        </div>
      ))}
    </div>
  );
}

function History({ ms }: { ms: Milestone[] }) {
  const p = (m: Milestone, sel: string) => m.selections.find((s) => s.market === "1X2" && s.selection === sel)!.prob;
  return (
    <table className="history">
      <thead><tr><th>Milestone</th><th>Written</th><th>Refit</th><th className="r">Home</th><th className="r">Draw</th><th className="r">Away</th></tr></thead>
      <tbody>
        {ms.map((m) => (
          <tr key={m.milestone}>
            <td>{m.milestone}</td>
            <td className="num">{kickoffLocal(m.generated_at)}</td>
            <td className="num">{m.refit_as_of ? `${dateLocal(m.refit_as_of)} · run ${m.model_run_id}` : "—"}</td>
            {["home", "draw", "away"].map((s) => (
              <td key={s} className="num r">{pct(p(m, s).p)} <span className="dim">{interval(p(m, s).lo, p(m, s).hi)}</span></td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Match projection dashboard (§16.3), in the spec's order. */
export default function MatchPage() {
  const { id } = useParams<{ id: string }>();
  const { data: v, error } = useJson<MatchView>(`/api/matches/${id}`);
  const now = useNow();
  if (error) return <div className="panel error">Could not load match {id}: {error}</div>;
  if (!v) return <div className="panel dim">Loading match…</div>;
  const h = v.header;
  const last = v.milestones.at(-1);
  const w = h.weather;
  return (
    <div className="match">
      <Link href="/" className="back">← Fixtures</Link>

      <section className="panel strip">
        <div className="strip-teams">
          <span className="team">{h.home}</span><span className="dim">v</span><span className="team">{h.away}</span>
        </div>
        <div className="strip-meta">
          <span>{h.competition}{h.round && <span className="dim"> · {h.round}</span>}</span>
          <span className="num">{kickoffLocal(h.kickoff)}</span>
          <span className="num countdown">{countdown(h.kickoff, now)}</span>
          <span>{h.venue ?? <span className="dim">venue not listed</span>}</span>
          <span>{w ? (
            <span title={`Open-Meteo forecast issued ${kickoffLocal(w.observed_at)}, ${w.lead_hours.toFixed(0)} h before kickoff`}>
              <span className="num">{w.temperature_c?.toFixed(0) ?? "–"}°C · {w.precipitation_mm?.toFixed(1) ?? "–"} mm · {w.wind_speed_kmh?.toFixed(0) ?? "–"} km/h</span>
            </span>) : <span className="dim">no weather forecast yet</span>}</span>
          <span>Referee: {h.referee ?? <span className="dim">not announced</span>}</span>
          <CapabilityBadge caps={h.capabilities} />
          <FreshnessBadge f={v.freshness} />
        </div>
        <button className="override" disabled title="Override drawer comes with the §8.5 override mechanisms">Override…</button>
      </section>

      <section className="panel">
        <h3>Confidence</h3>
        <ConfidencePanel components={v.confidence} />
      </section>

      {last ? (
        <section className="panel">
          <h3>Markets <span className="dim">· {last.milestone} projection, posterior mean and 90% interval</span></h3>
          <Markets m={last} />
          <h4>Milestone history</h4>
          <div className="history-wrap"><History ms={v.milestones} /></div>
        </section>
      ) : (
        <section className="panel dim">{v.no_projection_reason}</section>
      )}

      {v.coherence && !v.coherence.coherent && (
        <section className="panel error">
          The correct-score matrix recomputed from the stored draws does not reproduce the published {v.coherence.milestone}{" "}
          projection (max difference {v.coherence.max_abs_diff.toExponential(2)}), so it is not shown (§11.2).
        </section>
      )}

      {v.matrix && (
        <section className="panel">
          <h3>Correct score</h3>
          <Heatmap m={v.matrix} home={h.home} away={h.away} />
        </section>
      )}

      {v.decomposition.length > 0 && (
        <section className="panel">
          <h3>Decomposition <span className="dim">· log expected goals, posterior mean and 90% interval per term</span></h3>
          <div className="waterfalls">{v.decomposition.map((d) => <Waterfall key={d.side} dec={d} />)}</div>
        </section>
      )}

      <section className="panel">
        <h3>Not available yet</h3>
        <ul className="unavail">
          {v.unavailable.map((u) => <li key={u.panel}><span className="unavail-panel">{u.panel}</span> <span className="dim">{u.reason}</span></li>)}
        </ul>
      </section>
    </div>
  );
}
