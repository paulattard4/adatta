import { dateLocal, kickoffLocal, pct } from "@/lib/format";
import type { Absence, Lineup, ProjectedPlayer, Projection, Squad, SquadPlayer, TeamSquad } from "@/lib/types";

const SHOW_MIN = 0.1; // pool players with P(start) + P(bench) below this are left out of the list

function Projected({ xs, pr, announced }: { xs: ProjectedPlayer[]; pr: Projection; announced: boolean }) {
  const shown = xs.filter((p) => p.p_start + p.p_bench >= SHOW_MIN);
  return (
    <>
      <h4>{announced ? "Projected before the announcement" : "Projected lineup"} <span className="dim">· P3-b</span></h4>
      <p className="squad-meta dim">
        As of <span className="num">{kickoffLocal(pr.as_of)}</span> · refit <span className="num">{dateLocal(pr.refit_as_of)}</span>
        {" "}· the model does not use the injury feed
      </p>
      <table className="proj">
        <thead><tr><th>Player</th><th className="r">Start</th><th className="r">Bench</th><th className="r">Min</th></tr></thead>
        <tbody>
          {shown.map((p, i) => (
            <tr key={`${p.name}-${i}`} className={i === 10 ? "proj-cut" : undefined}>
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
      {shown.length < xs.length && <p className="squad-meta dim">{xs.length - shown.length} more in the pool, each under {pct(SHOW_MIN, 0)}% to be named.</p>}
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
