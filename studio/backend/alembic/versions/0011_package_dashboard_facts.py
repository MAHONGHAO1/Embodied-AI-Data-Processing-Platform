"""Data packages carry exact dashboard facts written at parse and intake review."""

import sqlalchemy as sa
from alembic import op

revision = "0011_package_dashboard_facts"
down_revision = "0010_admission_integrity_source"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("data_packages", sa.Column("captured_started_at", sa.DateTime(), nullable=True))
    op.add_column("data_packages", sa.Column("captured_duration_s", sa.Numeric(14, 3), nullable=True))
    op.add_column("data_packages", sa.Column("captured_size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("data_packages", sa.Column("intake_valid_duration_s", sa.Numeric(14, 3), nullable=True))
    op.add_column("data_packages", sa.Column("intake_valid_size_bytes", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_data_packages_dashboard_facts_non_negative",
        "data_packages",
        "(captured_duration_s IS NULL OR captured_duration_s >= 0) "
        "AND (captured_size_bytes IS NULL OR captured_size_bytes >= 0) "
        "AND (intake_valid_duration_s IS NULL OR intake_valid_duration_s >= 0) "
        "AND (intake_valid_size_bytes IS NULL OR intake_valid_size_bytes >= 0)",
    )
    op.create_check_constraint(
        "ck_data_packages_dashboard_valid_within_captured",
        "data_packages",
        "(intake_valid_duration_s IS NULL OR captured_duration_s IS NULL "
        "OR intake_valid_duration_s <= captured_duration_s) "
        "AND (intake_valid_size_bytes IS NULL OR captured_size_bytes IS NULL "
        "OR intake_valid_size_bytes <= captured_size_bytes)",
    )
    op.create_index("ix_data_packages_workspace_captured", "data_packages", ["workspace_id", "captured_started_at"])
    op.create_index("ix_data_packages_task_captured", "data_packages", ["collection_task_id", "captured_started_at"])


def downgrade():
    op.drop_index("ix_data_packages_task_captured", table_name="data_packages")
    op.drop_index("ix_data_packages_workspace_captured", table_name="data_packages")
    op.drop_constraint("ck_data_packages_dashboard_valid_within_captured", "data_packages", type_="check")
    op.drop_constraint("ck_data_packages_dashboard_facts_non_negative", "data_packages", type_="check")
    for column in (
        "intake_valid_size_bytes",
        "intake_valid_duration_s",
        "captured_size_bytes",
        "captured_duration_s",
        "captured_started_at",
    ):
        op.drop_column("data_packages", column)
