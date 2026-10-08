"""Annotation and review follow assignment, not workspace membership."""

from __future__ import annotations

from tests.collection_api_fixtures import make_project, seed_intake_approved_package
from tests.test_annotation_work_items_api import (
    _create_annotated_batch,
    _headers_for,
    _make_annotator,
    _make_reviewer,
)

from data.database import WorkspaceMember
from data.models.annotation_work import AnnotationWorkItem, ReviewWorkItem
from data.services.annotation_work_items import assign_work_items_for_batch
from data.services.data_batches import create_data_batch


def _get(client, path, headers, **params):
    response = client.get(f"/api/v1/{path}", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _drop_memberships(db, user):
    db.query(WorkspaceMember).filter(WorkspaceMember.user_id == user.id).delete()
    db.commit()


def test_workers_see_assigned_items_across_workspaces_without_membership(client, db_session):
    ws, batch, _, annotators, reviewer = _create_annotated_batch(db_session)
    other_ws, other_batch, _, _, _ = _create_annotated_batch(db_session)
    assign_work_items_for_batch(db_session, batch_id=batch.id)
    other_items = assign_work_items_for_batch(db_session, batch_id=other_batch.id)
    worker, outsider = annotators
    moved = other_items[0]
    moved.assignee_user_id = worker.id
    db_session.query(ReviewWorkItem).filter_by(annotation_work_item_id=moved.id).update(
        {"assignee_user_id": reviewer.id}
    )
    db_session.commit()
    _drop_memberships(db_session, worker)
    _drop_memberships(db_session, reviewer)
    mine = {
        item.id
        for item in db_session.query(AnnotationWorkItem).filter_by(assignee_user_id=worker.id)
    }

    everything = _get(client, "annotation-work-items", _headers_for(worker))
    assert {row["id"] for row in everything["items"]} == mine
    assert moved.id in mine and len({row["workspace_id"] for row in everything["items"]}) == 2
    names = {row["workspace_id"]: row["workspace_name"] for row in everything["items"]}
    assert names[ws.id] == ws.name and names[other_ws.id] == other_ws.name
    assert {row["id"] for row in everything["workspaces"]} == {ws.id, other_ws.id}

    filtered = _get(client, "annotation-work-items", _headers_for(worker), workspace_id=other_ws.id)
    assert [row["id"] for row in filtered["items"]] == [moved.id]

    workbench = client.get(
        f"/api/v1/annotation-work-items/{moved.id}/workbench",
        headers=_headers_for(worker),
        params={"workspace_id": other_ws.id},
    )
    assert workbench.status_code == 200, workbench.text
    # Assignment still authorizes: another annotator cannot open the item.
    foreign = client.get(
        f"/api/v1/annotation-work-items/{moved.id}/workbench",
        headers=_headers_for(outsider),
        params={"workspace_id": other_ws.id},
    )
    assert foreign.status_code == 403
    # The workspace still locates the item.
    misplaced = client.get(
        f"/api/v1/annotation-work-items/{moved.id}/workbench",
        headers=_headers_for(worker),
        params={"workspace_id": ws.id},
    )
    assert misplaced.status_code == 404

    reviews = _get(client, "review-work-items", _headers_for(reviewer))
    assert {row["workspace_id"] for row in reviews["items"]} == {ws.id, other_ws.id}
    assert {row["id"] for row in reviews["workspaces"]} == {ws.id, other_ws.id}


def test_admin_lists_every_workspace_without_membership(client, db_session, admin_headers):
    ws, batch, _, _, _ = _create_annotated_batch(db_session, durations=("1.00",))
    assign_work_items_for_batch(db_session, batch_id=batch.id)
    db_session.commit()
    listed = _get(client, "annotation-work-items", admin_headers, workspace_id=ws.id)
    assert listed["total"] == 1
    assert ws.id in {
        row["id"] for row in _get(client, "review-work-items", admin_headers)["workspaces"]
    }


def test_reassign_and_batch_creation_accept_non_member_workers(client, db_session, admin_headers):
    ws, batch, _, _, _ = _create_annotated_batch(db_session, durations=("1.00",))
    item = assign_work_items_for_batch(db_session, batch_id=batch.id)[0]
    outsider = _make_annotator(db_session, ws)
    outside_reviewer = _make_reviewer(db_session, ws)
    _drop_memberships(db_session, outsider)
    _drop_memberships(db_session, outside_reviewer)

    reassigned = client.post(
        f"/api/v1/annotation-work-items/{item.id}/reassign",
        headers=admin_headers,
        json={"workspace_id": ws.id, "to_user_id": outsider.id, "reason": "cross-workspace"},
    )
    assert reassigned.status_code == 200, reassigned.text
    assert reassigned.json()["data"]["assignee_user_id"] == outsider.id

    project = make_project(db_session, ws)
    package = seed_intake_approved_package(db_session, ws, project)
    new_batch, _ = create_data_batch(
        db_session,
        workspace_id=ws.id,
        name="Non-member workers",
        data_package_ids=[package.id],
        label_ids=[],
        integrity_check_enabled=False,
        quality_check_enabled=False,
        compliance_check_enabled=False,
        annotation_enabled=True,
        annotator_user_ids=[outsider.id],
        reviewer_user_id=outside_reviewer.id,
        review_mode="single",
        created_by_user_id=None,
    )
    assert new_batch.annotator_user_ids_json == [outsider.id]
    assert new_batch.reviewer_user_id == outside_reviewer.id
