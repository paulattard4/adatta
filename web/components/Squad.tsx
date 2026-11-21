import { dateLocal, kickoffLocal, pct } from "@/lib/format";
import type { Absence, FormationView, Lineup, LineupScore, PitchPlayer, ProjectedPlayer, Projection, Squad, SquadPlayer, TeamSquad } from "@/lib/types";

const LINE_LABEL: Record<string, string> = { G: "GK", D: "D", M: "M", F: "F" };

const SECTION_LABEL: Record<ProjectedPlayer["section"], string> = {
  xi: "Projected XI", bench: "Projected bench", out: "Not in the matchday squad", squad: "Squad, not modelled",
};

function Tag({ p }: { p: ProjectedPlayer }) {
  const st = p.status ?? p.status_as_of;
  if (!st) return null;
  // listed out only after the projection's as-of time: the next run takes them out of the XI and bench
  const late = p.status === "out" && p.status_as_of !== "out" && (p.section === "xi" || p.section === "bench");
  const title = late ? "listed by the injury feed after this projection's as-of time; the next daily run moves them out"
    : p.status ? "listed by the injury feed; not used by the model" : "listed by the injury feed at the projection time";
  return <span className={`absence-tag ${st}`} title={title}>{late ? "out since" : st}</span>;
}

function Projected({ t, pr, announced }: { t: TeamSquad; pr: Projection; announced: boolean }) {
  const order: ProjectedPlayer["section"][] = ["xi", "bench", "out", "squad"];
  const limit = t.bench_limit !== null ? <> (league limit <span className="num">{t.bench_limit}</span>)</> : null;
  return (
    <>
      <h4>{announced ? "Projected before the announcement" : "Projected lineup"} <span className="dim">· P3-b</span></h4>
      <p className="squad-meta dim">
        As of <span className="num">{kickoffLocal(pr.as_of)}</span> · refit <span className="num">{dateLocal(pr.refit_as_of)}</span>
        {" "}· the model does not use the injury feed; players it listed as out at that time are kept out of the XI and bench
        {t.projected.some((p) => p.status === "out" && p.status_as_of !== "out" && (p.section === "xi" || p.section === "bench")) &&
          <> (&ldquo;out since&rdquo;: listed after it)</>}
      </p>
      <table className="proj">
        <colgroup><col className="c-pos" /><col /><col className="c-num" /><col className="c-num" /><col className="c-min" /></colgroup>
        <thead><tr><th>Pos</th><th>Player</th><th className="r">Start</th><th className="r">Bench</th><th className="r">Min</th></tr></thead>
        {order.map((sec) => {
          const xs = t.projected.filter((p) => p.section === sec);
          if (xs.length === 0) return null;
          return (
            <tbody key={sec}>
              <tr className="proj-sec"><td colSpan={5}>
                {SECTION_LABEL[sec]}{sec === "xi" && t.formation && <> · <span className="num">{t.formation.pitch_formation}</span>, as on the pitch</>}
                {sec === "bench" && <> · <span className="num">{xs.length}</span>{limit}</>}
                {sec === "squad" && <span className="dim"> · registered, no appearance in the last year</span>}
              </td></tr>
              {xs.map((p, i) => (
                <tr key={`${p.name}-${i}`}>
                  <td className="num dim">{p.slot ?? (LINE_LABEL[p.pos] ?? p.pos)}</td>
                  <td><span className="proj-name"><span className="proj-name-text" title={p.name}>{p.moved && <span className="pitch-moved" aria-label="moved in">◆</span>}{p.name}</span><Tag p={p} /></span></td>
                  <td className="num r">{p.p_start !== null ? `${pct(p.p_start, 0)}%` : "–"}</td>
                  <td className="num r dim">{p.p_bench !== null ? `${pct(p.p_bench, 0)}%` : "–"}</td>
                  <td className="num r dim">{p.emin !== null ? p.emin.toFixed(0) : "–"}</td>
                </tr>
              ))}
            </tbody>
          );
        })}
      </table>
    </>
  );
}

function Outcome({ o }: { o: LineupScore }) {
  return (
    <>
      <h4>How the projection did <span className="dim">· as of kickoff − 24 h</span></h4>
      <p className="squad-meta">
        XI <span className="num">{o.xi_hits}/11</span> started
        {o.xi_hits_raw !== o.xi_hits && <span className="dim"> (P3-b&apos;s own top XI: <span className="num">{o.xi_hits_raw}/11</span>)</span>}
        {" "}· bench <span className="num">{o.bench_hits}/{o.bench_n}</span> on the bench, <span className="num">{o.bench_in_squad}/{o.bench_n}</span> named
        {o.formation && <> · played <span className="num">{o.formation}</span>
          {o.formation_p !== null ? <>, P3-c gave it <span className="num">{pct(o.formation_p, 0)}%</span>{o.formation_top ? " (top pick)" : ""}</> : <span className="dim"> (no formation projection)</span>}
        </>}
      </p>
    </>
  );
}

/** Grid rows, forwards on top; within a row, column 1 (the team's left) first. */
export function pitchRows(xs: PitchPlayer[]): PitchPlayer[][] {
  const rows = new Map<number, PitchPlayer[]>();
  for (const p of xs) rows.set(p.row, [...(rows.get(p.row) ?? []), p]);
  return [...rows.keys()].sort((a, b) => b - a).map((r) => rows.get(r)!.sort((a, b) => a.col - b.col));
}

