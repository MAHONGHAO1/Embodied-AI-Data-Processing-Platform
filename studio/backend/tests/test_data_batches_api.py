"""Data batches: candidate selection, batch creation locking, and one-time package assignment."""

from datetime import datetime

from tests.collection_api_fixtures import (
    make_annotator_reviewer,
    make_project,
    make_workspace,
    seed_intake_approved_package,
    seed_package_pending_intake_review,
)

from data.models.data_batch import DataBatch, DataBatchPackage, DataBatchStageRun


def _create_batch_payload(workspace_id, package_ids, *, ann=None, rev=None, **overrides):
    body = {
        "workspace_id": workspace_id,
        "name": "Kitchen-1",
        "data_package_ids": package_ids,
        "label_ids": [],
        "integrity_check_enabled": False,
        "quality_check_enabled": False,
        "compliance_check_enabled": False,
        "annotation_enabled": bool(ann and rev),
        "annotator_user_ids": [ann.id] if ann else [],
        "reviewer_user_id": rev.id if rev else None,
        "review_mode": "single",
    }
    body.update(overrides)
    return body


def test_candidates_only_intake_approved_and_excludes_batched(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    approved = seed_intake_approved_package(db_session, workspace, project)
    pending, _ = seed_package_pending_intake_review(db_session, workspace, project)
    other = seed_intake_approved_package(db_session, workspace, project)

    before = client.get(
        "/api/v1/data-batches/candidates",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert before.status_code == 200
    before_ids = {item["id"] for item in before.json()["data"]["items"]}
    assert approved.id in before_ids
    assert other.id in before_ids
    assert pending.id not in before_ids

    created = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json=_create_batch_payload(workspace.id, [approved.id], name="Cand-Batch"),
    )
    assert created.status_code == 200

    after = client.get(
        "/api/v1/data-batches/candidates",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert after.status_code == 200
    after_ids = {item["id"] for item in after.json()["data"]["items"]}
    assert approved.id not in after_ids
    assert other.id in after_ids
    assert all(item["status"] == "intake_approved" for item in after.json()["data"]["items"])


def test_candidates_filter_by_task_label_ids(client, db_session, admin_headers):
    from tests.collection_api_fixtures import make_label, make_task

    from data.models.collection_core import CollectionTaskLabel

    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    modality = make_label(db_session, category="modality", name="ego-filter")
    other = make_label(db_session, category="modality", name="umi-filter")
    task_match = make_task(db_session, project)
    task_other = make_task(db_session, project)
    db_session.add(
        CollectionTaskLabel(collection_task_id=task_match.id, collection_label_id=modality.id)
    )
    db_session.add(
        CollectionTaskLabel(collection_task_id=task_other.id, collection_label_id=other.id)
    )
    db_session.commit()

    matched = seed_intake_approved_package(db_session, workspace, project)
    unmatched = seed_intake_approved_package(db_session, workspace, project)
    matched.collection_task_id = task_match.id
    unmatched.collection_task_id = task_other.id
    db_session.commit()

    filtered = client.get(
        "/api/v1/data-batches/candidates",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "label_ids": [modality.id]},
    )
    assert filtered.status_code == 200
    ids = {item["id"] for item in filtered.json()["data"]["items"]}
    assert matched.id in ids
    assert unmatched.id not in ids


def test_list_and_get_data_batches(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    p1 = seed_intake_approved_package(db_session, workspace, project)
    p2 = seed_intake_approved_package(db_session, workspace, project)

    created = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json=_create_batch_payload(workspace.id, [p1.id, p2.id], name="List-Batch"),
    )
    assert created.status_code == 200
    batch_id = created.json()["data"]["id"]

    listed = client.get(
        "/api/v1/data-batches",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert listed.status_code == 200
    items = listed.json()["data"]["items"]
    assert any(item["id"] == batch_id for item in items)
    match = next(item for item in items if item["id"] == batch_id)
    assert set(match["data_package_ids"]) == {p1.id, p2.id}

    detail = client.get(
        f"/api/v1/data-batches/{batch_id}",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert detail.status_code == 200
    payload = detail.json()["data"]
    assert payload["id"] == batch_id
    assert payload["name"] == "List-Batch"
    assert set(payload["data_package_ids"]) == {p1.id, p2.id}


def test_paginated_batch_list_supports_operational_filters(client, db_session, admin_headers):
    from tests.collection_api_fixtures import make_label

    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = seed_intake_approved_package(db_session, workspace, project)
    other_package = seed_intake_approved_package(db_session, workspace, project)
    labels = [
        make_label(db_session, category=category, name=f"batch-filter-{category}")
        for category in ("scene", "purpose", "training", "modality")
    ]

    created = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json=_create_batch_payload(
            workspace.id,
            [package.id],
            name="Filter-Match",
            label_ids=[label.id for label in labels],
            # Stage runs exist only when governance is enabled; the test sets their statuses.
            integrity_check_enabled=True,
        ),
    )
    assert created.status_code == 200
    batch_id = created.json()["data"]["id"]
    other = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json=_create_batch_payload(workspace.id, [other_package.id], name="Filter-Other"),
    )
    assert other.status_code == 200

    batch = db_session.get(DataBatch, batch_id)
    batch.annotation_enabled = True
    batch.status = "reviewing"
    package.upload_completed_at = datetime(2026, 6, 15, 12, 0, 0)
    other_package.upload_completed_at = datetime(2025, 1, 1, 0, 0, 0)
    for stage in db_session.query(DataBatchStageRun).filter_by(run_id=batch.governance_run.id):
        stage.status = {
            "integrity": "passed",
            "quality": "running",
            "compliance": "skipped",
        }[stage.stage]
    db_session.commit()

    response = client.get(
        "/api/v1/data-batches",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "page": 1,
            "size": 20,
            "scene_label_id": labels[0].id,
            "purpose_label_id": labels[1].id,
            "training_label_id": labels[2].id,
            "modality_label_id": labels[3].id,
            "integrity_status": "passed",
            "quality_status": "running",
            "compliance_status": "skipped",
            "annotation_status": "reviewing",
            "upload_completed_from": "2026-06-01T00:00:00",
            "upload_completed_to": "2026-06-30T23:59:59",
        },
    )

    assert response.status_code == 200
    result = response.json()["data"]
    assert result["total"] == 1
    assert result["items"][0]["id"] == batch_id
    assert result["items"][0]["annotation_status"] == "reviewing"
    assert result["items"][0]["upload_completed_at"].startswith("2026-06-15")


