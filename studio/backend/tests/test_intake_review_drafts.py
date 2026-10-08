"""Contract tests for versioned post upload intake review drafts."""

from __future__ import annotations

from uuid import uuid4

import pytest
from collection_api_fixtures import (
    make_annotator_reviewer,
    make_project,
    make_workspace,
    seed_package_pending_intake_review,
)
from test_episode_objects import verified_entries

from data.database import Workspace
from data.models.data_package import PackageIntakeReview, PackageIntakeReviewDraft
from data.services.collection_intake_review import review_data_package_intake
from data.services.episode_admission import (
    EpisodeAdmissionConflictError,
    record_episode_admission_fact,
)
from data.services.intake_review_drafts import get_draft, save_draft


def _draft_response(client, headers, package, workspace_id):
    response = client.get(
        f"/api/v1/data-packages/{package.id}/intake-review-draft",
        headers=headers,
        params={"workspace_id": workspace_id},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _patch_draft(
    client, headers, package, workspace_id, *, base_version, source_fingerprint, draft
):
    return client.patch(
        f"/api/v1/data-packages/{package.id}/intake-review-draft",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "base_version": base_version,
            "source_fingerprint": source_fingerprint,
            "draft": draft,
        },
    )


def test_drafts_are_isolated_by_reviewer_and_workspace(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    annotator, reviewer = make_annotator_reviewer(db_session, workspace)

    initial = get_draft(
        db_session,
        workspace_id=workspace.id,
        package_id=package.id,
        reviewer_id=annotator.id,
    )
    annotator_saved = save_draft(
        db_session,
        workspace_id=workspace.id,
        package_id=package.id,
        reviewer_id=annotator.id,
        base_version=0,
        source_fingerprint=initial["source_fingerprint"],
        draft={
            "viewed_episode_ids": [episodes[0].id],
            "rejected_episodes": {str(episodes[0].id): "camera blocked"},
            "last_episode_id": episodes[0].id,
        },
    )
    db_session.commit()

    reviewer_initial = get_draft(
        db_session,
        workspace_id=workspace.id,
        package_id=package.id,
        reviewer_id=reviewer.id,
    )
    reviewer_saved = save_draft(
        db_session,
        workspace_id=workspace.id,
        package_id=package.id,
        reviewer_id=reviewer.id,
        base_version=0,
        source_fingerprint=reviewer_initial["source_fingerprint"],
        draft={
            "viewed_episode_ids": [episodes[1].id],
            "rejected_episodes": {},
            "last_episode_id": episodes[1].id,
        },
    )
    db_session.commit()

    assert annotator_saved["draft_version"] == 1
    assert reviewer_saved["draft_version"] == 1
    assert annotator_saved["draft"]["rejected_episodes"] == {str(episodes[0].id): "camera blocked"}
    assert reviewer_saved["draft"]["rejected_episodes"] == {}
    assert (
        db_session.query(PackageIntakeReviewDraft)
        .filter(PackageIntakeReviewDraft.data_package_id == package.id)
        .count()
        == 2
    )
    assert package.status == "pending_intake_review"

    other_workspace = make_workspace(db_session)
    with pytest.raises(LookupError):
        get_draft(
            db_session,
            workspace_id=other_workspace.id,
            package_id=package.id,
            reviewer_id=annotator.id,
        )


def test_draft_save_uses_first_save_and_lost_update_cas(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    current = _draft_response(client, admin_headers, package, workspace.id)
    first_draft = {
        "viewed_episode_ids": [episodes[0].id],
        "rejected_episodes": {str(episodes[0].id): "blurred"},
        "last_episode_id": episodes[0].id,
    }

    first = _patch_draft(
        client,
        admin_headers,
        package,
        workspace.id,
        base_version=0,
        source_fingerprint=current["source_fingerprint"],
        draft=first_draft,
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["draft_version"] == 1

    stale_first_save = _patch_draft(
        client,
        admin_headers,
        package,
        workspace.id,
        base_version=0,
        source_fingerprint=current["source_fingerprint"],
        draft={
            "viewed_episode_ids": [episodes[1].id],
            "rejected_episodes": {},
            "last_episode_id": episodes[1].id,
        },
    )
    assert stale_first_save.status_code == 409
    assert "intake_draft_version_conflict" in stale_first_save.json()["detail"]
    assert _draft_response(client, admin_headers, package, workspace.id)["draft"] == first_draft

    second = _patch_draft(
        client,
        admin_headers,
        package,
        workspace.id,
        base_version=1,
        source_fingerprint=current["source_fingerprint"],
        draft={
            "viewed_episode_ids": [episodes[0].id, episodes[1].id],
            "rejected_episodes": {str(episodes[0].id): "blurred"},
            "last_episode_id": episodes[1].id,
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["draft_version"] == 2

    stale_lost_update = _patch_draft(
        client,
        admin_headers,
        package,
        workspace.id,
        base_version=1,
        source_fingerprint=current["source_fingerprint"],
        draft={
            "viewed_episode_ids": [episodes[1].id],
            "rejected_episodes": {},
            "last_episode_id": episodes[1].id,
        },
    )
    assert stale_lost_update.status_code == 409
    assert "intake_draft_version_conflict" in stale_lost_update.json()["detail"]
    saved = _draft_response(client, admin_headers, package, workspace.id)
    assert saved["draft_version"] == 2
    assert saved["draft"]["viewed_episode_ids"] == [episodes[0].id, episodes[1].id]


def test_draft_rejects_episode_ids_outside_package(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    other_package, other_episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(0.5,)
    )
    current = _draft_response(client, admin_headers, package, workspace.id)

    for invalid_draft in (
        {
            "viewed_episode_ids": [other_episodes[0].id],
            "rejected_episodes": {},
            "last_episode_id": None,
        },
        {
            "viewed_episode_ids": [],
            "rejected_episodes": {str(other_episodes[0].id): "foreign"},
            "last_episode_id": None,
        },
        {
            "viewed_episode_ids": [],
            "rejected_episodes": {},
            "last_episode_id": other_episodes[0].id,
        },
    ):
        response = _patch_draft(
            client,
            admin_headers,
            package,
            workspace.id,
            base_version=0,
            source_fingerprint=current["source_fingerprint"],
            draft=invalid_draft,
        )
        assert response.status_code == 422, response.text

    assert (
        db_session.query(PackageIntakeReviewDraft).filter_by(data_package_id=package.id).count()
        == 0
    )
    assert other_package.status == "pending_intake_review"
    assert episodes[0].validity_status == "valid"


def test_stale_source_rejects_save_and_preserves_saved_conclusion(
    client, db_session, admin_headers
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    current = _draft_response(client, admin_headers, package, workspace.id)
    saved_draft = {
        "viewed_episode_ids": [episodes[0].id],
        "rejected_episodes": {str(episodes[0].id): "camera obstruction"},
        "last_episode_id": episodes[0].id,
    }
    saved = _patch_draft(
        client,
        admin_headers,
        package,
        workspace.id,
        base_version=0,
        source_fingerprint=current["source_fingerprint"],
        draft=saved_draft,
    )
    assert saved.status_code == 200, saved.text

    episodes[0].source_fingerprint = "changed-source-" + uuid4().hex
    db_session.commit()

    stale = _patch_draft(
        client,
        admin_headers,
        package,
        workspace.id,
        base_version=1,
        source_fingerprint=current["source_fingerprint"],
        draft={
            "viewed_episode_ids": [episodes[0].id, episodes[1].id],
            "rejected_episodes": {str(episodes[0].id): "changed"},
            "last_episode_id": episodes[1].id,
        },
    )
    assert stale.status_code == 409
    assert "intake_sources_changed" in stale.json()["detail"]

    refreshed = _draft_response(client, admin_headers, package, workspace.id)
    assert refreshed["source_changed"] is True
    assert refreshed["draft_version"] == 1
    assert refreshed["draft"] == saved_draft


def test_final_review_requires_saved_version_and_matching_nonempty_reasons(
    client, db_session, admin_headers
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    current = _draft_response(client, admin_headers, package, workspace.id)
    first_id = str(episodes[0].id)

    no_saved_draft = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "approved",
            "rejected_episode_ids": [episodes[0].id],
            "base_version": 1,
            "source_fingerprint": current["source_fingerprint"],
            "episode_reasons": {first_id: "blurred"},
        },
    )
    assert no_saved_draft.status_code == 409
    assert "intake_draft_version_conflict" in no_saved_draft.json()["detail"]

    saved = _patch_draft(
        client,
        admin_headers,
        package,
        workspace.id,
        base_version=0,
        source_fingerprint=current["source_fingerprint"],
        draft={
            "viewed_episode_ids": [episodes[0].id],
            "rejected_episodes": {first_id: "blurred"},
            "last_episode_id": episodes[0].id,
        },
    )
    assert saved.status_code == 200, saved.text

    stale_version = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "approved",
            "rejected_episode_ids": [episodes[0].id],
            "base_version": 2,
            "source_fingerprint": current["source_fingerprint"],
            "episode_reasons": {first_id: "blurred"},
        },
    )
    assert stale_version.status_code == 409
    assert "intake_draft_version_conflict" in stale_version.json()["detail"]

    mismatched_reason = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "approved",
            "rejected_episode_ids": [episodes[0].id],
            "base_version": 1,
            "source_fingerprint": current["source_fingerprint"],
            "episode_reasons": {first_id: "different reason"},
        },
    )
    assert mismatched_reason.status_code == 409
    assert "intake_draft_decisions_changed" in mismatched_reason.json()["detail"]

    blank_reason_draft = _patch_draft(
        client,
        admin_headers,
        package,
        workspace.id,
        base_version=1,
        source_fingerprint=current["source_fingerprint"],
        draft={
            "viewed_episode_ids": [episodes[0].id],
            "rejected_episodes": {first_id: ""},
            "last_episode_id": episodes[0].id,
        },
    )
    assert blank_reason_draft.status_code == 200, blank_reason_draft.text
    blank_final = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "approved",
            "rejected_episode_ids": [episodes[0].id],
            "base_version": 2,
            "source_fingerprint": current["source_fingerprint"],
            "episode_reasons": {first_id: ""},
        },
    )
    assert blank_final.status_code == 422
    assert "each rejected Episode requires its own reason" in blank_final.json()["detail"]
    assert db_session.query(PackageIntakeReview).filter_by(data_package_id=package.id).count() == 0


