import { dateLocal, kickoffLocal, pct } from "@/lib/format";
import type { Absence, FormationView, Lineup, PitchPlayer, ProjectedPlayer, Projection, Squad, SquadPlayer, TeamSquad } from "@/lib/types";

const SHOW_MIN = 0.1; // pool players with P(start) + P(bench) below this are left out of the list

const LINE_ORDER: Record<string, number> = { G: 0, D: 1, M: 2, F: 3, U: 4 };

/** The most likely XI as a lineup: the keeper most likely to start, then the 10 outfield
 *  players most likely to start, by line. The rest follow by P(start). */
export function likelyXi(xs: ProjectedPlayer[]): { xi: ProjectedPlayer[]; rest: ProjectedPlayer[] } {
  const byStart = [...xs].sort((a, b) => b.p_start - a.p_start);
  const gk = byStart.find((p) => p.pos === "G");
  const outfield = byStart.filter((p) => p !== gk && p.pos !== "G").slice(0, gk ? 10 : 11);
  const chosen = new Set([gk, ...outfield].filter(Boolean));
  const xi = [...chosen].filter((p): p is ProjectedPlayer => p !== undefined)
    .sort((a, b) => (LINE_ORDER[a.pos] ?? 4) - (LINE_ORDER[b.pos] ?? 4) || b.p_start - a.p_start);
  return { xi, rest: byStart.filter((p) => !chosen.has(p)) };
}

