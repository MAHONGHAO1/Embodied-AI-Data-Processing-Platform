from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from data.database import (
    ArtifactOperation,
    Episode,
    EpisodeArtifact,
    EpisodeReview,
    JobRun,
    TaskSet,
    User,
    WorkItem,
    Workspace,
)
from data.services.batches import create_batch


def _episode_context(db_session):
    suffix = uuid4().hex
    actor = db_session.query(User).order_by(User.id).first()
    workspace = Workspace(name=f"episode projection workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"episode projection project {suffix}")
    db_session.add(project)
    db_session.flush()
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name=f"episode projection batch {suffix}",
        batch_type="teleop",
        actor_id=actor.id,
    )
    source = Episode(
        episode_uid=f"episode-projection-source-{suffix}",
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        kind="source",
        modality="teleop",
        quality_status="passed",
    )
    db_session.add(source)
    db_session.flush()
    derived = Episode(
        episode_uid=f"episode-projection-derived-{suffix}",
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        parent_episode_id=source.id,
        kind="derived",
        derivation_version=1,
        modality="teleop",
        source_start_ns=0,
        source_end_ns=10_000_000_000,
        quality_status="passed",
        metadata_json={
            "metrics": {
                "reference_frame_count": 300,
                "duration_s": 10.0,
                "average_rgb_rate_hz": 30.0,
            }
        },
    )
    failed = Episode(
        episode_uid=f"episode-projection-failed-{suffix}",
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        parent_episode_id=source.id,
        kind="derived",
        derivation_version=1,
        modality="teleop",
        source_start_ns=20_000_000_000,
        source_end_ns=30_000_000_000,
        quality_status="failed",
    )
    db_session.add_all((derived, failed))
    db_session.flush()
    return actor, workspace, source, derived, failed


def test_work_queue_projects_safe_derived_episode_metrics_and_excludes_quality_failures(db_session):
    from data.services.work_queue_projection import create_episode_work_item, list_work_queue

    actor, workspace, _source, derived, failed = _episode_context(db_session)
    create_episode_work_item(
        db_session, episode_id=derived.id, kind="annotation", actor_id=actor.id
    )
    create_episode_work_item(db_session, episode_id=failed.id, kind="annotation", actor_id=actor.id)
    db_session.commit()

    page = list_work_queue(
        db_session, workspace_id=workspace.id, actor_id=actor.id, stage="annotation"
    )

    assert page["total"] == 1
    row = page["items"][0]
    assert row["episode"]["kind"] == "derived"
    assert row["quality"] == {"status": "passed", "restored": False}
    assert row["metrics"] == {
        "reference_frame_count": 300,
        "duration_s": 10.0,
        "average_rgb_rate_hz": 30.0,
    }
    assert row["work_item"]["available_actions"] == ["claim"]
    assert "metadata_json" not in row
    assert "uri" not in str(row)


def test_work_queue_filters_by_task_set(db_session):
    from data.services.work_queue_projection import create_episode_work_item, list_work_queue

    actor, workspace, source, _derived, _failed = _episode_context(db_session)
    other_task_set = TaskSet(workspace_id=workspace.id, name="work queue other task set")
    db_session.add(other_task_set)
    db_session.flush()
    other_batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=other_task_set.id,
        name="work queue other batch",
        batch_type="teleop",
        actor_id=actor.id,
    )
    other_source = Episode(
        episode_uid=f"episode-projection-other-source-{uuid4().hex}",
        workspace_id=workspace.id,
        task_set_id=other_task_set.id,
        batch_id=other_batch.id,
        kind="source",
        modality="teleop",
        quality_status="passed",
    )
    db_session.add(other_source)
    db_session.flush()
    create_episode_work_item(db_session, episode_id=source.id, kind="cut", actor_id=actor.id)
    create_episode_work_item(db_session, episode_id=other_source.id, kind="cut", actor_id=actor.id)
    db_session.commit()

    scoped = list_work_queue(
        db_session,
        workspace_id=workspace.id,
        actor_id=actor.id,
        stage="cut",
        task_set_id=source.task_set_id,
    )
    assert scoped["total"] == 1
    assert scoped["items"][0]["episode"]["episode_uid"] == source.episode_uid

    with pytest.raises(PermissionError):
        list_work_queue(
            db_session,
            workspace_id=workspace.id,
            actor_id=actor.id,
            stage="cut",
            task_set_id=other_task_set.id + 10_000,
        )


