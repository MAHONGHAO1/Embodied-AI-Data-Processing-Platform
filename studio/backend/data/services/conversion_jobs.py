"""Durable MCAP/QRDF conversion jobs executed by Celery workers."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from data.config import settings
from data.database import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RETRY_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JobRun,
    QrdfData,
    SessionLocal,
    Task,
)
from data.services.conversion import convert_mcap_to_qrdf, convert_qrdf_to_lerobot
from data.services.job_runs import create_or_get_job
from data.utils.formatting import format_api_datetime
from data.utils.storage_uri import is_cloud_uri

_ACTIVE_STATUSES = (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING, JOB_STATUS_RETRY_PENDING)
_MCAP_OPTION_KEYS = frozenset(
    {
        "episode_id",
        "task_name",
        "dataset_name",
        "image_workers",
        "generate_preview",
        "reader_backend",
    }
)
_LEROBOT_OPTION_KEYS = frozenset({"fps", "lerobot_version", "image_size", "export_template"})


def _status_for_public(job: JobRun) -> str:
    if job.status == JOB_STATUS_SUCCEEDED:
        result = job.result_json if isinstance(job.result_json, dict) else {}
        return "blocked" if result.get("blocked") is True else "completed"
    if job.status in {JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}:
        return "failed" if job.status == JOB_STATUS_FAILED else "blocked"
    return "running" if job.status == JOB_STATUS_RUNNING else "pending"


def _progress_for_public(job: JobRun) -> int:
    return max(0, min(100, int(job.progress_percent or 0)))


def _conversion_payload(job: JobRun) -> dict[str, Any]:
    """Return compatibility status without paths, worker exceptions, or details."""
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    payload: dict[str, Any] = {
        "job_id": job.id,
        "conversion_type": job.kind,
        "status": _status_for_public(job),
        "progress": _progress_for_public(job),
        "async": True,
    }
    if job.resource_type == "task":
        try:
            payload["task_id"] = int(job.resource_id)
        except (TypeError, ValueError):
            pass
    scope = detail.get("authorization_scope")
    if isinstance(scope, dict):
        payload["authorization_scope"] = dict(scope)
    if job.started_at:
        payload["started_at"] = format_api_datetime(job.started_at)
    if job.finished_at:
        payload["finished_at"] = format_api_datetime(job.finished_at)
    if job.status == JOB_STATUS_FAILED:
        payload["error"] = "conversion failed"
    return payload


def get_conversion_job(job_id: str) -> dict[str, Any] | None:
    """Load conversion status from the database, never process-local memory."""
    db = SessionLocal()
    try:
        job = db.get(JobRun, job_id)
        if job is None or job.kind not in {"mcap_to_qrdf", "qrdf_to_lerobot"}:
            return None
        return _conversion_payload(job)
    finally:
        db.close()


def _set_job(job_id: str, payload: dict[str, Any]) -> None:
    """Compatibility helper for tests and legacy callers; state is still durable."""
    conversion_type = str(payload.get("conversion_type") or "qrdf_to_lerobot")
    if conversion_type not in {"mcap_to_qrdf", "qrdf_to_lerobot"}:
        raise ValueError("unsupported conversion type")
    scope = payload.get("authorization_scope")
    actor_id = scope.get("creator_actor_id") if isinstance(scope, dict) else None
    task_id = payload.get("task_id")
    db = SessionLocal()
    try:
        if task_id is None and isinstance(scope, dict) and scope.get("qrdf_ids"):
            qrdf = db.get(QrdfData, int(scope["qrdf_ids"][0]))
            task_id = qrdf.task_id if qrdf is not None else None
        resource_type = "task" if task_id is not None else "conversion_scope"
        resource_id = str(task_id) if task_id is not None else job_id
        job = db.get(JobRun, job_id)
        if job is None:
            job = JobRun(
                id=job_id,
                kind=conversion_type,
                resource_type=resource_type,
                resource_id=resource_id,
                idempotency_key=f"legacy-conversion:{job_id}",
                queue="media" if conversion_type == "mcap_to_qrdf" else "export",
                actor_id=actor_id if isinstance(actor_id, int) else None,
                status=JOB_STATUS_QUEUED,
                phase=JOB_STATUS_QUEUED,
                detail_json={},
            )
            db.add(job)
        detail = dict(job.detail_json or {})
        if isinstance(scope, dict):
            detail["authorization_scope"] = dict(scope)
        job.detail_json = detail
        status = str(payload.get("status") or "pending")
        job.status = {
            "running": JOB_STATUS_RUNNING,
            "completed": JOB_STATUS_SUCCEEDED,
            "blocked": JOB_STATUS_SUCCEEDED,
            "failed": JOB_STATUS_FAILED,
        }.get(status, JOB_STATUS_QUEUED)
        job.phase = status
        job.progress_percent = int(
            payload.get("progress") or (100 if status in {"completed", "blocked", "failed"} else 0)
        )
        job.result_json = dict(payload.get("result") or {})
        if status == "blocked":
            job.result_json = {**job.result_json, "blocked": True}
        if status == "failed":
            job.error_code = "media_execution_failed"
            job.error_message = "conversion failed"
        if status in {"completed", "blocked", "failed"}:
            job.finished_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def _conversion_already_succeeded(meta: dict[str, Any]) -> bool:
    prev = meta.get("mcap_to_qrdf") or {}
    if not isinstance(prev, dict) or prev.get("status") == "failed":
        return False
    if prev.get("qrdf_path") or prev.get("episode_path"):
        return True
    return prev.get("conversion_type") == "mcap_to_qrdf" and bool(prev.get("storage_path"))


def _mark_mcap_conversion_failed(task_id: int, message: str, operator: str = "system") -> None:
    from data.services.state_machine import (
        WorkItemManagedTaskError,
        assert_legacy_task_transition_allowed,
        transit_task,
    )

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            return
        meta = dict(task.metadata_json or {})
        if _conversion_already_succeeded(meta):
            return
        public_message = "MCAP conversion failed"
        meta["mcap_to_qrdf"] = {
            "status": "failed",
            "error": public_message,
            "finished_at": format_api_datetime(datetime.utcnow()),
        }
        task.metadata_json = meta

        try:
            assert_legacy_task_transition_allowed(db, task)
        except WorkItemManagedTaskError:
            db.commit()
            return

        try:
            from data.database import TaskStatus

            current = TaskStatus(task.status)
            if current in (TaskStatus.PENDING_COLLECT, TaskStatus.COLLECT_DONE):
                transit_task(db, task, "fail", operator, public_message)
            elif current == TaskStatus.PENDING_PREPROCESS:
                meta["preprocess"] = {
                    "status": "failed",
                    "message": public_message,
                    "finished_at": format_api_datetime(datetime.utcnow()),
                }
                task.metadata_json = meta
                transit_task(db, task, "fail", operator, public_message)
            elif task.status != "failed":
                task.status = "failed"
                task.updated_at = datetime.utcnow()
                db.commit()
        except WorkItemManagedTaskError:
            db.commit()
        except ValueError:
            task.status = "failed"
            task.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def _conversion_output_staging_paths(result: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("qrdf_path", "storage_path"):
        value = result.get(key)
        if value and str(value) not in paths:
            paths.append(str(value))
    return paths


def _apply_mcap_conversion_to_task(
    task_id: int,
    result: dict[str, Any],
    *,
    auto_preprocess: bool,
    operator: str,
) -> bool:
    from data.services.cloud_storage import (
        cleanup_local_staging,
        new_process_run_id,
        publish_preprocess_output,
    )
    from data.services.preprocess import start_preprocess
    from data.services.state_machine import ensure_ready_for_preprocess
    from data.utils.storage_paths import resolve_storage_path

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            cleanup_local_staging(*_conversion_output_staging_paths(result))
            return False
        if auto_preprocess and task.status in {"failed", "rejected"}:
            from data.security.audit import emit_audit_event

            emit_audit_event(
                "job.retry.denied",
                actor=operator,
                resource=f"task:{task.id}",
                detail={"kind": "preprocess", "reason": "resource_fencing_required"},
                level="warning",
            )
            cleanup_local_staging(*_conversion_output_staging_paths(result))
            return False
        local_path = None
        staging_paths = _conversion_output_staging_paths(result)
        if result.get("qrdf_path"):
            local_path = resolve_storage_path(result["qrdf_path"]) or Path(result["qrdf_path"])
        elif result.get("storage_path"):
            local_path = resolve_storage_path(result["storage_path"])
        if local_path and local_path.exists():
            run_id = new_process_run_id(task_id)
            cloud_uri = publish_preprocess_output(
                local_path,
                task_id=task_id,
                workspace_id=task.workspace_id,
                project_id=task.project_id,
                run_id=run_id,
            )
            result = {**result, "storage_path": cloud_uri, "cloud_uri": cloud_uri, "run_id": run_id}
            cleanup_local_staging(*staging_paths, local_path)
        task.storage_path = result.get("storage_path")
        if not task.storage_path and result.get("qrdf_path"):
            try:
                task.storage_path = (
                    Path(result["qrdf_path"])
                    .resolve()
                    .relative_to(Path(settings.storage_root).resolve())
                    .as_posix()
                )
            except ValueError:
                cleanup_local_staging(*staging_paths)
                return False
        meta = dict(task.metadata_json or {})
        meta["mcap_to_qrdf"] = result
        task.metadata_json = meta
        db.commit()
        if auto_preprocess:
            from data.services.task_dispatcher import JobDispatchUnavailable, require_celery_worker

            try:
                require_celery_worker("media")
            except JobDispatchUnavailable as exc:
                meta = dict(task.metadata_json or {})
                meta["preprocess_dispatch"] = {
                    "status": "waiting_for_worker",
                    "message": str(exc),
                }
                task.metadata_json = meta
                db.commit()
                return True
            task = ensure_ready_for_preprocess(db, task, operator)
            start_preprocess(task.id, operator)
        return True
    finally:
        db.close()


def _safe_relative_output(value: Any) -> str | None:
    if value in (None, ""):
        return None
    from data.security.task_args import resolve_task_local_path

    resolved = resolve_task_local_path(str(value))
    if resolved is None:
        return None
    return resolved.relative_to(Path(settings.storage_root).resolve()).as_posix()


def _safe_options(kwargs: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    options = {key: kwargs[key] for key in allowed if key in kwargs and kwargs[key] is not None}
    if isinstance(options.get("image_size"), tuple):
        options["image_size"] = list(options["image_size"])
    return options


def _job_output_dir(job: JobRun, leaf: str, requested: Any = None) -> tuple[str, Path]:
    """Resolve an attempt output only inside this job's private staging prefix."""
    relative = _safe_relative_output(requested or f"hot/conversion_jobs/{job.id}/{leaf}")
    if relative is None:
        raise ValueError("conversion output directory is unavailable")
    root = Path(settings.storage_root).resolve()
    job_root = (root / "hot" / "conversion_jobs" / job.id).resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(job_root)
    except ValueError as exc:
        raise ValueError("conversion output must stay inside its job staging directory") from exc
    return relative, resolved


