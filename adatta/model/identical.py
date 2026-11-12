"""Check that the team model's default path gives the same draws as at an earlier commit.

A change that adds an optional term (a flag, a covariate group) must leave the model with
the option off bit-identical: same seed, same draws (Phase 2 M2). This samples a small
synthetic fit with team_strength.py as it was at REF and as it is now, for both structures,
and compares every stored variable exactly. Prints metrics only (CI logs are public).

    uv run --frozen --group model python -m adatta.model.identical --ref <commit>

Needs the model group (Linux / Docker, D13) and the commit in the local history.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import numpy as np
import polars as pl

MODULE = "adatta/model/team_strength.py"
LEAGUES = [39, 40]


def synthetic() -> pl.DataFrame:
    """Two leagues, three seasons of weekly rounds (as tests/test_backtest.matches)."""
    rng = np.random.default_rng(0)
    rows, mid = [], 0
    for season in (2017, 2018, 2019):
        start = datetime(season, 8, 7, 15, tzinfo=UTC)
        for week in range(36):
            for lg, base in ((39, 0), (40, 10)):
                teams = rng.permutation(np.arange(base, base + 10))
                for i in range(0, 10, 2):
                    mid += 1
                    rows.append({
                        "match_id": mid, "league_id": lg, "season": season,
                        "kickoff": start + timedelta(days=7 * week, hours=i),
                        "home": int(teams[i]) + 1000, "away": int(teams[i + 1]) + 1000,
                        "round": "Regular Season - 1",
                        "hg": int(rng.poisson(1.5)), "ag": int(rng.poisson(1.1)),
                    })  # fmt: skip
    return pl.DataFrame(rows)


def at_ref(ref: str, tmp: Path) -> ModuleType:
    source = subprocess.run(["git", "show", f"{ref}:{MODULE}"], check=True,
                            capture_output=True, text=True).stdout  # fmt: skip
    path = tmp / "team_strength_ref.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("team_strength_ref", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def compare(old: ModuleType, new: ModuleType, draws: int = 200) -> bool:
    df = synthetic()
    as_of = df["kickoff"][900] + timedelta(days=1)
    ok = True
    for structure in ("strength_open", "independent"):
        a = old.slim(old.sample(old.prepare(df, LEAGUES, as_of, 1095, 3.0), seed=11,
                                draws=draws, chains=2, structure=structure))  # fmt: skip
        b = new.slim(new.sample(new.prepare(df, LEAGUES, as_of, 1095, 3.0), seed=11,
                                draws=draws, chains=2, structure=structure))  # fmt: skip
        same = a.keys() == b.keys() and all(np.array_equal(a[k], b[k]) for k in a)
        print(f"{structure}: {len(a)} variables x {draws * 2} draws, bit-identical: {same}")
        ok &= same
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", required=True, help="the commit to compare with")
    args = ap.parse_args()
    from adatta.model import team_strength as new

    with tempfile.TemporaryDirectory() as tmp:
        old = at_ref(args.ref, Path(tmp))
        ok = compare(old, new)
    print(f"default model vs {args.ref[:12]}: {'identical' if ok else 'DIFFERENT'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