def test_work_queue_searches_episode_uid_with_compact_and_escaped_keywords(db_session):
    from data.services.work_queue_projection import create_episode_work_item, list_work_queue

    actor, workspace, source, _derived, _failed = _episode_context(db_session)
    source.episode_uid = "drv_29_search-target"
    sibling = Episode(
        episode_uid="drv_290_search-sibling",
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        kind="source",
        modality="teleop",
        quality_status="passed",
    )
    literal_percent = Episode(
        episode_uid="drv%literal-search",
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        kind="source",
        modality="teleop",
        quality_status="passed",
    )
    db_session.add_all((sibling, literal_percent))
    db_session.flush()
    for episode in (source, sibling, literal_percent):
        create_episode_work_item(db_session, episode_id=episode.id, kind="cut", actor_id=actor.id)
    db_session.commit()

    compact = list_work_queue(
        db_session,
        workspace_id=workspace.id,
        actor_id=actor.id,
        stage="cut",
        episode_keyword="DRV29",
    )
    assert compact["total"] == 2
    assert {row["episode"]["episode_uid"] for row in compact["items"]} == {
        "drv_29_search-target",
        "drv_290_search-sibling",
    }

    literal = list_work_queue(
        db_session,
        workspace_id=workspace.id,
        actor_id=actor.id,
        stage="cut",
        episode_keyword="%literal",
    )
    assert literal["total"] == 1
    assert literal["items"][0]["episode"]["episode_uid"] == "drv%literal-search"

    with pytest.raises(ValueError, match="keyword is too long"):
        list_work_queue(
            db_session,
            workspace_id=workspace.id,
            actor_id=actor.id,
            stage="cut",
            episode_keyword="x" * 129,
        )


def test_derived_work_waits_for_its_preview_before_it_can_be_claimed(db_session):
    from data.services.work_queue_projection import (
        claim_work_item,
        create_episode_work_item,
        list_work_queue,
    )

    actor, workspace, _source, derived, _failed = _episode_context(db_session)
    item = create_episode_work_item(
        db_session, episode_id=derived.id, kind="annotation", actor_id=actor.id
    )
    preview_job = JobRun(
        id=uuid4().hex,
        kind="episode_preview",
        resource_type="episode",
        resource_id=str(derived.id),
        workspace_id=workspace.id,
        task_set_id=derived.task_set_id,
        idempotency_key=f"queue-preview-{uuid4().hex}",
        queue="media",
        status="queued",
        phase="queued",
    )
    db_session.add(preview_job)
    db_session.commit()

    page = list_work_queue(
        db_session, workspace_id=workspace.id, actor_id=actor.id, stage="annotation"
    )

    assert page["items"][0]["preview"] == {"status": "processing"}
    assert page["items"][0]["work_item"]["available_actions"] == []
    with pytest.raises(ValueError, match="episode preview is still processing"):
        claim_work_item(db_session, work_item_id=item.id, actor_id=actor.id)

    preview_job.status = "failed"
    preview_job.phase = "failed"
    db_session.commit()
    failed_page = list_work_queue(
        db_session, workspace_id=workspace.id, actor_id=actor.id, stage="annotation"
    )
    assert failed_page["items"][0]["preview"] == {"status": "failed"}

    artifact = EpisodeArtifact(
        episode_id=derived.id,
        artifact_type="process_preview",
        storage_role="process",
        storage_uri=f"oss://test-process/preview-{uuid4().hex}.mp4",
    )
    db_session.add(artifact)
    db_session.flush()
    db_session.add(
        ArtifactOperation(
            id=uuid4().hex,
            artifact_id=artifact.id,
            job_id=preview_job.id,
            operation_kind="process_preview_publish",
            status="published",
            target_uri=artifact.storage_uri,
        )
    )
    preview_job.status = "succeeded"
    preview_job.phase = "succeeded"
    db_session.commit()
    db_session.expire_all()

    ready_page = list_work_queue(
        db_session, workspace_id=workspace.id, actor_id=actor.id, stage="annotation"
    )

    assert ready_page["items"][0]["preview"] == {"status": "ready"}
    assert ready_page["items"][0]["work_item"]["available_actions"] == ["claim"]
    claimed = claim_work_item(db_session, work_item_id=item.id, actor_id=actor.id)
    assert claimed.status == "assigned"


