"""Preprocessing job core logic (shared between Celery and threads), driven by QRDF SDK."""

import logging
from datetime import datetime
from pathlib import Path

from data.database import SessionLocal, Task
from data.infra.redis_client import redis_service
from data.integrations.qrdf import run_preprocess_pipeline
from data.services.cloud_storage import (
    cleanup_cloud_mirror,
    cleanup_local_staging,
    cleanup_task_previews,
    cleanup_task_terminal_storage,
    cleanup_zip_extract_staging,
    collect_preprocess_uris,
    collect_raw_stage_uris,
    materialize_for_processing,
    new_process_run_id,
    publish_preprocess_output,
)
from data.services.preprocess_facts import (
    sanitize_episode_facts,
    sanitize_preprocess_fact,
    sanitize_topic_facts,
)
from data.services.quality_facts import sanitize_quality_fact
from data.services.state_machine import auto_advance_after_preprocess, ensure_ready_for_preprocess
from data.utils.formatting import format_api_datetime

logger = logging.getLogger(__name__)
_RUN_OWNED_FACTS = frozenset({"preprocess", "episodes", "topics", "quality_check"})


def _is_cancelled(task_id: int, task: Task | None = None) -> bool:
    if redis_service.is_preprocess_cancelled(task_id):
        return True
    if task:
        meta = task.metadata_json or {}
        if meta.get("preprocess", {}).get("status") == "terminated":
            return True
    return False


def _mark_preprocess_failed(
    db,
    task: Task,
    operator: str,
    message: str,
    *,
    code: str = "preprocess_failed",
    quality_check: object = None,
) -> None:
    meta = dict(task.metadata_json or {})
    preprocess = sanitize_preprocess_fact(
        {
            **(meta.get("preprocess") or {}),
            "status": "failed",
            "code": code,
            "message": message,
            "finished_at": format_api_datetime(datetime.utcnow()),
        }
    )
    if "message" not in preprocess:
        preprocess["message"] = "预处理执行失败"
    meta = _replace_run_owned_facts(
        meta,
        preprocess=preprocess,
        quality_check=quality_check,
    )
    task.metadata_json = meta
    db.commit()
    try:
        from data.database import TaskStatus
        from data.services.state_machine import transit_task

        if TaskStatus(task.status) not in (
            TaskStatus.PENDING_ANNOTATE,
            TaskStatus.ANNOTATE_DONE,
            TaskStatus.PENDING_AUDIT,
            TaskStatus.AUDIT_PASSED,
            TaskStatus.STORAGE_READY,
            TaskStatus.FAILED,
        ):
            task = transit_task(db, task, "fail", operator, preprocess["message"])
    except ValueError:
        db.commit()
    _cache_status(
        task.id,
        task.status,
        preprocess,
        meta["quality_check"],
    )


def _persist_running_step(task_id: int, task_status: str, preprocess: dict) -> None:
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            return
        safe_preprocess = sanitize_preprocess_fact(preprocess)
        meta = _replace_run_owned_facts(
            task.metadata_json or {},
            preprocess=safe_preprocess,
        )
        task.metadata_json = meta
        db.commit()
        _cache_status(task_id, task_status, safe_preprocess, meta.get("quality_check", {}))
    finally:
        db.close()


def _replace_run_owned_facts(
    meta: dict,
    *,
    preprocess: object,
    episodes: object = None,
    topics: object = None,
    quality_check: object = None,
) -> dict:
    result = {key: value for key, value in meta.items() if key not in _RUN_OWNED_FACTS}
    result["preprocess"] = sanitize_preprocess_fact(preprocess)
    result["episodes"] = sanitize_episode_facts(episodes)
    result["topics"] = sanitize_topic_facts(topics)
    result["quality_check"] = sanitize_quality_fact(quality_check)
    return result


