from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.config import settings
from data.database import (
    Dataset,
    DatasetRevision,
    ExportJob,
    Project,
    QrdfData,
    QuerySession,
    Task,
    User,
    get_db,
)
from data.integrations.qrdf import compute_dataset_stats_from_records
from data.integrations.qrdf.lerobot_export import LEROBOT_EXPORT_TEMPLATES
from data.schemas.common import (
    ApiResponse,
    DatasetCreate,
    DatasetQuery,
    DatasetRevisionCreate,
    DatasetRevisionExportCreate,
    DatasetRevisionList,
    DatasetRevisionOut,
    ExportCreate,
    QrdfToLerobotRequest,
)
from data.security.audit import emit_audit_event
from data.security.signed_url import (
    consume_download_jti,
    create_signed_download_token,
    verify_signed_download_token,
)
from data.services.browser_object_access import issue_browser_download_url, unavailable_payload
from data.services.cloud_storage import cleanup_export_staging
from data.services.conversion import resolve_qrdf_storage_path
from data.services.conversion_jobs import dispatch_qrdf_to_lerobot, get_conversion_job
from data.services.dataset_revisions import (
    WorkflowConflict,
    create_dataset_revision,
    enqueue_dataset_export,
    list_dataset_revisions,
    retire_dataset_revision,
    serialize_revision_summary,
)
from data.services.dataset_service import (
    build_dataset_detail,
    build_lineage_snapshot,
    enrich_dataset_stats,
    next_dataset_version,
)
from data.services.export_authorization import (
    ExportScopeError,
    actor_can_access_export_job,
    build_export_authorization_scope,
)
from data.services.export_files import list_export_output_files, resolve_export_file
from data.services.job_runs import (
    UNFENCED_RETRY_PUBLIC_MESSAGE,
    ResourceFencingRequired,
    require_recoverable_job_kind,
)
from data.services.query_service import search_packages
from data.services.task_dispatcher import (
    JobDispatchUnavailable,
    dispatch_export,
    dispatch_media_job,
    require_celery_worker,
)
from data.services.workspace_access import (
    project_access_filter,
    qrdf_access_filter,
    require_project_actor,
    require_qrdf_actor,
    require_task_actor,
    require_workspace_actor,
)
from data.services.workspace_scope import ensure_default_project, project_ids_for_workspace
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, get_optional_user, require_permission, success
from data.utils.storage_uri import (
    export_job_storage_display_uri,
    is_cloud_uri,
    to_display_storage_uri,
    to_download_uri,
)

router = APIRouter(prefix="/dataset", tags=["数据集"])


def _actor_id(user: dict | None) -> int | None:
    if not user:
        return None
    try:
        return int(user.get("sub") or user.get("id"))
    except (TypeError, ValueError):
        return None


def _export_is_accessible(db: Session, user: dict | None, job: ExportJob) -> bool:
    return actor_can_access_export_job(db, actor_id=_actor_id(user), export=job)


def _require_project_access(db: Session, user: dict, project_id: int) -> None:
    try:
        require_project_actor(db, actor_id=_actor_id(user), project_id=project_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="project does not exist") from exc


def _require_workspace_access(db: Session, user: dict, workspace_id: int) -> None:
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="workspace does not exist") from exc


def _dataset_if_accessible(db: Session, user: dict, dataset_id: int) -> Dataset | None:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        return None
    try:
        require_project_actor(db, actor_id=_actor_id(user), project_id=dataset.project_id)
    except (PermissionError, ValueError):
        return None
    return dataset


def _dataset_qrdf_items_if_accessible(
    db: Session,
    user: dict,
    dataset: Dataset,
) -> list[QrdfData] | None:
    qrdf_ids = list(dataset.qrdf_ids or [])
    if not qrdf_ids:
        return []
    try:
        build_export_authorization_scope(
            db,
            qrdf_ids=qrdf_ids,
            dataset_id=dataset.id,
            actor_id=_actor_id(user),
        )
    except (ExportScopeError, PermissionError, TypeError, ValueError):
        return None
    records = db.query(QrdfData).filter(QrdfData.id.in_(qrdf_ids)).all()
    if len(records) != len(set(qrdf_ids)):
        return None
    return records


