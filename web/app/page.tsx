"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { CapabilityBadge, FreshnessBadge } from "@/components/Badges";
import { ConfidenceBadge } from "@/components/Confidence";
import { ProbCell } from "@/components/Prob";
import { useJson } from "@/lib/fetch";
import { kickoffLocal } from "@/lib/format";
import type { FixtureList } from "@/lib/types";

const isoDay = (d: Date) => d.toISOString().slice(0, 10);

/** Fixture and competition browser (§16.2): grouped by competition, not a flat calendar. */
export default function Browser() {
  const today = useMemo(() => new Date(), []);
  const [from, setFrom] = useState(isoDay(today));
  const [to, setTo] = useState(isoDay(new Date(today.getTime() + 14 * 86400e3)));
  const [comp, setComp] = useState("");
  const [level, setLevel] = useState("");
  const all = useJson<FixtureList>("/api/fixtures");
  const q = new URLSearchParams();
  const start = new Date(`${from}T00:00:00`);
  q.set("start", (start < today ? today : start).toISOString());
  q.set("end", new Date(`${to}T23:59:59`).toISOString());
  if (comp) q.set("competition_id", comp);
  if (level) q.set("team_level", level);
  const { data, error, loading } = useJson<FixtureList>(`/api/fixtures?${q}`);
  const comps = all.data?.competitions ?? [];
  const levels = [...new Set(comps.flatMap((c) => c.fixtures.map((f) => f.team_level)))];

  return (
    <div className="browser">
      <div className="filters panel">
        <label>Competition
          <select value={comp} onChange={(e) => setComp(e.target.value)}>
            <option value="">All</option>
            {comps.map((c) => <option key={c.competition_id} value={c.competition_id}>{c.country} · {c.name}</option>)}
          </select>
        </label>
        <label>From <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label>
        <label>To <input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label>
        <label>Team level
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">All</option>
            {levels.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </label>
        <span className="filter-note" title="§13.2: a headline confidence label needs a backtested combining formula; edge analytics is wave 2.">
          Confidence-tier and “has edge” filters: not yet available
        </span>
      </div>
      {error && <div className="panel error">Could not load fixtures: {error}. Is the API running on 127.0.0.1:8000?</div>}
      {loading && !data && <div className="panel dim">Loading fixtures…</div>}
      {data && data.competitions.length === 0 && <div className="panel dim">No projected-bundle fixtures in this range.</div>}
      {data?.competitions.map((g) => (
        <section key={g.competition_id} className="panel comp">
          <div className="comp-head">
            <h2>{g.name} <span className="dim">{g.country}</span></h2>
            <FreshnessBadge f={g.freshness} />
            <CapabilityBadge caps={g.capabilities} />
          </div>
          <div className="fx-head">
            <span>Kickoff</span><span>Match</span><span className="c">Home</span><span className="c">Draw</span>
            <span className="c">Away</span><span>Confidence</span>
          </div>
          {g.fixtures.map((f) => (
            <Link key={f.match_id} href={`/match/${f.match_id}`} className="fx-row">
              <span className="num kickoff">{kickoffLocal(f.kickoff)}</span>
              <span className="teams">{f.home} <span className="dim">v</span> {f.away}</span>
              {f.headline ? (
                <>
                  <ProbCell prob={f.headline.home} label="Home" compact />
                  <ProbCell prob={f.headline.draw} label="Draw" compact />
                  <ProbCell prob={f.headline.away} label="Away" compact />
                </>
              ) : (
                <span className="no-proj dim">{f.no_projection_reason}</span>
              )}
              <ConfidenceBadge components={f.confidence} />
            </Link>
          ))}
        </section>
      ))}
      <p className="note">
        Probabilities are posterior means with 90% credible intervals (small numbers, and the band in each bar).
        Kickoffs in your local time. Each dot is one named confidence component; hover for its value.
      </p>
    </div>
  );
}
