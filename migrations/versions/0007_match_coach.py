"""match coach (Phase 1b, D12)

match_coach: the coach in each team's lineup per match, parsed from stored detail payloads,
for manager-change widening.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('match_coach',
    sa.Column('coach_row_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('match_id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('source_id', sa.String(length=64), nullable=False),
    sa.Column('storage_key', sa.String(length=255), nullable=False),
    sa.Column('coach_ref', sa.String(length=64), nullable=False),
    sa.Column('coach_name', sa.String(length=128), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['match_id'], ['match.match_id'], ),
    sa.ForeignKeyConstraint(['source_id'], ['source.source_id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['team.team_id'], ),
    sa.PrimaryKeyConstraint('coach_row_id')
    )
    op.create_index('ix_match_coach', 'match_coach', ['match_id', 'team_id', 'superseded_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_match_coach', table_name='match_coach')
    op.drop_table('match_coach')
