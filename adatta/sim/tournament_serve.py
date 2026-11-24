"""Stage D v2 serving: tournament forecasts (docs/BEYOND_LEAGUES_PLAN.md, "D v2 serving
acceptance"). Numpy from the stored draws of the latest passing live national refit, as D v1
serving; no NUTS, no odds.

An edition is served when it is in `config/tournament_formats.yml` with window `live`, its
rules validate, the replay of its played results is clean, every team is among the servable
sides, and the serving refit is current for its next match. Each forecast is a JSON under
`serving/tournament/<edition>/` and a `projection_run` row that points to it (component
`tournament`). A new one is written when the refit or the set of final results changes.

    python -m adatta.sim.tournament_serve shadow --edition euro2024

`shadow` writes a past edition's forecast as of its opening Monday, from the cached refit, to
`serving/tournament/shadow/<edition>.json` (the dashboard's test page; no DB writes).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.eval import tournament as ev
from adatta.sim import tournament as tm

PREFIX = "serving/tournament"
COMPONENT = "tournament"
LABEL = ("Team-only (Stage D v2): the tournament simulator on the results-based national "
         "model; extra time at κ = 1 and a coin-flip shoot-out (Stage B fallbacks)")  # fmt: skip
RECENT_RUNS = 500  # projection runs searched for an edition's last forecast


def shadow_key(key: str) -> str:
    return f"{PREFIX}/shadow/{key}.json"


def payload(d: ev.EdData, res: tm.Result, as_of: datetime, refit_as_of: datetime,
            model_run_id: int | None, shadow: bool, finals: int) -> dict[str, Any]:  # fmt: skip
    """Per team: P(each group position), P(advance), P(reach each round) and P(win), with the
    Monte Carlo SE of each (sqrt(p(1 - p) / n))."""
    n = res.n_sims
    se_max = 0.0

    def p(x: float) -> list[float]:
        nonlocal se_max
        se = float(np.sqrt(x * (1 - x) / n))
        se_max = max(se_max, se)
        return [float(x), se]

    groups = []
    for letter, teams in d.groups.items():
        rows: list[dict[str, Any]] = []
        for t in teams:
            i = res.teams.index(t)
            rows.append({
                "team_id": t, "name": d.names.get(t, str(t)),
                "position": [p(x) for x in res.position[i, : len(teams)]],
                "advance": p(res.advance[i]),
                "reach": {r.name: p(res.reach[r.name][i]) for r in d.ed.rounds},
                "win": p(res.win[i]),
            })  # fmt: skip
        rows.sort(key=lambda r: -r["advance"][0])
        groups.append({"group": letter, "teams": rows})
    return {
        "edition": d.ed.key, "name": d.ed.name, "as_of": as_of.isoformat(),
        "refit_as_of": refit_as_of.isoformat(), "model_run_id": model_run_id, "shadow": shadow,
        "label": LABEL, "n_sims": n, "mc_se_max": se_max, "finals_fixed": finals,
        "rounds": [r.name for r in d.ed.rounds], "groups": groups,
        "random_groups": res.random_groups, "random_thirds": res.random_thirds,
    }  # fmt: skip


def _finals(d: ev.EdData, t: datetime) -> int:
    return sum(p.final and p.kickoff + ev.LAG <= t for p in d.group_matches + d.ko)


def live_editions() -> list[tm.Edition]:
    return [e for e in tm.load_formats().values() if e.window == "live"]


def last_forecast(session: Session, key: str) -> models.ProjectionRun | None:
    PR = models.ProjectionRun
    for pr in session.scalars(select(PR).order_by(PR.projection_run_id.desc()).limit(RECENT_RUNS)):
        cv = (pr.component_versions or {}).get(COMPONENT)
        if cv and cv.get("edition") == key:
            return pr
    return None


def run_tournaments(session: Session, store: RawStore, manifest: dict[str, Any],
                    now: datetime | None = None) -> tuple[dict[str, int], list[str]]:  # fmt: skip
    """Write a forecast for each servable live edition whose refit or final results changed
    since its last one. Returns (counts, problems: withheld editions and why)."""
    from adatta.model import national as nat
    from adatta.model import national_live as nl
    from adatta.sim import national_serve as ns

    now = now or datetime.now(UTC)
    counts = {"written": 0, "unchanged": 0, "withheld": 0}
    problems: list[str] = []
    for ed in live_editions():
        try:
            d = ev.load(session, store, ed)
        except ValueError as exc:  # a team outside the servable sides, or unmatched
            counts["withheld"] += 1
            problems.append(f"tournament {ed.key}: withheld: {exc}")
            continue
        upcoming = [p.kickoff for p in d.group_matches + d.ko if not p.final and p.kickoff > now]
        if not upcoming:
            continue  # over
        rp = ev.replay(d)
        if rp.problems:
            counts["withheld"] += 1
            problems.append(f"tournament {ed.key}: withheld: the replay found "
                            f"{len(rp.problems)} problems")  # fmt: skip
            continue
        run, _ = ns.latest(session, now)
        if run is None or run.as_of is None or not ns.current(run, min(upcoming)):
            counts["withheld"] += 1
            continue  # no current refit: the national panel reports it
        refit_as_of = ns._utc(run.as_of)
        if refit_as_of > now:
            raise ValueError(f"refit {run.model_run_id} is from the future")  # never leak
        finals = _finals(d, now)
        fingerprint = f"{run.model_run_id}:{finals}"
        last = last_forecast(session, ed.key)
        seen = None if last is None else last.component_versions[COMPONENT].get("fingerprint")
        if seen == fingerprint:
            counts["unchanged"] += 1
            continue
        post = nl.load(store, run)
        st = ev.state_at(d, rp, now)
        ev._clamp_months(st, nat.month_index(refit_as_of))
        rt = ev.rates_for(d, post, {"n_months": int(run.manifest["n_months"])}, st, tm.SEED)
        res = tm.simulate(ed, st, rt, tm.N_SIMS, tm.SEED)
        body = payload(d, res, now, refit_as_of, run.model_run_id, False, finals)
        key = f"{PREFIX}/{ed.key}/{now:%Y%m%dT%H%M}.json"
        store.put(key, json.dumps(body).encode())
        session.add(models.ProjectionRun(
            generated_at=now, as_of=now, git_commit=manifest["git_commit"],
            lockfile_hash=manifest["lockfile_hash"], config_hash=run.config_hash,
            component_versions={COMPONENT: {
                "edition": ed.key, "key": key, "fingerprint": fingerprint,
                "model_run_id": run.model_run_id, "as_of": refit_as_of.isoformat(),
                "n_sims": tm.N_SIMS}},
            training_snapshot_hash=hashlib.sha256(run.data_snapshot_hash.encode()).hexdigest(),
            source_snapshot_ids={}, override_set_hash="none", seed=tm.SEED,
        ))  # fmt: skip
        session.flush()
        counts["written"] += 1
    return counts, problems


def latest_payload(session: Session, store: RawStore, key: str, shadow: bool
                   ) -> dict[str, Any] | None:  # fmt: skip
    """The edition's latest served forecast, or its shadow forecast."""
    if shadow:
        raw = try_get(store, shadow_key(key))
    else:
        pr = last_forecast(session, key)
        raw = None if pr is None else try_get(store, pr.component_versions[COMPONENT]["key"])
    return None if raw is None else dict(json.loads(raw))


