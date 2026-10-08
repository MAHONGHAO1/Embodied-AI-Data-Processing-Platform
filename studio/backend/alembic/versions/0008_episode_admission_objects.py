"""Episode admission facts describe storage with one object manifest."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_episode_admission_objects"
down_revision = "0007_dashboard_package_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "episode_admission_facts",
        sa.Column(
            "objects_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade():
    op.drop_column("episode_admission_facts", "objects_json")
