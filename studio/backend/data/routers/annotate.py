"""Data annotation module P0 endpoints (Scheme 2)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from data.database import Project, Task, get_db
from data.integrations.qrdf.topic_preview import (
    get_topic_preview_status,
    load_topic_preview_timeline,
    resolve_topic_preview_file,
)
from data.schemas.annotate import (
    AnnotateSubmitRequest,
    BehaviorTagCreateRequest,
    BehaviorTagUpdateRequest,
    RegionFrameIdRequest,
)
from data.schemas.common import TaskClaim
from data.security.audit import emit_audit_event
from data.services.annotate_service import (
    AnnotationSubmissionConflict,
    allocate_region_frame_id,
    build_workbench_data,
    find_next_annotate_task,
    get_annotate_stats,
    get_total_frames,
    record_claim_started,
    register_rf_id,
    release_annotate_task,
    resolve_scene,
    resolve_task_dataset_storage,
    resolve_task_fps,
    submit_annotations,
    task_quality_level,
    update_rf_id,
)
from data.services.behavior_tags_service import (
    create_tag,
    delete_tag,
    get_active_vocabulary_snapshot,
    list_active_tags,
    update_tag,
)
from data.services.browser_object_access import issue_browser_preview_url, unavailable_payload
from data.services.database_errors import is_concurrency_operational_error
from data.services.task_lock import task_lock_service
from data.services.task_pipeline import pipeline_key_for_task
from data.services.workspace_access import require_task_actor
from data.services.workspace_scope import resolve_task_workspace_id
from data.utils.formatting import format_api_datetime, normalize_api_fps
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(tags=["数据标注"])
logger = logging.getLogger(__name__)


def _require_annotate_read(user: dict) -> None:
    role = user.get("role", "viewer")
    from data.config import get_runtime_config

    rbac = get_runtime_config()["rbac"]
    perms: list[str] = rbac["roles"].get(role, rbac["roles"]["viewer"]).get("permissions", [])
    if "*" in perms:
        return
    if (
        "annotate:read" in perms
        or "annotate:*" in perms
        or "audit:read" in perms
        or "audit:*" in perms
    ):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")


def _user_id(user: dict) -> str:
    return str(user.get("id") or user.get("sub") or user.get("email") or "")


def _annotator_id(user: dict) -> int | None:
    raw = user.get("sub") or user.get("id")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _require_annotate_task_scope(db: Session, user: dict, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    try:
        require_task_actor(db, actor_id=_annotator_id(user), task=task)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return task


def _task_list_item(db: Session, task: Task) -> dict:
    meta = task.metadata_json or {}
    project = db.get(Project, task.project_id)
    storage = resolve_task_dataset_storage(task)
    episodes_meta = meta.get("episodes") or (meta.get("preprocess") or {}).get("episodes") or []
    total_frames = 0
    if episodes_meta:
        ep = episodes_meta[0]
        if ep.get("frame_count"):
            total_frames = int(ep["frame_count"])
        elif ep.get("end_frame") is not None:
            total_frames = int(ep["end_frame"]) + 1
    if total_frames <= 0:
        total_frames = get_total_frames(task, storage)
    pre = meta.get("preprocess") or {}
    if pre.get("fps"):
        fps = normalize_api_fps(pre["fps"])
    else:
        fps = resolve_task_fps(storage, meta)
    duration_sec = round(total_frames / fps, 2) if fps > 0 and total_frames > 0 else 0
    return {
        "id": task.id,
        "workspace_id": resolve_task_workspace_id(db, task),
        "name": task.name,
        "pipeline_key": pipeline_key_for_task(task),
        "scene": resolve_scene(task, project, meta),
        "status": task.status,
        "quality_level": task_quality_level(meta),
        "total_frames": total_frames,
        "duration_sec": duration_sec,
        "claimed_by": task.claimed_by or None,
        "created_at": format_api_datetime(task.created_at),
    }


@router.get("/annotate/tasks")
def list_annotate_tasks(
    status: str = Query("pending_annotate"),
    scene: str | None = Query(None),
    quality_level: str | None = Query(None, description="质检等级筛选"),
    mine: bool = Query(False),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "annotate:read")
    q = db.query(Task).filter(Task.status == status)
    if scene:
        q = q.join(Project).filter(Project.scene == scene)
    if mine:
        q = q.filter(Task.claimed_by == _user_id(user))

    if quality_level:
        all_tasks = q.order_by(Task.created_at.desc()).all()
        filtered = [t for t in all_tasks if task_quality_level(t.metadata_json) == quality_level]
        total = len(filtered)
        start = (page - 1) * size
        tasks = filtered[start : start + size]
    else:
        total = q.count()
        tasks = q.order_by(Task.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return success({"items": [_task_list_item(db, t) for t in tasks], "total": total})


@router.get("/annotate/stats")
def annotate_stats(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Annotation progress: backlog, personal completed count today, and personal claimed task count."""
    require_permission(user, "annotate:read")
    stats = get_annotate_stats(db, user_key=_user_id(user), annotator_id=_annotator_id(user))
    return success(stats)