def test_derived_work_projects_exact_cut_review_batch_preview_statuses(db_session):
    from data.services.work_queue_projection import create_episode_work_item, list_work_queue

    actor, workspace, source, processing, _quality_failed = _episode_context(db_session)
    processing.metadata_json = {
        **processing.metadata_json,
        "lineage": {"cut_review_work_item_id": 101},
    }
    failed = Episode(
        episode_uid=f"episode-projection-batch-failed-{uuid4().hex}",
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        parent_episode_id=source.id,
        kind="derived",
        derivation_version=1,
        modality=source.modality,
        source_start_ns=10_000_000_000,
        source_end_ns=20_000_000_000,
        quality_status="passed",
        metadata_json={"lineage": {"cut_review_work_item_id": 102}},
    )
    ready = Episode(
        episode_uid=f"episode-projection-batch-ready-{uuid4().hex}",
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        parent_episode_id=source.id,
        kind="derived",
        derivation_version=1,
        modality=source.modality,
        source_start_ns=30_000_000_000,
        source_end_ns=40_000_000_000,
        quality_status="passed",
        metadata_json={"lineage": {"cut_review_work_item_id": 103}},
    )
    db_session.add_all((failed, ready))
    db_session.flush()
    create_episode_work_item(
        db_session, episode_id=processing.id, kind="annotation", actor_id=actor.id
    )
    create_episode_work_item(db_session, episode_id=failed.id, kind="annotation", actor_id=actor.id)
    create_episode_work_item(db_session, episode_id=ready.id, kind="annotation", actor_id=actor.id)
    now = datetime.utcnow()
    running_batch = JobRun(
        id=uuid4().hex,
        kind="derived_preview_batch",
        resource_type="episode",
        resource_id=str(source.id),
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        idempotency_key=f"batch-preview-processing-{uuid4().hex}",
        queue="media",
        status="running",
        phase="running",
        detail_json={"source_episode_id": source.id, "cut_review_work_item_id": 101},
        created_at=now,
        updated_at=now,
    )
    failed_batch = JobRun(
        id=uuid4().hex,
        kind="derived_preview_batch",
        resource_type="episode",
        resource_id=str(source.id),
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        idempotency_key=f"batch-preview-failed-{uuid4().hex}",
        queue="media",
        status="failed",
        phase="failed",
        detail_json={"source_episode_id": source.id, "cut_review_work_item_id": 102},
        created_at=now,
        updated_at=now,
    )
    ready_batch = JobRun(
        id=uuid4().hex,
        kind="derived_preview_batch",
        resource_type="episode",
        resource_id=str(source.id),
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        idempotency_key=f"batch-preview-ready-{uuid4().hex}",
        queue="media",
        status="failed",
        phase="failed",
        detail_json={"source_episode_id": source.id, "cut_review_work_item_id": 103},
        created_at=now,
        updated_at=now,
    )
    unrelated_batch = JobRun(
        id=uuid4().hex,
        kind="derived_preview_batch",
        resource_type="episode",
        resource_id=str(source.id),
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        idempotency_key=f"batch-preview-unrelated-{uuid4().hex}",
        queue="media",
        status="running",
        phase="running",
        detail_json={"source_episode_id": source.id, "cut_review_work_item_id": 999},
        created_at=now + timedelta(seconds=1),
        updated_at=now + timedelta(seconds=1),
    )
    artifact = EpisodeArtifact(
        episode_id=ready.id,
        artifact_type="process_preview",
        storage_role="process",
        storage_uri=f"oss://test-process/batch-preview-{uuid4().hex}.mp4",
    )
    db_session.add_all((running_batch, failed_batch, ready_batch, unrelated_batch, artifact))
    db_session.flush()
    db_session.add(
        ArtifactOperation(
            id=uuid4().hex,
            artifact_id=artifact.id,
            job_id=ready_batch.id,
            operation_kind="process_preview_publish",
            status="published",
            target_uri=artifact.storage_uri,
        )
    )
    db_session.commit()

    page = list_work_queue(
        db_session, workspace_id=workspace.id, actor_id=actor.id, stage="annotation"
    )
    statuses = {row["episode"]["id"]: row["preview"]["status"] for row in page["items"]}

    assert statuses[processing.id] == "processing"
    assert statuses[failed.id] == "failed"
    assert statuses[ready.id] == "ready"


