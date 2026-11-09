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
    # Provider's stage label ("Regular Season - 12", "Round of 16", "Promotion Play-offs").
    round: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Venue as last listed by the provider (pre-match, published); changes are in
    # fixture_snapshot. NULL where the provider gives no venue name.
    venue_id: Mapped[int | None] = mapped_column(ForeignKey("venue.venue_id"), nullable=True)
    __table_args__ = (
        UniqueConstraint("source_id", "source_ref"),
        Index("ix_match_kickoff", "kickoff_time"),
    )


class Venue(Temporal, Base):
    """A ground, keyed by the provider's name and city (its venue id is missing on up to 46%
    of 2025/26 fixtures, docs/FEASIBILITY_REGISTER.md 2.8). Coordinates and elevation come
    from Open-Meteo geocoding of the city (static facts, spec §6.2). geocode_status:
    pending (not tried) | auto (one clear match) | ambiguous | unresolved (both need review)
    | reviewed (set from config/venue_overrides.yml). Only auto and reviewed coordinates are
    used for weather."""

    __tablename__ = "venue"
    venue_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # provider id
    name: Mapped[str] = mapped_column(String(128))
    city: Mapped[str] = mapped_column(String(128))
    country: Mapped[str] = mapped_column(String(64))  # the competition's country
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    elevation_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)  # IANA name
    geocode_status: Mapped[str] = mapped_column(String(16), default="pending")
    geocode_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    geocode_match: Mapped[str | None] = mapped_column(String(255), nullable=True)
    geocoded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("source_id", "name", "city"),
        CheckConstraint(
            "geocode_status IN ('pending', 'auto', 'ambiguous', 'unresolved', 'reviewed')",
            name="ck_venue_geocode_status",
        ),
    )


class TeamHomeVenue(Temporal, Base):
    """A spell at a team's home base (spec Team.home_venue_id, versioned: ground moves and
    shares, docs/FEASIBILITY_REGISTER.md 2.14). Derived from the venues of the team's home
    league fixtures as published by the provider: a run of at least three consecutive home
    fixtures at one venue starts a spell; shorter runs elsewhere are temporary venues and do
    not move the base. valid_from is the spell's first home fixture, valid_to the next
    spell's. A rebuild that changes a team's spells supersedes its old rows (superseded_at)
    rather than deleting them."""

    __tablename__ = "team_home_venue"
    spell_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.team_id"))
    venue_id: Mapped[int] = mapped_column(ForeignKey("venue.venue_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    n_home_matches: Mapped[int] = mapped_column(Integer)  # league home fixtures in the spell
    __table_args__ = (Index("ix_team_home_venue_team", "team_id", "valid_from"),)


class WeatherSnapshot(Base):
    """An Open-Meteo forecast for one match's venue and kickoff, as issued when captured
    (spec §6.2, §12.2). Pre-match only: a row is usable as of observed_at, and lead_hours
    says how far ahead it was. Summary columns cover the match (kickoff to kickoff + 2 h);
    `hourly` keeps kickoff - 1 h to + 3 h; the full response is in the raw store."""

    __tablename__ = "weather_snapshot"
    snapshot_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    venue_id: Mapped[int] = mapped_column(ForeignKey("venue.venue_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    kickoff_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # as known then
    lead_hours: Mapped[float] = mapped_column(Float)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    precipitation_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    hourly: Mapped[dict[str, Any]] = mapped_column(JsonType)
    storage_key: Mapped[str] = mapped_column(String(255))
    __table_args__ = (Index("ix_weather_snapshot_match", "match_id", "observed_at"),)


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


class OddsCapture(Base):
    """One row per (match, milestone kind): was the market captured, missed, or not offered?

    Evaluation-only (spec §15). The full provider payload is in the raw store under
    storage_key; odds_snapshot holds the normalised subset. Never joined into feature builds.
    """

    __tablename__ = "odds_capture"
    capture_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    snapshot_kind: Mapped[str] = mapped_column(String(16))  # see capture.snapshots.KINDS
    # captured / missed (our poller failed) / no_market (provider offered no odds) /
    # not_published (lineup never published, so the lineup-kind capture could not happen)
    status: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    n_bookmakers: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("match_id", "snapshot_kind"),)


class OddsSnapshot(Base):
    """Evaluation-only. Never a model feature. Normalised prices for one capture."""

    __tablename__ = "odds_snapshot"
    snapshot_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    capture_id: Mapped[int | None] = mapped_column(
        ForeignKey("odds_capture.capture_id"), nullable=True
    )
    bookmaker: Mapped[str] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(32))  # 1X2, OU, BTTS
    line: Mapped[float | None] = mapped_column(Float, nullable=True)  # 2.5 for OU
    selection: Mapped[str] = mapped_column(String(32))
    decimal_odds: Mapped[float] = mapped_column(Float)
    snapshot_kind: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    __table_args__ = (Index("ix_odds_match_kind", "match_id", "snapshot_kind"),)


class FixtureSnapshot(Base):
    """Fixture-calendar snapshots: appended only when kickoff/status/referee/venue changes."""

    __tablename__ = "fixture_snapshot"
    snapshot_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    kickoff_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # event_time
    status: Mapped[str] = mapped_column(String(16))
    referee: Mapped[str | None] = mapped_column(String(128), nullable=True)
    venue_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    round: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (Index("ix_fixture_snapshot_match", "match_id", "observed_at"),)


class PlayerStatusSnapshot(Temporal, Base):
    """Spec §7.1: per-player availability as of a date. Open while valid_to is NULL; a change
    closes the open row and opens a new one. expected_return stays NULL for API-Football
    (verified unavailable, docs/SOURCE_FEASIBILITY.md)."""

    __tablename__ = "player_status_snapshot"
    status_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("player.player_id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("team.team_id"))
    match_id: Mapped[int | None] = mapped_column(ForeignKey("match.match_id"), nullable=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32))  # missing / questionable
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expected_return: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    competition_eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (Index("ix_player_status_open", "player_id", "match_id", "valid_to"),)


class LineupSnapshot(Base):
    """Confirmed lineup as first seen before kickoff (observed_at gives its pre-match timing)."""

    __tablename__ = "lineup_snapshot"
    lineup_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("team.team_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    formation: Mapped[str | None] = mapped_column(String(16), nullable=True)
    coach_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    coach_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    starters: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)
    substitutes: Mapped[list[dict[str, Any]]] = mapped_column(JsonType)
    __table_args__ = (UniqueConstraint("match_id", "team_id"),)


class CaptureLog(Base):
    """One row per capture job run/scope: proves a feed was polled (so absence = no change),
    records quota use per job (spec §4.2 rule 4), and carries missed-capture events."""

    __tablename__ = "capture_log"
    log_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(32))  # calendar / status / odds / lineups
    scope: Mapped[str] = mapped_column(String(64))  # league id or match id
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(16))  # ok / missed / error
    requests: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(String(500), default="")
    __table_args__ = (Index("ix_capture_log_job", "job", "scope", "run_at"),)