@router.get("/annotate/queue/next")
def annotate_queue_next(
    exclude_task_id: int | None = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "annotate:read")
    nxt = find_next_annotate_task(db, exclude_task_id=exclude_task_id)
    if not nxt:
        return success({"task_id": None})
    return success({"task_id": nxt.id, "scene": _task_list_item(db, nxt)["scene"]})


@router.post("/annotate/{task_id}/claim")
def claim_annotate_task(
    task_id: int,
    body: TaskClaim,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "annotate:write")
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}
    if task.status != "pending_annotate":
        return {"code": 400, "message": "仅待标注任务可领取", "data": None}
    user_id = body.user_id or _user_id(user)
    try:
        result = task_lock_service.claim(db, task, user_id, body.lock_expire)
    except ValueError as e:
        return {"code": 409, "message": str(e), "data": None}
    task.claimed_user_id = user_id
    record_claim_started(task, db)
    db.commit()
    return success(result)


@router.post("/annotate/{task_id}/release")
def release_annotate_task_endpoint(
    task_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Release claimed annotation task (cleans up region_frame session if not submitted)."""
    require_permission(user, "annotate:write")
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}
    if task.status != "pending_annotate":
        return {"code": 400, "message": "仅待标注任务可释放", "data": None}
    user_id = _user_id(user)
    try:
        release_annotate_task(db, task, user_id)
    except ValueError as e:
        return {"code": 403, "message": str(e), "data": None}
    return success({"released": True, "task_id": task_id})


@router.get("/annotate/{task_id}/data")
def get_annotate_data(
    task_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}
    _require_annotate_read(user)
    vocabulary = get_active_vocabulary_snapshot(db)
    return success(
        build_workbench_data(
            db,
            task,
            vocabulary=vocabulary,
            actor_id=_annotator_id(user),
        )
    )


def _require_admin(user: dict) -> None:
    if user.get("role") == "admin":
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")


@router.get("/annotate/tags")
def list_behavior_tags(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    require_permission(user, "annotate:read")
    items = list_active_tags(db)
    return success({"items": items})


@router.post("/annotate/tags")
def create_behavior_tag(
    body: BehaviorTagCreateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_admin(user)
    try:
        item = create_tag(
            db,
            tag_key=body.resolved_key(),
            tag_label=body.resolved_label(),
            color=body.color,
            sort_order=body.sort_order,
        )
    except ValueError as e:
        return {"code": 400, "message": str(e), "data": None}
    return success(item)


@router.put("/annotate/tags/{tag_key}")
def update_behavior_tag(
    tag_key: str,
    body: BehaviorTagUpdateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_admin(user)
    try:
        item = update_tag(
            db,
            tag_key,
            tag_label=body.resolved_label(),
            color=body.color,
            sort_order=body.sort_order,
            is_active=body.is_active,
        )
    except ValueError as e:
        return {"code": 400, "message": str(e), "data": None}
    return success(item)


@router.delete("/annotate/tags/{tag_key}")
def delete_behavior_tag(
    tag_key: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_admin(user)
    try:
        item = delete_tag(db, tag_key)
    except ValueError as e:
        return {"code": 400, "message": str(e), "data": None}
    return success(item)


@router.get("/tags/{category}")
def list_tags_by_category(
    category: str, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    require_permission(user, "annotate:read")
    if category in ("behavior", "annotate", "default"):
        return list_behavior_tags(db=db, user=user)
    return success({"items": []})


@router.get("/annotate/{task_id}/preview/media")
def annotate_preview_media(
    task_id: int,
    topic: str = Query("head_color"),
    direct: bool = False,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_annotate_read(user)
    _require_annotate_task_scope(db, user, task_id)
    return _serve_preview_media(
        task_id, topic, db, direct=direct, actor=str(user.get("email") or "")
    )


@router.get("/annotate/{task_id}/preview")
def annotate_preview_descriptor(
    task_id: int,
    topic: str = Query("head_color"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_annotate_read(user)
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}
    status_data = _preview_status(task, topic, db, generate=True)
    if not status_data.get("available"):
        return {"code": 400, "message": status_data.get("error", "无法预览"), "data": None}
    return success({key: value for key, value in status_data.items() if key != "path"})


@router.get("/annotate/{task_id}/preview/timeline")
def annotate_preview_timeline(
    task_id: int,
    topic: str = Query("head_color"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_annotate_read(user)
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}
    status_data = _preview_status(task, topic, db, generate=True)
    if status_data.get("legacy"):
        return {"code": 400, "message": "历史预览没有精确时间轴", "data": None}
    if not status_data.get("available"):
        return {"code": 400, "message": status_data.get("error", "无法预览"), "data": None}
    try:
        timeline = load_topic_preview_timeline(
            resolve_task_dataset_storage(task),
            topic_alias=topic,
        )
    except ValueError as exc:
        return {"code": 400, "message": str(exc), "data": None}
    return success(timeline)


@router.get("/annotate/{task_id}/{topic}/preview/media")
def annotate_preview_media_by_path(
    task_id: int,
    topic: str,
    direct: bool = False,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_annotate_read(user)
    _require_annotate_task_scope(db, user, task_id)
    return _serve_preview_media(
        task_id, topic, db, direct=direct, actor=str(user.get("email") or "")
    )


def _serve_preview_media(
    task_id: int,
    topic: str,
    db: Session,
    *,
    direct: bool = False,
    actor: str = "",
):
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}

    status_data = _preview_status(task, topic, db, generate=True)
    if not status_data.get("available"):
        return {"code": 400, "message": status_data.get("error", "无法预览"), "data": None}

    from pathlib import Path

    preview_path, _ = resolve_topic_preview_file(
        resolve_task_dataset_storage(task),
        topic_alias=topic,
        generate=False,
        task_id=task_id,
    )
    p = Path(preview_path) if preview_path else None
    if not p or not p.is_file():
        p = Path(status_data["path"])
    if not p.is_file():
        return {"code": 400, "message": "预览文件不存在", "data": None}

    media_type = str(status_data.get("media_type") or "video/mp4")
    if direct:
        access = issue_browser_preview_url(
            p,
            resource_type="task",
            resource_id=task_id,
            media_type=media_type,
        )
        if access:
            emit_audit_event(
                "file.direct_url",
                actor=actor,
                resource=f"task:{task_id}",
                detail={"delivery": "oss_browser", "kind": "preview"},
            )
            return success(access.as_payload())
        return success(unavailable_payload(media_type))
    return FileResponse(p, media_type=media_type)


def _preview_status(task: Task, topic: str, db: Session, *, generate: bool) -> dict:
    del db
    query = f"?topic={topic}"
    return get_topic_preview_status(
        resolve_task_dataset_storage(task),
        topic_alias=topic,
        generate=generate,
        task_id=task.id,
        media_api=f"/api/v1/annotate/{task.id}/preview/media{query}",
        timeline_api=f"/api/v1/annotate/{task.id}/preview/timeline{query}",
    )


@router.post("/annotate/{task_id}/region-frames/id")
def allocate_region_frame(
    task_id: int,
    body: RegionFrameIdRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "annotate:write")
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}
    if task.status != "pending_annotate":
        return {"code": 400, "message": "任务不在待标注状态", "data": None}

    user_id = _user_id(user)
    try:
        task_lock_service.require_claim(task, user_id)
    except ValueError as e:
        return {"code": 403, "message": str(e), "data": None}

    if body.start_frame > body.end_frame:
        return {"code": 400, "message": "起止帧非法", "data": None}

    storage = resolve_task_dataset_storage(task)
    total = get_total_frames(task, storage)
    if total > 0 and (body.start_frame < 0 or body.end_frame >= total):
        return {"code": 400, "message": f"帧号超出范围 [0, {total - 1}]", "data": None}

    rf_id = allocate_region_frame_id()
    register_rf_id(task, db, rf_id, body.start_frame, body.end_frame)
    return success({"id": rf_id})


@router.put("/annotate/{task_id}/region-frames/{rf_id}")
def update_region_frame_session(
    task_id: int,
    rf_id: str,
    body: RegionFrameIdRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Synchronize region_frame session frame range after drag/resize."""
    require_permission(user, "annotate:write")
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}
    if task.status != "pending_annotate":
        return {"code": 400, "message": "任务不在待标注状态", "data": None}

    user_id = _user_id(user)
    try:
        task_lock_service.require_claim(task, user_id)
    except ValueError as e:
        return {"code": 403, "message": str(e), "data": None}

    if body.start_frame > body.end_frame:
        return {"code": 400, "message": "起止帧非法", "data": None}

    storage = resolve_task_dataset_storage(task)
    total = get_total_frames(task, storage)
    if total > 0 and (body.start_frame < 0 or body.end_frame >= total):
        return {"code": 400, "message": f"帧号超出范围 [0, {total - 1}]", "data": None}

    try:
        update_rf_id(task, db, rf_id, body.start_frame, body.end_frame)
    except ValueError as e:
        return {"code": 400, "message": str(e), "data": None}
    return success({"id": rf_id, "start_frame": body.start_frame, "end_frame": body.end_frame})


