"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { ProfitLine, type PLPoint } from "@/components/Charts";
import { useJson } from "@/lib/fetch";
import { dateLocal, kickoffLocal, pct, tierLabel } from "@/lib/format";
import type { Bet, BetList } from "@/lib/types";

/** Every selection we rated above the market before kickoff (the dashboard's ▲), 1 unit
 * each at the bookmakers' odds as quoted, settled on the 90-minute score. */

const MARKETS: Record<Bet["market"], string> = { "1X2": "1X2", "OU2.5": "Over/under 2.5", BTTS: "Both teams score" };
const SELECTIONS: Record<Bet["selection"], [Bet["market"], string]> = {
  home: ["1X2", "Home"], draw: ["1X2", "Draw"], away: ["1X2", "Away"],
  over: ["OU2.5", "Over 2.5"], under: ["OU2.5", "Under 2.5"], yes: ["BTTS", "BTTS Yes"], no: ["BTTS", "BTTS No"],
};
const GAP_BANDS: [number, number, string][] = [[0, 2, "0–2"], [2, 5, "2–5"], [5, 10, "5–10"], [10, Infinity, "10+"]];
const ODDS_BANDS: [number, number, string][] = [[1, 1.5, "under 1.50"], [1.5, 2, "1.50–2.00"], [2, 3, "2.00–3.00"],
  [3, 5, "3.00–5.00"], [5, Infinity, "5.00+"]];
const PAGE = 100;

