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
PREVIOUS_REVISION = "0024_cloud_runtime_settings"
TARGET_REVISION = "0025_task_set_domain"
CAPTURE_CANDIDATE_PREVIOUS_REVISION = "0031_dataset_item_identity"
CAPTURE_CANDIDATE_TARGET_REVISION = "0032_capture_episode_candidate"


def _alembic_config(database_url: URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url.render_as_string(hide_password=False))
    return config


def _migration_database_url() -> URL:
    base_url = make_url(os.environ["TEST_DATABASE_URL"])
    database_name = f"{base_url.database}_task_set_migration"
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


def _seed_project_domain(connection) -> None:
    connection.execute(text("INSERT INTO workspaces (id, name) VALUES (7, 'warehouse')"))
    connection.execute(
        text(
            "INSERT INTO projects (id, workspace_id, name, description, scene) "
            "VALUES (41, 7, 'warehouse operations', 'preserved', 'packing')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO batches (id, workspace_id, project_id, name, batch_type) "
            "VALUES (51, 7, 41, 'morning', 'ego')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO import_sessions (id, batch_id, import_type, status) "
            "VALUES ('import-51', 51, 'oss_scan', 'succeeded')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO episodes "
            "(id, episode_uid, workspace_id, project_id, batch_id, import_session_id, kind, modality, source_fingerprint) "
            "VALUES (61, 'source-61', 7, 41, 51, 'import-51', 'source', 'ego', :fingerprint)"
        ),
        {"fingerprint": "a" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO project_source_imports "
            "(id, project_id, batch_id, import_session_id, source_episode_id, source_fingerprint, status) "
            "VALUES (71, 41, 51, 'import-51', 61, :fingerprint, 'imported')"
        ),
        {"fingerprint": "a" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO import_candidates "
            "(id, import_session_id, project_source_import_id, candidate_type, status, original_name, "
            "size_bytes, source_fingerprint, locator_json, created_at, updated_at) "
            "VALUES ('candidate-71', 'import-51', 71, 'ego_episode_oss', 'consumed', 'source-61', "
            "123, :fingerprint, '{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"fingerprint": "a" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO job_runs "
            "(id, kind, resource_type, resource_id, workspace_id, project_id, idempotency_key, queue, status) "
            "VALUES ('job-61', 'episode_quality', 'episode', '61', 7, 41, 'job-61', 'media', 'succeeded')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO external_oss_import_scopes "
            "(id, workspace_id, project_id, bucket, prefixes_json, is_enabled, revision, created_at, updated_at) "
            "VALUES (81, 7, 41, 'source-bucket', '[\"prod/raw/v1/tasks\"]'::jsonb, true, 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )


def test_task_set_upgrade_and_downgrade_preserve_domain_identity():
    database_url = _migration_database_url()
    _reset_database(database_url)
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, PREVIOUS_REVISION)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                _seed_project_domain(connection)

            command.upgrade(config, TARGET_REVISION)

            inspector = inspect(engine)
            assert "projects" not in inspector.get_table_names()
            assert {"task_sets", "task_set_source_imports", "external_project_refs"} <= set(
                inspector.get_table_names()
            )
            with engine.connect() as connection:
                assert connection.execute(
                    text(
                        "SELECT id, workspace_id, name, description, scene, external_project_ref_id "
                        "FROM task_sets WHERE id = 41"
                    )
                ).one() == (41, 7, "warehouse operations", "preserved", "packing", None)
                assert (
                    connection.execute(
                        text("SELECT task_set_id FROM batches WHERE id = 51")
                    ).scalar_one()
                    == 41
                )
                assert (
                    connection.execute(
                        text("SELECT task_set_id FROM episodes WHERE id = 61")
                    ).scalar_one()
                    == 41
                )
                assert (
                    connection.execute(
                        text("SELECT task_set_id FROM job_runs WHERE id = 'job-61'")
                    ).scalar_one()
                    == 41
                )
                assert (
                    connection.execute(
                        text("SELECT task_set_id FROM external_oss_import_scopes WHERE id = 81")
                    ).scalar_one()
                    == 41
                )
                assert (
                    connection.execute(
                        text("SELECT task_set_id FROM task_set_source_imports WHERE id = 71")
                    ).scalar_one()
                    == 41
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT task_set_source_import_id FROM import_candidates WHERE id = 'candidate-71'"
                        )
                    ).scalar_one()
                    == 71
                )

            command.downgrade(config, PREVIOUS_REVISION)

            inspector = inspect(engine)
            assert "task_sets" not in inspector.get_table_names()
            assert {"projects", "project_source_imports"} <= set(inspector.get_table_names())
            with engine.connect() as connection:
                assert (
                    connection.execute(text("SELECT id FROM projects WHERE id = 41")).scalar_one()
                    == 41
                )
                assert (
                    connection.execute(
                        text("SELECT project_id FROM batches WHERE id = 51")
                    ).scalar_one()
                    == 41
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT project_source_import_id FROM import_candidates WHERE id = 'candidate-71'"
                        )
                    ).scalar_one()
                    == 71
                )
        finally:
            engine.dispose()
    finally:
        _drop_database(database_url)


