import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from data.config import settings
from data.database import (
    JOB_STATUS_QUEUED,
    JOB_STATUS_RETRY_PENDING,
    Task,
    TaskStatus,
    UploadSession,
    get_db,
)
from data.infra import oss_client
from data.integrations.qrdf.service import (
    get_dataset_summary,
    get_preview_status,
    list_dataset_files,
    list_episodes,
    resolve_preview_file,
)
from data.schemas.common import (
    BagImportRequest,
    CollectReviewSubmit,
    OssImportRequest,
    OssScanRequest,
    UploadInit,
    UploadMerge,
)
from data.security.audit import emit_audit_event
from data.services.audit_resource import (
    build_audit_metadata_detail,
    default_episode_id,
    resolve_audit_task_storage,
    sample_audit_episode_frames,
    serve_audit_preview_media,
)
from data.services.bag_ingest import bag_ingest_service, list_bag_folders
from data.services.browser_object_access import issue_browser_preview_url, unavailable_payload
from data.services.cloud_storage import enqueue_external_raw_copy_job
from data.services.oss_import_scope import OssImportScopeError, require_oss_import_scope
from data.services.preprocess import start_preprocess
from data.services.provenance import (
    assert_collection_attribution_complete,
    bind_collection_attribution_on_create,
)
from data.services.public_metadata import (
    serialize_public_qrdf_detail,
    serialize_public_qrdf_episodes,
    serialize_public_qrdf_summary,
)
from data.services.state_machine import transit_task
from data.services.task_dispatcher import (
    JobDispatchUnavailable,
    dispatch_media_job,
    require_celery_worker,
)
from data.services.upload import enqueue_upload_merge_job, upload_service
from data.services.workspace_access import (
    require_actor,
    require_project_actor,
    require_task_actor,
    require_workspace_actor,
    task_access_filter,
)
from data.services.workspace_scope import resolve_task_workspace_id, task_workspace_filter
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, get_role_permissions, require_permission, success
from data.utils.storage_uri import (
    enrich_dataset_files_for_display,
    task_storage_display_uri,
    to_display_storage_uri,
)

router = APIRouter(prefix="/collect", tags=["数据收集"])
logger = logging.getLogger(__name__)


def _actor_id(user: dict) -> int | None:
    try:
        raw = user.get("sub") or user.get("id")
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _audit_direct_preview_issuance(result: object, *, user: dict, task_id: int) -> None:
    if not isinstance(result, dict):
        return
    payload = result.get("data")
    if not isinstance(payload, dict) or payload.get("direct") is not True:
        return
    emit_audit_event(
        "file.direct_url",
        actor=str(user.get("email") or ""),
        resource=f"task:{task_id}",
        detail={"delivery": "oss_browser", "kind": "preview"},
    )


def _authorize_project(db: Session, user: dict, project_id: int):
    try:
        return require_project_actor(db, actor_id=_actor_id(user), project_id=project_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _authorize_workspace(db: Session, user: dict, workspace_id: int):
    try:
        return require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _authorize_task(db: Session, user: dict, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task does not exist")
    try:
        require_task_actor(db, actor_id=_actor_id(user), task=task)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return task


def _authorize_upload(db: Session, user: dict, upload_id: str) -> UploadSession | None:
    upload = db.get(UploadSession, upload_id)
    if upload is None:
        return None
    try:
        actor = require_actor(db, actor_id=_actor_id(user))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if upload.task_id:
        task = _authorize_task(db, user, upload.task_id)
        task_workspace_id = resolve_task_workspace_id(db, task)
        if task_workspace_id is None or upload.workspace_id != task_workspace_id:
            raise HTTPException(status_code=403, detail="upload task workspace mismatch")
    if actor.role == "admin":
        return upload
    if upload.owner_user_id != actor.id or upload.workspace_id is None:
        raise HTTPException(status_code=403, detail="upload session access denied")
    _authorize_workspace(db, user, upload.workspace_id)
    return upload


def _dispatch_persisted_media_job(job) -> None:
    if job.status not in {JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING}:
        return
    try:
        dispatch_media_job(job, worker_prechecked=True)
    except Exception as exc:
        logger.warning(
            "media job remains queued after dispatch failure job_id=%s error_type=%s",
            job.id,
            type(exc).__name__,
        )


@router.get("/bag/folders")
def bag_folders(user: dict = Depends(get_current_user)):
    """List ingestible MCAP folders under ~/data/bag."""
    require_permission(user, "collect:read")
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=403, detail="global bag browsing requires administrator access"
        )
    return success({"folders": list_bag_folders()})


@router.get("/bag/{folder_name}/scan")
def bag_scan(folder_name: str, user: dict = Depends(get_current_user)):
    """Scan the list of MCAP files within the specified bag folder."""
    require_permission(user, "collect:read")
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=403, detail="global bag browsing requires administrator access"
        )
    try:
        result = bag_ingest_service.scan(folder_name)
    except FileNotFoundError:
        return {"code": 404, "message": "bag directory does not exist", "data": None}
    except ValueError:
        return {"code": 400, "message": "invalid bag request", "data": None}
    except Exception as exc:
        logger.warning("bag scan failed error_type=%s", type(exc).__name__)
        return {"code": 400, "message": "bag scan failed", "data": None}
    return success(result)