@router.post("/annotate/{task_id}/submit")
def submit_annotate(
    task_id: int,
    body: AnnotateSubmitRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "annotate:write")
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}

    if body.task_id is not None and body.task_id != task_id:
        return {"code": 400, "message": "task_id 与路径不一致", "data": None}

    user_id = _user_id(user)
    try:
        task_lock_service.require_claim(task, user_id)
    except ValueError as e:
        return {"code": 403, "message": str(e), "data": None}

    try:
        result = submit_annotations(
            db,
            task,
            body,
            operator=user.get("email", user_id),
            annotator_id=_annotator_id(user),
            claimant_id=user_id,
        )
    except AnnotationSubmissionConflict as e:
        db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"code": 409, "message": str(e), "data": None},
        )
    except OperationalError as e:
        db.rollback()
        is_conflict = is_concurrency_operational_error(db, e)
        logger.error(
            "annotation submit database error task_id=%s error_type=%s concurrency=%s",
            task_id,
            type(e).__name__,
            is_conflict,
        )
        response_status = (
            status.HTTP_409_CONFLICT if is_conflict else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return JSONResponse(
            status_code=response_status,
            content={
                "code": response_status,
                "message": (
                    "task annotation state changed concurrently; retry"
                    if is_conflict
                    else "database service is unavailable"
                ),
                "data": None,
            },
        )
    except ValueError as e:
        db.rollback()
        return {"code": 400, "message": str(e), "data": None}

    return success(result)


# --- P1 reserved routes ---


def _feature_disabled(feature: str) -> dict:
    return {
        "code": 501,
        "message": "FEATURE_NOT_ENABLED",
        "data": {"feature": feature, "enabled": False},
    }


@router.get("/annotate/{task_id}/timeseries")
def p1_timeseries(
    task_id: int,
    episode_id: str | None = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    _require_annotate_read(user)
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}

    from data.integrations.qrdf.timeseries import extract_episode_timeseries

    storage = resolve_task_dataset_storage(task)
    meta = task.metadata_json or {}
    episodes = meta.get("episodes") or (meta.get("preprocess") or {}).get("episodes") or []
    eid = episode_id or (episodes[0].get("episode_id") if episodes else None)

    payload = extract_episode_timeseries(storage, episode_id=eid)
    if not payload.get("ok"):
        return {"code": 400, "message": payload.get("message", "时序抽取失败"), "data": payload}
    return success(payload)


@router.get("/annotate/{task_id}/multimodal/meta")
def annotate_multimodal_meta(
    task_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Lightweight metadata (clips/tags) for multimodal page, without building full workbench data."""
    _require_annotate_read(user)
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}

    from data.integrations.qrdf.topic_preview import list_available_streams
    from data.services.annotate_schema import clip_descriptions_from_doc
    from data.services.annotate_service import (
        load_annotation_doc,
        resolve_episode_task_language,
        resolve_scene,
        resolve_task_dataset_storage,
        resolve_task_fps,
        to_scheme2_annotations,
    )

    meta = task.metadata_json or {}
    project = db.get(Project, task.project_id)
    storage = resolve_task_dataset_storage(task)
    fps = resolve_task_fps(storage, meta)
    vocabulary = get_active_vocabulary_snapshot(db)
    annotation_doc = load_annotation_doc(db, task.id)
    clip_descriptions = clip_descriptions_from_doc(
        annotation_doc,
        action_by_key=vocabulary.action_by_key,
        label_by_key=vocabulary.label_by_key,
        label_to_key=vocabulary.label_to_key,
    )
    episodes = meta.get("episodes") or (meta.get("preprocess") or {}).get("episodes") or []

    return success(
        {
            "task_id": task.id,
            "scene": resolve_scene(task, project, meta),
            "fps": fps,
            "task": resolve_episode_task_language(task, meta, annotation_doc),
            "tags": vocabulary.action_options(),
            "annotations": to_scheme2_annotations(
                clip_descriptions=clip_descriptions,
                vocabulary=vocabulary,
            ),
            "episodes": episodes,
            "episode_id": episodes[0].get("episode_id") if episodes else "episode_000001",
            "preview_streams": list_available_streams(storage),
        }
    )


@router.get("/annotate/{task_id}/multimodal/session")
def annotate_multimodal_session(
    task_id: int,
    episode_id: str | None = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Multimodal inspection session: EEF timeline + camera streams + curve metadata."""
    _require_annotate_read(user)
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}

    from data.integrations.qrdf.multimodal_preview import build_multimodal_session

    storage = resolve_task_dataset_storage(task)
    meta = task.metadata_json or {}
    episodes = meta.get("episodes") or (meta.get("preprocess") or {}).get("episodes") or []
    eid = episode_id or (episodes[0].get("episode_id") if episodes else None)
    payload = build_multimodal_session(storage, episode_id=eid)
    if not payload.get("ok"):
        return {
            "code": 400,
            "message": payload.get("message", "多模态会话加载失败"),
            "data": payload,
        }
    return success(payload)


@router.get("/annotate/{task_id}/multimodal/frame")
def annotate_multimodal_frame(
    task_id: int,
    frame_index: int = Query(0, ge=0),
    episode_id: str | None = Query(None),
    reference_topic: str | None = Query(None),
    kinds: str | None = Query(None, description="编码种类：rgb,depth"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Retrieve frame image by reference timeline index (depth only by default; reuse annotation preview MP4 for RGB)."""
    _require_annotate_read(user)
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}

    from data.integrations.qrdf.multimodal_preview import (
        get_multimodal_frame,
        lookup_playback_time_sec,
    )

    storage = resolve_task_dataset_storage(task)
    meta = task.metadata_json or {}
    episodes = meta.get("episodes") or (meta.get("preprocess") or {}).get("episodes") or []
    eid = episode_id or (episodes[0].get("episode_id") if episodes else None)

    payload = get_multimodal_frame(
        storage,
        episode_id=eid or "episode_000001",
        frame_index=frame_index,
        reference_topic=reference_topic,
        kinds=kinds or "depth",
    )
    if not payload.get("ok"):
        return {"code": 400, "message": payload.get("message", "帧加载失败"), "data": payload}
    time_sec = lookup_playback_time_sec(
        storage, eid or "episode_000001", frame_index, reference_topic
    )
    if time_sec is not None:
        payload["time_sec"] = time_sec
    return success(payload)


@router.get("/annotate/{task_id}/episodes/{episode_id}/frames")
def annotate_episode_frames(
    task_id: int,
    episode_id: str,
    topic: str | None = Query(None),
    start_index: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Frame sampling for annotation task episode (depth maps, etc.)."""
    _require_annotate_read(user)
    task = db.get(Task, task_id)
    if not task:
        return {"code": 404, "message": "任务不存在", "data": None}

    from data.integrations.qrdf.frame_preview import sample_episode_frames

    storage = resolve_task_dataset_storage(task)
    payload = sample_episode_frames(
        storage,
        episode_id,
        topic=topic,
        start_index=start_index,
        limit=limit,
    )
    if not payload.get("ok"):
        return {"code": 400, "message": payload.get("message", "帧采样失败"), "data": payload}
    return success(payload)


@router.get("/annotate/records")
def p1_annotate_records(user: dict = Depends(get_current_user)):
    require_permission(user, "annotate:read")
    return _feature_disabled("annotate_records")


@router.get("/annotate/records/{task_id}")
def p1_annotate_record_detail(task_id: int, user: dict = Depends(get_current_user)):
    _require_annotate_read(user)
    return _feature_disabled("annotate_records")
