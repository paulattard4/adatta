"""spec §3.4 capability table; normal-time / extra-time / shootout scores (spec §4.4)

Replaces the Phase 0 `capability` table with `competition_source_capability` (status,
valid_from/valid_to, last_audited_at, season FK). The old table is dropped only if empty:
nothing has written to it before this revision, so a non-empty table means something
unexpected happened and the migration refuses rather than discard rows.

match_result: home/away_goals_ft -> home/away_goals_nt (90' score), plus ET-only goals and
shootout counts. Rows ingested before this revision used the provider's total `goals`, which
includes ET for AET/PEN matches; re-ingesting those fixtures supersedes them with a corrected
version (the ingest compares the full score tuple).

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    n_old = bind.execute(sa.text("SELECT COUNT(*) FROM capability")).scalar_one()
    if n_old:
        raise RuntimeError(
            f"capability table has {n_old} rows; refusing to drop. Migrate them by hand first."
        )
    op.drop_table("capability")

    op.create_table(
        "competition_source_capability",
        sa.Column("capability_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("competition_id", sa.Integer(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("coverage_ratio", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_audited_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('available', 'partial', 'absent', 'unknown')", name="ck_capability_status"
        ),
        sa.ForeignKeyConstraint(["competition_id"], ["competition.competition_id"]),
        sa.ForeignKeyConstraint(["season_id"], ["season.season_id"]),
        sa.ForeignKeyConstraint(["source_id"], ["source.source_id"]),
        sa.PrimaryKeyConstraint("capability_id"),
        sa.UniqueConstraint(
            "competition_id", "season_id", "source_id", "capability", "valid_from"
        ),
    )
    op.create_index(
        "ix_capability_lookup",
        "competition_source_capability",
        ["competition_id", "season_id", "capability", "valid_to"],
    )

    with op.batch_alter_table("match_result") as batch:
        batch.alter_column("home_goals_ft", new_column_name="home_goals_nt")
        batch.alter_column("away_goals_ft", new_column_name="away_goals_nt")
        batch.add_column(sa.Column("home_goals_et", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("away_goals_et", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("home_pens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("away_pens", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("match_result") as batch:
        batch.drop_column("away_pens")
        batch.drop_column("home_pens")
        batch.drop_column("away_goals_et")
        batch.drop_column("home_goals_et")
        batch.alter_column("away_goals_nt", new_column_name="away_goals_ft")
        batch.alter_column("home_goals_nt", new_column_name="home_goals_ft")

    op.drop_index("ix_capability_lookup", table_name="competition_source_capability")
    op.drop_table("competition_source_capability")
    op.create_table(
        "capability",
        sa.Column("capability_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("competition_id", sa.Integer(), nullable=False),
        sa.Column("season_year", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("coverage_ratio", sa.Float(), nullable=False),
        sa.Column(
            "audited_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["competition_id"], ["competition.competition_id"]),
        sa.ForeignKeyConstraint(["source_id"], ["source.source_id"]),
        sa.PrimaryKeyConstraint("capability_id"),
        sa.UniqueConstraint("competition_id", "season_year", "source_id", "capability"),
    )
