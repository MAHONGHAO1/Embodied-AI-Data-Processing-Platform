"""Data preprocessing API (Technical Design 4.4 / Pipeline 2)."""

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from data.database import Task, get_db
from data.infra.redis_client import redis_service
from data.schemas.common import McapToQrdfRequest, PreprocessBatchRequest
from data.security.audit import emit_audit_event
from data.services.conversion_jobs import dispatch_mcap_to_qrdf, get_conversion_job
from data.services.job_runs import (
    UNFENCED_RETRY_PUBLIC_MESSAGE,
    ResourceFencingRequired,
    require_recoverable_job_kind,
)
from data.services.preprocess import start_preprocess, terminate_preprocess
from data.services.state_machine import ensure_ready_for_preprocess
from data.services.task_dispatcher import (
    JobDispatchUnavailable,
    dispatch_preprocess_batch,
    require_celery_worker,
)
from data.services.workspace_access import require_task_actor, task_access_filter
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success
from data.utils.storage_paths import resolve_storage_path
from data.utils.storage_uri import to_display_storage_uri

router = APIRouter(prefix="/preprocess", tags=["数据预处理"])

_HIDDEN_DETAIL = "详情已隐藏"
_MAX_PUBLIC_MESSAGE_LENGTH = 500
_MAX_PUBLIC_ITEMS = 100
_PUBLIC_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'(:=\[])(?:/(?:[^/\s]+/)+[^/\s]*|~[/\\]|[A-Za-z]:[/\\])")
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_SECRET_MARKER = re.compile(r"(?i)(?:access[_-]?key|api[_-]?key|password|secret|token)\s*[:=]")


def _actor_id(user: dict) -> int | None:
    try:
        raw = user.get("sub") or user.get("id")
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _authorize_task(db: Session, user: dict, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task does not exist")
    try:
        require_task_actor(db, actor_id=_actor_id(user), task=task)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="task does not exist") from exc
    return task


def _deny_unfenced_preprocess_retry(user: dict, task_id: int) -> None:
    try:
        require_recoverable_job_kind("preprocess")
    except ResourceFencingRequired:
        emit_audit_event(
            "job.retry.denied",
            actor=str(user.get("email") or ""),
            resource=f"task:{task_id}",
            detail={"kind": "preprocess", "reason": "resource_fencing_required"},
            level="warning",
        )
        raise HTTPException(status_code=409, detail=UNFENCED_RETRY_PUBLIC_MESSAGE) from None


def _storage_display_path(storage_path: str | None, *, cloud_uri: str | None = None) -> str:
    display = to_display_storage_uri(None, cloud_uri=cloud_uri)
    if display:
        return display
    if not storage_path:
        return ""
    resolved = resolve_storage_path(storage_path)
    return to_display_storage_uri(str(resolved)) if resolved else ""


def _public_message(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split())
    if not normalized:
        return ""
    if (
        len(normalized) > _MAX_PUBLIC_MESSAGE_LENGTH
        or "://" in normalized
        or _ABSOLUTE_PATH.search(normalized)
        or _AWS_ACCESS_KEY.search(normalized)
        or _SECRET_MARKER.search(normalized)
    ):
        return _HIDDEN_DETAIL
    return normalized


