"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { ProbCell } from "@/components/Prob";
import { useJson } from "@/lib/fetch";
import { complement, dateLocal, fairOdds, kickoffLocal, pct, showsOdds, tierLabel } from "@/lib/format";
import type { MeanScore, PlayedList, PlayedMatch, ScoreSummary } from "@/lib/types";

const METRIC_LABEL: Record<string, string> = {
  log_loss_1x2: "1X2 log loss", rps_1x2: "1X2 RPS", brier_1x2: "1X2 Brier",
  log_loss_ou25: "Over/under 2.5 log loss", log_loss_btts: "Both teams score log loss",
};
const FEW = 200; // below this, a mean score says little
const isoDay = (d: Date) => d.toISOString().slice(0, 10);

const score = (s: MeanScore | undefined) =>
  s ? <><span className="num">{s.mean.toFixed(3)}</span>{s.lo !== null && s.hi !== null && <> <span className="num dim">[{s.lo.toFixed(3)}, {s.hi.toFixed(3)}]</span></>}</> : <span className="dim">—</span>;

/** The probability the forecast gave the 1X2 outcome that came in. */
const hitP = (m: PlayedMatch) => (m.outcome === "home" ? m.home_p : m.outcome === "draw" ? m.draw_p : m.away_p).p;

function Outcome({ m }: { m: PlayedMatch }) {
  const cells: [string, "home" | "draw" | "away", PlayedMatch["home_p"]][] = [
    ["Home", "home", m.home_p], ["Draw", "draw", m.draw_p], ["Away", "away", m.away_p],
  ];
  return (
    <>
      {cells.map(([label, key, p]) => (
        <div key={key} className={m.outcome === key ? "played-cell hit" : "played-cell"}>
          <ProbCell prob={p} label={label} compact odds={showsOdds(m.team_level)} />
        </div>
      ))}
    </>
  );
}

/** A two-way side market: the selection that came in and the probability it was given. */
function Side({ prob, hit, yes, no, market, ll, odds }:
  { prob: PlayedMatch["over25"]; hit: boolean; yes: string; no: string; market: string; ll: number | null; odds: boolean }) {
  if (!prob) return <span className="c dim played-side">—</span>;
  return (
    <div className="played-cell hit played-side" title={`${market}: log loss ${ll?.toFixed(3) ?? "—"}`}>
      <ProbCell prob={hit ? prob : complement(prob)} label={hit ? yes : no} compact odds={odds} />
    </div>
  );
}

/** The bookmakers' de-margined 1X2 before kickoff, beside ours (evaluation only). */
function MarketLine({ m }: { m: PlayedMatch }) {
  if (!m.market) return <span className="dim played-comp">Market: no price before kickoff</span>;
  const k = m.market;
  return (
    <span className="dim played-comp" title={`last price before kickoff, margin removed, averaged over ${k.bookmakers} bookmakers; 1X2 log loss ${k.scores.log_loss_1x2.toFixed(3)}`}>
      Market: <span className="num">{pct(k.home)}% · {pct(k.draw)}% · {pct(k.away)}%</span>
      {k.over25 !== null && <> · over 2.5 <span className="num">{pct(k.over25)}%</span></>}
      {k.btts !== null && <> · BTTS <span className="num">{pct(k.btts)}%</span></>}
    </span>
  );
}