type Odds = "mean" | "best";
const price = (b: Bet, o: Odds) => (o === "best" ? b.odds_best : b.odds_mean);
const pl = (b: Bet, o: Odds) => (b.won ? price(b, o) - 1 : -1);
const units = (v: number, d = 2) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}`;
const tone = (v: number) => (v > 0 ? "better" : v < 0 ? "worse" : "");

type Totals = {
  n: number; won: number; pl: number; roi: number; lo: number | null; hi: number | null;
  odds: number; ours: number; market: number; gap: number;
};

/** Flat 1-unit stakes: profit, return on stake with a 95% interval (the mean of per-bet
 * profit ± 1.96 sd/√n), and the win rate beside the rates we and the market implied. */
function totals(bets: Bet[], o: Odds): Totals {
  const n = bets.length;
  const xs = bets.map((b) => pl(b, o));
  const sum = xs.reduce((a, b) => a + b, 0);
  const mean = n ? sum / n : 0;
  const sd = n > 1 ? Math.sqrt(xs.reduce((a, b) => a + (b - mean) ** 2, 0) / (n - 1)) : null;
  const half = sd === null ? null : 1.96 * sd / Math.sqrt(n);
  const avg = (f: (b: Bet) => number) => (n ? bets.reduce((a, b) => a + f(b), 0) / n : 0);
  return {
    n, won: bets.filter((b) => b.won).length, pl: sum, roi: mean,
    lo: half === null ? null : mean - half, hi: half === null ? null : mean + half,
    odds: avg((b) => price(b, o)), ours: avg((b) => b.ours), market: avg((b) => b.market_p), gap: avg((b) => b.gap),
  };
}

function Roi({ t }: { t: Totals }) {
  return (
    <span className={tone(t.roi)}>
      <span className="num">{units(100 * t.roi, 1)}%</span>
      {t.lo !== null && t.hi !== null && <span className="num dim"> [{units(100 * t.lo, 1)}, {units(100 * t.hi, 1)}]</span>}
    </span>
  );
}

function Breakdown({ title, groups, odds, limit }:
  { title: string; groups: [string, Bet[]][]; odds: Odds; limit?: number }) {
  const [open, setOpen] = useState(false);
  const all = groups.filter(([, bs]) => bs.length > 0);
  if (all.length === 0) return null;
  const rows = limit && !open ? all.slice(0, limit) : all;
  return (
    <div className="table-wrap">
      <table className="scores bets-table breakdown-table">
        <thead>
          <tr>
            <th>{title}</th><th>Bets</th><th>Won</th>
            <th title="How often the selection came in, against the average probability we gave it and the market's (margin removed)">Win rate · ours · market</th>
            <th>Avg odds</th><th>P/L (units)</th><th title="Profit per unit staked, with its 95% interval">ROI [95%]</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([k, bs]) => {
            const t = totals(bs, odds);
            return (
              <tr key={k}>
                <td>{k}</td><td className="num">{t.n}</td><td className="num">{t.won}</td>
                <td className="num">{pct(t.won / t.n)}% <span className="dim">· {pct(t.ours)}% · {pct(t.market)}%</span></td>
                <td className="num">{t.odds.toFixed(2)}</td>
                <td className={`num ${tone(t.pl)}`}>{units(t.pl)}</td>
                <td><Roi t={t} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {limit != null && all.length > limit && (
        <button type="button" className="more" onClick={() => setOpen(!open)}>
          {open ? `Show the latest ${limit}` : `Show all ${all.length}`}
        </button>
      )}
    </div>
  );
}

/** Tier order within a country: numbered tiers first (1, 2, ...), then cups, then the rest. */
const tierRank = (tier: string) =>
  /^\d+$/.test(tier) ? Number(tier) : tier === "cup" ? 50 : tier === "national" ? 90 : 99;
const byCountryTier = (a: Bet, b: Bet) =>
  a.country.localeCompare(b.country) || tierRank(a.tier) - tierRank(b.tier) || a.competition.localeCompare(b.competition);

/** A UTC day (the date filter's) as "Sat 28 Nov 2026". */
const dayLabel = (day: string) =>
  new Date(`${day}T12:00:00Z`).toLocaleDateString(undefined, {
    weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
  });

const group = (bets: Bet[], key: (b: Bet) => string, order?: string[]): [string, Bet[]][] => {
  const m = new Map<string, Bet[]>();
  for (const k of order ?? []) m.set(k, []);
  for (const b of bets) { const k = key(b); if (!m.has(k)) m.set(k, []); m.get(k)!.push(b); }
  return [...m.entries()];
};
const band = (v: number, bands: [number, number, string][]) => bands.find(([a, z]) => v >= a && v < z)?.[2] ?? "";

const num = (s: string) => (s.trim() === "" ? null : Number(s));

export default function BetsPage() {
  const { data, error, loading } = useJson<BetList>("/api/bets");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [market, setMarket] = useState("");
  const [selection, setSelection] = useState("");
  const [country, setCountry] = useState("");
  const [league, setLeague] = useState("");
  const [level, setLevel] = useState("");
  const [model, setModel] = useState("");
  const [gapMin, setGapMin] = useState("");
  const [gapMax, setGapMax] = useState("");
  const [oddsMin, setOddsMin] = useState("");
  const [oddsMax, setOddsMax] = useState("");
  const [odds, setOdds] = useState<Odds>("mean");
  const [shown, setShown] = useState(PAGE);

  const all = useMemo(() => data?.bets ?? [], [data]);
  const countries = useMemo(() => [...new Set(all.map((b) => b.country))].sort(), [all]);
  const leagues = useMemo(() => {
    const m = new Map<number, Bet>();
    for (const b of all) if (!country || b.country === country) m.set(b.competition_id, b);
    return [...m.values()].sort(byCountryTier);
  }, [all, country]);
  const levels = useMemo(() => [...new Set(all.map((b) => b.team_level))].sort(), [all]);

  const bets = useMemo(() => {
    const gl = num(gapMin), gh = num(gapMax), ol = num(oddsMin), oh = num(oddsMax);
    return all.filter((b) => {
      const day = b.kickoff.slice(0, 10);
      const o = price(b, odds);
      return (!from || day >= from) && (!to || day <= to)
        && (!market || b.market === market) && (!selection || b.selection === selection)
        && (!country || b.country === country) && (!league || String(b.competition_id) === league)
        && (!level || b.team_level === level)
        && (!model || (model === "point") === b.point)
        && (gl === null || 100 * b.gap >= gl) && (gh === null || 100 * b.gap <= gh)
        && (ol === null || o >= ol) && (oh === null || o <= oh);
    });
  }, [all, from, to, market, selection, country, league, level, model, gapMin, gapMax, oddsMin, oddsMax, odds]);

  const t = useMemo(() => totals(bets, odds), [bets, odds]);
  const curve = useMemo(() => {
    let run = 0;
    return bets.map((b, i): PLPoint => {
      run += pl(b, odds);
      return {
        n: i + 1, total: run, label: dateLocal(b.kickoff),
        detail: `${dateLocal(b.kickoff)} ${b.home} v ${b.away}, ${SELECTIONS[b.selection][1]} @${price(b, odds).toFixed(2)} ${b.won ? "won" : "lost"}`,
      };
    });
  }, [bets, odds]);
  const latest = useMemo(() => bets.slice().reverse(), [bets]);
  const reset = () => {
    setFrom(""); setTo(""); setMarket(""); setSelection(""); setCountry(""); setLeague(""); setLevel(""); setModel("");
    setGapMin(""); setGapMax(""); setOddsMin(""); setOddsMax(""); setShown(PAGE);
  };

  return (
    <div>
      <div className="filters panel">
        <label>From <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label>
        <label>To <input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label>
        <label>Market
          <select value={market} onChange={(e) => { setMarket(e.target.value); setSelection(""); }}>
            <option value="">All</option>
            {Object.entries(MARKETS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label>Selection
          <select value={selection} onChange={(e) => setSelection(e.target.value)}>
            <option value="">All</option>
            {Object.entries(SELECTIONS).filter(([, [m]]) => !market || m === market)
              .map(([k, [, v]]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label>Country
          <select value={country} onChange={(e) => { setCountry(e.target.value); setLeague(""); }}>
            <option value="">All</option>
            {countries.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label>Competition
          <select value={league} onChange={(e) => setLeague(e.target.value)}>
            <option value="">All</option>
            {leagues.map((b) => <option key={b.competition_id} value={b.competition_id}>{b.competition}{country ? "" : ` (${b.country})`}</option>)}
          </select>
        </label>
        <label>Team level
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">All</option>
            {levels.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </label>
        <label title="Full model: forecasts with an interval. Baseline: the C2 leagues' point forecasts (D54).">Model
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            <option value="">All</option><option value="full">Full model</option><option value="point">Baseline</option>
          </select>
        </label>
        <label title="Our probability minus the market's (margin removed), in percentage points">Gap pts
          <span className="range">
            <input type="number" inputMode="decimal" min={0} step={0.5} placeholder="min" value={gapMin} onChange={(e) => setGapMin(e.target.value)} />
            <input type="number" inputMode="decimal" min={0} step={0.5} placeholder="max" value={gapMax} onChange={(e) => setGapMax(e.target.value)} />
          </span>
        </label>
        <label title="The bookmakers' decimal odds as quoted">Odds
          <span className="range">
            <input type="number" inputMode="decimal" min={1} step={0.1} placeholder="min" value={oddsMin} onChange={(e) => setOddsMin(e.target.value)} />
            <input type="number" inputMode="decimal" min={1} step={0.1} placeholder="max" value={oddsMax} onChange={(e) => setOddsMax(e.target.value)} />
          </span>
        </label>
        <label>Settle at
          <span className="seg seg-inline" role="group" aria-label="Odds to settle at">
            <button type="button" aria-pressed={odds === "mean"} onClick={() => setOdds("mean")}
                    title="The bookmakers' average odds as quoted, the last before kickoff">Average</button>
            <button type="button" aria-pressed={odds === "best"} onClick={() => setOdds("best")}
                    title="The best quoted odds, the last before kickoff: an upper bound, as one account rarely gets every best price">Best</button>
          </span>
        </label>
        <button type="button" className="reset" onClick={reset}>Reset</button>
      </div>

      {error && <div className="panel error">Could not load bets: {error}. Is the API running on 127.0.0.1:8000?</div>}
      {loading && !data && <div className="panel dim">Loading bets…</div>}

      {data && (
        <section className="panel">
          <h2>Selections above the market</h2>
          <p className="note">
            Every selection we rated above the market before kickoff (ours minus the market with its margin removed: the ▲ on the
            other pages), 1 unit each at the bookmakers&apos; odds as quoted (never de-margined), the last before kickoff, settled
            on the 90-minute score. Our probability is the last forecast published before kickoff. 1X2, over/under 2.5 and both
            teams score; odds were first captured in November 2026, so samples are small: read the intervals.
          </p>
          {t.n === 0 ? <p className="dim">No bets match these filters.</p> : (
            <>
              <div className="tiles">
                <div className="tile"><span className="tile-label">Bets</span><span className="num tile-value">{t.n}</span>
                  <span className="dim small">{new Set(bets.map((b) => b.match_id)).size} matches</span></div>
                <div className="tile"><span className="tile-label">Won</span><span className="num tile-value">{t.won}</span>
                  <span className="dim small">{pct(t.won / t.n)}% (ours {pct(t.ours)}%, market {pct(t.market)}%)</span></div>
                <div className="tile"><span className="tile-label">Profit / loss</span>
                  <span className={`num tile-value ${tone(t.pl)}`}>{units(t.pl)}</span>
                  <span className="dim small">units, {t.n} staked</span></div>
                <div className="tile"><span className="tile-label">ROI</span>
                  <span className={`num tile-value ${tone(t.roi)}`}>{units(100 * t.roi, 1)}%</span>
                  <span className="dim small">{t.lo !== null && t.hi !== null
                    ? <>95% interval <span className="num">{units(100 * t.lo, 1)}% to {units(100 * t.hi, 1)}%</span></>
                    : "per unit staked"}</span></div>
                <div className="tile"><span className="tile-label">Avg odds</span><span className="num tile-value">{t.odds.toFixed(2)}</span>
                  <span className="dim small">{odds === "best" ? "best" : "average"} quoted</span></div>
                <div className="tile"><span className="tile-label">Avg gap</span><span className="num tile-value">+{(100 * t.gap).toFixed(1)}</span>
                  <span className="dim small">points above the market</span></div>
              </div>
              <ProfitLine points={curve} />
            </>
          )}
        </section>
      )}

      {data && t.n > 0 && (
        <section className="panel">
          <h2>Breakdown</h2>
          <div className="breakdowns">
            <Breakdown title="Selection" odds={odds}
                       groups={group(bets, (b) => SELECTIONS[b.selection][1], Object.values(SELECTIONS).map(([, v]) => v))} />
            <Breakdown title="Competition" odds={odds}
                       groups={group([...bets].sort(byCountryTier), (b) => `${b.country} · ${b.competition} (${tierLabel(b.tier)})`)} />
            <Breakdown title="Gap (pts)" odds={odds} groups={group(bets, (b) => band(100 * b.gap, GAP_BANDS), GAP_BANDS.map((x) => x[2]))} />
            <Breakdown title="Odds" odds={odds} groups={group(bets, (b) => band(price(b, odds), ODDS_BANDS), ODDS_BANDS.map((x) => x[2]))} />
            <Breakdown title="Date (UTC)" odds={odds} limit={14}
                       groups={group([...bets].sort((a, b) => b.kickoff.localeCompare(a.kickoff)), (b) => dayLabel(b.kickoff.slice(0, 10)))} />
          </div>
        </section>
      )}

      {data && t.n > 0 && (
        <section className="panel">
          <h2>Bets <span className="dim small">latest first</span></h2>
          <div className="table-wrap">
            <table className="scores bets-table bet-list">
              <thead>
                <tr><th>Kickoff</th><th>Match</th><th>Result</th><th>Selection</th><th>Ours · market</th><th>Gap</th>
                  <th>Odds avg · best</th><th>P/L</th></tr>
              </thead>
              <tbody>
                {latest.slice(0, shown).map((b) => (
                  <tr key={`${b.match_id}-${b.selection}`}>
                    <td className="num">{kickoffLocal(b.kickoff)}</td>
                    <td><Link href={`/match/${b.match_id}`}>{b.home} v {b.away}</Link>
                      <span className="dim"> · {b.competition} <span className="tier">{tierLabel(b.tier)}</span></span></td>
                    <td className="num">{b.home_goals}–{b.away_goals}</td>
                    <td className={b.won ? "won-cell" : ""}>{SELECTIONS[b.selection][1]}{b.won && " ✓"}</td>
                    <td className="num">{pct(b.ours)}% · {pct(b.market_p)}%</td>
                    <td className="num">+{(100 * b.gap).toFixed(1)}</td>
                    <td className="num" title={`${b.bookmakers} bookmaker${b.bookmakers === 1 ? "" : "s"}`}>{b.odds_mean.toFixed(2)} · {b.odds_best.toFixed(2)}</td>
                    <td className={`num ${tone(pl(b, odds))}`}>{units(pl(b, odds))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {shown < latest.length && (
            <button type="button" className="more" onClick={() => setShown(shown + PAGE)}>
              Show {Math.min(PAGE, latest.length - shown)} more of {latest.length - shown}
            </button>
          )}
        </section>
      )}
    </div>
  );
}
