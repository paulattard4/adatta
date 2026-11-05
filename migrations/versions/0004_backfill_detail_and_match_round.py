"""backfill detail and match round (Phase 0 task 6)

match_detail: one row per match whose post-match detail payload (lineups, events,
statistics, player stats) is stored in the raw store, with the capabilities it carries.
match.round: the provider's stage label, needed to separate regular season from play-offs
and cup stages. backfill_job gains requests, n_fixtures and error for resumable reporting.
All additions are nullable or defaulted; existing rows are untouched.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('match_detail',
    sa.Column('match_id', sa.Integer(), nullable=False),
    sa.Column('source_id', sa.String(length=64), nullable=False),
    sa.Column('storage_key', sa.String(length=255), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('flags', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['match_id'], ['match.match_id'], ),
    sa.ForeignKeyConstraint(['source_id'], ['source.source_id'], ),
    sa.PrimaryKeyConstraint('match_id')
    )
    with op.batch_alter_table('backfill_job') as batch:
        batch.add_column(sa.Column('requests', sa.Integer(), server_default='0', nullable=False))
        batch.add_column(sa.Column('n_fixtures', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('error', sa.String(length=500), nullable=True))
    with op.batch_alter_table('match') as batch:
        batch.add_column(sa.Column('round', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('match') as batch:
        batch.drop_column('round')
    with op.batch_alter_table('backfill_job') as batch:
        batch.drop_column('error')
        batch.drop_column('n_fixtures')
        batch.drop_column('requests')
    op.drop_table('match_detail')
