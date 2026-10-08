"""Review work items: return requires reason, approval advances batch status, and reassignment."""

from __future__ import annotations

from uuid import uuid4

from data.database import User, WorkspaceMember
from data.services.annotation_work_items import assign_work_items_for_batch
from data.utils.helpers import create_access_token, hash_password


def _headers_for(user: User) -> dict[str, str]:
    token = create_access_token(user.id, user.email, user.role)
    return {"Authorization": f"Bearer {token}"}


def _make_user(db, workspace, *, role: str) -> User:
    user = User(
        email=f"{role}-{uuid4().hex}@t.com",
        password_hash=hash_password(f"{role}-pass-123"),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id))
    db.commit()
    db.refresh(user)
    return user


def _seed_submitted_review(db, source=None):
    from tests.test_annotation_work_items_api import (
        _create_annotated_batch,
        _qrdf_annotation_payload,
    )

    from data.services.annotation_work_items import (
        save_annotation_draft,
        submit_annotation_work_item,
    )

    workspace, batch, _packages, annotators, reviewer = _create_annotated_batch(
        db, durations=("1.00",)
    )
    ann_item = assign_work_items_for_batch(db, batch_id=batch.id)[0]
    annotator = next(actor for actor in annotators if actor.id == ann_item.assignee_user_id)
    payload = {
        "episodes": {
            str(member["episode_id"]): _qrdf_annotation_payload(member["episode_id"])
            for member in ann_item.episode_members_json
        }
    }
    if source:
        payload["source"] = source
    save_annotation_draft(
        db, workspace_id=workspace.id, item_id=ann_item.id, actor=annotator, draft_json=payload
    )
    submit_annotation_work_item(db, workspace_id=workspace.id, item_id=ann_item.id, actor=annotator)
    db.commit()
    return workspace, batch, ann_item, ann_item.review_item, annotator, reviewer


def test_return_requires_reason_and_reopens_annotation(client, db_session):
    workspace, batch, ann_item, rev_item, annotator, reviewer = _seed_submitted_review(db_session)
    headers = _headers_for(reviewer)

    missing = client.post(
        f"/api/v1/review-work-items/{rev_item.id}/return",
        headers=headers,
        json={"workspace_id": workspace.id, "reason": ""},
    )
    assert missing.status_code == 422

    returned = client.post(
        f"/api/v1/review-work-items/{rev_item.id}/return",
        headers=headers,
        json={"workspace_id": workspace.id, "reason": "labels incomplete"},
    )
    assert returned.status_code == 200
    assert returned.json()["data"]["status"] == "returned"
    assert returned.json()["data"]["reason"] == "labels incomplete"

    db_session.refresh(ann_item)
    assert ann_item.status == "returned"
    assert ann_item.return_reason == "labels incomplete"

    listed = client.get(
        "/api/v1/annotation-work-items",
        headers=_headers_for(annotator),
        params={"workspace_id": workspace.id},
    )
    assert listed.status_code == 200
    ann_payload = next(item for item in listed.json()["data"]["items"] if item["id"] == ann_item.id)
    assert ann_payload["return_reason"] == "labels incomplete"
    assert ann_payload["status"] == "returned"


def test_approve_moves_batch_to_publishing_when_all_done(client, db_session, monkeypatch):
    workspace, batch, ann_item, rev_item, _annotator, reviewer = _seed_submitted_review(db_session)
    called: list[int] = []

    def _stub(batch_id: int) -> None:
        called.append(batch_id)

    monkeypatch.setattr(
        "data.routers.review_work_items.schedule_asset_creation",
        _stub,
    )

    approved = client.post(
        f"/api/v1/review-work-items/{rev_item.id}/approve",
        headers=_headers_for(reviewer),
        json={"workspace_id": workspace.id},
    )
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "approved"

    db_session.refresh(ann_item)
    db_session.refresh(batch)
    assert ann_item.status == "done"
    assert batch.status == "publishing"
    assert called == [batch.id]

    again = client.post(
        f"/api/v1/review-work-items/{rev_item.id}/approve",
        headers=_headers_for(reviewer),
        json={"workspace_id": workspace.id},
    )
    assert again.status_code == 409


def test_admin_non_assignee_cannot_approve(client, db_session, admin_headers):
    workspace, _batch, _ann_item, rev_item, _annotator, _reviewer = _seed_submitted_review(
        db_session
    )
    denied = client.post(
        f"/api/v1/review-work-items/{rev_item.id}/approve",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert denied.status_code == 403


def test_review_reassign_rejects_previous_reviewer(client, db_session, admin_headers):
    workspace, _batch, _ann_item, rev_item, _annotator, reviewer = _seed_submitted_review(
        db_session
    )
    replacement = _make_user(db_session, workspace, role="auditor")

    reassigned = client.post(
        f"/api/v1/review-work-items/{rev_item.id}/reassign",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "to_user_id": replacement.id,
            "reason": "ooo coverage",
        },
    )
    assert reassigned.status_code == 200
    assert reassigned.json()["data"]["assignee_user_id"] == replacement.id

    denied = client.post(
        f"/api/v1/review-work-items/{rev_item.id}/approve",
        headers=_headers_for(reviewer),
        json={"workspace_id": workspace.id},
    )
    assert denied.status_code == 403


def test_review_list_exposes_algorithm_source_and_confidence(client, db_session):
    workspace, batch, ann_item, rev_item, annotator, reviewer = _seed_submitted_review(
        db_session,
        source={
            "kind": "algorithm",
            "name": "ego-vl",
            "version": "1.3.0",
            "run_id": "run-001",
            "confidence": 0.82,
        },
    )
    # Later mutable drafts must not replace the fixed provenance under review.
    ann_item.draft_json = {"source": {"kind": "different", "confidence": 0.01}}
    db_session.commit()

    listed = client.get(
        "/api/v1/review-work-items",
        headers=_headers_for(reviewer),
        params={"workspace_id": workspace.id},
    )
    assert listed.status_code == 200
    payload = next(item for item in listed.json()["data"]["items"] if item["id"] == rev_item.id)
    assert payload["source"]["kind"] == "algorithm"
    assert payload["confidence"] == 0.82


def test_review_list_omits_source_when_missing(client, db_session):
    workspace, _batch, ann_item, rev_item, _annotator, reviewer = _seed_submitted_review(db_session)
    ann_item.draft_json = {"mode": "whole"}
    db_session.commit()

    listed = client.get(
        "/api/v1/review-work-items",
        headers=_headers_for(reviewer),
        params={"workspace_id": workspace.id},
    )
    payload = next(item for item in listed.json()["data"]["items"] if item["id"] == rev_item.id)
    assert payload["source"] is None
    assert payload["confidence"] is None