function Pitch({ xs, positional, label }: { xs: PitchPlayer[]; positional: boolean; label: string }) {
  return (
    <div className="pitch" aria-label={label}>
      {pitchRows(xs).map((row) => (
        <div className="pitch-row" key={row[0].row}>
          {row.map((p) => (
            <div className="pitch-player" key={`${p.row}-${p.col}`}
              title={p.usual ? `${p.name}: usually ${positional ? p.usual : (LINE_LABEL[p.usual] ?? p.usual)}` : p.name}>
              <span className="pitch-code num">{positional ? p.code : (LINE_LABEL[p.code] ?? p.code)}</span>
              <span className="pitch-name">{p.moved && <span className="pitch-moved" aria-label="moved in">◆</span>}{p.name}</span>
              <span className="pitch-foot">
                <span className="num dim">{p.p_start !== null ? `${pct(p.p_start, 0)}%` : "–"}</span>
                {p.status && <span className={`absence-tag ${p.status}`}>{p.status}</span>}
              </span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function FormationBlock({ f }: { f: FormationView }) {
  const label = (x: string) => (x === "other" ? "other shape" : x);
  return (
    <>
      <h4>Projected formation <span className="dim">· P3-c</span></h4>
      <div className="form-opts">
        {f.options.map((o, i) => (
          <span key={o.formation} className={`form-opt${i === 0 ? " top" : ""}${o.formation === f.announced ? " played" : ""}`}>
            <span className="num">{label(o.formation)}</span><span className="num">{pct(o.p, 0)}%</span>
          </span>
        ))}
        {f.announced && !f.options.some((o) => o.formation === f.announced) && (
          <span className="form-opt played" title="announced; not among P3-c's three most likely">
            <span className="num">{f.announced}</span>
            <span className="num">{f.announced_p !== null ? `${pct(f.announced_p, 0)}%` : "–"}</span>
          </span>
        )}
      </div>
      {f.announced && <p className="squad-meta dim">Announced <span className="num">{f.announced}</span> (marked): P3-c gave it {f.announced_p !== null ? <span className="num">{pct(f.announced_p, 0)}%</span> : "no probability"}{f.announced_p !== null && !f.options.some((o) => o.formation === f.announced) && f.options.some((o) => o.formation === "other") ? " as part of other shape" : ""}.</p>}
      <p className="squad-meta dim">
        As of <span className="num">{kickoffLocal(f.as_of)}</span> · refit <span className="num">{dateLocal(f.refit_as_of)}</span>
        {" "}· the model does not use opponent strength
      </p>
      {f.pitch.length > 0 ? (
        <>
          <div className="squad-sub dim">
            Projected XI in <span className="num">{f.pitch_formation}</span>
            {f.last_kickoff
              ? <> · placed in the last lineup in it (<span className="num">{dateLocal(f.last_kickoff)}</span>)</>
              : <> · no earlier lineup in it: placed by usual position</>}
            {!f.positional && " · by line only"}
          </div>
          <Pitch xs={f.pitch} positional={f.positional} label={`projected XI in ${f.pitch_formation}`} />
          <p className="squad-meta dim">
            {f.moved > 0 && <><span className="pitch-moved">◆</span> replaces a player from that lineup and takes a vacated position (<span className="num">{f.moved}</span>). </>}
            Percentages are P3-b&apos;s chance to start; hover a player for his usual position when it differs.
          </p>
        </>
      ) : <p className="squad-meta dim">No pitch for <span className="num">{f.pitch_formation}</span>.</p>}
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
        {(lu.pitch_formation ?? lu.formation) && <> · <span className="num">{lu.pitch_formation ?? lu.formation}</span></>}
        {lu.coach && <> · {lu.coach}</>}
      </p>
      {lu.pitch.length > 0 && <>
        <Pitch xs={lu.pitch} positional={lu.positional} label={`announced lineup in ${lu.pitch_formation}`} />
        {!lu.positional && <p className="squad-meta dim">Positions by line only: this league-season&apos;s grid doesn&apos;t pass the positional test.</p>}
      </>}
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
  // six blocks in a fixed order; the two sides share grid rows so each block starts level
  return (
    <div className="squad-side">
      <div className="squad-block squad-team">{t.team}</div>
      <div className="squad-block">
        <h4>Availability</h4>
        {!checked ? <p className="dim squad-meta">Not captured for this competition.</p>
          : t.absences.length === 0 ? <p className="dim squad-meta">No absences listed by the feed.</p>
          : <Absences xs={t.absences} />}
      </div>
      <div className="squad-block">
        {t.lineup ? <LineupBlock lu={t.lineup} /> : <><h4>Lineup</h4><p className="dim squad-meta">{note ?? "No lineup captured."}</p></>}
      </div>
      <div className="squad-block">{t.outcome && <Outcome o={t.outcome} />}</div>
      <div className="squad-block">{t.formation && <FormationBlock f={t.formation} />}</div>
      <div className="squad-block">{pr && t.projected.length > 0 && <Projected t={t} pr={pr} announced={t.lineup !== null} />}</div>
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
