"""Algorithms submit annotation results in batches, idempotently."""

from uuid import uuid4


def _seed_assigned_item(db_session, actor_id):
    actor_id = int(actor_id)
    from tests.test_annotation_work_items_api import _create_annotated_batch

    from data.database import WorkspaceMember
    from data.services.annotation_work_items import assign_work_items_for_batch

    workspace, batch, _packages, _annotators, _reviewer = _create_annotated_batch(
        db_session, durations=("1.00",)
    )
    item = assign_work_items_for_batch(db_session, batch_id=batch.id)[0]
    item.assignee_user_id = actor_id
    if (
        not db_session.query(WorkspaceMember)
        .filter_by(workspace_id=workspace.id, user_id=actor_id)
        .first()
    ):
        db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=actor_id))
    db_session.commit()
    return workspace, item


def _payload(db, item):
    from tests.test_annotation_work_items_api import _qrdf_annotation_payload

    return {
        "episodes": {
            str(member["episode_id"]): _qrdf_annotation_payload(member["episode_id"])
            for member in item.episode_members_json
        }
    }


def _body(client, db, headers):
    actor_id = client.get("/api/v1/auth/me", headers=headers).json()["data"]["id"]
    workspace, item = _seed_assigned_item(db, actor_id)
    return item, {
        "workspace_id": workspace.id,
        "client_request_id": str(uuid4()),
        "items": [
            {
                "work_item_id": item.id,
                "payload": _payload(db, item),
                "source": {"kind": "algorithm", "name": "ego-vl"},
                "review_required": True,
            }
        ],
    }


def test_batch_submit_is_idempotent_and_records_the_source(client, db_session, annotator_headers):
    from data.database import EpisodeAnnotation

    item, body = _body(client, db_session, annotator_headers)
    first = client.post(
        "/api/v1/annotation-work-items/batch-submit", json=body, headers=annotator_headers
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/v1/annotation-work-items/batch-submit", json=body, headers=annotator_headers
    )
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    ids = [int(k) for k in body["items"][0]["payload"]["episodes"]]
    assert db_session.query(EpisodeAnnotation).filter(
        EpisodeAnnotation.episode_id.in_(ids)
    ).count() == len(ids)
    body["items"][0]["source"]["name"] = "different-algorithm"
    changed = client.post(
        "/api/v1/annotation-work-items/batch-submit", json=body, headers=annotator_headers
    )
    assert changed.status_code == 409, changed.text


def test_batch_submit_downgrades_skip_review_without_permission(
    client, db_session, annotator_headers
):
    from data.models.annotation_work import ReviewWorkItem

    item, body = _body(client, db_session, annotator_headers)
    body["items"][0]["review_required"] = False
    response = client.post(
        "/api/v1/annotation-work-items/batch-submit", json=body, headers=annotator_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["items"][0]["review_required"] is True
    assert (
        db_session.query(ReviewWorkItem).filter_by(annotation_work_item_id=item.id).one().status
        == "assigned"
    )


def test_authorized_skip_completes_real_work(client, db_session, admin_headers):
    from data.models.annotation_work import ReviewWorkItem

    item, body = _body(client, db_session, admin_headers)
    body["items"][0]["review_required"] = False
    response = client.post(
        "/api/v1/annotation-work-items/batch-submit", json=body, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["items"][0]["status"] == "done"
    assert (
        db_session.query(ReviewWorkItem).filter_by(annotation_work_item_id=item.id).one().status
        == "approved"
    )


def test_failed_batch_rolls_back_revisions_and_key_can_retry(client, db_session, annotator_headers):
    from data.database import EpisodeAnnotation

    item, body = _body(client, db_session, annotator_headers)
    payload = body["items"][0]["payload"]["episodes"]
    missing_id = next(iter(payload))
    missing = payload.pop(missing_id)
    failed = client.post(
        "/api/v1/annotation-work-items/batch-submit", json=body, headers=annotator_headers
    )
    assert failed.status_code == 422
    assert (
        db_session.query(EpisodeAnnotation)
        .filter(EpisodeAnnotation.episode_id.in_([int(missing_id)] + [int(k) for k in payload]))
        .count()
        == 0
    )
    payload[missing_id] = missing
    assert (
        client.post(
            "/api/v1/annotation-work-items/batch-submit", json=body, headers=annotator_headers
        ).status_code
        == 200
    )


def test_concurrent_same_key_materializes_once(client, db_session, annotator_headers):
    from concurrent.futures import ThreadPoolExecutor

    from data.database import EpisodeAnnotation, SessionLocal, User
    from data.services.annotation_work_items import batch_submit_annotation_items

    item, body = _body(client, db_session, annotator_headers)
    actor_id = item.assignee_user_id

    def submit(_):
        with SessionLocal() as db:
            result = batch_submit_annotation_items(
                db,
                workspace_id=body["workspace_id"],
                actor=db.get(User, actor_id),
                client_request_id=body["client_request_id"],
                items=body["items"],
            )
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results[0] == results[1]
    ids = [int(k) for k in body["items"][0]["payload"]["episodes"]]
    assert db_session.query(EpisodeAnnotation).filter(
        EpisodeAnnotation.episode_id.in_(ids)
    ).count() == len(ids)