def _task_source_is_available(task: Task) -> None:
    from data.security.task_args import resolve_task_local_path

    if not task.storage_path:
        raise ValueError("task has no uploaded MCAP source")
    if is_cloud_uri(str(task.storage_path)):
        return
    source = resolve_task_local_path(task.storage_path)
    if source is None or not source.is_file():
        raise FileNotFoundError("task MCAP source is unavailable")


def _stable_key(prefix: str, values: dict[str, Any]) -> str:
    encoded = json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _active_job(db, *, kind: str, resource_type: str, resource_id: int) -> JobRun | None:
    return db.scalar(
        select(JobRun)
        .where(
            JobRun.kind == kind,
            JobRun.resource_type == resource_type,
            JobRun.resource_id == str(resource_id),
            JobRun.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(JobRun.created_at.desc(), JobRun.id.desc())
    )


def _resolve_mcap_job_source(task_id: int):
    from data.services.cloud_storage import materialize_for_processing

    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            raise ValueError("task does not exist")
        _task_source_is_available(task)
        storage_path = str(task.storage_path)
    finally:
        db.close()
    return materialize_for_processing(storage_path)


def execute_mcap_conversion_job(job: JobRun) -> dict[str, Any]:
    """Worker handler: resolve source by Task ID and perform conversion."""
    from data.services.workspace_access import require_task_actor

    task_id = int(job.resource_id)
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            raise ValueError("task does not exist")
        require_task_actor(db, actor_id=job.actor_id, task=task)
    finally:
        db.close()
    detail = dict(job.detail_json or {})
    options = dict(detail.get("options") or {})
    options["output_dir"], output = _job_output_dir(job, "qrdf", options.get("output_dir"))
    shutil.rmtree(output, ignore_errors=True)
    operator = str(detail.get("operator") or "system")[:128]
    try:
        with _resolve_mcap_job_source(task_id) as source:
            if source is None or not source.is_file():
                raise FileNotFoundError("task MCAP source is unavailable")
            result = convert_mcap_to_qrdf(mcap_path=str(source), **options)
        applied = _apply_mcap_conversion_to_task(
            task_id,
            result,
            auto_preprocess=bool(detail.get("auto_preprocess")),
            operator=operator,
        )
        return {"task_id": task_id, "blocked": not applied}
    except Exception as exc:
        _mark_mcap_conversion_failed(task_id, str(exc), operator)
        raise


def _validated_qrdf_scope(scope: dict[str, Any]) -> tuple[int, int]:
    from data.services.export_authorization import build_export_authorization_scope

    actor_id = scope.get("creator_actor_id")
    qrdf_ids = scope.get("qrdf_ids")
    if not isinstance(actor_id, int) or not isinstance(qrdf_ids, list) or len(qrdf_ids) != 1:
        raise ValueError("conversion authorization scope is unavailable")
    qrdf_id = int(qrdf_ids[0])
    db = SessionLocal()
    try:
        resolved = build_export_authorization_scope(
            db,
            qrdf_ids=[qrdf_id],
            dataset_id=scope.get("dataset_id"),
            actor_id=actor_id,
        )
        for key in ("workspace_ids", "project_ids", "qrdf_ids", "dataset_id"):
            if resolved.get(key) != scope.get(key):
                raise ValueError("conversion authorization scope is unavailable")
        qrdf = db.get(QrdfData, qrdf_id)
        if qrdf is None:
            raise ValueError("QRDF source does not exist")
        return qrdf_id, int(qrdf.task_id)
    finally:
        db.close()


def execute_qrdf_conversion_job(job: JobRun) -> dict[str, Any]:
    """Worker handler: revalidate scope, resolve QRDF by ID, then convert."""
    from data.services.conversion import resolve_qrdf_storage_path

    detail = dict(job.detail_json or {})
    scope = detail.get("authorization_scope")
    if not isinstance(scope, dict):
        raise ValueError("conversion authorization scope is unavailable")
    if job.actor_id != scope.get("creator_actor_id"):
        raise ValueError("conversion actor scope is unavailable")
    qrdf_id, task_id = _validated_qrdf_scope(scope)
    if str(task_id) != str(job.resource_id):
        raise ValueError("conversion task scope is unavailable")
    db = SessionLocal()
    try:
        storage_path = resolve_qrdf_storage_path(qrdf_id=qrdf_id, db=db)
    finally:
        db.close()
    options = dict(detail.get("options") or {})
    if isinstance(options.get("image_size"), list):
        options["image_size"] = tuple(options["image_size"])
    options["output_dir"], _output = _job_output_dir(job, "lerobot", options.get("output_dir"))
    result = convert_qrdf_to_lerobot(storage_path=storage_path, **options)
    return {
        "qrdf_id": qrdf_id,
        "task_id": task_id,
        "output_ready": bool(result.get("storage_path") or result.get("lerobot_path")),
    }


def dispatch_mcap_to_qrdf(
    async_mode: bool,
    kwargs: dict[str, Any],
    *,
    task_id: int | None = None,
    auto_preprocess: bool = False,
    operator: str = "system",
    actor_id: int | None = None,
) -> dict[str, Any]:
    del async_mode  # Compatibility input only; public conversion is always queued.
    if task_id is None:
        raise ValueError("task_id is required for MCAP conversion")
    if kwargs.get("output_dir"):
        raise ValueError("custom output_dir is not supported")
    from data.services.workspace_access import require_task_actor

    options = _safe_options(kwargs, _MCAP_OPTION_KEYS)
    db = SessionLocal()
    try:
        task = db.get(Task, int(task_id))
        if task is None:
            raise ValueError("task does not exist")
        require_task_actor(db, actor_id=actor_id, task=task)
        _task_source_is_available(task)
        from data.services.task_dispatcher import dispatch_media_job, require_celery_worker

        require_celery_worker("media")
        active = _active_job(db, kind="mcap_to_qrdf", resource_type="task", resource_id=task.id)
        if active is not None:
            if active.status in {JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING}:
                dispatch_media_job(active, worker_prechecked=True)
            payload = _conversion_payload(active)
            payload["deduped"] = True
            return payload
        revision = task.updated_at.isoformat() if task.updated_at else "initial"
        base_key = _stable_key(
            f"mcap-to-qrdf:{task.id}:{revision}",
            {"options": options, "auto_preprocess": bool(auto_preprocess)},
        )
        terminal = db.scalar(select(JobRun).where(JobRun.idempotency_key == base_key))
        idempotency_key = base_key if terminal is None else f"{base_key}:rerun:{uuid4().hex}"
        job = create_or_get_job(
            db,
            kind="mcap_to_qrdf",
            resource_type="task",
            resource_id=task.id,
            idempotency_key=idempotency_key,
            queue="media",
            actor_id=actor_id,
            detail={
                "options": options,
                "auto_preprocess": bool(auto_preprocess),
                "operator": str(operator or "system")[:128],
            },
        )
        dispatch_media_job(job, worker_prechecked=True)
        return {"job_id": job.id, "status": "pending", "async": True}
    finally:
        db.close()


def dispatch_qrdf_to_lerobot(async_mode: bool, kwargs: dict[str, Any]) -> dict[str, Any]:
    del async_mode  # Compatibility input only; public conversion is always queued.
    sync_kwargs = dict(kwargs)
    scope = sync_kwargs.pop("_authorization_scope", None)
    if sync_kwargs.get("output_dir"):
        raise ValueError("custom output_dir is not supported")
    if not isinstance(scope, dict):
        raise ValueError("conversion authorization scope is required")
    qrdf_id, task_id = _validated_qrdf_scope(scope)
    del qrdf_id
    actor_id = int(scope["creator_actor_id"])
    options = _safe_options(sync_kwargs, _LEROBOT_OPTION_KEYS)

    from data.services.task_dispatcher import dispatch_media_job, require_celery_worker

    require_celery_worker("export")
    db = SessionLocal()
    try:
        base_key = _stable_key(
            f"qrdf-to-lerobot:{task_id}",
            {"options": options, "authorization_scope": scope},
        )
        existing = db.scalar(select(JobRun).where(JobRun.idempotency_key == base_key))
        if existing is not None and existing.status in _ACTIVE_STATUSES:
            if existing.status in {JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING}:
                dispatch_media_job(existing, worker_prechecked=True)
            payload = _conversion_payload(existing)
            payload["deduped"] = True
            return payload
        idempotency_key = base_key if existing is None else f"{base_key}:rerun:{uuid4().hex}"
        job = create_or_get_job(
            db,
            kind="qrdf_to_lerobot",
            resource_type="task",
            resource_id=task_id,
            idempotency_key=idempotency_key,
            queue="export",
            actor_id=actor_id,
            detail={"options": options, "authorization_scope": dict(scope)},
        )
        dispatch_media_job(job, worker_prechecked=True)
        return {"job_id": job.id, "status": "pending", "async": True}
    finally:
        db.close()
