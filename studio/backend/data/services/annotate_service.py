"""Data annotation P0 business logic (clip_descriptions primary structure)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from data.config import settings
from data.database import Annotation, AnnotationRecord, Project, Task, TaskStatus
from data.infra.redis_client import redis_service
from data.integrations.qrdf import list_episodes
from data.integrations.qrdf.topic_preview import (
    infer_task_fps,
    list_available_streams,
    list_episode_rgb_topics,
    resolve_episode_preview_root,
)
from data.schemas.annotate import AnnotateSubmitRequest
from data.schemas.behavior_ai import BehaviorAiSuggestionSource
from data.services.annotate_schema import (
    clip_descriptions_from_doc,
    normalize_submit_segment,
    region_frames_to_clip_descriptions,
    segments_to_annotations,
    validate_segment_keys,
    validate_segment_overlap,
)
from data.services.annotate_tags import (
    DEFAULT_FEATURE_FLAGS,
    TYPE_OPTIONS,
    annotation_item_fields,
    enrich_region_frames_from_segments,
)
from data.services.behavior_ai_capabilities import build_behavior_ai_capability
from data.services.behavior_ai_provenance import (
    BehaviorAiProvenanceError,
    validate_behavior_ai_provenance,
)
from data.services.behavior_annotation_options import build_behavior_annotation_options
from data.services.behavior_tags_service import (
    BehaviorVocabularySnapshot,
    get_active_vocabulary_snapshot,
)
from data.services.state_machine import transit_task
from data.services.task_pipeline import pipeline_key_for_task
from data.utils.formatting import normalize_api_fps

ANNOTATE_CLAIM_STARTED_KEY = "_annotate_claim_started_at"


class AnnotationSubmissionConflict(ValueError):
    """The annotation task was changed while a submit was in flight."""


def claim_annotation_submission(db: Session, task_id: int, *, claimant_id: str) -> Task:
    from data.services.task_lock import task_lock_service

    task = db.scalar(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise KeyError(f"task not found: {task_id}")
    if task.status != TaskStatus.PENDING_ANNOTATE.value:
        raise AnnotationSubmissionConflict("task annotation state changed concurrently; retry")
    task_lock_service.require_claim(task, claimant_id)
    if task.claimed_user_id and task.claimed_user_id != claimant_id:
        raise AnnotationSubmissionConflict("task annotation assignment changed concurrently; retry")

    expected_updated_at = task.updated_at
    revision_condition = (
        Task.updated_at == expected_updated_at
        if expected_updated_at is not None
        else Task.updated_at.is_(None)
    )
    changed = db.execute(
        update(Task)
        .where(
            Task.id == task.id,
            Task.status == TaskStatus.PENDING_ANNOTATE.value,
            revision_condition,
        )
        .values(updated_at=datetime.utcnow()),
        execution_options={"synchronize_session": False},
    )
    if changed.rowcount != 1:
        raise AnnotationSubmissionConflict("task annotation state changed concurrently; retry")
    db.flush()
    db.expire(task)
    db.refresh(task)
    task_lock_service.require_claim(task, claimant_id)
    if task.claimed_user_id and task.claimed_user_id != claimant_id:
        raise AnnotationSubmissionConflict("task annotation assignment changed concurrently; retry")
    return task


def resolve_task_fps(storage: str | None, meta: dict | None = None) -> int:
    """Frame rate for task display and annotation: prioritize metadata, then QRDF calculation, finally ceiling rounding."""
    meta = meta or {}
    if meta.get("preprocess", {}).get("fps"):
        return normalize_api_fps(meta["preprocess"]["fps"])
    return normalize_api_fps(infer_task_fps(storage))


def resolve_task_dataset_storage(task: Task) -> str | None:
    meta = task.metadata_json or {}
    mcap = meta.get("mcap_to_qrdf") or {}
    if mcap.get("storage_path"):
        return mcap["storage_path"]
    preprocess = meta.get("preprocess") or {}
    if preprocess.get("qrdf_dataset_path"):
        return preprocess["qrdf_dataset_path"]
    if task.storage_path and not str(task.storage_path).lower().endswith(".mcap"):
        return task.storage_path
    if mcap.get("qrdf_path"):
        from pathlib import Path

        from data.config import settings

        try:
            return (
                Path(mcap["qrdf_path"])
                .resolve()
                .relative_to(Path(settings.storage_root).resolve())
                .as_posix()
            )
        except ValueError:
            return mcap["qrdf_path"]
    return task.storage_path


def task_quality_level(meta: dict | None) -> str:
    meta = meta or {}
    qc = meta.get("quality_check") or {}
    if isinstance(qc, dict) and qc.get("level"):
        return str(qc["level"])
    pre = meta.get("preprocess") or {}
    return str(pre.get("quality") or "")


def record_claim_started(task: Task, db: Session) -> None:
    meta = dict(task.metadata_json or {})
    meta[ANNOTATE_CLAIM_STARTED_KEY] = datetime.utcnow().isoformat()
    task.metadata_json = meta
    db.commit()


def clear_claim_started(task: Task, db: Session) -> None:
    meta = dict(task.metadata_json or {})
    if ANNOTATE_CLAIM_STARTED_KEY in meta:
        meta.pop(ANNOTATE_CLAIM_STARTED_KEY, None)
        task.metadata_json = meta
        db.commit()


def compute_annotate_duration_sec(task: Task) -> float:
    meta = task.metadata_json or {}
    raw = meta.get(ANNOTATE_CLAIM_STARTED_KEY)
    if not raw:
        return 0.0
    try:
        started = datetime.fromisoformat(str(raw))
        return max(0.0, (datetime.utcnow() - started).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def get_annotate_stats(db: Session, *, user_key: str, annotator_id: int | None) -> dict[str, int]:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    backlog = db.query(Task).filter(Task.status == "pending_annotate").count()
    mine_in_progress = (
        db.query(Task)
        .filter(Task.status == "pending_annotate", Task.claimed_by == user_key)
        .count()
    )
    today_q = db.query(AnnotationRecord).filter(AnnotationRecord.submitted_at >= today_start)
    if annotator_id is not None:
        today_q = today_q.filter(AnnotationRecord.annotator_id == annotator_id)
    today_completed = today_q.count()
    return {
        "backlog": backlog,
        "today_completed": today_completed,
        "mine_in_progress": mine_in_progress,
    }


def resolve_scene(task: Task, project: Project | None, meta: dict) -> str:
    pre = meta.get("preprocess") or {}
    if pre.get("scene"):
        return pre["scene"]
    if project and project.scene:
        return project.scene
    if task.name:
        return task.name
    return task.data_source or "演示数据"


def resolve_episode_task_language(
    task: Task, meta: dict, annotation_doc: dict | None = None
) -> str:
    doc = annotation_doc or {}
    if doc.get("task"):
        return str(doc["task"])
    episodes = meta.get("episodes") or (meta.get("preprocess") or {}).get("episodes") or []
    if episodes and episodes[0].get("task_language"):
        return str(episodes[0]["task_language"])
    pre = meta.get("preprocess") or {}
    if pre.get("task_language"):
        return str(pre["task_language"])
    return task.name or ""


def get_total_frames(task: Task, storage: str | None) -> int:
    """Episode total frame count (index range [0, total_frames - 1]). Prioritize metadata, then QRDF metrics."""
    meta = task.metadata_json or {}
    episodes = meta.get("episodes") or (meta.get("preprocess") or {}).get("episodes") or []
    if episodes:
        ep = episodes[0]
        if ep.get("frame_count"):
            return int(ep["frame_count"])
        end = ep.get("end_frame")
        if end is not None:
            return int(end) + 1
    if storage:
        listed = list_episodes(storage)
        if listed and listed[0].get("frame_count"):
            live = int(listed[0]["frame_count"])
            if live > 0:
                return live
    return 0


def load_annotation_doc(db: Session, task_id: int, *, task: Task | None = None) -> dict:
    task = task or db.get(Task, task_id)
    # In pending_annotation stage not yet submitted; persistent annotations should only appear in review and later stages; ignore legacy leftovers in SQL.
    if task and task.status == TaskStatus.PENDING_ANNOTATE.value:
        return {}

    ann = (
        db.query(Annotation)
        .filter(Annotation.task_id == task_id)
        .order_by(Annotation.version.desc())
        .first()
    )
    if not ann:
        return {}
    data = ann.data_json or {}
    if isinstance(data, list):
        return {"region_frames": data}
    doc = dict(data)
    if task:
        dataset_ref = doc.get("dataset_ref") or {}
        if dataset_ref and (
            dataset_ref.get("resource_type") != "task"
            or str(dataset_ref.get("resource_id") or "") != str(task.id)
        ):
            return {}
        if not dataset_ref:
            expected = resolve_task_dataset_storage(task) or task.storage_path or ""
            stored = str(doc.get("storage_uri") or "")
            if stored and expected and stored != expected:
                return {}
    return doc


def annotation_action_tags(annotation_doc: Mapping[str, Any]) -> list[str]:
    """Return unique normalized actions from one submitted annotation fact."""
    clip_descriptions = annotation_doc.get("clip_descriptions") or {}
    segments = clip_descriptions.get("segments") or {}
    tags: list[str] = []
    for segment in segments.values():
        action = str(segment.get("action") or "").strip()
        if action and action not in tags:
            tags.append(action)
    return tags


def load_clip_descriptions(
    db: Session,
    task_id: int,
    *,
    vocabulary: BehaviorVocabularySnapshot | None = None,
) -> dict[str, Any]:
    vocabulary = vocabulary or get_active_vocabulary_snapshot(db)
    return clip_descriptions_from_doc(
        load_annotation_doc(db, task_id),
        action_by_key=vocabulary.action_by_key,
        label_by_key=vocabulary.label_by_key,
        label_to_key=vocabulary.label_to_key,
    )


def load_region_frames(
    db: Session,
    task_id: int,
    *,
    vocabulary: BehaviorVocabularySnapshot | None = None,
) -> list[dict]:
    """Transition phase: derive region_frames list from clip_descriptions."""
    vocabulary = vocabulary or get_active_vocabulary_snapshot(db)
    clip = load_clip_descriptions(db, task_id, vocabulary=vocabulary)
    segments = clip.get("segments") or {}
    region_frames: list[dict] = []
    for seg in segments.values():
        fields = annotation_item_fields(
            seg,
            action_by_key=vocabulary.action_by_key,
            label_by_key=vocabulary.label_by_key,
            label_to_key=vocabulary.label_to_key,
        )
        region_frames.append(
            {
                "id": seg.get("id"),
                "start_frame": seg.get("start_frame"),
                "end_frame": seg.get("end_frame"),
                "quality": fields["quality"],
                "success": fields["success"],
                "action": fields["action"],
                "tag": fields["tag"],
                "tag_label": fields["tag_label"],
                "type": fields["type"],
                "subtask": fields["subtask"],
                "remark": fields["subtask"],
            }
        )
    region_frames.sort(key=lambda x: (x.get("start_frame", 0), x.get("end_frame", 0)))
    return region_frames


def to_scheme2_annotations(
    region_frames: list[dict] | None = None,
    clip_descriptions: dict | None = None,
    *,
    vocabulary: BehaviorVocabularySnapshot | None = None,
) -> list[dict]:
    action_by_key = vocabulary.action_by_key if vocabulary else None
    label_by_key = vocabulary.label_by_key if vocabulary else None
    label_to_key = vocabulary.label_to_key if vocabulary else None
    if clip_descriptions and clip_descriptions.get("segments"):
        return segments_to_annotations(
            clip_descriptions["segments"],
            action_by_key=action_by_key,
            label_by_key=label_by_key,
        )
    if region_frames:
        clip = region_frames_to_clip_descriptions(
            region_frames,
            action_by_key=action_by_key,
            label_by_key=label_by_key,
            label_to_key=label_to_key,
        )
        return segments_to_annotations(
            clip["segments"],
            action_by_key=action_by_key,
            label_by_key=label_by_key,
        )
    return []


def get_rf_session(task: Task) -> dict[str, dict[str, int]]:
    return redis_service.get_region_frame_ids(task.id)


def register_rf_id(task: Task, db: Session, rf_id: str, start_frame: int, end_frame: int) -> None:
    _ = db
    redis_service.register_region_frame_id(
        task.id,
        rf_id,
        start_frame=start_frame,
        end_frame=end_frame,
    )


def update_rf_id(task: Task, db: Session, rf_id: str, start_frame: int, end_frame: int) -> None:
    """Synchronize region_frame session frame range after drag/resize (must be registered)."""
    session = get_rf_session(task)
    if rf_id not in session:
        raise ValueError(f"区域帧 {rf_id} 未在标注会话中登记，请先调用 region-frames/id")
    register_rf_id(task, db, rf_id, start_frame, end_frame)


def clear_rf_session(task: Task, db: Session) -> None:
    _ = db
    redis_service.clear_region_frame_session(task.id)


def allocate_region_frame_id() -> str:
    return f"rf_{uuid.uuid4().hex[:8]}"


def validate_submit_segments(
    raw_segments: list[dict],
    *,
    total_frames: int,
    session: dict[str, dict[str, int]],
    action_by_key: Mapping[str, object],
    label_to_key: Mapping[str, str],
) -> dict[str, dict]:
    if not raw_segments:
        raise ValueError("至少提交一个片段")

    segments: dict[str, dict] = {}
    seen_ids: set[str] = set()
    for raw in raw_segments:
        key, seg = normalize_submit_segment(
            raw,
            total_frames=total_frames,
            action_by_key=action_by_key,
            label_to_key=label_to_key,
        )
        rf_id = seg["id"]
        if rf_id in seen_ids:
            raise ValueError(f"重复的片段 id: {rf_id}")
        seen_ids.add(rf_id)
        if rf_id not in session:
            raise ValueError(f"片段 {rf_id} 未在标注会话中登记，请先调用 region-frames/id")
        sess = session[rf_id]
        if (
            sess.get("start_frame") != seg["start_frame"]
            or sess.get("end_frame") != seg["end_frame"]
        ):
            raise ValueError(f"片段 {rf_id} 与会话登记不一致")
        if key in segments:
            raise ValueError(f"重复的 segment 键: {key}")
        segments[key] = seg

    validate_segment_keys(segments)
    validate_segment_overlap(segments)
    return segments


def enrich_segments(
    segments: dict[str, dict],
    fps: float,
    *,
    vocabulary: BehaviorVocabularySnapshot,
) -> dict[str, dict]:
    enriched: dict[str, dict] = {}
    for key, seg in segments.items():
        start = int(seg["start_frame"])
        end = int(seg["end_frame"])
        fields = annotation_item_fields(
            seg,
            action_by_key=vocabulary.action_by_key,
            label_by_key=vocabulary.label_by_key,
            label_to_key=vocabulary.label_to_key,
        )
        enriched[key] = {
            **seg,
            **fields,
            "start_time": round(start / fps, 2) if fps > 0 else 0.0,
            "end_time": round(end / fps, 2) if fps > 0 else 0.0,
        }
    return enriched


def build_workbench_data(
    db: Session,
    task: Task,
    *,
    vocabulary: BehaviorVocabularySnapshot | None = None,
    actor_id: int | None = None,
) -> dict[str, Any]:
    vocabulary = vocabulary or get_active_vocabulary_snapshot(db)
    meta = task.metadata_json or {}
    project = db.get(Project, task.project_id)
    storage = resolve_task_dataset_storage(task)
    fps = resolve_task_fps(storage, meta)

    from data.integrations.qrdf.timeseries import detect_view_toggles

    preview_streams = list_available_streams(
        storage,
        task_id=task.id,
        api_base="/api/v1/annotate",
        generate=True,
    )
    preview_status = (preview_streams[0].get("preview") if preview_streams else {}) or {}
    view_toggles = detect_view_toggles(storage)
    annotation_doc = load_annotation_doc(db, task.id, task=task)
    clip_descriptions = clip_descriptions_from_doc(
        annotation_doc,
        action_by_key=vocabulary.action_by_key,
        label_by_key=vocabulary.label_by_key,
        label_to_key=vocabulary.label_to_key,
    )
    episodes_meta = meta.get("episodes") or (meta.get("preprocess") or {}).get("episodes") or []
    total_frames = get_total_frames(task, storage)
    episodes = episodes_meta if episodes_meta else list_episodes(storage)
    task_language = resolve_episode_task_language(task, meta, annotation_doc)
    pre_fps = (meta.get("preprocess") or {}).get("fps")
    native_fps = (
        round(float(pre_fps), 3)
        if pre_fps
        else (round(float(infer_task_fps(storage)), 3) if storage else None)
    )
    behavior_options = build_behavior_annotation_options(db, vocabulary=vocabulary)
    ai_behavior = _teleop_behavior_ai_capability(task, storage=storage, actor_id=actor_id)

    return {
        "task_id": task.id,
        "scene": resolve_scene(task, project, meta),
        "status": task.status,
        "preview_available": preview_status.get("available", False),
        "preview_error": preview_status.get("error"),
        "fps": fps,
        "native_fps": native_fps,
        "display_fps": fps,
        "task": task_language,
        "clip_descriptions": clip_descriptions,
        "annotations": to_scheme2_annotations(
            clip_descriptions=clip_descriptions,
            vocabulary=vocabulary,
        ),
        "preview_streams": preview_streams,
        "view_toggles": view_toggles,
        "feature_flags": dict(DEFAULT_FEATURE_FLAGS),
        "tags": behavior_options["action_options"],
        "success_options": behavior_options["success_options"],
        "quality_options": behavior_options["quality_options"],
        "behavior_annotation_options": behavior_options,
        "ai_behavior": ai_behavior,
        "type_options": TYPE_OPTIONS,
        "total_frames": total_frames,
        "max_frame_index": max(0, total_frames - 1) if total_frames > 0 else 0,
        "duration_sec": round(total_frames / fps, 2) if fps > 0 and total_frames > 0 else 0,
        "episodes": episodes,
    }


def _default_episode_summary(segments: dict[str, dict], task_language: str) -> dict[str, Any]:
    if not segments:
        return {"success": -1, "quality": 1, "language": task_language}
    successes = [int(s.get("success", -1)) for s in segments.values()]
    qualities = [int(s.get("quality", 1)) for s in segments.values()]
    episode_success = (
        1 if all(s == 1 for s in successes) else (0 if any(s == 0 for s in successes) else -1)
    )
    episode_quality = 1 if all(q == 1 for q in qualities) else 0
    return {
        "success": episode_success,
        "quality": episode_quality,
        "language": task_language,
    }


def persist_submit(
    db: Session,
    task: Task,
    *,
    clip_descriptions: dict,
    annotator_email: str,
    annotator_id: int | None,
    episode_id: str,
    total_frames: int,
    fps: float,
    task_language: str = "",
    episode_summary: dict | None = None,
    duration_sec: float = 0,
    vocabulary: BehaviorVocabularySnapshot,
    ai_suggestion_sources: Sequence[BehaviorAiSuggestionSource] = (),
) -> int:
    segments = clip_descriptions.get("segments") or {}
    action_summary: dict[str, int] = {}
    for seg in segments.values():
        key = seg.get("action", "other")
        action_summary[key] = action_summary.get(key, 0) + 1

    region_frames = enrich_region_frames_from_segments(
        segments,
        fps,
        action_by_key=vocabulary.action_by_key,
        label_by_key=vocabulary.label_by_key,
        label_to_key=vocabulary.label_to_key,
    )

    ann = (
        db.query(Annotation)
        .filter(Annotation.task_id == task.id)
        .order_by(Annotation.version.desc())
        .first()
    )
    version = (ann.version + 1) if ann else 1
    verified_ai_suggestion_sources = _verified_ai_suggestion_sources(
        db,
        task=task,
        annotator_id=annotator_id,
        sources=ai_suggestion_sources,
    )
    payload = {
        "episode_id": episode_id,
        "total_frames": total_frames,
        "fps": fps,
        "task": task_language,
        "clip_descriptions": clip_descriptions,
        "episode_summary": episode_summary,
        "region_frames": region_frames,
        "dataset_ref": {
            "resource_type": "task",
            "resource_id": str(task.id),
        },
    }
    if verified_ai_suggestion_sources:
        payload["ai_suggestion_sources"] = verified_ai_suggestion_sources
    db.add(
        Annotation(
            task_id=task.id,
            data_json=payload,
            version=version,
            updated_by=annotator_email,
        )
    )

    db.add(
        AnnotationRecord(
            task_id=task.id,
            annotator_id=annotator_id,
            segment_count=len(segments),
            action_summary=action_summary,
            duration_sec=int(duration_sec),
            source="human",
        )
    )
    return version


def _teleop_behavior_ai_capability(
    task: Task,
    *,
    storage: str | None,
    actor_id: int | None,
) -> dict[str, object]:
    enabled = settings.behavior_ai_can_call_provider
    if not enabled:
        return build_behavior_ai_capability(
            enabled=False, eligible=False, disabled_reason="ai_disabled"
        )
    if (
        pipeline_key_for_task(task) != "teleop_v1"
        or task.status != TaskStatus.PENDING_ANNOTATE.value
    ):
        return build_behavior_ai_capability(
            enabled=True, eligible=False, disabled_reason="ai_target_not_eligible"
        )
    if actor_id is None or str(task.claimed_user_id or "") != str(actor_id):
        return build_behavior_ai_capability(
            enabled=True, eligible=False, disabled_reason="ai_target_not_claimed"
        )
    resolved = resolve_episode_preview_root(storage)
    if resolved is None:
        return build_behavior_ai_capability(
            enabled=True, eligible=False, disabled_reason="ai_preview_unavailable"
        )
    episode_id, _preview_root = resolved
    topics = list_episode_rgb_topics(storage, episode_id)
    return build_behavior_ai_capability(enabled=True, eligible=bool(topics), rgb_topics=topics)


def submit_annotations(
    db: Session,
    task: Task,
    body: AnnotateSubmitRequest,
    *,
    operator: str,
    annotator_id: int | None,
    claimant_id: str,
) -> dict[str, Any]:
    task = claim_annotation_submission(db, task.id, claimant_id=claimant_id)

    vocabulary = get_active_vocabulary_snapshot(db)

    storage = resolve_task_dataset_storage(task)
    total_frames = get_total_frames(task, storage)
    fps = float(resolve_task_fps(storage, task.metadata_json or {}))
    session = get_rf_session(task)
    raw_segments = body.resolved_segment_items()
    segments = validate_submit_segments(
        raw_segments,
        total_frames=total_frames,
        session=session,
        action_by_key=vocabulary.action_by_key,
        label_to_key=vocabulary.label_to_key,
    )
    enriched_segments = enrich_segments(segments, fps, vocabulary=vocabulary)
    clip_descriptions = {"segments": enriched_segments}

    meta = task.metadata_json or {}
    episodes = (
        meta.get("episodes")
        or (meta.get("preprocess") or {}).get("episodes")
        or list_episodes(storage)
    )
    episode_id = body.episode_id or (
        episodes[0].get("episode_id", "episode_000001") if episodes else "episode_000001"
    )
    task_language = body.task or resolve_episode_task_language(task, meta)
    episode_summary = (
        dict(body.episode_summary)
        if body.episode_summary
        else _default_episode_summary(enriched_segments, task_language)
    )
    duration_sec = compute_annotate_duration_sec(task)

    persist_submit(
        db,
        task,
        clip_descriptions=clip_descriptions,
        annotator_email=operator,
        annotator_id=annotator_id,
        episode_id=episode_id,
        total_frames=total_frames,
        fps=fps,
        task_language=task_language,
        episode_summary=episode_summary,
        duration_sec=duration_sec,
        vocabulary=vocabulary,
        ai_suggestion_sources=body.ai_suggestion_sources,
    )

    task = transit_task(db, task, "annotate_done", operator)
    task = transit_task(db, task, "submit_audit", operator)
    clear_rf_session(task, db)
    clear_claim_started(task, db)
    task_lock_release(db, task)

    next_task = (
        db.query(Task)
        .filter(Task.status == "pending_annotate")
        .order_by(Task.created_at.asc())
        .first()
    )

    annotations = segments_to_annotations(
        enriched_segments,
        action_by_key=vocabulary.action_by_key,
        label_by_key=vocabulary.label_by_key,
    )
    region_frames = enrich_region_frames_from_segments(
        enriched_segments,
        fps,
        action_by_key=vocabulary.action_by_key,
        label_by_key=vocabulary.label_by_key,
        label_to_key=vocabulary.label_to_key,
    )

    return {
        "task_id": task.id,
        "next_status": task.status,
        "clip_descriptions": clip_descriptions,
        "annotations": annotations,
        "region_frames": region_frames,
        "next_task_id": next_task.id if next_task else None,
        "duration_sec": duration_sec,
    }


def _verified_ai_suggestion_sources(
    db: Session,
    *,
    task: Task,
    annotator_id: int | None,
    sources: Sequence[BehaviorAiSuggestionSource],
) -> list[dict[str, object]]:
    if not sources:
        return []
    if task.workspace_id is None or annotator_id is None:
        raise BehaviorAiProvenanceError()
    return validate_behavior_ai_provenance(
        db,
        sources=sources,
        resource_type="task",
        resource_id=str(task.id),
        workspace_id=task.workspace_id,
        actor_id=annotator_id,
    )


def release_annotate_task(db: Session, task: Task, user_id: str) -> None:
    from data.services.task_lock import task_lock_service

    task_lock_service.release(db, task, user_id)
    clear_rf_session(task, db)
    clear_claim_started(task, db)
    task.claimed_user_id = ""
    db.commit()


def task_lock_release(db: Session, task: Task) -> None:
    redis_service.release_lock(str(task.id))
    task.claimed_by = None
    task.claim_token = None
    task.claim_expire_at = None
    db.commit()


def find_next_annotate_task(db: Session, *, exclude_task_id: int | None = None) -> Task | None:
    q = db.query(Task).filter(Task.status == "pending_annotate")
    if exclude_task_id:
        q = q.filter(Task.id != exclude_task_id)
    return q.order_by(Task.created_at.asc()).first()
