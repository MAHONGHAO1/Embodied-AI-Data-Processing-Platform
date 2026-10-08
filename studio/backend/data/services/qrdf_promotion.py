"""Durable promotion of an audited legacy task into official QRDF storage."""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from data.database import JobRun, QrdfData, Task, TaskStatus, User
from data.services.annotate_qrdf_writer import write_annotation_to_qrdf
from data.services.annotate_service import (
    load_annotation_doc,
    resolve_task_dataset_storage,
)
from data.services.behavior_tags_service import get_active_vocabulary_snapshot
from data.services.cloud_storage import (
    cleanup_cloud_mirror,
    cleanup_local_staging,
    cleanup_task_terminal_storage,
    cleanup_zip_extract_staging,
    collect_process_stage_uris,
    make_dataset_id,
    materialize_for_processing,
    promote_to_official_dataset,
    publish_annotation_patch,
)
from data.services.state_machine import transit_task

QRDF_PROMOTION_JOB_KIND = "qrdf_promote"
logger = logging.getLogger(__name__)


class QrdfPromotionRetired(RuntimeError):
    """Stable domain error for the removed official-promotion workflow."""


def enqueue_qrdf_promotion_job(
    db: Session,
    *,
    task: Task,
    qrdf: QrdfData,
    actor_id: int | None,
) -> JobRun:
    """Stage one promotion job in the caller's audit transaction."""
    raise QrdfPromotionRetired("qrdf_promote is retired; use process/export artifacts")
    idempotency_key = f"qrdf-promote:{task.id}:{qrdf.id}"
    existing = db.query(JobRun).filter(JobRun.idempotency_key == idempotency_key).one_or_none()
    if existing is not None:
        return existing
    now = datetime.utcnow()
    job = JobRun(
        id=uuid4().hex,
        kind=QRDF_PROMOTION_JOB_KIND,
        resource_type="task",
        resource_id=str(task.id),
        idempotency_key=idempotency_key,
        queue="media",
        actor_id=actor_id,
        status="queued",
        phase="queued",
        detail_json={"qrdf_id": qrdf.id},
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()
    return job


def materialize_and_promote_qrdf(db: Session, job: JobRun) -> dict[str, int | str]:
    """Execute one fenced promotion; storage-ready is committed only after I/O."""
    raise QrdfPromotionRetired("qrdf_promote is retired; use process/export artifacts")
    if job.kind != QRDF_PROMOTION_JOB_KIND or job.resource_type != "task":
        raise ValueError("invalid QRDF promotion job contract")
    qrdf_id = int((job.detail_json or {}).get("qrdf_id") or 0)
    task = db.get(Task, int(job.resource_id))
    qrdf = db.get(QrdfData, qrdf_id)
    if task is None or qrdf is None or qrdf.task_id != task.id:
        raise ValueError("QRDF promotion resource is unavailable")
    if task.status == TaskStatus.STORAGE_READY.value:
        return {"task_id": task.id, "qrdf_id": qrdf.id, "status": task.status}
    if task.status != TaskStatus.AUDIT_PASSED.value:
        raise ValueError("task is not ready for QRDF promotion")

    dataset_storage = resolve_task_dataset_storage(task) or task.storage_path
    if not dataset_storage:
        raise ValueError("task dataset storage is unavailable")
    process_uris = collect_process_stage_uris(task)
    actor = db.get(User, job.actor_id) if job.actor_id is not None else None
    operator = actor.email if actor is not None else "system"
    annotation_doc = load_annotation_doc(db, task.id, task=task)
    vocabulary = get_active_vocabulary_snapshot(db)

    with materialize_for_processing(dataset_storage) as local_dataset:
        annotation_storage = str(local_dataset) if local_dataset else dataset_storage
        write_annotation_to_qrdf(
            storage_path=annotation_storage,
            task_id=task.id,
            annotation_doc=annotation_doc,
            vocabulary=vocabulary,
            updated_by=operator,
        )
        if local_dataset:
            publish_annotation_patch(
                local_dataset,
                source_cloud_uri=dataset_storage,
                task_id=task.id,
            )
            cleanup_local_staging(local_dataset)

    official_uri = promote_to_official_dataset(
        dataset_storage,
        dataset_id=make_dataset_id(task.project_id, qrdf.id),
        task_id=task.id,
        workspace_id=task.workspace_id,
        project_id=task.project_id,
        metadata={
            "episodes": (qrdf.metadata_json or {}).get("episodes") or [],
            "quality_check": (qrdf.metadata_json or {}).get("quality_check") or {},
            "scene": qrdf.scene,
        },
    )
    if not official_uri:
        raise RuntimeError("QRDF promotion did not return an official resource")

    qrdf.storage_path = official_uri
    task.storage_path = official_uri
    transit_task(db, task, "storage_ready", operator, commit=False)
    db.commit()

    try:
        if process_uris:
            cleanup_cloud_mirror(*process_uris)
            cleanup_zip_extract_staging(*process_uris)
        cleanup_task_terminal_storage(task)
    except Exception:
        logger.exception("QRDF promotion cleanup failed task_id=%s", task.id)

    return {"task_id": task.id, "qrdf_id": qrdf.id, "status": task.status}
