"""manager and manager_tenure (Phase 3, P3-c (b))

A coach is a top-level entity; a tenure is a run of consecutive team matches under one
coach, built from match_coach and exact to the match. Additive only.

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def _temporal() -> list[sa.Column]:
    return [
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('valid_from', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table('manager',
    sa.Column('manager_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('source_id', sa.String(length=64), nullable=False),
    sa.Column('source_ref', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=True),
    *_temporal(),
    sa.ForeignKeyConstraint(['source_id'], ['source.source_id'], ),
    sa.PrimaryKeyConstraint('manager_id'),
    sa.UniqueConstraint('source_id', 'source_ref'),
    )
    op.create_table('manager_tenure',
    sa.Column('tenure_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('manager_id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('source_id', sa.String(length=64), nullable=False),
    sa.Column('first_match_id', sa.Integer(), nullable=False),
    sa.Column('last_match_id', sa.Integer(), nullable=False),
    sa.Column('n_matches', sa.Integer(), nullable=False),
    *_temporal(),
    sa.ForeignKeyConstraint(['manager_id'], ['manager.manager_id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['team.team_id'], ),
    sa.ForeignKeyConstraint(['source_id'], ['source.source_id'], ),
    sa.ForeignKeyConstraint(['first_match_id'], ['match.match_id'], ),
    sa.ForeignKeyConstraint(['last_match_id'], ['match.match_id'], ),
    sa.PrimaryKeyConstraint('tenure_id'),
    )
    op.create_index('ix_tenure_team', 'manager_tenure', ['team_id', 'valid_from'], unique=False)
    op.create_index('ix_tenure_manager', 'manager_tenure', ['manager_id', 'valid_from'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_tenure_manager', table_name='manager_tenure')
    op.drop_index('ix_tenure_team', table_name='manager_tenure')
    op.drop_table('manager_tenure')
    op.drop_table('manager')
