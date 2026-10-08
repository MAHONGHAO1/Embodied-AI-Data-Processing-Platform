"""Unassigned data package listing and adjustment API."""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from decimal import Decimal
from threading import Barrier

import pytest
from collection_api_fixtures import (
    make_collector,
    make_device_model,
    make_project,
    make_workspace,
    seed_package_pending_intake_review,
)

from data.database import SessionLocal
from data.models.data_package import DataPackage, PackageIntakeReview
from data.services import collection_packages
from data.services.collection_packages import (
    AssignmentLockedError,
    adjust_pending_packages,
)
from data.services.collection_projects import archive_collection_project
from data.services.collection_tasks import (
    ArchivedCollectionProjectError,
    create_collection_task,
)


def _create_task_with_packages(db_session, *, workspace, project, device, name, hours="4.00"):
    task = create_collection_task(
        db_session,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=name,
        target_duration_hours=Decimal(hours),
        default_package_duration_hours=Decimal("2.00"),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    db_session.commit()
    return task


def test_list_packages_filters_by_project_task_and_status(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    other_project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="listed",
    )
    _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=other_project,
        device=device,
        name="not-listed",
        hours="2.00",
    )

    response = client.get(
        "/api/v1/data-packages",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "collection_task_id": task.id,
            "status": "pending_assignment",
        },
    )

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 2
    assert {item["collection_project_id"] for item in items} == {project.id}
    assert {item["collection_task_id"] for item in items} == {task.id}
    assert {item["status"] for item in items} == {"pending_assignment"}


def test_get_package_detail_lists_episodes_without_storage_uris(
    client,
    db_session,
    admin_headers,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session,
        workspace,
        project,
        episode_hours=(1.0, 1.0),
    )
    package.qrdf_facts_json = {
        "capture": {"mode": "ego"},
        "preview": {"available": False},
        "internal_path": "/collection-uploads/should-not-leak",
        "storage": {
            "uri": "s3://private-bucket/raw",
            "bucket": "private-bucket",
            "object_key": "raw/private",
            "bucketName": "camel-private-bucket",
            "objectKey": "raw/camel-private.mcap",
            "uploadId": "private-upload-id",
            "accessKey": "private-access-key",
        },
        "locations": [
            "s3://private-bucket/raw/object.mcap",
            "oss://private-bucket/raw/object.mcap",
            "nas://private-bucket/raw/object.mcap",
            "file:///private/raw/object.mcap",
            "gs://private-bucket/raw/object.mcap",
            "azure://private-container/raw/object.mcap",
            "http://objects.example/private/raw/object.mcap",
            "https://objects.example/private/raw/object.mcap",
        ],
    }
    db_session.add(
        PackageIntakeReview(
            data_package_id=package.id,
            reviewer_user_id=None,
            verdict="approved",
            is_bulk=False,
            rejected_episode_ids_json=[],
            reason="looks good",
        )
    )
    db_session.commit()

    detail = client.get(
        f"/api/v1/data-packages/{package.id}",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )

    assert detail.status_code == 200
    payload = detail.json()["data"]
    assert len(payload["episodes"]) == 2
    assert {row["episode_uid"] for row in payload["episodes"]} == {
        episodes[0].episode_uid,
        episodes[1].episode_uid,
    }
    assert payload["episodes"][0] == {
        "id": episodes[0].id,
        "episode_uid": episodes[0].episode_uid,
        "external_episode_id": None,
        "validity_status": "valid",
        "modality": "rgb",
        "duration_hours": "1.00",
        "privacy_sensitive": False,
        "preview_available": True,
        "admission_status": "passed",
        "integrity_source": "server",
    }
    assert payload["qrdf_facts"] == {
        "capture": {"mode": "ego"},
        "preview": {"available": False},
        "locations": [],
        "storage": {},
    }
    assert payload["intake_review"]["verdict"] == "approved"
    assert payload["intake_review"]["reason"] == "looks good"
    blob = str(payload)
    assert "source_fingerprint" not in blob
    assert "s3://" not in blob
    assert "oss://" not in blob
    assert "nas://" not in blob
    assert "file://" not in blob
    assert "gs://" not in blob
    assert "azure://" not in blob
    assert "http://objects.example/" not in blob
    assert "https://objects.example/" not in blob
    assert "bucketName" not in blob
    assert "objectKey" not in blob
    assert "uploadId" not in blob
    assert "accessKey" not in blob
    assert "/collection-uploads/" not in blob
    assert "internal_path" not in blob


def test_adjust_can_resize_add_and_delete_pending_packages(
    client,
    db_session,
    admin_headers,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="adjust-me",
    )
    pending = (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == task.id)
        .order_by(DataPackage.id.asc())
        .all()
    )

    response = client.post(
        "/api/v1/data-packages/adjust",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_task_id": task.id,
            "operations": [
                {
                    "op": "resize",
                    "data_package_id": pending[0].id,
                    "target_duration_hours": "1.50",
                },
                {"op": "delete", "data_package_id": pending[1].id},
                {"op": "add", "target_duration_hours": "2.00"},
            ],
        },
    )

    assert response.status_code == 200
    packages = response.json()["data"]["packages"]
    assert len(packages) == 2
    assert {row["target_duration_hours"] for row in packages} == {"1.50", "2.00"}
    assert all(row["status"] == "pending_assignment" for row in packages)
    assert all(row["package_uid"].startswith("pkg_") for row in packages)


