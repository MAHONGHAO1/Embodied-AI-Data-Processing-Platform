"""Admission facts record who proved source integrity (client or server)."""

import sqlalchemy as sa
from alembic import op

revision = "0010_admission_integrity_source"
down_revision = "0009_drop_admission_process_ref"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "episode_admission_facts",
        sa.Column(
            "integrity_source",
            sa.String(length=16),
            nullable=False,
            server_default="server",
        ),
    )
    op.create_check_constraint(
        "ck_episode_admission_facts_integrity_source",
        "episode_admission_facts",
        "integrity_source IN ('client', 'server')",
    )


def downgrade():
    op.drop_constraint(
        "ck_episode_admission_facts_integrity_source",
        "episode_admission_facts",
        type_="check",
    )
    op.drop_column("episode_admission_facts", "integrity_source")
