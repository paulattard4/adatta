"""Spec §3.5 Phase 0 tables: bundles synced from config, model runs recorded, override rules."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.db import models
from adatta.db.models import Base
from adatta.eval import baseline, harness


def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    normalize.ensure_source(s)
    return s


def test_bundles_sync_from_config_and_are_idempotent() -> None:
    s = session()
    for lid in (39, 40):
        normalize.ensure_competition(
            s, league_id=lid, name=str(lid), country="England", level="senior", tier="1"
        )
    cfg = harness.load_config()
    assert baseline.sync_bundles(s, cfg) == 2  # other bundles' leagues aren't ingested here
    assert baseline.sync_bundles(s, cfg) == 0
    england = s.scalar(
        select(models.CompetitionBundle).where(models.CompetitionBundle.name == "england")
    )
    assert england is not None
    members = s.scalars(
        select(models.CompetitionBundleMember).where(
            models.CompetitionBundleMember.bundle_id == england.bundle_id
        )
    ).all()
    assert sorted(m.position for m in members) == [0, 1]


def test_record_run_stores_the_manifest() -> None:
    s = session()
    manifest = {
        "created_at": "20261105T162900Z", "code_sha": "abc", "seed": 0,
        "data_snapshot": {"sha256": "d" * 64}, "window_scored": "dev",
        "config_hash": "c" * 64, "lockfile_hash": "l" * 64,
    }  # fmt: skip
    row = baseline.record_run(s, manifest, "reports/baseline/x", {"dixon_coles": {"n": 1}})
    assert row.model_run_id and row.window == "dev" and row.manifest["code_sha"] == "abc"
    assert row.created_at == datetime(2026, 11, 5, 16, 29, tzinfo=UTC)


def test_parametric_override_mode_is_constrained() -> None:
    s = session()
    s.add(
        models.OverrideParametric(
            match_id=1, entity_type="team", entity_id=1, attribute="attack", mode="replace",
            value=0.1, applied_at=datetime.now(UTC), applied_by="test",
        )
    )  # fmt: skip
    with pytest.raises(IntegrityError):
        s.flush()
