from datetime import datetime, timedelta
from uuid import uuid4

from data.database import (
    ArtifactOperation,
    Batch,
    EpisodeArtifact,
    ImportSession,
    JobQueueSlot,
    JobRun,
    RealtimeEvent,
    SecurityAuditEvent,
    TaskSet,
    Workspace,
)
from data.services.runtime_retention import cleanup_runtime_rows


def _batch(db_session) -> Batch:
    suffix = uuid4().hex
    workspace = Workspace(name=f"retention workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"retention task set {suffix}")
    db_session.add(task_set)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="retention batch",
        batch_type="ego",
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def _job(job_id: str, *, status: str, moment: datetime) -> JobRun:
    return JobRun(
        id=job_id,
        kind="dashboard_etl",
        resource_type="platform",
        resource_id="global",
        idempotency_key=f"retention:{job_id}",
        queue="analytics",
        status=status,
        phase=status,
        created_at=moment,
        updated_at=moment,
        finished_at=moment if status in {"succeeded", "failed", "cancelled"} else None,
    )


def test_runtime_retention_deletes_only_expired_terminal_rows_and_keeps_audit(db_session):
    now = datetime(2026, 8, 21, 12, 0)
    old = now - timedelta(days=45)
    recent = now - timedelta(days=2)
    batch = _batch(db_session)

    old_event = RealtimeEvent(
        event_id="retention-event-old",
        resource_type="batch",
        resource_id=str(batch.id),
        resource_version=1,
        event_name="batch.updated",
        safe_payload={"id": batch.id},
        created_at=old,
        published_at=old,
    )
    recent_event = RealtimeEvent(
        event_id="retention-event-recent",
        resource_type="batch",
        resource_id=str(batch.id),
        resource_version=2,
        event_name="batch.updated",
        safe_payload={"id": batch.id},
        created_at=recent,
        published_at=recent,
    )
    pending_event = RealtimeEvent(
        event_id="retention-event-pending",
        resource_type="batch",
        resource_id=str(batch.id),
        resource_version=3,
        event_name="batch.updated",
        safe_payload={"id": batch.id},
        created_at=old,
    )
    old_job = _job("retention-job-old", status="succeeded", moment=old)
    recent_job = _job("retention-job-recent", status="failed", moment=recent)
    queued_job = _job("retention-job-queued", status="queued", moment=old)
    old_session = ImportSession(
        id="retention-import-old",
        batch_id=batch.id,
        import_type="chunked_upload",
        status="cancelled",
        original_name="old.qrdf.zip",
        created_at=old,
        updated_at=old,
    )
    recent_session = ImportSession(
        id="retention-import-recent",
        batch_id=batch.id,
        import_type="chunked_upload",
        status="cancelled",
        original_name="recent.qrdf.zip",
        created_at=recent,
        updated_at=recent,
    )
    audit = SecurityAuditEvent(
        action="retention.test",
        result="success",
        occurred_at=old,
    )
    db_session.add_all(
        [
            old_event,
            recent_event,
            pending_event,
            old_job,
            recent_job,
            queued_job,
            old_session,
            recent_session,
            audit,
        ]
    )
    db_session.commit()
    old_event_id = old_event.event_id
    recent_event_id = recent_event.event_id
    pending_event_id = pending_event.event_id
    old_job_id = old_job.id
    recent_job_id = recent_job.id
    queued_job_id = queued_job.id
    old_session_id = old_session.id
    recent_session_id = recent_session.id
    audit_id = audit.id

    result = cleanup_runtime_rows(
        db_session,
        now=now,
        realtime_event_retention_days=7,
        job_retention_days=30,
        import_session_retention_days=30,
        batch_size=100,
        expire_direct_uploads=False,
    )
    db_session.commit()

    assert result["realtime_events_deleted"] >= 1
    assert result["job_runs_deleted"] >= 1
    assert result["import_sessions_deleted"] >= 1
    assert db_session.get(RealtimeEvent, old_event_id) is None
    assert db_session.get(RealtimeEvent, recent_event_id) is not None
    assert db_session.get(RealtimeEvent, pending_event_id) is not None
    assert db_session.get(JobRun, old_job_id) is None
    assert db_session.get(JobRun, recent_job_id) is not None
    assert db_session.get(JobRun, queued_job_id) is not None
    assert db_session.get(ImportSession, old_session_id) is None
    assert db_session.get(ImportSession, recent_session_id) is not None
    assert db_session.get(SecurityAuditEvent, audit_id) is not None


def test_runtime_retention_unlinks_terminal_job_references_and_keeps_artifacts(db_session):
    now = datetime(2026, 8, 21, 12, 0)
    old = now - timedelta(days=45)
    recent = now - timedelta(days=2)
    batch = _batch(db_session)
    import_session = ImportSession(
        id="retention-import-with-operation",
        batch_id=batch.id,
        import_type="chunked_upload",
        status="cancelled",
        original_name="retained.qrdf.zip",
        created_at=recent,
        updated_at=recent,
    )
    db_session.add(import_session)
    db_session.flush()
    artifact = EpisodeArtifact(
        import_session_id=import_session.id,
        artifact_type="import_original",
        storage_role="raw",
        storage_uri="nas://raw/retention/import-original.qrdf.zip",
        retention_policy="permanent",
    )
    old_job = _job("retention-job-with-references", status="succeeded", moment=old)
    db_session.add_all([artifact, old_job])
    db_session.flush()
    operation = ArtifactOperation(
        id="retention-operation",
        artifact_id=artifact.id,
        job_id=old_job.id,
        operation_kind="import_original_publish",
        status="published",
        target_uri=artifact.storage_uri,
    )
    slot = JobQueueSlot(
        queue="analytics",
        slot_number=999,
        job_id=old_job.id,
        worker_id="retention-worker",
        lease_expires_at=recent,
    )
    db_session.add_all([operation, slot])
    db_session.commit()
    old_job_id = old_job.id
    artifact_id = artifact.id
    import_session_id = import_session.id

    result = cleanup_runtime_rows(
        db_session,
        now=now,
        realtime_event_retention_days=7,
        job_retention_days=30,
        import_session_retention_days=30,
        batch_size=100,
        expire_direct_uploads=False,
    )
    db_session.commit()

    assert result["job_runs_deleted"] >= 1
    assert db_session.get(JobRun, old_job_id) is None
    db_session.refresh(operation)
    db_session.refresh(slot)
    assert operation.job_id is None
    assert slot.job_id is None
    assert slot.worker_id == ""
    assert slot.lease_expires_at is None
    assert db_session.get(EpisodeArtifact, artifact_id) is not None
    assert db_session.get(ImportSession, import_session_id) is not None


def test_runtime_retention_includes_bounded_native_lerobot_bundle_cleanup(db_session, monkeypatch):
    from data.services import runtime_retention

    calls: list[tuple[int, datetime]] = []
    monkeypatch.setattr(
        runtime_retention,
        "cleanup_expired_native_lerobot_bundles",
        lambda db, *, limit, now: calls.append((limit, now)) or 3,
    )

    now = datetime(2026, 8, 28, 12, 0)
    result = cleanup_runtime_rows(
        db_session,
        now=now,
        realtime_event_retention_days=7,
        job_retention_days=30,
        import_session_retention_days=30,
        batch_size=12,
        expire_direct_uploads=False,
    )

    assert result["native_lerobot_bundles_expired"] == 3
    assert calls == [(12, now)]
