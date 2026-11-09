"""venue and weather snapshots (Phase 2 preparation, docs/FEASIBILITY_REGISTER.md)

venue: grounds keyed by provider name and city, with geocoded coordinates and elevation.
match.venue_id: the listed venue. weather_snapshot: Open-Meteo forecasts captured before
kickoff (as-issued forecast history does not exist before about Feb 2024).

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('venue',
    sa.Column('venue_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('source_id', sa.String(length=64), nullable=False),
    sa.Column('source_ref', sa.String(length=64), nullable=True),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('city', sa.String(length=128), nullable=False),
    sa.Column('country', sa.String(length=64), nullable=False),
    sa.Column('latitude', sa.Float(), nullable=True),
    sa.Column('longitude', sa.Float(), nullable=True),
    sa.Column('elevation_m', sa.Float(), nullable=True),
    sa.Column('geocode_status', sa.String(length=16), nullable=False),
    sa.Column('geocode_source', sa.String(length=64), nullable=True),
    sa.Column('geocode_match', sa.String(length=255), nullable=True),
    sa.Column('geocoded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("geocode_status IN ('pending', 'auto', 'ambiguous', 'unresolved', 'reviewed')", name='ck_venue_geocode_status'),
    sa.ForeignKeyConstraint(['source_id'], ['source.source_id'], ),
    sa.PrimaryKeyConstraint('venue_id'),
    sa.UniqueConstraint('source_id', 'name', 'city')
    )
    with op.batch_alter_table('match') as batch:
        batch.add_column(sa.Column('venue_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_match_venue', 'venue', ['venue_id'], ['venue_id'])
    op.create_table('weather_snapshot',
    sa.Column('snapshot_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('match_id', sa.Integer(), nullable=False),
    sa.Column('venue_id', sa.Integer(), nullable=False),
    sa.Column('source_id', sa.String(length=64), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('kickoff_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('lead_hours', sa.Float(), nullable=False),
    sa.Column('latitude', sa.Float(), nullable=False),
    sa.Column('longitude', sa.Float(), nullable=False),
    sa.Column('temperature_c', sa.Float(), nullable=True),
    sa.Column('precipitation_mm', sa.Float(), nullable=True),
    sa.Column('wind_speed_kmh', sa.Float(), nullable=True),
    sa.Column('hourly', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('storage_key', sa.String(length=255), nullable=False),
    sa.ForeignKeyConstraint(['match_id'], ['match.match_id'], ),
    sa.ForeignKeyConstraint(['source_id'], ['source.source_id'], ),
    sa.ForeignKeyConstraint(['venue_id'], ['venue.venue_id'], ),
    sa.PrimaryKeyConstraint('snapshot_id')
    )
    op.create_index('ix_weather_snapshot_match', 'weather_snapshot', ['match_id', 'observed_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_weather_snapshot_match', table_name='weather_snapshot')
    op.drop_table('weather_snapshot')
    with op.batch_alter_table('match') as batch:
        batch.drop_constraint('fk_match_venue', type_='foreignkey')
        batch.drop_column('venue_id')
    op.drop_table('venue')
