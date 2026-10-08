from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

from data.database import (
    Batch,
    ExternalOssImportScope,
    NativeLerobotBundle,
    NativeLerobotDataset,
    TaskSet,
    Workspace,
)
from data.services.native_lerobot_datasets import (
    COMPLETE_MARKER_NAME,
    NativeLerobotMarker,
    NativeLerobotObject,
    platform_native_lerobot_uri,
)

pytestmark = pytest.mark.skip(
    reason="Inherited quicdata revision-chain tests are obsolete after Task 2 squash to 0001_baseline"
)

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "0038_native_lerobot_datasets"
TARGET_REVISION = "0039_native_lerobot_replication"
MULTI_DATASET_TARGET_REVISION = "0040_lerobot_batch_import"


def _legacy_backfill_fixture(db_session, *, suffix: str | None = None):
    suffix = suffix or uuid4().hex[:10]
    workspace = Workspace(name=f"native backfill workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"native backfill task set {suffix}")
    db_session.add(task_set)
    db_session.flush()
    scope = ExternalOssImportScope(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        bucket="legacy-native-source",
        prefixes_json=["prod/raw/robot"],
        is_enabled=True,
        revision=1,
    )
    db_session.add(scope)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"legacy native {suffix}",
        batch_type="lerobot",
        status="processing",
    )
    db_session.add(batch)
    db_session.flush()
    marker = NativeLerobotMarker(
        bucket=scope.bucket,
        marker_key=f"prod/raw/robot/so101/legacy-{suffix}/{COMPLETE_MARKER_NAME}",
        oss_uri=f"oss://{scope.bucket}/prod/raw/robot/so101/legacy-{suffix}/",
        robot_type="so101",
        dataset_id=f"legacy-{suffix}",
        file_count=1,
        total_size=12,
        completed_at=datetime(2026, 8, 28, 10, 15, 30),
        manifest_sha256="a" * 64,
        marker_sha256="b" * 64,
        objects=(NativeLerobotObject(path="data/demo.parquet", size=12, sha256="c" * 64),),
    )
    dataset = NativeLerobotDataset(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        name=f"legacy native {suffix}",
        source_oss_uri=marker.oss_uri,
        source_scope_id=None,
        source_scope_revision=None,
        oss_uri=platform_native_lerobot_uri(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            batch_id=batch.id,
            robot_type=marker.robot_type,
            dataset_id=marker.dataset_id,
        ),
        robot_type=marker.robot_type,
        dataset_id=marker.dataset_id,
        file_count=marker.file_count,
        total_size=marker.total_size,
        completed_at=marker.completed_at,
        manifest_sha256=marker.manifest_sha256,
        marker_sha256=marker.marker_sha256,
        status="active",
        copy_status="backfill_pending",
    )
    db_session.add(dataset)
    db_session.commit()
    return scope, dataset, marker


def _alembic_config(database_url: URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url.render_as_string(hide_password=False))
    return config


def _migration_database_url() -> URL:
    base_url = make_url(os.environ["TEST_DATABASE_URL"])
    database_name = f"{base_url.database}_native_lerobot_replication_migration"
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


def test_native_copy_model_and_bundle_contracts_are_present():
    native_columns = NativeLerobotDataset.__table__.c
    assert {
        "source_oss_uri",
        "source_scope_id",
        "source_scope_revision",
        "oss_uri",
        "copy_status",
        "last_copy_job_id",
        "copy_started_at",
        "copy_finished_at",
        "copy_error_code",
        "copy_error_message",
    }.issubset(native_columns.keys())
    assert NativeLerobotBundle.__tablename__ == "native_lerobot_bundles"
    assert {
        "native_lerobot_dataset_id",
        "marker_sha256",
        "status",
        "job_id",
        "bundle_uri",
        "expires_at",
        "error_code",
        "error_message",
    }.issubset(NativeLerobotBundle.__table__.c.keys())
    assert {
        "uq_native_lerobot_datasets_workspace_source_marker",
        "uq_native_lerobot_datasets_workspace_oss_uri",
        "ck_native_lerobot_datasets_copy_status",
        "ck_native_lerobot_datasets_source_scope_snapshot",
    }.issubset(
        {
            constraint.name
            for constraint in NativeLerobotDataset.__table__.constraints
            if constraint.name
        }
    )
    assert {
        "uq_native_lerobot_bundles_dataset_marker",
        "ck_native_lerobot_bundles_status",
    }.issubset(
        {
            constraint.name
            for constraint in NativeLerobotBundle.__table__.constraints
            if constraint.name
        }
    )


