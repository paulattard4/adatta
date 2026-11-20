import { kickoffLocal } from "@/lib/format";
import type { Absence, Lineup, Squad, SquadPlayer, TeamSquad } from "@/lib/types";

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

function Side({ t, checked, note }: { t: TeamSquad; checked: boolean; note: string | null }) {
  return (
    <div className="squad-side">
      <div className="squad-team">{t.team}</div>
      <h4>Availability</h4>
      {!checked ? <p className="dim squad-meta">Not captured for this competition.</p>
        : t.absences.length === 0 ? <p className="dim squad-meta">No absences listed by the feed.</p>
        : <Absences xs={t.absences} />}
      {t.lineup ? <LineupBlock lu={t.lineup} /> : <><h4>Lineup</h4><p className="dim squad-meta">{note ?? "No lineup captured."}</p></>}
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
        <Side t={s.home} checked={checked} note={s.lineup_note} />
        <Side t={s.away} checked={checked} note={s.lineup_note} />
      </div>
      <p className="squad-foot dim">
        As of <span className="num">{kickoffLocal(s.as_of)}</span>
        {checked && <> · injury feed last checked <span className="num">{kickoffLocal(s.status_checked_at!)}</span></>}
        {" "}· provider lists, not model output; doubtful players may still play.
      </p>
    </>
  );
}
