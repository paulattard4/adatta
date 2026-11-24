"use client";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { useJson } from "@/lib/fetch";
import { dateLocal, kickoffLocal, pct } from "@/lib/format";
import type { McProb, TournamentView } from "@/lib/types";

const ROUND_LABEL: Record<string, string> = { R32: "R32", R16: "R16", QF: "QF", SF: "SF", F: "Final", "3P": "3rd pl." };
const MAIN = ["R32", "R16", "QF", "SF", "F"];

/** A probability with its Monte Carlo standard error on hover. */
function P({ x }: { x: McProb }) {
  return <td className="num r" title={`± ${pct(x[1], 2)} (Monte Carlo SE)`}>{pct(x[0])}</td>;
}

/** Stage D v2 tournament page: P(group position), P(advance), P(reach each round), P(win). */
export default function TournamentPage() {
  return <Suspense fallback={<div className="panel dim">Loading tournament…</div>}><Tournament /></Suspense>;
}

function Tournament() {
  const { key } = useParams<{ key: string }>();
  const shadow = useSearchParams().get("shadow") === "1";
  const { data: t, error } = useJson<TournamentView>(`/api/tournaments/${key}${shadow ? "?shadow=true" : ""}`);
  if (error) return <div className="panel error">No served forecast for {key}: {error}</div>;
  if (!t) return <div className="panel dim">Loading tournament…</div>;
  const rounds = MAIN.filter((r) => t.rounds.includes(r));
  const size = Math.max(...t.groups.map((g) => g.teams.length));
  return (
    <div className="tournament">
      <section className="panel strip">
        <div className="strip-teams"><span className="team">{t.name}</span>{t.shadow && <span className="shadow-flag dim">shadow</span>}</div>
        <div className="strip-meta">
          <span className="num">as of {kickoffLocal(t.as_of)}</span>
          <span>refit {dateLocal(t.refit_as_of)}{t.model_run_id != null && ` · run ${t.model_run_id}`}</span>
          <span className="num">{t.n_sims.toLocaleString()} simulated tournaments · Monte Carlo SE ≤ {pct(t.mc_se_max, 2)} pts</span>
          <span>{t.finals_fixed} results fixed</span>
        </div>
      </section>
      <p className="dim">
        {t.label}. {t.shadow && "Shadow: a past edition as of its opening Monday, for checking the page; not a live forecast. "}
        Ties the data cannot break (fair play, rankings, lots) are drawn at random: {pct(t.random_groups)}% of simulated group
        tables and {pct(t.random_thirds)}% of third-place rankings needed it. Hover a figure for its Monte Carlo SE.
      </p>
      <div className="tournament-groups">
        {t.groups.map((g) => (
          <section key={g.group} className="panel">
            <h3>Group {g.group}</h3>
            <div className="history-wrap">
              <table className="history">
                <thead>
                  <tr>
                    <th>Team</th>
                    {Array.from({ length: size }, (_, i) => <th key={i} className="r">{i + 1}{["st", "nd", "rd"][i] ?? "th"}</th>)}
                    <th className="r">Through</th>
                    {rounds.map((r) => <th key={r} className="r">{ROUND_LABEL[r] ?? r}</th>)}
                    <th className="r">Win</th>
                  </tr>
                </thead>
                <tbody>
                  {g.teams.map((tm) => (
                    <tr key={tm.team_id}>
                      <td>{tm.name}</td>
                      {tm.position.map((x, i) => <P key={i} x={x} />)}
                      <P x={tm.advance} />
                      {rounds.map((r) => <P key={r} x={tm.reach[r]} />)}
                      <P x={tm.win} />
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
