"""P3b-2, serving the half markets (docs/PHASE3B_TASKS.md; spec §8.2; D51). Numpy only.

Within each stored 90' draw d, the score matrix M_d is split by binomial thinning with the
production share refit's draw d (`adatta.sim.half_split`), and the half markets are read
off per draw: half-time 1X2, HT/FT (the nine combinations) and half-time O/U 1.5, the three
markets that gated on dev and on window (i). The published probability is the mean over
draws and the interval the 5th-95th percentile of the per-draw probability, as for every
90' market.

Scope (rule 8): regular-season league fixtures of the bundle leagues whose league-season has
`has_half_split_stats` available, with a passing share refit no older than MAX_AGE. Anything
else gets no half rows, never an imputed split.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import SOURCE_ID
from adatta.adapters.storage import RawStore
from adatta.db import models
from adatta.model import half_split as hm
from adatta.sim import half_split as hs

Arr = NDArray[np.float64]
Sel = tuple[str, float | None, str]
MAX_AGE = timedelta(days=28)  # the dev share grid (D14)
STALE_AFTER = timedelta(days=9)  # reported, as for the 90' refits
TOL = 1e-9
CODES = ("1", "X", "2")  # home, draw, away
HTFT_SELS = tuple(f"{a}/{b}" for a in CODES for b in CODES)  # half time / full time
HT_OU_LINE = 1.5
MARKETS = ("HT1X2", "HTFT", "HTOU")


def _utc(t: datetime) -> datetime:
    from datetime import UTC

    return t if t.tzinfo else t.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Share:
    model_run_id: int
    as_of: datetime
    post: dict[str, Arr]
    league_ids: list[int]
    team_ids: list[int]

    def sides(self, league: int, home: int, away: int) -> tuple[Arr, Arr]:
        """(draws,) first-half share of each side's goals (S0: the side term only; a league
        outside the fit takes the pooled side term, alpha = 0 there in S0)."""
        lg = league if league in self.league_ids else self.league_ids[0]
        ph = hm.share(self.post, self.league_ids, self.team_ids, 0, lg, home)
        pa = hm.share(self.post, self.league_ids, self.team_ids, 1, lg, away)
        return ph, pa


def latest_share(session: Session, now: datetime
                 ) -> tuple[models.ModelRun | None, list[str]]:  # fmt: skip
    """The latest passing production share refit with as_of <= now and no older than
    MAX_AGE, and problems (newest refit failed; serving refit older than STALE_AFTER; none
    usable)."""
    MR = models.ModelRun
    rows = session.scalars(
        select(MR).where(MR.component == hm.LIVE_COMPONENT, MR.window == "live",
                         MR.model_name == hm.LIVE_MODEL, MR.as_of <= now)
        .order_by(MR.as_of, MR.model_run_id)
    ).all()  # fmt: skip
    problems = []
    if rows and rows[-1].status != "ok":
        problems.append(f"half split: share refit as of {rows[-1].as_of:%Y-%m-%d} "
                        f"{rows[-1].status}")  # fmt: skip
    passing = [r for r in rows if r.status == "ok" and r.as_of is not None]
    if not passing:
        problems.append("half split: no passing share refit; no half markets")
        return None, problems
    run = passing[-1]
    assert run.as_of is not None
    age = now - _utc(run.as_of)
    if age > MAX_AGE:
        problems.append(f"half split: latest passing share refit is as of {run.as_of:%Y-%m-%d}, "
                        f"older than {MAX_AGE.days} days; no half markets")  # fmt: skip
        return None, problems
    if age > STALE_AFTER:
        problems.append(f"half split: latest passing share refit is as of {run.as_of:%Y-%m-%d}, "
                        f"older than {STALE_AFTER.days} days")  # fmt: skip
    return run, problems


def load_share(store: RawStore, run: models.ModelRun) -> Share:
    if run.trace_path is None or run.as_of is None:
        raise ValueError(f"model_run {run.model_run_id} has no stored share draws")
    post, lids, tids = hm.decode(store.get(run.trace_path))
    return Share(run.model_run_id, _utc(run.as_of), post, lids, tids)


def in_scope(session: Session, pairs: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """The (league_id, season year) pairs with `has_half_split_stats` available."""
    if not pairs:
        return set()
    K, C, S = models.CompetitionSourceCapability, models.Competition, models.Season
    rows = session.execute(
        select(C.source_ref, S.year).select_from(K)
        .join(C, C.competition_id == K.competition_id).join(S, S.season_id == K.season_id)
        .where(K.capability == "has_half_split_stats", K.valid_to.is_(None),
               K.status == "available", C.source_id == SOURCE_ID,
               C.source_ref.in_(sorted({str(lg) for lg, _ in pairs})))
    ).all()  # fmt: skip
    return {(int(a), int(b)) for a, b in rows} & pairs


def pair(p: Arr, n: int) -> Arr:
    """`p`'s draws spread evenly over n draws (thinned, or repeated when there are fewer),
    to pair by index with the n stored 90' draws."""
    idx = (np.arange(n) * len(p)) // n
    return np.asarray(p[idx], dtype=np.float64)