def served(session: Session) -> list[dict[str, Any]]:
    """The live editions with a served forecast: key, name and its as-of."""
    out = []
    for ed in live_editions():
        pr = last_forecast(session, ed.key)
        if pr is not None:
            out.append({"edition": ed.key, "name": ed.name,
                        "as_of": pr.generated_at.isoformat()})  # fmt: skip
    return out


def shadow(session: Session, store: RawStore, key: str) -> dict[str, Any]:
    """A past edition as of its opening Monday from the cached refit (acceptance test 6)."""
    ed = tm.load_formats()[key]
    if ed.window == "live":
        raise ValueError("shadow is for past editions")
    d = ev.load(session, store, ed)
    rp = ev.replay(d)
    if rp.problems:
        raise ValueError(f"{key}: replay problems")
    t0 = ev.monday(min(p.kickoff for p in d.group_matches))
    res, _, _ = ev.forecast(store, d, rp, t0, tm.N_SIMS)
    post, _ = ev.refit(store, t0)
    body = payload(d, res, t0, datetime.fromtimestamp(float(post["_as_of"]), UTC), None, True,
                   _finals(d, t0))  # fmt: skip
    store.put(shadow_key(key), json.dumps(body).encode())
    return body


def main(argv: Sequence[str] | None = None) -> None:
    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session

    ap = argparse.ArgumentParser(prog="python -m adatta.sim.tournament_serve")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("shadow")
    s.add_argument("--edition", required=True)
    args = ap.parse_args(argv)
    body = shadow(get_session(), make_store(get_settings()), args.edition)
    print(json.dumps({k: body[k] for k in ("edition", "as_of", "refit_as_of", "n_sims",
                                            "mc_se_max", "finals_fixed")}))  # fmt: skip


if __name__ == "__main__":
    main()
