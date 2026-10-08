"""Settle queued legacy work while preserving all historical rows.

Stop legacy producers/workers and drain running jobs before deployment.
"""
import sqlalchemy as sa

from alembic import op

revision = "0003_retire_legacy_jobs"
down_revision = "0002_package_supplement"
branch_labels = None
depends_on = None
RETIRED_KIND_PARAMS = {
    "kind_episode_publish": "episode_publish",
    "kind_native_lerobot_scan": "native_lerobot_scan",
    "kind_native_lerobot_copy": "native_lerobot_copy",
    "kind_native_lerobot_bundle": "native_lerobot_bundle",
}

def upgrade():
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE job_runs IN SHARE ROW EXCLUSIVE MODE"))
    running_query = sa.text(
        "SELECT count(*) FROM job_runs "
        "WHERE kind IN ("
        ":kind_episode_publish, :kind_native_lerobot_scan, "
        ":kind_native_lerobot_copy, :kind_native_lerobot_bundle"
        ") AND status='running'"
    )
    if connection.execute(running_query, RETIRED_KIND_PARAMS).scalar():
        raise RuntimeError("Drain running legacy jobs before applying legacy retirement")
    connection.execute(
        sa.text(
            "UPDATE job_runs SET status='cancelled', "
            "error_code='legacy_workflow_retired', "
            "error_message='Legacy workflow retired; history retained' "
            "WHERE kind IN ("
            ":kind_episode_publish, :kind_native_lerobot_scan, "
            ":kind_native_lerobot_copy, :kind_native_lerobot_bundle"
            ") "
            "AND status IN ('queued','retry_pending')"
        ),
        RETIRED_KIND_PARAMS,
    )

def downgrade():
    # Cancelled history must not automatically execute again on downgrade.
    pass
