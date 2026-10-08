"""Transactional outbox for idempotent, user-safe realtime resource updates."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import inspect, or_, select, update
from sqlalchemy.orm import Session

from data.database import (
    Batch,
    Episode,
    ImportSession,
    JobRun,
    NativeLerobotImportSession,
    NativeLerobotScanSnapshot,
    RealtimeEvent,
    WorkItem,
    Workspace,
)

RESOURCE_MODELS = {
    "work_queue": Workspace,
    "batch": Batch,
    "episode": Episode,
    "import_session": ImportSession,
    "job_run": JobRun,
    "work_item": WorkItem,
    "native_lerobot_scan_snapshot": NativeLerobotScanSnapshot,
    "native_lerobot_import_session": NativeLerobotImportSession,
}
ALLOWED_EVENT_NAMES = frozenset(
    {
        "batch.updated",
        "import_session.updated",
        "job_run.updated",
        "episode.updated",
        "work_item.updated",
        "publication.updated",
        "work_queue.invalidated",
        "native_lerobot_scan_snapshot.updated",
        "native_lerobot_import_session.updated",
    }
)
_UNSAFE_EXACT_KEYS = frozenset(
    {
        "access_key",
        "detail_json",
        "local_path",
        "metadata_json",
        "result_json",
        "secret",
        "signature",
        "signed_url",
        "storage_uri",
        "traceback",
        "uri",
        "url",
    }
)


def enqueue_resource_event(
    db: Session,
    *,
    resource: Any,
    resource_type: str,
    event_name: str,
    resource_snapshot: dict[str, Any],
) -> RealtimeEvent:
    """Increment one locked resource revision and enqueue its durable update."""
    model = RESOURCE_MODELS.get(resource_type)
    if model is None or not isinstance(resource, model):
        raise ValueError("resource_type does not match realtime resource")
    if event_name not in ALLOWED_EVENT_NAMES:
        raise ValueError("unsupported realtime event")
    _require_safe_snapshot(resource_snapshot)

    mapper = inspect(resource).mapper
    primary_key = mapper.primary_key[0]
    resource_id = getattr(resource, primary_key.key)
    if resource_id is None:
        raise ValueError("realtime resource must be flushed before enqueue")

    result = db.execute(
        update(model)
        .where(primary_key == resource_id)
        .values(realtime_version=model.realtime_version + 1)
        .returning(model.realtime_version),
        execution_options={"synchronize_session": False},
    )
    resource_version = result.scalar_one_or_none()
    if resource_version is None:
        raise ValueError("realtime resource is unavailable")

    resource.realtime_version = int(resource_version)
    event = RealtimeEvent(
        event_id=str(uuid4()),
        resource_type=resource_type,
        resource_id=str(resource_id),
        resource_version=int(resource_version),
        event_name=event_name,
        safe_payload=dict(resource_snapshot),
    )
    db.add(event)
    db.flush()
    return event


def enqueue_work_queue_invalidated(db: Session, *, workspace: Workspace) -> RealtimeEvent:
    """Notify authorized workspace members to refresh their server-paged queue."""
    return enqueue_resource_event(
        db,
        resource=workspace,
        resource_type="work_queue",
        event_name="work_queue.invalidated",
        resource_snapshot={"workspace_id": workspace.id},
    )


def enqueue_work_queue_range_invalidated(
    db: Session,
    *,
    workspace: Workspace,
    source: Episode,
    affected_episode_count: int,
) -> RealtimeEvent:
    """Coalesce one source derivation into a single workspace refresh event."""

    if source.kind != "source" or source.workspace_id != workspace.id:
        raise ValueError("work queue invalidation source scope is invalid")
    if affected_episode_count < 1:
        raise ValueError("work queue invalidation range must not be empty")
    return enqueue_resource_event(
        db,
        resource=workspace,
        resource_type="work_queue",
        event_name="work_queue.invalidated",
        resource_snapshot={
            "workspace_id": workspace.id,
            "source_episode_id": source.id,
            "affected_episode_count": affected_episode_count,
            "reason": "derived_episodes_created",
        },
    )


def publish_pending_events(
    db: Session,
    *,
    emit: Callable[[str, dict[str, Any]], object],
    now: datetime,
    limit: int = 100,
) -> int:
    """Publish committed events once and persist bounded retry state on failure."""
    if limit < 1 or limit > 1000:
        raise ValueError("realtime publish limit must be between 1 and 1000")
    effective_now = _naive_utc(now)
    events = db.scalars(
        select(RealtimeEvent)
        .where(
            RealtimeEvent.published_at.is_(None),
            or_(
                RealtimeEvent.next_attempt_at.is_(None),
                RealtimeEvent.next_attempt_at <= effective_now,
            ),
        )
        .order_by(RealtimeEvent.created_at, RealtimeEvent.event_id)
        .with_for_update(skip_locked=True)
        .limit(limit)
    ).all()

    published = 0
    for event in events:
        event.attempt_count = int(event.attempt_count or 0) + 1
        try:
            emit(event.event_name, realtime_event_payload(event))
        except Exception:
            event.last_error = "realtime publish failed"
            event.next_attempt_at = effective_now + timedelta(
                seconds=_retry_delay_seconds(event.attempt_count)
            )
        else:
            event.published_at = effective_now
            event.next_attempt_at = None
            event.last_error = ""
            published += 1
    db.commit()
    return published


async def publish_pending_events_async(
    db: Session,
    *,
    emit: Callable[[str, dict[str, Any]], Awaitable[object]],
    now: datetime,
    limit: int = 100,
) -> int:
    """Async counterpart for Socket.IO's coroutine-based emit API."""
    if limit < 1 or limit > 1000:
        raise ValueError("realtime publish limit must be between 1 and 1000")
    effective_now = _naive_utc(now)
    events = db.scalars(
        select(RealtimeEvent)
        .where(
            RealtimeEvent.published_at.is_(None),
            or_(
                RealtimeEvent.next_attempt_at.is_(None),
                RealtimeEvent.next_attempt_at <= effective_now,
            ),
        )
        .order_by(RealtimeEvent.created_at, RealtimeEvent.event_id)
        .with_for_update(skip_locked=True)
        .limit(limit)
    ).all()

    published = 0
    for event in events:
        event.attempt_count = int(event.attempt_count or 0) + 1
        try:
            await emit(event.event_name, realtime_event_payload(event))
        except Exception:
            event.last_error = "realtime publish failed"
            event.next_attempt_at = effective_now + timedelta(
                seconds=_retry_delay_seconds(event.attempt_count)
            )
        else:
            event.published_at = effective_now
            event.next_attempt_at = None
            event.last_error = ""
            published += 1
    db.commit()
    return published


def realtime_event_payload(event: RealtimeEvent) -> dict[str, Any]:
    """Return the exact browser-safe event envelope stored by the outbox."""
    _require_safe_snapshot(event.safe_payload)
    occurred_at = event.created_at or datetime.utcnow()
    return {
        "event_id": event.event_id,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "resource_version": event.resource_version,
        "occurred_at": _naive_utc(occurred_at).replace(tzinfo=timezone.utc).isoformat(),
        "resource": dict(event.safe_payload),
    }


def _require_safe_snapshot(value: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ValueError("realtime resource snapshot must be an object")
    _walk_safe_value(value)


def _walk_safe_value(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if (
                normalized in _UNSAFE_EXACT_KEYS
                or normalized.endswith(("_path", "_uri", "_url"))
                or "access_key" in normalized
                or "secret" in normalized
                or "signature" in normalized
                or "traceback" in normalized
            ):
                raise ValueError("unsafe realtime payload field")
            _walk_safe_value(child)
    elif isinstance(value, list):
        for child in value:
            _walk_safe_value(child)


def _retry_delay_seconds(attempt_count: int) -> int:
    return min(300, 2 ** min(max(attempt_count, 1), 8))


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
