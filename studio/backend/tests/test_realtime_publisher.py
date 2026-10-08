import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from data.database import (
    Batch,
    JobRun,
    NativeLerobotImportSession,
    NativeLerobotScanSnapshot,
    RealtimeEvent,
    TaskSet,
    User,
    Workspace,
    WorkspaceMember,
)
from data.models.native_lerobot_direct import NativeLerobotDirectSource
from data.realtime.outbox import enqueue_resource_event, enqueue_work_queue_invalidated
from data.realtime.projections import (
    native_lerobot_import_session_snapshot,
    native_lerobot_scan_snapshot,
)
from data.realtime.publisher import publish_realtime_events


class RecordingSocketServer:
    def __init__(self):
        self.messages = []

    async def emit(self, event_name, payload, *, room, namespace):
        self.messages.append((event_name, payload, room, namespace))


def _batch(db_session):
    suffix = uuid4().hex
    workspace = Workspace(name=f"realtime publisher workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"realtime publisher project {suffix}")
    db_session.add(project)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="realtime publisher batch",
        batch_type="teleop",
    )
    db_session.add(batch)
    db_session.flush()
    return workspace, batch


def test_publisher_routes_committed_event_to_server_derived_room(db_session):
    workspace, batch = _batch(db_session)
    inactive_member = User(
        email="inactive-realtime-member@example.com",
        password_hash="test",
        role="viewer",
        is_active=False,
    )
    db_session.add(inactive_member)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=inactive_member.id))
    enqueue_resource_event(
        db_session,
        resource=batch,
        resource_type="batch",
        event_name="batch.updated",
        resource_snapshot={"id": batch.id, "status": "created"},
    )
    db_session.commit()
    server = RecordingSocketServer()

    published = asyncio.run(
        publish_realtime_events(
            db_session,
            server=server,
            now=datetime.now(timezone.utc),
        )
    )

    assert published == 1
    assert server.messages[0][0] == "batch.updated"
    admin = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    assert (
        f"user:{admin.id}:epoch:{admin.realtime_session_epoch}:workspace:{workspace.id}:batch:{batch.id}"
        in {message[2] for message in server.messages}
    )
    inactive_rooms = {
        f"user:{user.id}:epoch:{user.realtime_session_epoch}:workspace:{workspace.id}:batch:{batch.id}"
        for user in (inactive_member,)
    }
    assert inactive_rooms.isdisjoint({message[2] for message in server.messages})
    assert server.messages[0][3] == "/"


def test_publisher_routes_work_queue_invalidation_to_workspace_room(db_session):
    workspace, _created_batch = _batch(db_session)
    enqueue_work_queue_invalidated(db_session, workspace=workspace)
    db_session.commit()
    server = RecordingSocketServer()

    published = asyncio.run(
        publish_realtime_events(
            db_session,
            server=server,
            now=datetime.now(timezone.utc),
        )
    )

    assert published == 1
    assert server.messages[0][0] == "work_queue.invalidated"
    assert server.messages[0][1]["resource"] == {"workspace_id": workspace.id}
    admin = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    assert (
        f"user:{admin.id}:epoch:{admin.realtime_session_epoch}:workspace:{workspace.id}:work_queue"
        in {message[2] for message in server.messages}
    )


def test_publisher_routes_native_lerobot_realtime_events_to_workspace_members(db_session):
    workspace, batch = _batch(db_session)
    batch.batch_type = "lerobot"
    snapshot = NativeLerobotScanSnapshot(
        id=str(uuid4()),
        workspace_id=workspace.id,
        task_set_id=batch.task_set_id,
        status="succeeded",
        is_current=True,
    )
    member = User(
        email=f"native-realtime-member-{uuid4().hex}@example.test",
        password_hash="test",
        role="viewer",
    )
    db_session.add_all((snapshot, member))
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=member.id))
    session = NativeLerobotImportSession(
        id=str(uuid4()),
        batch_id=batch.id,
        workspace_id=workspace.id,
        task_set_id=batch.task_set_id,
        scan_snapshot_id=snapshot.id,
        created_by_user_id=member.id,
    )
    db_session.add(session)
    db_session.flush()
    enqueue_resource_event(
        db_session,
        resource=snapshot,
        resource_type="native_lerobot_scan_snapshot",
        event_name="native_lerobot_scan_snapshot.updated",
        resource_snapshot=native_lerobot_scan_snapshot(snapshot),
    )
    enqueue_resource_event(
        db_session,
        resource=session,
        resource_type="native_lerobot_import_session",
        event_name="native_lerobot_import_session.updated",
        resource_snapshot=native_lerobot_import_session_snapshot(session),
    )
    db_session.commit()
    server = RecordingSocketServer()

    assert (
        asyncio.run(
            publish_realtime_events(db_session, server=server, now=datetime.now(timezone.utc))
        )
        == 2
    )
    rooms = {message[2] for message in server.messages}
    assert (
        f"user:{member.id}:epoch:{member.realtime_session_epoch}:workspace:{workspace.id}:"
        f"native_lerobot_scan_snapshot:{snapshot.id}"
    ) in rooms
    assert (
        f"user:{member.id}:epoch:{member.realtime_session_epoch}:workspace:{workspace.id}:"
        f"native_lerobot_import_session:{session.id}"
    ) in rooms