def test_exact_batch_preview_job_overrides_cancelled_legacy_job(db_session):
    from data.services.work_queue_projection import create_episode_work_item, list_work_queue

    actor, workspace, source, derived, _quality_failed = _episode_context(db_session)
    derived.metadata_json = {
        **derived.metadata_json,
        "lineage": {"cut_review_work_item_id": 701},
    }
    create_episode_work_item(
        db_session, episode_id=derived.id, kind="annotation", actor_id=actor.id
    )
    legacy = JobRun(
        id=uuid4().hex,
        kind="episode_preview",
        resource_type="episode",
        resource_id=str(derived.id),
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        idempotency_key=f"legacy-preview-{uuid4().hex}",
        queue="media",
        status="cancelled",
        phase="cancelled",
    )
    batch = JobRun(
        id=uuid4().hex,
        kind="derived_preview_batch",
        resource_type="episode",
        resource_id=str(source.id),
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        idempotency_key=f"batch-preview-{uuid4().hex}",
        queue="media",
        status="running",
        phase="running",
        detail_json={"source_episode_id": source.id, "cut_review_work_item_id": 701},
    )
    db_session.add_all((legacy, batch))
    db_session.commit()

    page = list_work_queue(
        db_session, workspace_id=workspace.id, actor_id=actor.id, stage="annotation"
    )

    assert page["items"][0]["preview"] == {"status": "processing"}


def test_derived_preview_batch_recovery_and_retry_permission_contract(db_session):
    from data.routers.jobs import _RETRY_PERMISSION_BY_KIND
    from data.services.job_runs import job_kind_is_recoverable, retry_terminal_job

    assert job_kind_is_recoverable("derived_preview_batch")
    assert _RETRY_PERMISSION_BY_KIND["derived_preview_batch"] == "episode:write"
    actor, workspace, source, _derived, _quality_failed = _episode_context(db_session)
    failed_job = JobRun(
        id=uuid4().hex,
        kind="derived_preview_batch",
        resource_type="episode",
        resource_id=str(source.id),
        workspace_id=workspace.id,
        task_set_id=source.task_set_id,
        idempotency_key=f"batch-preview-retry-{uuid4().hex}",
        queue="media",
        status="failed",
        phase="failed",
        detail_json={"source_episode_id": source.id, "cut_review_work_item_id": 101},
    )
    db_session.add(failed_job)
    db_session.commit()

    retry = retry_terminal_job(db_session, failed_job.id, actor_id=actor.id)

    assert retry.task_set_id == source.task_set_id
    assert retry.detail_json["source_episode_id"] == source.id
    assert retry.detail_json["cut_review_work_item_id"] == 101