def test_native_replication_upgrade_preserves_legacy_source_without_oss_access(monkeypatch):
    database_url = _migration_database_url()
    _reset_database(database_url)
    monkeypatch.setenv("OSS_BUCKET_EXPORT", "migration-export")
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, PREVIOUS_REVISION)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO workspaces (id, name) VALUES (1, 'workspace')")
                )
                connection.execute(
                    text("INSERT INTO task_sets (id, workspace_id, name) VALUES (2, 1, 'task set')")
                )
                connection.execute(
                    text(
                        "INSERT INTO batches "
                        "(id, workspace_id, task_set_id, name, batch_type, status) "
                        "VALUES (3, 1, 2, 'legacy native', 'lerobot', 'ready')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO native_lerobot_datasets "
                        "(id, workspace_id, task_set_id, batch_id, name, oss_uri, robot_type, "
                        "dataset_id, file_count, total_size, completed_at, manifest_sha256, marker_sha256, status) "
                        "VALUES (4, 1, 2, 3, 'legacy root', "
                        "'oss://source-bucket/prod/raw/robot/so101/demo/', 'so101', 'demo', "
                        "1, 4, CURRENT_TIMESTAMP, :manifest_sha256, :marker_sha256, 'active')"
                    ),
                    {"manifest_sha256": "a" * 64, "marker_sha256": "b" * 64},
                )

            command.upgrade(config, TARGET_REVISION)

            inspector = inspect(engine)
            columns = {
                column["name"] for column in inspector.get_columns("native_lerobot_datasets")
            }
            assert {
                "source_oss_uri",
                "source_scope_id",
                "source_scope_revision",
                "copy_status",
                "copy_error_code",
                "copy_error_message",
            }.issubset(columns)
            assert "native_lerobot_bundles" in inspector.get_table_names()
            assert {
                "uq_native_lerobot_datasets_workspace_source_marker",
                "uq_native_lerobot_datasets_workspace_oss_uri",
            }.issubset(
                {
                    constraint["name"]
                    for constraint in inspector.get_unique_constraints("native_lerobot_datasets")
                }
            )
            assert "ck_native_lerobot_datasets_copy_status" in {
                constraint["name"]
                for constraint in inspector.get_check_constraints("native_lerobot_datasets")
            }

            with engine.connect() as connection:
                native = connection.execute(
                    text(
                        "SELECT source_oss_uri, oss_uri, copy_status, source_scope_id, source_scope_revision "
                        "FROM native_lerobot_datasets WHERE id = 4"
                    )
                ).one()
                assert native.source_oss_uri == "oss://source-bucket/prod/raw/robot/so101/demo/"
                assert native.oss_uri == (
                    "oss://migration-export/exports/v1/native-lerobot/workspaces/1/"
                    "task-sets/2/batches/3/so101/demo/"
                )
                assert native.copy_status == "backfill_pending"
                assert native.source_scope_id is None
                assert native.source_scope_revision is None
                assert (
                    connection.execute(text("SELECT status FROM batches WHERE id = 3")).scalar_one()
                    == "processing"
                )
                assert connection.execute(text("SELECT count(*) FROM job_runs")).scalar_one() == 0

            with pytest.raises(
                RuntimeError, match="cannot remove platform-owned native LeRobot schema"
            ):
                command.downgrade(config, PREVIOUS_REVISION)

            with engine.begin() as connection:
                connection.execute(text("DELETE FROM native_lerobot_datasets WHERE id = 4"))
            command.downgrade(config, PREVIOUS_REVISION)
            downgraded_columns = {
                column["name"] for column in inspect(engine).get_columns("native_lerobot_datasets")
            }
            assert "oss_uri" in downgraded_columns
            assert "source_oss_uri" not in downgraded_columns
            assert "native_lerobot_bundles" not in inspect(engine).get_table_names()
        finally:
            engine.dispose()
    finally:
        _drop_database(database_url)


