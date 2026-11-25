"""C2 step 2 data scope (D49, D52; docs/BEYOND_LEAGUES_PLAN.md "C2 step 2 acceptance").

The eight new bundles live in `config/c2_bundles.yml`, apart from `evaluation.yml` (part of
every fit's cache hash), so the five bundles' data, plans and cached refits are untouched.

- Rounds: each league's round-label families are classed regular / phase / excluded. A phase
  match counts only if both sides played that league-season's regular season, and a phase
  group (connected sides) with fewer than three sides is a tie and is dropped: cross-tier
  play-offs share labels with groups in some leagues. An unclassed label raises.
- Targets: top-flight matches only (`target`). Second tiers are training rows only.
- Availability: production loads second tiers once a summer, so a second-tier match is
  observable from `second_tier_available` after its season (`available_at`), never at
  kickoff + lag. `harness.refit_plan` reads both columns; `observable` filters a frame to
  what a refit at `as_of` may read before `team_strength.prepare`.

    uv run python -m adatta.eval.c2 report     # rows kept per league-season (counts only)
"""

from __future__ import annotations

import argparse
import hashlib
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl
import yaml
from pydantic import BaseModel
from sqlalchemy.orm import Session

from adatta.eval import harness

FILE = "config/c2_bundles.yml"
CODE = "adatta/eval/c2.py"
Klass = Literal["regular", "phase", "excluded"]
FAMILY = re.compile(r" - \d+$")
MIN_GROUP = 3  # sides in a phase group; fewer is a tie


class UnclassedRound(ValueError):
    pass


class C2Config(BaseModel):
    second_tier_available: str  # "MM-DD" after the season's start year + 1
    bundles: dict[str, list[int]]
    rounds: dict[int, dict[str, Klass]]

    def available_from(self, season: int) -> datetime:
        month, day = (int(x) for x in self.second_tier_available.split("-"))
        return datetime(season + 1, month, day, tzinfo=UTC)


def load_config(path: Path | str = FILE) -> C2Config:
    return C2Config(**yaml.safe_load(Path(path).read_text()))


def digest(files: tuple[str, ...] = (FILE, CODE)) -> str:
    """What C2 bundles' cache hashes add: the scope config and this code."""
    h = hashlib.sha256()
    for f in files:
        h.update(f.encode())
        h.update(Path(f).read_bytes())
    return h.hexdigest()[:16]


def is_c2(bundle: str, c2: C2Config | None = None) -> bool:
    return bundle in (c2 or load_config()).bundles


def leagues_of(bundle: str, five: dict[str, list[int]], c2: C2Config | None = None) -> list[int]:
    """A bundle's leagues, top flight first: one of the five (evaluation.yml) or a C2 one."""
    if bundle in five:
        return five[bundle]
    c2 = c2 or load_config()
    if bundle in c2.bundles:
        return c2.bundles[bundle]
    raise SystemExit(f"unknown bundle {bundle!r}")


def bundle_for(leagues: list[int], c2: C2Config | None = None) -> str | None:
    """The C2 bundle with exactly these leagues, or None (one of the five)."""
    c2 = c2 or load_config()
    return next((b for b, ls in c2.bundles.items() if ls == list(leagues)), None)


def family(label: str) -> str:
    return FAMILY.sub("", label)


def classify(df: pl.DataFrame, rounds: dict[int, dict[str, Klass]]) -> pl.DataFrame:
    """`df` with `family` and `klass`; UnclassedRound on a label the config does not list."""
    df = df.with_columns(family=pl.col("round").map_elements(family, return_dtype=pl.Utf8))
    pairs = df.select("league_id", "family").unique().rows()
    missing = sorted((lg, f) for lg, f in pairs if f not in rounds.get(int(lg), {}))
    if missing:
        raise UnclassedRound(f"round labels not classed in {FILE}: {missing}")
    table = pl.DataFrame(
        [{"league_id": lg, "family": f, "klass": k}
         for lg, fams in rounds.items() for f, k in fams.items()],
        schema={"league_id": pl.Int64, "family": pl.Utf8, "klass": pl.Utf8},
    )  # fmt: skip
    return df.join(table, on=["league_id", "family"], how="left")


def _components(pairs: list[tuple[int, int]]) -> list[set[int]]:
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for h, a in pairs:
        parent[find(h)] = find(a)
    groups: dict[int, set[int]] = defaultdict(set)
    for x in list(parent):
        groups[find(x)].add(x)
    return list(groups.values())


