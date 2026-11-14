import Link from "next/link";
import { ProbCell } from "@/components/Prob";
import { liveClock, timeLocal, tierLabel } from "@/lib/format";
import type { LiveList, LiveMatch } from "@/lib/types";

export const LIVE_REFRESH_MS = 60_000; // the API asks the provider at most once a minute anyway

export const score = (m: LiveMatch) =>
  m.home_goals == null || m.away_goals == null ? "–" : `${m.home_goals}–${m.away_goals}`;

/** "In play" at the top of the fixture browser: live score beside the pre-match projection.
 * No fair odds here: a pre-kickoff price is stale once the match has started. */
export function InPlay({ live }: { live: LiveList }) {
  if (live.matches.length === 0 && !live.note) return null;
  return (
    <section className="panel comp in-play">
      <div className="comp-head">
        <h2><span className="live-dot" aria-hidden /> In play</h2>
        <span className="dim small">
          Pre-match projection, not updated in play.
          {live.fetched_at && <> Scores as of <span className="num">{timeLocal(live.fetched_at)}</span>.</>}
        </span>
        {live.note && <span className="live-note">{live.note}</span>}
      </div>
      {live.matches.length > 0 && (
        <div className="fx-head">
          <span>Minute</span><span>Match</span><span className="c">Home</span><span className="c">Draw</span>
          <span className="c">Away</span><span>Projection</span>
        </div>
      )}
      {live.matches.map((m) => (
        <Link key={m.match_id} href={`/match/${m.match_id}`} className="fx-row">
          <span className="num live-clock">{liveClock(m.status, m.elapsed, m.extra)}</span>
          <span className="teams">
            {m.home} <span className="num live-score">{score(m)}</span> {m.away}
            <span className="dim played-comp">{m.competition} <span className="tier">{tierLabel(m.competition_tier)}</span></span>
          </span>
          {m.forecast ? (
            <>
              <ProbCell prob={m.forecast.home} label="Home" compact />
              <ProbCell prob={m.forecast.draw} label="Draw" compact />
              <ProbCell prob={m.forecast.away} label="Away" compact />
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
