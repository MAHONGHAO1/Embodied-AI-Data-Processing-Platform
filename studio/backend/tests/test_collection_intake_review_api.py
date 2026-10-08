"""Per-package intake review API."""

from collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_package_pending_intake_review,
)

from data.models.data_package import DataPackage, PackageIntakeReview
from data.security.audit import list_recent_audit_events


def test_intake_approve_marks_rejected_episodes_and_freezes_valid_duration(
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
    bad_episode, good_episode = episodes

    response = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "approved",
            "rejected_episode_ids": [bad_episode.id],
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "intake_approved"
    assert response.json()["data"]["intake_valid_duration_hours"] == "1.00"
    db_session.refresh(bad_episode)
    db_session.refresh(good_episode)
    assert bad_episode.validity_status == "intake_rejected"
    assert good_episode.validity_status == "valid"
    review = (
        db_session.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == package.id)
        .one()
    )
    assert review.verdict == "approved"
    assert review.is_bulk is False
    assert review.rejected_episode_ids_json == [bad_episode.id]

    again = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "verdict": "approved"},
    )
    assert again.status_code == 409
    assert "already_reviewed" in again.json()["detail"]
    db_session.refresh(package)
    assert str(package.intake_valid_duration_hours) == "1.00"

    audit = next(
        event
        for event in list_recent_audit_events(
            limit=20,
            event="collection.intake.review",
        )
        if event["resource"] == f"data_package:{package.id}"
    )
    assert audit["detail"]["verdict"] == "approved"
    assert audit["detail"]["is_bulk"] is False
    assert audit["detail"]["rejected_episode_ids"] == [bad_episode.id]


def test_intake_reject_voids_package_without_autotop_up(
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
        episode_hours=(1.0,),
    )
    before = (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == package.collection_task_id)
        .count()
    )

    response = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "rejected",
            "reason": "bad capture",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "voided"
    assert response.json()["data"]["intake_valid_duration_hours"] == "0.00"
    after = (
        db_session.query(DataPackage)
        .filter(DataPackage.collection_task_id == package.collection_task_id)
        .count()
    )
    assert after == before
    db_session.expire_all()
    assert all(
        db_session.get(type(episodes[0]), episode.id).validity_status == "intake_rejected"
        for episode in episodes
    )
    review = (
        db_session.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == package.id)
        .one()
    )
    assert review.verdict == "rejected"
    assert review.reason == "bad capture"


def test_intake_review_requires_admin_and_pending_state(
    client,
    db_session,
    admin_headers,
    operator_headers,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, _ = seed_package_pending_intake_review(db_session, workspace, project)
    payload = {"workspace_id": workspace.id, "verdict": "approved"}

    forbidden = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=operator_headers,
        json=payload,
    )
    assert forbidden.status_code == 403

    package.status = "ingested"
    db_session.commit()
    wrong_state = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json=payload,
    )
    assert wrong_state.status_code == 409
    assert "pending_intake_review" in wrong_state.json()["detail"]


def test_intake_review_uses_episode_admission_facts_not_package_preview(
    client,
    db_session,
    admin_headers,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    package.qrdf_facts_json = {}
    db_session.commit()

    accepted = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "verdict": "approved"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["data"]["accepted_episode_ids"] == [item.id for item in episodes]


def test_intake_review_validates_rejection_reason_and_episode_scope(
    client,
    db_session,
    admin_headers,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, _ = seed_package_pending_intake_review(db_session, workspace, project)
    other_package, other_episodes = seed_package_pending_intake_review(
        db_session,
        workspace,
        project,
        episode_hours=(0.5,),
    )

    missing_reason = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "rejected",
            "reason": " ",
        },
    )
    assert missing_reason.status_code == 422

    foreign_episode = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "approved",
            "rejected_episode_ids": [other_episodes[0].id],
        },
    )
    assert foreign_episode.status_code == 422
    db_session.refresh(other_package)
    db_session.refresh(other_episodes[0])
    assert other_package.status == "pending_intake_review"
    assert other_episodes[0].validity_status == "valid"


def test_bulk_approve_sets_is_bulk_and_approves_all(
    client,
    db_session,
    admin_headers,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    first, _ = seed_package_pending_intake_review(db_session, workspace, project)
    second, _ = seed_package_pending_intake_review(db_session, workspace, project)

    response = client.post(
        "/api/v1/data-packages/intake-review/bulk-approve",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "data_package_ids": [first.id, second.id],
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["approved_count"] == 2
    db_session.expire_all()
    assert db_session.get(DataPackage, first.id).status == "intake_approved"
    assert db_session.get(DataPackage, second.id).status == "intake_approved"
    reviews = (
        db_session.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id.in_([first.id, second.id]))
        .all()
    )
    assert len(reviews) == 2
    assert all(review.is_bulk for review in reviews)
    assert all(review.verdict == "approved" for review in reviews)


def test_bulk_approve_is_atomic_when_one_not_pending(
    client,
    db_session,
    admin_headers,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    first, _ = seed_package_pending_intake_review(db_session, workspace, project)
    second, _ = seed_package_pending_intake_review(db_session, workspace, project)
    second.status = "intake_approved"
    db_session.commit()

    response = client.post(
        "/api/v1/data-packages/intake-review/bulk-approve",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "data_package_ids": [first.id, second.id],
        },
    )

    assert response.status_code == 409
    assert str(second.id) in response.json()["detail"]
    db_session.refresh(first)
    assert first.status == "pending_intake_review"
    assert (
        db_session.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == first.id)
        .count()
        == 0
    )
