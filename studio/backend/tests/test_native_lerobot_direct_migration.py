"""Migration contracts for direct native LeRobot source records."""

from __future__ import annotations

import os
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "0018_annotation_work_item_provenance_state"
TARGET_REVISION = "0019_native_lerobot_direct_sources"
CANCELLATION_TARGET_REVISION = "0020_native_lerobot_direct_cancellation"
DIRECT_JOB_ID = "11111111-1111-1111-1111-111111111111"
ORDINARY_JOB_ID = "22222222-2222-2222-2222-222222222222"


def _alembic_config(database_url: URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url.render_as_string(hide_password=False))
    return config


def _migration_database_url() -> URL:
    base_url = make_url(os.environ["TEST_DATABASE_URL"])
    database_name = f"{base_url.database}_native_lerobot_direct_migration"
    if not re.fullmatch(r"quicdata_test[a-z0-9_]*", database_name):
        raise RuntimeError("migration test database name is not approved")
    return base_url.set(database=database_name)


def _reset_database(database_url: URL) -> None:
    engine = create_engine(database_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_url.database}" WITH (FORCE)')
            )
            connection.execute(text(f'CREATE DATABASE "{database_url.database}"'))
    finally:
        engine.dispose()


def _drop_database(database_url: URL) -> None:
    engine = create_engine(database_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_url.database}" WITH (FORCE)')
            )
    finally:
        engine.dispose()


def _insert_job(connection, *, job_id: str, resource_type: str) -> None:
    connection.execute(
        text(
            "INSERT INTO job_runs ("
            "id, kind, resource_type, resource_id, workspace_id, task_set_id, idempotency_key, "
            "queue, actor_id, status, phase, progress_percent, detail_json, result_json, "
            "error_code, error_message, retry_count, attempt_count, lease_worker_id, lease_token, "
            "lease_expires_at, recovery_dispatch_token, recovery_dispatch_expires_at, "
            "recovery_dispatched_at, realtime_version, created_at, started_at, finished_at, updated_at"
            ") VALUES ("
            ":id, 'native_lerobot_direct_validate', :resource_type, 'source-1', NULL, NULL, :idempotency_key, "
            "'ingest', NULL, 'queued', 'queued', 0, '{}'::jsonb, '{}'::jsonb, '', '', 0, 0, '', '', "
            "NULL, '', NULL, NULL, 0, CURRENT_TIMESTAMP, NULL, NULL, CURRENT_TIMESTAMP"
            ")"
        ),
        {
            "id": job_id,
            "resource_type": resource_type,
            "idempotency_key": f"migration-test:{job_id}",
        },
    )
    connection.execute(
        text(
            "INSERT INTO job_queue_slots ("
            "queue, slot_number, job_id, worker_id, lease_expires_at, created_at, updated_at"
            ") VALUES ('ingest', :slot_number, :job_id, '', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"slot_number": 1 if job_id == DIRECT_JOB_ID else 2, "job_id": job_id},
    )


def test_direct_source_downgrade_removes_only_direct_job_records() -> None:
    database_url = _migration_database_url()
    _reset_database(database_url)
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, TARGET_REVISION)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                _insert_job(
                    connection,
                    job_id=DIRECT_JOB_ID,
                    resource_type="native_lerobot_direct_source",
                )
                _insert_job(connection, job_id=ORDINARY_JOB_ID, resource_type="artifact")

            command.downgrade(config, PREVIOUS_REVISION)

            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT count(*) FROM job_runs WHERE id = :job_id"),
                        {"job_id": DIRECT_JOB_ID},
                    ).scalar_one()
                    == 0
                )
                assert (
                    connection.execute(
                        text("SELECT count(*) FROM job_queue_slots WHERE job_id = :job_id"),
                        {"job_id": DIRECT_JOB_ID},
                    ).scalar_one()
                    == 0
                )
                assert (
                    connection.execute(
                        text("SELECT resource_type FROM job_runs WHERE id = :job_id"),
                        {"job_id": ORDINARY_JOB_ID},
                    ).scalar_one()
                    == "artifact"
                )
                assert (
                    connection.execute(
                        text("SELECT count(*) FROM job_queue_slots WHERE job_id = :job_id"),
                        {"job_id": ORDINARY_JOB_ID},
                    ).scalar_one()
                    == 1
                )
        finally:
            engine.dispose()
    finally:
        _drop_database(database_url)


def test_direct_cancellation_downgrade_preserves_a_terminal_failure_record() -> None:
    database_url = _migration_database_url()
    _reset_database(database_url)
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, CANCELLATION_TARGET_REVISION)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO native_lerobot_direct_sources ("
                        "id, name, description, source_workspace_id, created_by_user_id, "
                        "robot_type, dataset_id, status, file_count, total_size, manifest_sha256, "
                        "marker_ref_json, catalog_dataset_id, catalog_dataset_version_id, "
                        "validation_job_id, error_code, error_message, created_at, updated_at"
                        ") VALUES ("
                        "'cancelled-source', 'Cancelled source', '', NULL, NULL, "
                        "'unknown', 'cancelled-source', 'cancelled', 1, 1, :sha256, "
                        "'{}'::jsonb, NULL, NULL, NULL, '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"
                        ")"
                    ),
                    {"sha256": "a" * 64},
                )
                connection.execute(
                    text(
                        "INSERT INTO native_lerobot_direct_objects ("
                        "id, source_id, path, object_key, size_bytes, sha256, status, upload_id, "
                        "provider_ref_json, created_at, updated_at"
                        ") VALUES ("
                        "'cancelled-object', 'cancelled-source', 'meta/info.json', "
                        "'export/v1/native-lerobot-direct/cancelled-source/objects/meta/info.json', "
                        "1, :sha256, 'cancelled', '', "
                        '\'{"bucket_role": "export"}\'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP'
                        ")"
                    ),
                    {"sha256": "b" * 64},
                )

            command.downgrade(config, TARGET_REVISION)

            with engine.connect() as connection:
                source = (
                    connection.execute(
                        text(
                            "SELECT status, error_code, error_message "
                            "FROM native_lerobot_direct_sources WHERE id = 'cancelled-source'"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert source == {
                    "status": "failed",
                    "error_code": "native_lerobot_direct_cancelled",
                    "error_message": "native LeRobot upload cancelled",
                }
                source_object = (
                    connection.execute(
                        text(
                            "SELECT status, upload_id, provider_ref_json "
                            "FROM native_lerobot_direct_objects WHERE id = 'cancelled-object'"
                        )
                    )
                    .mappings()
                    .one()
                )
                assert source_object == {
                    "status": "declared",
                    "upload_id": "",
                    "provider_ref_json": {},
                }
        finally:
            engine.dispose()
    finally:
        _drop_database(database_url)


def test_full_migration_chain_round_trips_from_head_to_base_and_head() -> None:
    """Every revision, including direct upload cancellation, has a reversible path."""
    database_url = _migration_database_url()
    _reset_database(database_url)
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")

        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert (
                    connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                    == CANCELLATION_TARGET_REVISION
                )
                assert (
                    connection.execute(
                        text("SELECT to_regclass('native_lerobot_direct_sources')")
                    ).scalar_one()
                    == "native_lerobot_direct_sources"
                )
                assert (
                    connection.execute(
                        text("SELECT to_regclass('native_lerobot_direct_objects')")
                    ).scalar_one()
                    == "native_lerobot_direct_objects"
                )
        finally:
            engine.dispose()
    finally:
        _drop_database(database_url)