def test_final_review_is_idempotent_but_cannot_change_final_conclusion(
    client, db_session, admin_headers
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    current = _draft_response(client, admin_headers, package, workspace.id)
    first_id = str(episodes[0].id)
    draft = {
        "viewed_episode_ids": [episodes[0].id],
        "rejected_episodes": {first_id: "blurred"},
        "last_episode_id": episodes[0].id,
    }
    saved = _patch_draft(
        client,
        admin_headers,
        package,
        workspace.id,
        base_version=0,
        source_fingerprint=current["source_fingerprint"],
        draft=draft,
    )
    assert saved.status_code == 200, saved.text
    final_payload = {
        "workspace_id": workspace.id,
        "verdict": "approved",
        "rejected_episode_ids": [episodes[0].id],
        "base_version": 1,
        "source_fingerprint": current["source_fingerprint"],
        "episode_reasons": {first_id: "blurred"},
    }

    first_final = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json=final_payload,
    )
    assert first_final.status_code == 200, first_final.text
    db_session.expire_all()
    review = (
        db_session.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == package.id)
        .one()
    )
    assert review.episode_reasons_json == {first_id: "blurred"}
    assert review.source_fingerprint == current["source_fingerprint"]

    retry = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json=final_payload,
    )
    assert retry.status_code == 200, retry.text
    assert (
        db_session.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == package.id)
        .count()
        == 1
    )

    changed = dict(final_payload)
    changed["episode_reasons"] = {first_id: "changed after final"}
    changed_response = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json=changed,
    )
    assert changed_response.status_code == 409
    assert "already_reviewed" in changed_response.json()["detail"]

    overwrite = _patch_draft(
        client,
        admin_headers,
        package,
        workspace.id,
        base_version=1,
        source_fingerprint=current["source_fingerprint"],
        draft=draft,
    )
    assert overwrite.status_code == 409
    assert "package_intake_finalized_or_unavailable" in overwrite.json()["detail"]


