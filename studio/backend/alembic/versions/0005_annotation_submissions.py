"""Freeze annotation submissions and exact reviewer decisions."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0005_annotation_submissions"
down_revision = "0004_intake_review_drafts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "annotation_submissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("annotation_work_item_id", sa.Integer(), sa.ForeignKey("annotation_work_items.id"), nullable=False),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("submitted_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("episodes_json", JSONB(), nullable=False),
        sa.Column("draft_json", JSONB(), nullable=False),
        sa.Column("source_json", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("annotation_work_item_id", "generation", "draft_version", name="uq_annotation_submission_version"),
        sa.CheckConstraint("generation >= 1 AND draft_version >= 0", name="ck_annotation_submission_version"),
    )
    op.create_index("ix_annotation_submissions_item", "annotation_submissions", ["annotation_work_item_id"])
    op.add_column("annotation_work_items", sa.Column("current_submission_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_annotation_current_submission", "annotation_work_items", "annotation_submissions", ["current_submission_id"], ["id"])
    op.add_column("review_work_items", sa.Column("submission_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_review_submission", "review_work_items", "annotation_submissions", ["submission_id"], ["id"])
    op.create_table(
        "annotation_submission_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("submission_id", sa.Integer(), sa.ForeignKey("annotation_submissions.id"), nullable=False),
        sa.Column("review_work_item_id", sa.Integer(), sa.ForeignKey("review_work_items.id"), nullable=False),
        sa.Column("reviewer_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("submission_id", name="uq_annotation_submission_review"),
        sa.CheckConstraint("decision IN ('approved', 'returned')", name="ck_annotation_submission_review_decision"),
    )
    # Historical package rows have no trustworthy submission boundary. Do not
    # invent one from global EpisodeAnnotation versions during migration.
    op.execute("""CREATE FUNCTION deny_annotation_evidence_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'annotation submission evidence is immutable'; END;
    $$ LANGUAGE plpgsql""")
    for table in ("annotation_submissions", "annotation_submission_reviews"):
        op.execute(f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION deny_annotation_evidence_mutation()")


def downgrade():
    for table in ("annotation_submission_reviews", "annotation_submissions"):
        op.execute(f"DROP TRIGGER immutable_{table} ON {table}")
    op.execute("DROP FUNCTION deny_annotation_evidence_mutation()")
    op.drop_table("annotation_submission_reviews")
    op.drop_constraint("fk_review_submission", "review_work_items", type_="foreignkey")
    op.drop_column("review_work_items", "submission_id")
    op.drop_constraint("fk_annotation_current_submission", "annotation_work_items", type_="foreignkey")
    op.drop_column("annotation_work_items", "current_submission_id")
    op.drop_table("annotation_submissions")
