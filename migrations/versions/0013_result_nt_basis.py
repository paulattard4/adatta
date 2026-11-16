"""match_result.nt_basis: where the 90' score came from

`provider` (the fixture's score fields, split by normalize.split_score) or `events` (the goal
events of the stored detail, docs/BEYOND_LEAGUES_PLAN.md Stage A, A0). A fixture re-ingest
keeps an `events` row while the provider's totals still agree with it. Existing rows are
`provider`; the server default keeps the poller running older code working. Additive only.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('match_result') as batch:
        batch.add_column(sa.Column('nt_basis', sa.String(length=16), nullable=False,
                                   server_default='provider'))


def downgrade() -> None:
    with op.batch_alter_table('match_result') as batch:
        batch.drop_column('nt_basis')