/** Our scores against the bookmakers' on the same matches, paired by match. */
function MarketScores({ summary }: { summary: ScoreSummary[] }) {
  const rows = summary.filter((s) => s.vs_market.length > 0);
  if (!rows.length) return null;
  const keys = ["log_loss_1x2", "rps_1x2", "log_loss_ou25", "log_loss_btts"];
  const diff = (s: MeanScore | undefined) => {
    if (!s) return <span className="dim">—</span>;
    const sure = s.lo !== null && s.hi !== null && (s.hi < 0 || s.lo > 0);
    return (
      <span className={sure ? (s.mean < 0 ? "better" : "worse") : undefined}>
        <span className="num">{s.mean >= 0 ? "+" : ""}{s.mean.toFixed(3)}</span>
        {s.lo !== null && s.hi !== null && <> <span className="num dim">[{s.lo >= 0 ? "+" : ""}{s.lo.toFixed(3)}, {s.hi >= 0 ? "+" : ""}{s.hi.toFixed(3)}]</span></>}
      </span>
    );
  };
  return (
    <section className="panel">
      <h2>Against the market</h2>
      <p className="note">
        The bookmakers&apos; last price before kickoff, margin removed, averaged over the bookmakers quoting the whole market, scored
        on the same matches. Ours minus the market&apos;s, paired by match, with its 95% interval: below zero means our forecast
        scored better. For evaluation only: odds are never a model input.
      </p>
      <div className="table-wrap">
        <table className="scores">
          <thead>
            <tr><th>Segment</th><th className="num">Priced</th>
              {keys.map((k) => <th key={k}>{METRIC_LABEL[k]}: market · ours − market</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.segment}>
                <td>{s.segment === "all" ? "All" : <>{s.segment} <span className="tier">{tierLabel(s.tier)}</span></>}</td>
                <td className="num">{s.vs_market[0]?.n ?? 0} <span className="dim">of {s.n}</span></td>
                {keys.map((k) => {
                  const mk = s.market_scores.find((x) => x.metric === k);
                  return <td key={k}>{mk ? <span className="num">{mk.mean.toFixed(3)}</span> : "—"} · {diff(s.vs_market.find((x) => x.metric === k))}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** How each side's projected lineup (as of kickoff - 24 h) did: starters, bench, formation. */
function LineupLine({ m }: { m: PlayedMatch }) {
  const xs = [m.home_lineup, m.away_lineup];
  if (!xs.some(Boolean)) return null;
  const xi = xs.map((x) => (x ? `${x.xi_hits}` : "–")).join(" · ");
  const bench = xs.map((x) => (x ? `${x.bench_hits}/${x.bench_n}` : "–")).join(" · ");
  const shape = xs.map((x) => (x?.formation_top === null || !x ? "–" : x.formation_top ? "✓" : "✗")).join(" ");
  return (
    <span className="dim played-comp" title="projected lineup as of kickoff − 24 h, home · away: starters who started of 11, projected bench named on the bench, formation was P3-c's top pick">
      Lineups: XI <span className="num">{xi}</span> of 11 · bench <span className="num">{bench}</span>
      {xs.some((x) => x?.formation_top !== null && x !== null) && <> · shape <span className="num">{shape}</span></>}
    </span>
  );
}

function LineupScores({ summary }: { summary: ScoreSummary[] }) {
  const rows = summary.filter((s) => s.lineup);
  return (
    <section className="panel">
      <h2>Projected lineups</h2>
      <p className="note">
        The stored projection as of kickoff − 24 h against the announced lineup, per team-match. Players the injury feed lists as out
        are kept out of the projected XI; the P3-b column is its own top XI without that rule. Positions compare the pitch shown (the
        projected XI placed in the last lineup in the projected formation) with that last lineup unchanged, where the league&apos;s grid
        is positional and the announced formation is the pitch&apos;s. Reported, not a verdict: projections start on 20 November.
      </p>
      <div className="table-wrap">
        <table className="scores">
          <thead>
            <tr><th>Segment</th><th className="num">Team-matches</th><th>XI started, of 11</th><th>P3-b top XI, of 11</th>
              <th>Bench named on the bench</th><th>Formation top pick</th>
              <th title="share of the 11 positions whose player started there; positional leagues, announced formation as the pitch's">Positions right: pitch · last lineup</th></tr>
          </thead>
          <tbody>
            {rows.map((s) => {
              const l = s.lineup!;
              return (
                <tr key={s.segment}>
                  <td>{s.segment === "all" ? "All" : <>{s.segment} <span className="tier">{tierLabel(s.tier)}</span></>}</td>
                  <td className="num">{l.n}</td>
                  <td className="num">{l.xi_mean.toFixed(2)}</td>
                  <td className="num">{l.xi_raw_mean.toFixed(2)}</td>
                  <td className="num">{l.bench_n > 0 ? `${pct(l.bench_hits / l.bench_n, 0)}%` : "—"} <span className="dim">({l.bench_hits}/{l.bench_n})</span></td>
                  <td className="num">{l.formation_top !== null ? `${pct(l.formation_top, 0)}%` : "—"} <span className="dim">(n {l.n_formation})</span></td>
                  <td className="num">{l.slot_acc !== null && l.slot_acc_last !== null
                    ? <>{pct(l.slot_acc, 0)}% · {pct(l.slot_acc_last, 0)}%</> : "—"} <span className="dim">(n {l.n_slot})</span></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** Finished matches against the forecast published before kickoff (no reconstructions). */
export default function Played() {
  const [comp, setComp] = useState("");
  // default: the last day (owner, 2026-11-27); clear the dates for every played match
  const today = useMemo(() => new Date(), []);
  const [from, setFrom] = useState(isoDay(new Date(today.getTime() - 86400e3)));
  const [to, setTo] = useState(isoDay(today));
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
            {comps.map((c) => <option key={c.competition_id} value={c.competition_id ?? ""}>{c.segment} ({tierLabel(c.tier)})</option>)}
          </select>
        </label>
        <label>From <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label>
        <label>To <input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label>
        <span className="filter-note">Only forecasts published before kickoff. Lower scores are better. @ fair decimal odds 1/p (senior).</span>
      </div>
      {error && <div className="panel error">Could not load played matches: {error}. Is the API running on 127.0.0.1:8000?</div>}
      {loading && !data && <div className="panel dim">Loading played matches…</div>}
      {data && data.matches.length === 0 && (all.data?.matches.length ?? 0) > 0 && (
        <div className="panel dim">
          No forecast match finished {from || to ? <>between {from || "the start"} and {to || "now"}</> : "in this selection"}.
          {" "}Widen the dates, or clear them for every played match.
        </div>
      )}
      {data && data.matches.length === 0 && all.data && all.data.matches.length === 0 && (
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
                    <td>{s.segment === "all" ? "All" : <>{s.segment} <span className="tier">{tierLabel(s.tier)}</span></>}</td><td className="num">{s.n}</td>
                    {Object.keys(METRIC_LABEL).map((k) => <td key={k}>{score(s.scores.find((x) => x.metric === k))}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {data && overall && <MarketScores summary={data.summary} />}
      {data && overall?.lineup && <LineupScores summary={data.summary} />}
      {data && data.matches.length > 0 && (
        <section className="panel">
          <h2>Matches</h2>
          <div className="played-head">
            <span>Kickoff</span><span>Match</span><span className="c">Result</span><span className="pc">Home</span>
            <span className="pc">Draw</span><span className="pc">Away</span><span className="pc">Goals 2.5</span>
            <span className="pc">BTTS</span><span className="c">1X2 log loss</span>
          </div>
          {data.matches.map((m) => (
            <Link key={m.match_id} href={`/match/${m.match_id}`} className="played-row"
                  title={`${m.competition} (${tierLabel(m.competition_tier)}) · forecast ${m.milestone}, published ${kickoffLocal(m.generated_at)}`}>
              <span className="num kickoff">{kickoffLocal(m.kickoff)}</span>
              <span className="teams">{m.home} <span className="dim">v</span> {m.away}<span className="dim played-comp"> · {m.competition} <span className="tier">{tierLabel(m.competition_tier)}</span></span>
                <MarketLine m={m} />
                <LineupLine m={m} /></span>
              <span className="num c result">{m.home_goals}–{m.away_goals}</span>
              <Outcome m={m} />
              <Side prob={m.over25} hit={m.home_goals + m.away_goals > 2.5} yes="Over 2.5" no="Under 2.5"
                    market="Over/under 2.5" ll={m.scores.log_loss_ou25} odds={showsOdds(m.team_level)} />
              <Side prob={m.btts} hit={m.home_goals > 0 && m.away_goals > 0} yes="BTTS Yes" no="BTTS No"
                    market="Both teams score" ll={m.scores.log_loss_btts} odds={showsOdds(m.team_level)} />
              <span className="num c ll">{m.scores.log_loss_1x2.toFixed(3)}
                <span className="dim played-pp"> p {pct(hitP(m))}%{showsOdds(m.team_level) && <> · @{fairOdds(hitP(m))}</>}</span>
              </span>
            </Link>
          ))}
        </section>
      )}
    </div>
  );
}