class BackfillJob(Base):
    """Resumable, idempotent backfill queue: one row per (source, competition ref, season)."""

    __tablename__ = "backfill_job"
    job_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    source_ref: Mapped[str] = mapped_column(String(64))
    season_year: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=5)
    # pending -> fixtures (calendar + results ingested) -> done (detail stored); or failed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    requests: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    n_fixtures: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    __table_args__ = (UniqueConstraint("source_id", "source_ref", "season_year"),)


class MatchDetail(Base):
    """Which post-match detail (lineups, events, statistics, player stats) is stored for a
    match. The payload itself stays in the raw store (principle 4); this row says where, and
    which capabilities it carries, so detail can be normalised later without re-fetching."""

    __tablename__ = "match_detail"
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    storage_key: Mapped[str] = mapped_column(String(255))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    flags: Mapped[dict[str, Any]] = mapped_column(JsonType)  # capability -> present


# --------------------------------------------------------------------------- spec §3.5 Phase 0
# Built now because retrofitting them is a redo (§19.2 step 2), even where nothing writes
# them yet: bundles, registrations, overrides, and the run / projection manifest tables.


class CompetitionBundle(Base):
    """A group of competitions onboarded and maintained together (§3.1, §19.1), e.g. a top
    flight and its feeder, fitted jointly so promoted teams carry their strength across."""

    __tablename__ = "competition_bundle"
    bundle_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)


class CompetitionBundleMember(Base):
    __tablename__ = "competition_bundle_member"
    bundle_id: Mapped[int] = mapped_column(
        ForeignKey("competition_bundle.bundle_id"), primary_key=True
    )
    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competition.competition_id"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer)  # 0 = reference league (top flight)