def test_external_project_reference_cannot_cross_workspace():
    database_url = _migration_database_url()
    _reset_database(database_url)
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, TARGET_REVISION)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO workspaces (id, name) VALUES (1, 'one'), (2, 'two')")
                )
                connection.execute(
                    text(
                        "INSERT INTO task_sets (id, workspace_id, name) VALUES (11, 1, 'task set')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO external_project_refs "
                        "(id, workspace_id, provider, external_project_id, display_name, status, metadata_json) "
                        "VALUES (22, 2, 'erp', 'project-22', 'External', 'active', '{}'::jsonb)"
                    )
                )
                connection.execute(text("SAVEPOINT cross_workspace_reference"))
                try:
                    connection.execute(
                        text("UPDATE task_sets SET external_project_ref_id = 22 WHERE id = 11")
                    )
                except Exception:
                    connection.execute(text("ROLLBACK TO SAVEPOINT cross_workspace_reference"))
                else:
                    raise AssertionError("cross-workspace external project reference was accepted")
        finally:
            engine.dispose()
    finally:
        _drop_database(database_url)


def test_capture_episode_candidate_upgrade_and_guarded_downgrade():
    database_url = _migration_database_url()
    _reset_database(database_url)
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, CAPTURE_CANDIDATE_PREVIOUS_REVISION)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO workspaces (id, name) VALUES (107, 'capture')")
                )
                connection.execute(
                    text(
                        "INSERT INTO task_sets (id, workspace_id, name) VALUES (141, 107, 'capture sources')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO batches (id, workspace_id, task_set_id, name, batch_type) "
                        "VALUES (151, 107, 141, 'capture batch', 'ego')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO import_sessions (id, batch_id, import_type, status) "
                        "VALUES ('import-capture', 151, 'oss_scan', 'init')"
                    )
                )

            command.upgrade(config, CAPTURE_CANDIDATE_TARGET_REVISION)

            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO import_candidates "
                        "(id, import_session_id, candidate_type, status, original_name, size_bytes, "
                        "source_fingerprint, locator_json, created_at, updated_at) "
                        "VALUES ('capture-candidate', 'import-capture', 'capture_episode_oss', "
                        "'discovered', 'episode-a', 1, :fingerprint, '{}'::jsonb, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"fingerprint": "c" * 64},
                )
                connection.execute(
                    text(
                        "INSERT INTO task_set_source_imports "
                        "(task_set_id, source_fingerprint, source_kind, display_name) "
                        "VALUES (141, :fingerprint, 'capture_oss', 'episode-a')"
                    ),
                    {"fingerprint": "e" * 64},
                )

            with engine.begin() as connection:
                connection.execute(text("SAVEPOINT capture_episode_identity"))
                try:
                    connection.execute(
                        text(
                            "INSERT INTO task_set_source_imports "
                            "(task_set_id, source_fingerprint, source_kind, display_name) "
                            "VALUES (141, :fingerprint, 'capture_oss', 'episode-a')"
                        ),
                        {"fingerprint": "f" * 64},
                    )
                except Exception:
                    connection.execute(text("ROLLBACK TO SAVEPOINT capture_episode_identity"))
                else:
                    raise AssertionError("duplicate capture episode identity was accepted")

            with pytest.raises(RuntimeError, match="capture_episode_oss"):
                command.downgrade(config, CAPTURE_CANDIDATE_PREVIOUS_REVISION)

            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM import_candidates WHERE id = 'capture-candidate'")
                )
            command.downgrade(config, CAPTURE_CANDIDATE_PREVIOUS_REVISION)

            with engine.begin() as connection:
                connection.execute(text("SAVEPOINT capture_candidate_constraint"))
                try:
                    connection.execute(
                        text(
                            "INSERT INTO import_candidates "
                            "(id, import_session_id, candidate_type, status, original_name, size_bytes, "
                            "source_fingerprint, locator_json, created_at, updated_at) "
                            "VALUES ('capture-candidate-old-head', 'import-capture', "
                            "'capture_episode_oss', 'discovered', 'episode-a', 1, :fingerprint, "
                            "'{}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                        ),
                        {"fingerprint": "d" * 64},
                    )
                except Exception:
                    connection.execute(text("ROLLBACK TO SAVEPOINT capture_candidate_constraint"))
                else:
                    raise AssertionError("old candidate constraint accepted capture_episode_oss")
        finally:
            engine.dispose()
    finally:
        _drop_database(database_url)
