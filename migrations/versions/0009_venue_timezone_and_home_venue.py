"""venue timezone and team home-venue spells (travel burden, altitude differential)

venue.timezone: IANA zone from the geocoder (the Canary Islands are an hour behind mainland
Spain). team_home_venue: versioned home base per team (docs/FEASIBILITY_REGISTER.md 2.14).
Additive only, so the poller running older code is unaffected.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('venue') as batch:
        batch.add_column(sa.Column('timezone', sa.String(length=64), nullable=True))
    op.create_table('team_home_venue',
    sa.Column('spell_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('venue_id', sa.Integer(), nullable=False),
    sa.Column('source_id', sa.String(length=64), nullable=False),
    sa.Column('n_home_matches', sa.Integer(), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['source_id'], ['source.source_id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['team.team_id'], ),
    sa.ForeignKeyConstraint(['venue_id'], ['venue.venue_id'], ),
    sa.PrimaryKeyConstraint('spell_id')
    )
    op.create_index('ix_team_home_venue_team', 'team_home_venue', ['team_id', 'valid_from'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_team_home_venue_team', table_name='team_home_venue')
    op.drop_table('team_home_venue')
    with op.batch_alter_table('venue') as batch:
        batch.drop_column('timezone')