def _public_messages(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value[:_MAX_PUBLIC_ITEMS]:
        message = _public_message(raw)
        if message and message not in result:
            result.append(message)
    return result


def _public_identifier(value: object) -> str:
    normalized = _public_message(value)
    if normalized == _HIDDEN_DETAIL or not _PUBLIC_IDENTIFIER.fullmatch(normalized):
        return ""
    return normalized


def _public_quality(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    result: dict = {}
    level = _public_identifier(source.get("level"))
    if level:
        result["level"] = level
    score = source.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        result["score"] = score
    if isinstance(source.get("ok"), bool):
        result["ok"] = source["ok"]
    for key in ("error_count", "warning_count"):
        if isinstance(source.get(key), int) and not isinstance(source[key], bool):
            result[key] = source[key]
    issues: list[dict] = []
    raw_issues = source.get("issues")
    for raw in raw_issues[:_MAX_PUBLIC_ITEMS] if isinstance(raw_issues, list) else []:
        if not isinstance(raw, dict):
            continue
        raw_level = raw.get("level") or raw.get("severity") or "info"
        level = str(raw_level).strip().lower()
        if level not in {"error", "warning", "info", "success"}:
            level = "info"
        message = _public_message(raw.get("message"))
        if message:
            issues.append({"level": level, "message": message})
    if issues:
        result["issues"] = issues
    return result


def _public_timing(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    return {
        key: source[key]
        for key in ("duration_sec", "state_pass_sec", "camera_pass_sec", "throughput_gbps")
        if key in source and isinstance(source[key], (int, float))
    }


def _public_mcap_conversion(mcap: dict, storage_uri: str) -> dict:
    result: dict = {}
    for key in ("conversion_type", "status", "episode_id"):
        value = _public_identifier(mcap.get(key))
        if value:
            result[key] = value
    for key in ("size_bytes", "size_gb", "image_workers"):
        value = mcap.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = value
    finished_at = _public_message(mcap.get("finished_at"))
    if finished_at and finished_at != _HIDDEN_DETAIL:
        result["finished_at"] = finished_at
    timing = _public_timing(mcap.get("timing"))
    quality = _public_quality(mcap.get("quality"))
    if timing:
        result["timing"] = timing
    if quality:
        result["quality"] = quality
    if storage_uri:
        result["storage_uri"] = storage_uri
        result["qrdf_storage_uri"] = storage_uri
    return result


def _public_preprocess(preprocess: dict) -> dict:
    result: dict = {}
    for key in ("status", "quality"):
        value = _public_identifier(preprocess.get(key))
        if value:
            result[key] = value
    for key in ("progress", "quality_score"):
        value = preprocess.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = value
    for key in ("started_at", "finished_at"):
        value = _public_message(preprocess.get(key))
        if value and value != _HIDDEN_DETAIL:
            result[key] = value
    message = _public_message(preprocess.get("message"))
    if message:
        result["message"] = message
    steps: list[dict] = []
    raw_steps = preprocess.get("steps")
    for raw in raw_steps[:_MAX_PUBLIC_ITEMS] if isinstance(raw_steps, list) else []:
        if not isinstance(raw, dict):
            continue
        step: dict = {}
        for key in ("name", "status"):
            value = _public_identifier(raw.get(key))
            if value:
                step[key] = value
        progress = raw.get("progress")
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            step["progress"] = progress
        step_message = _public_message(raw.get("message"))
        if step_message:
            step["message"] = step_message
        if step:
            steps.append(step)
    if steps:
        result["steps"] = steps
    anomalies = preprocess.get("anomalies")
    if isinstance(anomalies, list):
        result["anomaly_count"] = len(anomalies)
        public_anomalies = _public_messages(anomalies)
        if public_anomalies:
            result["anomalies"] = public_anomalies
    return result


@router.post("/convert/mcap-to-qrdf")
def convert_mcap_to_qrdf_api(
    body: McapToQrdfRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Pipeline 2: Submit MCAP -> QRDF conversion to the persistent media queue."""
    require_permission(user, "preprocess:write")
    if not body.task_id:
        return {"code": 400, "message": "task_id is required for MCAP conversion", "data": None}
    if body.output_dir:
        return {"code": 400, "message": "custom output_dir is not supported", "data": None}
    task_name = body.task_name
    dataset_name = body.dataset_name
    task = _authorize_task(db, user, body.task_id)
    if body.auto_preprocess and task.status in {"rejected", "failed"}:
        _deny_unfenced_preprocess_retry(user, task.id)
    task_name = task_name or task.name
    dataset_name = dataset_name or f"qrdf_task_{task.id}"
    kwargs = {
        "episode_id": body.episode_id,
        "task_name": task_name,
        "dataset_name": dataset_name,
        "image_workers": body.image_workers,
        "generate_preview": body.generate_preview,
    }
    try:
        payload = dispatch_mcap_to_qrdf(
            True,
            kwargs,
            task_id=body.task_id,
            auto_preprocess=body.auto_preprocess,
            operator=user.get("email", ""),
            actor_id=_actor_id(user),
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"code": 400, "message": str(exc), "data": None}
    except RuntimeError as exc:
        return {"code": 503, "message": str(exc), "data": None}

    return success(payload)


@router.get("/convert/mcap-to-qrdf/{job_id}")
def convert_mcap_to_qrdf_status(
    job_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Query MCAP -> QRDF asynchronous conversion task status."""
    require_permission(user, "preprocess:read")
    job = get_conversion_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="conversion job does not exist")
    task_id = job.get("task_id")
    if not isinstance(task_id, int) or task_id <= 0:
        raise HTTPException(status_code=404, detail="conversion job does not exist")
    try:
        _authorize_task(db, user, task_id)
    except HTTPException as exc:
        if exc.status_code in {403, 404}:
            raise HTTPException(status_code=404, detail="conversion job does not exist") from exc
        raise
    return success(job)


@router.post("/batch")
def batch_preprocess(
    body: PreprocessBatchRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Batch trigger preprocessing tasks executed by media workers."""
    require_permission(user, "preprocess:write")
    operator = user.get("email", "")
    allowed = ("pending_preprocess", "rejected", "failed", "pending_collect", "collect_done")
    tasks: list[Task] = []
    for task_id in body.task_ids:
        task = db.get(Task, task_id)
        if task is not None:
            _authorize_task(db, user, task.id)
        if task is not None and task.status in {"rejected", "failed"}:
            _deny_unfenced_preprocess_retry(user, task.id)
        if task is not None:
            tasks.append(task)
    try:
        require_celery_worker("media")
    except JobDispatchUnavailable as exc:
        return {"code": 503, "message": str(exc), "data": None}
    queued: list[int] = []
    skipped: list[dict] = []

    tasks_by_id = {task.id: task for task in tasks}
    for task_id in body.task_ids:
        task = tasks_by_id.get(task_id)
        if not task:
            skipped.append({"task_id": task_id, "reason": "任务不存在"})
            continue
        if task.status not in allowed:
            skipped.append({"task_id": task_id, "reason": f"状态 {task.status} 不可预处理"})
            continue
        if task.status == "pending_collect" and not task.storage_path:
            skipped.append({"task_id": task_id, "reason": "尚未上传数据"})
            continue
        try:
            ensure_ready_for_preprocess(db, task, operator)
            queued.append(task_id)
        except ValueError as exc:
            skipped.append({"task_id": task_id, "reason": str(exc)})

    if not queued:
        return {"code": 400, "message": "没有可入队的预处理任务", "data": {"skipped": skipped}}

    payload = dispatch_preprocess_batch(queued, operator, max_concurrency=body.max_concurrency)
    return success({**payload, "skipped": skipped})


@router.get("/list")
def list_preprocess_tasks(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "preprocess:read")
    q = db.query(Task).filter(
        Task.status.in_(["pending_preprocess", "preprocess_done", "pending_annotate", "rejected"]),
        task_access_filter(db, actor_id=_actor_id(user)),
    )
    if status:
        q = q.filter(Task.status == status)
    total = q.count()
    tasks = q.order_by(Task.updated_at.desc()).offset((page - 1) * size).limit(size).all()
    return success(
        {
            "total": total,
            "list": [
                {
                    "id": t.id,
                    "project_id": t.project_id,
                    "workspace_id": t.workspace_id,
                    "name": t.name,
                    "status": t.status,
                    "data_source": t.data_source,
                    "pipeline_key": _public_identifier((t.metadata_json or {}).get("pipeline_key")),
                    "preprocess": _public_preprocess(
                        (t.metadata_json or {}).get("preprocess") or {}
                    ),
                    "quality_check": _public_quality((t.metadata_json or {}).get("quality_check")),
                    "updated_at": format_api_datetime(t.updated_at),
                }
                for t in tasks
            ],
        }
    )


@router.get("/{task_id}/status")
def preprocess_status(
    task_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    require_permission(user, "preprocess:read")
    task = _authorize_task(db, user, task_id)
    cached = redis_service.get_task_status(task_id)
    if cached:
        cached_status = cached.get("status")
        return success(
            {
                "task_id": task_id,
                "status": cached_status if isinstance(cached_status, str) else task.status,
                "preprocess": _public_preprocess(cached.get("preprocess") or {}),
                "quality_check": _public_quality(cached.get("quality_check")),
                "cache_source": "redis",
            }
        )
    meta = task.metadata_json or {}
    return success(
        {
            "task_id": task.id,
            "status": task.status,
            "preprocess": _public_preprocess(
                meta.get("preprocess") or {"status": "pending", "progress": 0}
            ),
            "quality_check": _public_quality(meta.get("quality_check")),
            "cache_source": "database",
        }
    )


@router.post("/{task_id}/trigger")
def trigger_preprocess(
    task_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    require_permission(user, "preprocess:write")
    task = _authorize_task(db, user, task_id)
    if task.status in {"rejected", "failed"}:
        _deny_unfenced_preprocess_retry(user, task.id)
    try:
        require_celery_worker("media")
    except JobDispatchUnavailable as exc:
        return {"code": 503, "message": str(exc), "data": None}
    allowed = ("pending_preprocess", "rejected", "failed", "pending_collect", "collect_done")
    if task.status not in allowed:
        return {"code": 400, "message": "当前状态不可触发预处理", "data": None}
    if task.status == "pending_collect" and not task.storage_path:
        return {
            "code": 400,
            "message": "任务尚未上传数据文件，请先在数据接入页完成上传",
            "data": None,
        }
    try:
        task = ensure_ready_for_preprocess(db, task, user.get("email", ""))
    except ValueError as e:
        return {"code": 400, "message": str(e), "data": None}
    start_preprocess(task.id, user.get("email", ""))
    return success({"task_id": task.id, "status": task.status, "message": "预处理已启动"})


@router.post("/{task_id}/terminate")
def terminate(task_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    require_permission(user, "preprocess:write")
    task = _authorize_task(db, user, task_id)
    preprocess = (task.metadata_json or {}).get("preprocess", {})
    if preprocess.get("status") != "running":
        return {"code": 400, "message": "预处理未在运行中", "data": None}
    task = terminate_preprocess(db, task, user.get("email", ""))
    return success({"task_id": task.id, "status": task.status})


@router.get("/{task_id}/result")
def preprocess_result(
    task_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    require_permission(user, "preprocess:read")
    task = _authorize_task(db, user, task_id)
    meta = task.metadata_json or {}
    mcap = meta.get("mcap_to_qrdf") or {}
    preprocess = meta.get("preprocess") or {}
    qrdf_rel = (
        mcap.get("storage_path")
        or preprocess.get("qrdf_dataset_path")
        or (
            task.storage_path
            if task.storage_path and not str(task.storage_path).lower().endswith(".mcap")
            else None
        )
    )
    mcap_enriched = None
    if mcap:
        mcap_cloud = mcap.get("cloud_uri")
        mcap_display = _storage_display_path(mcap.get("storage_path"), cloud_uri=mcap_cloud)
        mcap_enriched = _public_mcap_conversion(mcap, mcap_display)
    cloud_uri = preprocess.get("cloud_uri") or (mcap or {}).get("cloud_uri")
    qrdf_display = _storage_display_path(qrdf_rel, cloud_uri=cloud_uri)
    public_preprocess = _public_preprocess(preprocess)
    return success(
        {
            "task_id": task.id,
            "status": task.status,
            "preprocess": public_preprocess,
            "quality_check": _public_quality(meta.get("quality_check")),
            "anomaly_count": len(preprocess.get("anomalies") or []),
            "anomalies": public_preprocess.get("anomalies", []),
            "mcap_to_qrdf": mcap_enriched,
            "qrdf_storage_uri": qrdf_display,
        }
    )