def test_pending_worker_update_invalidates_stale_final_and_finalized_package_rejects_worker(
    client, db_session, admin_headers
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    current = _draft_response(client, admin_headers, package, workspace.id)
    episode = episodes[0]
    draft = {
        "viewed_episode_ids": [episode.id],
        "rejected_episodes": {},
        "last_episode_id": episode.id,
    }
    saved = _patch_draft(
        client,
        admin_headers,
        package,
        workspace.id,
        base_version=0,
        source_fingerprint=current["source_fingerprint"],
        draft=draft,
    )
    assert saved.status_code == 200, saved.text

    record_episode_admission_fact(
        db_session,
        episode_id=episode.id,
        attempt=2,
        source_fingerprint=episode.source_fingerprint,
        validation_policy_version="v1",
        integrity_status="pending",
        preview_status="pending",
        output_verification_status="pending",
        report_ref={},
    )
    db_session.commit()

    stale_final = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "approved",
            "rejected_episode_ids": [],
            "base_version": 1,
            "source_fingerprint": current["source_fingerprint"],
            "episode_reasons": {},
        },
    )
    assert stale_final.status_code == 409
    assert "intake_sources_changed" in stale_final.json()["detail"]
    db_session.expire_all()
    assert db_session.get(type(package), package.id).status == "pending_intake_review"
    assert db_session.query(PackageIntakeReview).filter_by(data_package_id=package.id).count() == 0

    approved_package, approved_episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=approved_package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="",
    )
    db_session.commit()

    with pytest.raises(EpisodeAdmissionConflictError, match="package_intake_finalized"):
        record_episode_admission_fact(
            db_session,
            episode_id=approved_episodes[0].id,
            attempt=2,
            source_fingerprint=approved_episodes[0].source_fingerprint,
            validation_policy_version="v1",
            integrity_status="passed",
            preview_status="ready",
            output_verification_status="verified",
            report_ref={"uri": "db://retry"},
            objects=verified_entries(),
        )
    db_session.rollback()


def test_intake_draft_endpoint_hides_other_workspace_and_requires_workspace_access(
    client, db_session, admin_headers
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, _episodes = seed_package_pending_intake_review(db_session, workspace, project)
    other_workspace = make_workspace(db_session)

    hidden = client.get(
        f"/api/v1/data-packages/{package.id}/intake-review-draft",
        headers=admin_headers,
        params={"workspace_id": other_workspace.id},
    )
    assert hidden.status_code == 404

    unmembered = Workspace(name=f"unmembered-{uuid4().hex}", creator="admin@quicdata.com")
    db_session.add(unmembered)
    db_session.commit()
    denied = client.get(
        f"/api/v1/data-packages/{package.id}/intake-review-draft",
        headers=admin_headers,
        params={"workspace_id": unmembered.id},
    )
    assert denied.status_code == 403
