"""venue.provider_country: the venue's own country from the provider's /venues

For the national-team host flag (D35 Stage 0, docs/BEYOND_LEAGUES_PLAN.md): venue.country is
the competition's country, which is wrong for a ground hosting another country's league
(Cardiff City Stadium: Championship, England; Wales plays there at home). Additive only, so
the poller running older code is unaffected.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('venue') as batch:
        batch.add_column(sa.Column('provider_country', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('venue') as batch:
        batch.drop_column('provider_country')