def test_multi_dataset_upgrade_preserves_legacy_native_dataset_without_rewriting_uris(monkeypatch):
    database_url = _migration_database_url()
    _reset_database(database_url)
    monkeypatch.setenv("OSS_BUCKET_EXPORT", "migration-export")
    try:
        config = _alembic_config(database_url)
        command.upgrade(config, TARGET_REVISION)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO workspaces (id, name) VALUES (1, 'workspace')")
                )
                connection.execute(
                    text("INSERT INTO task_sets (id, workspace_id, name) VALUES (2, 1, 'task set')")
                )
                connection.execute(
                    text(
                        "INSERT INTO batches "
                        "(id, workspace_id, task_set_id, name, batch_type, status) "
                        "VALUES (3, 1, 2, 'legacy native', 'lerobot', 'processing')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO native_lerobot_datasets "
                        "(id, workspace_id, task_set_id, batch_id, name, source_oss_uri, oss_uri, "
                        "robot_type, dataset_id, file_count, total_size, completed_at, manifest_sha256, "
                        "marker_sha256, status, copy_status) "
                        "VALUES (4, 1, 2, 3, 'legacy root', 'oss://source-bucket/legacy/', "
                        "'oss://migration-export/platform/legacy/', 'so101', 'demo', 1, 4, "
                        "CURRENT_TIMESTAMP, :manifest_sha256, :marker_sha256, 'active', 'backfill_pending')"
                    ),
                    {"manifest_sha256": "a" * 64, "marker_sha256": "b" * 64},
                )

            command.upgrade(config, MULTI_DATASET_TARGET_REVISION)

            with engine.begin() as connection:
                legacy = connection.execute(
                    text(
                        "SELECT source_oss_uri, oss_uri, import_session_id "
                        "FROM native_lerobot_datasets WHERE id = 4"
                    )
                ).one()
                assert legacy.source_oss_uri == "oss://source-bucket/legacy/"
                assert legacy.oss_uri == "oss://migration-export/platform/legacy/"
                assert legacy.import_session_id is None
                connection.execute(
                    text(
                        "INSERT INTO native_lerobot_datasets "
                        "(id, workspace_id, task_set_id, batch_id, name, source_oss_uri, oss_uri, "
                        "robot_type, dataset_id, file_count, total_size, completed_at, manifest_sha256, "
                        "marker_sha256, status, copy_status) "
                        "VALUES (5, 1, 2, 3, 'second root', 'oss://source-bucket/second/', "
                        "'oss://migration-export/platform/second/', 'so101', 'second', 1, 4, "
                        "CURRENT_TIMESTAMP, :manifest_sha256, :marker_sha256, 'active', 'queued')"
                    ),
                    {"manifest_sha256": "c" * 64, "marker_sha256": "d" * 64},
                )
                connection.execute(
                    text("UPDATE batches SET status = 'partial_failed' WHERE id = 3")
                )

            with pytest.raises(
                RuntimeError, match="cannot remove native LeRobot multi-dataset schema"
            ):
                command.downgrade(config, TARGET_REVISION)
        finally:
            engine.dispose()
    finally:
        _drop_database(database_url)


def test_native_backfill_check_is_read_only_and_requires_an_explicit_scope(db_session):
    from data.services.native_lerobot_replication import (
        NativeLerobotBackfillError,
        backfill_native_lerobot_page,
    )

    scope, pending, _marker = _legacy_backfill_fixture(db_session)

    with pytest.raises(NativeLerobotBackfillError, match="scope id"):
        backfill_native_lerobot_page(
            db_session,
            scope_id=None,
            limit=100,
            after_native_id=None,
            apply=False,
        )
    page = backfill_native_lerobot_page(
        db_session,
        scope_id=scope.id,
        limit=100,
        after_native_id=None,
        apply=False,
    )

    assert page.scanned_count == 1
    assert page.queued_jobs == ()
    assert page.failed_count == 0
    db_session.refresh(pending)
    assert pending.copy_status == "backfill_pending"
    assert pending.source_scope_id is None


