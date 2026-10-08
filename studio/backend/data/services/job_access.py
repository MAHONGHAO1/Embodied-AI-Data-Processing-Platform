"""Default-deny authorization for the resource referenced by a durable job."""

from __future__ import annotations

from sqlalchemy.orm import Session

from data.database import (
    Batch,
    DatasetRevision,
    Episode,
    EpisodeArtifact,
    ImportSession,
    JobRun,
    NativeLerobotBundle,
    NativeLerobotDataset,
    NativeLerobotScanSnapshot,
    TaskSet,
)
from data.models.native_lerobot_direct import NativeLerobotDirectSource
from data.services.workspace_access import (
    require_actor,
    require_artifact_actor,
    require_batch_actor,
    require_episode_actor,
    require_import_session_actor,
    require_workspace_actor,
)
from data.utils.helpers import get_role_permissions

_READ_PERMISSION_BY_RESOURCE = {
    "dataset_revision": "dataset:read",
    "batch": "batch:read",
    "episode": "episode:read",
    "import_session": "import:read",
    "native_lerobot_dataset": "batch:read",
    "native_lerobot_bundle": "batch:read",
    "native_lerobot_scan_snapshot": "import:read",
    "native_lerobot_direct_source": "dataset:read",
    "artifact": "episode:read",
    "platform": "dashboard:read",
}


def job_resource_read_permission(resource_type: str) -> str | None:
    return _READ_PERMISSION_BY_RESOURCE.get(resource_type)


def actor_can_access_job_resource(
    db: Session,
    *,
    actor_id: int | None,
    resource_type: str,
    resource_id: str,
) -> bool:
    try:
        actor = require_actor(db, actor_id=actor_id)
        if not _role_has_permission(actor.role, job_resource_read_permission(resource_type)):
            return False
        if resource_type == "dataset_revision":
            revision = db.get(DatasetRevision, int(resource_id))
            if revision is None:
                return False
            require_workspace_actor(db, actor_id=actor.id, workspace_id=revision.workspace_id)
            return True
        if resource_type == "batch":
            batch = db.get(Batch, int(resource_id))
            if batch is None:
                return False
            require_batch_actor(db, actor_id=actor.id, batch=batch)
            return True
        if resource_type == "episode":
            episode = db.get(Episode, int(resource_id))
            if episode is None:
                return False
            require_episode_actor(db, actor_id=actor.id, episode=episode)
            return True
        if resource_type == "import_session":
            import_session = db.get(ImportSession, resource_id)
            if import_session is None:
                return False
            require_import_session_actor(db, actor_id=actor.id, import_session=import_session)
            return True
        if resource_type == "native_lerobot_scan_snapshot":
            snapshot = db.get(NativeLerobotScanSnapshot, resource_id)
            if snapshot is None:
                return False
            task_set = db.get(TaskSet, snapshot.task_set_id)
            if task_set is None or task_set.workspace_id != snapshot.workspace_id:
                return False
            require_workspace_actor(db, actor_id=actor.id, workspace_id=snapshot.workspace_id)
            return True
        if resource_type == "native_lerobot_dataset":
            dataset = _native_lerobot_dataset(db, resource_id=resource_id)
            require_workspace_actor(db, actor_id=actor.id, workspace_id=dataset.workspace_id)
            return True
        if resource_type == "native_lerobot_bundle":
            dataset = _native_lerobot_bundle_dataset(db, resource_id=resource_id)
            require_workspace_actor(db, actor_id=actor.id, workspace_id=dataset.workspace_id)
            return True
        if resource_type == "native_lerobot_direct_source":
            source = db.get(NativeLerobotDirectSource, resource_id)
            return source is not None and actor.role == "admin"
        if resource_type == "artifact":
            artifact = db.get(EpisodeArtifact, int(resource_id))
            if artifact is None:
                return False
            require_artifact_actor(db, actor_id=actor.id, artifact=artifact)
            return True
        if resource_type == "platform":
            return _actor_can_access_dashboard_scope(
                db,
                actor_id=actor.id,
                actor_role=actor.role,
                scope_key=resource_id,
            )
    except (PermissionError, TypeError, ValueError):
        return False
    return False


def _role_has_permission(role: str, permission: str | None) -> bool:
    if not permission:
        return False
    permissions = set(get_role_permissions(role))
    prefix = permission.split(":", maxsplit=1)[0]
    return "*" in permissions or permission in permissions or f"{prefix}:*" in permissions


def actor_can_access_job(db: Session, *, actor_id: int | None, job: JobRun) -> bool:
    return job_has_consistent_resource_scope(db, job=job) and actor_can_access_job_resource(
        db,
        actor_id=actor_id,
        resource_type=job.resource_type,
        resource_id=job.resource_id,
    )


