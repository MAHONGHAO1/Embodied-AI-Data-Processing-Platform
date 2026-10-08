"""Collection task API and offline data package pre-generation."""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime
from decimal import Decimal

import pytest
from collection_api_fixtures import (
    make_collector,
    make_device_model,
    make_label,
    make_project,
    make_workspace,
)

from data.database import SessionLocal
from data.models.collection_core import CollectionTaskLabel
from data.models.data_package import DataPackage
from data.services.collection_projects import archive_collection_project
from data.services.collection_tasks import (
    ArchivedCollectionProjectError,
    create_collection_task,
    split_package_durations,
)


def test_split_package_durations_with_remainder():
    assert split_package_durations(Decimal("5.00"), Decimal("2.00")) == [
        Decimal("2.00"),
        Decimal("2.00"),
        Decimal("1.00"),
    ]


def test_split_package_durations_without_remainder():
    assert split_package_durations(Decimal("4.00"), Decimal("2.00")) == [
        Decimal("2.00"),
        Decimal("2.00"),
    ]


def test_split_package_durations_rejects_unbounded_allocation():
    with pytest.raises(ValueError, match="at most 10000"):
        split_package_durations(Decimal("99999999.99"), Decimal("0.01"))


@pytest.mark.parametrize(
    ("target", "default_package"),
    [
        (Decimal("0"), Decimal("2")),
        (Decimal("-1"), Decimal("2")),
        (Decimal("2"), Decimal("0")),
        (Decimal("2"), Decimal("-1")),
    ],
)
def test_split_package_durations_rejects_non_positive_values(target, default_package):
    with pytest.raises(ValueError, match="positive"):
        split_package_durations(target, default_package)