def test_admin_can_surface_release_for_another_assignees_work_item(db_session):
    from data.services.episode_workbench import work_item_available_actions
    from data.services.work_queue_projection import create_episode_work_item

    actor, _workspace, _source, derived, _failed = _episode_context(db_session)
    assert actor.role == "admin"
    assignee = User(
        email=f"queue-assignee-{uuid4().hex}@example.test",
        password_hash="not-used",
        role="annotator",
    )
    db_session.add(assignee)
    db_session.flush()
    item = create_episode_work_item(
        db_session, episode_id=derived.id, kind="annotation", actor_id=actor.id
    )
    item.status = "assigned"
    item.assignee_user_id = assignee.id

    assert work_item_available_actions(item, actor=actor) == ["release"]


def test_release_submit_review_retires_legacy_publication(db_session):
    from data.services.episode_workbench import save_draft
    from data.services.work_queue_projection import (
        claim_work_item,
        continue_work_item,
        create_episode_work_item,
        release_work_item,
        review_work_item,
        submit_work_item,
    )

    actor, _workspace, _source, derived, _failed = _episode_context(db_session)
    annotation = create_episode_work_item(
        db_session, episode_id=derived.id, kind="annotation", actor_id=actor.id
    )
    claim_work_item(db_session, work_item_id=annotation.id, actor_id=actor.id)
    released = release_work_item(db_session, work_item_id=annotation.id, actor_id=actor.id)
    assert released.status == "pending"
    assert released.assignee_user_id is None

    claim_work_item(db_session, work_item_id=annotation.id, actor_id=actor.id)
    continue_work_item(db_session, work_item_id=annotation.id, actor_id=actor.id)
    save_draft(
        db_session,
        work_item_id=annotation.id,
        actor_id=actor.id,
        base_version=0,
        payload={
            "segments": [
                {
                    "id": "segment-a",
                    "start_ns": "1000000000",
                    "end_ns": "2000000000",
                    "description": "pick up the item",
                }
            ],
            "outcome": "success",
            "note": "annotation draft is ready for review",
        },
    )
    review_item = submit_work_item(
        db_session, work_item_id=annotation.id, actor_id=actor.id, base_version=1
    )
    assert annotation.status == "submitted"
    assert review_item.kind == "review"
    claim_work_item(db_session, work_item_id=review_item.id, actor_id=actor.id)
    continue_work_item(db_session, work_item_id=review_item.id, actor_id=actor.id)

    review_work_item(
        db_session,
        work_item_id=review_item.id,
        actor_id=actor.id,
        decision="accepted",
    )
    db_session.commit()
    assert db_session.get(WorkItem, review_item.id).status == "accepted"
    assert (
        db_session.query(JobRun)
        .filter(JobRun.kind == "episode_publish", JobRun.resource_id == str(derived.id))
        .count()
        == 0
    )
    assert (
        db_session.query(EpisodeReview)
        .filter(EpisodeReview.review_work_item_id == review_item.id)
        .count()
        == 1
    )


def test_review_queue_filters_by_review_target_kind(db_session):
    from data.services.work_queue_projection import list_work_queue

    actor, workspace, source, derived, _failed = _episode_context(db_session)
    cut_review = WorkItem(
        workspace_id=workspace.id,
        episode_id=source.id,
        kind="review",
        review_target_kind="cut",
        status="pending",
    )
    annotation_review = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="review",
        review_target_kind="annotation",
        status="pending",
    )
    db_session.add_all((cut_review, annotation_review))
    db_session.commit()

    all_reviews = list_work_queue(
        db_session,
        workspace_id=workspace.id,
        actor_id=actor.id,
        stage="review",
    )
    cut_reviews = list_work_queue(
        db_session,
        workspace_id=workspace.id,
        actor_id=actor.id,
        stage="review",
        review_target_kind="cut",
        limit=1,
    )
    annotation_reviews = list_work_queue(
        db_session,
        workspace_id=workspace.id,
        actor_id=actor.id,
        stage="review",
        review_target_kind="annotation",
    )

    assert all_reviews["total"] == 2
    assert cut_reviews["total"] == 1
    assert [row["work_item"]["id"] for row in cut_reviews["items"]] == [cut_review.id]
    assert annotation_reviews["total"] == 1
    assert [row["work_item"]["id"] for row in annotation_reviews["items"]] == [annotation_review.id]