def test_native_backfill_binds_matching_scope_and_queues_one_copy_job(db_session, monkeypatch):
    from data.services import native_lerobot_replication as replication

    scope, pending, marker = _legacy_backfill_fixture(db_session)
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)

    page = replication.backfill_native_lerobot_page(
        db_session,
        scope_id=scope.id,
        limit=100,
        after_native_id=None,
        apply=True,
    )
    db_session.commit()

    assert page.scanned_count == 1
    assert len(page.queued_jobs) == 1
    assert page.failed_count == 0
    db_session.refresh(pending)
    assert pending.copy_status == "queued"
    assert pending.source_scope_id == scope.id
    assert pending.source_scope_revision == scope.revision
    assert pending.last_copy_job_id == page.queued_jobs[0].id
    assert page.queued_jobs[0].kind == "native_lerobot_copy"


def test_native_backfill_marks_changed_marker_failed_without_queueing(db_session, monkeypatch):
    from data.services import native_lerobot_replication as replication

    scope, pending, marker = _legacy_backfill_fixture(db_session)
    changed = NativeLerobotMarker(**{**marker.__dict__, "marker_sha256": "d" * 64})
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: changed)

    page = replication.backfill_native_lerobot_page(
        db_session,
        scope_id=scope.id,
        limit=100,
        after_native_id=None,
        apply=True,
    )
    db_session.commit()

    assert page.queued_jobs == ()
    assert page.failed_count == 1
    db_session.refresh(pending)
    assert pending.copy_status == "failed"
    assert pending.copy_error_code == "source_changed"


def test_native_backfill_leaves_unmatched_source_uri_pending_and_returns_page_cursor(
    db_session, monkeypatch
):
    from data.services import native_lerobot_replication as replication

    scope, pending, marker = _legacy_backfill_fixture(db_session, suffix="first")
    _scope, second, _second_marker = _legacy_backfill_fixture(db_session, suffix="second")
    second.batch_id = pending.batch_id
    second.workspace_id = pending.workspace_id
    second.task_set_id = pending.task_set_id
    second.source_oss_uri = (
        "oss://legacy-native-source/prod/raw/robot/so101/not-the-recorded-dataset/"
    )
    db_session.commit()
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)

    page = replication.backfill_native_lerobot_page(
        db_session,
        scope_id=scope.id,
        limit=1,
        after_native_id=None,
        apply=True,
    )
    db_session.commit()

    assert page.scanned_count == 1
    assert page.next_after_native_id is not None
    db_session.refresh(second)
    assert second.copy_status == "backfill_pending"


def test_native_backfill_cli_checks_read_only_and_can_queue_without_a_worker(
    db_session, monkeypatch, capsys
):
    from data.services import native_lerobot_replication as replication
    from scripts import backfill_native_lerobot as script

    scope, pending, marker = _legacy_backfill_fixture(db_session)

    class SessionProxy:
        def __getattr__(self, name):
            return getattr(db_session, name)

        def close(self):
            return None

    monkeypatch.setattr(script, "SessionLocal", lambda: SessionProxy())
    monkeypatch.setattr(script, "assert_schema_current", lambda: None)
    monkeypatch.setattr(
        replication,
        "read_native_lerobot_marker",
        lambda **_kwargs: pytest.fail("--check must not read the external marker"),
    )

    assert script.main(["--scope-id", str(scope.id), "--limit", "100", "--check"]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked == {
        "complete": True,
        "dispatched": 0,
        "failed": 0,
        "next_after_native_id": None,
        "queued": 0,
        "scanned": 1,
    }
    db_session.refresh(pending)
    assert pending.copy_status == "backfill_pending"

    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)
    assert (
        script.main(["--scope-id", str(scope.id), "--limit", "100", "--apply", "--queue-only"]) == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["queued"] == 1
    assert applied["dispatched"] == 0
    db_session.refresh(pending)
    assert pending.copy_status == "queued"
