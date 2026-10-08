"""Persist package lifecycle and distinct duration bases in dashboard facts."""

import sqlalchemy as sa
from alembic import op

revision = "0007_dashboard_package_lifecycle"
down_revision = "0006_dashboard_package_facts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "dwd_episode_fact",
        sa.Column("annotation_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("dwd_episode_fact", sa.Column("queue_key", sa.String(32), nullable=True))
    op.add_column(
        "dwd_episode_fact", sa.Column("intake_valid_duration_s", sa.Float(), nullable=True)
    )
    op.add_column(
        "dwd_episode_fact",
        sa.Column("annotation_effective_duration_ns", sa.BigInteger(), nullable=True),
    )
    # Version 1 rows are reprojected by the next incremental ETL, even if their
    # source Episode and package have not changed since the previous build.
    op.add_column(
        "dwd_episode_fact",
        sa.Column("projection_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("dwd_episode_fact", sa.Column("source_revision", sa.String(32), nullable=True))


def downgrade():
    for name in (
        "source_revision",
        "projection_version",
        "annotation_effective_duration_ns",
        "intake_valid_duration_s",
        "queue_key",
        "annotation_eligible",
    ):
        op.drop_column("dwd_episode_fact", name)