def test_review_target_kind_requires_review_stage(client, admin_headers, db_session):
    from data.services.work_queue_projection import list_work_queue

    actor, workspace, _source, _derived, _failed = _episode_context(db_session)

    with pytest.raises(ValueError, match="invalid review target kind"):
        list_work_queue(
            db_session,
            workspace_id=workspace.id,
            actor_id=actor.id,
            stage="review",
            review_target_kind="other",
        )

    with pytest.raises(ValueError, match="review target filter requires review stage"):
        list_work_queue(
            db_session,
            workspace_id=workspace.id,
            actor_id=actor.id,
            stage="cut",
            review_target_kind="cut",
        )

    wrong_stage = client.get(
        "/api/v1/work-queue",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "stage": "cut", "review_target_kind": "cut"},
    )
    invalid_kind = client.get(
        "/api/v1/work-queue",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "stage": "review", "review_target_kind": "other"},
    )

    assert wrong_stage.status_code == 422
    assert wrong_stage.json()["detail"] == "review target filter requires review stage"
    assert invalid_kind.status_code == 422


def test_completed_contains_only_accepted_annotation_reviews(db_session):
    from data.services.work_queue_projection import list_work_queue

    actor, workspace, source, derived, _failed = _episode_context(db_session)
    cut = WorkItem(
        workspace_id=workspace.id,
        episode_id=source.id,
        kind="cut",
        status="accepted",
        generation=1,
    )
    annotation = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="annotation",
        status="accepted",
        generation=1,
    )
    rejected_annotation = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="annotation",
        status="rejected",
        generation=2,
    )
    db_session.add_all((cut, annotation, rejected_annotation))
    db_session.flush()
    accepted_cut_review = WorkItem(
        workspace_id=workspace.id,
        episode_id=source.id,
        kind="review",
        review_target_kind="cut",
        review_of_work_item_id=cut.id,
        status="accepted",
        generation=1,
    )
    accepted_annotation_review = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="review",
        review_target_kind="annotation",
        review_of_work_item_id=annotation.id,
        status="accepted",
        generation=1,
    )
    rejected_annotation_review = WorkItem(
        workspace_id=workspace.id,
        episode_id=derived.id,
        kind="review",
        review_target_kind="annotation",
        review_of_work_item_id=rejected_annotation.id,
        status="rejected",
        generation=2,
    )
    db_session.add_all(
        (accepted_cut_review, accepted_annotation_review, rejected_annotation_review)
    )
    db_session.commit()

    page = list_work_queue(
        db_session,
        workspace_id=workspace.id,
        actor_id=actor.id,
        stage="completed",
        limit=100,
    )

    assert page["total"] == 1
    assert [row["work_item"]["id"] for row in page["items"]] == [accepted_annotation_review.id]
    assert page["items"][0]["work_item"]["kind"] == "review"


def test_episode_and_work_queue_api_expose_only_safe_projections(client, admin_headers, db_session):
    from data.services.work_queue_projection import create_episode_work_item

    actor, workspace, _source, derived, _failed = _episode_context(db_session)
    item = create_episode_work_item(
        db_session, episode_id=derived.id, kind="annotation", actor_id=actor.id
    )
    db_session.commit()

    queue = client.get(
        "/api/v1/work-queue",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "stage": "annotation"},
    )
    assert queue.status_code == 200
    row = queue.json()["data"]["items"][0]
    assert row["work_item"]["id"] == item.id
    assert row["work_item"]["available_actions"] == ["claim"]
    assert "metadata_json" not in row

    episode = client.get(f"/api/v1/episodes/{derived.id}", headers=admin_headers)
    assert episode.status_code == 200
    assert episode.json()["data"]["quality"] == {"status": "passed", "restored": False}
    assert "metadata_json" not in episode.json()["data"]

    claimed = client.post(f"/api/v1/work-queue/items/{item.id}/claim", headers=admin_headers)
    assert claimed.status_code == 200
    assert claimed.json()["data"]["work_item"]["work_item"]["status"] == "assigned"