def test_adjust_rejects_assigned_package(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="locked",
        hours="2.00",
    )
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    owner = make_collector(db_session, workspace, name="Owner")
    operator = make_collector(db_session, workspace, name="Op")
    package.status = "assigned"
    package.responsible_collector_id = owner.id
    package.operator_collector_id = operator.id
    db_session.commit()

    response = client.post(
        "/api/v1/data-packages/adjust",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_task_id": task.id,
            "operations": [{"op": "delete", "data_package_id": package.id}],
        },
    )

    assert response.status_code == 409
    assert "assignment_locked" in response.json()["detail"]


def test_adjust_adds_replacement_after_assigned_package_is_voided(
    client,
    db_session,
    admin_headers,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="replacement",
        hours="2.00",
    )
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    original_package_uid = package.package_uid
    owner = make_collector(db_session, workspace, name="Owner")
    assigned = client.post(
        f"/api/v1/data-packages/{package.id}/assign",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "responsible_collector_id": owner.id,
            "operator_collector_id": owner.id,
        },
    )
    assert assigned.status_code == 200
    voided = client.post(
        f"/api/v1/data-packages/{package.id}/void",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "reason": "wrong assignment"},
    )
    assert voided.status_code == 200

    response = client.post(
        "/api/v1/data-packages/adjust",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_task_id": task.id,
            "operations": [{"op": "add", "target_duration_hours": "1.00"}],
        },
    )

    assert response.status_code == 200
    packages = response.json()["data"]["packages"]
    replacement = next(row for row in packages if row["status"] == "pending_assignment")
    assert replacement["package_uid"] != original_package_uid


def test_adjust_add_rejects_archived_project(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="archived",
        hours="2.00",
    )
    project.status = "archived"
    db_session.commit()

    response = client.post(
        "/api/v1/data-packages/adjust",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_task_id": task.id,
            "operations": [{"op": "add", "target_duration_hours": "1.00"}],
        },
    )

    assert response.status_code == 409
    assert "archived" in response.json()["detail"]


def test_adjust_add_waits_for_concurrent_archive_and_rejects_new_package(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="archive-race",
        hours="2.00",
    )
    workspace_id = workspace.id
    project_id = project.id
    task_id = task.id

    archive_session = SessionLocal()
    archive_collection_project(
        archive_session,
        project_id=project_id,
        workspace_id=workspace_id,
    )

    def add_package():
        session = SessionLocal()
        try:
            try:
                adjust_pending_packages(
                    session,
                    workspace_id=workspace_id,
                    collection_task_id=task_id,
                    operations=[{"op": "add", "target_duration_hours": Decimal("1.00")}],
                )
                session.commit()
                return "added"
            except ArchivedCollectionProjectError:
                session.rollback()
                return "archived"
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(add_package)
            try:
                with pytest.raises(FutureTimeoutError):
                    future.result(timeout=0.25)
            finally:
                archive_session.commit()
            assert future.result(timeout=5) == "archived"
    finally:
        archive_session.rollback()
        archive_session.close()

    db_session.expire_all()
    packages = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task_id).all()
    assert len(packages) == 1