@router.post("/bag/import")
def bag_import(
    body: BagImportRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Ingest MCAP from the specified folder under ~/data/bag into a QRDF dataset via QRDF Recorder SDK."""
    require_permission(user, "collect:write")
    if body.task_id:
        _authorize_task(db, user, body.task_id)
    else:
        _authorize_project(db, user, body.project_id)
    try:
        result = bag_ingest_service.import_folder(
            db,
            folder_name=body.folder_name,
            project_id=body.project_id,
            task_id=body.task_id,
            task_name=body.task_name,
            episode_limit=body.episode_limit,
            operator=user.get("email", "system"),
            actor_id=_actor_id(user),
            auto_preprocess=body.auto_preprocess,
        )
    except (FileNotFoundError, ValueError):
        return {"code": 400, "message": "bag import request failed", "data": None}
    except Exception as exc:
        db.rollback()
        logger.warning("bag import failed error_type=%s", type(exc).__name__)
        return {"code": 400, "message": "bag import failed", "data": None}
    return success(result)


@router.get("/tasks")
def collect_task_board(
    project_id: int | None = Query(None),
    workspace_id: int | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Data intake task dashboard (Technical Design 7.3.2 Data Intake Task Page)."""
    require_permission(user, "collect:read")
    q = db.query(Task).filter(Task.task_type == "collect")
    if workspace_id:
        _authorize_workspace(db, user, workspace_id)
        q = q.filter(task_workspace_filter(db, workspace_id))
    elif project_id:
        _authorize_project(db, user, project_id)
        q = q.filter(Task.project_id == project_id)
    else:
        q = q.filter(task_access_filter(db, actor_id=_actor_id(user)))
    if status:
        q = q.filter(Task.status == status)
    total = q.count()
    tasks = q.order_by(Task.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return success(
        {
            "total": total,
            "list": [
                {
                    "id": t.id,
                    "project_id": t.project_id,
                    "workspace_id": resolve_task_workspace_id(db, t),
                    "name": t.name,
                    "status": t.status,
                    "reject_reason": t.reject_reason or "",
                    "data_source": t.data_source,
                    "collector": (
                        {
                            "id": t.collector_profile.id,
                            "display_name": t.collector_profile.display_name,
                        }
                        if t.collector_profile
                        else None
                    ),
                    "collection_source": (
                        {
                            "id": t.collection_source.id,
                            "label": t.collection_source.label,
                            "kind": t.collection_source.kind,
                        }
                        if t.collection_source
                        else None
                    ),
                    "storage_uri": task_storage_display_uri(t),
                    "created_at": format_api_datetime(t.created_at),
                    "collect_review": ((t.metadata_json or {}).get("collect") or {}).get("review")
                    or {},
                }
                for t in tasks
            ],
        }
    )


@router.post("/upload/init")
def upload_init(
    body: UploadInit,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "collect:write")
    try:
        actor = require_actor(db, actor_id=_actor_id(user))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    workspace_id = body.workspace_id
    if body.task_id:
        task = _authorize_task(db, user, body.task_id)
        task_workspace_id = resolve_task_workspace_id(db, task)
        if workspace_id is not None and workspace_id != task_workspace_id:
            raise HTTPException(
                status_code=400, detail="upload workspace does not match task workspace"
            )
        workspace_id = task_workspace_id
    elif workspace_id is None and actor.role != "admin":
        raise HTTPException(status_code=400, detail="workspace_id is required for unbound uploads")
    elif workspace_id is not None:
        _authorize_workspace(db, user, workspace_id)
    try:
        result = upload_service.init_upload(
            db,
            body.file_md5,
            body.total_chunks,
            body.file_name,
            body.task_id,
            owner_user_id=actor.id,
            workspace_id=workspace_id,
        )
        if body.task_id:
            task = db.get(Task, body.task_id)
            if task and task.ingested_by_user_id is None:
                task.ingested_by_user_id = _actor_id(user)
                db.commit()
    except ValueError as e:
        return {"code": 400, "message": str(e), "data": None}
    return success(result)


@router.post("/upload/chunk")
async def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "collect:write")
    _authorize_upload(db, user, upload_id)
    try:
        await upload_service.save_chunk_stream(db, upload_id, chunk_index, file)
    except ValueError as e:
        return {"code": 400, "message": str(e), "data": None}
    finally:
        await file.close()
    return success({"success": True, "chunk_index": chunk_index})


@router.get("/upload/{upload_id}/status")
def upload_status(
    upload_id: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    """Chunked upload progress polling (Technical Design 7.4.1 Task Interaction Specification)."""
    require_permission(user, "collect:read")
    _authorize_upload(db, user, upload_id)
    session = db.get(UploadSession, upload_id)
    if not session:
        return {"code": 404, "message": "upload_id 不存在", "data": None}
    uploaded = session.uploaded_chunks or []
    round(len(uploaded) / session.total_chunks * 100) if session.total_chunks else 0
    cached = upload_service.get_upload_progress(session)
    merge_result = {}
    if session.status == "merged" and isinstance(session.merge_result_json, dict):
        # The merge result is consumed by the browser to advance the workflow.
        # Keep it deliberately small so storage implementation details and any
        # future worker metadata cannot become part of the public contract.
        for key in ("upload_id", "task_id", "format", "file_name", "follow_up_job_id"):
            value = session.merge_result_json.get(key)
            if value is not None:
                merge_result[key] = value
    return success(
        {
            "upload_id": upload_id,
            "status": session.status,
            "uploaded_chunks": cached["uploaded_chunks"],
            "total_chunks": cached["total_chunks"],
            "progress": cached["progress"],
            "cache_source": cached.get("source", "database"),
            "task_id": session.task_id,
            "merge_job_id": session.merge_job_id,
            "merge_result": merge_result,
        }
    )


@router.post("/upload/merge")
def upload_merge(
    body: UploadMerge,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "collect:write")
    _authorize_upload(db, user, body.upload_id)
    try:
        require_celery_worker("media")
    except JobDispatchUnavailable:
        return JSONResponse(
            status_code=503,
            content={"code": 503, "message": "media worker is unavailable", "data": None},
        )
    try:
        job = enqueue_upload_merge_job(
            db,
            body.upload_id,
            body.file_md5,
            actor_id=_actor_id(user),
        )
    except ValueError as e:
        return {"code": 400, "message": str(e), "data": None}
    _dispatch_persisted_media_job(job)
    return JSONResponse(
        status_code=202,
        content=success(
            {
                "upload_id": body.upload_id,
                "job": {"id": job.id, "status": job.status},
            }
        ),
    )


@router.post("/oss/scan")
def oss_scan(
    body: OssScanRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Scan object list under the specified prefix in an external OSS bucket (must be associated with an existing intake task)."""
    require_permission(user, "collect:read")
    task = _authorize_task(db, user, body.task_id)
    if task.task_type != "collect":
        return {"code": 400, "message": "只能关联数据接入类型的任务", "data": None}
    try:
        require_oss_import_scope(db, task=task, bucket=body.bucket, keys=[body.prefix])
    except OssImportScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not oss_client.is_oss_configured():
        return {
            "code": 400,
            "message": "OSS 凭据未配置，请在 backend/.env 设置 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET 后重启服务",
            "data": None,
        }
    from data.services.storage_mode import allow_oss_import

    if not allow_oss_import():
        return {
            "code": 400,
            "message": "当前为纯本地存储模式（storage_mode=local），不支持 OSS 扫描；请改用本地上传，或将 storage_mode 设为 hybrid/cloud",
            "data": None,
        }
    try:
        objects = oss_client.list_prefix(body.bucket, body.prefix, body.extension_filter)
    except Exception as exc:
        logger.warning("OSS scan failed task_id=%s error_type=%s", task.id, type(exc).__name__)
        return {"code": 400, "message": "OSS scan failed", "data": None}
    return success({"bucket": body.bucket, "prefix": body.prefix, "objects": objects})


def _infer_oss_import_format(key: str) -> str:
    """Infer OSS import object format by file name."""
    import re

    name = key.lower().rsplit("/", 1)[-1]
    if name.endswith(".mcap"):
        return "mcap"
    if name.endswith(".qrdf.zip") or name.endswith("_qrdf.zip"):
        return "qrdf"
    if re.match(r"^episode_\d{6}\.zip$", name):
        return "qrdf"
    if name.endswith(".zip"):
        return "qrdf"
    return "unknown"


def _create_or_reuse_oss_task(
    db: Session,
    *,
    ref_task: Task,
    key: str,
    bucket: str,
    actor_id: int | None,
    reuse_existing: bool = False,
) -> Task:
    """Create or reuse an intake task: reuse an associated empty task for the first file, create new tasks in the same project for remaining files."""
    file_name = Path(key).name
    is_new_task = False
    if (
        reuse_existing
        and ref_task.status == TaskStatus.PENDING_COLLECT.value
        and not ref_task.storage_path
    ):
        task = ref_task
        task.name = f"oss-import-{file_name}"
        task.data_source = "oss_import"
    else:
        task = Task(
            project_id=ref_task.project_id,
            workspace_id=ref_task.workspace_id,
            name=f"oss-import-{file_name}",
            task_type="collect",
            data_source="oss_import",
        )
        db.add(task)
        db.flush()
        is_new_task = True
    task.ingested_by_user_id = actor_id
    if task.created_by_user_id is None:
        task.created_by_user_id = actor_id
    task.storage_path = None
    task.metadata_json = {
        **(task.metadata_json or {}),
        "collect": {
            "source": "oss_import",
            "source_bucket": bucket,
            "source_key": key,
            "bucket": bucket,
            "key": key,
            "file_name": file_name,
        },
    }
    try:
        if is_new_task and ref_task.collection_source_id and ref_task.collector_id:
            bind_collection_attribution_on_create(
                db,
                task=task,
                collection_source_id=ref_task.collection_source_id,
                collector_id=ref_task.collector_id,
                actor_id=actor_id,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(task)
    return task


@router.post("/oss/import")
def oss_import(
    body: OssImportRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Batch import objects from an external OSS bucket and associate them with existing collect tasks."""
    require_permission(user, "collect:write")
    ref_task = _authorize_task(db, user, body.task_id)
    if body.workspace_id:
        _authorize_workspace(db, user, body.workspace_id)
    if body.project_id:
        _authorize_project(db, user, body.project_id)
    if ref_task.task_type != "collect":
        return {"code": 400, "message": "只能关联数据接入类型的任务", "data": None}
    try:
        require_oss_import_scope(db, task=ref_task, bucket=body.bucket, keys=body.keys)
    except OssImportScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    from data.services.storage_mode import allow_oss_import

    if not allow_oss_import():
        return {
            "code": 400,
            "message": "当前为纯本地存储模式（storage_mode=local），不支持从对象存储导入；请改用本地上传，或将 storage_mode 设为 hybrid/cloud",
            "data": None,
        }

    try:
        require_celery_worker("media")
    except JobDispatchUnavailable:
        return JSONResponse(
            status_code=503,
            content={"code": 503, "message": "media worker is unavailable", "data": None},
        )

    created = []
    errors = []
    skipped = []
    seen_paths = set()
    target_workspace_id = resolve_task_workspace_id(db, ref_task)
    if target_workspace_id is None:
        raise HTTPException(status_code=400, detail="OSS import task has no workspace")
    for idx, key in enumerate(body.keys):
        storage_path = f"oss://{body.bucket}/{key}"
        if storage_path in seen_paths:
            skipped.append({"key": key, "task_id": None, "reason": "当前请求中重复的路径"})
            continue
        seen_paths.add(storage_path)

        existing = (
            db.query(Task)
            .filter(
                task_workspace_filter(db, target_workspace_id),
                Task.task_type == "collect",
                or_(
                    Task.storage_path == storage_path,
                    and_(
                        Task.metadata_json["collect"]["bucket"].as_string() == body.bucket,
                        Task.metadata_json["collect"]["key"].as_string() == key,
                    ),
                ),
            )
            .first()
        )
        if existing:
            skipped.append(
                {"key": key, "task_id": existing.id, "reason": "该 OSS 路径已存在接入任务"}
            )
            continue

        try:
            task = _create_or_reuse_oss_task(
                db,
                ref_task=ref_task,
                key=key,
                bucket=body.bucket,
                actor_id=_actor_id(user),
                reuse_existing=(idx == 0 and not ref_task.storage_path),
            )
            job = enqueue_external_raw_copy_job(db, task.id, actor_id=_actor_id(user))
        except Exception as exc:
            db.rollback()
            logger.warning("OSS raw-copy job registration failed error_type=%s", type(exc).__name__)
            errors.append({"key": key, "error": "import_registration_failed"})
            continue
        _dispatch_persisted_media_job(job)
        created.append(
            {
                "task_id": task.id,
                "status": task.status,
                "key": key,
                "job": {"id": job.id, "status": job.status},
            }
        )

    return JSONResponse(
        status_code=202,
        content=success(
            {"created": created, "skipped": skipped, "errors": errors, "total": len(body.keys)}
        ),
    )


def _user_has_collect_write(user: dict) -> bool:
    if user.get("role") == "admin":
        return True
    perms = set(user.get("permissions") or get_role_permissions(user.get("role", "")))
    return "*" in perms or "collect:write" in perms or "collect:*" in perms


def _collect_review_ready(task: Task) -> tuple[bool, str]:
    meta = task.metadata_json or {}
    collect = meta.get("collect") or {}
    conv = meta.get("mcap_to_qrdf") or {}
    if not task.storage_path:
        return False, "任务尚未上传数据文件"
    conv_mode = collect.get("conversion") or ""
    fmt = (collect.get("format") or "").lower()
    if fmt == "unknown":
        return (
            False,
            "未能识别为 MCAP 或 QRDF 数据包。请上传 .mcap，或标准 QRDF zip（含 dataset.json / episodes），"
            "或单 episode 包（episode_XXXXXX.zip）",
        )
    if fmt == "mcap" or conv_mode == "mcap_to_qrdf_pending":
        if conv.get("error") or conv.get("status") == "failed":
            return False, "MCAP 转换失败，请重新上传"
        if conv_mode == "mcap_to_qrdf_pending" and not (
            conv.get("storage_path") or conv.get("qrdf_path")
        ):
            return False, "MCAP 转换进行中，请稍后审核"
    return True, ""


def _public_mcap_conversion(conv: object) -> dict:
    """Construct conversion DTO for intake review, omitting internal file paths by default."""
    if not isinstance(conv, dict):
        return {}

    public: dict = {}
    for key in ("conversion_type", "status", "episode_id", "finished_at", "run_id"):
        value = conv.get(key)
        if isinstance(value, str) and value:
            public[key] = value
    for key in ("progress", "size_bytes", "size_gb", "image_workers"):
        value = conv.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            public[key] = value

    timing = conv.get("timing")
    if isinstance(timing, dict):
        public_timing = {
            key: value
            for key in ("duration_sec", "state_pass_sec", "camera_pass_sec", "throughput_gbps")
            if isinstance((value := timing.get(key)), (int, float)) and not isinstance(value, bool)
        }
        if public_timing:
            public["timing"] = public_timing

    quality = conv.get("quality")
    if isinstance(quality, dict):
        public_quality = {}
        if isinstance(quality.get("level"), str) and quality["level"]:
            public_quality["level"] = quality["level"]
        if isinstance(quality.get("ok"), bool):
            public_quality["ok"] = quality["ok"]
        for key in ("score", "episode_count"):
            value = quality.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                public_quality[key] = value
        if public_quality:
            public["quality"] = public_quality

    for key in ("cloud_uri", "storage_uri", "storage_path"):
        raw = conv.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        parsed = urlsplit(raw)
        decoded = unquote(raw).lower()
        if (
            parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or any(
                marker in decoded for marker in ("access_key=", "password=", "secret=", "token=")
            )
        ):
            continue
        if parsed.scheme:
            if parsed.scheme not in ("oss", "nas") or not parsed.netloc:
                continue
            display_uri = raw
        else:
            path = Path(raw)
            if path.is_absolute() or ".." in path.parts or raw.startswith(("/", "\\")):
                continue
            display_uri = to_display_storage_uri(raw)
        if display_uri.startswith(("oss://", "nas://")):
            public["storage_path"] = display_uri
            public["storage_uri"] = display_uri
            break

    if public.get("status") == "failed":
        public["error"] = "MCAP conversion failed"
    return public


@router.get("/{task_id}/review")
def get_collect_review(
    task_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Intake review: inspect raw uploaded data details (independent from pipeline manual audit)."""
    require_permission(user, "collect:read")
    task = _authorize_task(db, user, task_id)
    meta = task.metadata_json or {}
    collect = meta.get("collect") or {}
    conv = meta.get("mcap_to_qrdf") or {}
    review = collect.get("review") or {}
    ready, ready_msg = _collect_review_ready(task)
    can_submit = task.status == "collect_done" and ready and _user_has_collect_write(user)
    file_size = None
    storage = resolve_audit_task_storage(task)
    metadata_detail = build_audit_metadata_detail(task, db=db) if storage else {}
    public_metadata_detail = serialize_public_qrdf_detail(metadata_detail)
    episodes = public_metadata_detail.get("episodes") or []
    topics = public_metadata_detail.get("topics") or []
    sdk_summary = public_metadata_detail.get("sdk_summary") or serialize_public_qrdf_summary(
        get_dataset_summary(storage) if storage else {}
    )
    quality_check = public_metadata_detail.get("quality_check") or {}
    preview_status = (
        get_preview_status(storage, generate=False)
        if storage
        else {"available": False, "error": ""}
    )
    if task.storage_path and not str(task.storage_path).startswith("oss://"):
        try:
            local = Path(task.storage_path)
            if not local.is_absolute():
                local = Path(settings.storage_root) / task.storage_path
            if local.exists():
                file_size = local.stat().st_size
        except OSError:
            pass
    elif conv.get("size_bytes"):
        file_size = conv.get("size_bytes")
    preview_api = (
        f"/api/v1/collect/{task.id}/review/preview/media" if preview_status.get("available") else ""
    )
    display_uri = task_storage_display_uri(task)
    preview_storage = storage or task.storage_path
    return success(
        {
            "task_id": task.id,
            "name": task.name,
            "status": task.status,
            "data_source": task.data_source,
            "storage_path": display_uri,
            "storage_uri": display_uri,
            "file_name": collect.get("file_name")
            or (Path(task.storage_path).name if task.storage_path else ""),
            "format": collect.get("format") or "",
            "file_size": file_size,
            "merged_at": collect.get("merged_at") or "",
            "conversion": collect.get("conversion") or "",
            "mcap_to_qrdf": _public_mcap_conversion(conv),
            "reject_reason": task.reject_reason or review.get("reject_reason") or "",
            "review_history": review,
            "ready": ready,
            "ready_message": ready_msg,
            "can_submit": can_submit,
            "episodes": episodes,
            "topics": topics,
            "tags": public_metadata_detail.get("tags") or [],
            "sdk_summary": sdk_summary,
            "quality_check": quality_check,
            "quality_level": quality_check.get("level") or "",
            "metadata_detail": public_metadata_detail,
            "episode_id": default_episode_id(task, storage),
            "preview_available": preview_status.get("available", False),
            "preview_error": preview_status.get("error", ""),
            "preview_api": preview_api,
            "files": enrich_dataset_files_for_display(
                preview_storage,
                list_dataset_files(preview_storage),
            )
            if preview_storage
            else [],
        }
    )


@router.get("/{task_id}/review/preview")
def get_collect_review_preview(
    task_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Intake review preview metadata (aligned with QRDF details preview endpoint)."""
    require_permission(user, "collect:read")
    task = _authorize_task(db, user, task_id)
    storage = resolve_audit_task_storage(task)
    if not storage:
        return success(
            {
                "task_id": task_id,
                "preview_available": False,
                "preview_error": "暂无可用数据集路径，请等待转换完成",
                "preview_api": "",
            }
        )
    status = get_preview_status(storage, generate=True)
    preview_api = (
        f"/api/v1/collect/{task_id}/review/preview/media" if status.get("available") else ""
    )
    meta = task.metadata_json or {}
    return success(
        {
            "task_id": task_id,
            "name": task.name,
            "preview_available": status.get("available", False),
            "preview_error": status.get("error", ""),
            "preview_api": preview_api,
            "media_type": status.get("media_type"),
            "sdk_summary": serialize_public_qrdf_summary(get_dataset_summary(storage)),
            "episodes": serialize_public_qrdf_episodes(
                list_episodes(storage) or meta.get("episodes") or []
            ),
            "files": enrich_dataset_files_for_display(storage, list_dataset_files(storage)),
        }
    )


@router.get("/{task_id}/review/preview/media")
def collect_review_preview_media(
    task_id: int,
    topic: str = Query("head_color"),
    direct: bool = False,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "collect:read")
    task = _authorize_task(db, user, task_id)
    storage = resolve_audit_task_storage(task)
    if storage:
        status = get_preview_status(storage, generate=True)
        if status.get("available"):
            preview_path = resolve_preview_file(storage, generate=False) or status.get("path")
            p = Path(preview_path) if preview_path else None
            if p and p.is_file():
                media_type = str(status.get("media_type") or "video/mp4")
                if direct:
                    access = issue_browser_preview_url(
                        p,
                        resource_type="task",
                        resource_id=task_id,
                        media_type=media_type,
                    )
                    result = success(
                        access.as_payload() if access else unavailable_payload(media_type)
                    )
                    _audit_direct_preview_issuance(result, user=user, task_id=task_id)
                    return result
                return FileResponse(p, media_type=media_type)
    result = serve_audit_preview_media(task, topic, direct=direct)
    if direct:
        _audit_direct_preview_issuance(result, user=user, task_id=task_id)
    if isinstance(result, FileResponse):
        return result
    return result


@router.get("/{task_id}/review/episodes/{episode_id}/frames")
def get_collect_review_episode_frames(
    task_id: int,
    episode_id: str,
    topic: str | None = Query(None),
    start_index: int = Query(0, ge=0),
    limit: int = Query(8, ge=1, le=32),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "collect:read")
    task = _authorize_task(db, user, task_id)
    payload = sample_audit_episode_frames(
        task,
        episode_id,
        topic=topic,
        start_index=start_index,
        limit=limit,
    )
    if not payload.get("ok", True) and not payload.get("frames"):
        return {"code": 400, "message": payload.get("message", "帧采样失败"), "data": None}
    return success(payload)


@router.post("/{task_id}/review/submit")
def submit_collect_review(
    task_id: int,
    body: CollectReviewSubmit,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Intake review: transition automatically to preprocessing on pass, revert to pending collect on reject."""
    require_permission(user, "collect:write")
    task = _authorize_task(db, user, task_id)
    if task.status != "collect_done":
        return {"code": 400, "message": "任务不在待接入审核状态", "data": None}
    ready, ready_msg = _collect_review_ready(task)
    if not ready:
        return {"code": 400, "message": ready_msg or "数据尚未就绪", "data": None}
    operator = user.get("email", "system")
    meta = dict(task.metadata_json or {})
    collect = dict(meta.get("collect") or {})
    review = dict(collect.get("review") or {})
    if body.is_passed:
        try:
            assert_collection_attribution_complete(db, task)
        except ValueError as exc:
            return {"code": 400, "message": str(exc), "data": None}
        try:
            require_celery_worker("media")
        except JobDispatchUnavailable as exc:
            return {"code": 503, "message": str(exc), "data": None}
        review.update(
            {
                "is_passed": True,
                "reject_reason": body.reject_reason,
                "reviewer": operator,
                "reviewed_at": format_api_datetime(datetime.utcnow()),
            }
        )
        collect["review"] = review
        meta["collect"] = collect
        task.metadata_json = meta
        task.reject_reason = ""
        db.commit()
        task = transit_task(
            db, task, "start_preprocess", operator, body.reject_reason or "接入审核通过"
        )
        dispatch_mode = start_preprocess(task.id, operator)
        return success(
            {
                "task_id": task.id,
                "status": task.status,
                "dispatch": dispatch_mode,
                "message": "接入审核通过，已自动进入预处理",
            }
        )
    if not body.reject_reason.strip():
        return {"code": 400, "message": "请填写驳回原因", "data": None}
    review.update(
        {
            "is_passed": False,
            "reject_reason": body.reject_reason,
            "reviewer": operator,
            "reviewed_at": format_api_datetime(datetime.utcnow()),
        }
    )
    collect["review"] = review
    meta["collect"] = collect
    task.metadata_json = meta
    db.commit()
    task = transit_task(db, task, "collect_reject", operator, body.reject_reason)
    return success({"task_id": task.id, "status": task.status, "message": "已驳回，请重新上传数据"})
