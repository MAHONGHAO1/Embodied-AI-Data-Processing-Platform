from __future__ import annotations

from uuid import uuid4

import pytest

from data.database import (
    Batch,
    CollectionDevice,
    TaskLabel,
    TaskSet,
    User,
    Workspace,
)
from data.services.import_sessions import create_import_session


def _batch_context(db_session):
    suffix = uuid4().hex[:10]
    actor = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    workspace = Workspace(name=f"collection resources {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"project {suffix}")
    label = TaskLabel(key=f"collection-{suffix}", name="close the box")
    db_session.add_all((project, label))
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="EGO collection",
        batch_type="ego",
        created_by_user_id=actor.id,
    )
    db_session.add(batch)
    db_session.flush()
    db_session.commit()
    return actor, workspace, batch, label


def test_collection_device_directory_is_workspace_scoped(client, admin_headers, db_session):
    _actor, workspace, _batch, _label = _batch_context(db_session)

    created = client.post(
        "/api/v1/collection-devices",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "name": "EGO Phone 01",
            "device_type": "phone",
            "model": "iPhone",
            "serial_number": " PHONE-SN-01 ",
        },
    )

    assert created.status_code == 200
    assert created.json()["data"] == {
        "id": created.json()["data"]["id"],
        "name": "EGO Phone 01",
        "device_type": "phone",
        "model": "iPhone",
        "serial_number": "PHONE-SN-01",
        "is_active": True,
    }
    listed = client.get(
        "/api/v1/collection-devices",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert listed.status_code == 200
    assert listed.json()["data"]["items"] == [created.json()["data"]]

    deactivated = client.patch(
        f"/api/v1/collection-devices/{created.json()['data']['id']}",
        headers=admin_headers,
        json={"is_active": False},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["data"]["is_active"] is False
    active_only = client.get(
        "/api/v1/collection-devices",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert active_only.json()["data"]["items"] == []

    reactivated = client.patch(
        f"/api/v1/collection-devices/{created.json()['data']['id']}",
        headers=admin_headers,
        json={"is_active": True},
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["data"]["is_active"] is True
    active_again = client.get(
        "/api/v1/collection-devices",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert [item["id"] for item in active_again.json()["data"]["items"]] == [
        created.json()["data"]["id"]
    ]


def test_import_requires_explicit_default_attribution_keys(db_session):
    actor, _workspace, batch, label = _batch_context(db_session)

    with pytest.raises(ValueError, match="task label is required"):
        create_import_session(
            db_session,
            batch_id=batch.id,
            import_type="chunked_upload",
            actor_id=actor.id,
        )
    explicit_unknown = create_import_session(
        db_session,
        batch_id=batch.id,
        import_type="chunked_upload",
        actor_id=actor.id,
        task_label_id=label.id,
        default_collector_profile_id=None,
        default_collection_device_id=None,
    )

    assert explicit_unknown.default_collector_profile_id is None
    assert explicit_unknown.default_collection_device_id is None


def test_cross_workspace_device_is_rejected(db_session):
    actor, _workspace, batch, label = _batch_context(db_session)
    foreign_workspace = Workspace(name=f"foreign device {uuid4().hex[:8]}", creator="test")
    db_session.add(foreign_workspace)
    db_session.flush()
    device = CollectionDevice(
        workspace_id=foreign_workspace.id,
        name="Foreign Phone",
        device_type="phone",
        serial_number=f"FOREIGN-{uuid4().hex[:8]}",
    )
    db_session.add(device)
    db_session.commit()

    with pytest.raises(ValueError, match="collection device is unavailable"):
        create_import_session(
            db_session,
            batch_id=batch.id,
            import_type="chunked_upload",
            actor_id=actor.id,
            task_label_id=label.id,
            default_collector_profile_id=None,
            default_collection_device_id=device.id,
        )
