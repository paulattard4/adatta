"""P3-j step 3: the season-end verdict on J's live shadow (docs/PHASE3_TASKS.md P3-j (d),
D65), by the gate pre-registered in config/lineup_j_live_gate.yml. Read once: the judge
refuses before `opens_after` and after a stored verdict.

    python -m adatta.eval.lineup_j_live judge

Scores come from each shadow row's posterior-mean score matrix and the 90' result. The
repo is public: the output carries metrics only.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from typing import Any

import numpy as np
import polars as pl
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.eval import knockout as kb

CONFIG = "config/lineup_j_live_gate.yml"
MARKER = "serving/lineup_j/verdict/read"
METRICS = ("ll_1x2", "rps", "ll_ou25", "ll_btts", "crps_total")
Z95 = 1.959963984540054


def load_gate(path: str = CONFIG) -> dict[str, Any]:
    with open(path) as f:
        return dict(yaml.safe_load(f)["lineup_j_live_gate"])


def scores(matrix: list[float], hg: int, ag: int) -> dict[str, float]:
    """1X2 log loss and RPS, O/U 2.5 and BTTS log loss, total-goals CRPS of one matrix."""
    n = int(round(math.sqrt(len(matrix))))
    m = np.asarray(matrix, dtype=np.float64).reshape(n, n)
    m = m / m.sum()
    i, j = np.indices(m.shape)
    p = np.array([m[i > j].sum(), np.trace(m), m[i < j].sum()])
    o = 0 if hg > ag else 1 if hg == ag else 2
    cum, hit = np.cumsum(p)[:2], np.array([o == 0, o <= 1], dtype=np.float64)
    over, btts = float(m[(i + j) >= 3].sum()), float(m[1:, 1:].sum())

    def ll(q: float, yes: bool) -> float:
        return float(-np.log(np.clip(q if yes else 1 - q, 1e-12, 1.0)))

    tot = np.array([m[(i + j) == t].sum() for t in range(2 * n - 1)])
    y = min(hg + ag, 2 * n - 2)
    crps = float(np.sum((np.cumsum(tot) - (np.arange(2 * n - 1) >= y)) ** 2))
    return {"ll_1x2": float(-np.log(max(p[o], 1e-12))), "rps": float(np.sum((cum - hit) ** 2) / 2),
            "ll_ou25": ll(over, hg + ag > 2), "ll_btts": ll(btts, hg > 0 and ag > 0),
            "crps_total": crps, "p_home": float(p[0]), "p_draw": float(p[1]),
            "p_away": float(p[2])}  # fmt: skip


def paired(j: np.ndarray, p: np.ndarray) -> dict[str, float]:
    """Mean of J - P relative to P's mean, with its paired 95% interval."""
    d = j - p
    base = float(p.mean())
    se = float(d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 1 else float("nan")
    m = float(d.mean())
    return {"n": float(len(d)), "rel": m / base, "lo": (m - Z95 * se) / base,
            "hi": (m + Z95 * se) / base}  # fmt: skip


def scored(rows: pl.DataFrame) -> pl.DataFrame:
    """One row per match x horizon x model with its scores; rows carry match_id,
    league_id, hg, ag, horizon, model, matrix."""
    out = [{**{k: r[k] for k in ("match_id", "league_id", "hg", "ag", "horizon", "model",
                                 "model_run_id")},
            **scores(r["matrix"], int(r["hg"]), int(r["ag"]))}
           for r in rows.iter_rows(named=True)]  # fmt: skip
    return pl.DataFrame(out)


def verdict(sc: pl.DataFrame, gate: dict[str, Any], runtime_ratio: float, failed_refits: int,
            ) -> dict[str, Any]:  # fmt: skip
    """The gate at the headline horizon; every horizon's pairs reported."""
    out: dict[str, Any] = {"horizons": {}}
    for h in sorted({str(x) for x in sc["horizon"]}):
        s = sc.filter(pl.col("horizon") == h)
        w = s.pivot(on="model", index=["match_id", "league_id", "hg", "ag"],
                    values=list(METRICS)).drop_nulls()  # fmt: skip
        res = {m: paired(w[f"{m}_J"].to_numpy(), w[f"{m}_P"].to_numpy()) for m in METRICS}
        seg = {f"{lg} {m}": paired(g[f"{m}_J"].to_numpy(), g[f"{m}_P"].to_numpy())["rel"]
               for (lg,), g in w.group_by("league_id") for m in METRICS}  # fmt: skip
        out["horizons"][h] = {"pairs": w.height, "metrics": res, "segments": seg}
    head = out["horizons"].get(gate["horizon"])
    if head is None:
        out["pass"] = False
        out["reason"] = "no pairs at the headline horizon"
        return out
    ll, rps = head["metrics"]["ll_1x2"], head["metrics"]["rps"]
    route1 = ll["hi"] < 0 and rps["hi"] < 0
    route2 = (ll["rel"] < 0 and rps["rel"] < 0 and ll["hi"] < gate["route2_upper"]
              and rps["hi"] < gate["route2_upper"])  # fmt: skip
    worst = max(head["segments"].values())
    jrows = sc.filter((pl.col("horizon") == gate["horizon"]) & (pl.col("model") == "J"))
    cal = kb.a1_calibration(jrows.select("p_home", "p_draw", "p_away", "hg", "ag"),
                            n_sims=int(gate["ece_sims"]))  # fmt: skip
    cal_ok = all(cal[k]["pass"] for k in ("home", "draw", "away"))
    checks = {"route 1": route1, "route 2": route2,
              "segments": worst <= gate["max_segment_rel"], "calibration": cal_ok,
              "inference": True, "runtime": runtime_ratio <= gate["max_runtime_ratio"]}  # fmt: skip
    n = head["pairs"]
    se_rel = gate["dev_sd"] / math.sqrt(max(n, 1)) / gate["dev_mean_p"]
    out |= {"checks": checks, "route": "1" if route1 else "2" if route2 else None,
            "worst_segment": worst, "calibration": cal, "runtime_ratio": runtime_ratio,
            "failed_refits": failed_refits,
            "detectable_gain": (Z95 + 0.8416212335729143) * se_rel,
            "pass": bool((route1 or route2) and all(v for k, v in checks.items()
                                                     if not k.startswith("route")))}  # fmt: skip
    return out


def load(session: Session, gate: dict[str, Any], leagues: list[int]) -> pl.DataFrame:
    """Shadow rows of the season's final bundle league matches, with the 90' score."""
    from adatta.eval import harness

    m, _ = harness.load_matches(session, leagues)
    final = pl.col("hg").is_not_null() & pl.col("ag").is_not_null()
    m = m.filter((pl.col("season") == gate["season"]) & final)
    m = m.select("match_id", "league_id", "hg", "ag")
    S = models.LineupJShadow
    rows = session.execute(
        select(S.match_id, S.horizon, S.model, S.model_run_id, S.matrix)
        .where(S.match_id.in_(m["match_id"].to_list()))
    ).all()  # fmt: skip
    sh = pl.DataFrame([tuple(r) for r in rows], orient="row",
                      schema={"match_id": pl.Int64, "horizon": pl.Utf8, "model": pl.Utf8,
                              "model_run_id": pl.Int64, "matrix": pl.Object})  # fmt: skip
    return sh.join(m, on="match_id")


def runtime(session: Session, j_ids: set[int]) -> tuple[float, int]:
    """J's median refit time over P's, over the refits pairs were read from, and the
    season's failing J shadow refits."""
    MR = models.ModelRun
    j = session.scalars(select(MR).where(MR.component == "p3j", MR.window == "shadow")).all()
    used = [r for r in j if r.model_run_id in j_ids]
    p_ids = {int(r.manifest["p_run"]) for r in used}
    p = session.scalars(select(MR).where(MR.model_run_id.in_(p_ids))).all() if p_ids else []
    jt = [float(r.manifest["diag"].get("fit_s", np.nan)) for r in used]
    pt = [float(r.manifest["diag"].get("fit_s", np.nan)) for r in p]
    ratio = float(np.nanmedian(jt) / np.nanmedian(pt)) if jt and pt else float("nan")
    return ratio, sum(r.status != "ok" for r in j)


def markdown(v: dict[str, Any]) -> str:
    route = f" (route {v['route']})" if v.get("route") else ""
    lines = ["# P3-j step 3: J vs P live (shadow), read once", "",
             f"**Gate: {'PASS' if v['pass'] else 'FAIL'}**{route}", ""]  # fmt: skip
    for h, x in v["horizons"].items():
        lines.append(f"## {h}: {x['pairs']} pairs (negative = J better)")
        for m, r in x["metrics"].items():
            lines.append(f"- {m}: {r['rel']:+.2%} [{r['lo']:+.2%}, {r['hi']:+.2%}]")
    if "checks" in v:
        lines += ["", "## Checks"] + [f"- {k}: {'yes' if ok else 'no'}"
                                      for k, ok in v["checks"].items()]  # fmt: skip
        lines.append(f"- worst segment {v['worst_segment']:+.2%}; runtime ratio "
                     f"{v['runtime_ratio']:.2f}; failing J refits {v['failed_refits']}; "
                     f"detectable gain at this n {v['detectable_gain']:.2%}")  # fmt: skip
    return "\n".join(lines) + "\n"


def judge(session: Session, store: RawStore, leagues: list[int], now: datetime,
          gate: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    gate = gate or load_gate()
    if now < datetime.fromisoformat(gate["opens_after"]).replace(tzinfo=UTC):
        raise SystemExit(f"the window is read once, after {gate['opens_after']}")
    if try_get(store, MARKER) is not None:
        raise SystemExit("the verdict was already read; an opened window is never re-judged")
    rows = load(session, gate, leagues)
    sc = scored(rows)
    j_ids = {int(x) for x in sc.filter(pl.col("model") == "J")["model_run_id"]}
    ratio, failed = runtime(session, j_ids)
    v = verdict(sc, gate, ratio, failed)
    store.put(MARKER, json.dumps({"read_at": now.isoformat(), "pass": v["pass"]}).encode())
    return v


def main() -> None:
    ap = argparse.ArgumentParser(prog="adatta.eval.lineup_j_live")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("judge", help="the season-end verdict, read once")
    ap.parse_args()
    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.db.session import get_session
    from adatta.eval import harness

    cfg = harness.load_config()
    leagues = [int(x) for xs in cfg.bundles.values() for x in xs]
    with get_session() as s:
        v = judge(s, make_store(get_settings()), leagues, datetime.now(UTC))
    print(markdown(v))


if __name__ == "__main__":
    main()
