"""P3-h2 verdict (docs/PHASE3_TASKS.md P3-h2 (c)-(d), D64): the projected pitch with
unavailable players replaced (`slots_replaced`) against the last lineup unchanged
(`slots`), both from the same stored formation row, scored against the announced lineup.

Window (fixed 2026-11-27, before any score): team-matches of the five bundles' league
matches with kickoff from WINDOW_FROM to before WINDOW_TO, each with a stored projection
as of kickoff - 24 h carrying both pitches and an announced XI. Judged once, with at least
MIN_N team-matches; with fewer, the window runs on a week at a time (counted, not scored).

CLI:
  count  [--until YYYY-MM-DD]   eligible team-matches only, no score
  judge  [--until YYYY-MM-DD]   the verdict, once (a marker in the store refuses a second)
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

WINDOW_FROM = datetime(2026, 11, 27, tzinfo=UTC)  # approval and the first shadow rows
WINDOW_TO = datetime(2026, 12, 24, tzinfo=UTC)  # the winter break (kickoff before)
MIN_N = 1000
SLOT_MARGIN = 0.01  # slot accuracy may be worse by at most this (point estimate)
MARKER = "formation/verdicts/p3h2.json"
Z95 = 1.959963984540054


@dataclass(frozen=True)
class Pair:
    """One team-match: both pitches against the announced XI."""

    match_id: int
    team_id: int
    bundle: str
    overlap_last: int  # pitch players who started, last lineup unchanged
    overlap_replaced: int  # the same, with replacements
    replacements: int
    replaced_hits: int  # replacements who started
    acc_last: float | None  # slot accuracy, positional and same formation only
    acc_replaced: float | None


def pair(match_id: int, team_id: int, bundle: str, slots: list[dict[str, Any]],
         replaced: list[dict[str, Any]], started: set[int],
         announced_codes: dict[int, str] | None) -> Pair:  # fmt: skip
    """Score one team-match. `announced_codes` (player -> slot code of the announced
    grid) is given only where the league-season is positional and the announced formation
    is the pitch's."""
    from adatta.api.lineups import slot_accuracy

    last = [int(s["player_id"]) for s in slots]
    rep = [int(s["player_id"]) for s in replaced]
    swaps = [s for s in replaced if s.get("replaced") is not None]
    acc_l = acc_r = None
    if announced_codes is not None:
        acc_l = slot_accuracy([(str(s["code"]), int(s["player_id"])) for s in slots],
                              announced_codes)  # fmt: skip
        acc_r = slot_accuracy([(str(s["code"]), int(s["player_id"])) for s in replaced],
                              announced_codes)  # fmt: skip
    return Pair(
        match_id,
        team_id,
        bundle,
        len(set(last) & started),
        len(set(rep) & started),
        len(swaps),
        sum(int(s["player_id"]) in started for s in swaps),
        acc_l,
        acc_r,
    )


