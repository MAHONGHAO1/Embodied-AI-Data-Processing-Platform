from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from data.database import (
    Batch,
    Dataset,
    ExternalOssImportScope,
    NativeLerobotDataset,
    NativeLerobotImportSession,
    TaskSet,
    User,
    Workspace,
)
from data.infra.oss_client import OSSDirectoryPage, OSSObjectInfo


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _marker() -> tuple[dict[str, object], str]:
    objects = [{"path": "data/demo.parquet", "size": 12, "sha256": "a" * 64}]
    marker = {
        "schema_version": 1,
        "robot_type": "so101",
        "dataset_id": "api-demo",
        "objects": objects,
        "file_count": 1,
        "total_size": 12,
        "manifest_sha256": hashlib.sha256(
            _canonical_json({"robot_type": "so101", "dataset_id": "api-demo", "objects": objects})
        ).hexdigest(),
        "completed_at": "2026-08-27T11:00:00Z",
    }
    return marker, hashlib.sha256(_canonical_json(marker)).hexdigest()


def _scope_context(db_session):
    actor = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    suffix = uuid4().hex[:10]
    workspace = Workspace(name=f"native API workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"native API task set {suffix}")
    db_session.add(task_set)
    db_session.flush()
    db_session.add(
        ExternalOssImportScope(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            bucket="test-raw",
            prefixes_json=["prod/raw/robot"],
            is_enabled=True,
            revision=1,
            created_by_user_id=actor.id,
        )
    )
    db_session.commit()
    return actor, workspace, task_set


def _install_marker_listing(monkeypatch):
    from data.infra import oss_client

    marker, marker_sha256 = _marker()
    marker_key = "prod/raw/robot/so101/api-demo/complete.json"

    def fake_directories(bucket: str, prefix: str, *, continuation_token, max_keys):
        assert bucket == "test-raw"
        assert continuation_token is None
        if prefix == "prod/raw/robot/":
            return OSSDirectoryPage(prefixes=("prod/raw/robot/so101/",), next_token=None)
        if prefix == "prod/raw/robot/so101/":
            return OSSDirectoryPage(prefixes=("prod/raw/robot/so101/api-demo/",), next_token=None)
        raise AssertionError("unexpected payload listing")

    monkeypatch.setattr(oss_client, "list_prefix_directory_page", fake_directories)
    monkeypatch.setattr(
        oss_client,
        "object_info",
        lambda bucket, key: (
            OSSObjectInfo(
                size=len(_canonical_json(marker)),
                etag="api-marker",
                crc64=None,
                version_id=None,
                metadata={"x-oss-meta-sha256": marker_sha256},
            )
            if (bucket, key) == ("test-raw", marker_key)
            else None
        ),
    )
    monkeypatch.setattr(
        oss_client,
        "read_json_object",
        lambda bucket, key, *, max_bytes, if_match=None: marker,
    )


def test_legacy_lerobot_candidate_scan_and_registration_are_retired(
    client, admin_headers, db_session
):
    _actor, workspace, task_set = _scope_context(db_session)

    candidates = client.get(
        "/api/v1/batches/lerobot-candidates",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "task_set_id": task_set.id},
    )
    created = client.post(
        "/api/v1/batches/lerobot",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "task_set_id": task_set.id,
            "name": "retired legacy LeRobot registration",
            "candidate_token": "retired-candidate-token",
        },
    )

    assert candidates.status_code == 404
    assert created.status_code in {404, 405}
    assert db_session.query(Batch).filter(Batch.workspace_id == workspace.id).count() == 0


def test_historical_native_lerobot_directory_is_read_only(
    client, admin_headers, db_session, monkeypatch
):
    from data.services.native_lerobot_datasets import (
        discover_native_lerobot_candidates,
        register_native_lerobot_dataset,
    )

    actor, workspace, task_set = _scope_context(db_session)
    _install_marker_listing(monkeypatch)
    candidate = discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )[0]
    batch, native_dataset, _job = register_native_lerobot_dataset(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="API direct registration",
        candidate_token=candidate.token,
        actor_id=actor.id,
    )
    db_session.commit()

    listed = client.get(
        "/api/v1/native-lerobot-datasets",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "task_set_id": task_set.id},
    )

    assert listed.status_code == 200
    row = listed.json()["data"]["items"][0]
    assert row["id"] == native_dataset.id
    assert row["type"] == "native_lerobot"
    assert "oss_uri" not in row

    uri = client.get(
        f"/api/v1/native-lerobot-datasets/{native_dataset.id}/oss-uri",
        headers=admin_headers,
    )
    assert uri.status_code == 409

    native_dataset.copy_status = "succeeded"
    batch.status = "ready"
    db_session.commit()
    uri = client.get(
        f"/api/v1/native-lerobot-datasets/{native_dataset.id}/oss-uri",
        headers=admin_headers,
    )
    assert uri.status_code == 200
    assert uri.json()["data"]["oss_uri"] == native_dataset.oss_uri
    assert "test-raw" not in uri.text

    archived = client.patch(
        f"/api/v1/native-lerobot-datasets/{native_dataset.id}",
        headers=admin_headers,
        json={"status": "archived"},
    )
    assert archived.status_code == 410
    db_session.refresh(native_dataset)
    assert native_dataset.status == "active"
    db_session.refresh(batch)
    assert batch.status == "ready"


