from uuid import uuid4

import pytest

from data.database import (
    Batch,
    ImportSession,
    JobRun,
    TaskSet,
    User,
    Workspace,
    WorkspaceMember,
)
from data.models.native_lerobot_direct import NativeLerobotDirectSource
from data.realtime.subscriptions import (
    SubscriptionDenied,
    resolve_resource_subscription,
)


def _batch(db_session):
    actor = db_session.query(User).filter(User.role == "admin").first()
    suffix = uuid4().hex
    workspace = Workspace(name=f"socket subscription workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"socket subscription project {suffix}")
    db_session.add(project)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="socket subscription batch",
        batch_type="teleop",
    )
    db_session.add(batch)
    db_session.flush()
    return actor, workspace, batch


def test_batch_subscription_uses_server_generated_room(db_session):
    actor, workspace, batch = _batch(db_session)

    subscription = resolve_resource_subscription(
        db_session,
        actor_id=actor.id,
        realtime_session_epoch=actor.realtime_session_epoch,
        resource_type="batch",
        resource_id=str(batch.id),
    )

    assert subscription.workspace_id == workspace.id
    assert subscription.room == (
        f"user:{actor.id}:epoch:{actor.realtime_session_epoch}:workspace:{workspace.id}:batch:{batch.id}"
    )


def test_cross_workspace_subscription_is_denied(db_session):
    _actor, _workspace, batch = _batch(db_session)
    outsider = User(email="socket-outsider@example.com", password_hash="not-used", role="viewer")
    db_session.add(outsider)
    db_session.commit()

    with pytest.raises(SubscriptionDenied, match="access denied"):
        resolve_resource_subscription(
            db_session,
            actor_id=outsider.id,
            realtime_session_epoch=outsider.realtime_session_epoch,
            resource_type="batch",
            resource_id=str(batch.id),
        )


def test_work_queue_subscription_is_limited_to_workspace_scope(db_session):
    actor, workspace, batch = _batch(db_session)
    assert batch.workspace_id == workspace.id

    subscription = resolve_resource_subscription(
        db_session,
        actor_id=actor.id,
        realtime_session_epoch=actor.realtime_session_epoch,
        resource_type="work_queue",
        resource_id=str(workspace.id),
    )

    assert subscription.room == (
        f"user:{actor.id}:epoch:{actor.realtime_session_epoch}:workspace:{workspace.id}:work_queue"
    )


def test_import_session_subscription_accepts_its_uuid_identifier(db_session):
    actor, workspace, batch = _batch(db_session)
    import_session = ImportSession(
        id=str(uuid4()),
        batch_id=batch.id,
        import_type="oss_scan",
        status="init",
        owner_user_id=actor.id,
    )
    db_session.add(import_session)
    db_session.flush()

    subscription = resolve_resource_subscription(
        db_session,
        actor_id=actor.id,
        realtime_session_epoch=actor.realtime_session_epoch,
        resource_type="import_session",
        resource_id=import_session.id,
    )

    assert subscription.workspace_id == workspace.id
    assert subscription.room == (
        f"user:{actor.id}:epoch:{actor.realtime_session_epoch}:workspace:{workspace.id}:"
        f"import_session:{import_session.id}"
    )


def test_job_run_subscription_accepts_its_uuid_identifier(db_session):
    actor, workspace, batch = _batch(db_session)
    job = JobRun(
        id=uuid4().hex,
        kind="episode_quality",
        resource_type="batch",
        resource_id=str(batch.id),
        workspace_id=workspace.id,
        task_set_id=batch.task_set_id,
        idempotency_key=f"subscription-job-{uuid4().hex}",
        queue="media",
    )
    db_session.add(job)
    db_session.flush()

    subscription = resolve_resource_subscription(
        db_session,
        actor_id=actor.id,
        realtime_session_epoch=actor.realtime_session_epoch,
        resource_type="job_run",
        resource_id=job.id,
    )

    assert subscription.workspace_id == workspace.id
    assert subscription.room == (
        f"user:{actor.id}:epoch:{actor.realtime_session_epoch}:workspace:{workspace.id}:job_run:{job.id}"
    )


