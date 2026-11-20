"""formation_projection (P3-h: P3-c served, and the last lineup in that formation)

Append-only rows of P3-c's formation distribution per team of an upcoming fixture and the
pitch shown with it (the team's last lineup in that formation as of the projection), so
what the dashboard showed can be scored later. Additive only.

Revision ID: 0018
Revises: 0017
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    op.create_table('formation_projection',
    sa.Column('formation_projection_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('match_id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('refit_key', sa.String(length=255), nullable=False),
    sa.Column('refit_as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('probs', JSON, nullable=False),
    sa.Column('pitch_formation', sa.String(length=16), nullable=False),
    sa.Column('last_match_id', sa.Integer(), nullable=True),
    sa.Column('last_kickoff', sa.DateTime(timezone=True), nullable=True),
    sa.Column('positional', sa.Boolean(), nullable=False),
    sa.Column('slots', JSON, nullable=True),
    sa.ForeignKeyConstraint(['match_id'], ['match.match_id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['team.team_id'], ),
    sa.PrimaryKeyConstraint('formation_projection_id'),
    )
    op.create_index('ix_formation_projection_match', 'formation_projection', ['match_id', 'generated_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_formation_projection_match', table_name='formation_projection')
    op.drop_table('formation_projection')
