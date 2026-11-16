"""player_registration: shirt number and position from the squad list (Phase 3, P3-a)

The daily `/players/squads` capture versions a player's registration with a team: a spell
opens when the player is first listed and closes when they are no longer listed. A changed
number or position supersedes the spell. Additive only.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('player_registration') as batch:
        batch.add_column(sa.Column('shirt_number', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('position', sa.String(length=16), nullable=True))
        batch.create_index('ix_registration_team_open', ['team_id', 'valid_to'])


def downgrade() -> None:
    with op.batch_alter_table('player_registration') as batch:
        batch.drop_index('ix_registration_team_open')
        batch.drop_column('position')
        batch.drop_column('shirt_number')