def test_global_dashboard_job_subscription_is_admin_only(db_session):
    admin = User(
        email="dashboard-socket-admin@example.com",
        password_hash="test",
        role="admin",
        is_active=True,
    )
    operator = User(
        email="dashboard-socket-operator@example.com",
        password_hash="test",
        role="operator",
        is_active=True,
    )
    db_session.add_all([admin, operator])
    db_session.flush()
    job = JobRun(
        id=uuid4().hex,
        kind="dashboard_etl",
        resource_type="platform",
        resource_id="global",
        status="queued",
        queue="general",
        idempotency_key="dashboard-socket-global",
        detail_json={"scope_key": "global", "workspace_id": None, "task_set_id": None},
    )
    db_session.add(job)
    db_session.commit()

    subscription = resolve_resource_subscription(
        db_session,
        actor_id=admin.id,
        realtime_session_epoch=admin.realtime_session_epoch,
        resource_type="job_run",
        resource_id=job.id,
    )
    assert subscription.room.endswith(f":platform:global:job_run:{job.id}")

    with pytest.raises(SubscriptionDenied, match="access denied"):
        resolve_resource_subscription(
            db_session,
            actor_id=operator.id,
            realtime_session_epoch=operator.realtime_session_epoch,
            resource_type="job_run",
            resource_id=job.id,
        )


def test_global_native_lerobot_direct_job_subscription_is_admin_only(db_session):
    admin = User(
        email="direct-source-socket-admin@example.com",
        password_hash="test",
        role="admin",
        is_active=True,
    )
    operator = User(
        email="direct-source-socket-operator@example.com",
        password_hash="test",
        role="operator",
        is_active=True,
    )
    source = NativeLerobotDirectSource(
        id=uuid4().hex,
        name="direct source socket",
        robot_type="unknown",
        dataset_id="direct-source-socket",
        status="queued",
        file_count=1,
        total_size=1,
        manifest_sha256="a" * 64,
    )
    job = JobRun(
        id=uuid4().hex,
        kind="native_lerobot_direct_validate",
        resource_type="native_lerobot_direct_source",
        resource_id=source.id,
        status="queued",
        queue="ingest",
        idempotency_key="direct-source-socket-job",
        detail_json={"native_lerobot_direct_source_id": source.id},
    )
    source.validation_job_id = job.id
    db_session.add_all([admin, operator, source, job])
    db_session.commit()

    subscription = resolve_resource_subscription(
        db_session,
        actor_id=admin.id,
        realtime_session_epoch=admin.realtime_session_epoch,
        resource_type="job_run",
        resource_id=job.id,
    )
    assert subscription.workspace_id == 0
    assert subscription.room.endswith(f":native_lerobot_direct_source:global:job_run:{job.id}")

    with pytest.raises(SubscriptionDenied, match="access denied"):
        resolve_resource_subscription(
            db_session,
            actor_id=operator.id,
            realtime_session_epoch=operator.realtime_session_epoch,
            resource_type="job_run",
            resource_id=job.id,
        )


def test_workspace_dashboard_job_subscription_requires_dashboard_read(db_session):
    _actor, workspace, batch = _batch(db_session)
    assert batch.workspace_id == workspace.id
    viewer = User(
        email="dashboard-socket-viewer@example.com",
        password_hash="test",
        role="viewer",
        is_active=True,
    )
    db_session.add(viewer)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=viewer.id))
    job = JobRun(
        id=uuid4().hex,
        kind="dashboard_etl",
        resource_type="platform",
        resource_id=f"workspace:{workspace.id}",
        workspace_id=workspace.id,
        status="queued",
        queue="general",
        idempotency_key=f"dashboard-socket-workspace-{workspace.id}",
        detail_json={
            "scope_key": f"workspace:{workspace.id}",
            "workspace_id": workspace.id,
            "task_set_id": None,
        },
    )
    db_session.add(job)
    db_session.commit()

    with pytest.raises(SubscriptionDenied, match="access denied"):
        resolve_resource_subscription(
            db_session,
            actor_id=viewer.id,
            realtime_session_epoch=viewer.realtime_session_epoch,
            resource_type="job_run",
            resource_id=job.id,
        )


def test_dashboard_job_subscription_rejects_inconsistent_persisted_scope(db_session):
    admin = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    job = JobRun(
        id=uuid4().hex,
        kind="dashboard_etl",
        resource_type="platform",
        resource_id="global",
        workspace_id=1,
        status="queued",
        queue="general",
        idempotency_key=f"dashboard-socket-forged-{uuid4().hex}",
        detail_json={"scope_key": "global", "workspace_id": None, "task_set_id": None},
    )
    db_session.add(job)
    db_session.commit()

    with pytest.raises(SubscriptionDenied, match="access denied"):
        resolve_resource_subscription(
            db_session,
            actor_id=admin.id,
            realtime_session_epoch=admin.realtime_session_epoch,
            resource_type="job_run",
            resource_id=job.id,
        )