class PlayerRegistration(Temporal, Base):
    """Which team (and so which level) a player is registered with, over time (§3.2).
    A player can hold several registrations in one season at different levels."""

    __tablename__ = "player_registration"
    registration_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("player.player_id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("team.team_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    __table_args__ = (Index("ix_registration_player", "player_id", "valid_from"),)


class OverrideParametric(Base):
    """Parametric scale factor (§8.5): an extra log-space term applied at simulation time
    only. Stored inference and source data are never modified; revoking sets revoked_at."""

    __tablename__ = "override_parametric"
    override_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    entity_type: Mapped[str] = mapped_column(String(16))  # team / player
    entity_id: Mapped[int] = mapped_column(Integer)
    attribute: Mapped[str] = mapped_column(String(64))  # e.g. attack, defence, minutes
    mode: Mapped[str] = mapped_column(String(16))  # additive / multiplicative
    value: Mapped[float] = mapped_column(Float)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied_by: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(String(500), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("entity_type IN ('team', 'player')", name="ck_override_param_entity"),
        CheckConstraint("mode IN ('additive', 'multiplicative')", name="ck_override_param_mode"),
        Index("ix_override_param_match", "match_id"),
    )


class OverrideStructural(Base):
    """Structural override (§8.5): replaces a scraped or inferred field for an upcoming match
    (availability, formation, lineup slot, referee, weather, venue, competition_eligible,
    call-up status). Read at simulation time with precedence explicit override > inferred
    value; the underlying data is never mutated."""

    __tablename__ = "override_structural"
    override_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    entity_type: Mapped[str] = mapped_column(String(32))  # player / team / referee / venue / ...
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field: Mapped[str] = mapped_column(String(64))
    value: Mapped[Any] = mapped_column(JsonType)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied_by: Mapped[str] = mapped_column(String(64))
    why: Mapped[str] = mapped_column(String(500), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("ix_override_struct_match", "match_id"),)


class ModelRun(Base):
    """One fit (or backtest) of one model component, with its reproducibility manifest
    (§14.8). Experiment tracking starts as this Postgres table."""

    __tablename__ = "model_run"
    model_run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(64))  # e.g. dixon_coles_static
    component: Mapped[str] = mapped_column(String(32))  # phase1a / layer2 / layer5 ...
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window: Mapped[str] = mapped_column(String(16), default="")  # dev / holdout / live / fit
    git_commit: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    lockfile_hash: Mapped[str] = mapped_column(String(64))
    data_snapshot_hash: Mapped[str] = mapped_column(String(64))
    seed: Mapped[int] = mapped_column(Integer)
    trace_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    report_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    backtest_scores: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    manifest: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'ok', 'failed', 'challenger', 'champion', 'rejected', "
            "'retired')",
            name="ck_model_run_status",
        ),
    )


class ProjectionRun(Base):
    """One batch of published projections and everything needed to explain them later:
    "why did this projection say 64%" must be answerable months on (§14.8)."""

    __tablename__ = "projection_run"
    projection_run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    git_commit: Mapped[str] = mapped_column(String(64))
    lockfile_hash: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    component_versions: Mapped[dict[str, Any]] = mapped_column(JsonType)  # layer -> model_run
    q_league_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    training_snapshot_hash: Mapped[str] = mapped_column(String(64))
    source_snapshot_ids: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    override_set_hash: Mapped[str] = mapped_column(String(64))
    seed: Mapped[int] = mapped_column(Integer)


class MarketProjection(Base):
    """A model probability for one match x market x selection at one milestone (§3.1, §15).
    Odds live in odds_snapshot and are compared downstream; they never feed this table."""

    __tablename__ = "market_projection"
    projection_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    projection_run_id: Mapped[int] = mapped_column(ForeignKey("projection_run.projection_run_id"))
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    milestone: Mapped[str] = mapped_column(String(16))  # initial / T-7d / ... / close
    market_type: Mapped[str] = mapped_column(String(16))  # 1X2 / OU / BTTS ...
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    selection: Mapped[str] = mapped_column(String(32))
    probability: Mapped[float] = mapped_column(Float)
    interval_lo: Mapped[float | None] = mapped_column(Float, nullable=True)
    interval_hi: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (
        CheckConstraint("probability >= 0 AND probability <= 1", name="ck_projection_prob"),
        Index("ix_projection_match", "match_id", "milestone"),
    )


class MatchCoach(Temporal, Base):
    """The coach named in a team's lineup for one match (Phase 1b, D12), parsed from the
    stored detail payload. Known from the lineup release, about an hour before kickoff; the
    model reads it only for training matches (kickoff + result lag). A re-fetch naming a
    different coach supersedes the row instead of overwriting it."""

    __tablename__ = "match_coach"
    coach_row_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("team.team_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    storage_key: Mapped[str] = mapped_column(String(255))
    coach_ref: Mapped[str] = mapped_column(String(64))
    coach_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (Index("ix_match_coach", "match_id", "team_id", "superseded_at"),)


class TeamMatchStat(Temporal, Base):
    """Post-match team statistics for one match x team (Phase 1b, D8), parsed from the stored
    detail payload. Usable for a prediction as of kickoff + result lag, like the result.
    A re-fetch with different values supersedes the row instead of overwriting it.

    Counts the provider sends as null are stored as 0 when the team's statistics block is
    present (the provider omits zero counts); xG null stays null (absent, never imputed)."""

    __tablename__ = "team_match_stat"
    stat_id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("match.match_id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("team.team_id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source.source_id"))
    storage_key: Mapped[str] = mapped_column(String(255))
    xg: Mapped[float | None] = mapped_column(Float, nullable=True)
    shots_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shots_on_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shots_inside_box: Mapped[int | None] = mapped_column(Integer, nullable=True)
    corners: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fouls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yellow_cards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    red_cards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    possession: Mapped[float | None] = mapped_column(Float, nullable=True)  # percent
    stats: Mapped[dict[str, Any]] = mapped_column(JsonType)  # every provider type -> value
    __table_args__ = (Index("ix_team_match_stat", "match_id", "team_id", "superseded_at"),)
