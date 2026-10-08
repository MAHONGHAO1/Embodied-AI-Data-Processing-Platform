from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

pytestmark = pytest.mark.skip(
    reason="Inherited quicdata revision-chain tests are obsolete after Task 2 squash to 0001_baseline"
)

ROOT = Path(__file__).resolve().parents[2]
LEGACY_HEAD = "0009_ego_offline_ingest"


def _alembic_config(database_url: URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url.render_as_string(hide_password=False))
    return config


def _migration_database_url() -> URL:
    base_url = make_url(os.environ["TEST_DATABASE_URL"])
    database_name = f"{base_url.database}_migration"
    if not re.fullmatch(r"quicdata_test[a-z0-9_]*", database_name):
        raise RuntimeError("migration test database name is not approved")
    return base_url.set(database=database_name)


def _reset_migration_database(database_url: URL) -> None:
    admin_engine = create_engine(
        database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin_engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_url.database}" WITH (FORCE)')
            )
            connection.execute(text(f'CREATE DATABASE "{database_url.database}"'))
    finally:
        admin_engine.dispose()


def _drop_migration_database(database_url: URL) -> None:
    admin_engine = create_engine(
        database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin_engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_url.database}" WITH (FORCE)')
            )
    finally:
        admin_engine.dispose()


def test_business_reset_preserves_identity_and_recreates_business_schema():
    database_url = _migration_database_url()
    _reset_migration_database(database_url)
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, LEGACY_HEAD)

        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users (id, email, password_hash, role, created_at) "
                        "VALUES (1, 'owner@example.com', 'hash', 'admin', CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, name, created_at) VALUES (1, 'workspace', CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO workspace_members (id, workspace_id, user_id, access_level, created_at) "
                        "VALUES (1, 1, 1, 'owner', CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO projects (id, workspace_id, name, created_at) "
                        "VALUES (1, 1, 'legacy project', CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO tasks (id, project_id, workspace_id, name, created_at) "
                        "VALUES (1, 1, 1, 'legacy task', CURRENT_TIMESTAMP)"
                    )
                )

            command.upgrade(config, "head")

            inspector = inspect(engine)
            assert {
                "batches",
                "collection_devices",
                "episodes",
                "episode_device_attributions",
                "import_sessions",
                "episode_artifacts",
            } <= set(inspector.get_table_names())
            batch_columns = {column["name"] for column in inspector.get_columns("batches")}
            workspace_member_columns = {
                column["name"] for column in inspector.get_columns("workspace_members")
            }
            assert "pipeline_key" not in batch_columns
            assert "source_type" not in batch_columns
            assert "access_level" not in workspace_member_columns
            assert "tasks" not in inspector.get_table_names()
            with engine.connect() as connection:
                assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 1
                assert connection.execute(text("SELECT count(*) FROM workspaces")).scalar_one() == 1
                assert (
                    connection.execute(text("SELECT count(*) FROM workspace_members")).scalar_one()
                    == 1
                )
                assert connection.execute(text("SELECT count(*) FROM task_sets")).scalar_one() == 0
        finally:
            engine.dispose()
    finally:
        _drop_migration_database(database_url)


def test_batch_contract_migration_rejects_unsupported_existing_types():
    database_url = _migration_database_url()
    _reset_migration_database(database_url)
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, "0020_episode_workbench_contract")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, name, created_at) VALUES (1, 'workspace', CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO projects (id, workspace_id, name, created_at) "
                        "VALUES (1, 1, 'project', CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO batches "
                        "(id, workspace_id, project_id, name, batch_type, pipeline_key, source_type, status, "
                        "metadata_json, created_at, updated_at) VALUES "
                        "(1, 1, 1, 'legacy import', 'manual_upload', 'legacy', 'upload', 'created', "
                        "'{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )

            with pytest.raises(RuntimeError, match="unsupported batch_type"):
                command.upgrade(config, "head")
        finally:
            engine.dispose()
    finally:
        _drop_migration_database(database_url)


def test_collector_profile_key_migration_backfills_stable_workspace_numbers():
    database_url = _migration_database_url()
    _reset_migration_database(database_url)
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, "0021_uat_frontend_optimizations")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, name, created_at) VALUES (1, 'workspace', CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO personnel_profiles "
                        "(id, workspace_id, name, is_active, metadata_json, created_at) VALUES "
                        "(11, 1, 'Ava', true, '{}'::jsonb, CURRENT_TIMESTAMP), "
                        "(12, 1, 'Bob', true, '{}'::jsonb, CURRENT_TIMESTAMP)"
                    )
                )

            command.upgrade(config, "0022_collector_profile_key")

            inspector = inspect(engine)
            columns = {
                column["name"]: column for column in inspector.get_columns("personnel_profiles")
            }
            unique_constraints = {
                constraint["name"]
                for constraint in inspector.get_unique_constraints("personnel_profiles")
            }
            with engine.connect() as connection:
                rows = connection.execute(
                    text("SELECT id, profile_key FROM personnel_profiles ORDER BY id")
                ).all()

            assert columns["profile_key"]["nullable"] is False
            assert rows == [(11, "0000"), (12, "0001")]
            assert "uq_personnel_profiles_workspace_profile_key" in unique_constraints
            assert "uq_personnel_profiles_workspace_name" not in unique_constraints
        finally:
            engine.dispose()
    finally:
        _drop_migration_database(database_url)


