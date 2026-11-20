"""lineup_projection (projected lineups: P3-b served for upcoming fixtures)

Append-only rows of P3-b's projected selection and minutes per player and run, so what the
dashboard showed can be scored later. Additive only.

Revision ID: 0016
Revises: 0015
"""
from alembic import op
import sqlalchemy as sa

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('lineup_projection',
    sa.Column('lineup_projection_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('match_id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('player_id', sa.Integer(), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('refit_key', sa.String(length=255), nullable=False),
    sa.Column('refit_as_of', sa.DateTime(timezone=True), nullable=False),
    sa.Column('pos', sa.String(length=4), nullable=False),
    sa.Column('p_start', sa.Float(), nullable=False),
    sa.Column('p_bench', sa.Float(), nullable=False),
    sa.Column('p_out', sa.Float(), nullable=False),
    sa.Column('emin_start', sa.Float(), nullable=False),
    sa.Column('emin_bench', sa.Float(), nullable=False),
    sa.Column('emin', sa.Float(), nullable=False),
    sa.CheckConstraint('p_start >= 0 AND p_start <= 1', name='ck_lineup_proj_start'),
    sa.ForeignKeyConstraint(['match_id'], ['match.match_id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['team.team_id'], ),
    sa.ForeignKeyConstraint(['player_id'], ['player.player_id'], ),
    sa.PrimaryKeyConstraint('lineup_projection_id'),
    )
    op.create_index('ix_lineup_projection_match', 'lineup_projection', ['match_id', 'generated_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_lineup_projection_match', table_name='lineup_projection')
    op.drop_table('lineup_projection')