def _conversion_scope_is_accessible(db: Session, user: dict, job: dict) -> bool:
    """Validate the immutable legacy conversion scope against the current actor."""
    raw_scope = job.get("authorization_scope")
    if (
        not isinstance(raw_scope, dict)
        or raw_scope.get("version") != 1
        or not isinstance(raw_scope.get("creator_actor_id"), int)
        or db.get(User, raw_scope["creator_actor_id"]) is None
    ):
        return False
    try:
        resolved = build_export_authorization_scope(
            db,
            qrdf_ids=list(raw_scope.get("qrdf_ids") or []),
            dataset_id=raw_scope.get("dataset_id"),
            actor_id=_actor_id(user),
        )
    except (ExportScopeError, PermissionError, TypeError, ValueError):
        return False
    return all(
        resolved.get(key) == raw_scope.get(key)
        for key in ("workspace_ids", "project_ids", "qrdf_ids", "dataset_id")
    )


def _public_conversion_job(job: dict) -> dict:
    """Never return converter filesystem paths or worker exceptions to the API."""
    return {
        key: job[key]
        for key in (
            "job_id",
            "conversion_type",
            "status",
            "progress",
            "async",
            "started_at",
            "finished_at",
        )
        if key in job
    }


def _export_item(
    job: ExportJob,
    db: Session | None = None,
    *,
    signed_actor_id: int | None = None,
) -> dict:
    file_path = resolve_export_file(job)
    params = job.params_json or {}
    st = str(job.status or "").upper()
    storage_uri = export_job_storage_display_uri(job) if st == "COMPLETED" else ""
    if not storage_uri and st == "COMPLETED" and file_path:
        try:
            storage_uri = to_display_storage_uri(
                str(file_path.resolve().relative_to(Path(settings.storage_root).resolve()))
            )
        except ValueError:
            storage_uri = to_display_storage_uri(str(file_path.resolve()))
    download_url = storage_uri
    download_api = ""
    signed_download_url = ""
    if (
        st == "COMPLETED"
        and signed_actor_id is not None
        and (file_path or is_cloud_uri(storage_uri))
    ):
        download_api = f"/api/v1/dataset/export/{job.id}/download"
        # Short-lived signed link: allows downloading via query `sig=` without Bearer (time-limited and use-limited)
        try:
            sig = create_signed_download_token(
                resource_type="export",
                resource_id=job.id,
                subject=str(signed_actor_id),
                ttl_seconds=600,
                max_uses=5,
            )
            signed_download_url = f"{download_api}?sig={sig}"
        except Exception:
            signed_download_url = ""
    project_id = job.project_id or params.get("project_id")
    dataset_name = ""
    if db and job.dataset_id:
        ds = db.get(Dataset, job.dataset_id)
        if ds:
            dataset_name = ds.name
    return {
        "export_id": job.id,
        "dataset_id": job.dataset_id,
        "dataset_name": dataset_name,
        "project_id": project_id,
        "query_id": job.query_id or None,
        "status": job.status,
        "progress": job.progress,
        "download_url": download_url,
        "download_api": download_api,
        "signed_download_url": signed_download_url,
        "download_filename": file_path.name if file_path else "",
        "storage_path": storage_uri,
        "storage_uri": storage_uri,
        "storage_path_abs": storage_uri,
        "format": job.format,
        "export_template": (job.params_json or {}).get("export_template", "generic"),
        "lerobot_version": (job.params_json or {}).get("lerobot_version", "v3.0"),
        "error_message": job.error_message,
        "created_at": format_api_datetime(job.created_at),
        "output_files": list_export_output_files(job) if (st == "COMPLETED" and file_path) else [],
    }


