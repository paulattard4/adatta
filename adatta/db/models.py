"""Minimal schema (spec Phase 0). Every fact row carries source_id and the temporal contract:
event_time, published_at, observed_at, ingested_at, valid_from/valid_to, superseded_at.
Odds live in their own table and are NEVER joined into feature builds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

BigIntPk = BigInteger().with_variant(Integer, "sqlite")
JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _now() -> Any:
    return func.now()


class Temporal:
    """Mixin: temporal data contract columns."""

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Source(Base):
    __tablename__ = "source"
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "api_football"
    description: Mapped[str] = mapped_column(String(255), default="")


class Competition(Base):
    __tablename__ = "competition"
    competition_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    source_ref: Mapped[str] = mapped_column(String(64))  # provider's league id
    name: Mapped[str] = mapped_column(String(128))
    country: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(32), default="senior")  # senior/u21/u19/reserve...
    is_national: Mapped[bool] = mapped_column(Boolean, default=False)
    tier: Mapped[str] = mapped_column(String(16), default="")
    sport: Mapped[str] = mapped_column(String(32), default="football")
    __table_args__ = (UniqueConstraint("source_id", "source_ref"),)


class Season(Base):
    __tablename__ = "season"
    season_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    competition_id: Mapped[int] = mapped_column(ForeignKey("competition.competition_id"))
    year: Mapped[int] = mapped_column(Integer)  # season start year
    __table_args__ = (UniqueConstraint("competition_id", "year"),)


class Team(Base):
    __tablename__ = "team"
    team_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    source_ref: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    country: Mapped[str] = mapped_column(String(64), default="")
    team_level: Mapped[str] = mapped_column(String(32), default="senior")
    is_national: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_team_id: Mapped[int | None] = mapped_column(ForeignKey("team.team_id"), nullable=True)
    __table_args__ = (UniqueConstraint("source_id", "source_ref"),)


class Player(Base):
    __tablename__ = "player"
    player_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    source_ref: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    birth_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("source_id", "source_ref"),)


class Match(Base):
    __tablename__ = "match"
    match_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    source_ref: Mapped[str] = mapped_column(String(64))
    season_id: Mapped[int] = mapped_column(ForeignKey("season.season_id"))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("team.team_id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("team.team_id"))
    kickoff_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # event_time
    neutral: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    __table_args__ = (
        UniqueConstraint("source_id", "source_ref"),
        Index("ix_match_kickoff", "kickoff_time"),
    )


class MatchResult(Temporal, Base):
    """Versioned result facts: corrections supersede earlier rows rather than overwrite."""

    __tablename__ = "match_result"
    result_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Spec §4.4: normal time, extra time and shootout are separate facts. Layer 2 trains on
    # normal-time goals only; ET goals (ET-only, not cumulative) feed the ET sub-model.
    home_goals_ht: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_goals_ht: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_goals_nt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_goals_nt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_goals_et: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_goals_et: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_pens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_pens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (Index("ix_match_result_match", "match_id", "superseded_at"),)


class RawPayload(Base):
    """Index of raw provider payloads stored in the object store."""

    __tablename__ = "raw_payload"
    payload_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    endpoint: Mapped[str] = mapped_column(String(128))
    params: Mapped[dict[str, Any]] = mapped_column(JsonType)
    storage_key: Mapped[str] = mapped_column(String(512))
    content_hash: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    __table_args__ = (UniqueConstraint("storage_key"),)


CAPABILITY_STATUSES = ("available", "partial", "absent", "unknown")


class CompetitionSourceCapability(Base):
    """Temporal capability facts (spec §3.4): what a source provides per competition-season.

    A row is open while valid_to is NULL. A re-audit that confirms the same status only bumps
    last_audited_at; a changed status closes the open row and opens a new one (never overwrite).
    coverage_ratio is NULL when status is 'unknown' (nothing observable to measure).
    """

    __tablename__ = "competition_source_capability"
    capability_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    competition_id: Mapped[int] = mapped_column(ForeignKey("competition.competition_id"))
    season_id: Mapped[int] = mapped_column(ForeignKey("season.season_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    capability: Mapped[str] = mapped_column(String(64))  # has_lineups, has_xg, ...
    coverage_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # one of CAPABILITY_STATUSES
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_audited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("competition_id", "season_id", "source_id", "capability", "valid_from"),
        CheckConstraint(
            "status IN ('available', 'partial', 'absent', 'unknown')", name="ck_capability_status"
        ),
        Index("ix_capability_lookup", "competition_id", "season_id", "capability", "valid_to"),
    )


class OddsSnapshot(Base):
    """Evaluation-only. Never a model feature."""

    __tablename__ = "odds_snapshot"
    snapshot_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    bookmaker: Mapped[str] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(32))  # 1X2, OU2.5, ...
    selection: Mapped[str] = mapped_column(String(32))
    decimal_odds: Mapped[float] = mapped_column(Float)
    snapshot_kind: Mapped[str] = mapped_column(String(16))  # initial/T-7d/T-24h/T-60m/close
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    __table_args__ = (Index("ix_odds_match_kind", "match_id", "snapshot_kind"),)


class BackfillJob(Base):
    """Resumable, idempotent backfill queue: one row per (source, competition ref, season)."""

    __tablename__ = "backfill_job"
    job_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    source_ref: Mapped[str] = mapped_column(String(64))
    season_year: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=5)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/done/failed
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    __table_args__ = (UniqueConstraint("source_id", "source_ref", "season_year"),)
