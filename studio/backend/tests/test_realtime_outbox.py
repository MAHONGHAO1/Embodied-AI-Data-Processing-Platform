from datetime import datetime, timezone
from uuid import uuid4

import pytest

from data.database import Batch, JobRun, RealtimeEvent, TaskSet, Workspace
from data.realtime.outbox import (
    enqueue_resource_event,
    enqueue_work_queue_invalidated,
    publish_pending_events,
)


def _batch(db_session):
    suffix = uuid4().hex
    workspace = Workspace(name=f"realtime outbox workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"realtime outbox project {suffix}")
    db_session.add(project)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="realtime batch",
        batch_type="teleop",
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def test_enqueue_increments_resource_version_and_defers_publication(db_session):
    batch = _batch(db_session)

    event = enqueue_resource_event(
        db_session,
        resource=batch,
        resource_type="batch",
        event_name="batch.updated",
        resource_snapshot={"id": batch.id, "status": "created"},
    )

    assert batch.realtime_version == 1
    assert event.resource_version == 1
    assert event.published_at is None
    db_session.commit()

    seen = []
    published = publish_pending_events(
        db_session,
        emit=lambda event_name, payload: seen.append((event_name, payload)),
        now=datetime.now(timezone.utc),
    )

    matching = [
        (event_name, payload)
        for event_name, payload in seen
        if payload["event_id"] == event.event_id
    ]
    assert published == len(seen)
    assert len(matching) == 1
    assert matching[0][0] == "batch.updated"
    assert matching[0][1]["resource_version"] == 1
    assert matching[0][1]["resource"] == {"id": batch.id, "status": "created"}


def test_outbox_rejects_sensitive_snapshot_fields(db_session):
    batch = _batch(db_session)

    with pytest.raises(ValueError, match="unsafe realtime payload field"):
        enqueue_resource_event(
            db_session,
            resource=batch,
            resource_type="batch",
            event_name="batch.updated",
            resource_snapshot={"storage_uri": "oss://private/raw/object"},
        )


def test_work_queue_invalidation_uses_workspace_version_and_safe_payload(db_session):
    batch = _batch(db_session)
    workspace = db_session.get(Workspace, batch.workspace_id)
    assert workspace is not None

    event = enqueue_work_queue_invalidated(db_session, workspace=workspace)

    assert workspace.realtime_version == 1
    assert event.resource_type == "work_queue"
    assert event.resource_id == str(workspace.id)
    assert event.event_name == "work_queue.invalidated"
    assert event.safe_payload == {"workspace_id": workspace.id}


def test_preview_job_event_invalidates_the_workspace_work_queue(db_session):
    from data.tasks.batch_workers import _enqueue_job_event

    batch = _batch(db_session)
    job = JobRun(
        id="preview-realtime-job",
        kind="episode_preview",
        resource_type="episode",
        resource_id="42",
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        idempotency_key="preview-realtime-job",
        queue="media",
        status="running",
        phase="running",
    )
    db_session.add(job)
    db_session.commit()

    _enqueue_job_event(db_session, job)

    events = (
        db_session.query(RealtimeEvent)
        .filter(
            RealtimeEvent.event_name == "work_queue.invalidated",
            RealtimeEvent.resource_id == str(batch.workspace_id),
        )
        .all()
    )
    assert len(events) == 1
    assert events[0].resource_id == str(batch.workspace_id)
    assert events[0].safe_payload == {"workspace_id": batch.workspace_id}


def test_publish_failure_is_recorded_without_reverting_resource_state(db_session):
    batch = _batch(db_session)
    event = enqueue_resource_event(
        db_session,
        resource=batch,
        resource_type="batch",
        event_name="batch.updated",
        resource_snapshot={"id": batch.id, "status": "created"},
    )
    db_session.commit()

    published = publish_pending_events(
        db_session,
        emit=lambda _event_name, _payload: (_ for _ in ()).throw(
            RuntimeError("broker password=private")
        ),
        now=datetime.now(timezone.utc),
    )
    db_session.refresh(batch)
    db_session.refresh(event)

    assert published == 0
    assert batch.realtime_version == 1
    assert event.published_at is None
    assert event.attempt_count == 1
    assert event.last_error == "realtime publish failed"
    assert event.next_attempt_at is not None
