"""Single final intake and linked ordinary supplement packages."""
import sqlalchemy as sa

from alembic import op

revision = "0002_package_supplement"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("data_packages", sa.Column("supplement_for_package_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_package_supplement", "data_packages", "data_packages", ["supplement_for_package_id"], ["id"])
    op.create_index("ix_data_packages_supplement_for_package_id", "data_packages", ["supplement_for_package_id"])
    op.add_column("data_packages", sa.Column("supplement_reason", sa.Text(), nullable=False, server_default=""))
    op.add_column("data_packages", sa.Column("created_by_user_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_package_creator", "data_packages", "users", ["created_by_user_id"], ["id"])
    op.add_column("package_intake_reviews", sa.Column("request_fingerprint", sa.String(64), nullable=True))

def downgrade():
    op.drop_column("package_intake_reviews", "request_fingerprint")
    op.drop_constraint("fk_package_creator", "data_packages", type_="foreignkey")
    op.drop_column("data_packages", "created_by_user_id")
    op.drop_column("data_packages", "supplement_reason")
    op.drop_index("ix_data_packages_supplement_for_package_id", table_name="data_packages")
    op.drop_constraint("fk_package_supplement", "data_packages", type_="foreignkey")
    op.drop_column("data_packages", "supplement_for_package_id")
