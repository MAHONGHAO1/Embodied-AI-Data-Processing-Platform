"""One final intake, linked ordinary replacement, and immutable original progress."""

from decimal import Decimal
from uuid import uuid4

from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_package_pending_intake_review,
)

from data.models.collection_core import CollectionTask
from data.models.data_package import DataPackage, PackageIntakeReview


def test_single_final_intake_and_new_supplement(client, db_session, admin_headers):
    ws = make_workspace(db_session)
    project = make_project(db_session, ws)
    original, episodes = seed_package_pending_intake_review(
        db_session, ws, project, episode_hours=(8, 2)
    )
    decision = {
        "workspace_id": ws.id,
        "verdict": "approved",
        "rejected_episode_ids": [episodes[1].id],
    }
    path = f"/api/v1/data-packages/{original.id}/intake-review"
    first = client.post(path, json=decision, headers=admin_headers)
    assert first.status_code == 200, first.text
    assert first.json()["data"]["intake_valid_duration_hours"] == "8.00"
    assert client.post(path, json=decision, headers=admin_headers).json() == first.json()
    changed = client.post(
        path, json={**decision, "rejected_episode_ids": []}, headers=admin_headers
    )
    assert changed.status_code == 409
    request = {
        "workspace_id": ws.id,
        "target_duration_hours": "2.00",
        "reason": "补足不合格时长",
        "client_request_id": str(uuid4()),
    }
    route = f"/api/v1/data-packages/{original.id}/supplements"
    created = client.post(route, json=request, headers=admin_headers)
    assert created.status_code == 200, created.text
    body = created.json()["data"]
    assert body["status"] == "pending_assignment"
    assert body["supplement_for_package_id"] == original.id
    assert body["collection_task_id"] == original.collection_task_id
    assert client.post(route, json=request, headers=admin_headers).json() == created.json()
    assert (
        client.post(
            route, json={**request, "target_duration_hours": "3.00"}, headers=admin_headers
        ).status_code
        == 409
    )
    db_session.expire_all()
    assert db_session.get(
        CollectionTask, original.collection_task_id
    ).target_duration_hours == Decimal("10.00")
    assert db_session.get(DataPackage, original.id).intake_valid_duration_hours == Decimal("8.00")
    assert db_session.query(PackageIntakeReview).filter_by(data_package_id=original.id).count() == 1


def test_supplement_requires_final_review_and_same_workspace(client, db_session, admin_headers):
    ws = make_workspace(db_session)
    original, _ = seed_package_pending_intake_review(db_session, ws, make_project(db_session, ws))
    body = {
        "workspace_id": ws.id,
        "target_duration_hours": "1.00",
        "reason": "test",
        "client_request_id": str(uuid4()),
    }
    assert (
        client.post(
            f"/api/v1/data-packages/{original.id}/supplements", json=body, headers=admin_headers
        ).status_code
        == 409
    )
