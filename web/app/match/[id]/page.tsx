"use client";
import { useParams } from "next/navigation";
import { CapabilityBadge, FreshnessBadge } from "@/components/Badges";
import { ConfidencePanel } from "@/components/Confidence";
import { Heatmap } from "@/components/Heatmap";
import { LIVE_REFRESH_MS, score } from "@/components/Live";
import { MatchNav } from "@/components/MatchNav";
import { ProbCell } from "@/components/Prob";
import { SquadPanel } from "@/components/Squad";
import { Waterfall } from "@/components/Waterfall";
import { useJson, useNow } from "@/lib/fetch";
import { cameIn, countdown, dateLocal, fairOdds, HALF_MARKETS, interval, kickoffLocal, liveClock, MARKET_LABEL, oddsInterval, pct, showsOdds, tierLabel } from "@/lib/format";
import type { LiveList, MatchView, Milestone, Qualify, Result, Selection } from "@/lib/types";

const SEL_LABEL: Record<string, string> = { home: "Home", draw: "Draw", away: "Away", over: "Over", under: "Under", yes: "Yes", no: "No" };

function Markets({ m, odds, result }: { m: Milestone; odds: boolean; result: Result | null }) {
  const groups = new Map<string, Selection[]>();
  for (const s of m.selections) {
    if (HALF_MARKETS.includes(s.market)) continue; // the Half time panel
    const k = s.line == null ? s.market : `${s.market} ${s.line}`;
    groups.set(k, [...(groups.get(k) ?? []), s]);
  }
  const order = ["1X2", "OU 1.5", "OU 2.5", "OU 3.5", "BTTS"];
  return (
    <div className="markets">
      {[...groups.entries()].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0])).map(([k, sels]) => (
        <div key={k} className={sels.length > 2 ? "market wide" : "market"}>
          <div className="market-name">{MARKET_LABEL[sels[0].market] ?? sels[0].market}{sels[0].line != null && <span className="num"> {sels[0].line}</span>}</div>
          <div className="market-sels">
            {sels.map((s) => <ProbCell key={s.selection} prob={s.prob} label={SEL_LABEL[s.selection] ?? s.selection} odds={odds}
                                    cameIn={result !== null && cameIn(s, result.home_goals, result.away_goals)} />)}
          </div>
        </div>
      ))}
    </div>
  );
}

const RES = [["1", "Home"], ["X", "Draw"], ["2", "Away"]];

/** P3b-2: half-time 1X2, the HT/FT grid (rows half time, columns full time) and half-time
 *  O/U 1.5 of the latest milestone, posterior mean and 90% interval. */
