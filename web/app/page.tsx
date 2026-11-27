"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { CapabilityBadge, FreshnessBadge } from "@/components/Badges";
import { ConfidenceBadge } from "@/components/Confidence";
import { InPlay, LIVE_REFRESH_MS } from "@/components/Live";
import { ProbCell, TwoWayCell } from "@/components/Prob";
import { useJson } from "@/lib/fetch";
import { kickoffLocal, pct, tierLabel, topGap } from "@/lib/format";
import type { FixtureList, LiveList, TournamentList } from "@/lib/types";

const isoDay = (d: Date) => d.toISOString().slice(0, 10);

/** Fixture and competition browser (§16.2): grouped by competition, not a flat calendar. */
export default function Browser() {
  const today = useMemo(() => new Date(), []);
  const [from, setFrom] = useState(isoDay(today));
  // default: the next 3 days (owner, 2026-11-27); the date pickers widen it
  const [to, setTo] = useState(isoDay(new Date(today.getTime() + 3 * 86400e3)));
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
  const live = useJson<LiveList>("/api/live", LIVE_REFRESH_MS);
  const tours = useJson<TournamentList>("/api/tournaments");
  const comps = all.data?.competitions ?? [];
  const levels = [...new Set(comps.flatMap((c) => c.fixtures.map((f) => f.team_level)))];

  return (
    <div className="browser">
      <div className="filters panel">
        <label>Competition
          <select value={comp} onChange={(e) => setComp(e.target.value)}>
            <option value="">All</option>
            {comps.map((c) => <option key={c.competition_id} value={c.competition_id}>{c.country} · {c.name} ({tierLabel(c.tier)})</option>)}
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
      {live.data && <InPlay live={live.data} />}
      {tours.data && tours.data.tournaments.length > 0 && (
        <div className="panel">
          Tournaments: {tours.data.tournaments.map((x, i) => (
            <span key={x.edition}>{i > 0 && " · "}<Link href={`/tournament/${x.edition}`}>{x.name}</Link></span>
          ))}
        </div>
      )}
      {error && <div className="panel error">Could not load fixtures: {error}. Is the API running on 127.0.0.1:8000?</div>}
      {loading && !data && <div className="panel dim">Loading fixtures…</div>}
      {data && data.competitions.length === 0 && <div className="panel dim">No projected-bundle fixtures in this range.</div>}
      {data?.competitions.map((g) => (
        <section key={g.competition_id} className="panel comp">
          <div className="comp-head">
            <h2>{g.name} <span className="dim">{g.country}</span> <span className="tier">{tierLabel(g.tier)}</span></h2>
            {g.fixtures.some((f) => f.confidence.some((c) => c.key === "model" && c.value.startsWith("baseline"))) &&
              <span className="tag" style={{ ["--c" as string]: "var(--limited)" }}
                title="Phase 1a, results-only Dixon-Coles: the full model was not proven better in this league (D54). Point forecasts, no interval.">baseline model</span>}
            <FreshnessBadge f={g.freshness} />
            <CapabilityBadge caps={g.capabilities} />
          </div>
          <div className="fx-head fx-wide">
            <span>Kickoff</span><span>Match</span><span className="c">Home</span><span className="c">Draw</span>
            <span className="c">Away</span><span className="c">Goals 2.5</span><span className="c">BTTS</span><span>Confidence</span>
          </div>
          {g.fixtures.map((f) => (
            <Link key={f.match_id} href={`/match/${f.match_id}`} className="fx-row fx-wide">
              <span className="num kickoff">{kickoffLocal(f.kickoff)}</span>
              <span className="teams">{f.home} <span className="dim">v</span> {f.away}
                {f.market && <span className="dim market-line" title={`Bookmakers' latest prices, margin removed, averaged over ${f.market.bookmakers}: not a model input`}>
                  <span className="nw">Market <span className="num">{pct(f.market.home)}% · {pct(f.market.draw)}% · {pct(f.market.away)}%</span></span>
                  {f.market.over25 !== null && <> · <span className="nw">over 2.5 <span className="num">{pct(f.market.over25)}%</span></span></>}
                  {f.market.btts !== null && <> · <span className="nw">BTTS <span className="num">{pct(f.market.btts)}%</span></span></>}</span>}
              </span>
              {f.headline ? (() => {
                const h = f.headline;
                const g = f.market ? topGap({ home: h.home.p, draw: h.draw.p, away: h.away.p }, f.market) : null;
                const at = (k: "home" | "draw" | "away") => (g?.key === k ? g : undefined);
                return (
                  <>
                    <ProbCell prob={h.home} label="Home" compact odds gap={at("home")} />
                    <ProbCell prob={h.draw} label="Draw" compact odds gap={at("draw")} />
                    <ProbCell prob={h.away} label="Away" compact odds gap={at("away")} />
                    <TwoWayCell prob={h.over25} price={f.market?.over25} yes="Over 2.5" no="Under 2.5" />
                    <TwoWayCell prob={h.btts} price={f.market?.btts} yes="BTTS Yes" no="BTTS No" />
                  </>
                );
              })() : (
                <span className="no-proj dim">{f.no_projection_reason}</span>
              )}
              <ConfidenceBadge components={f.confidence} />
            </Link>
          ))}
        </section>
      ))}
      <p className="note">
        Probabilities are posterior means with 90% credible intervals (small numbers, and the band in each bar).
        Leagues tagged baseline model show point forecasts, without an interval.
        The @ figure is the fair decimal price 1/p, with no margin; hover for its interval.
        Kickoffs in your local time. Each dot is one named confidence component; hover for its value.
        Market is the bookmakers&apos; latest price with the margin removed: it is not a model input.
        ▲ marks, in each market, the selection we rate furthest above the market (ours minus the market, in percentage points).
        Goals 2.5 and BTTS show that selection; without a market price they show Over 2.5 and BTTS Yes.
        It is a comparison, not a betting signal: our probabilities have not yet been checked for calibration against the market.
      </p>
    </div>
  );
}
