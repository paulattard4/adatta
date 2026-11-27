"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { ProbCell } from "@/components/Prob";
import { useJson } from "@/lib/fetch";
import { dateLocal, fairOdds, kickoffLocal, mktOf, pct, positiveGaps, tierLabel, twoWaySide, type TwoWayKind } from "@/lib/format";
import { GapTrend, Reliability } from "@/components/Charts";
import type { ClvSummary, MeanScore, PlayedList, PlayedMatch, ScoreSummary } from "@/lib/types";

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
  const g = positiveGaps({ home: m.home_p.p, draw: m.draw_p.p, away: m.away_p.p }, m.market);
  return (
    <>
      {cells.map(([label, key, p]) => (
        <div key={key} className={m.outcome === key ? "played-cell hit" : "played-cell"}>
          <ProbCell prob={p} label={label} compact odds gap={g[key]} won={m.outcome === key} mkt={mktOf(m.market, key)} />
        </div>
      ))}
    </>
  );
}

/** A yes/no market: the selection we rated above the market before kickoff (when it had a
 * price), else the one that came in; the cell is marked when the selection shown came in. */
function Side({ prob, hit, kind, m, name, ll }:
  { prob: PlayedMatch["over25"]; hit: boolean; kind: TwoWayKind; m: PlayedMatch; name: string; ll: number | null }) {
  if (!prob) return <span className="c dim played-side">—</span>;
  const won = hit ? "yes" : "no";
  const s = twoWaySide(kind, prob, m.market, won);
  const came = s.side === won;
  return (
    <div className={came ? "played-cell hit played-side" : "played-cell played-side"}
         title={`${name}: log loss ${ll?.toFixed(3) ?? "—"}`}>
      <ProbCell prob={s.prob} label={s.label} compact odds gap={s.gap} won={came} mkt={s.mkt} />
    </div>
  );
}

/** The bookmakers' de-margined 1X2 before kickoff, beside ours (evaluation only). */
function MarketLine({ m }: { m: PlayedMatch }) {
  if (!m.market) return <span className="dim played-comp">Market: no price before kickoff</span>;
  const k = m.market;
  return (
    <span className="dim played-comp" title={`last price before kickoff, margin removed, averaged over ${k.bookmakers} bookmakers; 1X2 log loss ${k.scores.log_loss_1x2.toFixed(3)}`}>
      <span className="nw">Market: <span className="num">{pct(k.home)}% · {pct(k.draw)}% · {pct(k.away)}%</span></span>
      {k.over25 !== null && <> · <span className="nw">over 2.5 <span className="num">{pct(k.over25)}%</span></span></>}
      {k.btts !== null && <> · <span className="nw">BTTS <span className="num">{pct(k.btts)}%</span></span></>}
    </span>
  );
}

const sign = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;

/** Closing-line value: did the market move towards our first forecast? */
function Toward({ c }: { c: ClvSummary | null }) {
  if (!c || c.toward === null) return <span className="dim">—</span>;
  return (
    <span title={`${c.n_moved} matches where the market moved, of ${c.n} with an opening and a later closing price`}>
      <span className="num">{pct(c.toward, 0)}%</span>
      {c.toward_lo !== null && c.toward_hi !== null && <> <span className="num dim">[{pct(c.toward_lo, 0)}, {pct(c.toward_hi, 0)}]</span></>}
      <span className="dim"> of {c.n_moved}</span>
    </span>
  );
}

function Closed({ c }: { c: ClvSummary | null }) {
  if (!c || c.closed === null) return <span className="dim">—</span>;
  const sure = c.closed_lo !== null && c.closed_hi !== null && (c.closed_lo > 0 || c.closed_hi < 0);
  return (
    <span className={sure ? (c.closed > 0 ? "better" : "worse") : undefined}>
      <span className="num">{sign(c.closed)}</span>
      {c.closed_lo !== null && c.closed_hi !== null && <> <span className="num dim">[{sign(c.closed_lo)}, {sign(c.closed_hi)}]</span></>}
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
        scored better. The last two columns are closing-line value: between our first forecast and kickoff, did the market move
        towards us? It reads skill far sooner than results do. For evaluation only: odds are never a model input.
      </p>
      <div className="table-wrap">
        <table className="scores">
          <thead>
            <tr><th>Segment</th><th className="num">Priced</th>
              {keys.map((k) => <th key={k}>{METRIC_LABEL[k]}: market · ours − market</th>)}
              <th title="Between our first forecast and kickoff, the share of matches where the market moved towards our forecast (of those where it moved), with its 95% interval">Market moved our way</th>
              <th title="The share of our disagreement with the market that it closed by kickoff, sum(d·m)/sum(d·d), with a bootstrap 95% interval: 0 is no information, 1 is all the way">Disagreement closed</th></tr>
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
                <td><Toward c={s.clv} /></td>
                <td><Closed c={s.clv} /></td>
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
        <span className="filter-note">Only forecasts published before kickoff. Lower scores are better. @ fair decimal odds 1/p.</span>
      </div>
      {error && <div className="panel error">Could not load played matches: {error}. Is the API running on 127.0.0.1:8000?</div>}
      {loading && !data && <div className="panel dim">Loading played matches…</div>}
      {!loading && data && data.matches.length === 0 && (all.data?.matches.length ?? 0) > 0 && (
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
      {data && overall && (data.reliability.length > 0 || data.trend.length > 0) && (
        <section className="panel">
          <h2>Calibration and trend</h2>
          <p className="note">
            Left: when we gave an outcome a probability, how often it happened (home, draw and away pooled).
            Right: the running 1X2 log loss gap to the market as matches are added, from the tenth match on.
            {overall.n < FEW && <> With {overall.n} matches both are a first look.</>}
          </p>
          <div className="charts">
            {data.reliability.length > 0 && <Reliability bins={data.reliability} />}
            {data.trend.length > 0 && <GapTrend points={data.trend} />}
          </div>
        </section>
      )}
      {data && overall?.lineup && <LineupScores summary={data.summary} />}
      {data && data.matches.length > 0 && (
        <section className="panel">
          <h2>Matches</h2>
          <p className="note">
            ▲ marks every selection we rated above the market before kickoff (ours minus the market, in percentage points; in 1X2
            that can be two). Goals 2.5 and BTTS show the selection above the market, or the one that came in when there was no
            price. A green cell came in, and a green ▲ ✓ is a marked selection that came in. mkt @ is the bookmakers&apos; average
            decimal odds before kickoff, margin included; hover for the best price. A comparison, not a betting signal.
          </p>
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
              <Side prob={m.over25} hit={m.home_goals + m.away_goals > 2.5} kind="ou" m={m} name="Over/under 2.5"
                    ll={m.scores.log_loss_ou25} />
              <Side prob={m.btts} hit={m.home_goals > 0 && m.away_goals > 0} kind="btts" m={m} name="Both teams score"
                    ll={m.scores.log_loss_btts} />
              <span className="num c ll">{m.scores.log_loss_1x2.toFixed(3)}
                <span className="dim played-pp"> p {pct(hitP(m))}% · @{fairOdds(hitP(m))}</span>
              </span>
            </Link>
          ))}
        </section>
      )}
    </div>
  );
}
