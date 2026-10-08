"""QRDF data management API (Technical Design 4.6 / Pipeline 4)."""

import logging
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from data.database import QrdfData, Task, get_db
from data.integrations.qrdf import (
    get_dataset_summary,
    list_dataset_files,
    list_topic_stats,
    validate_dataset,
)
from data.integrations.qrdf import (
    list_episodes as qrdf_list_episodes,
)
from data.integrations.qrdf.frame_preview import get_episode_detail, sample_episode_frames
from data.integrations.qrdf.metadata_detail import build_qrdf_metadata_detail
from data.integrations.qrdf.service import resolve_dataset_path
from data.integrations.qrdf.topic_preview import (
    get_topic_preview_status,
    list_episode_rgb_topics,
    resolve_topic_preview_file,
)
from data.schemas.common import ApiResponse, PublishedQrdfEpisodeList, QrdfQuery
from data.security.audit import emit_audit_event
from data.security.signed_url import (
    consume_download_jti,
    create_signed_download_token,
    verify_signed_download_token,
)
from data.services.browser_object_access import (
    issue_browser_download_url,
    issue_browser_preview_url,
    unavailable_payload,
)
from data.services.public_metadata import (
    serialize_public_qrdf_detail,
    serialize_public_qrdf_episode_detail,
    serialize_public_qrdf_episodes,
    serialize_public_qrdf_metadata,
    serialize_public_qrdf_quality,
    serialize_public_qrdf_summary,
    serialize_public_qrdf_topics,
)
from data.services.published_episodes import (
    list_published_qrdf_episodes,
    serialize_published_qrdf_episode,
)
from data.services.qrdf_service import compute_qrdf_stats
from data.services.workspace_access import (
    qrdf_access_filter,
    require_project_actor,
    require_qrdf_actor,
    require_workspace_actor,
)
from data.services.workspace_scope import project_ids_for_workspace
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, get_optional_user, require_permission, success
from data.utils.storage_uri import (
    enrich_dataset_files_for_display,
    qrdf_storage_display_uri,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/qrdf", tags=["QRDF数据管理"])


def _actor_id(user: dict | None) -> int | None:
    if not user:
        return None
    try:
        return int(user.get("sub") or user.get("id"))
    except (TypeError, ValueError):
        return None


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


def _qrdf_if_accessible(
    db: Session,
    user: dict | None,
    qrdf_id: int,
    *,
    actor_id: int | None = None,
) -> QrdfData | None:
    item = db.get(QrdfData, qrdf_id)
    if item is None:
        return None
    try:
        require_qrdf_actor(
            db, actor_id=actor_id if actor_id is not None else _actor_id(user), qrdf=item
        )
    except (PermissionError, ValueError):
        return None
    return item


@router.get("/list")
def list_qrdf(
    project_id: int | None = Query(None),
    workspace_id: int | None = Query(None),
    scene: str | None = Query(None),
    quality: str | None = Query(None),
    keyword: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "qrdf:read")
    q = db.query(QrdfData).filter(qrdf_access_filter(db, actor_id=_actor_id(user)))
    if workspace_id:
        _require_workspace_access(db, user, workspace_id)
        pids = project_ids_for_workspace(db, workspace_id)
        if pids:
            q = q.filter(QrdfData.project_id.in_(pids))
        else:
            q = q.filter(QrdfData.id == -1)
    elif project_id:
        _require_project_access(db, user, project_id)
        q = q.filter(QrdfData.project_id == project_id)
    if scene:
        q = q.filter(QrdfData.scene == scene)
    if quality:
        q = q.filter(QrdfData.quality_level == quality)
    if keyword:
        q = q.filter(QrdfData.name.contains(keyword))
    total = q.count()
    items = q.order_by(QrdfData.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return success(
        {
            "total": total,
            "list": [_qrdf_item(i, signed_actor_id=_actor_id(user)) for i in items],
        }
    )


@router.get("/stats")
def qrdf_stats(
    project_id: int | None = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """QRDF inventory statistics: asset count, episode count, storage usage."""
    require_permission(user, "qrdf:read")
    if project_id:
        _require_project_access(db, user, project_id)
    return success(compute_qrdf_stats(db, project_id=project_id, actor_id=_actor_id(user)))


@router.get("/published-episodes", response_model=ApiResponse[PublishedQrdfEpisodeList])
def list_published_episodes(
    workspace_id: int = Query(..., gt=0),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """List immutable published Episodes usable for QRDF browse and Dataset drafts."""
    require_permission(user, "qrdf:read")
    require_permission(user, "workspace:read")
    _require_workspace_access(db, user, workspace_id)
    total, items = list_published_qrdf_episodes(
        db,
        workspace_id=workspace_id,
        page=page,
        size=size,
    )
    return success(
        {
            "total": total,
            "items": [serialize_published_qrdf_episode(episode) for episode in items],
        }
    )


@router.post("/query")
def query_qrdf(
    body: QrdfQuery, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    require_permission(user, "qrdf:read")
    filters = body.model_dump(exclude_none=True, exclude={"page", "size"})
    if filters:
        if filters.get("workspace_id"):
            _require_workspace_access(db, user, int(filters["workspace_id"]))
        if filters.get("project_id"):
            _require_project_access(db, user, int(filters["project_id"]))
        from data.services.query_service import search_packages

        data = search_packages(db, filters, body.page, body.size, actor_id=_actor_id(user))
        return success(data)

    q = db.query(QrdfData).filter(qrdf_access_filter(db, actor_id=_actor_id(user)))
    total = q.count()
    items = (
        q.order_by(QrdfData.created_at.desc())
        .offset((body.page - 1) * body.size)
        .limit(body.size)
        .all()
    )
    return success(
        {"total": total, "list": [_qrdf_item(i, signed_actor_id=_actor_id(user)) for i in items]}
    )


@router.get("/{qrdf_id}")
def get_qrdf(qrdf_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}
    task = db.get(Task, item.task_id)
    metadata = item.metadata_json or {}
    topics = metadata.get("topics") or list_topic_stats(item.storage_path)
    sdk_summary = get_dataset_summary(item.storage_path)
    base = _qrdf_item(item, signed_actor_id=_actor_id(user))
    base["sdk_summary"] = serialize_public_qrdf_summary(sdk_summary)
    if topics:
        base["topics"] = serialize_public_qrdf_topics(topics)
    quality = metadata.get("quality_check") or validate_dataset(item.storage_path)
    base["quality_check"] = serialize_public_qrdf_quality(quality)
    base["metadata_detail"] = serialize_public_qrdf_detail(
        build_qrdf_metadata_detail(
            storage_path=item.storage_path,
            metadata_json=item.metadata_json,
            qrdf_id=item.id,
            task_id=item.task_id,
        )
    )
    return success({**base, "task": {"id": task.id, "status": task.status} if task else None})


@router.get("/{qrdf_id}/episodes")
def get_episodes(
    qrdf_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}
    meta = item.metadata_json or {}
    public_meta = serialize_public_qrdf_metadata(meta)
    episodes = serialize_public_qrdf_episodes(
        qrdf_list_episodes(item.storage_path) or public_meta.get("episodes") or []
    )
    enriched_episodes = []
    for entry in episodes:
        if not isinstance(entry, dict):
            enriched_episodes.append(entry)
            continue
        episode = dict(entry)
        episode_id = str(episode.get("episode_id") or "")
        episode["rgb_topics"] = (
            list_episode_rgb_topics(item.storage_path, episode_id) if episode_id else []
        )
        enriched_episodes.append(episode)
    return success({"qrdf_id": qrdf_id, "episodes": enriched_episodes})


@router.get("/{qrdf_id}/metadata")
def get_qrdf_metadata(
    qrdf_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    """Metadata details: full view of Episode/Topic/QC/tags (P1)."""
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}
    detail = build_qrdf_metadata_detail(
        storage_path=item.storage_path,
        metadata_json=item.metadata_json,
        qrdf_id=item.id,
        task_id=item.task_id,
    )
    return success({"qrdf_id": qrdf_id, **serialize_public_qrdf_detail(detail)})


@router.get("/{qrdf_id}/episodes/{episode_id}/frames")
def get_episode_frames(
    qrdf_id: int,
    episode_id: str,
    topic: str | None = Query(None),
    start_index: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Frame data sample preview: camera JPEG thumbnails / status topic values (P1)."""
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}
    payload = sample_episode_frames(
        item.storage_path,
        episode_id,
        topic=topic,
        start_index=start_index,
        limit=limit,
    )
    if not payload.get("ok"):
        return {"code": 400, "message": payload.get("message", "帧采样失败"), "data": payload}
    return success(payload)


@router.get("/{qrdf_id}/timeseries")
def get_qrdf_timeseries(
    qrdf_id: int,
    episode_id: str | None = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """QRDF EEF / gripper timeseries (P1 multimodal curve)."""
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}

    from data.integrations.qrdf.multimodal_preview import build_multimodal_session

    eid = episode_id
    if not eid:
        from data.integrations.qrdf.service import list_episodes

        eps = list_episodes(item.storage_path) or []
        eid = eps[0].get("episode_id") if eps else None

    payload = build_multimodal_session(item.storage_path, episode_id=eid)
    if not payload.get("ok"):
        return {"code": 400, "message": payload.get("message", "时序抽取失败"), "data": payload}
    return success(payload)


@router.get("/{qrdf_id}/multimodal/session")
def get_qrdf_multimodal_session(
    qrdf_id: int,
    episode_id: str | None = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}

    from data.integrations.qrdf.multimodal_preview import build_multimodal_session
    from data.integrations.qrdf.service import list_episodes

    eid = episode_id
    if not eid:
        eps = list_episodes(item.storage_path) or []
        eid = eps[0].get("episode_id") if eps else None

    payload = build_multimodal_session(item.storage_path, episode_id=eid)
    if not payload.get("ok"):
        return {
            "code": 400,
            "message": payload.get("message", "多模态会话加载失败"),
            "data": payload,
        }
    return success(payload)


@router.get("/{qrdf_id}/multimodal/frame")
def get_qrdf_multimodal_frame(
    qrdf_id: int,
    frame_index: int = Query(0, ge=0),
    episode_id: str | None = Query(None),
    reference_topic: str | None = Query(None),
    kinds: str | None = Query(None, description="编码种类：rgb,depth"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}

    from data.integrations.qrdf.multimodal_preview import (
        get_multimodal_frame,
        lookup_playback_time_sec,
    )
    from data.integrations.qrdf.service import list_episodes

    eid = episode_id
    if not eid:
        eps = list_episodes(item.storage_path) or []
        eid = eps[0].get("episode_id") if eps else None

    payload = get_multimodal_frame(
        item.storage_path,
        episode_id=eid or "episode_000001",
        frame_index=frame_index,
        reference_topic=reference_topic,
        kinds=kinds or "depth",
    )
    if not payload.get("ok"):
        return {"code": 400, "message": payload.get("message", "帧加载失败"), "data": payload}
    time_sec = lookup_playback_time_sec(
        item.storage_path, eid or "episode_000001", frame_index, reference_topic
    )
    if time_sec is not None:
        payload["time_sec"] = time_sec
    return success(payload)


@router.get("/{qrdf_id}/episodes/{episode_id}/metadata")
def get_episode_metadata(
    qrdf_id: int,
    episode_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Single episode metadata and metrics details (P1)."""
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}
    detail = get_episode_detail(item.storage_path, episode_id)
    if not detail.get("ok"):
        return {"code": 404, "message": "Episode 不存在", "data": detail}
    return success(serialize_public_qrdf_episode_detail(detail))


@router.get("/{qrdf_id}/episodes/{episode_id}/preview")
def get_episode_preview(
    qrdf_id: int,
    episode_id: str,
    topic: str = Query(..., min_length=1, max_length=256),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Describe one existing Episode RGB preview without generating media in HTTP."""
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}
    if topic not in set(list_episode_rgb_topics(item.storage_path, episode_id)):
        return {
            "code": 400,
            "message": "requested topic is not an RGB stream for this Episode",
            "data": None,
        }

    encoded_topic = quote(topic, safe="")
    media_api = f"/api/v1/qrdf/{qrdf_id}/episodes/{quote(episode_id, safe='')}/preview/media?topic={encoded_topic}"
    status = get_topic_preview_status(
        item.storage_path,
        topic_alias=topic,
        generate=False,
        episode_id=episode_id,
        media_api=media_api,
    )
    return success(
        {
            "qrdf_id": qrdf_id,
            "episode_id": episode_id,
            "topic": topic,
            "preview_available": bool(status.get("available")),
            "preview_error": str(status.get("error") or ""),
            "preview_api": media_api if status.get("available") else "",
            "media_type": status.get("media_type") or "video/mp4",
        }
    )


@router.get("/{qrdf_id}/episodes/{episode_id}/preview/media")
def get_episode_preview_media(
    qrdf_id: int,
    episode_id: str,
    topic: str = Query(..., min_length=1, max_length=256),
    direct: bool = False,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Serve exactly one authorized RGB preview artifact for an Episode."""
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}
    if topic not in set(list_episode_rgb_topics(item.storage_path, episode_id)):
        return {
            "code": 400,
            "message": "requested topic is not an RGB stream for this Episode",
            "data": None,
        }

    status = get_topic_preview_status(
        item.storage_path,
        topic_alias=topic,
        generate=False,
        episode_id=episode_id,
    )
    if not status.get("available"):
        return {"code": 404, "message": "RGB preview is unavailable", "data": None}
    preview_path, _resolved_topic = resolve_topic_preview_file(
        item.storage_path,
        topic_alias=topic,
        generate=False,
        episode_id=episode_id,
    )
    if preview_path is None or not preview_path.is_file():
        return {"code": 404, "message": "RGB preview is unavailable", "data": None}
    media_type = str(status.get("media_type") or "video/mp4")
    if direct:
        access = issue_browser_preview_url(
            preview_path,
            resource_type="qrdf",
            resource_id=qrdf_id,
            media_type=media_type,
        )
        if access:
            emit_audit_event(
                "file.direct_url",
                actor=str(user.get("email") or ""),
                resource=f"qrdf:{qrdf_id}",
                detail={"delivery": "oss_browser", "kind": "preview"},
            )
            return success(access.as_payload())
        return success(unavailable_payload(media_type))
    emit_audit_event(
        "file.preview",
        actor=user.get("email"),
        resource=str(qrdf_id),
        detail={"asset_type": "qrdf", "episode_id": episode_id, "topic": topic},
    )
    return FileResponse(preview_path, media_type=media_type)


@router.get("/{qrdf_id}/preview")
def preview_qrdf(
    qrdf_id: int,
    episode_id: str | None = Query(None, max_length=128),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}

    status = get_topic_preview_status(
        item.storage_path,
        generate=True,
        episode_id=episode_id,
    )
    meta = item.metadata_json or {}
    public_meta = serialize_public_qrdf_metadata(meta)
    sdk_summary = get_dataset_summary(item.storage_path)
    episodes = serialize_public_qrdf_episodes(
        qrdf_list_episodes(item.storage_path) or public_meta.get("episodes") or []
    )
    preview_episode_id = status.get("episode_id") or episode_id
    episode_query = (
        f"?episode_id={quote(preview_episode_id, safe='')}" if preview_episode_id else ""
    )
    preview_api = (
        f"/api/v1/qrdf/{qrdf_id}/preview/media{episode_query}" if status.get("available") else ""
    )

    display_uri = qrdf_storage_display_uri(item)
    return success(
        {
            "qrdf_id": qrdf_id,
            "name": item.name,
            "data_source": item.data_source,
            "scene": item.scene,
            "storage_path": display_uri,
            "storage_uri": display_uri,
            "preview_available": status.get("available", False),
            "preview_error": status.get("error", ""),
            "preview_api": preview_api,
            "preview_episode_id": preview_episode_id,
            "media_type": status.get("media_type"),
            "quality_level": item.quality_level,
            "metadata": public_meta,
            "sdk_summary": serialize_public_qrdf_summary(sdk_summary),
            "episodes": episodes,
            "files": enrich_dataset_files_for_display(
                display_uri,
                list_dataset_files(item.storage_path),
            ),
        }
    )


@router.get("/{qrdf_id}/preview/media")
def preview_qrdf_media(
    qrdf_id: int,
    episode_id: str | None = Query(None, max_length=128),
    direct: bool = False,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Authenticated preview stream (played on frontend via fetch + Blob to prevent missing token in direct URLs)."""
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}

    status = get_topic_preview_status(
        item.storage_path,
        generate=True,
        episode_id=episode_id,
    )
    if not status["available"]:
        return {"code": 400, "message": status.get("error", "无法预览"), "data": None}

    preview_path, _ = resolve_topic_preview_file(
        item.storage_path,
        topic_alias="head_color",
        generate=False,
        episode_id=episode_id,
    )
    preview_path = preview_path or status.get("path")

    p = Path(preview_path) if preview_path else None
    if not p or not p.is_file():
        return {"code": 400, "message": "预览文件不存在或数据格式不支持", "data": None}

    media_type = str(status.get("media_type") or "video/mp4")
    if direct:
        access = issue_browser_preview_url(
            p,
            resource_type="qrdf",
            resource_id=qrdf_id,
            media_type=media_type,
        )
        if access:
            emit_audit_event(
                "file.direct_url",
                actor=str(user.get("email") or ""),
                resource=f"qrdf:{qrdf_id}",
                detail={"delivery": "oss_browser", "kind": "preview"},
            )
            return success(access.as_payload())
        return success(unavailable_payload(media_type))
    return FileResponse(p, media_type=media_type)


def _safe_zip_name(name: str, fallback: str) -> str:
    safe = re.sub(r"[^\w\-.]+", "_", name or "").strip("._") or fallback
    return safe[:80]


@router.get("/{qrdf_id}/download-url")
def get_qrdf_download_url(
    qrdf_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return a public OSS URL only for an authorized, single-object QRDF ZIP."""
    require_permission(user, "qrdf:read")
    item = _qrdf_if_accessible(db, user, qrdf_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}
    filename = f"{_safe_zip_name(item.name, f'qrdf_{qrdf_id}')}.zip"
    storage_uri = qrdf_storage_display_uri(item)
    access = None
    if storage_uri.lower().split("?", 1)[0].endswith(".zip"):
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
            resource=f"qrdf:{qrdf_id}",
            detail={"delivery": "oss_browser", "kind": "download"},
        )
    return success(payload)


@router.get("/{qrdf_id}/download")
def download_qrdf(
    qrdf_id: int,
    background_tasks: BackgroundTasks,
    sig: str | None = Query(None, description="短时签名令牌（可选，替代 Bearer）"),
    db: Session = Depends(get_db),
    user: dict | None = Depends(get_optional_user),
):
    """Pack the QRDF dataset directory into a zip for download: Bearer or signed ``sig``."""
    actor = None
    actor_id = None
    if sig:
        try:
            payload = verify_signed_download_token(sig, resource_type="qrdf", resource_id=qrdf_id)
            actor = payload.get("sub")
            actor_id = _actor_id({"sub": actor})
        except ValueError:
            return {"code": 401, "message": "下载签名无效或已过期", "data": None}
    else:
        if not user:
            return {"code": 401, "message": "未登录", "data": None}
        require_permission(user, "qrdf:read")
        actor = user.get("email")
        actor_id = _actor_id(user)

    item = _qrdf_if_accessible(db, user, qrdf_id, actor_id=actor_id)
    if not item:
        return {"code": 404, "message": "QRDF 数据不存在", "data": None}
    if sig:
        max_uses = int(payload.get("max_uses") or 5)
        if not consume_download_jti(str(payload.get("jti") or ""), max_uses):
            return {"code": 403, "message": "下载次数已用尽", "data": None}
    dataset_path = resolve_dataset_path(item.storage_path)
    if not dataset_path or not dataset_path.is_dir():
        return {"code": 404, "message": "数据集目录不存在", "data": None}

    tmp_dir = Path(tempfile.mkdtemp(prefix="qrdf_dl_"))
    zip_base = tmp_dir / f"qrdf_{qrdf_id}"
    try:
        shutil.make_archive(str(zip_base), "zip", dataset_path)
    except OSError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.warning(
            "qrdf archive failed qrdf_id=%s error_type=%s",
            qrdf_id,
            type(exc).__name__,
        )
        return {"code": 500, "message": "打包失败", "data": None}

    zip_path = zip_base.with_suffix(".zip")
    filename = f"{_safe_zip_name(item.name, f'qrdf_{qrdf_id}')}.zip"
    background_tasks.add_task(shutil.rmtree, tmp_dir, True)
    emit_audit_event(
        "file.download",
        actor=actor,
        resource=f"qrdf:{qrdf_id}",
        detail={"via": "signed" if sig else "bearer"},
    )
    return FileResponse(zip_path, filename=filename, media_type="application/zip")


def _qrdf_item(i: QrdfData, *, signed_actor_id: int | None = None) -> dict:
    storage_uri = "" if i.data_source == "episode_publication" else qrdf_storage_display_uri(i)
    signed_download_url = ""
    if signed_actor_id is not None:
        try:
            sig = create_signed_download_token(
                resource_type="qrdf",
                resource_id=i.id,
                subject=str(signed_actor_id),
                ttl_seconds=600,
                max_uses=5,
            )
            signed_download_url = f"/api/v1/qrdf/{i.id}/download?sig={sig}"
        except Exception:
            signed_download_url = ""
    return {
        "id": i.id,
        "task_id": i.task_id,
        "project_id": i.project_id,
        "name": i.name,
        "data_source": i.data_source,
        "scene": i.scene,
        "quality_level": i.quality_level,
        "storage_path": storage_uri,
        "storage_uri": storage_uri,
        "download_api": f"/api/v1/qrdf/{i.id}/download",
        "signed_download_url": signed_download_url,
        "metadata": serialize_public_qrdf_metadata(i.metadata_json),
        "created_at": format_api_datetime(i.created_at),
    }
