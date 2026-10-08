"""Import-session state creation and cleanup eligibility."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from data.database import Batch, CollectionDevice, ImportSession, TaskLabel, User
from data.services.collector_profiles import available_collector_profile

VALID_IMPORT_TYPES = frozenset({"chunked_upload", "oss_scan", "filesystem_scan", "duance_episode"})
TERMINAL_IMPORT_STATUSES = frozenset({"succeeded", "failed", "cancelled", "superseded"})


def create_import_session(
    db: Session,
    *,
    batch_id: int,
    import_type: str,
    actor_id: int | None,
    task_label_id: int | None = None,
    default_collector_profile_id: int | None = None,
    default_collection_device_id: int | None = None,
    original_name: str = "",
    source_fingerprint: str = "",
    retention_until: datetime | None = None,
) -> ImportSession:
    # Lock the Batch before checking existing sessions. This prevents two
    # concurrent requests from assigning different labels to one Batch.
    batch = db.scalar(select(Batch).where(Batch.id == batch_id).with_for_update())
    if batch is None:
        raise ValueError("batch does not exist")
    if actor_id is not None and db.get(User, actor_id) is None:
        raise ValueError("import actor does not exist")
    if import_type not in VALID_IMPORT_TYPES:
        raise ValueError("unsupported import_type")
    if not isinstance(task_label_id, int) or isinstance(task_label_id, bool) or task_label_id <= 0:
        raise ValueError("task label is required")
    task_label = db.get(TaskLabel, task_label_id)
    if task_label is None:
        raise ValueError("task label does not exist")
    if len(original_name) > 256 or len(source_fingerprint) > 64:
        raise ValueError("import metadata is too long")
    if default_collector_profile_id is not None:
        collector = available_collector_profile(
            db, workspace_id=batch.workspace_id, profile_id=default_collector_profile_id
        )
        if collector is None:
            raise ValueError("collector profile is unavailable")
    if default_collection_device_id is not None:
        device = db.get(CollectionDevice, default_collection_device_id)
        if device is None or device.workspace_id != batch.workspace_id or not device.is_active:
            raise ValueError("collection device is unavailable")

    existing_labels = set(
        db.scalars(
            select(ImportSession.task_label_id).where(ImportSession.batch_id == batch.id)
        ).all()
    )
    if existing_labels and (None in existing_labels or existing_labels != {task_label.id}):
        raise ValueError("batch cannot mix task labels")

    import_session = ImportSession(
        id=str(uuid4()),
        batch_id=batch_id,
        import_type=import_type,
        owner_user_id=actor_id,
        task_label_id=task_label.id,
        default_collector_profile_id=default_collector_profile_id,
        default_collection_device_id=default_collection_device_id,
        original_name=original_name,
        source_fingerprint=source_fingerprint,
        retention_until=retention_until,
    )
    db.add(import_session)
    db.flush()
    return import_session


def import_session_can_cleanup_originals(import_session: ImportSession, *, now: datetime) -> bool:
    """Only an expired terminal session permits cleanup of its exact import prefix."""
    return bool(
        import_session.status in TERMINAL_IMPORT_STATUSES
        and import_session.retention_until is not None
        and import_session.retention_until <= now
    )