function HalfTime({ m, odds }: { m: Milestone; odds: boolean }) {
  const get = (market: string, sel: string) => m.selections.find((s) => s.market === market && s.selection === sel);
  const ht = RES.map(([c, l]) => [get("HT1X2", c), l] as const);
  const ou = ["over", "under"].map((c) => [get("HTOU", c), SEL_LABEL[c]] as const);
  return (
    <div className="markets">
      <div className="market wide">
        <div className="market-name">{MARKET_LABEL.HT1X2}</div>
        <div className="market-sels">{ht.map(([s, l]) => s && <ProbCell key={l} prob={s.prob} label={l} odds={odds} />)}</div>
      </div>
      <div className="market">
        <div className="market-name">{MARKET_LABEL.HTOU}<span className="num"> 1.5</span></div>
        <div className="market-sels">{ou.map(([s, l]) => s && <ProbCell key={l} prob={s.prob} label={l} odds={odds} />)}</div>
      </div>
      <div className="market wide">
        <div className="market-name">{MARKET_LABEL.HTFT} <span className="dim">· rows half time, columns full time</span></div>
        <table className="history htft">
          <thead><tr><th></th>{RES.map(([c, l]) => <th key={c} className="r">FT {l}</th>)}</tr></thead>
          <tbody>
            {RES.map(([a, la]) => (
              <tr key={a}>
                <td>HT {la}</td>
                {RES.map(([b]) => {
                  const s = get("HTFT", `${a}/${b}`);
                  return <td key={b} className="num r" title={s && odds ? `fair odds ${fairOdds(s.prob.p)} (${oddsInterval(s.prob.lo, s.prob.hi)})` : undefined}>
                    {s ? <>{pct(s.prob.p)} <span className="dim">{interval(s.prob.lo, s.prob.hi)}</span></> : "–"}
                  </td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function History({ ms, odds }: { ms: Milestone[]; odds: boolean }) {
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
              <td key={s} className="num r" title={odds ? `fair odds ${fairOdds(p(m, s).p)} (${oddsInterval(p(m, s).lo, p(m, s).hi)})` : undefined}>
                {pct(p(m, s).p)} <span className="dim">{interval(p(m, s).lo, p(m, s).hi)}</span>
                {odds && <span className="hist-odds"><span className="prob-odds-at">@</span>{fairOdds(p(m, s).p)}</span>}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const HOW: [string, string][] = [["90", "in normal time"], ["et", "in extra time"], ["pens", "on penalties"]];

/** Stage D v2: who goes through, and how, beside the 90' forecast. */
function GoingThrough({ q, home, away, odds }: { q: Qualify; home: string; away: string; odds: boolean }) {
  const legs = q.legs.length > 1;
  return (
    <>
      <div className="markets">
        <div className="market">
          <div className="market-name">To go through <span className="dim">· {q.stage}</span></div>
          <div className="market-sels">
            <ProbCell prob={q.home} label={home} odds={odds} />
            <ProbCell prob={q.away} label={away} odds={odds} />
          </div>
        </div>
      </div>
      <table className="history qualify-path">
        <thead><tr><th>How</th><th className="r">{home}</th><th className="r">{away}</th></tr></thead>
        <tbody>
          {HOW.map(([k, label]) => (
            <tr key={k}>
              <td>{k === "90" && legs ? "on aggregate" : label}</td>
              {["home", "away"].map((s) => {
                const p = q.path[`${s}_${k}`];
                return <td key={s} className="num r">{pct(p.p)} <span className="dim">{interval(p.lo, p.hi)}</span></td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {legs && (
        <p className="dim qualify-legs">
          {q.round}: {q.legs.map((l, i) => (
            <span key={l.match_id}>{i > 0 && " · "}leg {i + 1} {l.home} v {l.away}, {l.score ?? kickoffLocal(l.kickoff)}</span>
          ))}
        </p>
      )}
      <p className="dim">{q.label}. {q.milestone} projection, posterior mean and 90% interval.</p>
    </>
  );
}

/** Match projection dashboard (§16.3), in the spec's order. */
export default function MatchPage() {
  const { id } = useParams<{ id: string }>();
  const { data: v, error } = useJson<MatchView>(`/api/matches/${id}`);
  const now = useNow();
  const started = v ? new Date(v.header.kickoff).getTime() <= now : false;
  const live = useJson<LiveList>(started ? "/api/live" : null, LIVE_REFRESH_MS);
  const inPlay = live.data?.matches.find((m) => String(m.match_id) === id) ?? null;
  if (error) return <div className="panel error">Could not load match {id}: {error}</div>;
  if (!v) return <div className="panel dim">Loading match…</div>;
  const h = v.header;
  const last = v.milestones.at(-1);
  const w = h.weather;
  return (
    <div className="match">
      <MatchNav id={h.match_id} competitionId={h.competition_id} kickoff={h.kickoff}
                title={`${kickoffLocal(h.kickoff)} · ${h.home} v ${h.away}`} />

      <section className="panel strip">
        <div className="strip-teams">
          <span className="team">{h.home}</span><span className="dim">v</span><span className="team">{h.away}</span>
        </div>
        <div className="strip-meta">
          <span>{h.competition} <span className="tier">{tierLabel(h.competition_tier)}</span>{h.round && <span className="dim"> · {h.round}</span>}</span>
          <span className="num">{kickoffLocal(h.kickoff)}</span>
          {h.result ? (
            <span className="num final-score" title="90-minute score">FT {h.result.home_goals}–{h.result.away_goals}</span>
          ) : inPlay ? (
            <span className="live-state"><span className="live-dot" aria-hidden />
              <span className="num live-clock">{liveClock(inPlay.status, inPlay.elapsed, inPlay.extra)}</span>
              <span className="num live-score">{score(inPlay)}</span></span>
          ) : <span className="num countdown">{countdown(h.kickoff, now)}</span>}
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

      {v.market && last && !h.result && (
        <section className="panel">
          <h3>Market <span className="dim">· bookmakers&apos; latest prices, margin removed, averaged over {v.market.bookmakers}; for information only, not a model input and not an edge signal</span></h3>
          <div className="market-cmp">
            {([["Home", "home"], ["Draw", "draw"], ["Away", "away"]] as const).map(([label, k]) => {
              const ours = last.selections.find((x) => x.market === "1X2" && x.selection === k);
              return (
                <span key={k}>{label}: market <span className="num">{pct(v.market![k])}%</span>
                  {ours && <> · ours <span className="num">{pct(ours.prob.p)}%</span></>}</span>
              );
            })}
            {v.market.over25 !== null && <span>Over 2.5: market <span className="num">{pct(v.market.over25)}%</span></span>}
            {v.market.btts !== null && <span>Both teams score: market <span className="num">{pct(v.market.btts)}%</span></span>}
          </div>
        </section>
      )}

      <section className="panel">
        <h3>Confidence</h3>
        <ConfidencePanel components={v.confidence} />
      </section>

      {last ? (
        <section className="panel">
          <h3>Markets <span className="dim">· {last.milestone} projection, posterior mean and 90% interval{showsOdds(h.team_level) && ", @ fair decimal odds 1/p"}{inPlay && !h.result && " · pre-match, not updated in play"}{h.result && " · outlined: came in"}</span></h3>
          <Markets m={last} odds={showsOdds(h.team_level)} result={h.result} />
          <h4>Milestone history</h4>
          <div className="history-wrap"><History ms={v.milestones} odds={showsOdds(h.team_level)} /></div>
        </section>
      ) : (
        <section className="panel dim">{v.no_projection_reason}</section>
      )}

      {last && v.half_coherence?.coherent && (
        <section className="panel">
          <h3>Half time <span className="dim">· {last.milestone} projection, the 90-minute score split by each side&apos;s first-half share; posterior mean and 90% interval</span></h3>
          <HalfTime m={last} odds={showsOdds(h.team_level)} />
        </section>
      )}
      {last && v.half_coherence && !v.half_coherence.coherent && (
        <section className="panel error">
          The half-time markets recomputed from the stored draws do not reproduce the published {v.half_coherence.milestone}{" "}
          projection (max difference {v.half_coherence.max_abs_diff.toExponential(2)}), so they are not shown (§11.2).
        </section>
      )}
      {last && !v.half_coherence && v.half_reason && (
        <section className="panel dim">Half time: {v.half_reason}</section>
      )}

      {v.qualify && (
        <section className="panel">
          <h3>Going through</h3>
          <GoingThrough q={v.qualify} home={h.home} away={h.away} odds={showsOdds(h.team_level)} />
        </section>
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
          <Heatmap m={v.matrix} home={h.home} away={h.away} odds={showsOdds(h.team_level)} />
        </section>
      )}

      {v.decomposition.length > 0 && (
        <section className="panel">
          <h3>Decomposition <span className="dim">· log expected goals, posterior mean and 90% interval per term</span></h3>
          <div className="waterfalls">{v.decomposition.map((d) => <Waterfall key={d.side} dec={d} />)}</div>
        </section>
      )}

      {v.squad && (
        <section className="panel">
          <h3>Squad and availability <span className="dim">· feed and lineups as captured; the projected lineup is from P3-b</span></h3>
          <SquadPanel s={v.squad} />
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