@router.post("/convert/qrdf-to-lerobot")
def convert_qrdf_to_lerobot_api(
    body: QrdfToLerobotRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Pipeline 5: QRDF -> LeRobot training data conversion (synchronous or asynchronous)."""
    require_permission(user, "export:write")
    if body.storage_path:
        return {"code": 400, "message": "storage_path source is not supported", "data": None}
    if bool(body.qrdf_id) == bool(body.task_id):
        return {
            "code": 400,
            "message": "please provide exactly one QRDF or task source",
            "data": None,
        }

    qrdf_item = None
    if body.qrdf_id:
        qrdf_item = db.get(QrdfData, body.qrdf_id)
        if qrdf_item is None:
            return {"code": 404, "message": "QRDF source does not exist", "data": None}
        try:
            require_qrdf_actor(db, actor_id=_actor_id(user), qrdf=qrdf_item)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="workspace access denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="QRDF source does not exist") from exc
    else:
        task = db.get(Task, body.task_id)
        if task is None:
            return {"code": 404, "message": "task source does not exist", "data": None}
        try:
            require_task_actor(db, actor_id=_actor_id(user), task=task)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="workspace access denied") from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="task source does not exist") from exc
        qrdf_item = (
            db.query(QrdfData)
            .filter(QrdfData.task_id == task.id, QrdfData.project_id == task.project_id)
            .order_by(QrdfData.id.desc())
            .first()
        )
        if qrdf_item is None:
            return {"code": 400, "message": "task has no indexed QRDF source", "data": None}

    try:
        scope = build_export_authorization_scope(
            db,
            qrdf_ids=[qrdf_item.id],
            dataset_id=None,
            actor_id=_actor_id(user),
        )
        storage = resolve_qrdf_storage_path(qrdf_id=qrdf_item.id, db=db)
    except (FileNotFoundError, ValueError, ExportScopeError) as exc:
        return {"code": 400, "message": str(exc), "data": None}

    image_size = None
    if body.image_size:
        w, h = body.image_size.split(",")
        image_size = (int(w), int(h))

    kwargs = {
        "storage_path": storage,
        "output_dir": body.output_dir,
        "fps": body.fps,
        "lerobot_version": body.lerobot_version,
        "image_size": image_size,
        "export_template": body.export_template,
        "_authorization_scope": scope,
    }
    try:
        payload = dispatch_qrdf_to_lerobot(body.async_mode, kwargs)
    except (FileNotFoundError, ValueError) as exc:
        return {"code": 400, "message": str(exc), "data": None}
    except RuntimeError as exc:
        return {"code": 503, "message": str(exc), "data": None}

    return success(_public_conversion_job(payload))


@router.get("/convert/qrdf-to-lerobot/{job_id}")
def convert_qrdf_to_lerobot_status(
    job_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Query QRDF -> LeRobot asynchronous conversion task status."""
    require_permission(user, "export:read")
    job = get_conversion_job(job_id)
    if not job:
        return {"code": 404, "message": "转换任务不存在或已过期", "data": None}
    if not _conversion_scope_is_accessible(db, user, job):
        return {"code": 404, "message": "转换任务不存在或已过期", "data": None}
    return success(_public_conversion_job(job))


@router.post("/create")
def create_dataset(
    body: DatasetCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:write")
    name = str(body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="dataset name is required")
    if body.workspace_id:
        _require_workspace_access(db, user, body.workspace_id)
        workspace_id = body.workspace_id
        project_id = ensure_default_project(db, body.workspace_id).id
    elif body.project_id:
        _require_project_access(db, user, body.project_id)
        project_id = body.project_id
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project does not exist")
        workspace_id = project.workspace_id
    else:
        return {"code": 400, "message": "请指定数采工作空间 workspace_id", "data": None}
    if (
        db.query(Dataset.id)
        .filter(Dataset.workspace_id == workspace_id, Dataset.name == name)
        .first()
        is not None
    ):
        raise HTTPException(status_code=409, detail="dataset already exists in workspace")
    qrdf_list = (
        db.query(QrdfData).filter(QrdfData.id.in_(body.qrdf_ids)).all() if body.qrdf_ids else []
    )
    if body.qrdf_ids:
        found_ids = {q.id for q in qrdf_list}
        missing = set(body.qrdf_ids) - found_ids
        if missing:
            return {"code": 404, "message": f"QRDF 不存在: {sorted(missing)}", "data": None}
        for qrdf in qrdf_list:
            try:
                require_qrdf_actor(db, actor_id=_actor_id(user), qrdf=qrdf)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail="workspace access denied") from exc
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="QRDF does not exist") from exc
        ws_pids = project_ids_for_workspace(db, workspace_id) if body.workspace_id else [project_id]
        wrong_project = [q.id for q in qrdf_list if q.project_id not in ws_pids]
        if wrong_project:
            return {
                "code": 400,
                "message": f"QRDF {wrong_project} 不属于当前数采工作空间",
                "data": None,
            }
    stats = compute_dataset_stats_from_records(qrdf_list)
    stats = enrich_dataset_stats(qrdf_list, stats)
    operator = user.get("email", "system")
    version = next_dataset_version(db, project_id, name)
    snapshot = build_lineage_snapshot(
        qrdf_list,
        operator=operator,
        version=version,
        qrdf_ids=body.qrdf_ids,
        note="initial build",
    )
    lineage = {
        "current_version": version,
        "created_by": operator,
        "data_sources": snapshot["data_sources"],
        "versions": [snapshot],
    }
    ds = Dataset(
        project_id=project_id,
        workspace_id=workspace_id,
        name=name,
        description=body.description,
        qrdf_ids=body.qrdf_ids,
        version=version,
        created_by=operator,
        lineage_json=lineage,
        stats_json=stats,
    )
    db.add(ds)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="dataset already exists in workspace") from exc
    db.refresh(ds)
    return success(
        {
            "id": ds.id,
            "name": ds.name,
            "version": ds.version,
            "created_by": ds.created_by,
            "lineage": ds.lineage_json,
            "stats": ds.stats_json,
        }
    )