def test_publisher_routes_global_dashboard_job_only_to_active_admins(db_session):
    job = JobRun(
        id="dashboard-global-publisher-test",
        kind="dashboard_etl",
        resource_type="platform",
        resource_id="global",
        status="queued",
        progress_percent=0,
        idempotency_key="dashboard-global-publisher-test",
        queue="general",
        detail_json={"scope_key": "global", "workspace_id": None, "task_set_id": None},
    )
    db_session.add(job)
    db_session.flush()
    enqueue_resource_event(
        db_session,
        resource=job,
        resource_type="job_run",
        event_name="job_run.updated",
        resource_snapshot={"id": job.id, "status": job.status},
    )
    db_session.commit()
    server = RecordingSocketServer()

    published = asyncio.run(
        publish_realtime_events(
            db_session,
            server=server,
            now=datetime.now(timezone.utc),
        )
    )

    active_admins = (
        db_session.query(User)
        .filter(User.role == "admin", User.is_active.is_(True))
        .order_by(User.id)
        .all()
    )
    assert published == 1
    assert {message[2] for message in server.messages} == {
        f"user:{admin.id}:epoch:{admin.realtime_session_epoch}:platform:global:job_run:{job.id}"
        for admin in active_admins
    }
    assert {message[0] for message in server.messages} == {"job_run.updated"}


def test_publisher_routes_global_native_lerobot_direct_job_only_to_active_admins(db_session):
    source = NativeLerobotDirectSource(
        id=uuid4().hex,
        name="direct source publisher",
        robot_type="unknown",
        dataset_id="direct-source-publisher",
        status="queued",
        file_count=1,
        total_size=1,
        manifest_sha256="b" * 64,
    )
    job = JobRun(
        id=uuid4().hex,
        kind="native_lerobot_direct_validate",
        resource_type="native_lerobot_direct_source",
        resource_id=source.id,
        status="queued",
        progress_percent=0,
        idempotency_key="direct-source-publisher-job",
        queue="ingest",
        detail_json={"native_lerobot_direct_source_id": source.id},
    )
    source.validation_job_id = job.id
    db_session.add_all((source, job))
    db_session.flush()
    enqueue_resource_event(
        db_session,
        resource=job,
        resource_type="job_run",
        event_name="job_run.updated",
        resource_snapshot={"id": job.id, "status": job.status},
    )
    db_session.commit()
    server = RecordingSocketServer()

    published = asyncio.run(
        publish_realtime_events(
            db_session,
            server=server,
            now=datetime.now(timezone.utc),
        )
    )

    active_admins = (
        db_session.query(User)
        .filter(User.role == "admin", User.is_active.is_(True))
        .order_by(User.id)
        .all()
    )
    assert published == 1
    assert {message[2] for message in server.messages} == {
        f"user:{admin.id}:epoch:{admin.realtime_session_epoch}:"
        f"native_lerobot_direct_source:global:job_run:{job.id}"
        for admin in active_admins
    }


def test_publisher_rejects_inconsistent_dashboard_job_scope(db_session):
    workspace, _batch_row = _batch(db_session)
    job = JobRun(
        id="dashboard-forged-publisher-test",
        kind="dashboard_etl",
        resource_type="platform",
        resource_id=f"workspace:{workspace.id}",
        workspace_id=workspace.id,
        status="queued",
        progress_percent=0,
        idempotency_key="dashboard-forged-publisher-test",
        queue="general",
        detail_json={"scope_key": "global"},
    )
    db_session.add(job)
    db_session.flush()
    enqueue_resource_event(
        db_session,
        resource=job,
        resource_type="job_run",
        event_name="job_run.updated",
        resource_snapshot={"id": job.id, "status": job.status},
    )
    db_session.commit()

    server = RecordingSocketServer()
    published = asyncio.run(
        publish_realtime_events(
            db_session,
            server=server,
            now=datetime.now(timezone.utc),
        )
    )

    event = db_session.query(RealtimeEvent).filter_by(resource_id=job.id).one()
    assert published == 0
    assert server.messages == []
    assert event.published_at is None
    assert event.attempt_count == 1
    assert event.last_error == "realtime publish failed"