def _replace_pipeline_metadata_facts(meta: dict, pipeline: dict) -> dict:
    return _replace_run_owned_facts(
        meta,
        preprocess=meta.get("preprocess"),
        episodes=pipeline.get("episodes"),
        topics=pipeline.get("topics"),
        quality_check=pipeline.get("quality"),
    )


def run_preprocess(task_id: int, operator: str = "system") -> None:
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            return
        if _is_cancelled(task_id, task):
            return

        task = ensure_ready_for_preprocess(db, task, operator)
        db.refresh(task)

        # Clean up previous process mirror and preview cache before retrying preprocessing
        try:
            cleanup_cloud_mirror(*collect_preprocess_uris(task))
            cleanup_task_previews(task.id)
        except Exception:
            pass

        run_id = new_process_run_id(task_id)
        meta = dict(task.metadata_json or {})
        previous_preprocess = sanitize_preprocess_fact(meta.get("preprocess"))
        preprocess = sanitize_preprocess_fact(
            {
                "fps": previous_preprocess.get("fps"),
                "status": "running",
                "progress": 0,
                "steps": [],
                "run_id": run_id,
                "started_at": format_api_datetime(datetime.utcnow()),
            }
        )
        meta = _replace_run_owned_facts(meta, preprocess=preprocess)
        task.metadata_json = meta
        db.commit()
        task_status = task.status
        storage_path = task.storage_path
        _ws_id = task.workspace_id
        _proj_id = task.project_id
        _cache_status(task_id, task_status, preprocess, {})
        db.close()
        db = None

        def on_step(step: dict) -> None:
            if _is_cancelled(task_id):
                return
            preprocess["steps"] = list(preprocess.get("steps", [])) + [step]
            preprocess["progress"] = step.get("progress", preprocess.get("progress", 0))
            preprocess["message"] = step.get("message", "")
            _persist_running_step(task_id, task_status, preprocess)

        # Materialize cloud path to local first; SDK four steps execute atomically locally
        with materialize_for_processing(storage_path) as local_path:
            pipeline = run_preprocess_pipeline(
                str(local_path) if local_path else None,
                source_uri=storage_path,
                on_step=on_step,
                cancelled=lambda: _is_cancelled(task_id),
            )
            # Preprocessing results must be uploaded to cloud within the with-block (temp dir cleaned up upon exit)
            _dataset_local = pipeline.get("dataset_path")
            _cloud_uri = None
            if _dataset_local:
                _cloud_uri = publish_preprocess_output(
                    Path(_dataset_local),
                    task_id=task_id,
                    workspace_id=_ws_id,
                    project_id=_proj_id,
                    run_id=run_id,
                )
                cleanup_local_staging(_dataset_local)

        if pipeline.get("cancelled") or _is_cancelled(task_id):
            db = SessionLocal()
            try:
                task = db.get(Task, task_id)
                if task:
                    cleanup_task_terminal_storage(task)
                    if _cloud_uri:
                        cleanup_cloud_mirror(_cloud_uri)
            finally:
                db.close()
            return

        db = SessionLocal()
        task = db.get(Task, task_id)
        if not task:
            return
        if _is_cancelled(task_id, task):
            return

        # Explicit failure when QRDF is not resolved (including full zip / single episode failed to promote); keep raw mirror for retry
        if not pipeline.get("dataset_path") and not _cloud_uri:
            anomalies = pipeline.get("anomalies") or []
            msg = anomalies[0] if anomalies else "未找到可处理的 QRDF 数据集"
            meta = dict(task.metadata_json or {})
            failed_preprocess = sanitize_preprocess_fact(
                {
                    **(meta.get("preprocess") or {}),
                    "status": "failed",
                    "code": "preprocess_failed",
                    "progress": 100,
                    "steps": pipeline.get("steps") or [],
                    "anomalies": anomalies,
                    "message": msg,
                    "cleaning": pipeline.get("cleaning") or {},
                    "aligning_stats": pipeline.get("aligning_stats") or {},
                    "finished_at": format_api_datetime(datetime.utcnow()),
                }
            )
            if "message" not in failed_preprocess:
                failed_preprocess["message"] = next(
                    iter(failed_preprocess.get("anomalies") or []),
                    "预处理执行失败",
                )
            quality = sanitize_quality_fact(pipeline.get("quality"))
            meta = _replace_run_owned_facts(
                meta,
                preprocess=failed_preprocess,
                quality_check=quality,
            )
            task.metadata_json = meta
            db.commit()
            _mark_preprocess_failed(
                db,
                task,
                operator,
                failed_preprocess["message"],
                quality_check=quality,
            )
            redis_service.clear_preprocess_job(task_id)
            return

        meta = _replace_pipeline_metadata_facts(task.metadata_json or {}, pipeline)

        quality = meta["quality_check"]

        sdk_steps = pipeline["steps"]
        cloud_uri = _cloud_uri

        preprocess = sanitize_preprocess_fact(
            {
                **meta.get("preprocess", {}),
                "status": "done",
                "progress": 100,
                "steps": sdk_steps,
                "quality": quality.get("level", "unknown"),
                "quality_score": quality.get("score", 0.0),
                "message": sdk_steps[-1]["message"] if sdk_steps else "预处理完成",
                "finished_at": format_api_datetime(datetime.utcnow()),
                "dataset_ref": {
                    "resource_type": "task",
                    "resource_id": str(task_id),
                    "stage": "process",
                    "run_id": run_id,
                },
                "run_id": run_id,
                "anomalies": pipeline.get("anomalies", []),
                "cleaning": pipeline.get("cleaning", {}),
                "aligning_stats": pipeline.get("aligning_stats", {}),
            }
        )
        if "message" not in preprocess:
            preprocess["message"] = "预处理完成"
        meta["preprocess"] = preprocess
        task.metadata_json = meta
        if cloud_uri:
            task.storage_path = cloud_uri
        db.commit()

        # Before entering annotation, raw is no longer used downstream; release local mirror and zip extract cache early.
        # Without real OSS, cleanup_cloud_mirror automatically skips to avoid deleting canonical copy.
        raw_uris = collect_raw_stage_uris(task)
        if raw_uris:
            cleanup_cloud_mirror(*raw_uris)
            cleanup_zip_extract_staging(*raw_uris)

        _cache_status(task_id, task.status, preprocess, quality)

        from data.integrations.qrdf.modality import ensure_modality_json
        from data.services.annotate_service import resolve_task_fps

        modality_source = cloud_uri or _dataset_local or task.storage_path
        with materialize_for_processing(modality_source) as dataset_path:
            if dataset_path:
                fps = resolve_task_fps(str(dataset_path), meta)
                ensure_modality_json(str(dataset_path), fps=float(fps))

        auto_advance_after_preprocess(db, task, operator)
        task = db.get(Task, task_id)
        if task:
            _cache_status(task_id, task.status, preprocess, quality)
        redis_service.clear_preprocess_job(task_id)
    except Exception as exc:
        logger.error(
            "preprocess execution failed code=%s task_id=%s error_type=%s",
            "preprocess_exception",
            task_id,
            type(exc).__name__,
        )
        if db is None:
            db = SessionLocal()
        task = db.get(Task, task_id)
        if task:
            _mark_preprocess_failed(
                db,
                task,
                operator,
                "预处理执行失败",
                code="preprocess_exception",
            )
        redis_service.clear_preprocess_job(task_id)
        raise
    finally:
        if db is not None:
            db.close()


def _cache_status(task_id: int, status: str, preprocess: dict, quality_check: dict) -> None:
    redis_service.set_task_status(
        task_id,
        {
            "status": status,
            "preprocess": sanitize_preprocess_fact(preprocess),
            "quality_check": sanitize_quality_fact(quality_check),
        },
    )
