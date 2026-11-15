"""venue.geo_country: the country of a venue's city from an Open-Meteo place search

Fallback for the national-team host flag where the provider's /venues listings do not place
a venue (docs/BEYOND_LEAGUES_PLAN.md, geocoding fallback). Kept apart from provider_country,
its own source. Additive only, so the poller running older code is unaffected.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('venue') as batch:
        batch.add_column(sa.Column('geo_country', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('venue') as batch:
        batch.drop_column('geo_country')