def test_failed_episode_detail_exposes_safe_quality_diagnostic(client, admin_headers, db_session):
    actor, workspace, _source, _derived, failed = _episode_context(db_session)
    job = JobRun(
        id=uuid4().hex,
        kind="episode_quality",
        resource_type="episode",
        resource_id=str(failed.id),
        workspace_id=workspace.id,
        task_set_id=failed.task_set_id,
        idempotency_key=f"quality-diagnostic:{failed.id}",
        queue="media",
        actor_id=actor.id,
        status="failed",
        phase="failed",
        error_code="episode_quality_failed",
        error_message="reference camera timeline does not cover source",
        detail_json={"traceback": "private /opt/quicdata/uat path must not be exposed"},
    )
    db_session.add(job)
    db_session.commit()

    response = client.get(f"/api/v1/episodes/{failed.id}", headers=admin_headers)

    assert response.status_code == 200
    diagnostic = response.json()["data"]["quality_diagnostic"]
    assert diagnostic == {
        "job_id": job.id,
        "error_code": "reference_timeline_incomplete",
        "summary": "reference_timeline_incomplete",
        "phase": "reference_timeline",
        "occurred_at": diagnostic["occurred_at"],
        "attempt": 1,
        "suggested_action": "inspect_recording_and_retry",
        "retry_allowed": True,
    }
    assert "/opt/quicdata" not in response.text


def test_accepted_review_rejects_retired_publication(
    client, admin_headers, db_session, monkeypatch
):
    from data.routers import work_queue
    from data.services.work_queue_projection import create_episode_work_item

    actor, _workspace, _source, derived, _failed = _episode_context(db_session)
    annotation = create_episode_work_item(
        db_session, episode_id=derived.id, kind="annotation", actor_id=actor.id
    )
    db_session.commit()
    dispatched: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        work_queue,
        "dispatch_media_job",
        lambda job, *, worker_prechecked: dispatched.append((job.id, job.queue, worker_prechecked)),
    )

    assert (
        client.post(
            f"/api/v1/work-queue/items/{annotation.id}/claim", headers=admin_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{annotation.id}/continue", headers=admin_headers
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/v1/work-queue/items/{annotation.id}/draft",
            headers=admin_headers,
            json={
                "base_version": 0,
                "payload": {
                    "segments": [
                        {
                            "id": "segment-a",
                            "start_ns": "1000000000",
                            "end_ns": "2000000000",
                            "description": "pick up the item",
                        }
                    ],
                    "outcome": "success",
                    "note": "annotation draft is ready for review",
                },
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{annotation.id}/submit",
            headers=admin_headers,
            json={"base_version": 1},
        ).status_code
        == 200
    )
    review_item = (
        db_session.query(WorkItem)
        .filter(WorkItem.episode_id == derived.id, WorkItem.kind == "review")
        .one()
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{review_item.id}/claim", headers=admin_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/work-queue/items/{review_item.id}/continue", headers=admin_headers
        ).status_code
        == 200
    )

    response = client.post(
        f"/api/v1/work-queue/items/{review_item.id}/review",
        headers=admin_headers,
        json={"decision": "accepted"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["work_item"]["work_item"]["status"] == "accepted"
    assert dispatched == []
    assert (
        db_session.query(JobRun)
        .filter(JobRun.kind == "episode_publish", JobRun.resource_id == str(derived.id))
        .count()
        == 0
    )


def test_annotation_work_cannot_be_created_for_a_source_episode(db_session):
    from data.services.work_queue_projection import create_episode_work_item

    actor, _workspace, source, _derived, _failed = _episode_context(db_session)

    with pytest.raises(ValueError, match="derived"):
        create_episode_work_item(
            db_session, episode_id=source.id, kind="annotation", actor_id=actor.id
        )
