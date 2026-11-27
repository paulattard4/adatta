import Link from "next/link";
import { ProbCell, TwoWayCell } from "@/components/Prob";
import { liveClock, pct, timeLocal, tierLabel, topGap } from "@/lib/format";
import type { LiveList, LiveMatch } from "@/lib/types";

export const LIVE_REFRESH_MS = 60_000; // the API asks the provider at most once a minute anyway

export const score = (m: LiveMatch) =>
  m.home_goals == null || m.away_goals == null ? "–" : `${m.home_goals}–${m.away_goals}`;

/** "In play" at the top of the fixture browser: live score beside the pre-match projection
 * and its fair odds, both labelled pre-match: neither is updated in play. */
export function InPlay({ live }: { live: LiveList }) {
  if (live.matches.length === 0 && !live.note) return null;
  return (
    <section className="panel comp in-play">
      <div className="comp-head">
        <h2><span className="live-dot" aria-hidden /> In play</h2>
        <span className="dim small">
          Pre-match projection and fair odds, not updated in play; ▲ marks the selection furthest above the market at kickoff.
          {live.fetched_at && <> Scores as of <span className="num">{timeLocal(live.fetched_at)}</span>.</>}
        </span>
        {live.note && <span className="live-note">{live.note}</span>}
      </div>
      {live.matches.length > 0 && (
        <div className="fx-head fx-wide">
          <span>Minute</span><span>Match</span><span className="c">Home</span><span className="c">Draw</span>
          <span className="c">Away</span><span className="c">Goals 2.5</span><span className="c">BTTS</span><span>Projection</span>
        </div>
      )}
      {live.matches.map((m) => (
        <Link key={m.match_id} href={`/match/${m.match_id}`} className="fx-row fx-wide">
          <span className="num live-clock">{liveClock(m.status, m.elapsed, m.extra)}</span>
          <span className="teams">
            {m.home} <span className="num live-score">{score(m)}</span> {m.away}
            <span className="dim played-comp">{m.competition} <span className="tier">{tierLabel(m.competition_tier)}</span></span>
            {m.market && <MarketAtKickoff m={m} />}
          </span>
          {m.forecast ? (
            <>
              <Outcome m={m} />
              <TwoWayCell prob={m.forecast.over25} price={m.market?.over25} yes="Over 2.5" no="Under 2.5" />
              <TwoWayCell prob={m.forecast.btts} price={m.market?.btts} yes="BTTS Yes" no="BTTS No" />
              <span className="dim small" title={`published ${timeLocal(m.forecast.generated_at)}`}>pre-match · {m.forecast.milestone}</span>
            </>
          ) : (
            <span className="no-proj dim">No projection was published before kickoff.</span>
          )}
        </Link>
      ))}
    </section>
  );
}

/** The pre-match 1X2, the selection furthest above the market at kickoff marked. */
function Outcome({ m }: { m: LiveMatch }) {
  const f = m.forecast!;
  const g = m.market ? topGap({ home: f.home.p, draw: f.draw.p, away: f.away.p }, m.market) : null;
  const at = (k: "home" | "draw" | "away") => (g?.key === k ? g : undefined);
  return (
    <>
      <ProbCell prob={f.home} label="Home" compact odds gap={at("home")} />
      <ProbCell prob={f.draw} label="Draw" compact odds gap={at("draw")} />
      <ProbCell prob={f.away} label="Away" compact odds gap={at("away")} />
    </>
  );
}

/** The bookmakers' last price before kickoff, margin removed (not a model input). */
function MarketAtKickoff({ m }: { m: LiveMatch }) {
  const k = m.market!;
  return (
    <span className="dim market-line" title={`Bookmakers' last price before kickoff, margin removed, averaged over ${k.bookmakers}: not a model input`}>
      <span className="nw">Market at kickoff <span className="num">{pct(k.home)}% · {pct(k.draw)}% · {pct(k.away)}%</span></span>
      {k.over25 !== null && <> · <span className="nw">over 2.5 <span className="num">{pct(k.over25)}%</span></span></>}
      {k.btts !== null && <> · <span className="nw">BTTS <span className="num">{pct(k.btts)}%</span></span></>}
    </span>
  );
}
