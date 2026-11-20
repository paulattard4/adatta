"""lineup_projection: P3-f shadow columns (status-informed availability)

The injury feed's status as of the projection, the live shares rho, and P3-f's
probabilities, stored beside P3-b's so P3-f can be judged live. Nullable, additive only.

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None

COLUMNS = (
    ('status', sa.String(length=16)),
    ('rho_missing', sa.Float()),
    ('rho_questionable', sa.Float()),
    ('p_start_f', sa.Float()),
    ('p_bench_f', sa.Float()),
    ('p_out_f', sa.Float()),
)


def upgrade() -> None:
    for name, kind in COLUMNS:
        op.add_column('lineup_projection', sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column('lineup_projection', name)
