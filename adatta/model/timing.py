"""Time one Phase 1b fit (D9 sizing): England bundle, as of the start of 2023/24.

    scripts/model.sh uv run --frozen --group model python -m adatta.model.timing

Prints wall time and the main §14.5 diagnostics. Metrics only (logs may be public on CI).
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import numpy as np

from adatta.db.session import get_session
from adatta.eval.harness import load_config, load_matches
from adatta.model import team_strength as ts


def main() -> None:
    import arviz as az
    import pymc as pm

    cfg = load_config()
    leagues = cfg.bundles["england"]
    with get_session() as s:
        df, _ = load_matches(s, leagues)
    as_of = datetime(2023, 8, 7, tzinfo=UTC)
    fd = ts.prepare(df, leagues, as_of, cfg.lookback_days)
    chains = int(os.environ.get("CHAINS", "4"))
    draws = int(os.environ.get("DRAWS", "1000"))
    print(f"matches={len(fd.hg)} teams={len(fd.team_ids)} steps={fd.n_steps} "
          f"latent~{2 * fd.n_steps * len(fd.team_ids)} chains={chains} draws={draws}")  # fmt: skip
    t0 = time.time()
    model = ts.build(fd)
    with model:
        idata = pm.sample(
            draws=draws, tune=draws, chains=chains, nuts_sampler="numpyro", random_seed=20261105,
            target_accept=float(os.environ.get("TARGET_ACCEPT", "0.95")), progressbar=False,
        )  # fmt: skip
    wall = time.time() - t0
    summ = az.summary(idata, var_names=["~attack", "~defence", "~_z"], filter_vars="like")
    div = int(idata.sample_stats["diverging"].sum())
    print(f"wall_seconds={wall:.0f} divergences={div} "
          f"rhat_max={summ['r_hat'].max():.3f} ess_bulk_min={summ['ess_bulk'].min():.0f} "
          f"ess_tail_min={summ['ess_tail'].min():.0f}")  # fmt: skip
    print(summ.loc[[i for i in summ.index if "[" not in i or "league" in i]].to_string())
    energy = idata.sample_stats["energy"].values  # (chain, draw)
    bfmi = (np.diff(energy, axis=1) ** 2).mean(axis=1) / energy.var(axis=1)
    print("bfmi per chain:", [round(float(b), 2) for b in bfmi])
    post, divs = idata.posterior, idata.sample_stats["diverging"]
    for v in ("sigma_walk", "reset_weeks", "sigma_home", "sigma_attack", "sigma_defence"):
        x = post[v].values.ravel()
        d = divs.values.ravel()
        if d.any():
            print(f"{v}: divergent mean {x[d].mean():.4f} vs all {x.mean():.4f} "
                  f"(q05 {np.quantile(x, 0.05):.4f})")  # fmt: skip


if __name__ == "__main__":
    main()
