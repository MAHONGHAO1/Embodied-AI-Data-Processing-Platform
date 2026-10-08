"""Server-derived Socket.IO rooms with object-level authorization."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

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
)
from data.services.job_access import actor_can_access_job
from data.services.workspace_access import (
    require_batch_actor,
    require_episode_actor,
    require_import_session_actor,
    require_workspace_actor,
)


class SubscriptionDenied(PermissionError):
    """Raised for malformed, unresolved, or unauthorized resource subscriptions."""


@dataclass(frozen=True)
class ResourceSubscription:
    resource_type: str
    resource_id: str
    workspace_id: int
    room: str


def resolve_resource_subscription(
    db: Session,
    *,
    actor_id: int,
    realtime_session_epoch: int,
    resource_type: str,
    resource_id: str,
) -> ResourceSubscription:
    """Authorize a resource and return only a server-controlled private room."""
    try:
        actor = db.get(User, actor_id)
        if actor is None or actor.realtime_session_epoch != realtime_session_epoch:
            raise SubscriptionDenied("access denied")
        if resource_type == "batch":
            identifier = _positive_identifier(resource_id)
            batch = db.get(Batch, identifier)
            if batch is None:
                raise SubscriptionDenied("access denied")
            require_batch_actor(db, actor_id=actor_id, batch=batch)
            return _subscription(
                actor_id, realtime_session_epoch, resource_type, identifier, batch.workspace_id
            )
        if resource_type == "import_session":
            import_session = db.get(ImportSession, _uuid_identifier(resource_id, compact=False))
            if import_session is None:
                raise SubscriptionDenied("access denied")
            require_import_session_actor(db, actor_id=actor_id, import_session=import_session)
            batch = db.get(Batch, import_session.batch_id)
            if batch is None:
                raise SubscriptionDenied("access denied")
            return _subscription(
                actor_id,
                realtime_session_epoch,
                resource_type,
                import_session.id,
                batch.workspace_id,
            )
        if resource_type == "native_lerobot_scan_snapshot":
            snapshot = db.get(
                NativeLerobotScanSnapshot, _uuid_identifier(resource_id, compact=False)
            )
            if snapshot is None:
                raise SubscriptionDenied("access denied")
            task_set = db.get(TaskSet, snapshot.task_set_id)
            if task_set is None or task_set.workspace_id != snapshot.workspace_id:
                raise SubscriptionDenied("access denied")
            require_workspace_actor(db, actor_id=actor_id, workspace_id=snapshot.workspace_id)
            return _subscription(
                actor_id, realtime_session_epoch, resource_type, snapshot.id, snapshot.workspace_id
            )
        if resource_type == "native_lerobot_import_session":
            session = db.get(
                NativeLerobotImportSession, _uuid_identifier(resource_id, compact=False)
            )
            if session is None:
                raise SubscriptionDenied("access denied")
            batch = db.get(Batch, session.batch_id)
            if (
                batch is None
                or batch.batch_type != "lerobot"
                or batch.workspace_id != session.workspace_id
                or batch.task_set_id != session.task_set_id
            ):
                raise SubscriptionDenied("access denied")
            require_workspace_actor(db, actor_id=actor_id, workspace_id=session.workspace_id)
            return _subscription(
                actor_id, realtime_session_epoch, resource_type, session.id, session.workspace_id
            )
        if resource_type == "episode":
            identifier = _positive_identifier(resource_id)
            episode = db.get(Episode, identifier)
            if episode is None:
                raise SubscriptionDenied("access denied")
            require_episode_actor(db, actor_id=actor_id, episode=episode)
            return _subscription(
                actor_id, realtime_session_epoch, resource_type, identifier, episode.workspace_id
            )
        if resource_type == "work_item":
            identifier = _positive_identifier(resource_id)
            item = db.get(WorkItem, identifier)
            if item is None:
                raise SubscriptionDenied("access denied")
            require_workspace_actor(db, actor_id=actor_id, workspace_id=item.workspace_id)
            return _subscription(
                actor_id, realtime_session_epoch, resource_type, identifier, item.workspace_id
            )
        if resource_type == "job_run":
            job = db.get(JobRun, _uuid_identifier(resource_id, compact=True))
            if job is None or not actor_can_access_job(db, actor_id=actor_id, job=job):
                raise SubscriptionDenied("access denied")
            if job.workspace_id is None:
                if job.resource_type == "platform" and job.resource_id == "global":
                    room_scope = "platform:global"
                elif job.resource_type == "native_lerobot_direct_source":
                    room_scope = "native_lerobot_direct_source:global"
                else:
                    raise SubscriptionDenied("access denied")
                return ResourceSubscription(
                    resource_type=resource_type,
                    resource_id=job.id,
                    workspace_id=0,
                    room=(
                        f"user:{actor_id}:epoch:{realtime_session_epoch}:"
                        f"{room_scope}:job_run:{job.id}"
                    ),
                )
            require_workspace_actor(db, actor_id=actor_id, workspace_id=job.workspace_id)
            return _subscription(
                actor_id, realtime_session_epoch, resource_type, job.id, job.workspace_id
            )
        if resource_type == "work_queue":
            identifier = _positive_identifier(resource_id)
            require_workspace_actor(db, actor_id=actor_id, workspace_id=identifier)
            return ResourceSubscription(
                resource_type=resource_type,
                resource_id=str(identifier),
                workspace_id=identifier,
                room=f"user:{actor_id}:epoch:{realtime_session_epoch}:workspace:{identifier}:work_queue",
            )
    except (PermissionError, ValueError):
        raise SubscriptionDenied("access denied") from None
    raise SubscriptionDenied("access denied")


def _positive_identifier(value: str) -> int:
    try:
        identifier = int(value)
    except (TypeError, ValueError) as exc:
        raise SubscriptionDenied("access denied") from exc
    if identifier < 1:
        raise SubscriptionDenied("access denied")
    return identifier


def _uuid_identifier(value: str, *, compact: bool) -> str:
    try:
        raw = str(value)
        parsed = UUID(raw)
    except (TypeError, ValueError, AttributeError) as exc:
        raise SubscriptionDenied("access denied") from exc
    canonical = parsed.hex if compact else str(parsed)
    if raw != canonical:
        raise SubscriptionDenied("access denied")
    return canonical


def _subscription(
    actor_id: int,
    realtime_session_epoch: int,
    resource_type: str,
    resource_id: str | int,
    workspace_id: int,
) -> ResourceSubscription:
    return ResourceSubscription(
        resource_type=resource_type,
        resource_id=str(resource_id),
        workspace_id=workspace_id,
        room=(
            f"user:{actor_id}:epoch:{realtime_session_epoch}:workspace:{workspace_id}:"
            f"{resource_type}:{resource_id}"
        ),
    )
