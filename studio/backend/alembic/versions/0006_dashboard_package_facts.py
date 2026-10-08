"""Allow warehouse facts for package Episodes without legacy IDs."""

import sqlalchemy as sa

from alembic import op

revision = "0006_dashboard_package_facts"
down_revision = "0005_annotation_submissions"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "dwd_episode_fact",
        "task_set_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        nullable=True,
    )
    op.alter_column(
        "dwd_episode_fact",
        "batch_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        nullable=True,
    )


def downgrade():
    op.alter_column(
        "dwd_episode_fact",
        "batch_id",
        existing_type=sa.Integer(),
        existing_nullable=True,
        nullable=False,
    )
    op.alter_column(
        "dwd_episode_fact",
        "task_set_id",
        existing_type=sa.Integer(),
        existing_nullable=True,
        nullable=False,
    )
