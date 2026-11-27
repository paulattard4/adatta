"""formation_projection.slots_replaced, replacements (P3-h2, D64: shadow)

The projected pitch with players the injury feed lists as out replaced from the same slot
code (else the same line), stored beside P3-h's pitch and not shown until its live verdict.
Additive only.

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    op.add_column('formation_projection', sa.Column('slots_replaced', JSON, nullable=True))
    op.add_column('formation_projection', sa.Column('replacements', sa.Integer(), nullable=True))


def downgrade() -> None:  # forward-only in practice (CLAUDE.md); kept for symmetry
    op.drop_column('formation_projection', 'replacements')
    op.drop_column('formation_projection', 'slots_replaced')