def draw_markets(m: Arr, ph: Arr, pa: Arr) -> dict[Sel, Arr]:
    """Per-draw probability of every served half selection from (D, g, g) 90' matrices and
    the paired (D,) shares."""
    g = m.shape[1]
    i, j = np.indices((g, g))
    bh, ba = hs.thinning(ph, g), hs.thinning(pa, g)

    def split(mm: Arr) -> Arr:  # (D, g, g) half-time matrices of mm, draw by draw
        return np.asarray(np.einsum("dnx,dna,day->dxy", bh, mm, ba), dtype=np.float64)

    joint = np.stack([hs.result(split(m * r)) for r in (i > j, i == j, i < j)],
                     axis=-1)  # (D, 3 HT, 3 FT)  # fmt: skip
    ht = joint.sum(axis=2)
    h1 = split(m)
    under = h1[:, (i + j) <= int(HT_OU_LINE)].sum(axis=1)
    out: dict[Sel, Arr] = {}
    for k, c in enumerate(CODES):
        out[("HT1X2", None, c)] = ht[:, k]
    for a in range(3):
        for b in range(3):
            out[("HTFT", None, HTFT_SELS[3 * a + b])] = joint[:, a, b]
    out[("HTOU", HT_OU_LINE, "over")] = 1.0 - under
    out[("HTOU", HT_OU_LINE, "under")] = under
    return out


def summarise(dm: dict[Sel, Arr], interval: tuple[float, float]
              ) -> dict[Sel, tuple[float, float, float]]:  # fmt: skip
    out = {}
    for s, p in dm.items():
        lo, hi = np.quantile(p, interval)
        out[s] = (float(p.mean()), float(lo), float(hi))
    return out


def coherence(half: dict[Sel, tuple[float, float, float]], ft: tuple[float, float, float]
              ) -> list[str]:  # fmt: skip
    """P3b-2 (c): the HT/FT nine sum to 1 and marginalise to the 90' 1X2 written with them
    (`ft`: home, draw, away) and to the half-time 1X2; half-time O/U 1.5 sums to 1."""
    j = np.array([[half[("HTFT", None, f"{a}/{b}")][0] for b in CODES] for a in CODES])
    ht = np.array([half[("HT1X2", None, c)][0] for c in CODES])
    bad = []
    if abs(j.sum() - 1) > TOL:
        bad.append(f"HT/FT sums to {j.sum():.12f}")
    if np.abs(j.sum(axis=0) - np.array(ft)).max() > TOL:
        bad.append("HT/FT does not marginalise to the 90' 1X2")
    if np.abs(j.sum(axis=1) - ht).max() > TOL:
        bad.append("HT/FT does not marginalise to the half-time 1X2")
    ou = half[("HTOU", HT_OU_LINE, "over")][0] + half[("HTOU", HT_OU_LINE, "under")][0]
    if abs(ou - 1) > TOL:
        bad.append("half-time O/U 1.5 does not sum to 1")
    return bad
