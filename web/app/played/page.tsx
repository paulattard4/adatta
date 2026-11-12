"use client";
import Link from "next/link";
import { useState } from "react";
import { ProbCell } from "@/components/Prob";
import { useJson } from "@/lib/fetch";
import { dateLocal, kickoffLocal, pct } from "@/lib/format";
import type { MeanScore, PlayedList, PlayedMatch } from "@/lib/types";

const METRIC_LABEL: Record<string, string> = {
  log_loss_1x2: "1X2 log loss", rps_1x2: "1X2 RPS", brier_1x2: "1X2 Brier",
  log_loss_ou25: "Over/under 2.5 log loss", log_loss_btts: "Both teams score log loss",
};
const FEW = 200; // below this, a mean score says little

const score = (s: MeanScore | undefined) =>
  s ? <><span className="num">{s.mean.toFixed(3)}</span> <span className="num dim">[{s.lo.toFixed(3)}, {s.hi.toFixed(3)}]</span></> : <span className="dim">—</span>;

function Outcome({ m }: { m: PlayedMatch }) {
  const cells: [string, "home" | "draw" | "away", PlayedMatch["home_p"]][] = [
    ["Home", "home", m.home_p], ["Draw", "draw", m.draw_p], ["Away", "away", m.away_p],
  ];
  return (
    <>
      {cells.map(([label, key, p]) => (
        <div key={key} className={m.outcome === key ? "played-cell hit" : "played-cell"}>
          <ProbCell prob={p} label={label} compact />
        </div>
      ))}
    </>
  );
}

/** Finished matches against the forecast published before kickoff (no reconstructions). */
export default function Played() {
  const [comp, setComp] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const all = useJson<PlayedList>("/api/played");
  const q = new URLSearchParams();
  if (comp) q.set("competition_id", comp);
  if (from) q.set("start", new Date(`${from}T00:00:00`).toISOString());
  if (to) q.set("end", new Date(`${to}T23:59:59`).toISOString());
  const { data, error, loading } = useJson<PlayedList>(`/api/played?${q}`);
  const comps = (all.data?.summary ?? []).filter((s) => s.competition_id !== null);
  const overall = data?.summary.find((s) => s.segment === "all");

  return (
    <div className="browser">
      <div className="filters panel">
        <label>Competition
          <select value={comp} onChange={(e) => setComp(e.target.value)}>
            <option value="">All</option>
            {comps.map((c) => <option key={c.competition_id} value={c.competition_id ?? ""}>{c.segment}</option>)}
          </select>
        </label>
        <label>From <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label>
        <label>To <input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label>
        <span className="filter-note">Only forecasts published before kickoff. Lower scores are better.</span>
      </div>
      {error && <div className="panel error">Could not load played matches: {error}. Is the API running on 127.0.0.1:8000?</div>}
      {loading && !data && <div className="panel dim">Loading played matches…</div>}
      {data && data.matches.length === 0 && (
        <div className="panel dim">
          No forecast match has been played yet.
          {data.forecasts_since && <> Forecasting began on {dateLocal(data.forecasts_since)}; </>}
          results appear here as the first forecast matches finish (from 21 November).
        </div>
      )}
      {data && overall && (
        <section className="panel">
          <h2>Scores so far</h2>
          <p className="note">
            Mean per match with its 95% interval. A uniform 1X2 forecast (1/3 each) scores a log loss of{" "}
            <span className="num">{data.uniform_log_loss_1x2.toFixed(3)}</span>.
            {overall.n < FEW && <> With {overall.n} matches the intervals are wide: read these as a first look, not a verdict.</>}
          </p>
          <div className="table-wrap">
            <table className="scores">
              <thead>
                <tr><th>Segment</th><th className="num">n</th>
                  {Object.keys(METRIC_LABEL).map((k) => <th key={k}>{METRIC_LABEL[k]}</th>)}</tr>
              </thead>
              <tbody>
                {data.summary.map((s) => (
                  <tr key={s.segment}>
                    <td>{s.segment === "all" ? "All" : s.segment}</td><td className="num">{s.n}</td>
                    {Object.keys(METRIC_LABEL).map((k) => <td key={k}>{score(s.scores.find((x) => x.metric === k))}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {data && data.matches.length > 0 && (
        <section className="panel">
          <h2>Matches</h2>
          <div className="played-head">
            <span>Kickoff</span><span>Match</span><span className="c">Result</span><span className="c">Home</span>
            <span className="c">Draw</span><span className="c">Away</span><span className="c">1X2 log loss</span>
          </div>
          {data.matches.map((m) => (
            <Link key={m.match_id} href={`/match/${m.match_id}`} className="played-row"
                  title={`${m.competition} · forecast ${m.milestone}, published ${kickoffLocal(m.generated_at)}`}>
              <span className="num kickoff">{kickoffLocal(m.kickoff)}</span>
              <span className="teams">{m.home} <span className="dim">v</span> {m.away}<span className="dim played-comp"> · {m.competition}</span></span>
              <span className="num c result">{m.home_goals}–{m.away_goals}</span>
              <Outcome m={m} />
              <span className="num c ll">{m.scores.log_loss_1x2.toFixed(3)}
                <span className="dim played-pp"> p {pct(m.outcome === "home" ? m.home_p.p : m.outcome === "draw" ? m.draw_p.p : m.away_p.p)}%</span>
              </span>
            </Link>
          ))}
        </section>
      )}
    </div>
  );
}
