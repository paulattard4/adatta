"""team.gender: men or women, apart from team_level (levels only)

A national side's gender and level come from its name (docs/BEYOND_LEAGUES_PLAN.md, gender as
its own field). Existing rows are men's; the server default keeps the poller running older
code working. Additive only.

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('team') as batch:
        batch.add_column(sa.Column('gender', sa.String(length=16), nullable=False,
                                   server_default='men'))


def downgrade() -> None:
    with op.batch_alter_table('team') as batch:
        batch.drop_column('gender')
