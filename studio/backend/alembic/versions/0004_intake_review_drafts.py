"""Persist intake viewing progress and structured final review reasons."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0004_intake_review_drafts"
down_revision = "0003_retire_legacy_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "package_intake_review_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("data_package_id", sa.Integer(), sa.ForeignKey("data_packages.id"), nullable=False),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("reviewer_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("draft_json", JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("data_package_id", "reviewer_user_id", name="uq_intake_draft_reviewer"),
        sa.CheckConstraint("draft_version >= 1", name="ck_intake_draft_version"),
    )
    op.add_column("package_intake_reviews", sa.Column("episode_reasons_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("package_intake_reviews", sa.Column("source_fingerprint", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("package_intake_reviews", "source_fingerprint")
    op.drop_column("package_intake_reviews", "episode_reasons_json")
    op.drop_table("package_intake_review_drafts")