def _resolve_filters(body: DatasetQuery) -> dict:
    if body.filters:
        return body.filters.model_dump(exclude_none=True)
    legacy = {}
    if body.project_id:
        legacy["project_id"] = body.project_id
    if body.scene:
        legacy["scene"] = body.scene
    if body.quality:
        legacy["quality"] = body.quality
    if body.keyword:
        legacy["keyword"] = body.keyword
    return legacy


@router.post("/query")
def query_dataset(
    body: DatasetQuery,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    filters = _resolve_filters(body)
    if filters:
        if filters.get("workspace_id"):
            _require_workspace_access(db, user, int(filters["workspace_id"]))
        if filters.get("project_id"):
            _require_project_access(db, user, int(filters["project_id"]))
        data = search_packages(db, filters, body.page, body.size, actor_id=_actor_id(user))
        return success(data)

    q = db.query(Dataset).filter(
        project_access_filter(db, actor_id=_actor_id(user), project_column=Dataset.project_id)
    )
    total = q.count()
    items = (
        q.order_by(Dataset.created_at.desc())
        .offset((body.page - 1) * body.size)
        .limit(body.size)
        .all()
    )
    preview = [
        {
            "id": d.id,
            "name": d.name,
            "project_id": d.project_id,
            "version": d.version,
            "stats": d.stats_json,
            "created_at": format_api_datetime(d.created_at),
        }
        for d in items
    ]
    return success({"total_matched": total, "preview": preview})


def _revision_if_accessible(db: Session, user: dict, revision_id: int) -> DatasetRevision | None:
    revision = db.get(DatasetRevision, revision_id)
    if revision is None:
        return None
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=revision.workspace_id)
    except (PermissionError, ValueError):
        return None
    return revision