def _mean_ci(d: np.ndarray) -> tuple[float, float, float]:
    m = float(d.mean()) if len(d) else float("nan")
    se = float(d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 1 else float("nan")
    return m, m - Z95 * se, m + Z95 * se


def verdict(pairs: list[Pair]) -> dict[str, Any]:
    """(d) 1: XI overlap better, paired 95% interval above 0; (d) 2: slot accuracy not
    worse by more than SLOT_MARGIN in positional pairs (point estimate). Needs MIN_N."""
    d = np.array([p.overlap_replaced - p.overlap_last for p in pairs], dtype=np.float64)
    m, lo, hi = _mean_ci(d)
    acc = [(p.acc_replaced, p.acc_last) for p in pairs
           if p.acc_replaced is not None and p.acc_last is not None]  # fmt: skip
    da = np.array([a - b for a, b in acc], dtype=np.float64)
    am, alo, ahi = _mean_ci(da)
    reps = sum(p.replacements for p in pairs)
    c1 = bool(len(d) >= MIN_N and lo > 0)
    c2 = bool(len(da) == 0 or am >= -SLOT_MARGIN)
    by: dict[str, dict[str, float]] = {}
    for b in sorted({p.bundle for p in pairs}):
        g = np.array([p.overlap_replaced - p.overlap_last for p in pairs if p.bundle == b])
        by[b] = {"n": float(len(g)), "mean": float(g.mean())}
    return {
        "n": len(pairs), "enough": len(pairs) >= MIN_N,
        "overlap": {"last": float(np.mean([p.overlap_last for p in pairs])) if pairs else None,
                    "replaced": float(np.mean([p.overlap_replaced for p in pairs]))
                    if pairs else None, "diff": m, "lo": lo, "hi": hi},
        "slot_accuracy": {"n": len(da), "diff": am, "lo": alo, "hi": ahi},
        "replacements": reps,
        "with_replacement": sum(p.replacements > 0 for p in pairs),
        "replacement_hit_rate": (sum(p.replaced_hits for p in pairs) / reps) if reps else None,
        "by_bundle": by,
        "checks": {"1 XI overlap better (interval above 0)": c1,
                   f"2 slot accuracy not worse by > {SLOT_MARGIN}": c2},
        "passed": c1 and c2,
    }  # fmt: skip


def report(v: dict[str, Any], until: datetime) -> str:
    o, a = v["overlap"], v["slot_accuracy"]
    hit = v["replacement_hit_rate"]
    lines = [f"# P3-h2 verdict: kickoff {WINDOW_FROM:%Y-%m-%d} to before {until:%Y-%m-%d}", "",
             f"{v['n']} team-matches; {v['with_replacement']} with a replacement "
             f"({v['replacements']} replacements, {hit:.1%} of them started)."
             if hit is not None else f"{v['n']} team-matches; no replacements.",
             f"XI overlap: last lineup {o['last']:.3f}, replaced {o['replaced']:.3f}, difference "
             f"{o['diff']:+.4f} [{o['lo']:+.4f}, {o['hi']:+.4f}].",
             f"Slot accuracy (positional, same formation, n {a['n']}): difference "
             f"{a['diff']:+.4f} [{a['lo']:+.4f}, {a['hi']:+.4f}].",
             "By bundle: " + "; ".join(f"{b} n {int(x['n'])} {x['mean']:+.4f}"
                                       for b, x in v["by_bundle"].items()), "",
             "Checks: " + "; ".join(f"{k}: {'pass' if ok else 'FAIL'}"
                                    for k, ok in v["checks"].items()),
             f"**P3-h2 {'PASSES' if v['passed'] else 'FAILS'}.**"]  # fmt: skip
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- loading


def load_pairs(session: Any, until: datetime) -> list[Pair]:
    """Every eligible team-match of the window, scored."""
    from sqlalchemy import select

    from adatta.adapters import normalize
    from adatta.api import lineups as al
    from adatta.db import models
    from adatta.eval import harness

    cfg = harness.load_config()
    bundle_of = {int(x): b for b, ids in cfg.bundles.items() for x in ids}
    M, S, C = models.Match, models.Season, models.Competition
    kick: dict[int, datetime] = {}
    league: dict[int, int] = {}
    for mid, ko, ref in session.execute(
        select(M.match_id, M.kickoff_time, C.source_ref)
        .join(S, S.season_id == M.season_id).join(C, C.competition_id == S.competition_id)
        .where(C.source_ref.in_([str(x) for x in bundle_of]), M.kickoff_time >= WINDOW_FROM,
               M.kickoff_time < until, M.status.in_(sorted(normalize.FINISHED)))
    ):  # fmt: skip
        kick[mid] = ko if ko.tzinfo else ko.replace(tzinfo=UTC)
        league[mid] = int(ref)
    _, form = al.scoring_runs(session, kick)
    snaps = al.announced(session, list(kick))
    refs = {str(x["player_ref"]) for s in snaps.values() for x in s.starters
            if x.get("player_ref") is not None}  # fmt: skip
    ids = al.player_ids(session, refs)
    out = []
    for (mid, tid), f in sorted(form.items()):
        snap = snaps.get((mid, tid))
        if snap is None or not f.slots or f.slots_replaced is None:
            continue
        started = {ids[str(x["player_ref"])] for x in snap.starters
                   if str(x.get("player_ref")) in ids}  # fmt: skip
        if len(started) != 11:
            continue
        codes = None
        if f.positional:
            got = al.announced_pitch(snap.starters, positional=True)
            if got is not None and got[0] == f.pitch_formation:
                codes = {ids[str(x["player_ref"])]: c for x, c, _, _ in got[1]
                         if str(x.get("player_ref")) in ids}  # fmt: skip
        out.append(pair(mid, tid, bundle_of[league[mid]], list(f.slots),
                        list(f.slots_replaced), started, codes))  # fmt: skip
    return out


def main(argv: Sequence[str] | None = None) -> None:
    from adatta.adapters.storage import make_store, try_get
    from adatta.config import get_settings
    from adatta.db.session import get_session

    p = argparse.ArgumentParser(prog="python -m adatta.eval.pitch_replaced")
    p.add_argument("cmd", choices=("count", "judge"))
    p.add_argument("--until", default=None, help="kickoff before (default WINDOW_TO, or a "
                   "later Monday when the window ran on)")  # fmt: skip
    args = p.parse_args(argv)
    until = (datetime.fromisoformat(args.until).replace(tzinfo=UTC) if args.until
             else WINDOW_TO)  # fmt: skip
    if until < WINDOW_TO or (until - WINDOW_TO) % timedelta(days=7):
        raise SystemExit("--until is WINDOW_TO or a whole number of weeks after it")
    with get_session() as session:
        pairs = load_pairs(session, until)
    if args.cmd == "count":
        print(f"{len(pairs)} eligible team-matches (need {MIN_N}); no score computed.")
        return
    store = make_store(get_settings())
    if try_get(store, MARKER) is not None:
        raise SystemExit(f"P3-h2 was judged already ({MARKER}); it is read once")
    if len(pairs) < MIN_N:
        raise SystemExit(f"{len(pairs)} team-matches, fewer than {MIN_N}: run the window on")
    v = verdict(pairs)
    rec = {"until": until.isoformat(), "judged_at": datetime.now(UTC).isoformat(), **v}
    store.put(MARKER, json.dumps(rec, default=str).encode())
    print(report(v, until))


if __name__ == "__main__":
    main()