def test_paginated_batch_list_filters_by_collection_project(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    other_project = make_project(db_session, workspace)
    package = seed_intake_approved_package(db_session, workspace, project)
    other_package = seed_intake_approved_package(db_session, workspace, other_project)
    created = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json=_create_batch_payload(workspace.id, [package.id], name="Project-Match"),
    )
    assert created.status_code == 200
    other = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json=_create_batch_payload(workspace.id, [other_package.id], name="Project-Other"),
    )
    assert other.status_code == 200

    response = client.get(
        "/api/v1/data-batches",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "page": 1,
            "size": 20,
            "collection_project_id": project.id,
        },
    )

    assert response.status_code == 200
    result = response.json()["data"]
    assert result["total"] == 1
    assert result["items"][0]["id"] == created.json()["data"]["id"]


def test_create_batch_moves_packages_to_batched_and_rejects_reuse(
    client, db_session, admin_headers
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    p1 = seed_intake_approved_package(db_session, workspace, project)
    p2 = seed_intake_approved_package(db_session, workspace, project)
    # Create separate annotator/reviewer users and add them to workspace (fixture helper)
    ann, rev = make_annotator_reviewer(db_session, workspace)

    created = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json=_create_batch_payload(
            workspace.id,
            [p1.id, p2.id],
            ann=ann,
            rev=rev,
            name="Kitchen-1",
        ),
    )
    assert created.status_code == 200
    body = created.json()["data"]
    batch_id = body["id"]
    assert set(body["data_package_ids"]) == {p1.id, p2.id}

    membership_ids = {
        row.data_package_id
        for row in db_session.query(DataBatchPackage).filter_by(data_batch_id=batch_id)
    }
    assert membership_ids == {p1.id, p2.id}

    db_session.refresh(p1)
    db_session.refresh(p2)
    assert p1.status == "batched"
    assert p2.status == "batched"

    again = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json=_create_batch_payload(
            workspace.id,
            [p1.id],
            name="Kitchen-2",
        ),
    )
    assert again.status_code == 409
    assert "already_batched" in again.json()["detail"]
    assert isinstance(batch_id, int)


def test_create_batch_rejects_dual_review_mode(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = seed_intake_approved_package(db_session, workspace, project)

    response = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json=_create_batch_payload(
            workspace.id,
            [package.id],
            name="Dual-Reject",
            review_mode="dual",
        ),
    )
    assert response.status_code == 422
    assert "review_mode" in str(response.json()).lower()
