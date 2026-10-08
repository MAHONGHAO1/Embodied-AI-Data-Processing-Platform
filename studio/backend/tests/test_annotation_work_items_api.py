"""Annotation work items: load-balanced assignment, draft saving, submission, and reassignment."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_intake_approved_package,
)

from data.database import Episode, EpisodeAnnotation, User, WorkspaceMember
from data.models.annotation_work import AnnotationWorkItem, ReviewWorkItem
from data.models.data_package import DataPackage
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.annotation_work_items import assign_work_items_for_batch
from data.services.data_batches import create_data_batch
from data.utils.helpers import create_access_token, hash_password


def _headers_for(user: User) -> dict[str, str]:
    token = create_access_token(user.id, user.email, user.role)
    return {"Authorization": f"Bearer {token}"}


def _make_annotator(db, workspace, *, email: str | None = None) -> User:
    user = User(
        email=email or f"ann-{uuid4().hex}@t.com",
        password_hash=hash_password("ann-pass-123"),
        role="annotator",
        is_active=True,
    )
    db.add(user)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id))
    db.commit()
    db.refresh(user)
    return user


def _make_reviewer(db, workspace) -> User:
    user = User(
        email=f"rev-{uuid4().hex}@t.com",
        password_hash=hash_password("rev-pass-123"),
        role="auditor",
        is_active=True,
    )
    db.add(user)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id))
    db.commit()
    db.refresh(user)
    return user


def _create_annotated_batch(db, *, durations: tuple[str, ...] = ("3.00", "2.00", "2.00")):
    workspace = make_workspace(db)
    project = make_project(db, workspace)
    packages = []
    for hours in durations:
        package = seed_intake_approved_package(
            db, workspace, project, episode_hours=(float(hours),)
        )
        package.governed_valid_duration_hours = Decimal(hours)
        packages.append(package)
    db.commit()

    annotators = [
        _make_annotator(db, workspace),
        _make_annotator(db, workspace),
    ]
    annotators.sort(key=lambda u: u.id)
    reviewer = _make_reviewer(db, workspace)

    batch, _ = create_data_batch(
        db,
        workspace_id=workspace.id,
        name=f"Ann-Batch-{uuid4().hex[:6]}",
        data_package_ids=[p.id for p in packages],
        label_ids=[],
        integrity_check_enabled=False,
        quality_check_enabled=False,
        compliance_check_enabled=False,
        annotation_enabled=True,
        annotator_user_ids=[a.id for a in annotators],
        reviewer_user_id=reviewer.id,
        review_mode="single",
        created_by_user_id=None,
    )
    batch.status = "annotating"
    for episode in (
        db.query(Episode)
        .filter(Episode.data_package_id.in_([package.id for package in packages]))
        .all()
    ):
        seed_annotation_source(db, episode)
    db.commit()
    db.refresh(batch)
    return workspace, batch, packages, annotators, reviewer


def seed_annotation_source(db, episode):
    metadata = dict(episode.metadata_json or {})
    duration_ns = int(metadata.get("timing", {}).get("duration_s", 3600) * 1_000_000_000)
    metadata["timing"] = {
        "start_timestamp_ns": "0",
        "end_timestamp_ns": str(duration_ns),
        "duration_s": duration_ns / 1_000_000_000,
    }
    metadata["collection_upload"] = {"external_episode_id": f"draft-{episode.id}"}
    episode.metadata_json = metadata
    fact = db.query(EpisodeAdmissionFact).filter_by(episode_id=episode.id, attempt=1).one()
    # source_binding() reads fact.objects_json exclusively: give the manifest's
    # "data" entry a deterministic identity so the submitted QRDF payload's
    # data_sha256 can be checked against source_binding()'s data_sha256, which
    # comes from the manifest.
    from data.services.episode_objects import object_entry

    objects = [dict(entry) for entry in (fact.objects_json or [])]
    for entry in objects:
        if entry.get("kind") == "data":
            entry.update(
                object_entry(
                    path=entry["path"],
                    kind="data",
                    ref={
                        "bucket_role": "raw",
                        "object_key": f"raw/{episode.id}/data.mcap",
                        "version_id": "v1",
                        "etag": "source-etag",
                        "size_bytes": 1,
                        "sha256": "0" * 64,
                    },
                )
            )
    fact.objects_json = objects


def test_load_balance_assigns_shorter_load_first(db_session):
    # two annotators, three packages durations 3,2,2 → expected counts/sums stable by user_id tie-break
    workspace, batch, packages, annotators, reviewer = _create_annotated_batch(
        db_session, durations=("3.00", "2.00", "2.00")
    )
    a_lo, a_hi = annotators

    assign_work_items_for_batch(db_session, batch_id=batch.id)
    db_session.commit()

    items = (
        db_session.query(AnnotationWorkItem)
        .filter(AnnotationWorkItem.data_batch_id == batch.id)
        .order_by(AnnotationWorkItem.data_package_id.asc())
        .all()
    )
    assert len(items) == 3
    by_pkg = {item.data_package_id: item for item in items}
    # package order by id matches creation order: 3 → a_lo, 2 → a_hi, 2 → a_hi
    assert by_pkg[packages[0].id].assignee_user_id == a_lo.id
    assert by_pkg[packages[1].id].assignee_user_id == a_hi.id
    assert by_pkg[packages[2].id].assignee_user_id == a_hi.id

    loads: dict[int, Decimal] = {a_lo.id: Decimal("0"), a_hi.id: Decimal("0")}
    counts = {a_lo.id: 0, a_hi.id: 0}
    for item in items:
        pkg = db_session.get(DataPackage, item.data_package_id)
        loads[item.assignee_user_id] += Decimal(str(pkg.governed_valid_duration_hours))
        counts[item.assignee_user_id] += 1
    assert counts[a_lo.id] == 1
    assert counts[a_hi.id] == 2
    assert loads[a_lo.id] == Decimal("3.00")
    assert loads[a_hi.id] == Decimal("4.00")

    reviews = (
        db_session.query(ReviewWorkItem).filter(ReviewWorkItem.data_batch_id == batch.id).all()
    )
    assert len(reviews) == 3
    assert all(r.assignee_user_id == reviewer.id for r in reviews)

    db_session.refresh(batch)
    snapshot = batch.assignment_snapshot_json or {}
    assert snapshot.get("reviewer_user_id") == reviewer.id
    mapping = {
        int(entry["data_package_id"]): int(entry["assignee_user_id"])
        for entry in snapshot.get("assignments", [])
    }
    assert mapping[packages[0].id] == a_lo.id
    assert mapping[packages[1].id] == a_hi.id
    assert mapping[packages[2].id] == a_hi.id


def test_annotator_lists_only_own_items(client, db_session):
    workspace, batch, packages, annotators, _reviewer = _create_annotated_batch(
        db_session, durations=("1.00", "1.00")
    )
    assign_work_items_for_batch(db_session, batch_id=batch.id)
    db_session.commit()

    a_lo, a_hi = annotators
    listed = client.get(
        "/api/v1/annotation-work-items",
        headers=_headers_for(a_lo),
        params={"workspace_id": workspace.id},
    )
    assert listed.status_code == 200
    items = listed.json()["data"]["items"]
    assert items
    assert all(item["assignee_user_id"] == a_lo.id for item in items)
    assert all(item["assignee_user_id"] != a_hi.id for item in items)


def test_reassign_rejects_writes_from_previous_assignee(client, db_session, admin_headers):
    workspace, batch, packages, annotators, _reviewer = _create_annotated_batch(
        db_session, durations=("1.00",)
    )
    assign_work_items_for_batch(db_session, batch_id=batch.id)
    db_session.commit()

    item = (
        db_session.query(AnnotationWorkItem)
        .filter(AnnotationWorkItem.data_batch_id == batch.id)
        .one()
    )
    previous = next(a for a in annotators if a.id == item.assignee_user_id)
    replacement = next(a for a in annotators if a.id != previous.id)

    reassigned = client.post(
        f"/api/v1/annotation-work-items/{item.id}/reassign",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "to_user_id": replacement.id,
            "reason": "coverage swap",
        },
    )
    assert reassigned.status_code == 200
    body = reassigned.json()["data"]
    assert body["assignee_user_id"] == replacement.id
    assert body["generation"] >= 2

    denied = client.patch(
        f"/api/v1/annotation-work-items/{item.id}",
        headers=_headers_for(previous),
        json={"workspace_id": workspace.id, "draft_json": {"note": "stale"}},
    )
    assert denied.status_code == 403

    allowed = client.patch(
        f"/api/v1/annotation-work-items/{item.id}",
        headers=_headers_for(replacement),
        json={"workspace_id": workspace.id, "draft_json": {"note": "ok"}},
    )
    assert allowed.status_code == 200
    assert allowed.json()["data"]["draft_json"] == {"note": "ok"}


def test_submit_then_submit_again_is_conflict(client, db_session):
    workspace, batch, _packages, annotators, _reviewer = _create_annotated_batch(
        db_session, durations=("1.00",)
    )
    assign_work_items_for_batch(db_session, batch_id=batch.id)
    db_session.commit()
    item = (
        db_session.query(AnnotationWorkItem)
        .filter(AnnotationWorkItem.data_batch_id == batch.id)
        .one()
    )
    assignee = next(a for a in annotators if a.id == item.assignee_user_id)

    saved = client.patch(
        f"/api/v1/annotation-work-items/{item.id}",
        headers=_headers_for(assignee),
        json={
            "workspace_id": workspace.id,
            "draft_json": {
                "episodes": {
                    str(member["episode_id"]): _qrdf_annotation_payload(member["episode_id"])
                    for member in item.episode_members_json
                }
            },
        },
    )
    assert saved.status_code == 200
    first = client.post(
        f"/api/v1/annotation-work-items/{item.id}/submit",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id},
    )
    assert first.status_code == 200
    assert first.json()["data"]["status"] == "submitted"

    second = client.post(
        f"/api/v1/annotation-work-items/{item.id}/submit",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id},
    )
    assert second.status_code == 409


def test_admin_non_assignee_cannot_submit(client, db_session, admin_headers):
    workspace, batch, _packages, _annotators, _reviewer = _create_annotated_batch(
        db_session, durations=("1.00",)
    )
    assign_work_items_for_batch(db_session, batch_id=batch.id)
    db_session.commit()
    item = (
        db_session.query(AnnotationWorkItem)
        .filter(AnnotationWorkItem.data_batch_id == batch.id)
        .one()
    )

    denied = client.post(
        f"/api/v1/annotation-work-items/{item.id}/submit",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert denied.status_code == 403


def _qrdf_annotation_payload(episode_id: int) -> dict[str, object]:
    return {
        "kind": "episode_annotation",
        "qrdf_version": "0.2.0",
        "source": {
            "episode_id": f"draft-{episode_id}",
            "data_sha256": "0" * 64,
        },
        "episode": {"outcome": "unknown"},
        "tracks": [
            {
                "name": "high_level_subtask",
                "items": [
                    {
                        "id": "01998211-1234-7000-8000-123456789abc",
                        "target": {"type": "time_range", "start_ns": "0", "end_ns": "1000000000"},
                        "high_level_subtask": "Pick up the cup",
                    }
                ],
            }
        ],
    }


def test_package_annotation_draft_rejects_invalid_qrdf_payload_before_submission(
    client, db_session
):
    workspace, batch, _packages, annotators, _reviewer = _create_annotated_batch(
        db_session, durations=("1.00", "1.00")
    )
    assign_work_items_for_batch(db_session, batch_id=batch.id)
    db_session.commit()
    item = (
        db_session.query(AnnotationWorkItem)
        .filter(AnnotationWorkItem.data_batch_id == batch.id)
        .order_by(AnnotationWorkItem.id)
        .first()
    )
    assert item is not None
    assignee = next(user for user in annotators if user.id == item.assignee_user_id)
    members = item.episode_members_json
    partial = client.patch(
        f"/api/v1/annotation-work-items/{item.id}",
        headers=_headers_for(assignee),
        json={
            "workspace_id": workspace.id,
            "draft_json": {"episodes": {}},
        },
    )
    assert partial.status_code == 200
    rejected = client.post(
        f"/api/v1/annotation-work-items/{item.id}/submit",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "annotation_draft_membership_mismatch"

    invalid_payloads = {
        str(member["episode_id"]): {"note": f"episode-{member['episode_id']}"} for member in members
    }
    saved = client.patch(
        f"/api/v1/annotation-work-items/{item.id}",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id, "draft_json": {"episodes": invalid_payloads}},
    )
    assert saved.status_code == 200
    rejected = client.post(
        f"/api/v1/annotation-work-items/{item.id}/submit",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id},
    )
    assert rejected.status_code == 422
    assert "annotation_draft_payload_invalid" in rejected.json()["detail"]
    assert (
        db_session.query(EpisodeAnnotation)
        .filter(EpisodeAnnotation.episode_id.in_([member["episode_id"] for member in members]))
        .count()
        == 0
    )


def test_package_annotation_draft_materializes_every_frozen_episode(client, db_session):
    workspace, batch, _packages, annotators, _reviewer = _create_annotated_batch(
        db_session, durations=("1.00", "1.00")
    )
    assign_work_items_for_batch(db_session, batch_id=batch.id)
    db_session.commit()
    item = (
        db_session.query(AnnotationWorkItem)
        .filter(AnnotationWorkItem.data_batch_id == batch.id)
        .order_by(AnnotationWorkItem.id)
        .first()
    )
    assert item is not None
    assignee = next(user for user in annotators if user.id == item.assignee_user_id)
    members = item.episode_members_json
    payloads = {
        str(member["episode_id"]): _qrdf_annotation_payload(member["episode_id"])
        for member in members
    }
    saved = client.patch(
        f"/api/v1/annotation-work-items/{item.id}",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id, "draft_json": {"episodes": payloads}},
    )
    assert saved.status_code == 200
    submitted = client.post(
        f"/api/v1/annotation-work-items/{item.id}/submit",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id},
    )
    assert submitted.status_code == 200
    annotations = (
        db_session.query(EpisodeAnnotation)
        .filter(EpisodeAnnotation.episode_id.in_([member["episode_id"] for member in members]))
        .all()
    )
    assert len(annotations) == len(members)
    assert {annotation.episode_id for annotation in annotations} == {
        member["episode_id"] for member in members
    }
    assert {annotation.payload_json["source"]["episode_id"] for annotation in annotations} == {
        payload["source"]["episode_id"] for payload in payloads.values()
    }
    db_session.refresh(item)
    assert item.materialized_annotation_versions_json == [
        {
            "episode_id": annotation.episode_id,
            "annotation_id": annotation.id,
            "version": annotation.version,
        }
        for annotation in sorted(annotations, key=lambda annotation: annotation.episode_id)
    ]


def test_returned_materialized_package_annotation_rejects_empty_resubmission(client, db_session):
    workspace, batch, _packages, annotators, reviewer = _create_annotated_batch(
        db_session, durations=("1.00",)
    )
    assign_work_items_for_batch(db_session, batch_id=batch.id)
    db_session.commit()
    item = (
        db_session.query(AnnotationWorkItem)
        .filter(AnnotationWorkItem.data_batch_id == batch.id)
        .one()
    )
    review = (
        db_session.query(ReviewWorkItem)
        .filter(ReviewWorkItem.annotation_work_item_id == item.id)
        .one()
    )
    assignee = next(user for user in annotators if user.id == item.assignee_user_id)
    payloads = {
        str(member["episode_id"]): _qrdf_annotation_payload(member["episode_id"])
        for member in item.episode_members_json
    }

    saved = client.patch(
        f"/api/v1/annotation-work-items/{item.id}",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id, "draft_json": {"episodes": payloads}},
    )
    assert saved.status_code == 200
    submitted = client.post(
        f"/api/v1/annotation-work-items/{item.id}/submit",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id},
    )
    assert submitted.status_code == 200

    returned = client.post(
        f"/api/v1/review-work-items/{review.id}/return",
        headers=_headers_for(reviewer),
        json={"workspace_id": workspace.id, "reason": "revise annotation"},
    )
    assert returned.status_code == 200

    cleared = client.patch(
        f"/api/v1/annotation-work-items/{item.id}",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id, "draft_json": {}},
    )
    assert cleared.status_code == 200
    rejected = client.post(
        f"/api/v1/annotation-work-items/{item.id}/submit",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "annotation_draft_required"


def test_uncertain_legacy_empty_draft_cannot_adopt_unrelated_episode_annotation(client, db_session):
    workspace, batch, _packages, annotators, _reviewer = _create_annotated_batch(
        db_session, durations=("1.00",)
    )
    assign_work_items_for_batch(db_session, batch_id=batch.id)
    db_session.commit()
    item = (
        db_session.query(AnnotationWorkItem)
        .filter(AnnotationWorkItem.data_batch_id == batch.id)
        .one()
    )
    item.materialization_provenance_state = "uncertain"
    episode_id = int(item.episode_members_json[0]["episode_id"])
    # This mirrors a legacy/reused EpisodeAnnotation with no package-item
    # provenance. It must not make a never-materialized package task require a
    # non-empty draft.
    db_session.add(
        EpisodeAnnotation(
            episode_id=episode_id,
            version=1,
            payload_json={"legacy": True},
        )
    )
    db_session.commit()
    assignee = next(user for user in annotators if user.id == item.assignee_user_id)

    submitted = client.post(
        f"/api/v1/annotation-work-items/{item.id}/submit",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id},
    )

    assert submitted.status_code == 422
    assert submitted.json()["detail"] == "annotation_draft_required"
    db_session.refresh(item)
    assert item.materialized_annotation_versions_json == []


def test_uncertain_legacy_returned_item_rejects_empty_resubmission(client, db_session):
    workspace, batch, _packages, annotators, _reviewer = _create_annotated_batch(
        db_session, durations=("1.00",)
    )
    item = assign_work_items_for_batch(db_session, batch_id=batch.id)[0]
    item.materialization_provenance_state = "uncertain"
    item.status = "returned"
    item.return_reason = "historical result requires a real new submission"
    db_session.commit()
    assignee = next(user for user in annotators if user.id == item.assignee_user_id)
    rejected = client.post(
        f"/api/v1/annotation-work-items/{item.id}/submit",
        headers=_headers_for(assignee),
        json={"workspace_id": workspace.id},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "annotation_draft_required"
