"""Task-level preview resources for audit module (based on task storage_path, independent of qrdf_id)."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from data.database import Task
from data.integrations.qrdf.frame_preview import get_episode_detail, sample_episode_frames
from data.integrations.qrdf.metadata_detail import build_qrdf_metadata_detail
from data.integrations.qrdf.service import list_episodes, resolve_dataset_path
from data.integrations.qrdf.topic_preview import (
    get_topic_preview_status,
    resolve_topic_preview_file,
)
from data.services.annotate_service import resolve_task_dataset_storage
from data.services.browser_object_access import issue_browser_preview_url, unavailable_payload
from data.services.preprocess_facts import sanitize_episode_facts, sanitize_topic_facts
from data.utils.helpers import success

logger = logging.getLogger(__name__)


def resolve_audit_task_storage(task: Task) -> str | None:
    storage = resolve_task_dataset_storage(task)
    if storage:
        return storage
    raw = (task.storage_path or "").strip()
    if not raw:
        return None
    resolved = resolve_dataset_path(raw)
    return str(resolved) if resolved else raw


def default_episode_id(task: Task, storage_path: str | None) -> str:
    meta = task.metadata_json or {}
    episodes = meta.get("episodes") or (meta.get("preprocess") or {}).get("episodes") or []
    if episodes:
        return str(episodes[0].get("episode_id") or "episode_000001")
    if storage_path:
        listed = list_episodes(storage_path)
        if listed:
            return str(listed[0].get("episode_id") or "episode_000001")
    return "episode_000001"


def build_audit_metadata_detail(task: Task, *, db: Session | None = None) -> dict:
    storage = resolve_audit_task_storage(task)
    result = build_qrdf_metadata_detail(
        storage_path=storage,
        metadata_json=task.metadata_json or {},
        task_id=task.id,
    )
    if db is not None:
        _writeback_meta_cache(task, result, db)
    return result


def _writeback_meta_cache(task: Task, detail: dict, db: Session) -> None:
    """Persist sanitized episode/topic facts after a QRDF SDK read."""
    task_id = task.id
    try:
        metadata = dict(task.metadata_json or {})
        changed = False

        episode_source = metadata.get("episodes") or detail.get("episodes")
        safe_episodes = (
            sanitize_episode_facts(episode_source) if episode_source is not None else None
        )
        if safe_episodes is not None and metadata.get("episodes") != safe_episodes:
            metadata["episodes"] = safe_episodes
            changed = True

        topic_source = metadata.get("topics") or detail.get("topics")
        safe_topics = sanitize_topic_facts(topic_source) if topic_source is not None else None
        if safe_topics is not None and metadata.get("topics") != safe_topics:
            metadata["topics"] = safe_topics
            changed = True

        if changed:
            task.metadata_json = metadata
            db.commit()
    except Exception as error:
        try:
            db.rollback()
        except Exception as rollback_error:
            logger.error(
                "audit metadata cache rollback failed code=%s task_id=%s error_type=%s",
                "audit_cache_rollback_failed",
                task_id,
                type(rollback_error).__name__,
            )
        logger.error(
            "audit metadata cache writeback failed code=%s task_id=%s error_type=%s",
            "audit_cache_writeback_failed",
            task_id,
            type(error).__name__,
        )


def serve_audit_preview_media(
    task: Task, topic: str, *, direct: bool = False
) -> FileResponse | dict:
    storage = resolve_audit_task_storage(task)
    if not storage:
        return {"code": 400, "message": "任务无可用数据集路径", "data": None}

    status = get_topic_preview_status(storage, topic_alias=topic, generate=True, task_id=task.id)
    if not status.get("available"):
        return {"code": 400, "message": status.get("error", "无法预览"), "data": None}

    preview_path, _ = resolve_topic_preview_file(
        storage, topic_alias=topic, generate=False, task_id=task.id
    )
    path = Path(preview_path) if preview_path else None
    if not path or not path.is_file():
        path = Path(status["path"])
    if not path.is_file():
        return {"code": 400, "message": "预览文件不存在", "data": None}

    media_type = str(status.get("media_type") or "video/mp4")
    if direct:
        access = issue_browser_preview_url(
            path,
            resource_type="task",
            resource_id=task.id,
            media_type=media_type,
        )
        return success(access.as_payload() if access else unavailable_payload(media_type))
    return FileResponse(path, media_type=media_type)


def sample_audit_episode_frames(
    task: Task,
    episode_id: str,
    *,
    topic: str | None = None,
    start_index: int = 0,
    limit: int = 10,
) -> dict:
    storage = resolve_audit_task_storage(task)
    if not storage:
        return {"ok": False, "message": "任务无可用数据集路径", "frames": []}
    return sample_episode_frames(
        storage,
        episode_id,
        topic=topic,
        start_index=start_index,
        limit=limit,
    )


def get_audit_episode_metadata(task: Task, episode_id: str) -> dict:
    storage = resolve_audit_task_storage(task)
    if not storage:
        return {"ok": False, "message": "任务无可用数据集路径", "episode_id": episode_id}
    return get_episode_detail(storage, episode_id)