@router.post("/revisions", response_model=ApiResponse[DatasetRevisionOut])
def create_dataset_revision_api(
    body: DatasetRevisionCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:write")
    try:
        revision = create_dataset_revision(
            db,
            workspace_id=body.workspace_id,
            name=body.name,
            description=body.description,
            filter_json=body.filter_json,
            items=[item.model_dump() for item in body.items],
            actor_id=_actor_id(user) or 0,
            request_id=str(body.request_id) if body.request_id is not None else None,
        )
        return success(serialize_revision_summary(db, revision.id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except WorkflowConflict as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/revisions", response_model=ApiResponse[DatasetRevisionList])
def list_dataset_revisions_api(
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    _require_workspace_access(db, user, workspace_id)
    revisions = list_dataset_revisions(db, workspace_id=workspace_id)
    return success(
        {"items": [serialize_revision_summary(db, revision.id) for revision in revisions]}
    )


@router.get("/revisions/{revision_id}", response_model=ApiResponse[DatasetRevisionOut])
def get_dataset_revision_api(
    revision_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    if _revision_if_accessible(db, user, revision_id) is None:
        return {"code": 404, "message": "数据集版本不存在", "data": None}
    return success(serialize_revision_summary(db, revision_id))


@router.post("/revisions/{revision_id}/export")
def export_dataset_revision_api(
    revision_id: int,
    body: DatasetRevisionExportCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "export:write")
    if _revision_if_accessible(db, user, revision_id) is None:
        return {"code": 404, "message": "数据集版本不存在", "data": None}
    try:
        require_celery_worker("export")
        job = enqueue_dataset_export(
            db,
            revision_id,
            actor_id=_actor_id(user) or 0,
            export_profile=body.export_profile,
        )
        dispatch = dispatch_media_job(job, worker_prechecked=True)
    except JobDispatchUnavailable as exc:
        return {"code": 503, "message": str(exc), "data": None}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except WorkflowConflict as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return success({"job_id": job.id, "status": job.status, "dispatch": dispatch})


@router.delete("/revisions/{revision_id}", response_model=ApiResponse[DatasetRevisionOut])
def retire_dataset_revision_api(
    revision_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:write")
    if _revision_if_accessible(db, user, revision_id) is None:
        return {"code": 404, "message": "数据集版本不存在", "data": None}
    try:
        revision = retire_dataset_revision(db, revision_id, actor_id=_actor_id(user) or 0)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    return success(serialize_revision_summary(db, revision.id))


@router.get("/list")
def list_datasets(
    project_id: int | None = Query(None),
    workspace_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Dataset entity list (used by management page, distinct from 4.2 multidimensional query)."""
    require_permission(user, "dataset:read")
    q = db.query(Dataset).filter(
        project_access_filter(db, actor_id=_actor_id(user), project_column=Dataset.project_id)
    )
    if workspace_id:
        _require_workspace_access(db, user, workspace_id)
        pids = project_ids_for_workspace(db, workspace_id)
        if pids:
            q = q.filter(Dataset.project_id.in_(pids))
        else:
            q = q.filter(Dataset.id == -1)
    elif project_id:
        _require_project_access(db, user, project_id)
        q = q.filter(Dataset.project_id == project_id)
    total = q.count()
    items = q.order_by(Dataset.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return success(
        {
            "total": total,
            "list": [
                {
                    "id": d.id,
                    "name": d.name,
                    "project_id": d.project_id,
                    "version": d.version,
                    "created_by": d.created_by,
                    "stats": d.stats_json,
                    "created_at": format_api_datetime(d.created_at),
                }
                for d in items
            ],
        }
    )


@router.get("/qrdf/list")
def list_qrdf(
    project_id: int | None = Query(None),
    scene: str | None = Query(None),
    quality: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "qrdf:read")
    q = db.query(QrdfData).filter(qrdf_access_filter(db, actor_id=_actor_id(user)))
    if project_id:
        _require_project_access(db, user, project_id)
        q = q.filter(QrdfData.project_id == project_id)
    if scene:
        q = q.filter(QrdfData.scene == scene)
    if quality:
        q = q.filter(QrdfData.quality_level == quality)
    total = q.count()
    items = q.order_by(QrdfData.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return success(
        {
            "total": total,
            "list": [
                {
                    "id": i.id,
                    "project_id": i.project_id,
                    "name": i.name,
                    "data_source": i.data_source,
                    "scene": i.scene,
                    "quality_level": i.quality_level,
                    "created_at": format_api_datetime(i.created_at),
                }
                for i in items
            ],
        }
    )


@router.get("/export/templates")
def list_export_templates(user: dict = Depends(get_current_user)):
    """LeRobot export template list (generic / Dobot ATOM dedicated)."""
    require_permission(user, "export:read")
    return success(list(LEROBOT_EXPORT_TEMPLATES.values()))


@router.post("/export")
def create_export(
    body: ExportCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "export:write")
    qrdf_ids: list[int] = []
    dataset_id = body.dataset_id

    if body.query_id:
        session = db.get(QuerySession, body.query_id)
        if not session:
            return {"code": 404, "message": "query_id 不存在", "data": None}
        qrdf_ids = list(session.matched_ids or [])
    elif body.dataset_id:
        ds = db.get(Dataset, body.dataset_id)
        if not ds:
            return {"code": 404, "message": "数据集不存在", "data": None}
        qrdf_ids = list(ds.qrdf_ids or [])
    else:
        return {"code": 400, "message": "需提供 query_id 或 dataset_id", "data": None}

    try:
        scope = build_export_authorization_scope(
            db,
            qrdf_ids=qrdf_ids,
            dataset_id=dataset_id,
            actor_id=_actor_id(user),
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="export resource access denied") from None
    except ExportScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        require_celery_worker("export")
    except JobDispatchUnavailable as exc:
        return {"code": 503, "message": str(exc), "data": None}

    params = {**(body.params or {}), "qrdf_ids": qrdf_ids}
    project_id = int(scope["project_ids"][0])
    params["project_id"] = project_id
    job = ExportJob(
        dataset_id=dataset_id or 0,
        project_id=project_id,
        query_id=body.query_id or "",
        format=body.format,
        params_json=params,
        authorization_scope_json=scope,
        status="PENDING",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    mode = dispatch_export(job.id)
    emit_audit_event(
        "export.create",
        actor=str(user.get("email") or user.get("sub") or ""),
        resource=str(job.id),
        detail={
            "format": body.format,
            "dataset_id": dataset_id,
            "query_id": body.query_id,
            "qrdf_count": len(qrdf_ids),
            "dispatch": mode,
        },
    )
    return success(
        {
            "export_id": job.id,
            "task_id": f"export_{job.id}",
            "status": "PENDING",
            "dispatch": mode,
        }
    )


@router.get("/export/list")
def export_history(
    project_id: int | None = Query(None),
    workspace_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "export:read")
    q = db.query(ExportJob)
    if workspace_id:
        pids = project_ids_for_workspace(db, workspace_id)
        from sqlalchemy import or_

        if pids:
            q = q.filter(or_(ExportJob.project_id.in_(pids), ExportJob.project_id.is_(None)))
        else:
            q = q.filter(ExportJob.project_id.is_(None))
    elif project_id:
        from sqlalchemy import or_

        q = q.filter(or_(ExportJob.project_id == project_id, ExportJob.project_id.is_(None)))
    jobs = [
        job
        for job in q.order_by(ExportJob.created_at.desc()).all()
        if _export_is_accessible(db, user, job)
    ]
    total = len(jobs)
    page_jobs = jobs[(page - 1) * size : page * size]
    return success(
        {
            "total": total,
            "list": [_export_item(j, db, signed_actor_id=_actor_id(user)) for j in page_jobs],
        }
    )


@router.get("/export/{export_id}")
def get_export(
    export_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    require_permission(user, "export:read")
    job = db.get(ExportJob, export_id)
    if not job:
        return {"code": 404, "message": "导出任务不存在", "data": None}
    if not _export_is_accessible(db, user, job):
        return {"code": 404, "message": "导出任务不存在", "data": None}
    return success(_export_item(job, db, signed_actor_id=_actor_id(user)))


@router.get("/export/{export_id}/download-url")
def get_export_download_url(
    export_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Describe a direct browser download only after normal export authorization."""
    require_permission(user, "export:read")
    job = db.get(ExportJob, export_id)
    if not job or not _export_is_accessible(db, user, job):
        return {"code": 404, "message": "导出任务不存在", "data": None}
    if job.status not in ("COMPLETED", "completed"):
        return {"code": 400, "message": "导出任务尚未完成", "data": None}

    storage_uri = export_job_storage_display_uri(job)
    file_path = resolve_export_file(job)
    filename = (
        file_path.name
        if file_path
        else Path(storage_uri.rstrip("/")).name or f"export_{job.id}.zip"
    )
    access = issue_browser_download_url(
        storage_uri,
        download_name=filename,
        media_type="application/zip",
    )
    payload = access.as_payload() if access else unavailable_payload("application/zip")
    payload["filename"] = filename
    if access:
        emit_audit_event(
            "file.direct_url",
            actor=str(user.get("email") or ""),
            resource=f"export:{job.id}",
            detail={"delivery": "oss_browser", "kind": "download"},
        )
    return success(payload)


@router.get("/export/{export_id}/download")
def download_export(
    export_id: int,
    sig: str | None = Query(None, description="短时签名令牌（可选，替代 Bearer）"),
    db: Session = Depends(get_db),
    user: dict | None = Depends(get_optional_user),
):
    """Authenticated export file download: Bearer or signed `sig`."""
    actor = None
    actor_id = None
    if sig:
        try:
            payload = verify_signed_download_token(
                sig, resource_type="export", resource_id=export_id
            )
            actor = payload.get("sub")
            actor_id = _actor_id({"sub": actor})
        except ValueError:
            return {"code": 401, "message": "下载签名无效或已过期", "data": None}
    else:
        if not user:
            return {"code": 401, "message": "未登录", "data": None}
        require_permission(user, "export:read")
        actor = user.get("email")
        actor_id = _actor_id(user)

    job = db.get(ExportJob, export_id)
    if not job:
        return {"code": 404, "message": "导出任务不存在", "data": None}
    if not actor_can_access_export_job(db, actor_id=actor_id, export=job):
        return {"code": 404, "message": "导出任务不存在", "data": None}
    if sig:
        max_uses = int(payload.get("max_uses") or 5)
        if not consume_download_jti(str(payload.get("jti") or ""), max_uses):
            return {"code": 403, "message": "下载次数已用尽", "data": None}
    if job.status not in ("COMPLETED", "completed"):
        return {"code": 400, "message": "导出任务尚未完成", "data": None}

    file_path = resolve_export_file(job)
    if not file_path and is_cloud_uri(job.download_url):
        from data.services.cloud_storage import materialize_for_processing

        with materialize_for_processing(job.download_url) as local:
            if local and local.is_file():
                emit_audit_event(
                    "export.download",
                    actor=actor,
                    resource=str(export_id),
                    detail={"via": "signed" if sig else "bearer", "cloud": True},
                )
                return FileResponse(local, filename=local.name, media_type="application/zip")
        return {"code": 404, "message": "导出文件不存在或已被清理", "data": None}
    if not file_path:
        return {"code": 404, "message": "导出文件不存在或已被清理", "data": None}

    if not is_cloud_uri(job.download_url):
        canonical = to_download_uri(str(file_path))
        if canonical and job.download_url != canonical:
            job.download_url = canonical
            db.commit()

    emit_audit_event(
        "export.download",
        actor=actor,
        resource=str(export_id),
        detail={"via": "signed" if sig else "bearer"},
    )
    return FileResponse(file_path, filename=file_path.name, media_type="application/zip")


@router.post("/export/{export_id}/retry")
def retry_export(
    export_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    """One-click retry for failed export tasks."""
    require_permission(user, "export:write")
    job = db.get(ExportJob, export_id)
    if not job:
        return {"code": 404, "message": "导出任务不存在", "data": None}
    if not _export_is_accessible(db, user, job):
        return {"code": 404, "message": "导出任务不存在", "data": None}
    if job.status not in ("FAILED", "failed"):
        return {"code": 400, "message": "仅失败任务可重试", "data": None}
    try:
        require_recoverable_job_kind("export")
    except ResourceFencingRequired:
        emit_audit_event(
            "job.retry.denied",
            actor=str(user.get("email") or ""),
            resource=f"export_job:{job.id}",
            detail={"kind": "export", "reason": "resource_fencing_required"},
            level="warning",
        )
        raise HTTPException(status_code=409, detail=UNFENCED_RETRY_PUBLIC_MESSAGE) from None
    try:
        require_celery_worker("export")
    except JobDispatchUnavailable as exc:
        return {"code": 503, "message": str(exc), "data": None}
    cleanup_export_staging(export_id)
    job.status = "PENDING"
    job.progress = 0
    job.error_message = ""
    job.download_url = ""
    db.commit()
    mode = dispatch_export(job.id)
    return success({"task_id": f"export_{job.id}", "status": "PENDING", "dispatch": mode})


@router.get("/{dataset_id}")
def get_dataset(
    dataset_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    """Dataset details (including version lineage and scene/topic distribution)."""
    require_permission(user, "dataset:read")
    ds = _dataset_if_accessible(db, user, dataset_id)
    if not ds:
        return {"code": 404, "message": "数据集不存在", "data": None}
    qrdf_items = _dataset_qrdf_items_if_accessible(db, user, ds)
    if qrdf_items is None:
        return {"code": 404, "message": "数据集不存在", "data": None}
    return success(build_dataset_detail(ds, qrdf_items))


@router.get("/{dataset_id}/versions")
def list_dataset_versions(
    dataset_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    """Dataset version history and lineage snapshot (P1)."""
    require_permission(user, "dataset:read")
    ds = _dataset_if_accessible(db, user, dataset_id)
    if not ds:
        return {"code": 404, "message": "数据集不存在", "data": None}
    if _dataset_qrdf_items_if_accessible(db, user, ds) is None:
        return {"code": 404, "message": "数据集不存在", "data": None}
    lineage = ds.lineage_json or {}
    return success(
        {
            "dataset_id": ds.id,
            "name": ds.name,
            "current_version": lineage.get("current_version") or ds.version,
            "created_by": ds.created_by or lineage.get("created_by"),
            "created_at": format_api_datetime(ds.created_at),
            "data_sources": lineage.get("data_sources") or [],
            "versions": lineage.get("versions") or [],
        }
    )


@router.get("/{dataset_id}/lineage")
def get_dataset_lineage(
    dataset_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    """Full dataset lineage chain: QRDF -> Task -> data source (P1)."""
    require_permission(user, "dataset:read")
    ds = _dataset_if_accessible(db, user, dataset_id)
    if not ds:
        return {"code": 404, "message": "数据集不存在", "data": None}
    qrdf_items = _dataset_qrdf_items_if_accessible(db, user, ds)
    if qrdf_items is None:
        return {"code": 404, "message": "数据集不存在", "data": None}
    detail = build_dataset_detail(ds, qrdf_items)
    return success(
        {
            "dataset_id": ds.id,
            "name": ds.name,
            "version": ds.version,
            "lineage": detail.get("lineage"),
            "qrdf_trace": detail.get("qrdf_list"),
            "scene_distribution": (ds.stats_json or {}).get("scene_distribution", {}),
            "topic_distribution": (ds.stats_json or {}).get("topic_distribution", {}),
            "data_source_distribution": (ds.stats_json or {}).get("data_source_distribution", {}),
        }
    )