def scope(df: pl.DataFrame, c2: C2Config) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(rows in scope, every finished row with its class and fate) from `finished_rows`.
    Fates: regular, phase, excluded, cross-tier (a phase match with a side outside the
    league-season's regular season), tie (a phase group of fewer than MIN_GROUP sides)."""
    df = classify(df, c2.rounds)
    reg = df.filter(pl.col("klass") == "regular")
    sides = (pl.concat([reg.select("league_id", "season", team="home"),
                        reg.select("league_id", "season", team="away")])
             .unique().group_by("league_id", "season").agg(pl.col("team")))  # fmt: skip
    members = {(int(lg), int(s)): set(ts) for lg, s, ts in sides.rows()}
    fate: dict[int, str] = {}
    phase = df.filter(pl.col("klass") == "phase")
    for (lg, season, _fam), g in phase.group_by("league_id", "season", "family"):
        inside = members.get((int(lg), int(season)), set())
        inner: list[tuple[int, int, int]] = []
        for mid, h, a in g.select("match_id", "home", "away").rows():
            if h in inside and a in inside:
                inner.append((mid, h, a))
            else:
                fate[mid] = "cross-tier"
        for comp in _components([(h, a) for _, h, a in inner]):
            ok = len(comp) >= MIN_GROUP
            for mid, h, _ in inner:
                if h in comp:
                    fate[mid] = "phase" if ok else "tie"
    fates = pl.DataFrame({"match_id": list(fate), "fate": list(fate.values())},
                         schema={"match_id": pl.Int64, "fate": pl.Utf8})  # fmt: skip
    df = df.join(fates, on="match_id", how="left").with_columns(
        fate=pl.coalesce(pl.col("fate"), pl.col("klass"))
    )
    kept = df.filter(pl.col("fate").is_in(["regular", "phase"]))
    return kept.with_columns(phase=pl.col("fate") == "phase").drop("family", "klass", "fate"), df


def annotate(df: pl.DataFrame, leagues: list[int], c2: C2Config, lag_hours: float) -> pl.DataFrame:
    """`target` (top flight) and `available_at` (kickoff + lag; for a second-tier row no
    earlier than the summer load after its season)."""
    lag = timedelta(hours=lag_hours)
    top = leagues[0]
    seasons = sorted({int(s) for s in df["season"].unique()})
    load = pl.DataFrame(
        {"season": seasons, "loaded": [c2.available_from(s) for s in seasons]},
        schema={"season": pl.Int64, "loaded": pl.Datetime("us", "UTC")},
    )
    return (
        df.join(load, on="season", how="left")
        .with_columns(target=pl.col("league_id") == top,
                      available_at=pl.when(pl.col("league_id") == top)
                      .then(pl.col("kickoff") + lag)
                      .otherwise(pl.max_horizontal(pl.col("kickoff") + lag, pl.col("loaded"))))
        .drop("loaded")
    )  # fmt: skip


def load_matches(session: Session, leagues: list[int], lag_hours: float,
                 c2: C2Config | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:  # fmt: skip
    """The C2 counterpart of `harness.load_matches`: rows in scope with a known 90' score
    (plus `target`, `available_at`, `phase`), and the unscorable ones with the reason."""
    c2 = c2 or load_config()
    kept, _ = scope(harness.finished_rows(session, leagues), c2)
    df, unscorable = harness.split_known(harness.dedupe(kept))
    return annotate(df, leagues, c2, lag_hours), unscorable


def observable(df: pl.DataFrame, as_of: datetime) -> pl.DataFrame:
    """The rows a refit at `as_of` may read, for frames with `available_at` (C2); else df."""
    if "available_at" not in df.columns:
        return df
    return df.filter(pl.col("available_at") <= as_of)


def dev_verdict(cmp: pl.DataFrame, gate: harness.Phase1bGate) -> dict[str, Any]:
    """D52 (2a): on dev, no top flight worse than Phase 1a by more than the segment limit on
    any D28 segment metric (warm matches). The gain is the holdout's to decide."""
    seg = cmp.filter((pl.col("segment_type") == "league_id")
                     & pl.col("metric").is_in(gate.segment_metrics))  # fmt: skip
    if seg.height == 0:
        raise ValueError("no league segments")
    checks = [{"name": f"league {r['seg']} {r['metric']}", "value": r["rel"],
               "limit": gate.max_segment_rel_worse,
               "passed": r["rel"] <= gate.max_segment_rel_worse}
              for r in seg.sort("seg", "metric").iter_rows(named=True)]  # fmt: skip
    return {
        "verdict": "pass" if all(c["passed"] for c in checks) else "fail",
        "checks": checks,
        "failed": [c["name"] for c in checks if not c["passed"]],
    }


def report(session: Session, c2: C2Config, lag_hours: float) -> pl.DataFrame:
    """Rows per league-season by fate, plus kept and unscorable (counts only)."""
    out = []
    for bundle, leagues in c2.bundles.items():
        _, fates = scope(harness.finished_rows(session, leagues), c2)
        kept, unscorable = load_matches(session, leagues, lag_hours, c2)
        counts = fates.group_by("league_id", "season", "fate").len()
        for (lg, season), g in counts.group_by("league_id", "season"):
            row: dict[str, Any] = {"bundle": bundle, "league_id": int(lg), "season": int(season),
                                   "tier": leagues.index(int(lg)) + 1}  # fmt: skip
            row |= {f: 0 for f in ("regular", "phase", "excluded", "cross-tier", "tie")}
            row |= dict(zip(g["fate"].to_list(), g["len"].to_list(), strict=True))
            sel = (pl.col("league_id") == lg) & (pl.col("season") == season)
            row["kept"] = kept.filter(sel).height
            row["unscorable"] = unscorable.filter(sel).height
            out.append(row)
    return pl.DataFrame(out).sort("bundle", "tier", "season")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--out", default="reports/c2")
    args = ap.parse_args()
    from adatta.db.session import get_session

    cfg = harness.load_config()
    c2 = load_config()
    with get_session() as s:
        rep = report(s, c2, cfg.result_available_after_hours)
        s.rollback()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"scope_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.csv"
    rep.write_csv(path)
    with pl.Config(tbl_rows=200, tbl_cols=20, tbl_width_chars=200):
        print(rep)
    tot = rep.select(pl.exclude("bundle", "league_id", "season", "tier").sum())
    print(tot)
    print(f"report {path}")


if __name__ == "__main__":
    main()
