"""Stage D v2 serving, check (a): the stored two-legged national ties scored before they are
served (docs/BEYOND_LEAGUES_PLAN.md, "D v2 serving acceptance"; owner: "Approve with (a)").

Every played tie of a round with a written rule (`config/tournament_formats.yml`, `ties`):
- **Replay:** the stored path fits the rule (extra time only when level, a shoot-out only
  after it) and names the side that went through; no irregular tie.
- **Before leg 1** (the served case), as of the Monday of leg 1, from that Monday's cached
  refit; **between legs** (Stage B's case), as of leg 2's kickoff - 3 h with leg 1 fixed,
  from the latest cached refit at or before then.
- **Gate:** at each point, P(advance) log loss no worse than "level = coin flip" (point
  estimate <= 0, the paired 95% interval reported); and the replay exact. ECE is reported.

No fits and no API requests. Metrics only in the output; rows go to the store.

    uv run python -m adatta.eval.ties run [--upload RUN]
"""

from __future__ import annotations

import argparse
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np
import polars as pl

from adatta.adapters.storage import RawStore
from adatta.eval import tournament as ev
from adatta.sim import ties
from adatta.sim import tournament as tm

PREFIX = "tournament/v2/ties"
LO = datetime(2019, 1, 1, tzinfo=UTC)
OPENED = datetime(2024, 1, 1, tzinfo=UTC)
POINTS = ("before_leg1", "between_legs")


def point(store: RawStore, t: ties.Tie, confs: dict[int, str], as_of: datetime
          ) -> tuple[ties.TieProb, dict[str, Any]]:  # fmt: skip
    """The tie's P(advance) as of `as_of` from the latest cached refit at or before it."""
    from adatta.eval import knockout as kb
    from adatta.model import national as nat

    post, diag = ev.refit(store, as_of)
    refit_at = datetime.fromtimestamp(float(post["_as_of"]), UTC)
    if refit_at > as_of:
        raise ValueError(f"refit {refit_at} after {as_of}")  # never leak
    m0 = nat.month_index(refit_at)
    months = [max(nat.month_index(x.kickoff), m0) for x in t.legs]
    rt = ties.tie_rates(post, t, confs, m0, int(diag["n_months"]), months,
                        nat.comp_type(t.league_id))  # fmt: skip
    p = ties.tie_prob(rt, t, months, as_of)
    if p is None:
        raise ValueError(f"tie {t.legs[0].match_id} decided before {as_of}")
    if p.stage == "between_legs":  # identity with Stage B's closed form, every time
        l1, l2 = t.legs
        assert l1.nt is not None
        m2, lam2, mu2 = ties.leg_matrix(rt, l2, months[1])
        sb, sb_base = kb.two_legs_m(m2, l1.nt[0], l1.nt[1], lam2, mu2, t.rule, tm.KAPPA_ET,
                                    tm.P_SHOOTOUT)  # fmt: skip
        gap = max(float(np.abs(sb - p.a).max()), float(np.abs(sb_base - p.base).max()))
        if gap > 1e-9:
            raise ValueError(f"tie {l1.match_id}: {gap:.2e} from Stage B's two_legs")
    return p, {"refit": refit_at.isoformat()}


def run(session: Any, store: RawStore) -> tuple[pl.DataFrame, dict[str, Any]]:
    rules = ties.load_rules()
    found, irregular, confs = ties.stored(session, store, rules, LO, datetime.now(UTC))
    played = [t for t in found if all(x.status in ties.FINAL for x in t.legs)]
    rows, problems = [], []
    for t in played:
        w, why = ties.winner(t)
        if why or w is None:
            problems.append(f"tie {t.legs[0].match_id}: {why or 'no winner'}")
            continue
        if t.rule.legs != 2:
            continue
        l1, l2 = t.legs
        for name, as_of in (("before_leg1", ev.monday(l1.kickoff)),
                            ("between_legs", l2.kickoff - ties.LAG)):  # fmt: skip
            p, meta = point(store, t, confs, as_of)
            assert p.stage == name, (p.stage, name)
            s = ties.summary(p)
            rows.append({"match_id": l1.match_id, "league_id": t.league_id, "season": t.season,
                         "round": t.round, "point": name, "as_of": as_of, "refit": meta["refit"],
                         "window": "dev" if l1.kickoff < OPENED else "opened",
                         "p": float(p.a.mean()), "base": float(p.base.mean()),
                         "y": float(w == t.a), "extra_time": float(l2.status in ("AET", "PEN")),
                         "path_sum": float(sum(s["path"].values()))})  # fmt: skip
    df = pl.DataFrame(rows)
    return df, gate(df, problems, irregular, len(played), len(found))


