"""Batch / Episode work queue projections and explicit human state changes."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import Float, case, cast, func, or_
from sqlalchemy.orm import Session, selectinload

from data.database import Episode, EpisodeArtifact, JobRun, TaskSet, User, WorkItem
from data.services.derived_preview_batch import (
    latest_preview_job_for_episode,
    latest_preview_jobs_for_episodes,
)
from data.services.episode_workbench import (
    publication_projection,
    review_draft,
    submit_draft,
    work_item_available_actions,
)
from data.services.task_labels import task_label_brief
from data.services.workspace_access import require_workspace_actor

WORK_QUEUE_STAGES = frozenset({"cut", "annotation", "review", "completed"})
QUALITY_READY_STATUSES = frozenset({"passed", "recovered", "profiled"})
WORKER_ROLES = {
    "cut": frozenset({"admin", "annotator"}),
    "annotation": frozenset({"admin", "annotator"}),
    "review": frozenset({"admin", "auditor"}),
}
PREVIEW_PROCESSING_STATUSES = frozenset({"queued", "running", "retry_pending"})
PREVIEW_FAILED_STATUSES = frozenset({"failed", "cancelled"})
WORK_ITEM_FILTER_STATUSES = frozenset(
    {"pending", "assigned", "in_progress", "submitted", "rejected", "accepted"}
)
WORK_QUEUE_SORT_FIELDS = frozenset({"created_at", "updated_at", "episode_uid", "duration"})
_SORTABLE_NUMBER_PATTERN = r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"


def create_episode_work_item(
    db: Session,
    *,
    episode_id: int,
    kind: str,
    actor_id: int,
) -> WorkItem:
    _require_stage(kind)
    episode = _require_episode(db, episode_id)
    require_workspace_actor(db, actor_id=actor_id, workspace_id=episode.workspace_id)
    if kind == "review":
        raise ValueError("review work is created only by submitting cut or annotation work")
    if kind == "annotation" and episode.kind != "derived":
        raise ValueError("annotation work requires a derived episode")
    if kind == "cut" and episode.kind != "source":
        raise ValueError("cut work requires a source episode")
    item = WorkItem(
        workspace_id=episode.workspace_id,
        episode_id=episode.id,
        kind=kind,
        status="pending",
        created_by_user_id=actor_id,
        generation=1,
        version=1,
    )
    db.add(item)
    db.flush()
    return item


def list_work_queue(
    db: Session,
    *,
    workspace_id: int,
    actor_id: int,
    stage: str,
    review_target_kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
    task_set_id: int | None = None,
    task_label_id: int | None = None,
    status: str | None = None,
    assignee_user_id: int | None = None,
    episode_keyword: str | None = None,
    updated_from: datetime | None = None,
    updated_to: datetime | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> dict[str, object]:
    _require_stage(stage)
    if review_target_kind is not None and review_target_kind not in {"cut", "annotation"}:
        raise ValueError("invalid review target kind")
    if review_target_kind is not None and stage != "review":
        raise ValueError("review target filter requires review stage")
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("invalid work queue page")
    if sort_by is not None and sort_by not in WORK_QUEUE_SORT_FIELDS:
        raise ValueError("invalid work queue sort")
    if sort_order is not None and sort_order not in {"asc", "desc"}:
        raise ValueError("invalid work queue sort")
    if status is not None and status not in WORK_ITEM_FILTER_STATUSES:
        raise ValueError("invalid work queue status")
    normalized_keyword = _normalized_episode_keyword(episode_keyword)
    require_workspace_actor(db, actor_id=actor_id, workspace_id=workspace_id)
    if task_set_id is not None:
        task_set = db.get(TaskSet, task_set_id)
        if task_set is None or int(task_set.workspace_id) != int(workspace_id):
            raise PermissionError("task set is outside the requested workspace")
    query = (
        db.query(WorkItem, Episode)
        .options(
            selectinload(Episode.task_label),
            selectinload(Episode.artifacts).selectinload(EpisodeArtifact.operations),
        )
        .join(Episode, Episode.id == WorkItem.episode_id)
        .filter(
            WorkItem.workspace_id == workspace_id,
            Episode.quality_status.in_(QUALITY_READY_STATUSES),
        )
    )
    if task_set_id is not None:
        query = query.filter(Episode.task_set_id == task_set_id)
    if task_label_id is not None:
        query = query.filter(Episode.task_label_id == task_label_id)
    if status is not None:
        query = query.filter(WorkItem.status == status)
    if assignee_user_id is not None:
        query = query.filter(WorkItem.assignee_user_id == assignee_user_id)
    if normalized_keyword:
        query = query.filter(_episode_keyword_filter(normalized_keyword))
    if updated_from is not None:
        query = query.filter(WorkItem.updated_at >= updated_from)
    if updated_to is not None:
        query = query.filter(WorkItem.updated_at <= updated_to)
    if stage == "completed":
        query = query.filter(
            WorkItem.kind == "review",
            WorkItem.review_target_kind == "annotation",
            WorkItem.status == "accepted",
        )
    else:
        terminal_statuses = (
            ("accepted", "cancelled", "stale", "rejected")
            if stage == "review"
            else (
                "accepted",
                "cancelled",
                "stale",
            )
        )
        query = query.filter(
            WorkItem.kind == stage,
            WorkItem.status.notin_(terminal_statuses),
        )
    if stage == "review" and review_target_kind is not None:
        query = query.filter(WorkItem.review_target_kind == review_target_kind)
    total = query.count()
    effective_sort_by = sort_by or ("updated_at" if stage == "completed" else "created_at")
    effective_sort_order = sort_order or ("desc" if stage == "completed" else "asc")
    duration_text = func.jsonb_extract_path_text(Episode.metadata_json, "metrics", "duration_s")
    duration = case(
        (duration_text.op("~")(_SORTABLE_NUMBER_PATTERN), cast(duration_text, Float)),
        else_=None,
    )
    sort_fields = {
        "created_at": WorkItem.created_at,
        "updated_at": WorkItem.updated_at,
        "episode_uid": Episode.episode_uid,
        "duration": duration,
    }
    direction = "asc" if effective_sort_order == "asc" else "desc"
    primary = getattr(sort_fields[effective_sort_by], direction)().nullslast()
    stable = getattr(WorkItem.id, direction)()
    rows = query.order_by(primary, stable).offset(offset).limit(limit).all()
    actor = require_workspace_actor(db, actor_id=actor_id, workspace_id=workspace_id)
    preview_statuses = _preview_statuses(db, episodes=[episode for _item, episode in rows])
    items = [
        _queue_row(
            item, episode, actor=actor, preview_status=preview_statuses.get(episode.id, "ready")
        )
        for item, episode in rows
    ]
    if stage == "completed":
        for row, (_item, episode) in zip(items, rows, strict=True):
            row["publication"] = publication_projection(db, episode=episode, actor_id=actor.id)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _normalized_episode_keyword(value: str | None) -> str:
    if value is None:
        return ""
    keyword = str(value).strip()
    if len(keyword) > 128:
        raise ValueError("work queue episode keyword is too long")
    return keyword


def _escape_like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _episode_keyword_filter(keyword: str):
    """Match only a literal Episode UID fragment plus safe compact identifiers."""
    clauses = [Episode.episode_uid.ilike(f"%{_escape_like_literal(keyword)}%", escape="\\")]
    if re.fullmatch(r"[A-Za-z0-9_\-\s]+", keyword):
        compact = re.sub(r"[_\-\s]+", "", keyword).lower()
        if compact:
            compact_uid = func.lower(func.regexp_replace(Episode.episode_uid, r"[_\s-]+", "", "g"))
            clauses.append(compact_uid.like(f"%{compact}%"))
    return or_(*clauses)


def episode_summary(episode: Episode) -> dict[str, object]:
    return {
        **_episode_projection(episode),
        "quality": _quality_projection(episode),
        "metrics": _metrics_projection(episode),
        "workflow_status": episode.workflow_status,
        "annotation_status": episode.annotation_status,
        "review_status": episode.review_status,
    }


def work_item_summary(db: Session, *, item: WorkItem, actor_id: int) -> dict[str, object]:
    episode = _require_episode(db, item.episode_id)
    actor = require_workspace_actor(db, actor_id=actor_id, workspace_id=item.workspace_id)
    return _queue_row(
        item, episode, actor=actor, preview_status=_preview_status_for_episode(db, episode)
    )


def publication_summary(db: Session, *, episode: Episode, actor_id: int) -> dict[str, object]:
    return publication_projection(db, episode=episode, actor_id=actor_id)


def claim_work_item(db: Session, *, work_item_id: int, actor_id: int) -> WorkItem:
    item, actor = _locked_item_and_actor(db, work_item_id=work_item_id, actor_id=actor_id)
    if item.status not in {"pending", "rejected"}:
        raise ValueError("work item cannot be claimed")
    if item.status == "rejected" and item.kind == "review":
        raise ValueError("terminal review work item cannot be claimed")
    _require_worker_role(actor, item.kind)
    _require_episode_preview_ready(db, item)
    item.status = "assigned"
    item.assignee_user_id = actor.id
    _bump(item)
    db.flush()
    return item


def continue_work_item(db: Session, *, work_item_id: int, actor_id: int) -> WorkItem:
    item, actor = _locked_item_and_actor(db, work_item_id=work_item_id, actor_id=actor_id)
    _require_assignee(item, actor)
    if item.status == "in_progress":
        return item
    if item.status != "assigned":
        raise ValueError("work item cannot be continued")
    _require_episode_preview_ready(db, item)
    item.status = "in_progress"
    _bump(item)
    db.flush()
    return item


def release_work_item(db: Session, *, work_item_id: int, actor_id: int, note: str = "") -> WorkItem:
    item, actor = _locked_item_and_actor(db, work_item_id=work_item_id, actor_id=actor_id)
    if item.status not in {"assigned", "in_progress", "rejected"}:
        raise ValueError("submitted or terminal work item cannot be released")
    if item.assignee_user_id != actor.id:
        if actor.role != "admin" or not note.strip():
            raise PermissionError("only the assignee can release this work item")
    item.status = "pending"
    item.assignee_user_id = None
    item.note = note.strip()[:2000]
    _bump(item)
    db.flush()
    return item


def submit_work_item(
    db: Session, *, work_item_id: int, actor_id: int, base_version: int
) -> WorkItem:
    """Compatibility service wrapper for the versioned submit contract."""
    _submitted, review = submit_draft(
        db,
        work_item_id=work_item_id,
        actor_id=actor_id,
        base_version=base_version,
    )
    return review


def review_work_item(
    db: Session,
    *,
    work_item_id: int,
    actor_id: int,
    decision: str,
    note: str = "",
    collector_profile_id: int | None | object = None,
) -> JobRun | None:
    kwargs: dict[str, object] = {}
    if collector_profile_id is not None:
        kwargs["collector_profile_id"] = collector_profile_id
    result = review_draft(
        db,
        work_item_id=work_item_id,
        actor_id=actor_id,
        decision=decision,
        note=note,
        **kwargs,
    )
    return result.publication


def _queue_row(
    item: WorkItem, episode: Episode, *, actor: User, preview_status: str
) -> dict[str, object]:
    available_actions = _available_actions(item, actor)
    if preview_status != "ready":
        # A user may still release an already held item while media generation
        # is pending or failed. Claiming and opening the workbench stay blocked.
        available_actions = [action for action in available_actions if action == "release"]
    return {
        "episode": _episode_projection(episode),
        "quality": _quality_projection(episode),
        "metrics": _metrics_projection(episode),
        "preview": {"status": preview_status},
        "work_item": {
            "id": item.id,
            "kind": item.kind,
            "status": item.status,
            "review_target_kind": item.review_target_kind,
            "assignee_user_id": item.assignee_user_id,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            "available_actions": available_actions,
        },
    }


def _episode_projection(episode: Episode) -> dict[str, object]:
    return {
        "id": episode.id,
        "episode_uid": episode.episode_uid,
        "batch_id": episode.batch_id,
        "kind": episode.kind,
        "parent_episode_id": episode.parent_episode_id,
        "modality": episode.modality,
        "task_label": task_label_brief(episode.task_label),
        "scene": episode.scene,
    }


def _quality_projection(episode: Episode) -> dict[str, object]:
    status = "passed" if episode.quality_status == "profiled" else episode.quality_status
    return {"status": status, "restored": status == "recovered"}


def _metrics_projection(episode: Episode) -> dict[str, int | float | None]:
    if episode.quality_status not in QUALITY_READY_STATUSES:
        return {"reference_frame_count": None, "duration_s": None, "average_rgb_rate_hz": None}
    raw = (
        episode.metadata_json.get("metrics", {}) if isinstance(episode.metadata_json, dict) else {}
    )
    return {
        "reference_frame_count": _safe_metric(raw.get("reference_frame_count"), integer=True),
        "duration_s": _safe_metric(raw.get("duration_s")),
        "average_rgb_rate_hz": _safe_metric(raw.get("average_rgb_rate_hz")),
    }


def _safe_metric(value: object, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = int(value) if integer else float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    return numeric


def _available_actions(item: WorkItem, actor: User) -> list[str]:
    return work_item_available_actions(item, actor=actor)


def _preview_statuses(db: Session, *, episodes: list[Episode]) -> dict[int, str]:
    jobs_by_episode_id = latest_preview_jobs_for_episodes(db, episodes=episodes)
    return {
        int(episode.id): _preview_status(
            episode, preview_job=jobs_by_episode_id.get(int(episode.id))
        )
        for episode in episodes
        if episode.id is not None
    }


def _preview_status_for_episode(db: Session, episode: Episode) -> str:
    preview_job = latest_preview_job_for_episode(db, episode=episode)
    return _preview_status(episode, preview_job=preview_job)


def _preview_status(episode: Episode, *, preview_job: JobRun | None) -> str:
    if episode.kind != "derived":
        return "ready"
    for artifact in episode.artifacts:
        if artifact.artifact_type != "process_preview":
            continue
        if any(
            operation.operation_kind == "process_preview_publish"
            and operation.status == "published"
            for operation in artifact.operations
        ):
            return "ready"
    # Older derived Episodes predate durable preview JobRuns. They remain
    # accessible; all new post-cut Episodes receive a preview JobRun.
    if preview_job is None:
        return "ready"
    if preview_job.status in PREVIEW_FAILED_STATUSES:
        return "failed"
    if preview_job.status in PREVIEW_PROCESSING_STATUSES:
        return "processing"
    # A succeeded job without a published artifact is an inconsistent output,
    # never a reason to expose an unusable workbench.
    return "failed"


def _require_episode_preview_ready(db: Session, item: WorkItem) -> None:
    episode = _require_episode(db, item.episode_id)
    status = _preview_status_for_episode(db, episode)
    if status == "processing":
        raise ValueError("episode preview is still processing")
    if status != "ready":
        raise ValueError("episode preview is unavailable")


def _locked_item_and_actor(
    db: Session, *, work_item_id: int, actor_id: int
) -> tuple[WorkItem, User]:
    item = db.query(WorkItem).filter(WorkItem.id == work_item_id).with_for_update().one_or_none()
    if item is None or item.episode_id is None:
        raise ValueError("episode work item does not exist")
    actor = require_workspace_actor(db, actor_id=actor_id, workspace_id=item.workspace_id)
    return item, actor


def _require_episode(db: Session, episode_id: int | None) -> Episode:
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise ValueError("episode does not exist")
    return episode


def _require_stage(kind: str) -> None:
    if kind not in WORK_QUEUE_STAGES:
        raise ValueError("unsupported work queue stage")


def _require_worker_role(actor: User, kind: str) -> None:
    _require_stage(kind)
    if actor.role not in WORKER_ROLES[kind]:
        raise PermissionError("actor role cannot operate this work item")


def _require_assignee(item: WorkItem, actor: User) -> None:
    _require_worker_role(actor, item.kind)
    if item.assignee_user_id != actor.id:
        raise PermissionError("work item is assigned to another user")


def _bump(item: WorkItem) -> None:
    item.version = int(item.version or 0) + 1
    item.updated_at = datetime.utcnow()