def test_historical_native_lerobot_archive_mutation_is_retired(
    client, admin_headers, db_session, monkeypatch
):
    from data.services.native_lerobot_datasets import (
        discover_native_lerobot_candidates,
        register_native_lerobot_dataset,
    )

    actor, workspace, task_set = _scope_context(db_session)
    _install_marker_listing(monkeypatch)
    candidate = discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )[0]
    batch, succeeded, _job = register_native_lerobot_dataset(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="mixed archive",
        candidate_token=candidate.token,
        actor_id=actor.id,
    )
    succeeded.copy_status = "succeeded"
    failed = NativeLerobotDataset(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        name="failed sibling",
        source_oss_uri="oss://test-raw/prod/raw/robot/so101/failed-sibling/",
        oss_uri="oss://test-export/exports/failed-sibling/",
        robot_type="so101",
        dataset_id="failed-sibling",
        file_count=1,
        total_size=1,
        completed_at=succeeded.completed_at,
        manifest_sha256="d" * 64,
        marker_sha256="f" * 64,
        copy_status="failed",
    )
    db_session.add(failed)
    batch.status = "partial_failed"
    db_session.commit()

    archived = client.patch(
        f"/api/v1/native-lerobot-datasets/{succeeded.id}",
        headers=admin_headers,
        json={"status": "archived"},
    )

    assert archived.status_code == 410
    db_session.refresh(succeeded)
    assert succeeded.status == "active"
    db_session.refresh(failed)
    assert failed.status == "active"
    assert failed.copy_status == "failed"
    db_session.refresh(batch)
    assert batch.status == "partial_failed"


def test_dataset_catalog_paginates_qrdf_and_native_lerobot_without_uri(
    client, admin_headers, operator_headers, db_session, monkeypatch
):
    from data.services.native_lerobot_datasets import (
        discover_native_lerobot_candidates,
        register_native_lerobot_dataset,
    )

    actor, workspace, task_set = _scope_context(db_session)
    qrdf = Dataset(
        workspace_id=workspace.id,
        name="API QRDF catalog entry",
        description="safe catalog projection",
        created_by_user_id=actor.id,
    )
    db_session.add(qrdf)
    db_session.flush()
    _install_marker_listing(monkeypatch)
    candidate = discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )[0]
    _batch, native_dataset, _job = register_native_lerobot_dataset(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="API native catalog entry",
        candidate_token=candidate.token,
        actor_id=actor.id,
    )
    db_session.commit()

    first = client.get(
        "/api/v1/datasets/catalog",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "limit": 1, "offset": 0},
    )
    second = client.get(
        "/api/v1/datasets/catalog",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "limit": 1, "offset": 1},
    )
    denied = client.get(
        "/api/v1/datasets/catalog",
        headers=operator_headers,
        params={"workspace_id": workspace.id, "limit": 1, "offset": 0},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert denied.status_code == 403
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    assert first_data["total"] == 2
    assert second_data["total"] == 2
    catalog_rows = first_data["items"] + second_data["items"]
    assert {(row["type"], row["id"]) for row in catalog_rows} == {
        ("qrdf", qrdf.id),
        ("native_lerobot", native_dataset.id),
    }
    native_row = next(row for row in catalog_rows if row["type"] == "native_lerobot")
    assert native_row["dataset_id"] == "api-demo"
    assert native_row["copy_status"] == "queued"
    assert "oss_uri" not in native_row


def test_native_catalog_includes_safe_batch_and_session_summary(client, admin_headers, db_session):
    actor, workspace, task_set = _scope_context(db_session)
    legacy_batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="legacy one-at-a-time import",
        batch_type="lerobot",
        status="ready",
        created_by_user_id=actor.id,
    )
    session_batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="session batch import",
        batch_type="lerobot",
        status="ready",
        created_by_user_id=actor.id,
    )
    db_session.add_all((legacy_batch, session_batch))
    db_session.flush()
    import_session = NativeLerobotImportSession(
        id=str(uuid4()),
        batch_id=session_batch.id,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        created_by_user_id=actor.id,
        status="completed",
        selected_count=1,
        registered_count=1,
    )
    db_session.add(import_session)
    legacy = NativeLerobotDataset(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=legacy_batch.id,
        name="legacy native dataset",
        source_oss_uri="oss://test-raw/prod/raw/robot/so101/legacy/",
        oss_uri="oss://test-export/platform/legacy/",
        robot_type="so101",
        dataset_id="legacy",
        file_count=1,
        total_size=1,
        completed_at=datetime(2026, 8, 28, 10, 0, 0),
        manifest_sha256="a" * 64,
        marker_sha256="b" * 64,
        copy_status="succeeded",
        created_by_user_id=actor.id,
    )
    imported = NativeLerobotDataset(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=session_batch.id,
        import_session_id=import_session.id,
        name="session native dataset",
        source_oss_uri="oss://test-raw/prod/raw/robot/so101/session/",
        oss_uri="oss://test-export/platform/session/",
        robot_type="so101",
        dataset_id="session",
        file_count=1,
        total_size=1,
        completed_at=datetime(2026, 8, 28, 10, 0, 0),
        manifest_sha256="c" * 64,
        marker_sha256="d" * 64,
        copy_status="succeeded",
        created_by_user_id=actor.id,
    )
    db_session.add_all((legacy, imported))
    db_session.commit()

    response = client.get(
        "/api/v1/datasets/catalog",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )

    assert response.status_code == 200, response.text
    rows = {
        row["id"]: row
        for row in response.json()["data"]["items"]
        if row["type"] == "native_lerobot"
    }
    assert rows[legacy.id]["batch_name"] == legacy_batch.name
    assert rows[legacy.id]["source_session"] == "historical_single_import"
    assert rows[imported.id]["batch_name"] == session_batch.name
    assert rows[imported.id]["source_session"] == "batch_import_session"
    assert "source_oss_uri" not in rows[imported.id]


