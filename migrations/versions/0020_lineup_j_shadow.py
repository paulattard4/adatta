"""lineup_j_shadow (P3-j step 3, D65: J's live shadow beside P, comparator (a))

Append-only rows of J's forecast and P's from the passing refit of the same as-of time, at
lineups known and at T-24h, with the lineup signals used and the posterior-mean score
matrix, so the season-end verdict can score them once. Not shown. Additive only.

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    op.create_table('lineup_j_shadow',
    sa.Column('lineup_j_shadow_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('match_id', sa.Integer(), nullable=False),
    sa.Column('horizon', sa.String(length=4), nullable=False),
    sa.Column('model', sa.String(length=8), nullable=False),
    sa.Column('model_run_id', sa.Integer(), nullable=False),
    sa.Column('refit_as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('lineup_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('signals', JSON, nullable=False),
    sa.Column('matrix', JSON, nullable=False),
    sa.Column('p_home', sa.Float(), nullable=False),
    sa.Column('p_draw', sa.Float(), nullable=False),
    sa.Column('p_away', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['match_id'], ['match.match_id'], ),
    sa.PrimaryKeyConstraint('lineup_j_shadow_id'),
    sa.UniqueConstraint('match_id', 'horizon', 'model'),
    )


def downgrade() -> None:  # forward-only in practice (CLAUDE.md); kept for symmetry
    op.drop_table('lineup_j_shadow')