def job_has_consistent_resource_scope(db: Session, *, job: JobRun) -> bool:
    """Reject malformed dashboard jobs before they can cross tenant rooms."""
    if job.resource_type == "native_lerobot_dataset":
        try:
            dataset = _native_lerobot_dataset(db, resource_id=job.resource_id)
        except (TypeError, ValueError):
            return False
        return _native_lerobot_job_scope_is_consistent(db, job=job, dataset=dataset)
    if job.resource_type == "native_lerobot_bundle":
        try:
            dataset = _native_lerobot_bundle_dataset(db, resource_id=job.resource_id)
        except (TypeError, ValueError):
            return False
        return _native_lerobot_job_scope_is_consistent(db, job=job, dataset=dataset)
    if job.resource_type == "native_lerobot_scan_snapshot":
        snapshot = db.get(NativeLerobotScanSnapshot, job.resource_id)
        if snapshot is None:
            return False
        task_set = db.get(TaskSet, snapshot.task_set_id)
        return bool(
            task_set is not None
            and task_set.workspace_id == snapshot.workspace_id
            and job.workspace_id == snapshot.workspace_id
            and job.task_set_id == snapshot.task_set_id
        )
    if job.resource_type == "native_lerobot_direct_source":
        source = db.get(NativeLerobotDirectSource, job.resource_id)
        detail = job.detail_json if isinstance(job.detail_json, dict) else {}
        return bool(
            source is not None
            and job.kind == "native_lerobot_direct_validate"
            and job.workspace_id is None
            and job.task_set_id is None
            and detail.get("native_lerobot_direct_source_id") == source.id
        )
    if job.resource_type != "platform":
        return True
    try:
        detail = job.detail_json if isinstance(job.detail_json, dict) else {}
        if detail.get("scope_key") != job.resource_id:
            return False
        if job.resource_id == "global":
            return (
                job.workspace_id is None
                and job.task_set_id is None
                and detail.get("workspace_id") is None
                and detail.get("task_set_id") is None
            )
        if job.resource_id.startswith("workspace:"):
            workspace_id = int(job.resource_id.removeprefix("workspace:"))
            return (
                job.workspace_id == workspace_id
                and job.task_set_id is None
                and detail.get("workspace_id") == workspace_id
                and detail.get("task_set_id") is None
            )
        if job.resource_id.startswith("task-set:"):
            task_set_id = int(job.resource_id.removeprefix("task-set:"))
            task_set = db.get(TaskSet, task_set_id)
            return bool(
                task_set is not None
                and job.task_set_id == task_set_id
                and job.workspace_id == task_set.workspace_id
                and detail.get("task_set_id") == task_set_id
                and detail.get("workspace_id") == task_set.workspace_id
            )
    except (TypeError, ValueError):
        return False
    return False


def _native_lerobot_dataset(db: Session, *, resource_id: str) -> NativeLerobotDataset:
    dataset_id = int(resource_id)
    if dataset_id <= 0:
        raise ValueError("native dataset is unavailable")
    dataset = db.get(NativeLerobotDataset, dataset_id)
    if dataset is None:
        raise ValueError("native dataset is unavailable")
    return dataset


def _native_lerobot_bundle_dataset(db: Session, *, resource_id: str) -> NativeLerobotDataset:
    bundle_id = int(resource_id)
    if bundle_id <= 0:
        raise ValueError("native bundle is unavailable")
    bundle = db.get(NativeLerobotBundle, bundle_id)
    if bundle is None:
        raise ValueError("native bundle is unavailable")
    return _native_lerobot_dataset(db, resource_id=str(bundle.native_lerobot_dataset_id))


def _native_lerobot_job_scope_is_consistent(
    db: Session,
    *,
    job: JobRun,
    dataset: NativeLerobotDataset,
) -> bool:
    task_set = db.get(TaskSet, dataset.task_set_id)
    return bool(
        task_set is not None
        and task_set.workspace_id == dataset.workspace_id
        and job.workspace_id == dataset.workspace_id
        and job.task_set_id == dataset.task_set_id
    )


def _actor_can_access_dashboard_scope(
    db: Session,
    *,
    actor_id: int,
    actor_role: str,
    scope_key: str,
) -> bool:
    if scope_key == "global":
        return actor_role == "admin"
    if scope_key.startswith("workspace:"):
        workspace_id = int(scope_key.removeprefix("workspace:"))
        require_workspace_actor(db, actor_id=actor_id, workspace_id=workspace_id)
        return True
    if scope_key.startswith("task-set:"):
        task_set_id = int(scope_key.removeprefix("task-set:"))
        task_set = db.get(TaskSet, task_set_id)
        if task_set is None:
            return False
        require_workspace_actor(db, actor_id=actor_id, workspace_id=task_set.workspace_id)
        return True
    return False
