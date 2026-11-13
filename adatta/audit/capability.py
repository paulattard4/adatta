"""Capability audit (spec §3.4, §4.4): fill competition_source_capability from real payloads.

Per competition-season: read declared coverage from /leagues, sample finished fixtures with a
fixed seed, fetch their detail in batched /fixtures?ids= calls, and measure each capability's
coverage_ratio. Declared flags only seed expectations; the stored status comes from payloads.

Status rule (from the observed ratio over the sample):
    no finished fixture sampled -> unknown (ratio NULL)
    ratio >= AVAILABLE_MIN       -> available
    0 < ratio < AVAILABLE_MIN    -> partial
    ratio == 0                   -> absent

Temporal rows: the first row for a capability is valid from the season's first kickoff (the
payloads describe the season's matches, so the fact holds as-of then). A later audit with the
same status only updates last_audited_at; a changed status (or ratio moved by more than
RATIO_TOLERANCE) closes the open row at audit time and opens a new one. Rows are never edited
in place otherwise.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import api_football_coverage as cov
from adatta.adapters import normalize
from adatta.adapters.api_football import IDS_BATCH_MAX, SOURCE_ID, ApiFootballClient
from adatta.adapters.quota import QuotaExceeded
from adatta.config import Competition
from adatta.db import models

AVAILABLE_MIN = 0.95
RATIO_TOLERANCE = 0.05
DEFAULT_SAMPLE = 40
DEFAULT_SEED = 20261105


@dataclass
class CapabilityResult:
    capability: str
    declared: bool | None
    n_sampled: int
    n_with: int
    status: str
    coverage_ratio: float | None

    @property
    def mismatch(self) -> bool:
        """Declared coverage contradicts what the payloads show."""
        if self.declared is None or self.status == "unknown":
            return False
        return (self.declared and self.status == "absent") or (
            not self.declared and self.status in ("available", "partial")
        )


@dataclass
class SeasonAudit:
    league_id: int
    competition: str
    season: int
    n_finished: int
    sampled_ids: list[int]
    season_start: datetime | None
    results: list[CapabilityResult] = field(default_factory=list)


def classify(n_with: int, n_sampled: int) -> tuple[str, float | None]:
    if n_sampled == 0:
        return "unknown", None
    ratio = n_with / n_sampled
    if ratio >= AVAILABLE_MIN:
        return "available", ratio
    if ratio > 0:
        return "partial", ratio
    return "absent", 0.0


def sample_ids(finished: list[int], k: int, seed: int, league_id: int, season: int) -> list[int]:
    """Deterministic sample: same seed + league + season + fixture list -> same ids."""
    if len(finished) <= k:
        return sorted(finished)
    rng = random.Random(f"{seed}:{league_id}:{season}")
    return sorted(rng.sample(sorted(finished), k))


def evaluate(
    declared: dict[str, bool | None], details: list[dict[str, Any]]
) -> list[CapabilityResult]:
    """Aggregate per-fixture observations into one result per spec capability."""
    flags = [cov.observed_flags(fx) for fx in details]
    out: list[CapabilityResult] = []
    for cap in cov.OBSERVED_CAPABILITIES:
        n_with = sum(f[cap] for f in flags)
        status, ratio = classify(n_with, len(flags))
        out.append(CapabilityResult(cap, declared.get(cap), len(flags), n_with, status, ratio))
    for cap in cov.POLICY_CAPABILITIES:
        out.append(CapabilityResult(cap, None, 0, 0, "unknown", None))
    return out


def audit_season(
    session: Session,
    client: ApiFootballClient,
    comp: Competition,
    season: int,
    league_seasons: list[dict[str, Any]],
    *,
    sample: int = DEFAULT_SAMPLE,
    seed: int = DEFAULT_SEED,
) -> SeasonAudit:
    declared = cov.declared_flags(cov.season_coverage(league_seasons, season))
    fixtures = client.fixtures(session, comp.league_id, season)
    finished = [fid for fid, _ in cov.finished_fixtures(fixtures)]
    ids = sample_ids(finished, sample, seed, comp.league_id, season)
    details: list[dict[str, Any]] = []
    for i in range(0, len(ids), IDS_BATCH_MAX):
        details.extend(client.fixtures_by_ids(session, ids[i : i + IDS_BATCH_MAX]))
    returned = set(cov.fixture_ids(details))
    if returned != set(ids):
        missing = sorted(set(ids) - returned)
        raise RuntimeError(f"/fixtures?ids= did not return {len(missing)} ids, e.g. {missing[:5]}")
    return SeasonAudit(
        league_id=comp.league_id,
        competition=comp.name,
        season=season,
        n_finished=len(finished),
        sampled_ids=ids,
        season_start=cov.first_kickoff(fixtures),
        results=evaluate(declared, details),
    )


def write_capabilities(
    session: Session, comp: Competition, audit: SeasonAudit, now: datetime | None = None
) -> dict[str, int]:
    """Upsert temporal capability rows for one audited season. Returns counts by action."""
    now = now or datetime.now(UTC)
    normalize.ensure_source(session)
    db_comp = normalize.ensure_competition(
        session,
        league_id=comp.league_id,
        name=comp.name,
        country=comp.country,
        level=comp.level,
        tier=str(comp.tier),
        is_national=comp.national,
    )
    db_season = normalize.ensure_season(session, db_comp.competition_id, audit.season)
    counts = {"inserted": 0, "confirmed": 0, "superseded": 0}
    T = models.CompetitionSourceCapability
    for r in audit.results:
        cur = session.scalar(
            select(T).where(
                T.competition_id == db_comp.competition_id,
                T.season_id == db_season.season_id,
                T.source_id == SOURCE_ID,
                T.capability == r.capability,
                T.valid_to.is_(None),
            )
        )
        if cur is not None and _same(cur, r):
            cur.last_audited_at = now
            counts["confirmed"] += 1
            continue
        if cur is not None:
            cur.valid_to = now
            valid_from = now
            counts["superseded"] += 1
        else:
            valid_from = audit.season_start or now
            counts["inserted"] += 1
        session.add(
            T(
                competition_id=db_comp.competition_id,
                season_id=db_season.season_id,
                source_id=SOURCE_ID,
                capability=r.capability,
                coverage_ratio=r.coverage_ratio,
                status=r.status,
                valid_from=valid_from,
                last_audited_at=now,
            )
        )
    session.flush()
    return counts


def _same(row: models.CompetitionSourceCapability, r: CapabilityResult) -> bool:
    if row.status != r.status:
        return False
    if row.coverage_ratio is None or r.coverage_ratio is None:
        return row.coverage_ratio is r.coverage_ratio
    # round: 1.0 - 0.95 is 0.0500000000000000044 in floats and must count as within tolerance
    return round(abs(row.coverage_ratio - r.coverage_ratio), 9) <= RATIO_TOLERANCE


def report_rows(audits: list[SeasonAudit]) -> list[dict[str, Any]]:
    return [
        {
            "league_id": a.league_id,
            "competition": a.competition,
            "season": a.season,
            "n_finished": a.n_finished,
            "capability": r.capability,
            "declared": r.declared,
            "n_sampled": r.n_sampled,
            "n_with": r.n_with,
            "coverage_ratio": r.coverage_ratio,
            "status": r.status,
            "mismatch": r.mismatch,
        }
        for a in audits
        for r in a.results
    ]


def run_audit(
    session: Session,
    client: ApiFootballClient,
    comps: list[Competition],
    *,
    sample: int = DEFAULT_SAMPLE,
    seed: int = DEFAULT_SEED,
    season: int | None = None,
) -> tuple[list[SeasonAudit], list[str]]:
    """Audit every configured (competition, season); commit per season so a stop is resumable.

    Returns the audits and a list of errors. Stops cleanly when the quota budget runs out.
    """
    audits: list[SeasonAudit] = []
    errors: list[str] = []
    for comp in comps:
        years = [season] if season is not None else comp.seasons
        try:
            league_seasons = client.league_seasons(session, comp.league_id)
        except QuotaExceeded:
            errors.append("quota budget exhausted; re-run to continue")
            session.commit()
            break
        for year in years:
            try:
                a = audit_season(
                    session, client, comp, year, league_seasons, sample=sample, seed=seed
                )
                write_capabilities(session, comp, a)
                session.commit()
                audits.append(a)
            except QuotaExceeded:
                session.commit()
                errors.append("quota budget exhausted; re-run to continue")
                return audits, errors
            except Exception as exc:  # one bad season must not abort the audit
                session.rollback()
                errors.append(f"{comp.name} {year}: {type(exc).__name__}: {exc}")
    return audits, errors