def test_assign_returns_manifest_and_locks_package(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="assign-me",
        hours="2.00",
    )
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    owner = make_collector(db_session, workspace, name="Owner")
    payload = {
        "workspace_id": workspace.id,
        "responsible_collector_id": owner.id,
        "operator_collector_id": owner.id,
    }

    assigned = client.post(
        f"/api/v1/data-packages/{package.id}/assign",
        headers=admin_headers,
        json=payload,
    )

    assert assigned.status_code == 200
    body = assigned.json()["data"]
    assert body["status"] == "assigned"
    assert body["responsible_collector_id"] == owner.id
    assert body["operator_collector_id"] == owner.id
    assert body["assigned_at"].endswith("Z")
    assert body["offline_manifest"] == {
        "schema_version": 1,
        "collector_id": owner.id,
        "package_uid": package.package_uid,
        "workspace_id": workspace.id,
        "collection_project_id": project.id,
        "collection_task_id": task.id,
        "target_duration_hours": "2.00",
        "responsible_collector_id": owner.id,
        "operator_collector_id": owner.id,
        "device_model_id": device.id,
        "assigned_at": body["assigned_at"],
    }

    manifest = client.get(
        f"/api/v1/data-packages/{package.id}/offline-manifest",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert manifest.status_code == 200
    assert manifest.json()["data"] == body["offline_manifest"]

    again = client.post(
        f"/api/v1/data-packages/{package.id}/assign",
        headers=admin_headers,
        json=payload,
    )
    assert again.status_code == 409
    assert "assignment_locked" in again.json()["detail"]


def test_concurrent_assign_allows_one_winner_and_preserves_its_collectors(
    db_session,
    monkeypatch,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="concurrent-assign",
        hours="2.00",
    )
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    collectors = [
        (
            make_collector(db_session, workspace, name="Owner A"),
            make_collector(db_session, workspace, name="Operator A"),
        ),
        (
            make_collector(db_session, workspace, name="Owner B"),
            make_collector(db_session, workspace, name="Operator B"),
        ),
    ]
    package_id = package.id
    workspace_id = workspace.id
    collector_ids = [(owner.id, owner.id) for owner, operator in collectors]

    original_require = collection_packages._require_workspace_package
    stale_read_barrier = Barrier(2)

    def synchronize_stale_reads(*args, **kwargs):
        loaded = original_require(*args, **kwargs)
        if loaded.status == "pending_assignment":
            stale_read_barrier.wait(timeout=5)
        return loaded

    monkeypatch.setattr(
        collection_packages,
        "_require_workspace_package",
        synchronize_stale_reads,
    )

    def assign(pair):
        session = SessionLocal()
        try:
            try:
                collection_packages.assign_data_package(
                    session,
                    workspace_id=workspace_id,
                    data_package_id=package_id,
                    responsible_collector_id=pair[0],
                    operator_collector_id=pair[1],
                )
                session.commit()
                return "ok", pair
            except AssignmentLockedError:
                session.rollback()
                return "assignment_locked", pair
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(assign, collector_ids))

    assert sorted(result[0] for result in results) == ["assignment_locked", "ok"]
    winner = next(pair for status, pair in results if status == "ok")
    db_session.expire_all()
    persisted = db_session.get(DataPackage, package_id)
    assert (persisted.responsible_collector_id, persisted.operator_collector_id) == winner


def test_assign_rejects_inactive_or_other_workspace_collectors(
    client,
    db_session,
    admin_headers,
):
    workspace = make_workspace(db_session)
    other_workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="collector-validation",
    )
    packages = (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == task.id)
        .order_by(DataPackage.id.asc())
        .all()
    )
    make_collector(db_session, workspace, name="Active")
    inactive = make_collector(db_session, workspace, name="Inactive")
    inactive.is_active = False
    outsider = make_collector(db_session, other_workspace, name="Outsider")
    db_session.commit()

    inactive_response = client.post(
        f"/api/v1/data-packages/{packages[0].id}/assign",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collector_id": inactive.id,
        },
    )
    assert inactive_response.status_code == 422
    assert "active" in inactive_response.json()["detail"]

    outsider_response = client.post(
        f"/api/v1/data-packages/{packages[1].id}/assign",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collector_id": outsider.id,
        },
    )
    assert outsider_response.status_code == 422
    assert "workspace" in outsider_response.json()["detail"]


def test_offline_manifest_rejects_pending_and_voided_packages(
    client,
    db_session,
    admin_headers,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="manifest-states",
    )
    packages = (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == task.id)
        .order_by(DataPackage.id.asc())
        .all()
    )
    owner = make_collector(db_session, workspace, name="Owner")
    operator = make_collector(db_session, workspace, name="Operator")
    packages[1].status = "voided"
    packages[1].responsible_collector_id = owner.id
    packages[1].operator_collector_id = operator.id
    db_session.commit()

    for package in packages:
        response = client.get(
            f"/api/v1/data-packages/{package.id}/offline-manifest",
            headers=admin_headers,
            params={"workspace_id": workspace.id},
        )
        assert response.status_code == 409


def test_void_assigned_package_does_not_autotop_up(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="void-me",
        hours="2.00",
    )
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    owner = make_collector(db_session, workspace, name="Owner")
    assigned = client.post(
        f"/api/v1/data-packages/{package.id}/assign",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "responsible_collector_id": owner.id,
            "operator_collector_id": owner.id,
        },
    )
    assert assigned.status_code == 200
    before = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).count()

    voided = client.post(
        f"/api/v1/data-packages/{package.id}/void",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "reason": "wrong collector"},
    )

    assert voided.status_code == 200
    assert voided.json()["data"]["status"] == "voided"
    after = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).count()
    assert after == before


def test_void_rejects_uploading_package(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = _create_task_with_packages(
        db_session,
        workspace=workspace,
        project=project,
        device=device,
        name="uploading",
        hours="2.00",
    )
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    owner = make_collector(db_session, workspace, name="Owner")
    operator = make_collector(db_session, workspace, name="Operator")
    package.status = "uploading"
    package.responsible_collector_id = owner.id
    package.operator_collector_id = operator.id
    db_session.commit()

    response = client.post(
        f"/api/v1/data-packages/{package.id}/void",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "reason": "too late"},
    )

    assert response.status_code == 409
    assert "cannot be voided" in response.json()["detail"]