def gate(df: pl.DataFrame, problems: list[str], irregular: list[int], played: int,
         found: int) -> dict[str, Any]:  # fmt: skip
    out: dict[str, Any] = {"ties_found": found, "ties_played": played, "replay_problems": problems,
                           "irregular": len(irregular), "points": {}}  # fmt: skip
    ok = not problems and not irregular
    for name in POINTS:
        d = df.filter(pl.col("point") == name)
        p, b, y = d["p"].to_numpy(), d["base"].to_numpy(), d["y"].to_numpy()
        diff = ev._ll(p, y) - ev._ll(b, y)
        se = diff.std(ddof=1) / np.sqrt(len(diff)) if len(diff) > 1 else float("nan")
        ece, null = ev._ece(p, y)
        passes = len(diff) > 0 and float(diff.mean()) <= 0
        ok = ok and passes
        out["points"][name] = {
            "n": len(diff), "by_window": dict(d.group_by("window").len().iter_rows()),
            "went_to_extra_time": int(d["extra_time"].sum()),
            "logloss": float(ev._ll(p, y).mean()), "logloss_base": float(ev._ll(b, y).mean()),
            "diff": [float(diff.mean()), float(diff.mean() - 1.96 * se),
                     float(diff.mean() + 1.96 * se)],
            "ece": [ece, null], "passes": passes,
            "path_sum_max_gap": float(np.abs(d["path_sum"].to_numpy() - 1).max()),
        }  # fmt: skip
    out["passes"] = ok
    return out


def markdown(v: dict[str, Any]) -> str:
    lines = [f"## D v2 serving, check (a): {'PASSES' if v['passes'] else 'FAILS'}", "",
             f"- {v['ties_played']} played two-legged ties of {v['ties_found']} stored with a "
             f"written rule; replay problems {len(v['replay_problems'])}, irregular "
             f"{v['irregular']}"]  # fmt: skip
    for name, x in v["points"].items():
        lines.append(
            f"- {name}: n {x['n']} {x['by_window']}, {x['went_to_extra_time']} to extra time; "
            f"log loss {x['logloss']:.4f} against {x['logloss_base']:.4f}: {x['diff'][0]:+.4f} "
            f"[{x['diff'][1]:+.4f}, {x['diff'][2]:+.4f}]; ECE {x['ece'][0]:.4f} (noise floor "
            f"{x['ece'][1]:.4f}); {'pass' if x['passes'] else 'FAIL'}"
        )
    for pr in v["replay_problems"]:
        lines.append(f"- replay: {pr}")
    return "\n".join(lines)


def shadow(session: Any, store: RawStore, run: str) -> dict[str, Any]:
    """Serving acceptance test 5: the March 2025 UEFA ties as of the Monday before leg 1, run
    through the serving path (`ties.project`) from the cached refits, against check (a)'s
    stored rows of run `run`; metrics only."""
    import time

    from adatta.model import national as nat

    rows = pl.read_parquet(io.BytesIO(store.get(f"{PREFIX}/{run}/rows.parquet")))
    want = {r["match_id"]: r["p"] for r in rows.filter(pl.col("point") == "before_leg1")
            .iter_rows(named=True)}  # fmt: skip
    lo, hi = datetime(2025, 3, 1, tzinfo=UTC), datetime(2025, 4, 1, tzinfo=UTC)
    found, _, confs = ties.stored(session, store, ties.load_rules(), lo, hi)
    gaps, secs = [], []
    for t in found:
        l1 = t.legs[0]
        as_of = ev.monday(l1.kickoff)
        post, diag = ev.refit(store, as_of)
        refit_at = datetime.fromtimestamp(float(post["_as_of"]), UTC)
        t0 = time.perf_counter()
        sel = ties.project(post, t, l1.match_id, confs, refit_at, int(diag["n_months"]), as_of)
        secs.append(time.perf_counter() - t0)
        gaps.append(abs(sel[(ties.QUALIFY, None, "home")][0] - want[l1.match_id]))
        assert nat.month_index(refit_at) <= nat.month_index(as_of)
    return {
        "ties": len(found),
        "max_gap": max(gaps),
        "passes": max(gaps) <= 1e-9,
        "seconds_per_tie_max": max(secs),
        "seconds_total": sum(secs),
    }


def main(argv: Sequence[str] | None = None) -> None:
    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session

    ap = argparse.ArgumentParser(prog="python -m adatta.eval.ties")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("run")
    s1.add_argument("--upload", default="")
    s2 = sub.add_parser("shadow")
    s2.add_argument("--run", required=True)
    args = ap.parse_args(argv)
    store = make_store(get_settings())
    if args.cmd == "shadow":
        print(json.dumps(shadow(get_session(), store, args.run)))
        return
    df, v = run(get_session(), store)
    if args.upload:
        buf = io.BytesIO()
        df.write_parquet(buf)
        store.put(f"{PREFIX}/{args.upload}/rows.parquet", buf.getvalue())
        store.put(f"{PREFIX}/{args.upload}/verdict.json", json.dumps(v, default=str).encode())
    print(markdown(v))


if __name__ == "__main__":
    main()
