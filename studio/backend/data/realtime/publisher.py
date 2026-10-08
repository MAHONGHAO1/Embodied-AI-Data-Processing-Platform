"""Publish committed realtime outbox records through server-derived rooms."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import socketio
from sqlalchemy.orm import Session

from data.database import (
    Batch,
    Episode,
    ImportSession,
    JobRun,
    NativeLerobotImportSession,
    NativeLerobotScanSnapshot,
    TaskSet,
    User,
    WorkItem,
    WorkspaceMember,
)
from data.realtime.outbox import publish_pending_events_async
from data.services.job_access import job_has_consistent_resource_scope


async def publish_realtime_events(
    db: Session,
    *,
    server: socketio.AsyncServer,
    now: datetime,
    limit: int = 100,
) -> int:
    """Deliver eligible outbox rows through rooms resolved from current ownership."""

    async def emit(event_name: str, payload: dict[str, Any]) -> None:
        rooms = _rooms_for_resource(
            db,
            resource_type=str(payload["resource_type"]),
            resource_id=str(payload["resource_id"]),
        )
        for room in rooms:
            await server.emit(event_name, payload, room=room, namespace="/")

    return await publish_pending_events_async(
        db,
        emit=emit,
        now=now,
        limit=limit,
    )


def _rooms_for_resource(db: Session, *, resource_type: str, resource_id: str) -> tuple[str, ...]:
    workspace_id: int
    resource_fragment: str
    if resource_type == "batch":
        batch = db.get(Batch, _positive_id(resource_id))
        if batch is not None:
            workspace_id = batch.workspace_id
            resource_fragment = f"batch:{batch.id}"
        else:
            raise ValueError("realtime resource ownership is unavailable")
    elif resource_type == "import_session":
        import_session = db.get(ImportSession, resource_id)
        if import_session is not None:
            batch = db.get(Batch, import_session.batch_id)
            if batch is not None:
                workspace_id = batch.workspace_id
                resource_fragment = f"import_session:{import_session.id}"
            else:
                raise ValueError("realtime resource ownership is unavailable")
        else:
            raise ValueError("realtime resource ownership is unavailable")
    elif resource_type == "native_lerobot_scan_snapshot":
        snapshot = db.get(NativeLerobotScanSnapshot, resource_id)
        task_set = db.get(TaskSet, snapshot.task_set_id) if snapshot is not None else None
        if snapshot is None or task_set is None or task_set.workspace_id != snapshot.workspace_id:
            raise ValueError("realtime resource ownership is unavailable")
        workspace_id = snapshot.workspace_id
        resource_fragment = f"native_lerobot_scan_snapshot:{snapshot.id}"
    elif resource_type == "native_lerobot_import_session":
        session = db.get(NativeLerobotImportSession, resource_id)
        batch = db.get(Batch, session.batch_id) if session is not None else None
        if (
            session is None
            or batch is None
            or batch.batch_type != "lerobot"
            or batch.workspace_id != session.workspace_id
            or batch.task_set_id != session.task_set_id
        ):
            raise ValueError("realtime resource ownership is unavailable")
        workspace_id = session.workspace_id
        resource_fragment = f"native_lerobot_import_session:{session.id}"
    elif resource_type == "episode":
        episode = db.get(Episode, _positive_id(resource_id))
        if episode is not None:
            workspace_id = episode.workspace_id
            resource_fragment = f"episode:{episode.id}"
        else:
            raise ValueError("realtime resource ownership is unavailable")
    elif resource_type == "work_item":
        item = db.get(WorkItem, _positive_id(resource_id))
        if item is not None:
            workspace_id = item.workspace_id
            resource_fragment = f"work_item:{item.id}"
        else:
            raise ValueError("realtime resource ownership is unavailable")
    elif resource_type == "job_run":
        job = db.get(JobRun, resource_id)
        if job is None or not job_has_consistent_resource_scope(db, job=job):
            raise ValueError("realtime resource ownership is unavailable")
        if job.workspace_id is not None:
            workspace_id = job.workspace_id
            resource_fragment = f"job_run:{job.id}"
        elif job.resource_type == "platform" and job.resource_id == "global":
            return tuple(
                f"user:{user_id}:epoch:{session_epoch}:platform:global:job_run:{job.id}"
                for user_id, session_epoch in db.query(User.id, User.realtime_session_epoch)
                .filter(User.role == "admin", User.is_active.is_(True))
                .all()
            )
        elif job.resource_type == "native_lerobot_direct_source":
            return tuple(
                f"user:{user_id}:epoch:{session_epoch}:"
                f"native_lerobot_direct_source:global:job_run:{job.id}"
                for user_id, session_epoch in db.query(User.id, User.realtime_session_epoch)
                .filter(User.role == "admin", User.is_active.is_(True))
                .all()
            )
        else:
            raise ValueError("realtime resource ownership is unavailable")
    elif resource_type == "work_queue":
        workspace_id = _positive_id(resource_id)
        resource_fragment = "work_queue"
    else:
        raise ValueError("realtime resource ownership is unavailable")
    return tuple(
        f"user:{user_id}:epoch:{session_epoch}:workspace:{workspace_id}:{resource_fragment}"
        for user_id, session_epoch in _workspace_recipients(db, workspace_id)
    )


def _workspace_recipients(db: Session, workspace_id: int) -> tuple[tuple[int, int], ...]:
    member_rows = (
        db.query(User.id, User.realtime_session_epoch)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .filter(WorkspaceMember.workspace_id == workspace_id, User.is_active.is_(True))
        .all()
    )
    admin_rows = (
        db.query(User.id, User.realtime_session_epoch)
        .filter(User.role == "admin", User.is_active.is_(True))
        .all()
    )
    return tuple(sorted(set(member_rows).union(admin_rows)))


def _positive_id(value: str) -> int:
    try:
        identifier = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("realtime resource ownership is unavailable") from exc
    if identifier < 1:
        raise ValueError("realtime resource ownership is unavailable")
    return identifier