function Projected({ xs, pr, announced }: { xs: ProjectedPlayer[]; pr: Projection; announced: boolean }) {
  const { xi, rest } = likelyXi(xs);
  // players the injury feed lists are always shown, however unlikely the model makes them
  const others = rest.filter((p) => p.p_start + p.p_bench >= SHOW_MIN || p.status !== null);
  const shown = [...xi, ...others];
  return (
    <>
      <h4>{announced ? "Projected before the announcement" : "Projected lineup"} <span className="dim">· P3-b</span></h4>
      <p className="squad-meta dim">
        As of <span className="num">{kickoffLocal(pr.as_of)}</span> · refit <span className="num">{dateLocal(pr.refit_as_of)}</span>
        {" "}· the model does not use the injury feed
      </p>
      <table className="proj">
        <thead><tr><th>Most likely XI, then the rest</th><th className="r">Start</th><th className="r">Bench</th><th className="r">Min</th></tr></thead>
        <tbody>
          {shown.map((p, i) => (
            <tr key={`${p.name}-${i}`} className={i === xi.length && i > 0 ? "proj-cut" : undefined}>
              <td>
                <span className="num dim proj-pos">{p.pos}</span> {p.name}
                {p.status && <span className={`absence-tag ${p.status}`} title="listed by the injury feed; not used by the model">{p.status}</span>}
              </td>
              <td className="num r">{pct(p.p_start, 0)}%</td>
              <td className="num r dim">{pct(p.p_bench, 0)}%</td>
              <td className="num r dim">{p.emin.toFixed(0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {shown.length < xs.length && <p className="squad-meta dim">{xs.length - shown.length} more in the pool, each under {pct(SHOW_MIN, 0)}% to be named and not listed by the feed.</p>}
    </>
  );
}

const LINE_LABEL: Record<string, string> = { G: "GK", D: "D", M: "M", F: "F" };

/** Grid rows, forwards on top; within a row, column 1 (the team's left) first. */
export function pitchRows(xs: PitchPlayer[]): PitchPlayer[][] {
  const rows = new Map<number, PitchPlayer[]>();
  for (const p of xs) rows.set(p.row, [...(rows.get(p.row) ?? []), p]);
  return [...rows.keys()].sort((a, b) => b - a).map((r) => rows.get(r)!.sort((a, b) => a.col - b.col));
}

function FormationBlock({ f }: { f: FormationView }) {
  const label = (x: string) => (x === "other" ? "other shape" : x);
  return (
    <>
      <h4>Projected formation <span className="dim">· P3-c</span></h4>
      <div className="form-opts">
        {f.options.map((o, i) => (
          <span key={o.formation} className={`form-opt${i === 0 ? " top" : ""}`}>
            <span className="num">{label(o.formation)}</span><span className="num">{pct(o.p, 0)}%</span>
          </span>
        ))}
      </div>
      <p className="squad-meta dim">
        As of <span className="num">{kickoffLocal(f.as_of)}</span> · refit <span className="num">{dateLocal(f.refit_as_of)}</span>
        {" "}· the model does not use opponent strength
      </p>
      {f.last_kickoff ? (
        <>
          <div className="squad-sub dim">
            Last lineup in <span className="num">{f.pitch_formation}</span> (<span className="num">{dateLocal(f.last_kickoff)}</span>)
            {!f.positional && " · positions by line only"}
          </div>
          <div className="pitch" aria-label={`last lineup in ${f.pitch_formation}`}>
            {pitchRows(f.pitch).map((row) => (
              <div className="pitch-row" key={row[0].row}>
                {row.map((p) => (
                  <div className="pitch-player" key={`${p.row}-${p.col}`}>
                    <span className="pitch-code num">{f.positional ? p.code : (LINE_LABEL[p.code] ?? p.code)}</span>
                    <span className="pitch-name" title={p.name}>{p.name}</span>
                    <span className="num dim">{p.p_start !== null ? `${pct(p.p_start, 0)}%` : "–"}</span>
                    {p.status && <span className={`absence-tag ${p.status}`} title="listed by the injury feed">{p.status}</span>}
                  </div>
                ))}
              </div>
            ))}
          </div>
          <p className="squad-meta dim">History, not a forecast of who starts: the percentages are P3-b&apos;s chance to start (– not in this fixture&apos;s pool).</p>
        </>
      ) : <p className="squad-meta dim">No previous lineup in <span className="num">{f.pitch_formation}</span>.</p>}
    </>
  );
}

function Players({ xs }: { xs: SquadPlayer[] }) {
  return (
    <ul className="squad-list">
      {xs.map((p, i) => (
        <li key={`${p.number}-${p.name}-${i}`}>
          <span className="num dim">{p.number ?? "–"}</span>
          <span className="squad-name">{p.name}</span>
          <span className="num dim">{p.pos ?? ""}</span>
        </li>
      ))}
    </ul>
  );
}

function LineupBlock({ lu }: { lu: Lineup }) {
  return (
    <>
      <h4>Lineup</h4>
      <p className="squad-meta dim">
        Announced, first seen <span className="num">{kickoffLocal(lu.observed_at)}</span>
        {lu.formation && <> · <span className="num">{lu.formation}</span></>}
        {lu.coach && <> · {lu.coach}</>}
      </p>
      <div className="squad-sub dim">Starting XI</div>
      <Players xs={lu.starters} />
      {lu.bench.length > 0 && <><div className="squad-sub dim">Bench</div><Players xs={lu.bench} /></>}
    </>
  );
}

function Absences({ xs }: { xs: Absence[] }) {
  return (
    <div>
      {xs.map((a) => (
        <div className="absence" key={`${a.name}-${a.since}`}>
          <span className={`absence-tag ${a.status}`}>{a.status}</span>
          <span>{a.name}{a.reason && <span className="dim"> · {a.reason}</span>}
            <span className="dim small"> · listed since <span className="num">{kickoffLocal(a.since)}</span></span></span>
        </div>
      ))}
    </div>
  );
}

function Side({ t, checked, note, pr }: { t: TeamSquad; checked: boolean; note: string | null; pr: Projection | null }) {
  return (
    <div className="squad-side">
      <div className="squad-team">{t.team}</div>
      <h4>Availability</h4>
      {!checked ? <p className="dim squad-meta">Not captured for this competition.</p>
        : t.absences.length === 0 ? <p className="dim squad-meta">No absences listed by the feed.</p>
        : <Absences xs={t.absences} />}
      {t.lineup ? <LineupBlock lu={t.lineup} /> : <><h4>Lineup</h4><p className="dim squad-meta">{note ?? "No lineup captured."}</p></>}
      {t.formation && <FormationBlock f={t.formation} />}
      {pr && t.projected.length > 0 && <Projected xs={t.projected} pr={pr} announced={t.lineup !== null} />}
    </div>
  );
}

/** Captured availability (injury feed) and announced lineups, as they stood (docs/DASHBOARD_TASKS.md). */
export function SquadPanel({ s }: { s: Squad }) {
  if (s.hidden_reason) return <p className="dim">{s.hidden_reason}</p>;
  const checked = s.status_checked_at !== null;
  return (
    <>
      <div className="squad">
        <Side t={s.home} checked={checked} note={s.lineup_note} pr={s.projection} />
        <Side t={s.away} checked={checked} note={s.lineup_note} pr={s.projection} />
      </div>
      <p className="squad-foot dim">
        As of <span className="num">{kickoffLocal(s.as_of)}</span>
        {checked && <> · injury feed last checked <span className="num">{kickoffLocal(s.status_checked_at!)}</span></>}
        {" "}· provider lists, not model output; doubtful players may still play.
      </p>
    </>
  );
}
