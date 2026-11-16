"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo } from "react";
import { useJson } from "@/lib/fetch";
import { kickoffLocal, tierLabel } from "@/lib/format";
import type { FixtureList, PlayedList } from "@/lib/types";

interface Item {
  id: number; kickoff: string; competitionId: number; label: string;
}
interface Group { key: string; label: string; items: Item[] }

const byKickoff = (a: Item, b: Item) => a.kickoff.localeCompare(b.kickoff) || a.id - b.id;

const WINDOW_DAYS = 7;

/** Jump between matches from a match page: a dropdown of the fixtures of the next 7 days (by
 * competition) and the matches played in the last 7, and previous / next within the same
 * competition (played and upcoming together, by kickoff). Keyboard: Alt + ← / →. */
export function MatchNav({ id, competitionId, kickoff, title }:
  { id: number; competitionId: number; kickoff: string; title: string }) {
  const router = useRouter();
  // fixed once per page, so the request URLs stay stable between renders
  const [fxUrl, plUrl] = useMemo(() => {
    const now = Date.now(), span = WINDOW_DAYS * 86400e3;
    const iso = (t: number) => new Date(t).toISOString();
    return [`/api/fixtures?start=${iso(now)}&end=${iso(now + span)}`, `/api/played?start=${iso(now - span)}`];
  }, []);
  const fx = useJson<FixtureList>(fxUrl);
  const pl = useJson<PlayedList>(plUrl);

  const groups = useMemo<Group[]>(() => {
    const out: Group[] = (fx.data?.competitions ?? []).map((c) => ({
      key: `c${c.competition_id}`,
      label: `${c.country} · ${c.name} (${tierLabel(c.tier)})`,
      items: c.fixtures.map((f) => ({ id: f.match_id, kickoff: f.kickoff, competitionId: c.competition_id,
                                     label: `${kickoffLocal(f.kickoff)} · ${f.home} v ${f.away}` })),
    }));
    const played = (pl.data?.matches ?? []).map((m) => ({
      id: m.match_id, kickoff: m.kickoff, competitionId: m.competition_id,
      label: `${kickoffLocal(m.kickoff)} · ${m.home} ${m.home_goals}–${m.away_goals} ${m.away} · ${m.competition}`,
    }));
    if (played.length) out.push({ key: "played", label: "Played", items: played.sort((a, b) => byKickoff(b, a)) });
    return out;
  }, [fx.data, pl.data]);

  // previous / next in the same competition, from this match's kickoff (it may be in neither list: just kicked off)
  const [prev, next] = useMemo(() => {
    const seen = new Set<number>();
    const same = groups.flatMap((g) => g.items)
      .filter((i) => i.competitionId === competitionId && i.id !== id && !seen.has(i.id) && seen.add(i.id))
      .sort(byKickoff);
    const me: Item = { id, kickoff, competitionId, label: "" };
    return [same.filter((i) => byKickoff(i, me) < 0).at(-1) ?? null, same.find((i) => byKickoff(i, me) > 0) ?? null];
  }, [groups, id, competitionId, kickoff]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      const to = e.key === "ArrowLeft" ? prev : e.key === "ArrowRight" ? next : null;
      if (to) { e.preventDefault(); router.push(`/match/${to.id}`); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [prev, next, router]);

  const listed = groups.some((g) => g.items.some((i) => i.id === id));
  const waiting = !fx.data || !pl.data ? "loading matches…" : null;
  return (
    <nav className="match-nav" aria-label="Match navigation">
      <Link href="/" className="back">← Fixtures</Link>
      <Link href="/played" className="back">Played</Link>
      <button type="button" className="nav-step" disabled={!prev} title={prev ? `${prev.label} (Alt + ←)` : waiting ?? "no earlier match in this competition"}
              onClick={() => prev && router.push(`/match/${prev.id}`)}>‹ Prev</button>
      <select className="match-jump" value={String(id)} aria-label="Go to match"
              onChange={(e) => router.push(`/match/${e.target.value}`)}>
        {!listed && <option value={String(id)}>{title}</option>}
        {groups.map((g) => (
          <optgroup key={g.key} label={g.label}>
            {g.items.map((i) => <option key={`${g.key}-${i.id}`} value={String(i.id)}>{i.label}</option>)}
          </optgroup>
        ))}
      </select>
      <button type="button" className="nav-step" disabled={!next} title={next ? `${next.label} (Alt + →)` : waiting ?? "no later match in this competition"}
              onClick={() => next && router.push(`/match/${next.id}`)}>Next ›</button>
    </nav>
  );
}
