"""Admission facts keep only the episode object manifest."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_drop_admission_process_ref"
down_revision = "0008_episode_admission_objects"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("episode_admission_facts", "process_ref_json")
    op.drop_column("episode_admission_facts", "source_objects_json")


def downgrade():
    op.add_column(
        "episode_admission_facts",
        sa.Column(
            "source_objects_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "episode_admission_facts",
        sa.Column(
            "process_ref_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