def test_0023_marks_only_proven_failed_import_placeholders():
    database_url = _migration_database_url()
    _reset_migration_database(database_url)
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, "0022_collector_profile_key")
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO workspaces (id, name) VALUES (1, 'workspace')")
                )
                connection.execute(
                    text("INSERT INTO projects (id, workspace_id, name) VALUES (1, 1, 'project')")
                )
                connection.execute(
                    text(
                        "INSERT INTO batches (id, workspace_id, project_id, name, batch_type) "
                        "VALUES (1, 1, 1, 'batch', 'ego')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO import_sessions (id, batch_id, import_type, status) "
                        "VALUES ('session-1', 1, 'oss_scan', 'failed')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO episodes "
                        "(id, episode_uid, workspace_id, project_id, batch_id, import_session_id, kind, modality, "
                        "source_fingerprint, workflow_status, quality_status) VALUES "
                        "(1, 'repair-me', 1, 1, 1, 'session-1', 'source', 'ego', :fp1, 'discovered', 'pending'), "
                        "(2, 'has-quality-job', 1, 1, 1, 'session-1', 'source', 'ego', :fp2, 'quality_pending', 'pending'), "
                        "(3, 'raw-published', 1, 1, 1, 'session-1', 'source', 'ego', :fp3, 'discovered', 'pending'), "
                        "(4, 'no-failure-evidence', 1, 1, 1, 'session-1', 'source', 'ego', :fp4, 'discovered', 'pending')"
                    ),
                    {"fp1": "1" * 64, "fp2": "2" * 64, "fp3": "3" * 64, "fp4": "4" * 64},
                )
                connection.execute(
                    text(
                        "INSERT INTO episode_artifacts "
                        "(id, episode_id, import_session_id, artifact_type, storage_role, storage_uri) VALUES "
                        "(1, 1, 'session-1', 'raw_source', 'raw', 'oss://raw/repair-me'), "
                        "(2, 2, 'session-1', 'raw_source', 'raw', 'oss://raw/has-quality-job'), "
                        "(3, 3, 'session-1', 'raw_source', 'raw', 'oss://raw/raw-published'), "
                        "(4, 4, 'session-1', 'raw_source', 'raw', 'oss://raw/no-failure-evidence')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO job_runs "
                        "(id, kind, resource_type, resource_id, workspace_id, project_id, idempotency_key, queue, status) VALUES "
                        "('parse-failed', 'import_parse', 'import_session', 'session-1', 1, 1, 'parse-failed', 'ingest', 'failed'), "
                        "('quality-existing', 'episode_quality', 'episode', '2', 1, 1, 'quality-existing', 'media', 'queued')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO artifact_operations "
                        "(id, artifact_id, job_id, operation_kind, status, target_uri) VALUES "
                        "('operation-1', 1, 'parse-failed', 'raw_source_publish', 'pending', 'oss://raw/repair-me'), "
                        "('operation-2', 2, 'parse-failed', 'raw_source_publish', 'pending', 'oss://raw/has-quality-job'), "
                        "('operation-3', 3, 'parse-failed', 'raw_source_publish', 'published', 'oss://raw/raw-published'), "
                        "('operation-4', 4, NULL, 'raw_source_publish', 'pending', 'oss://raw/no-failure-evidence')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO project_source_imports "
                        "(id, project_id, batch_id, import_session_id, source_fingerprint, status) VALUES "
                        "(1, 1, 1, 'session-1', :fp1, 'failed'), "
                        "(2, 1, 1, 'session-1', :fp2, 'failed'), "
                        "(3, 1, 1, 'session-1', :fp3, 'failed')"
                    ),
                    {"fp1": "1" * 64, "fp2": "2" * 64, "fp3": "3" * 64},
                )
                before_counts = {
                    table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                    for table in (
                        "episodes",
                        "episode_artifacts",
                        "artifact_operations",
                        "project_source_imports",
                        "job_runs",
                    )
                }

            command.upgrade(config, "head")

            with engine.connect() as connection:
                rows = connection.execute(
                    text("SELECT id, workflow_status, quality_status FROM episodes ORDER BY id")
                ).all()
                after_counts = {
                    table: connection.execute(
                        text(
                            "SELECT count(*) FROM task_set_source_imports"
                            if table == "project_source_imports"
                            else f"SELECT count(*) FROM {table}"
                        )
                    ).scalar_one()
                    for table in before_counts
                }

            assert rows == [
                (1, "import_failed", "not_started"),
                (2, "quality_pending", "pending"),
                (3, "discovered", "pending"),
                (4, "discovered", "pending"),
            ]
            assert after_counts == before_counts
        finally:
            engine.dispose()
    finally:
        _drop_migration_database(database_url)