def test_native_copy_retry_and_reauthorization_are_state_and_scope_gated(
    client, admin_headers, db_session, monkeypatch
):
    from data.services.native_lerobot_datasets import (
        discover_native_lerobot_candidates,
        register_native_lerobot_dataset,
    )

    actor, workspace, task_set = _scope_context(db_session)
    _install_marker_listing(monkeypatch)
    candidate = discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )[0]
    batch, native_dataset, original_job = register_native_lerobot_dataset(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="retryable API native dataset",
        candidate_token=candidate.token,
        actor_id=actor.id,
    )
    succeeded_sibling = NativeLerobotDataset(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        name="successful retry sibling",
        source_oss_uri="oss://test-raw/prod/raw/robot/so101/successful-sibling/",
        oss_uri="oss://test-export/platform/successful-sibling/",
        robot_type="so101",
        dataset_id="successful-sibling",
        file_count=1,
        total_size=1,
        completed_at=native_dataset.completed_at,
        manifest_sha256="e" * 64,
        marker_sha256="f" * 64,
        copy_status="succeeded",
        created_by_user_id=actor.id,
    )
    db_session.add(succeeded_sibling)
    native_dataset.copy_status = "failed"
    native_dataset.copy_error_code = "source_changed"
    original_job.status = "failed"
    original_job.phase = "failed"
    original_job.lease_token = ""
    batch.status = "partial_failed"
    db_session.commit()

    first_retry = client.post(
        f"/api/v1/native-lerobot-datasets/{native_dataset.id}/copy/retry",
        headers=admin_headers,
    )
    assert first_retry.status_code == 410, first_retry.text
    reauthorized = client.post(
        f"/api/v1/native-lerobot-datasets/{native_dataset.id}/source-reauthorization",
        headers=admin_headers,
        json={"candidate_token": "retired-candidate-token"},
    )
    assert reauthorized.status_code == 410, reauthorized.text
    assert (
        client.get(
            "/api/v1/batches/lerobot-candidates",
            headers=admin_headers,
            params={"workspace_id": workspace.id, "task_set_id": task_set.id},
        ).status_code
        == 404
    )
    db_session.refresh(native_dataset)
    assert native_dataset.copy_status == "failed"
    assert native_dataset.copy_error_code == "source_changed"
    db_session.refresh(original_job)
    assert original_job.status == "failed"
    db_session.refresh(succeeded_sibling)
    assert succeeded_sibling.status == "active"
    assert succeeded_sibling.copy_status == "succeeded"