@pytest.mark.parametrize(
    ("target", "default_package"),
    [
        (Decimal("0.001"), Decimal("2.00")),
        (Decimal("1.234"), Decimal("2.00")),
        (Decimal("2.00"), Decimal("0.001")),
        (Decimal("2.00"), Decimal("1.234")),
        (Decimal("100000000.00"), Decimal("99999999.99")),
        (Decimal("2.00"), Decimal("100000000.00")),
    ],
)
def test_split_package_durations_rejects_values_outside_numeric_10_2(
    target,
    default_package,
):
    with pytest.raises(ValueError, match=r"NUMERIC\(10,2\)"):
        split_package_durations(target, default_package)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_duration_hours", "0.001"),
        ("target_duration_hours", "1.234"),
        ("target_duration_hours", "100000000.00"),
        ("default_package_duration_hours", "0.001"),
        ("default_package_duration_hours", "1.234"),
        ("default_package_duration_hours", "100000000.00"),
    ],
)
def test_create_task_rejects_duration_outside_numeric_10_2(
    client,
    db_session,
    admin_headers,
    field,
    value,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    body = {
        "workspace_id": workspace.id,
        "collection_project_id": project.id,
        "name": f"Invalid {field}",
        "target_duration_hours": "2.00",
        "default_package_duration_hours": "1.00",
        "label_ids": [],
    }
    if field == "target_duration_hours":
        body["default_package_duration_hours"] = "99999999.99"
    body[field] = value

    response = client.post(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        json=body,
    )

    assert response.status_code == 422


def test_create_task_pregenerates_packages_and_labels(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    scene = make_label(db_session, category="scene")
    purpose = make_label(db_session, category="purpose")

    response = client.post(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "name": " Pick cups ",
            "description": " demo ",
            "target_duration_hours": "5.00",
            "default_package_duration_hours": "2.00",
            "device_model_id": device.id,
            "label_ids": [scene.id, purpose.id],
            "sop_text": " wash hands first ",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["name"] == "Pick cups"
    assert data["description"] == "demo"
    assert data["sop_text"] == "wash hands first"
    assert data["capture_mode"] == "offline"
    assert data["package_count"] == 3
    assert [package["target_duration_hours"] for package in data["packages"]] == [
        "2.00",
        "2.00",
        "1.00",
    ]
    assert {package["status"] for package in data["packages"]} == {"pending_assignment"}
    assert all(package["package_uid"].startswith("pkg_") for package in data["packages"])
    assert len({package["package_uid"] for package in data["packages"]}) == 3

    persisted_labels = {
        row.collection_label_id
        for row in db_session.query(CollectionTaskLabel)
        .filter(CollectionTaskLabel.collection_task_id == data["id"])
        .all()
    }
    assert persisted_labels == {scene.id, purpose.id}


def test_create_task_rejects_archived_project(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    project.status = "archived"
    db_session.commit()

    response = client.post(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "name": "Too late",
            "target_duration_hours": "2.00",
            "label_ids": [],
        },
    )

    assert response.status_code in {409, 422}
    assert "archived" in response.json()["detail"]


def test_create_task_waits_for_concurrent_archive_and_rejects_new_task(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    workspace_id = workspace.id
    project_id = project.id

    archive_session = SessionLocal()
    archive_collection_project(
        archive_session,
        project_id=project_id,
        workspace_id=workspace_id,
    )

    def create_task():
        session = SessionLocal()
        try:
            try:
                create_collection_task(
                    session,
                    workspace_id=workspace_id,
                    collection_project_id=project_id,
                    name="archive race",
                    target_duration_hours=Decimal("2.00"),
                    label_ids=[],
                    created_by_user_id=None,
                )
                session.commit()
                return "created"
            except ArchivedCollectionProjectError:
                session.rollback()
                return "archived"
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(create_task)
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
    assert (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_project_id == project_id)
        .count()
        == 0
    )


def test_create_task_rejects_inactive_or_missing_labels(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    inactive = make_label(db_session, category="training")
    inactive.is_active = False
    db_session.commit()

    for label_id in (inactive.id, 2_147_483_647):
        response = client.post(
            "/api/v1/collection-tasks",
            headers=admin_headers,
            json={
                "workspace_id": workspace.id,
                "collection_project_id": project.id,
                "name": f"Invalid label {label_id}",
                "target_duration_hours": "2.00",
                "label_ids": [label_id],
            },
        )
        assert response.status_code == 422
        assert "label" in response.json()["detail"]


def test_list_and_get_task_include_intake_progress(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    created = client.post(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "name": "Progress",
            "target_duration_hours": "4.00",
            "default_package_duration_hours": "2.00",
            "label_ids": [],
        },
    )
    assert created.status_code == 200
    task_id = created.json()["data"]["id"]
    packages = (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == task_id)
        .order_by(DataPackage.id)
        .all()
    )
    responsible = make_collector(db_session, workspace, name="Responsible")
    operator = make_collector(db_session, workspace, name="Operator")
    packages[0].status = "assigned"
    packages[0].responsible_collector_id = responsible.id
    packages[0].operator_collector_id = operator.id
    packages[0].intake_valid_duration_hours = Decimal("1.50")
    packages[1].intake_valid_duration_hours = Decimal("0.25")
    db_session.commit()

    listed = client.get(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
        },
    )
    assert listed.status_code == 200
    item = next(row for row in listed.json()["data"]["items"] if row["id"] == task_id)
    assert item["target_duration_hours"] == "4.00"
    assert item["package_count"] == 2
    assert item["assigned_count"] == 1
    assert item["intake_valid_duration_hours"] == "1.75"

    detail = client.get(
        f"/api/v1/collection-tasks/{task_id}",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == task_id
    assert len(detail.json()["data"]["packages"]) == 2


def test_task_capture_period_spans_package_capture_starts(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    created = client.post(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "name": "Capture period",
            "target_duration_hours": "6.00",
            "default_package_duration_hours": "2.00",
            "label_ids": [],
        },
    )
    assert created.status_code == 200
    task = created.json()["data"]
    assert task["captured_started_from"] is None
    assert task["captured_started_to"] is None
    packages = (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == task["id"])
        .order_by(DataPackage.id)
        .all()
    )
    packages[0].captured_started_at = datetime(2026, 9, 3, 8, 0, 0)
    packages[1].captured_started_at = datetime(2026, 9, 1, 9, 30, 0)
    db_session.commit()

    listed = client.get(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert listed.status_code == 200
    item = next(row for row in listed.json()["data"]["items"] if row["id"] == task["id"])
    assert item["captured_started_from"].startswith("2026-09-01T09:30:00")
    assert item["captured_started_to"].startswith("2026-09-03T08:00:00")

    detail = client.get(
        f"/api/v1/collection-tasks/{task['id']}",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert data["captured_started_from"] == item["captured_started_from"]
    starts = [package["captured_started_at"] for package in data["packages"]]
    assert starts[0].startswith("2026-09-03T08:00:00")
    assert starts[2] is None
